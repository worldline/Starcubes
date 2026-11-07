"""core/etl.py — Wasserzeichen-, Budget- und Statistik-Helfer (ohne DB)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.etl import (
    EtlAborted,
    _as_naive_utc,
    _build_stats_payload,
    _ceil_second,
    _fmt_rate,
    _preflight_watermark,
    _rate,
    _RunBudget,
    _snapshot_stats,
    _timeout_explanation,
)


class TestCeilSecond:
    def test_ganze_sekunde_bleibt(self):
        dt = datetime(2026, 1, 1, 12, 0, 5, tzinfo=timezone.utc)
        assert _ceil_second(dt) == dt

    def test_mikrosekunden_runden_auf(self):
        dt = datetime(2026, 1, 1, 12, 0, 5, 1, tzinfo=timezone.utc)
        assert _ceil_second(dt) == datetime(
            2026, 1, 1, 12, 0, 6, tzinfo=timezone.utc)

    def test_999999_rundet_auf_naechste_sekunde(self):
        dt = datetime(2026, 1, 1, 12, 0, 5, 999_999, tzinfo=timezone.utc)
        assert _ceil_second(dt) == datetime(
            2026, 1, 1, 12, 0, 6, tzinfo=timezone.utc)

    def test_tageswechsel(self):
        dt = datetime(2026, 1, 1, 23, 59, 59, 5, tzinfo=timezone.utc)
        assert _ceil_second(dt) == datetime(2026, 1, 2, tzinfo=timezone.utc)


class TestAsNaiveUtc:
    def test_offset_wird_utc_und_naiv(self):
        dt = datetime(2026, 1, 1, 14, tzinfo=timezone(timedelta(hours=2)))
        assert _as_naive_utc(dt) == datetime(2026, 1, 1, 12)

    def test_naiv_bleibt(self):
        assert _as_naive_utc(datetime(2026, 1, 1, 12)) == datetime(
            2026, 1, 1, 12)


class TestPreflightWatermark:
    def test_gueltige_werte(self):
        report = {"watermark_probe": {
            "max_wm": "2026-01-01T12:00:00Z",
            "src_now": "2026-01-01T12:05:00Z",
        }}
        max_wm, src_now = _preflight_watermark(report)
        assert max_wm == datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        assert src_now == datetime(2026, 1, 1, 12, 5, tzinfo=timezone.utc)

    def test_src_now_darf_fehlen(self):
        report = {"watermark_probe": {"max_wm": "2026-01-01T12:00:00Z"}}
        assert _preflight_watermark(report) == (
            datetime(2026, 1, 1, 12, tzinfo=timezone.utc), None)

    @pytest.mark.parametrize("report", [
        None, {}, {"watermark_probe": {}},
        {"watermark_probe": {"max_wm": ""}},
        {"watermark_probe": {"max_wm": "kein Datum"}},
    ])
    def test_unbrauchbar_none(self, report):
        assert _preflight_watermark(report) is None


class _Orig(Exception):
    def __init__(self, msg, pgcode=None):
        super().__init__(msg)
        self.pgcode = pgcode


class TestTimeoutExplanation:
    def test_andere_fehler_geben_none(self):
        assert _timeout_explanation(ValueError("kaputt"), False, 60) is None

    def test_postgres_code_mit_budget(self):
        msg = _timeout_explanation(_Orig("x", "57014"), True, 300)
        assert msg is not None
        assert "max_run_s=300s" in msg
        assert "retrying will not help" in msg

    def test_postgres_code_ohne_budget(self):
        msg = _timeout_explanation(_Orig("x", "57014"), False, 300)
        assert msg is not None
        assert "statement_timeout" in msg
        assert "max_run_s is not capping" in msg

    def test_meldungstext_zaehlt_auch(self):
        assert _timeout_explanation(
            Exception("canceling statement due to statement timeout"),
            False, 60) is not None

    def test_oracle_code(self):
        assert _timeout_explanation(
            Exception("ORA-01013: user requested cancel"), False, 60) is not None

    def test_orig_attribut_wird_ausgepackt(self):
        exc = Exception("outer")
        exc.orig = _Orig("x", "57014")
        assert _timeout_explanation(exc, True, 60) is not None


class TestRunBudget:
    def test_ohne_frist_laeuft(self):
        _RunBudget("s", 0).check()

    def test_negative_sekunden_bedeuten_keine_frist(self):
        _RunBudget("s", -10).check()

    def test_interrupt_wirft(self):
        b = _RunBudget("s", 0)
        b.interrupt("Lease verloren")
        with pytest.raises(EtlAborted, match="Lease verloren"):
            b.check()

    def test_abgelaufene_frist_wirft(self):
        import time
        b = _RunBudget("s", 60)
        b.deadline = time.monotonic() - 1
        with pytest.raises(EtlAborted, match="max_run_s=60s"):
            b.check()

    def test_interrupt_gewinnt_gegen_frist(self):
        import time
        b = _RunBudget("s", 60)
        b.deadline = time.monotonic() + 100
        b.interrupt("manuell")
        with pytest.raises(EtlAborted, match="manuell"):
            b.check()


class TestRates:
    def test_rate(self):
        assert _rate(100, 10) == 10.0
        assert _rate(100, 0) is None
        assert _rate(100, -1) is None

    @pytest.mark.parametrize("value,expected", [
        (None, "?"),
        (0, "0/s"),
        (999, "999/s"),
        (1000, "1k/s"),
        (1_500_000, "1.5M/s"),
    ])
    def test_fmt_rate(self, value, expected):
        assert _fmt_rate(value) == expected


def _stats_array(*slots):
    """Baut das flache Array: pro Slot (rows, busy, wait)."""
    out = []
    for rows, busy, wait in slots:
        out.extend([float(rows), float(busy), float(wait)])
    return out


class TestStatsPayload:
    def test_snapshot_layout(self):
        # ein Load-Slot, zwei Transform-Slots, ein Extract-Slot
        arr = _stats_array((100, 10, 5), (40, 4, 1), (60, 6, 2), (200, 20, 3))
        snap = _snapshot_stats(arr, load_base=0,
                               transform_bases=[3, 6], extract_base=9,
                               n_transform=2)
        assert snap["load"]["rows"] == 100
        assert snap["load"]["rows_s"] == 10.0
        assert [p["rows"] for p in snap["transform"]["procs"]] == [40, 60]
        # busy_s ist das Maximum, nicht die Summe.
        assert snap["transform"]["busy_s"] == 6.0
        assert snap["transform"]["rows"] == 100
        # wait_s dagegen summiert.
        assert snap["transform"]["wait_s"] == 3.0
        assert snap["extract"]["rows"] == 200

    def test_final_payload_ueberschreibt_extract_und_setzt_total(self):
        arr = _stats_array((100, 10, 5), (40, 4, 1), (200, 20, 3))
        payload = _build_stats_payload(
            arr, load_base=0, transform_bases=[3], extract_base=6,
            n_transform=1, extract_busy=25.0, extract_wait=2.0,
            extract_auto_dim_s=1.5, duplicates=7, auto_dim_inserted=3,
            runtime_ms=2500, rows_total=100, live=False,
            auto_dim_capped={"host": 2})
        assert payload["extract"]["busy_s"] == 25.0
        assert payload["extract"]["rows_s"] == 4.0
        assert payload["extract"]["dedupe_rows"] == 7
        assert payload["extract"]["auto_dim_capped"] == {"host": 2}
        assert payload["total"]["wall_s"] == 2.5
        assert payload["total"]["rows_s"] == 40.0

    def test_live_liest_nur_den_worker_slot(self):
        arr = _stats_array((100, 10, 5), (40, 4, 1), (200, 20, 3))
        payload = _build_stats_payload(
            arr, load_base=0, transform_bases=[3], extract_base=6,
            n_transform=1, extract_busy=999.0, extract_wait=999.0,
            extract_auto_dim_s=0.0, duplicates=0, auto_dim_inserted=0,
            runtime_ms=1, rows_total=100, live=True)
        assert payload["extract"]["busy_s"] == 20.0
        assert "total" not in payload

    def test_log_reader_report(self):
        class Reader:
            parsed_rows = 10
            unparsed_rows = 2
            match_pct = 83.3333
            files_read = 3

        arr = _stats_array((0, 0, 0), (0, 0, 0), (0, 0, 0))
        payload = _build_stats_payload(
            arr, load_base=0, transform_bases=[3], extract_base=6,
            n_transform=1, extract_busy=0.0, extract_wait=0.0,
            extract_auto_dim_s=0.0, duplicates=0, auto_dim_inserted=0,
            runtime_ms=1, rows_total=12, live=False, log_reader=Reader())
        ext = payload["extract"]
        assert ext["parsed_rows"] == 10
        assert ext["unparsed_rows"] == 2
        assert ext["match_pct"] == 83.333
        assert ext["files_read"] == 3

    def test_log_reader_ohne_lesung_match_pct_none(self):
        class Reader:
            parsed_rows = 0
            unparsed_rows = 0
            match_pct = None
            files_read = 0

        arr = _stats_array((0, 0, 0), (0, 0, 0), (0, 0, 0))
        payload = _build_stats_payload(
            arr, load_base=0, transform_bases=[3], extract_base=6,
            n_transform=1, extract_busy=0.0, extract_wait=0.0,
            extract_auto_dim_s=0.0, duplicates=0, auto_dim_inserted=0,
            runtime_ms=1, rows_total=0, live=False, log_reader=Reader())
        assert payload["extract"]["match_pct"] is None
