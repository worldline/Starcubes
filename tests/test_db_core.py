"""core/cache.py, core/preflight.py, core/adhoc.py gegen PostgreSQL."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

pytestmark = pytest.mark.db

from core.adhoc import (
    history_delete,
    history_list,
    history_touch,
    run_query,
)
from core.cache import (
    bump_cache_epoch,
    clear_cache,
    get_cache_epoch,
    get_cached_plot,
    put_cached_plot,
)
from core.preflight import relation_exists, run_preflight
from core.schema import create_fact
from core.source import FactColumn


class TestCache:
    def _key(self):
        return "k" + uuid.uuid4().hex

    def test_put_get_bump_clear(self, meta_engine, make_datasource):
        ds = make_datasource("t_cache_" + uuid.uuid4().hex[:8])
        key = self._key()
        put_cached_plot(meta_engine, cache_key=key, datasource_id=ds,
                        cache_epoch=0, group_by="host", measure=None,
                        payload={"data": [1, 2]})
        assert get_cached_plot(meta_engine, key) == {"data": [1, 2]}
        assert get_cached_plot(meta_engine, "fehlt") is None

        assert bump_cache_epoch(meta_engine, ds) == 1
        assert get_cache_epoch(meta_engine, ds) == 1

        assert clear_cache(meta_engine, ds, group_by="host") == 1
        assert get_cached_plot(meta_engine, key) is None

    def test_measure_none_wird_zu_count_star(self, meta_engine,
                                             make_datasource):
        ds = make_datasource("t_cache_" + uuid.uuid4().hex[:8])
        key = self._key()
        put_cached_plot(meta_engine, cache_key=key, datasource_id=ds,
                        cache_epoch=0, group_by="host", measure=None,
                        payload={"x": 1})
        assert clear_cache(meta_engine, ds, measure="count(*)") == 1

    def test_group_by_ohne_datasource_wird_abgelehnt(self, meta_engine):
        with pytest.raises(ValueError, match="require a datasource_id"):
            clear_cache(meta_engine, group_by="host")

    def test_max_age(self, meta_engine, make_datasource):
        ds = make_datasource("t_cache_" + uuid.uuid4().hex[:8])
        key = self._key()
        put_cached_plot(meta_engine, cache_key=key, datasource_id=ds,
                        cache_epoch=0, group_by="host", measure=None,
                        payload={"x": 1})
        assert get_cached_plot(meta_engine, key, max_age_s=3600) == {"x": 1}
        # Negatives Alter macht jeden Eintrag zu alt.
        assert get_cached_plot(meta_engine, key, max_age_s=-1) is None


class TestPreflight:
    def _src(self, make_source, make_datasource, scratch_schema, pg_dsn):
        ds = make_datasource(scratch_schema)
        return make_source(
            id=ds, schema_name=scratch_schema, dest_dsn=pg_dsn,
            source_dsn=pg_dsn, watermark_column="ts",
            partition_column="ts", partition_period="month",
            fact_columns=[
                FactColumn("ts", "timestamptz", "time", 0, "ts"),
                FactColumn("val", "integer", "measure", 1, "val"),
            ],
            dimensions=[], indices=[])

    def test_fehlende_tabelle_blockiert_und_repair_legt_an(
            self, make_source, make_datasource, scratch_schema, pg_dsn,
            meta_engine):
        src = self._src(make_source, make_datasource, scratch_schema, pg_dsn)
        eng = src.dest_engine()

        report = run_preflight(src, meta_engine, eng, source_engine=None,
                               repair=False)
        assert report["blocked"] is True
        codes = [c["code"] for c in report["checks"] if c["level"] == "block"]
        assert "schema_objects" in codes

        report = run_preflight(src, meta_engine, eng, source_engine=None,
                               repair=True)
        assert report["blocked"] is False
        assert any("fact" in r for r in report["repairs"])
        assert src.schema_name  # benutzt

    def test_relation_exists(self, make_source, make_datasource,
                             scratch_schema, pg_dsn, meta_engine):
        src = self._src(make_source, make_datasource, scratch_schema, pg_dsn)
        eng = src.dest_engine()
        with eng.connect() as conn:
            assert relation_exists(conn, src.schema_name, "fact") is False
        create_fact(src, eng)
        with eng.connect() as conn:
            assert relation_exists(conn, src.schema_name, "fact") is True


class TestAdhoc:
    def _src(self, make_source, make_datasource, scratch_schema, pg_dsn):
        ds = make_datasource(scratch_schema)
        return make_source(id=ds, schema_name=scratch_schema,
                           dest_dsn=pg_dsn, source_dsn=pg_dsn,
                           partition_column="ts", partition_period="month")

    def test_select(self, make_source, make_datasource, scratch_schema,
                    pg_dsn):
        src = self._src(make_source, make_datasource, scratch_schema, pg_dsn)
        res = run_query(src.console_engine(), "SELECT 42 AS x",
                        tag="sq:1:abc", row_limit=10)
        assert res["columns"] == ["x"]
        assert res["rows"] == [[42]]
        assert res["truncated"] is False

    def test_row_limit(self, make_source, make_datasource, scratch_schema,
                       pg_dsn):
        src = self._src(make_source, make_datasource, scratch_schema, pg_dsn)
        res = run_query(src.console_engine(),
                        "SELECT generate_series(1, 10) AS i",
                        tag="sq:1:abc", row_limit=3)
        assert len(res["rows"]) == 3
        assert res["truncated"] is True

    def test_schreiben_wird_verweigert(self, make_source, make_datasource,
                                       scratch_schema, pg_dsn):
        src = self._src(make_source, make_datasource, scratch_schema, pg_dsn)
        with pytest.raises(DBAPIError):
            run_query(src.console_engine(), "CREATE TABLE verboten (x int)",
                      tag="sq:1:abc")

    def test_history(self, make_source, make_datasource, scratch_schema,
                     pg_dsn, meta_engine):
        src = self._src(make_source, make_datasource, scratch_schema, pg_dsn)
        history_touch(meta_engine, user_id=1, datasource_id=src.id,
                      sql="SELECT 1")
        history_touch(meta_engine, user_id=1, datasource_id=src.id,
                      sql="SELECT 2")
        entries = history_list(meta_engine, user_id=1,
                               datasource_id=src.id)
        assert [e["sql"] for e in entries] == ["SELECT 2", "SELECT 1"]
        assert history_delete(meta_engine, user_id=1,
                              entry_id=entries[0]["id"]) == 1
        assert len(history_list(meta_engine, user_id=1,
                                datasource_id=src.id)) == 1


class TestConfigDb:
    def test_db_wert_und_env_vorrang(self, meta_engine, pg_dsn, monkeypatch):
        import core.config as config

        key = "STARCUBES_SMTP_HOST"
        with meta_engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO starcubes_meta.app_config (key, value, updated_by) "
                "VALUES (:k, 'aus-der-db', 'test') "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"),
                {"k": key})
        # ``pg_dsn`` and not ``str(meta_engine.url)``: SQLAlchemy masks the
        # password in that rendering, and a DSN with ``***`` cannot connect.
        monkeypatch.setenv("STARCUBES_POSTGRES_DSN", pg_dsn)
        monkeypatch.delenv(key, raising=False)
        config.invalidate()
        try:
            assert config.get(key) == "aus-der-db"
            monkeypatch.setenv(key, "aus-env")
            assert config.get(key) == "aus-env"
        finally:
            with meta_engine.begin() as conn:
                conn.execute(text(
                    "DELETE FROM starcubes_meta.app_config WHERE key = :k"),
                    {"k": key})
            config.invalidate()
