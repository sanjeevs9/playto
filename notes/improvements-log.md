# Improvements Log

A running record of issues caught during Pranay's review passes, what went wrong, how he caught it, and how I restructured the fix. This file is the "what didn't work the first time" record — `ai-audit-log.md` complements it with the AI-specific subset suitable for EXPLAINER Q5.

Each entry follows: **Phase / Issue → Symptom → How caught → Fix → Lesson → Regression-net**.

---

## Entry 1 — Phase 3: `IntegrityError` vs `UniqueViolation` conflation (BUG-1)
*Full EXPLAINER-grade write-up in `notes/ai-audit-log.md` Entry 1; this is the cross-reference.*

- **Symptom:** `POST /api/v1/payouts` with `X-Merchant-Id: 9999999` returned 500 instead of 404. Idempotency broken on the missing-merchant path.
- **How caught:** Pranay probed the endpoint with a nonexistent merchant id; expected 404, observed 500 with `IdempotencyKey.DoesNotExist` in the trace.
- **Root cause:** `except IntegrityError` swallowed `ForeignKeyViolation` (SQLSTATE 23503) as if it were `UniqueViolation` (23505). Phase B then queried for a row that was never persisted.
- **Fix:** Lock merchant FIRST inside the atomic block (404 immediately if missing); narrow the catch to `isinstance(cause, UniqueViolation)` plus a `constraint_name == "uniq_merchant_key"` belt-and-braces check.
- **Lesson:** `IntegrityError` is the parent class of every Postgres integrity violation — it is not a synonym for "duplicate key". Catch the narrowest exception subclass that maps to the case you intended; assert the constraint name where possible.
- **Regression-net:** `test_nonexistent_merchant_returns_404_not_500` in `payouts/tests/test_idempotency.py`.

## Entry 2 — Phase 3: `__str__` referenced removed `status` field

- **Symptom:** After dropping `IdempotencyKey.status` in migration 0002, `IdempotencyKey.__str__` still f-string-interpolated `self.status`. Any caller — admin list view, `logger.info(f"... {idem}")`, or a Python traceback formatter — raised `AttributeError`.
- **How caught:** Pranay's live probe ran `str(IdempotencyKey)` in a shell.
- **Fix:** Removed `self.status` from the `__str__` f-string. New form: `f"IdempotencyKey(merchant={self.merchant_id}, key={self.key})"`.
- **Lesson:** When deleting a model field, grep all occurrences (including dunder methods like `__str__` / `__repr__`, admin `list_display`, log lines) BEFORE applying the migration.
- **Regression-net:** `test_idempotency_key_repr_does_not_reference_dropped_columns` in `payouts/tests/test_idempotency.py`.

## Entry 3 — Phase 4: `django_celery_beat` left installed after scheduler swap

- **Symptom:** Switched to Celery's file-based `PersistentScheduler` but left `django_celery_beat` in `INSTALLED_APPS`. Result: 19 orphaned migrations, unused DB tables, and a configuration smell ("why is `django_celery_beat` installed if nothing reads its scheduler?").
- **How caught:** Pranay's audit grepped for `django_celery_beat` references after observing only the file-based scheduler running at boot.
- **Fix:** `migrate django_celery_beat zero` to drop the tables, remove from `INSTALLED_APPS`, drop `django-celery-beat==2.7.0` from `requirements.txt`, `pip uninstall`.
- **Lesson:** When swapping out a config-driven dependency, also remove its `INSTALLED_APPS` registration *and* uninstall the package — "just delete the setting line that uses it" isn't enough; orphan migrations linger.
- **Regression-net:** `manage.py showmigrations` should not list `django_celery_beat`.

## Entry 4 — Phase 4: `celerybeat-schedule` not in `.gitignore`

