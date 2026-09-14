"""Periodic maintenance that does not belong inside a load.

The forever session is the only periodic engine in this system — there is no
cron, no systemd timer, no Celery — so anything that must happen regularly
either happens inside a load or does not happen at all. Most of it belongs
inside the load and does (partition creation, dimension import, preflight).
What is collected here is the rest: work worth doing every few hours and
wasteful to pay for per iteration.

Three concerns, from `docs/etl-automation.md`:

* **U6 — statistics and BRIN ranges.** The load is COPY-only: rows are
  inserted, never updated, never deleted. Dead tuples barely accumulate, so
  autovacuum's scale factors are effectively never reached on a partition
  holding hundreds of millions of rows. Two things rot as a result, neither
  of which raises anything. Planner statistics go stale on exactly the
  partition being written — the one every chart query touches. And BRIN
  ranges are never summarised: a BRIN index only covers page ranges that
  have been summarised, and summarisation happens during VACUUM or an
  explicit `brin_summarize_new_values()`, so freshly COPY'd blocks are not
  covered by the index at all. The index the whole storage design rests on
  silently stops applying to the newest data.
* **N13 — a dimension filling up.** Dim tables are keyed `smallserial`:
  32767 values, hard. An auto-dimension pointed at something
  high-cardinality climbs towards that ceiling and then fails inside
  `_resolve_auto_dim_chunk`, in the parent, mid-extract.
* **N14 — disk growth.** Fact partitions are UNLOGGED and unbounded. One
  measurement is already on the Storage tab; a series of them is what turns
  "the disk filled up overnight" into something visible in advance, and what
  would let a retention policy be sized rather than guessed.

* **U3b — retention.** `datasource.retention_periods` disposes of
  partitions that have aged out. The only part of this module that destroys
  anything, and therefore the only part that takes the datasource lock.

Deliberately *not* here: `VACUUM`. Full `VACUUM ANALYZE fact` stays a manual
action for after a large delete or backfill — the targeted ANALYZE plus BRIN
summarisation below addresses the actual problem for a fraction of the I/O.

**The lock is taken for retention and for nothing else.** ANALYZE takes
SHARE UPDATE EXCLUSIVE, which does not conflict with COPY;
`brin_summarize_new_values` takes a row-level lock; the rest is catalogue
reads and `count(*)` over tables of at most 32767 rows. Locking those would
refuse a hand-run housekeeping during forever mode for no reason. Dropping a
partition is the one thing here that is not safe alongside a load, so
`_run_retention` takes the datasource lock non-blocking and skips itself when
a load holds it — the rest of the run continues either way.

Django-free, like everything in `core/`.
"""
from __future__ import annotations

import json as _json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text

from .cache import clear_cache
from .source import Source

logger = logging.getLogger(__name__)

# smallserial. The ceiling is on the *sequence*, not the row count — deleting
# a dim row does not give the value back — so fill is measured as
# max(index), which is what will actually collide.
DIM_INDEX_CEILING = 32767
_DIM_WARN_PCT = float(os.environ.get("STARCUBES_DIM_FULL_WARN_PCT", 80))

# How many partitions retention may remove in one housekeeping run.
#
# Not a performance limit — a blast radius. Fact partitions are UNLOGGED and
# nothing backs them up, so the difference between `retention_periods = 12`
# and a mistyped `1` is the difference between a policy and an accident. With
# a step limit the mistake walks forward a couple of partitions per interval,
# visibly, through the log and through housekeeping_run.report, and somebody
# can still stop it. Without one it is a single irreversible pass.
_MAX_RETENTION_DROPS = int(os.environ.get("STARCUBES_MAX_RETENTION_DROPS", 2))


def _analyze_targets(partitions: list[dict]) -> list[str]:
    """The partitions worth ANALYZEing: the newest two that hold data.

    Two rules this deliberately does *not* follow.

    Not "the newest partition": `ensure_partitions_for` keeps a period of
    runway ahead of the window (`forward=1`), so the newest partition is
    normally a future, empty one and analyzing it achieves nothing.

    Not "the partition covering now" either, which was the first attempt.
    That assumes the load is current, and the first real datasource it ran
    against had every one of its 3.9M rows in a partition four months back —
    the source had simply stopped producing. The calendar said to analyze two
    empty partitions and leave the only populated one untouched.

    Sizes are measured for N14 regardless, so choosing from them costs
    nothing and does not care what month it is. The second-newest is included
    because late arrivals and the window's `back=1` reach still write there.

    On `heap_bytes`, not the total: once `create_indices` has run, every
    partition carries index pages, so the *total* size is non-zero even for
    an empty future partition — which put the ANALYZE two weeks into the
    future the first time this was tried.
    """
    non_empty = [p["name"] for p in partitions if p["heap_bytes"] > 0]
    if non_empty:
        return non_empty[-2:]
    # Nothing loaded yet: analyzing an empty table is instant, and doing it
    # gives the planner a baseline before the first load rather than after.
    return [partitions[-1]["name"]] if partitions else []


