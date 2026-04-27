"""Payout + IdempotencyKey models.

The Payout model is owned by this app; balance-mutation primitives that touch
both the Payout and the LedgerEntry live in ``payouts.services.create_payout``.
"""

import uuid

from django.db import models
from django.db.models import CheckConstraint, Q, UniqueConstraint


class Payout(models.Model):
    """A merchant-initiated transfer of funds to a registered bank account.

    Lifecycle (verbatim from the spec):
        PENDING -> PROCESSING -> COMPLETED, OR
        PENDING -> PROCESSING -> FAILED.

    Anything backwards is rejected by the state-machine guard at
    ``payouts.state_machine``. The check is centralised in one map so a CTO
    review can answer "where is FAILED -> COMPLETED blocked?" with a single
    file:line citation.
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        PROCESSING = "PROCESSING", "Processing"
        COMPLETED = "COMPLETED", "Completed"
        FAILED = "FAILED", "Failed"

    TERMINAL_STATUSES = frozenset({Status.COMPLETED, Status.FAILED})

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    merchant = models.ForeignKey(
        "merchants.Merchant",
        on_delete=models.PROTECT,
        related_name="payouts",
    )
    bank_account = models.ForeignKey(
        "merchants.BankAccount",
        on_delete=models.PROTECT,
        related_name="payouts",
    )
    amount_paise = models.BigIntegerField()
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    retry_count = models.IntegerField(default=0)
    failure_reason = models.CharField(max_length=255, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "payouts"
        indexes = [
            models.Index(fields=["merchant", "-created_at"]),
            # Used by the retry sweeper (Phase 4) — find PROCESSING payouts
            # whose started_at is older than the timeout.
            models.Index(fields=["status", "started_at"]),
        ]
        constraints = [
            CheckConstraint(
                condition=Q(amount_paise__gt=0),
                name="payout_amount_positive",
            ),
            CheckConstraint(
                condition=Q(retry_count__gte=0),
                name="payout_retry_nonnegative",
            ),
        ]

    def __str__(self) -> str:
        return f"Payout {self.id} ({self.status}, {self.amount_paise} paise)"

    def transition_to(self, new_status: str, **fields_to_save) -> None:
        """Validate-and-persist a status change.

        MUST be called inside a transaction with this row locked via
        ``select_for_update()`` — otherwise two callers can race past the
        legality check and both believe they performed the transition. The
        worker enforces this; do not call from view code.

        ``fields_to_save`` lets the caller persist related fields (e.g.
        ``started_at``, ``completed_at``, ``failure_reason``) atomically with
        the status change.
        """
        # Lazy import — keeps state_machine free of model-import cycles.
        from .state_machine import LEGAL_TRANSITIONS, IllegalTransition

        legal = LEGAL_TRANSITIONS.get(self.status, frozenset())
        if new_status not in legal:
            raise IllegalTransition(
                from_status=self.status,
                to_status=new_status,
                payout_id=str(self.id),
            )

        self.status = new_status
        for k, v in fields_to_save.items():
            setattr(self, k, v)
        # ``updated_at`` is auto_now and is updated automatically when included.
        self.save(update_fields=["status", "updated_at", *fields_to_save.keys()])


class IdempotencyKey(models.Model):
    """Caches the response for a (merchant, key) pair so retried POSTs are safe.

    Contract:
      * Same (merchant, key) + same body => identical cached response.
      * Same (merchant, key) + different body => 422 conflict.
      * Different merchants may reuse the same UUID without collision.
      * Rows expire after ``IDEMPOTENCY_KEY_TTL_HOURS`` (default 24h); a
        scheduled task in Phase 4 deletes expired rows.

    Concurrency contract (the "in flight" case):
      The first POST inserts the row inside the work transaction. A second
      POST whose insert collides on the unique constraint blocks at the index
      level until the first transaction commits or rolls back. On commit,
      the second request catches the UniqueViolation and re-reads the row
      under ``select_for_update`` to return the cached response.

    Why there is no IN_PROGRESS / COMPLETED status field:
      We considered one. It would be dead weight here — the entire flow
      commits atomically, so no other connection ever observes IN_PROGRESS.
      The row is invisible until the work transaction commits with the
      response already saved on it. Removing the column keeps the model the
      shape of what is actually used.
    """

    id = models.BigAutoField(primary_key=True)
    merchant = models.ForeignKey(
        "merchants.Merchant",
        on_delete=models.CASCADE,
        related_name="idempotency_keys",
    )
    key = models.UUIDField()
    request_hash = models.CharField(max_length=64)  # sha256 hex
    response_status = models.IntegerField(null=True, blank=True)
    response_body = models.JSONField(null=True, blank=True)
    payout = models.ForeignKey(
        Payout,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="idempotency_keys",
    )
    expires_at = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "idempotency_keys"
        constraints = [
            UniqueConstraint(fields=["merchant", "key"], name="uniq_merchant_key"),
        ]

    def __str__(self) -> str:
        return f"IdempotencyKey(merchant={self.merchant_id}, key={self.key})"
