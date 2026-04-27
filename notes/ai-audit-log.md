# AI Audit log

A running log of bugs that AI-generated code introduced and that I caught in
review. Each entry is the source material for EXPLAINER.md Question 5.

---

## Entry 1 — Phase 3 idempotency layer: `IntegrityError` conflated with `UniqueViolation`

**Date caught:** 2026-04-27 during Phase 3 audit probe.
**File:** `backend/payouts/services.py` (original buggy version preserved at
`notes/q5-ai-audit-original.py`).

### What AI gave me

The `create_payout` flow in `services.py` ran the `IdempotencyKey` `INSERT`
*before* validating that the merchant exists, then caught any `IntegrityError`
to fall through to a "Phase B" cached-response path. Excerpt of the buggy
shape:

```python
try:
    with transaction.atomic():
        idem = IdempotencyKey.objects.create(
            merchant_id=merchant_id,                # ← FK to merchants.id
            key=idempotency_key,
            ...
        )
        try:
            merchant = Merchant.objects.select_for_update().get(id=merchant_id)
        except Merchant.DoesNotExist:
            ...                                     # ← unreachable for missing merchant
except IntegrityError:                              # ← catches FK violation too
    pass

with transaction.atomic():
    idem = IdempotencyKey.objects.select_for_update().get(   # ← raises DoesNotExist
        merchant_id=merchant_id, key=idempotency_key
    )
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

Expected: `404 merchant_not_found` deterministically.
Got: `500 Internal Server Error`, every time. Stack trace ended in
`IdempotencyKey.DoesNotExist`.

### Why it was wrong

The `except IntegrityError` block treated every integrity violation as
"duplicate idempotency key". But `INSERT` on a child table can fail multiple
ways:

| SQLSTATE | Class | Meaning | Was the handler ready for it? |
|---|---|---|---|
| 23505 | `UniqueViolation` | duplicate `(merchant, key)` | yes — intended case |
| 23503 | `ForeignKeyViolation` | `merchant_id` references no merchant | **no** |
| 23502 | `NotNullViolation` | a required column is NULL | no |
| 23514 | `CheckViolation` | a `CHECK` constraint failed | no |

All four raise `django.db.IntegrityError`. So when the merchant didn't exist,
Phase A's `INSERT` failed with `ForeignKeyViolation`, the broad `except`
swallowed it as if it were a duplicate key, and Phase B then tried to read
a row that had never been written. The uncaught `DoesNotExist` became a 500.

There was a second symptom: the `Merchant.DoesNotExist` handler immediately
*after* the `INSERT` was unreachable code in this scenario, because the FK
check fires inside the `INSERT` and short-circuits before the `SELECT FOR
UPDATE` runs.

### How I fixed it

Two changes, both in `payouts/services.py`:

1. **Validate merchant *first*.** Move `Merchant.objects.select_for_update().get(...)`
   to the top of the atomic block. If it raises `Merchant.DoesNotExist`, return
   `(404, merchant_not_found)` immediately — no `IdempotencyKey` row is written
   (we cannot anchor the FK without a merchant). With this change, by the time
   we run `IdempotencyKey.objects.create(...)`, the `merchant_id` is guaranteed
   to point at a real row, so the only `IntegrityError` path left is the
   `(merchant, key)` unique-constraint violation.

2. **Narrow the catch.** Replace `except IntegrityError` with
   `except IntegrityError as exc` followed by an `isinstance(exc.__cause__,
   psycopg.errors.UniqueViolation)` check, plus a constraint-name check
   against `"uniq_merchant_key"`. Anything else propagates.

The dead `Merchant.DoesNotExist` handler was deleted.

A regression test was added at
`payouts/tests/test_idempotency.py::test_nonexistent_merchant_returns_404_not_500`
that fails against the original code and passes against the fix.

### Lesson

`IntegrityError` is the *parent class* of every Postgres integrity violation,
not a synonym for "duplicate key". Treating exception classes as equivalent to
specific error semantics is the canonical AI bug in transactional code. The
defensive pattern in money-moving code is:

- Validate every FK target up front, ideally under a row lock.
- Catch the *narrowest* exception subclass that maps to the case you intended.
- Where possible, also assert on the `constraint_name` exposed by `psycopg`'s
  `diag` so that a future migration adding a new constraint cannot silently
  reroute exceptions.

---

## Entry 2 — Phase 3 model migration: `__str__` referenced a removed column

**Date caught:** 2026-04-27 during Phase 3 audit re-verify.
**Files:** `backend/payouts/models.py` (`IdempotencyKey.__str__`); migration
`backend/payouts/migrations/0002_remove_idempotencykey_status.py`.

### What AI gave me

We agreed to drop the `IdempotencyKey.status` field (the `IN_PROGRESS` /
`COMPLETED` enum was dead state — no other connection ever observed
`IN_PROGRESS` because the work transaction commits atomically). I asked AI
to remove the field. It generated a clean migration:

```python
operations = [
    migrations.RemoveField(
        model_name="idempotencykey",
        name="status",
    ),
]
```

…and updated the `Meta` class. But it left the `__str__` method untouched:

```python
def __str__(self) -> str:
    return f"IdempotencyKey({self.merchant_id}, {self.key}, {self.status})"
                                                          # ↑ still here
