"""core/rollup.py — reine Konfigurations- und Ausdruckslogik.

Die DDL- und Refresh-Seite braucht eine Datenbank; hier geht es um die
Entscheidungen, die davor liegen: welche Specs es gibt, wann eine Rollup
nicht mehr sichtbar sein darf (``spec_hash``), was eine Konfiguration
ablehnbar macht und welcher Ausdruck eine Measure beantwortet.
"""
from __future__ import annotations

from datetime import datetime, timezone

from core.rollup import (
    RollupSpec,
    RollupState,
    final_through,
    floor_to_bucket,
    interval_label,
    measure_columns,
    measure_expr,
    rollup_extent,
    rollup_specs,
    spec_hash,
    unreconstructable_span,
    validate_config,
)
from core.source import FactColumn


def _cols():
    return [
        FactColumn("ts", "timestamptz", "time", 0, "ts"),
        FactColumn("host", "smallint", "dimension", 1, "host"),
        FactColumn("path", "smallint", "auto-dimension", 2, "path"),
        FactColumn("val", "integer", "measure", 3, "val"),
        FactColumn("amount", "bigint", "measure", 4, "amount"),
        FactColumn("ok", "boolean", "measure", 5, "ok"),
    ]


class TestIntervalLabel:
    def test_stabile_suffixe(self):
        assert interval_label(60) == "1m"
        assert interval_label(300) == "5m"
        assert interval_label(3600) == "1h"
        assert interval_label(7200) == "2h"
        assert interval_label(86400) == "1d"
        assert interval_label(172800) == "2d"

    def test_nicht_teilbare_werte_in_sekunden(self):
        assert interval_label(90) == "90s"
        # 0 ist durch 86400 teilbar; die Funktion hat keinen Sonderfall.
        assert interval_label(0) == "0d"


class TestRollupSpecs:
    def test_aus_erzeugt_nichts(self, make_source):
        assert rollup_specs(make_source(rollup_interval_s=0)) == []
        assert rollup_specs(make_source(rollup_interval_s=-1)) == []

    def test_kern_und_marginals(self, make_source):
        src = make_source(
            rollup_interval_s=3600,
            rollup_columns=["path", "host", "path"],
            rollup_marginals=["path", "host", "path"],
            rollup_measures=["sum(val)", "avg(val)", "sum(val)"],
        )
        specs = rollup_specs(src)
        assert [s.name for s in specs] == [
            "rollup_1h", "rollup_1h_host", "rollup_1h_path"]
        core = specs[0]
        assert core.columns == ("host", "path")
        assert core.measures == ("avg(val)", "sum(val)")
        assert core.is_marginal is False
        assert specs[1].columns == ("host",)
        assert specs[1].is_marginal is True

    def test_ohne_kernspalten_nur_marginals(self, make_source):
        src = make_source(rollup_interval_s=3600,
                          rollup_columns=[], rollup_marginals=["host"])
        assert [s.name for s in rollup_specs(src)] == ["rollup_1h_host"]


class TestSpecHash:
    def _spec(self, **over):
        values = dict(name="r", columns=("host",), measures=("sum(val)",),
                      interval_s=3600)
        values.update(over)
        return RollupSpec(**values)

    def test_gleich_fuer_gleiche_form(self):
        assert spec_hash(self._spec()) == spec_hash(self._spec())

    def test_name_zaehlt_nicht(self):
        # Der Name ist die Identität der Tabelle, nicht ihres Inhalts.
        assert spec_hash(self._spec(name="a")) == spec_hash(self._spec(name="b"))

    def test_spalten_aendern_den_hash(self):
        assert spec_hash(self._spec()) != spec_hash(self._spec(columns=("path",)))

    def test_measures_aendern_den_hash(self):
        assert spec_hash(self._spec()) != spec_hash(
            self._spec(measures=("sum(val)", "avg(val)")))

    def test_intervall_aendert_den_hash(self):
        assert spec_hash(self._spec()) != spec_hash(self._spec(interval_s=7200))


