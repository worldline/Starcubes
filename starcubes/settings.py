"""Minimal Django settings for the starcubes project (small first pass).

Reads `STARCUBES_POSTGRES_DSN` from the environment — the same DSN used by the
ETL core, scripts, and `preview_server.py` — so a single env var configures
both Django and the framework-agnostic core. `STATICFILES_DIRS` points at `viz/static/`, where the Dockerfile's
jsbuilder stage drops the vendored `@hgruber/timeseries.js` bundle under
`viz/static/timeseries/timeseries.js`. Django's collectstatic picks it up
and serves it as `/static/timeseries/timeseries.js`.

Django tables (auth_user, django_session, etc.) land in starcubes_meta instead
of public via search_path. The custom database backend
(``starcubes.db_backends.DatabaseWrapper``) ensures the schema exists on first
connection. starcubes_meta application tables are managed via SQLAlchemy + raw
SQL (``migrations/starcubes_meta.sql``), not Django ORM models.
"""
from __future__ import annotations

import os
from pathlib import Path
import logging
from urllib.parse import unquote, urlparse


class UrlDecodingFormatter(logging.Formatter):
    """Apply urllib.parse.unquote to the formatted message."""

    def format(self, record: logging.LogRecord) -> str:
        return unquote(super().format(record))

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY    = os.environ.get("STARCUBES_SECRET_KEY", "dev-key-change-me")
DEBUG         = os.environ.get("STARCUBES_DEBUG", "1") == "1"
ALLOWED_HOSTS = os.environ.get("STARCUBES_ALLOWED_HOSTS", "*").split(",")

# Django's CSRF Origin check (since 4.0) compares the browser's Origin header
# against a value derived from request.get_host() — with ALLOWED_HOSTS="*"
# and nginx sitting in front of uwsgi, that derivation is fragile (nginx's
# catch-all `server_name _;` can end up feeding the wrong host through).
# CSRF_TRUSTED_ORIGINS is the documented, deployment-agnostic fix: an
# explicit allowlist of scheme://host[:port] origins, independent of how the
# request reaches Django. Must include the scheme (http:// or https://).
CSRF_TRUSTED_ORIGINS = [
    o for o in os.environ.get("STARCUBES_CSRF_TRUSTED_ORIGINS", "").split(",") if o
]

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.admin",
    "django.contrib.staticfiles",
    "viz",
    "sources",
]

MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.gzip.GZipMiddleware",
    "starcubes.middleware.RequireLoginMiddleware",
]

ROOT_URLCONF     = "starcubes.urls"
WSGI_APPLICATION = "starcubes.wsgi.application"

TEMPLATES = [
    {
        "BACKEND":  "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS":  {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "sources.context_processors.topbar_sources",
            ],
        },
    },
]


def _db_from_dsn(dsn: str) -> dict:
    """Map a SQLAlchemy-style postgres URL to Django's DATABASES dict."""
    if not dsn:
        return {"ENGINE": "django.db.backends.dummy"}
    u = urlparse(dsn)
    return {
        "ENGINE":   "starcubes.db_backends",
        "NAME":     u.path.lstrip("/"),
        "USER":     unquote(u.username) if u.username else "",
        "PASSWORD": unquote(u.password) if u.password else "",
        "HOST":     u.hostname or "",
        "PORT":     str(u.port) if u.port else "",
        "OPTIONS":  {"options": "-c search_path=starcubes_meta,public"},
    }


STARCUBES_POSTGRES_DSN = os.environ.get("STARCUBES_POSTGRES_DSN", "")
DATABASES = {
    "default": _db_from_dsn(STARCUBES_POSTGRES_DSN),
}

