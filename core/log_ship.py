"""Ship a process's log from its local spool file into `starcubes_meta.log_line`.

A spawned ETL, forever or rollup process writes its stdout and stderr to a
spool file (sources/etl_spawn.py) — the only way to keep what no Python
logging handler sees: `self.stdout.write`, a traceback printed on the way out,
faulthandler's dump of a crashed interpreter. This module moves those lines
into the one log table every container can read (migration 0046).

**Two shippers, one protocol.**

- `LogShipper` is a thread inside the process itself
  (`sources/etl_logging.configure_etl_logging` starts it). Once a second it
  reads the complete lines appended since its last offset and inserts them;
  at exit it ships the rest, including a last line without a newline, and
  removes the spool file.
- `sweep_spool_dir` is called by the container's `node_agent` for files whose
  process died without that last step (SIGKILL, OOM, a segfault). It ships
  what is left from the recorded offset and removes the file.

They cannot both ship one file: the thread holds an exclusive `flock` on the
spool file for its whole life, the kernel releases it when the process dies,
and the sweep only touches a file it can lock itself. The offset and the
metadata (kind, datasource, run_ref) live in a sidecar `<spool>.json` the
spawner writes first, so the sweep knows what a file belongs to even if the
process never got as far as starting its thread.

**What a line gets.** `level` is parsed from the log format sources use
(`<dd/Mon/yyyy HH:MM:SS> LEVEL name pid=… message`). A traceback, or
faulthandler's dump, is a block of lines without that prefix; they inherit
the level that opened the block, so a filter on ERROR shows the whole
traceback. Other unprefixed output is raw and keeps `level` NULL. `ts` is the
shipping time: the format has seconds and no time zone, and a shipment is at
most a second late.

**It must never hurt the run.** Every database error is swallowed and retried
on the next tick; the lines wait in the spool file meanwhile. The spool file
itself is bounded by `core.etl._bound_log_size`; when it is truncated the
shipper notices the shrink and starts again at offset 0.

**Retention.** `STARCUBES_LOG_MAX_LINES_PER_RUN` (default 200 000) caps one
run's rows — past it the oldest go and one marker line says how many.
`purge_old_lines` applies `STARCUBES_LOG_RETENTION_DAYS` (default 14) and is
called by the node_agent.

No Django imports — same constraint as the rest of `core/`.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import threading
import time
from typing import Optional

from sqlalchemy import text

from .node import hostname
from .pglock import open_dedicated

logger = logging.getLogger(__name__)

KINDS = ("etl", "backfill", "forever",
         "rollup_advise", "rollup_build", "rollup_verify")
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

SHIP_INTERVAL_S = 1.0
BATCH_LINES = 5000
MAX_LINE_CHARS = 8000
# A spool file nobody holds a lock on is only swept once it has been quiet
# this long: a just-spawned process has not started its shipper yet.
SWEEP_QUIET_S = 60

_PREFIX_RE = re.compile(
    r"^\d{2}/[A-Za-z]{3}/\d{4} \d{2}:\d{2}:\d{2} "
    r"(DEBUG|INFO|WARNING|ERROR|CRITICAL)\b")
# Lines that continue a traceback-like block opened by a WARNING+ record.
_CONTINUATION_RE = re.compile(
    r"^(\s|Traceback \(most recent call last\)|During handling of|"
    r"The above exception|[A-Za-z_][\w.]*(Error|Exception|Exit|Interrupt|"
    r"Aborted|Failed|Locked|Lost)\b)")
_FATAL_RE = re.compile(r"^(Fatal Python error|Current thread|Thread 0x)")


def max_lines_per_run() -> int:
    return int(os.environ.get("STARCUBES_LOG_MAX_LINES_PER_RUN", "200000"))


def retention_days() -> int:
    return int(os.environ.get("STARCUBES_LOG_RETENTION_DAYS", "14"))


def sidecar_path(spool_path: str) -> str:
    return spool_path + ".json"


def write_sidecar(spool_path: str, *, kind: str, datasource_id: Optional[int],
                  offset: int = 0) -> None:
    """Record what a spool file belongs to, and how far it has been shipped.

    Written atomically (rename), so a crash mid-write leaves the old value.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown log kind {kind!r}")
    data = {"kind": kind, "datasource_id": datasource_id, "offset": int(offset),
            "run_ref": os.path.basename(spool_path)}
    tmp = sidecar_path(spool_path) + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh)
    os.replace(tmp, sidecar_path(spool_path))


