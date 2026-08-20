"""Run a single incremental ETL pass for a datasource.

Equivalent to scripts/etl_run.py, but usable via ``manage.py etl_run``.

Also provides data maintenance operations: truncate partitions, delete
fact rows, clear ETL history, set the high watermark, and more.
"""
from __future__ import annotations

import os
import signal
import sys
import time
from datetime import datetime, timezone

from django.conf import settings
from django.core.management.base import BaseCommand

from sqlalchemy import text

from core.etl import import_fact, truncate_last_partition, delete_fact_from, \
                      clear_etl_history, set_highwatermark, EtlAborted
from core.schema import import_dimensions, refresh_dim_counts, \
                        create_future_partitions, vacuum_analyze_fact
from core.source import Source, _get_cached_engine
from core.utils import _parse_dt, _pid_alive
from sources.etl_logging import configure_etl_logging


_stopping = False


def _signal_handler(signum, frame):
    """Forever mode: extend the existing flag-based stop protocol."""
    global _stopping
    _stopping = True


def _abort_signal_handler(signum, frame):
    """Single-run mode: propagate SIGTERM/SIGINT as an EtlAborted so the
    finally block in import_fact can reap the workers and write the row.
    Without this, the default SIGTERM kills the parent immediately and
    the etl_run row is left on 'running' with no PID to clean up."""
    raise EtlAborted(f"killed by user (signal {signum})")


