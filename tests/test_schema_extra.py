"""core/schema.py — Typ-Sicherheit und Inline-CSV (ohne DB)."""
from __future__ import annotations

from datetime import datetime, timezone

from core.schema import _is_safe_cast, _parse_inline_values, _to_naive_local


class TestIsSafeCast:
    def test_gleich_ist_keine_erweiterung(self):
        assert _is_safe_cast("integer", "integer") is False

    def test_nach_text_ist_immer_sicher(self):
        assert _is_safe_cast("integer", "text") is True
        assert _is_safe_cast("boolean", "character varying") is True

    def test_bekannte_erweiterungen(self):
        assert _is_safe_cast("smallint", "integer") is True
        assert _is_safe_cast("smallint", "bigint") is True
        assert _is_safe_cast("integer", "bigint") is True
        assert _is_safe_cast("real", "double precision") is True
        assert _is_safe_cast("timestamp", "timestamptz") is True

    def test_verengung_ist_nicht_sicher(self):
        assert _is_safe_cast("bigint", "integer") is False
        assert _is_safe_cast("integer", "smallint") is False
        assert _is_safe_cast("double precision", "real") is False
        assert _is_safe_cast("timestamptz", "timestamp") is False

    def test_seitwaerts_ist_nicht_sicher(self):
        assert _is_safe_cast("integer", "numeric") is False


class TestParseInlineValues:
    def test_leere_und_kommentarzeilen(self):
        text = "\n# kopf\n\n1,a\n"
        assert _parse_inline_values(text) == [("1", "a", "")]

    def test_zwei_und_drei_felder(self):
        text = "1,eins\n2,zwei,Beschreibung\n"
        assert _parse_inline_values(text) == [
            ("1", "eins", ""),
            ("2", "zwei", "Beschreibung"),
        ]

    def test_fehlender_name_nimmt_die_id(self):
        assert _parse_inline_values("42\n") == [("42", "42", "")]

    def test_leerzeichen_werden_getrimmt(self):
        assert _parse_inline_values(" 1 , eins , text \n") == [
            ("1", "eins", "text")]

    def test_mehr_als_drei_felder_ignoriert_den_rest(self):
        assert _parse_inline_values("1,a,b,c\n") == [("1", "a", "b")]

    def test_leerer_text(self):
        assert _parse_inline_values("") == []


class TestToNaiveLocal:
    def test_naiv_bleibt(self):
        dt = datetime(2026, 1, 1, 12)
        assert _to_naive_local(dt) == dt

    def test_bewusst_wird_lokal_und_naiv(self):
        dt = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        got = _to_naive_local(dt)
        assert got.tzinfo is None
        # Die naive Wandzeit ist die lokale; als lokal gelesen ergibt sie
        # wieder denselben Zeitpunkt.
        assert got.astimezone(timezone.utc) == dt
