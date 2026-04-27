"""EXHIBIT for EXPLAINER.md Question 5 — "The AI Audit".

This file is the ORIGINAL, BUGGY version of payouts/services.py as initially
generated. Preserved here verbatim for the EXPLAINER write-up. DO NOT IMPORT
THIS — it is dead code kept solely as evidence of the bug that was caught and
fixed.

============================================================================
THE BUG (lines 104-195 below):
----------------------------------------------------------------------------
``except IntegrityError:`` at line 184 conflates THREE distinct Postgres
integrity violations into one handler:

    SQLSTATE 23505  unique_violation   <-- the case I intended
    SQLSTATE 23503  foreign_key_violation
    SQLSTATE 23502  not_null_violation
    SQLSTATE 23514  check_violation

All four raise ``django.db.IntegrityError`` (the parent exception). The
``IdempotencyKey`` row's INSERT at line 107 references ``merchants.id`` via
the ``merchant_id`` FK. When the caller passes a merchant_id that does NOT
exist, Postgres rejects the INSERT with ForeignKeyViolation (23503) — not
UniqueViolation. My handler catches it, falls through to "Phase B", and
queries for a row that was never persisted. The ``DoesNotExist`` from that
SELECT is uncaught -> 500 Internal Server Error.

============================================================================
TRIGGER:
----------------------------------------------------------------------------
    POST /api/v1/payouts
    X-Merchant-Id: 99999          # nonexistent
    Idempotency-Key: <any uuid>
    body: {"amount_paise": 100, "bank_account_id": 1}

Expected:  404 merchant_not_found, deterministically.
Got:       500 every time. Idempotency broken on the 404 path.

The ``Merchant.DoesNotExist`` handler at lines 122-133 below is DEAD CODE —
the FK violation at line 107 fires first and short-circuits the flow before
the SELECT FOR UPDATE on line 118 ever runs.

============================================================================
THE FIX:
----------------------------------------------------------------------------
1. Lock the merchant FIRST (line 107 below moves to the top of the atomic
   block). 404 immediately if not found, no idempotency row written. The
   INSERT at line 107 then runs only when the FK target is guaranteed to
   exist, so a future IntegrityError there can ONLY mean a duplicate
   (merchant, key) — the case Phase B is designed for.

2. Narrow the catch to ``psycopg.errors.UniqueViolation`` and verify the
   constraint name is "uniq_merchant_key". Anything else bubbles up.

3. Delete the now-unreachable ``Merchant.DoesNotExist`` handler.

4. Delete the ``IdempotencyKey.Status`` enum + field — IN_PROGRESS was never
   observable by other connections (the entire flow commits atomically), so
   COMPLETED was the only meaningful state. Removing dead state.

============================================================================
LESSON:
----------------------------------------------------------------------------
``IntegrityError`` is not a synonym for "duplicate key". A senior engineer
must catch the specific Postgres exception subclass (or the SQLSTATE / the
constraint name). The narrower the catch, the harder it is for a future
schema change to silently misroute exceptions.
============================================================================
"""

# --- BUGGY ORIGINAL BELOW THIS LINE -----------------------------------------

import hashlib
import json
import logging
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from merchants.models import BankAccount, LedgerEntry, Merchant
from merchants.services import available_balance_paise

from .models import IdempotencyKey, Payout

logger = logging.getLogger("playto.payouts")


class IdempotencyConflict(Exception):
    def __init__(self, merchant_id: int, key: uuid.UUID):
        self.merchant_id = merchant_id
        self.key = key
        super().__init__(
            f"merchant {merchant_id}: idempotency key {key} reused with "
            f"a different request body"
        )


def _hash_body(body: dict) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _serialize_payout(p):
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
):
    body = {
        "amount_paise": amount_paise,
        "bank_account_id": bank_account_id,
    }
    request_hash = _hash_body(body)
    expires_at = timezone.now() + timedelta(
        hours=settings.IDEMPOTENCY_KEY_TTL_HOURS
    )

    # ---- Phase A: claim + work ------------------------------------------
    try:
        with transaction.atomic():
            # BUG: this INSERT runs BEFORE merchant existence is validated.
            # If merchant_id doesn't exist, the FK constraint fires here as
            # ForeignKeyViolation (a subclass of IntegrityError, NOT
            # UniqueViolation), and the broad except below swallows it.
            idem = IdempotencyKey.objects.create(
                merchant_id=merchant_id,
                key=idempotency_key,
                request_hash=request_hash,
                status=IdempotencyKey.Status.IN_PROGRESS,
                expires_at=expires_at,
            )

            # DEAD CODE: this branch is unreachable for nonexistent merchants
            # because the FK violation at the INSERT above fires first and
            # short-circuits.
            try:
                merchant = Merchant.objects.select_for_update().get(
                    id=merchant_id
                )
            except Merchant.DoesNotExist:
                response_body, response_status = (
                    {"error": "merchant_not_found"},
                    404,
                )
                idem.response_body = response_body
                idem.response_status = response_status
                idem.status = IdempotencyKey.Status.COMPLETED
                idem.save(
                    update_fields=["response_body", "response_status", "status"]
                )
                return response_body, response_status

            try:
                bank_account = BankAccount.objects.get(
                    id=bank_account_id, merchant_id=merchant_id
                )
            except BankAccount.DoesNotExist:
                response_body, response_status = (
                    {"error": "bank_account_not_found"},
                    404,
                )
            else:
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
                    transaction.on_commit(
                        lambda pid=str(payout.id): _enqueue_process(pid)
                    )

            idem.response_body = response_body
            idem.response_status = response_status
            idem.status = IdempotencyKey.Status.COMPLETED
            idem.save()
            return response_body, response_status

    except IntegrityError:
        # BUG: too broad. Catches ForeignKeyViolation as if it were a
        # UniqueViolation. Falls through to Phase B with a row that was
        # never persisted -> Phase B's SELECT raises DoesNotExist -> 500.
        pass

    # ---- Phase B: another caller already claimed this key ---------------
    with transaction.atomic():
        # If we reached here from the FK-violation path above, the row does
        # not exist and this raises DoesNotExist -> uncaught -> 500.
        idem = IdempotencyKey.objects.select_for_update().get(
            merchant_id=merchant_id, key=idempotency_key
        )
        if idem.request_hash != request_hash:
            raise IdempotencyConflict(merchant_id, idempotency_key)
        return idem.response_body, idem.response_status


def _enqueue_process(payout_id: str):
    from .tasks import process_payout
    process_payout.delay(payout_id)
