"""Threshold alarm evaluation over the loaded fact tables.

`starcubes_meta.alarm` holds the rules; this module is the evaluator. It is
driven by the ETL itself: `sources/management/commands/etl_run.py` calls
`run_alarm_cycle` for its own datasource after every load. That is the only
moment new data exists, so it is the only moment an alarm's answer can have
changed — a separate timer would either re-ask the same question or arrive
after the fact. `manage.py alarm_run` runs one cycle over every datasource
by hand, for testing and manual driving.

Context
-------
An alarm's context is the chart's whole filter expression, stored as the
flat filter rows `[{col, op, vals, andOr, indent}]` in
`alarm.context_filter` (jsonb) — exactly what `chart.html:gatherParams()`
serializes. `NULL`/`[]` means the whole datasource. The nested tree is
derived at evaluation time via `core.query.build_filter_tree` and compiled
with the same `_build_where` the chart endpoints use, so an alarm evaluates
precisely the slice the user was looking at.

Grouped tokens (`<col>:grouped:<slug>`) are forbidden in alarm contexts:
group sets are user-editable (any logged-in user can rename, delete or
re-group them in the Group Dims tab), so a stored grouped reference would
break silently — or worse, keep working while its meaning changes. The CRUD
rejects them at save time (`check_no_grouped_filters`); the evaluator checks
again defensively so a legacy row is logged and skipped, never silently
mis-evaluated. Fan-out tokens (`dimension.filter_group`) stay allowed —
they are admin-configured and a broken one fails validation loudly.

Evaluation — one sliding window per load
----------------------------------------
The clock is the *fact clock* — `max(watermark_fact_sql)`, the data's own
high watermark, not the wall clock (fact data lags behind by the watermark
lag). Each load evaluates exactly one trailing window ending at the HWM:
`[hwm - window_s, hwm)`. Consecutive loads therefore overlap; the question
asked is always "over the last window_s of data we have, does the measure
breach?".

`alarm.eval_end` records the HWM of the last evaluation. A load that did not
move the HWM is skipped outright — the window would be identical and so
would the answer.

Trade-offs (accepted):

* A load that fails or is delayed takes its evaluation with it. Because the
  window slides with the HWM rather than being chained to the previous one,
  a breach that lived entirely between two loads is never seen.
* Same philosophy as `core/audit.py`: rows arriving late into a window that
  has already slid past are never re-evaluated.
* A total data standstill is invisible here — the HWM stops, so the window
  stops sliding and `count(*) < 1` never comes true. Watching the pipeline
  itself (etl_run status, watermark_lag) is the ETL monitor's job.

Episodes
--------
A firing is an *episode* row in `starcubes_meta.alarm_firing` spanning the
first to the last breach, stamped with the **data high watermark** at each
step, never with wall-clock or window-boundary times: opened at the HWM of
the load that first saw the breach (`first_breach_at`), extended to the HWM
of every further breaching load (`last_breach_at`), closed at the HWM of the
first load that saw the value back under the threshold (`closed_at`).
`ux_alarm_firing_open` enforces at most one open episode per alarm, so the
evaluator needs no locking of its own. A SQL NULL aggregate (an empty
`sum(col)` window) is *not* a breach; `count(*)` over an empty window is a
genuine 0, so `count(*) < 1` is a valid "no data" alarm.

The only wired action is the logging itself — the row plus
`logging.getLogger(__name__)`. `notify_channels` keeps its vocabulary
(email/slack/pagerduty stay reserved), see `docs/backlog.md`.

No Django imports — same constraint as the rest of `core/`.
"""
from __future__ import annotations

import logging
import operator
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import text

from .query import (
    _build_where,
    _parse_measure,
    build_filter_tree,
    split_grouped_col,
    validate_filter_tree,
)
from .source import Source

logger = logging.getLogger(__name__)

_COND_OPS = {
    ">": operator.gt,
    "<": operator.lt,
    "=": operator.eq,
    ">=": operator.ge,
    "<=": operator.le,
}


def check_no_grouped_filters(tree: list[dict]) -> None:
    """Raise ValueError if any filter col in `tree` is a grouped token.

    Called at save time (alarm CRUD) and defensively by the evaluator — a
    grouped reference would silently change meaning when its group set is
    edited, so it must never reach `_build_where`.
    """
    grouped: list[str] = []

    def _walk(nodes: list[dict]) -> None:
        for node in nodes:
            col = node["row"]["col"]
            if split_grouped_col(col)[2]:
                grouped.append(col)
            _walk(node["children"])

    _walk(tree)
    if grouped:
        raise ValueError(
            f"grouped dimension filters cannot be used in alarms "
            f"(group sets are editable and would silently change the "
            f"alarm's meaning): {', '.join(sorted(set(grouped)))}")


