# EXPLAINER

Answers to the five take-home questions. Each cites file:line so a reviewer can navigate to the code.

---

## 1. The Ledger

> *Paste your balance calculation query. Why did you model credits and debits this way?*

`backend/merchants/services.py:33-42`:

```python
def available_balance_paise(merchant_id: int) -> int:
    total = LedgerEntry.objects.filter(merchant_id=merchant_id).aggregate(
        total=Sum("amount_paise"),
    )["total"]
    return total or 0
```

One Postgres query: `SELECT COALESCE(SUM(amount_paise), 0) FROM ledger_entries WHERE merchant_id = %s`. No Python iteration over rows; `amount_paise` is a `BigIntegerField` (no floats anywhere).

**Why signed amounts on a single table** — balance is the SUM, not a join + subtract. The take-home invariant ("sum of credits minus debits = displayed balance") holds *by construction*: there is no separate "displayed balance" field that could disagree with the SUM — the SUM **is** the balance.

```python
# backend/merchants/models.py
class EntryType(models.TextChoices):
    CREDIT = "CREDIT", "Customer payment credit"   # amount > 0
    DEBIT  = "DEBIT",  "Payout debit"              # amount < 0
    REFUND = "REFUND", "Refund for failed payout"  # amount > 0
```

**Why sign-matches-type at the database level, not the application** (`backend/merchants/models.py:88-103`) — a CHECK constraint refuses any row where the sign doesn't match the type, so even a buggy code path can't write a CREDIT with a negative amount or a DEBIT with a positive one. Defense in depth.

**Why immutable rows** — corrections happen by writing a *compensating* REFUND, never by mutating an existing CREDIT or DEBIT. The Django admin returns `False` from `has_change_permission` and `has_delete_permission` (`backend/merchants/admin.py:50-58`) — the audit trail is structurally append-only.

For a failed payout the cycle is `CREDIT(+50_000) → DEBIT(-50_000) → REFUND(+50_000)`, SUM = 50_000 (balance fully restored). REFUND + state transition land in the same `transaction.atomic()` block (`backend/payouts/tasks.py:_apply_outcome`), satisfying the rubric's atomicity requirement.

---

## 2. The Lock

> *Paste the exact code that prevents two concurrent payouts from overdrawing a balance. Explain what database primitive it relies on.*

`backend/merchants/services.py:45-87` (`hold_funds`):

```python
@transaction.atomic
def hold_funds(*, merchant_id, amount_paise, description=""):
    if amount_paise <= 0:
        raise ValueError(f"amount_paise must be positive, got {amount_paise}")

    # Acquire the row lock first. If another transaction has already
    # locked this row we block here until it commits or rolls back.
    merchant = Merchant.objects.select_for_update().get(id=merchant_id)

    # Recompute balance INSIDE the lock — anything else is TOCTOU.
    available = available_balance_paise(merchant_id)
    if available < amount_paise:
        raise InsufficientFundsError(merchant_id, amount_paise, available)

    return LedgerEntry.objects.create(
        merchant=merchant,
        amount_paise=-amount_paise,
        entry_type=LedgerEntry.EntryType.DEBIT,
        description=description,
    )
```

The same primitive is used inline in `backend/payouts/services.py:125-128` for the API (`create_payout`), which additionally writes the Payout + IdempotencyKey rows in the same atomic block.

### Database primitive

**Postgres row-level lock acquired via `SELECT ... FOR UPDATE`.** When two transactions try to lock the same merchant row, Postgres blocks the second one — it waits, doesn't spin, until the first commits or rolls back. After commit, the second transaction's balance recomputation sees the new DEBIT and correctly raises `InsufficientFundsError` if the remaining balance no longer covers the request.

**Isolation level: READ COMMITTED**, which is Postgres's default. The lock semantics are designed for it: every `select_for_update` runs inside `transaction.atomic()`, so the second transaction sees a fresh snapshot after the first commits. An earlier revision pinned this explicitly via libpq's `options` startup parameter; we removed the pin during deploy because Neon's PgBouncer pooler refuses `options` in transaction-pooled mode (see `backend/playto_pay/settings.py:96-115` for the full reasoning, plus `notes/improvements-log.md` Entry 6). On a non-pooled Postgres we'd re-add the pin — the lock semantics are unchanged either way.

**Test:** `backend/merchants/tests/test_concurrency.py::test_concurrent_debits_cannot_overdraw` — two threads through a `Barrier` hit the lock at the same instant; asserts exactly one success + one `InsufficientFundsError`, and the final SUM matches the expected post-debit balance. Stress-tested at 15 consecutive runs.

---

## 3. The Idempotency

> *How does your system know it has seen a key before? What happens if the first request is in flight when the second arrives?*

The `IdempotencyKey` table has `UNIQUE(merchant, key)` — `backend/payouts/models.py:154-156`:

```python
constraints = [
    UniqueConstraint(fields=["merchant", "key"], name="uniq_merchant_key"),
]
```

Recognition is the database, not the application. Inside the work transaction, `create_payout` attempts an INSERT (`backend/payouts/services.py:135-140`):