class TestValidateConfig:
    def test_aus_ist_immer_gueltig(self, make_source):
        src = make_source(rollup_interval_s=0, rollup_columns=["gibt_es_nicht"],
                          rollup_measures=["quatsch(x)"])
        assert validate_config(src) == []

    def test_gueltige_konfiguration(self, make_source):
        src = make_source(
            rollup_interval_s=3600,
            fact_columns=_cols(),
            rollup_columns=["host", "path"],
            rollup_marginals=[],
            rollup_measures=["sum(val)", "avg(amount)", "min(val)"],
        )
        assert validate_config(src) == []

    def test_negatives_intervall(self, make_source):
        problems = validate_config(make_source(rollup_interval_s=-60))
        assert any("must not be negative" in p for p in problems)

    def test_unbekannte_spalte(self, make_source):
        src = make_source(rollup_interval_s=3600, fact_columns=_cols(),
                          rollup_columns=["fehlt"])
        assert any("is not a fact column" in p for p in validate_config(src))

    def test_falsche_rolle(self, make_source):
        src = make_source(rollup_interval_s=3600, fact_columns=_cols(),
                          rollup_columns=["ts"])
        assert any("dimension columns only" in p for p in validate_config(src))

    def test_ueberschneidung_kern_marginal(self, make_source):
        src = make_source(rollup_interval_s=3600, fact_columns=_cols(),
                          rollup_columns=["host"], rollup_marginals=["host"])
        assert any("both rollup_columns and rollup_marginals" in p
                   for p in validate_config(src))

    def test_quantile_wird_abgelehnt(self, make_source):
        src = make_source(rollup_interval_s=3600, fact_columns=_cols(),
                          rollup_measures=["quantile(val)"])
        assert any("percentiles cannot be pre-aggregated" in p
                   for p in validate_config(src))

    def test_unbekannte_funktion(self, make_source):
        src = make_source(rollup_interval_s=3600, fact_columns=_cols(),
                          rollup_measures=["median(val)"])
        assert any("unknown function" in p for p in validate_config(src))

    def test_measure_auf_dimensionsspalte(self, make_source):
        src = make_source(rollup_interval_s=3600, fact_columns=_cols(),
                          rollup_measures=["sum(host)"])
        assert any("not 'measure'" in p for p in validate_config(src))

    def test_typ_ohne_summentyp(self, make_source):
        src = make_source(rollup_interval_s=3600, fact_columns=_cols(),
                          rollup_measures=["sum(ok)"])
        assert any("no defined sum type" in p for p in validate_config(src))

    def test_kaputte_schreibweise(self, make_source):
        src = make_source(rollup_interval_s=3600, fact_columns=_cols(),
                          rollup_measures=["sum val"])
        assert any("not of the form" in p for p in validate_config(src))


class TestMeasureColumns:
    def _spec(self, *measures):
        return RollupSpec(name="r", columns=("host",), measures=tuple(measures),
                          interval_s=3600)

    def test_sum_legt_eine_spalte_an(self, make_source):
        got = measure_columns(self._spec("sum(val)"), make_source(fact_columns=_cols()))
        assert got == {"sum_val": ("bigint", "sum(f.val)")}

    def test_avg_braucht_summe_und_count(self, make_source):
        got = measure_columns(self._spec("avg(amount)"), make_source(fact_columns=_cols()))
        assert got["sum_amount"][0] == "numeric"
        assert got["cnt_amount"] == ("bigint", "count(f.amount)")

    def test_min_max(self, make_source):
        got = measure_columns(self._spec("min(val)", "max(amount)"),
                              make_source(fact_columns=_cols()))
        assert got["min_val"] == ("integer", "min(f.val)")
        assert got["max_amount"] == ("bigint", "max(f.amount)")

    def test_extent_materialisiert_immer_alle_vier(self, make_source):
        # Die Rung-Liste ist Präsentation und nicht Teil des spec_hash; deshalb
        # müssen die Spalten unabhängig von ihr existieren.
        got = measure_columns(self._spec("extent(val)"),
                              make_source(fact_columns=_cols()))
        assert set(got) == {"sum_val", "cnt_val", "min_val", "max_val"}

    def test_kein_n(self, make_source):
        got = measure_columns(self._spec("sum(val)"), make_source(fact_columns=_cols()))
        assert "n" not in got


