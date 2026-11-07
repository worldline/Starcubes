"""core/profile.py — YAML-Parsing, Validierung und Export-Roundtrip."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from core.profile import (
    ProfileError,
    dump_profile,
    parse_profile_text,
    validate_profile_name,
)

EXAMPLES = Path(__file__).resolve().parent.parent / "profiles" / "examples"


class TestValidateProfileName:
    @pytest.mark.parametrize("name", ["oracle_payments", "a", "A-b_1",
                                      "x" * 64, "1abc"])
    def test_gueltig(self, name):
        assert validate_profile_name(name) == name

    @pytest.mark.parametrize("name", ["", "mit leerzeichen", "a/b", "a.b",
                                      "-abc", "x" * 65, None, 12])
    def test_ungueltig(self, name):
        with pytest.raises(ProfileError):
            validate_profile_name(name)


class TestParseExamples:
    @pytest.mark.parametrize("path", sorted(EXAMPLES.glob("*.yaml")))
    def test_jedes_beispiel_parst(self, path):
        prof = parse_profile_text(path.read_text(), path.stem)
        assert prof.name == path.stem
        assert prof.source_dialect in ("logfile", "postgresql", "oracle")

    def test_nginx_transform_paar(self):
        prof = parse_profile_text(
            (EXAMPLES / "nginx_combined.yaml").read_text(), "nginx_combined")
        assert prof.source_dialect == "logfile"
        path_col = next(c for c in prof.fact_columns if c.name == "path")
        assert path_col.transform_pattern == "^(/[^/?]*).*$"
        assert path_col.transform_replace == r"\1"
        bytes_col = next(c for c in prof.fact_columns if c.name == "bytes")
        assert bytes_col.transform_pattern == "^-$"
        assert bytes_col.transform_replace == "0"

    def test_fact_columns_tuples_sind_sieben_stellig(self):
        prof = parse_profile_text(
            (EXAMPLES / "nginx_combined.yaml").read_text(), "nginx_combined")
        for row in prof.fact_columns_as_tuples():
            assert len(row) == 7


class TestParseErrors:
    def _minimal(self, **over):
        doc = {
            "source": {"dialect": "postgresql", "base_table": "t",
                       "watermark_column": "ts"},
            "partition": {"column": "ts", "period": "day"},
            "fact_columns": [], "dimensions": [],
        }
        doc.update(over)
        return yaml.safe_dump(doc)

    def test_kein_yaml(self):
        with pytest.raises(ProfileError, match="not valid YAML"):
            parse_profile_text("a: [", "x")

    def test_kein_mapping(self):
        with pytest.raises(ProfileError, match="must be a YAML mapping"):
            parse_profile_text("- 1\n- 2\n", "x")

    def test_fehlende_source_keys(self):
        text = yaml.safe_dump({
            "source": {"dialect": "postgresql", "watermark_column": "ts"},
            "partition": {"column": "ts", "period": "day"},
        })
        with pytest.raises(ProfileError, match="base_table"):
            parse_profile_text(text, "x")

    def test_logquelle_braucht_regex(self):
        text = yaml.safe_dump({
            "source": {"dialect": "logfile", "watermark_column": "ts"},
            "partition": {"column": "ts", "period": "day"},
        })
        with pytest.raises(ProfileError, match="log_line_regex"):
            parse_profile_text(text, "x")

    def test_fehlende_partition(self):
        text = yaml.safe_dump({
            "source": {"dialect": "postgresql", "base_table": "t",
                       "watermark_column": "ts"},
            "partition": {"column": "ts"},
        })
        with pytest.raises(ProfileError, match="partition.period"):
            parse_profile_text(text, "x")

    def test_ungueltige_periode(self):
        text = yaml.safe_dump({
            "source": {"dialect": "postgresql", "base_table": "t",
                       "watermark_column": "ts"},
            "partition": {"column": "ts", "period": "year"},
        })
        with pytest.raises(ProfileError, match="partition.period"):
            parse_profile_text(text, "x")

    def test_ladder_agg_braucht_ladder_style(self):
        text = self._minimal(measures=[{
            "name": "x", "agg": "quantile(v)", "graph_style": "multibar",
            "is_default": True, "ordinal": 1}])
        with pytest.raises(ProfileError, match="pairs"):
            parse_profile_text(text, "x")

    def test_extent_nicht_als_candlestick(self):
        text = self._minimal(measures=[{
            "name": "x", "agg": "extent(v)", "graph_style": "candlestick",
            "is_default": True, "ordinal": 1}])
        with pytest.raises(ProfileError, match="cannot draw an extent"):
            parse_profile_text(text, "x")

    def test_group_dim_unbekannte_dimension(self):
        text = self._minimal(
            dimensions=[{
                "fact_column": "host", "dim_table": "host",
                "source_table": None, "source_key": None, "source_name": None,
                "source_description": None, "is_primary": True}],
            group_dims=[{
                "dimension": "fehlt", "name": "g", "slug": "g",
                "position": 0, "groups": []}])
        with pytest.raises(ProfileError, match="not one of this profile"):
            parse_profile_text(text, "x")


class TestDumpRoundTrip:
    def _ds(self):
        return {
            "source_dialect": "logfile",
            "base_table": None,
            "watermark_column": "ts",
            "log_line_regex": r"^(?P<ts>\S+) (?P<val>\d+)$",
            "log_time_format": "%Y-%m-%dT%H:%M:%S%z",
            "log_time_zone": "UTC",
            "late_arrival_overlap_s": 0,
            "partition_column": "ts",
            "partition_period": "month",
            "chunksize": 1000,
            "transform_procs": 1,
            "interval_ladder": [1, 60, 3600],
            "bar_pixel_target": 5,
            "name": "meine_quelle",
            "schema_name": "sc_quelle",
            "source_dsn": "file:///geheim/*.log",
        }

    def _facts(self):
        return [
            {"name": "ts", "pg_type": "timestamptz", "role": "time",
             "ordinal": 1, "source_expr": "ts"},
            {"name": "val", "pg_type": "integer", "role": "measure",
             "ordinal": 2, "source_expr": "val",
             "transform_pattern": "^-$", "transform_replace": "0"},
        ]

    def test_roundtrip(self):
        text = dump_profile(self._ds(), self._facts(), [], [], [], [],
                            name="mein_profil", description="Beschreibung")
        prof = parse_profile_text(text, "mein_profil")
        assert prof.name == "mein_profil"
        assert prof.description == "Beschreibung"
        assert prof.source_dialect == "logfile"
        assert prof.source["log_line_regex"] == r"^(?P<ts>\S+) (?P<val>\d+)$"
        assert prof.partition == {"column": "ts", "period": "month"}
        assert prof.runtime_defaults["chunksize"] == 1000
        assert prof.fact_columns[1].transform_pattern == "^-$"
        assert prof.fact_columns[1].transform_replace == "0"

    def test_transform_paar_nur_wenn_gesetzt(self):
        text = dump_profile(self._ds(), self._facts(), [], [], [], [],
                            name="p")
        # Die erste Spalte hat kein Transform-Paar — es darf nicht als
        # null/null im YAML auftauchen.
        doc = yaml.safe_load(text)
        assert "transform_pattern" not in doc["fact_columns"][0]
        assert doc["fact_columns"][1]["transform_pattern"] == "^-$"

    def test_source_dsn_wird_nie_exportiert(self):
        text = dump_profile(self._ds(), self._facts(), [], [], [], [],
                            name="p")
        assert "source_dsn" not in text
        assert "geheim" not in text

    def test_indizes_und_measures_sortiert(self):
        text = dump_profile(
            self._ds(), self._facts(),
            fact_indices=[{"column_name": "ts", "method": "brin"},
                          {"column_name": "a", "method": "btree"}],
            measures=[
                {"name": "b", "agg": "count(*)", "graph_style": "multibar",
                 "is_default": False, "ordinal": 2, "percentiles": None},
                {"name": "a", "agg": "count(*)", "graph_style": "multibar",
                 "is_default": True, "ordinal": 1, "percentiles": None},
            ],
            name="p")
        doc = yaml.safe_load(text)
        assert [m["name"] for m in doc["measures"]] == ["a", "b"]
        assert [i["column_name"] for i in doc["fact_indices"]] == ["a", "ts"]

    def test_inline_dimension_wandert_in_eigenen_block(self):
        text = dump_profile(
            self._ds(), self._facts(),
            dimensions=[{
                "fact_column": "val", "dim_table": "dim_val",
                "source_table": None, "source_key": None, "source_name": None,
                "source_description": None, "is_primary": True,
                "inline_values": "1,eins\n2,zwei\n"}],
            name="p")
        doc = yaml.safe_load(text)
        assert doc["inline_dimensions"] == {
            "dim_val": {"values": "1,eins\n2,zwei\n"}}
        prof = parse_profile_text(text, "p")
        assert prof.dimensions[0].inline_values == "1,eins\n2,zwei\n"
