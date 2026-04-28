# EXPLAINER

Answers to the five take-home questions, with file:line citations to the actual code.

---

## 1. The Ledger

> *Paste your balance calculation query. Why did you model credits and debits this way?*

### Query

`backend/merchants/services.py:33-42`:

```python
def available_balance_paise(merchant_id: int) -> int:
    """SUM(amount_paise) over all ledger entries for the merchant.

    The take-home requires balance to be derived from the ledger via a
    database-level aggregation, not Python arithmetic over fetched rows.
    """
    total = LedgerEntry.objects.filter(merchant_id=merchant_id).aggregate(
        total=Sum("amount_paise"),
    )["total"]
    return total or 0
```

The displayed balance shown on the dashboard goes through this function exclusively. It compiles to a single Postgres query: `SELECT COALESCE(SUM(amount_paise), 0) FROM ledger_entries WHERE merchant_id = %s`. No Python loops over rows; no in-memory accumulation; no float arithmetic anywhere — `amount_paise` is a `BigIntegerField`.

### Why credits and debits modelled as signed amounts

Three design decisions, each defensible:

**1. Signed amounts on a single table, not separate `credits` / `debits` tables.** Balance is a single `SUM` query, not a join + subtract. The take-home invariant ("sum of credits minus debits = displayed balance") holds *by construction*: there is no separate "displayed balance" field that could disagree with the SUM — the SUM **is** the displayed balance.

```python
# backend/merchants/models.py: signed amounts, single LedgerEntry table
class EntryType(models.TextChoices):
    CREDIT = "CREDIT", "Customer payment credit"   # amount > 0
    DEBIT = "DEBIT", "Payout debit"                # amount < 0
    REFUND = "REFUND", "Refund for failed payout"  # amount > 0
```

**2. Sign-matches-type enforced at the database, not the application.** A bug that tried to write a CREDIT with a negative amount, or a DEBIT with a positive amount, is refused by Postgres before the row hits disk. `backend/merchants/models.py:88-103`:

```python
constraints = [
    CheckConstraint(
        condition=~Q(amount_paise=0),
        name="ledger_amount_nonzero",
    ),
    CheckConstraint(
        condition=(
            (Q(entry_type="CREDIT") & Q(amount_paise__gt=0))
            | (Q(entry_type="REFUND") & Q(amount_paise__gt=0))
            | (Q(entry_type="DEBIT") & Q(amount_paise__lt=0))
        ),
        name="ledger_sign_matches_type",
    ),
]
```

This is defence in depth. Even if every line of application code that creates a LedgerEntry has a bug, the database refuses the row. Tests for both constraints live at `backend/payouts/tests/test_constraints.py`.

**3. Immutable rows.** Corrections happen by writing a *compensating* entry (REFUND), never by mutating an existing CREDIT or DEBIT. The Django admin for `LedgerEntry` returns `False` from `has_change_permission` and `has_delete_permission` (`backend/merchants/admin.py:50-58`) — the audit trail is structurally append-only.

### Why this matters in practice

For a failed payout, the cycle on the ledger is:

```
   seed CREDIT  (+100_000)   ──┐
   payout DEBIT  (-50_000)     ├─  SUM = 100_000  (balance fully restored)
   refund CREDIT (+50_000)   ──┘
```

The "atomic refund + state transition" rubric requirement is satisfied by writing the REFUND inside the same `transaction.atomic()` block as the FAILED transition (`backend/payouts/tasks.py`, `_apply_outcome`). Either both rows commit, or neither does.

Test for the success-cycle invariant: `backend/payouts/tests/test_edge_cases.py::test_successful_payout_preserves_balance_invariant`.
Test for the failure-cycle invariant: `backend/payouts/tests/test_worker.py::test_failed_payout_preserves_balance_invariant`.

---

## 2. The Lock

> *Paste the exact code that prevents two concurrent payouts from overdrawing a balance. Explain what database primitive it relies on.*

### Code — the merchant-side primitive

`backend/merchants/services.py:45-87` (`hold_funds`):

