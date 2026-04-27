# Playto Payout Engine

A merchant payout engine for the Playto Pay take-home challenge — a minimal version of the service that sits between an Indian agency / freelancer's collected USD balance and their INR bank account.

The submission focuses on the four properties the rubric grades: **money integrity**, **concurrency**, **idempotency**, and **state-machine correctness**. The full reasoning behind each architectural choice is in [`EXPLAINER.md`](./EXPLAINER.md). This README is the operator's view: how to run it, where things live, and how to verify it works.

---

## What's in the box

```
.
├── backend/                  Django 5.1 + DRF + Celery 5.4 + psycopg 3
│   ├── playto_pay/           Settings, celery app, root URL conf
│   ├── merchants/            Merchant, BankAccount, LedgerEntry + lock primitive
│   ├── payouts/              Payout + IdempotencyKey + state machine + worker
│   └── requirements.txt
├── frontend/                 Vite + React 19 + TypeScript + Tailwind + TanStack Query
│   └── src/
├── notes/
│   ├── ai-audit-log.md       Source for EXPLAINER Q5 — AI bugs caught and fixed
│   ├── improvements-log.md   Internal review log of every issue caught + lesson
│   └── q5-ai-audit-original.py  The original buggy services.py (Q5 exhibit)
├── .github/workflows/ci.yml  Backend + frontend CI on push/PR
├── docker-compose.yml        Postgres 16 + Redis 7 for local dev
├── EXPLAINER.md              Answers to the 5 take-home questions
└── README.md                 (this file)
```

---

## Quick start

Prerequisites: **Python 3.13.x**, **Node 20+**, **Docker** (for Postgres + Redis). Tested on macOS 15 with Python 3.13.9 and Node 20.19; the Postgres+Redis stack is identical on Linux for CI.

```bash
# 1. Bring up Postgres + Redis on non-default ports (5433 / 6380)
#    so the dev stack does not clash with anything you already run locally.
docker compose up -d

# 2. Backend
cd backend
python3.13 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env           # already points at the docker-compose ports
python manage.py migrate
python manage.py seed          # populates 3 realistic merchants

python manage.py runserver     # http://127.0.0.1:8000

# 3. In a second terminal — the worker:
cd backend && source venv/bin/activate
celery -A playto_pay worker -l info

# 4. In a third terminal — the beat scheduler (drives retry sweep + idempotency cleanup):
cd backend && source venv/bin/activate
celery -A playto_pay beat -l info

# 5. Frontend
cd frontend
cp .env.example .env
npm install
npm run dev                    # http://127.0.0.1:5173
```

Open `http://127.0.0.1:5173`, pick a seeded merchant, and submit a payout. The dashboard polls every 3 seconds; you'll see the payout move through `PENDING → PROCESSING → COMPLETED` (70%), `→ FAILED + REFUND` (20%), or hang in `PROCESSING` until the retry sweeper resolves it (10%).

### Resetting the local DB

```bash
python manage.py seed --reset  # nukes all merchant/payout/ledger data and reseeds
```

---

## Architecture in one paragraph

Every balance read is a Postgres `SUM(LedgerEntry.amount_paise)` aggregation — the ledger is the source of truth, no denormalised balance column. Every balance mutation runs inside `transaction.atomic()` with `SELECT … FOR UPDATE` on the merchant row, which serialises payout creation per merchant and prevents over-draft. Idempotency is a `(merchant, key)` unique-constraint INSERT inside the work transaction; a duplicate raises `psycopg.errors.UniqueViolation` (narrowly caught, with the constraint name asserted) and falls through to a Phase B fallback that re-reads the cached response under `select_for_update`. Payout state changes go through `Payout.transition_to()` which consults a `LEGAL_TRANSITIONS` map at `payouts/state_machine.py`. The Celery worker simulates the bank outside the row lock, then re-acquires the lock to apply the outcome — refund + state transition land in the same `atomic` block, satisfying the rubric's atomicity requirement.

```
client ──POST /api/v1/payouts──▶ create_payout ─┐
  + Idempotency-Key                             │  atomic:
                                                ├─ select_for_update Merchant
                                                ├─ INSERT IdempotencyKey (uniq)
                                                ├─ INSERT Payout (PENDING)
                                                ├─ INSERT LedgerEntry (DEBIT)
                                                └─ on_commit ─▶ Celery process_payout
                                                                    │
                                                                    ▼
                                                          _simulate_bank()
                                                            70% success → COMPLETED
                                                            20% failure → FAILED + REFUND  (atomic)
                                                            10% hang    → stay PROCESSING
                                                                    │
                                       beat every 10s ──▶ retry_stuck_payouts
                                                            select_for_update SKIP LOCKED
                                                            bump started_at, queue retry_payout
                                                            (max 3 attempts → FAILED + REFUND)
```

