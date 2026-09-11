"""Measure what a rollup would cost, so the operator can choose its columns.

`core/rollup.py` can pre-aggregate any set of dimension columns, but which
set is worth it is not derivable from the schema — it depends on how the
values actually co-occur. On a 14-dimension payment schema the spread between
the best and worst choice was three orders of magnitude, and a *single*
column accounted for more of it than the other thirteen together. Nobody can
guess that; this module measures it.

**How the measurement works.** Not by sampling: estimating distinct
combinations from a sample is the unseen-species problem, and the error runs
in the direction that matters (a sample always undercounts rare combinations,
so it flatters the rollup). Instead one exact pass over a real window of
`fact`, materialised into a temporary base table at bucket granularity — and
every further number is then computed from that small table rather than from
`fact` again. That is the same asymmetry the rollup itself exploits:
coarsening is cheap, refining is not.

**The two densities.** Combination counts saturate: doubling the rows in a
bucket does not double the distinct tuples in it. So a measurement at one
density says little about another, and a datasource whose real load is still
ramping up would be sized against the wrong number. The base table is
therefore re-grouped at a coarser bucket width, which is exactly an
observation at 24x the density, and the two points give the saturation
exponent.

`K` — the distinct tuples over the whole window, ignoring time — is reported
and used for the per-column marginal costs, where it is exact because every
comparison happens inside the same window. It deliberately does *not* cap the
extrapolation, which was the first thing tried and is wrong: a longer window
keeps finding combinations a shorter one never saw, so K measured here is a
lower bound on the real asymptote, and capping with it understates the
combinations and overstates the speed-up. See `_project`.

No Django imports.
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from sqlalchemy import text

from core.query import split_grouped_col
from core.rollup import ROLLUP_FUNCS, _SUM_TYPE
from core.source import Source

logger = logging.getLogger(__name__)

_MEASURE_RE = re.compile(r"^(\w+)\((\w+)\)$")

# Row overhead in the rollup table: 24 bytes of tuple header plus the bucket
# timestamptz and the bigint count. Everything else is per-column and added
# from the pg_type below. Approximate on purpose — this sizes a decision, and
# being 20% off does not change which column an operator drops.
_ROW_OVERHEAD_BYTES = 24 + 8 + 8
_TYPE_BYTES = {
    "smallint": 2, "integer": 4, "int": 4, "bigint": 8,
    "numeric": 12, "real": 4, "double precision": 8,
    "timestamptz": 8, "text": 16,
}


@dataclass
class DimStat:
    """One dimension column's cardinality, cost and usage."""
    column: str
    distinct: int
    marginal_cost: float   # |K(all)| / |K(all without this column)|
    gb_hits: int           # group_by requests seen in query_cache


@dataclass
class Stage:
    """One candidate column set on the greedy ladder."""
    columns: tuple[str, ...]
    dropped: tuple[str, ...]
    combos_per_bucket: float
    rows_per_day: float
    bytes_per_day: float
    factor: float          # fact rows/day divided by rollup rows/day


@dataclass
class MeasureStat:
    name: str
    agg: str
    derivable: bool
    reason: str
    bytes_per_row: int


@dataclass
class Advice:
    datasource: str
    interval_s: int
    window_from: datetime
    window_to: datetime
    rows_in_window: int
    buckets_in_window: int
    rows_per_bucket: float
    rows_per_day_measured: float
    rows_per_day_assumed: float      # after --extrapolate-to, if given
    saturation_exponent: Optional[float]
    dims: list[DimStat] = field(default_factory=list)
    stages: list[Stage] = field(default_factory=list)
    measures: list[MeasureStat] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# ── Windowing ───────────────────────────────────────────────────────────

