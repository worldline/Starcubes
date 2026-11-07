"""Seed-Daten für View-, API- und Browser-Tests.

Eine vollständige Datasource entsteht in einem eigenen Schema: Meta-Zeilen
(datasource, fact_column, dimension, fact_index, measure), die physischen
Tabellen und ein paar Faktenzeilen. Das ist genau der Zustand, den
``Source.load`` und die Chart-/API-Views erwarten; die GUI-Wege (Anlegen per
Formular, ETL) werden dadurch nicht vorweggenommen, sondern nur die
Ausgangslage bereitgestellt.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, text

from core.schema import create_fact
from core.source import Source

UNKNOWN = "__unknown__"


@dataclass(frozen=True)
class Seeded:
    """Handgriff auf eine geseedete Datasource."""

    id: int
    name: str
    schema: str
    anchor: datetime
    source_table: str
    source: Source


def _exec(engine, sql, params=None):
    with engine.begin() as conn:
        return conn.execute(text(sql), params or {})


def seed_datasource(meta_engine, pg_dsn: str, schema: str, *,
                    name: str | None = None) -> Seeded:
    """Legt eine Datasource samt Tabellen und Zeilen an.

    Der Zeitanker liegt eine Stunde zurück und ist auf die Minute gerundet,
    damit die Tests ein festes, aber von „jetzt" abhängiges Fenster haben,
    das sicher in der aktuellen Monatspartition liegt.
    """
    name = name or "ui_" + uuid.uuid4().hex[:10]
    source_table = "src_" + uuid.uuid4().hex[:10]
    anchor = (datetime.now(timezone.utc).replace(second=0, microsecond=0)
              - timedelta(hours=1))
    engine = create_engine(pg_dsn, future=True)
    _exec(engine, f"CREATE SCHEMA IF NOT EXISTS {schema}")
    _exec(engine, f"CREATE TABLE IF NOT EXISTS public.{source_table} "
                  f"(ts timestamptz, host smallint, path smallint, "
                  f" val integer, id bigint)")

    ds_id = _exec(engine, """
        INSERT INTO starcubes_meta.datasource
          (name, schema_name, source_dialect, source_dsn, import_query,
           base_table, watermark_column, partition_column, partition_period,
           chunksize, transform_procs, interval_ladder, bar_pixel_target)
        VALUES (:n, :s, 'postgresql', :d,
                :q, :b, 'ts', 'ts', 'month', 1000, 1,
                ARRAY[1,60,3600,86400], 5)
        RETURNING id
    """, {"n": name, "s": schema, "d": pg_dsn,
          "q": f"SELECT ts, host, path, val, id FROM public.{source_table}",
          "b": source_table}).scalar()

    facts = [
        ("ts", "timestamptz", "time", 0, "ts"),
        ("host", "smallint", "dimension", 1, "host"),
        ("path", "smallint", "auto-dimension", 2, "path"),
        ("val", "integer", "measure", 3, "val"),
        ("id", "bigint", "pk", 4, "id"),
    ]
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO starcubes_meta.fact_column "
            "  (datasource_id, name, pg_type, role, ordinal, source_expr) "
            "VALUES (:d, :n, :t, :r, :o, :e)"),
            [{"d": ds_id, "n": n, "t": t, "r": r, "o": o, "e": e}
             for n, t, r, o, e in facts])
        conn.execute(text(
            "INSERT INTO starcubes_meta.dimension "
            "  (datasource_id, fact_column, dim_table, source_table, "
            "   source_key, source_name, source_description, is_primary) "
            "VALUES (:d, 'host', 'dim_host', NULL, NULL, NULL, NULL, true), "
            "       (:d, 'path', 'dim_path', NULL, NULL, NULL, NULL, true)"),
            {"d": ds_id})
        conn.execute(text(
            "INSERT INTO starcubes_meta.fact_index "
            "  (datasource_id, column_name, method) "
            "VALUES (:d, 'ts', 'brin')"), {"d": ds_id})
        conn.execute(text(
            "INSERT INTO starcubes_meta.measure "
            "  (datasource_id, name, agg, graph_style, is_default, ordinal) "
            "VALUES (:d, 'Anzahl', 'count(*)', 'multibar', true, 1), "
            "       (:d, 'Summe', 'sum(val)', 'multibar', false, 2)"),
            {"d": ds_id})

    src = Source.load(meta_engine, pg_dsn, name)
    create_fact(src, engine)

    with engine.begin() as conn:
        conn.execute(text(
            f"CREATE TABLE {schema}.dim_host ("
            f" index smallint PRIMARY KEY, source_id text, name text, "
            f" description text, count bigint NOT NULL DEFAULT 0)"))
        conn.execute(text(
            f"CREATE TABLE {schema}.dim_path ("
            f" index smallint PRIMARY KEY, source_id text, name text, "
            f" description text, count bigint NOT NULL DEFAULT 0)"))
        conn.execute(text(
            f"INSERT INTO {schema}.dim_host VALUES "
            f"(-1, '-1', 'NULL', '', 0), (0, :unk, 'unknown', '', 0), "
            f"(1, '1', 'eins', '', 0), (2, '2', 'zwei', '', 0)"),
            {"unk": UNKNOWN})
        conn.execute(text(
            f"INSERT INTO {schema}.dim_path VALUES "
            f"(-1, '-1', 'NULL', '', 0), (0, :unk, 'unknown', '', 0), "
            f"(10, '10', 'p10', '', 0), (11, '11', 'p11', '', 0)"),
            {"unk": UNKNOWN})

    rows = [
        (anchor + timedelta(seconds=5), 1, 10, 10, 100),
        (anchor + timedelta(seconds=10), 1, 11, 20, 101),
        (anchor + timedelta(seconds=20), 2, 10, 30, 102),
        (anchor + timedelta(seconds=25), 2, 11, 40, 103),
        (anchor + timedelta(seconds=30), 1, 10, 50, 104),
        (anchor + timedelta(seconds=35), 2, 11, 60, 105),
    ]
    with engine.begin() as conn:
        conn.execute(text(
            f"INSERT INTO {schema}.fact (ts, host, path, val, id) "
            f"VALUES (:ts, :host, :path, :val, :id)"),
            [{"ts": ts, "host": h, "path": p, "val": v, "id": i}
             for ts, h, p, v, i in rows])
        conn.execute(text(
            f"INSERT INTO public.{source_table} (ts, host, path, val, id) "
            f"VALUES (:ts, :host, :path, :val, :id)"),
            [{"ts": ts, "host": h, "path": p, "val": v, "id": i}
             for ts, h, p, v, i in rows])

    engine.dispose()
    return Seeded(ds_id, name, schema, anchor, source_table, src)


def drop_seeded(meta_engine, handle: Seeded) -> None:
    """Entfernt Meta-Zeile, Schema und Quelltabelle."""
    engine = create_engine(
        meta_engine.url.render_as_string(hide_password=False), future=True)
    try:
        _exec(engine, "DELETE FROM starcubes_meta.datasource WHERE id = :id",
              {"id": handle.id})
        _exec(engine, f"DROP SCHEMA IF EXISTS {handle.schema} CASCADE")
        _exec(engine, f"DROP TABLE IF EXISTS public.{handle.source_table}")
    finally:
        engine.dispose()


def create_user(username: str | None = None, password: str = "test-pass-123",
                *, superuser: bool = False):
    """Legt einen Login-Benutzer über das Django-ORM an."""
    from django.contrib.auth import get_user_model

    User = get_user_model()
    username = username or "user_" + uuid.uuid4().hex[:10]
    user = User.objects.create_user(
        username=username, password=password,
        email=f"{username}@example.org")
    if superuser:
        user.is_superuser = True
        user.is_staff = True
        user.save(update_fields=["is_superuser", "is_staff"])
    return user
