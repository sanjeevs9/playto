"""State-machine guard tests for ``Payout.transition_to``.

Spec verbatim:
    Legal: PENDING -> PROCESSING -> COMPLETED, OR PENDING -> PROCESSING -> FAILED.
    Illegal (must be rejected): COMPLETED -> *, FAILED -> *, anything backwards.

These tests pin the legal/illegal map so refactors can't silently widen the
state machine.
"""

import pytest
from django.utils import timezone

from merchants.models import BankAccount, Merchant
from payouts.models import Payout
from payouts.state_machine import IllegalTransition

LEGAL = [
    (Payout.Status.PENDING, Payout.Status.PROCESSING),
    (Payout.Status.PROCESSING, Payout.Status.COMPLETED),
    (Payout.Status.PROCESSING, Payout.Status.FAILED),
]

ILLEGAL = [
    # Skipping the PROCESSING step is illegal.
    (Payout.Status.PENDING, Payout.Status.COMPLETED),
    (Payout.Status.PENDING, Payout.Status.FAILED),
    # Anything backwards.
    (Payout.Status.PROCESSING, Payout.Status.PENDING),
    (Payout.Status.COMPLETED, Payout.Status.PENDING),
    (Payout.Status.COMPLETED, Payout.Status.PROCESSING),
    (Payout.Status.FAILED, Payout.Status.PENDING),
    (Payout.Status.FAILED, Payout.Status.PROCESSING),
    # Terminal -> terminal: explicitly the spec's "FAILED -> COMPLETED" example.
    (Payout.Status.COMPLETED, Payout.Status.FAILED),
    (Payout.Status.FAILED, Payout.Status.COMPLETED),
    # Self-transitions are illegal — the state machine has no "no-op" edge.
    (Payout.Status.PENDING, Payout.Status.PENDING),
    (Payout.Status.PROCESSING, Payout.Status.PROCESSING),
    (Payout.Status.COMPLETED, Payout.Status.COMPLETED),
    (Payout.Status.FAILED, Payout.Status.FAILED),
]


def _make_payout(status: str) -> Payout:
    """Create a Payout in the given status by writing the field directly.

    We bypass ``transition_to`` here on purpose — the test setup needs to
    *put* the row in arbitrary states so we can exercise the guard from each
    state, including illegal starting states the guard itself would refuse
    to produce.
    """
    merchant = Merchant.objects.create(
        name=f"SM Test {status}", email=f"sm-{status.lower()}-{id(status)}@example.com"
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
        amount_paise=1_000,
        status=status,
    )
    if status in (Payout.Status.PROCESSING, Payout.Status.COMPLETED, Payout.Status.FAILED):
        payout.started_at = timezone.now()
        payout.save(update_fields=["started_at"])
    if status in (Payout.Status.COMPLETED, Payout.Status.FAILED):
        payout.completed_at = timezone.now()
        payout.save(update_fields=["completed_at"])
    return payout


@pytest.mark.django_db
@pytest.mark.parametrize("from_status,to_status", LEGAL)
def test_legal_transition_succeeds(from_status, to_status):
    payout = _make_payout(from_status)
    payout.transition_to(to_status)
    payout.refresh_from_db()
    assert payout.status == to_status


@pytest.mark.django_db
@pytest.mark.parametrize("from_status,to_status", ILLEGAL)
def test_illegal_transition_is_rejected(from_status, to_status):
    payout = _make_payout(from_status)
    with pytest.raises(IllegalTransition) as excinfo:
        payout.transition_to(to_status)
    assert excinfo.value.from_status == from_status
    assert excinfo.value.to_status == to_status

    # The DB row must NOT have changed.
    payout.refresh_from_db()
    assert payout.status == from_status


@pytest.mark.django_db
def test_failed_to_completed_is_blocked_explicitly():
    """The spec calls this case out by name. Worth a dedicated test so
    grep-by-symptom finds it instantly."""
    payout = _make_payout(Payout.Status.FAILED)
    with pytest.raises(IllegalTransition):
        payout.transition_to(Payout.Status.COMPLETED)
    payout.refresh_from_db()
    assert payout.status == Payout.Status.FAILED
