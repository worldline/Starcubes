"""core/schema.py gegen echtes PostgreSQL.

Markiert ``db``: ohne ``STARCUBES_TEST_DSN`` übersprungen. Jeder Test bekommt
ein eigenes Schema, das garantiert wieder abgeräumt wird.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.db

from core.schema import (  # noqa: E402
    create_dim_tables,
    create_fact,
    create_future_partitions,
    drop_schema,
    ensure_partitions_for,
    ensure_starcubes_meta,
    import_dimensions,
    list_fact_partitions,
    read_dimensions,
)
from core.source import Dimension, FactColumn  # noqa: E402


def _src(make_source, scratch_schema, pg_dsn, **over):
    values = dict(
        schema_name=scratch_schema,
        dest_dsn=pg_dsn,
        source_dsn=pg_dsn,
        partition_period="month",
        partition_column="ts",
        fact_columns=[
            FactColumn("ts", "timestamptz", "time", 0, "ts"),
            FactColumn("host", "smallint", "dimension", 1, "host"),
            FactColumn("val", "integer", "measure", 2, "val"),
        ],
        indices=[],
    )
    values.update(over)
    return make_source(**values)


class TestMetaSchema:
    def test_idempotent(self, meta_engine):
        assert ensure_starcubes_meta(meta_engine) is False
        assert ensure_starcubes_meta(meta_engine) is False
        with meta_engine.connect() as conn:
            n = conn.execute(text(
                "SELECT count(*) FROM information_schema.tables "
                " WHERE table_schema = 'starcubes_meta'")).scalar()
        assert n and n > 20


class TestCreateFact:
    def test_legt_tabelle_und_partitionen_an(self, make_source, scratch_schema,
                                              pg_dsn):
        src = _src(make_source, scratch_schema, pg_dsn)
        eng = src.dest_engine()
        create_fact(src, eng)
        assert list_fact_partitions(src, eng)
        with eng.connect() as conn:
            exists = conn.execute(text(
                "SELECT 1 FROM information_schema.tables "
                " WHERE table_schema = :s AND table_name = 'fact'"),
                {"s": scratch_schema}).scalar()
        assert exists

    def test_idempotent(self, make_source, scratch_schema, pg_dsn):
        src = _src(make_source, scratch_schema, pg_dsn)
        eng = src.dest_engine()
        create_fact(src, eng)
        first = list_fact_partitions(src, eng)
        create_fact(src, eng)
        assert list_fact_partitions(src, eng) == first

    def test_future_partition(self, make_source, scratch_schema, pg_dsn):
        src = _src(make_source, scratch_schema, pg_dsn)
        eng = src.dest_engine()
        create_fact(src, eng)
        before = list_fact_partitions(src, eng)
        created = create_future_partitions(src, eng, 1, quiet=True)
        assert created
        assert len(list_fact_partitions(src, eng)) == len(before) + 1

    def test_ensure_partitions_for_fuellt_luecke(self, make_source,
                                                 scratch_schema, pg_dsn):
        src = _src(make_source, scratch_schema, pg_dsn)
        eng = src.dest_engine()
        create_fact(src, eng)
        before = set(list_fact_partitions(src, eng))
        start = datetime.now(timezone.utc) - timedelta(days=400)
        end = datetime.now(timezone.utc) + timedelta(days=30)
        ensure_partitions_for(src, eng, start, end, back=0, forward=0,
                              quiet=True)
        after = set(list_fact_partitions(src, eng))
        assert after >= before
        assert len(after) > len(before)


class TestDimensions:
    def _dim(self):
        return Dimension(
            id=1, fact_column="host", dim_table="dim_host",
            source_table=None, source_key=None, source_name=None,
            source_description=None, is_primary=True,
            inline_values="1,eins\n2,zwei\n")

    def test_create_dim_tables_inline(self, make_source, scratch_schema,
                                      pg_dsn):
        src = _src(make_source, scratch_schema, pg_dsn,
                   dimensions=[self._dim()])
        eng = src.dest_engine()
        create_fact(src, eng)
        created = create_dim_tables(src, eng, quiet=True)
        assert created == ["dim_host"]
        with eng.connect() as conn:
            ids = {r[0] for r in conn.execute(text(
                f"SELECT source_id FROM {scratch_schema}.dim_host")).all()}
        assert {"-1", "__unknown__", "1", "2"} <= ids

    def test_import_dimensions_inline_und_read_dimensions(
            self, make_source, scratch_schema, pg_dsn):
        src = _src(make_source, scratch_schema, pg_dsn,
                   dimensions=[self._dim()])
        eng = src.dest_engine()
        create_fact(src, eng)
        import_dimensions(src, None, eng, quiet=True)
        frames, unknowns = read_dimensions(src, eng)
        mapping = frames["dim_host"]
        assert mapping["1"] == mapping[1] == mapping[1.0]
        assert mapping["2"] > mapping["1"]
        assert "-1" in mapping
        assert unknowns["dim_host"] > 0

    def test_pandas_zugriff_nutzt_search_path(self, make_source,
                                              scratch_schema, pg_dsn):
        # create_dim_tables schreibt über pandas to_sql in das Schema; der
        # Test belegt, dass das nicht still in public landet.
        src = _src(make_source, scratch_schema, pg_dsn,
                   dimensions=[self._dim()])
        eng = src.dest_engine()
        create_fact(src, eng)
        create_dim_tables(src, eng, quiet=True)
        with eng.connect() as conn:
            in_public = conn.execute(text(
                "SELECT 1 FROM information_schema.tables "
                " WHERE table_schema = 'public' AND table_name = 'dim_host'"
            )).scalar()
        assert in_public is None


class TestDropSchema:
    def test_drop_raeumt_alles_ab(self, make_source, scratch_schema, pg_dsn):
        src = _src(make_source, scratch_schema, pg_dsn)
        eng = src.dest_engine()
        create_fact(src, eng)
        drop_schema(src, eng)
        with eng.connect() as conn:
            exists = conn.execute(text(
                "SELECT 1 FROM information_schema.schemata "
                " WHERE schema_name = :s"), {"s": scratch_schema}).scalar()
        assert exists is None
        # Der Fixture-Teardown darf ein bereits gelöschtes Schema löschen.
