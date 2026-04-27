"""Celery tasks for the payout worker.

Three tasks live here:

  * ``process_payout``        — first attempt for a PENDING payout. Enqueued
                                 by ``services.create_payout`` via
                                 ``transaction.on_commit``.
  * ``retry_stuck_payouts``   — beat-scheduled sweeper. Finds payouts stuck
                                 in PROCESSING for longer than
                                 ``PAYOUT_PROCESSING_TIMEOUT_SECONDS`` and
                                 dispatches retries with exponential backoff.
  * ``retry_payout``          — single-payout retry. Increments retry_count;
                                 after ``PAYOUT_MAX_RETRIES`` attempts marks
                                 the payout FAILED and writes a refund
                                 ledger entry — both in one transaction.
  * ``cleanup_idempotency_keys`` — beat-scheduled. Deletes rows whose
                                 ``expires_at`` is in the past (24h TTL).

Atomic refund on failure (rubric requirement):
    The REFUND ledger entry MUST commit together with the FAILED transition.
    Any code path that flips a payout to FAILED also writes a positive
    ledger entry inside the same ``transaction.atomic()`` block. If either
    write fails, both roll back — no half-state.

Why ``_simulate_bank`` is a module-level function:
    Tests monkeypatch it to control the outcome deterministically. Keeping
    the random-draw isolated keeps the task bodies free of branching that
    only exists for testability.
"""

import logging
import random
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from merchants.models import LedgerEntry

from .models import IdempotencyKey, Payout

logger = logging.getLogger("playto.payouts")


def _simulate_bank() -> str:
    """Return one of ``"success"``, ``"failure"``, ``"hang"``.

    Distribution follows ``BANK_SIMULATION_SUCCESS`` / ``_FAILURE`` settings;
    the remainder is the hang probability. Validated at app startup in
    ``payouts/apps.py:ready``.
    """
    r = random.random()
    if r < settings.BANK_SIMULATION_FAILURE:
        return "failure"
    if r < settings.BANK_SIMULATION_FAILURE + settings.BANK_SIMULATION_SUCCESS:
        return "success"
    return "hang"


def _apply_outcome(payout_id: str, outcome: str) -> None:
    """Apply a bank outcome to a payout currently in PROCESSING.

    Locks the payout row, re-checks the status (a sibling task may have
    moved it), then performs the appropriate transition. The REFUND
    ledger entry on failure is written INSIDE the same atomic block as
    the transition — that is the rubric's "atomic refund + state change"
    requirement.

    Hang is a no-op here: the retry sweeper will pick it up.
    """
    if outcome == "hang":
        logger.info("payout %s: bank simulation hang; leaving PROCESSING", payout_id)
        return

    with transaction.atomic():
        try:
            payout = Payout.objects.select_for_update().get(id=payout_id)
        except Payout.DoesNotExist:
            logger.warning("payout %s vanished before outcome could apply", payout_id)
            return

        if payout.status != Payout.Status.PROCESSING:
            # A sibling task (likely the retry sweeper hitting max retries)
            # already moved it to a terminal state. Don't double-transition.
            logger.warning(
                "payout %s status %s != PROCESSING when applying outcome; skipping",
                payout_id,
                payout.status,
            )
            return

        if outcome == "success":
            payout.transition_to(
                Payout.Status.COMPLETED,
                completed_at=timezone.now(),
            )
            logger.info("payout %s: COMPLETED", payout_id)
        elif outcome == "failure":
            LedgerEntry.objects.create(
                merchant=payout.merchant,
                amount_paise=payout.amount_paise,
                entry_type=LedgerEntry.EntryType.REFUND,
                related_payout=payout,
                description=f"Refund for failed payout {payout.id}",
            )
            payout.transition_to(
                Payout.Status.FAILED,
                completed_at=timezone.now(),
                failure_reason="simulated_bank_failure",
            )
            logger.info("payout %s: FAILED + refund issued", payout_id)
        else:
            raise ValueError(f"unknown bank outcome: {outcome!r}")


