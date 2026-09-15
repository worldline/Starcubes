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

The rows store dimension *ids*, because an id is what the fact table joins
on and what survives a rename. An id is not what a user recognises, though,
so `resolve_context_labels` looks the names up at display time — never
writing them back, so a renamed dimension row reads correctly the next time
the list is opened.

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
`logging.getLogger(__name__)`. `notify_channels` selects which transports
fire: `sources/alarm_notify.py` (Django-side) resolves the per-channel
recipients and sends. This module's job is to *announce*: every open/close
transition is returned to the caller as a transition dict (see
`evaluate_alarm`), in the same transaction that wrote the episode row, so a
caller can dispatch notifications without re-querying what happened. See
`docs/alarm.md` and `docs/backlog.md` for the channel semantics.

No Django imports — same constraint as the rest of `core/`.
"""
from __future__ import annotations

import logging
import operator
import os
from datetime import datetime, timedelta, timezone
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

# The alarm phase runs synchronously inside the ETL loop, so a pathological
# filter must not be able to block it without bound. The chart's equivalent
# query has had a timeout all along; this is the same instrument for the
# evaluator, which builds the same SQL through the same _build_where.
# Env rather than a Django setting: core/ must stay importable without
# Django (the spawned etl_run and the standalone alarm_run both land here).
ALARM_TIMEOUT_MS = int(os.environ.get("STARCUBES_ALARM_TIMEOUT_MS", 30_000))

# Distinguishes "the caller did not pass a fact_max" from "the fact table is
# empty", which is what a None fact_max means everywhere else in here.
_UNSET = object()


def _set_timeout(conn) -> None:
    """Bound one statement. `SET LOCAL` needs a transaction and cannot take
    a bind parameter under the extended protocol, hence set_config() — the
    same form core/audit.py uses for its fact-side aggregates."""
    conn.execute(text("SELECT set_config('statement_timeout', :ms, true)"),
                 {"ms": str(int(ALARM_TIMEOUT_MS))})

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


def normalize_context_rows(rows: Optional[list[dict]]) -> list[tuple]:
    """Canonical form of a context, for comparing two alarms' filters.

    Two contexts are "the same filter" when this returns equal values. Each
    row becomes ``(col, op, tuple(sorted(vals)), andOr, indent)``: the values
    are sorted because their order never mattered, and stringified because a
    context arrives as JSON from one caller and as jsonb from another.

    The *row* order is deliberately preserved. Together with `indent` and
    `andOr` it encodes the tree `build_filter_tree` rebuilds, so reordering
    rows is not generally meaning-preserving. The cost is that two contexts
    that differ only in a harmless reordering are not recognised as the same
    — acceptable, because every context is produced by the same chart flow,
    which emits its rows in a stable order.
    """
    out: list[tuple] = []
    for raw in (rows or []):
        if not isinstance(raw, dict):
            continue
        row = raw.get("row", raw)           # both flat shapes, as elsewhere
        if not isinstance(row, dict):
            continue
        vals = tuple(sorted(str(v) for v in (row.get("vals") or [])))
        out.append((str(row.get("col") or ""), str(row.get("op") or ""),
                    vals, str(row.get("andOr") or "and"),
                    int(row.get("indent") or 0)))
    return out


# Ops whose vals are dimension source_ids. `starts_with` and friends match
# free text, and a grouped token's vals are group names — neither addresses a
# dimension row, so neither is resolvable to a name.
_LABELLED_OPS = {"in", "not_in"}


def resolve_context_labels(
        source: Source,
        contexts: list[Optional[list[dict]]],
) -> dict[str, dict[str, str]]:
    """Map the ids stored in alarm contexts to their dimension names.

    Returns ``{filter_column: {source_id: name}}`` over every row of every
    context in `contexts`. A context stores ids (`peer_id in (9999)`) because
    ids are what the fact table joins on and what survives a rename — but an
    id is not what the user recognises, so the *display* resolves it. Nothing
    is written back: the name is looked up when it is shown, so a renamed
    dimension row reads correctly the next time the list is opened.

    Whole lists are resolved in one call on purpose: the lookup is one query
    per dim *table*, not one per alarm, so an alarm list of any size costs the
    same handful of queries.

    Column resolution mirrors `core.query._filter_sql` exactly — including
    fan-out tokens (`dimension.filter_group`), which resolve through the
    dim_table their members share. A dim table that is configured but not
    created yet is skipped rather than raised, the same stance `viz.views`
    takes for its meta endpoint.

    A column that resolves to a dim table always appears in the result, even
    when none of its ids are left — that is what lets the caller tell an id
    whose dimension row has gone (show it raw, flagged) from a column that
    never carried names in the first place (show it raw, unremarkably).
    """
    dims_by_fact = {d.fact_column: d for d in source.dimensions}
    fanout = source.filter_fanout()
    table_of: dict[str, Optional[str]] = {}

    def _dim_table(col: str) -> Optional[str]:
        if col not in table_of:
            if split_grouped_col(col)[2]:
                table_of[col] = None            # vals are group names already
            else:
                member = (fanout.get(col) or [col])[0]
                dim = dims_by_fact.get(member)
                table_of[col] = (dim.dim_table
                                 if dim and dim.dim_table else None)
        return table_of[col]

    ids_by_col: dict[str, set[str]] = {}
    ids_by_table: dict[str, set[str]] = {}
    for ctx in contexts:
        for raw in (ctx or []):
            if not isinstance(raw, dict):
                continue
            # Tolerate both flat shapes build_filter_tree accepts.
            row = raw.get("row", raw)
            if not isinstance(row, dict) or row.get("op") not in _LABELLED_OPS:
                continue
            col, vals = row.get("col"), row.get("vals") or []
            if not col or not vals:
                continue
            table = _dim_table(col)
            if not table:
                continue
            ids = {str(v) for v in vals}
            ids_by_col.setdefault(col, set()).update(ids)
            ids_by_table.setdefault(table, set()).update(ids)

    if not ids_by_table:
        return {}

    names: dict[str, dict[str, str]] = {}
    with source.dest_engine().begin() as conn:
        for table, ids in ids_by_table.items():
            qname = f"{source.schema_name}.{table}"
            if conn.execute(text("SELECT to_regclass(:q)"),
                            {"q": qname}).scalar() is None:
                continue
            rows = conn.execute(text(
                f"SELECT source_id, name FROM {qname} "
                f" WHERE source_id = ANY(:ids)"
            ), {"ids": sorted(ids)}).all()
            names[table] = {str(r[0]): r[1] for r in rows if r[1] is not None}

    out: dict[str, dict[str, str]] = {}
    for col, ids in ids_by_col.items():
        table = table_of[col]
        if table is None or table not in names:
            continue                      # no dim table, or it does not exist
        found = names[table]
        out[col] = {i: found[i] for i in ids if i in found}
    return out


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
        fact_types={c.name: c.pg_type for c in source.fact_columns},
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

    Called **once per datasource per cycle** by `run_alarm_cycle`, which
    hands the result to every alarm on that datasource. It used to run per
    alarm: twenty alarms meant twenty identical max() scans per load, and
    with a derived watermark expression there is neither a BRIN nor
    partition pruning to make them cheap.
    """
    with source.dest_engine().begin() as conn:
        _set_timeout(conn)
        return conn.execute(text(
            f"SELECT max({source.watermark_fact_sql()}) "
            f"FROM {source.schema_name}.fact"
        )).scalar()


