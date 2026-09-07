"""Alarm notification transports — e-mail, XMPP and PagerDuty.

The alarm evaluator (`core/alarm.py`) produces episode transitions and stays
transport-free; this module is the Django-side counterpart that resolves the
addresses and sends. One public entry point (`dispatch_transitions`), never
raises — a notification failure must neither colour the ETL's exit status nor
suppress the other channels: each channel's send sits in its own try/except,
so one broken SMTP server cannot suppress a PagerDuty trigger.

Recipients are the login users: e-mail goes to `auth_user.email`, XMPP to
`auth_user.xmpp_jid`. The PagerDuty integration token is a per-datasource
routing key. Global transport config lives in `STARCUBES_*` env vars — see
`starcubes/settings.py` and `docs/deployment-config.md`. There is deliberately
no free-form recipient list; the accepted limitation is that no external or
distribution-list recipients are possible (docs/alarm.md).
"""
from __future__ import annotations

import asyncio
import json
import logging
import smtplib
import urllib.request
from email.message import EmailMessage
from email.utils import formatdate

from sqlalchemy import text

from core import config as core_config

from . import accounts

logger = logging.getLogger("sources.alarm_notify")


def _cfg(key: str, default=None):
    """Resolved setting — env var first, DB row, else the default. Replaces
    the direct `settings.STARCUBES_*` reads so the transports see GUI-edited
    values (and so spawned etl_run / standalone alarm_run resolve the same
    way, without Django settings)."""
    v = core_config.get(key)
    return default if v is None else v

# Bound every network send so a dead transport cannot stall the ETL loop.
_TIMEOUT_S = 10
_XMPP_TIMEOUT_S = 30

# Dedup key starcubes-alarm-<alarm_id>#<episode_id>. Per-episode on purpose:
# a re-opened episode must produce a fresh PagerDuty incident instead of
# being silently deduplicated into the resolved one.
_PD_DEDUP_PREFIX = "starcubes-alarm"
_PD_DEDUP_SEP = "#"

# Channels with an address book (the two int[] columns). `pagerduty` needs
# no recipient ids. `log` is not a selectable channel any more — the episode
# row plus the evaluator's log line are written unconditionally in
# core/alarm.py, so the log side of an alarm needs no dispatch at all.
_ADDRESSED = ("email", "xmpp")


def _fmt(v) -> str:
    """Numbers as a person reads them; anything else as-is.

    `%g` alone drops into scientific notation past six significant digits,
    so a count of 8729883 rows — or a pipeline alarm reporting 8729883
    seconds since the last successful run — arrived in the subject line as
    "8.72988e+06". Correct, and useless in an alert somebody is reading at
    three in the morning. Large magnitudes get thousands separators
    instead; small ones keep `%g`, which is what makes 3.9 render as "3.9"
    rather than "3.900000".
    """
    if v is None:
        return "-"
    if isinstance(v, float):
        if abs(v) >= 10000:
            return f"{v:,.0f}"
        return f"{v:g}"
    if isinstance(v, int) and abs(v) >= 10000:
        return f"{v:,d}"
    return str(v)


def _ctx_summary(ctx) -> str:
    """Compact one-line form of a context (the flat filter rows a context
    carries). Display only — the same shape `detail.html:ctxSummary` renders
    client-side."""
    if not ctx:
        return "whole datasource"
    parts = []
    for raw in ctx:
        if not isinstance(raw, dict):
            continue
        row = raw.get("row", raw)
        if not isinstance(row, dict):
            continue
        vals = ", ".join(str(v) for v in (row.get("vals") or []))
        parts.append(f"{row.get('col')} {row.get('op')} ({vals})")
    return " AND ".join(parts) or "whole datasource"


def render_message(t: dict) -> tuple[str, str]:
    """Subject and body for one episode transition, shared by the e-mail and
    XMPP transports (and the PagerDuty `summary`). English on purpose — every
    other user-visible string in the app is English too."""
    opened = t.get("kind") == "opened"
    word = "OPENED" if opened else "RESOLVED"
    subject = (
        f"{_cfg('STARCUBES_SMTP_SUBJECT_PREFIX', '[starcubes]')} {word}: "
        f"{t['ds_name']} / {t['alarm_name']} — "
        f"{t['measure']} {t['cond_op']} {_fmt(t['threshold'])} "
        f"(value {_fmt(t['value'])})"
    )
    lines = [
        f"Alarm: {t['alarm_name']}" + (" triggered" if opened else " resolved"),
        f"Datasource: {t['ds_name']}",
        f"Context: {_ctx_summary(t.get('context_filter'))}",
        f"Condition: {t['measure']} {t['cond_op']} {_fmt(t['threshold'])}"
        f" — value {_fmt(t['value'])}",
        f"Episode: #{t['episode_id']}",
        f"First breach at: {_fmt(t['first_breach_at'])}",
        f"Last breach at: {_fmt(t['last_breach_at'])}",
        f"Closed at: {_fmt(t['closed_at'])}",
        f"Details: /sources/alarms/firings/?ds={t['datasource_id']}",
    ]
    return subject, "\n".join(lines)


