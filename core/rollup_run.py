"""Run history for the three long rollup commands.

`rollup_advise`, `rollup_build` and `rollup_verify` run for minutes to hours.
That rules out running them inside a web request — uwsgi's `harakiri` kills
the whole worker process at 120s, taking its other in-flight requests with it
— so the GUI spawns them the way it spawns an ETL run, detached and with a log
file. A detached process needs somewhere to say what it is doing and what it
found, and that is `starcubes_meta.rollup_run`.

The row is written by the *command*, not by the view that spawned it. That is
what makes a run started from a shell appear in the GUI history exactly like
one started from a button, and it is the same arrangement `core/etl.py` has
with `etl_run`.

Two of the three commands produce a result worth keeping past the run:
`advise` returns a ladder of candidate column sets the operator picks a stage
from, and `verify` returns a pass/skip/fail matrix. Both land in `report` as
JSON. A `build` has no report — its result is `rollup_state`.

No Django imports: takes a SQLAlchemy engine like the rest of `core/`.
"""
from __future__ import annotations

import json
import logging
import os
import signal
from typing import Optional

from sqlalchemy import text

from core.utils import _pid_alive, tail_file

logger = logging.getLogger(__name__)

KINDS = ("advise", "build", "verify")


def _json(value) -> Optional[str]:
    """Serialise for a jsonb column, tolerating datetimes and dataclasses."""
    if value is None:
        return None
    return json.dumps(value, default=str)


def install_abort_handler() -> None:
    """Turn SIGTERM/SIGINT into a KeyboardInterrupt.

    Without this the default SIGTERM disposition kills the command outright,
    the `finish_run` never happens, and the row is left on 'running'. Nothing
    reaps it either: the spawning process (a uwsgi worker, or runserver) never
    waits on the child, so the pid lingers as a zombie and `os.kill(pid, 0)`
    — the only portable liveness test there is — reports it alive. `etl_run`
    solves the same problem the same way, by raising instead of dying.

    KeyboardInterrupt rather than a custom exception on purpose: `rollup_build`
    already treats it as "stop here, what is built is valid", which is exactly
    true of a killed backfill — every batch has committed.
    """
    def _abort(signum, _frame):
        raise KeyboardInterrupt(f"killed by user (signal {signum})")
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _abort)
        except ValueError:
            # Not the main thread — nothing to install, and nothing that
            # would have been signalled anyway.
            pass


def start_run(meta_engine, datasource_id: int, kind: str, *,
              args: Optional[dict] = None,
              log_path: Optional[str] = None) -> int:
    """Insert the `running` row and return its id.

    The pid recorded is this process's own — the command writes its own row,
    so `os.getpid()` is exactly the process a kill button has to signal.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown rollup run kind: {kind!r}")
    with meta_engine.begin() as conn:
        return conn.execute(text("""
            INSERT INTO starcubes_meta.rollup_run
                   (datasource_id, kind, status, started_at, pid, args, log_path)
            VALUES (:ds, :kind, 'running', now(), :pid,
                    CAST(:args AS jsonb), :log_path)
            RETURNING id
        """), {"ds": datasource_id, "kind": kind, "pid": os.getpid(),
               "args": _json(args),
               "log_path": log_path or os.environ.get("STARCUBES_ETL_LOG_PATH")}
        ).scalar_one()


def progress(meta_engine, run_id: Optional[int], line: str) -> None:
    """Record the latest progress line.

    Best-effort by design: a backfill that loses its meta connection should
    keep aggregating, not abort. The caller has already printed the same line
    to stdout, so nothing is lost but the live display.
    """
    if run_id is None:
        return
    try:
        with meta_engine.begin() as conn:
            conn.execute(text(
                "UPDATE starcubes_meta.rollup_run "
                "   SET progress = :line, progress_at = now() "
                " WHERE id = :id"), {"id": run_id, "line": line[:2000]})
    except Exception:
        logger.debug("rollup_run %s: progress update failed", run_id,
                     exc_info=True)


def finish_run(meta_engine, run_id: Optional[int], status: str, *,
               report: Optional[dict] = None,
               error: Optional[str] = None,
               log_path: Optional[str] = None) -> None:
    """Close the row out. `status` is 'ok' or 'failed'."""
    if run_id is None:
        return
    with meta_engine.begin() as conn:
        conn.execute(text("""
            UPDATE starcubes_meta.rollup_run
               SET status = :status, finished_at = now(),
                   report = COALESCE(CAST(:report AS jsonb), report),
                   error = :error,
                   log_tail = :tail
             WHERE id = :id
        """), {"id": run_id, "status": status, "report": _json(report),
               "error": (error or None), "tail": _read_tail(log_path)})


def _read_tail(log_path: Optional[str]) -> Optional[str]:
    """The run's log tail, the same shape `etl_run.log_tail` carries."""
    path = log_path or os.environ.get("STARCUBES_ETL_LOG_PATH")
    return tail_file(path) if path else None


