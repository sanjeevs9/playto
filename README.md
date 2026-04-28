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
- **Infra (live)**: **Render free-tier** single web service running gunicorn + Celery worker (with embedded beat via `-B`) as one container, driven by `start.sh` at repo root. **Neon** (Postgres 17, Singapore region) and **Upstash** (Redis 7 with TLS) are the managed data plane. Deploy artefacts (`Procfile`, `runtime.txt`, `nixpacks.toml`, `start.sh`) at repo root — the `Procfile` also defines the 3-service split for Railway / Render-paid deploys, so the same repo deploys both ways. Frontend ships as a same-origin SPA — Vite builds with `base: "/static/"`, Django's catch-all serves `index.html`, WhiteNoise serves the hashed assets. See **Deploy** below.

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

## Deploy

Live demo: **<https://playto-rvfx.onrender.com>**

This repo deploys two ways from the same artefacts. The actual production deploy uses the Render free tier (single-service monolith); the same `Procfile` also describes a 3-service split for Railway / Render-paid / Fly. Both paths are documented below.

### How the live demo is shaped

Render's free Web Service tier supports exactly one foreground process bound to `$PORT`. We sidestep that by running the Celery worker (with embedded beat via `-B`) as a *background subprocess* of the same container, while gunicorn stays in the foreground:

```
                   ┌──────────────────────────────────────────────┐
                   │  ONE Render Web Service (free, 512 MB)       │
                   │                                              │
                   │  start.sh:                                   │
                   │    1. python manage.py migrate --noinput     │
                   │    2. python manage.py seed                  │
                   │    3. celery -A playto_pay worker -B &       │  ← background
                   │    4. exec gunicorn ... --bind 0.0.0.0:$PORT │  ← foreground
                   └──────────────────────────────────────────────┘
                              │                       │
                  ┌───────────┴────────┐  ┌──────────┴───────────┐
                  ▼                    ▼  ▼                      │
           Neon Postgres 17       Upstash Redis 7 (rediss:// TLS)
        (Singapore, 3 GB free)   (10 K cmds/day free)
```

The `&`-then-`exec` shell idiom is the trick: Render only watches gunicorn (the foreground process); the worker runs alongside, sharing env vars and lifecycle. Beat is embedded inside the worker (`-B` flag) so the retry sweeper + idempotency-key cleanup still fire on schedule.

This is the **deploy-time pragmatic choice the EXPLAINER calls out as a tradeoff**: a paid tier (or Railway / Fly with a worker plan) would split into separate services for independent scaling. Application code is identical either way — splitting is a config change, not a code change.

### Render deploy — recreate the live demo

1. **Push to GitHub** so Render can auto-build on push.
2. **Provision managed Postgres** at <https://neon.tech> (free, no card). Create a project in your closest region, copy the connection string. Format:
   ```
   postgresql://user:pass@host.neon.tech/db?sslmode=require&channel_binding=require
   ```
3. **Provision managed Redis** at <https://upstash.com> (free, no card). Create a regional Redis DB, copy the `rediss://...` connection string (TLS-enabled).
4. **Create a Render Web Service** pointing at the GitHub repo. Free tier is fine.
   - **Root Directory:** *(empty — repo root)*
   - **Build Command:**
     ```
     cd frontend && npm ci && npm run build && cd ../backend && pip install -r requirements.txt && python manage.py collectstatic --noinput
     ```
   - **Start Command:** `./start.sh`
5. **Set environment variables** in Render's Environment tab. **`?ssl_cert_reqs=CERT_REQUIRED` on the Upstash URLs is mandatory** — Celery 5.4 refuses to initialize a TLS Redis connection without an explicit cert-requirements policy:

   | Variable | Value |
   |---|---|
   | `DJANGO_SECRET_KEY` | random ≥50 chars (`python -c "import secrets; print(secrets.token_urlsafe(50))"`) |
   | `DJANGO_DEBUG` | `false` |
   | `DJANGO_ALLOWED_HOSTS` | `<your-service>.onrender.com` (no scheme) |
   | `DJANGO_CSRF_TRUSTED_ORIGINS` | `https://<your-service>.onrender.com` (with scheme) |
   | `DATABASE_URL` | the Neon connection string (`postgresql://...?sslmode=require&channel_binding=require`) |
   | `CELERY_BROKER_URL` | `<Upstash rediss URL>/0?ssl_cert_reqs=CERT_REQUIRED` |
   | `CELERY_RESULT_BACKEND` | `<Upstash rediss URL>/0?ssl_cert_reqs=CERT_REQUIRED` |
