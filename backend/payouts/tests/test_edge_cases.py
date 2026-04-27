"""Boundary and validation edge-case tests for the payout API."""

import uuid
from typing import Tuple

import pytest
from django.db.models import Sum
from rest_framework.test import APIClient

from merchants.models import BankAccount, LedgerEntry, Merchant
from payouts import tasks
from payouts.models import Payout


def _seed(*, balance_paise: int, email: str) -> Tuple[Merchant, BankAccount]:
    merchant = Merchant.objects.create(name="Edge", email=email)
    LedgerEntry.objects.create(
        merchant=merchant,
        amount_paise=balance_paise,
        entry_type=LedgerEntry.EntryType.CREDIT,
        description="seed",
    )
    bank = BankAccount.objects.create(
        merchant=merchant,
        holder_name="Edge",
        account_number_last4="9999",
        ifsc="HDFC0000999",
    )
    return merchant, bank


def _post(client: APIClient, merchant_id: int, body: dict, key=None):
    return client.post(
        "/api/v1/payouts",
        body,
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(key or uuid.uuid4()),
        HTTP_X_MERCHANT_ID=str(merchant_id),
    )


@pytest.mark.django_db
def test_payout_of_exact_balance_succeeds_and_zeros_available():
    """A payout of exactly the available balance must succeed and bring
    available to zero. Validates the ``<`` vs ``<=`` boundary in the
    insufficient-funds check.
    """
    merchant, bank = _seed(balance_paise=12_345, email="boundary@example.com")
    client = APIClient()

    r = _post(
        client,
        merchant.id,
        {"amount_paise": 12_345, "bank_account_id": bank.id},
    )
    assert r.status_code == 201, r.json()

    available = (
        LedgerEntry.objects.filter(merchant=merchant)
        .aggregate(total=Sum("amount_paise"))["total"]
    )
    assert available == 0


@pytest.mark.django_db
def test_payout_one_paisa_over_balance_is_rejected():
    merchant, bank = _seed(balance_paise=12_345, email="overdraft@example.com")
    client = APIClient()

    r = _post(
        client,
        merchant.id,
        {"amount_paise": 12_346, "bank_account_id": bank.id},
    )
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "insufficient_funds"
    assert body["available_paise"] == 12_345
    assert body["requested_paise"] == 12_346


@pytest.mark.django_db
def test_zero_amount_payout_is_rejected_at_serializer():
    merchant, bank = _seed(balance_paise=10_000, email="zero-amt@example.com")
    client = APIClient()

    r = _post(
        client, merchant.id, {"amount_paise": 0, "bank_account_id": bank.id}
    )
    assert r.status_code == 400  # serializer validation


@pytest.mark.django_db
def test_negative_amount_payout_is_rejected_at_serializer():
    merchant, bank = _seed(balance_paise=10_000, email="neg-amt@example.com")
    client = APIClient()

    r = _post(
        client, merchant.id, {"amount_paise": -1, "bank_account_id": bank.id}
    )
    assert r.status_code == 400


@pytest.mark.django_db
def test_bank_account_belonging_to_other_merchant_returns_404():
    """Cross-tenant scoping at the create-payout layer.

    The view filters bank account by ``(id, merchant=...)`` so a merchant
    cannot drain to another merchant's bank account by guessing an id.
    """
    merchant_a, bank_a = _seed(balance_paise=100_000, email="a@cross.example")
    merchant_b, _ = _seed(balance_paise=100_000, email="b@cross.example")
    client = APIClient()

    r = _post(
        client,
        merchant_b.id,
        {"amount_paise": 1_000, "bank_account_id": bank_a.id},
    )
    assert r.status_code == 404
    assert r.json()["error"] == "bank_account_not_found"


@pytest.mark.django_db
def test_successful_payout_preserves_balance_invariant(monkeypatch):
    """Mirror of the failure-cycle invariant test, but for the success path:
    seed credit (+100k) -> debit (-50k) -> COMPLETED; SUM = 50k.

    Exercise the full process_payout flow with the simulation forced to
    success.
    """
    merchant, bank = _seed(balance_paise=100_000, email="success-inv@example.com")
    monkeypatch.setattr(tasks, "_simulate_bank", lambda: "success")
    client = APIClient()

    r = _post(
        client,
        merchant.id,
        {"amount_paise": 50_000, "bank_account_id": bank.id},
    )
    assert r.status_code == 201
    payout_id = r.json()["id"]
    # ``transaction.on_commit`` is a no-op inside the rolled-back test
    # transaction wrapped by ``django_db``, so we drive the worker by
    # hand. In production the hook fires it automatically.
    tasks.process_payout(payout_id)

    payout = Payout.objects.get(id=payout_id)
    assert payout.status == Payout.Status.COMPLETED

    final = (
        LedgerEntry.objects.filter(merchant=merchant)
        .aggregate(total=Sum("amount_paise"))["total"]
    )
    assert final == 50_000  # 100k - 50k = 50k, no refund on success
    # No REFUND entry — that would indicate a bug in the success path.
    assert not LedgerEntry.objects.filter(
        merchant=merchant, entry_type=LedgerEntry.EntryType.REFUND
    ).exists()
