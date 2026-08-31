"""Seed a datasource into starcubes_meta from a profile.

The profile is loaded via ``core.profile.load_profile``, which looks in
``profiles/user/`` first and then ``profiles/examples/``. Default profile is
``oracle_payments``; override with ``--profile <name>``. To add one, drop a
YAML file in either directory — no Python changes required.

The INSERTs live in ``sources/profile_store.py``, shared with the GUI, so
the CLI and the browser cannot write a profile differently.

See docs/source-profiles.md.
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from sqlalchemy import text

from core.profile import ProfileError, iter_profiles, load_profile
from core.schema import create_fact, import_dimensions
from core.source import Source, _get_cached_engine
from sources.profile_store import insert_datasource, insert_profile_children


def _available_profiles() -> str:
    """Comma-separated profile names for the --profile help text."""
    return ", ".join(p["name"] for p in iter_profiles()) or "<none>"


def _upsert_datasource(meta_engine, name, schema, source_dsn, force, profile):
    """Insert a datasource plus every child row from a loaded profile.

    One transaction, so a failure part-way leaves no half-seeded datasource
    behind. The statements themselves live in ``sources/profile_store.py``,
    shared with the GUI.
    """
    with meta_engine.begin() as conn:
        existing = conn.execute(text(
            "SELECT id FROM starcubes_meta.datasource WHERE name = :n"
        ), {"n": name}).scalar()
        if existing and not force:
            raise CommandError(
                f"datasource {name!r} already exists (id={existing}). "
                f"Re-run with --force to replace.")
        if existing and force:
            conn.execute(text(
                "DELETE FROM starcubes_meta.datasource WHERE id = :id"
            ), {"id": existing})

        ds_id = insert_datasource(conn, profile, name, schema, source_dsn)
        insert_profile_children(conn, ds_id, profile)
    return ds_id


class Command(BaseCommand):
    help = "Seed an example datasource into starcubes_meta."

    def add_arguments(self, parser):
        parser.add_argument("--postgres-dsn", default=None,
                            help="SQLAlchemy URL for the Postgres DB "
                                 "(default: settings.STARCUBES_POSTGRES_DSN)")
        parser.add_argument("--source-dsn", default=None,
                            help="SQLAlchemy URL for the source DB "
                                 "(env: STARCUBES_SOURCE_DSN)")
        parser.add_argument("--name", default="source",
                            help="datasource name (default: source)")
        parser.add_argument("--schema", default="starcubes_source",
                            help="Postgres schema for this datasource's warehouse "
                                 "tables (default: starcubes_source)")
        parser.add_argument("--profile", default="oracle_payments",
                            help="name of the profile under profiles/ "
                                 "(default: oracle_payments). Available: "
                                 f"{_available_profiles()}")
        parser.add_argument("--force", action="store_true",
                            help="delete an existing datasource with the same name "
                                 "before inserting")
        parser.add_argument("--skip-initial-ddl", action="store_true",
                            help="register config rows only; skip create_fact() "
                                 "and import_dimensions()")

    def handle(self, *args, **options):
        import os
        dsn = options["postgres_dsn"] or settings.STARCUBES_POSTGRES_DSN
        source_dsn = options["source_dsn"] or os.environ.get("STARCUBES_SOURCE_DSN", "")

        if not dsn:
            self.stderr.write("Error: --postgres-dsn or STARCUBES_POSTGRES_DSN is required")
            self.stderr.flush()
            raise SystemExit(1)
        if not source_dsn and not options["skip_initial_ddl"]:
            self.stderr.write("Error: --source-dsn or STARCUBES_SOURCE_DSN is required "
                               "unless --skip-initial-ddl is set")
            self.stderr.flush()
            raise SystemExit(1)

        try:
            profile = load_profile(options["profile"])
        except ProfileError as exc:
            raise CommandError(str(exc))

        self.stdout.write(f"loaded profile {profile.name!r}: "
                          f"{len(profile.fact_columns)} fact columns, "
                          f"{len(profile.dimensions)} dimensions, "
                          f"{len(profile.measures)} measures")

        meta_engine = _get_cached_engine(dsn)
        ds_id = _upsert_datasource(meta_engine, options["name"],
                                    options["schema"], source_dsn,
                                    options["force"], profile)
        self.stdout.write(f"registered datasource {options['name']!r} "
                          f"(id={ds_id}) → schema {options['schema']}")

        if options["skip_initial_ddl"]:
            self.stdout.write("skipping initial DDL (--skip-initial-ddl). "
                              "Run core.schema.create_fact + core.schema.import_dimensions "
                              "manually when the source DB is reachable.")
            return

        source = Source.load(meta_engine, dest_dsn=dsn, name_or_id=ds_id)
        source_engine = source.source_engine()
        dest_engine = source.dest_engine()

        self.stdout.write(f"creating {options['schema']}.fact + {source.partition_period} partitions …")
        create_fact(source, dest_engine)

        self.stdout.write("loading dimensions from source …")
        import_dimensions(source, source_engine, dest_engine)

        self.stdout.write("\nseed complete.")