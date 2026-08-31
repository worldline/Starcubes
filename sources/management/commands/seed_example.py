"""Seed an example datasource into starcubes_meta.

The example is loaded from ``examples/<profile>.yaml`` via
``core.profile.load_profile``. Default profile is ``oracle_payments``;
override with ``--profile <name>``. To add a new example, drop a YAML file
in ``examples/`` — no Python changes required.
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from sqlalchemy import text

from core.profile import ProfileError, iter_profiles, load_profile
from core.schema import create_fact, import_dimensions
from core.source import Source, _get_cached_engine


def _upsert_datasource(meta_engine, name, schema, source_dsn, force, profile):
    """Insert a datasource plus its child rows from a loaded profile.

    Mirror of the historical in-line loop: writes one row into
    ``starcubes_meta.datasource`` and N rows each into ``fact_column``,
    ``dimension`` and ``fact_index``; ``measure`` is populated by the
    caller so the function stays symmetric with the legacy version.
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

        # Become the default source only when no other source claims it
        # (the partial unique index ux_datasource_default allows just one).
        has_default = conn.execute(text(
            "SELECT 1 FROM starcubes_meta.datasource WHERE is_default LIMIT 1"
        )).scalar()

        src = profile.source
        partition = profile.partition
        runtime = profile.runtime_defaults
        ladder = runtime.get("interval_ladder") or [1, 60, 3600, 86400]
        ds_id = conn.execute(text(
            "INSERT INTO starcubes_meta.datasource ("
            "  name, schema_name, source_dialect, source_dsn, "
            "  base_table, watermark_column, watermark_max_query, "
            "  partition_column, partition_period, "
            "  chunksize, transform_procs, small_window_hint, "
            "  interval_ladder, bar_pixel_target, is_default, "
            "  watermark_fact_expr"
            ") VALUES ("
            "  :name, :schema, :dialect, :dsn, "
            "  :base, :watermark_column, NULLIF(:wmq, ''), "
            "  :partition_column, :partition_period, "
            "  :chunksize, :transform_procs, NULLIF(:hint, ''), "
            "  CAST(:ladder AS int[]), :bar_pixel_target, :is_def, "
            "  NULLIF(:wm_fact_expr, '')"
            ") RETURNING id"
        ), {"name": name, "schema": schema,
            "dialect": src.get("dialect", "oracle"),
            "dsn": source_dsn or "",
            "base": src["base_table"],
            "watermark_column": src["watermark_column"],
            "wmq": src.get("watermark_max_query") or "",
            "partition_column": partition["column"],
            "partition_period": partition.get("period", "month"),
            "chunksize": runtime.get("chunksize", 10000),
            "transform_procs": runtime.get("transform_procs", 1),
            "hint": src.get("small_window_hint") or "",
            "ladder": "{" + ",".join(str(x) for x in ladder) + "}",
            "bar_pixel_target": runtime.get("bar_pixel_target", 5),
            "is_def": not has_default,
            "wm_fact_expr": src.get("watermark_fact_expr") or ""}).scalar()

        conn.execute(text(
            "INSERT INTO starcubes_meta.fact_column "
            "  (datasource_id, name, pg_type, role, ordinal, source_expr) "
            "VALUES (:ds, :n, :t, :r, :o, :e)"
        ), [{"ds": ds_id, "n": n, "t": t, "r": r, "o": o, "e": e}
            for (n, t, r, o, e) in profile.fact_columns_as_tuples()])

        conn.execute(text(
            "INSERT INTO starcubes_meta.dimension "
            "  (datasource_id, fact_column, dim_table, source_table, "
            "   source_key, source_name, source_description, is_primary, inline_values) "
            "VALUES (:ds, :fc, :dt, :st, :sk, :sn, :sd, :p, :iv)"
        ), [{"ds": ds_id, "fc": fc, "dt": dt, "st": st,
             "sk": sk, "sn": sn, "sd": sd, "p": p, "iv": iv}
            for (fc, dt, st, sk, sn, sd, p, iv) in profile.dimensions_as_tuples()])

        conn.execute(text(
            "INSERT INTO starcubes_meta.fact_index "
            "  (datasource_id, column_name, method) "
            "VALUES (:ds, :c, :m)"
        ), [{"ds": ds_id, "c": c, "m": m}
            for (c, m) in profile.fact_indices_as_tuples()])

        conn.execute(text(
            "INSERT INTO starcubes_meta.measure "
            "  (datasource_id, name, agg, graph_style, is_default, ordinal, percentiles) "
            "VALUES (:ds, :n, :a, :gs, :d, :o, :p)"
        ), [{"ds": ds_id, "n": n, "a": a, "gs": gs, "d": d, "o": o, "p": p}
            for (n, a, gs, d, o, p) in profile.measures_as_tuples()])

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
                            help="name of the profile under examples/ "
                                 "(default: oracle_payments). Available: "
                                 f"{', '.join(iter_profiles()) or '<none>'}")
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