def _brin_indices(source: Source, conn) -> list[str]:
    """Concrete BRIN indexes on the fact partitions.

    `CREATE INDEX ... USING brin` on a partitioned table creates a
    *partitioned* index (relkind 'I') plus one real index per partition
    (relkind 'i'). `brin_summarize_new_values` needs the real ones, so the
    parent is excluded here rather than filtered out later and forgotten.
    """
    return list(conn.execute(text(
        "SELECT ci.relname "
        "  FROM pg_inherits inh "
        "  JOIN pg_class parent ON parent.oid = inh.inhparent "
        "  JOIN pg_class part   ON part.oid   = inh.inhrelid "
        "  JOIN pg_namespace n  ON n.oid      = parent.relnamespace "
        "  JOIN pg_index x      ON x.indrelid = part.oid "
        "  JOIN pg_class ci     ON ci.oid     = x.indexrelid "
        "  JOIN pg_am am        ON am.oid     = ci.relam "
        " WHERE n.nspname = :s AND parent.relname = 'fact' "
        "   AND am.amname = 'brin' AND ci.relkind = 'i' "
        " ORDER BY ci.relname"
    ), {"s": source.schema_name}).scalars().all())


def _dimension_fill(source: Source, conn) -> tuple[list[dict], list[dict]]:
    """Fill level per dim table, and a warning for each one running out."""
    rows: list[dict] = []
    warnings: list[dict] = []
    schema = source.schema_name
    for d in source.primary_dimensions():
        if not d.dim_table:
            continue
        try:
            r = conn.execute(text(
                f"SELECT count(*) AS n, coalesce(max(index), 0) AS hi "
                f"  FROM {schema}.{d.dim_table}"
            )).mappings().first()
        except Exception as exc:
            warnings.append({"code": "dim_unreadable",
                             "message": f"{d.dim_table}: {exc}"})
            continue
        hi = int(r["hi"])
        pct = 100.0 * hi / DIM_INDEX_CEILING
        rows.append({"table": d.dim_table, "rows": int(r["n"]),
                     "max_index": hi, "pct_full": round(pct, 1)})
        if pct >= _DIM_WARN_PCT:
            warnings.append({
                "code": "dim_filling_up",
                "message": (
                    f"dimension {d.dim_table} is {pct:.0f}% of the "
                    f"smallserial ceiling ({hi}/{DIM_INDEX_CEILING}). At 100% "
                    f"the next new value fails mid-extract, in the parent "
                    f"process. If this is an auto-dimension, check it is not "
                    f"pointed at an id column."),
            })
    return rows, warnings


def _partition_sizes(source: Source, conn) -> list[dict]:
    # Both numbers are needed and they answer different questions.
    # `bytes` is the disk N14 watches, indexes included. `heap_bytes` is the
    # only honest "does this partition hold rows" signal: an index on an
    # empty partition still occupies pages, so the total is non-zero for
    # every partition the moment create_indices has run.
    rows = conn.execute(text(
        "SELECT c.relname AS name, "
        "       pg_total_relation_size(c.oid) AS bytes, "
        "       pg_relation_size(c.oid)       AS heap_bytes "
        "  FROM pg_inherits i "
        "  JOIN pg_class parent ON parent.oid = i.inhparent "
        "  JOIN pg_class c      ON c.oid      = i.inhrelid "
        "  JOIN pg_namespace n  ON n.oid      = parent.relnamespace "
        " WHERE n.nspname = :s AND parent.relname = 'fact' "
        " ORDER BY c.relname"
    ), {"s": source.schema_name}).mappings().all()
    return [{"name": r["name"], "bytes": int(r["bytes"]),
             "heap_bytes": int(r["heap_bytes"])} for r in rows]