def _measure_window(source: Source, dest_engine, interval_s: int,
                    window_buckets: int) -> tuple[datetime, datetime]:
    """The newest fully-loaded span of `window_buckets` buckets.

    Anchored on max(partition_column) rather than on the high watermark: the
    advisor only needs rows that are *representative*, not rows that are
    final, and max() costs one index-free but tiny aggregate while the HWM
    would drag the ETL history in. The newest bucket is excluded — it is
    partially filled by definition and would understate the density.
    """
    pc = source.partition_column
    with dest_engine.connect() as conn:
        newest = conn.execute(text(
            f"SELECT max({pc}) FROM {source.schema_name}.fact")).scalar()
    if newest is None:
        raise RuntimeError(
            f"{source.name}: fact is empty — nothing to measure")
    if newest.tzinfo is None:
        newest = newest.replace(tzinfo=timezone.utc)
    epoch = int(newest.timestamp())
    end = datetime.fromtimestamp((epoch // interval_s) * interval_s,
                                 tz=timezone.utc)
    return end - timedelta(seconds=interval_s * window_buckets), end


# ── The one pass over fact ──────────────────────────────────────────────

def _dimension_columns(source: Source) -> list[str]:
    """Dimension fact columns, in ordinal order — rollup candidates."""
    return [c.name for c in sorted(source.fact_columns, key=lambda c: c.ordinal)
            if c.role in ("dimension", "auto-dimension")]


def _build_base(conn, source: Source, dims: list[str], interval_s: int,
                t_from: datetime, t_to: datetime) -> int:
    """Materialise (bucket, dims..., n) for the window into a temp table.

    This is the only statement that reads `fact`. Everything the advisor
    reports afterwards is an aggregation over `_advise_base`, which is smaller
    by whatever factor the rollup would achieve — so the expensive question
    ("what would this cost?") is answered by paying that cost exactly once.
    """
    pc = source.partition_column
    col_list = ", ".join(f"f.{c}" for c in dims)
    group_by = ", ".join(str(i) for i in range(1, len(dims) + 2))
    conn.execute(text("DROP TABLE IF EXISTS _advise_base"))
    conn.execute(text(
        f"CREATE TEMP TABLE _advise_base AS "
        f"SELECT floor(extract(epoch from f.{pc}) / {int(interval_s)})::bigint "
        f"         AS bucket, {col_list}, count(*)::bigint AS n "
        f"  FROM {source.schema_name}.fact f "
        f" WHERE f.{pc} >= :t_from AND f.{pc} < :t_to "
        f" GROUP BY {group_by}"
    ), {"t_from": t_from, "t_to": t_to})
    return int(conn.execute(text(
        "SELECT coalesce(sum(n), 0) FROM _advise_base")).scalar() or 0)


def _combos(conn, cols: list[str], *, bucket_divisor: int = 1,
            with_time: bool = True) -> int:
    """Distinct tuples in the base table.

    `bucket_divisor` re-groups at a coarser bucket width — dividing the bucket
    index by 24 is an observation of the same data at 24x the density, which
    is where the saturation exponent comes from. `with_time=False` drops the
    bucket entirely and yields K, the asymptote.
    """
    if not cols and not with_time:
        return 1
    parts = []
    if with_time:
        parts.append("bucket" if bucket_divisor == 1
                     else f"(bucket / {int(bucket_divisor)})")
    parts.extend(cols)
    sel = ", ".join(parts)
    return int(conn.execute(text(
        f"SELECT count(*) FROM (SELECT {sel} FROM _advise_base "
        f"GROUP BY {sel}) t")).scalar() or 0)


# ── Usage statistics ────────────────────────────────────────────────────

def _group_by_hits(meta_engine, datasource_id: int) -> dict[str, int]:
    """group_by request counts from `query_cache`.

    Grouped tokens are folded onto their real column: `response:grouped:foo`
    and `response` both need `response` in the rollup.

    Incomplete by construction — `core.cache.is_cacheable` only lets
    `count(*)` requests into the cache, so measure-bearing requests are
    invisible here. That is fine for choosing *dimensions*, because the
    group_by choice does not depend on the measure, and it is why the advisor
    prints the caveat rather than pretending the numbers are complete.
    """
    with meta_engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT group_by, sum(hits) AS hits, count(*) AS entries "
            "  FROM starcubes_meta.query_cache "
            " WHERE datasource_id = :ds GROUP BY group_by"
        ), {"ds": datasource_id}).mappings().all()
    out: dict[str, int] = {}
    for r in rows:
        token = (r["group_by"] or "").strip()
        if not token:
            continue
        col, _slug, _grouped = split_grouped_col(token)
        # An entry that was stored but never hit again still proves the
        # breakdown was asked for once, so entries count as a floor.
        out[col] = out.get(col, 0) + max(int(r["hits"] or 0),
                                         int(r["entries"] or 0))
    return out


# ── Measures ────────────────────────────────────────────────────────────

