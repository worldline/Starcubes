"""sources/alarm_fields.py — Feldvalidierung für Formular und Import."""
from __future__ import annotations

from types import SimpleNamespace

from django.utils.datastructures import MultiValueDict

from sources.alarm_fields import (
    alarm_row_to_dict,
    check_alarm_context,
    context_labels,
    parse_alarm_fields,
    parse_context_field,
)


def _data(**kw):
    """MultiValueDict wie aus request.POST bzw. alarm_form_data."""
    d = MultiValueDict()
    for key, value in kw.items():
        if isinstance(value, (list, tuple)):
            for v in value:
                d.appendlist(key, v)
        else:
            d[key] = value
    return d


class TestParseContextField:
    def test_none_bedeutet_nicht_gesendet(self):
        assert parse_context_field(None) == (None, None)

    def test_leer_bedeutet_global(self):
        assert parse_context_field("") == ([], None)

    def test_gueltige_zeilen(self):
        rows, err = parse_context_field(
            '[{"col": "host", "op": "in", "vals": ["1", 2]}]')
        assert err is None
        assert rows == [{"col": "host", "op": "in", "vals": ["1", 2]}]

    def test_kaputtes_json(self):
        rows, err = parse_context_field("{")
        assert rows is None
        assert "JSON array" in err

    def test_falsche_form(self):
        rows, err = parse_context_field('{"col": "host"}')
        assert rows is None
        assert "context rows must be" in err

    def test_unbekannter_op(self):
        rows, err = parse_context_field(
            '[{"col": "host", "op": "zwischen", "vals": []}]')
        assert rows is None and err

    def test_vals_muessen_liste_sein(self):
        rows, err = parse_context_field(
            '[{"col": "host", "op": "in", "vals": "1"}]')
        assert rows is None and err


class TestParseAlarmFieldsDefaults:
    def test_name_ist_pflicht(self):
        values, errors = parse_alarm_fields(_data())
        assert "name" in errors
        assert values["measure"] == "count(*)"
        assert values["window_s"] == 300
        assert values["enabled"] is True
        assert values["rule_kind"] == "threshold"

    def test_minimaler_gueltiger_alarm(self):
        values, errors = parse_alarm_fields(_data(name="zu wenig"))
        assert errors == {}
        assert values["name"] == "zu wenig"
        assert values["comment"] is None

    def test_name_zu_lang(self):
        _, errors = parse_alarm_fields(_data(name="x" * 201))
        assert "max 200" in errors["name"]

    def test_kommentar_zu_lang(self):
        _, errors = parse_alarm_fields(
            _data(name="a", comment="x" * 10001))
        assert "max 10000" in errors["comment"]


class TestParseAlarmFieldsNumbers:
    def test_threshold_float(self):
        values, errors = parse_alarm_fields(
            _data(name="a", threshold="1.5"))
        assert errors == {}
        assert values["threshold"] == 1.5

    def test_threshold_unsinn(self):
        _, errors = parse_alarm_fields(
            _data(name="a", threshold="abc"))
        assert "must be a number" in errors["threshold"]

    def test_window_null_wird_abgelehnt(self):
        _, errors = parse_alarm_fields(_data(name="a", window_s="0"))
        assert "> 0" in errors["window_s"]

    def test_window_unsinn(self):
        _, errors = parse_alarm_fields(_data(name="a", window_s="x"))
        assert "integer" in errors["window_s"]

    def test_cond_op(self):
        values, _ = parse_alarm_fields(_data(name="a", cond_op="<="))
        assert values["cond_op"] == "<="
        _, errors = parse_alarm_fields(_data(name="a", cond_op="~"))
        assert "cond_op" in errors


class TestParseAlarmFieldsMeasure:
    def test_count_star(self):
        values, _ = parse_alarm_fields(_data(name="a", measure="count(*)"))
        assert values["measure"] == "count(*)"

    def test_bare_spalte(self):
        values, _ = parse_alarm_fields(_data(name="a", measure="val"))
        assert values["measure"] == "val"

    def test_funktion(self):
        values, _ = parse_alarm_fields(_data(name="a", measure="avg(val)"))
        assert values["measure"] == "avg(val)"

    def test_quantile_ist_nicht_vergleichbar(self):
        _, errors = parse_alarm_fields(
            _data(name="a", measure="quantile(val)"))
        assert "not comparable" in errors["measure"]

    def test_kaputte_schreibweise(self):
        _, errors = parse_alarm_fields(
            _data(name="a", measure="avg (val)"))
        assert "measure must be" in errors["measure"]