class Command(BaseCommand):
    help = "Run a single incremental ETL pass, or a data maintenance operation."

    def add_arguments(self, parser):
        parser.add_argument("--name", default=None,
                            help="datasource name (auto-detected if only one exists)")
        parser.add_argument("--id", type=int, default=None,
                            help="datasource id (alternative to --name)")
        parser.add_argument("--forever", type=int, default=None, metavar="SECONDS",
                            help="re-run ETL every SECONDS seconds (waits between runs)")
        parser.add_argument("--forever-id", type=int, default=None, metavar="ID",
                            help="existing etl_forever row ID (set by the GUI; "
                                 "skips duplicate-process check)")
        parser.add_argument("--postgres-dsn", default=None,
                            help="SQLAlchemy URL for Postgres "
                                 "(default: settings.STARCUBES_POSTGRES_DSN)")

        # ETL window overrides (combinable with each other and a normal ETL run)
        parser.add_argument("--start", default=None,
                            help="override HWM: start loading from this timestamp "
                                 "(YYYY-MM-DD HH:MM:SS or ISO 8601)")
        parser.add_argument("--end", default=None,
                            help="override end_time: last timestamp to import "
                                 "(YYYY-MM-DD HH:MM:SS or ISO 8601)")
        parser.add_argument("--limit", default=None, type=int,
                            help="maximum number of rows to import")

        # Standalone maintenance actions (one per invocation)
        parser.add_argument("--refresh-counts", action="store_true",
                            help="refresh dim count columns from fact table and exit")
        parser.add_argument("--import-dims", action="store_true",
                            help="import/update dimension tables and exit "
                                 "(no ETL, no source DB connection needed for inline dims)")
        parser.add_argument("--truncate-last", action="store_true",
                            help="truncate the oldest fact partition and exit")
        parser.add_argument("--delete-from", default=None,
                            help="delete all fact data at or after this timestamp "
                                 "and exit (YYYY-MM-DD HH:MM:SS or ISO 8601)")
        parser.add_argument("--clear-etl-history", action="store_true",
                            help="delete all etl_run rows for this datasource and exit")
        parser.add_argument("--set-highwatermark", action="store_true",
                            help="insert etl_run row with status=ok and "
                                 "last_end_time=max(partition_column) from fact "
                                 "and exit")
        parser.add_argument("--vacuum-analyze", action="store_true",
                            help="run VACUUM ANALYZE on the fact table and exit")
        parser.add_argument("--create-partitions", default=None, type=int,
                            metavar="N",
                            help="create N future range partitions (sized by datasource.partition_period) and exit")

    def handle(self, *args, **options):
        log = configure_etl_logging()
        log_path = os.environ.get("STARCUBES_ETL_LOG_PATH")
        log.info("etl_run start: argv=%s pid=%d log=%s",
                 " ".join(sys.argv[1:]), os.getpid(), log_path or "<stderr>")

        dsn = options["postgres_dsn"] or settings.STARCUBES_POSTGRES_DSN
        if not dsn:
            self.stderr.write("Error: --postgres-dsn or STARCUBES_POSTGRES_DSN is required")
            self.stderr.flush()
            raise SystemExit(1)

        meta = _get_cached_engine(dsn)

        # Resolve datasource: --id takes precedence, then --name, then auto-detect
        if options["id"]:
            name_or_id = options["id"]
        elif options["name"]:
            name_or_id = options["name"]
        else:
            with meta.begin() as conn:
                rows = conn.execute(text(
                    "SELECT id, name FROM starcubes_meta.datasource"
                )).mappings().all()
            if len(rows) == 0:
                self.stderr.write("Error: no datasources found in starcubes_meta.datasource")
                self.stderr.flush()
                raise SystemExit(1)
            elif len(rows) == 1:
                name_or_id = rows[0]["name"]
                self.stdout.write(f"Auto-detected datasource: {name_or_id}")
            else:
                self.stderr.write(
                    "Error: multiple datasources exist; specify --name or --id.\n"
                    "Available datasources:\n"
                )
                for r in rows:
                    self.stderr.write(f"  id={r['id']}  name={r['name']}\n")
                self.stderr.flush()
                raise SystemExit(1)

        source = Source.load(meta, dest_dsn=dsn, name_or_id=name_or_id)

        if options["refresh_counts"]:
            refresh_dim_counts(source, source.dest_engine())
            self.stdout.write(f"{source.name}: dim counts refreshed")
            return

        if options["import_dims"]:
            needs_source = any(d.is_primary and d.source_table
                               for d in source.dimensions)
            src_engine = source.source_engine() if needs_source else None
            import_dimensions(source, src_engine, source.dest_engine())
            self.stdout.write(f"{source.name}: dimensions imported/updated")
            return

        if options["truncate_last"]:
            part = truncate_last_partition(source, source.dest_engine())
            self.stdout.write(f"{source.name}: truncated partition {part}")
            return

        if options["delete_from"]:
            from_time = _parse_dt(options["delete_from"])
            n = delete_fact_from(source, source.dest_engine(), from_time)
            self.stdout.write(f"{source.name}: deleted {n} rows from {from_time}")
            return

        if options["clear_etl_history"]:
            n = clear_etl_history(source, meta)
            self.stdout.write(f"{source.name}: cleared {n} etl_run row(s)")
            return

        if options["set_highwatermark"]:
            hwm = set_highwatermark(source, source.dest_engine(), meta)
            self.stdout.write(f"{source.name}: high watermark set to {hwm}")
            return

        if options["vacuum_analyze"]:
            vacuum_analyze_fact(source, source.dest_engine())
            self.stdout.write(f"{source.name}: VACUUM ANALYZE completed")
            return

        if options["create_partitions"]:
            parts = create_future_partitions(source, source.dest_engine(),
                                             options["create_partitions"])
            self.stdout.write(f"{source.name}: created {len(parts)} partition(s): "
                              f"{', '.join(parts)}")
            return

        # Full ETL run (with optional window overrides)
        interval = options["forever"]
        if interval is not None and interval < 1:
            self.stderr.write("Error: --forever requires a positive number of seconds")
            self.stderr.flush()
            raise SystemExit(1)

        forever_id = None
        if interval is not None:
            signal.signal(signal.SIGTERM, _signal_handler)
            signal.signal(signal.SIGINT, _signal_handler)

            gui_forever_id = options["forever_id"]
            if gui_forever_id:
                # GUI path: the view already created the etl_forever row
                # (atomic check+insert).  Just stamp our PID.
                forever_id = gui_forever_id
                with meta.begin() as conn:
                    conn.execute(text(
                        "UPDATE starcubes_meta.etl_forever "
                        " SET pid = :pid WHERE id = :id"
                    ), {"pid": os.getpid(), "id": forever_id})
            else:
                # CLI path: check for a running forever session and create
                # the row ourselves.
                with meta.begin() as conn:
                    # Mark stale forever sessions as failed
                    conn.execute(text(
                        "UPDATE starcubes_meta.etl_forever "
                        " SET status = 'failed', pid = NULL, "
                        "     stopped_at = now(), "
                        "     error = 'stale: process did not update status' "
                        " WHERE datasource_id = :ds AND status IN ('running','stopping') "
                        "   AND started_at < now() - interval '1 hour'"
                    ), {"ds": source.id})

                    existing = conn.execute(text(
                        "SELECT id, pid FROM starcubes_meta.etl_forever "
                        " WHERE datasource_id = :ds AND status IN ('running','stopping')"
                    ), {"ds": source.id}).mappings().one_or_none()
                    if existing:
                        if existing["pid"] and _pid_alive(existing["pid"]):
                            self.stderr.write(
                                f"Error: forever ETL already running "
                                f"(pid={existing['pid']})")
                            self.stderr.flush()
                            raise SystemExit(1)
                        conn.execute(text(
                            "UPDATE starcubes_meta.etl_forever "
                            " SET status = 'failed', pid = NULL, "
                            "     stopped_at = now(), error = 'stale process' "
                            " WHERE id = :id"
                        ), {"id": existing["id"]})

                    forever_id = conn.execute(text(
                        "INSERT INTO starcubes_meta.etl_forever "
                        "  (datasource_id, pid, interval_s, status) "
                        " VALUES (:ds, :pid, :interval, 'running') "
                        " RETURNING id"
                    ), {"ds": source.id, "pid": os.getpid(),
                         "interval": interval}).scalar()
        else:
            # Single-run mode: SIGTERM/SIGINT abort the run instead of
            # killing the parent outright, so import_fact's finally block
            # can reap the workers and write the etl_run row.
            signal.signal(signal.SIGTERM, _abort_signal_handler)
            signal.signal(signal.SIGINT, _abort_signal_handler)

        start_time = _parse_dt(options["start"]) if options["start"] else None
        end_time = _parse_dt(options["end"]) if options["end"] else None
        iteration = 0

        try:
            while True:
                # Check stop conditions (forever mode only)
                if forever_id is not None:
                    if _stopping:
                        self.stdout.write("\nSignal received, stopping forever loop.\n")
                        break
                    with meta.begin() as conn:
                        stop = conn.execute(text(
                            "SELECT stop_requested FROM starcubes_meta.etl_forever "
                            " WHERE id = :id"
                        ), {"id": forever_id}).scalar()
                    if stop:
                        self.stdout.write("\nStop requested, exiting forever loop.\n")
                        break

                try:
                    rows = import_fact(source, meta,
                                       start_time=start_time,
                                       end_time_override=end_time,
                                       row_limit=options["limit"],
                                       log_path=log_path)
                except EtlAborted as exc:
                    # import_fact's finally already reaped the workers and
                    # wrote the etl_run row as 'failed'. Exit quietly —
                    # no traceback, this was a user-requested kill.
                    self.stderr.write(f"\n{exc}\n")
                    self.stderr.flush()
                    raise SystemExit(1)
                except Exception:
                    # Individual run failure — etl_run row already records it.
                    # Continue looping in forever mode; break in one-shot mode.
                    if forever_id is None:
                        raise
                    log.exception("%s: ETL run failed, continuing loop", source.name)
                    rows = 0

                iteration += 1

                # Window overrides only apply to the first iteration.
                if iteration == 1:
                    start_time = None
                    end_time = None

                with meta.begin() as conn:
                    last_runs = conn.execute(text(
                        "SELECT id, started_at, finished_at, "
                        "       first_end_time, last_end_time, "
                        "       rows, runtime_ms, status, error "
                        "  FROM starcubes_meta.etl_run "
                        " WHERE datasource_id = :ds "
                        " ORDER BY id DESC LIMIT 5"
                    ), {"ds": source.id}).mappings().all()

                self.stdout.write(f"\n{source.name}: loaded {rows} rows this run.\n")
                self.stdout.write("recent etl_run rows (newest first):")
                for r in last_runs:
                    self.stdout.write(
                        f"  id={r['id']} status={r['status']} "
                        f"rows={r['rows']} runtime={r['runtime_ms']}ms "
                        f"first={r['first_end_time']} last={r['last_end_time']}"
                        f"{(' err=' + r['error']) if r['error'] else ''}")

                if interval is None:
                    break

                self.stdout.write(f"\n--- next run in {interval}s (iteration {iteration}) ---\n")
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
                        "UPDATE starcubes_meta.etl_forever "
                        " SET status = 'stopped', pid = NULL, "
                        "     stopped_at = now(), stop_requested = false "
                        " WHERE id = :id"
                    ), {"id": forever_id})