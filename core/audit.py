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
  * ``audit_id_details`` — a targeted follow-up on ids ``audit_ids`` already
    named. It fetches each row's source watermark and pairs it with the
    ``etl_run`` whose window covered that watermark, turning "these rows are
    missing" into "the ETL read that window N seconds after their watermark
    and still did not see them". That number is what ``wallclock_lag_s``
    gets set from.

The two sides are counted differently on purpose:

  * Fact side — one query, bucketed server-side by integer division on the
    epoch. One scan covers every bucket.
  * Source side — N plain `count(*)` range queries over `watermark_column`.
    Deliberately no `date_trunc`/`TRUNC`: that spelling is dialect-specific
    (the open multi-dialect WHERE item in `docs/backlog.md`), while a bare
    range predicate works everywhere and rides the existing index on the
    watermark column.

No Django imports — same constraint as the rest of `core/`.
"""
from __future__ import annotations

import json as _json
import logging
import os
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
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
# (`docs/backlog.md`, "Console concurrency").
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

# How many missing ids an audit will hand to a targeted backfill. Past this
# the run records ids_truncated and the operator falls back to a window
# backfill.
#
# Not thrift. The targeted extract chunks its IN-list at 1000 for Oracle, so
# 100k ids are a hundred round trips -- still far cheaper than re-reading a
# window of a million rows, but the curve is heading the wrong way, and once
# the gap approaches the size of the window the window scan simply wins.
# Repairing *part* of a gap and reporting success would be worse than
# refusing, which is why the cap sets a flag rather than silently slicing.
BACKFILL_IDS_MAX = int(os.environ.get("STARCUBES_BACKFILL_IDS_MAX", 100_000))


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _clamp_to_floor(source: Source, start: datetime,
                    end: datetime) -> datetime:
    """Raise `start` to the datasource's retention floor.

    Below the floor the fact table is empty on purpose (a dropped or
    truncated partition), so comparing it against the source would report
    100% loss over a window in which nothing was lost. Retention is not a
    drop loop — it is a floor concept, and this is the audit learning
    about it.
    """
    floor = getattr(source, "retention_floor", None)
    if floor is None:
        return start
    floor = _as_utc(floor)
    if end <= floor:
        raise ValueError(
            f"the whole window lies below the retention floor {floor} — "
            f"those rows were disposed of on purpose, so there is nothing "
            f"to reconcile")
    if start < floor:
        logger.info("%s: audit window starts below the retention floor "
                    "%s — clamping", source.name, floor)
        return floor
    return start


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
    start = _clamp_to_floor(source, start, end)

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
              full_ids: int = 0,
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

    `full_ids > 0` additionally returns `missing_all` (the complete missing
    list, capped at that many, plus `missing_all_truncated`) and `lag_sample`
    (`sample` ids spread evenly across the whole missing set). The first is
    what a targeted backfill consumes; the second is what stage three of
    `run_audit` measures lateness on, and it is *not* the head of the list --
    see `sample_evenly` for why that would be biased. Both are opt-in because
    the browser has no use for either. `sample` still governs what
    `missing_ids` and the `*_display` lists hold, so no existing caller sees
    a change.
    """
    start = _as_utc(start)
    end = _as_utc(end)
    if end <= start:
        raise ValueError("audit window end must be after start")
    start = _clamp_to_floor(source, start, end)

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

    result = {
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
    if full_ids > 0:
        # str() and not the native type: a PK is not always an integer, and
        # this list round-trips through JSON on its way into audit_run.
        # _bind_pk puts it back in its native form at the other end.
        result["missing_all"] = [str(v) for v in missing[:full_ids]]
        result["missing_all_truncated"] = bool(len(missing) > full_ids)
        # Spread across the *whole* missing set, and taken here rather than
        # from `missing_all` on purpose: that list is capped, so sampling it
        # would inherit the cap's head bias on exactly the runs where the gap
        # is largest. See `sample_evenly`.
        result["lag_sample"] = sample_evenly(missing, sample)
    return result


def gap_span(rows: list[dict]) -> Optional[tuple[str, str]]:
    """(start, end) covering every bucket with a gap, or None if clean."""
    gaps = [r for r in rows if (r.get("missing") or 0) > 0]
    if not gaps:
        return None
    return gaps[0]["start"], gaps[-1]["end"]


def _norm_pk(v) -> Optional[str]:
    """One PK value in the same string form `audit_ids` hands back.

    `audit_ids` returns `str(v)` over an int64/object numpy array, while the
    source driver may hand the same id back as int, float or Decimal
    depending on dialect and column scale. Comparing those directly would
    make every row look unmatched, so both sides go through here.
    """
    if v is None:
        return None
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, int):
        return str(v)
    if isinstance(v, (float, Decimal)):
        try:
            iv = int(v)
            if iv == v:
                return str(iv)
        except (ValueError, OverflowError, InvalidOperation):
            pass
    return str(v)


