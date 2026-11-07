"""Django-View- und API-Tests ohne Browser (``pytest.mark.db``).

Deckt die Stellen ab, die eine reine Unit-Test-Ebene nicht sieht:
Rollen, CSRF, Statuscodes, JSON-Verträge, Template-Sichtbarkeit und die
Datenbankwege der Chart- und Datasource-Endpunkte.
"""
from __future__ import annotations

import json
from datetime import timedelta

import pytest
from django.test import Client
from sqlalchemy import text

pytestmark = pytest.mark.db


def _iso(dt):
    return dt.isoformat()


def _window(handle, minutes=1):
    return {
        "name": handle.name,
        "from": _iso(handle.anchor),
        "to": _iso(handle.anchor + timedelta(minutes=minutes)),
    }


# ---------------------------------------------------------------------------
# Öffentliche Endpunkte / Grundgerüst
# ---------------------------------------------------------------------------

class TestPublicSurface:
    def test_chart_seite(self, django_client):
        resp = django_client.get("/")
        assert resp.status_code == 200
        assert b"timeseries" in resp.content

    def test_sources_anonym_umgeleitet(self, django_client):
        resp = django_client.get("/sources/")
        assert resp.status_code == 302
        assert "/login/" in resp["Location"]

    def test_admin_anonym_umgeleitet(self, django_client):
        resp = django_client.get("/admin/")
        assert resp.status_code == 302

    def test_plot_json_ist_oeffentlich(self, django_client, seeded):
        resp = django_client.get("/plot.json", {
            **_window(seeded), "group_by": "host", "interval": 60})
        assert resp.status_code == 200
        plot = resp.json()
        assert plot["interval"] == 60
        slot = plot["data"]["0"]
        assert slot["_1"] == 3
        assert slot["_2"] == 3

    def test_plot_json_mit_summe(self, django_client, seeded):
        resp = django_client.get("/plot.json", {
            **_window(seeded), "group_by": "host", "interval": 60,
            "measure": "sum(val)"})
        assert resp.status_code == 200
        slot = resp.json()["data"]["0"]
        assert slot["_1"] == 80
        assert slot["_2"] == 130

    def test_plot_json_ohne_name_400(self, django_client):
        assert django_client.get("/plot.json").status_code == 400

    def test_api_sources(self, django_client, seeded):
        resp = django_client.get("/api/sources")
        assert resp.status_code == 200
        names = {s["name"] for s in resp.json()["sources"]}
        assert seeded.name in names

    def test_api_source_meta(self, django_client, seeded):
        resp = django_client.get("/api/source-meta", {"name": seeded.name})
        assert resp.status_code == 200
        meta = resp.json()
        assert meta["id"] == seeded.id
        assert meta["not_ready"] is None
        assert {c["name"] for c in meta["fact_columns"]} == {
            "ts", "host", "path", "val", "id"}
        assert {m["name"] for m in meta["measures"]} == {"Anzahl", "Summe"}
        assert {"1", "2"} <= {v[0] for v in meta["dimensions"]["dim_host"]}

    def test_drill_json(self, django_client, seeded):
        resp = django_client.get("/drill.json", {
            **_window(seeded), "group_by": "host", "group_by_val": "1"})
        assert resp.status_code == 200
        dims = {d["column"]: d for d in resp.json()["dimensions"]}
        values = {v["source_id"]: v["count"]
                  for v in dims["path"]["values"]}
        assert values == {"10": 2, "11": 1}

    def test_txn_json(self, django_client, seeded):
        resp = django_client.get("/txn.json", {
            **_window(seeded), "group_by": "host", "group_by_val": "1"})
        assert resp.status_code == 200
        assert resp.json()["transaction_ids"] == ["100", "101", "104"]

    def test_gaps_json_liefert_die_huelle(self, django_client, seeded):
        resp = django_client.get("/gaps.json", _window(seeded))
        assert resp.status_code == 200
        body = resp.json()
        assert body["gaps"] == []
        assert body["candidate_gaps"] == 0
        assert "profile" in body

    def test_deck_asset_liefert_bild(self, django_client):
        resp = django_client.get("/docs/img/chart-hero.png")
        assert resp.status_code == 200
        assert resp["Content-Type"] == "image/png"

    def test_deck_asset_verweigert_traversal(self, django_client):
        resp = django_client.get("/presentation/../README.md")
        assert resp.status_code == 404

    def test_deck_asset_unbekannt(self, django_client):
        assert django_client.get("/docs/gibt-es-nicht.md").status_code == 404


# ---------------------------------------------------------------------------
# Login-Gates und Rollen
# ---------------------------------------------------------------------------

