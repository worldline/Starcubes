"""Resume forever ETL sessions orphaned by container shutdown.

Run at container start (from docker/entrypoint.sh, after `bootstrap`):

    manage.py etl_resume_forever

For each `starcubes_meta.etl_forever` row left in status `running` or
`stopping` whose PID is no longer alive, mark the orphan row `failed`
and spawn a fresh forever ETL subprocess with the same interval. The
unique partial index `ux_etl_forever_active` guarantees one active row
per datasource, so the old row is failed *before* the new row is
inserted.

When the host PID namespace is shared and the recorded PID happens to
belong to a different live process, the row is left alone — the GUI
stale-detection will catch it within an hour.
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand
from sqlalchemy import text

from core.source import _get_cached_engine
from core.utils import _pid_alive
from sources.etl_logging import configure_etl_logging
from sources.etl_spawn import PYTHON_EXE, spawn_etl_subprocess


class Command(BaseCommand):
    help = "Restart forever ETL sessions that were active before container shutdown."

    def add_arguments(self, parser):
        parser.add_argument("--postgres-dsn", default=None,
                            help="SQLAlchemy URL for Postgres "
                                 "(default: settings.STARCUBES_POSTGRES_DSN)")

    def handle(self, *args, **options):
        configure_etl_logging()
        dsn = options["postgres_dsn"] or settings.STARCUBES_POSTGRES_DSN
        if not dsn:
            self.stderr.write("Error: --postgres-dsn or STARCUBES_POSTGRES_DSN is required")
            raise SystemExit(1)

        meta = _get_cached_engine(dsn)

        with meta.begin() as conn:
            orphans = conn.execute(text(
                "SELECT f.id, f.datasource_id, f.pid, f.interval_s, d.name "
                "  FROM starcubes_meta.etl_forever f "
                "  JOIN starcubes_meta.datasource d ON d.id = f.datasource_id "
                " WHERE f.status IN ('running', 'stopping') "
                " ORDER BY f.id"
            )).mappings().all()

        if not orphans:
            self.stdout.write("No active forever sessions to resume.")
            return

        resumed = 0
        skipped = 0
        for row in orphans:
            if row["pid"] and _pid_alive(row["pid"]):
                self.stdout.write(
                    f"datasource {row['name']!r}: PID {row['pid']} still alive — "
                    f"leaving etl_forever id={row['id']} as is")
                skipped += 1
                continue

            with meta.begin() as conn:
                conn.execute(text(
                    "UPDATE starcubes_meta.etl_forever "
                    " SET status = 'failed', pid = NULL, "
                    "     stopped_at = now(), "
                    "     error = 'container restarted' "
                    " WHERE id = :id"
                ), {"id": row["id"]})

                new_id = conn.execute(text(
                    "INSERT INTO starcubes_meta.etl_forever "
                    "  (datasource_id, interval_s, status) "
                    " VALUES (:ds, :interval, 'running') "
                    " RETURNING id"
                ), {"ds": row["datasource_id"],
                    "interval": row["interval_s"]}).scalar()

            cmd = [PYTHON_EXE, "manage.py", "etl_run",
                   "--name", row["name"], "--forever", str(row["interval_s"]),
                   "--forever-id", str(new_id)]
            try:
                spawn_etl_subprocess(cmd, datasource_name=row["name"])
            except OSError as exc:
                with meta.begin() as conn:
                    conn.execute(text(
                        "UPDATE starcubes_meta.etl_forever "
                        " SET status = 'failed', pid = NULL, "
                        "     stopped_at = now(), "
                        "     error = 'subprocess failed to start on resume' "
                        " WHERE id = :id"
                    ), {"id": new_id})
                self.stderr.write(
                    f"datasource {row['name']!r}: failed to spawn resume "
                    f"subprocess: {exc}")
                continue

            self.stdout.write(
                f"datasource {row['name']!r}: resumed forever ETL "
                f"(interval={row['interval_s']}s, etl_forever id={new_id}, "
                f"replaced orphan id={row['id']})")
            resumed += 1

        self.stdout.write(f"Resumed {resumed}, skipped {skipped}.")
