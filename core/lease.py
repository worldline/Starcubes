"""Process leases: whether a run's process is alive, as every container sees it.

`os.kill(pid, 0)` answers that question only inside one PID namespace. With two
containers on one database each judged the other's runs dead, so this module
replaces it everywhere a run's liveness is decided.

**A lease is one database connection.** A managed process — `etl_run` in any
mode, the rollup commands — opens a connection of its own when it starts and
keeps it until it exits. Its `(pg_backend_pid(), backend_start)` is written onto
every run row the process owns (`lease_pid`, `lease_start`), next to
`instance_id` and `hostname`. The process is alive exactly as long as that pair
is in `pg_stat_activity`: Postgres removes the backend on a clean exit, on
SIGKILL or OOM (the socket closes), and after the TCP keepalives set by
`core.pglock` when the host itself is gone. There is no heartbeat to write and
no timeout to tune. It is the principle `core.adhoc.cancel_query` already relies
on, and like it, it needs every container to use the same database role:
`pg_stat_activity` hides another role's `backend_start`.

**A lost lease ends the process.** A network partition can cut the lease
connection while the process keeps running — to every other container it is
dead, and one of them may already be taking its forever session over.
`Lease.check()` notices (the connection's next statement fails) and callers
abort rather than keep writing.

**Legacy rows.** A row an older image wrote has `lease_pid IS NULL`. Nobody can
judge those from another container, so `reap_dead_runs` leaves the old rules in
place for them and for nothing else.

No Django imports — same constraint as the rest of `core/`.
"""
from __future__ import annotations

import atexit
import logging
import os
from typing import Optional

from sqlalchemy import text

from .node import hostname, instance_id, short_instance_id
from .pglock import open_dedicated

logger = logging.getLogger(__name__)

APPLICATION_NAME_PREFIX = "starcubes/"

# A forever row the GUI inserts before its subprocess exists has no lease yet.
# Past this age with still no pid and no lease, the subprocess never started.
STARTING_GRACE_S = 60

# LISTEN/NOTIFY channel for stop requests (migration 0043). The payload is
# the instance id of the container that runs the process; every node_agent
# listens and acts only on its own processes.
CONTROL_CHANNEL = "starcubes_control"

# The run tables that carry a `control` column, by the kind prefix the view
# `run` uses for them.
CONTROL_TABLES = {"etl_run": "starcubes_meta.etl_run",
                  "rollup_run": "starcubes_meta.rollup_run"}


def application_name(os_pid: Optional[int] = None,
                     role: Optional[str] = None) -> str:
    """`starcubes/<instance8>/<pid>[/<role>]` — what the lease is called.

    Run processes carry no role; that exact name is what `verify_local_pid`
    matches. A long-lived helper (the node_agent) names its role so it can be
    told apart in `pg_stat_activity`.
    """
    name = (f"{APPLICATION_NAME_PREFIX}{short_instance_id()}/"
            f"{os_pid if os_pid is not None else os.getpid()}")
    return f"{name}/{role}" if role else name


def alive_sql(alias: str) -> str:
    """SQL boolean: the lease on row `alias` is held right now."""
    return (f"EXISTS (SELECT 1 FROM pg_stat_activity sa "
            f" WHERE sa.pid = {alias}.lease_pid "
            f"   AND sa.backend_start = {alias}.lease_start)")


class LeaseLost(RuntimeError):
    """The lease connection is gone while the process still runs."""


class Lease:
    def __init__(self, dedicated, backend_pid: int, backend_start, os_pid: int):
        self.dedicated = dedicated
        self.backend_pid = backend_pid
        self.backend_start = backend_start
        self.os_pid = os_pid
        self.instance_id = instance_id()
        self.hostname = hostname()
        self.lost = False

    def columns(self) -> dict:
        """The owner columns every run row this process writes carries."""
        return {"hostname": self.hostname, "instance_id": self.instance_id,
                "lease_pid": self.backend_pid,
                "lease_start": self.backend_start, "pid": self.os_pid}

    def check(self) -> bool:
        """True while the lease connection still answers.

        A lost lease stays lost, so it is reported once: the ETL's progress
        thread asks every 5 s until the run reaches a chunk boundary, and a
        traceback per question buried the log in identical ERROR blocks.
        """
        if self.lost:
            return False
        try:
            self.dedicated.conn.execute(text("SELECT 1"))
            self.dedicated.conn.commit()
            return True
        except Exception:
            self.lost = True
            logger.error("lease connection lost (backend pid %s) — this process "
                         "is dead to every other container", self.backend_pid,
                         exc_info=True)
            return False

    def require(self) -> None:
        """Raise `LeaseLost` unless `check()` passes."""
        if not self.check():
            raise LeaseLost("the lease connection to the metadata database is "
                            "gone — another container may already consider "
                            "this process dead; stopping rather than risk "
                            "writing alongside a replacement")

    def release(self) -> None:
        try:
            self.dedicated.close()
        except Exception:
            logger.debug("lease close failed", exc_info=True)


_lease: Optional[Lease] = None


def acquire_process_lease(dsn: str, *, role: Optional[str] = None) -> Lease:
    """This process's lease, opened on first use.

    Keyed on `os.getpid()`: the ETL forks its transform and load workers, and a
    forked child must neither use nor close its parent's connection.
    """
    global _lease
    if _lease is not None and _lease.os_pid == os.getpid():
        return _lease
    os_pid = os.getpid()
    dedicated = open_dedicated(dsn,
                               application_name=application_name(os_pid, role))
    try:
        backend_pid, backend_start = dedicated.conn.execute(text(
            "SELECT pid, backend_start FROM pg_stat_activity "
            " WHERE pid = pg_backend_pid()")).one()
        dedicated.conn.commit()
    except Exception:
        dedicated.close()
        raise
    _lease = Lease(dedicated, backend_pid, backend_start, os_pid)
    atexit.register(_release_at_exit)
    return _lease


