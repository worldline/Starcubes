"""core/schema.py — Partitionsnamen und Schema-Validierung (ohne DB)."""
from __future__ import annotations

from datetime import datetime

import pytest

from core.schema import (
    SchemaNameError,
    _default_window,
    _floor_to_period,
    _next_period,
    _parse_partition_name,
    _partition_name,
    _prev_period,
    validate_schema_name,
)


class TestValidateSchemaName:
    @pytest.mark.parametrize("name", [
        "starcubes_payments",
        "_priv",
        "a",
        "t_0123456789ab",
        "x" * 63,
    ])
    def test_gueltig(self, name):
        assert validate_schema_name(name) == name

    @pytest.mark.parametrize("name", [
        "",
        "Upper",
        "1abc",
        "mit-bindestrich",
        "mit leerzeichen",
        "x" * 64,
        "schema.punkt",
        123,
        None,
    ])
    def test_ungueltig(self, name):
        with pytest.raises(SchemaNameError):
            validate_schema_name(name)

    def test_fehlertext_nennt_regel(self):
        with pytest.raises(SchemaNameError, match="lower-case"):
            validate_schema_name("Bad")


class TestFloorToPeriod:
    def test_tag_entfernt_uhrzeit(self):
        got = _floor_to_period(datetime(2026, 5, 17, 13, 44, 59, 999), "day")
        assert got == datetime(2026, 5, 17)

    def test_woche_ist_iso_montag(self):
        # 2026-05-17 ist ein Sonntag.
        assert _floor_to_period(datetime(2026, 5, 17), "week") == datetime(2026, 5, 11)
        # Montag bleibt Montag.
        assert _floor_to_period(datetime(2026, 5, 11), "week") == datetime(2026, 5, 11)

    def test_monat_ist_erster(self):
        assert _floor_to_period(datetime(2026, 5, 17), "month") == datetime(2026, 5, 1)

    def test_unbekannte_periode(self):
        with pytest.raises(ValueError, match="unknown partition_period"):
            _floor_to_period(datetime(2026, 1, 1), "year")


class TestNextPrevPeriod:
    def test_tag_jahreswechsel(self):
        assert _next_period(datetime(2026, 12, 31), "day") == datetime(2027, 1, 1)
        assert _prev_period(datetime(2027, 1, 1), "day") == datetime(2026, 12, 31)

    def test_woche(self):
        assert _next_period(datetime(2026, 12, 28), "week") == datetime(2027, 1, 4)
        assert _prev_period(datetime(2027, 1, 4), "week") == datetime(2026, 12, 28)

    def test_monat_jahreswechsel(self):
        assert _next_period(datetime(2026, 12, 1), "month") == datetime(2027, 1, 1)
        assert _prev_period(datetime(2027, 1, 1), "month") == datetime(2026, 12, 1)

    def test_monat_schaltjahr_februar(self):
        assert _next_period(datetime(2024, 1, 1), "month") == datetime(2024, 2, 1)
        assert _next_period(datetime(2024, 2, 1), "month") == datetime(2024, 3, 1)

    def test_unbekannte_periode(self):
        with pytest.raises(ValueError):
            _next_period(datetime(2026, 1, 1), "year")
        with pytest.raises(ValueError):
            _prev_period(datetime(2026, 1, 1), "year")


class TestPartitionName:
    def test_tag(self):
        assert _partition_name("day", datetime(2026, 5, 7)) == "fact_y2026m05d07"

    def test_monat(self):
        assert _partition_name("month", datetime(2026, 5, 1)) == "fact_y2026m05"

    def test_woche(self):
        # 2026-05-11 ist ein Montag.
        assert _partition_name("week", datetime(2026, 5, 11)) == "fact_y2026w20"

    def test_woche_am_jahreswechsel_nutzt_iso_jahr(self):
        # 2021-01-01 gehört zur ISO-Woche 2020-W53.
        assert _partition_name("week", datetime(2021, 1, 1)) == "fact_y2020w53"

    def test_eigenes_praefix(self):
        assert _partition_name("day", datetime(2026, 5, 7), prefix="rollup_1h") == \
            "rollup_1h_y2026m05d07"

    def test_unbekannte_periode(self):
        with pytest.raises(ValueError):
            _partition_name("year", datetime(2026, 1, 1))


class TestParsePartitionName:
    @pytest.mark.parametrize("period,dt", [
        ("day", datetime(2026, 5, 7)),
        ("month", datetime(2026, 5, 1)),
        ("week", datetime(2026, 5, 11)),
        ("week", datetime(2021, 1, 4)),  # ISO 2021-W01
    ])
    def test_roundtrip(self, period, dt):
        assert _parse_partition_name(period, _partition_name(period, dt)) == dt

    def test_woche_53_zurueckgelesen(self):
        assert _parse_partition_name("week", "fact_y2020w53") == datetime(2020, 12, 28)

    def test_falsches_muster(self):
        with pytest.raises(ValueError):
            _parse_partition_name("day", "fact_y2026m05")
        with pytest.raises(ValueError):
            _parse_partition_name("week", "fact_y2026m05d07")

    def test_unbekannte_periode(self):
        with pytest.raises(ValueError):
            _parse_partition_name("year", "fact_y2026")


class TestDefaultWindow:
    def test_werte(self):
        assert _default_window("day") == (30, 7)
        assert _default_window("week") == (14, 4)
        assert _default_window("month") == (4, 1)

    def test_unbekannt(self):
        with pytest.raises(ValueError):
            _default_window("year")
