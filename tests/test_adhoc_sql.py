"""core/adhoc.py — die Lexer-Schicht vor der Query-Konsole.

``validate_sql`` ist nicht die Sicherheitsgrenze (das sind die Read-only-
Transaktion und der Server-Cursor), aber die einzige Schicht, die
"genau eine Anweisung" durchsetzt — und die Quelle der Fehlermeldungen.
"""
from __future__ import annotations

import pytest

from core.adhoc import (
    MAX_SQL_LEN,
    SqlRejected,
    make_tag,
    sql_hash,
    validate_sql,
)


class TestValidateSql:
    def test_einfaches_select(self):
        assert validate_sql("  SELECT 1  ") == "SELECT 1"

    def test_with_erlaubt(self):
        sql = "WITH x AS (SELECT 1 AS a) SELECT * FROM x"
        assert validate_sql(sql) == sql

    def test_trailing_semikolon_entfernt(self):
        assert validate_sql("SELECT 1;") == "SELECT 1"

    def test_mehrere_anweisungen_abgelehnt(self):
        with pytest.raises(SqlRejected, match="single statement"):
            validate_sql("SELECT 1; SELECT 2")

    def test_semikolon_im_string_ist_kein_trenner(self):
        sql = "SELECT 'a;b'"
        assert validate_sql(sql) == sql

    def test_escaped_quote_im_string(self):
        sql = "SELECT 'it''s; fine'"
        assert validate_sql(sql) == sql

    def test_backslash_escape(self):
        sql = "SELECT E'it\\'s; fine'"
        assert validate_sql(sql) == sql

    def test_semikolon_in_double_quoted_identifier(self):
        sql = 'SELECT "a;b" FROM t'
        assert validate_sql(sql) == sql

    def test_dollar_quoted_body(self):
        sql = "SELECT $$a;b$$"
        assert validate_sql(sql) == sql

    def test_named_dollar_quote(self):
        sql = "SELECT $tag$a;b$tag$"
        assert validate_sql(sql) == sql

    def test_zeilenkommentar_vor_select(self):
        assert validate_sql("-- führend\nSELECT 1") == "-- führend\nSELECT 1"

    def test_verschachtelter_blockkommentar(self):
        sql = "/* a /* b */ c */ SELECT 1"
        assert validate_sql(sql) == sql

    def test_kommentar_mit_semikolon(self):
        sql = "-- a; b\nSELECT 1"
        assert validate_sql(sql) == sql

    def test_insert_abgelehnt(self):
        with pytest.raises(SqlRejected, match="SELECT / WITH"):
            validate_sql("INSERT INTO t VALUES (1)")

    def test_update_abgelehnt(self):
        with pytest.raises(SqlRejected):
            validate_sql("UPDATE t SET a = 1")

    def test_delete_abgelehnt(self):
        with pytest.raises(SqlRejected):
            validate_sql("DELETE FROM t")

    def test_leer_abgelehnt(self):
        with pytest.raises(SqlRejected, match="no SQL"):
            validate_sql("   ")

    def test_none_abgelehnt(self):
        with pytest.raises(SqlRejected, match="no SQL"):
            validate_sql(None)

    def test_zu_lang_abgelehnt(self):
        with pytest.raises(SqlRejected, match="too long"):
            validate_sql("SELECT " + "1" * MAX_SQL_LEN)

    def test_nur_semikolon_abgelehnt(self):
        with pytest.raises(SqlRejected, match="no SQL"):
            validate_sql(";")


class TestSqlHash:
    def test_stabil(self):
        assert sql_hash("SELECT 1") == sql_hash("  SELECT 1  ")

    def test_unterscheidet(self):
        assert sql_hash("SELECT 1") != sql_hash("SELECT 2")


class TestMakeTag:
    def test_format(self):
        assert make_tag(42, "abc") == "sq:42:abc"