# ── Chart Query console (viz/views.py query_* endpoints) ────────────────────
# Kept well below uwsgi's harakiri = 120: harakiri kills the whole worker
# *process*, taking its other in-flight requests with it, so an ad-hoc query
# must never get anywhere near that. viz.views clamps this to 90 s regardless
# of what the environment says.
STARCUBES_QUERY_TIMEOUT_MS = int(os.environ.get("STARCUBES_QUERY_TIMEOUT_MS", "30000"))
STARCUBES_QUERY_ROW_LIMIT = int(os.environ.get("STARCUBES_QUERY_ROW_LIMIT", "1000"))

# ── ETL logging (sources/etl_spawn.py, sources/etl_logging.py) ──────────────
# Every spawned ETL process (GUI-triggered or forever-mode) writes its own
# per-run log file here instead of the historical /dev/null — see CLAUDE.md
# "ETL logging and observability". STARCUBES_LOG_DIR must be writable by the
# process; the Dockerfile creates /app/logs for the container image.
STARCUBES_LOG_DIR = os.environ.get("STARCUBES_LOG_DIR", str(BASE_DIR / "logs"))
STARCUBES_LOG_LEVEL = os.environ.get("STARCUBES_LOG_LEVEL", "INFO")
# Max number of per-datasource log files kept; oldest beyond this are pruned
# on each spawn.
STARCUBES_LOG_KEEP = int(os.environ.get("STARCUBES_LOG_KEEP", "20"))
# Log files older than this many days are pruned on each spawn regardless of
# STARCUBES_LOG_KEEP.
STARCUBES_LOG_MAX_DAYS = int(os.environ.get("STARCUBES_LOG_MAX_DAYS", "14"))
# A single log file is truncated in place once it exceeds this size (bounds
# a long-running forever session); see core/etl.py:_bound_log_size.
STARCUBES_LOG_MAX_BYTES = int(os.environ.get("STARCUBES_LOG_MAX_BYTES", str(64 * 1024 * 1024)))
# Mirror ETL log lines to the container's PID-1 stdout (so `podman logs` /
# `docker logs` show them), in addition to the per-run log file. Off by
# default outside the container image.
STARCUBES_ETL_LOG_STDOUT = os.environ.get("STARCUBES_ETL_LOG_STDOUT", "0") == "1"
# How often (seconds) import_fact's progress thread emits a heartbeat log
# line with queue depths and per-stage rates.
STARCUBES_ETL_LOG_INTERVAL_S = int(os.environ.get("STARCUBES_ETL_LOG_INTERVAL_S", "30"))

# Initialize oracledb thick mode if Oracle client libs are available.
from core.source import init_oracle_client
init_oracle_client()

STATIC_URL       = "/static/"
STATIC_ROOT      = os.environ.get("STARCUBES_STATIC_ROOT", str(BASE_DIR / "staticfiles"))
STATICFILES_DIRS = [BASE_DIR / "viz" / "static"]