def current_lease() -> Optional[Lease]:
    """The lease this process holds, if it acquired one."""
    if _lease is not None and _lease.os_pid == os.getpid():
        return _lease
    return None


def _release_at_exit() -> None:
    lease = current_lease()
    if lease is not None:
        lease.release()


def lease_state(conn, lease_pid: Optional[int], lease_start) -> Optional[bool]:
    """True/False for a row with a lease, None for a legacy row without one."""
    if lease_pid is None:
        return None
    return bool(conn.execute(text(
        "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
        " WHERE pid = :p AND backend_start = :s)"),
        {"p": lease_pid, "s": lease_start}).scalar())


def is_local(row) -> bool:
    """The row's process runs in this container — only then may it be signalled."""
    return bool(row.get("instance_id")) and row["instance_id"] == instance_id()


def verify_local_pid(conn, row) -> bool:
    """The row's `pid` is the process holding its lease, in this container.

    The guard in front of every `os.kill`: a PID is a small number, and after
    a restart the same number can belong to any process. The lease connection's
    `application_name` names the instance and the OS pid that opened it, and
    only a live lease can be asked.
    """
    if not is_local(row) or not row.get("pid") or row.get("lease_pid") is None:
        return False
    return bool(conn.execute(text(
        "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
        " WHERE pid = :p AND backend_start = :s AND application_name = :a)"),
        {"p": row["lease_pid"], "s": row["lease_start"],
         "a": application_name(row["pid"])}).scalar())


def request_stop(conn, table: str, run_id: int, *, force: bool,
                 instance: Optional[str]) -> None:
    """Ask the process of a running row to stop, from any container.

    `table` is a key of `CONTROL_TABLES`. 'term' is honoured by the process
    itself at its next check; 'kill' additionally wakes the node_agent of the
    owning container (`instance`) to send SIGKILL. Never downgrades a pending
    'kill' to 'term'. The NOTIFY is delivered when the caller's transaction
    commits, together with the row it points at.
    """
    conn.execute(text(
        f"UPDATE {CONTROL_TABLES[table]} "
        "   SET control = CASE WHEN control = 'kill' THEN 'kill' ELSE :c END "
        " WHERE id = :id AND status = 'running'"),
        {"c": "kill" if force else "term", "id": run_id})
    conn.execute(text("SELECT pg_notify(:ch, :payload)"),
                 {"ch": CONTROL_CHANNEL, "payload": instance or ""})


def reap_dead_runs(conn, datasource_id: Optional[int] = None) -> int:
    """Close every run whose process is gone. Returns the number of rows.

    Runs inside the caller's transaction. Rows with a lease are judged by it;
    legacy rows (no lease) keep the rules they had before 0042 — the one-hour
    age for `etl_run`, nothing for a forever row with a pid (an older image
    judges its own) — so a rolling update reaps nothing it cannot see.
    """
    ds_clause = "" if datasource_id is None else " AND datasource_id = :ds"
    params = {"ds": datasource_id, "grace": STARTING_GRACE_S}
    n = 0
    n += conn.execute(text(
        "UPDATE starcubes_meta.etl_run t "
        "   SET status = 'failed', finished_at = now(), "
        "       runtime_ms = extract(epoch from now() - started_at) * 1000, "
        "       progress_rows = NULL, progress_at = NULL, "
        "       error = coalesce(error, "
        "               'process gone: its lease connection closed') "
        " WHERE status = 'running' AND lease_pid IS NOT NULL "
        f"  AND NOT {alive_sql('t')}" + ds_clause), params).rowcount
    n += conn.execute(text(
        "UPDATE starcubes_meta.etl_run "
        "   SET status = 'failed', "
        "       error = 'stale run: process did not update status' "
        " WHERE status = 'running' AND lease_pid IS NULL "
        "   AND started_at < now() - interval '1 hour'" + ds_clause),
        params).rowcount
    n += conn.execute(text(
        "UPDATE starcubes_meta.etl_forever t "
        "   SET status = 'failed', pid = NULL, stopped_at = now(), "
        "       error = 'process gone: its lease connection closed' "
        " WHERE status IN ('running', 'stopping') AND lease_pid IS NOT NULL "
        f"  AND NOT {alive_sql('t')}" + ds_clause), params).rowcount
    n += conn.execute(text(
        "UPDATE starcubes_meta.etl_forever "
        "   SET status = 'failed', stopped_at = now(), "
        "       error = 'subprocess never started' "
        " WHERE status IN ('running', 'stopping') "
        "   AND lease_pid IS NULL AND pid IS NULL "
        "   AND started_at < now() - make_interval(secs => :grace)"
        + ds_clause), params).rowcount
    n += conn.execute(text(
        "UPDATE starcubes_meta.rollup_run t "
        "   SET status = 'failed', finished_at = now(), "
        "       error = coalesce(error, "
        "               'process gone: its lease connection closed') "
        " WHERE status = 'running' AND lease_pid IS NOT NULL "
        f"  AND NOT {alive_sql('t')}" + ds_clause), params).rowcount
    return n
