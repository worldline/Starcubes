"""Pre-aggregation: bucket-granularity rollups of the fact table.

Every chart request re-aggregates raw rows. `core/cache.py` freezes the
finished payload, but only for an exactly repeated request — the first look
at any window pays the full scan. This module removes that scan for the
resolutions that span weeks and months.

The shape is deliberately *not* a cube. Materialising all 2^n marginals is
infeasible and also unnecessary: the base aggregation at bucket granularity
over a chosen set of dimension columns already contains every marginal, and
a second, cheap aggregation at query time extracts the one being asked for.
What cannot be avoided is choosing that column set — see `core/rollup_advise.py`
for the measurement behind that decision.

**A rollup is a column set.** The core rollup and each marginal are the same
mechanism; a marginal is simply a spec whose column set has one member.
`pick_rollup` returns the *narrowest* spec covering a query, so there is one
notion of coverage in the read path rather than two.

**The bucket column is named after `source.partition_column`.** That is what
keeps the change to the query builder small: `core.query._build_where` and the
slot expression work unchanged, because to them a rollup looks like a thinner
`fact`. Only three things are substituted — the table name, `count(*)` and the
measure expressions.

**What a rollup can never serve:** percentiles. They do not sum, so every
`quantile(col)` measure stays on `fact` permanently. Note this is a statement
about the *agg form*, not about the ladder styles: an `extent(col)` measure
draws with the same renderers but is built from min/avg/max, each of which
folds across buckets exactly — so it is served from here like any scalar.

No Django imports — callable from `core/` and from management commands the
same way `core/query.py` is.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import text

from core.query import filter_tree_columns, split_grouped_col
from core.schema import ensure_partitions_for, ensure_schema
from core.source import Source

logger = logging.getLogger(__name__)

# Aggregate forms a rollup can carry. `quantile` is deliberately absent and
# must stay absent: percentile_cont over pre-aggregated buckets is not the
# percentile of the underlying rows, and there is no correction for it.
ROLLUP_FUNCS = ("sum", "avg", "min", "max", "extent")

# The rungs an `extent(col)` ladder can carry, in canonical order. Mirrors
# `core.query.EXTENT_RUNGS` — this module already imports from `core.query`,
# but keeping the tuple local keeps the DDL side readable on its own.
EXTENT_RUNGS = ("min", "avg", "max")

# What `sum()` returns in Postgres, by input type. Getting this wrong is not
# a rounding problem but an overflow one: sum(bigint) is numeric precisely
# because bigint would overflow.
_SUM_TYPE = {
    "smallint":         "bigint",
    "integer":          "bigint",
    "int":              "bigint",
    "bigint":           "numeric",
    "numeric":          "numeric",
    "real":             "double precision",
    "double precision": "double precision",
}

_MEASURE_RE = re.compile(r"^(\w+)\((\w+)\)$")
_EXTENT_RE = re.compile(r"^extent\((\w+)\)$")


# ── Specs ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RollupSpec:
    """One rollup table: a column set at a bucket width, plus its measures."""
    name: str                    # table name inside the datasource schema
    columns: tuple[str, ...]     # dimension fact columns, sorted
    measures: tuple[str, ...]    # agg forms beside the implicit count(*), sorted
    interval_s: int              # bucket width in seconds
    is_marginal: bool = False

    def covers_columns(self, needed: set[str]) -> bool:
        return needed.issubset(set(self.columns))


@dataclass(frozen=True)
class RollupState:
    """What `starcubes_meta.rollup_state` holds for one rollup table."""
    spec_hash: str
    built_from: Optional[datetime]
    built_through: Optional[datetime]
    rows_built: int = 0


def interval_label(interval_s: int) -> str:
    """Short, stable suffix for a bucket width — the table's identity.

    Stable matters: the name is stored in `rollup_state.rollup_name` and is
    what `rollup_drop` looks for.
    """
    if interval_s % 86400 == 0:
        return f"{interval_s // 86400}d"
    if interval_s % 3600 == 0:
        return f"{interval_s // 3600}h"
    if interval_s % 60 == 0:
        return f"{interval_s // 60}m"
    return f"{interval_s}s"


def rollup_specs(source: Source) -> list[RollupSpec]:
    """Every rollup configured for this datasource, narrowest last.

    Order is not the pick order — `pick_rollup` sorts by width itself — but
    keeping the core first makes the CLI output read the way an operator
    configured it.
    """
    iv = int(source.rollup_interval_s or 0)
    if iv <= 0:
        return []
    label = interval_label(iv)
    measures = tuple(sorted(set(source.rollup_measures or ())))

    specs: list[RollupSpec] = []
    core_cols = tuple(sorted(set(source.rollup_columns or ())))
    if core_cols:
        specs.append(RollupSpec(name=f"rollup_{label}", columns=core_cols,
                                measures=measures, interval_s=iv))
    for col in sorted(set(source.rollup_marginals or ())):
        specs.append(RollupSpec(name=f"rollup_{label}_{col}", columns=(col,),
                                measures=measures, interval_s=iv,
                                is_marginal=True))
    return specs


def spec_hash(spec: RollupSpec) -> str:
    """Stable hash over everything that changes what the table *contains*.

    Compared against `rollup_state.spec_hash` on every read. A mismatch does
    not raise and does not delete anything — it simply makes the rollup
    invisible to `pick_rollup`, so a reconfiguration degrades to `fact`
    instead of returning numbers computed under the old shape. Same job
    `cache_epoch` does for `query_cache`, except derived from the config
    rather than bumped by an event.
    """
    payload = {
        "c":  list(spec.columns),
        "m":  list(spec.measures),
        "iv": int(spec.interval_s),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def validate_config(source: Source) -> list[str]:
    """Problems with this datasource's rollup configuration, as messages.

    Empty list means the configuration is usable. Called by `rollup_config`
    before it writes anything, so a typo never reaches the DDL.
    """
    problems: list[str] = []
    iv = int(source.rollup_interval_s or 0)
    if iv < 0:
        problems.append(f"rollup_interval_s must not be negative, got {iv}")
    if iv == 0:
        return problems  # off — nothing else matters

    by_name = {c.name: c for c in source.fact_columns}
    dim_roles = {"dimension", "auto-dimension"}
    for col in list(source.rollup_columns or ()) + list(source.rollup_marginals or ()):
        fc = by_name.get(col)
        if fc is None:
            problems.append(f"{col!r} is not a fact column of {source.name!r}")
        elif fc.role not in dim_roles:
            problems.append(
                f"{col!r} has role {fc.role!r}; a rollup groups by dimension "
                f"columns only")
    overlap = set(source.rollup_columns or ()) & set(source.rollup_marginals or ())
    if overlap:
        problems.append(
            f"{sorted(overlap)} appear in both rollup_columns and "
            f"rollup_marginals; a marginal is only useful for a column the "
            f"core set does not hold")

    for agg in source.rollup_measures or ():
        m = _MEASURE_RE.match(agg or "")
        if not m:
            problems.append(
                f"measure {agg!r} is not of the form <func>(<column>)")
            continue
        func, col = m.group(1), m.group(2)
        if func == "quantile":
            problems.append(
                f"measure {agg!r}: percentiles cannot be pre-aggregated — "
                f"they do not sum, so ladder styles always read fact")
            continue
        if func not in ROLLUP_FUNCS:
            problems.append(
                f"measure {agg!r}: unknown function {func!r}; "
                f"choose from {sorted(ROLLUP_FUNCS)}")
            continue
        fc = by_name.get(col)
        if fc is None:
            problems.append(f"measure {agg!r}: {col!r} is not a fact column")
        elif fc.role != "measure":
            problems.append(
                f"measure {agg!r}: {col!r} has role {fc.role!r}, not 'measure'")
        elif fc.pg_type not in _SUM_TYPE:
            problems.append(
                f"measure {agg!r}: column type {fc.pg_type!r} has no defined "
                f"sum type; add it to core.rollup._SUM_TYPE if it is summable")
    return problems


# ── Measure materialisation ─────────────────────────────────────────────
#
# A measure changes the *width* of a rollup row, never the number of rows —
# which is why the operator decision about measures is a storage question and
# the one about dimensions is a cardinality question. Several measure forms
# can share one stored column: sum(amount) and avg(amount) both read sum_amount.

def measure_columns(spec: RollupSpec, source: Source) -> dict[str, tuple[str, str]]:
    """Stored measure columns for `spec`: {column: (pg_type, fact-side agg)}.

    `n` (the count) is not in here — it is mandatory and handled separately,
    because `count(*)` is not tied to any measure column.

    **`extent(col)` materialises all four columns, whatever its rung list
    says.** That is load-bearing, not laziness. The rung list lives in
    `starcubes_meta.measure.percentiles`, i.e. in the measure *catalog* — it
    is deliberately not part of `RollupSpec` and therefore not part of
    `spec_hash`. Honouring it here (skipping `sum_`/`cnt_` for a `min,max`
    ladder) would mean that editing a measure from `min,max` to `min,avg,max`
    leaves the hash valid while the read path selects `sum_col`/`cnt_col`
    columns that were never created: a hard SQL error at chart time, not the
    graceful degradation to `fact` every other reconfiguration gets.

    The two ways out were "always materialise all four" and "put the rung
    list into `spec_hash`". The latter would couple the spec to the measure
    catalog, so that *any* catalog edit could stale a rollup. This costs
    16 bytes per row when the operator picked a two-rung ladder, and buys a
    rung list that is a pure presentation choice: freely editable, no
    rebuild, no hash involvement.
    """
    by_name = {c.name: c for c in source.fact_columns}
    out: dict[str, tuple[str, str]] = {}
    for agg in spec.measures:
        m = _MEASURE_RE.match(agg or "")
        if not m:
            continue
        func, col = m.group(1), m.group(2)
        fc = by_name.get(col)
        if fc is None or func not in ROLLUP_FUNCS:
            continue
        if func in ("sum", "avg", "extent"):
            out[f"sum_{col}"] = (_SUM_TYPE.get(fc.pg_type, "numeric"),
                                 f"sum(f.{col})")
        if func in ("avg", "extent"):
            # count(col), not the row count: with a NULL in the column the two
            # differ, and avg would be pulled toward zero by rows that never
            # contributed a value.
            out[f"cnt_{col}"] = ("bigint", f"count(f.{col})")
        if func in ("min", "extent"):
            out[f"min_{col}"] = (fc.pg_type, f"min(f.{col})")
        if func in ("max", "extent"):
            out[f"max_{col}"] = (fc.pg_type, f"max(f.{col})")
    return out


def measure_expr(measure: Optional[str], spec: RollupSpec, *,
                 rungs: Optional[list] = None) -> Optional[str]:
    """The rollup-side expression producing the same value as `measure`.

    A scalar for every additive form; for `extent(col)` an ``ARRAY[…]`` over
    the stored extremes and the folded mean. That a *ladder* can come out of
    this function at all is the whole point of the form: percentiles cannot,
    min/avg/max can, and the read path does not otherwise care which it got —
    `core.query.build_chart_sql` drops the string into its one value slot
    either way. `rungs` selects and orders the array elements (None → the
    canonical `EXTENT_RUNGS`) and is ignored for every other measure; the
    caller resolves it from the measure catalog via `Source.rungs_for`, so
    this module never has to reach into `source.measures`.

    Returns None when this rollup cannot answer for that measure, which is
    what condition 5 of `pick_rollup` tests. `f` is the rollup table alias,
    matching what `core.query.build_chart_sql` emits.
    """
    if measure in (None, "", "count(*)"):
        return "sum(f.n)"
    m = _MEASURE_RE.match(measure)
    if m:
        func, col = m.group(1), m.group(2)
    else:
        func, col = "sum", measure     # bare column means sum, as in _parse_measure
    if f"{func}({col})" not in spec.measures:
        return None
    if func == "sum":
        return f"sum(f.sum_{col})"
    if func == "avg":
        return f"sum(f.sum_{col})::numeric / NULLIF(sum(f.cnt_{col}), 0)"
    if func == "min":
        return f"min(f.min_{col})"
    if func == "max":
        return f"max(f.max_{col})"
    if func == "extent":
        # Every element cast to double precision, for the same reason the
        # fact-side counterpart in `core.query.extent_value_expr` does it: the
        # array needs one element type, and `percentile_cont` on the other
        # ladder path yields double precision, so both feeders hand Python the
        # same shape. The avg rung reuses the scalar `avg` expression above
        # verbatim — reproducing its arithmetic rather than "improving" it is
        # what keeps `rollup_verify`'s extent cases comparable to its
        # long-standing avg cases.
        parts = {
            "min": f"min(f.min_{col})::double precision",
            "avg": (f"(sum(f.sum_{col})::numeric / "
                    f"NULLIF(sum(f.cnt_{col}), 0))::double precision"),
            "max": f"max(f.max_{col})::double precision",
        }
        picked = [r for r in EXTENT_RUNGS if r in set(rungs or EXTENT_RUNGS)]
        if len(picked) < 2:
            return None
        return "ARRAY[" + ", ".join(parts[r] for r in picked) + "]"
    return None


# ── DDL ─────────────────────────────────────────────────────────────────

def ensure_rollup_tables(source: Source, dest_engine, *,
                         quiet: bool = False) -> list[str]:
    """Create every configured rollup table and its partitions. Idempotent.

    RANGE-partitioned on the same `partition_period` as `fact`, so retention
    and housekeeping reuse the machinery that already exists for fact
    partitions.

    **LOGGED**, unlike `fact`. `fact` is UNLOGGED because it can be rebuilt
    from the source; a rollup could be rebuilt from `fact`, but a crash would
    discard a year of aggregates whose rebuild is expensive. At the row counts
    a rollup has, the WAL it costs is a rounding error next to the fact table's.
    """
    specs = rollup_specs(source)
    if not specs:
        return []
    schema = source.schema_name
    pc = source.partition_column
    by_name = {c.name: c for c in source.fact_columns}
    created: list[str] = []

    with dest_engine.begin() as conn:
        ensure_schema(conn, schema)
        for spec in specs:
            cols_ddl = ", ".join(
                f"{c} {by_name[c].pg_type}" for c in spec.columns
                if c in by_name)
            meas_ddl = "".join(
                f", {name} {pg_type}"
                for name, (pg_type, _agg) in sorted(measure_columns(spec, source).items()))
            conn.execute(text(
                f"CREATE TABLE IF NOT EXISTS {schema}.{spec.name} ("
                f"  {pc} timestamptz NOT NULL, {cols_ddl}, "
                f"  n bigint NOT NULL{meas_ddl}"
                f") PARTITION BY RANGE ({pc})"
            ))
            created.append(spec.name)

    # Partitions and the BRIN index need the parent to exist, hence the second
    # pass. BRIN and not btree: the bucket column is written in time order, so
    # correlation is perfect and the index is a few pages.
    now = datetime.now(timezone.utc)
    for spec in specs:
        ensure_partitions_for(source, dest_engine, now, now,
                              back=1, forward=1, quiet=True,
                              table=spec.name, unlogged=False,
                              require_existing=False)
        with dest_engine.begin() as conn:
            conn.execute(text(
                f"CREATE INDEX IF NOT EXISTS ix_{spec.name}_{pc} "
                f"ON {schema}.{spec.name} USING brin({pc})"
            ))
    if created and not quiet:
        logger.info("%s: rollup tables ready: %s",
                    source.name, ", ".join(created))
    return created


def drop_rollup_tables(source: Source, dest_engine, meta_engine) -> list[str]:
    """Drop every rollup table of this datasource and forget its state.

    Safe at any time: the rollup is purely additive in the read path, so
    without state every query goes to `fact` again.

    Reads the table names from `rollup_state` as well as from the current
    configuration — otherwise a rollup left behind by an earlier column set
    would survive the drop and never be touched again.
    """
    schema = source.schema_name
    names = {s.name for s in rollup_specs(source)}
    with meta_engine.begin() as conn:
        names |= set(conn.execute(text(
            "SELECT rollup_name FROM starcubes_meta.rollup_state "
            " WHERE datasource_id = :ds"
        ), {"ds": source.id}).scalars().all())
    dropped: list[str] = []
    with dest_engine.begin() as conn:
        for name in sorted(names):
            conn.execute(text(f"DROP TABLE IF EXISTS {schema}.{name} CASCADE"))
            dropped.append(name)
    with meta_engine.begin() as conn:
        conn.execute(text(
            "DELETE FROM starcubes_meta.rollup_state WHERE datasource_id = :ds"
        ), {"ds": source.id})
    return dropped


# ── State ───────────────────────────────────────────────────────────────

def read_rollup_states(meta_engine, datasource_id: int) -> dict[str, RollupState]:
    """Build state per rollup table. One round trip; empty dict when off."""
    with meta_engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT rollup_name, spec_hash, built_from, built_through, rows_built "
            "  FROM starcubes_meta.rollup_state WHERE datasource_id = :ds"
        ), {"ds": datasource_id}).mappings().all()
    return {r["rollup_name"]: RollupState(
        spec_hash=r["spec_hash"],
        built_from=_as_utc(r["built_from"]),
        built_through=_as_utc(r["built_through"]),
        rows_built=int(r["rows_built"] or 0),
    ) for r in rows}


def last_ok_hwm(meta_engine, datasource_id: int) -> Optional[datetime]:
    """High watermark of the newest successful run, tz-aware UTC.

    The rollup needs it to decide which buckets are final, and four callers
    need it: the ETL hook, housekeeping, `rollup_build` and anything hand-run.
    Read fresh rather than taken from the run that just finished — a caught
    ETL failure leaves the previous watermark standing, and rolling up past it
    would aggregate a window the load never completed.
    """
    with meta_engine.begin() as conn:
        hwm = conn.execute(text(
            "SELECT last_end_time FROM starcubes_meta.etl_run "
            " WHERE datasource_id = :ds AND status = 'ok' "
            " ORDER BY finished_at DESC LIMIT 1"
        ), {"ds": datasource_id}).scalar()
    return _as_utc(hwm)


def _as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Timestamps come back naive from some drivers; the read path compares
    them against tz-aware bounds, and a naive/aware comparison raises."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None \
        else dt.astimezone(timezone.utc)


def write_rollup_state(meta_engine, datasource_id: int, spec: RollupSpec, *,
                       built_from: Optional[datetime],
                       built_through: Optional[datetime],
                       rows_built: int = 0) -> None:
    """Upsert the state row. `built_from`/`built_through` are set, not merged —
    callers own the widening (see `extend_rollup_state`)."""
    with meta_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO starcubes_meta.rollup_state "
            "    (datasource_id, rollup_name, spec_hash, built_from, "
            "     built_through, rows_built, last_built_at) "
            "VALUES (:ds, :n, :h, :bf, :bt, :rb, now()) "
            "ON CONFLICT (datasource_id, rollup_name) DO UPDATE "
            "  SET spec_hash = EXCLUDED.spec_hash, "
            "      built_from = EXCLUDED.built_from, "
            "      built_through = EXCLUDED.built_through, "
            "      rows_built = EXCLUDED.rows_built, "
            "      last_built_at = now()"
        ), {"ds": datasource_id, "n": spec.name, "h": spec_hash(spec),
            "bf": built_from, "bt": built_through, "rb": int(rows_built)})


def unreconstructable_span(source: Source,
                           states: dict[str, RollupState]
                           ) -> Optional[tuple[datetime, datetime]]:
    """The span a rollup holds that `fact` can no longer reproduce, if any.

    Retention deliberately leaves the rollups alone (see
    `core.etl._raise_retention_floor`), so below `retention_floor` a rollup
    stops being a cache and becomes the *only* copy of those numbers. That
    inverts the safety of two commands: `rollup_drop` and `rollup_build
    --rebuild` are cheap and reversible above the floor and irreversible
    below it, because the backfill can only read what `fact` still has.

    Returns `(oldest_bucket_held, retention_floor)` when such a span exists,
    else None. Callers use it to refuse without an explicit --force.
    """
    floor = source.retention_floor
    if floor is None:
        return None
    if floor.tzinfo is None:
        floor = floor.replace(tzinfo=timezone.utc)
    lows = [st.built_from for st in states.values()
            if st.built_from is not None and st.built_from < floor]
    return (min(lows), floor) if lows else None


def rollup_extent(states: dict[str, RollupState]
                  ) -> tuple[Optional[datetime], Optional[datetime]]:
    """(oldest, newest) bucket bound across every rollup with a built range.

    The chart's advertised time extent is clamped to `retention_floor` so a
    user is not offered a range where `fact` is empty on purpose. Rollups
    reaching further back are exactly the case that clamp must not apply to —
    the counts are there, they simply cannot be drilled into.
    """
    lows = [st.built_from for st in states.values() if st.built_from]
    highs = [st.built_through for st in states.values() if st.built_through]
    return (min(lows) if lows else None, max(highs) if highs else None)


def rewind_rollup_state(meta_engine, datasource_id: int,
                        to: datetime) -> int:
    """Pull `built_through` back to `to` wherever it reaches past it.

    This is how historical mutation is handled — a rewind, not an
    invalidation. A repair (`dedupe_from`) already rewinds the watermark; the
    rollup only has to forget the same span, and the next ETL hook rebuilds
    it. Nothing is deleted: the stale buckets are simply outside
    [built_from, built_through) until they are overwritten.
    """
    with meta_engine.begin() as conn:
        res = conn.execute(text(
            "UPDATE starcubes_meta.rollup_state "
            "   SET built_through = :to "
            " WHERE datasource_id = :ds AND built_through > :to"
        ), {"ds": datasource_id, "to": to})
    return res.rowcount or 0


# ── Building ────────────────────────────────────────────────────────────

def _bucket_expr(source: Source, interval_s: int) -> str:
    """Bucket start for a fact row, as a timestamptz.

    Epoch arithmetic rather than `date_trunc`: unix epoch is intrinsically
    UTC, and UTC is exactly what the chart snaps its windows to
    (`Math.floor(epochSec / interval) * interval` in chart.html). `date_trunc`
    would follow the server's TimeZone setting and put the day boundary
    somewhere the chart never asks about.
    """
    pc = source.partition_column
    return (f"to_timestamp(floor(extract(epoch from f.{pc}) / {int(interval_s)}) "
            f"* {int(interval_s)})")


def build_rollup_sql(source: Source, spec: RollupSpec) -> tuple[str, str]:
    """(delete_sql, insert_sql) for one window, both taking :t_from/:t_to.

    DELETE before INSERT rather than ON CONFLICT: the natural key is the whole
    dimension tuple, which for a wide spec is more columns than a unique index
    should carry, and a rebuilt window legitimately produces a *different set*
    of tuples (a combination can vanish). Deleting the window first is the
    only form that converges on the right rows.
    """
    schema = source.schema_name
    pc = source.partition_column
    cols = list(spec.columns)
    meas = sorted(measure_columns(spec, source).items())

    col_list = "".join(f", {c}" for c in cols)
    meas_list = "".join(f", {name}" for name, _ in meas)
    meas_sel = "".join(f", {agg}" for _, (_ty, agg) in meas)
    group_by = ", ".join(str(i) for i in range(1, len(cols) + 2))

    delete_sql = (
        f"DELETE FROM {schema}.{spec.name} "
        f" WHERE {pc} >= :t_from AND {pc} < :t_to")
    insert_sql = (
        f"INSERT INTO {schema}.{spec.name} ({pc}{col_list}, n{meas_list}) "
        f"SELECT {_bucket_expr(source, spec.interval_s)}"
        f"{''.join(f', f.{c}' for c in cols)}, count(*)::bigint{meas_sel} "
        f"  FROM {schema}.fact f "
        f" WHERE f.{pc} >= :t_from AND f.{pc} < :t_to "
        f" GROUP BY {group_by}")
    return delete_sql, insert_sql


def build_window(source: Source, dest_engine, meta_engine, spec: RollupSpec,
                 t_from: datetime, t_to: datetime) -> int:
    """Rebuild one half-open window of one rollup. Returns rows written.

    The DELETE and the INSERT share a transaction, so a window is never
    half-replaced — a reader either sees the old content or the new.
    """
    delete_sql, insert_sql = build_rollup_sql(source, spec)
    params = {"t_from": t_from, "t_to": t_to}
    ensure_partitions_for(source, dest_engine, t_from, t_to,
                          back=1, forward=1, quiet=True,
                          table=spec.name, unlogged=False,
                          require_existing=False)
    with dest_engine.begin() as conn:
        conn.execute(text(delete_sql), params)
        res = conn.execute(text(insert_sql), params)
    return res.rowcount or 0


def floor_to_bucket(dt: datetime, interval_s: int) -> datetime:
    """Round `dt` down to a bucket boundary, in UTC. Mirrors `_bucket_expr`."""
    epoch = int(dt.timestamp())
    return datetime.fromtimestamp((epoch // interval_s) * interval_s,
                                  tz=timezone.utc)


def final_through(source: Source, hwm: Optional[datetime],
                  interval_s: int) -> Optional[datetime]:
    """Newest bucket boundary whose data can no longer change.

    Same finality rule `core.cache.is_cacheable` applies: everything below
    `hwm - late_arrival_overlap_s` is settled, because the overlap span is
    exactly where an incremental run may still insert late rows. Buckets
    above it must not be rolled up — they would be wrong with no event to
    invalidate them.
    """
    if hwm is None:
        return None
    settled = hwm - timedelta(seconds=max(0, source.late_arrival_overlap_s))
    return floor_to_bucket(settled, interval_s)


def refresh_rollups(source: Source, dest_engine, meta_engine, *,
                    hwm: Optional[datetime],
                    max_buckets: int = 0,
                    quiet: bool = True) -> dict:
    """Advance every rollup up to the finality horizon. The ETL hook's entry point.

    Only moves `built_through` forward. The backfill (`rollup_build
    --backfill`) is what moves `built_from` back, and the two never contend
    for the same span.

    A rollup whose stored `spec_hash` disagrees with the configuration is
    rebuilt from scratch rather than extended — its rows describe a different
    shape, and appending to them would produce a table that is neither.

    `max_buckets` caps one call so a datasource that has been idle for months
    does not turn a routine ETL run into an hours-long aggregation; the rest
    follows on the next run.
    """
    specs = rollup_specs(source)
    report = {"built": [], "rows": 0, "skipped": []}
    if not specs:
        return report
    iv = int(source.rollup_interval_s)
    through = final_through(source, hwm, iv)
    if through is None:
        report["skipped"].append("no successful high watermark yet")
        return report

    states = read_rollup_states(meta_engine, source.id)
    for spec in specs:
        st = states.get(spec.name)
        want = spec_hash(spec)
        if st is not None and st.spec_hash == want and st.built_through:
            start = st.built_through
            keep_from = st.built_from
        else:
            # Unknown, or built under a different spec: start one bucket back
            # from the horizon. The backfill fills in the history.
            start = through - timedelta(seconds=iv)
            keep_from = start
        if start >= through:
            continue
        if max_buckets > 0:
            capped = start + timedelta(seconds=iv * max_buckets)
            if capped < through:
                through_eff = capped
            else:
                through_eff = through
        else:
            through_eff = through

        rows = build_window(source, dest_engine, meta_engine, spec,
                            start, through_eff)
        write_rollup_state(
            meta_engine, source.id, spec,
            built_from=min(keep_from, start) if keep_from else start,
            built_through=through_eff,
            rows_built=(st.rows_built if st and st.spec_hash == want else 0) + rows,
        )
        report["built"].append({"rollup": spec.name, "rows": rows,
                                "from": start, "through": through_eff})
        report["rows"] += rows
        if not quiet:
            logger.info("%s: %s +%d rows [%s, %s)",
                        source.name, spec.name, rows, start, through_eff)
    return report


# ── Read path ───────────────────────────────────────────────────────────

def pick_rollup(source: Source, states: dict[str, RollupState], *,
                group_by: Optional[str],
                filter_tree: Optional[list],
                measure: Optional[str],
                graph_style: str,
                interval: int,
                t_from: datetime,
                t_to: datetime,
                ladder_styles: tuple[str, ...] = ()) -> Optional[RollupSpec]:
    """The narrowest rollup that can answer this query, or None.

    Modelled on `core.cache.is_cacheable`: a pure predicate over the request
    and the recorded state, no I/O. Returning None is never an error — it
    means the read path uses `fact`, which is always correct, only slower.

    The seven conditions, in the order they are cheapest to reject on:

      1. the bucket width must divide the chart interval, or a slot would
         straddle bucket boundaries
      2. the window must lie on bucket boundaries, or the time filter cuts a
         bucket in half and silently drops or double-counts it
      3. the stored spec hash must match, and the window must lie inside
         [built_from, built_through)
      4. no *percentile* ladder — percentiles do not sum. An `extent(col)`
         ladder passes: its rungs are min/avg/max, each of which folds across
         buckets exactly, which is the entire reason that form exists
      5. the measure must be derivable *and* materialised in that spec
      6. the real group_by column must be in the spec
      7. every column a filter actually reads must be in the spec — resolved
         exactly the way `_filter_sql` resolves it, grouped tokens and
         fan-out included
    """
    specs = rollup_specs(source)
    if not specs:
        return None
    iv = int(source.rollup_interval_s or 0)
    if iv <= 0 or interval <= 0 or interval % iv != 0:
        return None
    if int(t_from.timestamp()) % iv != 0 or int(t_to.timestamp()) % iv != 0:
        return None
    # The gate is the agg form, not the style: `LADDER_STYLES` is a set of
    # shapes and an `extent(col)` measure wears the same one. `measure_expr`
    # (condition 5) then decides whether this particular spec materialised it.
    if graph_style in ladder_styles and not _EXTENT_RE.match(measure or ""):
        return None

    dims_by_fact = {d.fact_column: d for d in source.dimensions}
    needed: set[str] = set()
    if group_by:
        real_col, _slug, _is_grouped = split_grouped_col(group_by)
        needed.add(real_col)
    needed |= filter_tree_columns(filter_tree or [], dims_by_fact)

    candidates = []
    for spec in specs:
        st = states.get(spec.name)
        if st is None or st.spec_hash != spec_hash(spec):
            continue
        if st.built_from is None or st.built_through is None:
            continue
        if t_from < st.built_from or t_to > st.built_through:
            continue
        if measure_expr(measure, spec) is None:
            continue
        if not spec.covers_columns(needed):
            continue
        candidates.append(spec)
    if not candidates:
        return None
    # Narrowest first: fewer columns means fewer rows to scan, and a marginal
    # beats the core set whenever both cover the query.
    return min(candidates, key=lambda s: (len(s.columns), s.name))