```python
idem = IdempotencyKey.objects.create(
    merchant=merchant, key=idempotency_key,
    request_hash=request_hash, expires_at=expires_at,
)
```

A second caller with the same `(merchant, key)` triggers Postgres's unique-index check, which raises `psycopg.errors.UniqueViolation` (SQLSTATE 23505). We catch it narrowly (`backend/payouts/services.py:191-204`):

```python
except IntegrityError as exc:
    cause = getattr(exc, "__cause__", None)
    if not isinstance(cause, UniqueViolation):
        raise
    constraint = getattr(getattr(cause, "diag", None), "constraint_name", "")
    if constraint and constraint != "uniq_merchant_key":
        raise
    # Fall through to Phase B.
```

Two layers of narrowing: the `isinstance` rejects every other `IntegrityError` subclass (`ForeignKeyViolation`, `NotNullViolation`, `CheckViolation` — see Q5), and the `constraint_name` check ensures a future migration adding a different unique constraint won't silently reroute here. The `request_hash` is a SHA-256 over canonical-JSON of the body; mismatch on retry returns `422 idempotency_conflict`.

### What happens if the first request is in flight when the second arrives

| t | Tx A (first) | Tx B (second, same key) |
|---|---|---|
| 0 | `BEGIN` | `BEGIN` |
| 1 | `SELECT … FOR UPDATE` Merchant → locked | `SELECT … FOR UPDATE` Merchant → **blocks** |
| 2 | INSERT IdempotencyKey, Payout, LedgerEntry | (still blocked) |
| 3 | save response, `COMMIT` | lock acquired |
| 4 | (done; on_commit fires Celery worker) | INSERT IdempotencyKey → **`UniqueViolation`** |
| 5 | | catch, narrow, Phase B |
| 6 | | `SELECT … FOR UPDATE` IdempotencyKey, return cached response |

The Merchant row lock at step 1 is what serialises the two requests. Phase B fallback at `backend/payouts/services.py:206-213`:

```python
with transaction.atomic():
    idem = IdempotencyKey.objects.select_for_update().get(
        merchant_id=merchant_id, key=idempotency_key
    )
    if idem.request_hash != request_hash:
        raise IdempotencyConflict(merchant_id, idempotency_key)
    return idem.response_body, idem.response_status
```

The `select_for_update` in Phase B is an explicit safety net: if the `UniqueViolation` reaches B before A's row lock fully releases, this `SELECT FOR UPDATE` blocks until A's tx commits — guaranteeing B sees the cached response, not an empty row.

**Expiry:** `expires_at` is set on the row at INSERT time using `IDEMPOTENCY_KEY_TTL_HOURS` (default 24). A Celery beat task (`payouts/tasks.py:cleanup_idempotency_keys`) deletes expired rows hourly.

---

## 4. The State Machine

> *Where in the code is failed-to-completed blocked? Show the check.*

The map at `backend/payouts/state_machine.py:23-30`:

```python
LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    Payout.Status.PENDING:    frozenset({Payout.Status.PROCESSING}),
    Payout.Status.PROCESSING: frozenset({Payout.Status.COMPLETED, Payout.Status.FAILED}),
    Payout.Status.COMPLETED:  frozenset(),
    Payout.Status.FAILED:     frozenset(),
}
```

Both terminal states map to `frozenset()`. The check at `backend/payouts/models.py:96-101`:

```python
legal = LEGAL_TRANSITIONS.get(self.status, frozenset())
if new_status not in legal:
    raise IllegalTransition(
        from_status=self.status,
        to_status=new_status,
        payout_id=str(self.id),
    )
```

`Payout.transition_to()` is the *only* place the application sets `Payout.status`. Both `_apply_outcome` (worker) and `retry_payout` (sweeper) call it under `select_for_update`-locked transactions, so concurrent callers can't race past the legality check.

### How `FAILED → COMPLETED` is blocked, specifically

For a `transition_to(COMPLETED)` call on a row at `FAILED`:

1. `LEGAL_TRANSITIONS.get(self.status, frozenset())` → for key `FAILED`, returns `frozenset()`.
2. `if new_status not in legal:` → `COMPLETED not in frozenset()` is **`True`**.
3. `raise IllegalTransition(from_status="FAILED", to_status="COMPLETED", ...)`.
4. The save never runs. The DB row stays at `FAILED`.

A dedicated test at `backend/payouts/tests/test_state_machine.py::test_failed_to_completed_is_blocked_explicitly` exercises this exact path; a parametric test alongside it covers all 13 illegal transitions plus all 3 legal ones.

The "atomic refund on FAILED" rubric requirement is met by writing the REFUND `LedgerEntry` inside the same `transaction.atomic()` block as the `transition_to(FAILED)` call (`tasks.py:_apply_outcome` for natural failure, `tasks.py:retry_payout` for max-retries). Tested by `test_apply_outcome_atomicity_failure_path` — monkeypatches `transition_to` to raise *after* the LedgerEntry insert, asserts the refund row rolls back. This is a test that *proves* atomicity, not just that both side-effects landed.

---

## 5. The AI Audit

