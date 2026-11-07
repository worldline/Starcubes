"""core/audit.py gegen PostgreSQL — Zähl- und PK-Vergleich."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.db

from core.audit import audit_ids, audit_window  # noqa: E402
from core.schema import create_fact  # noqa: E402
from core.source import FactColumn  # noqa: E402


def _dt(minutes=0):
    return (datetime.now(timezone.utc).replace(second=0, microsecond=0)
            - timedelta(hours=2) + timedelta(minutes=minutes))


@pytest.fixture
def env(make_source, make_datasource, scratch_schema, pg_dsn):
    table = "audit_src_" + uuid.uuid4().hex[:8]
    ds_id = make_datasource(
        scratch_schema, source_dsn=pg_dsn,
        import_query=f"SELECT ts, val, id FROM public.{table}",
        base_table=f"public.{table}")
    src = make_source(
        id=ds_id, name="audit_test", schema_name=scratch_schema,
        source_dsn=pg_dsn, dest_dsn=pg_dsn,
        import_query=f"SELECT ts, val, id FROM public.{table}",
        base_table=f"public.{table}",
        watermark_column="ts", partition_column="ts",
        partition_period="month",
        fact_columns=[
            FactColumn("ts", "timestamptz", "time", 0, "ts"),
            FactColumn("val", "integer", "measure", 1, "val"),
            FactColumn("id", "bigint", "pk", 2, "id"),
        ],
        dimensions=[], indices=[])
    eng = src.dest_engine()
    with eng.begin() as conn:
        conn.execute(text(
            f"CREATE TABLE public.{table} "
            f"(ts timestamptz, val integer, id bigint)"))
    create_fact(src, eng)

    def source_rows(ids):
        with eng.begin() as conn:
            conn.execute(text(
                f"INSERT INTO public.{table} (ts, val, id) "
                f"VALUES (:ts, :val, :id)"),
                [{"ts": _dt(i), "val": i, "id": pk}
                 for i, pk in enumerate(ids)])

    def fact_rows(ids):
        with eng.begin() as conn:
            conn.execute(text(
                f"INSERT INTO {scratch_schema}.fact (ts, val, id) "
                f"VALUES (:ts, :val, :id)"),
                [{"ts": _dt(i), "val": i, "id": pk}
                 for i, pk in enumerate(ids)])

    try:
        yield src, eng, source_rows, fact_rows
    finally:
        with eng.begin() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS public.{table}"))


class TestAuditWindow:
    def test_zaehlt_fehlende_zeilen(self, env):
        src, eng, source_rows, fact_rows = env
        source_rows([1001, 1002, 1003, 1004, 1005])
        fact_rows([1001, 1002, 1004, 1005])          # 1003 fehlt
        rows = audit_window(src, eng, start=_dt(0), end=_dt(10), buckets=1)
        assert len(rows) == 1
        assert rows[0]["source_rows"] == 5
        assert rows[0]["fact_rows"] == 4
        assert rows[0]["missing"] == 1
        assert rows[0]["surplus"] == 0

    def test_mehrere_buckets(self, env):
        src, eng, source_rows, fact_rows = env
        source_rows([1, 2, 3, 4])
        fact_rows([1, 2])
        rows = audit_window(src, eng, start=_dt(0), end=_dt(10), buckets=2)
        assert len(rows) == 2
        assert sum(r["missing"] or 0 for r in rows) == 2


class TestAuditIds:
    def test_nennt_die_fehlende_id(self, env):
        src, eng, source_rows, fact_rows = env
        source_rows([1001, 1002, 1003, 1004, 1005])
        fact_rows([1001, 1002, 1004, 1005])
        res = audit_ids(src, eng, start=_dt(0), end=_dt(10))
        assert res["missing_count"] == 1
        assert res["missing_ids"] == ["1003"]
        assert res["surplus_count"] == 0
        assert res["duplicate_rows"] == 0

    def test_erkennt_duplikate(self, env):
        src, eng, source_rows, fact_rows = env
        source_rows([1001, 1002, 1003])
        fact_rows([1001, 1001, 1002, 1003])
        res = audit_ids(src, eng, start=_dt(0), end=_dt(10))
        assert res["duplicate_rows"] == 1
        assert res["fact_distinct"] == 3
        assert res["missing_count"] == 0

    def test_full_ids_liefert_die_ganze_liste(self, env):
        src, eng, source_rows, fact_rows = env
        source_rows([1, 2, 3, 4, 5])
        fact_rows([1, 2])
        res = audit_ids(src, eng, start=_dt(0), end=_dt(10),
                        sample=2, full_ids=10)
        assert res["missing_all"] == ["3", "4", "5"]
        assert res["missing_all_truncated"] is False
        # Die Anzeige bleibt beim Sample.
        assert res["missing_ids"] == ["3", "4"]
        assert res["truncated"] is True
