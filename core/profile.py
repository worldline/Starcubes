"""Example-profile loader.

Profiles are YAML files under ``examples/<name>.yaml`` at the project root.
They describe an example datasource (fact columns, dimensions, measures,
indices, plus the source-side scalars) that ``manage.py seed_example`` and
the ``/sources/new/`` "Preload example Oracle configuration" checkbox can
both seed into ``starcubes_meta`` without hard-coding the column lists in
Python.

The profile is **meta only**: it carries no example fact rows. Dimension
content still comes from the Oracle source (via
``core.schema.import_dimensions``) or from an inline CSV (the loader
resolves the path so the seed can pass ``inline_values`` straight through
to ``starcubes_meta.dimension``).

No Django imports — same rule as ``core/etl.py``, ``core/source.py``, etc.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


# Project-root path layout: ``core/profile.py`` lives at
# ``<root>/core/profile.py``, so ``parents[1]`` is the root. ``examples/``
# sits next to ``iso4217.csv`` and ``examples.sql``.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_EXAMPLES_DIR = _PROJECT_ROOT / "examples"


# Roles that ``starcubes_meta.fact_column.role`` accepts. ``core.source.Source``
# uses these as opaque strings; they are validated here so a typo in the
# YAML fails fast at seed time rather than mid-ETL.
_VALID_ROLES = {"time", "pk", "measure", "dimension", "auto-dimension"}

# ``fact_index.method`` — keep aligned with ``sources/views.py:INDEX_METHODS``.
_VALID_INDEX_METHODS = {"brin", "btree", "hash"}

# ``measure.graph_style`` — keep aligned with ``sources/views.py:GRAPH_STYLES``.
_VALID_GRAPH_STYLES = {
    "multibar", "multiline", "multipoint",
    "quantile-bands", "quantile-steps", "quantile-steps-open",
    "error-bars", "candlestick",
}


class ProfileError(ValueError):
    """Raised when a profile YAML is malformed or violates a schema rule."""


@dataclass(frozen=True)
class FactColumn:
    name: str
    pg_type: str
    role: str
    ordinal: int
    source_expr: str


@dataclass(frozen=True)
class Dimension:
    fact_column: str
    dim_table: str
    source_table: str | None
    source_key: str | None
    source_name: str | None
    source_description: str | None
    is_primary: bool
    # Inline-CSV payload (verbatim CSV text), or ``None`` for source-loaded.
    inline_values: str | None


@dataclass(frozen=True)
class FactIndex:
    column_name: str
    method: str


@dataclass(frozen=True)
class Measure:
    name: str
    agg: str
    graph_style: str
    is_default: bool
    ordinal: int
    percentiles: str | None


@dataclass(frozen=True)
class ExampleProfile:
    """In-memory representation of one ``examples/<name>.yaml`` file."""

    name: str
    description: str
    source_dialect: str
    # Source-side scalars (base_table, watermark_*, small_window_hint, …).
    source: dict[str, Any]
    partition: dict[str, Any]
    runtime_defaults: dict[str, Any]
    fact_columns: list[FactColumn] = field(default_factory=list)
    dimensions: list[Dimension] = field(default_factory=list)
    fact_indices: list[FactIndex] = field(default_factory=list)
    measures: list[Measure] = field(default_factory=list)

    def fact_columns_as_tuples(self) -> list[tuple[str, str, str, int, str]]:
        """Match the historical ``(name, pg_type, role, ordinal, source_expr)``
        shape consumed by ``_upsert_datasource`` in seed_example.py."""
        return [(fc.name, fc.pg_type, fc.role, fc.ordinal, fc.source_expr)
                for fc in self.fact_columns]

    def dimensions_as_tuples(self) -> list[tuple]:
        """Match the historical ``(fact_col, dim_table, src_table, src_key,
        src_name, src_desc, is_primary, inline_values)`` shape."""
        return [(d.fact_column, d.dim_table, d.source_table, d.source_key,
                 d.source_name, d.source_description, d.is_primary,
                 d.inline_values)
                for d in self.dimensions]

    def fact_indices_as_tuples(self) -> list[tuple[str, str]]:
        """Match the historical ``(column_name, method)`` shape."""
        return [(fi.column_name, fi.method) for fi in self.fact_indices]

    def measures_as_tuples(self) -> list[tuple]:
        """Match the historical ``(name, agg, graph_style, is_default,
        ordinal, percentiles)`` shape."""
        return [(m.name, m.agg, m.graph_style, m.is_default, m.ordinal,
                 m.percentiles)
                for m in self.measures]


def _resolve_inline_csv(name: str) -> str:
    """Resolve a profile-relative CSV filename to a verified path and read it.

    Same trick as the historical ``_ISO4217_CSV`` in ``seed_example.py:86-87``:
    load at profile-load time so the consumer sees a populated string instead
    of a path it has to re-read.

    ``name`` is the basename as written in the YAML (e.g. ``iso4217.csv``);
    the file is looked up in the project root. Profile authors may also use
    paths relative to the profile file itself — see ``_resolve_csv_path``.
    """
    path = _resolve_csv_path(name)
    if not path.is_file():
        raise ProfileError(
            f"inline_dimensions.csv={name!r} resolved to {path}, which is "
            f"not a regular file")
    return path.read_text()


def _resolve_csv_path(name: str) -> Path:
    """Allow either project-root-relative or profile-dir-relative paths.

    Most users will write ``iso4217.csv`` (project root, where it already
    lives). Profiles shipped under ``examples/`` may also reference files
    inside their own directory with a ``./`` or a sibling basename.
    """
    p = Path(name)
    if p.is_absolute():
        return p
    if p.parts and p.parts[0] == "examples":
        return _PROJECT_ROOT / p
    # Try the examples/ directory first (siblings of the profile); fall back
    # to project root.
    candidate_profile_dir = _EXAMPLES_DIR / p.name
    if candidate_profile_dir.is_file():
        return candidate_profile_dir
    candidate_root = _PROJECT_ROOT / p.name
    if candidate_root.is_file():
        return candidate_root
    # Return the most likely location for the error message; ``_resolve_inline_csv``
    # will then report "not a regular file".
    return candidate_root


def iter_profiles() -> list[str]:
    """Return the names of every profile shipped under ``examples/``.

    The name is the basename without ``.yaml``.
    """
    if not _EXAMPLES_DIR.is_dir():
        return []
    return sorted(p.stem for p in _EXAMPLES_DIR.glob("*.yaml")
                  if p.is_file())


def load_profile(name: str = "oracle_payments") -> ExampleProfile:
    """Load and validate ``examples/<name>.yaml`` into an ``ExampleProfile``."""
    path = _EXAMPLES_DIR / f"{name}.yaml"
    if not path.is_file():
        available = iter_profiles() or ["<none — examples/ directory missing>"]
        raise ProfileError(
            f"profile {name!r} not found at {path}. "
            f"Available profiles: {available}")
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ProfileError(f"profile {name!r} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ProfileError(
            f"profile {name!r} must be a YAML mapping at the top level, "
            f"got {type(raw).__name__}")
    return _parse_profile(name, raw)


def _parse_profile(name: str, raw: dict[str, Any]) -> ExampleProfile:
    """Validate the parsed YAML dict and return an ``ExampleProfile``."""
    if raw.get("profile") and raw["profile"] != name:
        # Profile self-identifies with a different name than its filename.
        # Allow, but record the filename (the ``name`` arg) as canonical so
        # ``--profile`` always wins.
        pass

    src = raw.get("source") or {}
    if not isinstance(src, dict):
        raise ProfileError(f"profile {name!r}: 'source' must be a mapping")
    if "dialect" not in src:
        raise ProfileError(f"profile {name!r}: 'source.dialect' is required")
    if "base_table" not in src:
        raise ProfileError(f"profile {name!r}: 'source.base_table' is required")
    if "watermark_column" not in src:
        raise ProfileError(
            f"profile {name!r}: 'source.watermark_column' is required")

    partition = raw.get("partition") or {}
    if "column" not in partition:
        raise ProfileError(f"profile {name!r}: 'partition.column' is required")
    if "period" not in partition:
        raise ProfileError(f"profile {name!r}: 'partition.period' is required")

    runtime = dict(raw.get("runtime_defaults") or {})

    fact_columns = [_parse_fact_column(name, i, fc)
                    for i, fc in enumerate(raw.get("fact_columns") or [])]
    dimensions = [_parse_dimension(name, i, d)
                  for i, d in enumerate(raw.get("dimensions") or [])]
    fact_indices = [_parse_fact_index(name, i, fi)
                    for i, fi in enumerate(raw.get("fact_indices") or [])]
    measures = [_parse_measure(name, i, m)
                for i, m in enumerate(raw.get("measures") or [])]

    # Resolve inline-CSV payloads now so the consumer (seed_example,
    # _insert_seed_children) gets a populated string. This mirrors the
    # historical "load at import time" pattern in seed_example.py:86-87.
    inline_defs = raw.get("inline_dimensions") or {}
    if not isinstance(inline_defs, dict):
        raise ProfileError(
            f"profile {name!r}: 'inline_dimensions' must be a mapping")
    inline_by_dim: dict[str, str] = {}
    for dim_table, spec in inline_defs.items():
        if not isinstance(spec, dict) or "csv" not in spec:
            raise ProfileError(
                f"profile {name!r}: inline_dimensions.{dim_table} must be "
                f"a mapping with a 'csv' key")
        inline_by_dim[dim_table] = _resolve_inline_csv(spec["csv"])

    # Splice the resolved CSV text into the matching Dimension entries.
    filled_dimensions: list[Dimension] = []
    for dim in dimensions:
        if dim.source_table is None and dim.dim_table in inline_by_dim:
            dim = Dimension(
                fact_column=dim.fact_column, dim_table=dim.dim_table,
                source_table=dim.source_table, source_key=dim.source_key,
                source_name=dim.source_name,
                source_description=dim.source_description,
                is_primary=dim.is_primary,
                inline_values=inline_by_dim[dim.dim_table],
            )
        filled_dimensions.append(dim)

    return ExampleProfile(
        name=name,
        description=str(raw.get("description") or "").strip(),
        source_dialect=str(src["dialect"]),
        source=dict(src),
        partition=dict(partition),
        runtime_defaults=runtime,
        fact_columns=fact_columns,
        dimensions=filled_dimensions,
        fact_indices=fact_indices,
        measures=measures,
    )


def _parse_fact_column(profile: str, idx: int, raw: Any) -> FactColumn:
    if not isinstance(raw, dict):
        raise ProfileError(
            f"profile {profile!r}: fact_columns[{idx}] must be a mapping")
    try:
        role = str(raw["role"])
    except KeyError:
        raise ProfileError(
            f"profile {profile!r}: fact_columns[{idx}].role is required")
    if role not in _VALID_ROLES:
        raise ProfileError(
            f"profile {profile!r}: fact_columns[{idx}].role={role!r} is "
            f"not one of {sorted(_VALID_ROLES)}")
    try:
        return FactColumn(
            name=str(raw["name"]),
            pg_type=str(raw["pg_type"]),
            role=role,
            ordinal=int(raw["ordinal"]),
            source_expr=str(raw["source_expr"]),
        )
    except KeyError as exc:
        raise ProfileError(
            f"profile {profile!r}: fact_columns[{idx}] missing {exc.args[0]!r}")


def _parse_dimension(profile: str, idx: int, raw: Any) -> Dimension:
    if not isinstance(raw, dict):
        raise ProfileError(
            f"profile {profile!r}: dimensions[{idx}] must be a mapping")
    try:
        fact_column = str(raw["fact_column"])
        dim_table = str(raw["dim_table"])
    except KeyError as exc:
        raise ProfileError(
            f"profile {profile!r}: dimensions[{idx}] missing {exc.args[0]!r}")
    # source_table / source_key / source_name / source_description are
    # nullable for inline dimensions; ``None`` is encoded as ``null`` in YAML.
    return Dimension(
        fact_column=fact_column,
        dim_table=dim_table,
        source_table=raw.get("source_table"),
        source_key=raw.get("source_key"),
        source_name=raw.get("source_name"),
        source_description=raw.get("source_description"),
        is_primary=bool(raw.get("is_primary", False)),
        inline_values=None,  # populated later from inline_dimensions
    )


def _parse_fact_index(profile: str, idx: int, raw: Any) -> FactIndex:
    if not isinstance(raw, dict):
        raise ProfileError(
            f"profile {profile!r}: fact_indices[{idx}] must be a mapping")
    try:
        method = str(raw["method"])
    except KeyError:
        raise ProfileError(
            f"profile {profile!r}: fact_indices[{idx}].method is required")
    if method not in _VALID_INDEX_METHODS:
        raise ProfileError(
            f"profile {profile!r}: fact_indices[{idx}].method={method!r} is "
            f"not one of {sorted(_VALID_INDEX_METHODS)}")
    try:
        return FactIndex(column_name=str(raw["column_name"]), method=method)
    except KeyError as exc:
        raise ProfileError(
            f"profile {profile!r}: fact_indices[{idx}] missing {exc.args[0]!r}")


def _parse_measure(profile: str, idx: int, raw: Any) -> Measure:
    if not isinstance(raw, dict):
        raise ProfileError(
            f"profile {profile!r}: measures[{idx}] must be a mapping")
    try:
        graph_style = str(raw["graph_style"])
    except KeyError:
        raise ProfileError(
            f"profile {profile!r}: measures[{idx}].graph_style is required")
    if graph_style not in _VALID_GRAPH_STYLES:
        raise ProfileError(
            f"profile {profile!r}: measures[{idx}].graph_style={graph_style!r} "
            f"is not one of {sorted(_VALID_GRAPH_STYLES)}")
    try:
        return Measure(
            name=str(raw["name"]),
            agg=str(raw["agg"]),
            graph_style=graph_style,
            is_default=bool(raw["is_default"]),
            ordinal=int(raw["ordinal"]),
            percentiles=raw.get("percentiles"),
        )
    except KeyError as exc:
        raise ProfileError(
            f"profile {profile!r}: measures[{idx}] missing {exc.args[0]!r}")