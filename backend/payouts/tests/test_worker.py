"""Tests for the Celery worker tasks.

We mock ``_simulate_bank`` to control the bank outcome deterministically;
otherwise the tests would be flaky in proportion to the simulation
probabilities. Celery is configured in ``conftest.py`` to run tasks
synchronously (``CELERY_TASK_ALWAYS_EAGER=True``) so the tests don't need
a broker.
"""

import threading
import uuid
from datetime import timedelta

import pytest
from django.db import connections
from django.db.models import Sum
from django.utils import timezone

from merchants.models import BankAccount, LedgerEntry, Merchant
from payouts import tasks
from payouts.models import IdempotencyKey, Payout


def _seed_merchant_with_payout(*, balance_paise=100_000, payout_paise=50_000):
    """Create a merchant + bank + ledger seed + a PENDING payout with the
    matching DEBIT entry already on the ledger (mirroring what
    ``services.create_payout`` would do)."""
    merchant = Merchant.objects.create(
        name="Worker Test", email=f"worker-{uuid.uuid4().hex[:8]}@example.com"
    )
    LedgerEntry.objects.create(
        merchant=merchant,
        amount_paise=balance_paise,
        entry_type=LedgerEntry.EntryType.CREDIT,
        description="seed",
    )
    bank = BankAccount.objects.create(
        merchant=merchant,
        holder_name="Test",
        account_number_last4="1234",
        ifsc="HDFC0000123",
    )
    payout = Payout.objects.create(
        merchant=merchant,
        bank_account=bank,
        amount_paise=payout_paise,
        status=Payout.Status.PENDING,
    )
    LedgerEntry.objects.create(
        merchant=merchant,
        amount_paise=-payout_paise,
        entry_type=LedgerEntry.EntryType.DEBIT,
        related_payout=payout,
        description=f"Hold for payout {payout.id}",
    )
    return merchant, payout


def _balance(merchant) -> int:
    return (
        LedgerEntry.objects.filter(merchant=merchant).aggregate(
            total=Sum("amount_paise")
        )["total"]
        or 0
    )


@pytest.mark.django_db
def test_process_payout_success_transitions_to_completed(monkeypatch):
    monkeypatch.setattr(tasks, "_simulate_bank", lambda: "success")
    merchant, payout = _seed_merchant_with_payout()

    tasks.process_payout(str(payout.id))

    payout.refresh_from_db()
    assert payout.status == Payout.Status.COMPLETED
    assert payout.started_at is not None
    assert payout.completed_at is not None
    # Debit stands; balance does NOT come back. Ledger SUM = 100k - 50k = 50k.
    assert _balance(merchant) == 50_000
    # No refund entry was written for a successful payout.
    assert (
        LedgerEntry.objects.filter(
            merchant=merchant, entry_type=LedgerEntry.EntryType.REFUND
        ).count()
        == 0
    )


@pytest.mark.django_db
def test_process_payout_failure_writes_refund_atomically(monkeypatch):
    """The take-home rubric requires that the refund + state transition be
    atomic. Verify both side-effects landed and the balance is fully
    restored — the SUM-based invariant is the rubric's check."""
    monkeypatch.setattr(tasks, "_simulate_bank", lambda: "failure")
    merchant, payout = _seed_merchant_with_payout()

    tasks.process_payout(str(payout.id))

    payout.refresh_from_db()
    assert payout.status == Payout.Status.FAILED
    assert payout.completed_at is not None
    assert payout.failure_reason == "simulated_bank_failure"

    # Refund entry exists and offsets the debit exactly.
    refund = LedgerEntry.objects.get(
        merchant=merchant,
        entry_type=LedgerEntry.EntryType.REFUND,
        related_payout=payout,
    )
    assert refund.amount_paise == 50_000
    # Balance fully restored — SUM(ledger) equals the original credit.
    assert _balance(merchant) == 100_000


@pytest.mark.django_db
def test_process_payout_hang_leaves_processing(monkeypatch):
    """A hung simulation must NOT transition the payout to a terminal state.
    The retry sweeper picks it up later via the ``started_at < cutoff`` query."""
    monkeypatch.setattr(tasks, "_simulate_bank", lambda: "hang")
    merchant, payout = _seed_merchant_with_payout()

    tasks.process_payout(str(payout.id))

    payout.refresh_from_db()
    assert payout.status == Payout.Status.PROCESSING
    assert payout.started_at is not None
    assert payout.completed_at is None
    # No refund: payout is still in flight.
    assert (
        LedgerEntry.objects.filter(
            merchant=merchant, entry_type=LedgerEntry.EntryType.REFUND
        ).count()
        == 0
    )