6. **Deploy.** The first build runs `npm ci` + `npm run build` + `pip install` + `collectstatic`. Then `./start.sh` migrates, seeds, and boots the worker + gunicorn. ~5–8 min total.
7. **Verify the live URL:**
   - `GET /healthz` → `{"status": "ok"}`
   - `GET /api/v1/merchants` → 3 seeded merchants
   - Open the root URL in a browser → dashboard loads, picking a merchant shows balance + ledger + payout history. Submitting a payout runs through the worker; the row transitions PENDING → PROCESSING → COMPLETED (or FAILED + REFUND) within ~3 seconds.

### Avoiding the cold-start

Render's free tier sleeps after 15 minutes of inactivity. First request after idle is ~30s. Set up a free <https://uptimerobot.com> monitor pinging `/healthz` every 5 minutes — keeps the service warm so the CTO's first click doesn't hit a cold container.

### Railway alternative (3-service split, not used live)

The same `Procfile` defines `release` / `web` / `worker` / `beat` for a 3-service deploy on Railway (paid Hobby plan, $5/mo). The architecture is more "production-correct" — independent scaling per process, no cohabiting subprocesses. Steps:

1. Railway → New Project → Deploy from GitHub.
2. Add Railway Postgres + Redis plugins (`DATABASE_URL`, `REDIS_URL` auto-injected).
3. Three services from the same repo:
   - `web` — uses Procfile's `web:` line (default).
   - `worker` — override start: `cd backend && celery -A playto_pay worker -l info --concurrency=2`.
   - `beat` — override start: `cd backend && celery -A playto_pay beat -l info`.
4. Same env vars as the Render path, except `CELERY_BROKER_URL=${{REDIS_URL}}/0` and `CELERY_RESULT_BACKEND=${{REDIS_URL}}/1` (Railway's Redis isn't TLS-only, so the `?ssl_cert_reqs=...` query param isn't required).
5. Generate a public domain on the `web` service, paste it into `DJANGO_ALLOWED_HOSTS` / `DJANGO_CSRF_TRUSTED_ORIGINS`.

### Production hardening that already lives in the code

- **HTTPS-only.** When `DJANGO_DEBUG=false`, `settings.py` sets `SECURE_SSL_REDIRECT`, 1-year HSTS with subdomain inclusion + preload eligibility, secure session + CSRF cookies, `X_FRAME_OPTIONS=DENY`, `Content-Security-Policy`-adjacent referrer policy, and `nosniff`. All gated on `not DEBUG` so dev runs unaffected.
- **TLS termination behind the proxy.** `SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")` makes Django trust the platform's edge-terminated TLS (Render and Railway both forward this header) so `SECURE_SSL_REDIRECT` doesn't loop.
- **Single-origin SPA + API.** No CORS surface in production — the dashboard and the API share an origin. `CORS_ALLOW_ALL_ORIGINS` is gated on `DEBUG=true`, so it's *off* in production.
- **WhiteNoise + manifest static files** with content hashing already configured (`STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"`). Long-cache the JS/CSS, no-cache the SPA shell.
- **Ephemeral beat schedule.** `CELERY_BEAT_SCHEDULE` is a Python dict in code, not a state file. The persistent-scheduler file at `celerybeat-schedule` rebuilds itself on each restart, which is fine for our task list (retry sweeper + idempotency cleanup, both idempotent on re-fire).
- **Neon PgBouncer compatibility.** We do *not* set the libpq `options` startup parameter (e.g. for pinning isolation level) because Neon's transaction-pooled connections reject it. We rely on Postgres's READ COMMITTED default — see EXPLAINER Q2 for the full reasoning + tradeoff.

---

## EXPLAINER

Answers to the five take-home questions live in [`EXPLAINER.md`](./EXPLAINER.md). Each answer cites file:line so a reviewer can navigate directly to the code under discussion.
