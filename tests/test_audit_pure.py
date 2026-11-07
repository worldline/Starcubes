"""core/audit.py — reine Fenster-, PK- und Ladder-Helfer."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import numpy as np
import pytest

from core.audit import (
    _as_utc,
    _bind_pk,
    _clamp_to_floor,
    _coerce_ids,
    _norm_pk,
    gap_span,
    lag_ladder,
    sample_evenly,
)


class TestAsUtc:
    def test_naiv_wird_utc(self):
        got = _as_utc(datetime(2026, 1, 1, 12))
        assert got == datetime(2026, 1, 1, 12, tzinfo=timezone.utc)

    def test_offset_wird_konvertiert(self):
        from datetime import timedelta
        got = _as_utc(datetime(2026, 1, 1, 14,
                               tzinfo=timezone(timedelta(hours=2))))
        assert got.hour == 12


class TestClampToFloor:
    def _src(self, make_source, floor):
        return make_source(retention_floor=floor)

    def test_ohne_floor_unveraendert(self, make_source):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = datetime(2026, 1, 2, tzinfo=timezone.utc)
        assert _clamp_to_floor(make_source(), start, end) == start

    def test_start_unter_floor_wird_angehoben(self, make_source):
        floor = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = datetime(2026, 1, 2, tzinfo=timezone.utc)
        assert _clamp_to_floor(self._src(make_source, floor), start, end) == floor

    def test_start_ueber_floor_bleibt(self, make_source):
        floor = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        start = datetime(2026, 1, 2, tzinfo=timezone.utc)
        end = datetime(2026, 1, 3, tzinfo=timezone.utc)
        assert _clamp_to_floor(self._src(make_source, floor), start, end) == start

    def test_ganzes_fenster_unter_floor_wirft(self, make_source):
        floor = datetime(2026, 1, 2, tzinfo=timezone.utc)
        with pytest.raises(ValueError, match="below the retention floor"):
            _clamp_to_floor(
                self._src(make_source, floor),
                datetime(2026, 1, 1, tzinfo=timezone.utc),
                datetime(2026, 1, 2, tzinfo=timezone.utc))

    def test_ende_genau_auf_floor_wirft(self, make_source):
        floor = datetime(2026, 1, 2, tzinfo=timezone.utc)
        with pytest.raises(ValueError):
            _clamp_to_floor(
                self._src(make_source, floor),
                datetime(2026, 1, 1, tzinfo=timezone.utc), floor)

    def test_naiver_floor_wird_utc_gelesen(self, make_source):
        src = self._src(make_source, datetime(2026, 1, 1, 12))
        got = _clamp_to_floor(
            src,
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 2, tzinfo=timezone.utc))
        assert got == datetime(2026, 1, 1, 12, tzinfo=timezone.utc)


class TestGapSpan:
    def test_sauber(self):
        assert gap_span([{"missing": 0}, {"missing": None}]) is None
        assert gap_span([]) is None

    def test_erste_und_letzte_luecke(self):
        rows = [
            {"start": "a", "end": "b", "missing": 0},
            {"start": "c", "end": "d", "missing": 2},
            {"start": "e", "end": "f", "missing": 0},
            {"start": "g", "end": "h", "missing": 5},
        ]
        assert gap_span(rows) == ("c", "h")


class TestNormPk:
    @pytest.mark.parametrize("raw,expected", [
        (None, None),
        (12, "12"),
        (12.0, "12"),
        (Decimal("12"), "12"),
        (Decimal("12.5"), "12.5"),
        (12.5, "12.5"),
        ("abc", "abc"),
        (True, "True"),
    ])
    def test_norm(self, raw, expected):
        assert _norm_pk(raw) == expected


class TestBindPk:
    def test_int_string_wird_int(self):
        assert _bind_pk("12") == 12

    def test_nicht_numerisch_bleibt_string(self):
        assert _bind_pk("abc") == "abc"

    def test_none_bleibt_none(self):
        assert _bind_pk(None) is None


class TestLagLadder:
    def test_leer(self):
        assert lag_ladder([]) == []

    def test_stoppt_nach_erster_erreichender_stufe(self):
        got = lag_ladder([5, 10, 20])
        assert got == [{"add_s": 30, "caught_pct": 100.0}]

    def test_mehrere_stufen_und_prozent(self):
        got = lag_ladder([10, 100, 1000])
        assert got[0] == {"add_s": 30, "caught_pct": 33.3}
        assert got[1] == {"add_s": 60, "caught_pct": 33.3}
        assert got[2] == {"add_s": 300, "caught_pct": 66.7}
        # 1000s wird erst mit 3600 erreicht; danach Schluss.
        assert got[-1] == {"add_s": 3600, "caught_pct": 100.0}

    def test_jenseits_der_letzten_stufe_wird_der_istwert_genannt(self):
        got = lag_ladder([100_000])
        assert got[-1]["add_s"] == 100_000
        assert got[-1]["caught_pct"] == 100.0
        assert len(got) == len([30, 60, 300, 900, 3600, 21600, 86400]) + 1


class TestSampleEvenly:
    def test_kleiner_als_n_gibt_alles(self):
        assert sample_evenly(np.array([1, 2]), 5) == ["1", "2"]

    def test_verteilt_statt_kopf(self):
        arr = np.arange(100)
        got = sample_evenly(arr, 4)
        assert got == ["0", "25", "50", "75"]

    def test_strings(self):
        assert all(isinstance(v, str) for v in sample_evenly(np.array([1, 2]), 1))


class TestCoerceIds:
    def test_integer_werden_int64(self):
        arr = _coerce_ids([1, 2, 3], 3)
        assert arr.dtype == np.int64
        assert arr.tolist() == [1, 2, 3]

    def test_nicht_numerisch_wird_object_strings(self):
        arr = _coerce_ids(["a", "b"], 2)
        assert arr.dtype == object
        assert arr.tolist() == ["a", "b"]

    def test_grosse_ints_bleiben_exakt(self):
        big = 2**62 + 1
        arr = _coerce_ids([big], 1)
        assert int(arr[0]) == big