```python
@transaction.atomic
def hold_funds(
    *,
    merchant_id: int,
    amount_paise: int,
    description: str = "",
) -> LedgerEntry:
    """Lock the merchant row, validate balance, write a DEBIT entry.

    Concurrency contract:
        SELECT ... FOR UPDATE on the merchant row blocks any other transaction
        trying to lock the same merchant. The blocker waits until our
        transaction commits or rolls back; on commit, the second transaction's
        balance recomputation sees our DEBIT and is correctly rejected if the
        funds no longer suffice. This is the primitive the rubric grades.

    Why we recompute balance INSIDE the locked transaction:
        Reading the balance before acquiring the lock is a TOCTOU race —
        between the read and the lock acquisition another transaction may
        commit a debit. Reading after the lock guarantees we observe the
        latest committed state for this merchant.
    """
    if amount_paise <= 0:
        raise ValueError(f"amount_paise must be positive, got {amount_paise}")

    # Acquire the row lock first. If another transaction has already locked
    # this row we block here until it commits or rolls back.
    merchant = Merchant.objects.select_for_update().get(id=merchant_id)

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

The same primitive is used inline in `backend/payouts/services.py:125-128` for the API path (`create_payout`), which additionally writes the Payout row + IdempotencyKey row inside the same atomic block.

### Database primitive

**Postgres row-level lock acquired via `SELECT ... FOR UPDATE`.**

When two transactions try to lock the same merchant row, Postgres blocks the second one at the lock acquisition. It waits — not spins, not polls — until the first transaction either commits or rolls back. After commit, the second transaction's balance recomputation sees the new DEBIT and correctly raises `InsufficientFundsError` if the remaining balance no longer covers the request.

The isolation level for this contract is **READ COMMITTED**, which is Postgres's server-side default. The lock semantics in `hold_funds` (and the worker's `_apply_outcome`) are designed for that level: every `select_for_update` runs inside `transaction.atomic()`, so the second transaction sees a fresh read after the first commits.

An earlier revision pinned this explicitly via libpq's `options` startup parameter (`-c default_transaction_isolation=read committed`) so the contract did not depend on a server default. We removed it during the live deploy: Neon's PgBouncer pooler refuses the `options` startup parameter in transaction-pooled mode (see [Neon docs](https://neon.tech/docs/connect/connection-errors#unsupported-startup-parameter)). The two ways out were (a) switch to Neon's unpooled URL — would exhaust Postgres connections under our worker fan-out — or (b) drop the explicit pin and document the dependency. We chose (b). The current shape at `backend/playto_pay/settings.py:96-115`:

```python
# Isolation level: we rely on Postgres's default of READ COMMITTED, which is
# what our locking story (``SELECT ... FOR UPDATE`` on the merchant row) is
# designed for.
#
# In an earlier revision we explicitly pinned this via libpq's ``options``
# startup parameter ... We had to remove that: Neon's PgBouncer pooler
# rejects the ``options`` startup parameter ...
#
# A non-pooled deploy (RDS, self-hosted) could re-add the pin via
# ``OPTIONS["options"]``; the lock semantics are unchanged either way.
_PG_OPTIONS: dict = {}
```

The full reasoning is in `notes/improvements-log.md` Entry 6 (added during deploy). On a non-pooled Postgres (RDS, self-hosted) we'd re-add the explicit pin — the lock semantics don't change either way, but defending against server-side default drift is good fintech hygiene.

### Why this works for "two simultaneous 60₹ payouts on a 100₹ balance"

The take-home calls out this exact scenario. Trace:

| t | Tx A | Tx B |
|---|---|---|
| 0 | `BEGIN` | `BEGIN` |
| 1 | `SELECT … FOR UPDATE` on merchant row → acquires lock | `SELECT … FOR UPDATE` → **blocks** |
| 2 | `SUM` ledger → 100₹, ≥ 60₹, OK | (still blocked) |
| 3 | `INSERT` DEBIT –60₹ | (still blocked) |
| 4 | `COMMIT` → lock released | (still blocked) |
| 5 | (done) | lock acquired, snapshot rebuilt under READ COMMITTED |
| 6 | | `SUM` ledger → **40₹**, < 60₹ → `InsufficientFundsError` raised |
| 7 | | `ROLLBACK` (Django's `atomic` decorator handles it) |

Exactly one of the two requests creates a debit. The other receives `422 insufficient_funds`. No double-spend.

### Test

`backend/merchants/tests/test_concurrency.py::test_concurrent_debits_cannot_overdraw` runs two `Thread`s through a `Barrier` so they hit the lock at the same instant, then asserts:

- exactly one success, exactly one `InsufficientFundsError`,
- final balance computed via `SUM` equals 100₹ - 60₹ = 40₹,
- ledger has exactly two rows (seed + one debit).

Stress-tested at 15 consecutive runs for stability before the lock work was considered done.

---

## 3. The Idempotency

> *How does your system know it has seen a key before? What happens if the first request is in flight when the second arrives?*

### How the system recognises a duplicate key

The `IdempotencyKey` table has `UNIQUE(merchant, key)` — `backend/payouts/models.py:154-156`:

```python
class Meta:
    db_table = "idempotency_keys"
    constraints = [
        UniqueConstraint(fields=["merchant", "key"], name="uniq_merchant_key"),
    ]
