"""Manually flush the server-side plot_json cache.

Usage:
    manage.py query_cache_clear                          # all datasources
    manage.py query_cache_clear --name foo               # one datasource by name
    manage.py query_cache_clear --id 7                   # one datasource by id
    manage.py query_cache_clear --name foo --group-by peer
    manage.py query_cache_clear --name foo --measure 'count(*)'

`--group-by` and `--measure` filter the delete to rows whose stored
breakdown columns match. Both require `--name` or `--id` — cross-source
selective deletes are not allowed.
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand
from sqlalchemy import text

from core.cache import clear_cache
from core.source import _get_cached_engine


class Command(BaseCommand):
    help = "Delete server-side plot_json cache rows."

    def add_arguments(self, parser):
        parser.add_argument("--name", default=None,
                            help="datasource name (omit to flush all)")
        parser.add_argument("--id", type=int, default=None,
                            help="datasource id (alternative to --name)")
        parser.add_argument("--group-by", dest="group_by", default=None,
                            help="filter to rows with this group_by "
                                 "(requires --name/--id)")
        parser.add_argument("--measure", default=None,
                            help="filter to rows with this measure "
                                 "(requires --name/--id)")
        parser.add_argument("--postgres-dsn", default=None,
                            help="SQLAlchemy URL for Postgres "
                                 "(default: settings.STARCUBES_POSTGRES_DSN)")

    def handle(self, *args, **options):
        dsn = options["postgres_dsn"] or settings.STARCUBES_POSTGRES_DSN
        if not dsn:
            self.stderr.write("Error: --postgres-dsn or STARCUBES_POSTGRES_DSN is required")
            raise SystemExit(1)

        meta = _get_cached_engine(dsn)

        ds_id = options["id"]
        if ds_id is None and options["name"]:
            with meta.begin() as conn:
                ds_id = conn.execute(text(
                    "SELECT id FROM starcubes_meta.datasource WHERE name = :n"
                ), {"n": options["name"]}).scalar()
            if ds_id is None:
                self.stderr.write(f"Error: no datasource named {options['name']!r}")
                raise SystemExit(1)

        group_by = options["group_by"]
        measure = options["measure"]
        if (group_by is not None or measure is not None) and ds_id is None:
            self.stderr.write(
                "Error: --group-by/--measure require --name or --id")
            raise SystemExit(1)

        n = clear_cache(meta, datasource_id=ds_id,
                        group_by=group_by, measure=measure)

        scope_parts = [
            f"datasource_id={ds_id}" if ds_id is not None else "all datasources"]
        if group_by is not None:
            scope_parts.append(f"group_by={group_by!r}")
        if measure is not None:
            scope_parts.append(f"measure={measure!r}")
        scope = ", ".join(scope_parts)
        self.stdout.write(f"deleted {n} query_cache rows ({scope})")
