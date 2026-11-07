"""core/stream_stats.py — Statistik-Bausteine ohne Datenbank.

`check_stream_stats` prüft das Gesamtverhalten an sechs synthetischen
Strömen; hier stehen die einzelnen Verteilungs- und Fensterfunktionen.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import numpy as np
import pytest

from core.stream_stats import (
    MAX_WINDOW_S,
    MIN_WINDOW_S,
    _bonferroni,
    _chi2_sf,
    _fmt_period,
    _kolmogorov_q,
    _log_p0_rse,
    _norm_sf,
    _smooth,
    cluster_estimate,
    contiguous_runs,
    dispersion,
    equal_mu_bins,
    mean_rate,
    pick_reading,
    plan_scan,
    stream_window,
)

H = 3600
D = 86400


class TestNormalTail:
    def test_mitte(self):
        assert _norm_sf(0) == pytest.approx(0.5)

    def test_symmetrie(self):
        assert _norm_sf(-1) == pytest.approx(1 - _norm_sf(1))

    def test_weit_rechts(self):
        assert _norm_sf(10) == pytest.approx(0.0, abs=1e-20)


class TestChi2:
    def test_ungueltige_df(self):
        assert math.isnan(_chi2_sf(1, 0))
        assert math.isnan(_chi2_sf(1, -2))

    def test_x_kleiner_gleich_null(self):
        assert _chi2_sf(0, 2) == 1.0
        assert _chi2_sf(-1, 2) == 1.0

    def test_exakt_df2(self):
        assert _chi2_sf(2, 2) == pytest.approx(math.exp(-1))

    def test_exakt_df4(self):
        assert _chi2_sf(2, 4) == pytest.approx(2 * math.exp(-1))

    def test_ungerade_df_wilson_hilferty(self):
        # 3.841 ist das 95%-Quantil von chi2_1.
        assert _chi2_sf(3.841, 1) == pytest.approx(0.05, abs=0.01)

    def test_monoton_fallend(self):
        assert _chi2_sf(1, 10) > _chi2_sf(5, 10) > _chi2_sf(20, 10)


class TestKolmogorov:
    def test_kleines_lambda_ist_eins(self):
        assert _kolmogorov_q(0) == 1.0
        assert _kolmogorov_q(0.1) == 1.0

    def test_grosses_lambda_geht_gegen_null(self):
        assert _kolmogorov_q(10) == pytest.approx(0.0, abs=1e-10)

    def test_im_wertebereich(self):
        assert 0.0 <= _kolmogorov_q(0.5) <= 1.0


class TestBonferroni:
    def test_ohne_trials_unveraendert(self):
        assert _bonferroni(0.01, 1) == 0.01
        assert _bonferroni(0.01, 0) == 0.01

    def test_kleines_p_wird_multipliziert(self):
        assert _bonferroni(1e-9, 10) == pytest.approx(1e-8)

    def test_grosses_p_geht_ueber_die_gegenwahrscheinlichkeit(self):
        assert _bonferroni(0.01, 10) == pytest.approx(1 - 0.99 ** 10)

    def test_nan_bleibt_nan(self):
        assert math.isnan(_bonferroni(float("nan"), 10))


class TestFmtPeriod:
    @pytest.mark.parametrize("seconds,expected", [
        (0, "0 s"),
        (30, "30 s"),
        (89, "89 s"),
        (90, "2 min"),
        (3600, "60 min"),
        (5400, "1.5 h"),
        (7200, "2.0 h"),
    ])
    def test_werte(self, seconds, expected):
        assert _fmt_period(seconds) == expected


class TestMeanRate:
    def test_blindes_profil(self):
        assert mean_rate({}) == 0.0
        assert mean_rate({h: 0.0 for h in range(24)}) == 0.0

    def test_mittel_ueber_24_stunden(self):
        # 24 Stunden mit je 1/s → 1.0; nur eine Stunde mit 24 → 1.0.
        assert mean_rate({h: 1.0 for h in range(24)}) == 1.0
        assert mean_rate({12: 24.0}) == 1.0


class TestPlanScan:
    def test_leeres_fenster(self):
        assert plan_scan({}, 100, 100, max_hours=6) == ([], False)

    def test_volle_stunden(self):
        # 01:00:00 bis 02:00:00 → genau eine volle Stunde [01:00, 02:00).
        hours, contiguous = plan_scan({h: 1.0 for h in range(24)},
                                      H, 2 * H, max_hours=6)
        assert hours == [H]
        assert contiguous is True

    def test_duenner_strom_liest_alles_kontiguierlich(self):
        rate = {h: 0.1 for h in range(24)}
        hours, contiguous = plan_scan(rate, 0, 3 * H, max_hours=1)
        assert hours == [0, H, 2 * H]
        assert contiguous is True

    def test_duenner_strom_ignoriert_max_hours(self):
        rate = {h: 0.1 for h in range(24)}
        hours, _ = plan_scan(rate, 0, 10 * H, max_hours=1)
        assert len(hours) == 10

    def test_kaputter_scan_ist_auf_7_tage_begrenzt(self):
        rate = {h: 0.1 for h in range(24)}
        hours, _ = plan_scan(rate, 0, 30 * D, max_hours=1)
        assert len(hours) == MAX_WINDOW_S // H

    def test_geschaeftig_sampelt_ueber_stunden_des_tages(self):
        # Mittel 20/24 > CONTIGUOUS_MAX_RATE_PER_S — also der gesampelte Zweig.
        rate = {12: 20.0}
        # Drei Tage, nur die Stunde 12 hat Verkehr.
        hours, contiguous = plan_scan(rate, 0, 3 * D, max_hours=2)
        # Neueste zuerst ausgewählt, am Ende aber chronologisch sortiert.
        assert hours == [D + 12 * H, 2 * D + 12 * H]
        assert contiguous is False

    def test_blindes_profil_nimmt_alle_hours(self):
        hours, contiguous = plan_scan({}, 0, 2 * H, max_hours=2)
        assert hours == [0, H]
        assert contiguous is True

    def test_round_robin_ueber_mehrere_hods(self):
        rate = {3: 10.0, 15: 10.0}
        hours, _ = plan_scan(rate, 0, 2 * D, max_hours=3)
        # Zwei Runden: erst die neuesten, dann die älteren; Ergebnis sortiert.
        assert hours == [3 * H, D + 3 * H, D + 15 * H]


class TestStreamWindow:
    def _src(self, make_source, floor=None):
        return make_source(retention_floor=floor)

    def test_ohne_grenzen_nimmt_hwm(self, make_source):
        t_to = 10 * D
        t_from, to, notes = stream_window(self._src(make_source), None, None,
                                          t_to)
        assert to == t_to
        assert t_from == t_to - MIN_WINDOW_S
        assert notes == []

    def test_fenster_hinter_hwm_wird_zurueckgezogen(self, make_source):
        hwm = 10 * D
        t_from, to, notes = stream_window(
            self._src(make_source), hwm - H, hwm + 5 * H, hwm)
        assert to == hwm
        assert any("watermark" in n for n in notes)

    def test_zu_langes_fenster_wird_gekappt(self, make_source):
        t_to = 30 * D
        t_from, to, notes = stream_window(
            self._src(make_source), 0, t_to, t_to)
        assert to - t_from == MAX_WINDOW_S
        assert any("7 days" in n for n in notes)

    def test_geschaeftiger_strom_wird_auf_einen_tag_erweitert(self, make_source):
        rate = {h: 1.0 for h in range(24)}
        t_to = 10 * D + 12 * H
        t_from, to, notes = stream_window(
            self._src(make_source), t_to - 2 * H, t_to, t_to,
            rate_per_s=rate)
        assert to - t_from == MIN_WINDOW_S
        assert to == t_to
        assert any("one day" in n for n in notes)

    def test_duenner_strom_wird_auf_sieben_tage_erweitert(self, make_source):
        rate = {h: 0.1 for h in range(24)}
        t_to = 10 * D
        t_from, to, notes = stream_window(
            self._src(make_source), t_to - 2 * H, t_to, t_to,
            rate_per_s=rate)
        assert to - t_from == MAX_WINDOW_S
        assert any("thin" in n for n in notes)

    def test_retention_floor_hebt_den_anfang(self, make_source):
        rate = {h: 1.0 for h in range(24)}
        t_to = 10 * D
        floor = datetime.fromtimestamp(t_to - 2 * D, timezone.utc)
        t_from, to, notes = stream_window(
            make_source(retention_floor=floor), t_to - 3 * D, t_to, t_to,
            rate_per_s=rate)
        assert t_from == int(floor.timestamp())
        assert any("retention floor" in n for n in notes)
        # Die Kapp-/Erweiterungsnotizen sind dann irreführend und weg.
        assert not any(n.startswith(("limited to", "widened")) for n in notes)

    def test_floor_laesst_weniger_als_einen_tag(self, make_source):
        rate = {h: 1.0 for h in range(24)}
        t_to = 10 * D
        floor = datetime.fromtimestamp(t_to - H, timezone.utc)
        t_from, to, notes = stream_window(
            make_source(retention_floor=floor), t_to - 3 * D, t_to, t_to,
            rate_per_s=rate)
        assert t_from == int(floor.timestamp())
        assert to == t_to
        assert any("less than a day" in n for n in notes)


class TestContiguousRuns:
    def test_leer(self):
        assert contiguous_runs(np.array([], dtype=float)) == []

    def test_ununterbrochen(self):
        assert contiguous_runs(np.array([0, 1, 2, 3], dtype=float), 1) == [(0, 4)]

    def test_mit_luecke(self):
        assert contiguous_runs(np.array([0, 1, 5, 6], dtype=float), 1) == [
            (0, 2), (2, 4)]


class TestSmooth:
    def test_leer(self):
        got = _smooth(np.array([], dtype=float), 5)
        assert got.size == 0

    def test_win_1_gibt_mittelwert(self):
        x = np.array([1.0, 2.0, 3.0])
        assert np.allclose(_smooth(x, 1), 2.0)

    def test_win_zu_gross_gibt_mittelwert(self):
        x = np.array([1.0, 2.0, 3.0])
        assert np.allclose(_smooth(x, 99), 2.0)

    def test_raender_werden_nicht_gepolstert(self):
        # Regression: Edge-Padding wiederholte den ersten Wert und blies eine
        # einzelne Spitze am Rand auf. Erwartet: Mittel über die *vorhandenen*
        # Nachbarn (100+0)/2 = 50, nicht (100+100+0)/3.
        x = np.array([100.0, 0.0, 0.0, 0.0, 0.0])
        got = _smooth(x, 3)
        assert got[0] == pytest.approx(50.0)
        assert got[-1] == pytest.approx(0.0)

    def test_laenge_bleibt(self):
        x = np.arange(20, dtype=float)
        assert _smooth(x, 5).size == 20


class TestEqualMuBins:
    def test_mu_null(self):
        assert equal_mu_bins(np.ones(1000), 0) is None

    def test_zu_wenige_bins(self):
        # total=1000, mu=20 → 50 Bins < MIN_BINS.
        assert equal_mu_bins(np.ones(1000), 20) is None

    def test_genau_genug_bins(self):
        idx = equal_mu_bins(np.ones(1000), 10)
        assert idx is not None
        assert len(idx) == 101
        assert idx[0] == 0 and idx[-1] == 1000

    def test_grenzen_sind_monoton(self):
        idx = equal_mu_bins(np.linspace(0, 100, 1001), 1)
        assert idx is not None
        assert np.all(np.diff(idx) >= 0)


class TestDispersion:
    def test_perfekte_poisson_verteilung(self):
        d, chi2, df = dispersion(np.array([10.0, 10.0]),
                                 np.array([10.0, 10.0]))
        assert chi2 == 0.0
        assert d == 0.0
        assert df == 2

    def test_df_cost_wird_abgezogen(self):
        _, _, df = dispersion(np.array([10.0] * 10),
                              np.array([10.0] * 10), df_cost=4.0)
        assert df == 6

    def test_zu_wenige_bins(self):
        d, chi2, df = dispersion(np.array([1.0]), np.array([1.0]))
        assert math.isnan(d) and math.isnan(chi2) and df == 0

    def test_null_erwartung_faellt_raus(self):
        d, _, df = dispersion(np.array([1.0, 2.0]),
                              np.array([0.0, 2.0]))
        # Nur ein Bin mit mu > 0 bleibt — unter zwei ist keine Aussage möglich.
        assert df == 0

    def test_ueberdispersion(self):
        d, _, _ = dispersion(np.array([0.0, 20.0]), np.array([10.0, 10.0]))
        assert d > 1.0


class TestLogP0Rse:
    def test_ungueltig(self):
        assert _log_p0_rse(0.0, 10) is None
        assert _log_p0_rse(1.0, 10) is None
        assert _log_p0_rse(0.5, 1) is None

    def test_formel(self):
        got = _log_p0_rse(0.5, 100)
        expected = math.sqrt((1 - 0.5) / (100 * 0.5)) / (-math.log(0.5))
        assert got == pytest.approx(expected)


class TestClusterEstimate:
    def test_unbrauchbare_eingaben(self):
        for d, p0, mu in [(float("nan"), 0.5, 10), (1.0, 0.0, 10),
                          (1.0, 0.5, 0)]:
            got = cluster_estimate(d, p0, mu)
            assert got == {"cluster_size": None, "batch_size": None,
                           "batch_share": None}

    def test_poisson_hat_keine_batches(self):
        mu = 10.0
        p0 = math.exp(-mu)   # m1 = mu / -ln p0 = 1
        got = cluster_estimate(1.0, p0, mu)
        assert got["cluster_size"] == pytest.approx(1.0)
        assert got["batch_share"] == 0.0
        assert got["batch_size"] == 1.0

    def test_konstruierter_batch(self):
        # m1=10 (jeder Cluster 10 Zeilen), D=10 → m2=100, also B=10 und p=1.
        mu = 10.0
        p0 = math.exp(-1.0)   # -ln p0 = 1 → m1 = 10
        got = cluster_estimate(10.0, p0, mu)
        assert got["cluster_size"] == pytest.approx(10.0)
        assert got["batch_size"] == pytest.approx(10.0, abs=1e-6)
        assert got["batch_share"] == pytest.approx(1.0, abs=1e-6)


class TestPickReading:
    def test_keine_brauchbare_stufe(self):
        entry, why = pick_reading([{"usable": False}])
        assert entry is None
        assert "no scale" in why

    def test_plateau_wird_genommen(self):
        entries = [
            {"usable": True, "batch_share": 0.10},
            {"usable": True, "batch_share": 0.50},
            {"usable": True, "batch_share": 0.52},
            {"usable": True, "batch_share": 0.90},
        ]
        entry, why = pick_reading(entries)
        assert entry is entries[1]
        assert why == "plateau"

    def test_ohne_plateau_die_feinste_stufe(self):
        entries = [
            {"usable": True, "batch_share": 0.1},
            {"usable": True, "batch_share": 0.3},
            {"usable": True, "batch_share": 0.6},
        ]
        entry, why = pick_reading(entries)
        assert entry is entries[0]
        assert "finest" in why
