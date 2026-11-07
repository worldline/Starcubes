"""viz/views.py — Filterparser und Fehlerübersetzung (ohne DB)."""
from __future__ import annotations

import pytest
from django.http import QueryDict
from sqlalchemy.exc import OperationalError

from viz.views import (
    _friendly_db_error,
    _missing_relation_note,
    _parse_filter_spec,
    _parse_filter_tree,
    _query_timeout_ms,
)


class TestParseFilterSpec:
    @pytest.mark.parametrize("spec,expected", [
        ("host=1,2", ("host", "in", ["1", "2"])),
        ("host!=1", ("host", "not_in", ["1"])),
        ("path~=/api", ("path", "starts_with", ["/api"])),
        ("path!~=/api", ("path", "starts_not_with", ["/api"])),
        ("path*=/api", ("path", "contains", ["/api"])),
        ("path!*=/api", ("path", "not_contains", ["/api"])),
    ])
    def test_operatoren(self, spec, expected):
        assert _parse_filter_spec(spec) == expected

    def test_erster_trenner_gewinnt(self):
        # Ein Wert, der selbst "*=" enthält, darf den Split nicht bestimmen.
        assert _parse_filter_spec("name=a*=b") == ("name", "in", ["a*=b"])

    def test_gleichzeitig_mehrere_trenner(self):
        assert _parse_filter_spec("a!~=x=y") == ("a", "starts_not_with", ["x=y"])

    def test_leere_werte_werden_verworfen(self):
        assert _parse_filter_spec("host=1,,2") == ("host", "in", ["1", "2"])

    def test_nur_leere_werte_ist_ungueltig(self):
        assert _parse_filter_spec("host=,,") is None

    def test_kein_trenner(self):
        assert _parse_filter_spec("host") is None


class TestParseFilterTree:
    def test_leer(self):
        assert _parse_filter_tree(QueryDict("")) == []

    def test_flach(self):
        q = QueryDict("filter=host=1&filter=path=2")
        tree = _parse_filter_tree(q)
        assert [n["row"]["col"] for n in tree] == ["host", "path"]
        assert [n["andOr"] for n in tree] == ["and", "and"]

    def test_verschachtelt_ueber_indent(self):
        q = QueryDict("filter=host=1&filter=path=2&indent=0&indent=1")
        tree = _parse_filter_tree(q)
        assert len(tree) == 1
        assert tree[0]["row"]["col"] == "host"
        assert tree[0]["children"][0]["row"]["col"] == "path"

    def test_andor_gehoert_zur_folgezeile(self):
        # Der Client sendet ein andor je Trenner; die n-te Zeile liest
        # andor_list[n-1]. Die erste Zeile behält daher "and".
        q = QueryDict("filter=host=1&filter=path=2&andor=or")
        tree = _parse_filter_tree(q)
        assert tree[0]["andOr"] == "and"
        assert tree[1]["andOr"] == "or"

    def test_ungueltige_specs_werden_uebersprungen(self):
        q = QueryDict("filter=host&filter=path=2")
        tree = _parse_filter_tree(q)
        assert [n["row"]["col"] for n in tree] == ["path"]


class TestQueryTimeout:
    def test_default(self, monkeypatch):
        from django.conf import settings
        monkeypatch.setattr(settings, "STARCUBES_QUERY_TIMEOUT_MS", 30_000)
        assert _query_timeout_ms() == 30_000

    def test_deckel_bei_90s(self, monkeypatch):
        from django.conf import settings
        monkeypatch.setattr(settings, "STARCUBES_QUERY_TIMEOUT_MS", 120_000)
        assert _query_timeout_ms() == 90_000


class _Orig(Exception):
    def __init__(self, msg, pgcode=None):
        super().__init__(msg)
        self.pgcode = pgcode


class TestFriendlyDbError:
    def test_abgebrochen(self):
        msg, cancelled = _friendly_db_error(
            Exception(), )
        # Ohne .orig wird der Fehler selbst als Text genommen.
        assert cancelled is False
        assert isinstance(msg, str)

    def test_57014(self):
        exc = Exception("x")
        exc.orig = _Orig("cancelled", "57014")
        assert _friendly_db_error(exc) == ("Query cancelled or timed out.", True)

    def test_25006(self):
        exc = Exception("x")
        exc.orig = _Orig("read only", "25006")
        assert _friendly_db_error(exc) == ("This console is read-only.", False)

    def test_chart_42P01(self):
        exc = Exception("x")
        exc.orig = _Orig('relation "sc.fact" does not exist', "42P01")
        msg, cancelled = _friendly_db_error(exc, chart=True)
        assert "missing in schema sc: fact" in msg
        assert "run Create Schema" in msg
        assert cancelled is False

    def test_chart_unerreichbar(self):
        exc = OperationalError("SELECT 1", {}, Exception("conn refused"))
        msg, _ = _friendly_db_error(exc, chart=True)
        assert "not reachable" in msg

    def test_chart_lookup(self):
        msg, _ = _friendly_db_error(LookupError("datasource 'x' not found"),
                                    chart=True)
        assert "not found" in msg

    def test_chart_unbekannt_ist_none(self):
        msg, _ = _friendly_db_error(ValueError("kaputt"), chart=True)
        assert msg is None

    def test_console_gibt_originaltext(self):
        exc = Exception("x")
        exc.orig = _Orig("syntax error at or near")
        assert _friendly_db_error(exc) == ("syntax error at or near", False)


class TestMissingRelationNote:
    def test_englische_anfuehrungszeichen(self):
        note = _missing_relation_note(
            Exception('relation "sc.fact" does not exist'))
        assert "missing in schema sc: fact" in note

    def test_deutsche_anfuehrungszeichen(self):
        note = _missing_relation_note(
            Exception("Relation »sc.fact« existiert nicht"))
        assert "missing in schema sc: fact" in note

    def test_ohne_namen(self):
        note = _missing_relation_note(Exception("irgendwas"))
        assert "missing relation in the warehouse" in note
