"""Gemeinsame Fixtures für die starcubes-Testsuite.

Zwei Testklassen:

* **Ohne Datenbank** laufen immer. ``conftest`` importiert Django, setzt aber
  ``STARCUBES_POSTGRES_DSN`` bewusst auf einen leeren String, damit
  ``SourcesConfig.ready()`` beim Testlauf niemals die produktive Datenbank
  anfasst.
* **Mit Datenbank** brauchen ``STARCUBES_TEST_DSN`` — einen *separaten*
  PostgreSQL-DSN. Ist er nicht gesetzt oder nicht erreichbar, werden diese
  Tests übersprungen (``pytest.mark.db``), nicht rot. Bei gesetztem DSN
  zeigen auch die Django-Settings auf diese Testdatenbank (nie auf die
  produktive ``STARCUBES_POSTGRES_DSN``), damit View- und UI-Tests dieselbe
  Datenbasis benutzen.

Ein lokaler Wegwerf-Cluster für die DB-Tests::

    initdb -D /tmp/sc_pg -U "$USER" --auth=trust
    pg_ctl -D /tmp/sc_pg -o "-k /tmp/sc_pg_sock -p 55432" -l /tmp/sc_pg.log start
    STARCUBES_TEST_DSN='postgresql+psycopg2://'"$USER"'@/postgres?host=/tmp/sc_pg_sock&port=55432' \\
        python -m pytest
"""
from __future__ import annotations

import os
import uuid

import pytest

# Vor django.setup() lesen: settings.py baut DATABASES aus dem DSN beim
# Import, und sources.apps.ready() legt bei gesetztem DSN das Meta-Schema an.
# Der Test-DSN ist die einzige erlaubte Quelle; ohne ihn bleibt der
# produktive DSN leer, damit kein Testlauf versehentlich die echte Datenbank
# anfasst.
TEST_DSN = os.environ.get("STARCUBES_TEST_DSN", "").strip()
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "starcubes.settings")
os.environ["STARCUBES_POSTGRES_DSN"] = TEST_DSN
os.environ.setdefault("STARCUBES_DEBUG", "1")
# Playwrights Sync-API hinterlässt eine laufende Event-Loop; Django verweigert
# danach ORM-Zugriffe im selben Thread ("SynchronousOnlyOperation"). Im
# Testprozess ist der dokumentierte Schalter dafür richtig — die Alternative
# wäre, jede ORM-Nutzung in einen Thread zu verlagern.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

import django  # noqa: E402

django.setup()


def _skip_without_db() -> None:
    if not TEST_DSN:
        pytest.skip("STARCUBES_TEST_DSN nicht gesetzt — DB-Test übersprungen")


@pytest.fixture(scope="session")
def pg_dsn() -> str:
    _skip_without_db()
    from sqlalchemy import create_engine, text

    engine = create_engine(TEST_DSN, future=True)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - jeder Verbindungsfehler ist ein Skip
        pytest.skip(f"PostgreSQL nicht erreichbar: {exc}")
    finally:
        engine.dispose()
    return TEST_DSN


