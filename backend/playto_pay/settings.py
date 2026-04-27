"""
Django settings for the Playto payout engine.

Configuration is environment-driven so the same code runs locally, in CI,
and on Railway.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")


def env(name: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and value in (None, ""):
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value  # type: ignore[return-value]


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-only-secret-do-not-use-in-prod")
DEBUG = env_bool("DJANGO_DEBUG", default=True)

ALLOWED_HOSTS = [h.strip() for h in env("DJANGO_ALLOWED_HOSTS", "*").split(",") if h.strip()]
CSRF_TRUSTED_ORIGINS = [
    o.strip() for o in env("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "corsheaders",
    "merchants",
    "payouts",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "playto_pay.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "playto_pay.wsgi.application"

# --- Database --------------------------------------------------------------
#
# Two paths in priority order:
#   1. DATABASE_URL (Railway, Render, Fly all set this) -> dj_database_url.
#      The library handles URL-encoded passwords (we have hit `@`, `:`, `%`
#      in real Railway-rotated passwords), preserves ``sslmode=require`` query
#      params Railway appends, and accepts both ``postgres://`` and
#      ``postgresql://`` schemes.
#   2. Otherwise, individual POSTGRES_* env vars (the local-dev path used by
#      docker-compose).
#
# Isolation level: we rely on Postgres's default of READ COMMITTED, which is
# what our locking story (``SELECT ... FOR UPDATE`` on the merchant row) is
# designed for.
#
# In an earlier revision we explicitly pinned this via libpq's ``options``
# startup parameter ("-c default_transaction_isolation=read committed") so
# the contract did not depend on a server-side default. We had to remove
# that: Neon's PgBouncer pooler (the pooled connection mode) rejects the
# ``options`` startup parameter — see
# https://neon.tech/docs/connect/connection-errors#unsupported-startup-parameter.
# The two ways out were (a) switch to the unpooled Neon URL (would exhaust
# Postgres connections under our worker fan-out), or (b) drop the pin and
# document the dependency. We chose (b).
#
# A non-pooled deploy (RDS, self-hosted) could re-add the pin via
# ``OPTIONS["options"]``; the lock semantics are unchanged either way.
import dj_database_url

_PG_OPTIONS: dict = {}

# CONN_MAX_AGE default is 0 — close the DB connection after each request.
# Rationale: in this stack we run gunicorn (web) + Celery worker + Celery beat,
# all without a connection pooler. Each process holding persistent connections
# would compound (~13 idle floor for 2 web + 4 worker + 1 beat) against
# Postgres's max_connections=100 default and exhaust the pool under burst load.
# Per-request connections are slightly slower per-request but eliminate the
# pool-exhaustion failure mode. With PgBouncer in front we'd raise this.
_PG_CONN_MAX_AGE = int(env("POSTGRES_CONN_MAX_AGE", "0"))

if env("DATABASE_URL", ""):
    DATABASES = {
        "default": dj_database_url.config(
            conn_max_age=_PG_CONN_MAX_AGE,
            conn_health_checks=True,
        ),
    }
    DATABASES["default"].setdefault("OPTIONS", {}).update(_PG_OPTIONS)
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": env("POSTGRES_DB", "playto_pay"),
            "USER": env("POSTGRES_USER", os.environ.get("USER", "postgres")),
            "PASSWORD": env("POSTGRES_PASSWORD", ""),
            "HOST": env("POSTGRES_HOST", "127.0.0.1"),
            "PORT": env("POSTGRES_PORT", "5432"),
            "CONN_MAX_AGE": _PG_CONN_MAX_AGE,
            "OPTIONS": _PG_OPTIONS,
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

# Single-origin SPA deploy: include the frontend's Vite build output in
# STATICFILES_DIRS so ``collectstatic`` copies it into ``staticfiles/`` and
# WhiteNoise serves it at ``/static/``. The Vite ``base`` config is
# ``"/static/"`` in production builds (see ``frontend/vite.config.ts``), so
# the asset URLs in ``index.html`` line up with what WhiteNoise serves.
# In dev the directory may not exist; the conditional avoids a noisy warning.
_FRONTEND_DIST = BASE_DIR.parent / "frontend" / "dist"
STATICFILES_DIRS = [_FRONTEND_DIST] if _FRONTEND_DIST.exists() else []

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.LimitOffsetPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_THROTTLE_CLASSES": [],
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
}

CORS_ALLOW_ALL_ORIGINS = env_bool("CORS_ALLOW_ALL_ORIGINS", default=DEBUG)
CORS_ALLOWED_ORIGINS = [
    o.strip() for o in env("CORS_ALLOWED_ORIGINS", "").split(",") if o.strip()
]
CORS_ALLOW_HEADERS = [
    "accept",
    "accept-encoding",
    "authorization",
    "content-type",
    "dnt",
    "origin",
    "user-agent",
    "x-csrftoken",
    "x-requested-with",
    "idempotency-key",
    "x-merchant-id",
]

# --- Production security headers (gated on !DEBUG) ----------------------
# Enabled when DJANGO_DEBUG is false. Dev-time runs unaffected. Each setting
# is the standard Django/OWASP-recommended value for a money-moving service
# behind an HTTPS-terminating proxy (Railway / Render / Fly all do TLS
# termination at the edge).
if not DEBUG:
    # Trust the proxy's X-Forwarded-Proto so SSL_REDIRECT works from behind
    # a TLS-terminating load balancer (Railway sets this header for us).
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = True
    # 1-year HSTS with subdomain inclusion + preload eligibility.
    SECURE_HSTS_SECONDS = 31_536_000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
    X_FRAME_OPTIONS = "DENY"
    SECURE_CONTENT_TYPE_NOSNIFF = True

CELERY_BROKER_URL = env("CELERY_BROKER_URL", "redis://127.0.0.1:6379/0")
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", "redis://127.0.0.1:6379/1")
CELERY_TASK_TRACK_STARTED = True
# acks_late + reject_on_worker_lost: if a worker dies mid-task, the broker
# re-delivers the message instead of acking it as done. Combined with
# prefetch=1 this means we lose at most one in-flight task per worker death,
# rather than the prefetch buffer.
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TIMEZONE = "UTC"
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
# Hard + soft time limits prevent a hung bank-API call (in production: a real
# external HTTP request) from holding a worker forever. The soft limit raises
# SoftTimeLimitExceeded which our task can clean up; the hard limit kills the
# worker process if it ignores the soft signal.
CELERY_TASK_TIME_LIMIT = int(env("CELERY_TASK_TIME_LIMIT", "60"))
CELERY_TASK_SOFT_TIME_LIMIT = int(env("CELERY_TASK_SOFT_TIME_LIMIT", "50"))

CELERY_BEAT_SCHEDULE = {
    # Sweep PROCESSING payouts older than the timeout and dispatch retries.
    # Schedule runs every 10 seconds; the cutoff inside the task is
    # PAYOUT_PROCESSING_TIMEOUT_SECONDS (default 30s).
    "retry-stuck-payouts": {
        "task": "payouts.retry_stuck_payouts",
        "schedule": 10.0,
    },
    # Hourly cleanup of expired idempotency keys. The TTL itself
    # (IDEMPOTENCY_KEY_TTL_HOURS, default 24h) is recorded on each row at
    # write time; this task only deletes rows whose expires_at has passed.
    "cleanup-expired-idempotency-keys": {
        "task": "payouts.cleanup_idempotency_keys",
        "schedule": 3600.0,
    },
}

# Domain knobs — kept here so they can be tuned without touching code paths.
PAYOUT_PROCESSING_TIMEOUT_SECONDS = int(env("PAYOUT_PROCESSING_TIMEOUT_SECONDS", "30"))
PAYOUT_MAX_RETRIES = int(env("PAYOUT_MAX_RETRIES", "3"))
IDEMPOTENCY_KEY_TTL_HOURS = int(env("IDEMPOTENCY_KEY_TTL_HOURS", "24"))
BANK_SIMULATION_SUCCESS = float(env("BANK_SIMULATION_SUCCESS", "0.70"))
BANK_SIMULATION_FAILURE = float(env("BANK_SIMULATION_FAILURE", "0.20"))
# 0.10 hang implied by remainder. (success + failure + hang == 1.0)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "format": "[{asctime}] {levelname} {name}: {message}",
            "style": "{",
        }
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "default",
        }
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "playto": {"handlers": ["console"], "level": "DEBUG", "propagate": False},
        "celery": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}