class TestParseAlarmFieldsChannels:
    def test_csv_und_getlist(self):
        values, errors = parse_alarm_fields(_data(
            name="a", notify_channels=["email", "xmpp,email"]))
        assert errors == {}
        assert values["notify_channels"] == ["email", "xmpp", "email"]

    def test_log_wird_still_entfernt(self):
        values, errors = parse_alarm_fields(
            _data(name="a", notify_channels=["log", "email"]))
        assert errors == {}
        assert values["notify_channels"] == ["email"]

    def test_unbekannter_kanal(self):
        _, errors = parse_alarm_fields(
            _data(name="a", notify_channels=["taube"]))
        assert "unknown channel" in errors["notify_channels"]


class TestParseAlarmFieldsRecipients:
    def test_abwesend_bleibt_none(self):
        values, errors = parse_alarm_fields(_data(name="a"))
        assert errors == {}
        assert values["notify_email_recipients"] is None
        assert values["notify_xmpp_recipients"] is None

    def test_csv_und_dedupe(self):
        values, _ = parse_alarm_fields(_data(
            name="a", notify_email_recipients=["3,1", "3"]))
        assert values["notify_email_recipients"] == [1, 3]

    def test_keine_ids(self):
        _, errors = parse_alarm_fields(_data(
            name="a", notify_email_recipients=["anna"]))
        assert "user ids" in errors["notify_email_recipients"]


class TestParseAlarmFieldsFlags:
    def test_enabled_abwesend_bleibt_true(self):
        values, _ = parse_alarm_fields(_data(name="a"))
        assert values["enabled"] is True

    def test_enabled_anwesend_an(self):
        values, _ = parse_alarm_fields(_data(name="a", enabled="on"))
        assert values["enabled"] is True

    def test_enabled_anwesend_leer_ist_aus(self):
        values, _ = parse_alarm_fields(_data(name="a", enabled=""))
        assert values["enabled"] is False

    def test_severity_wird_kleingeschrieben(self):
        values, errors = parse_alarm_fields(_data(name="a", severity="WARNING"))
        assert errors == {}
        assert values["severity"] == "warning"

    def test_leere_severity_ist_none(self):
        values, _ = parse_alarm_fields(_data(name="a", severity=""))
        assert values["severity"] is None

    def test_unbekannte_severity(self):
        _, errors = parse_alarm_fields(_data(name="a", severity="fatal"))
        assert "severity" in errors


class TestParseAlarmFieldsPipeline:
    def test_condition_ist_beim_anlegen_pflicht(self):
        _, errors = parse_alarm_fields(
            _data(name="a", rule_kind="pipeline"))
        assert "condition" in errors

    def test_gueltige_condition(self):
        values, errors = parse_alarm_fields(_data(
            name="a", rule_kind="pipeline", condition="data_stale"))
        assert errors == {}
        assert values["rule_kind"] == "pipeline"
        assert values["condition"] == "data_stale"

    def test_unbekannte_condition(self):
        _, errors = parse_alarm_fields(_data(
            name="a", rule_kind="pipeline", condition="fehlt"))
        assert "condition" in errors

    def test_pipeline_ignoriert_datenfelder(self):
        values, errors = parse_alarm_fields(_data(
            name="a", rule_kind="pipeline", condition="data_stale",
            measure="sum(val)", window_s="0", context="[]"))
        assert errors == {}
        # Die Datenfeld-Branches wurden gar nicht erst betreten.
        assert values["measure"] == "count(*)"
        assert values["window_s"] == 300
        assert values["context_rows"] is None

    def test_edit_mit_gespeichertem_kind_braucht_keine_condition(self):
        values, errors = parse_alarm_fields(
            _data(name="a"), rule_kind="pipeline")
        assert errors == {}
        assert values["rule_kind"] == "pipeline"
        assert values["condition"] is None

    def test_unbekanntes_rule_kind(self):
        _, errors = parse_alarm_fields(
            _data(name="a", rule_kind="quatsch"))
        assert "rule_kind" in errors


class TestAlarmRowToDict:
    def _row(self):
        return SimpleNamespace(
            id=1, datasource_id=2, name="a", rule_kind="threshold",
            condition=None, context_filter=[], comment=None,
            measure="count(*)", cond_op="<", threshold=1.5, window_s=300,
            notify_channels=["email"], notify_email_recipients=[1],
            notify_xmpp_recipients=None, severity=None, enabled=1,
            created_at=None, updated_at=None)

    def test_shape(self):
        got = alarm_row_to_dict(self._row())
        assert got["threshold"] == 1.5
        assert got["enabled"] is True
        assert got["notify_xmpp_recipients"] == []
        assert got["created_at"] is None
        # eval_end ist Laufzeitmarke, keine Konfiguration.
        assert "eval_end" not in got


class TestNoDbPaths:
    def test_pipeline_context_check_ohne_db(self):
        assert check_alarm_context(None, "", 1, {"rule_kind": "pipeline"}) == \
            (None, {}, 0)

    def test_context_labels_ohne_kontexte(self):
        assert context_labels(None, "", 1, [None, []]) == {}
