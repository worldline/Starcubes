"""End-to-End: import_fact von PostgreSQL nach PostgreSQL.

Der wertvollste Integrationstest der Suite: er durchläuft Preflight,
Extrakt, Transform- und Ladeprozess, die Wasserzeichen-Fortschreibung und
den zweiten Lauf, der nichts mehr laden darf.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.db

from core.etl import import_fact  # noqa: E402
from core.schema import create_fact  # noqa: E402
from core.source import FactColumn  # noqa: E402


def _dt(days_ago=0, hour=12):
    # Mikrosekunden sind hier wesentlich: die Fenstergrenze ist halboffen
    # (`< end_time`), und `ceil_second` nimmt die Zeile am Datenrand genau
    # dann mit, wenn der Watermark keine ganze Sekunde ist. Ein sekunden-
    # genauer Watermark lässt die neueste Zeile bewusst bis zum nächsten
    # Lauf liegen — das wäre kein Fehler, würde diesen Test aber zu einem
    # Test der Randbedingung statt des Ladepfads machen.
    return (datetime.now(timezone.utc).replace(minute=0, second=0,
                                              microsecond=123456)
            - timedelta(days=days_ago) + timedelta(hours=hour))


@pytest.fixture
def env(make_source, scratch_schema, pg_dsn, meta_engine):
    """Quelltabelle + datasource-Zeile + Source + fact-Tabelle."""
    table = "etl_src_" + uuid.uuid4().hex[:8]
    name = "etl_test_" + uuid.uuid4().hex[:8]
    eng = None
    ds_id = None
    with meta_engine.begin() as meta:
        ds_id = meta.execute(text(
            "INSERT INTO starcubes_meta.datasource "
            "  (name, schema_name, source_dialect, source_dsn, "
            "   import_query, base_table, watermark_column, partition_column, "
            "   partition_period) "
            "VALUES (:n, :s, 'postgresql', :d, :q, :b, 'ts', 'ts', 'month') "
            "RETURNING id"),
            {"n": name, "s": scratch_schema, "d": pg_dsn,
             "q": f"SELECT ts, val, id FROM public.{table}", "b": table}).scalar()

    src = make_source(
        id=ds_id, name=name, schema_name=scratch_schema,
        source_dsn=pg_dsn, dest_dsn=pg_dsn,
        import_query=f"SELECT ts, val, id FROM public.{table}", base_table=table,
        watermark_column="ts", partition_column="ts",
        partition_period="month", chunksize=2,
        fact_columns=[
            FactColumn("ts", "timestamptz", "time", 0, "ts"),
            FactColumn("val", "integer", "measure", 1, "val"),
            FactColumn("id", "bigint", "pk", 2, "id"),
        ],
        dimensions=[], indices=[],
    )
    eng = src.dest_engine()
    with eng.begin() as conn:
        conn.execute(text(f"CREATE TABLE public.{table} (ts timestamptz, val integer, id bigint)"))
    create_fact(src, eng)

    def insert(rows):
        with eng.begin() as conn:
            conn.execute(text(
                f"INSERT INTO public.{table} (ts, val, id) VALUES (:ts, :val, :id)"),
                [{"ts": ts, "val": val, "id": 1000 + val} for ts, val in rows])

    try:
        yield src, eng, insert, meta_engine
    finally:
        with eng.begin() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS public.{table}"))
        with meta_engine.begin() as meta:
            meta.execute(text(
                "DELETE FROM starcubes_meta.etl_run WHERE datasource_id = :d"),
                {"d": ds_id})
            meta.execute(text(
                "DELETE FROM starcubes_meta.datasource WHERE id = :d"),
                {"d": ds_id})


def _count(eng, src):
    with eng.connect() as conn:
        return conn.execute(text(
            f"SELECT count(*) FROM {src.schema_name}.fact")).scalar()


class TestImportFact:
    def test_erster_lauf_laedt_alles(self, env):
        src, eng, insert, meta = env
        base = _dt(days_ago=1)
        rows = [(base + timedelta(minutes=i), i) for i in range(5)]
        insert(rows)

        loaded = import_fact(src, meta, quiet=True,
                             transform_q_size=2, load_q_size=2)
        assert loaded == 5
        assert _count(eng, src) == 5

        with meta.connect() as conn:
            run = conn.execute(text(
                "SELECT status, rows, kind, last_end_time FROM "
                "starcubes_meta.etl_run WHERE datasource_id = :d "
                " ORDER BY id DESC LIMIT 1"), {"d": src.id}).mappings().one()
        assert run["status"] == "ok"
        assert run["rows"] == 5
        assert run["kind"] == "incremental"
        assert run["last_end_time"] is not None

    def test_zweiter_lauf_laedt_nichts(self, env):
        src, eng, insert, meta = env
        insert([(_dt(days_ago=1), 1)])
        assert import_fact(src, meta, quiet=True,
                           transform_q_size=2, load_q_size=2) == 1
        assert import_fact(src, meta, quiet=True,
                           transform_q_size=2, load_q_size=2) == 0
        assert _count(eng, src) == 1

    def test_neue_zeile_wird_nachgeladen(self, env):
        src, eng, insert, meta = env
        base = _dt(days_ago=1)
        insert([(base, 1)])
        import_fact(src, meta, quiet=True, transform_q_size=2, load_q_size=2)
        insert([(base + timedelta(minutes=5), 2)])
        loaded = import_fact(src, meta, quiet=True,
                             transform_q_size=2, load_q_size=2)
        assert loaded == 1
        with eng.connect() as conn:
            vals = conn.execute(text(
                f"SELECT val FROM {src.schema_name}.fact ORDER BY val"
            )).scalars().all()
        assert vals == [1, 2]

    def test_audit_sieht_keine_luecke(self, env):
        from core.audit import audit_window

        src, eng, insert, meta = env
        start = _dt(days_ago=1)
        insert([(start + timedelta(minutes=i), i) for i in range(4)])
        import_fact(src, meta, quiet=True, transform_q_size=2, load_q_size=2)
        rows = audit_window(src, eng, start=start,
                            end=start + timedelta(minutes=10), buckets=2)
        assert rows
        assert sum(int(r.get("missing") or 0) for r in rows) == 0

    def test_backfill_dupliziert_nicht(self, env):
        src, eng, insert, meta = env
        base = _dt(days_ago=2)
        rows = [(base + timedelta(minutes=i), i) for i in range(3)]
        insert(rows)
        import_fact(src, meta, quiet=True, transform_q_size=2, load_q_size=2)
        # Dasselbe geschlossene Fenster noch einmal.
        loaded = import_fact(
            src, meta, quiet=True, transform_q_size=2, load_q_size=2,
            start_time=base, end_time_override=base + timedelta(minutes=10),
            backfill=True)
        assert loaded == 0
        assert _count(eng, src) == 3
