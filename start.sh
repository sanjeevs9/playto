#!/usr/bin/env bash
#
# Render free-tier startup script — runs all three processes from one container.
#
# Why one container instead of three Render services:
#   Render's free tier covers Web Services (one process bound to $PORT). Workers
#   require a paid Background Worker plan. We sidestep that by running the
#   Celery worker (with embedded beat via -B) as a background subprocess and
#   keeping gunicorn in the foreground so Render sees it as the main process.
#
# Production note (worth flagging in EXPLAINER / interview):
#   This is a deploy-time pragmatic choice, not an application-design choice.
#   The application code (services.create_payout, tasks.process_payout, the
#   beat schedule in settings.py) is the same as what we'd ship to a paid
#   tier with three separate services. Splitting them apart is a Render config
#   change, not a code change.
#

set -euo pipefail

cd backend

# Apply migrations on every boot. Idempotent — Django's migrate is a no-op
# if the schema is already current.
python manage.py migrate --noinput

# Seed merchants + bank accounts + credit history. The seed command uses
# get_or_create internally, so re-running on a populated DB is safe; existing
# rows stay put. See merchants/management/commands/seed.py.
python manage.py seed

# Celery worker with EMBEDDED beat (-B). Three reasons for the embedded form
# on free-tier deploys:
#   1. One process supervises both worker and beat — no second container
#      to coordinate.
#   2. Concurrency=2 fits Render free-tier 512 MB without thrashing.
#   3. Backgrounding (the trailing &) keeps the parent shell free for
#      gunicorn to take over as the foreground process Render watches.
celery -A playto_pay worker -B -l info --concurrency=2 &

# Gunicorn in the foreground. When this exits, the container exits — Render
# uses that signal to know the service died and triggers a restart.
#   --bind 0.0.0.0:$PORT  → bind to whatever port Render assigns.
#   --workers 2 --threads 4 → 8 concurrent request slots; right-sized for
#                              the demo and the 512 MB free-tier RAM.
#   --log-file -          → write access logs to stdout so Render captures them.
exec gunicorn playto_pay.wsgi \
  --bind "0.0.0.0:${PORT:?PORT must be set by Render}" \
  --workers 2 \
  --threads 4 \
  --log-file -