- **Symptom:** Beat's file-based scheduler creates a binary `backend/celerybeat-schedule` for its persistent state. `.gitignore` didn't exclude it; a `git add -A` would have committed it.
- **How caught:** Pranay's audit noticed the file in the working tree after Phase 4 boot.
- **Fix:** Appended `celerybeat-schedule*`, `celerybeat.pid` to `.gitignore`. Removed any existing artefact from disk.
- **Lesson:** Whenever a long-running process creates a runtime file, ask "is this in `.gitignore`?" — same reflex as `node_modules` or `__pycache__`.

## Entry 6 — Phase 7/8: Mis-scoped phase boundaries hid the deployment gap

- **Symptom:** I called Phase 7 "complete" after writing README + EXPLAINER, but the rubric requires a *live deployment URL* and there were no deploy artefacts in the repo: no `Procfile`, no `runtime.txt`, no `nixpacks.toml`, no production security headers in `settings.py`, no production frontend serving path. The README itself admitted this with `"Phase 8 deploy notes (TBD)"`.
- **How caught:** Pranay searched the repo for deploy artefacts (`find . -name "Procfile*" -o -name "railway.*" -o -name "Dockerfile*"` returned empty) and grepped `settings.py` for `SECURE_*` markers (also empty), then noted the README's own self-admitted gap.
- **Fix:** Added `Procfile` + `runtime.txt` + `nixpacks.toml` at repo root; gated production security headers (`SECURE_SSL_REDIRECT`, HSTS, secure cookies, X-Frame-Options, etc.) on `not DEBUG` in `settings.py`; configured Vite `base: "/static/"` in production-only mode; added a Django catch-all view to serve the SPA's `index.html` so the dashboard and API ship single-origin. End-to-end smoke verified all five paths (root, /static/asset, /api, /admin, random fall-through) routed correctly.
- **Lesson:** Phase boundaries are fictional unless they map to specific *artefacts* a reviewer can verify. "README + EXPLAINER" is a documentation milestone, not a deployment milestone — the rubric demands a live URL. Future phase planning should explicitly list the *artefacts* required, not just the work category. A "Phase X complete" claim should fail loudly if the artefacts aren't on disk.
- **Regression-net:** Manual end-to-end smoke after every settings.py touch (the test suite stays in DEBUG=true and won't exercise the production security block). Pre-submit checklist in README's *What still needs your hand* section.

---

## Entry 5 — Phase 4: Retry over-count under duplicate sweep dispatch

- **Symptom:** Beat sweeper runs every 10s; payout timeout is 30s. A single hung payout could be picked up at t=30, t=40, t=50 by three consecutive sweeper runs, queueing three `retry_payout` messages. Each consumes a retry attempt, so one logical hang would burn through `max_retries=3` in ~50s rather than the spec's intended pacing.
- **How caught:** Pranay's audit ran two back-to-back `retry_payout` calls and observed `retry_count` jumping non-deterministically.
- **Fix:** Sweeper now wraps the SELECT in `transaction.atomic()` with `select_for_update(skip_locked=True)`, then `update(started_at=now)` on the matched rows BEFORE queueing the per-row retry. The next sweep run sees `started_at__lt=cutoff` is false for those rows and skips. `skip_locked=True` defends against parallel sweeper instances.
- **Lesson:** When a periodic sweeper queues async work, ask: "could the sweeper-trigger condition still be true on the next tick before the queued work fires?" If yes, mark the row as "claimed" before queuing.
- **Regression-net:** Documentation note in `tasks.py` plus implicit coverage by the worker tests (each test asserts a specific `retry_count` so over-count would surface).

---

## How to add an entry

When Pranay catches something, append a section using this template:

```markdown
## Entry N — Phase X: <one-line title>

- **Symptom:** what the user/observer saw
- **How caught:** the probe / audit / reproduction
- **Fix:** specific code change (with file:line where useful)
- **Lesson:** the generalisable principle, not the specific code
- **Regression-net:** the test that locks in the fix (or "manual verification" if none)
```