def _window_below_floor(source: Source, alarm: dict,
                        fact_max: datetime) -> bool:
    """True when this alarm's window reaches below the retention floor.

    Such a window must not be evaluated, and that is arithmetic rather than
    caution. Below the floor the partitions are gone *on purpose*, so the
    aggregate silently measures a shorter span than the rule asked for — and
    the error is not symmetric. `count(*) <` and `sum(col) <`, which is
    exactly what people write to detect an outage, then report an outage
    that retention itself caused. `>` and `>=` merely miss breaches.

    The floor and the window are both in the partition clock (`_build_where`
    filters on `partition_column`), so the comparison is sound even when
    `watermark_fact_expr` puts `fact_max` on a different one.
    """
    floor = getattr(source, "retention_floor", None)
    if floor is None:
        return False
    if floor.tzinfo is None:
        floor = floor.replace(tzinfo=timezone.utc)
    win_start = fact_max - timedelta(seconds=alarm["window_s"])
    if win_start >= floor:
        return False
    logger.warning(
        "alarm %s (%s): window starts %s, below the retention floor %s — "
        "not evaluated. Shorten window_s below %.0fs or raise "
        "retention_periods; a truncated window would make a 'too little "
        "data' rule fire on data that was disposed of on purpose.",
        alarm["name"], source.name, win_start, floor,
        (fact_max - floor).total_seconds())
    return True


