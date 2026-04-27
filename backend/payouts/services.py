"""Service layer for the payout API.

``create_payout`` is the orchestration entrypoint that combines:
    1. Merchant lock (404 immediately if not found).
    2. Idempotency claim (try-INSERT, fall back to read-cached-response on
       UniqueViolation).
    3. Atomic Payout + DEBIT LedgerEntry creation.
    4. ``transaction.on_commit`` enqueue of the worker.

Why everything is in one transaction:
    The IdempotencyKey row, the Payout row, and the DEBIT LedgerEntry must
    all commit together. If they did not, a crash mid-flow could leave a
    Payout without a debit (over-payment) or a debit without a Payout
    (lost funds). The atomic block guarantees all-or-nothing.

Why merchant is locked BEFORE the idempotency-key INSERT:
    The IdempotencyKey row carries an FK to merchants.id. If we INSERT
    before validating merchant existence, a missing merchant raises
    ForeignKeyViolation — which is also a subclass of IntegrityError, and
    a too-broad ``except IntegrityError`` would silently treat it as a
    duplicate-key case and break the flow. Locking the merchant first
    guarantees the only ``IntegrityError`` path that remains is the
    (merchant, key) UniqueViolation we actually want to handle.

    See ``notes/q5-ai-audit-original.py`` for the original buggy version
    and the AI-audit narrative for EXPLAINER Q5.
"""

import hashlib
import json
import logging
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from psycopg.errors import UniqueViolation

from merchants.models import BankAccount, LedgerEntry, Merchant
from merchants.services import available_balance_paise

from .models import IdempotencyKey, Payout

logger = logging.getLogger("playto.payouts")


class IdempotencyConflict(Exception):
    """Same idempotency key reused with a different request body."""

    def __init__(self, merchant_id: int, key: uuid.UUID):
        self.merchant_id = merchant_id
        self.key = key
        super().__init__(
            f"merchant {merchant_id}: idempotency key {key} reused with "
            f"a different request body"
        )


def _hash_body(body: dict) -> str:
    """SHA-256 of a canonical JSON encoding.

    Sort keys so logically-equal bodies hash identically regardless of dict
    order. ``separators`` removes whitespace so ``{"a":1}`` and ``{"a": 1}``
    are the same.
    """
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _serialize_payout(p: Payout) -> dict:
    return {
        "id": str(p.id),
        "merchant_id": p.merchant_id,
        "bank_account_id": p.bank_account_id,
        "amount_paise": p.amount_paise,
        "status": p.status,
        "retry_count": p.retry_count,
        "failure_reason": p.failure_reason,
        "started_at": p.started_at.isoformat() if p.started_at else None,
        "completed_at": p.completed_at.isoformat() if p.completed_at else None,
        "created_at": p.created_at.isoformat(),
        "updated_at": p.updated_at.isoformat(),
    }


def create_payout(
    *,
    merchant_id: int,
    idempotency_key: uuid.UUID,
    bank_account_id: int,
    amount_paise: int,
) -> tuple[dict, int]:
    """Idempotent payout creation. Returns (response_body, http_status).

    Phase A: lock merchant -> claim (merchant, key) -> do work -> commit.
    Phase B: on UniqueViolation (key already claimed by a prior request),
             open a fresh transaction, ``select_for_update`` the row to
             block on any in-flight transaction holding it, then return the
             cached response (or 422 if the body hash differs).

    Note: a 404 for "merchant_not_found" is NOT cached. Without an existing
    merchant we cannot anchor an IdempotencyKey row (the FK would fail), and
    a 404 is naturally idempotent — the same input yields the same answer
    every time without needing a cache.
    """
    body = {
        "amount_paise": amount_paise,
        "bank_account_id": bank_account_id,
    }
    request_hash = _hash_body(body)
    expires_at = timezone.now() + timedelta(
        hours=settings.IDEMPOTENCY_KEY_TTL_HOURS
    )

    # ---- Phase A: lock merchant -> claim key -> do work -----------------
    try:
        with transaction.atomic():
            # Lock the merchant row FIRST. Two effects:
            #   1. 404 immediately if no such merchant — no IdempotencyKey
            #      row written, no FK violation.
            #   2. Subsequent payout-creating transactions for the same
            #      merchant serialise on this lock, which is the rubric's
            #      concurrency primitive.
            try:
                merchant = Merchant.objects.select_for_update().get(
                    id=merchant_id
                )
            except Merchant.DoesNotExist:
                return {"error": "merchant_not_found"}, 404

            # With merchant guaranteed to exist, the only IntegrityError
            # path left for this INSERT is the (merchant, key)
            # UniqueViolation we want to handle in Phase B.
            idem = IdempotencyKey.objects.create(
                merchant=merchant,
                key=idempotency_key,
                request_hash=request_hash,
                expires_at=expires_at,
            )

            try:
                bank_account = BankAccount.objects.get(
                    id=bank_account_id, merchant=merchant
                )
            except BankAccount.DoesNotExist:
                response_body = {"error": "bank_account_not_found"}
                response_status = 404
            else:
                # Recompute available balance INSIDE the lock — anything else
                # is a TOCTOU race. ``available_balance_paise`` runs a Sum
                # aggregation against the ledger.
                available = available_balance_paise(merchant_id)
                if available < amount_paise:
                    response_body = {
                        "error": "insufficient_funds",
                        "available_paise": available,
                        "requested_paise": amount_paise,
                    }
                    response_status = 422
                else:
                    payout = Payout.objects.create(
                        merchant=merchant,
                        bank_account=bank_account,
                        amount_paise=amount_paise,
                        status=Payout.Status.PENDING,
                    )
                    LedgerEntry.objects.create(
                        merchant=merchant,
                        amount_paise=-amount_paise,
                        entry_type=LedgerEntry.EntryType.DEBIT,
                        related_payout=payout,
                        description=f"Hold for payout {payout.id}",
                    )
                    response_body = _serialize_payout(payout)
                    response_status = 201
                    idem.payout = payout

                    # Enqueue worker AFTER the tx commits — never inside,
                    # or the worker could pick up the row before it is
                    # visible to other connections.
                    transaction.on_commit(
                        lambda pid=str(payout.id): _enqueue_process(pid)
                    )

            idem.response_body = response_body
            idem.response_status = response_status
            idem.save()
            return response_body, response_status

    except IntegrityError as exc:
        # Narrow: the only IntegrityError we are prepared to handle here is
        # the (merchant, key) UniqueViolation, i.e. a concurrent caller
        # already claimed this key. Any other integrity error (e.g. a
        # future migration adds a NOT NULL column or another unique
        # constraint) MUST propagate — silently swallowing it would mask
        # real bugs.
        cause = getattr(exc, "__cause__", None)
        if not isinstance(cause, UniqueViolation):
            raise
        constraint = getattr(getattr(cause, "diag", None), "constraint_name", "")
        if constraint and constraint != "uniq_merchant_key":
            raise
        # Fall through to Phase B.

    # ---- Phase B: another caller already claimed this key ---------------
    with transaction.atomic():
        idem = IdempotencyKey.objects.select_for_update().get(
            merchant_id=merchant_id, key=idempotency_key
        )
        if idem.request_hash != request_hash:
            raise IdempotencyConflict(merchant_id, idempotency_key)
        return idem.response_body, idem.response_status


def _enqueue_process(payout_id: str) -> None:
    """Defer-import then enqueue. Keeps the import out of services-module
    load-time so unit tests don't need Celery infrastructure to import this
    file."""
    from .tasks import process_payout

    process_payout.delay(payout_id)
