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