def _evaluate_window(
    source: Source,
    alarm: dict,
    fact_max: datetime,
) -> tuple[Optional[float], bool]:
    """Evaluate the one trailing window `[fact_max - window_s, fact_max)`.

    Returns `(value, breach)`. A SQL NULL aggregate is never a breach — an
    empty `sum(col)` window says nothing, whereas `count(*)` returns a
    genuine 0 and can legitimately satisfy `< 1`.

    Callers must have cleared `_window_below_floor` first — a truncated
    window produces a confident wrong number, not an obviously missing one.
    """
    win_start = fact_max - timedelta(seconds=alarm["window_s"])
    sql, params = alarm_aggregate_sql(source, alarm, win_start, fact_max)
    with source.dest_engine().begin() as conn:
        _set_timeout(conn)
        raw = conn.execute(text(sql), params).scalar()
    value = None if raw is None else float(raw)
    breach = value is not None and _COND_OPS[alarm["cond_op"]](
        value, alarm["threshold"])
    return value, breach


def evaluate_alarm(source: Source, meta_engine, alarm: dict, *,
                   fact_max=_UNSET) -> dict:
    """Evaluate one alarm at the current HWM and run its episode machine.

    Called once per ETL load. Every episode timestamp written here is the
    data high watermark `fact_max` — the state of the data at the moment the
    breach became visible — never a window boundary and never the wall
    clock.

    All episode/cursor writes happen in a single meta transaction so a crash
    mid-alarm leaves no half-applied episode. The `eval_end` update
    deliberately does not touch `updated_at` — runtime progress must never
    masquerade as a user configuration change in `alarm_history`.

    `fact_max` is the datasource's fact clock. `run_alarm_cycle` reads it
    once and passes it in for every alarm on that datasource; a caller that
    omits it gets the old behaviour of reading it here.
    """
    counts = {"windows": 0, "breaches": 0,
              "episodes_opened": 0, "episodes_extended": 0,
              "episodes_closed": 0, "transitions": []}

    if fact_max is _UNSET:
        fact_max = fact_max_time(source)
    if fact_max is None:
        # Empty fact table (fresh datasource before the first ETL run):
        # nothing to evaluate, and no cursor to set.
        return counts

    if alarm["eval_end"] is not None and fact_max <= alarm["eval_end"]:
        # The load brought no new data: the window would be identical to the
        # one already evaluated, and so would its verdict.
        return counts

    if _window_below_floor(source, alarm, fact_max):
        # Not "no breach" — no answer. Same shape as the eval_end skip above:
        # no window counted, no episode touched, and a standing episode is
        # deliberately left open rather than closed on missing evidence.
        return counts

    value, breach = _evaluate_window(source, alarm, fact_max)
    counts["windows"] = 1
    if breach:
        counts["breaches"] = 1

    episode_step(meta_engine, alarm, breach=breach, at=fact_max, value=value,
                 counts=counts, label=source.name)
    stamp_transitions(counts["transitions"], alarm)
    return counts


