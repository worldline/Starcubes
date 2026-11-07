"""core/utils.py — Datums-Parsing, PK-Formatierung, Log-Tail."""
from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from core.utils import _parse_dt, _pid_alive, format_pks, tail_file


class TestParseDt:
    def test_naive_wird_utc(self):
        assert _parse_dt("2026-01-02 03:04:05") == datetime(
            2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

    def test_leerzeichen_wird_T(self):
        assert _parse_dt("2026-01-02T03:04:05") == _parse_dt(
            "2026-01-02 03:04:05")

    def test_Z_wird_offset(self):
        got = _parse_dt("2026-01-02T03:04:05Z")
        assert got.tzinfo == timezone.utc
        assert got.hour == 3

    def test_bestehender_offset_bleibt_erhalten(self):
        got = _parse_dt("2026-01-02T03:04:05+02:00")
        assert got.utcoffset().total_seconds() == 7200
        assert got.astimezone(timezone.utc).hour == 1

    def test_umgebende_leerzeichen(self):
        assert _parse_dt("  2026-01-02 03:04:05  ").year == 2026

    def test_ungueltig_wirft(self):
        with pytest.raises(ValueError):
            _parse_dt("nicht ein datum")


class TestFormatPks:
    def test_leere_formatierung_gibt_kopie(self):
        src = ["1", "2"]
        out = format_pks(src, None)
        assert out == src
        assert out is not src

    def test_numerisches_pk_geht_als_int(self):
        assert format_pks(["7"], "{0:012d}") == ["000000000007"]

    def test_nicht_numerisches_pk_bleibt_string(self):
        assert format_pks(["abc"], "X-{0}") == ["X-abc"]

    def test_unpassendes_format_faellt_auf_rohwert_zurueck(self):
        assert format_pks(["abc"], "{0:012d}") == ["abc"]

    def test_mehrere_werte(self):
        assert format_pks(["1", "x"], "<{0}>") == ["<1>", "<x>"]


class TestTailFile:
    def test_letzte_zeilen(self, tmp_path):
        p = tmp_path / "log.txt"
        p.write_text("\n".join(f"z{i}" for i in range(10)))
        assert tail_file(str(p), max_lines=3) == "z7\nz8\nz9"

    def test_max_bytes_schneidet_erste_teilzeile_ab(self, tmp_path):
        p = tmp_path / "log.txt"
        p.write_text("aaaa\nbbbb\ncccc\n")
        out = tail_file(str(p), max_bytes=10, max_lines=100)
        # Der Schnitt liegt mitten in "bbbb" — die Teilzeile muss weg sein.
        assert out is not None
        assert out.splitlines() == ["cccc"]

    def test_fehlende_datei_none(self, tmp_path):
        assert tail_file(str(tmp_path / "fehlt")) is None

    def test_leere_datei(self, tmp_path):
        p = tmp_path / "leer"
        p.write_text("")
        assert tail_file(str(p)) == ""


class TestPidAlive:
    def test_eigene_pid(self):
        assert _pid_alive(os.getpid()) is True

    def test_sehr_unwahrscheinliche_pid(self):
        assert _pid_alive(2**31 - 1) is False
