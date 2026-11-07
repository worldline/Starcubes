"""core/rollup.py gegen PostgreSQL — bauen, Zustand, Auswahl."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.db

from core.rollup import (  # noqa: E402
    drop_rollup_tables,
    ensure_rollup_tables,
    pick_rollup,
    read_rollup_states,
    refresh_rollups,
    spec_hash,
    rollup_specs,
)
from core.schema import create_fact  # noqa: E402
from core.source import Dimension, FactColumn  # noqa: E402


def _bucket() -> datetime:
    now = int(datetime.now(timezone.utc).timestamp())
    return datetime.fromtimestamp((now // 3600 - 1) * 3600, timezone.utc)


@pytest.fixture
def env(make_source, make_datasource, scratch_schema, pg_dsn, meta_engine):
    ds_id = make_datasource(scratch_schema)
    src = make_source(
        id=ds_id, name="rollup_test", schema_name=scratch_schema,
        source_dsn=pg_dsn, dest_dsn=pg_dsn,
        partition_column="ts", partition_period="month",
        fact_columns=[
            FactColumn("ts", "timestamptz", "time", 0, "ts"),
            FactColumn("host", "smallint", "dimension", 1, "host"),
            FactColumn("val", "integer", "measure", 2, "val"),
        ],
        dimensions=[Dimension(
            id=1, fact_column="host", dim_table="dim_host",
            source_table=None, source_key=None, source_name=None,
            source_description=None, is_primary=True,
            inline_values="1,eins\n2,zwei\n")],
        indices=[],
        rollup_interval_s=3600,
        rollup_columns=["host"],
        rollup_marginals=[],
        rollup_measures=["sum(val)"],
    )
    eng = src.dest_engine()
    create_fact(src, eng)
    bucket = _bucket()
    with eng.begin() as conn:
        conn.execute(text(
            f"INSERT INTO {scratch_schema}.fact (ts, host, val) VALUES "
            f"(:a, 1, 10), (:b, 2, 20), (:c, 1, 5)"),
            {"a": bucket + timedelta(minutes=10),
             "b": bucket + timedelta(minutes=20),
             "c": bucket + timedelta(minutes=30)})
    try:
        yield src, eng, meta_engine, bucket, ds_id
    finally:
        drop_rollup_tables(src, eng, meta_engine)


class TestRollupBuild:
    def test_baut_und_waehlt(self, env):
        src, eng, meta, bucket, ds_id = env
        ensure_rollup_tables(src, eng, quiet=True)
        through = bucket + timedelta(seconds=3600)
        report = refresh_rollups(src, eng, meta, hwm=through, quiet=True)
        assert report["rows"] == 2       # host 1 und host 2
        assert report["built"][0]["rollup"] == "rollup_1h"

        states = read_rollup_states(meta, ds_id)
        state = states["rollup_1h"]
        assert state.spec_hash == spec_hash(rollup_specs(src)[0])
        assert state.built_from == bucket
        assert state.built_through == through

        spec = pick_rollup(
            src, states, group_by="host", filter_tree=None,
            measure="count(*)", graph_style="multibar", interval=3600,
            t_from=bucket, t_to=through)
        assert spec is not None
        assert spec.name == "rollup_1h"

    def test_rollup_zeilen(self, env):
        src, eng, meta, bucket, ds_id = env
        ensure_rollup_tables(src, eng, quiet=True)
        through = bucket + timedelta(seconds=3600)
        refresh_rollups(src, eng, meta, hwm=through, quiet=True)
        with eng.connect() as conn:
            rows = conn.execute(text(
                f"SELECT host, n, sum_val FROM {src.schema_name}.rollup_1h "
                f"ORDER BY host")).all()
        assert rows == [(1, 2, 15), (2, 1, 20)]

    def test_zweiter_lauf_baut_nichts_neu(self, env):
        src, eng, meta, bucket, ds_id = env
        ensure_rollup_tables(src, eng, quiet=True)
        through = bucket + timedelta(seconds=3600)
        refresh_rollups(src, eng, meta, hwm=through, quiet=True)
        report = refresh_rollups(src, eng, meta, hwm=through, quiet=True)
        assert report["built"] == []
        assert report["rows"] == 0

    def test_sum_measure_wird_serviert(self, env):
        src, eng, meta, bucket, ds_id = env
        ensure_rollup_tables(src, eng, quiet=True)
        through = bucket + timedelta(seconds=3600)
        refresh_rollups(src, eng, meta, hwm=through, quiet=True)
        states = read_rollup_states(meta, ds_id)
        spec = pick_rollup(
            src, states, group_by="host", filter_tree=None,
            measure="sum(val)", graph_style="multibar", interval=3600,
            t_from=bucket, t_to=through)
        assert spec is not None

    def test_hash_mismatch_degradiert_zu_fact(self, env):
        src, eng, meta, bucket, ds_id = env
        ensure_rollup_tables(src, eng, quiet=True)
        through = bucket + timedelta(seconds=3600)
        refresh_rollups(src, eng, meta, hwm=through, quiet=True)
        states = read_rollup_states(meta, ds_id)

        # Andere Konfiguration: die gespeicherte Form passt nicht mehr.
        from dataclasses import replace
        changed = replace(src, rollup_columns=["host", "val"])
        assert pick_rollup(
            changed, states, group_by="host", filter_tree=None,
            measure="count(*)", graph_style="multibar", interval=3600,
            t_from=bucket, t_to=through) is None

    def test_fenster_ausserhalb_des_gebauten_bereichs(self, env):
        src, eng, meta, bucket, ds_id = env
        ensure_rollup_tables(src, eng, quiet=True)
        through = bucket + timedelta(seconds=3600)
        refresh_rollups(src, eng, meta, hwm=through, quiet=True)
        states = read_rollup_states(meta, ds_id)
        assert pick_rollup(
            src, states, group_by="host", filter_tree=None,
            measure="count(*)", graph_style="multibar", interval=3600,
            t_from=bucket - timedelta(hours=1), t_to=through) is None