def episode_step(meta_engine, alarm: dict, *, breach: bool, at: datetime,
                 value: Optional[float], counts: dict,
                 label: str = "") -> None:
    """Open, extend or close this alarm's episode, and record the cursor.

    The whole of the episode machine, and deliberately ignorant of *why*
    something breached: it takes a verdict, an instant and a value. That is
    what lets a pipeline alarm — whose condition is ETL state rather than a
    measure over fact rows — reuse it verbatim instead of growing a second,
    subtly different copy (see `core/pipeline_alarm.py`).

    `at` is the clock the episode is stamped with. For a threshold alarm
    that is the data high watermark; for a pipeline alarm it is the wall
    clock, because "how long has nothing arrived" has no answer in data
    time. `label` only ever appears in the log line.

    Everything happens in one transaction, so a crash mid-alarm leaves no
    half-applied episode. The `eval_end` update deliberately does not touch
    `updated_at` — runtime progress must never masquerade as a user
    configuration change in `alarm_history`.
    """
    transitions = counts["transitions"]
    with meta_engine.begin() as conn:
        # Only the columns the two transition dicts need: the episode row's
        # first breach stamps and values travel into the "closed" transition
        # unchanged (the UPDATE below does not touch them).
        open_ep = conn.execute(text(
            "SELECT id, first_breach_at, last_breach_at, first_value, last_value "
            "FROM starcubes_meta.alarm_firing "
            "WHERE alarm_id = :aid AND closed_at IS NULL"
        ), {"aid": alarm["id"]}).mappings().first()

        if breach:
            if open_ep is None:
                # ux_alarm_firing_open (partial unique on closed_at IS NULL)
                # backs this up: should a second open episode ever race in,
                # the IntegrityError propagates to the per-alarm handler in
                # run_alarm_cycle and is logged.
                episode_id = conn.execute(text(
                    "INSERT INTO starcubes_meta.alarm_firing "
                    "  (alarm_id, first_breach_at, last_breach_at, "
                    "   first_value, last_value) "
                    "VALUES (:aid, :at, :at, :v, :v) RETURNING id"
                ), {"aid": alarm["id"], "at": at, "v": value}).scalar()
                counts["episodes_opened"] += 1
                transitions.append({
                    "kind": "opened", "episode_id": episode_id,
                    "value": value,
                    "first_breach_at": at, "last_breach_at": at,
                    "closed_at": None,
                })
                logger.info("alarm %s (%s): episode opened at %s (v=%s)",
                            alarm["name"], label, at, value)
            else:
                conn.execute(text(
                    "UPDATE starcubes_meta.alarm_firing "
                    "SET last_breach_at = :at, last_value = :v "
                    "WHERE id = :eid"
                ), {"at": at, "v": value, "eid": open_ep["id"]})
                counts["episodes_extended"] += 1
        elif open_ep is not None:
            conn.execute(text(
                "UPDATE starcubes_meta.alarm_firing "
                "SET closed_at = :at WHERE id = :eid"
            ), {"at": at, "eid": open_ep["id"]})
            counts["episodes_closed"] += 1
            transitions.append({
                "kind": "closed", "episode_id": open_ep["id"],
                "value": value,
                "first_breach_at": open_ep["first_breach_at"],
                "last_breach_at": open_ep["last_breach_at"],
                "closed_at": at,
            })
            logger.info("alarm %s (%s): episode closed at %s",
                        alarm["name"], label, at)

        conn.execute(text(
            "UPDATE starcubes_meta.alarm SET eval_end = :end "
            "WHERE id = :aid"
        ), {"end": at, "aid": alarm["id"]})


def stamp_transitions(transitions: list[dict], alarm: dict) -> None:
    """Stamp every transition with the alarm's identity and channel config.

    Plain row data the Django-side dispatcher (`sources/alarm_notify.py`)
    needs to resolve recipients and routing keys. No transport knowledge
    here: `core/` never resolves addresses, `auth_user` is Django-managed.

    `measure` is what the dispatcher renders as the condition, and it is
    subscripted there rather than `.get()`. A pipeline alarm has no measure,
    so `core/pipeline_alarm.py` puts a readable condition label in that key
    before calling this — which is exactly why the notification path needed
    no change at all to carry a second kind of alarm.

    `note` is an optional free line for the notification body, absent for
    every threshold alarm and for most pipeline ones. It exists because a
    condition answers with a *number*, which is what the episode machine
    needs and occasionally not what the reader needs -- "rows arrived 18
    minutes late" is a measurement, "adding 5 minutes would have caught 90%
    of them" is the decision it feeds.

    Note what is *not* stamped: an `alarm.rule_kind` value must never land
    on a transition under the key `kind`, which already means opened/closed.
    """
    for t in transitions:
        t.update(
            alarm_id=alarm["id"], alarm_name=alarm["name"],
            datasource_id=alarm["datasource_id"], ds_name=alarm["ds_name"],
            measure=alarm["measure"], cond_op=alarm["cond_op"],
            threshold=alarm["threshold"], window_s=alarm["window_s"],
            context_filter=alarm.get("context_filter"),
            notify_channels=alarm.get("notify_channels") or [],
            notify_email_recipients=alarm.get("notify_email_recipients") or [],
            notify_xmpp_recipients=alarm.get("notify_xmpp_recipients") or [],
            severity=alarm.get("severity") or None,
            note=alarm.get("note") or None,
        )


