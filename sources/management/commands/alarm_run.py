"""Evaluate threshold alarms — one cycle, or a global forever loop.

The `etl forever` pattern without the per-datasource scoping: alarms live
in ``starcubes_meta.alarm`` across all datasources, so one process
evaluates them all each cycle (``starcubes_meta.alarm_forever`` pins at
most one active session world-wide via ``ux_alarm_forever_active``). The
evaluation logic itself is Django-free in ``core/alarm.py``; this command
only owns the loop, the session row and the signal handling.

    manage.py alarm_run --once          # one cycle (also the default)
    manage.py alarm_run --forever 60    # loop, one cycle every 60 s

SIGTERM/SIGINT set a flag (they must stop the loop, not kill mid-cycle);
the loop also polls ``stop_requested`` each iteration so a future GUI can
stop it through the DB. Per-cycle exceptions are caught and logged — the
loop survives a broken alarm or a flaky source connection.
"""
from __future__ import annotations

import os
import signal
import sys
import time

from django.conf import settings
from django.core.management.base import BaseCommand

from sqlalchemy import text

from core.alarm import run_alarm_cycle
from core.source import _get_cached_engine
from core.utils import _pid_alive
from sources.etl_logging import configure_etl_logging


_stopping = False


def _signal_handler(signum, frame):
    """Forever mode: flag-based stop, same protocol as etl_run."""
    global _stopping
    _stopping = True


class Command(BaseCommand):
    help = ("Evaluate all enabled threshold alarms once, "
            "or in a global forever loop.")

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true",
                            help="run exactly one evaluation cycle and exit "
                                 "(the default when no flags are given)")
        parser.add_argument("--forever", type=int, default=None, metavar="SECONDS",
                            help="re-evaluate every SECONDS seconds (waits between cycles)")
        parser.add_argument("--forever-id", type=int, default=None, metavar="ID",
                            help="existing alarm_forever row ID (set by "
                                 "alarm_resume_forever; skips the "
                                 "duplicate-process check)")
        parser.add_argument("--postgres-dsn", default=None,
                            help="SQLAlchemy URL for Postgres "
                                 "(default: settings.STARCUBES_POSTGRES_DSN)")

    def handle(self, *args, **options):
        log = configure_etl_logging()
        log_path = os.environ.get("STARCUBES_ETL_LOG_PATH")
        log.info("alarm_run start: argv=%s pid=%d log=%s",
                 " ".join(sys.argv[1:]), os.getpid(), log_path or "<stderr>")

        dsn = options["postgres_dsn"] or settings.STARCUBES_POSTGRES_DSN
        if not dsn:
            self.stderr.write("Error: --postgres-dsn or STARCUBES_POSTGRES_DSN is required")
            self.stderr.flush()
            raise SystemExit(1)

        meta = _get_cached_engine(dsn)

        interval = options["forever"]
        if options["once"] and interval is not None:
            self.stderr.write("Error: --once cannot be combined with --forever\n")
            self.stderr.flush()
            raise SystemExit(1)
        if interval is not None and interval < 1:
            self.stderr.write("Error: --forever requires a positive number of seconds")
            self.stderr.flush()
            raise SystemExit(1)

        forever_id = None
        if interval is not None:
            signal.signal(signal.SIGTERM, _signal_handler)
            signal.signal(signal.SIGINT, _signal_handler)

            resume_forever_id = options["forever_id"]
            if resume_forever_id:
                # Resume path: alarm_resume_forever already inserted the row.
                # Just stamp our PID.
                forever_id = resume_forever_id
                with meta.begin() as conn:
                    conn.execute(text(
                        "UPDATE starcubes_meta.alarm_forever "
                        " SET pid = :pid WHERE id = :id"
                    ), {"pid": os.getpid(), "id": forever_id})
            else:
                # CLI path: fail stale actives, refuse when a live evaluator
                # holds the session, then insert our own row.
                with meta.begin() as conn:
                    conn.execute(text(
                        "UPDATE starcubes_meta.alarm_forever "
                        " SET status = 'failed', pid = NULL, "
                        "     stopped_at = now(), "
                        "     error = 'stale: process did not update status' "
                        " WHERE singleton AND status IN ('running','stopping') "
                        "   AND started_at < now() - interval '1 hour'"
                    ))

                    existing = conn.execute(text(
                        "SELECT id, pid FROM starcubes_meta.alarm_forever "
                        " WHERE singleton AND status IN ('running','stopping')"
                    )).mappings().one_or_none()
                    if existing:
                        if existing["pid"] and _pid_alive(existing["pid"]):
                            self.stderr.write(
                                f"Error: alarm evaluation loop already running "
                                f"(pid={existing['pid']})")
                            self.stderr.flush()
                            raise SystemExit(1)
                        conn.execute(text(
                            "UPDATE starcubes_meta.alarm_forever "
                            " SET status = 'failed', pid = NULL, "
                            "     stopped_at = now(), error = 'stale process' "
                            " WHERE id = :id"
                        ), {"id": existing["id"]})

                    forever_id = conn.execute(text(
                        "INSERT INTO starcubes_meta.alarm_forever "
                        "  (pid, interval_s, status) "
                        " VALUES (:pid, :interval, 'running') "
                        " RETURNING id"
                    ), {"pid": os.getpid(),
                        "interval": interval}).scalar()

        iteration = 0
        try:
            while True:
                if forever_id is not None:
                    if _stopping:
                        self.stdout.write("\nSignal received, stopping alarm loop.\n")
                        break
                    with meta.begin() as conn:
                        stop = conn.execute(text(
                            "SELECT stop_requested "
                            "  FROM starcubes_meta.alarm_forever WHERE id = :id"
                        ), {"id": forever_id}).scalar()
                    if stop:
                        self.stdout.write("\nStop requested, exiting alarm loop.\n")
                        break

                try:
                    counts = run_alarm_cycle(meta, dsn)
                except Exception:
                    # Cycle-level failure (meta DB unreachable, …) — logged,
                    # then the loop continues; per-alarm failures are already
                    # isolated inside run_alarm_cycle.
                    if forever_id is None:
                        raise
                    log.exception("alarm cycle failed, continuing loop")
                    counts = None

                iteration += 1
                if counts is not None:
                    self.stdout.write(
                        f"cycle {iteration}: alarms={counts['alarms']} "
                        f"windows={counts['windows']} breaches={counts['breaches']} "
                        f"episodes +{counts['episodes_opened']}"
                        f"/~{counts['episodes_extended']}"
                        f"/closed {counts['episodes_closed']} "
                        f"errors={counts['errors']}")
                else:
                    self.stdout.write(f"cycle {iteration}: failed")

                if interval is None:
                    break

                self.stdout.write(f"--- next cycle in {interval}s ---")
                self.stdout.flush()

                # Interruptible sleep: check _stopping every second
                for _ in range(interval):
                    if _stopping:
                        break
                    time.sleep(1)
                if _stopping:
                    self.stdout.write("\nSignal during sleep, stopping.\n")
                    break

        finally:
            if forever_id is not None:
                with meta.begin() as conn:
                    conn.execute(text(
                        "UPDATE starcubes_meta.alarm_forever "
                        " SET status = 'stopped', pid = NULL, "
                        "     stopped_at = now(), stop_requested = false "
                        " WHERE id = :id"
                    ), {"id": forever_id})