TIME_ZONE = "Europe/Berlin"
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ── Keycloak / OIDC SSO (optional) ──────────────────────────────────────────
# Off by default: ModelBackend (password login) always works regardless of
# these vars. SSO becomes available only once STARCUBES_OIDC_ISSUER,
# STARCUBES_OIDC_CLIENT_ID, and STARCUBES_OIDC_CLIENT_SECRET are ALL set —
# that presence check IS the on/off switch, no separate boolean flag, same
# pattern as CSRF_TRUSTED_ORIGINS being empty meaning "not configured".
#
# mozilla-django-oidc wants each OP endpoint (authorize/token/userinfo/jwks)
# as a separate setting; it does not do dynamic discovery at request time.
# Keycloak's endpoint layout is fixed relative to the realm issuer URL, so
# rather than making the operator hand-configure four URLs that can drift
# out of sync, derive them all from one STARCUBES_OIDC_ISSUER — same
# "one value implies everything" shape as STARCUBES_POSTGRES_DSN. This is
# string concatenation only, no network call, so Django boot never depends
# on Keycloak being reachable.
#
# STARCUBES_OIDC_ISSUER is the realm URL with no trailing slash, e.g.
# https://keycloak.example.com/realms/starcubes (Keycloak Admin Console >
# Realm Settings > General > Endpoints, or {issuer}/.well-known/openid-configuration).
#
# The redirect URI registered on the Keycloak client MUST match what Django
# computes at request time: https://<host>/oidc/callback/. If a TLS-terminating
# proxy sits in front of this container, it must forward X-Forwarded-Proto and
# SECURE_PROXY_SSL_HEADER must be set for that deployment, or Django will
# compute http://... and Keycloak will reject the redirect_uri as a mismatch.
# See HOWTO.md "SSO login" for the full checklist — this is deployment-specific
# in the same way STARCUBES_CSRF_TRUSTED_ORIGINS is, so it isn't auto-derived.
STARCUBES_OIDC_ISSUER = os.environ.get("STARCUBES_OIDC_ISSUER", "").rstrip("/")
STARCUBES_OIDC_CLIENT_ID = os.environ.get("STARCUBES_OIDC_CLIENT_ID", "")
STARCUBES_OIDC_CLIENT_SECRET = os.environ.get("STARCUBES_OIDC_CLIENT_SECRET", "")
# White-labels the login-page button text without a template edit.
STARCUBES_OIDC_DISPLAY_NAME = os.environ.get("STARCUBES_OIDC_DISPLAY_NAME", "Keycloak")

OIDC_ENABLED = bool(
    STARCUBES_OIDC_ISSUER and STARCUBES_OIDC_CLIENT_ID and STARCUBES_OIDC_CLIENT_SECRET
)

# Password login must keep working regardless of OIDC_ENABLED (both
# coexist). ModelBackend here reproduces Django's implicit default (no
# AUTHENTICATION_BACKENDS today == ["...ModelBackend"]) so the OFF path is
# byte-for-byte the same auth behaviour as before this setting existed.
AUTHENTICATION_BACKENDS = ["django.contrib.auth.backends.ModelBackend"]

if OIDC_ENABLED:
    AUTHENTICATION_BACKENDS = [
        "starcubes.oidc.KeycloakOIDCBackend",
        "django.contrib.auth.backends.ModelBackend",
    ]

    OIDC_RP_CLIENT_ID = STARCUBES_OIDC_CLIENT_ID
    OIDC_RP_CLIENT_SECRET = STARCUBES_OIDC_CLIENT_SECRET
    # Keycloak signs ID tokens with RS256 by default; mozilla-django-oidc
    # defaults to HS256 — token verification fails unless set explicitly.
    OIDC_RP_SIGN_ALGO = "RS256"
    # "profile" is required for the preferred_username claim the custom
    # backend keys the Django username on (see starcubes/oidc.py). Without
    # it, Keycloak may omit preferred_username and login falls back to the
    # library's email-hash username.
    OIDC_RP_SCOPES = "openid email profile"

    OIDC_OP_AUTHORIZATION_ENDPOINT = f"{STARCUBES_OIDC_ISSUER}/protocol/openid-connect/auth"
    OIDC_OP_TOKEN_ENDPOINT         = f"{STARCUBES_OIDC_ISSUER}/protocol/openid-connect/token"
    OIDC_OP_USER_ENDPOINT          = f"{STARCUBES_OIDC_ISSUER}/protocol/openid-connect/userinfo"
    OIDC_OP_JWKS_ENDPOINT          = f"{STARCUBES_OIDC_ISSUER}/protocol/openid-connect/certs"
    # RP-initiated logout at Keycloak's own /protocol/openid-connect/logout
    # is intentionally not wired up — local Django session logout (existing
    # LogoutView) is enough for now.

LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "urldecoded": {
            "()": "starcubes.settings.UrlDecodingFormatter",
            "format": "[%(asctime)s] %(message)s",
            "datefmt": "%d/%b/%Y %H:%M:%S",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "urldecoded",
        },
    },
    "loggers": {
        "django.server": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "django.request": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
    },
}
