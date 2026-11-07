"""core/alarm_io.py — Exportdatei: Form, Header, Roundtrip."""
from __future__ import annotations

import pytest
import yaml

from core.alarm_io import (
    FORMAT,
    MAX_ALARM_FILE_BYTES,
    AlarmIOError,
    dump_alarms,
    parse_alarms_text,
)


def _alarm(**over):
    base = {
        "name": "zu wenig",
        "rule_kind": "threshold",
        "measure": "count(*)",
        "cond_op": "<",
        "threshold": 1,
        "window_s": 300,
        "context_filter": [{"col": "host", "op": "in", "vals": ["1"]}],
        "comment": "mehrzeilig\nzweite Zeile",
        "enabled": True,
        "severity": "critical",
        "notify_channels": ["email"],
        "notify_email_recipients": ["anna", "bob"],
        "notify_xmpp_recipients": [],
    }
    base.update(over)
    return base


class TestDump:
    def test_header_und_struktur(self):
        text = dump_alarms([{"datasource": "payments",
                             "alarms": [_alarm()]}],
                           exported_at="2026-01-01T00:00:00Z")
        doc = yaml.safe_load(text)
        assert doc["format"] == FORMAT
        assert doc["exported_at"] == "2026-01-01T00:00:00Z"
        assert doc["datasources"][0]["datasource"] == "payments"

    def test_leere_quelle_bleibt_erhalten(self):
        doc = yaml.safe_load(dump_alarms([{"datasource": "leer",
                                           "alarms": []}]))
        assert doc["datasources"][0]["alarms"] == []

    def test_quelle_ohne_namen_wirft(self):
        with pytest.raises(AlarmIOError, match="datasource name"):
            dump_alarms([{"alarms": []}])

    def test_pipeline_verliert_datenfelder(self):
        doc = yaml.safe_load(dump_alarms([{
            "datasource": "p",
            "alarms": [_alarm(rule_kind="pipeline", condition="data_stale")],
        }]))
        alarm = doc["datasources"][0]["alarms"][0]
        assert alarm["condition"] == "data_stale"
        for dead in ("measure", "window_s", "context_filter"):
            assert dead not in alarm

    def test_threshold_behaelt_kontext_und_labels(self):
        doc = yaml.safe_load(dump_alarms([{
            "datasource": "p",
            "alarms": [_alarm(context_labels={"host": {"1": "web"}})],
        }]))
        alarm = doc["datasources"][0]["alarms"][0]
        assert alarm["context_filter"] == [
            {"col": "host", "op": "in", "vals": ["1"]}]
        assert alarm["context_labels"] == {"host": {"1": "web"}}

    def test_false_und_null_bleiben(self):
        doc = yaml.safe_load(dump_alarms([{
            "datasource": "p", "alarms": [_alarm(enabled=False)]}]))
        assert doc["datasources"][0]["alarms"][0]["enabled"] is False

    def test_ganzzahliges_float_wird_int(self):
        doc = yaml.safe_load(dump_alarms([{
            "datasource": "p", "alarms": [_alarm(threshold=5.0)]}]))
        assert doc["datasources"][0]["alarms"][0]["threshold"] == 5
        assert isinstance(doc["datasources"][0]["alarms"][0]["threshold"], int)


class TestRoundTrip:
    def test_dump_parse_dump(self):
        entries = [{
            "datasource": "payments",
            "alarms": [
                _alarm(),
                _alarm(name="pipeline", rule_kind="pipeline",
                       condition="data_stale"),
            ],
        }, {"datasource": "leer", "alarms": []}]
        text = dump_alarms(entries)
        parsed = parse_alarms_text(text)
        assert [d["datasource"] for d in parsed] == ["payments", "leer"]
        assert [a["name"] for a in parsed[0]["alarms"]] == ["zu wenig", "pipeline"]
        # Ein zweiter Export derselben geparsten Daten ist stabil.
        again = dump_alarms([{"datasource": d["datasource"],
                              "alarms": d["alarms"]} for d in parsed])
        assert yaml.safe_load(again) == yaml.safe_load(text)


