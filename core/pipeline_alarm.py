"""Alarms whose condition is the pipeline rather than the data.

Everything the preflight (`core/preflight.py`), the housekeeping
(`core/housekeeping.py`) and `etl_run.max_wm`/`src_now` detect had, until
now, no way of reaching a person. The findings sat in `etl_run.stats`, in
`housekeeping_run.report` and in a GUI panel, and were seen when somebody
looked.

The alarm subsystem already owns everything hard about *not* being seen:
channels, per-user opt-in, PagerDuty severity and dedup keys, and above all
the episode semantics in `alarm_firing` — one row per incident, opened and
closed, at most one open per rule. A pipeline condition has exactly that
shape: it starts, it persists, it ends. So this is not a second alarm
subsystem; it is a second *kind* of row in the existing one, sharing the
episode machine (`core.alarm.episode_step`), the notification path and the
Firings view.

**Every condition here answers with a number**, so `cond_op` and `threshold`
decide the breach exactly as they do for a threshold alarm and none of the
episode bookkeeping needed a pipeline-shaped variant. `None` means "not
answerable yet" — no run has happened, no housekeeping report exists — and
is treated as "no breach that we can assert", which deliberately does *not*
close a standing episode: silence is not recovery.

**Everything is read from `starcubes_meta` only.** No `Source.load`, no
source connection, no fact query. That is the property that lets the
request-path middleware and the supervisor process evaluate these for a
datasource whose source database is unreachable — or whose ETL is precisely
the thing that has stopped.

**The clock is the wall clock**, unlike a threshold alarm, which stamps its
episodes with the data high watermark. That is not a compromise: a threshold
alarm asks a question about data and must answer in the data's own time,
while "how long has nothing arrived" has no answer in data time at all. For
the same reason the `eval_end` skip does not apply here — a pipeline
condition changes without any new data — and no replay cursor is needed,
because the episode machine is idempotent and `ux_alarm_firing_open` allows
only one open episode per rule.

See `docs/alarm.md` and U7/U9/U10/N13 in `docs/etl-automation.md`.
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Callable, Optional

from sqlalchemy import text

from .alarm import _COND_OPS, episode_step, stamp_transitions

logger = logging.getLogger(__name__)

# Advisory-lock key for "somebody is evaluating pipeline alarms right now".
# Same class id as the datasource lock so both are recognisable in pg_locks;
# objid 0 is free because no datasource can have id 0 (serial starts at 1).
_LOCK_CLASS = 0x53544152
_LOCK_OBJID = 0

# How long a middleware-driven evaluation waits before bothering again.
# Read here rather than in the middleware so the CLI and the supervisor
# agree on it without importing Django.
EVAL_INTERVAL_S = int(os.environ.get("STARCUBES_PIPELINE_ALARM_INTERVAL_S", 60))


@contextmanager
def pipeline_eval_lock(meta_engine):
    """Yield True when this process may evaluate pipeline alarms.

    Three evaluators exist by design — the ETL loop after each load, the
    request path, and the supervisor process — because a wedged loop cannot
    report that it is wedged and a request path only runs when somebody is
    looking. Without an interlock they would race to open the same episode,
    one would lose on `ux_alarm_firing_open`, and every coincidental
    overlap would be counted as an error.

    `pg_try_advisory_lock` is non-blocking on purpose: a missed cycle is
    nothing, the next one is seconds away, and blocking a request thread on
    a lock held by the ETL would be worse than the problem.
    """
    conn = meta_engine.connect()
    got = False
    try:
        got = bool(conn.execute(text("SELECT pg_try_advisory_lock(:c, :o)"),
                                {"c": _LOCK_CLASS, "o": _LOCK_OBJID}).scalar())
        conn.commit()
        yield got
    finally:
        try:
            if got:
                conn.execute(text("SELECT pg_advisory_unlock(:c, :o)"),
                             {"c": _LOCK_CLASS, "o": _LOCK_OBJID})
                conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# The conditions
# ---------------------------------------------------------------------------

def _data_stale(meta_engine, ds_id: int) -> Optional[float]:
    """Seconds the newest row in the source lags the source's own clock.

    Both operands come from the source (`etl_run.src_now`/`max_wm`, migration
    0035), so the figure carries no clock skew. This is the only thing in the
    system that can see a source which is reachable and has stopped
    producing: a standstill freezes the high watermark, so a *threshold*
    alarm's trailing window stops sliding and can never fire.
    """
    with meta_engine.begin() as conn:
        row = conn.execute(text(
            "SELECT extract(epoch from (src_now - max_wm)) AS lag "
            "  FROM starcubes_meta.etl_run "
            " WHERE datasource_id = :ds "
            "   AND max_wm IS NOT NULL AND src_now IS NOT NULL "
            " ORDER BY started_at DESC LIMIT 1"
        ), {"ds": ds_id}).mappings().first()
    return None if row is None else float(row["lag"])


def _run_failed(meta_engine, ds_id: int) -> Optional[float]:
    """Failed runs since the last successful one.

    A count rather than a boolean so one condition covers both "tell me
    about any failure" (threshold 1) and "tell me when it keeps failing"
    (threshold 3). Backfills are excluded: a failed backfill is a manual
    operation going wrong, not the pipeline being unwell.
    """
    with meta_engine.begin() as conn:
        n = conn.execute(text(
            "SELECT count(*) FROM starcubes_meta.etl_run "
            " WHERE datasource_id = :ds AND kind = 'incremental' "
            "   AND status = 'failed' "
            "   AND id > coalesce((SELECT max(id) "
            "                        FROM starcubes_meta.etl_run "
            "                       WHERE datasource_id = :ds "
            "                         AND status = 'ok'), 0)"
        ), {"ds": ds_id}).scalar()
    return float(n or 0)


def _no_recent_run(meta_engine, ds_id: int) -> Optional[float]:
    """Seconds since the last successful run finished.

    The one condition that can see a forever loop which is wedged or simply
    gone, and the reason this module must be evaluated from outside the ETL
    process as well as inside it. Falls back to the datasource's creation
    time so a source that has *never* loaded is not silently fine.
    """
    with meta_engine.begin() as conn:
        row = conn.execute(text(
            "SELECT extract(epoch from (now() - coalesce("
            "         (SELECT max(finished_at) FROM starcubes_meta.etl_run "
            "           WHERE datasource_id = d.id AND status = 'ok'), "
            "         d.created_at))) AS age "
            "  FROM starcubes_meta.datasource d WHERE d.id = :ds"
        ), {"ds": ds_id}).mappings().first()
    return None if row is None else float(row["age"])


def _preflight_warn(meta_engine, ds_id: int) -> Optional[float]:
    """Preflight findings at warn or block on the newest run that made one.

    One rule delivers U1, U2, U5b, U10 and N16 at once — the checks produce
    the signal, this turns it into a message. Blocks are counted too, though
    a block also fails the run, so `run_failed` usually speaks first.
    """
    # Counted in Python rather than in SQL: an aggregate over the unnested
    # array cannot tell "no run has a preflight report" (which must answer
    # None) from "the newest report is clean" (which must answer 0), and
    # asking twice to find out is worse than counting five list entries.
    with meta_engine.begin() as conn:
        checks = conn.execute(text(
            "SELECT stats->'preflight'->'checks' AS checks "
            "  FROM starcubes_meta.etl_run "
            " WHERE datasource_id = :ds AND stats ? 'preflight' "
            " ORDER BY id DESC LIMIT 1"
        ), {"ds": ds_id}).scalar()
    if checks is None:
        return None
    return float(sum(1 for c in checks
                     if c.get("level") in ("warn", "block")))


def _dim_full_pct(meta_engine, ds_id: int) -> Optional[float]:
    """The fullest dimension, in percent of the 32767 smallserial ceiling.

    From the newest housekeeping report. At 100% the next new value fails
    inside `_resolve_auto_dim_chunk`, in the parent, mid-extract — which is
    why this is worth hearing about at 80.
    """
    with meta_engine.begin() as conn:
        dims = conn.execute(text(
            "SELECT report->'dimensions' AS dims "
            "  FROM starcubes_meta.housekeeping_run "
            " WHERE datasource_id = :ds "
            " ORDER BY id DESC LIMIT 1"
        ), {"ds": ds_id}).scalar()
    if not dims:
        # No housekeeping run yet, or a datasource with no dimensions at
        # all. Neither is a breach, and neither is evidence of recovery.
        return None
    return max(float(d.get("pct_full") or 0) for d in dims)


def _audit_gap(meta_engine, ds_id: int) -> Optional[float]:
    """Rows the newest completed audit found missing from fact.

    The one condition that can see a **late arrival beyond the overlap**.
    `late_arrival_overlap_s` re-reads a trailing span on every load, so a row
    that shows up within it is caught; a row slower than that is already below
    the high watermark when it appears, and no load will ever look there
    again. Nothing in the pipeline notices — the run says `status='ok'`, the
    watermark keeps moving, and the gap is permanent until somebody compares
    the two sides.

    `starcubes_meta.audit_run` is that comparison, written by
    `core/audit.py:run_audit` on the forever loop's schedule. Reading it here
    keeps this module's rule intact: metadata only, no `Source.load`, no
    source connection, so the condition stays answerable for a datasource
    whose source is unreachable.

    `None` — not zero — while no audit has completed. Zero would assert "no
    gap", which is exactly what an unaudited datasource cannot claim, and the
    module treats None as "no breach we can assert" without closing a
    standing episode. That is also what makes `audit_interval_s` defaulting
    to off harmless: the alarm exists and stays quiet until somebody turns
    the audit on.

    A `failed` audit is skipped rather than read as clean, for the same
    reason: its counts describe the part that ran.
    """
    with meta_engine.begin() as conn:
        n = conn.execute(text(
            "SELECT missing_count FROM starcubes_meta.audit_run "
            " WHERE datasource_id = :ds AND status = 'ok' "
            "   AND missing_count IS NOT NULL "
            " ORDER BY started_at DESC LIMIT 1"
        ), {"ds": ds_id}).scalar()
    return None if n is None else float(n)


def _needed_lag_p99(meta_engine, ds_id: int) -> Optional[float]:
    """Seconds the load window would have had to reach further back.

    The diagnosis behind `audit_gap`. That condition says rows were lost;
    this one says *by how much* -- `etl_run.last_end_time` minus the missing
    row's own watermark value, measured by `core/audit.py:audit_id_details`
    and stored by stage three of `run_audit`.

    Raw seconds, deliberately, and not a shortfall against either knob. The
    number applies to both and they are not interchangeable: raising
    `watermark_lag_s` by it keeps the window's tip below the row and holds
    *every* row back with it, while `late_arrival_overlap_s` set to it
    re-reads a trailing span instead and leaves freshness alone. Which price
    to pay is an operator's decision, so the alarm states the measurement and
    `_needed_lag_note` below spells out what each increment would buy.

    `None` when no successful audit has found anything -- never zero. Zero
    would assert "nothing arrives late", which a datasource with no audit
    cannot claim, and the module treats None as no assertion either way.
    """
    with meta_engine.begin() as conn:
        v = conn.execute(text(
            "SELECT needed_lag_p99_s FROM starcubes_meta.audit_run "
            " WHERE datasource_id = :ds AND status = 'ok' "
            "   AND needed_lag_p99_s IS NOT NULL "
            " ORDER BY started_at DESC LIMIT 1"
        ), {"ds": ds_id}).scalar()
    return None if v is None else float(v)


def _needed_lag_note(meta_engine, ds_id: int) -> Optional[str]:
    """The ladder, as one line for the notification body.

    Answers the question an operator actually has in front of the number:
    *what do I have to add, and what does that buy me.* Both knobs are named
    with their current values, because the decision is between them and
    neither is derivable from the measurement alone.
    """
    with meta_engine.begin() as conn:
        row = conn.execute(text(
            "SELECT a.report -> 'lag_ladder' AS ladder, "
            "       d.watermark_lag_s AS wl, "
            "       d.late_arrival_overlap_s AS ov "
            "  FROM starcubes_meta.audit_run a "
            "  JOIN starcubes_meta.datasource d ON d.id = a.datasource_id "
            " WHERE a.datasource_id = :ds AND a.status = 'ok' "
            "   AND a.needed_lag_p99_s IS NOT NULL "
            " ORDER BY a.started_at DESC LIMIT 1"
        ), {"ds": ds_id}).mappings().first()
    if not row or not row["ladder"]:
        return None
    rungs = ", ".join(f"+{_fmt_secs(r['add_s'])} -> {r['caught_pct']:g}%"
                      for r in row["ladder"])
    return (f"Current reach: watermark_lag_s={row['wl']}s, "
            f"late_arrival_overlap_s={row['ov']}s. "
            f"Would have caught: {rungs}. "
            f"Either knob buys the same reach -- watermark_lag_s delays every "
            f"row, late_arrival_overlap_s re-reads a trailing span instead.")


def _fmt_secs(n: float) -> str:
    """Seconds as something an operator reads without counting zeroes."""
    n = int(n)
    if n < 120:
        return f"{n}s"
    if n < 7200:
        return f"{n // 60}min"
    if n < 172800:
        return f"{n // 3600}h"
    return f"{n // 86400}d"


class _Condition:
    """One pipeline condition: how to measure it, and how to say it.

    `label` goes into the transition's `measure` key, which is what the
    notification dispatcher renders as the condition and subscripts rather
    than `.get()`s. Putting a readable label there is what let a second kind
    of alarm reuse `sources/alarm_notify.py` without a single edit — the
    subject line comes out as
    "OPENED: payments / no data — source data lag (s) > 3600 (value 14400)".
    """

    def __init__(self, code: str, label: str, title: str,
                 fn: Callable[[object, int], Optional[float]],
                 note_fn: Optional[Callable[[object, int],
                                            Optional[str]]] = None):
        self.code = code
        self.label = label
        self.title = title
        self.fn = fn
        # Optional one-line elaboration for the notification body. A
        # condition answers with a number, which is what the episode machine
        # needs; a number is sometimes not what the *reader* needs. Only
        # `needed_lag_p99` has one so far, because only there is the number
        # an input to a decision rather than a verdict.
        self.note_fn = note_fn


CONDITIONS: dict[str, _Condition] = {
    c.code: c for c in (
        _Condition("data_stale", "source data lag (s)",
                   "Source has stopped producing", _data_stale),
        _Condition("run_failed", "failed runs since last ok",
                   "ETL runs are failing", _run_failed),
        _Condition("no_recent_run", "seconds since last successful run",
                   "No ETL run is completing", _no_recent_run),
        _Condition("preflight_warn", "preflight findings (warn+block)",
                   "Preflight is reporting problems", _preflight_warn),
        _Condition("dim_full_pct", "fullest dimension (% of 32767)",
                   "A dimension is filling up", _dim_full_pct),
        _Condition("audit_gap", "rows missing in the newest audit",
                   "The audit found rows the ETL lost", _audit_gap),
        _Condition("needed_lag_p99", "lateness of the missing rows, p99 (s)",
                   "Rows are arriving later than the window reaches",
                   _needed_lag_p99, note_fn=_needed_lag_note),
    )
}


def evaluate_pipeline_alarm(meta_engine, alarm: dict) -> dict:
    """Evaluate one pipeline alarm and run its episode machine.

    Returns the same counter shape as `core.alarm.evaluate_alarm`, so
    `run_alarm_cycle` sums both kinds without caring which produced what.
    """
    counts = {"windows": 0, "breaches": 0,
              "episodes_opened": 0, "episodes_extended": 0,
              "episodes_closed": 0, "transitions": []}

    cond = CONDITIONS.get(alarm.get("condition") or "")
    if cond is None:
        # The CHECK constraint makes this unreachable from the GUI, but a
        # row written by hand — or by a newer version of this code, on a
        # database an older one is still reading — must be skipped loudly
        # rather than evaluated as "no breach".
        raise ValueError(
            f"alarm {alarm['name']!r}: unknown pipeline condition "
            f"{alarm.get('condition')!r}")

    value = cond.fn(meta_engine, alarm["datasource_id"])
    if value is None:
        # Nothing to judge yet. Deliberately not "no breach": closing a
        # standing episode because the evidence went missing would report a
        # recovery that did not happen.
        return counts

    counts["windows"] = 1
    breach = _COND_OPS[alarm["cond_op"]](value, alarm["threshold"])
    if breach:
        counts["breaches"] = 1

    episode_step(meta_engine, alarm, breach=breach,
                 at=datetime.now(timezone.utc), value=value,
                 counts=counts, label=alarm["ds_name"])

    # The label stands in for `measure` so the notification path needs no
    # knowledge of pipeline alarms at all. See `_Condition`.
    #
    # The note is computed only when there is a transition to carry it: it is
    # a second metadata read, and the overwhelming majority of evaluations
    # produce no transition at all.
    note = None
    if counts["transitions"] and cond.note_fn is not None:
        try:
            note = cond.note_fn(meta_engine, alarm["datasource_id"])
        except Exception:
            # An elaboration that fails must not cost the notification it was
            # meant to elaborate.
            logger.exception("%s: building the note for %s failed",
                             alarm.get("ds_name"), cond.code)
    stamp_transitions(counts["transitions"],
                      {**alarm, "measure": cond.label, "note": note})
    return counts