def send_email(to_addrs: list[str], subject: str, body: str) -> None:
    """One e-mail to every recipient over one SMTP connection."""
    host = _cfg("STARCUBES_SMTP_HOST")
    from_ = _cfg("STARCUBES_SMTP_FROM")
    if not (host and from_):
        raise RuntimeError("e-mail transport not configured (STARCUBES_SMTP_HOST/_FROM)")
    if not to_addrs:
        return
    msg = EmailMessage()
    msg["From"] = from_
    msg["To"] = ", ".join(to_addrs)
    msg["Subject"] = subject
    msg["Date"] = formatdate()
    msg.set_content(body)

    tls = _cfg("STARCUBES_SMTP_TLS", "starttls")
    port = int(_cfg("STARCUBES_SMTP_PORT", "25"))
    if tls == "ssl":
        smtp = smtplib.SMTP_SSL(host, port, timeout=_TIMEOUT_S)
    else:
        smtp = smtplib.SMTP(host, port, timeout=_TIMEOUT_S)
    with smtp:
        if tls == "starttls":
            smtp.starttls()
        user, password = (_cfg("STARCUBES_SMTP_USER"),
                          _cfg("STARCUBES_SMTP_PASSWORD"))
        if user and password:
            smtp.login(user, password)
        smtp.send_message(msg)


async def _xmpp_run(jids: list[str], text_msg: str) -> None:
    """Connect, send one chat message per JID, disconnect. Runs inside
    `asyncio.run` and is itself bounded by `asyncio.wait_for`."""
    from slixmpp import ClientXMPP

    jid = _cfg("STARCUBES_XMPP_JID")
    password = _cfg("STARCUBES_XMPP_PASSWORD")
    if not (jid and password):
        raise RuntimeError("XMPP transport not configured (STARCUBES_XMPP_JID/_PASSWORD)")

    client = ClientXMPP(jid, password)
    client.register_plugin("xep_0030")   # service discovery
    client.register_plugin("xep_0199")   # XMPP ping

    done = asyncio.Event()

    def _on_session(_event):
        for to in jids:
            client.send_message(mto=to, mbody=text_msg, mtype="chat")
        # Let the stream flush before dropping the connection.
        asyncio.get_running_loop().call_later(0.5, done.set)

    client.add_event_handler("session_start", _on_session)

    kwargs = {}
    if _cfg("STARCUBES_XMPP_HOST"):
        kwargs = {"host": _cfg("STARCUBES_XMPP_HOST"),
                  "port": int(_cfg("STARCUBES_XMPP_PORT", "5222"))}
    await client.connect(**kwargs)
    try:
        await asyncio.wait_for(done.wait(), timeout=_XMPP_TIMEOUT_S)
    finally:
        client.disconnect()


def send_xmpp(to_jids: list[str], body: str) -> None:
    """One bot account sends one chat message per JID. Synchronous wrapper —
    the ETL loop and `alarm_run` are plain sync commands."""
    if not to_jids:
        return
    asyncio.run(asyncio.wait_for(_xmpp_run(to_jids, body),
                                 timeout=_XMPP_TIMEOUT_S + _TIMEOUT_S))


def send_pagerduty(routing_key: str, t: dict) -> None:
    """Events API v2 `trigger` on open, `resolve` on close — the resolve is
    what closes PagerDuty's incident when the alarm clears."""
    dedup = (_PD_DEDUP_PREFIX + "-" + str(t["alarm_id"])
             + _PD_DEDUP_SEP + str(t["episode_id"]))
    opened = t.get("kind") == "opened"
    _, body = render_message(t)
    payload = {
        "routing_key": routing_key,
        "dedup_key": dedup,
        "event_action": "trigger" if opened else "resolve",
        "payload": {
            "source": f"starcubes/{t['ds_name']}",
            "severity": (t.get("severity")
                         or _cfg("STARCUBES_PAGERDUTY_SEVERITY", "critical")),
            "summary": body.splitlines()[0],
            "custom_details": {
                "alarm_id": t["alarm_id"],
                "alarm_name": t["alarm_name"],
                "episode_id": t["episode_id"],
                "measure": t["measure"],
                "cond_op": t["cond_op"],
                "threshold": t["threshold"],
                "value": t["value"],
                "first_breach_at": _fmt(t["first_breach_at"]),
                "last_breach_at": _fmt(t["last_breach_at"]),
                "closed_at": _fmt(t["closed_at"]),
            },
        },
    }
    _post_pagerduty(payload)


