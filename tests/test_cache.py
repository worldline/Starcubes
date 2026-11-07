"""core/cache.py — Schlüsselbildung und Cache-Eignung.

Der Cache ist ein reiner Beschleuniger; ein falscher Schlüssel liefert
allerdings *fremde* Daten unter der richtigen Antwort, und eine zu großzügige
Eignung friert ein Fenster ein, in das noch Late Arrivals fallen.
"""
from __future__ import annotations

from core.cache import is_cacheable, make_cache_key


def _key(**overrides):
    base = dict(
        datasource_id=1,
        group_by="host",
        measure="count(*)",
        t_from_epoch=1_000,
        t_to_epoch=2_000,
        interval=60,
        filter_tree=None,
        series_label="",
        cache_epoch=7,
    )
    base.update(overrides)
    return make_cache_key(**base)


class TestMakeCacheKey:
    def test_gleich_fuer_gleiche_anfrage(self):
        assert _key() == _key()

    def test_from_wird_auf_intervall_gerundet(self):
        # 960 und 1_019 liegen im selben 60s-Bucket.
        assert _key(t_from_epoch=960) == _key(t_from_epoch=1_019)
        assert _key(t_from_epoch=960) != _key(t_from_epoch=1_020)

    def test_unterschiedliche_intervalle(self):
        assert _key(interval=60) != _key(interval=300)

    def test_unterschiedliche_quelle(self):
        assert _key(datasource_id=1) != _key(datasource_id=2)

    def test_unterschiedliche_gruppierung(self):
        assert _key(group_by="host") != _key(group_by="path")

    def test_unterschiedliche_measure(self):
        assert _key(measure="count(*)") != _key(measure="sum(val)")

    def test_leere_measure_ist_count_star(self):
        assert _key(measure=None) == _key(measure="count(*)")
        assert _key(measure="") == _key(measure="count(*)")

    def test_filter_gehoert_zum_schluessel(self):
        assert _key(filter_tree=None) != _key(
            filter_tree=[{"col": "host", "op": "=", "vals": ["a"]}])

    def test_cache_epoch_gehoert_zum_schluessel(self):
        assert _key(cache_epoch=1) != _key(cache_epoch=2)

    def test_kind_trennt_schluesselraeume(self):
        assert _key(kind="plot") != _key(kind="gap_profile")

    def test_hex_sha256(self):
        key = _key()
        assert len(key) == 64
        int(key, 16)


class TestIsCacheable:
    def _ok(self, **overrides):
        base = dict(
            measure="count(*)",
            interval=60,
            t_to_epoch=10_020,
            hwm_epoch=10_020,
            now_epoch=20_000,
            late_arrival_overlap_s=0,
        )
        base.update(overrides)
        return is_cacheable(**base)

    def test_gueltiger_fall(self):
        assert self._ok() is True

    def test_nur_count_star(self):
        assert self._ok(measure=None) is True
        assert self._ok(measure="") is True
        assert self._ok(measure="sum(val)") is False

    def test_ohne_hwm_nicht_cachebar(self):
        assert self._ok(hwm_epoch=None) is False

    def test_intervall_muss_positiv_sein(self):
        assert self._ok(interval=0) is False
        assert self._ok(interval=-60) is False

    def test_to_muss_auf_intervallgrenze_liegen(self):
        assert self._ok(t_to_epoch=10_010, interval=60) is False
        assert self._ok(t_to_epoch=10_020, interval=60) is True

    def test_zu_neue_fenster_nicht_cachebar(self):
        # jetzt - intervall = 19_940.
        assert self._ok(t_to_epoch=19_980, hwm_epoch=20_000,
                        now_epoch=20_000) is False
        # Genau an bzw. unter der Grenze ist ok.
        assert self._ok(t_to_epoch=19_920, hwm_epoch=20_000,
                        now_epoch=20_000) is True

    def test_hinter_dem_hwm_nicht_cachebar(self):
        assert self._ok(t_to_epoch=10_080, hwm_epoch=10_020) is False
        assert self._ok(t_to_epoch=10_020, hwm_epoch=10_020) is True

    def test_late_arrival_schiebt_die_grenze_nach_hinten(self):
        # Ohne Overlap wäre 10_020 cachebar; mit 180s Overlap muss das Fenster
        # unter 10_020 - 180 = 9_840 liegen.
        assert self._ok(t_to_epoch=9_900, hwm_epoch=10_020,
                        late_arrival_overlap_s=0) is True
        assert self._ok(t_to_epoch=9_900, hwm_epoch=10_020,
                        late_arrival_overlap_s=180) is False
        assert self._ok(t_to_epoch=9_840, hwm_epoch=10_020,
                        late_arrival_overlap_s=180) is True

    def test_negative_overlap_wird_geklemmt(self):
        assert self._ok(late_arrival_overlap_s=-500) is True
