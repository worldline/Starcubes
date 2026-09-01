"""Start the global alarm evaluation loop at container boot.

Run from docker/entrypoint.sh (after ``bootstrap`` and
``etl_resume_forever``):

    manage.py alarm_resume_forever

The alarm loop is deliberately always-on — a cycle with zero enabled
alarms costs one meta SELECT, so there is no GUI toggle to forget. This
command cleans up first, then starts:

  * an ``alarm_forever`` row left in ``running``/``stopping`` by a dead
    PID (the container restarted) is marked ``failed``, mirroring
    ``etl_resume_forever``;
  * a row whose PID is still alive (shared host PID namespace) is left
    alone — its own loop keeps running;
  * then a fresh ``running`` row is inserted (the ``singleton`` partial
    unique index forces fail-before-insert) and the loop is spawned
    detached via ``spawn_etl_subprocess``. The ``datasource_name``
    argument only routes the log file — the slug ``alarm-loop`` matches
    no datasource, so the loop's log never shows up in a per-source ETL
    Log tab, and it rides the same ``prune_etl_logs`` retention.
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand
from sqlalchemy import text

from core.source import _get_cached_engine
from core.utils import _pid_alive
from sources.etl_logging import configure_etl_logging
from sources.etl_spawn import PYTHON_EXE, spawn_etl_subprocess

# Detached loop cadence. Evaluated windows advance with each datasource's
# own data (the fact clock), so this only bounds how soon a breach is
# noticed — 60 s is prompt without hammering the fact tables.
DEFAULT_INTERVAL_S = 60


class Command(BaseCommand):
    help = "Start the global alarm evaluation loop (idempotent across container restarts)."

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_S,
                            metavar="SECONDS",
                            help=f"loop interval (default: {DEFAULT_INTERVAL_S}s)")
        parser.add_argument("--postgres-dsn", default=None,
                            help="SQLAlchemy URL for Postgres "
                                 "(default: settings.STARCUBES_POSTGRES_DSN)")

    def handle(self, *args, **options):
        configure_etl_logging()
        dsn = options["postgres_dsn"] or settings.STARCUBES_POSTGRES_DSN
        if not dsn:
            self.stderr.write("Error: --postgres-dsn or STARCUBES_POSTGRES_DSN is required")
            raise SystemExit(1)

        interval = options["interval"]
        if interval < 1:
            self.stderr.write("Error: --interval must be positive")
            raise SystemExit(1)

        meta = _get_cached_engine(dsn)

        with meta.begin() as conn:
            orphans = conn.execute(text(
                "SELECT id, pid, interval_s "
                "  FROM starcubes_meta.alarm_forever "
                " WHERE singleton AND status IN ('running', 'stopping') "
                " ORDER BY id"
            )).mappings().all()

        for row in orphans:
            if row["pid"] and _pid_alive(row["pid"]):
                self.stdout.write(
                    f"alarm loop PID {row['pid']} still alive — leaving "
                    f"alarm_forever id={row['id']} as is")
                # A live evaluator owns the singleton slot; nothing to start.
                return
            with meta.begin() as conn:
                conn.execute(text(
                    "UPDATE starcubes_meta.alarm_forever "
                    " SET status = 'failed', pid = NULL, "
                    "     stopped_at = now(), "
                    "     error = 'container restarted' "
                    " WHERE id = :id"
                ), {"id": row["id"]})
            # The orphan's interval wins — it was configured by whoever
            # started the loop before the restart.
            interval = row["interval_s"]
            self.stdout.write(
                f"alarm_forever id={row['id']} was orphaned by the restart — "
                f"marked failed, restarting with interval={interval}s")

        with meta.begin() as conn:
            new_id = conn.execute(text(
                "INSERT INTO starcubes_meta.alarm_forever "
                "  (interval_s, status) "
                " VALUES (:interval, 'running') "
                " RETURNING id"
            ), {"interval": interval}).scalar()

        cmd = [PYTHON_EXE, "manage.py", "alarm_run",
               "--forever", str(interval), "--forever-id", str(new_id)]
        try:
            spawn_etl_subprocess(cmd, datasource_name="alarm-loop")
        except OSError as exc:
            with meta.begin() as conn:
                conn.execute(text(
                    "UPDATE starcubes_meta.alarm_forever "
                    " SET status = 'failed', pid = NULL, "
                    "     stopped_at = now(), "
                    "     error = 'subprocess failed to start on resume' "
                    " WHERE id = :id"
                ), {"id": new_id})
            self.stderr.write(f"failed to spawn alarm loop subprocess: {exc}")
            raise SystemExit(1)

        self.stdout.write(
            f"alarm evaluation loop started (interval={interval}s, "
            f"alarm_forever id={new_id}, pid pending)")