def _run_pipeline_alarms(meta_engine, alarms: list[dict],
                         counts: dict) -> None:
    """Evaluate the pipeline rules of a cycle, under the shared lock.

    Imported lazily: `core/pipeline_alarm.py` imports the episode machine
    from here, so a module-level import would be circular. It is also the
    honest shape — a cycle with no pipeline rules should not pay for the
    import at all.

    The lock is what lets three evaluators coexist (the ETL loop, the
    request path and the supervisor process). Without it they would race to
    open the same episode, one would lose on `ux_alarm_firing_open`, and the
    cycle would count a spurious error every time two of them happened to
    tick together.
    """
    if not alarms:
        return
    from .pipeline_alarm import evaluate_pipeline_alarm, pipeline_eval_lock

    with pipeline_eval_lock(meta_engine) as got:
        if not got:
            logger.debug("pipeline alarms: another evaluator holds the "
                         "lock, skipping this cycle")
            return
        for alarm in alarms:
            try:
                res = evaluate_pipeline_alarm(meta_engine, alarm)
            except Exception:
                logger.exception("alarm cycle: evaluating pipeline alarm "
                                 "%s (%s) failed, skipping it",
                                 alarm["name"], alarm["ds_name"])
                counts["errors"] += 1
                continue
            for key in ("windows", "breaches", "episodes_opened",
                        "episodes_extended", "episodes_closed"):
                counts[key] += res[key]
            counts["transitions"].extend(res["transitions"])


def run_alarm_cycle(meta_engine, dest_dsn: str, *,
                    datasource_id: Optional[int] = None,
                    source: Optional[Source] = None,
                    kinds: tuple[str, ...] = ("threshold", "pipeline")) -> dict:
    """One evaluation cycle over every enabled alarm.

    `datasource_id` narrows the cycle to one datasource — that is how
    `etl_run` drives it after a load. `source` lets that caller hand in the
    `Source` it already holds, so the cycle does not re-load it; it is only
    honoured for the datasource it belongs to.

    `kinds` selects which sort of rule to evaluate. It matters more than it
    looks: a cycle restricted to `("pipeline",)` touches nothing but
    `starcubes_meta` — no `Source.load`, no source connection, no fact
    query — which is what lets the middleware and the supervisor process
    evaluate alarms for a datasource whose source database is unreachable,
    or whose ETL is precisely the thing that has stopped.

    Sources are otherwise loaded once per datasource per cycle; each alarm
    and each datasource is isolated in its own try/except so one broken rule
    (say, a filter referencing a deleted fan-out group) is logged and skipped
    instead of killing the cycle. Returns counters for the caller's summary
    line.
    """
    counts = {"alarms": 0, "windows": 0, "breaches": 0,
              "episodes_opened": 0, "episodes_extended": 0,
              "episodes_closed": 0, "errors": 0, "transitions": []}

    where = "WHERE a.enabled AND a.rule_kind = ANY(:kinds)"
    params: dict = {"kinds": list(kinds)}
    if datasource_id is not None:
        where += " AND a.datasource_id = :ds"
        params["ds"] = datasource_id

    with meta_engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT a.id, a.datasource_id, a.name, a.context_filter, "
            "       a.rule_kind, a.condition, "
            "       a.measure, a.cond_op, a.threshold, a.window_s, "
            "       a.notify_channels, a.notify_email_recipients, "
            "       a.notify_xmpp_recipients, a.severity, "
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

    # Pipeline alarms first, and separately: they need no Source and no fact
    # clock, so evaluating them must not be held hostage to loading one.
    _run_pipeline_alarms(
        meta_engine, [a for a in alarms if a["rule_kind"] == "pipeline"],
        counts)

    by_ds: dict[int, list[dict]] = {}
    for alarm in alarms:
        if alarm["rule_kind"] != "threshold":
            continue
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
        # One fact clock per datasource, not one per alarm: every alarm here
        # evaluates against the same HWM, and the max() behind it is the
        # expensive part of the whole phase.
        try:
            ds_fact_max = fact_max_time(ds_source)
        except Exception:
            logger.exception("alarm cycle: reading the fact clock of "
                             "datasource %s failed, skipping its %d alarms",
                             ds_id, len(ds_alarms))
            counts["errors"] += len(ds_alarms)
            continue
        if ds_fact_max is None:
            # Empty fact table: nothing to evaluate and no cursor to set,
            # exactly what evaluate_alarm would conclude per alarm.
            continue
        for alarm in ds_alarms:
            try:
                res = evaluate_alarm(ds_source, meta_engine, alarm,
                                     fact_max=ds_fact_max)
            except Exception:
                logger.exception("alarm cycle: evaluating alarm %s (%s) "
                                 "failed, skipping it", alarm["name"],
                                 alarm["ds_name"])
                counts["errors"] += 1
                continue
            for key in ("windows", "breaches", "episodes_opened",
                        "episodes_extended", "episodes_closed"):
                counts[key] += res[key]

            # The transitions ride along to the Django-side dispatcher —
            # this module stays free of any transport or addressing detail
            # (`auth_user` is Django-managed, see sources/alarm_notify.py).
            counts["transitions"].extend(res["transitions"])

    return counts
