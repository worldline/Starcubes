"""core/log_ship.py — Klassifikation und Sidecar (ohne DB)."""
from __future__ import annotations

import json

import pytest

from core.log_ship import (
    MAX_LINE_CHARS,
    KINDS,
    _Classifier,
    _Reader,
    read_sidecar,
    sidecar_path,
    write_sidecar,
)

PREFIX = "07/Nov/2026 12:00:00 "


class TestClassifier:
    def test_level_praefix(self):
        c = _Classifier()
        assert c.level(PREFIX + "INFO starting") == "INFO"
        assert c.level(PREFIX + "DEBUG detail") == "DEBUG"

    def test_warning_oeffnet_block(self):
        c = _Classifier()
        assert c.level(PREFIX + "WARNING kaputt") == "WARNING"
        # Fortsetzung (eingerückt) erbt das Level.
        assert c.level("  Datei x.py, Zeile 3") == "WARNING"

    def test_traceback_erbt(self):
        c = _Classifier()
        c.level(PREFIX + "ERROR fehlgeschlagen")
        assert c.level("Traceback (most recent call last):") == "ERROR"
        assert c.level("  File \"x.py\", line 1") == "ERROR"

    def test_leerzeile_erbt(self):
        c = _Classifier()
        c.level(PREFIX + "WARNING x")
        assert c.level("") == "WARNING"

    def test_info_schliesst_block(self):
        c = _Classifier()
        c.level(PREFIX + "WARNING x")
        c.level(PREFIX + "INFO weiter")
        assert c.level("  normale Zeile") is None

    def test_rohe_ausgabe_ohne_level(self):
        c = _Classifier()
        assert c.level("einfach nur text") is None

    def test_fatal_python_error(self):
        c = _Classifier()
        assert c.level("Fatal Python error: Segmentation fault") == "CRITICAL"
        assert c.level("Current thread 0x0001") == "CRITICAL"


class TestSidecar:
    def test_roundtrip(self, tmp_path):
        spool = str(tmp_path / "run.log")
        write_sidecar(spool, kind="etl", datasource_id=7)
        data = read_sidecar(spool)
        assert data is not None
        assert data["kind"] == "etl"
        assert data["datasource_id"] == 7
        assert data["offset"] == 0
        assert data["run_ref"] == "run.log"

    def test_offset_und_atomar(self, tmp_path):
        spool = str(tmp_path / "run.log")
        write_sidecar(spool, kind="forever", datasource_id=None, offset=123)
        assert read_sidecar(spool)["offset"] == 123
        # Keine .tmp-Datei zurückgelassen.
        assert not (tmp_path / "run.log.json.tmp").exists()

    def test_unbekannter_kind(self, tmp_path):
        with pytest.raises(ValueError, match="unknown log kind"):
            write_sidecar(str(tmp_path / "x.log"), kind="quatsch",
                          datasource_id=None)

    def test_fehlend_oder_kaputt(self, tmp_path):
        assert read_sidecar(str(tmp_path / "fehlt.log")) is None
        path = sidecar_path(str(tmp_path / "x.log"))
        with open(path, "w") as fh:
            fh.write("{")
        assert read_sidecar(str(tmp_path / "x.log")) is None

    def test_unbekannter_kind_in_datei(self, tmp_path):
        path = sidecar_path(str(tmp_path / "x.log"))
        with open(path, "w") as fh:
            json.dump({"kind": "quatsch"}, fh)
        assert read_sidecar(str(tmp_path / "x.log")) is None

    def test_kinds_sind_bekannt(self):
        assert "etl" in KINDS and "forever" in KINDS


class TestReader:
    def test_liest_nur_komplette_zeilen_ausser_final(self, tmp_path):
        p = tmp_path / "spool.log"
        p.write_text("eins\nzwei")
        reader = _Reader(str(p), 0)
        assert reader.read(final=False) == [(None, "eins")]
        assert reader.offset == len("eins\n")

    def test_final_liest_die_letzte_teilzeile(self, tmp_path):
        p = tmp_path / "spool.log"
        p.write_text("eins\nzwei")
        reader = _Reader(str(p), 0)
        assert reader.read(final=True) == [(None, "eins"), (None, "zwei")]

    def test_offset_setzt_fort(self, tmp_path):
        p = tmp_path / "spool.log"
        p.write_text("eins\n")
        reader = _Reader(str(p), 0)
        assert reader.read(final=True) == [(None, "eins")]
        assert reader.read(final=True) == []
        with open(p, "a") as fh:
            fh.write("zwei\n")
        assert reader.read(final=True) == [(None, "zwei")]

    def test_truncation_setzt_zurueck(self, tmp_path):
        p = tmp_path / "spool.log"
        p.write_text("eins\nzwei\n")
        reader = _Reader(str(p), 0)
        reader.read(final=True)
        p.write_text("neu\n")     # kürzer als der Offset
        assert reader.read(final=True) == [(None, "neu")]

    def test_fehlende_datei(self, tmp_path):
        assert _Reader(str(tmp_path / "fehlt"), 0).read(final=True) == []

    def test_zeilenlaenge_wird_gekappt(self, tmp_path):
        p = tmp_path / "spool.log"
        p.write_text("x" * (MAX_LINE_CHARS + 100) + "\n")
        reader = _Reader(str(p), 0)
        [(level, line)] = reader.read(final=True)
        assert level is None
        assert len(line) == MAX_LINE_CHARS
