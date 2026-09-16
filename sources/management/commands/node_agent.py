"""The per-container agent: carries out what only this container can do.

Several containers may share one database. Most coordination needs nothing
running — the process lease (core/lease.py) makes liveness visible to all of
them, and a stop request is a column the process reads itself. One thing
cannot be done from the database: sending a signal. A SIGKILL for a process
too stuck to read its `control` column has to come from inside the container
that runs it, and that is this command.

    manage.py node_agent [--once]

Started by docker/entrypoint.sh next to pipeline_watch, one per container.
Each cycle:

- **Kill requests** (migration 0043): rows with `control = 'kill'` whose
  `instance_id` is this container's. The pid is verified against the row's
  lease first (`core.lease.verify_local_pid` — a PID alone is a small number
  any process may hold after a restart), then SIGKILL goes to the process
  group if the process leads one (every spawned run does, via
  `start_new_session`), else to the process alone, so a run started from a
  shell cannot take the shell down with it. The row is closed here, since a
  killed process writes nothing.
- **Forever adoption** (sources/forever_adopt.py): a session whose process is
  gone, or which its draining container released, is taken over and run here.
  On every notification and at least once a minute; off with
  `STARCUBES_FOREVER_ADOPT=0`, for a container that must never run ETL.

Woken by `NOTIFY starcubes_control` and polling every few seconds as a
fallback, so a lost notification costs seconds, not a stuck run.

Like pipeline_watch it is unsupervised: if it dies, nothing restarts it. It
holds a lease of its own (role `node_agent` in its application_name), which is
how the rest of the system can see whether a container's agent is alive.
Touches `starcubes_meta` only. Never raises out of a cycle.
"""
from __future__ import annotations

import logging
import os
import select
import signal
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from sqlalchemy import text

from core.lease import (CONTROL_CHANNEL, acquire_process_lease,
                        reap_dead_runs, verify_local_pid)
from core.node import instance_id
from core.pglock import open_dedicated
from core.source import _get_cached_engine
from sources.etl_logging import configure_etl_logging
from sources.forever_adopt import adopt_orphans

log = logging.getLogger("sources.node_agent")

POLL_S = 5
ADOPT_EVERY_S = 60

_stopping = False


def _signal_handler(_signum, _frame):
    global _stopping
    _stopping = True


