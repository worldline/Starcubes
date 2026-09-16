"""Resume forever ETL sessions whose process is gone, at container start.

Run at container start (from docker/entrypoint.sh, after `bootstrap`):

    manage.py etl_resume_forever

The rules live in sources/forever_adopt.py, shared with `manage.py node_agent`,
which applies them continuously: a session whose lease is gone, or which its
draining container released, is taken over and spawned in this container; a
session that was asked to stop is closed instead; a live one is left alone,
wherever it runs.

The one difference from the node_agent: this command also judges rows written
by an image older than migration 0042, which carry no lease, by their PID —
the rule it has always applied, and one that only means something in the
container that wrote them.
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand

from core.source import _get_cached_engine
from sources.etl_logging import configure_etl_logging
from sources.forever_adopt import adopt_orphans


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

        adopted, skipped = adopt_orphans(_get_cached_engine(dsn),
                                         include_legacy=True,
                                         say=self.stdout.write)
        self.stdout.write(f"Resumed {adopted}, skipped {skipped}.")
