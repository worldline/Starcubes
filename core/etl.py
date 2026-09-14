"""ETL engine for a single datasource.

Parameterised `import_fact`. The three-stage pipeline —
Extract (main process, pd.read_sql chunked) → Transform (N worker procs,
numpy vector replace + CSV serialization) → Load (1 worker proc, psycopg
`copy_from` on a raw connection) — is preserved verbatim; the copy-path is
the hot loop and untouched.

Every module-level global from the original implementation is now a field on the Source argument
or a worker argument. Run state is recorded in starcubes_meta.etl_run instead
of the per-schema etl_log table.
"""
from __future__ import annotations

import io
import json as _json
import logging
import multiprocessing as mp
import os
import queue
import signal
import stat
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd
import sqlalchemy
from sqlalchemy import text
from tqdm import tqdm

from .cache import bump_cache_epoch
from .preflight import (acquire_datasource_lock, blocking_message,
                        datasource_lock, relation_exists,
                        release_datasource_lock, run_preflight)
from .schema import (UNKNOWN_SOURCE_ID, ensure_partitions_for, import_dimensions,
                     read_dimensions)
from .source import Source
from .utils import tail_file

logger = logging.getLogger(__name__)


def _register_mapping(mapping: dict, sid: str, idx: int) -> None:
    """Register an auto-dim source_id under its str / int / float forms.

    Mirrors read_dimensions(): chunks may come back from the source as text
    or numeric; matching all three forms lets a single mapping serve both.
    """
    mapping[sid] = idx
    try:
        mapping[int(sid)] = idx
        mapping[float(sid)] = idx
    except (ValueError, TypeError):
        pass


def _load_auto_dim_maps(source: Source, dest_engine
                        ) -> tuple[dict[str, dict], dict[str, int]]:
    """Build {dim_table: mapping} for every auto-dimension table.

    The mapping has the same str/int/float key shape as read_dimensions so
    the substitution path matches regardless of whether the chunk column
    came back textual or numeric. Also returns {dim_table: -1_index} so
    source NULLs route to the same sentinel row regular dims use. Called
    once before the chunk loop; both dicts are mutated in place as new
    values are discovered and inserted.
    """
    schema = source.schema_name
    fact_to_table = source.auto_dim_fact_to_table()
    if not fact_to_table:
        return {}, {}
    maps: dict[str, dict] = {}
    null_idx: dict[str, int] = {}
    seen_tables: set[str] = set()
    with dest_engine.begin() as conn:
        for dim_table in fact_to_table.values():
            if dim_table in seen_tables:
                continue
            seen_tables.add(dim_table)
            rows = conn.execute(text(
                f"SELECT source_id, index FROM {schema}.{dim_table}"
            )).all()
            mapping: dict = {}
            minus_one_idx: int | None = None
            for sid, idx in rows:
                if sid == UNKNOWN_SOURCE_ID:
                    continue
                idx_int = int(idx)
                _register_mapping(mapping, sid, idx_int)
                if sid == "-1":
                    minus_one_idx = idx_int
            if minus_one_idx is None:
                raise RuntimeError(
                    f"{schema}.{dim_table} is missing the '-1' sentinel row "
                    f"— re-run core.schema.import_dimensions to add it.")
            maps[dim_table] = mapping
            null_idx[dim_table] = minus_one_idx
    return maps, null_idx