def _measure_stats(source: Source) -> list[MeasureStat]:
    """Which catalogued measures a rollup could serve, and at what row width.

    The interesting output is binary, not numeric: percentiles can never be
    pre-aggregated, and an operator who does not learn that up front will
    wonder why half the chart got faster.
    """
    by_name = {c.name: c for c in source.fact_columns}
    out = [MeasureStat(name="(count)", agg="count(*)", derivable=True,
                       reason="always present", bytes_per_row=0)]
    for m in source.measures:
        agg = (m.agg or "").strip()
        mm = _MEASURE_RE.match(agg)
        if agg == "count(*)":
            continue                      # already listed, and free
        if not mm:
            out.append(MeasureStat(m.name, agg, False,
                                   "not of the form <func>(<column>)", 0))
            continue
        func, col = mm.group(1), mm.group(2)
        if func == "quantile":
            out.append(MeasureStat(
                m.name, agg, False,
                "percentiles do not sum — ladder styles always read fact", 0))
            continue
        if func not in ROLLUP_FUNCS:
            out.append(MeasureStat(m.name, agg, False,
                                   f"unknown function {func!r}", 0))
            continue
        fc = by_name.get(col)
        if fc is None:
            out.append(MeasureStat(m.name, agg, False,
                                   f"{col!r} is not a fact column", 0))
            continue
        if func in ("sum", "avg"):
            width = _TYPE_BYTES.get(_SUM_TYPE.get(fc.pg_type, "numeric"), 8)
            if func == "avg":
                width += 8                # the separate count(col)
        elif func == "extent":
            # All four stored columns, always — see `measure_columns`, where
            # the rung list is deliberately not allowed to decide what is
            # materialised. So the widest measure form there is, and the one
            # worth naming in the report rather than lumping in with min/max.
            width = (_TYPE_BYTES.get(_SUM_TYPE.get(fc.pg_type, "numeric"), 8)
                     + 8                                    # count(col)
                     + 2 * _TYPE_BYTES.get(fc.pg_type, 8))  # min + max
        else:
            width = _TYPE_BYTES.get(fc.pg_type, 8)
        out.append(MeasureStat(m.name, agg, True,
                               "derivable from the stored aggregate", width))
    return out


# ── The advisor ─────────────────────────────────────────────────────────