---

## API reference

All requests below are unauthenticated for the take-home; production would scope each call to an authenticated merchant. Until then we use `X-Merchant-Id` as a bearer-style header and require `Idempotency-Key` (UUID v4) on the only mutating endpoint.

| Method | Path | Purpose |
|---|---|---|
| GET | `/healthz` | Liveness probe |
| GET | `/api/v1/merchants` | List merchants (selector dropdown) |
| GET | `/api/v1/merchants/<id>/balance` | `{ available_paise, held_paise }` |
| GET | `/api/v1/merchants/<id>/ledger` | Recent ledger entries (newest first) |
| GET | `/api/v1/merchants/<id>/bank-accounts` | Bank accounts on file |
| POST | `/api/v1/payouts` | **Idempotent** payout creation. Headers required: `X-Merchant-Id`, `Idempotency-Key` |
| GET | `/api/v1/payouts` | List this merchant's payouts |
| GET | `/api/v1/payouts/<uuid>` | Single payout detail (404 across merchant boundary) |

`POST /api/v1/payouts` body: `{ "amount_paise": <int>, "bank_account_id": <int> }`. Returns the created `Payout` with `status: "PENDING"` (201). Errors: `404 merchant_not_found`, `404 bank_account_not_found`, `422 insufficient_funds`, `422 idempotency_conflict` (same key, different body), `400` for missing headers / shape errors.

---

## Stack

- **Backend**: Django 5.1, DRF 3.15, Celery 5.4, psycopg 3.2, PostgreSQL 14+. Settings are env-driven; `dj-database-url` parses `DATABASE_URL` for Railway/Render deploys.
- **Frontend**: Vite + React 19 + TypeScript, Tailwind 3, TanStack Query 5 with 3-second polling, axios for HTTP, `crypto.randomUUID()` for client-side Idempotency-Key generation.
- **Infra (target)**: Railway. Single project with web (gunicorn) + worker + beat + Postgres + Redis services. Deploy artefacts (`Procfile`, `runtime.txt`, `nixpacks.toml`) at repo root. Frontend ships as a same-origin SPA — Vite builds with `base: "/static/"`, Django's catch-all serves `index.html`, WhiteNoise serves the hashed assets. See **Deploy** below.

---

## Testing

```bash
cd backend && source venv/bin/activate
pytest                          # full suite — currently 60 tests
pytest -k concurrency           # just the concurrency tests
pytest payouts/tests/test_state_machine.py -v
```

Suite breakdown:

| File | Count | Covers |
|---|---|---|
| `merchants/tests/test_concurrency.py` | 1 | Threaded SELECT FOR UPDATE on ledger debit (rubric concurrency test) |
| `merchants/tests/test_views.py` | 7 | Dashboard endpoints (balance, ledger, bank accounts, 404s) |
| `payouts/tests/test_idempotency.py` | 7 | Same-body, different-body, in-flight race, 404-replay (BUG-1 regression), repr-after-migration regression, error-response replay, cross-merchant scoping |
| `payouts/tests/test_state_machine.py` | 17 | Legal + illegal transitions parametric, plus dedicated FAILED→COMPLETED guard test |
| `payouts/tests/test_views.py` | 4 | Cross-tenant scoping, missing-header rejection |
| `payouts/tests/test_worker.py` | 10 | Success/failure/hang outcomes, retry sweep filter, max-retries refund, atomicity proof, concurrent `_apply_outcome` race, balance invariant |
| `payouts/tests/test_constraints.py` | 8 | DB-level CHECK + UNIQUE constraint enforcement |
| `payouts/tests/test_edge_cases.py` | 6 | Boundary cases (exact balance, off-by-one, zero/negative amounts, cross-merchant bank account, success-cycle balance invariant) |
| **total** | **60** | |

Concurrency tests use `pytest.mark.django_db(transaction=True)` so threads can see each other's committed writes. They are stress-tested at 15 consecutive runs for stability.

CI runs the full suite plus a frontend type-check + production build on every push and pull request — see `.github/workflows/ci.yml`.

---

## How review passes shaped the code

This repo went through several review passes during development. Two notes files capture what was caught and how it was fixed:

- [`notes/ai-audit-log.md`](./notes/ai-audit-log.md) — deep narrative on AI-introduced bugs that were caught and fixed. Two entries: the `IntegrityError` vs `UniqueViolation` conflation (Phase 3 idempotency layer) and the `__str__` regression after a column drop (Phase 3 model migration). Source material for EXPLAINER Q5.
- [`notes/improvements-log.md`](./notes/improvements-log.md) — running internal log of every issue caught during reviews, with **Symptom → How caught → Fix → Lesson → Regression-net** for each. Includes config drift, stale dependencies, and dead state in addition to the AI-specific bugs in the audit log.
- [`notes/q5-ai-audit-original.py`](./notes/q5-ai-audit-original.py) — verbatim preserved buggy `services.py` from Phase 3, kept as the EXPLAINER Q5 exhibit so the original code is recoverable without depending on git archaeology.