def _bind_pk(raw: str):
    """Bind a PK back in its native type where possible.

    `<pk_expr>` is numeric for the example source (`to_number(txn_id)`), and
    binding a string against it would force a per-row cast on the source
    side. Falls back to the string when the id is not an integer.
    """
    try:
        return int(raw)
    except (TypeError, ValueError):
        return raw


# Rungs for the "what would I have to add" ladder, in seconds. Round
# durations an operator would actually type into a config field, not
# percentiles: the question being answered is "what does this much reach buy
# me", and the answer has to be readable as a trade-off.
_LAG_LADDER_RUNGS = (30, 60, 300, 900, 3600, 21600, 86400)


def lag_ladder(lags: list[float]) -> list[dict]:
    """`[{add_s, caught_pct}]` — what each increment would have caught.

    Deliberately this direction and not percentiles-in-seconds. The operator
    is choosing a compromise between freshness and re-reads, and the question
    that decision turns on is "how much coverage does this much cost me",
    which a percentile answers backwards.

    Stops one rung past the worst observed lateness: further rungs all read
    100% and say nothing. Always ends on a rung that reaches everything, so
    the ladder has a visible ceiling rather than trailing off.
    """
    if not lags:
        return []
    arr = np.asarray(lags, dtype=np.float64)
    worst = float(arr.max())
    out = []
    for rung in _LAG_LADDER_RUNGS:
        out.append({"add_s": rung,
                    "caught_pct": round(float((arr <= rung).mean()) * 100, 1)})
        if rung >= worst:
            break
    else:
        # Later than the coarsest rung. Name the actual figure rather than
        # leave the ladder implying a day is enough.
        out.append({"add_s": int(np.ceil(worst)), "caught_pct": 100.0})
    return out


def sample_evenly(arr, n: int) -> list:
    """`n` values spread across `arr`, not its head.

    `np.setdiff1d` returns its result **sorted**, so `missing[:n]` is the n
    lowest primary keys. With the usual monotonic key that is the n oldest
    missing rows -- a systematically biased sample for anything that asks how
    late rows arrive, which is exactly what stage three asks.

    Harmless where `audit_ids` uses the head: that list is for display.
    """
    if len(arr) <= n:
        return [str(v) for v in arr]
    step = len(arr) / float(n)
    return [str(arr[int(i * step)]) for i in range(n)]


