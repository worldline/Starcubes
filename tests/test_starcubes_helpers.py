"""starcubes/middleware.py, starcubes/storage.py, starcubes/version.py."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from django.http import HttpResponse
from django.test import RequestFactory

from starcubes.middleware import PipelineAlarmMiddleware, RequireLoginMiddleware
from starcubes.storage import _without_source_maps
import starcubes.version as version


class TestRequireLogin:
    def _mw(self):
        return RequireLoginMiddleware(lambda req: HttpResponse("ok"))

    def _req(self, path, authenticated=None):
        req = RequestFactory().get(path)
        if authenticated is not None:
            req.user = SimpleNamespace(is_authenticated=authenticated)
        return req

    @pytest.mark.parametrize("path", ["/login/", "/logout/", "/", "/help/",
                                      "/sourcesfoo"])
    def test_oeffentliche_pfade(self, path):
        assert self._mw()(self._req(path, False)).status_code == 200

    def test_geschuetzter_pfad_anonym(self):
        resp = self._mw()(self._req("/sources/1/", False))
        assert resp.status_code == 302
        assert resp["Location"].startswith("/login/?next=")

    def test_geschuetzter_pfad_angemeldet(self):
        assert self._mw()(self._req("/sources/1/", True)).status_code == 200

    def test_admin_anonym(self):
        assert self._mw()(self._req("/admin/", False)).status_code == 302

    def test_viz_bleibt_offen(self):
        assert self._mw()(self._req("/plot.json", False)).status_code == 200


class TestPipelineAlarmMiddleware:
    def test_fehler_beim_scheduling_faerbt_die_antwort_nicht(self, monkeypatch):
        mw = PipelineAlarmMiddleware(lambda req: HttpResponse("ok"))

        def boom():
            raise RuntimeError("kaputt")

        monkeypatch.setattr(mw, "_maybe_schedule", boom)
        resp = mw(RequestFactory().get("/"))
        assert resp.status_code == 200
        assert resp.content == b"ok"

    def test_throttle_startet_nur_einen_thread(self, monkeypatch):
        started = []

        class FakeThread:
            def __init__(self, **kw):
                started.append(kw)

            def start(self):
                pass

        import threading
        monkeypatch.setattr(threading, "Thread", FakeThread)
        import core.pipeline_alarm as pa
        monkeypatch.setattr(pa, "EVAL_INTERVAL_S", 1000.0)

        mw = PipelineAlarmMiddleware(lambda req: HttpResponse("ok"))
        req = RequestFactory().get("/")
        mw(req)
        mw(req)
        assert len(started) == 1
        assert started[0]["daemon"] is True


class TestStorage:
    def test_source_map_entfernt(self):
        patterns = [(
            ".js",
            (("sourceMappingURL", r"sourceMappingURL=.*"),
             ("url", r"url\(.*\)")),
        )]
        out = _without_source_maps(patterns)
        assert out == ((".js", (("url", r"url\(.*\)"),)),)

    def test_string_eintraege(self):
        patterns = [(".css", ("sourceMappingURL", "url"))]
        assert _without_source_maps(patterns) == ((".css", ("url",)),)

    def test_leere_klasse_faellt_weg(self):
        assert _without_source_maps([("x", (("sourceMappingURL", "a"),))]) == ()

    def test_normale_klasse_bleibt(self):
        assert _without_source_maps([("x", (("url", "a"),))]) == \
            (("x", (("url", "a"),)),)


class TestVersion:
    def test_substituted(self):
        assert version._substituted("  $Format:%h$  ") == ""
        assert version._substituted("  v1.2.3  ") == "v1.2.3"

    def test_archive_auf_dem_tag(self, monkeypatch):
        monkeypatch.setattr(version, "_ARCHIVE_DESCRIBE", "v2.0.0")
        assert version._version_from_archive() == "2.0.0"

    def test_archive_hinter_dem_tag(self, monkeypatch):
        monkeypatch.setattr(version, "_ARCHIVE_DESCRIBE",
                            "v2.0.0-alpha.2-3-gacacfe3")
        assert version._version_from_archive() == \
            "2.0.0-alpha.2+3.gacacfe3"

    def test_archive_unsubstituiert(self, monkeypatch):
        monkeypatch.setattr(version, "_ARCHIVE_DESCRIBE", "$Format:%h$")
        assert version._version_from_archive() == ""

    @pytest.mark.parametrize("value,expected", [
        ("2.0.0-alpha.1", True),
        ("2.0.0-rc.1", True),
        ("2.0.0", False),
        ("2.0.0+3.gabc", False),
        ("unknown", False),
    ])
    def test_is_prerelease(self, value, expected):
        assert version._is_prerelease(value) is expected

    def test_build_ref_env(self, monkeypatch):
        monkeypatch.setenv("STARCUBES_BUILD_REF", "abc123")
        assert version._build_ref() == "abc123"

    def test_version_env_gewinnt(self, monkeypatch):
        monkeypatch.setenv("STARCUBES_VERSION", "9.9.9-test")
        assert version._version() == "9.9.9-test"

    def test_build_info_keys(self, monkeypatch):
        monkeypatch.setenv("STARCUBES_VERSION", "1.2.3")
        monkeypatch.setenv("STARCUBES_BUILD_DATE", "2026-01-01")
        info = version.build_info()
        assert info["version"] == "1.2.3"
        assert info["is_prerelease"] is False
        assert info["build_date"] == "2026-01-01"
        assert "timeseries_version" in info


class TestDbFromDsn:
    def test_netloc_form(self):
        from starcubes.settings import _db_from_dsn
        db = _db_from_dsn("postgresql+psycopg2://anna:geheim@db.example:5433/sc")
        assert db["NAME"] == "sc"
        assert db["USER"] == "anna"
        assert db["PASSWORD"] == "geheim"
        assert db["HOST"] == "db.example"
        assert db["PORT"] == "5433"

    def test_socket_im_query_string(self):
        from starcubes.settings import _db_from_dsn
        db = _db_from_dsn(
            "postgresql+psycopg2://holger@/postgres?host=/tmp/sc_pg_sock&port=55432")
        assert db["NAME"] == "postgres"
        assert db["USER"] == "holger"
        assert db["HOST"] == "/tmp/sc_pg_sock"
        assert db["PORT"] == "55432"

    def test_leerer_dsn_ist_dummy(self):
        from starcubes.settings import _db_from_dsn
        assert _db_from_dsn("")["ENGINE"] == "django.db.backends.dummy"