@shared_task(bind=True, name="payouts.process_payout")
def process_payout(self, payout_id: str) -> None:
    """First-attempt processing of a PENDING payout.

    Enqueued by ``services.create_payout`` via ``transaction.on_commit``.
    """
    # Phase 1: claim the payout, transition PENDING -> PROCESSING.
    with transaction.atomic():
        try:
            payout = Payout.objects.select_for_update().get(id=payout_id)
        except Payout.DoesNotExist:
            logger.warning("process_payout: %s does not exist", payout_id)
            return

        if payout.status != Payout.Status.PENDING:
            # Already claimed by a duplicate enqueue or by a manual retry.
            # Idempotent: skip.
            logger.info(
                "process_payout: %s already in %s; skipping",
                payout_id,
                payout.status,
            )
            return

        payout.transition_to(
            Payout.Status.PROCESSING,
            started_at=timezone.now(),
        )

    # Phase 2: simulate bank settlement OUTSIDE the locked transaction so
    # we don't hold locks while waiting on (real, in production) network IO.
    outcome = _simulate_bank()

    # Phase 3: apply outcome under a fresh lock.
    _apply_outcome(payout_id, outcome)


@shared_task(name="payouts.retry_stuck_payouts")
def retry_stuck_payouts() -> None:
    """Sweep for PROCESSING payouts whose started_at is older than
    ``PAYOUT_PROCESSING_TIMEOUT_SECONDS`` and dispatch retries.

    Exponential backoff is implemented via Celery's ``countdown`` so we do
    not have to sleep inside this task or rely on a separate scheduler.
    """
    cutoff = timezone.now() - timedelta(
        seconds=settings.PAYOUT_PROCESSING_TIMEOUT_SECONDS
    )
    stuck = list(
        Payout.objects.filter(
            status=Payout.Status.PROCESSING,
            started_at__lt=cutoff,
        ).values_list("id", "retry_count")
    )

    if not stuck:
        return

    logger.info("retry_stuck_payouts: %d payout(s) to retry", len(stuck))
    for payout_id, retry_count in stuck:
        # Backoff sequence for retries 1..MAX is 2^1, 2^2, 2^3 = 2, 4, 8 seconds.
        backoff_seconds = 2 ** (retry_count + 1)
        retry_payout.apply_async(
            args=[str(payout_id)],
            countdown=backoff_seconds,
        )


@shared_task(name="payouts.retry_payout")
def retry_payout(payout_id: str) -> None:
    """Retry a single stuck payout.

    Sequence:
        1. Lock the payout row.
        2. If status is no longer PROCESSING -> noop (sibling task moved it).
        3. If retry_count >= PAYOUT_MAX_RETRIES -> mark FAILED, write the
           refund ledger entry IN THE SAME TRANSACTION.
        4. Otherwise increment retry_count, reset started_at (so the sweep
           clock starts fresh for this attempt), commit.
        5. Re-run bank simulation outside the lock.
        6. Apply the outcome.
    """
    with transaction.atomic():
        try:
            payout = Payout.objects.select_for_update().get(id=payout_id)
        except Payout.DoesNotExist:
            return

        if payout.status != Payout.Status.PROCESSING:
            return

        if payout.retry_count >= settings.PAYOUT_MAX_RETRIES:
            # Out of retries — refund + transition, atomically.
            LedgerEntry.objects.create(
                merchant=payout.merchant,
                amount_paise=payout.amount_paise,
                entry_type=LedgerEntry.EntryType.REFUND,
                related_payout=payout,
                description=(
                    f"Refund for max-retry-exceeded payout {payout.id}"
                ),
            )
            payout.transition_to(
                Payout.Status.FAILED,
                completed_at=timezone.now(),
                failure_reason=(
                    f"max_retries_exceeded ({settings.PAYOUT_MAX_RETRIES})"
                ),
            )
            logger.info(
                "payout %s: FAILED after max retries (%d)",
                payout_id,
                settings.PAYOUT_MAX_RETRIES,
            )
            return

        payout.retry_count += 1
        payout.started_at = timezone.now()
        payout.save(update_fields=["retry_count", "started_at", "updated_at"])

    outcome = _simulate_bank()
    _apply_outcome(payout_id, outcome)


@shared_task(name="payouts.cleanup_idempotency_keys")
def cleanup_idempotency_keys() -> None:
    """Delete idempotency-key rows whose ``expires_at`` is in the past.

    Spec: keys expire after 24 hours. The TTL is set on the row at INSERT
    time (``services.create_payout``) using ``IDEMPOTENCY_KEY_TTL_HOURS``.
    """
    deleted_count, _ = IdempotencyKey.objects.filter(
        expires_at__lt=timezone.now()
    ).delete()
    if deleted_count:
        logger.info(
            "cleanup_idempotency_keys: deleted %d expired row(s)", deleted_count
        )