def measure_value_expr(source: Source, measure: str) -> str:
    """SQL aggregate expression for an alarm's measure string.

    'count(*)' is special-cased *before* `_parse_measure` — its regex would
    otherwise fall through to ('sum', 'count(*)') (the same special case
    lives in `core/query.py:build_chart_sql`).
    """
    if measure == "count(*)":
        return "count(*)"
    mfunc, mcol = _parse_measure(measure)
    fact_cols = {c.name for c in source.fact_columns}
    if mcol not in fact_cols:
        raise ValueError(
            f"measure column {mcol!r} is not a fact column of "
            f"{source.name!r}")
    return f"{mfunc}(f.{mcol})"


def alarm_aggregate_sql(
    source: Source,
    alarm: dict,
    t_from: datetime,
    t_to: datetime,
) -> tuple[str, dict]:
    """Build the one-row aggregate query for one alarm over one window.

    Reuses `_build_where` with the alarm's filter tree — the same compiler
    the chart endpoints use, so the evaluated slice is identical by
    construction. `include_time_range=True` (the default) supplies the
    `f.<partition_column> >= :t_from AND f.<partition_column> < :t_to`
    bounds.
    """
    dims_by_fact = {d.fact_column: d for d in source.dimensions}
    tree = build_filter_tree(alarm.get("context_filter") or [])
    check_no_grouped_filters(tree)
    validate_filter_tree(source, tree)
    where_parts, params = _build_where(
        source.schema_name, source.partition_column,
        t_from, t_to, None, dims_by_fact, filter_tree=tree,
    )
    value_expr = measure_value_expr(source, alarm["measure"])
    sql = (
        f"SELECT {value_expr} AS v "
        f"FROM {source.schema_name}.fact f "
        f"WHERE {' AND '.join(where_parts)}"
    )
    return sql, params


def fact_max_time(source: Source) -> Optional[datetime]:
    """The fact clock: `max(watermark_fact_sql)` over the fact table.

    Deliberately not the wall clock — fact data lags behind by the
    watermark lag, and the evaluator must not advance into windows that
    have no data yet. `watermark_fact_sql()` falls back to
    `partition_column` when no expression is configured.
    """
    with source.dest_engine().connect() as conn:
        return conn.execute(text(
            f"SELECT max({source.watermark_fact_sql()}) "
            f"FROM {source.schema_name}.fact"
        )).scalar()


def _evaluate_window(
    source: Source,
    alarm: dict,
    fact_max: datetime,
) -> tuple[Optional[float], bool]:
    """Evaluate the one trailing window `[fact_max - window_s, fact_max)`.

    Returns `(value, breach)`. A SQL NULL aggregate is never a breach — an
    empty `sum(col)` window says nothing, whereas `count(*)` returns a
    genuine 0 and can legitimately satisfy `< 1`.
    """
    win_start = fact_max - timedelta(seconds=alarm["window_s"])
    sql, params = alarm_aggregate_sql(source, alarm, win_start, fact_max)
    with source.dest_engine().connect() as conn:
        raw = conn.execute(text(sql), params).scalar()
    value = None if raw is None else float(raw)
    breach = value is not None and _COND_OPS[alarm["cond_op"]](
        value, alarm["threshold"])
    return value, breach