class TestAuth:
    def test_query_run_anonym_403_json(self, django_client):
        resp = django_client.post("/query/run", {
            "sql": "SELECT 1", "token": "abcdef12"})
        assert resp.status_code == 403
        assert resp.json() == {"ok": False, "error": "login required"}

    def test_chart_sql_anonym_403(self, django_client):
        resp = django_client.get("/chart/sql", {"name": "x"})
        assert resp.status_code == 403

    def test_query_run_angemeldet(self, django_client, make_user, seeded):
        user = make_user()
        django_client.force_login(user)
        resp = django_client.post("/query/run", {
            "sql": "SELECT 42 AS x", "token": "abcdef12",
            "name": seeded.name})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["rows"] == [[42]]

    def test_query_run_schreibversuch_wird_abgelehnt(self, django_client,
                                                     make_user, seeded):
        django_client.force_login(make_user())
        resp = django_client.post("/query/run", {
            "sql": "INSERT INTO t VALUES (1)", "token": "abcdef12",
            "name": seeded.name})
        assert resp.status_code == 200
        assert resp.json()["ok"] is False

    def test_manage_ist_admin_only(self, django_client, make_user, seeded):
        normal = make_user()
        django_client.force_login(normal)
        assert django_client.get(
            f"/sources/{seeded.id}/manage/").status_code == 403

        django_client.force_login(make_user(superuser=True))
        assert django_client.get(
            f"/sources/{seeded.id}/manage/").status_code == 200

    def test_detail_zeigt_admin_panels_nur_dem_superuser(
            self, django_client, make_user, seeded):
        django_client.force_login(make_user())
        normal = django_client.get(f"/sources/{seeded.id}/")
        assert normal.status_code == 200
        assert b'id="delete-form"' not in normal.content
        assert b"Fact Columns (" not in normal.content
        assert b"Alarms (" in normal.content

        django_client.force_login(make_user(superuser=True))
        admin = django_client.get(f"/sources/{seeded.id}/")
        assert admin.status_code == 200
        assert b'id="delete-form"' in admin.content
        assert b"Fact Columns (" in admin.content


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------

class TestCsrf:
    def _token(self, client):
        client.get("/sources/")
        return client.cookies["csrftoken"].value

    def test_post_ohne_token_403(self, make_user):
        client = Client(enforce_csrf_checks=True)
        client.force_login(make_user())
        resp = client.post("/query/run", {
            "sql": "SELECT 1", "token": "abcdef12"})
        assert resp.status_code == 403

    def test_post_mit_token(self, make_user, seeded):
        client = Client(enforce_csrf_checks=True)
        client.force_login(make_user())
        token = self._token(client)
        resp = client.post(
            "/query/run", {"sql": "SELECT 1", "token": "abcdef12",
                           "name": seeded.name},
            HTTP_X_CSRFTOKEN=token)
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


# ---------------------------------------------------------------------------
# Alarm-CRUD und Dimensionen
# ---------------------------------------------------------------------------

class TestAlarms:
    def _add(self, client, seeded, **over):
        data = {
            "name": "zu wenig",
            "measure": "count(*)",
            "cond_op": "<",
            "threshold": "1",
            "window_s": "300",
        }
        data.update(over)
        return client.post(f"/sources/{seeded.id}/alarms/add/", data)

    def test_anlegen_listen_bearbeiten_loeschen(self, django_client,
                                                make_user, seeded):
        django_client.force_login(make_user())
        resp = self._add(django_client, seeded)
        assert resp.status_code == 200
        alarm = resp.json()["alarm"]
        alarm_id = alarm["id"]
        assert alarm["name"] == "zu wenig"

        listed = django_client.post(f"/sources/{seeded.id}/alarms/").json()
        assert [a["name"] for a in listed["alarms"]] == ["zu wenig"]

        edited = django_client.post(
            f"/sources/{seeded.id}/alarms/{alarm_id}/edit/",
            {"name": "noch weniger", "measure": "count(*)", "cond_op": "<",
             "threshold": "2", "window_s": "60"})
        assert edited.status_code == 200
        assert edited.json()["alarm"]["name"] == "noch weniger"

        gone = django_client.post(
            f"/sources/{seeded.id}/alarms/{alarm_id}/delete/")
        assert gone.status_code == 200
        assert django_client.post(
            f"/sources/{seeded.id}/alarms/").json()["alarms"] == []

    def test_gruppierter_kontext_wird_abgelehnt(self, django_client,
                                               make_user, seeded):
        django_client.force_login(make_user())
        ctx = json.dumps([{"col": "host:grouped:typen", "op": "in",
                           "vals": ["a"]}])
        resp = self._add(django_client, seeded, context=ctx)
        assert resp.status_code == 400
        assert "grouped" in resp.json()["error"]

    def test_unbekannte_measure_wird_abgelehnt(self, django_client,
                                               make_user, seeded):
        django_client.force_login(make_user())
        resp = self._add(django_client, seeded, measure="sum(fehlt)")
        assert resp.status_code == 400


class TestDimensions:
    def test_dim_values_ohne_sentinel(self, django_client, make_user, seeded,
                                      meta_engine):
        django_client.force_login(make_user())
        with meta_engine.connect() as conn:
            dim_id = conn.execute(text(
                "SELECT id FROM starcubes_meta.dimension "
                " WHERE datasource_id = :d AND fact_column = 'host'"),
                {"d": seeded.id}).scalar()
        resp = django_client.get(
            f"/sources/{seeded.id}/dimensions/{dim_id}/values/")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        ids = {v["source_id"] for v in body["values"]}
        assert {"1", "2"} <= ids
        assert "__unknown__" not in ids


# ---------------------------------------------------------------------------
# Admin-only Log- und Settings-Seiten
# ---------------------------------------------------------------------------

class TestAdminPages:
    def test_logs_normal_403_superuser_200(self, django_client, make_user):
        django_client.force_login(make_user())
        assert django_client.get("/sources/logs/").status_code == 403
        assert django_client.get(
            "/sources/logs/api/lines/").status_code == 403

        django_client.force_login(make_user(superuser=True))
        assert django_client.get("/sources/logs/").status_code == 200
        resp = django_client.get("/sources/logs/api/lines/", {"limit": 5})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_settings_normal_403_superuser_200(self, django_client, make_user):
        django_client.force_login(make_user())
        assert django_client.get("/sources/settings/").status_code == 403
        django_client.force_login(make_user(superuser=True))
        assert django_client.get("/sources/settings/").status_code == 200
