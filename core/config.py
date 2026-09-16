"""Global app configuration resolver — env var first, DB row fallback.

One key-value table (`starcubes_meta.app_config`, migration 0031) holds the
global settings an operator may edit in the GUI instead of passing env
vars: the notification transports and the SSO/OIDC login. The key IS the
env-var name it shadows. Resolution is env-first everywhere:

    1. env var set  -> the env value wins (an empty one counts as unset, on
                       every path below — the image ships some as ENV "")
    2. DB row       -> the stored value
    3. neither      -> the caller's default

This module is Django-free (the usual core/ constraint), so it is importable
by the web process, the spawned `etl_run` subprocess and standalone
`alarm_run` alike — the DB is the shared state, no env injection needed.

Never raises on a meta-DB error: one WARNING, then the last good snapshot
(or env/default on the first failure). The web write path calls
`invalidate()` so the saving process writes see their own change
immediately; other processes converge within the 30 s TTL.
"""
from __future__ import annotations

import logging
import os
import threading
import time

from sqlalchemy import text

from .source import _get_cached_engine

logger = logging.getLogger(__name__)

# The keys this table is read for — the env-var names themselves.
_CONFIG_KEYS = [
    "STARCUBES_SMTP_HOST", "STARCUBES_SMTP_PORT",
    "STARCUBES_SMTP_USER", "STARCUBES_SMTP_PASSWORD",
    "STARCUBES_SMTP_FROM", "STARCUBES_SMTP_TLS",
    "STARCUBES_SMTP_SUBJECT_PREFIX",
    "STARCUBES_XMPP_JID", "STARCUBES_XMPP_PASSWORD",
    "STARCUBES_XMPP_HOST", "STARCUBES_XMPP_PORT",
    "STARCUBES_PAGERDUTY_EVENTS_URL", "STARCUBES_PAGERDUTY_SEVERITY",
    "STARCUBES_OIDC_ISSUER", "STARCUBES_OIDC_CLIENT_ID",
    "STARCUBES_OIDC_CLIENT_SECRET", "STARCUBES_OIDC_DISPLAY_NAME",
]

_TTL_S = 30.0

_lock = threading.Lock()
_cache: dict | None = None    # last good DB snapshot (17 keys, str or None)
_cache_at: float = 0.0


def invalidate() -> None:
    """Drop the snapshot cache (the GUI write path calls this)."""
    global _cache, _cache_at
    with _lock:
        _cache = None
        _cache_at = 0.0


def get(key: str) -> str | None:
    """Resolve one key: env var first, then the DB row, else None."""
    env = os.environ.get(key)
    if env:
        return env
    return _snapshot().get(key)


def effective() -> dict:
    """The full resolution of all 17 keys plus the derived on/off booleans,
    recomputed from the *resolved* values (they replace the frozen
    settings.STARCUBES_ALARM_EMAIL_ENABLED / _XMPP_ENABLED)."""
    snap = _snapshot()
    out = dict(snap)
    out["alarm_email_enabled"] = bool(
        out.get("STARCUBES_SMTP_HOST") and out.get("STARCUBES_SMTP_FROM"))
    out["alarm_xmpp_enabled"] = bool(
        out.get("STARCUBES_XMPP_JID") and out.get("STARCUBES_XMPP_PASSWORD"))
    return out


def effective_oidc() -> dict:
    """The four SSO keys, env-first — the entry point the boot-time override
    (starcubes/oidc.py) and the login page read."""
    snap = _snapshot()
    return {k: snap.get(k) for k in (
        "STARCUBES_OIDC_ISSUER", "STARCUBES_OIDC_CLIENT_ID",
        "STARCUBES_OIDC_CLIENT_SECRET", "STARCUBES_OIDC_DISPLAY_NAME")}


def state() -> tuple[dict, dict]:
    """(env values, DB values) for both key sets — the uncached view the
    admin page needs to render "set via environment" vs "stored here" and
    to pre-fill forms. DB read errors degrade to an empty DB dict (the page
    renders env-only) instead of raising."""
    env = {k: os.environ.get(k) for k in _CONFIG_KEYS}
    dsn = os.environ.get("STARCUBES_POSTGRES_DSN")
    if not dsn:
        return env, {}
    try:
        with _get_cached_engine(dsn).connect() as conn:
            rows = conn.execute(text(
                "SELECT key, value FROM starcubes_meta.app_config"
            )).all()
        return env, dict(rows)
    except Exception:
        logger.warning("app_config unreadable — rendering env values only")
        return env, {}


def _snapshot() -> dict:
    """The cached resolution of all 17 keys.

    env values are read live on every call (cheap, and an operator changing
    an env var across a restart wins immediately). DB rows come from the TTL
    cache; on a meta-DB error the last good snapshot (or env/default on the
    first failure) is served so nothing here can take the app down."""
    global _cache, _cache_at
    dsn = os.environ.get("STARCUBES_POSTGRES_DSN")
    if not dsn:
        return {k: os.environ.get(k) or None for k in _CONFIG_KEYS}
    now = time.monotonic()
    if _cache is not None and now - _cache_at < _TTL_S:
        out = dict(_cache)
        for k in _CONFIG_KEYS:
            if os.environ.get(k):
                out[k] = os.environ[k]
        return out
    try:
        with _get_cached_engine(dsn).connect() as conn:
            rows = conn.execute(text(
                "SELECT key, value FROM starcubes_meta.app_config"
            )).all()
        db = dict(rows)
        merged = {k: os.environ.get(k) or db.get(k) for k in _CONFIG_KEYS}
    except Exception:
        logger.warning("app_config unreadable — serving the last good "
                       "snapshot (env/default values)")
        if _cache is not None:
            return dict(_cache)
        return {k: os.environ.get(k) or None for k in _CONFIG_KEYS}
    with _lock:
        _cache = merged
        _cache_at = now
    return merged