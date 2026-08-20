"""Seed an example Oracle payment-transaction datasource into starcubes_meta.

Equivalent to scripts/seed_example.py, but usable via ``manage.py seed_example``.
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from sqlalchemy import text

from core.schema import create_fact, import_dimensions
from core.source import Source, _get_cached_engine

BASE_TABLE          = "txn"
SMALL_WINDOW_HINT   = "/*+ ORDERED INDEX(txn, TXN3) */"
WATERMARK_MAX_QUERY = "SELECT max(end_time) FROM txn"

TOTAL_EXPR = (
    "extract( minute from "
    "TO_TIMESTAMP(substr(to_char(end_time),1,19)||'.'||"
    "lpad(end_time_ms,3,'0'),'YYYY-MM-DD HH24:MI:SS.FF3') - "
    "TO_TIMESTAMP(substr(to_char(create_time),1,19)||'.'||"
    "lpad(create_time_ms,3,'0'),'YYYY-MM-DD HH24:MI:SS.FF3')"
    ")*60000 + "
    "extract( second from "
    "TO_TIMESTAMP(substr(to_char(end_time),1,19)||'.'||"
    "lpad(end_time_ms,3,'0'),'YYYY-MM-DD HH24:MI:SS.FF3') - "
    "TO_TIMESTAMP(substr(to_char(create_time),1,19)||'.'||"
    "lpad(create_time_ms,3,'0'),'YYYY-MM-DD HH24:MI:SS.FF3')"
    ")*1000"
)

# (name, pg_type, role, ordinal, source_expr)
FACT_COLUMNS = [
    ("create_time", "timestamptz", "time",       1,
     "substr(to_char(create_time),1,19)||'.'||lpad(create_time_ms,3,'0')"),
    ("txn_id",      "bigint",      "pk",         2,
     "to_number(txn_id)"),
    ("stay",        "smallint",    "measure",    3,
     "stay"),
    ("total",       "smallint",    "measure",    4,
     TOTAL_EXPR),
    ("amount",      "bigint",      "measure",    5,
     "to_number(nvl(amount,0))"),
    ("cncy_code",   "smallint",    "dimension",  6,
     "to_number(nvl(cncy_code,-1))"),
    ("app_loc",     "smallint",    "dimension",  7,
     "to_char(create_app_loc_id)"),
    ("db_loc",      "smallint",    "dimension",  8,
     "SUBSTR(txn_id,2,1)"),
    ("response",    "smallint",    "dimension",  9,
     "res_code"),
    ("txn_type",    "smallint",    "dimension", 10,
     "to_char(txn_type_id)"),
    ("sec_type",    "smallint",    "dimension", 11,
     "to_char(sec_type_id)"),
    ("txn_origin",  "smallint",    "dimension", 12,
     "to_char(txn_origin_id)"),
    ("so_peer_1",   "smallint",    "dimension", 13,
     "to_char(nvl(so_peer_id, -1))"),
    ("so_peer_2",   "smallint",    "dimension", 14,
     "to_char(nvl(so_peer_id2, -1))"),
    ("so_peer_3",   "smallint",    "dimension", 15,
     "to_char(nvl(so_peer_id3, -1))"),
    ("src_peer",    "smallint",    "dimension", 16,
     "to_char(nvl(src_peer_id, -1))"),
    ("merch_cat",   "smallint",    "dimension", 17,
     "to_char(nvl(merch_cat_code, -1))"),
    ("card_type",   "smallint",    "dimension", 18,
     "to_char(nvl(card_type_id, -1))"),
    ("issuer",      "smallint",    "dimension", 19,
     "to_char(issuer_id)"),
]

# ISO 4217 currency codes — read from project-root iso4217.csv at import time
# so the single file is the source of truth for both seeding and manual edits.
from pathlib import Path as _Path
_ISO4217_CSV = (_Path(__file__).resolve().parents[3] / "iso4217.csv").read_text()

# (fact_col, dim_table, src_table, src_key, src_name, src_desc, is_primary, inline_values)
DIMENSIONS = [
    ("app_loc",    "app_loc",    "app_loc",    "app_loc_id",     "app_loc_name",     "remark",          True,  None),
    ("db_loc",     "db_loc",     "db_loc",     "db_loc_id",      "db_loc_name",      "remark",          True,  None),
    ("response",   "response",   "result",     "res_code",       "res_code",         "description",     True,  None),
    ("txn_type",   "txn_type",   "txn_type",   "txn_type_id",    "txn_type_name",    "description",     True,  None),
    ("sec_type",   "sec_type",   "sec_type",   "sec_type_id",    "sec_type_name",    "description",     True,  None),
    ("txn_origin", "txn_origin", "txn_origin", "txn_origin_id",  "txn_origin_name",  "txn_origin_name", True,  None),
    ("so_peer_1",  "peer",       "peer",       "peer_id",        "srv_name",         "remark",          True,  None),
    ("so_peer_2",  "peer",       "peer",       "peer_id",        "srv_name",         "remark",          False, None),
    ("so_peer_3",  "peer",       "peer",       "peer_id",        "srv_name",         "remark",          False, None),
    ("src_peer",   "peer",       "peer",       "peer_id",        "srv_name",         "remark",          False, None),
    ("merch_cat",  "merch_cat",  "merch_cat",  "merch_cat_code", "merch_cat_name",   "remark",          True,  None),
    ("card_type",  "card_type",  "card_type",  "card_type_id",   "card_type_name",   "remark",          True,  None),
    ("issuer",     "issuer",     "issuer",     "issuer_id",      "remark",           "remark",          True,  None),
    ("cncy_code",  "cncy_code",  None,         None,             None,               None,              True,  _ISO4217_CSV),
]

# (column_name, method)
FACT_INDICES = [
    ("create_time", "brin"),
]

# (name, agg, graph_style, is_default, ordinal)
MEASURES = [
    ("Transaction count", "count(*)",    "multibar", True,  1),
    ("Total amount",      "sum(amount)", "multibar", False, 2),
    ("Avg stay",          "avg(stay)",   "multibar", False, 3),
    ("Avg duration (ms)", "avg(total)",  "multibar", False, 4),
]


def _upsert_datasource(meta_engine, name, schema, source_dsn, force):
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

        ds_id = conn.execute(text(
            "INSERT INTO starcubes_meta.datasource ("
            "  name, schema_name, source_dialect, source_dsn, "
            "  base_table, watermark_column, watermark_max_query, "
            "  partition_column, partition_period, "
            "  chunksize, transform_procs, small_window_hint, "
            "  interval_ladder, bar_pixel_target, is_default"
            ") VALUES ("
            "  :name, :schema, 'oracle', :dsn, "
            "  :base, 'end_time', :wmq, "
            "  'create_time', 'month', "
            "  10000, 1, :hint, "
            "  '{1,60,3600,86400}', 5, :is_def"
            ") RETURNING id"
        ), {"name": name, "schema": schema,
            "dsn": source_dsn or "",
            "base": BASE_TABLE, "wmq": WATERMARK_MAX_QUERY,
            "hint": SMALL_WINDOW_HINT,
            "is_def": not has_default}).scalar()

        conn.execute(text(
            "INSERT INTO starcubes_meta.fact_column "
            "  (datasource_id, name, pg_type, role, ordinal, source_expr) "
            "VALUES (:ds, :n, :t, :r, :o, :e)"
        ), [{"ds": ds_id, "n": n, "t": t, "r": r, "o": o, "e": e}
            for (n, t, r, o, e) in FACT_COLUMNS])

        conn.execute(text(
            "INSERT INTO starcubes_meta.dimension "
            "  (datasource_id, fact_column, dim_table, source_table, "
            "   source_key, source_name, source_description, is_primary, inline_values) "
            "VALUES (:ds, :fc, :dt, :st, :sk, :sn, :sd, :p, :iv)"
        ), [{"ds": ds_id, "fc": fc, "dt": dt, "st": st,
             "sk": sk, "sn": sn, "sd": sd, "p": p, "iv": iv}
            for (fc, dt, st, sk, sn, sd, p, iv) in DIMENSIONS])

        conn.execute(text(
            "INSERT INTO starcubes_meta.fact_index "
            "  (datasource_id, column_name, method) "
            "VALUES (:ds, :c, :m)"
        ), [{"ds": ds_id, "c": c, "m": m} for (c, m) in FACT_INDICES])

        conn.execute(text(
            "INSERT INTO starcubes_meta.measure "
            "  (datasource_id, name, agg, graph_style, is_default, ordinal) "
            "VALUES (:ds, :n, :a, :gs, :d, :o)"
        ), [{"ds": ds_id, "n": n, "a": a, "gs": gs, "d": d, "o": o}
            for (n, a, gs, d, o) in MEASURES])

    return ds_id


class Command(BaseCommand):
    help = "Seed an example Oracle payment-transaction datasource into starcubes_meta."

    def add_arguments(self, parser):
        parser.add_argument("--postgres-dsn", default=None,
                            help="SQLAlchemy URL for the Postgres DB "
                                 "(default: settings.STARCUBES_POSTGRES_DSN)")
        parser.add_argument("--source-dsn", default=None,
                            help="SQLAlchemy URL for the Oracle source DB "
                                 "(env: STARCUBES_SOURCE_DSN)")
        parser.add_argument("--name", default="source",
                            help="datasource name (default: source)")
        parser.add_argument("--schema", default="starcubes_source",
                            help="Postgres schema for this datasource's warehouse "
                                 "tables (default: starcubes_source)")
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

        meta_engine = _get_cached_engine(dsn)
        ds_id = _upsert_datasource(meta_engine, options["name"],
                                    options["schema"], source_dsn,
                                    options["force"])
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