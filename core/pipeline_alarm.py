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
                 fn: Callable[[object, int], Optional[float]]):
        self.code = code
        self.label = label
        self.title = title
        self.fn = fn


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
    stamp_transitions(counts["transitions"],
                      {**alarm, "measure": cond.label})
    return counts
