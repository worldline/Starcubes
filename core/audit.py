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
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import text

from .source import Source

logger = logging.getLogger(__name__)

# One bucket per row of a GUI table; more than this is unreadable anyway, and
# each bucket costs one round-trip to the (possibly remote) source DB.
MAX_BUCKETS = 200

# The fact-side count cannot use the BRIN index when watermark_fact_expr is a
# derived expression, so it scans the range. This is an on-demand tool, but
# uWSGI only has 8 request slots — an unbounded scan would hold one of them
# (CLAUDE.md, "Console concurrency").
DEFAULT_TIMEOUT_MS = 120_000


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


def _source_counts(source: Source,
                   edges: list[datetime]) -> list[Optional[int]]:
    """Row counts per bucket from the upstream source, one query each."""
    wm = source.watermark_column
    frm = source.base_table or f"({source.import_query}) q"
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


def gap_span(rows: list[dict]) -> Optional[tuple[str, str]]:
    """(start, end) covering every bucket with a gap, or None if clean."""
    gaps = [r for r in rows if (r.get("missing") or 0) > 0]
    if not gaps:
        return None
    return gaps[0]["start"], gaps[-1]["end"]
