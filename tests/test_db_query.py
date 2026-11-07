"""core/query.py gegen PostgreSQL — die ausführenden Query-Funktionen.

Die reinen SQL-Bausteine sind in ``test_query_pure.py`` abgedeckt; hier
zählt, dass die erzeugten Statements gegen echte Tabellen das tun, was der
Chart erwartet.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.db

from core.query import (  # noqa: E402
    detect_gaps,
    drill_down,
    list_transactions,
    timeseries_ladder,
    timeseries_multibar,
)
from core.schema import create_fact  # noqa: E402
from core.source import Dimension, FactColumn  # noqa: E402


def _minute() -> datetime:
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    return now - timedelta(hours=1)


@pytest.fixture
def env(make_source, make_datasource, scratch_schema, pg_dsn):
    ds_id = make_datasource(scratch_schema)
    src = make_source(
        id=ds_id, name="query_test", schema_name=scratch_schema,
        source_dsn=pg_dsn, dest_dsn=pg_dsn,
        partition_column="ts", partition_period="month",
        fact_columns=[
            FactColumn("ts", "timestamptz", "time", 0, "ts"),
            FactColumn("host", "smallint", "dimension", 1, "host"),
            FactColumn("path", "smallint", "auto-dimension", 2, "path"),
            FactColumn("val", "integer", "measure", 3, "val"),
            FactColumn("id", "bigint", "pk", 4, "id"),
        ],
        dimensions=[
            Dimension(1, "host", "dim_host", None, None, None, None, True),
            Dimension(2, "path", "dim_path", None, None, None, None, True),
        ],
        indices=[])
    eng = src.dest_engine()
    create_fact(src, eng)
    with eng.begin() as conn:
        conn.execute(text(
            f"CREATE TABLE {scratch_schema}.dim_host ("
            f" index smallint PRIMARY KEY, source_id text, name text, "
            f" description text, count bigint NOT NULL DEFAULT 0)"))
        conn.execute(text(
            f"CREATE TABLE {scratch_schema}.dim_path ("
            f" index smallint PRIMARY KEY, source_id text, name text, "
            f" description text, count bigint NOT NULL DEFAULT 0)"))
        conn.execute(text(
            f"INSERT INTO {scratch_schema}.dim_host VALUES "
            f"(1, '1', 'eins', '', 0), (2, '2', 'zwei', '', 0)"))
        conn.execute(text(
            f"INSERT INTO {scratch_schema}.dim_path VALUES "
            f"(10, '10', 'p10', '', 0), (11, '11', 'p11', '', 0)"))
    start = _minute()
    rows = [
        (start + timedelta(seconds=5), 1, 10, 10, 100),
        (start + timedelta(seconds=10), 1, 11, 20, 101),
        (start + timedelta(seconds=20), 2, 10, 30, 102),
    ]
    with eng.begin() as conn:
        conn.execute(text(
            f"INSERT INTO {scratch_schema}.fact (ts, host, path, val, id) "
            f"VALUES (:ts, :host, :path, :val, :id)"),
            [{"ts": r[0], "host": r[1], "path": r[2], "val": r[3],
              "id": r[4]} for r in rows])
    try:
        yield src, eng, start
    finally:
        pass


class TestMultibar:
    def test_zaehlt_pro_serie(self, env):
        src, eng, start = env
        plot = timeseries_multibar(
            src, group_by="host", t_from=start,
            t_to=start + timedelta(seconds=60), interval=60,
            filter_tree=[], measure=None, dest_engine=eng)
        # Die Schlüssel in `data` sind Slot-Indizes ab `interval_start`.
        slot = 0
        assert plot["type"] == "multibar"
        assert plot["data"][slot]["_1"] == 2
        assert plot["data"][slot]["_2"] == 1

    def test_ohne_gruppierung(self, env):
        src, eng, start = env
        plot = timeseries_multibar(
            src, group_by="", t_from=start,
            t_to=start + timedelta(seconds=60), interval=60,
            filter_tree=[], measure=None, dest_engine=eng)
        slot = 0
        assert list(plot["data"][slot].values()) == [3]

    def test_summe(self, env):
        src, eng, start = env
        plot = timeseries_multibar(
            src, group_by="", t_from=start,
            t_to=start + timedelta(seconds=60), interval=60,
            filter_tree=[], measure="sum(val)", dest_engine=eng)
        slot = 0
        assert list(plot["data"][slot].values()) == [60]

    def test_filter(self, env):
        src, eng, start = env
        tree = [{
            "andOr": "and",
            "row": {"col": "host", "op": "in", "vals": ["1"]},
            "children": [],
        }]
        plot = timeseries_multibar(
            src, group_by="host", t_from=start,
            t_to=start + timedelta(seconds=60), interval=60,
            filter_tree=tree, measure=None, dest_engine=eng)
        slot = 0
        assert plot["data"][slot] == {"_1": 2}


class TestLadder:
    def test_quantile(self, env):
        src, eng, start = env
        plot = timeseries_ladder(
            src, measure_col="val", rungs=[50], ladder_kind="quantile",
            group_by="", t_from=start,
            t_to=start + timedelta(seconds=60), interval=60,
            filter_tree=[], graph_style="quantile-steps",
            dest_engine=eng)
        slot = 0
        values = list(plot["data"][slot].values())[0]
        assert values == [20.0]

    def test_extent(self, env):
        src, eng, start = env
        plot = timeseries_ladder(
            src, measure_col="val", rungs=["min", "avg", "max"],
            ladder_kind="extent", group_by="", t_from=start,
            t_to=start + timedelta(seconds=60), interval=60,
            filter_tree=[], graph_style="quantile-steps",
            dest_engine=eng)
        slot = 0
        values = list(plot["data"][slot].values())[0]
        assert values == [10.0, 20.0, 30.0]


class TestDrill:
    def test_verteilt_auf_die_anderen_dimensionen(self, env):
        src, eng, start = env
        results = drill_down(
            src, eng, "host", "1", start,
            start + timedelta(seconds=60), limit=10)
        by_col = {r["column"]: r for r in results}
        assert "path" in by_col
        vals = {v["source_id"]: v["count"] for v in by_col["path"]["values"]}
        assert vals["10"] == 1
        assert vals["11"] == 1

    def test_list_transactions(self, env):
        src, eng, start = env
        raw, display = list_transactions(
            src, eng, "host", "1", start,
            start + timedelta(seconds=60))
        assert raw == ["100", "101"]
        assert display == raw


class TestGapDetection:
    def _profile(self, start, hours):
        epoch = int(start.timestamp())
        return {"prof_from": epoch - 86400, "prof_to": epoch,
                "days": 1.0,
                "hours": {str(h): rate for h, rate in hours.items()},
                "source": "fact"}

    def test_findet_eine_luecke(self, env):
        src, eng, start = env
        # Stunde mit Rate 1/s: 60 s Lücke ergeben μ = 60 > 20.76.
        profile = self._profile(start, {h: 1.0 for h in range(24)})
        with eng.begin() as conn:
            conn.execute(text(
                f"INSERT INTO {src.schema_name}.fact (ts, host, path, val, id) "
                f"VALUES (:ts, 1, 10, 1, 999)"),
                {"ts": start + timedelta(seconds=120)})
        res = detect_gaps(
            src, t_from=start, t_to=start + timedelta(seconds=121),
            hwm=start + timedelta(seconds=121), profile=profile,
            dest_engine=eng)
        assert res["candidate_gaps"] >= 1
        assert res["gaps"]
        g = res["gaps"][0]
        assert g["left_txn_ms"] == int(
            (start + timedelta(seconds=20)).timestamp()) * 1000
        assert g["right_txn_ms"] == int(
            (start + timedelta(seconds=120)).timestamp()) * 1000

    def test_stille_stunde_erzeugt_keinen_gap(self, env):
        src, eng, start = env
        profile = self._profile(start, {h: 0.0 for h in range(24)})
        res = detect_gaps(
            src, t_from=start, t_to=start + timedelta(seconds=60),
            hwm=start + timedelta(seconds=60), profile=profile,
            dest_engine=eng)
        assert res["gaps"] == []
        assert res["candidate_gaps"] == 0
