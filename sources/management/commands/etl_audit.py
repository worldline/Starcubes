"""Audit one datasource's fact table against its source over a window.

Finds what the late-arrival overlap missed: rows the source has and `fact`
does not. Normally triggered by the forever loop once
``datasource.audit_interval_s`` has elapsed (``etl_run.py``); this is the
same code path, run by hand.

Two stages, and the second one usually does not run. Stage one counts both
sides per time bucket, which is cheap. Only when a bucket disagrees does
stage two pull both sides' primary keys and name the individual rows — and
that list is what makes the run worth storing: ``etl_run --backfill
--audit-run <id>`` re-fetches exactly those rows instead of re-scanning the
window.

Nothing here takes the datasource advisory lock: an audit only reads, and
refusing to run during forever mode would defeat the point. See
``core/audit.py`` and ``docs/etl-audit.md``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from django.conf import settings
from django.core.management.base import BaseCommand

from core.audit import (BACKFILL_IDS_MAX, audit_due, audit_window_seconds,
                        run_audit)
from core.source import Source, _get_cached_engine
from core.utils import _parse_dt
from sources.etl_logging import configure_etl_logging


class Command(BaseCommand):
    help = ("Audit a datasource's fact table against its source over a "
            "window; records the result in starcubes_meta.audit_run.")

    def add_arguments(self, parser):
        parser.add_argument("--name", default=None, help="datasource name")
        parser.add_argument("--id", type=int, default=None,
                            help="datasource id (alternative to --name)")
        parser.add_argument("--postgres-dsn", default=None,
                            help="SQLAlchemy URL for Postgres "
                                 "(default: settings.STARCUBES_POSTGRES_DSN)")
        parser.add_argument("--start", default=None,
                            help="window start (ISO). Default: audit_window_s "
                                 "before --end.")
        parser.add_argument("--end", default=None,
                            help="window end (ISO). Default: now.")
        parser.add_argument("--hours", type=float, default=None,
                            help="trailing window width in hours; a shorthand "
                                 "for --start, overridden by it")
        parser.add_argument("--buckets", type=int, default=24,
                            help="stage-one buckets (default 24, max 200)")
        parser.add_argument("--max-ids", type=int, default=BACKFILL_IDS_MAX,
                            help=f"cap on stored missing ids "
                                 f"(default {BACKFILL_IDS_MAX})")
        parser.add_argument("--if-due", action="store_true",
                            help="do nothing unless audit_interval_s has "
                                 "elapsed since the last run")

    def handle(self, *args, **options):
        configure_etl_logging()
        dsn = options["postgres_dsn"] or settings.STARCUBES_POSTGRES_DSN
        if not dsn:
            self.stderr.write(
                "Error: --postgres-dsn or STARCUBES_POSTGRES_DSN is required")
            self.stderr.flush()
            raise SystemExit(1)

        name_or_id = options["id"] or options["name"]
        if not name_or_id:
            self.stderr.write("Error: --name or --id is required")
            self.stderr.flush()
            raise SystemExit(1)

        meta = _get_cached_engine(dsn)
        source = Source.load(meta, dest_dsn=dsn, name_or_id=name_or_id)

        if options["if_due"] and not audit_due(meta, source.id):
            self.stdout.write(f"{source.name}: audit not due yet")
            return

        end = (_parse_dt(options["end"]) if options["end"]
               else datetime.now(timezone.utc))
        if options["start"]:
            start = _parse_dt(options["start"])
        elif options["hours"]:
            start = end - timedelta(hours=float(options["hours"]))
        else:
            width = audit_window_seconds(meta, source.id)
            if width <= 0:
                self.stderr.write(
                    "Error: no window given and audit_window_s / "
                    "audit_interval_s are both 0 — pass --start, --hours, or "
                    "configure the interval")
                self.stderr.flush()
                raise SystemExit(1)
            start = end - timedelta(seconds=width)

        result = run_audit(source, source.dest_engine(), meta,
                           start=start, end=end,
                           buckets=options["buckets"],
                           trigger="manual",
                           full_ids=options["max_ids"])

        self.stdout.write(
            f"\n{source.name}: audit run {result['id']} "
            f"[{result['window_start']}, {result['window_end']}) "
            f"— {result['status']}")
        if result["status"] == "failed":
            self.stderr.write(f"  ! {result['error']}")
            raise SystemExit(1)
        self.stdout.write(
            f"  source {result.get('source_rows')} / fact "
            f"{result.get('fact_rows')} row(s)")
        self.stdout.write(
            f"  missing {result['missing_count']}, surplus "
            f"{result['surplus_count']}, duplicate "
            f"{result['duplicate_rows']}")
        if result["ids_truncated"]:
            self.stderr.write(
                "  ! more missing rows than the id cap — a targeted backfill "
                "is refused for this run; use a window backfill")
        elif result["missing_ids"]:
            self.stdout.write(
                f"  repair with: manage.py etl_run --name {source.name} "
                f"--backfill --audit-run {result['id']}")