def evaluate_alarm(source: Source, meta_engine, alarm: dict) -> dict:
    """Evaluate one alarm at the current HWM and run its episode machine.

    Called once per ETL load. Every episode timestamp written here is the
    data high watermark `fact_max` — the state of the data at the moment the
    breach became visible — never a window boundary and never the wall
    clock.

    All episode/cursor writes happen in a single meta transaction so a crash
    mid-alarm leaves no half-applied episode. The `eval_end` update
    deliberately does not touch `updated_at` — runtime progress must never
    masquerade as a user configuration change in `alarm_history`.
    """
    counts = {"windows": 0, "breaches": 0,
              "episodes_opened": 0, "episodes_extended": 0,
              "episodes_closed": 0}

    fact_max = fact_max_time(source)
    if fact_max is None:
        # Empty fact table (fresh datasource before the first ETL run):
        # nothing to evaluate, and no cursor to set.
        return counts

    if alarm["eval_end"] is not None and fact_max <= alarm["eval_end"]:
        # The load brought no new data: the window would be identical to the
        # one already evaluated, and so would its verdict.
        return counts

    value, breach = _evaluate_window(source, alarm, fact_max)
    counts["windows"] = 1
    counts["breaches"] = int(breach)

    with meta_engine.begin() as conn:
        open_ep = conn.execute(text(
            "SELECT id FROM starcubes_meta.alarm_firing "
            "WHERE alarm_id = :aid AND closed_at IS NULL"
        ), {"aid": alarm["id"]}).scalar()

        if breach:
            if open_ep is None:
                # ux_alarm_firing_open (partial unique on closed_at IS NULL)
                # backs this up: should a second open episode ever race in,
                # the IntegrityError propagates to the per-alarm handler in
                # run_alarm_cycle and is logged.
                conn.execute(text(
                    "INSERT INTO starcubes_meta.alarm_firing "
                    "  (alarm_id, first_breach_at, last_breach_at, "
                    "   first_value, last_value) "
                    "VALUES (:aid, :hwm, :hwm, :v, :v)"
                ), {"aid": alarm["id"], "hwm": fact_max, "v": value})
                counts["episodes_opened"] += 1
                logger.info("alarm %s (%s): episode opened at hwm %s (v=%s)",
                            alarm["name"], source.name, fact_max, value)
            else:
                conn.execute(text(
                    "UPDATE starcubes_meta.alarm_firing "
                    "SET last_breach_at = :hwm, last_value = :v "
                    "WHERE id = :eid"
                ), {"hwm": fact_max, "v": value, "eid": open_ep})
                counts["episodes_extended"] += 1
        elif open_ep is not None:
            conn.execute(text(
                "UPDATE starcubes_meta.alarm_firing "
                "SET closed_at = :hwm WHERE id = :eid"
            ), {"hwm": fact_max, "eid": open_ep})
            counts["episodes_closed"] += 1
            logger.info("alarm %s (%s): episode closed at hwm %s",
                        alarm["name"], source.name, fact_max)

        conn.execute(text(
            "UPDATE starcubes_meta.alarm SET eval_end = :end "
            "WHERE id = :aid"
        ), {"end": fact_max, "aid": alarm["id"]})

    return counts


def run_alarm_cycle(meta_engine, dest_dsn: str, *,
                    datasource_id: Optional[int] = None,
                    source: Optional[Source] = None) -> dict:
    """One evaluation cycle over every enabled alarm.

    `datasource_id` narrows the cycle to one datasource — that is how
    `etl_run` drives it after a load. `source` lets that caller hand in the
    `Source` it already holds, so the cycle does not re-load it; it is only
    honoured for the datasource it belongs to.

    Sources are otherwise loaded once per datasource per cycle; each alarm
    and each datasource is isolated in its own try/except so one broken rule
    (say, a filter referencing a deleted fan-out group) is logged and skipped
    instead of killing the cycle. Returns counters for the caller's summary
    line.
    """
    counts = {"alarms": 0, "windows": 0, "breaches": 0,
              "episodes_opened": 0, "episodes_extended": 0,
              "episodes_closed": 0, "errors": 0}

    where = "WHERE a.enabled"
    params: dict = {}
    if datasource_id is not None:
        where += " AND a.datasource_id = :ds"
        params["ds"] = datasource_id

    with meta_engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT a.id, a.datasource_id, a.name, a.context_filter, "
            "       a.measure, a.cond_op, a.threshold, a.window_s, "
            "       a.eval_end, d.name AS ds_name "
            "  FROM starcubes_meta.alarm a "
            "  JOIN starcubes_meta.datasource d ON d.id = a.datasource_id "
            f" {where} "
            " ORDER BY a.datasource_id, a.name"
        ), params).mappings().all()
    alarms = [dict(r) for r in rows]
    counts["alarms"] = len(alarms)
    if not alarms:
        return counts

    by_ds: dict[int, list[dict]] = {}
    for alarm in alarms:
        by_ds.setdefault(alarm["datasource_id"], []).append(alarm)

    for ds_id, ds_alarms in by_ds.items():
        if source is not None and source.id == ds_id:
            ds_source = source
        else:
            try:
                ds_source = Source.load(meta_engine, dest_dsn, ds_id)
            except Exception:
                logger.exception("alarm cycle: loading datasource %s failed, "
                                 "skipping its %d alarms", ds_id,
                                 len(ds_alarms))
                counts["errors"] += len(ds_alarms)
                continue
        for alarm in ds_alarms:
            try:
                res = evaluate_alarm(ds_source, meta_engine, alarm)
            except Exception:
                logger.exception("alarm cycle: evaluating alarm %s (%s) "
                                 "failed, skipping it", alarm["name"],
                                 alarm["ds_name"])
                counts["errors"] += 1
                continue
            for key in ("windows", "breaches", "episodes_opened",
                        "episodes_extended", "episodes_closed"):
                counts[key] += res[key]

    return counts
