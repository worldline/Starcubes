"""core/alarm.py — Episodenmaschine gegen PostgreSQL."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.db

from core.alarm import episode_step  # noqa: E402


def _counts():
    return {"transitions": [], "episodes_opened": 0,
            "episodes_extended": 0, "episodes_closed": 0}


@pytest.fixture
def alarm(meta_engine, make_datasource):
    ds_id = make_datasource("t_alarm_" + uuid.uuid4().hex[:8])
    with meta_engine.begin() as conn:
        alarm_id = conn.execute(text(
            "INSERT INTO starcubes_meta.alarm "
            "  (datasource_id, name, context_filter, measure, cond_op, "
            "   threshold, window_s) "
            "VALUES (:ds, 'zu wenig', '[]'::jsonb, 'count(*)', '<', 1, 300) "
            "RETURNING id"), {"ds": ds_id}).scalar()
    return {"id": alarm_id, "name": "zu wenig", "datasource_id": ds_id}


def _episodes(meta_engine, alarm_id):
    with meta_engine.connect() as conn:
        return conn.execute(text(
            "SELECT id, first_breach_at, last_breach_at, closed_at "
            "  FROM starcubes_meta.alarm_firing WHERE alarm_id = :a "
            " ORDER BY id"), {"a": alarm_id}).mappings().all()


def _eval_end(meta_engine, alarm_id):
    with meta_engine.connect() as conn:
        return conn.execute(text(
            "SELECT eval_end FROM starcubes_meta.alarm WHERE id = :a"),
            {"a": alarm_id}).scalar()


class TestEpisodeStep:
    def test_oeffnen_verlaengern_schliessen(self, meta_engine, alarm):
        t0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        t1 = t0 + timedelta(minutes=5)
        t2 = t0 + timedelta(minutes=10)

        c = _counts()
        episode_step(meta_engine, alarm, breach=True, at=t0, value=0.0,
                     counts=c, label="test")
        assert c["episodes_opened"] == 1
        assert [t["kind"] for t in c["transitions"]] == ["opened"]
        assert _eval_end(meta_engine, alarm["id"]) == t0
        [ep] = _episodes(meta_engine, alarm["id"])
        assert ep["closed_at"] is None

        c = _counts()
        episode_step(meta_engine, alarm, breach=True, at=t1, value=0.0,
                     counts=c)
        assert c["episodes_extended"] == 1
        assert c["transitions"] == []
        [ep] = _episodes(meta_engine, alarm["id"])
        assert ep["last_breach_at"] == t1

        c = _counts()
        episode_step(meta_engine, alarm, breach=False, at=t2, value=5.0,
                     counts=c)
        assert c["episodes_closed"] == 1
        [closed] = c["transitions"]
        assert closed["kind"] == "closed"
        assert closed["closed_at"] == t2
        [ep] = _episodes(meta_engine, alarm["id"])
        assert ep["closed_at"] == t2
        assert _eval_end(meta_engine, alarm["id"]) == t2

    def test_ohne_offene_episode_und_kein_breach_passiert_nichts(
            self, meta_engine, alarm):
        c = _counts()
        episode_step(meta_engine, alarm, breach=False,
                     at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                     value=5.0, counts=c)
        assert c == _counts()
        assert _episodes(meta_engine, alarm["id"]) == []

    def test_zweite_episode_nach_dem_schliessen(self, meta_engine, alarm):
        t0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        for at, breach in [(t0, True), (t0 + timedelta(minutes=5), False),
                           (t0 + timedelta(minutes=10), True)]:
            episode_step(meta_engine, alarm, breach=breach, at=at,
                         value=0.0, counts=_counts())
        eps = _episodes(meta_engine, alarm["id"])
        assert len(eps) == 2
        assert eps[0]["closed_at"] is not None
        assert eps[1]["closed_at"] is None


class TestEvaluateAlarm:
    def _src(self, make_source, make_datasource, scratch_schema, pg_dsn):
        from core.schema import create_fact
        from core.source import FactColumn

        ds = make_datasource(scratch_schema)
        src = make_source(
            id=ds, schema_name=scratch_schema, dest_dsn=pg_dsn,
            source_dsn=pg_dsn, partition_column="ts",
            partition_period="month",
            fact_columns=[
                FactColumn("ts", "timestamptz", "time", 0, "ts"),
                FactColumn("val", "integer", "measure", 1, "val"),
            ],
            dimensions=[], indices=[])
        eng = src.dest_engine()
        create_fact(src, eng)
        return src, eng

    def _insert(self, src, at):
        with src.dest_engine().begin() as conn:
            conn.execute(text(
                f"INSERT INTO {src.schema_name}.fact (ts, val) "
                f"VALUES (:ts, 1)"), {"ts": at})

    def test_fact_max_time(self, make_source, make_datasource,
                           scratch_schema, pg_dsn):
        from core.alarm import fact_max_time
        src, _ = self._src(make_source, make_datasource, scratch_schema,
                           pg_dsn)
        assert fact_max_time(src) is None
        at = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)
        self._insert(src, at)
        assert fact_max_time(src) == at

    def test_breach_oeffnet_und_schliesst(self, meta_engine, make_source,
                                          make_datasource, scratch_schema,
                                          pg_dsn, alarm):
        from core.alarm import evaluate_alarm

        src, _ = self._src(make_source, make_datasource, scratch_schema,
                           pg_dsn)
        t0 = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)
        self._insert(src, t0)

        a = {"id": alarm["id"], "name": alarm["name"],
             "datasource_id": alarm["datasource_id"], "ds_name": "test",
             "eval_end": None, "measure": "count(*)", "cond_op": "<",
             "threshold": 1.0, "window_s": 300, "context_filter": [],
             "notify_channels": [], "notify_email_recipients": [],
             "notify_xmpp_recipients": []}

        # Im letzten Fenster liegt nichts → 0 < 1 → Episode öffnet.
        counts = evaluate_alarm(src, meta_engine, a)
        assert counts["windows"] == 1
        assert counts["breaches"] == 1
        assert counts["episodes_opened"] == 1
        a["eval_end"] = t0

        # Später eine Zeile weit vor dem Fenster → weiterhin leer → verlängert.
        later = t0 + timedelta(minutes=10)
        self._insert(src, later)
        counts = evaluate_alarm(src, meta_engine, a)
        assert counts["episodes_extended"] == 1
        assert counts["episodes_closed"] == 0
        a["eval_end"] = later

        # Eine Zeile im *neuen* letzten Fenster → 1 < 1 ist falsch → schließt.
        # Der HWM muss dabei mitwandern: `evaluate_alarm` überspringt ein
        # Fenster, dessen Obergrenze unverändert ist (die Zeile allein im
        # alten Fenster würde nichts bewegen).
        next_max = later + timedelta(minutes=10)
        # Eine Zeile setzt den HWM (sie liegt selbst am offenen Ende und
        # zählt nicht), die andere liegt im Fenster davor.
        self._insert(src, next_max)
        self._insert(src, next_max - timedelta(seconds=10))
        counts = evaluate_alarm(src, meta_engine, a)
        assert counts["breaches"] == 0
        assert counts["episodes_closed"] == 1
        a["eval_end"] = next_max

        # Kein neuer Datenstand → identisches Fenster wird übersprungen.
        counts = evaluate_alarm(src, meta_engine, a)
        assert counts["windows"] == 0
