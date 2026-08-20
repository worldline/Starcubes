"""Create indices and foreign keys for a datasource.

Calls ``core.schema.create_indices`` — builds FK constraints from primary
dimensions plus BRIN/btree indices defined in ``starcubes_meta.fact_index``.
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand

from core.schema import create_indices
from core.source import Source, _get_cached_engine


class Command(BaseCommand):
    help = "Create indices and foreign keys for a datasource."

    def add_arguments(self, parser):
        parser.add_argument("--name", default=None,
                            help="datasource name")
        parser.add_argument("--id", type=int, default=None,
                            help="datasource id (alternative to --name)")
        parser.add_argument("--postgres-dsn", default=None,
                            help="SQLAlchemy URL for Postgres "
                                 "(default: settings.STARCUBES_POSTGRES_DSN)")

    def handle(self, *args, **options):
        dsn = options["postgres_dsn"] or settings.STARCUBES_POSTGRES_DSN
        if not dsn:
            self.stderr.write("Error: --postgres-dsn or STARCUBES_POSTGRES_DSN is required")
            self.stderr.flush()
            raise SystemExit(1)

        name_or_id = options["id"] or options["name"]
        if not name_or_id:
            self.stderr.write("Error: --name or --id is required")
            self.stderr.flush()
            raise SystemExit(1)

        meta = _get_cached_engine(dsn)
        source = Source.load(meta, dest_dsn=dsn, name_or_id=name_or_id)
        create_indices(source, source.dest_engine())
        self.stdout.write(f"{source.name}: indices created")