def audit_due(meta_engine, datasource_id: int) -> bool:
    """True when `audit_interval_s` has elapsed since the last audit.

    A copy of `core/housekeeping.py:housekeeping_due` in shape and in
    reasoning, including the part that is easy to get wrong: the interval is
    read *fresh* from the metadata on every call rather than off the `Source`,
    because a forever session loads its `Source` once before the loop and
    would otherwise never see an interval edited in the GUI mid-session.

    Unlike housekeeping, the default is off (0). Housekeeping is catalogue
    reads, an ANALYZE and a BRIN summarise; an audit reads the *source* and
    the warehouse across a window, and that is not something to switch on for
    everyone by default.
    """
    with meta_engine.begin() as meta:
        row = meta.execute(text(
            "SELECT d.audit_interval_s AS iv, "
            "       (SELECT max(started_at) FROM starcubes_meta.audit_run a "
            "         WHERE a.datasource_id = d.id) AS last "
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
    return (datetime.now(timezone.utc) - _as_utc(last)).total_seconds() >= interval


def audit_window_seconds(meta_engine, datasource_id: int) -> int:
    """How far back a periodic audit should look, in seconds.

    `audit_window_s` when set, else `audit_interval_s`. The fallback is the
    point: an operator who sets only the interval still gets a window that
    reaches back at least to the previous run, so consecutive audits cannot
    leave an unexamined strip between them. Setting the window *wider* than
    the interval is the normal configuration -- late arrivals are the thing
    being hunted, and they land behind the tip.
    """
    with meta_engine.begin() as meta:
        row = meta.execute(text(
            "SELECT audit_window_s AS w, audit_interval_s AS iv "
            "  FROM starcubes_meta.datasource WHERE id = :ds"
        ), {"ds": datasource_id}).mappings().first()
    if not row:
        return 0
    return int(row["w"] or 0) or int(row["iv"] or 0)


def run_audit(source: Source, dest_engine, meta_engine, *,
              start: datetime, end: datetime,
              buckets: int = 24, trigger: str = "manual",
              stage_one: bool = True,
              full_ids: int = BACKFILL_IDS_MAX,
              timeout_ms: int = DEFAULT_TIMEOUT_MS) -> dict:
    """Audit `[start, end)` in two stages and record the result.

    Stage one is `audit_window`: counts, bucketed, one cheap query per side.
    Stage two is `audit_ids`, and it runs **only over the buckets
    `gap_span` names** -- it pulls both sides' primary keys into memory, and
    the normal answer to "did we lose anything" is no. This mirrors what an
    operator does by hand in the Audit tab, which is where the two-stage
    shape came from.

    `gap_span` has been sitting in this module without a caller since it was
    written; this is the caller it was written for.

    Writes one `starcubes_meta.audit_run` row and returns it as a dict. The
    row is the point, not a side effect: it is simultaneously the "result
    stored per run" the periodic-audit backlog item asked for and the staging
    row the targeted backfill reads its id list from
    (`etl_run --backfill --audit-run <id>`).

    `stage_one=False` goes straight to the id comparison over the given
    window. That is the GUI's "Compare IDs" button: the operator has already
    decided this window is worth the expensive read, so bucketing it first
    would be work done to answer a question already answered. It still writes
    an `audit_run` row, which is what gives that button's result an id the
    targeted backfill can be pointed at.

    Never raises for a finding. A stage that fails is recorded with
    status='failed' and the error text, because a periodic audit that throws
    would take the forever loop's iteration with it.
    """
    start = _as_utc(start)
    end = _as_utc(end)
    if end <= start:
        raise ValueError("audit window end must be after start")

    with meta_engine.begin() as meta:
        run_id = meta.execute(text(
            "INSERT INTO starcubes_meta.audit_run "
            "  (datasource_id, trigger, status, started_at, "
            "   window_start, window_end) "
            "VALUES (:ds, :t, 'running', now(), :a, :b) RETURNING id"
        ), {"ds": source.id, "t": trigger, "a": start, "b": end}).scalar()

    result: dict = {"id": run_id, "status": "ok", "trigger": trigger,
                    "window_start": start.isoformat(),
                    "window_end": end.isoformat(),
                    "missing_count": 0, "surplus_count": 0,
                    "duplicate_rows": 0, "missing_ids": None,
                    "ids_truncated": False, "error": None,
                    "needed_lag_p99_s": None, "needed_lag_median_s": None,
                    "needed_lag_max_s": None, "needed_lag_n": None}
    try:
        if not stage_one:
            rows = []
            span = (start.isoformat(), end.isoformat())
            unreadable = 0
        else:
            rows = audit_window(source, dest_engine, start=start, end=end,
                                buckets=buckets, timeout_ms=timeout_ms)

            # `audit_window` answers an unreadable bucket with
            # source_rows=None rather than discarding the other 23 -- right
            # for one bucket, and a trap for the run as a whole. With *every*
            # bucket unreadable the comparison learned nothing, and recording
            # that as ok/missing=0 would hand `audit_gap` a clean bill of
            # health from a run that never saw the source: the alarm would
            # read zero and close a standing episode. Silence is not
            # recovery, so this is a failure.
            unreadable = sum(1 for r in rows if r["source_rows"] is None)
            if unreadable == len(rows):
                raise RuntimeError(
                    f"the source was unreadable across all {len(rows)} "
                    f"bucket(s) -- the audit compared nothing. See the "
                    f"per-bucket errors logged above.")

            result["source_rows"] = sum(r["source_rows"] or 0 for r in rows)
            result["fact_rows"] = sum(r["fact_rows"] or 0 for r in rows)
            if unreadable:
                # Partially readable: the counts are real for the buckets
                # that answered, but a gap sitting in one that did not is
                # invisible, and `gap_span` cannot see it either (a None
                # bucket has no `missing`). Recorded so the number is not
                # read as a statement about the whole window.
                logger.warning(
                    "%s: %d of %d audit bucket(s) were unreadable — the "
                    "result covers only the rest of the window",
                    source.name, unreadable, len(rows))
            span = gap_span(rows)

        result["unreadable_buckets"] = unreadable
        if span is not None:
            # Narrow to the buckets that actually disagree. Stage two over
            # the whole window would read both sides in full to learn what
            # stage one already said about the clean part of it.
            ids = audit_ids(source, dest_engine,
                            start=_as_utc(datetime.fromisoformat(span[0])),
                            end=_as_utc(datetime.fromisoformat(span[1])),
                            full_ids=full_ids, timeout_ms=timeout_ms)
            result["missing_count"] = ids["missing_count"]
            result["surplus_count"] = ids["surplus_count"]
            result["duplicate_rows"] = ids["duplicate_rows"]
            result["missing_ids"] = ids.get("missing_all") or None
            result["ids_truncated"] = bool(ids.get("missing_all_truncated"))
            # In memory only -- the caller (the GUI endpoint) renders it; the
            # stored copy below drops `missing_all`, which can be 100k ids.
            result["ids"] = ids
            result["source_rows"] = ids["source_rows"]
            result["fact_rows"] = ids["fact_rows"]
            result["report"] = {"buckets": rows, "gap_span": list(span),
                                "unreadable_buckets": unreadable,
                                "ids": {k: v for k, v in ids.items()
                                        if k != "missing_all"}}

            # Stage three: *why* they were missing. A row is absent because
            # it arrived after the high watermark had already passed its
            # watermark value, and `audit_id_details` measures exactly that
            # distance. Guarded separately -- it is the diagnosis, and losing
            # it must not lose the finding itself, which is what actually
            # repairs the data.
            try:
                det = audit_id_details(
                    source, meta_engine,
                    ids=ids.get("lag_sample") or ids["missing_ids"],
                    start=_as_utc(datetime.fromisoformat(span[0])),
                    end=_as_utc(datetime.fromisoformat(span[1])))
                needed = det.get("needed_lag")
                if needed:
                    result["needed_lag_p99_s"] = needed["p99"]
                    result["needed_lag_median_s"] = needed["median"]
                    result["needed_lag_max_s"] = needed["max"]
                    result["needed_lag_n"] = needed["n"]
                    result["report"]["needed_lag"] = needed
                    result["report"]["lag_ladder"] = det["lag_ladder"]
                    logger.warning(
                        "%s: those rows arrived up to %.0fs after the window "
                        "had passed them (median %.0fs, p99 %.0fs over %d "
                        "resolved) — raising watermark_lag_s or "
                        "late_arrival_overlap_s by that much would have "
                        "caught them",
                        source.name, needed["max"], needed["median"],
                        needed["p99"], needed["n"])
                else:
                    # Nothing resolved: every sampled row either vanished
                    # from the source or has no identifiable covering run.
                    # Say so rather than leave the columns NULL silently.
                    result["report"]["needed_lag"] = None
                    logger.info(
                        "%s: lateness not resolvable for any sampled row "
                        "(%d not in source, %d without a covering run)",
                        source.name, det["not_in_source"], det["unresolved"])
            except Exception:
                logger.exception(
                    "%s: measuring how late those rows were failed — the gap "
                    "itself is still recorded", source.name)
            logger.warning(
                "%s: audit found %d missing, %d surplus, %d duplicate row(s) "
                "in [%s, %s)", source.name, ids["missing_count"],
                ids["surplus_count"], ids["duplicate_rows"], span[0], span[1])
        else:
            result["report"] = {"buckets": rows, "gap_span": None,
                                "unreadable_buckets": unreadable}
            logger.info("%s: audit clean over [%s, %s) — %d row(s) both sides",
                        source.name, start, end, result["source_rows"])
    except Exception as exc:
        logger.exception("%s: audit failed", source.name)
        result["status"] = "failed"
        result["error"] = str(exc)
        # NULL, not 0. `_audit_gap` filters on `missing_count IS NOT NULL`
        # precisely so a failed run cannot be read as "nothing is missing".
        result["missing_count"] = None
        result["surplus_count"] = None
        result["duplicate_rows"] = None

    with meta_engine.begin() as meta:
        meta.execute(text(
            "UPDATE starcubes_meta.audit_run "
            "   SET status = :s, finished_at = now(), "
            "       source_rows = :sr, fact_rows = :fr, "
            "       missing_count = :mc, surplus_count = :sc, "
            "       duplicate_rows = :dr, "
            "       missing_ids = CAST(:mi AS jsonb), "
            "       ids_truncated = :tr, "
            "       needed_lag_p99_s = :lp99, "
            "       needed_lag_median_s = :lmed, "
            "       needed_lag_max_s = :lmax, "
            "       needed_lag_n = :ln, "
            "       report = CAST(:rep AS jsonb), error = :err "
            " WHERE id = :id"
        ), {"id": run_id, "s": result["status"],
            "sr": result.get("source_rows"), "fr": result.get("fact_rows"),
            "mc": result["missing_count"], "sc": result["surplus_count"],
            "dr": result["duplicate_rows"],
            "mi": (_json.dumps(result["missing_ids"])
                   if result["missing_ids"] else None),
            "tr": result["ids_truncated"],
            "lp99": result["needed_lag_p99_s"],
            "lmed": result["needed_lag_median_s"],
            "lmax": result["needed_lag_max_s"],
            "ln": result["needed_lag_n"],
            "rep": (_json.dumps(result.get("report"))
                    if result.get("report") is not None else None),
            "err": result["error"]})
    return result


def backfill_ids_for_run(meta_engine, run_id: int) -> dict:
    """The stored id list for one audit run, with the reason it is unusable.

    Returns `{"ids": [...], "start": dt, "end": dt, "datasource_id": int}`,
    or raises ValueError naming why this run cannot drive a targeted
    backfill. Refusing is deliberate in the truncated case: repairing part of
    a gap and reporting success is worse than sending the operator to the
    window backfill that can repair all of it.
    """
    with meta_engine.begin() as meta:
        row = meta.execute(text(
            "SELECT datasource_id, status, window_start, window_end, "
            "       missing_ids, ids_truncated, missing_count "
            "  FROM starcubes_meta.audit_run WHERE id = :id"
        ), {"id": run_id}).mappings().first()
    if row is None:
        raise ValueError(f"audit run {run_id} does not exist")
    if row["status"] != "ok":
        raise ValueError(
            f"audit run {run_id} is {row['status']}, not ok — its id list "
            f"cannot be trusted")
    if row["ids_truncated"]:
        raise ValueError(
            f"audit run {run_id} found {row['missing_count']} missing rows, "
            f"more than the id cap (STARCUBES_BACKFILL_IDS_MAX). Use a window "
            f"backfill over [{row['window_start']}, {row['window_end']}) "
            f"instead — it repairs the whole gap.")
    ids = row["missing_ids"] or []
    if not ids:
        raise ValueError(
            f"audit run {run_id} found nothing missing — there is nothing to "
            f"backfill")
    return {"ids": [str(v) for v in ids],
            "start": _as_utc(row["window_start"]),
            "end": _as_utc(row["window_end"]),
            "datasource_id": int(row["datasource_id"])}


def audit_id_details(source: Source, meta_engine, *, ids: list,
                     start: datetime, end: datetime,
                     chunk: int = 1000) -> dict:
    """Source watermark + responsible ETL run for already-identified ids.

    Deliberately a *second* step on top of `audit_ids`, not part of it: the
    set difference is bounded by the ~46 B/id measured in `_fetch_id_array`
    and has to stay that cheap to be usable over a wide window. This runs
    only on the handful of ids that came back from it.

    What it answers that the id list alone cannot: *how much further back*
    the window would have had to reach. `audit_ids` says a row is missing;
    pairing the row's own watermark-column value with the `etl_run` whose
    window covered it gives

        needed_lag_s = etl_run.last_end_time - txn watermark value

    ``last_end_time`` is exactly the upper bound of the WHERE that run read
    the source with (`import_fact` builds ``WHERE wm >= ... AND wm <
    end_time`` and stores that same ``end_time`` here; ``first_end_time`` is
    the lower bound). So the difference reads: *the window's upper edge had
    already moved this far past the transaction, and it still was not
    visible.*

    **Both operands come from the source's own watermark column, and that is
    the whole point.** The obvious-looking alternative, ``started_at -
    watermark``, subtracts two different clocks: the watermark is source
    wall-clock time that `_as_utc` only *labels* as UTC, while
    ``etl_run.started_at`` is ``datetime.now(timezone.utc)`` on the starcubes
    host (`core/etl.py`). With a source writing in Europe/Berlin that is off
    by the full UTC offset and goes negative. ``started_at`` is still
    returned, for diagnostics ("when did this actually run"), but it must
    never enter the subtraction.

    Three properties worth knowing:

      * **It cannot be negative.** `covering()` only attributes a run when
        ``w < last_end_time``, so the difference is positive by
        construction. The clock-mixing version needed a sanity check here;
        this one does not.
      * **It is a lower bound**, not the answer. All that is known is that
        the row was missing at *that* run; when it actually became visible
        is unknowable from here.
      * **It sizes an increase of** ``watermark_lag_s``, not an absolute
        target. With lag ``L_old`` in force at the time,
        ``last_end_time = M - L_old`` (M = max(watermark) then), so catching
        the row needs ``L_new >= M - w = (last_end_time - w) + L_old``. In
        the usual case -- measuring *before* setting a lag, ``L_old = 0`` --
        the difference is the target outright, which is why the GUI says
        "raise by", not "set to".

    A row **no run covered** is excluded: that is a hole in the run history
    or a watermark jump, a different failure, so those are counted
    separately and kept out of the percentiles.

    When the wall-clock term set the bound (``last_end_time = src_now -
    wallclock_lag_s``) the number is still a valid lower bound -- it is what
    the WHERE compared against either way -- it just no longer maps onto one
    specific knob. Recording ``max_wm`` per run would disambiguate that, at
    the price of a column, without making the figure itself any sounder.

    No ``dest_engine`` parameter: unlike `audit_window` and `audit_ids`,
    this reads only the source and `starcubes_meta`. Fact was already
    established to *not* hold these rows.
    """
    start = _as_utc(start)
    end = _as_utc(end)
    if end <= start:
        raise ValueError("audit window end must be after start")
    ids = [str(i) for i in (ids or [])]
    if not ids:
        return {"rows": [], "resolved": 0, "unresolved": 0,
                "not_in_source": 0, "needed_lag": None}

    pk_sql = _source_pk_sql(source)
    wm = source.watermark_column

    # The window predicate stays in even though the ids alone would identify
    # the rows: <pk_expr> is an expression, so no index covers it, while the
    # watermark column is indexed and the window is already known. That
    # turns a full scan into an index range plus a filter.
    wm_by_pk: dict = {}
    engine = source.source_engine()
    with engine.begin() as conn:
        for i in range(0, len(ids), chunk):
            batch = ids[i:i + chunk]      # chunked at 1000 for Oracle's
            names = [f"p{j}" for j in range(len(batch))]   # IN-list limit
            params = {n: _bind_pk(v) for n, v in zip(names, batch)}
            # Naive timestamps: the source reads its watermark in its own
            # wall-clock time, same convention as _source_counts.
            params["a"] = start.replace(tzinfo=None)
            params["b"] = end.replace(tzinfo=None)
            placeholders = ", ".join(f":{n}" for n in names)
            rows = conn.execute(text(
                f"SELECT {pk_sql} AS pk, {wm} AS wm "
                f"  FROM {_source_from(source)} "
                f" WHERE {wm} >= :a AND {wm} < :b "
                f"   AND {pk_sql} IN ({placeholders})"
            ), params).all()
            for r in rows:
                key = _norm_pk(r[0])
                if key is not None and r[1] is not None:
                    wm_by_pk[key] = _as_utc(r[1])

    # Runs whose window overlaps [start, end). Only status='ok' incremental
    # runs: a failed run proves nothing about visibility, and a backfill
    # never had the chance to catch the row on its way in.
    #
    # A NULL first_end_time is not a broken row to skip -- it is how the
    # *initial* load of a datasource is recorded (`extract_from` is None,
    # so the window has no lower bound at all and everything below
    # last_end_time was in scope). Excluding those runs would report every
    # row from the first load as "no covering run", i.e. as a hole in the
    # run history, which is precisely the opposite of the truth. They are
    # kept and given an open lower bound instead.
    with meta_engine.begin() as meta:
        runs = [dict(r) for r in meta.execute(text(
            "SELECT id, started_at, finished_at, first_end_time, last_end_time "
            "  FROM starcubes_meta.etl_run "
            " WHERE datasource_id = :ds AND kind = 'incremental' "
            "   AND status = 'ok' AND last_end_time IS NOT NULL "
            "   AND last_end_time > :a "
            "   AND (first_end_time IS NULL OR first_end_time < :b) "
            " ORDER BY first_end_time NULLS FIRST"
        ), {"ds": source.id, "a": start, "b": end}).mappings().all()]
    open_lower = datetime.min.replace(tzinfo=timezone.utc)
    for r in runs:
        for k in ("started_at", "finished_at", "first_end_time", "last_end_time"):
            if r[k] is not None:
                r[k] = _as_utc(r[k])
        if r["first_end_time"] is None:
            r["first_end_time"] = open_lower
    firsts = [r["first_end_time"] for r in runs]

    def covering(w):
        """(last run whose window contains w, how many such runs there are).

        The *last* one, not the first: the row is still missing, so every
        covering run missed it, and the latest one yields the tightest lower
        bound on the lag that would have been needed. With
        late_arrival_overlap_s = 0 the windows do not overlap and there is
        only ever one.
        """
        hit, n = None, 0
        for idx in range(bisect_right(firsts, w) - 1, -1, -1):
            if runs[idx]["last_end_time"] > w:
                n += 1
                if hit is None:
                    hit = runs[idx]
        return hit, n

    display = format_pks(ids, source.pk_format)
    out, lags, unresolved, not_in_source = [], [], 0, 0
    for raw, disp in zip(ids, display):
        w = wm_by_pk.get(raw)
        # `txn_wm` is the row's own watermark-column value (end_time for the
        # example source). `run_watermark` is the run's -- the upper bound of
        # the WHERE it read the source with. Deliberately not both called
        # "watermark": a transaction does not have one, a run does.
        row = {"pk": raw, "pk_display": disp,
               "txn_wm": w.isoformat() if w else None,
               "run_id": None, "run_watermark": None, "run_started_at": None,
               "needed_lag_s": None, "covering_runs": 0}
        if w is None:
            not_in_source += 1
            out.append(row)
            continue
        run, n = covering(w)
        row["covering_runs"] = n
        if run is None:
            unresolved += 1
        else:
            row["run_id"] = run["id"]
            row["run_watermark"] = run["last_end_time"].isoformat()
            # started_at is carried for diagnostics only and must stay out of
            # this subtraction -- see the docstring's two-clocks note.
            row["run_started_at"] = run["started_at"].isoformat()
            row["needed_lag_s"] = (run["last_end_time"] - w).total_seconds()
            lags.append(row["needed_lag_s"])
        out.append(row)

    needed = None
    if lags:
        arr = np.asarray(lags, dtype=np.float64)
        needed = {"median": float(np.percentile(arr, 50)),
                  "p99": float(np.percentile(arr, 99)),
                  "max": float(arr.max()),
                  "n": int(arr.size)}
    return {"rows": out,
            # So the GUI can head the column with the name the source
            # actually uses ("end_time" here) instead of a generic word.
            "watermark_column": wm,
            "resolved": len(lags),
            "unresolved": unresolved,
            "not_in_source": not_in_source,
            "needed_lag": needed,
            # Computed here rather than by each caller so the GUI panel and
            # the stored audit run cannot disagree about what a given
            # increment would have bought.
            "lag_ladder": lag_ladder(lags)}
