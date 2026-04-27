"""View-layer tests focused on cross-merchant scoping and header validation.

The take-home does not require auth, but every endpoint is scoped by the
``X-Merchant-Id`` header. These tests pin that scoping so a future refactor
cannot accidentally leak one merchant's data to another.
"""

import uuid

import pytest
from rest_framework.test import APIClient

from merchants.models import BankAccount, LedgerEntry, Merchant
from payouts.models import Payout


def _seed(*, email: str):
    merchant = Merchant.objects.create(name="Test", email=email)
    LedgerEntry.objects.create(
        merchant=merchant,
        amount_paise=100_000,
        entry_type=LedgerEntry.EntryType.CREDIT,
    )
    bank = BankAccount.objects.create(
        merchant=merchant,
        holder_name="Test Holder",
        account_number_last4="1234",
        ifsc="HDFC0000123",
    )
    return merchant, bank


@pytest.mark.django_db
def test_get_payout_detail_is_scoped_to_merchant():
    """Merchant A creates a payout; merchant B must not be able to read it."""
    merchant_a, bank_a = _seed(email="a@example.com")
    merchant_b, _ = _seed(email="b@example.com")
    client = APIClient()

    r_create = client.post(
        "/api/v1/payouts",
        {"amount_paise": 50_000, "bank_account_id": bank_a.id},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        HTTP_X_MERCHANT_ID=str(merchant_a.id),
    )
    assert r_create.status_code == 201
    payout_id = r_create.json()["id"]

    # Merchant A can see it.
    r_a = client.get(
        f"/api/v1/payouts/{payout_id}",
        HTTP_X_MERCHANT_ID=str(merchant_a.id),
    )
    assert r_a.status_code == 200
    assert r_a.json()["id"] == payout_id

    # Merchant B must NOT see it. 404, not 200; not 403 either — we don't
    # disclose existence across tenants.
    r_b = client.get(
        f"/api/v1/payouts/{payout_id}",
        HTTP_X_MERCHANT_ID=str(merchant_b.id),
    )
    assert r_b.status_code == 404


@pytest.mark.django_db
def test_get_payout_list_is_scoped_to_merchant():
    """GET /payouts must only return payouts for the calling merchant."""
    merchant_a, bank_a = _seed(email="list-a@example.com")
    merchant_b, bank_b = _seed(email="list-b@example.com")
    client = APIClient()

    for _ in range(2):
        client.post(
            "/api/v1/payouts",
            {"amount_paise": 1_000, "bank_account_id": bank_a.id},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
            HTTP_X_MERCHANT_ID=str(merchant_a.id),
        )
    client.post(
        "/api/v1/payouts",
        {"amount_paise": 1_000, "bank_account_id": bank_b.id},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        HTTP_X_MERCHANT_ID=str(merchant_b.id),
    )

    r_a = client.get(
        "/api/v1/payouts", HTTP_X_MERCHANT_ID=str(merchant_a.id)
    )
    r_b = client.get(
        "/api/v1/payouts", HTTP_X_MERCHANT_ID=str(merchant_b.id)
    )

    assert r_a.status_code == 200
    assert r_b.status_code == 200
    assert len(r_a.json()["results"]) == 2
    assert len(r_b.json()["results"]) == 1
    a_ids = {p["id"] for p in r_a.json()["results"]}
    b_ids = {p["id"] for p in r_b.json()["results"]}
    assert a_ids.isdisjoint(b_ids), "merchant A and B results must not overlap"


@pytest.mark.django_db
def test_post_without_idempotency_key_header_is_rejected():
    merchant, bank = _seed(email="no-key@example.com")
    client = APIClient()
    r = client.post(
        "/api/v1/payouts",
        {"amount_paise": 1_000, "bank_account_id": bank.id},
        format="json",
        HTTP_X_MERCHANT_ID=str(merchant.id),
    )
    assert r.status_code == 400


@pytest.mark.django_db
def test_post_without_merchant_id_header_is_rejected():
    client = APIClient()
    r = client.post(
        "/api/v1/payouts",
        {"amount_paise": 1_000, "bank_account_id": 1},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    )
    assert r.status_code == 400