def advise(source: Source, dest_engine, meta_engine, *,
           interval_s: int = 3600,
           window_buckets: int = 24,
           coarse_factor: int = 24,
           extrapolate_to: Optional[float] = None,
           progress: Optional[Callable[[str], None]] = None) -> Advice:
    """Measure this datasource and return the numbers behind the decision.

    `interval_s` is the bucket width being considered, `window_buckets` how
    many of them to measure over (one day at hourly buckets), and
    `coarse_factor` the multiple used for the second density observation.

    `extrapolate_to` is rows per hour: pass it when the measured window is not
    representative of the load this is being sized for. On a datasource
    already carrying its real volume it should be left alone — the measurement
    is then the answer, and extrapolating from it can only add error.
    """
    say = progress or (lambda _m: None)
    dims = _dimension_columns(source)
    if not dims:
        raise RuntimeError(f"{source.name}: no dimension columns to roll up")

    t_from, t_to = _measure_window(source, dest_engine, interval_s,
                                   window_buckets)
    warnings: list[str] = []
    notes: list[str] = []

    # One connection for the whole run: the base table is TEMP, so it lives
    # and dies with this session.
    with dest_engine.connect() as conn:
        say(f"scanning fact over [{t_from:%Y-%m-%d %H:%M}, {t_to:%Y-%m-%d %H:%M}) …")
        rows_in_window = _build_base(conn, source, dims, interval_s,
                                     t_from, t_to)
        if rows_in_window == 0:
            raise RuntimeError(
                f"{source.name}: no rows in [{t_from}, {t_to}) — widen the "
                f"window with --window-buckets")

        buckets = int(conn.execute(text(
            "SELECT count(DISTINCT bucket) FROM _advise_base")).scalar() or 1)
        rows_per_bucket = rows_in_window / max(1, buckets)

        say("measuring the full column set …")
        combos_fine = _combos(conn, dims) / max(1, buckets)
        coarse_buckets = max(1, buckets // coarse_factor)
        combos_coarse = (_combos(conn, dims, bucket_divisor=coarse_factor)
                         / coarse_buckets)
        k_all = _combos(conn, dims, with_time=False)

        # Saturation exponent from the two densities. Needs a real spread in
        # both directions, otherwise the fit is noise dressed as a number.
        alpha: Optional[float] = None
        if buckets >= coarse_factor and combos_fine > 0 and combos_coarse > combos_fine:
            alpha = math.log(combos_coarse / combos_fine) / math.log(coarse_factor)
        else:
            notes.append(
                "no saturation fit: the window holds too few buckets for a "
                "second density observation, so extrapolation is unavailable "
                "(measured numbers below are still exact)")

        say(f"measuring marginal cost of {len(dims)} dimensions …")
        marginal: dict[str, float] = {}
        for col in dims:
            rest = [c for c in dims if c != col]
            k_rest = _combos(conn, rest, with_time=False) or 1
            marginal[col] = k_all / k_rest

        hits = _group_by_hits(meta_engine, source.id)
        dim_stats = [
            DimStat(column=col,
                    distinct=_combos(conn, [col], with_time=False),
                    marginal_cost=marginal[col],
                    gb_hits=hits.get(col, 0))
            for col in dims
        ]

        # Greedy ladder: drop whatever costs most per unit of demonstrated
        # demand. The marginal costs are measured once against the full set
        # rather than re-measured after every drop — an O(n) approximation
        # instead of O(n^2), and more than accurate enough to rank columns.
        say("building the candidate ladder …")
        by_name = {c.name: c for c in source.fact_columns}
        rows_per_day_measured = rows_per_bucket * (86400 / interval_s)
        target_rows_per_bucket = (extrapolate_to * (interval_s / 3600.0)
                                  if extrapolate_to else rows_per_bucket)
        rows_per_day_assumed = target_rows_per_bucket * (86400 / interval_s)

        measure_width = sum(m.bytes_per_row for m in _measure_stats(source)
                            if m.derivable)
        # Drop order: cost against demonstrated demand, both on a log scale.
        # A plain ratio does not work — marginal cost is multiplicative and
        # hit counts are additive, so `cost / hits` lets an unused column that
        # costs nothing (cost 1.0) outrank one that multiplies the table by
        # five. On logs, a column that costs nothing scores exactly zero and
        # is dropped last no matter how unused it is, which is the behaviour
        # an operator expects. The +2 keeps log(hits) defined at zero hits.
        order = sorted(
            dims,
            key=lambda c: (math.log(max(marginal[c], 1.0)) / math.log(2 + hits.get(c, 0)),
                           marginal[c]),
            reverse=True)
        stages: list[Stage] = []
        current = list(dims)
        dropped: list[str] = []
        while True:
            combos = _combos(conn, current) / max(1, buckets)
            k_cur = _combos(conn, current, with_time=False)
            projected = _project(combos, rows_per_bucket,
                                 target_rows_per_bucket, alpha, k_cur)
            rows_day = projected * (86400 / interval_s)
            width = _ROW_OVERHEAD_BYTES + measure_width + sum(
                _TYPE_BYTES.get(by_name[c].pg_type, 4) for c in current
                if c in by_name)
            stages.append(Stage(
                columns=tuple(current), dropped=tuple(dropped),
                combos_per_bucket=projected,
                rows_per_day=rows_day,
                bytes_per_day=rows_day * width,
                factor=(rows_per_day_assumed / rows_day) if rows_day else 0.0,
            ))
            nxt = next((c for c in order if c in current), None)
            if nxt is None or len(current) <= 1:
                break
            current = [c for c in current if c != nxt]
            dropped = dropped + [nxt]

    return Advice(
        datasource=source.name,
        interval_s=interval_s,
        window_from=t_from, window_to=t_to,
        rows_in_window=rows_in_window,
        buckets_in_window=buckets,
        rows_per_bucket=rows_per_bucket,
        rows_per_day_measured=rows_per_day_measured,
        rows_per_day_assumed=rows_per_day_assumed,
        saturation_exponent=alpha,
        dims=dim_stats,
        stages=stages,
        measures=_measure_stats(source),
        warnings=warnings,
        notes=notes,
    )


def _project(combos_at_measured: float, rows_measured: float,
             rows_target: float, alpha: Optional[float], k: int) -> float:
    """Combinations per bucket at the target density.

    Identity when no extrapolation was asked for — the measurement is then
    the answer and nothing here can improve on it.

    Otherwise a power-law step, capped at the row count. The cap is *not* K,
    which was the obvious choice and is wrong: K is the distinct tuples inside
    the measured window, and a longer window keeps finding new ones (new
    merchants, new currencies). It is therefore a lower bound on the true
    asymptote, and capping with it would understate the combinations and so
    *overstate* the speed-up — error in the dangerous direction. Measured
    against a 27-day hand count, the K cap turned a factor of 14 into 48.

    Rows per bucket is the one bound that always holds: a bucket cannot
    contain more distinct tuples than it has rows. With it the estimate errs
    high on combinations and low on the factor, which is the safe direction
    for a sizing decision.
    """
    if rows_target <= 0 or rows_measured <= 0:
        return combos_at_measured
    if abs(rows_target - rows_measured) < 1e-9 or alpha is None:
        return float(combos_at_measured)
    scaled = combos_at_measured * (rows_target / rows_measured) ** alpha
    return min(scaled, rows_target)