def _resolve_auto_dim_chunk(chunk, fact_to_table: dict[str, str],
                            auto_dim_maps: dict[str, dict],
                            auto_dim_null_idx: dict[str, int],
                            schema: str, dest_engine,
                            run_started: datetime) -> int:
    """For each auto-dim column in `chunk`, insert any new source values
    into its dim table and rewrite the chunk column with the dim indices.

    Returns the number of new dim rows inserted across all auto-dim
    columns. Mutates `chunk[col]` and `auto_dim_maps[dim_table]` in place.

    Concurrency note: assumes a single ETL process per datasource for this
    auto-dim table. The etl_forever active-session uniqueness covers the
    common case; running two import_fact() calls for the same source in
    parallel may insert duplicate source_id rows.
    """
    if not fact_to_table:
        return 0
    inserted_total = 0
    desc_default = (f"auto-discovered "
                    f"{run_started.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    for fact_col, dim_table in fact_to_table.items():
        mapping = auto_dim_maps[dim_table]
        null_idx = auto_dim_null_idx[dim_table]
        col = chunk[fact_col]
        unique_vals = [v for v in col.dropna().unique() if v not in mapping]
        if unique_vals:
            with dest_engine.begin() as conn:
                for v in unique_vals:
                    # Pandas stores integers as float64 in any column with
                    # NaN; strip a trailing ".0" so the dim row's source_id
                    # matches what the user sees in the source DB.
                    if isinstance(v, float) and v.is_integer():
                        sid = str(int(v))
                    else:
                        sid = str(v)
                    if sid in mapping:
                        # str form already registered this run; just record
                        # the numeric/etc. form on the local mapping.
                        _register_mapping(mapping, sid, mapping[sid])
                        continue
                    idx = conn.execute(text(
                        f"INSERT INTO {schema}.{dim_table} "
                        f"  (source_id, name, description) "
                        f"VALUES (:sid, :name, :desc) RETURNING index"
                    ), {"sid": sid, "name": sid,
                        "desc": desc_default}).scalar()
                    _register_mapping(mapping, sid, int(idx))
                    inserted_total += 1
        chunk[fact_col] = col.map(mapping).fillna(null_idx)
    return inserted_total


def _increment_dim_counts(source: Source, dest_engine,
                         last_end_time, end_time) -> None:
    """Add the just-loaded window's row counts to each dim table's count column.

    Cheaper than `refresh_dim_counts` (which recounts the entire fact table):
    this only counts rows in the ETL window and increments the existing
    dim.count values.  Called after each successful load.
    """
    schema = source.schema_name
    pc = source.partition_column
    with dest_engine.begin() as conn:
        for d in source.primary_dimensions():
            if not d.dim_table:
                continue
            if last_end_time is not None:
                conn.execute(text(
                    f"UPDATE {schema}.{d.dim_table} d "
                    f"   SET count = d.count + sub.c "
                    f"  FROM (SELECT {d.fact_column} AS idx, count(*) AS c "
                    f"          FROM {schema}.fact "
                    f"         WHERE {pc} >= :start AND {pc} < :end "
                    f"         GROUP BY {d.fact_column}) sub "
                    f" WHERE d.index = sub.idx"
                ), {"start": last_end_time, "end": end_time})
            else:
                conn.execute(text(
                    f"UPDATE {schema}.{d.dim_table} d "
                    f"   SET count = d.count + sub.c "
                    f"  FROM (SELECT {d.fact_column} AS idx, count(*) AS c "
                    f"          FROM {schema}.fact "
                    f"         WHERE {pc} < :end "
                    f"         GROUP BY {d.fact_column}) sub "
                    f" WHERE d.index = sub.idx"
                ), {"end": end_time})

# Upper bound on the PK set held in memory for overlap de-duplication.
# ~60-80 MB per million entries; beyond this the overlap window is almost
# certainly misconfigured, and a loud failure beats an OOM-killed parent.
_MAX_OVERLAP_PKS = int(os.environ.get("STARCUBES_MAX_OVERLAP_PKS", 5_000_000))

SMALLINT_MAX = 32767  # signed int16 upper bound
BIGINT_MAX = 9223372036854775807  # signed int64 upper bound
# Integer measures are clamped to [0, type max]: a `measure` column is a
# count/sum/duration and is never legitimately negative, so out-of-range
# values (either direction) are silently clamped rather than failing the
# COPY. See the `measure` role contract in `docs/star-schema.md`,
# "Integer measure columns must be non-negative and in range".


class EtlAborted(RuntimeError):
    """Raised when the ETL parent receives SIGTERM/SIGINT in single-run mode.

    The view's etl_kill endpoint sends SIGTERM, the parent raises this in
    its signal handler, import_fact's except block writes status='failed'
    with error='killed by user', and the finally block reaps the workers
    + finalises the etl_run row. The CLI also catches this and exits 1
    without a traceback.
    """


class EtlStageFailed(RuntimeError):
    """Raised when a transform/load worker dies or reports an error.

    Sibling of EtlAborted. Without this, a dead worker leaves the parent
    blocked forever in a bounded queue.put()/join() — both queues fill in
    a handful of chunks with no consumer on the other end, and there is
    no timeout anywhere in that path (see _put_with_abort/_join_with_abort
    below). Raising this from those helpers instead means the existing
    `except Exception` in import_fact writes status='failed' with a real
    error string, and the finally block's reaper still runs.
    """
    def __init__(self, stage: str, message: str, tb: str = ""):
        self.stage = stage
        self.message = message
        self.tb = tb
        super().__init__(f"{stage}: {message}")


def _ceil_second(dt: datetime) -> datetime:
    """Round a datetime up to the next whole second if it has sub-second precision.

    Source databases with second-resolution time indexes cannot match a
    sub-second HWM.  Rounding up ensures the next ETL run's WHERE clause
    starts strictly past all loaded rows — no boundary rows are missed.

    A second reason used to stand here: the window bounds were rendered
    into ``to_timestamp(..., 'YYYY-MM-DD HH24:MI:SS')`` literals, which
    have no room for a fractional second.  They are bind parameters now
    (see ``import_fact``), so that constraint is gone — the one above is
    not, and is why this still rounds.
    """
    if dt.microsecond:
        return dt.replace(microsecond=0) + timedelta(seconds=1)
    return dt


# ---------------------------------------------------------------------------
# Throughput metrics: shared-memory layout + jsonb shape
# ---------------------------------------------------------------------------
#
# Slot layout in stats_arr (3 doubles per slot, lock=False):
#   slot 0          → load worker
#   slot 1..N       → transform worker i (spawn order)
#   slot N+1        → extract (parent)
#
# Each slot writes (rows, busy_s, wait_s) once per chunk. Per-slot, single-
# writer, lock-free; a slightly stale read in the live snapshot is harmless.
# We deliberately avoid mp.Manager proxies on the hot path: a single shared
# array of doubles is ~2 orders of magnitude cheaper per write.

def _slot_rows_busy_wait(stats_arr, base: int) -> tuple[float, float, float]:
    return float(stats_arr[base + 0]), float(stats_arr[base + 1]), float(stats_arr[base + 2])


def _rate(rows: float, busy_s: float) -> Optional[float]:
    """rows/s; NULL when busy_s is 0 (avoid /0)."""
    if busy_s <= 0:
        return None
    return rows / busy_s


def _fmt_rate(rate: Optional[float]) -> str:
    """Compact human rate string; matches what the GUI statusbar prints."""
    if rate is None:
        return "?"
    if rate >= 1_000_000:
        return f"{rate/1_000_000:.1f}M/s"
    if rate >= 1_000:
        return f"{rate/1_000:.0f}k/s"
    return f"{rate:.0f}/s"


def _snapshot_stats(stats_arr, load_base: int, transform_bases: list[int],
                    extract_base: int, n_transform: int) -> dict:
    """Read the live shared array, return a snapshot dict (live + raw)."""
    load_rows, load_busy, load_wait = _slot_rows_busy_wait(stats_arr, load_base)
    transform_procs = []
    t_rows_total = 0.0
    t_busy_max = 0.0
    t_wait_total = 0.0
    for i, base in enumerate(transform_bases):
        r, b, w = _slot_rows_busy_wait(stats_arr, base)
        t_rows_total += r
        t_busy_max = max(t_busy_max, b)
        t_wait_total += w
        transform_procs.append({
            "slot": 1 + i,
            "rows": int(r),
            "busy_s": round(b, 3),
            "wait_s": round(w, 3),
            "rows_s": _rate(r, b),
        })
    extract_rows, extract_busy, extract_wait = _slot_rows_busy_wait(
        stats_arr, extract_base)
    return {
        "extract": {"rows": int(extract_rows),
                    "busy_s": round(extract_busy, 3),
                    "wait_s": round(extract_wait, 3),
                    "rows_s": _rate(extract_rows, extract_busy)},
        "transform": {"rows": int(t_rows_total),
                      "busy_s": round(t_busy_max, 3),
                      "wait_s": round(t_wait_total, 3),
                      "rows_s": _rate(t_rows_total, t_busy_max),
                      "procs": transform_procs},
        "load": {"rows": int(load_rows),
                 "busy_s": round(load_busy, 3),
                 "wait_s": round(load_wait, 3),
                 "rows_s": _rate(load_rows, load_busy)},
    }


def _build_stats_payload(stats_arr, load_base: int, transform_bases: list[int],
                         extract_base: int, n_transform: int,
                         extract_busy: float, extract_wait: float,
                         extract_auto_dim_s: float,
                         duplicates: int, auto_dim_inserted: int,
                         runtime_ms: int, rows_total: int,
                         *, live: bool) -> dict:
    """Compose the final (or live) jsonb shape.

    `live=False` mixes parent-side extract timings (which the workers
    don't see) with the worker-side stats_arr; `live=True` reads only
    the worker-visible slot for extract (which the parent doesn't write).
    """
    snap = _snapshot_stats(stats_arr, load_base, transform_bases,
                           extract_base, n_transform)
    # The parent tracks extract busy/wait on its own (workers don't know
    # source-DB latency), so overwrite with the more accurate figures.
    if not live:
        snap["extract"]["busy_s"] = round(extract_busy, 3)
        snap["extract"]["wait_s"] = round(extract_wait, 3)
        snap["extract"]["rows_s"] = _rate(rows_total, extract_busy)
        snap["extract"]["auto_dim_s"] = round(extract_auto_dim_s, 3)
        snap["extract"]["dedupe_rows"] = duplicates
        snap["extract"]["auto_dim_inserted"] = auto_dim_inserted
        snap["total"] = {"rows": rows_total,
                         "wall_s": round(runtime_ms / 1000.0, 3),
                         "rows_s": _rate(rows_total, runtime_ms / 1000.0)}
    return snap


def _extract_rate(rows_total: int, payload: Optional[dict]) -> Optional[float]:
    if not payload:
        return None
    return payload.get("extract", {}).get("rows_s")


def _transform_rate(rows_total: int, payload: Optional[dict]) -> Optional[float]:
    if not payload:
        return None
    return payload.get("transform", {}).get("rows_s")


def _load_rate(rows_total: int, payload: Optional[dict]) -> Optional[float]:
    if not payload:
        return None
    return payload.get("load", {}).get("rows_s")


# ---------------------------------------------------------------------------
# Worker entry points
# ---------------------------------------------------------------------------


def _transform_worker(in_queue: mp.JoinableQueue,
                      out_queue: mp.JoinableQueue,
                      dim_fact_to_table: dict[str, str],
                      dim_frames: dict[str, dict],
                      dim_unknowns: dict[str, int],
                      fact_column_order: list[str],
                      smallint_measure_cols: list[str],
                      bigint_measure_cols: list[str],
                      unmapped_log,
                      stats_arr, slot: int,
                      error_q: mp.Queue, abort_evt) -> None:
    """Map source dim keys to warehouse indices, route unmapped values to
    the dim's `__unknown__` row (logged to `unmapped_log` for the parent
    to persist), clamp smallint/bigint measures to [0, type max], emit
    tab-delimited CSV into a StringIO for `copy_from`.

    `unmapped_log` is an mp.Manager().list() — workers append
    (dim_table, fact_col, source_value_str, count) tuples per chunk per
    distinct unmapped value. Manager IPC is only paid when drift exists.

    `stats_arr` is an `mp.Array('d', 3*(N+2), lock=False)` shared with the
    parent. `slot` is this worker's index into the array; per-chunk writes
    to (rows, busy_s, wait_s) let the parent compute throughput without
    Manager proxy overhead on the hot path.

    `error_q`/`abort_evt` are the fail-fast channel back to the parent
    (see EtlStageFailed). `in_queue.get()`/`out_queue.put()` use a short
    timeout and poll `abort_evt` instead of blocking forever, so a load
    worker's death (which sets abort_evt) unsticks this worker's
    out_queue.put() instead of leaving it — and by extension the parent's
    transform_q.put() — blocked indefinitely.
    """
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    base = slot * 3
    try:
        while True:
            if abort_evt.is_set():
                return
            t_get_start = time.monotonic()
            try:
                chunk = in_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if chunk is None:
                in_queue.task_done()
                break
            wait_s = time.monotonic() - t_get_start
            n_rows = len(chunk.index)
            for fact_col, dim_table in dim_fact_to_table.items():
                mapping = dim_frames[dim_table]
                unknown_idx = dim_unknowns[dim_table]
                src = chunk[fact_col]
                mapped = src.map(mapping)
                unmapped = mapped.isna() & src.notna()
                if unmapped.any():
                    bad = src[unmapped].astype(str).value_counts()
                    for value, count in bad.items():
                        unmapped_log.append(
                            (dim_table, fact_col, str(value), int(count)))
                chunk[fact_col] = mapped.where(~unmapped, unknown_idx)
            for col in smallint_measure_cols:
                chunk[col] = chunk[col].clip(lower=0, upper=SMALLINT_MAX)
            for col in bigint_measure_cols:
                chunk[col] = chunk[col].clip(lower=0, upper=BIGINT_MAX)
            buf = io.StringIO()
            chunk[fact_column_order].to_csv(
                buf, sep="\t", float_format="%.0f", header=False, index=False)
            buf.seek(0)
            t_put_start = time.monotonic()
            while True:
                if abort_evt.is_set():
                    return
                try:
                    out_queue.put((n_rows, buf), timeout=0.5)
                    break
                except queue.Full:
                    continue
            put_s = time.monotonic() - t_put_start
            # busy_s includes mapping+clamp+csv+put so far; subtract put to
            # put backpressure into wait instead of busy.
            total_s = time.monotonic() - t_get_start
            busy_s = total_s - put_s
            stats_arr[base + 0] += n_rows
            stats_arr[base + 1] += busy_s
            stats_arr[base + 2] += wait_s + put_s
            in_queue.task_done()
    except Exception as exc:
        logger.exception("transform worker (slot %d) failed after %d rows",
                         slot, int(stats_arr[base]))
        abort_evt.set()
        try:
            error_q.put(("transform", repr(exc), traceback.format_exc()))
        except Exception:
            pass
    finally:
        in_queue.cancel_join_thread()


def _load_worker(in_queue: mp.JoinableQueue,
                 dest_dsn: str, schema_name: str,
                 stats_arr, slot: int,
                 error_q: mp.Queue, abort_evt) -> None:
    """Stream each CSV buffer into {schema}.fact via COPY FROM.

    Builds a fresh engine+connection inside the child so the load path
    doesn't depend on the parent's SQLAlchemy pool surviving fork.

    Each item on in_queue is `(n_rows, buf)` (sentinel `None` still).
    `n_rows` is what the worker passed through so the load stage can
    credit itself for them (otherwise `cur.copy_from` swallows rowcount).

    `stats_arr`/`slot` mirror `_transform_worker`; per-chunk (rows, busy,
    wait) writes feed the parent's throughput snapshot.

    `error_q`/`abort_evt`: see _transform_worker. This is the stage that
    originally had no exception handling at all — any failure here (a
    missing partition, a dropped connection, a constraint violation) used
    to kill the process silently (stderr was /dev/null) and leave the
    parent blocked forever in transform_q.put(). Now it logs, reports the
    failure, and exits so import_fact's finally block can reap everything
    and write status='failed' with a real error message.
    """
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    base = slot * 3
    engine = conn = cur = None
    try:
        engine = sqlalchemy.create_engine(
            dest_dsn, connect_args={"options": f"-csearch_path={schema_name}"})
        conn = engine.raw_connection()
        cur = conn.cursor()
        while True:
            if abort_evt.is_set():
                return
            t_get_start = time.monotonic()
            try:
                item = in_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                in_queue.task_done()
                break
            wait_s = time.monotonic() - t_get_start
            n_rows, buf = item
            try:
                cur.copy_from(buf, "fact", null="")
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
            busy_s = time.monotonic() - t_get_start - wait_s
            stats_arr[base + 0] += n_rows
            stats_arr[base + 1] += busy_s
            stats_arr[base + 2] += wait_s
            in_queue.task_done()
    except Exception as exc:
        logger.exception("load worker failed after %d rows", int(stats_arr[base]))
        abort_evt.set()
        try:
            error_q.put(("load", repr(exc), traceback.format_exc()))
        except Exception:
            pass
        # Deliberately not calling in_queue.task_done() for the failed
        # item — on the abort path nobody calls .join() again, and
        # marking it done would only mask that a chunk never landed.
    finally:
        for closer in (getattr(cur, "close", None),
                       getattr(conn, "close", None),
                       getattr(engine, "dispose", None)):
            if closer is not None:
                try:
                    closer()
                except Exception:
                    pass
        in_queue.cancel_join_thread()


# ---------------------------------------------------------------------------
# Parent-side abort-aware queue helpers
# ---------------------------------------------------------------------------
#
# transform_q/load_q are bounded (maxsize=8). Before EtlStageFailed existed,
# a dead worker meant: load_q fills (nobody drains it) -> transform workers
# block in out_queue.put() -> transform_q fills -> the parent blocks forever
# in transform_q.put(chunk). No put()/get()/join() anywhere had a timeout,
# and nothing checked worker liveness during the run (only in the teardown
# reaper). These helpers close that hole: every blocking queue operation on
# the parent side now polls for worker failure instead of blocking forever.

def _raise_if_failed(abort_evt, error_q: mp.Queue, workers: list[mp.Process],
                     budget: Optional["_RunBudget"] = None) -> None:
    """Raise EtlStageFailed if a worker signalled abort or simply died.

    Also the one place the run budget is checked while the parent is blocked
    on a queue: both bounded helpers below poll through here, so a run that
    is stuck on backpressure honours `max_run_s` without a second timer.

    Checked between every bounded put()/join() poll. `error_q.get_nowait()`
    normally has the reporting worker's own message; if it's empty (the
    worker was SIGKILLed/OOM-killed before it could report, or died before
    entering its own try block) fall back to a message built from whichever
    worker's exitcode went non-None — better than hanging forever with no
    explanation.
    """
    if budget is not None:
        budget.check()
    dead = [w for w in workers if w.exitcode is not None]
    if not abort_evt.is_set() and not dead:
        return
    try:
        stage, msg, tb = error_q.get_nowait()
    except queue.Empty:
        w = dead[0] if dead else None
        stage = (w.name if w else "worker")
        msg = (f"exited with code {w.exitcode} without reporting an error"
              if w is not None else "abort requested")
        tb = ""
    if tb:
        logger.error("child traceback (%s stage):\n%s", stage, tb)
    raise EtlStageFailed(stage, msg, tb)


def _put_with_abort(q: mp.JoinableQueue, item, abort_evt, error_q: mp.Queue,
                    workers: list[mp.Process], poll_s: float = 0.5,
                    budget: Optional["_RunBudget"] = None) -> None:
    """q.put(item) that gives up (raising EtlStageFailed) if a worker dies
    instead of blocking forever on a queue nobody is draining anymore."""
    while True:
        _raise_if_failed(abort_evt, error_q, workers, budget)
        try:
            q.put(item, timeout=poll_s)
            return
        except queue.Full:
            continue


def _join_with_abort(q: mp.JoinableQueue, abort_evt, error_q: mp.Queue,
                     workers: list[mp.Process], poll_s: float = 0.5,
                     budget: Optional["_RunBudget"] = None) -> None:
    """JoinableQueue.join() with a liveness check.

    join() has no timeout and no public way to poll its internal counter,
    so run it on a daemon thread and poll the thread's liveness instead of
    touching JoinableQueue's private _unfinished_tasks.
    """
    t = threading.Thread(target=q.join, name="etl-qjoin", daemon=True)
    t.start()
    while t.is_alive():
        t.join(timeout=poll_s)
        _raise_if_failed(abort_evt, error_q, workers, budget)


def _bound_log_size(max_bytes: int) -> None:
    """Truncate our own stdout in place once it exceeds max_bytes.

    Guards a long-running forever session from filling STARCUBES_LOG_DIR.
    The spawner (sources/etl_spawn.py) opens the log file O_APPEND, so
    after ftruncate(fd, 0) the next write lands back at offset 0 — crude
    but dependency-free. No-op when fd 1 isn't a regular file (a TTY or
    the historical DEVNULL/pipe case).
    """
    try:
        st = os.fstat(1)
    except OSError:
        return
    if not stat.S_ISREG(st.st_mode) or st.st_size <= max_bytes:
        return
    try:
        sys.stdout.flush()
        os.ftruncate(1, 0)
        os.lseek(1, 0, os.SEEK_SET)
        print(f"--- log truncated at {st.st_size} bytes ---", flush=True)
    except OSError:
        pass


def _as_utc(dt: datetime) -> datetime:
    """Normalize a DB timestamp to an aware UTC datetime.

    psycopg2 hands back timestamptz in the session timezone; normalizing
    here keeps a later .replace(tzinfo=None) from producing
    session-local-as-naive instead of UTC-as-naive.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _as_naive_utc(dt: datetime) -> datetime:
    """A DB timestamp as **naive UTC** — the form a source-side bind takes.

    The `.replace(tzinfo=None)` that `_as_utc` above warns about, done the
    one safe way: normalise first, strip second. Source timestamps are
    naive and hold UTC throughout this codebase (`Source.source_now_sql`),
    so a predicate against a watermark column has to carry the UTC
    wall-clock reading and nothing else.

    Written out as a helper rather than inlined because the two bounds of a
    load window reach it from different connections — the lower one via
    `_resume_state` off the warehouse, the upper one off the source — and
    stripping the offset without normalising renders the same instant two
    different ways depending on which one produced it.
    """
    return _as_utc(dt).replace(tzinfo=None)


def _overlap_pks(source: Source, dest_engine, since: datetime,
                 until: Optional[datetime] = None) -> set:
    """PKs already in fact whose watermark falls in ``[since, until)``.

    Held in a plain Python set and matched per chunk in the extract loop.
    At realistic overlaps (minutes) the set is small; ``_MAX_OVERLAP_PKS``
    turns an unrealistic one into a loud failure instead of an OOM kill of
    the ETL parent.
    """
    pk = source.pk_column()
    if pk is None:
        raise RuntimeError(
            f"{source.name}: late_arrival_overlap_s is set but no fact "
            f"column has role='pk'. Without a primary key the overlap "
            f"re-fetch would insert duplicate rows instead of filtering "
            f"them -- set the pk role or set late_arrival_overlap_s = 0.")

    schema = source.schema_name
    wm_expr = source.watermark_fact_sql()
    where = f" WHERE {wm_expr} >= :since"
    params: dict = {"since": since}
    if until is not None:
        where += f" AND {wm_expr} < :until"
        params["until"] = until

    with dest_engine.begin() as dst:
        n = dst.execute(text(
            f"SELECT count(*) FROM {schema}.fact{where}"), params).scalar()
        if n and n > _MAX_OVERLAP_PKS:
            raise RuntimeError(
                f"{source.name}: overlap window from {since} holds {n} fact "
                f"rows, above the {_MAX_OVERLAP_PKS} PK limit. Lower "
                f"late_arrival_overlap_s or raise "
                f"STARCUBES_MAX_OVERLAP_PKS.")
        if not n:
            return set()
        pks = pd.read_sql(text(
            f"SELECT {pk} FROM {schema}.fact{where}"), dst, params=params)
    return set(pks[pk])


def _source_min_watermark(source: Source, source_engine) -> Optional[datetime]:
    """`min(watermark_column)` in the source — where its data actually begins.

    Nothing ever asked this before. The engine only ever wanted the *tip*,
    because every window since the first one starts at the stored high
    watermark; the very first window simply had no lower bound at all
    (`WHERE wm < end_time`, open on the left since the beginning of time).
    Stepping that first load needs somewhere to start from.

    Asked only when `initial_window_s` is set and the fact table is empty —
    once per datasource lifetime, in practice.
    """
    wm = source.watermark_column
    if source.base_table:
        q = f"SELECT min({wm}) FROM {source.base_table}"
    else:
        q = f"SELECT min({wm}) FROM ({source.import_query}) q"
    with source_engine.begin() as src:
        value = src.execute(text(q)).scalar()
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    # Rounded *down*, unlike every other bound in here: this one is an
    # inclusive lower edge, and `_ceil_second` would step past the oldest
    # row it exists to include.
    return value.replace(microsecond=0)


def _dim_refresh_due(source: Source, meta_engine) -> bool:
    """Whether this run should import the dimensions.

    `import_dimensions` runs on every `import_fact`. At a 60-second forever
    interval that is a full `SELECT source_key, source_name, source_description`
    per regular dimension, sixty times an hour, against the source database —
    to discover changes that arrive maybe weekly.

    `datasource.dim_refresh_interval_s` throttles it, and defaults to 0
    (every run) because unlike the housekeeping interval this one **costs**
    something visible: a new value in a *regular* dimension maps to
    `__unknown__` until the next refresh, which is a wrong-looking series in
    the chart for up to one interval. Auto-dimensions are unaffected — they
    are resolved during the extract by `_resolve_auto_dim_chunk` and never
    depended on the periodic import.

    Read fresh from the metadata, like every other per-run setting: a forever
    session holds one `Source` for its whole life.
    """
    with meta_engine.begin() as meta:
        row = meta.execute(text(
            "SELECT dim_refresh_interval_s AS iv, "
            "       extract(epoch from (now() - dim_refreshed_at)) AS age "
            "  FROM starcubes_meta.datasource WHERE id = :ds"
        ), {"ds": source.id}).mappings().first()
    if row is None:
        return True
    interval = int(row["iv"] or 0)
    if interval <= 0:
        return True
    age = row["age"]
    if age is None:          # never refreshed
        return True
    if age >= interval:
        return True
    logger.debug("%s: dimensions refreshed %.0fs ago, interval is %ds — "
                 "skipping the import this run", source.name, age, interval)
    return False


def _fact_has_rows(source: Source, dest_engine) -> bool:
    """Cheapest possible "has anything been loaded yet".

    `LIMIT 1` inside the subquery, so it stops at the first row instead of
    counting a billion. Two callers: the resume logic, and the runtime cap,
    which must never apply to an initial load.
    """
    with dest_engine.begin() as dst:
        return bool(dst.execute(text(
            f"SELECT count(*) FROM "
            f"(SELECT 1 FROM {source.schema_name}.fact LIMIT 1) t"
        )).scalar())


class _RunBudget:
    """Wall-clock budget for one run (`datasource.max_run_s`).

    Checked between chunks and between every bounded queue poll, so a run
    wedged on backpressure gives up as reliably as one that is merely slow.
    A source that accepts the connection and then never answers is *not*
    caught here — the parent is blocked inside the DBAPI at that point,
    which is what the source statement timeout is for.

    Raising `EtlAborted` rather than inventing a new exception is deliberate:
    the reaping, the `status='failed'` row and the CLI's quiet exit already
    exist for exactly this shape of ending.
    """

    def __init__(self, source_name: str, seconds: int):
        self.source_name = source_name
        self.seconds = int(seconds)
        self.deadline = time.monotonic() + self.seconds

    def check(self) -> None:
        if time.monotonic() <= self.deadline:
            return
        raise EtlAborted(
            f"{self.source_name}: run exceeded max_run_s={self.seconds}s "
            f"— aborting. The next window will be *wider*, not smaller, so "
            f"if runs are genuinely too big, cap the rows with --limit or "
            f"raise max_run_s; this budget only stops one run from holding "
            f"the loop indefinitely.")


def _resume_state(source: Source, dest_engine, meta_engine,
                  dedupe_from: Optional[datetime] = None) -> tuple[
        Optional[datetime], Optional[datetime], set]:
    """Figure out where to resume from.

    `dedupe_from` is a pending high-watermark rewind (see
    `rewind_highwatermark`): the window has to reach back to it *and* carry
    the PK filter, even with `late_arrival_overlap_s` at 0, or the reload
    duplicates the rows the delete left behind. The caller reads it fresh
    from the metadata rather than off the Source, because a forever session
    holds one Source for its whole life.

    Returns (last_end_time, extract_from, existing_pks):
      - last_end_time: the high watermark. None if fact is empty (first
        run). Stays the HWM even when the extract reaches further back, so
        `_increment_dim_counts` does not re-count the overlap window on
        every pass.
      - extract_from: the real lower bound of the load window --
        last_end_time minus `late_arrival_overlap_s`. The source does not
        guarantee that rows become visible in watermark order, so a plain
        `>= last_end_time` window loses every row that shows up late.
      - existing_pks: PKs already in fact whose watermark falls inside the
        re-fetched span. Empty when there is nothing to de-duplicate.
    """
    schema = source.schema_name
    pc = source.partition_column
    wm_expr = source.watermark_fact_sql()
    overlap = max(0, int(source.late_arrival_overlap_s or 0))

    if not _fact_has_rows(source, dest_engine):
        return None, None, set()

    with meta_engine.begin() as meta:
        last_end = meta.execute(text(
            "SELECT last_end_time FROM starcubes_meta.etl_run "
            " WHERE datasource_id = :ds AND status = 'ok' "
            "   AND kind = 'incremental' "
            " ORDER BY finished_at DESC LIMIT 1"
        ), {"ds": source.id}).scalar()

    if last_end is not None:
        last_end = _ceil_second(_as_utc(last_end))
        if overlap <= 0 and dedupe_from is None:
            return last_end, last_end, set()
        extract_from = (last_end - timedelta(seconds=overlap)
                        if overlap > 0 else last_end)
        if dedupe_from is not None:
            extract_from = min(extract_from, _as_utc(dedupe_from))
        return last_end, extract_from, _overlap_pks(
            source, dest_engine, extract_from)

    # Prior run interrupted: reconstruct a conservative watermark from
    # fact. The reconstruction window is the last minute of partition_column
    # -- widened to the configured overlap when that is larger -- and every
    # PK inside it is pre-loaded so the first chunks can be de-duped.
    with dest_engine.begin() as dst:
        last_end = dst.execute(text(
            f"SELECT date_trunc('second', max({wm_expr})) "
            f"  FROM {schema}.fact "
            f" WHERE {pc} > (SELECT max({pc}) - interval '1 minute' "
            f"                FROM {schema}.fact)"
        )).scalar()
    if last_end is None:
        return None, None, set()
    last_end = _ceil_second(_as_utc(last_end))
    extract_from = last_end - timedelta(seconds=max(60, overlap))
    return last_end, extract_from, _overlap_pks(
        source, dest_engine, extract_from)


def import_fact(source: Source, meta_engine, *,
                quiet: bool = False,
                refresh_dimensions: bool = True,
                transform_q_size: int = 8,
                load_q_size: int = 8,
                start_time: datetime | None = None,
                end_time_override: datetime | None = None,
                row_limit: int | None = None,
                backfill: bool = False,
                log_path: str | None = None) -> int:
    """Run one incremental fact load for `source`. Returns rows loaded.

    Records a starcubes_meta.etl_run row (status='running' → 'ok'/'failed').
    Idempotent across interruptions: the next invocation resumes from the
    last successful high watermark, with a PK-based de-dupe pass covering
    any overlap.

    By default, dim tables are refreshed from the source before each load —
    new dim values would otherwise pass through `read_dimensions` unmapped
    and land in fact as raw source_ids (orphan or, worse, colliding with
    another dim's index). Disable only when the source dims are guaranteed
    static and the round-trip is too expensive.

    ``start_time`` overrides the high watermark — the extract starts from
    this timestamp instead of the last ``etl_run`` row's ``last_end_time``.
    Deduplication is skipped because the user chose the window explicitly.

    ``backfill`` re-loads a closed historical window (requires both
    ``start_time`` and ``end_time_override``) and de-dupes the *whole*
    window against the PKs already in fact, so only genuinely missing rows
    are inserted.  The run is recorded with ``kind='backfill'`` and is
    therefore invisible to ``_resume_state`` — a backfill must never move
    the high watermark backwards.

    ``end_time_override`` overrides the source ``max(watermark_column)``
    query — the extract ends at this timestamp.  The source max query is
    skipped entirely.

    ``row_limit`` caps the total rows extracted.  The extract loop breaks
    once the limit is reached; the final chunk is truncated if it would
    overshoot.

    ``log_path`` is recorded on the etl_run row (sources/etl_spawn.py sets
    it for a spawned process); on failure, the last portion of that file is
    also captured into ``log_tail`` so the GUI can show a traceback without
    filesystem access to the log volume.
    """
    if backfill and (start_time is None or end_time_override is None):
        raise ValueError(
            "backfill requires both start_time and end_time_override — "
            "a backfill re-loads one closed window, it does not chase the "
            "source watermark.")

    dest_engine = source.dest_engine()

    # The run budget, and with it the source statement timeout, is decided
    # before anything is opened. Read fresh from the metadata rather than off
    # the Source, for the usual reason: a forever session loads its Source
    # once, before the loop.
    #
    # Three kinds of run are exempt, all for the same reason — they are
    # legitimately long and a cap would turn them into a retry loop:
    # a backfill, an explicit --start/--end window (the user owns it), and
    # the initial load, whose window is "everything the source has" (N17).
    budget: Optional[_RunBudget] = None
    with meta_engine.begin() as meta:
        max_run_s = int(meta.execute(text(
            "SELECT max_run_s FROM starcubes_meta.datasource WHERE id = :ds"
        ), {"ds": source.id}).scalar() or 0)
    capped = (max_run_s > 0 and not backfill
              and start_time is None and end_time_override is None
              and _fact_has_rows(source, dest_engine))
    # The width cap (N17) is the better instrument for a long first load than
    # exempting it from the time cap: with it set, the first load is a series
    # of ordinary short runs and the exemption above stops mattering.
    with meta_engine.begin() as meta:
        initial_window_s = int(meta.execute(text(
            "SELECT initial_window_s FROM starcubes_meta.datasource "
            " WHERE id = :ds"), {"ds": source.id}).scalar() or 0)
    if capped:
        budget = _RunBudget(source.name, max_run_s)

    # A source that accepts the connection and then never answers blocks the
    # extract indefinitely — pool_pre_ping and pool_timeout bound the
    # checkout, not the execution. One knob rather than two: a statement may
    # not outlive the run it belongs to, and a second timeout column nobody
    # could size independently would only be a second thing to get wrong.
    source_engine = source.source_engine(
        statement_timeout_ms=max_run_s * 1000 if capped else None)

    started = datetime.now(timezone.utc)
    parent_pid = os.getpid()

    # One ETL per datasource, enforced for every entry point rather than
    # only for the two the GUI knows about. Taken *before* the etl_run row
    # is written: a refusal is not a run, and in forever mode a failed row
    # per iteration would be noise nobody can read past. Released in the
    # finally, after the terminal UPDATE.
    lock_handle = acquire_datasource_lock(source.dest_dsn, source.id)

    # Record the run before any source DB operations so connection
    # failures are always captured in etl_run.
    with meta_engine.begin() as meta:
        run_id = meta.execute(text(
            "INSERT INTO starcubes_meta.etl_run "
            "  (datasource_id, started_at, status, kind, pid, log_path) "
            "VALUES (:ds, :t, 'running', :kind, :pid, :log_path) RETURNING id"
        ), {"ds": source.id, "t": started, "pid": parent_pid,
            "kind": "backfill" if backfill else "incremental",
            "log_path": log_path}).scalar()

    status = "ok"
    error: Optional[str] = None
    rows_total = 0
    duplicates = 0
    auto_dim_inserted = 0
    current = ""
    current_pc_max = None  # running max of pc seen so far, for the live
                            # "processed through" progress indicator
    end_time: Optional[datetime] = None
    last_end_time: Optional[datetime] = None
    extract_from: Optional[datetime] = None
    # Set by the preflight below; merged into the stats jsonb in the finally
    # so a blocked run still leaves its findings behind.
    preflight_report: Optional[dict] = None
    # A high-watermark rewind waiting to be honoured, cleared once this run
    # has actually loaded the span it covers.
    dedupe_pending: Optional[datetime] = None
    # Upper-bound inputs; only set on the non-override path (see below).
    max_wm: Optional[datetime] = None
    src_now: Optional[datetime] = None
    manager = None
    workers: list[mp.Process] = []
    progress_thread: Optional[threading.Thread] = None
    progress_stop: Optional[threading.Event] = None
    # (rows, busy_s, wait_s) per slot. Slot 0=load, 1..N=transform,
    # N+1=extract. Set up before workers start so the parent can write
    # to its slot from the moment workers do.
    stats_arr: Optional[mp.sharedctypes.SynchronizedArray] = None
    extract_busy = 0.0
    extract_wait = 0.0
    extract_auto_dim_s = 0.0
    extract_slot_base = 0
    load_slot_base = 0
    transform_slot_bases: list[int] = []
    # Fail-fast channel: a worker sets abort_evt and pushes its traceback
    # here instead of dying silently behind a full queue (see
    # EtlStageFailed / _put_with_abort / _join_with_abort).
    error_q: mp.Queue = mp.Queue()
    abort_evt = mp.Event()
    transform_q: Optional[mp.JoinableQueue] = None
    load_q: Optional[mp.JoinableQueue] = None

    try:
        # Everything that has to be true before the load, in one place --
        # schema objects, metadata version, index coverage, the source
        # projection, the watermark against the fact tip. Runs before
        # import_dimensions because that already assumes the dim tables
        # exist, and a block lands as status='failed' through the same
        # except/finally as any other startup error. See core/preflight.py.
        preflight_report = run_preflight(
            source, meta_engine, dest_engine, source_engine=source_engine)
        blocked = blocking_message(preflight_report)
        if blocked:
            raise RuntimeError(blocked)

        if refresh_dimensions and _dim_refresh_due(source, meta_engine):
            import_dimensions(source, source_engine, dest_engine, quiet=quiet)
            with meta_engine.begin() as meta:
                meta.execute(text(
                    "UPDATE starcubes_meta.datasource "
                    "   SET dim_refreshed_at = now() WHERE id = :ds"
                ), {"ds": source.id})
        dim_frames, dim_unknowns = read_dimensions(source, dest_engine)

        dim_fact_to_table = source.dim_fact_to_table()
        auto_dim_fact_to_table = source.auto_dim_fact_to_table()
        auto_dim_maps, auto_dim_null_idx = _load_auto_dim_maps(
            source, dest_engine)
        fact_column_order = source.fact_column_order()
        smallint_measure_cols = source.smallint_measure_columns()
        bigint_measure_cols = source.bigint_measure_columns()
        pk_column = source.pk_column()
        wm = source.watermark_column
        pc = source.partition_column
        lag_wm = max(0, int(source.watermark_lag_s or 0))
        lag_now = max(0, int(source.wallclock_lag_s or 0))

        # discover source max(watermark). Prefer the explicit max query if set,
        # then a direct max() against base_table (cheap), and finally fall back
        # to wrapping the full import_query as a subquery (slow).
        # Skipped when end_time_override is provided.
        if end_time_override is not None:
            end_time = end_time_override
            if end_time.tzinfo is None:
                end_time = end_time.replace(tzinfo=timezone.utc)
            end_time = _ceil_second(end_time)
        else:
            # The wall-clock term of the upper bound (below) reads the
            # source DB's own clock, not this host's -- watermarks are
            # source wall-clock time throughout. For the two generated
            # query shapes it rides along as a second column, so it costs
            # no extra round-trip; only a user-supplied watermark_max_query
            # is opaque and needs its own statement.
            # Read on every run, not only when wallclock_lag_s is set. It
            # rides along as a second column on the two generated query
            # shapes, so it is free there, and `src_now - max_wm` is the only
            # thing that can see a source which is healthy but has stopped
            # producing (U9) or one whose watermark column is not UTC (N12).
            # Only a user-supplied watermark_max_query pays for it, with one
            # extra trivial round trip.
            want_now = True
            now_expr = source.source_now_sql()
            standalone_now = None
            if source.watermark_max_query:
                max_query = source.watermark_max_query
                if want_now:
                    standalone_now = (
                        f"SELECT {now_expr} FROM dual"
                        if source.source_dialect == "oracle"
                        else f"SELECT {now_expr}")
            elif source.base_table:
                _cols = f"max({wm})" + (f", {now_expr}" if want_now else "")
                max_query = f"SELECT {_cols} FROM {source.base_table}"
            else:
                _cols = f"max({wm})" + (f", {now_expr}" if want_now else "")
                max_query = f"SELECT {_cols} FROM ({source.import_query}) q"
            with source_engine.begin() as src:
                _row = src.execute(text(max_query)).first()
                end_time = _row[0] if _row is not None else None
                if want_now and standalone_now is None:
                    src_now = _row[1] if _row is not None else None
                elif standalone_now is not None:
                    src_now = src.execute(text(standalone_now)).scalar()
            if end_time is None:
                logger.log(logging.DEBUG if quiet else logging.INFO,
                          "%s: source has no data for watermark %s",
                          source.name, wm)
                return 0
            if end_time.tzinfo is None:
                end_time = end_time.replace(tzinfo=timezone.utc)
            end_time = _ceil_second(end_time)
            max_wm = end_time
            if src_now is not None:
                if src_now.tzinfo is None:
                    src_now = src_now.replace(tzinfo=timezone.utc)
                # _resume_state and the WHERE clause assume second-
                # granularity bounds; a raw clock reading would carry
                # sub-second parts straight into the HWM.
                src_now = _ceil_second(src_now)

            # Upper bound = the later of two independent guarantees:
            #   max(wm) - lag_wm   relative buffer under the data tip. Takes
            #                      the tip as proof of freshness and is fast,
            #                      but only advances while the tip moves.
            #   now     - lag_now  absolute deadline: every row gets this long
            #                      in source wall-clock time to become visible.
            #                      Independent of traffic, so it keeps the
            #                      window moving when the first term freezes.
            # The crossover is not configured, it falls out: the terms are
            # equal at now - max_wm == lag_now - lag_wm. lag_now == 0 drops
            # the second term rather than zeroing it -- max(max_wm, now)
            # would be `now`, i.e. no lag at all but its opposite.
            bounds = [max_wm - timedelta(seconds=lag_wm)]
            if lag_now:
                if src_now is None:
                    logger.warning(
                        "%s: source returned no wall-clock value; "
                        "wallclock_lag_s=%ds not applied this run",
                        source.name, lag_now)
                else:
                    skew = (src_now - max_wm).total_seconds()
                    if skew < 0:
                        # Source clock behind its own data -- almost always a
                        # timezone mismatch. Do not act on it. The reverse
                        # skew (clock ahead) is not detectable here and would
                        # keep the wall-clock term winning; that is what the
                        # window log line below is for.
                        logger.warning(
                            "%s: source clock %.0fs behind max(watermark) "
                            "(%s vs %s) -- wall-clock bound not applied",
                            source.name, -skew, src_now, max_wm)
                    else:
                        bounds.append(src_now - timedelta(seconds=lag_now))
            end_time = max(bounds)

            # HARD INVARIANT: the window's upper bound never passes the
            # newest row that actually exists. Violating it means the HWM
            # advances over a range the source has not produced yet, and
            # every row that later arrives in that range is below the HWM
            # and silently lost forever.
            #
            # This clamp is not theoretical: it shipped as a hotfix after a
            # production Oracle source dragged its HWM two hours past the
            # data. That particular cause -- source_now_sql() returning
            # LOCALTIMESTAMP, i.e. the *session* clock -- is fixed at the
            # root now (it reads UTC explicitly, matching how every naive
            # source timestamp is treated in this codebase).
            #
            # The clamp stays as the net underneath, because the root fix
            # rests on an assumption the clamp does not need: that the
            # source writes its time columns in UTC. If one ever does not,
            # max_wm and src_now part company again and only this line
            # stands between that and silent, permanent data loss.
            #
            # The clamp costs nothing real. Under a genuine standstill the
            # wall-clock term still closes the window right up to max_wm,
            # which is all there is to load; it just may no longer run off
            # into empty time.
            if end_time > max_wm:
                logger.warning(
                    "%s: wall-clock bound %s is past max(%s)=%s -- clamping. "
                    "src_now=%s (read as UTC) is %.0fs ahead of the newest "
                    "row; if that figure stays near a whole-hour offset "
                    "while rows keep arriving, %s does not hold UTC values, "
                    "which the rest of the pipeline assumes it does. Set "
                    "wallclock_lag_s to 0 in that case.",
                    source.name, end_time, wm, max_wm, src_now,
                    (src_now - max_wm).total_seconds(), wm)
                end_time = max_wm

        if backfill:
            # A backfill owns its window outright: no HWM lookup, and the
            # whole window is de-duped against what is already in fact so
            # only the genuinely missing rows get inserted.
            if start_time.tzinfo is None:
                start_time = start_time.replace(tzinfo=timezone.utc)
            last_end_time = extract_from = _ceil_second(start_time)
            existing_pks = _overlap_pks(
                source, dest_engine, extract_from, end_time)
        else:
            # Read fresh, not off the Source: a forever session loads its
            # Source once, before the loop, so a rewind armed from the GUI
            # mid-session would be invisible to it otherwise.
            with meta_engine.begin() as meta:
                dedupe_pending = meta.execute(text(
                    "SELECT dedupe_from FROM starcubes_meta.datasource "
                    " WHERE id = :ds"), {"ds": source.id}).scalar()
            last_end_time, extract_from, existing_pks = _resume_state(
                source, dest_engine, meta_engine, dedupe_pending)

            if extract_from is None and initial_window_s > 0:
                # The initial load, and somebody wants it stepped. Ask the
                # source where its data begins — the one question the engine
                # never had to ask, because every other window starts at the
                # stored watermark.
                extract_from = _source_min_watermark(source, source_engine)
                if extract_from is None:
                    logger.warning(
                        "%s: initial_window_s is set but the source reports "
                        "no min(%s); loading the whole history in one window",
                        source.name, source.watermark_column)

            if start_time is not None:
                if start_time.tzinfo is None:
                    start_time = start_time.replace(tzinfo=timezone.utc)
                # Explicit user-chosen window: no overlap, no de-dupe.
                last_end_time = extract_from = _ceil_second(start_time)
                existing_pks = set()
                # The user owns this window, so it is not the run that
                # honours the pending rewind — leave the flag standing.
                dedupe_pending = None

        if (not backfill and last_end_time is not None
                and end_time < last_end_time):
            # Both bounds are normally monotonic across runs, so this is a
            # guard, not the common path: it catches upstream deletions
            # lowering max(watermark), a watermark_lag_s raised at runtime,
            # and a preceding --start/--end run that set a higher HWM.
            #
            # The final UPDATE writes `last_end_time = end_time` from the
            # finally block, so an end_time below the existing HWM would
            # move the watermark *backwards* -- and _resume_state reads that
            # value back, making the next run re-pull everything in between.
            # Clamping keeps the HWM put; the window degenerates to the
            # overlap span, or to nothing at all when overlap is 0. A bare
            # `return 0` would not do: it would still run through the
            # finally and write the withdrawn end_time anyway.
            logger.info("%s: upper bound %s is below the HWM %s -- clamping "
                        "(nothing new to load)",
                        source.name, end_time, last_end_time)
            end_time = last_end_time

        # Cap the width while the load is still catching up. Placed here on
        # purpose: `extract_from` only exists after the block above, and
        # doing it before the HWM clamp would let that clamp quietly undo it.
        #
        # Measured from the high watermark rather than from `extract_from`,
        # because the late-arrival overlap re-reads ground that is already
        # loaded — capping from there would shrink the window by the overlap
        # and, with a cap smaller than the overlap, produce an `end_time`
        # below the HWM that the clamp above would push straight back up.
        #
        # It necessarily applies to every step of the catch-up rather than
        # only to the literally first run: cap step one and step two would
        # otherwise load the entire remainder, which is the thing being
        # avoided. A datasource that has caught up never reaches the cap,
        # because its window is narrower than the cap anyway.
        if not backfill and start_time is None and initial_window_s > 0:
            base = last_end_time if last_end_time is not None else extract_from
            if base is not None:
                capped = base + timedelta(seconds=initial_window_s)
                if capped < end_time:
                    logger.info(
                        "%s: catching up — window capped to %s "
                        "(initial_window_s=%ds, tip is %s)",
                        source.name, capped, initial_window_s, end_time)
                    end_time = capped

        # Update first_end_time now that we know it
        with meta_engine.begin() as meta:
            meta.execute(text(
                "UPDATE starcubes_meta.etl_run "
                " SET first_end_time = :first WHERE id = :id"
            ), {"first": extract_from, "id": run_id})

        # Make sure every partition the load window can possibly touch
        # exists before the worker pool starts — a COPY into a row with no
        # matching partition used to kill the load worker silently (its
        # stderr was /dev/null) and deadlock the parent forever in
        # transform_q.put(). back=1 absorbs skew between
        # partition_column and watermark_column when they differ;
        # forward=1 keeps one full period of runway past end_time so the
        # next run doesn't immediately need another partition. Runs every
        # call (including every forever-mode iteration), so the partition
        # window is kept current without a cron job or a manual GUI click.
        try:
            ensure_partitions_for(source, dest_engine,
                                  start=(extract_from or end_time), end=end_time,
                                  back=1, forward=1, quiet=quiet)
        except Exception:
            logger.exception("%s: partition pre-flight failed", source.name)
            raise

        query = source.import_query
        if (source.small_window_hint
                and extract_from is not None
                and (end_time - extract_from).days == 0):
            logger.log(logging.DEBUG if quiet else logging.INFO,
                      "%s: small window, applying %s",
                      source.name, source.small_window_hint)
            query = query.replace(
                "SELECT", f"SELECT {source.small_window_hint}", 1)

        # Window bounds as bind parameters, not as formatted literals.
        #
        # This used to emit `to_timestamp('2026-09-14 06:00:01',
        # 'YYYY-MM-DD HH24:MI:SS')`, which is Oracle spelling: MySQL and
        # MSSQL have no `to_timestamp` at all and reject the statement
        # outright. Postgres does have one -- and that is the worse half of
        # the story, because it made the bug silent rather than loud:
        #
        #   * Postgres `to_timestamp(text, text)` returns *timestamptz*,
        #     parsed in the session timezone -- not the naive timestamp the
        #     Oracle spelling produces.
        #   * `.strftime()` on an aware datetime prints *that object's own*
        #     fields. `extract_from` and `end_time` do not always carry the
        #     same tzinfo: the lower bound is the stored HWM, read back
        #     through the warehouse connection, while the upper bound comes
        #     off the source connection. Two connections, two session
        #     timezones, two different renderings of the same instant.
        #
        # Both halves cancel out whenever warehouse and source happen to sit
        # in the same zone, which is why an Oracle-only installation never
        # saw it. They stop cancelling the moment the zones differ:
        # measured against a Postgres source (session Europe/Berlin) with
        # the warehouse session on UTC, an incremental run that had nothing
        # to do re-extracted two hours it already held and inserted 16665
        # duplicate rows. The run log showed the tell plainly --
        # `window [2026-09-14 06:00:01+00:00, 2026-09-14 08:00:01+02:00)`,
        # one instant printed twice under two offsets.
        #
        # A bind carries a datetime rather than a rendering, so no dialect
        # gets to reinterpret it -- but only once the datetime itself is
        # unambiguous. `_as_naive_utc` is what does that, and it is the
        # load-bearing half: a bare `.replace(tzinfo=None)` drops the offset
        # while keeping each object's *local* fields, which reproduces the
        # exact split above -- measured, all 16665 duplicates came back.
        # `_as_utc` already carries that warning in its own docstring; this
        # is the second half of it.
        #
        # The `_sc_` prefix keeps the names clear of anything an operator
        # might have written in `import_query` (`core/query.py` uses the
        # same convention for `:_limit`).
        where_params: dict = {"_sc_until": _as_naive_utc(end_time)}
        if extract_from is not None:
            where = (f" WHERE {wm} >= :_sc_from "
                     f"   AND {wm} < :_sc_until")
            where_params["_sc_from"] = _as_naive_utc(extract_from)
        else:
            where = f" WHERE {wm} < :_sc_until"

        if backfill:
            _mode = "backfill"
        elif extract_from is None:
            _mode = "initial"
        else:
            _mode = "incremental"
        # Print both upper-bound candidates and which one won, not just the
        # result: a source clock running *ahead* is undetectable at the point
        # it is read and would silently let the wall-clock term win every
        # run. Seeing "wall-clock" here under healthy traffic is the symptom.
        if max_wm is not None and (lag_wm or lag_now):
            _cand_wm = max_wm - timedelta(seconds=lag_wm)
            if lag_now and src_now is not None and src_now >= max_wm:
                _cand_now = src_now - timedelta(seconds=lag_now)
                # The skew is printed because it is the one number that
                # exposes a clock mismatch. src_now is read as UTC, so a
                # skew parked near a whole-hour offset while rows keep
                # flowing means the watermark column does not hold UTC --
                # not that the source went quiet. Under a healthy source it
                # tracks how far behind the data tip trails real time.
                _skew = (src_now - max_wm).total_seconds()
                _who = 'wall-clock' if _cand_now > _cand_wm else 'watermark'
                _bound = (f"bound wm={_cand_wm} now={_cand_now} "
                          f"(skew {_skew:+.0f}s, {_who} wins), ")
            else:
                _bound = f"bound wm={_cand_wm} (watermark only), "
        else:
            _bound = ""
        logger.log(logging.DEBUG if quiet else logging.INFO,
                  "%s: window [%s, %s)  (%s, hwm=%s, %slag=%s/%ss, "
                  "overlap=%ss, dedupe against %d pk%s)",
                  source.name, extract_from, end_time, _mode, last_end_time,
                  _bound, lag_wm, lag_now,
                  source.late_arrival_overlap_s, len(existing_pks),
                  "" if len(existing_pks) == 1 else "s")

        manager = mp.Manager()
        unmapped_log = manager.list()  # appended to by transform workers

        transform_q: mp.JoinableQueue = mp.JoinableQueue(maxsize=transform_q_size)
        load_q: mp.JoinableQueue = mp.JoinableQueue(maxsize=load_q_size)

        # Shared-memory slot layout:
        #   slot 0          → load worker
        #   slot 1..N       → transform worker i (in spawn order)
        #   slot N+1        → extract (parent)
        # Each slot has 3 doubles: (rows, busy_s, wait_s). lock=False is
        # safe: one writer per slot, the parent only reads; a slightly
        # stale value in the live snapshot is harmless.
        n_transform = source.transform_procs
        n_slots = 1 + n_transform + 1  # load + N transform + extract
        stats_arr = mp.Array('d', 3 * n_slots, lock=False)
        load_slot_base = 0
        transform_slot_bases = [(1 + i) * 3 for i in range(n_transform)]
        extract_slot_base = (1 + n_transform) * 3

        workers = [mp.Process(
            target=_load_worker, name="etl-load",
            args=(load_q, source.dest_dsn, source.schema_name,
                  stats_arr, 0, error_q, abort_evt))]
        for i in range(n_transform):
            workers.append(mp.Process(
                target=_transform_worker, name=f"etl-transform-{i}",
                args=(transform_q, load_q, dim_fact_to_table, dim_frames,
                      dim_unknowns, fact_column_order, smallint_measure_cols,
                      bigint_measure_cols,
                      unmapped_log, stats_arr, 1 + i, error_q, abort_evt)))
        for w in workers:
            w.start()

        # Progress-writer thread: snapshots stats_arr every 5 s so the
        # GUI sees live row counts + rates even when the parent is
        # blocked on transform_q.put(). Started AFTER w.start() because
        # forking with a live thread is risky (would be visible in all
        # children).
        progress_stop = threading.Event()
        log_every_ticks = max(1, int(
            os.environ.get("STARCUBES_ETL_LOG_INTERVAL_S", "30")) // 5)
        _last_seen = [-1, -1]  # [rows_total, load rows] as of the last tick
        _tick = [0]
        def _progress_writer():
            assert stats_arr is not None and progress_stop is not None
            while not progress_stop.wait(5.0):
                try:
                    snap = _snapshot_stats(
                        stats_arr, load_slot_base, transform_slot_bases,
                        extract_slot_base, n_transform)
                    snap["queues"] = {"transform": transform_q.qsize(),
                                      "load": load_q.qsize()}
                    snap["workers"] = [{"name": w.name, "alive": w.is_alive(),
                                        "exitcode": w.exitcode}
                                       for w in workers]
                    snap["processed_through"] = (
                        current_pc_max.isoformat() if current_pc_max is not None else None)
                    moved = (rows_total != _last_seen[0]
                            or snap["load"]["rows"] != _last_seen[1])
                    _last_seen[0] = rows_total
                    _last_seen[1] = snap["load"]["rows"]
                    with meta_engine.begin() as meta:
                        if moved:
                            # Only advance progress_at when rows actually
                            # moved — a wedged run must go stale, not look
                            # perpetually alive (see GUI stall detection).
                            meta.execute(text(
                                "UPDATE starcubes_meta.etl_run "
                                "   SET progress_rows = :r, "
                                "       progress_at   = now(), "
                                "       stats         = CAST(:stats AS jsonb) "
                                " WHERE id = :id"
                            ), {"r": rows_total, "stats": _json.dumps(snap),
                                "id": run_id})
                        else:
                            meta.execute(text(
                                "UPDATE starcubes_meta.etl_run "
                                "   SET progress_rows = :r, "
                                "       stats         = CAST(:stats AS jsonb) "
                                " WHERE id = :id"
                            ), {"r": rows_total, "stats": _json.dumps(snap),
                                "id": run_id})
                    _tick[0] += 1
                    if _tick[0] % log_every_ticks == 0:
                        logger.info(
                            "%s: extract %d rows | tq %d lq %d | E %s T %s L %s | workers %s | through %s",
                            source.name, rows_total,
                            snap["queues"]["transform"], snap["queues"]["load"],
                            _fmt_rate(snap["extract"]["rows_s"]),
                            _fmt_rate(snap["transform"]["rows_s"]),
                            _fmt_rate(snap["load"]["rows_s"]),
                            ",".join(f"{w['name']}={'live' if w['alive'] else w['exitcode']}"
                                    for w in snap["workers"]),
                            current or "?")
                        _bound_log_size(int(os.environ.get(
                            "STARCUBES_LOG_MAX_BYTES", str(64 * 1024 * 1024))))
                except Exception as exc:
                    # Never let progress writes crash the run. The
                    # terminal UPDATE will still capture final metrics.
                    logger.warning("progress write failed: %r", exc)
        progress_thread = threading.Thread(
            target=_progress_writer, name="etl-progress", daemon=True)
        progress_thread.start()

        source_engine.dispose()  # drop parent pool before the main extract loop
        # `text()`, not a bare string: pandas branches on the type.
        # `SQLDatabase.execute` routes a `str` through
        # `Connection.exec_driver_sql` -- DBAPI paramstyle, so `:name` would
        # never be substituted -- and a `TextClause` through
        # `Connection.execute`, which is the SQLAlchemy bind path this
        # window now depends on. It also settles the `%` question for good:
        # an `import_query` containing `LIKE '%x%'` survives
        # `exec_driver_sql` only while no params accompany it, whereas
        # `text()` escapes for the driver's paramstyle itself.
        chunks = pd.read_sql(
            text(query + where), source_engine, params=where_params,
            chunksize=source.chunksize,
            parse_dates={pc: {"utc": True}})
        # A detached spawn's stderr is a regular log file, not a terminal —
        # tqdm's \r-repainted description would otherwise dominate the log
        # with one giant line. Only draw the bar when something is actually
        # watching it live.
        pbar = tqdm(chunks, desc=f"{source.name}: filling fact",
                     unit_scale=source.chunksize / 1000, unit=" krows",
                     disable=quiet or not sys.stderr.isatty())
        chunk_iter = iter(pbar)
        while True:
            t_fetch_start = time.monotonic()
            try:
                chunk = next(chunk_iter)
            except StopIteration:
                break
            # Notice a dead transform/load worker between chunks even
            # while the source DB itself is slow (a slow queue.put()
            # inside this iteration is also covered — see below).
            _raise_if_failed(abort_evt, error_q, workers)
            # Time inside next() = source DB read + auto-dim INSERTs
            # (= busy). transform_q.put() block time goes to wait.
            _chunk_busy = time.monotonic() - t_fetch_start
            extract_busy += _chunk_busy
            if chunk.empty:
                break
            current = (chunk[pc][chunk.index[-1]]
                       .astimezone(ZoneInfo("Europe/Berlin"))
                       .strftime("%d.%m. %H:%M:%S"))
            chunk_pc_max = chunk[pc].max()
            if pd.notna(chunk_pc_max) and (current_pc_max is None or chunk_pc_max > current_pc_max):
                current_pc_max = chunk_pc_max
            # Live rates from the previous sample window (cheap; reads
            # shared memory on each chunk without lock).
            if stats_arr is not None:
                live = _snapshot_stats(
                    stats_arr, load_slot_base, transform_slot_bases,
                    extract_slot_base, n_transform)
                live_e = _fmt_rate(live["extract"]["rows_s"])
                live_t = _fmt_rate(live["transform"]["rows_s"])
                live_l = _fmt_rate(live["load"]["rows_s"])
            else:
                live_e = live_t = live_l = "?"
            pbar.set_description(
                f"{source.name} [cs {source.chunksize/1000.0}k][{current}]"
                f"[rd {duplicates}]"
                f"[tq {transform_q.qsize()}][lq {load_q.qsize()}]"
                f"[E {live_e} T {live_t} L {live_l}]")
            # Cheap and unconditional when no budget is set. Between
            # chunks is the one place the parent is reliably not inside the
            # DBAPI, so it is where a slow-but-alive run notices its cap.
            if budget is not None:
                budget.check()
            # Stays armed for the whole run: `Source.import_query` carries
            # no ORDER BY, so a re-fetched row can surface in any chunk.
            # (The old "disable once a full chunk arrives clean" heuristic
            # assumed watermark-ordered delivery, which the source does not
            # provide -- that assumption is the bug this filter exists for.)
            if existing_pks and pk_column:
                overlap = set(chunk[pk_column]) & existing_pks
                if overlap:
                    duplicates += len(overlap)
                    chunk = chunk[~chunk[pk_column].isin(overlap)].copy()
            rows_total += len(chunk.index)
            # Auto-dim resolution runs in the parent before the put; the
            # time inside is foreign-DB latency, not extract itself —
            # track separately so extract_rows_per_s doesn't drop on
            # the first chunk with new dim values.
            t_auto_start = time.monotonic()
            if row_limit is not None and rows_total > row_limit:
                keep = row_limit - (rows_total - len(chunk.index))
                chunk = chunk.iloc[:keep].copy()
                rows_total = row_limit
                auto_dim_inserted += _resolve_auto_dim_chunk(
                    chunk, auto_dim_fact_to_table, auto_dim_maps,
                    auto_dim_null_idx, source.schema_name, dest_engine,
                    started)
                extract_auto_dim_s += time.monotonic() - t_auto_start
                t_put_start = time.monotonic()
                _put_with_abort(transform_q, chunk, abort_evt, error_q,
                                workers, budget=budget)
                _chunk_wait = time.monotonic() - t_put_start
                extract_wait += _chunk_wait
                # Parent never wrote its own slot before this — live["extract"]
                # was always None/"?" mid-run (only the terminal payload had
                # real extract numbers). _build_stats_payload(live=False)
                # overwrites this with the more accurate parent-side totals,
                # so this only improves the *live* view during the run.
                stats_arr[extract_slot_base + 0] += len(chunk.index)
                stats_arr[extract_slot_base + 1] += _chunk_busy
                stats_arr[extract_slot_base + 2] += _chunk_wait
                break
            auto_dim_inserted += _resolve_auto_dim_chunk(
                chunk, auto_dim_fact_to_table, auto_dim_maps,
                auto_dim_null_idx, source.schema_name, dest_engine,
                started)
            extract_auto_dim_s += time.monotonic() - t_auto_start
            t_put_start = time.monotonic()
            _put_with_abort(transform_q, chunk, abort_evt, error_q,
                                workers, budget=budget)
            _chunk_wait = time.monotonic() - t_put_start
            extract_wait += _chunk_wait
            stats_arr[extract_slot_base + 0] += len(chunk.index)
            stats_arr[extract_slot_base + 1] += _chunk_busy
            stats_arr[extract_slot_base + 2] += _chunk_wait
            if row_limit is not None and rows_total >= row_limit:
                break

        _join_with_abort(transform_q, abort_evt, error_q, workers,
                         budget=budget)
        _join_with_abort(load_q, abort_evt, error_q, workers, budget=budget)

        for _ in range(source.transform_procs):
            _put_with_abort(transform_q, None, abort_evt, error_q, workers,
                            budget=budget)
        _put_with_abort(load_q, None, abort_evt, error_q, workers)
    except EtlStageFailed as exc:
        status = "failed"
        error = f"{exc.stage}: {exc.message}"
        logger.error("%s: ETL run %s failed (%s stage)", source.name, run_id, exc.stage)
        raise
    except Exception as exc:
        status = "failed"
        error = repr(exc)
        logger.exception("%s: ETL run %s failed", source.name, run_id)
        raise
    finally:
        # Snapshot the manager-list before the workers go away. Aggregate by
        # (fact_col, source_value) — counts collected per chunk add up.
        unmapped_agg: dict[tuple[str, str], tuple[str, int]] = {}
        if manager is not None:
            for dim_table, fact_col, value, count in list(unmapped_log):
                key = (fact_col, value)
                prev = unmapped_agg.get(key)
                unmapped_agg[key] = (dim_table,
                                     (prev[1] if prev else 0) + count)

        # Stop the progress writer before the terminal UPDATE so the
        # `progress_*` snapshot we just wrote isn't immediately stomped
        # by a 5-s-late tick.
        if progress_stop is not None:
            progress_stop.set()
        if progress_thread is not None:
            progress_thread.join(timeout=2)

        # Release anything still blocked in the abort-aware queue helpers
        # and make sure the parent's own queue feeder threads don't hang
        # at interpreter exit trying to write into a pipe nobody drains
        # anymore (a queue with cancel_join_thread() not called can wedge
        # the process even after every worker has been killed below).
        abort_evt.set()
        for q in (transform_q, load_q):
            if q is None:
                continue
            q.cancel_join_thread()
            while True:
                try:
                    q.get_nowait()
                except Exception:
                    break
        error_q.cancel_join_thread()

        for w in workers:
            w.join(timeout=10)
            if w.is_alive():
                w.terminate()
                w.join(timeout=2)
            if w.is_alive():
                w.kill()
                w.join(timeout=1)
        if manager is not None:
            manager.shutdown()

        finished = datetime.now(timezone.utc)
        runtime_ms = int((finished - started).total_seconds() * 1000)

        # Roll up per-stage timings + per-worker breakdown + live rates
        # into the jsonb column. Reuse the same shape during the run so
        # the GUI can read one structure.
        stats_payload: Optional[dict] = None
        if stats_arr is not None:
            stats_payload = _build_stats_payload(
                stats_arr, load_slot_base, transform_slot_bases,
                extract_slot_base, n_transform,
                extract_busy, extract_wait, extract_auto_dim_s,
                duplicates, auto_dim_inserted, runtime_ms,
                rows_total, live=False)
        if preflight_report is not None:
            # stats is already jsonb and already surfaced by the GUI, so the
            # report needs no table of its own. Merged here rather than in
            # _build_stats_payload because a run blocked before the workers
            # started has no stats_arr at all.
            stats_payload = dict(stats_payload or {})
            stats_payload["preflight"] = preflight_report

        # Only worth reading the log on failure — a successful run has
        # nothing a GUI viewer needs that isn't already in stats/error.
        log_tail = tail_file(log_path) if (status == "failed" and log_path) else None

        with meta_engine.begin() as meta:
            meta.execute(text(
                "UPDATE starcubes_meta.etl_run "
                "   SET finished_at = :f, "
                "       last_end_time = :last, "
                "       rows = :r, "
                "       runtime_ms = :rt, "
                "       rows_per_s = :rps, "
                "       extract_rows_per_s = :ers, "
                "       transform_rows_per_s = :trs, "
                "       load_rows_per_s = :lrs, "
                "       stats = CAST(:stats AS jsonb), "
                "       progress_rows = NULL, "
                "       progress_at   = NULL, "
                "       status = :s, "
                "       error = :e, "
                "       log_tail = :log_tail, "
                "       max_wm = :max_wm, "
                "       src_now = :src_now "
                " WHERE id = :id"
            ), {"f": finished,
                "last": end_time if status == "ok" else None,
                "r": rows_total, "rt": runtime_ms,
                "rps": _rate(rows_total, runtime_ms / 1000.0),
                "ers": _extract_rate(rows_total, stats_payload),
                "trs": _transform_rate(rows_total, stats_payload),
                "lrs": _load_rate(rows_total, stats_payload),
                "stats": _json.dumps(stats_payload) if stats_payload else None,
                "s": status, "e": error, "log_tail": log_tail,
                # The data tip and the source's own clock at the moment this
                # run read them. Stored because their *difference* is the only
                # thing that can see a source which is up and has stopped
                # producing — see U9/N12 in docs/etl-automation.md.
                "max_wm": max_wm, "src_now": src_now,
                "id": run_id})
            if unmapped_agg:
                meta.execute(text(
                    "INSERT INTO starcubes_meta.etl_unmapped "
                    "  (etl_run_id, dim_table, fact_column, "
                    "   source_value, rows_count) "
                    "VALUES (:r, :d, :f, :v, :c)"
                ), [{"r": run_id, "d": dim_table, "f": fact_col,
                     "v": value, "c": total}
                    for (fact_col, value), (dim_table, total)
                    in unmapped_agg.items()])

            if status == "ok" and dedupe_pending is not None:
                # Matched on the value, not just the id: a rewind written
                # while this run was in flight is a different, still
                # unhonoured one and must survive.
                meta.execute(text(
                    "UPDATE starcubes_meta.datasource SET dedupe_from = NULL "
                    " WHERE id = :ds AND dedupe_from = :val"
                ), {"ds": source.id, "val": dedupe_pending})

        release_datasource_lock(lock_handle)

    summary_level = logging.DEBUG if quiet else logging.INFO
    extra = f" ({duplicates} duplicates filtered)" if duplicates else ""
    # Reload the just-written rates so the CLI summary mirrors the
    # GUI statusbar / history table.
    extract_rps = _extract_rate(rows_total, stats_payload)
    transform_rps = _transform_rate(rows_total, stats_payload)
    load_rps = _load_rate(rows_total, stats_payload)
    rates = (f" — overall {_fmt_rate(_rate(rows_total, runtime_ms/1000.0))}"
             f"  E={_fmt_rate(extract_rps)}"
             f"  T={_fmt_rate(transform_rps)}"
             f"  L={_fmt_rate(load_rps)}")
    logger.log(summary_level, "%s: loaded %d rows in %d ms%s%s; last watermark %s",
              source.name, rows_total, runtime_ms, extra, rates, current)
    if auto_dim_inserted:
        logger.log(summary_level, "%s: auto-discovered %d new dim row(s)",
                  source.name, auto_dim_inserted)
    if unmapped_agg:
        by_dim: dict[str, int] = {}
        for (_fc, _v), (dt, total) in unmapped_agg.items():
            by_dim[dt] = by_dim.get(dt, 0) + total
        summary = ", ".join(f"{dt}={n}" for dt, n
                            in sorted(by_dim.items()))
        logger.log(summary_level,
                  "%s: unmapped → __unknown__: %s (see starcubes_meta.etl_unmapped, run_id=%s)",
                  source.name, summary, run_id)

    if status == "ok":
        if backfill:
            # The backfill window overlaps rows an earlier run already
            # counted, so an increment would inflate the dim counts. They
            # only drive the chart's stacking order and are approximate by
            # design; `manage.py etl_run --refresh-counts` recomputes them.
            if rows_total:
                logger.info("%s: backfill inserted %d row(s); dim counts are "
                            "stale — run --refresh-counts to recompute",
                            source.name, rows_total)
                # Rows landed *behind* the high watermark, inside ranges the
                # server-side plot cache already considers final.
                bump_cache_epoch(meta_engine, source.id)
        else:
            # Deliberately the HWM, not extract_from: counting from the
            # overlap boundary would re-add the same rows on every pass.
            _increment_dim_counts(source, dest_engine, last_end_time, end_time)

    return rows_total


def _partition_upper_bound(source: Source, partition_name: str) -> datetime:
    """The exclusive upper bound of a partition, from its name.

    Partition boundaries are written as naive *local* times (see
    `_to_naive_local` in core/schema.py), so the aware form is the same
    conversion read backwards.
    """
    from .schema import _next_period, _parse_partition_name
    start = _parse_partition_name(source.partition_period, partition_name)
    return _next_period(start, source.partition_period).astimezone()


def _read_retention_floor(meta_engine, datasource_id: int) -> Optional[datetime]:
    """The floor as it stands right now.

    Read fresh rather than off the Source: `etl_run.py` loads the Source once
    before the forever loop, so a floor written by a GUI gesture mid-session
    would otherwise stay invisible until the session is restarted.
    """
    with meta_engine.begin() as meta:
        v = meta.execute(text(
            "SELECT retention_floor FROM starcubes_meta.datasource "
            " WHERE id = :ds"), {"ds": datasource_id}).scalar()
    return _as_utc(v) if v is not None else None


def _raise_retention_floor(source: Source, meta_engine,
                           floor: datetime, *, quiet: bool = False) -> None:
    """Record that rows below `floor` are gone on purpose.

    Never lowers an existing floor — disposal only ever moves forward.
    """
    with meta_engine.begin() as meta:
        meta.execute(text(
            "UPDATE starcubes_meta.datasource "
            "   SET retention_floor = greatest("
            "         coalesce(retention_floor, :f), :f) "
            " WHERE id = :ds"), {"ds": source.id, "f": floor})

    # The rollups are deliberately *not* clamped here. Their rows were
    # computed from the raw data while it existed, so they stay correct after
    # disposal -- and at roughly two orders of magnitude smaller than `fact`,
    # keeping them is close to free. Aggregates outliving the rows they came
    # from is the point, not an accident: retention can be aggressive on
    # `fact` while the chart keeps years of counts.
    #
    # The consequence is that below `retention_floor` a rollup is no longer a
    # cache but the *only* copy. `rollup_drop` and `rollup_build --rebuild`
    # know this and refuse to destroy that span without --force, and
    # `chart_meta` widens the offered range to reach it.

    if not quiet:
        logger.info("%s: retention floor now %s", source.name, floor)


def _retire_partition(source: Source, dest_engine, meta_engine,
                      partition_name: str, *, quiet: bool = False) -> None:
    """Move the retention floor for a partition that just went away.

    Only when it was the *oldest* one. A partition removed out of the middle
    leaves a hole that a single floor cannot express, and pretending
    otherwise would tell the audit that everything below it is gone. Say so
    and leave the floor where it is instead.
    """
    from .schema import list_fact_partitions
    remaining = list_fact_partitions(source, dest_engine)
    if remaining and remaining[0] < partition_name:
        logger.warning(
            "%s: %s was not the oldest partition — the retention floor is "
            "left alone, because a hole in the middle of the fact table "
            "cannot be expressed as a lower bound. The audit will report "
            "that window as a gap.", source.name, partition_name)
        return
    _raise_retention_floor(source, meta_engine,
                           _partition_upper_bound(source, partition_name),
                           quiet=quiet)


def rewind_highwatermark(source: Source, meta_engine, target: datetime, *,
                         reason: str, quiet: bool = False) -> Optional[datetime]:
    """Move the high watermark back to `target`, so the window is reloaded.

    This is the repair half of "deleting means two opposite things": rows
    were wrong, they were thrown away, and the ETL has to fetch them again.
    Disposal (`drop_partition` and friends) wants the exact opposite and
    writes a retention floor instead — hence the guard below, which refuses
    a rewind under the floor. Without it, retention routed through
    `delete_fact_from` would have the ETL reload everything the operator
    just disposed of, on the next iteration, at full throughput.

    Written as a **new** etl_run row rather than an UPDATE of an old one:
    the run history stays truthful, and `_resume_state` orders by
    `finished_at DESC`, so a fresh row wins even with a *lower*
    `last_end_time`. Returns the new watermark, or None if there was none
    to move.
    """
    target = _as_utc(target).replace(microsecond=0)
    floor = _read_retention_floor(meta_engine, source.id)
    if floor is not None and target < floor:
        raise ValueError(
            f"{source.name}: refusing to rewind the high watermark to "
            f"{target}, below the retention floor {floor}. Rows down there "
            f"were disposed of on purpose; a rewind would reload them.")

    now = datetime.now(timezone.utc)
    with meta_engine.begin() as meta:
        prev = meta.execute(text(
            "SELECT last_end_time FROM starcubes_meta.etl_run "
            " WHERE datasource_id = :ds AND status = 'ok' "
            "   AND kind = 'incremental' "
            " ORDER BY finished_at DESC LIMIT 1"
        ), {"ds": source.id}).scalar()
        if prev is None:
            if not quiet:
                logger.info("%s: no high watermark to rewind", source.name)
            return None
        prev = _as_utc(prev)
        if prev <= target:
            if not quiet:
                logger.info("%s: high watermark %s is already at or below "
                            "%s — not rewinding", source.name, prev, target)
            return prev
        stats = _json.dumps({"rewind": {"from": prev.isoformat(),
                                        "to": target.isoformat(),
                                        "reason": reason}})
        meta.execute(text(
            "INSERT INTO starcubes_meta.etl_run "
            "  (datasource_id, started_at, finished_at, "
            "   first_end_time, last_end_time, "
            "   rows, runtime_ms, status, kind, stats) "
            "VALUES (:ds, :t, :t, :first, :last, 0, 0, 'ok', 'incremental', "
            "        CAST(:stats AS jsonb))"
        ), {"ds": source.id, "t": now, "first": prev, "last": target,
            "stats": stats})
    logger.warning("%s: high watermark rewound from %s to %s (%s)",
                   source.name, prev, target, reason)
    return target


def _arm_dedupe(source: Source, meta_engine, from_time: datetime, *,
                quiet: bool = False) -> bool:
    """Tell the next incremental run to filter re-loaded rows by PK.

    A rewind reloads a window and there is no unique index on `fact` to
    absorb it. Three cases, and only the middle one needs arming:

    * ``partition_column == watermark_fact_sql()`` — the delete removed
      exactly what will be re-inserted, so the reload is clean.
    * they differ — rows below the delete boundary on the partition clock
      but above it on the watermark clock survived the delete and *will*
      come back. `_overlap_pks` already knows how to drop them; this is
      what arms it.
    * they differ and there is no ``role='pk'`` fact column — nothing can
      match the rows, so say so rather than silently duplicating them.
    """
    if source.watermark_fact_sql() == source.partition_column:
        return False
    if source.pk_column() is None:
        logger.warning(
            "%s: the watermark (%s) and the partition column (%s) differ and "
            "no fact column has role='pk', so the reload after this delete "
            "cannot be de-duplicated — it may insert duplicate rows. Set the "
            "pk role to close this.",
            source.name, source.watermark_fact_sql(), source.partition_column)
        return False
    with meta_engine.begin() as meta:
        meta.execute(text(
            "UPDATE starcubes_meta.datasource "
            "   SET dedupe_from = least(coalesce(dedupe_from, :f), :f) "
            " WHERE id = :ds"), {"ds": source.id, "f": from_time})
    if not quiet:
        logger.info("%s: armed PK de-duplication from %s for the reload",
                    source.name, from_time)
    return True


def truncate_last_partition(source: Source, dest_engine, *, meta_engine,
                            quiet: bool = False) -> str:
    """TRUNCATE the oldest fact partition.  Returns the partition name.

    Warns if only one partition exists (TRUNCATE would empty the
    entire fact table). Takes the datasource lock: removing rows under a
    running load is exactly the hand-run gesture that corrupts data.

    A disposal gesture — it raises the retention floor and leaves the high
    watermark alone.
    """
    from .schema import list_fact_partitions
    with datasource_lock(source.dest_dsn, source.id, holder="maintenance"):
        parts = list_fact_partitions(source, dest_engine)
        if not parts:
            raise RuntimeError(f"{source.schema_name}.fact has no partitions")
        name = parts[0]
        if len(parts) == 1 and not quiet:
            logger.warning("%s is the only partition — TRUNCATE empties the entire fact table", name)
        with dest_engine.begin() as conn:
            conn.execute(text(f"TRUNCATE {source.schema_name}.{name}"))
        _raise_retention_floor(source, meta_engine,
                               _partition_upper_bound(source, name),
                               quiet=quiet)
    bump_cache_epoch(meta_engine, source.id)
    if not quiet:
        logger.info("%s: truncated partition %s", source.name, name)
    return name


def truncate_partition(source: Source, dest_engine, partition_name: str,
                       *, meta_engine, quiet: bool = False) -> None:
    """TRUNCATE a named fact partition. Validates it belongs to the schema.

    A disposal gesture: the retention floor moves, the high watermark does
    not. See `_retire_partition` for why only the oldest partition moves it.
    """
    schema = source.schema_name
    with datasource_lock(source.dest_dsn, source.id, holder="maintenance"):
        with dest_engine.connect() as conn:
            exists = relation_exists(conn, schema, partition_name)
        if not exists:
            raise RuntimeError(f"Partition {schema}.{partition_name} not found")
        with dest_engine.begin() as conn:
            conn.execute(text(f"TRUNCATE {schema}.{partition_name}"))
        _retire_partition(source, dest_engine, meta_engine, partition_name,
                          quiet=quiet)
    bump_cache_epoch(meta_engine, source.id)
    if not quiet:
        logger.info("%s: truncated partition %s", source.name, partition_name)


def drop_partition(source: Source, dest_engine, partition_name: str,
                   *, meta_engine, quiet: bool = False) -> None:
    """DETACH and DROP a named fact partition.

    A disposal gesture: the retention floor moves, the high watermark does
    not. See `_retire_partition` for why only the oldest partition moves it.
    """
    schema = source.schema_name
    with datasource_lock(source.dest_dsn, source.id, holder="maintenance"):
        with dest_engine.connect() as conn:
            exists = relation_exists(conn, schema, partition_name)
        if not exists:
            raise RuntimeError(f"Partition {schema}.{partition_name} not found")
        with dest_engine.begin() as conn:
            conn.execute(text(
                f"ALTER TABLE {schema}.fact DETACH PARTITION {schema}.{partition_name}"))
            conn.execute(text(f"DROP TABLE {schema}.{partition_name}"))
        _retire_partition(source, dest_engine, meta_engine, partition_name,
                          quiet=quiet)
    bump_cache_epoch(meta_engine, source.id)
    if not quiet:
        logger.info("%s: dropped partition %s", source.name, partition_name)


def delete_fact_from(source: Source, dest_engine, from_time: datetime, *,
                     meta_engine, quiet: bool = False) -> int:
    """Delete all fact rows with ``partition_column >= from_time``.

    Returns the number of rows deleted. Refuses while an ETL holds the
    datasource lock.

    This is the *repair* gesture: the data is wrong, throw it away, load it
    again. So it also rewinds the high watermark to `from_time` — without
    that, the next window would start above the hole and the deleted rows
    would never be requested again — arms PK de-duplication for the reload
    where the two clocks make it necessary, and bumps the cache epoch,
    since the rows it removed sit behind the watermark and the plot cache
    considers that range final.
    """
    if from_time.tzinfo is None:
        from_time = from_time.replace(tzinfo=timezone.utc)
    with datasource_lock(source.dest_dsn, source.id, holder="maintenance"):
        with dest_engine.begin() as conn:
            n = conn.execute(text(
                f"DELETE FROM {source.schema_name}.fact "
                f" WHERE {source.partition_column} >= :from_time"
            ), {"from_time": from_time}).rowcount
        # Arm before rewinding: the run that picks up the rewound watermark
        # has to find the flag already set.
        _arm_dedupe(source, meta_engine, from_time, quiet=quiet)
        rewind_highwatermark(source, meta_engine, from_time,
                             reason="delete_fact_from", quiet=quiet)
        # The rollups have to forget the same span. A rewind, not an
        # invalidation: the stale buckets simply fall outside
        # [built_from, built_through) until the next ETL hook rebuilds them
        # from the reloaded rows, so no rebuild has to be scheduled and
        # nothing below the rewind point is disturbed.
        try:
            from .rollup import rewind_rollup_state
            rewind_rollup_state(meta_engine, source.id, from_time)
        except Exception:
            logger.exception("%s: could not rewind the rollups to %s -- run "
                             "`manage.py rollup_build --rebuild`",
                             source.name, from_time)
    bump_cache_epoch(meta_engine, source.id)
    if not quiet:
        logger.info("%s: deleted %d rows from %s", source.name, n, from_time)
    return n


def clear_etl_history(source: Source, meta_engine, *,
                      quiet: bool = False) -> int:
    """Delete all etl_run rows for this datasource.

    ``etl_unmapped`` rows are cascade-deleted automatically.
    """
    with meta_engine.begin() as conn:
        n = conn.execute(text(
            "DELETE FROM starcubes_meta.etl_run WHERE datasource_id = :ds"
        ), {"ds": source.id}).rowcount
    if not quiet:
        logger.info("%s: cleared %d etl_run row(s)", source.name, n)
    return n


def set_highwatermark(source: Source, dest_engine, meta_engine, *,
                      quiet: bool = False) -> datetime:
    """Insert an etl_run row with ``status='ok'`` and
    ``last_end_time = max(partition_column)`` from the fact table.

    Returns the new high watermark.  Raises ``RuntimeError`` if the
    fact table is empty.
    """
    pc = source.partition_column
    with dest_engine.begin() as conn:
        hwm = conn.execute(text(
            f"SELECT max({pc}) FROM {source.schema_name}.fact"
        )).scalar()
    if hwm is None:
        raise RuntimeError(f"{source.schema_name}.fact is empty — cannot set HWM")
    if hwm.tzinfo is None:
        hwm = hwm.replace(tzinfo=timezone.utc)

    hwm = _ceil_second(hwm)

    now = datetime.now(timezone.utc)
    # Find the previous HWM (last ok row) for first_end_time.
    with meta_engine.begin() as conn:
        prev = conn.execute(text(
            "SELECT last_end_time FROM starcubes_meta.etl_run "
            " WHERE datasource_id = :ds AND status = 'ok' "
            " ORDER BY finished_at DESC LIMIT 1"
        ), {"ds": source.id}).scalar()
    with meta_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO starcubes_meta.etl_run "
            "  (datasource_id, started_at, finished_at, "
            "   first_end_time, last_end_time, "
            "   rows, runtime_ms, status) "
            "VALUES (:ds, :t, :t, :first, :last, 0, 0, 'ok')"
        ), {"ds": source.id, "t": now, "first": prev, "last": hwm})
    if not quiet:
        logger.info("%s: high watermark set to %s", source.name, hwm)
    return hwm