class TestMeasureExpr:
    def _spec(self, *measures):
        return RollupSpec(name="r", columns=("host",), measures=tuple(measures),
                          interval_s=3600)

    def test_count_ohne_measure(self):
        assert measure_expr(None, self._spec()) == "sum(f.n)"
        assert measure_expr("count(*)", self._spec()) == "sum(f.n)"

    def test_sum(self):
        assert measure_expr("sum(val)", self._spec("sum(val)")) == "sum(f.sum_val)"

    def test_bare_spalte_bedeutet_sum(self):
        assert measure_expr("val", self._spec("sum(val)")) == "sum(f.sum_val)"

    def test_avg(self):
        got = measure_expr("avg(val)", self._spec("avg(val)"))
        assert got == "sum(f.sum_val)::numeric / NULLIF(sum(f.cnt_val), 0)"

    def test_min_max(self):
        assert measure_expr("min(val)", self._spec("min(val)")) == "min(f.min_val)"
        assert measure_expr("max(val)", self._spec("max(val)")) == "max(f.max_val)"

    def test_extent_array_in_kanonischer_reihenfolge(self):
        got = measure_expr("extent(val)", self._spec("extent(val)"))
        assert got is not None
        assert got.startswith("ARRAY[")
        assert "min(f.min_val)" in got
        assert "max(f.max_val)" in got

    def test_extent_rungs_bleiben_in_kanonischer_reihenfolge(self):
        # Die Reihenfolge ist der Vertrag mit der Fakt-Seite
        # (`normalize_rungs` sortiert ebenfalls nach EXTENT_RUNGS), nicht die
        # Eingabereihenfolge.
        got = measure_expr("extent(val)", self._spec("extent(val)"),
                           rungs=["max", "min"])
        assert got is not None
        assert got.index("min(f.min_val)") < got.index("max(f.max_val)")

    def test_extent_mit_weniger_als_zwei_rungs_ist_nicht_servierbar(self):
        assert measure_expr("extent(val)", self._spec("extent(val)"),
                            rungs=["min"]) is None

    def test_nicht_konfigurierte_measure(self):
        assert measure_expr("sum(amount)", self._spec("sum(val)")) is None

    def test_quantile_ist_nicht_servierbar(self):
        assert measure_expr("quantile(val)", self._spec("sum(val)")) is None


class TestBucketMath:
    def test_floor_to_bucket(self):
        dt = datetime(2026, 5, 7, 13, 47, 31, tzinfo=timezone.utc)
        assert floor_to_bucket(dt, 3600) == datetime(2026, 5, 7, 13, tzinfo=timezone.utc)
        assert floor_to_bucket(dt, 86400) == datetime(2026, 5, 7, tzinfo=timezone.utc)

    def test_floor_to_bucket_ist_idempotent(self):
        dt = datetime(2026, 5, 7, 13, 47, tzinfo=timezone.utc)
        once = floor_to_bucket(dt, 300)
        assert floor_to_bucket(once, 300) == once

    def test_final_through_zieht_die_overlap_ab(self, make_source):
        hwm = datetime(2026, 5, 7, 13, 47, 31, tzinfo=timezone.utc)
        src = make_source(late_arrival_overlap_s=0)
        assert final_through(src, hwm, 3600) == datetime(
            2026, 5, 7, 13, tzinfo=timezone.utc)
        src = make_source(late_arrival_overlap_s=3600)
        assert final_through(src, hwm, 3600) == datetime(
            2026, 5, 7, 12, tzinfo=timezone.utc)

    def test_final_through_ohne_hwm(self, make_source):
        assert final_through(make_source(), None, 3600) is None


class TestUnreconstructableSpan:
    def _state(self, from_, through=None):
        return RollupState(spec_hash="x", built_from=from_,
                           built_through=through or from_)

    def test_ohne_retention_floor_nichts(self, make_source):
        old = datetime(2025, 12, 1, tzinfo=timezone.utc)
        states = {"r": self._state(old)}
        assert unreconstructable_span(make_source(), states) is None

    def test_keine_rollup_reicht_unter_die_grenze(self, make_source):
        floor = datetime(2026, 1, 1, tzinfo=timezone.utc)
        states = {"r": self._state(datetime(2026, 2, 1, tzinfo=timezone.utc))}
        assert unreconstructable_span(
            make_source(retention_floor=floor), states) is None

    def test_unter_der_grenze_wird_gemeldet(self, make_source):
        floor = datetime(2026, 1, 1, tzinfo=timezone.utc)
        old = datetime(2025, 12, 1, tzinfo=timezone.utc)
        states = {"r": self._state(old)}
        assert unreconstructable_span(
            make_source(retention_floor=floor), states) == (old, floor)

    def test_naive_grenze_wird_als_utc_gelesen(self, make_source):
        floor = datetime(2026, 1, 1)  # naiv
        old = datetime(2025, 12, 1, tzinfo=timezone.utc)
        got = unreconstructable_span(
            make_source(retention_floor=floor), {"r": self._state(old)})
        assert got is not None
        assert got[1].tzinfo is timezone.utc


class TestRollupExtent:
    def test_min_und_max(self):
        a = RollupState("x", datetime(2026, 1, 1), datetime(2026, 2, 1))
        b = RollupState("y", datetime(2025, 12, 1), datetime(2026, 3, 1))
        assert rollup_extent({"a": a, "b": b}) == (
            datetime(2025, 12, 1), datetime(2026, 3, 1))

    def test_leer(self):
        assert rollup_extent({}) == (None, None)