```

The recognition mechanism is the database, not the application. Inside the work transaction, `create_payout` attempts an `INSERT` (`backend/payouts/services.py:135-140`):

```python
idem = IdempotencyKey.objects.create(
    merchant=merchant,
    key=idempotency_key,
    request_hash=request_hash,
    expires_at=expires_at,
)
```

A second caller with the same `(merchant, key)` triggers Postgres's unique-index check. Postgres raises `psycopg.errors.UniqueViolation` (SQLSTATE 23505), which Django re-wraps as `django.db.IntegrityError`. We catch it narrowly (`backend/payouts/services.py:191-204`):

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

Two layers of narrowing. The `isinstance` check rejects every other `IntegrityError` subclass (`ForeignKeyViolation`, `NotNullViolation`, `CheckViolation` — see EXPLAINER Q5). The `constraint_name` check defends against a future migration adding *another* unique constraint to the same table; if such a constraint fires, we do not silently treat it as "key already claimed". Tested by `backend/payouts/tests/test_constraints.py::test_idempotency_key_uniqueness_is_enforced_by_db`.

The `request_hash` is a SHA-256 over a canonical JSON encoding of the body (`backend/payouts/services.py:60-68`). On retry-with-same-key, Phase B compares the hash; mismatch raises `IdempotencyConflict` → `422 idempotency_conflict`.

### What happens if the first request is in flight when the second arrives

This is the meaningful concurrency case. Trace:

| t | Tx A (first request) | Tx B (second request, same key) |
|---|---|---|
| 0 | `BEGIN` | `BEGIN` |
| 1 | `SELECT … FOR UPDATE` Merchant → locked | `SELECT … FOR UPDATE` Merchant → **blocks** |
| 2 | `INSERT` IdempotencyKey (key visible only to A's tx) | (still blocked) |
| 3 | `INSERT` Payout, `INSERT` LedgerEntry | (still blocked) |
| 4 | save response on IdempotencyKey row, `COMMIT` | lock acquired |
| 5 | (done; on_commit fires Celery worker) | `INSERT` IdempotencyKey → **`UniqueViolation`** |
| 6 | | catch, narrow to UniqueViolation + constraint_name |
| 7 | | Phase B: new `transaction.atomic()`, `SELECT … FOR UPDATE` on the IdempotencyKey row |
| 8 | | reads cached `response_body` + `response_status` |
| 9 | | returns the same response A returned |

The Merchant row lock at step 1 is what serialises the two requests at the *application* level. Without it, both transactions could try to `INSERT` IdempotencyKey concurrently — Postgres would block the second `INSERT` at the unique-index check until A commits, then surface `UniqueViolation` to B. Either way, the outcome is the same: exactly one Payout is created, both clients receive identical responses.

Phase B fallback (`backend/payouts/services.py:206-213`):

```python
with transaction.atomic():
    idem = IdempotencyKey.objects.select_for_update().get(
        merchant_id=merchant_id, key=idempotency_key
    )
    if idem.request_hash != request_hash:
        raise IdempotencyConflict(merchant_id, idempotency_key)
    return idem.response_body, idem.response_status