```

The migration applied cleanly. The model class still imported. The test suite
still passed because nothing in it called `str(idempotency_key)`.

### How I caught it

A live probe in the Django shell after the migration:

```python
>>> from payouts.models import IdempotencyKey
>>> ik = IdempotencyKey.objects.create(merchant=m, key=uuid.uuid4(), ...)
>>> str(ik)
AttributeError: 'IdempotencyKey' object has no attribute 'status'
```

Any caller would have hit this in production:

- The Django admin list view calls `str(obj)` for every row.
- A `logger.info(f"saved {idem}")` line crashes mid-log, and the resulting
  exception in the logging path can replace the original error in the
  traceback — masking the real failure.
- A traceback formatter rendering this object during another error would
  obscure the real error with this `AttributeError`.

### Why it was wrong

The schema change had a wider blast radius than the migration captured. The
column lived in several places — the migration only handled one of them:

| Callsite | Reads the dropped column? | Caught by my tests? |
|---|---|---|
| `__str__` / `__repr__` | yes | no — no test called `str(obj)` |
| Django admin `list_display`, `search_fields` | possibly | no — admin not under test |
| `logger.info(f"… {obj}")` lines | yes (via `__str__`) | no — log lines do not fail tests |
| External serializers / API responses | yes | only if a test exercises the response |
| Database constraints, indexes, FKs | DB-level | yes — Django emits the migration |

AI correctly handled the database-level removal but didn't perform the
callsite audit: *"what places in Python code still mention `status` on this
model?"* A `grep -rn "self\.status" payouts/` would have found the
`__str__` reference in 1 second.

### How I fixed it

1. Removed `self.status` from the f-string:

   ```python
   def __str__(self) -> str:
       return f"IdempotencyKey(merchant={self.merchant_id}, key={self.key})"
   ```

2. Added a regression test that calls `str()` on a persisted row:

   ```python
   def test_idempotency_key_repr_does_not_reference_dropped_columns():
       ...
       s = str(ik)  # must not raise
       assert "merchant" in s
   ```

3. Greppped the rest of the codebase for `\.status` references on
   `IdempotencyKey` — none remained.

### Lesson

Schema changes need a *callsite* audit, not just a migration. AI is good at
producing migrations (a localised, schema-shaped problem). AI is poor at
finding everywhere that depends on the old schema (a system-wide grep
problem). The defensive pattern when removing a model field:

1. Generate the migration. **Do not apply yet.**
2. `grep -rn "<field>" <app>/` to find every dependent callsite —
   including dunder methods, admin classes, log lines, and serializers.
3. Update or delete each callsite **in the same diff as the migration**.
4. Add at least one regression test that exercises `__str__` / `__repr__` /
   the admin list view, since these surfaces typically have no other test
   coverage.

### Cross-entry meta-lesson

Entry 1 and Entry 2 fail the same way: AI does not reason about the
*system-wide blast radius* of a change.

- **Entry 1: too broad.** AI caught a wide exception class (`IntegrityError`)
  when it meant a narrow one (`UniqueViolation`).
- **Entry 2: too narrow.** AI did the migration but didn't grep for callsites.

The CTO-defensible pattern is: **before applying any schema or transactional
change, ask "what else in the system depends on the shape I'm about to
break?" — and grep for it.**

**Regression-net:** `payouts/tests/test_idempotency.py::test_idempotency_key_repr_does_not_reference_dropped_columns`.
