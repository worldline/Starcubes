"""core/logsource.py — Parser-Bausteine jenseits von check_logsource.

`check_logsource` deckt die Szenarien ab; hier stehen die Randfälle der
einzelnen Bausteine, die dort nur indirekt berührt werden.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from core.logsource import (
    LogSourceError,
    _cast,
    _guess_type,
    _parse_time,
    _resolve_zone,
    _supply_year,
    compile_line_regex,
    safe_column_name,
)

UTC = timezone.utc
BERLIN = ZoneInfo("Europe/Berlin")


class TestCompileLineRegex:
    def test_multiline_ist_gesetzt(self):
        rx = compile_line_regex(r"^start.*$")
        assert len(rx.findall("start a\nstart b")) == 2

    def test_ohne_multiline_wuerde_nur_einmal_matchen(self):
        # Der Beleg, warum das Flag da ist — nicht die Implementierung,
        # sondern das Verhalten, das es verhindert: ohne MULTILINE findet
        # das Muster über einen mehrzeiligen Text gar nichts, weil `$` nicht
        # am Zeilenende ankert.
        import re
        without = re.compile(r"^start.*$")
        assert len(without.findall("start a\nstart b")) == 0

    def test_kaputtes_muster(self):
        with pytest.raises(LogSourceError, match="does not compile"):
            compile_line_regex("(")

    def test_eigener_name_in_der_meldung(self):
        with pytest.raises(LogSourceError, match="log_record_start"):
            compile_line_regex("(", "log_record_start")


class TestResolveZone:
    def test_none_ist_utc(self):
        assert _resolve_zone(None) == ZoneInfo("UTC")

    def test_gueltige_zone(self):
        assert _resolve_zone("Europe/Berlin") == BERLIN

    def test_unbekannte_zone(self):
        with pytest.raises(LogSourceError, match="not a known IANA zone"):
            _resolve_zone("Mars/Olympus")


class TestParseTime:
    def test_iso_mit_Z(self):
        got = _parse_time("2026-01-02T03:04:05Z", None, UTC, None)
        assert got == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)

    def test_iso_mit_fraktionen(self):
        got = _parse_time("2026-01-02T03:04:05.123456Z", None, UTC, None)
        assert got.microsecond == 123456

    def test_format_ohne_offset_nutzt_zone(self):
        got = _parse_time("2026-01-02 03:04:05", "%Y-%m-%d %H:%M:%S",
                          BERLIN, None)
        # Berlin ist im Januar UTC+1.
        assert got == datetime(2026, 1, 2, 2, 4, 5, tzinfo=UTC)

    def test_offset_in_der_zeile_gewinnt(self):
        got = _parse_time("02/Jan/2026:03:04:05 +0200",
                          "%d/%b/%Y:%H:%M:%S %z", BERLIN, None)
        assert got == datetime(2026, 1, 2, 1, 4, 5, tzinfo=UTC)

    def test_jahr_kommt_aus_der_referenz(self):
        ref = datetime(2026, 6, 1, tzinfo=UTC)
        got = _parse_time("May 17 12:00:00", "%b %d %H:%M:%S", UTC, ref)
        assert got.year == 2026
        assert got.month == 5 and got.day == 17

    def test_jahreswechsel_geht_zurueck(self):
        ref = datetime(2026, 1, 2, tzinfo=UTC)
        got = _parse_time("Dec 31 23:59:00", "%b %d %H:%M:%S", UTC, ref)
        assert got.year == 2025

    def test_29_februar_im_nicht_schaltjahr(self):
        ref = datetime(2026, 6, 1, tzinfo=UTC)
        dt = _supply_year(datetime(2024, 2, 29, 12), ref)
        assert dt == datetime(2026, 2, 28, 12)

    def test_epoch_sekunden(self):
        got = _parse_time("1767323045", "@epoch", UTC, None)
        assert got == datetime.fromtimestamp(1767323045, UTC)

    def test_epoch_millisekunden(self):
        got = _parse_time("1767323045000", "@epoch_ms", UTC, None)
        assert got == datetime.fromtimestamp(1767323045, UTC)

    def test_epoch_unsinn(self):
        with pytest.raises(ValueError, match="is not a number"):
            _parse_time("abc", "@epoch", UTC, None)


class TestSupplyYear:
    def test_ohne_referenz_nimmt_das_aktuelle_jahr(self):
        got = _supply_year(datetime(1900, 5, 17, 12), None)
        assert got.year == datetime.now(UTC).year

    def test_zukunft_mehr_als_einen_tag_wird_vorjahr(self):
        ref = datetime(2026, 1, 2, tzinfo=UTC)
        got = _supply_year(datetime(1900, 12, 31, 12), ref)
        assert got.year == 2025

    def test_ein_tag_zukunft_bleibt(self):
        # Genau einen Tag voraus ist die erlaubte Rundungstoleranz.
        ref = datetime(2026, 1, 2, tzinfo=UTC)
        got = _supply_year(datetime(1900, 1, 3, 0), ref)
        assert got.year == 2026


class TestCast:
    def test_integer_mit_leerzeichen(self):
        assert _cast(" 12 ", "integer") == 12

    def test_integer_aus_dezimalstring(self):
        assert _cast("1043.0", "bigint") == 1043

    def test_integer_unsinn_wirft(self):
        with pytest.raises(ValueError):
            _cast("abc", "integer")

    def test_float_typen(self):
        assert _cast("1.5", "numeric") == 1.5
        assert _cast("2", "double precision") == 2.0

    def test_text_bleibt_text(self):
        assert _cast("  abc  ", "text") == "  abc  "

    def test_none_bleibt_none(self):
        assert _cast(None, "integer") is None


class TestSafeColumnName:
    def test_bereits_gueltig(self):
        assert safe_column_name("host") == ("host", None)

    def test_reserviertes_wort(self):
        name, note = safe_column_name("user")
        assert name == "user_"
        assert note is not None
        assert "reserved word" in note
        assert "'user'" in note

    def test_bindestrich(self):
        name, note = safe_column_name("user-agent")
        assert name == "user_agent"
        assert note is not None

    def test_grossschreibung(self):
        assert safe_column_name("Host") == (
            "host", safe_column_name("Host")[1])
        assert safe_column_name("Host")[1] is not None

    def test_fuehrende_ziffer(self):
        name, note = safe_column_name("2fa")
        assert name == "c_2fa"
        assert note is not None

    def test_leer(self):
        assert safe_column_name("")[0] == "c_"

    def test_laenge(self):
        assert len(safe_column_name("x" * 100)[0]) == 63


class TestGuessType:
    def test_leer(self):
        assert _guess_type([]) == ("text", 0)

    def test_kleine_integers(self):
        assert _guess_type(["1", "2", "300"]) == ("smallint", 0)

    def test_grenze_smallint(self):
        assert _guess_type(["32767"])[0] == "smallint"
        assert _guess_type(["32768"])[0] == "integer"
        assert _guess_type(["2147483648"])[0] == "bigint"

    def test_floats(self):
        assert _guess_type(["1.5", "2"]) == ("double precision", 0)

    def test_null_tokens_zaehlen_als_nicht_numerisch(self):
        # 9 von 10 numerisch: NUMERIC_SHARE 0.9 ist erreicht.
        values = [str(i) for i in range(9)] + ["-"]
        assert _guess_type(values) == ("smallint", 1)

    def test_zu_viele_nicht_numerische_werte(self):
        values = [str(i) for i in range(8)] + ["-", "n/a"]
        assert _guess_type(values) == ("text", 0)

    def test_echter_text(self):
        assert _guess_type(["GET", "POST"]) == ("text", 0)