class Command(BaseCommand):
    help = ("Per-container agent: carries out kill requests for this "
            "container's processes. Started by docker/entrypoint.sh.")

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true",
                            help="run a single cycle and exit")
        parser.add_argument("--postgres-dsn", default=None,
                            help="SQLAlchemy URL for Postgres "
                                 "(default: settings.STARCUBES_POSTGRES_DSN)")

    def handle(self, *args, **options):
        configure_etl_logging()
        dsn = options["postgres_dsn"] or settings.STARCUBES_POSTGRES_DSN
        if not dsn:
            self.stderr.write(
                "Error: --postgres-dsn or STARCUBES_POSTGRES_DSN is required")
            self.stderr.flush()
            raise SystemExit(1)

        signal.signal(signal.SIGTERM, _signal_handler)
        signal.signal(signal.SIGINT, _signal_handler)

        meta = _get_cached_engine(dsn)
        acquire_process_lease(dsn, role="node_agent")
        self._me = instance_id()
        self._unverified: set = set()
        self._adopt = os.environ.get("STARCUBES_FOREVER_ADOPT", "1") != "0"
        self._adopt_due = 0.0
        self._notified = False

        if options["once"]:
            self._cycle(meta)
            return

        log.info("node_agent: instance %s, listening on %s", self._me,
                 CONTROL_CHANNEL)
        listener = None
        while not _stopping:
            self._cycle(meta)
            if listener is None:
                listener = self._listen(dsn)
            if not self._wait(listener):
                # The listening connection broke. Polling carries on; a new
                # listener is opened on the next pass.
                try:
                    listener.close()
                except Exception:
                    pass
                listener = None
        if listener is not None:
            listener.close()
        log.info("node_agent: stopped")

    # ── waiting ────────────────────────────────────────────────────────────

    def _listen(self, dsn: str):
        try:
            listener = open_dedicated(dsn)
            raw = listener.conn.connection.dbapi_connection
            raw.set_session(autocommit=True)
            with raw.cursor() as cur:
                cur.execute(f"LISTEN {CONTROL_CHANNEL}")
            return listener
        except Exception:
            log.warning("node_agent: could not LISTEN, polling only",
                        exc_info=True)
            return None

    def _wait(self, listener) -> bool:
        """Sleep until a notification or POLL_S. False if the listener broke."""
        if listener is None:
            for _ in range(POLL_S):
                if _stopping:
                    break
                time.sleep(1)
            return True
        raw = listener.conn.connection.dbapi_connection
        deadline = time.monotonic() + POLL_S
        try:
            while not _stopping:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return True
                # Wake at least once a second so a SIGTERM is not held up.
                ready, _, _ = select.select([raw], [], [], min(1.0, remaining))
                if ready:
                    raw.poll()
                    if raw.notifies:
                        raw.notifies.clear()
                        self._notified = True
                        return True
            return True
        except Exception:
            log.warning("node_agent: listener connection lost", exc_info=True)
            return False

    # ── the work ───────────────────────────────────────────────────────────

    def _cycle(self, meta) -> None:
        """One pass. Never raises — a helper that dies on a transient database
        error is worse than none, because it looks like one."""
        try:
            self._kill_pass(meta)
        except Exception:
            log.exception("node_agent: kill pass failed, continuing")
        # Adoption on a notification (a session was released, or something
        # changed) and otherwise once a minute: it scans every forever row,
        # which the 5 s kill poll has no reason to do.
        now = time.monotonic()
        if self._adopt and (self._notified or now >= self._adopt_due):
            self._notified = False
            self._adopt_due = now + ADOPT_EVERY_S
            try:
                adopted, _ = adopt_orphans(meta, include_legacy=False)
                if adopted:
                    log.info("node_agent: adopted %d forever session(s)", adopted)
            except Exception:
                log.exception("node_agent: adoption failed, continuing")

    def _kill_pass(self, meta) -> None:
        with meta.begin() as conn:
            rows = conn.execute(text(
                "SELECT 'etl_run' AS tbl, id, datasource_id, pid, hostname, "
                "       instance_id, lease_pid, lease_start "
                "  FROM starcubes_meta.etl_run "
                " WHERE status = 'running' AND control = 'kill' "
                "   AND instance_id = :me AND lease_pid IS NOT NULL "
                "UNION ALL "
                "SELECT 'rollup_run', id, datasource_id, pid, hostname, "
                "       instance_id, lease_pid, lease_start "
                "  FROM starcubes_meta.rollup_run "
                " WHERE status = 'running' AND control = 'kill' "
                "   AND instance_id = :me AND lease_pid IS NOT NULL"
            ), {"me": self._me}).mappings().all()
        for row in rows:
            self._kill(meta, dict(row))

    def _kill(self, meta, row: dict) -> None:
        key = (row["tbl"], row["id"])
        with meta.begin() as conn:
            if not verify_local_pid(conn, row):
                # Either the process is already gone (the reap closes the
                # row) or the pid does not hold the lease — never signal that.
                if reap_dead_runs(conn, row["datasource_id"]) == 0 \
                        and key not in self._unverified:
                    self._unverified.add(key)
                    log.warning("node_agent: %s %s: pid %s does not hold the "
                                "run's lease — not signalling it",
                                row["tbl"], row["id"], row["pid"])
                return
        pid = int(row["pid"])
        try:
            if os.getpgid(pid) == pid:
                os.killpg(pid, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        log.info("node_agent: SIGKILL %s %s (pid %s) on request",
                 row["tbl"], row["id"], pid)
        with meta.begin() as conn:
            if row["tbl"] == "etl_run":
                conn.execute(text(
                    "UPDATE starcubes_meta.etl_run "
                    "   SET status = 'failed', error = 'killed by user (force)', "
                    "       finished_at = now(), "
                    "       runtime_ms = extract(epoch from now() - started_at) * 1000, "
                    "       progress_rows = NULL, progress_at = NULL "
                    " WHERE id = :id AND status = 'running'"), {"id": row["id"]})
            else:
                conn.execute(text(
                    "UPDATE starcubes_meta.rollup_run "
                    "   SET status = 'failed', finished_at = now(), "
                    "       error = 'killed by user (force)' "
                    " WHERE id = :id AND status = 'running'"), {"id": row["id"]})