> *One specific example where AI wrote subtly wrong code (bad locking, wrong aggregation, race condition). Paste what it gave you, what you caught, and what you replaced it with.*

**`IntegrityError` conflated with `UniqueViolation` in the idempotency layer.** Phase 3, building `payouts/services.py::create_payout`. AI wrote a "Phase A INSERT idempotency key inside the work tx; Phase B catch IntegrityError and read cached response" pattern. Tests passed for the happy path and the basic same-key cases. **Verbatim original at [`notes/q5-ai-audit-original.py`](./notes/q5-ai-audit-original.py).**

The buggy shape:

```python
try:
    with transaction.atomic():
        # AI inserted the idempotency-key row BEFORE validating the merchant.
        idem = IdempotencyKey.objects.create(
            merchant_id=merchant_id,            # ← FK to merchants.id
            key=idempotency_key, ...
        )
        try:
            merchant = Merchant.objects.select_for_update().get(id=merchant_id)
        except Merchant.DoesNotExist:
            ...                                  # ← unreachable
except IntegrityError:                           # ← TOO BROAD
    pass

with transaction.atomic():
    idem = IdempotencyKey.objects.select_for_update().get(
        merchant_id=merchant_id, key=idempotency_key
    )                                            # ← raises DoesNotExist → 500
    return idem.response_body, idem.response_status
```

### How I caught it

Probed with a nonexistent merchant id:

```
POST /api/v1/payouts
X-Merchant-Id: 9999999
Idempotency-Key: a8b3c11e-...
```

Expected: `404 merchant_not_found`, deterministic. Got: `500 Internal Server Error`, every retry. Traceback ended in `IdempotencyKey.DoesNotExist` from Phase B's SELECT.

### Why it was wrong

`django.db.IntegrityError` is the *parent class* of every Postgres integrity violation:

| SQLSTATE | psycopg subclass | Meaning |
|---|---|---|
| 23505 | `UniqueViolation` | duplicate key — **the case AI intended** |
| 23503 | `ForeignKeyViolation` | FK target does not exist |
| 23502 | `NotNullViolation` | required column is NULL |
| 23514 | `CheckViolation` | a `CHECK` constraint failed |

When the merchant didn't exist, the `IdempotencyKey` INSERT failed with `ForeignKeyViolation` (the FK from `idempotency_keys.merchant_id` → `merchants.id`). The broad `except IntegrityError` swallowed it as if it were a duplicate key. Phase B then queried for a row that had never been written. The uncaught `DoesNotExist` became a 500. The `Merchant.DoesNotExist` handler immediately after the INSERT was unreachable — the FK check fires inside the INSERT itself, before `SELECT FOR UPDATE` ever runs.

### How I fixed it

Two changes to `backend/payouts/services.py`:

1. **Lock the merchant first.** Move `select_for_update().get(id=merchant_id)` to the top of the atomic block. If `Merchant.DoesNotExist` raises, return `(404, merchant_not_found)` immediately — no IdempotencyKey row written, no FK violation possible. By the time the IdempotencyKey INSERT runs, the FK target is guaranteed to exist, so the only `IntegrityError` path remaining is the `(merchant, key)` UniqueViolation we want to handle.

2. **Narrow the catch.** Replace `except IntegrityError` with `isinstance(cause, UniqueViolation)` plus a `constraint_name == "uniq_merchant_key"` belt-and-braces guard. Anything else propagates so a future migration adding a different unique constraint cannot silently misroute exceptions.

Final fix at `backend/payouts/services.py:117-204`:

```python
try:
    with transaction.atomic():
        try:
            merchant = Merchant.objects.select_for_update().get(id=merchant_id)
        except Merchant.DoesNotExist:
            return {"error": "merchant_not_found"}, 404

        idem = IdempotencyKey.objects.create(
            merchant=merchant, key=idempotency_key, ...
        )
        # ... rest of work ...

except IntegrityError as exc:
    cause = getattr(exc, "__cause__", None)
    if not isinstance(cause, UniqueViolation):
        raise
    constraint = getattr(getattr(cause, "diag", None), "constraint_name", "")
    if constraint and constraint != "uniq_merchant_key":
        raise
    # Fall through to Phase B.
```

Regression test: `backend/payouts/tests/test_idempotency.py::test_nonexistent_merchant_returns_404_not_500` — fails against the original code, passes against the fix.

### Lesson

`IntegrityError` is not a synonym for "duplicate key" — it is the parent class of at least four distinct Postgres error families. The defensive pattern in money-moving code: validate every FK target up front under the same row lock that serialises the work, catch the *narrowest* exception subclass for the case you intended, and assert on the `constraint_name` from `psycopg`'s `diag` so future schema changes can't silently reroute exceptions.

A second AI bug — a column-removal migration that left a stale `__str__` reference, causing `AttributeError` on any code path that printed an `IdempotencyKey` — is documented at [`notes/ai-audit-log.md`](./notes/ai-audit-log.md) Entry 2. Together the two entries paint a consistent picture of how AI fails at *system-wide blast-radius reasoning*. Additional caught-and-fixed issues are catalogued in [`notes/improvements-log.md`](./notes/improvements-log.md).