def read_sidecar(spool_path: str) -> Optional[dict]:
    try:
        with open(sidecar_path(spool_path)) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if data.get("kind") in KINDS else None


class _Classifier:
    """Assigns a level to each line; carries the open block across calls."""

    def __init__(self):
        self._block_level: Optional[str] = None

    def level(self, line: str) -> Optional[str]:
        m = _PREFIX_RE.match(line)
        if m:
            level = m.group(1)
            self._block_level = level if level in ("WARNING", "ERROR", "CRITICAL") else None
            return level
        if _FATAL_RE.match(line):
            self._block_level = "CRITICAL"
            return "CRITICAL"
        if self._block_level and line and _CONTINUATION_RE.match(line):
            return self._block_level
        if line.strip() == "" and self._block_level:
            return self._block_level
        self._block_level = None
        return None


class _Reader:
    """Reads complete lines from a spool file from a byte offset on."""

    def __init__(self, path: str, offset: int):
        self.path = path
        self.offset = offset
        self.classifier = _Classifier()

    def read(self, *, final: bool) -> list[tuple[Optional[str], str]]:
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return []
        if size < self.offset:
            # _bound_log_size truncated the file in place; start over.
            self.offset = 0
        if size == self.offset:
            return []
        with open(self.path, "rb") as fh:
            fh.seek(self.offset)
            data = fh.read(4 * 1024 * 1024)
        if not final:
            cut = data.rfind(b"\n")
            if cut < 0:
                return []
            data = data[:cut + 1]
        self.offset += len(data)
        out = []
        for raw in data.decode("utf-8", errors="replace").splitlines():
            line = raw[:MAX_LINE_CHARS]
            out.append((self.classifier.level(line), line))
        return out


def _insert(conn, lines, *, kind, datasource_id, run_ref, host) -> None:
    for start in range(0, len(lines), BATCH_LINES):
        chunk = lines[start:start + BATCH_LINES]
        conn.execute(text(
            "INSERT INTO starcubes_meta.log_line "
            "  (datasource_id, kind, run_ref, hostname, level, line) "
            "VALUES (:ds, :kind, :run, :host, :level, :line)"),
            [{"ds": datasource_id, "kind": kind, "run": run_ref, "host": host,
              "level": level, "line": line} for level, line in chunk])


def _cap_run(conn, run_ref: str, cap: int) -> int:
    """Delete a run's oldest rows beyond `cap`. Returns how many went."""
    boundary = conn.execute(text(
        "SELECT id FROM starcubes_meta.log_line WHERE run_ref = :r "
        " ORDER BY id DESC OFFSET :cap LIMIT 1"), {"r": run_ref, "cap": cap}).scalar()
    if boundary is None:
        return 0
    return conn.execute(text(
        "DELETE FROM starcubes_meta.log_line WHERE run_ref = :r AND id <= :b"),
        {"r": run_ref, "b": boundary}).rowcount