class TestParseErrors:
    def _text(self, doc):
        return yaml.safe_dump(doc)

    def test_kaputtes_yaml(self):
        with pytest.raises(AlarmIOError, match="malformed YAML"):
            parse_alarms_text("a: [")

    def test_kein_mapping(self):
        with pytest.raises(AlarmIOError, match="top level"):
            parse_alarms_text("- 1")

    def test_fehlender_header(self):
        with pytest.raises(AlarmIOError, match="no 'format"):
            parse_alarms_text(self._text({"datasources": []}))

    def test_falscher_header(self):
        text = self._text({"format": "anderes/1", "datasources": []})
        with pytest.raises(AlarmIOError, match="not an alarm export"):
            parse_alarms_text(text)

    def test_leere_datasources(self):
        text = self._text({"format": FORMAT, "datasources": []})
        with pytest.raises(AlarmIOError, match="non-empty"):
            parse_alarms_text(text)

    def test_doppelte_quelle(self):
        text = self._text({"format": FORMAT, "datasources": [
            {"datasource": "p", "alarms": []},
            {"datasource": "p", "alarms": []}]})
        with pytest.raises(AlarmIOError, match="appears twice"):
            parse_alarms_text(text)

    def test_quelle_ohne_namen(self):
        text = self._text({"format": FORMAT, "datasources": [{"alarms": []}]})
        with pytest.raises(AlarmIOError, match="needs a 'datasource'"):
            parse_alarms_text(text)

    def test_doppelter_alarmname(self):
        text = self._text({"format": FORMAT, "datasources": [{
            "datasource": "p",
            "alarms": [{"name": "a"}, {"name": "a"}]}]})
        with pytest.raises(AlarmIOError, match="both named"):
            parse_alarms_text(text)

    def test_alarm_ohne_namen(self):
        text = self._text({"format": FORMAT, "datasources": [{
            "datasource": "p", "alarms": [{"threshold": 1}]}]})
        with pytest.raises(AlarmIOError, match="needs a 'name'"):
            parse_alarms_text(text)

    def test_bool_als_threshold(self):
        text = self._text({"format": FORMAT, "datasources": [{
            "datasource": "p", "alarms": [{"name": "a", "threshold": True}]}]})
        with pytest.raises(AlarmIOError, match="must be a number"):
            parse_alarms_text(text)

    def test_string_als_window(self):
        text = self._text({"format": FORMAT, "datasources": [{
            "datasource": "p", "alarms": [{"name": "a", "window_s": "300"}]}]})
        with pytest.raises(AlarmIOError, match="must be a number"):
            parse_alarms_text(text)

    def test_kanal_keine_liste(self):
        text = self._text({"format": FORMAT, "datasources": [{
            "datasource": "p",
            "alarms": [{"name": "a", "notify_channels": "email"}]}]})
        with pytest.raises(AlarmIOError, match="list of strings"):
            parse_alarms_text(text)

    def test_empfaenger_keine_liste(self):
        text = self._text({"format": FORMAT, "datasources": [{
            "datasource": "p",
            "alarms": [{"name": "a", "notify_email_recipients": 5}]}]})
        with pytest.raises(AlarmIOError, match="list of usernames"):
            parse_alarms_text(text)

    def test_kontext_keine_liste(self):
        text = self._text({"format": FORMAT, "datasources": [{
            "datasource": "p",
            "alarms": [{"name": "a", "context_filter": {}}]}]})
        with pytest.raises(AlarmIOError, match="list of filter rows"):
            parse_alarms_text(text)

    def test_zu_gross(self):
        with pytest.raises(AlarmIOError, match="larger than"):
            parse_alarms_text("x" * (MAX_ALARM_FILE_BYTES + 1))