def _partition_correlation(source: Source, conn) -> dict[str, float]:
    """Per-partition `pg_stats.correlation` for the partition column.

    The number BRIN lives or dies by. A BRIN index stores min/max per page
    range, so it only excludes a range when the column's physical order
    tracks its value order; `docs/bitmap-indexes.md` measured the gap at
    **141x** between clustered and scattered data. Correlation near 1.0 is
    the append-only ideal this storage design assumes; a falling number is
    the index quietly ceasing to help, and nothing else in the system says
    so.

    What lowers it is a **backfill**: it appends old timestamps to the end of
    the partition's heap, so the trailing page ranges end up spanning a wide
    min/max and become false positives for every query over the backfilled
    span. The damage is bounded by the partition and proportional to the rows
    inserted -- 37 rows are one page range, millions turn the tail of the
    partition into a mixed region.

    Worth stating because it is the obvious wrong answer: **REINDEX does not
    repair this.** BRIN recomputes min/max from the same, unchanged heap and
    arrives at the same ranges. Only physically rewriting the partition helps
    (CLUSTER over a temporary B-tree -- BRIN cannot serve as a CLUSTER index
    -- pg_repack, or rewriting it in sorted order).
    `brin_summarize_new_values` above is a different concern entirely: it
    makes new blocks indexed *at all*, and never makes a range narrower.

    Read after the ANALYZE, because pg_stats is what ANALYZE writes. Silent
    about partitions that have never been analysed -- an absent number is
    honest, a stale one is not.
    """
    rows = conn.execute(text(
        "SELECT s.tablename AS name, s.correlation "
        "  FROM pg_stats s "
        "  JOIN pg_class c     ON c.relname = s.tablename "
        "  JOIN pg_namespace n ON n.oid = c.relnamespace "
        "                     AND n.nspname = s.schemaname "
        "  JOIN pg_inherits i  ON i.inhrelid = c.oid "
        "  JOIN pg_class p     ON p.oid = i.inhparent "
        " WHERE s.schemaname = :s AND p.relname = 'fact' "
        "   AND s.attname = :col AND s.correlation IS NOT NULL"
    ), {"s": source.schema_name, "col": source.partition_column}).mappings().all()
    return {r["name"]: float(r["correlation"]) for r in rows}


def _retention_cutoff(source: Source, keep_periods: int) -> datetime:
    """The instant below which partitions may be disposed of.

    `keep_periods` whole periods back from the start of the current one, so
    "keep 12 months" means the twelve completed months plus the one being
    written, never eleven-and-a-bit.
    """
    from .schema import _floor_to_period, _prev_period

    period = source.partition_period
    cur = _floor_to_period(datetime.now(), period)
    for _ in range(keep_periods):
        cur = _prev_period(cur, period)
    return cur.astimezone()


