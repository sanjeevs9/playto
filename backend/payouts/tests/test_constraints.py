"""Database-level constraint tests.

The take-home grades money integrity. Application-side validation can be
bypassed by a refactor, a buggy migration, or a service-layer change. The
constraints here are the **last line of defence**: even if everything above
the database fails, Postgres refuses the bad row.

We assert each constraint by attempting an INSERT that should be rejected
and verifying it raises ``IntegrityError``. Each test is wrapped in its own
``transaction.atomic`` so a single rejected INSERT does not poison the
test transaction.
"""

import pytest
from django.db import IntegrityError, transaction

from merchants.models import BankAccount, LedgerEntry, Merchant
from payouts.models import Payout


def _make_merchant(email: str = "constraints@example.com") -> Merchant:
    return Merchant.objects.create(name="Constraint", email=email)


def _make_bank(merchant: Merchant) -> BankAccount:
    return BankAccount.objects.create(
        merchant=merchant,
        holder_name="Test",
        account_number_last4="0000",
        ifsc="HDFC0000000",
    )


@pytest.mark.django_db
def test_ledger_zero_amount_is_rejected_by_db():
    m = _make_merchant("zero-amount@example.com")
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            LedgerEntry.objects.create(
                merchant=m,
                amount_paise=0,
                entry_type=LedgerEntry.EntryType.CREDIT,
            )


@pytest.mark.django_db
def test_ledger_credit_with_negative_amount_is_rejected_by_db():
    """The sign-matches-type constraint refuses CREDIT with a negative amount."""
    m = _make_merchant("neg-credit@example.com")
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            LedgerEntry.objects.create(
                merchant=m,
                amount_paise=-100,
                entry_type=LedgerEntry.EntryType.CREDIT,
            )


@pytest.mark.django_db
def test_ledger_debit_with_positive_amount_is_rejected_by_db():
    """And refuses DEBIT with a positive amount."""
    m = _make_merchant("pos-debit@example.com")
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            LedgerEntry.objects.create(
                merchant=m,
                amount_paise=100,
                entry_type=LedgerEntry.EntryType.DEBIT,
            )


@pytest.mark.django_db
def test_ledger_refund_with_negative_amount_is_rejected_by_db():
    """REFUND is a credit-shaped entry — must be positive."""
    m = _make_merchant("neg-refund@example.com")
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            LedgerEntry.objects.create(
                merchant=m,
                amount_paise=-100,
                entry_type=LedgerEntry.EntryType.REFUND,
            )


@pytest.mark.django_db
def test_payout_zero_amount_is_rejected_by_db():
    m = _make_merchant("zero-payout@example.com")
    bank = _make_bank(m)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Payout.objects.create(
                merchant=m,
                bank_account=bank,
                amount_paise=0,
                status=Payout.Status.PENDING,
            )


@pytest.mark.django_db
def test_payout_negative_amount_is_rejected_by_db():
    m = _make_merchant("neg-payout@example.com")
    bank = _make_bank(m)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Payout.objects.create(
                merchant=m,
                bank_account=bank,
                amount_paise=-1,
                status=Payout.Status.PENDING,
            )


@pytest.mark.django_db
def test_payout_negative_retry_count_is_rejected_by_db():
    m = _make_merchant("neg-retry@example.com")
    bank = _make_bank(m)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Payout.objects.create(
                merchant=m,
                bank_account=bank,
                amount_paise=100,
                status=Payout.Status.PENDING,
                retry_count=-1,
            )


@pytest.mark.django_db
def test_idempotency_key_uniqueness_is_enforced_by_db():
    """The (merchant, key) unique constraint is what makes the idempotency
    layer correct. If a refactor accidentally drops it, this test fails."""
    import uuid
    from datetime import timedelta

    from django.utils import timezone

    from payouts.models import IdempotencyKey

    m = _make_merchant("idem-uniq@example.com")
    key = uuid.uuid4()
    expires = timezone.now() + timedelta(hours=24)

    IdempotencyKey.objects.create(
        merchant=m, key=key, request_hash="x" * 64, expires_at=expires
    )
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            IdempotencyKey.objects.create(
                merchant=m, key=key, request_hash="x" * 64, expires_at=expires
            )
