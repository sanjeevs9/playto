"""Celery tasks for the payout worker.

Phase 3 ships this module as stubs so that ``payouts.services.create_payout``
can call ``transaction.on_commit(lambda: process_payout.delay(payout_id))``
without import errors. Phase 4 fills in the bank-simulation logic, the state
transitions, and the retry sweeper.
"""

import logging

from celery import shared_task

logger = logging.getLogger("playto.payouts")


@shared_task(bind=True, name="payouts.process_payout")
def process_payout(self, payout_id: str) -> None:
    """Pick up a PENDING payout and run it through the bank simulation.

    Phase 4 will replace this stub with the full lifecycle:
        PENDING -> PROCESSING -> COMPLETED  (70% of the time)
        PENDING -> PROCESSING -> FAILED     (20% of the time, with refund)
        PENDING -> PROCESSING -> (hang)     (10% of the time; retry sweeper picks up)
    """
    logger.info(
        "process_payout stub invoked for %s; Phase 4 will implement", payout_id
    )


@shared_task(name="payouts.retry_stuck_payouts")
def retry_stuck_payouts() -> None:
    """Retry sweeper — Phase 4 implements."""
    logger.info("retry_stuck_payouts stub invoked; Phase 4 will implement")


@shared_task(name="payouts.cleanup_idempotency_keys")
def cleanup_idempotency_keys() -> None:
    """Delete idempotency-key rows whose ``expires_at`` is in the past.

    Phase 4 wires this to a Celery beat schedule.
    """
    logger.info("cleanup_idempotency_keys stub invoked; Phase 4 will implement")
