"""Periodic maintenance for a datasource: ANALYZE, BRIN, fill levels, sizes.

Everything too expensive to pay for on every load and worth doing every few
hours. Normally triggered by the forever loop once
``datasource.housekeeping_interval_s`` has elapsed (``etl_run.py``); this is
the same code path, run by hand.

Nothing here takes the datasource advisory lock — ANALYZE and
``brin_summarize_new_values`` are safe alongside a running load, and refusing
to run during forever mode would defeat the point. See
``core/housekeeping.py`` and ``docs/etl-automation.md``.
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand

from core.housekeeping import housekeeping_due, run_housekeeping
from core.source import Source, _get_cached_engine
from sources.etl_logging import configure_etl_logging


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"


class Command(BaseCommand):
    help = ("Run periodic maintenance for a datasource: targeted ANALYZE, "
            "BRIN summarisation, dimension fill levels, partition sizes.")

    def add_arguments(self, parser):
        parser.add_argument("--name", default=None, help="datasource name")
        parser.add_argument("--id", type=int, default=None,
                            help="datasource id (alternative to --name)")
        parser.add_argument("--postgres-dsn", default=None,
                            help="SQLAlchemy URL for Postgres "
                                 "(default: settings.STARCUBES_POSTGRES_DSN)")
        parser.add_argument("--no-analyze", action="store_true",
                            help="skip the targeted ANALYZE (it costs I/O on "
                                 "a box already streaming rows); still "
                                 "summarises BRIN ranges and measures")
        parser.add_argument("--if-due", action="store_true",
                            help="do nothing unless housekeeping_interval_s "
                                 "has elapsed since the last run")

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

        if options["if_due"] and not housekeeping_due(meta, source.id):
            self.stdout.write(f"{source.name}: housekeeping not due yet")
            return

        report = run_housekeeping(source, meta, source.dest_engine(),
                                  analyze=not options["no_analyze"])

        self.stdout.write(f"\n{source.name}: housekeeping "
                          f"({report['runtime_ms']} ms)")
        for a in report["analyzed"]:
            self.stdout.write(f"  ANALYZE {a['partition']}  {a['ms']} ms")
        ranges = sum(b["ranges"] for b in report["brin"])
        self.stdout.write(
            f"  BRIN: {ranges} new range(s) summarised across "
            f"{len(report['brin'])} of "
            f"{report.get('brin_indices_checked', 0)} index(es)")
        self.stdout.write(
            f"  storage: {len(report['partitions'])} partition(s), "
            f"{_fmt_bytes(report['total_bytes'])}")
        for d in sorted(report["dimensions"],
                        key=lambda r: r["pct_full"], reverse=True)[:5]:
            self.stdout.write(
                f"  dim {d['table']}: {d['rows']} row(s), "
                f"max index {d['max_index']} ({d['pct_full']}% of ceiling)")
        for w in report["warnings"]:
            self.stderr.write(f"  ! {w['code']}: {w['message']}")
