"""sources/log_views.py, sources/etl_spawn.py, sources/forever_adopt.py."""
from __future__ import annotations

import pytest
from django.test import RequestFactory

from sources.etl_spawn import slugify_ds
from sources.forever_adopt import _verdict
from sources.log_views import _BadFilter, _common_filters, _int, _ts


class TestLogFilters:
    def _req(self, **params):
        return RequestFactory().get("/sources/logs/api/lines/", params)

    def test_leer(self):
        where, params = _common_filters(
            self._req(), alias="l", time_col="ts", kinds_allowed=("etl",))
        assert where == []
        assert params == {}

    def test_datasource_int(self):
        where, params = _common_filters(
            self._req(datasource="7"), alias="l", time_col="ts",
            kinds_allowed=("etl",))
        assert where == ["l.datasource_id = :ds"]
        assert params == {"ds": 7}

    def test_datasource_unsinn(self):
        with pytest.raises(_BadFilter, match="datasource must be an integer"):
            _common_filters(self._req(datasource="x"), alias="l",
                            time_col="ts", kinds_allowed=("etl",))

    def test_kinds_whitelist(self):
        where, params = _common_filters(
            self._req(kind=["etl", "forever"]), alias="l", time_col="ts",
            kinds_allowed=("etl", "forever"))
        assert where == ["l.kind = ANY(:kinds)"]
        assert params == {"kinds": ["etl", "forever"]}

    def test_unbekannter_kind(self):
        with pytest.raises(_BadFilter, match="unknown kind"):
            _common_filters(self._req(kind=["quatsch"]), alias="l",
                            time_col="ts", kinds_allowed=("etl",))

    def test_host(self):
        where, params = _common_filters(
            self._req(host="node1"), alias="r", time_col="started_at",
            kinds_allowed=("etl",))
        assert where == ["r.hostname = :host"]
        assert params == {"host": "node1"}

    def test_zeitfenster(self):
        where, params = _common_filters(
            self._req(**{"from": "2026-01-01 00:00:00",
                         "to": "2026-01-02T00:00:00Z"}),
            alias="l", time_col="ts", kinds_allowed=("etl",))
        assert where == ["l.ts >= :from_ts", "l.ts < :to_ts"]
        assert params["from_ts"].hour == 0
        assert params["to_ts"].tzinfo is not None

    def test_unlesbare_zeit(self):
        with pytest.raises(_BadFilter, match="from is not a timestamp"):
            _common_filters(self._req(**{"from": "gestern"}), alias="l",
                            time_col="ts", kinds_allowed=("etl",))


class TestScalarHelpers:
    def test_int(self):
        assert _int("3", "x") == 3
        with pytest.raises(_BadFilter):
            _int(None, "x")

    def test_ts(self):
        assert _ts("2026-01-01T12:00:00Z", "x").year == 2026
        with pytest.raises(_BadFilter):
            _ts("x", "y")


class TestSlugifyDs:
    @pytest.mark.parametrize("name,expected", [
        ("payments", "payments"),
        ("my source", "my_source"),
        ("a/b:c", "a_b_c"),
        ("_rand_", "rand"),
        ("", "ds"),
        ("###", "ds"),
        ("with.dash-and_underscore", "with.dash-and_underscore"),
    ])
    def test_slug(self, name, expected):
        assert slugify_ds(name) == expected


class TestVerdict:
    def _row(self, **over):
        base = {"status": "running", "alive": False, "pid": None,
                "young": False, "stop_requested": False}
        base.update(over)
        return base

    def test_released_wird_adoptiert(self):
        assert _verdict(self._row(status="released"), False) == "adopt"

    def test_lebende_lease_bleibt(self):
        assert _verdict(self._row(alive=True), False) == "alive"

    def test_ohne_lease_legacy_wird_in_ruhe_gelassen(self):
        assert _verdict(self._row(alive=None), include_legacy=False) == "alive"

    def test_ohne_lease_legacy_mit_lebendigem_pid(self, monkeypatch):
        monkeypatch.setattr("sources.forever_adopt._pid_alive",
                            lambda pid: True)
        assert _verdict(self._row(alive=None, pid=123),
                        include_legacy=True) == "alive"

    def test_ohne_lease_legacy_toten_pid_wird_adoptiert(self, monkeypatch):
        monkeypatch.setattr("sources.forever_adopt._pid_alive",
                            lambda pid: False)
        assert _verdict(self._row(alive=None, pid=123),
                        include_legacy=True) == "adopt"

    def test_junge_zeile_ohne_pid_startet_noch(self):
        assert _verdict(self._row(alive=None, pid=None, young=True),
                        include_legacy=True) == "alive"

    def test_stop_request_gewinnt(self):
        assert _verdict(self._row(stop_requested=True), False) == "stop"

    def test_status_stopping_wird_geschlossen(self):
        assert _verdict(self._row(status="stopping"), False) == "stop"

    def test_tote_zeile_wird_adoptiert(self):
        assert _verdict(self._row(), False) == "adopt"
