"""Idempotency tests for POST /api/v1/payouts.

Three scenarios cover the take-home spec verbatim:
    1. Same key + same body twice -> identical response, only one payout.
    2. Same key + different body  -> 422 conflict.
    3. Two concurrent requests with the same key, racing -> exactly one
       payout created, both clients see the same response (the "first
       request in flight when the second arrives" case from the spec).
"""

import threading
import uuid
from datetime import timedelta

import pytest
from django.db import connections
from django.utils import timezone
from rest_framework.test import APIClient

from merchants.models import BankAccount, LedgerEntry, Merchant
from payouts.models import IdempotencyKey, Payout


def _make_merchant(*, balance_paise: int = 100_000, email: str = "idem@example.com"):
    merchant = Merchant.objects.create(name="Idem Test", email=email)
    LedgerEntry.objects.create(
        merchant=merchant,
        amount_paise=balance_paise,
        entry_type=LedgerEntry.EntryType.CREDIT,
        description="seed",
    )
    bank = BankAccount.objects.create(
        merchant=merchant,
        holder_name="Test Holder",
        account_number_last4="1234",
        ifsc="HDFC0000123",
        is_default=True,
    )
    return merchant, bank


def _post(client, merchant_id, key, body):
    return client.post(
        "/api/v1/payouts",
        body,
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(key),
        HTTP_X_MERCHANT_ID=str(merchant_id),
    )


@pytest.mark.django_db
def test_same_key_same_body_returns_same_payout():
    merchant, bank = _make_merchant()
    key = uuid.uuid4()
    body = {"amount_paise": 50_000, "bank_account_id": bank.id}
    client = APIClient()

    r1 = _post(client, merchant.id, key, body)
    r2 = _post(client, merchant.id, key, body)

    assert r1.status_code == 201, r1.json()
    assert r2.status_code == 201, r2.json()
    assert r1.json()["id"] == r2.json()["id"], "second call must return the same payout id"

    # Side-effects must have happened exactly once.
    assert Payout.objects.filter(merchant=merchant).count() == 1
    debits = LedgerEntry.objects.filter(
        merchant=merchant, entry_type=LedgerEntry.EntryType.DEBIT
    )
    assert debits.count() == 1
    assert debits.first().amount_paise == -50_000


@pytest.mark.django_db
def test_nonexistent_merchant_returns_404_not_500():
    """Regression for the BUG-1 / EXPLAINER Q5 case.

    Original bug: the IdempotencyKey INSERT ran before merchant validation,
    so a missing merchant produced a Postgres ForeignKeyViolation. The catch
    treated it as a duplicate-key case, fell to Phase B, queried for a row
    that was never persisted, and crashed with DoesNotExist -> 500.

    Expected: 404 ``merchant_not_found`` deterministically. Same input on a
    retry yields the same 404 with no DB writes left behind.
    """
    key = uuid.uuid4()
    body = {"amount_paise": 50_000, "bank_account_id": 1}
    client = APIClient()

    r1 = client.post(
        "/api/v1/payouts",
        body,
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(key),
        HTTP_X_MERCHANT_ID="99999",
    )
    r2 = client.post(
        "/api/v1/payouts",
        body,
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(key),
        HTTP_X_MERCHANT_ID="99999",
    )

    assert r1.status_code == 404, r1.content
    assert r2.status_code == 404, r2.content
    assert r1.json() == r2.json()
    # 404 path writes nothing — there is no merchant to anchor an
    # IdempotencyKey FK to. Verify the table is empty for the missing
    # merchant id.
    from payouts.models import IdempotencyKey

    assert (
        IdempotencyKey.objects.filter(merchant_id=99999).count() == 0
    ), "404 path must not leave IdempotencyKey rows behind"


@pytest.mark.django_db
def test_same_key_different_body_is_rejected():
    merchant, bank = _make_merchant()
    key = uuid.uuid4()
    client = APIClient()

    r1 = _post(
        client,
        merchant.id,
        key,
        {"amount_paise": 50_000, "bank_account_id": bank.id},
    )
    r2 = _post(
        client,
        merchant.id,
        key,
        {"amount_paise": 60_000, "bank_account_id": bank.id},
    )

    assert r1.status_code == 201
    assert r2.status_code == 422
    assert r2.json()["error"] == "idempotency_conflict"

    # No second payout, no second debit.
    assert Payout.objects.filter(merchant=merchant).count() == 1