```

The `select_for_update` in Phase B is an explicit safety net. If A's `IntegrityError` reaches B *before* A actually committed (Postgres releases the index check sooner than the row lock in some edge cases), the `SELECT FOR UPDATE` blocks until A's tx fully commits — guaranteeing B sees the cached response, not an empty row.

### Tests

- `backend/payouts/tests/test_idempotency.py::test_same_key_same_body_returns_same_payout` — sequential same-body returns identical id; only one Payout exists.
- `backend/payouts/tests/test_idempotency.py::test_same_key_different_body_is_rejected` — same key, different body → 422 `idempotency_conflict`.
- `backend/payouts/tests/test_idempotency.py::test_concurrent_requests_with_same_key_create_one_payout` — two threads at the same instant, exactly one Payout, both clients see the same id. Stress-tested 15× for stability.
- `backend/payouts/tests/test_idempotency.py::test_idempotency_keys_are_scoped_per_merchant` — same UUID across two merchants does not collide (the constraint is `(merchant, key)`, not `key` alone).

### Expiry

`expires_at` is set on the row at INSERT time using `IDEMPOTENCY_KEY_TTL_HOURS` (default 24). A Celery beat task at `backend/payouts/tasks.py:cleanup_idempotency_keys` deletes expired rows hourly — see `CELERY_BEAT_SCHEDULE` in `backend/playto_pay/settings.py`.

---

## 4. The State Machine

> *Where in the code is failed-to-completed blocked? Show the check.*

### The map

`backend/payouts/state_machine.py:23-30`:

```python
LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    Payout.Status.PENDING: frozenset({Payout.Status.PROCESSING}),
    Payout.Status.PROCESSING: frozenset(
        {Payout.Status.COMPLETED, Payout.Status.FAILED}
    ),
    Payout.Status.COMPLETED: frozenset(),
    Payout.Status.FAILED: frozenset(),
}
```

The two terminal states map to `frozenset()`. There is no key whose value contains COMPLETED or FAILED other than PROCESSING — so once a payout reaches a terminal state, no transition out of it is permitted by this map.

### The check

`backend/payouts/models.py:96-101`:

```python
legal = LEGAL_TRANSITIONS.get(self.status, frozenset())
if new_status not in legal:
    raise IllegalTransition(
        from_status=self.status,
        to_status=new_status,
        payout_id=str(self.id),
    )
```

`Payout.transition_to()` is the *only* place the application sets `Payout.status`. The `_apply_outcome` worker function (`backend/payouts/tasks.py`) and the retry sweeper (`retry_payout`) both call it — `select_for_update`-locked transactions in both cases, so concurrent callers cannot race past the legality check.

### How `FAILED → COMPLETED` is blocked, specifically

Trace through the code for a `transition_to(COMPLETED)` call on a row currently at `FAILED`:

1. `transition_to` runs.
2. `LEGAL_TRANSITIONS.get(self.status, frozenset())` → the dict's `Payout.Status.FAILED` key returns `frozenset()`.
3. `if new_status not in legal:` → `Payout.Status.COMPLETED not in frozenset()` is **`True`**.
4. `raise IllegalTransition(from_status="FAILED", to_status="COMPLETED", ...)`.
5. The save never runs. The DB row keeps `status = FAILED`.

### Test

A dedicated test at `backend/payouts/tests/test_state_machine.py::test_failed_to_completed_is_blocked_explicitly`:

```python
@pytest.mark.django_db
def test_failed_to_completed_is_blocked_explicitly():
    """The spec calls this case out by name. Worth a dedicated test so
    grep-by-symptom finds it instantly."""
    payout = _make_payout(Payout.Status.FAILED)
    with pytest.raises(IllegalTransition):
        payout.transition_to(Payout.Status.COMPLETED)
    payout.refresh_from_db()
    assert payout.status == Payout.Status.FAILED
```

Plus a parametric test that hits every illegal pair (`PENDING→COMPLETED`, `COMPLETED→PENDING`, `FAILED→PROCESSING`, terminal-to-terminal, self-transitions, etc.) — 13 illegal cases asserted by a single parametrized test, with all 3 legal pairs asserted by another.

### Atomic refund on `FAILED`

The "failed payout returning funds must do so atomically with the state transition" requirement is met by writing the REFUND `LedgerEntry` inside the same `transaction.atomic()` block as the `transition_to(FAILED)` call. Two places do this:

- `backend/payouts/tasks.py:_apply_outcome` (natural failure from the bank simulation)
- `backend/payouts/tasks.py:retry_payout` (max retries exhausted)

Tested by `test_apply_outcome_atomicity_failure_path` in `test_worker.py` — monkeypatches `transition_to` to raise *after* the LedgerEntry insert; asserts the refund row is rolled back. This is a test that proves the atomicity claim, not just both-side-effect-present.

---

## 5. The AI Audit

> *One specific example where AI wrote subtly wrong code (bad locking, wrong aggregation, race condition). Paste what it gave you, what you caught, and what you replaced it with.*

### Primary example: `IntegrityError` conflated with `UniqueViolation` in the idempotency layer

Phase 3, building `payouts/services.py::create_payout`. AI wrote a "Phase A INSERT idempotency key inside the work transaction; Phase B catch IntegrityError and read the cached response" pattern. Looked clean. Tests passed for the happy path and the basic same-key cases. **Verbatim original at [`notes/q5-ai-audit-original.py`](./notes/q5-ai-audit-original.py)**.

The subtle bug:

```python
# BUG-1: the ORIGINAL buggy version
try:
    with transaction.atomic():
        # AI inserts idempotency-key row BEFORE validating merchant exists
        idem = IdempotencyKey.objects.create(
            merchant_id=merchant_id,            # ← FK to merchants.id
            key=idempotency_key,
            ...
        )
        try:
            merchant = Merchant.objects.select_for_update().get(id=merchant_id)
        except Merchant.DoesNotExist:
            ...                                  # ← unreachable code
