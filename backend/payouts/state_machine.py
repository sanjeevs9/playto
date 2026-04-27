"""Single source of truth for legal Payout status transitions.

Spec verbatim:
    Legal: PENDING -> PROCESSING -> COMPLETED, OR PENDING -> PROCESSING -> FAILED.
    Illegal (must be rejected): COMPLETED -> *, FAILED -> *, anything backwards.

The map below is consulted by ``Payout.transition_to``. To answer the EXPLAINER
question "where is FAILED -> COMPLETED blocked?" — point at this file: the
``Status.FAILED`` key maps to an empty frozenset, so any attempted transition
out of FAILED raises ``IllegalTransition``. Same for COMPLETED.

Retry handling note:
    A retry of a stuck PROCESSING payout does NOT transition state. The retry
    sweeper re-invokes the bank simulation while the payout stays PROCESSING;
    only the simulation outcome (or hitting the max-retry limit) drives a
    PROCESSING -> COMPLETED / PROCESSING -> FAILED transition. This keeps the
    legal-transitions map consistent with the spec's "anything backwards is
    illegal" rule.
"""

from .models import Payout

LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    Payout.Status.PENDING: frozenset({Payout.Status.PROCESSING}),
    Payout.Status.PROCESSING: frozenset(
        {Payout.Status.COMPLETED, Payout.Status.FAILED}
    ),
    Payout.Status.COMPLETED: frozenset(),
    Payout.Status.FAILED: frozenset(),
}


class IllegalTransition(Exception):
    """Refused status change between Payout states.

    Carries from/to/payout_id so handlers can log, surface a 409, or alert.
    """

    def __init__(self, *, from_status: str, to_status: str, payout_id: str):
        self.from_status = from_status
        self.to_status = to_status
        self.payout_id = payout_id
        super().__init__(
            f"payout {payout_id}: {from_status} -> {to_status} is not legal"
        )
