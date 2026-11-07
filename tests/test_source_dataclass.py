"""core/source.py — Source-Dataclass: abgeleitete Sichten und SQL-Formen."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core.source import Dimension, FactColumn, Measure


def _cols():
    return [
        FactColumn("ts", "timestamptz", "time", 0, "ts"),
        FactColumn("host", "smallint", "dimension", 1, "host"),
        FactColumn("path", "smallint", "auto-dimension", 2, "path"),
        FactColumn("val", "integer", "measure", 3, "val"),
        FactColumn("small", "smallint", "measure", 4, "small"),
        FactColumn("big", "bigint", "measure", 5, "big"),
        FactColumn("id", "bigint", "pk", 6, "id"),
    ]


def _dims():
    return [
        Dimension(1, "host", "dim_host", "hosts", "id", "name", "", True),
        Dimension(2, "path", "dim_path", "paths", "id", "name", "", True),
        Dimension(3, "host", "dim_host", "hosts", "id", "name", "", False,
                  filter_group="hostpath"),
    ]


class TestPredicates:
    def test_is_logfile(self, make_source):
        assert make_source(source_dialect="logfile").is_logfile is True
        assert make_source(source_dialect="postgresql").is_logfile is False

    def test_pk_column(self, make_source):
        assert make_source(fact_columns=_cols()).pk_column() == "id"
        assert make_source(fact_columns=[]).pk_column() is None

    def test_primary_dimensions(self, make_source):
        got = make_source(dimensions=_dims()).primary_dimensions()
        assert [d.fact_column for d in got] == ["host", "path"]

    def test_filter_fanout_sortiert(self, make_source):
        src = make_source(dimensions=_dims())
        assert src.filter_fanout() == {"hostpath": ["host"]}

    def test_fact_column_order(self, make_source):
        cols = list(reversed(_cols()))
        assert make_source(fact_columns=cols).fact_column_order() == [
            "ts", "host", "path", "val", "small", "big", "id"]

    def test_measure_typen(self, make_source):
        src = make_source(fact_columns=_cols())
        assert src.smallint_measure_columns() == ["small"]
        assert src.bigint_measure_columns() == ["big"]


class TestDimMappings:
    def test_regulaere_dims_ohne_auto(self, make_source):
        src = make_source(fact_columns=_cols(), dimensions=_dims())
        assert src.dim_fact_to_table() == {"host": "dim_host"}

    def test_auto_dims(self, make_source):
        src = make_source(fact_columns=_cols(), dimensions=_dims())
        assert src.auto_dim_fact_to_table() == {"path": "dim_path"}


class TestCeilWatermark:
    def test_sql_quelle_rundet_auf(self, make_source):
        src = make_source()
        dt = datetime(2026, 1, 1, 12, 0, 0, 5, tzinfo=timezone.utc)
        assert src.ceil_watermark(dt) == datetime(
            2026, 1, 1, 12, 0, 1, tzinfo=timezone.utc)

    def test_logquelle_laesst_durch(self, make_source):
        src = make_source(source_dialect="logfile")
        dt = datetime(2026, 1, 1, 12, 0, 0, 5, tzinfo=timezone.utc)
        assert src.ceil_watermark(dt) is dt


class TestWatermarkExpressions:
    def test_fact_sql_ohne_ausdruck_nimmt_partitionsspalte(self, make_source):
        assert make_source(partition_column="ts").watermark_fact_sql() == "ts"

    def test_fact_sql_mit_ausdruck(self, make_source):
        assert make_source(
            watermark_fact_expr="create_time + total").watermark_fact_sql() == \
            "create_time + total"

    @pytest.mark.parametrize("dialect,sql", [
        ("postgresql", "CURRENT_TIMESTAMP AT TIME ZONE 'UTC'"),
        ("oracle", "SYS_EXTRACT_UTC(SYSTIMESTAMP)"),
        ("mysql", "UTC_TIMESTAMP()"),
        ("mssql", "SYSUTCDATETIME()"),
    ])
    def test_source_now_sql_pro_dialekt(self, make_source, dialect, sql):
        assert make_source(source_dialect=dialect).source_now_sql() == sql

    def test_source_now_sql_bei_logquelle_wirft(self, make_source):
        with pytest.raises(ValueError, match="log source"):
            make_source(source_dialect="logfile").source_now_sql()

    def test_probe_mit_eigenem_query_braucht_dual_nur_bei_oracle(self, make_source):
        src = make_source(source_dialect="oracle",
                          watermark_max_query="SELECT max(ts) FROM dual")
        max_q, now_q = src.watermark_probe_sql()
        assert max_q == "SELECT max(ts) FROM dual"
        assert now_q == "SELECT SYS_EXTRACT_UTC(SYSTIMESTAMP) FROM dual"

        src = make_source(source_dialect="postgresql",
                          watermark_max_query="SELECT max(ts)")
        max_q, now_q = src.watermark_probe_sql()
        assert now_q == "SELECT CURRENT_TIMESTAMP AT TIME ZONE 'UTC'"

    def test_probe_gegen_base_table(self, make_source):
        max_q, now_q = make_source(
            base_table="src", watermark_column="ts").watermark_probe_sql()
        assert max_q == ("SELECT max(ts), "
                         "CURRENT_TIMESTAMP AT TIME ZONE 'UTC' FROM src")
        assert now_q is None

    def test_probe_gegen_import_query(self, make_source):
        max_q, _ = make_source(
            base_table=None, import_query="SELECT ts FROM src",
            watermark_column="ts").watermark_probe_sql()
        assert "(SELECT ts FROM src) q" in max_q

    def test_probe_bei_logquelle_wirft(self, make_source):
        with pytest.raises(ValueError, match="log source"):
            make_source(source_dialect="logfile").watermark_probe_sql()


class TestMeasureCatalog:
    def _measures(self):
        return [
            Measure("Anzahl", "count(*)", "multibar", True, 1, None),
            Measure("Dauer p95", "quantile(stay)", "quantile-bands", False, 2,
                    "5,25,50,75,95"),
            Measure("Dauer hoch", "quantile(stay)", "quantile-steps", False, 3,
                    "90,99"),
            Measure("Spanne", "extent(stay)", "quantile-steps-open", False, 4,
                    "max,min"),
        ]

    def test_measure_row_name_gewinnt(self, make_source):
        src = make_source(measures=self._measures())
        assert src.measure_row("quantile(stay)", "Dauer hoch").name == "Dauer hoch"

    def test_measure_row_ohne_name_nimmt_die_erste(self, make_source):
        src = make_source(measures=self._measures())
        assert src.measure_row("quantile(stay)").name == "Dauer p95"

    def test_measure_row_unbekannt(self, make_source):
        assert make_source(measures=self._measures()).measure_row("sum(x)") is None

    def test_graph_style_default(self, make_source):
        assert make_source(measures=[]).graph_style_for("sum(x)") == "multibar"

    def test_graph_style_mit_name(self, make_source):
        src = make_source(measures=self._measures())
        assert src.graph_style_for("quantile(stay)", "Dauer hoch") == \
            "quantile-steps"

    def test_rungs_quantile_ohne_katalog(self, make_source):
        assert make_source(measures=[]).rungs_for("quantile(stay)") == \
            [5, 25, 50, 75, 95]

    def test_rungs_quantile_sortiert_und_dedupliziert(self, make_source):
        src = make_source(measures=self._measures())
        assert src.rungs_for("quantile(stay)", "Dauer p95") == [5, 25, 50, 75, 95]
        assert src.rungs_for("quantile(stay)", "Dauer hoch") == [90, 99]

    def test_rungs_extent_kanonisch(self, make_source):
        src = make_source(measures=self._measures())
        # "max,min" → kanonisch ["min", "max"].
        assert src.rungs_for("extent(stay)") == ["min", "max"]

    def test_rungs_extent_fallback_bei_einer_rung(self, make_source):
        measures = [Measure("x", "extent(v)", "quantile-steps", False, 1, "min")]
        src = make_source(measures=measures)
        assert src.rungs_for("extent(v)") == ["min", "avg", "max"]
