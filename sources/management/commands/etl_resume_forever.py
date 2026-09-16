"""Resume forever ETL sessions whose process is gone.

Run at container start (from docker/entrypoint.sh, after `bootstrap`):

    manage.py etl_resume_forever

For each `starcubes_meta.etl_forever` row left in status `running` or
`stopping` whose process is gone, mark the orphan row `failed` and spawn a
fresh forever ETL subprocess with the same interval, in this container.

"Gone" is decided by the process lease (core/lease.py), which every container
sees the same way — so a container starting next to a running one leaves that
one's sessions alone instead of starting a second loop for them. A row without
a lease was written by an older image and can only be judged by its PID, i.e.
only meaningfully in the container that wrote it; that is the transitional
rule for exactly those rows.

Several containers may run this at the same moment. The orphan is closed with
a conditional UPDATE (only while it is still active) and the new row is
inserted in the same transaction, under `ux_etl_forever_active`: whoever loses
either race skips the session instead of failing the container start.
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from core.lease import STARTING_GRACE_S, alive_sql
from core.source import _get_cached_engine
from core.utils import _pid_alive
from sources.etl_logging import configure_etl_logging
from sources.etl_spawn import PYTHON_EXE, spawn_etl_subprocess


class Command(BaseCommand):
    help = "Restart forever ETL sessions whose process is gone."

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
            candidates = conn.execute(text(
                "SELECT f.id, f.datasource_id, f.pid, f.interval_s, d.name, "
                "       f.hostname, f.lease_pid, "
                "       CASE WHEN f.lease_pid IS NULL THEN NULL "
                f"            ELSE {alive_sql('f')} END AS alive, "
                "       f.started_at > now() - make_interval(secs => :grace) "
                "           AS young "
                "  FROM starcubes_meta.etl_forever f "
                "  JOIN starcubes_meta.datasource d ON d.id = f.datasource_id "
                " WHERE f.status IN ('running', 'stopping') "
                " ORDER BY f.id"
            ), {"grace": STARTING_GRACE_S}).mappings().all()

        if not candidates:
            self.stdout.write("No active forever sessions to resume.")
            return

        resumed = 0
        skipped = 0
        for row in candidates:
            if row["alive"]:
                self.stdout.write(
                    f"datasource {row['name']!r}: running on "
                    f"{row['hostname'] or 'another container'} — "
                    f"leaving etl_forever id={row['id']} as is")
                skipped += 1
                continue
            if row["alive"] is None:
                if row["pid"] and _pid_alive(row["pid"]):
                    self.stdout.write(
                        f"datasource {row['name']!r}: PID {row['pid']} still "
                        f"alive — leaving etl_forever id={row['id']} as is")
                    skipped += 1
                    continue
                if not row["pid"] and row["young"]:
                    self.stdout.write(
                        f"datasource {row['name']!r}: etl_forever id={row['id']} "
                        f"is still starting — leaving it as is")
                    skipped += 1
                    continue

            try:
                with meta.begin() as conn:
                    closed = conn.execute(text(
                        "UPDATE starcubes_meta.etl_forever "
                        " SET status = 'failed', pid = NULL, "
                        "     stopped_at = now(), "
                        "     error = 'process gone; resumed at container start' "
                        " WHERE id = :id AND status IN ('running', 'stopping') "
                        " RETURNING id"
                    ), {"id": row["id"]}).scalar()
                    if closed is None:
                        # Another container closed it first.
                        skipped += 1
                        continue
                    new_id = conn.execute(text(
                        "INSERT INTO starcubes_meta.etl_forever "
                        "  (datasource_id, interval_s, status) "
                        " VALUES (:ds, :interval, 'running') "
                        " RETURNING id"
                    ), {"ds": row["datasource_id"],
                        "interval": row["interval_s"]}).scalar()
            except IntegrityError:
                self.stdout.write(
                    f"datasource {row['name']!r}: another container resumed "
                    f"it first — skipping")
                skipped += 1
                continue

            cmd = [PYTHON_EXE, "manage.py", "etl_run",
                   "--name", row["name"], "--forever", str(row["interval_s"]),
                   "--forever-id", str(new_id)]
            try:
                spawn_etl_subprocess(cmd, datasource_name=row["name"])
            except OSError as exc:
                with meta.begin() as conn:
                    conn.execute(text(
                        "UPDATE starcubes_meta.etl_forever "
                        "   SET status = 'failed', pid = NULL, "
                        "       stopped_at = now(), "
                        "       error = 'subprocess failed to start on resume' "
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