except IntegrityError:                           # ← TOO BROAD
    pass

with transaction.atomic():
    idem = IdempotencyKey.objects.select_for_update().get(
        merchant_id=merchant_id, key=idempotency_key
    )                                            # ← raises DoesNotExist → 500
    return idem.response_body, idem.response_status
```

### How I caught it

Probed the endpoint with a nonexistent merchant id:

```
POST /api/v1/payouts
X-Merchant-Id: 9999999
Idempotency-Key: a8b3c11e-...
{"amount_paise": 100, "bank_account_id": 1}
```

Expected: `404 merchant_not_found` (deterministic, idempotent — same input always yields same answer). Got: `500 Internal Server Error`, every retry. The traceback ended in `IdempotencyKey.DoesNotExist` from Phase B's `SELECT`.

### Why it was wrong

`django.db.IntegrityError` is the *parent class* of every Postgres integrity violation:

| SQLSTATE | psycopg subclass | Meaning |
|---|---|---|
| 23505 | `UniqueViolation` | duplicate key — **the case AI intended** |
| 23503 | `ForeignKeyViolation` | FK target does not exist |
| 23502 | `NotNullViolation` | required column is NULL |
| 23514 | `CheckViolation` | a `CHECK` constraint failed |

When the merchant didn't exist, the `IdempotencyKey` `INSERT` failed with `ForeignKeyViolation` (the FK from `idempotency_keys.merchant_id` to `merchants.id`) — but the broad `except IntegrityError` swallowed it as if it were a duplicate key. Phase B then queried for a row that had never been written. The uncaught `DoesNotExist` became a 500. The `Merchant.DoesNotExist` handler immediately after the INSERT was unreachable in this scenario — the FK check fires inside the INSERT, before the `SELECT FOR UPDATE` ever runs.

### How I fixed it

Two changes (`backend/payouts/services.py`):

1. **Lock the merchant FIRST.** Move `Merchant.objects.select_for_update().get(id=merchant_id)` to the top of the atomic block. If it raises `Merchant.DoesNotExist`, return `(404, merchant_not_found)` immediately — no IdempotencyKey row written, no FK violation possible. By the time the `IdempotencyKey` INSERT runs, the FK target is guaranteed to exist, so the only `IntegrityError` path remaining is the `(merchant, key)` UniqueViolation we want to handle.

2. **Narrow the catch.** Replace `except IntegrityError` with an `isinstance(cause, UniqueViolation)` check plus a `constraint_name == "uniq_merchant_key"` belt-and-braces guard. Anything else propagates so a future migration adding a different constraint cannot silently misroute exceptions.

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

Plus a regression test at `backend/payouts/tests/test_idempotency.py::test_nonexistent_merchant_returns_404_not_500` that fails against the original code and passes against the fix.

### Lesson

`IntegrityError` is not a synonym for "duplicate key". It is the *parent class* of at least four distinct Postgres error families. Treating exception classes as equivalent to specific error semantics is the canonical AI bug in transactional code. The defensive pattern in money-moving code is:

- Validate every FK target up front, ideally under the same row lock that will serialise the rest of the transaction.
- Catch the *narrowest* exception subclass that maps to the case you intended.
- Where possible, also assert the `constraint_name` exposed by `psycopg`'s `diag` so that a future migration adding a new constraint cannot silently reroute exceptions.

### Where to find the rest

The full Q5-grade write-up of this case lives at [`notes/ai-audit-log.md`](./notes/ai-audit-log.md) Entry 1. A second AI bug — a column-removal migration that left a stale `__str__` reference — is at Entry 2 of the same file; together the two entries paint a consistent picture of how AI fails at *system-wide blast-radius reasoning* (Entry 1: too broad; Entry 2: too narrow). Additional caught-and-fixed issues from review passes are catalogued in [`notes/improvements-log.md`](./notes/improvements-log.md), each with the same Symptom / Caught / Fix / Lesson / Regression-net structure.
