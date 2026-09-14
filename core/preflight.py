"""Pre-load checks for one datasource.

Everything that has to be true *before* an `import_fact` starts, expressed
as catalogue queries and one round trip against the source. The rules this
module follows come from `docs/etl-automation.md`:

    Preflight detects and blocks. It only repairs what is idempotent and
    finishes in milliseconds. Everything else is reported and waits for a
    human gesture.

So a missing fact/dim table is created here (the creators are idempotent and
the alternative is a foreign-key violation deep inside the transform), while
a missing index is only ever *reported* — `create_indices` takes ACCESS
EXCLUSIVE on the partitioned parent and there is no `CONCURRENTLY` anywhere
in this repo, so an unattended index build on a billion-row fact table would
block every COPY for hours while looking, from the outside, exactly like a
wedged ETL.

Django-free, like everything in `core/`. Returns a structured report rather
than raising — the shape follows `core/audit.py`: plain dicts, ISO strings,
`None` for "this probe failed" rather than a bogus zero. `import_fact` is
what turns a `block` into a failed run; nothing in here decides that.
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

import sqlalchemy
from sqlalchemy import text
from sqlalchemy.pool import NullPool

from .schema import create_dim_tables, create_fact
from .source import _SESSION_UTC_SQL, Source

logger = logging.getLogger(__name__)

CHECK_OK = "ok"
CHECK_WARN = "warn"
CHECK_BLOCK = "block"

# Meta columns the running code needs. Probed against information_schema so a
# code update that skipped `manage.py bootstrap` fails with one sentence
# instead of an UndefinedColumn out of an unrelated query (N16).
_REQUIRED_META_COLUMNS: tuple[tuple[str, str], ...] = (
    ("alarm", "severity"),
    ("datasource", "retention_floor"),
    ("datasource", "dedupe_from"),
    ("datasource", "max_run_s"),
    ("datasource", "retention_periods"),
    ("etl_run", "src_now"),
    ("alarm", "rule_kind"),
)

# The index check is time-aware on purpose: the first load deliberately runs
# without indices (fastest COPY) and the operator builds them afterwards, so
# "no indices" is the legitimate initial state. The real complaint is "this
# has been syncing for weeks and the indices are still missing", which needs
# both a row count and an age to express.
_INDEX_WARN_ROWS = int(os.environ.get("STARCUBES_INDEX_WARN_ROWS", 1_000_000))
_INDEX_WARN_HOURS = float(os.environ.get("STARCUBES_INDEX_WARN_HOURS", 24))

# How far the stored high watermark may stand above the fact tip before it is
# worth a line. A quiet source legitimately widens this gap, so it can only
# ever warn (see _check_watermark_tip for the second reason).
_HWM_GAP_WARN_S = float(os.environ.get("STARCUBES_HWM_GAP_WARN_S", 86400))


def _fmt_s(seconds: float) -> str:
    """Seconds in whatever unit reads naturally. A threshold of 90 s should
    not render as "0.0h"."""
    seconds = float(seconds)
    if abs(seconds) < 120:
        return f"{seconds:.0f}s"
    if abs(seconds) < 7200:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f}h"


def _check(code: str, level: str, message: str, **detail) -> dict:
    return {"code": code, "level": level, "message": message,
            "detail": detail}


def relation_exists(conn, schema: str, relname: str) -> bool:
    """True if `schema.relname` exists as a relation.

    Shared with core/etl.py's partition maintenance, which needs the same
    probe before a TRUNCATE/DROP.
    """
    return bool(conn.execute(text(
        "SELECT 1 FROM pg_class c "
        "  JOIN pg_namespace n ON n.oid = c.relnamespace "
        " WHERE n.nspname = :s AND c.relname = :t"
    ), {"s": schema, "t": relname}).scalar())


def _check_schema_objects(source: Source, dest_engine, *,
                          repair: bool) -> tuple[dict, list[str]]:
    """U2 — fact table and dim tables exist.

    `create_fact` / `create_dim_tables` are fully idempotent but are only
    ever invoked when a datasource is created or edited; `import_fact`
    assumes they ran. A dropped dim table otherwise surfaces late and badly,
    as a foreign-key violation or a KeyError inside the transform.
    """
    schema = source.schema_name
    wanted_dims = [d.dim_table for d in source.primary_dimensions()
                   if d.dim_table]
    repairs: list[str] = []
    with dest_engine.connect() as conn:
        fact_missing = not relation_exists(conn, schema, "fact")
        missing_dims = [t for t in wanted_dims
                        if not relation_exists(conn, schema, t)]

    if not fact_missing and not missing_dims:
        return _check("schema_objects", CHECK_OK,
                      f"{schema}.fact and {len(wanted_dims)} dim table(s) "
                      f"present",
                      dim_tables=wanted_dims), repairs

    missing = (["fact"] if fact_missing else []) + missing_dims
    if not repair:
        return _check("schema_objects", CHECK_BLOCK,
                      f"missing in schema {schema}: {', '.join(missing)}",
                      missing=missing), repairs

    # Creating a fact table mid-life is never routine — say so loudly, and
    # record it so the GUI can show that this run had to repair its own
    # schema. The cost of auto-creation is that a mistyped schema_name now
    # yields a second empty schema instead of a loud failure; the WARNING
    # and the stats entry are what keep that visible.
    if fact_missing:
        logger.warning("%s: %s.fact is missing — creating it",
                       source.name, schema)
        create_fact(source, dest_engine)
        repairs.append(f"created {schema}.fact")
    if missing_dims:
        logger.warning("%s: dim table(s) missing — creating: %s",
                       source.name, ", ".join(missing_dims))
        created = create_dim_tables(source, dest_engine, quiet=True)
        repairs.extend(f"created {schema}.{t}" for t in created)
    return _check("schema_objects", CHECK_WARN,
                  f"created missing object(s) in schema {schema}: "
                  f"{', '.join(missing)}",
                  missing=missing, repaired=True), repairs


def _check_meta_migrations(meta_engine) -> dict:
    """N16 — the metadata schema is at least as new as the code."""
    with meta_engine.begin() as conn:
        present = {(r[0], r[1]) for r in conn.execute(text(
            "SELECT table_name, column_name "
            "  FROM information_schema.columns "
            " WHERE table_schema = 'starcubes_meta'"
        )).all()}
    missing = [f"{t}.{c}" for (t, c) in _REQUIRED_META_COLUMNS
               if (t, c) not in present]
    if not missing:
        return _check("meta_migrations", CHECK_OK,
                      "starcubes_meta is up to date")
    return _check("meta_migrations", CHECK_BLOCK,
                  f"starcubes_meta is behind the code (missing: "
                  f"{', '.join(missing)}) — run `manage.py bootstrap`",
                  missing=missing)


def _expected_indices(source: Source) -> tuple[list[str], list[str]]:
    """The index and FK names `create_indices` builds, in its own order."""
    names = [f"ix_{ix.column_name}" for ix in source.indices]
    names += [f"ix_{d.fact_column}" for d in source.dimensions if d.dim_table]
    fks = [f"fk_{d.fact_column}" for d in source.primary_dimensions()]
    return names, fks


def _check_indices(source: Source, dest_engine, meta_engine) -> dict:
    """U10 — the indices and foreign keys `create_indices` would build.

    Report only, never build. Suppressed while the datasource is young or
    small, so the legitimate initial-load state does not nag; escalates to a
    warning once real volume has been loaded over real time without them.
    """
    schema = source.schema_name
    want_ix, want_fk = _expected_indices(source)
    with dest_engine.connect() as conn:
        if not relation_exists(conn, schema, "fact"):
            return _check("indices", CHECK_OK,
                          "skipped — fact table does not exist yet")
        have_ix = set(conn.execute(text(
            "SELECT i.relname "
            "  FROM pg_index x "
            "  JOIN pg_class i ON i.oid = x.indexrelid "
            "  JOIN pg_class t ON t.oid = x.indrelid "
            "  JOIN pg_namespace n ON n.oid = t.relnamespace "
            " WHERE n.nspname = :s AND t.relname = 'fact'"
        ), {"s": schema}).scalars().all())
        have_fk = set(conn.execute(text(
            "SELECT conname FROM pg_constraint "
            " WHERE conrelid = CAST(:rel AS regclass) AND contype = 'f'"
        ), {"rel": f"{schema}.fact"}).scalars().all())

    missing_ix = [n for n in want_ix if n not in have_ix]
    missing_fk = [n for n in want_fk if n not in have_fk]
    if not missing_ix and not missing_fk:
        return _check("indices", CHECK_OK,
                      f"{len(want_ix)} index/indices and {len(want_fk)} "
                      f"foreign key(s) present")

    with meta_engine.begin() as conn:
        row = conn.execute(text(
            "SELECT coalesce(sum(rows), 0) AS rows, min(started_at) AS since "
            "  FROM starcubes_meta.etl_run "
            " WHERE datasource_id = :ds AND status = 'ok'"
        ), {"ds": source.id}).mappings().first()
    loaded = int((row or {}).get("rows") or 0)
    since = (row or {}).get("since")
    age_h = 0.0
    if since is not None:
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        age_h = (datetime.now(timezone.utc) - since).total_seconds() / 3600.0

    detail = {"missing_indices": missing_ix, "missing_foreign_keys": missing_fk,
              "rows_loaded": loaded, "age_hours": round(age_h, 1)}
    if loaded >= _INDEX_WARN_ROWS and age_h >= _INDEX_WARN_HOURS:
        return _check("indices", CHECK_WARN,
                      f"{len(missing_ix) + len(missing_fk)} index/FK "
                      f"missing after {loaded} rows over {age_h:.0f}h — run "
                      f"`manage.py create_indices --name {source.name}` "
                      f"(missing: "
                      f"{', '.join(missing_ix + missing_fk)})",
                      **detail)
    return _check("indices", CHECK_OK,
                  f"{len(missing_ix) + len(missing_fk)} index/FK not built "
                  f"yet — normal during the initial load",
                  **detail)


def _check_warehouse_drift(source: Source, dest_engine) -> dict:
    """U5a — the fact table's columns against what the configuration says.

    `sync_schema` with `drop_columns=None` is already a pure dry run
    returning a diff, so this is a reading of that diff and nothing more.
    **The execute half is deliberately unreachable from here**: it is
    `drop_indices` → ALTER → `create_indices`, which on a billion-row fact
    table is hours of ACCESS EXCLUSIVE, and no preflight may start that.

    The severities follow from what the load would actually do:

    * A configured column the table does not have (`add_columns`) blocks.
      The COPY writes one value per configured column into a table with
      fewer, and fails on the column count — later, and far less legibly.
    * A type the table cannot widen into safely (`unsafe_type_changes`)
      blocks for the same reason: the load cannot put the value there.
    * A safe widening still pending (`alter_columns`) and a physical column
      the configuration has forgotten (`drop_columns`) only warn. The COPY
      names its columns, so an orphan is inert, and a narrower-but-castable
      type still accepts the rows.

    `create_dims`/`drop_dims` are deliberately *not* reported: the
    schema-objects check already creates a missing dim table, and two checks
    disagreeing about the same table would be worse than one.
    """
    from .schema import sync_schema

    try:
        diff = sync_schema(source, dest_engine, drop_columns=None, quiet=True)
    except Exception as exc:
        return _check("warehouse_drift", CHECK_WARN,
                      f"schema comparison failed: {exc}", error=str(exc))

    add = [c["name"] for c in diff.get("add_columns") or []]
    unsafe = [f"{c['name']} {c['from_type']}→{c['to_type']}"
              for c in diff.get("unsafe_type_changes") or []]
    alter = [f"{c['name']} {c['from_type']}→{c['to_type']}"
             for c in diff.get("alter_columns") or []]
    orphan = list(diff.get("drop_columns") or [])
    detail = {"add_columns": add, "unsafe_type_changes": unsafe,
              "alter_columns": alter, "drop_columns": orphan}

    if add or unsafe:
        parts = []
        if add:
            parts.append(f"missing column(s): {', '.join(add)}")
        if unsafe:
            parts.append(f"incompatible type(s): {', '.join(unsafe)}")
        return _check("warehouse_drift", CHECK_BLOCK,
                      f"{source.schema_name}.fact does not match the "
                      f"configuration — {'; '.join(parts)}. Run Sync Schema "
                      f"(it rebuilds the indices, so not from here)",
                      **detail)
    if alter or orphan:
        parts = []
        if alter:
            parts.append(f"widenable type(s): {', '.join(alter)}")
        if orphan:
            parts.append(f"column(s) no longer configured: "
                         f"{', '.join(orphan)}")
        return _check("warehouse_drift", CHECK_WARN,
                      f"{source.schema_name}.fact has drifted, harmlessly so "
                      f"far — {'; '.join(parts)}", **detail)
    return _check("warehouse_drift", CHECK_OK,
                  f"{source.schema_name}.fact matches the configuration")


def _check_source_projection(source: Source, source_engine) -> dict:
    """U5b — the upstream columns behind source_expr / base_table still exist.

    One `WHERE 1=0` round trip turns a rename that would otherwise fail in
    the middle of the extract loop — after the run row is written and the
    workers are up — into a refusal with a column name in it.
    """
    if source_engine is None:
        # Either the caller did not want a source round trip, or it could
        # not even build the engine (a missing driver, an unparseable DSN).
        # Both are "not checked", and neither is this check's finding.
        return _check("source_projection", CHECK_OK,
                      "skipped — no source connection available")
    probe = f"SELECT * FROM ({source.import_query}) q WHERE 1=0"
    try:
        with source_engine.begin() as src:
            # Two different failures live here and they need opposite
            # answers. A source that cannot be reached at all is the
            # extract's own problem and it reports it better than we can, so
            # that only warns. A query the source *rejects* is the drift this
            # check exists for — a renamed or dropped column raises exactly
            # like a connection error, and warning about it would let the run
            # proceed and die mid-extract, which is what we are here to stop.
            # Opening the connection separates the two.
            try:
                got = [str(c) for c in src.execute(text(probe)).keys()]
            except Exception as exc:
                return _check("source_projection", CHECK_BLOCK,
                              f"the source rejected the import query — "
                              f"check base_table / source_expr against the "
                              f"source schema: {exc}",
                              error=str(exc))
    except Exception as exc:
        return _check("source_projection", CHECK_WARN,
                      f"source not reachable for the projection probe: {exc}",
                      error=str(exc))

    have = {c.lower() for c in got}
    want = [c.name for c in source.fact_columns]
    missing = [c for c in want if c.lower() not in have]
    extra = sorted(have - {c.lower() for c in want})
    if missing:
        return _check("source_projection", CHECK_BLOCK,
                      f"source no longer projects: {', '.join(missing)} "
                      f"— check base_table / source_expr against the source "
                      f"schema",
                      missing=missing, extra=extra)
    return _check("source_projection", CHECK_OK,
                  f"source projects all {len(want)} fact column(s)",
                  extra=extra)


def _check_watermark_tz(source: Source, source_engine) -> dict:
    """Is the watermark column naive, the shape the load window binds against?

    The window bounds go down as **naive UTC** (`core/etl.py:_as_naive_utc`),
    because source timestamps are naive and hold UTC throughout this codebase
    (`Source.source_now_sql`). Against a timezone-*aware* watermark column
    that predicate is resolved by converting one side with the session clock,
    so the whole window slides by the session offset — silently. Measured on
    Postgres, session `Europe/Berlin`, `timestamptz` watermark: an
    incremental run with nothing to do inserted 16665 duplicate rows.

    `Source.source_engine` now pins every source session to UTC, which makes
    a naive bind match a naive and an aware column alike — so on a dialect
    that *can* be pinned this check has nothing left to refuse, and says so
    rather than staying silent: "aware, but safe" is worth seeing in the
    report. mssql has no session time zone at all (`AT TIME ZONE` is
    per-expression), so there the configuration is refused outright.

    The finding comes from the **value**, not from a type code:
    `datetime.tzinfo` is what the driver decided, which is dialect-free and
    needs no psycopg2 oid table.

    One indexed `max()` — literally the query the engine already runs as its
    default `watermark_max_query`. Deliberately *not* wrapped around
    `import_query`: a full `max()` over that subquery is the slow shape
    `core/etl.py:_source_min_watermark` already warns about, and no probe is
    worth a table scan before every load.
    """
    if source_engine is None:
        return _check("watermark_tz", CHECK_OK,
                      "skipped — no source connection available")

    wm = source.watermark_column
    if source.watermark_max_query:
        probe = source.watermark_max_query
    elif source.base_table:
        probe = f"SELECT max({wm}) FROM {source.base_table}"
    else:
        # import_query only. See the docstring: the probe is not worth what
        # it would cost here, and the engine reads the same value moments
        # later anyway.
        return _check("watermark_tz", CHECK_OK,
                      "skipped — needs base_table or watermark_max_query to "
                      "probe the watermark cheaply")

    try:
        with source_engine.begin() as src:
            value = src.execute(text(probe)).scalar()
    except Exception as exc:
        # Warn, never block. A query the source *rejects* is drift, and
        # _check_source_projection is what blocks on that; reaching the same
        # verdict from here would double the refusal for one cause.
        return _check("watermark_tz", CHECK_WARN,
                      f"could not read max({wm}) to check its time zone: "
                      f"{exc}", error=str(exc))

    if value is None:
        return _check("watermark_tz", CHECK_OK,
                      f"not checked — the source has no rows yet, so "
                      f"max({wm}) says nothing about its type")
    if getattr(value, "tzinfo", None) is None:
        return _check("watermark_tz", CHECK_OK,
                      f"{wm} reads as a naive timestamp — the shape the load "
                      f"window binds against", watermark=wm, aware=False)

    if source.source_dialect in _SESSION_UTC_SQL:
        return _check("watermark_tz", CHECK_OK,
                      f"{wm} is timezone-aware, and the {source.source_dialect} "
                      f"source session is pinned to UTC — the window bounds "
                      f"compare exactly", watermark=wm, aware=True,
                      session_pinned=True)

    return _check("watermark_tz", CHECK_BLOCK,
                  f"{wm} is timezone-aware and the {source.source_dialect} "
                  f"dialect has no session time zone to pin, so the load "
                  f"window would be compared on an unknown clock and shift "
                  f"rows in or out of it silently. Project the watermark as "
                  f"a naive UTC value — through import_query or a view — and "
                  f"point watermark_column at that.",
                  watermark=wm, aware=True, session_pinned=False)


def _check_watermark_tip(source: Source, dest_engine, meta_engine) -> dict:
    """U1 — the stored high watermark against the fact tip.

    A fact tip materially *below* the watermark means rows disappeared
    without the watermark following: a manual DELETE, a restore, a partition
    dropped outside the GUI. Two reasons this can only ever warn:

    * A quiet source widens the same gap legitimately — the window's upper
      bound is where the source was *read to*, not where rows arrived.
    * With `watermark_fact_expr` set, the tip (`partition_column`) and the
      watermark are two different clocks on the same rows. `partition_column`
      is used anyway: it is BRIN-indexed and partition-pruned, while
      `max(watermark_fact_expr)` is the expensive scan docs/etl-audit.md
      flags — and the delete gesture this looks for runs on
      `partition_column` too.
    """
    schema = source.schema_name
    with meta_engine.begin() as conn:
        hwm = conn.execute(text(
            "SELECT last_end_time FROM starcubes_meta.etl_run "
            " WHERE datasource_id = :ds AND status = 'ok' "
            "   AND kind = 'incremental' "
            " ORDER BY finished_at DESC LIMIT 1"
        ), {"ds": source.id}).scalar()
    if hwm is None:
        return _check("watermark_tip", CHECK_OK,
                      "skipped — no high watermark stored yet")

    with dest_engine.connect() as conn:
        if not relation_exists(conn, schema, "fact"):
            return _check("watermark_tip", CHECK_OK,
                          "skipped — fact table does not exist yet")
        tip = conn.execute(text(
            f"SELECT max({source.partition_column}) FROM {schema}.fact"
        )).scalar()
    if tip is None:
        return _check("watermark_tip", CHECK_OK,
                      "skipped — fact table is empty")

    if hwm.tzinfo is None:
        hwm = hwm.replace(tzinfo=timezone.utc)
    if tip.tzinfo is None:
        tip = tip.replace(tzinfo=timezone.utc)
    gap_s = (hwm - tip).total_seconds()
    detail = {"highwatermark": hwm.isoformat(), "fact_tip": tip.isoformat(),
              "gap_s": round(gap_s, 1),
              "partition_column": source.partition_column}
    if gap_s > _HWM_GAP_WARN_S:
        return _check("watermark_tip", CHECK_WARN,
                      f"the high watermark stands {_fmt_s(gap_s)} above "
                      f"the newest fact row — either the source stopped "
                      f"producing, or rows were removed without rewinding "
                      f"the watermark",
                      **detail)
    if gap_s < 0:
        # Normal whenever the watermark expression trails the partition
        # column, which is the whole reason watermark_fact_expr exists.
        return _check("watermark_tip", CHECK_OK,
                      f"high watermark is {_fmt_s(-gap_s)} below the fact "
                      f"tip", **detail)
    return _check("watermark_tip", CHECK_OK,
                  f"high watermark is {_fmt_s(gap_s)} above the fact tip",
                  **detail)


def _check_data_staleness(source: Source, meta_engine) -> dict:
    """U9 — the source is up and has stopped producing.

    The failure most likely to go unnoticed for a whole day, and the one
    nothing else can see: a standstill freezes the high watermark, so the
    alarm evaluator's trailing window stops sliding and `count(*) < 1` never
    becomes true. The ETL logs `rows=0` and sleeps.

    So this watches the *pipeline* instead of the data, using the two numbers
    every run already reads together on one query: the data tip `max_wm` and
    the source's own clock `src_now`. Both come from the source, so their
    difference carries no clock skew — the discipline docs/etl-audit.md
    insists on for `needed_lag_s`.

    Reported unconditionally, because the measurement is worth seeing even
    unconfigured: a lag parked near a whole-hour offset while rows keep
    arriving means the watermark column does not hold UTC (N12). It only
    escalates to a warning once `max_data_lag_s` is set, which is the honest
    default for a source that does not produce continuously — the
    alternative, time-of-day profiles, is a calendar engine in disguise.
    """
    with meta_engine.begin() as conn:
        threshold = int(conn.execute(text(
            "SELECT max_data_lag_s FROM starcubes_meta.datasource "
            " WHERE id = :ds"), {"ds": source.id}).scalar() or 0)
        row = conn.execute(text(
            "SELECT max_wm, src_now, started_at FROM starcubes_meta.etl_run "
            " WHERE datasource_id = :ds "
            "   AND max_wm IS NOT NULL AND src_now IS NOT NULL "
            " ORDER BY started_at DESC LIMIT 1"
        ), {"ds": source.id}).mappings().first()

    if row is None:
        return _check("data_staleness", CHECK_OK,
                      "skipped — no run has recorded the source clock yet")
    max_wm, src_now = row["max_wm"], row["src_now"]
    if max_wm.tzinfo is None:
        max_wm = max_wm.replace(tzinfo=timezone.utc)
    if src_now.tzinfo is None:
        src_now = src_now.replace(tzinfo=timezone.utc)
    lag_s = (src_now - max_wm).total_seconds()
    detail = {"max_wm": max_wm.isoformat(), "src_now": src_now.isoformat(),
              "measured_at": row["started_at"].isoformat(),
              "data_lag_s": round(lag_s, 1), "max_data_lag_s": threshold}

    if lag_s < 0:
        # The source's clock is *behind* its own newest row. `import_fact`
        # already refuses to apply the wall-clock bound when it sees this;
        # the preflight says the same thing in the report, because the cause
        # is almost always a timezone mismatch — the watermark column not
        # holding UTC, which the rest of the pipeline assumes it does — and
        # that is a configuration problem nobody will find by reading logs.
        return _check("data_staleness", CHECK_WARN,
                      f"the source's own clock is {_fmt_s(-lag_s)} *behind* its "
                      f"newest row, which cannot be true of one clock — "
                      f"almost always {source.watermark_column} not holding "
                      f"UTC values. The wall-clock window bound is not "
                      f"applied while this holds, and max_data_lag_s cannot "
                      f"be judged.",
                      **detail)

    if threshold and lag_s > threshold:
        return _check("data_staleness", CHECK_WARN,
                      f"the newest row in the source is {_fmt_s(lag_s)} "
                      f"behind the source's own clock, past the "
                      f"{_fmt_s(threshold)} max_data_lag_s — the source is "
                      f"reachable and has stopped producing",
                      **detail)
    if threshold:
        return _check("data_staleness", CHECK_OK,
                      f"source data is {_fmt_s(lag_s)} behind the source "
                      f"clock (limit {_fmt_s(threshold)})", **detail)
    return _check("data_staleness", CHECK_OK,
                  f"source data is {_fmt_s(lag_s)} behind the source "
                  f"clock — set max_data_lag_s to have that watched",
                  **detail)


def _check_alarm_windows(source: Source, meta_engine) -> dict:
    """Alarm windows that reach below the retention floor.

    Such an alarm is not evaluated (see `core/alarm.py:_evaluate_window`),
    which is the right answer and a silent one. Reported here so the silence
    is explained: a rule that has quietly stopped answering looks exactly
    like a rule that is content.
    """
    floor = getattr(source, "retention_floor", None)
    if floor is None:
        return _check("alarm_windows", CHECK_OK,
                      "skipped — nothing has been retired")
    if floor.tzinfo is None:
        floor = floor.replace(tzinfo=timezone.utc)

    with meta_engine.begin() as conn:
        hwm = conn.execute(text(
            "SELECT last_end_time FROM starcubes_meta.etl_run "
            " WHERE datasource_id = :ds AND status = 'ok' "
            "   AND kind = 'incremental' "
            " ORDER BY finished_at DESC LIMIT 1"
        ), {"ds": source.id}).scalar()
        rows = conn.execute(text(
            "SELECT name, window_s FROM starcubes_meta.alarm "
            " WHERE datasource_id = :ds AND enabled "
            "   AND rule_kind = 'threshold' ORDER BY name"
        ), {"ds": source.id}).mappings().all()
    if hwm is None or not rows:
        return _check("alarm_windows", CHECK_OK,
                      "skipped — no watermark or no threshold alarms")
    if hwm.tzinfo is None:
        hwm = hwm.replace(tzinfo=timezone.utc)

    room = (hwm - floor).total_seconds()
    over = [r["name"] for r in rows if r["window_s"] > room]
    detail = {"retention_floor": floor.isoformat(),
              "room_s": round(room, 1), "alarms": over}
    if over:
        return _check("alarm_windows", CHECK_WARN,
                      f"{len(over)} alarm window(s) reach below the "
                      f"retention floor and are therefore not evaluated: "
                      f"{', '.join(over)}. Only {_fmt_s(room)} of data is "
                      f"kept — shorten window_s or raise retention_periods",
                      **detail)
    return _check("alarm_windows", CHECK_OK,
                  f"all {len(rows)} alarm window(s) fit inside the "
                  f"{_fmt_s(room)} of retained data", **detail)


def run_preflight(source: Source, meta_engine, dest_engine, *,
                  source_engine=None, repair: bool = True) -> dict:
    """Run every pre-load check, cheapest first, and return the report.

    ``repair=True`` lets the schema-object check call the idempotent
    creators for anything missing; ``repair=False`` (the GUI's dry run)
    reports the same finding as a block and changes nothing.

    Never raises for a *finding* — a probe that itself explodes is caught
    and reported as a warning. The caller decides what ``blocked`` means.
    """
    checks: list[dict] = []
    repairs: list[str] = []

    check, done = _check_schema_objects(source, dest_engine, repair=repair)
    checks.append(check)
    repairs.extend(done)
    checks.append(_check_meta_migrations(meta_engine))
    checks.append(_check_indices(source, dest_engine, meta_engine))
    checks.append(_check_warehouse_drift(source, dest_engine))
    checks.append(_check_source_projection(source, source_engine))
    checks.append(_check_watermark_tz(source, source_engine))
    checks.append(_check_watermark_tip(source, dest_engine, meta_engine))
    checks.append(_check_data_staleness(source, meta_engine))
    checks.append(_check_alarm_windows(source, meta_engine))

    blocked = [c for c in checks if c["level"] == CHECK_BLOCK]
    for c in checks:
        if c["level"] == CHECK_WARN:
            logger.warning("%s: preflight %s: %s",
                           source.name, c["code"], c["message"])
        elif c["level"] == CHECK_BLOCK:
            logger.error("%s: preflight %s: %s",
                         source.name, c["code"], c["message"])
    return {"checks": checks,
            "repairs": repairs,
            "blocked": bool(blocked),
            "ran_at": datetime.now(timezone.utc).isoformat()}


def blocking_message(report: dict) -> Optional[str]:
    """One sentence naming every blocking finding, or None."""
    blocked = [c for c in report.get("checks", [])
               if c["level"] == CHECK_BLOCK]
    if not blocked:
        return None
    return "preflight blocked the run: " + "; ".join(
        f"{c['code']}: {c['message']}" for c in blocked)


# ---------------------------------------------------------------------------
# Single occupancy per datasource
# ---------------------------------------------------------------------------

# Advisory-lock class id, shared by every starcubes process. Any int32 does;
# this one spells "STAR" so it is recognisable in pg_locks.
_LOCK_CLASS = 0x53544152


class DatasourceLocked(RuntimeError):
    """Raised when somebody else already holds this datasource.

    Forever mode has been protected by `ux_etl_forever_active` and the GUI
    by its 409 all along, but a plain `manage.py etl_run --name x` from a
    shell had no interlock whatsoever and would happily run alongside a live
    forever session. That is not theoretical: auto-dimension resolution
    states single occupancy as a precondition (two parents inserting into
    one dim table race on the smallserial), and with no unique index on
    `fact` two overlapping windows silently duplicate rows.
    """


class _LockHandle:
    def __init__(self, engine, conn, datasource_id: int):
        self.engine = engine
        self.conn = conn
        self.datasource_id = datasource_id


def acquire_datasource_lock(dsn: str, datasource_id: int, *,
                            holder: str = "etl") -> _LockHandle:
    """Take the datasource's advisory lock, or raise `DatasourceLocked`.

    A Postgres advisory lock is cross-process, cross-host and released the
    moment the connection dies — no PID file, no stale state to reap.

    **It is session-scoped, and that dictates the connection.** Nothing in
    the ETL holds a connection across a run: every DB touch is a short
    `engine.begin()` block, and the extract loop even disposes the source
    pool on purpose. Taking the lock on a pooled connection would be
    silently worthless — `_install_fork_safety` invalidates records across
    pids, `pool_recycle` drops the session after 30 minutes, and a
    `pool_pre_ping` reconnect would carry on without the lock and without a
    word. So this opens its own NullPool connection and keeps exactly that
    one.

    The acquiring transaction is committed straight away: a session lock
    survives the commit, whereas leaving the transaction open would pin the
    snapshot for the whole run.
    """
    engine = sqlalchemy.create_engine(dsn, poolclass=NullPool)
    conn = engine.connect()
    try:
        got = conn.execute(text("SELECT pg_try_advisory_lock(:c, :d)"),
                           {"c": _LOCK_CLASS,
                            "d": int(datasource_id)}).scalar()
        conn.commit()
    except Exception:
        conn.close()
        engine.dispose()
        raise
    if not got:
        conn.close()
        engine.dispose()
        raise DatasourceLocked(
            f"datasource {datasource_id} is already held by another "
            f"starcubes process — refusing to run a second {holder} on it. "
            f"Stop the running ETL first.")
    return _LockHandle(engine, conn, int(datasource_id))


def release_datasource_lock(handle: Optional[_LockHandle]) -> None:
    """Give the lock back. Closing the connection would do it too; the
    explicit unlock keeps pg_locks tidy while the process is still alive."""
    if handle is None:
        return
    try:
        handle.conn.execute(text("SELECT pg_advisory_unlock(:c, :d)"),
                            {"c": _LOCK_CLASS, "d": handle.datasource_id})
        handle.conn.commit()
    except Exception:
        logger.debug("advisory unlock failed; closing the connection "
                     "releases it anyway", exc_info=True)
    finally:
        try:
            handle.conn.close()
        finally:
            handle.engine.dispose()


@contextmanager
def datasource_lock(dsn: str, datasource_id: int, *, holder: str = "etl"):
    """Scoped form of acquire/release, for the short maintenance gestures."""
    handle = acquire_datasource_lock(dsn, datasource_id, holder=holder)
    try:
        yield handle
    finally:
        release_datasource_lock(handle)