A CTO grepping the repo for evidence of review discipline should start here.

---

## Deploy (Railway)

Live demo: **(set after deploy)**

The repo is set up for a single Railway project with three services — `web`, `worker`, `beat` — pulling from the same GitHub repo and sharing a Postgres + Redis pair. All three use the same Nixpacks build (which compiles the React SPA *and* the Python backend into one image); they differ only in start command.

### One-time setup

1. **Push to GitHub** if you haven't already.
2. **Create a new Railway project** and point it at the repo.
3. Add the **Postgres** and **Redis** plugins. Railway auto-injects `DATABASE_URL` and `REDIS_URL` into every service in the project.
4. **Create three services**, each pointing at the same repo:
   - `web` — start command (default from Procfile): `cd backend && gunicorn playto_pay.wsgi --bind 0.0.0.0:$PORT --workers 2 --threads 4 --log-file -`
   - `worker` — override start command: `cd backend && celery -A playto_pay worker -l info --concurrency=2`
   - `beat` — override start command: `cd backend && celery -A playto_pay beat -l info`
5. **Set environment variables** on each service (Postgres + Redis URLs are auto-set by Railway; the rest must be set manually):

   | Variable | `web` | `worker` | `beat` |
   |---|---|---|---|
   | `DJANGO_SECRET_KEY` | random ≥50 chars | same | same |
   | `DJANGO_DEBUG` | `false` | `false` | `false` |
   | `DJANGO_ALLOWED_HOSTS` | your `*.up.railway.app` host | same | same |
   | `DJANGO_CSRF_TRUSTED_ORIGINS` | `https://your-app.up.railway.app` | (not used) | (not used) |
   | `CELERY_BROKER_URL` | `${{REDIS_URL}}/0` | same | same |
   | `CELERY_RESULT_BACKEND` | `${{REDIS_URL}}/1` | same | same |
   | `DATABASE_URL` | auto | auto | auto |
6. **Deploy**. The first deploy will run `release: cd backend && python manage.py migrate --noinput` from the Procfile before any service comes up.
7. **Seed the production DB** by running `python manage.py seed` against it (Railway's "Run Command" feature, or `railway run python manage.py seed` locally pointing at the prod env).
8. **Verify the live URL**:
   - `GET /healthz` → `{"status": "ok"}`
   - `GET /api/v1/merchants` → 3 seeded merchants
   - Open the root URL in a browser → dashboard loads, picking a merchant shows balance / ledger / payout history, submitting a payout runs through the worker.

### Production hardening that already lives in the code

- **HTTPS-only.** When `DJANGO_DEBUG=false`, `settings.py` sets `SECURE_SSL_REDIRECT`, 1-year HSTS with subdomain inclusion + preload eligibility, secure session + CSRF cookies, `X_FRAME_OPTIONS=DENY`, `Content-Security-Policy`-adjacent referrer policy, and `nosniff`. All gated on `not DEBUG` so dev runs unaffected.
- **TLS termination behind the proxy.** `SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")` makes Django trust Railway's edge-terminated TLS so `SECURE_SSL_REDIRECT` doesn't loop.
- **Single-origin SPA + API.** No CORS surface in production — the dashboard and the API share an origin. CORS_ALLOW_ALL_ORIGINS is gated on `DEBUG=true`, so it's *off* in production.
- **WhiteNoise + manifest static files** with content hashing already configured (`STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"`). Long-cache the JS/CSS, no-cache the SPA shell.
- **Ephemeral beat schedule** is fine: `CELERY_BEAT_SCHEDULE` is a Python dict in code, not a state file. The persistent-scheduler file at `celerybeat-schedule` rebuilds itself on each restart.

### What still needs your hand

- Connecting the GitHub repo to Railway (account-bound).
- Setting the env vars listed above in the Railway UI.
- Picking a Railway hostname and pasting it back into `DJANGO_ALLOWED_HOSTS` and `DJANGO_CSRF_TRUSTED_ORIGINS`.
- Smoke-testing the live URL with the verification checklist in step 8.
- Updating "Live demo: …" at the top of this section with the URL.

---

## EXPLAINER

Answers to the five take-home questions live in [`EXPLAINER.md`](./EXPLAINER.md). Each answer cites file:line so a reviewer can navigate directly to the code under discussion.