@pytest.fixture(scope="session")
def meta_engine(pg_dsn):
    """Engine auf ``starcubes_meta`` — Schema einmal pro Session anlegen."""
    from sqlalchemy import create_engine

    from core.schema import ensure_starcubes_meta

    engine = create_engine(pg_dsn, future=True)
    ensure_starcubes_meta(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def schema_name() -> str:
    """Ein frischer, gültiger Schema-Name pro Test."""
    return "t_" + uuid.uuid4().hex[:12]


@pytest.fixture
def scratch_schema(pg_dsn, schema_name):
    """Legt ein isoliertes Schema an und räumt es garantiert wieder ab."""
    from sqlalchemy import create_engine, text

    engine = create_engine(pg_dsn, future=True)
    with engine.begin() as conn:
        conn.execute(text(f"CREATE SCHEMA {schema_name}"))
    try:
        yield schema_name
    finally:
        with engine.begin() as conn:
            conn.execute(text(f"DROP SCHEMA IF EXISTS {schema_name} CASCADE"))
        engine.dispose()


@pytest.fixture
def dest_engine(pg_dsn, scratch_schema):
    from sqlalchemy import create_engine

    engine = create_engine(pg_dsn, future=True)
    yield engine
    engine.dispose()


@pytest.fixture
def make_datasource(meta_engine):
    """Legt eine ``starcubes_meta.datasource``-Zeile an (und räumt sie ab).

    Nur die NOT-NULL-Spalten; alles andere trägt seinen Default. Ein Löschen
    der Zeile räumt über ``ON DELETE CASCADE`` auch Alarme, Rollup-Zustand,
    Cache und Läufe ab.
    """
    from sqlalchemy import text

    created: list[int] = []

    def _make(schema_name: str, **over) -> int:
        name = over.pop("name", "ds_" + uuid.uuid4().hex[:12])
        values = {
            "n": name,
            "s": schema_name,
            "dialect": over.pop("source_dialect", "postgresql"),
            "dsn": over.pop("source_dsn", ""),
            "query": over.pop("import_query", "SELECT 1 AS ts"),
            "base": over.pop("base_table", None),
            "wm": over.pop("watermark_column", "ts"),
            "pc": over.pop("partition_column", "ts"),
            "period": over.pop("partition_period", "month"),
        }
        values.update(over)
        with meta_engine.begin() as conn:
            ds_id = conn.execute(text(
                "INSERT INTO starcubes_meta.datasource "
                "  (name, schema_name, source_dialect, source_dsn, "
                "   import_query, base_table, watermark_column, "
                "   partition_column, partition_period) "
                "VALUES (:n, :s, :dialect, :dsn, :query, :base, :wm, :pc, "
                "        :period) RETURNING id"), values).scalar()
        created.append(ds_id)
        return ds_id

    yield _make
    for ds_id in created:
        with meta_engine.begin() as conn:
            conn.execute(text(
                "DELETE FROM starcubes_meta.datasource WHERE id = :id"),
                {"id": ds_id})


@pytest.fixture(scope="session")
def django_orm(pg_dsn, meta_engine):
    """Django-ORM-Tabellen (auth_user, sessions, …) in der Testdatenbank.

    Reihenfolge ist wesentlich: ``starcubes_meta.sql`` läuft beim Bootstrap
    **vor** ``migrate`` und kann die ``auth_user``-Zusatzspalten nur ergänzen,
    wenn die Tabelle schon existiert. Auf einer frischen Datenbank legt
    ``migrate`` sie an; der zweite ``ensure_starcubes_meta(force=True)``-Lauf
    ist der Post-Migrate-Pass, den auch ``manage.py bootstrap`` macht.
    """
    from django.core.management import call_command

    from core.schema import ensure_starcubes_meta

    call_command("migrate", interactive=False, verbosity=0)
    ensure_starcubes_meta(meta_engine, force=True)
    return True


@pytest.fixture
def django_client(django_orm):
    """Django-Testclient; CSRF wird pro Test bei Bedarf eingeschaltet."""
    from django.test import Client

    return Client()


@pytest.fixture
def seeded(meta_engine, pg_dsn, django_orm, schema_name):
    """Eine vollständig konfigurierte Datasource mit Fact- und Dim-Daten.

    Deckt alles ab, was ``Source.load`` und die Chart-/API-Views brauchen:
    datasource, fact_column, dimension, fact_index, measure sowie die
    physischen Tabellen und Zeilen.
    """
    from tests.seed import drop_seeded, seed_datasource

    handle = seed_datasource(meta_engine, pg_dsn, schema_name)
    try:
        yield handle
    finally:
        drop_seeded(meta_engine, handle)


@pytest.fixture
def make_user(django_orm):
    """Legt Login-Benutzer an und löscht sie nach dem Test wieder."""
    from tests.seed import create_user

    created = []

    def _make(username=None, password="test-pass-123", *, superuser=False):
        user = create_user(username, password, superuser=superuser)
        created.append(user)
        return user

    yield _make
    for user in created:
        user.delete()


@pytest.fixture
def make_source(schema_name):
    """Baut eine minimale, gültige ``Source`` für Unit- und DB-Tests.

    Rein konstruierend, ohne Datenbankkontakt. Überschreiben ist erwünscht:
    ``make_source(fact_columns=[...])``. DB-Tests ersetzen ``schema_name`` und
    die DSNs selbst (``make_source(schema_name=scratch_schema, dest_dsn=pg_dsn)``).
    """
    from core.source import FactColumn, FactIndex, Source

    def _make(**overrides) -> Source:
        values = dict(
            id=1,
            name="test_source",
            schema_name=schema_name,
            source_dialect="postgresql",
            source_dsn="postgresql:///unused",
            dest_dsn="postgresql:///unused",
            import_query="SELECT ts, val FROM src",
            base_table="src",
            watermark_column="ts",
            watermark_max_query=None,
            partition_column="ts",
            partition_period="day",
            chunksize=500,
            transform_procs=1,
            small_window_hint=None,
            interval_ladder=[1, 60, 3600],
            bar_pixel_target=8.0,
            fact_columns=[
                FactColumn("ts", "timestamptz", "time", 0, "ts"),
                FactColumn("val", "integer", "measure", 1, "val"),
            ],
            dimensions=[],
            indices=[FactIndex("ts", "brin")],
            measures=[],
        )
        values.update(overrides)
        return Source(**values)

    return _make
