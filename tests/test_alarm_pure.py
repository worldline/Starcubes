"""core/alarm.py — Validierung, Kontext-Normalisierung, SQL-Aufbau.

Die Episode-Maschine selbst braucht eine Transaktion und wird in
``test_db_alarm.py`` geprüft.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.alarm import (
    _window_below_floor,
    alarm_aggregate_sql,
    check_no_grouped_filters,
    measure_value_expr,
    normalize_context_rows,
    stamp_transitions,
)
from core.source import Dimension, FactColumn


def _source(make_source, **over):
    return make_source(
        schema_name="sc_test",
        fact_columns=[
            FactColumn("ts", "timestamptz", "time", 0, "ts"),
            FactColumn("val", "integer", "measure", 1, "val"),
            FactColumn("host", "smallint", "dimension", 2, "host"),
        ],
        dimensions=[Dimension(
            id=1, fact_column="host", dim_table="dim_host",
            source_table="hosts", source_key="id", source_name="name",
            source_description="", is_primary=True)],
        **over)


class TestCheckNoGroupedFilters:
    def test_sauberer_baum(self):
        tree = [{"row": {"col": "host", "op": "in", "vals": ["1"]},
                 "children": []}]
        check_no_grouped_filters(tree)

    def test_gruppierter_token_wird_abgelehnt(self):
        tree = [{"row": {"col": "host:grouped:typen", "op": "in", "vals": []},
                 "children": [
                     {"row": {"col": "host:grouped",
                              "op": "in", "vals": []}, "children": []}]}]
        with pytest.raises(ValueError, match="grouped dimension filters"):
            check_no_grouped_filters(tree)

    def test_meldung_nennt_jeden_token_einmal_sortiert(self):
        tree = [
            {"row": {"col": "b:grouped", "op": "in", "vals": []}, "children": []},
            {"row": {"col": "a:grouped", "op": "in", "vals": []}, "children": []},
            {"row": {"col": "a:grouped", "op": "in", "vals": []}, "children": []},
        ]
        with pytest.raises(ValueError) as exc:
            check_no_grouped_filters(tree)
        assert "a:grouped, b:grouped" in str(exc.value)


class TestNormalizeContextRows:
    def test_werte_werden_sortiert_und_stringifiziert(self):
        rows = [{"col": "host", "op": "in", "vals": [3, "1", 2]}]
        assert normalize_context_rows(rows) == [
            ("host", "in", ("1", "2", "3"), "and", 0)]

    def test_reihenfolge_der_zeilen_bleibt(self):
        rows = [
            {"col": "b", "op": "in", "vals": []},
            {"col": "a", "op": "in", "vals": []},
        ]
        got = normalize_context_rows(rows)
        assert [r[0] for r in got] == ["b", "a"]

    def test_nested_shape_und_defaults(self):
        rows = [{"row": {"col": "host", "op": "in", "vals": ["x"]},
                 "andOr": "or", "indent": 2}]
        assert normalize_context_rows(rows) == [
            ("host", "in", ("x",), "or", 2)]

    def test_none_und_unsinn(self):
        assert normalize_context_rows(None) == []
        assert normalize_context_rows([None, 5, {"row": 7}]) == []


class TestMeasureValueExpr:
    @pytest.mark.parametrize("measure,expected", [
        ("count(*)", "count(*)"),
        ("sum(val)", "sum(f.val)"),
        ("val", "sum(f.val)"),
        ("avg(val)", "avg(f.val)"),
        ("min(val)", "min(f.val)"),
    ])
    def test_gueltig(self, make_source, measure, expected):
        assert measure_value_expr(_source(make_source), measure) == expected

    def test_unbekannte_spalte(self, make_source):
        with pytest.raises(ValueError, match="not a fact column"):
            measure_value_expr(_source(make_source), "sum(fehlt)")

    def test_unbekannte_funktion(self, make_source):
        with pytest.raises(ValueError, match="unknown measure function"):
            measure_value_expr(_source(make_source), "quantile(val)")


class TestAlarmAggregateSql:
    def test_baut_sql_mit_zeitfenster(self, make_source):
        src = _source(make_source)
        alarm = {"measure": "count(*)", "context_filter": []}
        t_from = datetime(2026, 1, 1, tzinfo=timezone.utc)
        t_to = t_from + timedelta(hours=1)
        sql, params = alarm_aggregate_sql(src, alarm, t_from, t_to)
        assert "SELECT count(*) AS v" in sql
        assert "FROM sc_test.fact f" in sql
        assert "f.ts >= :t_from" in sql
        assert "f.ts < :t_to" in sql
        assert params["t_from"] == t_from
        assert params["t_to"] == t_to

    def test_gruppierter_filter_wird_abgelehnt(self, make_source):
        src = _source(make_source)
        alarm = {
            "measure": "count(*)",
            "context_filter": [
                {"col": "host:grouped:typen", "op": "in", "vals": []}],
        }
        with pytest.raises(ValueError, match="grouped"):
            alarm_aggregate_sql(
                src, alarm,
                datetime(2026, 1, 1, tzinfo=timezone.utc),
                datetime(2026, 1, 2, tzinfo=timezone.utc))

    def test_unbekannte_filter_spalte_wird_abgelehnt(self, make_source):
        src = _source(make_source)
        alarm = {"measure": "count(*)",
                 "context_filter": [{"col": "fehlt", "op": "in", "vals": []}]}
        with pytest.raises(ValueError, match="not a valid fact column"):
            alarm_aggregate_sql(
                src, alarm,
                datetime(2026, 1, 1, tzinfo=timezone.utc),
                datetime(2026, 1, 2, tzinfo=timezone.utc))


class TestWindowBelowFloor:
    def _alarm(self, window_s=3600):
        return {"name": "a", "window_s": window_s}

    def test_ohne_floor_false(self, make_source):
        src = make_source()
        assert _window_below_floor(
            src, self._alarm(), datetime(2026, 1, 1, tzinfo=timezone.utc)) is False

    def test_fenster_ueber_dem_floor(self, make_source):
        floor = datetime(2026, 1, 1, tzinfo=timezone.utc)
        src = make_source(retention_floor=floor)
        facts = datetime(2026, 1, 2, tzinfo=timezone.utc)
        assert _window_below_floor(src, self._alarm(), facts) is False

    def test_fenster_unter_dem_floor(self, make_source):
        floor = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        src = make_source(retention_floor=floor)
        facts = datetime(2026, 1, 1, 12, 30, tzinfo=timezone.utc)
        assert _window_below_floor(src, self._alarm(), facts) is True

    def test_naiver_floor_gilt_als_utc(self, make_source):
        src = make_source(retention_floor=datetime(2026, 1, 1, 12))
        facts = datetime(2026, 1, 1, 12, 30, tzinfo=timezone.utc)
        assert _window_below_floor(src, self._alarm(), facts) is True


class TestStampTransitions:
    def _alarm(self):
        return {
            "id": 7, "name": "zu wenig", "datasource_id": 3,
            "ds_name": "payments", "measure": "count(*)",
            "cond_op": "<", "threshold": 1.0, "window_s": 300,
            "context_filter": [{"col": "host", "op": "in", "vals": ["1"]}],
            "notify_channels": ["email"],
            "notify_email_recipients": ["a@example.org"],
            "notify_xmpp_recipients": ["a@jabber"],
            "severity": "critical",
            "note": "hintergrund",
        }

    def test_stempelt_identitaet_und_kanaele(self):
        transitions = [{"kind": "opened", "at": "x"}]
        stamp_transitions(transitions, self._alarm())
        t = transitions[0]
        assert t["alarm_id"] == 7
        assert t["alarm_name"] == "zu wenig"
        assert t["measure"] == "count(*)"
        assert t["severity"] == "critical"
        assert t["note"] == "hintergrund"
        assert t["notify_channels"] == ["email"]

    def test_kind_bleibt_opened_closed(self):
        transitions = [{"kind": "opened"}]
        stamp_transitions(transitions, self._alarm())
        assert transitions[0]["kind"] == "opened"

    def test_leere_empfaengerlisten(self):
        transitions = [{}]
        alarm = self._alarm()
        for key in ("notify_channels", "notify_email_recipients",
                    "notify_xmpp_recipients", "severity", "note"):
            alarm.pop(key)
        stamp_transitions(transitions, alarm)
        assert transitions[0]["notify_channels"] == []
        assert transitions[0]["notify_email_recipients"] == []
        assert transitions[0]["severity"] is None
        assert transitions[0]["note"] is None
