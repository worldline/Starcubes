"""Source-vs-fact row-count reconciliation over a time window.

The ETL loads `[last_end_time, max(watermark_column))` and advances the high
watermark to that max. The source does not guarantee that rows become visible
in watermark order, so a row surfacing late — with a watermark below the
HWM — drops out of every future window. `late_arrival_overlap_s` re-fetches a
bounded span on every run and catches the common case, but any *bounded*
overlap can be exceeded.

This module is the net underneath: it counts rows on both sides per time
bucket and reports the difference, which is also the measurement that tells an
operator what `late_arrival_overlap_s` should actually be. Gaps it finds are
closed by `core/etl.py:import_fact(..., backfill=True)`.

Two modes:

  * ``audit_window`` — counts per bucket. Cheap (the DBs aggregate, nothing
    but the totals crosses the wire) and unbounded in window size, so it is
    the sweep that tells you *where* to look.
  * ``audit_ids`` — pulls both PK sets and takes the set difference, so it
    names the exact rows. It also closes a real blind spot in counting: a
    bucket that lost 100 rows and had 100 deleted upstream nets to zero and
    reads as clean. Bounded by RAM, hence ``MAX_AUDIT_IDS``.

The two sides are counted differently on purpose:

  * Fact side — one query, bucketed server-side by integer division on the
    epoch. One scan covers every bucket.
  * Source side — N plain `count(*)` range queries over `watermark_column`.
    Deliberately no `date_trunc`/`TRUNC`: that spelling is dialect-specific
    (the open multi-dialect WHERE item in CLAUDE.md's backlog), while a bare
    range predicate works everywhere and rides the existing index on the
    watermark column.

No Django imports — same constraint as the rest of `core/`.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np
from sqlalchemy import text

from .source import Source
from .utils import format_pks

logger = logging.getLogger(__name__)

# One bucket per row of a GUI table; more than this is unreadable anyway, and
# each bucket costs one round-trip to the (possibly remote) source DB.
MAX_BUCKETS = 200

# The fact-side count cannot use the BRIN index when watermark_fact_expr is a
# derived expression, so it scans the range. This is an on-demand tool, but
# uWSGI only has 8 request slots — an unbounded scan would hold one of them
# (CLAUDE.md, "Console concurrency").
DEFAULT_TIMEOUT_MS = 120_000

# Per-side ceiling on the ID comparison. Set from measurement, not from the
# 8 B an int64 nominally costs: end to end -- driver batch, numpy array, and
# setdiff1d's sort -- an id costs ~46 B (see _fetch_id_array for the full
# table), so 2M/side is roughly 190 MB across both sides. That is a
# defensible share of one of uWSGI's 8 request slots; beyond it, refuse
# loudly rather than let a worker get OOM-killed.
#
# For contrast, a Python set of the same ids measures 73 B/id on its own,
# which is why this path uses arrays. The ETL's overlap filter still uses a
# set: there the access pattern is repeated per-chunk membership tests, and
# a hash table is worth its overhead.
MAX_AUDIT_IDS = int(os.environ.get("STARCUBES_MAX_AUDIT_IDS", 2_000_000))

# How many ids travel back to the browser. The counts are always exact; this
# only caps the list rendered in the table.
DEFAULT_ID_SAMPLE = 200

# Rows per server-side cursor batch when streaming ids. Only one batch of
# driver tuples is alive at a time, which is the whole point -- see
# _fetch_id_array.
_FETCH_CHUNK = 100_000


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def audit_window(source: Source, dest_engine, *,
                 start: datetime, end: datetime, buckets: int = 24,
                 timeout_ms: int = DEFAULT_TIMEOUT_MS) -> list[dict]:
    """Compare source and fact row counts across `[start, end)`.

    Returns one dict per bucket: ``{start, end, source_rows, fact_rows,
    missing, surplus}``. ``missing`` is ``source_rows - fact_rows`` clamped at
    zero; a negative difference (duplicates, or rows deleted upstream after
    they were loaded) is reported as ``surplus`` instead. ``source_rows`` is
    None for a bucket whose source query failed.
    """
    start = _as_utc(start)
    end = _as_utc(end)
    if end <= start:
        raise ValueError("audit window end must be after start")

    buckets = max(1, min(int(buckets), MAX_BUCKETS))
    span_s = (end - start).total_seconds()
    width_s = span_s / buckets
    if width_s < 1:
        raise ValueError(
            f"window of {span_s:.0f}s split into {buckets} buckets gives "
            f"sub-second buckets; use fewer buckets or a wider window")

    edges = [start + timedelta(seconds=width_s * i) for i in range(buckets)]
    edges.append(end)

    fact_counts = _fact_counts(source, dest_engine, start, end,
                               width_s, buckets, timeout_ms)
    source_counts = _source_counts(source, edges)

    out = []
    for i in range(buckets):
        src = source_counts[i]
        fct = fact_counts.get(i, 0)
        diff = None if src is None else src - fct
        out.append({
            "start": edges[i].isoformat(),
            "end": edges[i + 1].isoformat(),
            "source_rows": src,
            "fact_rows": fct,
            "missing": max(0, diff) if diff is not None else None,
            "surplus": max(0, -diff) if diff is not None else None,
        })
    return out


def _fact_counts(source: Source, dest_engine, start: datetime, end: datetime,
                 width_s: float, buckets: int,
                 timeout_ms: int) -> dict[int, int]:
    """Row counts per bucket index, in one pass over the fact table."""
    wm_expr = source.watermark_fact_sql()
    sql = text(
        f"SELECT floor((extract(epoch from {wm_expr}) "
        f"              - extract(epoch from CAST(:start AS timestamptz))) "
        f"             / :width)::int AS bucket, "
        f"       count(*) AS n "
        f"  FROM {source.schema_name}.fact "
        f" WHERE {wm_expr} >= :start AND {wm_expr} < :end "
        f" GROUP BY 1"
    )
    with dest_engine.begin() as conn:
        conn.execute(text("SELECT set_config('statement_timeout', :ms, true)"),
                     {"ms": str(int(timeout_ms))})
        rows = conn.execute(
            sql, {"start": start, "end": end,
                  "width": width_s}).mappings().all()
    # Float division can land a row sitting exactly on `end` in bucket
    # == buckets; fold it back into the last real bucket.
    counts: dict[int, int] = {}
    for r in rows:
        b = min(max(int(r["bucket"]), 0), buckets - 1)
        counts[b] = counts.get(b, 0) + int(r["n"])
    return counts


def _source_from(source: Source) -> str:
    """FROM-clause for the upstream side of an audit.

    Prefers `base_table`, where the raw watermark column is in scope. A
    datasource that only carries an `import_query` works too, but only if
    that query projects the watermark -- it is allowed not to (see the
    watermark_column contract), and the resulting error is worth naming
    here rather than surfacing as a raw SQL failure.
    """
    if source.base_table:
        return source.base_table
    if source.watermark_column not in source.import_query:
        raise ValueError(
            f"{source.name}: auditing needs either base_table set, or an "
            f"import_query that projects the watermark column "
            f"({source.watermark_column}) so the window can be filtered.")
    return f"({source.import_query}) q"


def _source_counts(source: Source,
                   edges: list[datetime]) -> list[Optional[int]]:
    """Row counts per bucket from the upstream source, one query each."""
    wm = source.watermark_column
    frm = _source_from(source)
    sql = text(f"SELECT count(*) FROM {frm} "
               f" WHERE {wm} >= :a AND {wm} < :b")

    out: list[Optional[int]] = []
    engine = source.source_engine()
    with engine.begin() as conn:
        for i in range(len(edges) - 1):
            # The source reads its watermark in its own wall-clock time, the
            # same convention the ETL's WHERE clause uses — hand it a naive
            # timestamp rather than an offset-aware one.
            a = edges[i].replace(tzinfo=None)
            b = edges[i + 1].replace(tzinfo=None)
            try:
                out.append(
                    int(conn.execute(sql, {"a": a, "b": b}).scalar() or 0))
            except Exception:
                # One unreadable bucket must not discard the other 23; the
                # GUI renders a null as "?" rather than as a bogus gap.
                logger.exception("%s: source count failed for [%s, %s)",
                                 source.name, a, b)
                out.append(None)
    return out


def _source_pk_sql(source: Source) -> str:
    """Projection that yields the PK *as it was stored in fact*.

    Not the raw source column: `fact_column.source_expr` is what the ETL
    applied on the way in (the example source stores `to_number(txn_id)`,
    not the text `txn_id`), so comparing against the raw column would
    report every row as missing.
    """
    pk = source.pk_column()
    if pk is None:
        raise ValueError(
            f"{source.name}: comparing ids needs a fact column with "
            f"role='pk'. Use the count-based audit instead.")
    if source.base_table:
        col = next((c for c in source.fact_columns if c.name == pk), None)
        if col is not None and col.source_expr:
            return col.source_expr
    return pk


def _coerce_ids(values: list, n: int) -> np.ndarray:
    """One batch of PK values as a numpy array, int64 where possible.

    Oracle NUMBER arrives as float or Decimal depending on driver and scale,
    and float64 silently loses precision above 2^53 — so anything that does
    not round-trip through int64 exactly is compared as strings instead.
    Slower and heavier, but never wrong.
    """
    try:
        return np.fromiter((int(v) for v in values), dtype=np.int64, count=n)
    except (TypeError, ValueError, OverflowError):
        return np.fromiter((str(v) for v in values), dtype=object, count=n)


def _fetch_id_array(engine, sql: str, params: dict, label: str,
                    limit: int, timeout_ms: Optional[int] = None) -> np.ndarray:
    """Stream one column of PKs into a numpy array.

    Measured over 1M int64 PKs, peak RSS per id:

        pd.read_sql                                264 B
        Connection.execute(...).all()              193 B
        stream_results + Result.partitions()       193 B
        stream_results + Result.fetchmany()         46 B   <- this
        raw psycopg2 named cursor + fetchmany     31.5 B

    `stream_results` has to sit on the *connection*, and the batches have to
    be pulled with `fetchmany`: `partitions()` measured identical to a fully
    buffered fetch, so it is not a streaming read despite the flag. Folding
    each batch straight into an int64 array keeps only one batch of driver
    tuples alive; the array itself is the 8 B/id numpy needs, and the
    remainder is that live batch plus the transient doubling at the final
    concatenate.

    The raw named cursor is faster still, but it is psycopg2-only — this
    same function reads the (possibly Oracle) source side, where
    `stream_results` maps onto the driver's arraysize instead.
    """
    chunks: list[np.ndarray] = []
    total = 0
    with engine.connect().execution_options(
            stream_results=True, max_row_buffer=_FETCH_CHUNK) as conn:
        if timeout_ms is not None:
            conn.execute(text(
                "SELECT set_config('statement_timeout', :ms, true)"),
                {"ms": str(int(timeout_ms))})
        result = conn.execute(text(sql), params)
        while True:
            batch = result.fetchmany(_FETCH_CHUNK)
            if not batch:
                break
            total += len(batch)
            if total > limit:
                result.close()
                raise ValueError(
                    f"{label} side holds more than {limit} rows in this "
                    f"window. Narrow the window, or raise "
                    f"STARCUBES_MAX_AUDIT_IDS.")
            chunks.append(_coerce_ids([r[0] for r in batch], len(batch)))
    if not chunks:
        return np.empty(0, dtype=np.int64)
    # A mixed run (one batch int64, a later one object) would compare wrong;
    # normalise to strings rather than let numpy pick a common dtype.
    if len({c.dtype.kind for c in chunks}) > 1:
        chunks = [c.astype(str).astype(object) for c in chunks]
    return np.concatenate(chunks)


def audit_ids(source: Source, dest_engine, *, start: datetime, end: datetime,
              sample: int = DEFAULT_ID_SAMPLE,
              max_ids: int = MAX_AUDIT_IDS,
              timeout_ms: int = DEFAULT_TIMEOUT_MS) -> dict:
    """Name the rows that differ between source and fact over `[start, end)`.

    Both sides' primary keys are pulled into memory and diffed. Returns
    exact counts plus up to `sample` ids of each kind:

      missing   — in the source, not in fact. What the ETL lost.
      surplus   — in fact, not in the source. Deleted upstream after being
                  loaded, or loaded into the wrong window.
      duplicate — the same PK stored more than once in fact. There is no
                  unique constraint to prevent it, and the count-based
                  audit cannot see it at all.
    """
    start = _as_utc(start)
    end = _as_utc(end)
    if end <= start:
        raise ValueError("audit window end must be after start")

    pk = source.pk_column()
    pk_sql = _source_pk_sql(source)
    wm = source.watermark_column
    wm_expr = source.watermark_fact_sql()

    src_ids = _fetch_id_array(
        source.source_engine(),
        f"SELECT {pk_sql} AS pk FROM {_source_from(source)} "
        f" WHERE {wm} >= :a AND {wm} < :b",
        # The source reads its watermark in its own wall-clock time, the
        # same convention the ETL's WHERE clause uses.
        {"a": start.replace(tzinfo=None), "b": end.replace(tzinfo=None)},
        "source", max_ids)

    fact_ids = _fetch_id_array(
        dest_engine,
        f"SELECT {pk} AS pk FROM {source.schema_name}.fact "
        f" WHERE {wm_expr} >= :a AND {wm_expr} < :b",
        {"a": start, "b": end}, "fact", max_ids, timeout_ms=timeout_ms)

    # A dtype split (int64 on one side, str on the other) would make every
    # id look missing, so fall back to strings for both.
    if src_ids.dtype != fact_ids.dtype and len(src_ids) and len(fact_ids):
        src_ids = src_ids.astype(str).astype(object)
        fact_ids = fact_ids.astype(str).astype(object)

    fact_unique = np.unique(fact_ids)
    missing = np.setdiff1d(np.unique(src_ids), fact_unique, assume_unique=True)
    surplus = np.setdiff1d(fact_unique, np.unique(src_ids), assume_unique=True)

    def head(arr):
        raw = [str(v) for v in arr[:sample]]
        return raw, format_pks(raw, source.pk_format)

    missing_raw, missing_display = head(missing)
    surplus_raw, surplus_display = head(surplus)

    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "source_rows": int(len(src_ids)),
        "fact_rows": int(len(fact_ids)),
        "fact_distinct": int(len(fact_unique)),
        "duplicate_rows": int(len(fact_ids) - len(fact_unique)),
        "missing_count": int(len(missing)),
        "missing_ids": missing_raw,
        "missing_display": missing_display,
        "surplus_count": int(len(surplus)),
        "surplus_ids": surplus_raw,
        "surplus_display": surplus_display,
        "sample": int(sample),
        "truncated": bool(len(missing) > sample or len(surplus) > sample),
    }


def gap_span(rows: list[dict]) -> Optional[tuple[str, str]]:
    """(start, end) covering every bucket with a gap, or None if clean."""
    gaps = [r for r in rows if (r.get("missing") or 0) > 0]
    if not gaps:
        return None
    return gaps[0]["start"], gaps[-1]["end"]