class LogShipper:
    """The in-process shipper. `start()` once; `stop()` at exit."""

    def __init__(self, meta_dsn: str, spool_path: str):
        meta = read_sidecar(spool_path)
        if meta is None:
            raise ValueError(f"no sidecar for {spool_path}")
        self.meta_dsn = meta_dsn
        self.spool_path = spool_path
        self.kind = meta["kind"]
        self.datasource_id = meta.get("datasource_id")
        self.run_ref = meta["run_ref"]
        self.host = hostname()
        self.reader = _Reader(spool_path, int(meta.get("offset") or 0))
        self.cap = max_lines_per_run()
        self._since_cap_check = 0
        self._conn = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock_fh = None
        self._pending: list = []

    # ── lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> "LogShipper":
        self._lock_fh = open(self.spool_path, "rb")
        # Held for the life of the process; the node_agent's sweep only
        # touches files nobody holds.
        fcntl.flock(self._lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        self._thread = threading.Thread(target=self._run, name="log-shipper",
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        """Ship everything left, then remove the spool file and its sidecar."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        shipped_all = self._ship(final=True)
        if shipped_all:
            for path in (self.spool_path, sidecar_path(self.spool_path)):
                try:
                    os.unlink(path)
                except OSError:
                    pass
        self._close()
        if self._lock_fh is not None:
            self._lock_fh.close()

    # ── work ───────────────────────────────────────────────────────────────

    def _run(self) -> None:
        while not self._stop.wait(SHIP_INTERVAL_S):
            self._ship(final=False)

    def _ship(self, *, final: bool) -> bool:
        """Ship until caught up. True when nothing is left unshipped.

        Lines read but not yet committed stay in `_pending`, so a failed
        insert is retried with the same lines on the next call; the sidecar
        offset only moves once nothing is pending, which keeps it equal to
        what has been committed.
        """
        try:
            while True:
                if not self._pending:
                    self._pending = self.reader.read(final=final)
                    if not self._pending:
                        return True
                batch = self._pending[:BATCH_LINES]
                conn = self._connection()
                _insert(conn, batch, kind=self.kind,
                        datasource_id=self.datasource_id,
                        run_ref=self.run_ref, host=self.host)
                self._since_cap_check += len(batch)
                if self._since_cap_check >= max(1000, self.cap // 20):
                    self._since_cap_check = 0
                    dropped = _cap_run(conn, self.run_ref, self.cap)
                    if dropped:
                        _insert(conn, [("WARNING",
                                        f"--- {dropped} older line(s) of this log "
                                        f"dropped: STARCUBES_LOG_MAX_LINES_PER_RUN="
                                        f"{self.cap} ---")],
                                kind=self.kind, datasource_id=self.datasource_id,
                                run_ref=self.run_ref, host=self.host)
                conn.commit()
                del self._pending[:len(batch)]
                self._record_offset()
        except Exception:
            logger.debug("log shipping failed; the lines stay pending and in "
                         "the spool file, and are retried", exc_info=True)
            self._close()
            return False

    def _record_offset(self) -> None:
        # Only with nothing pending is the reader's offset also what has been
        # committed; the sweep resumes from this value after a crash.
        if self._pending:
            return
        try:
            write_sidecar(self.spool_path, kind=self.kind,
                          datasource_id=self.datasource_id,
                          offset=self.reader.offset)
        except OSError:
            pass

    def _connection(self):
        if self._conn is None:
            self._conn = open_dedicated(self.meta_dsn)
        return self._conn.conn

    def _close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None


def sweep_spool_dir(meta_engine, spool_dir: str) -> int:
    """Ship and remove spool files whose process is gone. Returns files done.

    A file is swept only if nobody holds its lock and it has been quiet for
    `SWEEP_QUIET_S`. A file without a sidecar (not written by the spawner)
    is left alone.
    """
    done = 0
    try:
        names = sorted(os.listdir(spool_dir))
    except OSError:
        return 0
    host = hostname()
    for name in names:
        if not name.endswith(".log"):
            continue
        path = os.path.join(spool_dir, name)
        meta = read_sidecar(path)
        if meta is None:
            continue
        try:
            if time.time() - os.path.getmtime(path) < SWEEP_QUIET_S:
                continue
            fh = open(path, "rb")
        except OSError:
            continue
        try:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                continue  # its process is alive and shipping
            reader = _Reader(path, int(meta.get("offset") or 0))
            lines = reader.read(final=True)
            with meta_engine.begin() as conn:
                if lines:
                    _insert(conn, lines, kind=meta["kind"],
                            datasource_id=meta.get("datasource_id"),
                            run_ref=meta["run_ref"], host=host)
                _insert(conn, [("WARNING", "--- the process ended without "
                                "shipping its log; the rest was recovered by "
                                "this container's node_agent ---")],
                        kind=meta["kind"], datasource_id=meta.get("datasource_id"),
                        run_ref=meta["run_ref"], host=host)
            for p in (path, sidecar_path(path)):
                try:
                    os.unlink(p)
                except OSError:
                    pass
            done += 1
        except Exception:
            logger.warning("sweeping spool file %s failed", path, exc_info=True)
        finally:
            fh.close()
    return done


def purge_old_lines(meta_engine, *, batch: int = 50000, max_batches: int = 20) -> int:
    """Delete lines older than `STARCUBES_LOG_RETENTION_DAYS`, in batches.

    Batched so a first purge on a large table holds no long lock; whatever is
    left goes on the next call. Several containers purging at once is
    harmless — a row deleted twice is deleted once.
    """
    days = retention_days()
    total = 0
    for _ in range(max_batches):
        with meta_engine.begin() as conn:
            n = conn.execute(text(
                "DELETE FROM starcubes_meta.log_line WHERE id IN ("
                "  SELECT id FROM starcubes_meta.log_line "
                "   WHERE ts < now() - make_interval(days => :d) "
                "   ORDER BY ts LIMIT :b)"), {"d": days, "b": batch}).rowcount
        total += n
        if n < batch:
            break
    return total