def _alive(pid: Optional[int]) -> bool:
    """`_pid_alive`, minus zombies.

    A process that has exited but not been waited on still answers signal 0,
    so `_pid_alive` alone calls it running forever — and nothing here ever
    waits on these children. /proc settles it on Linux; where /proc is absent
    the answer falls back to the signal test, which is what the rest of the
    codebase uses.
    """
    if not pid:
        return False
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            # State is the field after the comm field, which may itself
            # contain spaces and is therefore parenthesised.
            return fh.read().rpartition(b")")[2].split()[0:1] != [b"Z"]
    except FileNotFoundError:
        return False
    except OSError:
        pass
    return _pid_alive(pid)


def reap_stale(meta_engine, datasource_id: int) -> int:
    """Mark `running` rows whose process is gone as failed. Returns the count.

    A pid check rather than the age heuristic `etl_run` uses: an advise never
    runs an hour and a backfill routinely runs longer than one, so no single
    timeout separates the two. The pid does — with the caveat that it is only
    meaningful on the host that spawned the run, which is also true of the
    kill button and of `etl_resume_forever`.
    """
    with meta_engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT id, pid FROM starcubes_meta.rollup_run "
            " WHERE datasource_id = :ds AND status = 'running'"),
            {"ds": datasource_id}).mappings().all()
        dead = [r["id"] for r in rows if not _alive(r["pid"])]
        for run_id in dead:
            conn.execute(text(
                "UPDATE starcubes_meta.rollup_run "
                "   SET status = 'failed', finished_at = now(), "
                "       error = 'stale run: process did not update status' "
                " WHERE id = :id"), {"id": run_id})
    return len(dead)


def running_run(meta_engine, datasource_id: int) -> Optional[dict]:
    """The live run for this datasource, or None. Call `reap_stale` first."""
    with meta_engine.begin() as conn:
        row = conn.execute(text(
            "SELECT id, kind, started_at, pid, progress, progress_at "
            "  FROM starcubes_meta.rollup_run "
            " WHERE datasource_id = :ds AND status = 'running' "
            " ORDER BY started_at DESC LIMIT 1"),
            {"ds": datasource_id}).mappings().one_or_none()
    return dict(row) if row else None


def recent_runs(meta_engine, datasource_id: int, limit: int = 20) -> list[dict]:
    """The last `limit` runs, newest first, without the report payloads.

    `report` is deliberately excluded: an advise report carries every
    dimension and every ladder stage, and the history table shows none of it.
    `last_report` fetches the one that is actually displayed.
    """
    with meta_engine.begin() as conn:
        rows = conn.execute(text("""
            SELECT id, kind, status, started_at, finished_at, pid,
                   args, progress, progress_at, error, log_path, log_tail
              FROM starcubes_meta.rollup_run
             WHERE datasource_id = :ds
             ORDER BY started_at DESC
             LIMIT :limit
        """), {"ds": datasource_id, "limit": limit}).mappings().all()
    return [dict(r) for r in rows]


def last_report(meta_engine, datasource_id: int, kind: str) -> Optional[dict]:
    """The newest successful report of this kind, with the run around it."""
    with meta_engine.begin() as conn:
        row = conn.execute(text("""
            SELECT id, started_at, finished_at, args, report
              FROM starcubes_meta.rollup_run
             WHERE datasource_id = :ds AND kind = :kind
               AND status = 'ok' AND report IS NOT NULL
             ORDER BY started_at DESC LIMIT 1
        """), {"ds": datasource_id, "kind": kind}).mappings().one_or_none()
    return dict(row) if row else None