def _post_pagerduty(payload: dict) -> None:
    req = urllib.request.Request(
        _cfg("STARCUBES_PAGERDUTY_EVENTS_URL",
             "https://events.pagerduty.com/v2/enqueue"),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
        resp.read()


def send_pagerduty_test(routing_key: str, ds_name: str, ds_id: int) -> None:
    """Synthetic trigger+resolve pair for the PagerDuty admin page's test
    button. Dedup key `starcubes-test-<ds_id>` is stable per datasource, so
    a stale test incident from an earlier button press closes itself when
    the next test runs."""
    dedup = f"starcubes-test-{ds_id}"
    payload = {
        "routing_key": routing_key,
        "dedup_key": dedup,
        "payload": {
            "source": f"starcubes/{ds_name}",
            # Deliberately the global value: the test button exercises the
            # transport wiring, not any alarm's routing choice.
            "severity": _cfg("STARCUBES_PAGERDUTY_SEVERITY", "critical"),
            "summary": f"starcubes PagerDuty test — {ds_name}",
        },
    }
    _post_pagerduty(payload)
    payload["event_action"] = "resolve"
    _post_pagerduty(payload)


def _resolve_lookups(meta_engine, transitions: list[dict]) -> tuple[dict, dict]:
    """Resolve user ids and datasource ids of every transition in two batched
    lookups (one query per table, not one per transition)."""
    user_ids: set = set()
    for t in transitions:
        user_ids.update(t.get("notify_email_recipients") or [])
        user_ids.update(t.get("notify_xmpp_recipients") or [])
    ds_ids = sorted({t["datasource_id"] for t in transitions})

    users: dict = {}
    routing_keys: dict = {}
    with meta_engine.connect() as conn:
        if user_ids:
            rows = conn.execute(text(
                f"SELECT {accounts.CONTACT_COLS} "
                "FROM starcubes_meta.auth_user WHERE id = ANY(:ids)"
            ), {"ids": user_ids}).mappings().all()
            users = {r["id"]: dict(r) for r in rows}
        if ds_ids:
            rows = conn.execute(text(
                "SELECT id, pagerduty_routing_key "
                "FROM starcubes_meta.datasource WHERE id = ANY(:ids)"
            ), {"ids": ds_ids}).mappings().all()
            routing_keys = {r["id"]: r["pagerduty_routing_key"] for r in rows}
    return users, routing_keys


def dispatch_transitions(transitions: list[dict], meta_engine) -> dict:
    """Send every episode transition to its selected channels. The only
    public entry point — the management commands call this directly after
    `run_alarm_cycle`. Never raises: the address lookup failing (meta DB
    unreachable) counts as one error per channel it would have served."""
    counters = {"email": 0, "xmpp": 0, "pagerduty": 0, "errors": 0}
    if not transitions:
        return counters

    try:
        needs_lookup = any(t.get("notify_channels") for t in transitions)
        users, routing_keys = (
            _resolve_lookups(meta_engine, transitions)
            if needs_lookup else ({}, {}))
    except Exception:
        logger.exception("alarm notify: address lookup failed, "
                         "notifications skipped")
        users, routing_keys = None, None

    for t in transitions:
        subject, body = render_message(t)
        channels = t.get("notify_channels") or []
        for channel in channels:
            if users is None:
                # The address lookup failed — nothing addressable can be
                # served (log is not dispatched, see the _ADDRESSED note).
                counters["errors"] += 1
                continue
            # One try/except per channel: a broken SMTP server must not
            # suppress a PagerDuty trigger.
            try:
                if channel == "email":
                    to_addrs = _addresses(users, t, "email")
                    if not to_addrs:
                        logger.warning(
                            "alarm %s: email channel selected but no recipient "
                            "has an e-mail address", t["alarm_name"])
                        counters["errors"] += 1
                    else:
                        send_email(to_addrs, subject, body)
                        counters["email"] += 1
                elif channel == "xmpp":
                    to_jids = _addresses(users, t, "xmpp")
                    if not to_jids:
                        logger.warning(
                            "alarm %s: xmpp channel selected but no recipient "
                            "has an XMPP JID", t["alarm_name"])
                        counters["errors"] += 1
                    else:
                        send_xmpp(to_jids, body)
                        counters["xmpp"] += 1
                elif channel == "pagerduty":
                    key = routing_keys.get(t["datasource_id"])
                    if not key:
                        logger.warning(
                            "alarm %s: pagerduty channel selected but datasource "
                            "%s has no pagerduty_routing_key",
                            t["alarm_name"], t["datasource_id"])
                        counters["errors"] += 1
                    else:
                        send_pagerduty(key, t)
                        counters["pagerduty"] += 1
            except Exception:
                logger.exception("alarm %s: %s notification failed",
                                 t["alarm_name"], channel)
                counters["errors"] += 1
    return counters


def _addresses(users: dict, t: dict, kind: str) -> list[str]:
    """Address list for one channel kind, dropping users whose address is
    missing or who has not opted in to the channel (each drop logged as
    WARNING by the caller's empty-list check)."""
    ids = t.get(f"notify_{kind}_recipients") or []
    optin_col = "notify_email" if kind == "email" else "notify_xmpp"
    addrs = []
    for uid in ids:
        u = users.get(uid)
        addr_col = "email" if kind == "email" else "xmpp_jid"
        if u is None or not u.get(addr_col):
            logger.warning("alarm %s: user %s selected for %s channel "
                           "has no address", t["alarm_name"], uid, kind)
            continue
        if not u.get(optin_col):
            logger.warning("alarm %s: user %s selected for %s channel has "
                           "not opted in to %s notifications (My Profile)",
                           t["alarm_name"], uid, kind, kind)
            continue
        addrs.append(u[addr_col])
    return addrs