def _run_retention(source: Source, meta_engine, dest_engine,
                   report: dict, *, quiet: bool = False) -> None:
    """Drop partitions that have aged out of `datasource.retention_periods`.

    The one part of the housekeeping that destroys data, and therefore the
    one part that must not run alongside a load. It does **not** take the
    datasource lock itself: `drop_partition` already does, per partition, and
    `datasource_lock` opens its own connection each time — so a lock held
    here would refuse the very drops it is protecting. The first version did
    exactly that and skipped every run. Catching `DatasourceLocked` from the
    drop is both simpler and the right granularity: whatever was already
    disposed of stays disposed of, and the rest waits for the next interval.

    Everything else is deliberately delegated to `drop_partition`, which
    already refuses a partition that is not the oldest, raises
    `retention_floor` only when it is, and bumps the cache epoch. Retention
    adds one thing on top: it *clears* the plot cache rather than only
    invalidating it, because the epoch bump makes old rows unreachable
    without deleting them and a periodic policy would grow `query_cache`
    without bound.
    """
    from .etl import drop_partition
    from .preflight import DatasourceLocked
    from .schema import _parse_partition_name, list_fact_partitions

    with meta_engine.begin() as meta:
        keep = int(meta.execute(text(
            "SELECT retention_periods FROM starcubes_meta.datasource "
            " WHERE id = :ds"), {"ds": source.id}).scalar() or 0)
    if keep <= 0:
        return

    parts = list_fact_partitions(source, dest_engine)
    cutoff = _retention_cutoff(source, keep)
    period = source.partition_period
    doomed = [p for p in parts
              if _parse_partition_name(period, p).astimezone() < cutoff]

    # Two guards that have nothing to do with the policy and everything to do
    # with not destroying a datasource by arithmetic: never take more than
    # half of what exists, and never take the partition the newest data is
    # in. A `retention_periods` shorter than the data's own span would
    # otherwise be indistinguishable from "drop the table".
    if len(doomed) > len(parts) // 2:
        doomed = doomed[:len(parts) // 2]
    doomed = [p for p in doomed if p != parts[-1]][:_MAX_RETENTION_DROPS]
    if not doomed:
        return

    dropped: list[str] = []
    try:
        for name in doomed:
            logger.warning(
                "%s: retention drops partition %s (keeping %d %s(s), "
                "cutoff %s)", source.name, name, keep, period, cutoff)
            drop_partition(source, dest_engine, name,
                           meta_engine=meta_engine, quiet=quiet)
            dropped.append(name)
    except DatasourceLocked:
        report["warnings"].append(
            {"code": "retention_skipped",
             "message": "a load holds the datasource — retention will try "
                        "again on the next housekeeping run"})
    finally:
        if dropped:
            # The epoch bump inside drop_partition already makes the stale
            # keys unreachable; this is what stops them accumulating.
            clear_cache(meta_engine, source.id)

    report["retention"] = {
        "keep_periods": keep, "cutoff": cutoff.isoformat(),
        "dropped": dropped, "eligible": len(doomed),
        "step_limit": _MAX_RETENTION_DROPS,
    }


def run_housekeeping(source: Source, meta_engine, dest_engine, *,
                     analyze: bool = True, trigger: str = "manual",
                     quiet: bool = False) -> dict:
    """Do the periodic maintenance and record what it found.

    Returns the report and writes it to `starcubes_meta.housekeeping_run`.
    Individual steps are guarded: a dimension that cannot be read must not
    cost the partition measurements, and neither must cost the ANALYZE.
    """
    t0 = time.monotonic()
    report: dict = {"analyzed": [], "brin": [], "dimensions": [],
                    "partitions": [], "warnings": []}
    schema = source.schema_name

    # Retention runs before the measurements, or they would describe
    # partitions that no longer exist by the time the report is written.
    try:
        _run_retention(source, meta_engine, dest_engine, report, quiet=quiet)
    except Exception as exc:
        logger.exception("%s: retention failed", source.name)
        report["warnings"].append(
            {"code": "retention_failed", "message": str(exc)})

    # Measure first: the partition sizes are N14's whole point, and they are
    # also what decides where the ANALYZE goes (see _analyze_targets).
    with dest_engine.begin() as conn:
        dims, dim_warnings = _dimension_fill(source, conn)
        report["dimensions"] = dims
        report["warnings"].extend(dim_warnings)
        report["partitions"] = _partition_sizes(source, conn)
    report["total_bytes"] = sum(p["bytes"] for p in report["partitions"])

    if not report["partitions"]:
        report["warnings"].append(
            {"code": "no_partitions",
             "message": f"{schema}.fact has no partitions — nothing to do"})

    # ANALYZE and brin_summarize_new_values both run outside a transaction:
    # ANALYZE would otherwise hold its snapshot for the whole command, and
    # there is nothing here to roll back as a unit.
    if report["partitions"]:
        with dest_engine.connect().execution_options(
                isolation_level="AUTOCOMMIT") as conn:
            if analyze:
                for name in _analyze_targets(report["partitions"]):
                    t = time.monotonic()
                    try:
                        conn.execute(text(f"ANALYZE {schema}.{name}"))
                    except Exception as exc:
                        report["warnings"].append(
                            {"code": "analyze_failed",
                             "message": f"{name}: {exc}"})
                        continue
                    report["analyzed"].append(
                        {"partition": name,
                         "ms": int((time.monotonic() - t) * 1000)})

            brin = _brin_indices(source, conn)
            report["brin_indices_checked"] = len(brin)
            for ix in brin:
                t = time.monotonic()
                try:
                    n = conn.execute(text(
                        "SELECT brin_summarize_new_values("
                        "  CAST(:ix AS regclass))"
                    ), {"ix": f"{schema}.{ix}"}).scalar()
                except Exception as exc:
                    report["warnings"].append(
                        {"code": "brin_failed", "message": f"{ix}: {exc}"})
                    continue
                # Only the indexes that had work to do are worth storing;
                # an already-summarised index costs a range-map scan and
                # reports 0, every time, for every partition ever created.
                if n:
                    report["brin"].append(
                        {"index": ix, "ranges": int(n),
                         "ms": int((time.monotonic() - t) * 1000)})

            # After the ANALYZE on purpose: pg_stats is what ANALYZE writes,
            # and the sizes above were measured before it because they decide
            # where the ANALYZE goes. Reading correlation there would report
            # the previous run's number.
            try:
                corr = _partition_correlation(source, conn)
            except Exception as exc:
                report["warnings"].append(
                    {"code": "correlation_failed", "message": str(exc)})
            else:
                for part in report["partitions"]:
                    if part["name"] in corr:
                        part["correlation"] = round(corr[part["name"]], 4)

    # Pre-aggregation repair. The ETL hook is what normally advances the
    # rollups; this closes the gap left by a run that failed before reaching
    # it, or by a datasource loaded by something other than `etl_run`. Also
    # ANALYZEs the rollup parents — a table the planner has no statistics for
    # gets read with a nested loop where it wanted a hash, and the whole point
    # of the rollup is that the planner picks well on it.
    # Guarded like every other step: a rollup problem must not cost the report.
    if source.rollup_interval_s:
        try:
            from .rollup import last_ok_hwm, refresh_rollups, rollup_specs
            hwm = last_ok_hwm(meta_engine, source.id)
            rep = refresh_rollups(source, dest_engine, meta_engine, hwm=hwm)
            report["rollup"] = {"rows": rep["rows"],
                                "built": [b["rollup"] for b in rep["built"]],
                                "skipped": rep["skipped"]}
            with dest_engine.connect().execution_options(
                    isolation_level="AUTOCOMMIT") as conn:
                for spec in rollup_specs(source):
                    conn.execute(text(f"ANALYZE {schema}.{spec.name}"))
        except Exception as exc:
            logger.exception("%s: rollup maintenance failed", source.name)
            report["warnings"].append(
                {"code": "rollup_failed", "message": str(exc)})

    runtime_ms = int((time.monotonic() - t0) * 1000)
    report["ran_at"] = datetime.now(timezone.utc).isoformat()
    report["runtime_ms"] = runtime_ms

    with meta_engine.begin() as meta:
        meta.execute(text(
            "INSERT INTO starcubes_meta.housekeeping_run "
            "  (datasource_id, runtime_ms, trigger, report) "
            "VALUES (:ds, :rt, :trg, CAST(:rep AS jsonb))"
        ), {"ds": source.id, "rt": runtime_ms, "trg": trigger,
            "rep": _json.dumps(report)})

    for w in report["warnings"]:
        logger.warning("%s: housekeeping %s: %s",
                       source.name, w["code"], w["message"])
    if not quiet:
        ranges = sum(b["ranges"] for b in report["brin"])
        logger.info(
            "%s: housekeeping done in %d ms — analyzed %s, summarised %d "
            "BRIN range(s), %d partition(s) totalling %.1f GB",
            source.name, runtime_ms,
            ", ".join(a["partition"] for a in report["analyzed"]) or "nothing",
            ranges, len(report["partitions"]),
            report["total_bytes"] / 1024 ** 3)
        if report.get("retention", {}).get("dropped"):
            logger.warning("%s: retention dropped %s",
                           source.name,
                           ", ".join(report["retention"]["dropped"]))
    return report


def housekeeping_due(meta_engine, datasource_id: int) -> bool:
    """True when `housekeeping_interval_s` has elapsed since the last run.

    Read fresh from the metadata on every call rather than off the `Source`:
    a forever session loads its `Source` once, before the loop, so an
    interval edited in the GUI mid-session would otherwise never be seen.
    """
    with meta_engine.begin() as meta:
        row = meta.execute(text(
            "SELECT d.housekeeping_interval_s AS iv, "
            "       (SELECT max(ran_at) FROM starcubes_meta.housekeeping_run h "
            "         WHERE h.datasource_id = d.id) AS last "
            "  FROM starcubes_meta.datasource d WHERE d.id = :ds"
        ), {"ds": datasource_id}).mappings().first()
    if not row:
        return False
    interval = int(row["iv"] or 0)
    if interval <= 0:
        return False
    last: Optional[datetime] = row["last"]
    if last is None:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last).total_seconds() >= interval