@pytest.mark.django_db
def test_retry_stuck_payouts_only_dispatches_for_old_processing(monkeypatch):
    """The sweeper must pick up payouts whose started_at is older than the
    timeout and ignore both younger PROCESSING payouts and PENDING ones."""
    fired_payout_ids: list[str] = []

    def fake_apply_async(args, **kwargs):
        fired_payout_ids.append(args[0])

    monkeypatch.setattr(
        tasks.retry_payout, "apply_async", fake_apply_async
    )

    _, recent = _seed_merchant_with_payout()
    recent.status = Payout.Status.PROCESSING
    recent.started_at = timezone.now()  # just now — should NOT be picked up
    recent.save(update_fields=["status", "started_at"])

    _, stuck = _seed_merchant_with_payout()
    stuck.status = Payout.Status.PROCESSING
    stuck.started_at = timezone.now() - timedelta(minutes=5)  # well past timeout
    stuck.save(update_fields=["status", "started_at"])

    _, pending = _seed_merchant_with_payout()  # still PENDING — not stuck

    tasks.retry_stuck_payouts()

    assert fired_payout_ids == [str(stuck.id)], (
        f"only the old PROCESSING payout should be dispatched; "
        f"got {fired_payout_ids}"
    )


@pytest.mark.django_db
def test_retry_payout_at_max_retries_marks_failed_with_refund(monkeypatch):
    """After PAYOUT_MAX_RETRIES exhausted, the payout transitions to FAILED
    and a refund is issued — both inside one transaction. Critically, the
    bank simulation is NOT re-run when we hit max retries (we already gave
    up); test by leaving _simulate_bank unmonkeypatched and verifying the
    outcome doesn't depend on it."""
    from django.conf import settings

    merchant, payout = _seed_merchant_with_payout()
    payout.status = Payout.Status.PROCESSING
    payout.started_at = timezone.now() - timedelta(minutes=5)
    payout.retry_count = settings.PAYOUT_MAX_RETRIES  # exhausted
    payout.save(update_fields=["status", "started_at", "retry_count"])

    # Force _simulate_bank to fail loudly if it ever fires — proves the
    # max-retry path doesn't re-simulate.
    def explode():
        raise AssertionError("bank simulation must not be re-run at max retries")

    monkeypatch.setattr(tasks, "_simulate_bank", explode)

    tasks.retry_payout(str(payout.id))

    payout.refresh_from_db()
    assert payout.status == Payout.Status.FAILED
    assert payout.completed_at is not None
    assert "max_retries_exceeded" in payout.failure_reason

    # Refund landed atomically with the FAILED transition.
    refund = LedgerEntry.objects.get(
        merchant=merchant,
        entry_type=LedgerEntry.EntryType.REFUND,
        related_payout=payout,
    )
    assert refund.amount_paise == 50_000
    assert _balance(merchant) == 100_000


@pytest.mark.django_db
def test_retry_payout_below_max_increments_and_resimulates(monkeypatch):
    """One retry below the max bumps retry_count, resets started_at, and
    re-runs the simulation. With ``"success"`` outcome the payout completes."""
    monkeypatch.setattr(tasks, "_simulate_bank", lambda: "success")
    merchant, payout = _seed_merchant_with_payout()
    original_started = timezone.now() - timedelta(minutes=5)
    payout.status = Payout.Status.PROCESSING
    payout.started_at = original_started
    payout.retry_count = 1
    payout.save(update_fields=["status", "started_at", "retry_count"])

    tasks.retry_payout(str(payout.id))

    payout.refresh_from_db()
    assert payout.status == Payout.Status.COMPLETED
    assert payout.retry_count == 2  # incremented by retry_payout
    assert payout.started_at > original_started  # clock reset


@pytest.mark.django_db
def test_cleanup_idempotency_keys_deletes_only_expired():
    merchant = Merchant.objects.create(
        name="Cleanup", email="cleanup@example.com"
    )
    expired = IdempotencyKey.objects.create(
        merchant=merchant,
        key=uuid.uuid4(),
        request_hash="0" * 64,
        expires_at=timezone.now() - timedelta(hours=1),
    )
    fresh = IdempotencyKey.objects.create(
        merchant=merchant,
        key=uuid.uuid4(),
        request_hash="0" * 64,
        expires_at=timezone.now() + timedelta(hours=1),
    )

    tasks.cleanup_idempotency_keys()

    assert not IdempotencyKey.objects.filter(id=expired.id).exists()
    assert IdempotencyKey.objects.filter(id=fresh.id).exists()


