"""Tests for the merchant dashboard read endpoints.

These are the GET endpoints powering the React dashboard. Coverage focuses
on the response shape, the held-balance calculation (the only non-trivial
endpoint), and 404 handling.
"""

import uuid

import pytest
from rest_framework.test import APIClient

from merchants.models import BankAccount, LedgerEntry, Merchant
from payouts.models import Payout


def _seed_merchant(*, balance_paise: int, email: str) -> tuple[Merchant, BankAccount]:
    m = Merchant.objects.create(name="Dash Test", email=email)
    LedgerEntry.objects.create(
        merchant=m,
        amount_paise=balance_paise,
        entry_type=LedgerEntry.EntryType.CREDIT,
        description="seed credit",
    )
    bank = BankAccount.objects.create(
        merchant=m,
        holder_name="Dash Test",
        account_number_last4="0001",
        ifsc="HDFC0001234",
        nickname="primary",
        is_default=True,
    )
    return m, bank


@pytest.mark.django_db
def test_merchants_list_returns_seeded_rows():
    m1, _ = _seed_merchant(balance_paise=10_000, email="ml-1@example.com")
    m2, _ = _seed_merchant(balance_paise=20_000, email="ml-2@example.com")
    client = APIClient()

    r = client.get("/api/v1/merchants")
    assert r.status_code == 200
    ids = {row["id"] for row in r.json()["results"]}
    assert m1.id in ids
    assert m2.id in ids


@pytest.mark.django_db
def test_balance_endpoint_returns_available_and_held():
    m, _ = _seed_merchant(balance_paise=100_000, email="bal-basic@example.com")
    client = APIClient()
    r = client.get(f"/api/v1/merchants/{m.id}/balance")
    assert r.status_code == 200
    body = r.json()
    assert body["merchant_id"] == m.id
    assert body["available_paise"] == 100_000
    assert body["held_paise"] == 0


@pytest.mark.django_db
def test_balance_held_reflects_pending_payouts():
    """Held balance is SUM(payout.amount_paise) over PENDING + PROCESSING
    payouts. Available is unaffected by held — they're independent
    informational views."""
    m, bank = _seed_merchant(balance_paise=100_000, email="bal-held@example.com")
    # Two pending payouts directly (skip the API to keep this test focused).
    Payout.objects.create(
        merchant=m,
        bank_account=bank,
        amount_paise=20_000,
        status=Payout.Status.PENDING,
    )
    Payout.objects.create(
        merchant=m,
        bank_account=bank,
        amount_paise=15_000,
        status=Payout.Status.PROCESSING,
    )
    # COMPLETED + FAILED must NOT count toward held.
    Payout.objects.create(
        merchant=m,
        bank_account=bank,
        amount_paise=999_999,
        status=Payout.Status.COMPLETED,
    )
    Payout.objects.create(
        merchant=m,
        bank_account=bank,
        amount_paise=999_999,
        status=Payout.Status.FAILED,
    )

    client = APIClient()
    r = client.get(f"/api/v1/merchants/{m.id}/balance")
    assert r.status_code == 200
    body = r.json()
    assert body["held_paise"] == 35_000  # 20_000 + 15_000


@pytest.mark.django_db
def test_balance_for_nonexistent_merchant_returns_404():
    client = APIClient()
    r = client.get("/api/v1/merchants/9999999/balance")
    assert r.status_code == 404


@pytest.mark.django_db
def test_ledger_endpoint_returns_recent_entries_newest_first():
    m, _ = _seed_merchant(balance_paise=10_000, email="led-order@example.com")
    LedgerEntry.objects.create(
        merchant=m,
        amount_paise=5_000,
        entry_type=LedgerEntry.EntryType.CREDIT,
        description="second credit",
    )
    LedgerEntry.objects.create(
        merchant=m,
        amount_paise=3_000,
        entry_type=LedgerEntry.EntryType.CREDIT,
        description="third credit",
    )
    client = APIClient()
    r = client.get(f"/api/v1/merchants/{m.id}/ledger")
    assert r.status_code == 200
    rows = r.json()["results"]
    assert len(rows) == 3
    # Newest first.
    assert rows[0]["description"] == "third credit"
    assert rows[1]["description"] == "second credit"


@pytest.mark.django_db
def test_bank_accounts_endpoint_returns_default_first():
    m, _ = _seed_merchant(balance_paise=10_000, email="bank-order@example.com")
    BankAccount.objects.create(
        merchant=m,
        holder_name="Dash Test",
        account_number_last4="0002",
        ifsc="ICIC0001111",
        is_default=False,
    )
    client = APIClient()
    r = client.get(f"/api/v1/merchants/{m.id}/bank-accounts")
    assert r.status_code == 200
    rows = r.json()["results"]
    assert len(rows) == 2
    # is_default=True comes first per the ``-is_default`` ordering in the view.
    assert rows[0]["is_default"] is True


@pytest.mark.django_db
def test_ledger_for_nonexistent_merchant_returns_404():
    client = APIClient()
    r = client.get("/api/v1/merchants/9999999/ledger")
    assert r.status_code == 404
