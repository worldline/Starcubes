"""core/query.py — Filter- und SQL-Builder ohne Datenbank.

Die ausführenden Query-Funktionen brauchen PostgreSQL; die Bausteine davor
sind reine Zeichenketten- und Strukturlogik und hier vollständig prüfbar.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.query import (
    EXTENT_RUNGS,
    GAP_MAX_SCAN_S,
    _coerce_filter_vals,
    _inline_bind_params,
    _literal_smallint_array,
    _literal_text,
    _literal_text_array,
    _literal_ts,
    _profile_integral,
    _render_param,
    _utc_ts,
    build_filter_tree,
    build_group_by_indices,
    check_filter_col,
    filter_target_columns,
    filter_tree_columns,
    gap_profile_bounds,
    gap_scan_bounds,
    grouped_label,
    grouped_token,
    normalize_rungs,
    parse_ladder_agg,
    slugify_group_set,
    split_grouped_col,
    validate_filters,
    validate_filter_tree,
)
from core.source import Dimension, DimGroup, DimGroupMember, DimGroupSet, FactColumn


def _dim(**over):
    values = dict(
        id=1, fact_column="host", dim_table="dim_host",
        source_table="hosts", source_key="id", source_name="name",
        source_description="", is_primary=True,
    )
    values.update(over)
    return Dimension(**values)


def _gset(dim=None):
    dim = dim or _dim()
    gs = DimGroupSet(id=1, name="Host-Typen", slug="host-typen")
    gs.groups = [
        DimGroup(id=1, group_name="intern", color="#111",
                 members=[DimGroupMember(2), DimGroupMember(0)]),
        DimGroup(id=2, group_name="extern", color="#222",
                 members=[DimGroupMember(1)]),
    ]
    dim.group_sets = [gs]
    return dim, gs


class TestParseLadderAgg:
    @pytest.mark.parametrize("agg,expected", [
        ("quantile(stay)", ("quantile", "stay")),
        ("extent(stay)", ("extent", "stay")),
        ("  quantile(stay)  ", ("quantile", "stay")),
    ])
    def test_gueltig(self, agg, expected):
        assert parse_ladder_agg(agg) == expected

    @pytest.mark.parametrize("agg", [None, "", "sum(stay)", "quantile()", "quantile(a b)"])
    def test_ungueltig(self, agg):
        assert parse_ladder_agg(agg) is None


class TestNormalizeRungs:
    def test_quantile_sortiert_und_dedupliziert(self):
        assert normalize_rungs("quantile", [95, 5, 50, 5]) == [5, 50, 95]

    def test_quantile_akzeptiert_strings(self):
        assert normalize_rungs("quantile", ["5", "95"]) == [5, 95]

    @pytest.mark.parametrize("rungs", [[], [0], [100], [-5], ["min"]])
    def test_quantile_ungueltig(self, rungs):
        with pytest.raises(ValueError):
            normalize_rungs("quantile", rungs)

    def test_extent_kanonische_reihenfolge(self):
        assert normalize_rungs("extent", ["max", "min", "avg"]) == list(EXTENT_RUNGS)

    def test_extent_braucht_zwei_rungs(self):
        with pytest.raises(ValueError, match="at least two"):
            normalize_rungs("extent", ["min"])

    def test_extent_unbekannte_rung(self):
        with pytest.raises(ValueError, match="must be from"):
            normalize_rungs("extent", ["min", "median"])


class TestGroupTokens:
    def test_slugify(self):
        assert slugify_group_set("Größe") == "groesse"
        assert slugify_group_set("Host Typen") == "host-typen"
        assert slugify_group_set("  !!!  ") == "group"
        assert slugify_group_set("A/B-C") == "a-b-c"

    def test_grouped_token(self):
        assert grouped_token("host", "host-typen") == "host:grouped:host-typen"

    def test_grouped_label(self):
        assert grouped_label("host", "extern") == "host → extern"
        assert grouped_label("host", "host (grouped)") == "host (grouped)"

    @pytest.mark.parametrize("col,expected", [
        ("peer:grouped:peer-types", ("peer", "peer-types", True)),
        ("peer:grouped", ("peer", None, True)),
        ("peer:grouped:", ("peer", None, True)),
        ("peer", ("peer", None, False)),
        ("host:grouped", ("host", None, True)),
    ])
    def test_split_grouped_col(self, col, expected):
        assert split_grouped_col(col) == expected


class TestGroupByIndices:
    def test_sortiert_mitglieder(self):
        _dim_, gs = _gset()
        assert build_group_by_indices(gs) == {"intern": [0, 2], "extern": [1]}


class TestBuildFilterTree:
    def test_verschachtelt_nach_indent(self):
        flat = [
            {"col": "host", "op": "in", "vals": ["a"]},
            {"col": "path", "op": "in", "vals": ["b"], "andOr": "or", "indent": 1},
            {"col": "val", "op": "in", "vals": ["c"], "indent": 0},
        ]
        tree = build_filter_tree(flat)
        assert len(tree) == 2
        assert tree[0]["row"]["col"] == "host"
        assert tree[0]["children"][0]["row"]["col"] == "path"
        assert tree[0]["children"][0]["andOr"] == "or"
        assert tree[1]["row"]["col"] == "val"

    def test_row_form(self):
        tree = build_filter_tree([
            {"row": {"col": "host", "op": "not_in", "vals": ["x"]},
             "andOr": "and", "indent": 0},
        ])
        assert tree[0]["row"] == {"col": "host", "op": "not_in", "vals": ["x"]}

    def test_defaults(self):
        tree = build_filter_tree([{"col": "host", "op": "in", "vals": ["a"]}])
        assert tree[0]["andOr"] == "and"
        assert tree[0]["children"] == []

    def test_leere_liste(self):
        assert build_filter_tree([]) == []


class TestFilterColumns:
    def test_gruppierter_token_liest_nur_seine_spalte(self):
        dim, _ = _gset()
        dims = {"host": dim}
        assert filter_target_columns("host:grouped:host-typen", dims) == ["host"]

    def test_filter_group_faechert_auf(self):
        a = _dim(id=1, fact_column="host", filter_group="hostpath")
        b = _dim(id=2, fact_column="path", filter_group="hostpath")
        dims = {"host": a, "path": b}
        assert filter_target_columns("hostpath", dims) == ["host", "path"]

    def test_einfache_spalte(self):
        assert filter_target_columns("val", {}) == ["val"]

    def test_filter_tree_columns_rekursiv(self):
        a = _dim(id=1, fact_column="host", filter_group="hostpath")
        b = _dim(id=2, fact_column="path", filter_group="hostpath")
        dims = {"host": a, "path": b}
        tree = [{
            "row": {"col": "hostpath", "op": "in", "vals": []},
            "children": [{"row": {"col": "val", "op": "in", "vals": []},
                          "children": []}],
        }]
        assert filter_tree_columns(tree, dims) == {"host", "path", "val"}


class TestValidateFilter:
    def _source(self, make_source, **over):
        dim, _ = _gset()
        return make_source(fact_columns=[
            FactColumn("ts", "timestamptz", "time", 0, "ts"),
            FactColumn("host", "smallint", "dimension", 1, "host"),
            FactColumn("val", "integer", "measure", 2, "val"),
        ], dimensions=[dim], **over)

    def test_gueltige_spalten(self, make_source):
        src = self._source(make_source)
        check_filter_col(src, "host")
        check_filter_col(src, "val")
        check_filter_col(src, "host:grouped:host-typen")
        check_filter_col(src, "host:grouped")

    def test_unbekannte_spalte(self, make_source):
        src = self._source(make_source)
        with pytest.raises(ValueError, match="not a valid fact column"):
            check_filter_col(src, "gibtsnicht")

    def test_gruppierter_token_ohne_gruppe(self, make_source):
        dim, _ = _gset()
        src = make_source(
            fact_columns=[
                FactColumn("host", "smallint", "dimension", 0, "host")],
            dimensions=[dim])
        with pytest.raises(ValueError):
            check_filter_col(src, "host:grouped:fehlt")

    def test_ungueltiger_op(self, make_source):
        src = self._source(make_source)
        tree = [{"row": {"col": "val", "op": "zwischen", "vals": []},
                 "children": []}]
        with pytest.raises(ValueError, match="must be one of"):
            validate_filter_tree(src, tree)

    def test_validate_filters_groups_form(self, make_source):
        src = self._source(make_source)
        validate_filters(src, filter_groups=[[
            {"col": "val", "op": "in", "vals": ["1"]}]])
        with pytest.raises(ValueError):
            validate_filters(src, filter_groups=[[
                {"col": "nein", "op": "in", "vals": []}]])

    def test_tree_hat_vorrang(self, make_source):
        src = self._source(make_source)
        # Der Tree ist gültig, die Groups sind Unsinn — bei Vorrang des Trees
        # darf die Groups-Form gar nicht erst gelesen werden.
        validate_filters(
            src,
            filter_tree=[{"row": {"col": "val", "op": "in", "vals": []},
                          "children": []}],
            filter_groups=[[{"col": "nein", "op": "in", "vals": []}]])


class TestCoerceFilterVals:
    def test_integer_familie(self):
        assert _coerce_filter_vals("integer", ["1", "2"]) == [1, 2]
        assert _coerce_filter_vals("bigint", ["3"]) == [3]
        assert _coerce_filter_vals("smallint", ["4"]) == [4]

    def test_numeric_familie(self):
        assert _coerce_filter_vals("numeric", ["1.5"]) == [1.5]
        assert _coerce_filter_vals("double precision", ["2"]) == [2.0]

    def test_text_bleibt_text(self):
        assert _coerce_filter_vals("text", ["1", "abc"]) == ["1", "abc"]

    def test_unbekannter_typ_wird_text(self):
        assert _coerce_filter_vals(None, [1, 2]) == ["1", "2"]


class TestProfileIntegral:
    def test_ueberspringt_stunden_korrekt(self):
        # Regression: ohne `seg = seg_end` wurde das erste Segment 400×
        # addiert. 2 Stunden, konstante Rate 1/s → 7200, nicht 2×3600+...
        rate = {h: 1.0 for h in range(24)}
        start = 10 * 3600  # 10:00 UTC
        assert _profile_integral(rate, start, start + 7200) == 7200.0

    def test_stundenuebergang_mit_unterschiedlichen_raten(self):
        rate = {10: 1.0, 11: 2.0}
        start = 10 * 3600 + 1800  # 10:30
        end = 11 * 3600 + 1800    # 11:30
        # 30 min * 1/s + 30 min * 2/s = 1800 + 3600
        assert _profile_integral(rate, start, end) == 5400.0

    def test_stille_stunde_traegt_nichts_bei(self):
        assert _profile_integral({}, 0, 3600) == 0.0

    def test_ende_vor_start(self):
        assert _profile_integral({1: 5.0}, 100, 100) == 0.0
        assert _profile_integral({1: 5.0}, 200, 100) == 0.0


class TestGapBounds:
    def test_profil_endet_an_voller_utc_nacht(self, make_source):
        src = make_source()
        t_to = int(datetime(2026, 5, 7, 13, 30, tzinfo=timezone.utc).timestamp())
        hwm = int(datetime(2026, 5, 7, 10, 0, tzinfo=timezone.utc).timestamp())
        prof_from, prof_to = gap_profile_bounds(src, t_to, hwm, 7)
        assert prof_to == int(datetime(2026, 5, 7, tzinfo=timezone.utc).timestamp())
        assert prof_to - prof_from == 7 * 86400

    def test_retention_floor_hebt_den_anfang(self, make_source):
        floor = datetime(2026, 5, 5, tzinfo=timezone.utc)
        src = make_source(retention_floor=floor)
        t_to = int(datetime(2026, 5, 7, tzinfo=timezone.utc).timestamp())
        prof_from, _ = gap_profile_bounds(src, t_to, t_to, 7)
        assert prof_from == int(floor.timestamp())

    def test_scan_kappt_das_ende(self, make_source):
        src = make_source()
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = start + timedelta(days=10)
        scan_from, scan_to, truncated = gap_scan_bounds(src, start, end, None)
        assert truncated is True
        assert scan_to - scan_from == GAP_MAX_SCAN_S
        assert scan_from == int(start.timestamp())

    def test_scan_utzt_hwm_und_floor(self, make_source):
        floor = datetime(2026, 1, 2, tzinfo=timezone.utc)
        hwm = datetime(2026, 1, 5, tzinfo=timezone.utc)
        src = make_source(retention_floor=floor)
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = datetime(2026, 1, 6, tzinfo=timezone.utc)
        scan_from, scan_to, truncated = gap_scan_bounds(src, start, end, hwm)
        assert scan_from == int(floor.timestamp())
        assert scan_to == int(hwm.timestamp())
        assert truncated is False

    def test_naive_werte_sind_utc(self, make_source):
        src = make_source()
        start = datetime(2026, 1, 1, 12)
        end = datetime(2026, 1, 1, 13)
        scan_from, scan_to, _ = gap_scan_bounds(src, start, end, None)
        assert scan_from == int(datetime(2026, 1, 1, 12,
                                         tzinfo=timezone.utc).timestamp())
        assert scan_to - scan_from == 3600


class TestUtcTs:
    def test_timestamptz_wird_utc_normalisiert(self, make_source):
        src = make_source(fact_columns=[
            FactColumn("ts", "timestamptz", "time", 0, "ts")],
            partition_column="ts")
        assert _utc_ts(src, "f.ts") == "(f.ts AT TIME ZONE 'UTC')"

    def test_naive_spalte_bleibt(self, make_source):
        src = make_source(fact_columns=[
            FactColumn("ts", "timestamp", "time", 0, "ts")],
            partition_column="ts")
        assert _utc_ts(src, "f.ts") == "f.ts"


class TestLiterals:
    def test_ts(self):
        dt = datetime(2026, 5, 7, 1, 2, 3, tzinfo=timezone.utc)
        assert _literal_ts(dt) == "'2026-05-07 01:02:03+00'::timestamptz"

    def test_ts_wird_nach_utc_konvertiert(self):
        dt = datetime(2026, 5, 7, 3, 0, 0,
                      tzinfo=timezone(timedelta(hours=2)))
        assert _literal_ts(dt) == "'2026-05-07 01:00:00+00'::timestamptz"

    def test_text_escaping(self):
        assert _literal_text("a'b") == "'a''b'"
        assert _literal_text("a\\b") == "'a\\\\b'"
        # Die Reihenfolge ist wesentlich: Backslash zuerst.
        assert _literal_text("a\\'b") == "'a\\\\''b'"

    def test_arrays(self):
        assert _literal_smallint_array([1, 2]) == "ARRAY[1,2]::smallint[]"
        assert _literal_text_array(["a", "b'c"]) == \
            "ARRAY['a','b''c']::text[]"


class TestRenderParam:
    def test_datetime(self):
        dt = datetime(2026, 1, 1, tzinfo=timezone.utc)
        assert _render_param(dt, "") == _literal_ts(dt)

    def test_bool_vor_int(self):
        assert _render_param(True, "") == "true"
        assert _render_param(False, "") == "false"

    def test_int_und_str(self):
        assert _render_param(5, "") == "5"
        assert _render_param("x", "") == "'x'"

    def test_listen(self):
        assert _render_param([], "") == "ARRAY[]::smallint[]"
        assert _render_param([1, 2], "") == "ARRAY[1,2]::smallint[]"
        assert _render_param(["a"], "") == "ARRAY['a']::text[]"

    def test_unbekannter_typ(self):
        with pytest.raises(TypeError):
            _render_param(object(), "")


class TestInlineBindParams:
    def test_laengster_schluessel_gewinnt(self):
        sql = "SELECT * FROM t WHERE a = :flt0_0 AND b = :flt0"
        out = _inline_bind_params(sql, {"flt0": [1, 2], "flt0_0": [9]})
        assert out == "SELECT * FROM t WHERE a = ARRAY[9]::smallint[] " \
                      "AND b = ARRAY[1,2]::smallint[]"

    def test_unbekannter_name_bleibt(self):
        assert _inline_bind_params("SELECT 'x:y'::text", {}) == \
            "SELECT 'x:y'::text"

    def test_doppelpunkt_ohne_identifier(self):
        sql = "SELECT 'a:' || :v"
        assert _inline_bind_params(sql, {"v": "x"}) == \
            "SELECT 'a:' || 'x'"

    def test_ohne_params_unveraendert(self):
        sql = "SELECT :v"
        assert _inline_bind_params(sql, {}) == sql

    def test_doppelter_doppelpunkt_bleibt(self):
        # `::text` ist ein Cast, kein Bind-Parameter.
        assert _inline_bind_params("a::text", {}) == "a::text"