@pytest.mark.django_db
def test_same_key_replays_cached_error_response():
    """A failed first call (e.g. insufficient funds) is cached so the second
    call sees the same error rather than a fresh attempt that might succeed
    after balance has changed.

    Without caching, an over-balance amount could yield 422 on the first
    call and 201 on the retry if a credit landed in between — that breaks
    the spec's "same response" contract for retried requests.
    """
    merchant, bank = _make_merchant(balance_paise=10_000)
    key = uuid.uuid4()
    body = {"amount_paise": 50_000, "bank_account_id": bank.id}  # over-balance
    client = APIClient()

    r1 = _post(client, merchant.id, key, body)
    assert r1.status_code == 422
    assert r1.json()["error"] == "insufficient_funds"

    # Top up the merchant — by ledger semantics this would now cover the
    # request. But the cached response must still be returned.
    LedgerEntry.objects.create(
        merchant=merchant,
        amount_paise=200_000,
        entry_type=LedgerEntry.EntryType.CREDIT,
        description="topped up after failed attempt",
    )

    r2 = _post(client, merchant.id, key, body)
    assert r2.status_code == 422, (
        "second call must replay the cached 422, not retry against new balance"
    )
    assert r2.json() == r1.json()
    assert Payout.objects.filter(merchant=merchant).count() == 0


@pytest.mark.django_db
def test_idempotency_keys_are_scoped_per_merchant():
    """Two different merchants may legitimately use the same UUID without
    colliding. The unique constraint is on (merchant, key), not key alone.
    """
    merchant_a, bank_a = _make_merchant(email="merchant-a@example.com")
    merchant_b, bank_b = _make_merchant(email="merchant-b@example.com")
    shared_key = uuid.uuid4()
    client = APIClient()

    r_a = _post(
        client,
        merchant_a.id,
        shared_key,
        {"amount_paise": 10_000, "bank_account_id": bank_a.id},
    )
    r_b = _post(
        client,
        merchant_b.id,
        shared_key,
        {"amount_paise": 20_000, "bank_account_id": bank_b.id},
    )

    assert r_a.status_code == 201
    assert r_b.status_code == 201
    assert r_a.json()["id"] != r_b.json()["id"]
    assert Payout.objects.filter(merchant=merchant_a).count() == 1
    assert Payout.objects.filter(merchant=merchant_b).count() == 1


@pytest.mark.django_db
def test_idempotency_key_repr_does_not_reference_dropped_columns():
    """Regression: ``IdempotencyKey.__str__`` once interpolated ``self.status``
    after the column had been dropped in migration 0002. Any caller — the
    admin list view, a ``logger.info(f"... {idem}")`` line, or a Python
    traceback formatter — would raise ``AttributeError``. ``__str__`` must be
    safe to call on every persisted row.
    """
    merchant, _ = _make_merchant(email="repr-regression@example.com")
    ik = IdempotencyKey.objects.create(
        merchant=merchant,
        key=uuid.uuid4(),
        request_hash="0" * 64,
        expires_at=timezone.now() + timedelta(hours=1),
    )
    s = str(ik)  # must not raise
    assert "merchant" in s
    assert str(merchant.id) in s


@pytest.mark.django_db(transaction=True)
def test_concurrent_requests_with_same_key_create_one_payout():
    """The 'in-flight' case from the spec.

    Two threads POST simultaneously with the same key. The unique constraint
    on (merchant, key) serialises them: one INSERT wins, the other catches
    IntegrityError and re-reads the cached response under
    ``select_for_update``. Both clients see the same payout id, and exactly
    one Payout row exists.
    """
    merchant, bank = _make_merchant(email="concurrent-idem@example.com")
    key = uuid.uuid4()
    body = {"amount_paise": 50_000, "bank_account_id": bank.id}

    responses: list[tuple[int, dict]] = []
    barrier = threading.Barrier(2)

    def fire():
        barrier.wait()
        client = APIClient()
        try:
            r = _post(client, merchant.id, key, body)
            responses.append((r.status_code, r.json()))
        finally:
            connections.close_all()

    threads = [threading.Thread(target=fire) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(responses) == 2
    assert {r[0] for r in responses} == {201}, (
        f"both calls must succeed with 201; got {responses}"
    )
    assert responses[0][1]["id"] == responses[1][1]["id"], (
        "both calls must see the same payout id"
    )
    assert Payout.objects.filter(merchant=merchant).count() == 1
    assert (
        LedgerEntry.objects.filter(
            merchant=merchant, entry_type=LedgerEntry.EntryType.DEBIT
        ).count()
        == 1
    )