@pytest.mark.django_db(transaction=True)
def test_concurrent_apply_outcome_results_in_one_terminal_state():
    """Two threads simultaneously call ``_apply_outcome`` for the same
    payout. The ``select_for_update`` inside ``_apply_outcome`` plus the
    ``status != PROCESSING`` re-check must serialise them: exactly one
    transition occurs, exactly one refund is written.

    This guards against the at-least-once delivery case from Redis — if the
    broker delivers the same outcome message twice, the worker must not
    write two refunds or attempt an illegal terminal->terminal transition.
    """
    merchant, payout = _seed_merchant_with_payout(
        balance_paise=100_000, payout_paise=50_000
    )
    payout.status = Payout.Status.PROCESSING
    payout.started_at = timezone.now()
    payout.save(update_fields=["status", "started_at"])

    barrier = threading.Barrier(2)

    def fire(outcome):
        barrier.wait()
        try:
            tasks._apply_outcome(str(payout.id), outcome)
        finally:
            connections.close_all()

    t1 = threading.Thread(target=fire, args=("failure",))
    t2 = threading.Thread(target=fire, args=("failure",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    payout.refresh_from_db()
    assert payout.status == Payout.Status.FAILED, (
        "exactly one of the two threads should have transitioned the row"
    )
    refund_count = LedgerEntry.objects.filter(
        related_payout=payout,
        entry_type=LedgerEntry.EntryType.REFUND,
    ).count()
    assert refund_count == 1, (
        f"exactly one refund should be written; got {refund_count} "
        "(double-refund means the lock + status re-check failed)"
    )
    # And the balance invariant still holds — sum equals original credit.
    final = (
        LedgerEntry.objects.filter(merchant=merchant)
        .aggregate(total=Sum("amount_paise"))["total"]
    )
    assert final == 100_000


@pytest.mark.django_db
def test_apply_outcome_atomicity_failure_path(monkeypatch):
    """Atomicity guarantee, not just both-side-effect-present.

    The rubric requires: "A failed payout returning funds must do so
    atomically with the state transition." The previous test verifies both
    side-effects landed; this one verifies that if the transition fails
    AFTER the refund LedgerEntry was inserted, the refund is rolled back.

    We monkeypatch ``Payout.transition_to`` to raise. If ``_apply_outcome``
    is correctly inside ``transaction.atomic()``, the LedgerEntry insert
    must roll back. If a future refactor moves the LedgerEntry write
    outside the atomic block, this test fails — the refund persists
    despite the transition failure.
    """
    merchant, payout = _seed_merchant_with_payout()
    payout.status = Payout.Status.PROCESSING
    payout.started_at = timezone.now()
    payout.save(update_fields=["status", "started_at"])

    def boom(self, *args, **kwargs):
        raise RuntimeError("simulated mid-transaction failure")

    monkeypatch.setattr(Payout, "transition_to", boom)

    with pytest.raises(RuntimeError):
        tasks._apply_outcome(str(payout.id), "failure")

    # Atomicity proof: the refund LedgerEntry must NOT be persisted because
    # transition_to raised inside the same atomic block.
    assert (
        LedgerEntry.objects.filter(
            related_payout=payout,
            entry_type=LedgerEntry.EntryType.REFUND,
        ).count()
        == 0
    ), "refund persisted despite transition failure — atomicity violated"

    # And the row's status is unchanged.
    payout.refresh_from_db()
    assert payout.status == Payout.Status.PROCESSING


@pytest.mark.django_db
def test_failed_payout_preserves_balance_invariant(monkeypatch):
    """Spec invariant: SUM(amount_paise) over all ledger entries equals
    the merchant's available balance.

    For a failed payout the cycle is:
        seed credit (+100k) -> payout debit (-50k) -> refund credit (+50k)
        SUM = 100k. Original balance fully restored.
    """
    monkeypatch.setattr(tasks, "_simulate_bank", lambda: "failure")
    merchant, payout = _seed_merchant_with_payout(
        balance_paise=100_000, payout_paise=50_000
    )

    tasks.process_payout(str(payout.id))

    # Database-side aggregation, not Python.
    final = (
        LedgerEntry.objects.filter(merchant=merchant)
        .aggregate(total=Sum("amount_paise"))["total"]
    )
    assert final == 100_000
    # And the entry types tell the same story: 1 credit + 1 debit + 1 refund.
    by_type = dict(
        LedgerEntry.objects.filter(merchant=merchant)
        .values_list("entry_type")
        .annotate(c=Sum("amount_paise"))
    )
    assert by_type["CREDIT"] == 100_000
    assert by_type["DEBIT"] == -50_000
    assert by_type["REFUND"] == 50_000
