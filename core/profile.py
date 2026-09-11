"""Source-profile loader, catalogue and dumper.

A **source profile** is a YAML file describing one datasource's complete
``starcubes_meta`` configuration — the source-side scalars plus its fact
columns, dimensions, indices, measures and group dimensions. Profiles live
in two directories:

``profiles/examples/``
    Shipped with the repo, tracked in git, read-only from the GUI.

``profiles/user/``
    Configurations saved from a running installation. Overridable with
    ``STARCUBES_PROFILE_DIR`` so a container can mount it on a volume.

A name present in both resolves to the user copy, so a site can shadow a
shipped example without editing tracked files.

Profiles are consumed by ``manage.py seed_example --profile <name>``, by the
profile picker in ``/sources/new/``, and by the Apply Profile action on the
datasource detail page. They are produced by ``dump_profile()``, which the
Save as Profile / Download actions call.

The profile is **meta only**: it carries no fact rows. Dimension content
still comes from the source (via ``core.schema.import_dimensions``) or from
an inline CSV — either referenced by filename (``csv:``) or carried inline
in the YAML itself (``values:``), which is what ``dump_profile`` writes so
an exported profile stays a single self-contained file.

**The source DSN is deliberately never part of a profile.** It is a
deployment secret, not configuration; a profile applies fine without one.

No Django imports — same rule as ``core/etl.py``, ``core/source.py``, etc.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml


# Project-root path layout: ``core/profile.py`` lives at
# ``<root>/core/profile.py``, so ``parents[1]`` is the root. ``profiles/``
# sits next to ``iso4217.csv`` and ``examples.sql``.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_PROFILES_DIR = _PROJECT_ROOT / "profiles"
_EXAMPLES_DIR = _PROFILES_DIR / "examples"

# Saved profiles. Redirectable so a container can point it at a volume —
# ``COPY . /app/`` would otherwise put saved profiles inside the image,
# where the next build discards them. Read from the environment rather than
# Django settings because ``core/`` must not import Django.
_USER_DIR = Path(os.environ.get("STARCUBES_PROFILE_DIR")
                 or (_PROFILES_DIR / "user"))

# Profile names double as filenames, so they are the one piece of
# user-controlled input that reaches the filesystem. Same hardening idea as
# the log-filename check in ``sources/views.py:etl_log_tail``: an explicit
# allowlist, no dots (no ``..``), no separators.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

# Cap on an uploaded/saved profile. Inline dimension CSVs make profiles
# legitimately large (iso4217 is ~180 rows), but not megabytes-large.
MAX_PROFILE_BYTES = 2 * 1024 * 1024


# Roles that ``starcubes_meta.fact_column.role`` accepts. ``core.source.Source``
# uses these as opaque strings; they are validated here so a typo in the
# YAML fails fast at seed time rather than mid-ETL.
_VALID_ROLES = {"time", "pk", "measure", "dimension", "auto-dimension"}

# ``fact_index.method`` — keep aligned with ``sources/views.py:INDEX_METHODS``.
_VALID_INDEX_METHODS = {"brin", "btree", "hash"}

# ``measure.graph_style`` — keep aligned with ``sources/views.py:GRAPH_STYLES``
# and the CHECK constraint on ``starcubes_meta.measure``.
_VALID_GRAPH_STYLES = {
    "multibar", "multiline", "multipoint", "multistep",
    "quantile-bands", "quantile-steps", "quantile-steps-open",
    "error-bars", "candlestick",
}

_VALID_PARTITION_PERIODS = {"day", "week", "month"}

# ``source:`` keys that map 1:1 onto ``starcubes_meta.datasource`` columns.
# Order is the emitted order in ``dump_profile``.
_SOURCE_TEXT_FIELDS = (
    "base_table", "small_window_hint", "watermark_column",
    "watermark_max_query", "watermark_fact_expr", "import_query", "pk_format",
)
_SOURCE_INT_FIELDS = (
    "late_arrival_overlap_s", "watermark_lag_s", "wallclock_lag_s",
)


class ProfileError(ValueError):
    """Raised when a profile YAML is malformed or violates a schema rule."""


@dataclass(frozen=True)
class FactColumn:
    """One ``starcubes_meta.fact_column`` row."""

    name: str
    pg_type: str
    role: str
    ordinal: int
    source_expr: str


@dataclass(frozen=True)
class Dimension:
    """One ``starcubes_meta.dimension`` row.

    ``source_table is None`` marks an inline dimension; ``inline_values``
    then carries the CSV text, resolved at load time from the profile's
    ``inline_dimensions`` block.
    """

    fact_column: str
    dim_table: str
    source_table: str | None
    source_key: str | None
    source_name: str | None
    source_description: str | None
    is_primary: bool
    inline_values: str | None
    filter_group: str | None = None


@dataclass(frozen=True)
class FactIndex:
    """One ``starcubes_meta.fact_index`` row."""

    column_name: str
    method: str


@dataclass(frozen=True)
class Measure:
    """One ``starcubes_meta.measure`` row."""

    name: str
    agg: str
    graph_style: str
    is_default: bool
    ordinal: int
    percentiles: str | None


@dataclass(frozen=True)
class ProfileGroup:
    """One ``starcubes_meta.dim_group`` row plus its members.

    ``members`` are ``dim_group_member.dim_index`` values — positions in the
    *dimension table*, not stable identifiers. See ``ProfileGroupSet``.
    """

    name: str
    color: str
    members: tuple[int, ...]


@dataclass(frozen=True)
class ProfileGroupSet:
    """One ``starcubes_meta.dim_group_set`` with its groups and members.

    ``dimension`` is the carrying dimension's ``fact_column`` — the profile
    has no row ids, so the fact column is what identifies the dimension
    across installations.

    **Portability caveat:** group membership is stored as ``dim_index``,
    which is assigned when dimension values are imported. For an inline
    dimension (values shipped in the profile) those indices are
    reproducible. For a dimension imported from a live source they are
    not — the same group set applied elsewhere may then bucket the wrong
    values. ``sources/views.py:profile_apply`` surfaces this as a warning.
    """

    dimension: str
    name: str
    slug: str
    position: int
    groups: list[ProfileGroup] = field(default_factory=list)


@dataclass(frozen=True)
class ExampleProfile:
    """In-memory representation of one profile YAML file."""

    name: str
    description: str
    source_dialect: str
    # Source-side scalars (base_table, watermark_*, small_window_hint, …).
    source: dict[str, Any]
    partition: dict[str, Any]
    runtime_defaults: dict[str, Any]
    # Suggested (never enforced) identity values for the create form.
    defaults: dict[str, Any] = field(default_factory=dict)
    fact_columns: list[FactColumn] = field(default_factory=list)
    dimensions: list[Dimension] = field(default_factory=list)
    fact_indices: list[FactIndex] = field(default_factory=list)
    measures: list[Measure] = field(default_factory=list)
    group_dims: list[ProfileGroupSet] = field(default_factory=list)

    def fact_columns_as_tuples(self) -> list[tuple[str, str, str, int, str]]:
        """Match the historical ``(name, pg_type, role, ordinal, source_expr)``
        shape consumed by ``_upsert_datasource`` in seed_example.py."""
        return [(fc.name, fc.pg_type, fc.role, fc.ordinal, fc.source_expr)
                for fc in self.fact_columns]

    def dimensions_as_tuples(self) -> list[tuple]:
        """Match the historical ``(fact_col, dim_table, src_table, src_key,
        src_name, src_desc, is_primary, inline_values, filter_group)`` shape."""
        return [(d.fact_column, d.dim_table, d.source_table, d.source_key,
                 d.source_name, d.source_description, d.is_primary,
                 d.inline_values, d.filter_group)
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

    def datasource_scalars(self) -> dict[str, Any]:
        """The profile's ``starcubes_meta.datasource`` column values.

        Flattens ``source`` / ``partition`` / ``runtime_defaults`` into the
        column names both the GUI create form and ``_upsert_datasource``
        use, so neither has to know which YAML block a field came from.
        Keys absent from the profile are absent here too — the caller keeps
        whatever it already had.
        """
        out: dict[str, Any] = {"source_dialect": self.source_dialect}
        for key in _SOURCE_TEXT_FIELDS:
            if self.source.get(key) is not None:
                out[key] = self.source[key]
        for key in _SOURCE_INT_FIELDS:
            if self.source.get(key) is not None:
                out[key] = int(self.source[key])
        if self.partition.get("column") is not None:
            out["partition_column"] = self.partition["column"]
        if self.partition.get("period") is not None:
            out["partition_period"] = self.partition["period"]
        rt = self.runtime_defaults
        if rt.get("chunksize") is not None:
            out["chunksize"] = int(rt["chunksize"])
        if rt.get("transform_procs") is not None:
            out["transform_procs"] = int(rt["transform_procs"])
        if rt.get("interval_ladder") is not None:
            out["interval_ladder"] = list(rt["interval_ladder"])
        if rt.get("bar_pixel_target") is not None:
            out["bar_pixel_target"] = float(rt["bar_pixel_target"])
        return out


# --------------------------------------------------------------------------
# Inline-CSV resolution
# --------------------------------------------------------------------------

def _resolve_inline_csv(name: str) -> str:
    """Resolve a profile-relative CSV filename to a verified path and read it.

    Same trick as the historical ``_ISO4217_CSV`` in ``seed_example.py``:
    load at profile-load time so the consumer sees a populated string instead
    of a path it has to re-read.

    ``name`` is the basename as written in the YAML (e.g. ``iso4217.csv``);
    the file is looked up in the project root. Profile authors may also use
    paths relative to the profiles directory — see ``_resolve_csv_path``.
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
    lives). Profiles may also reference files inside the profiles tree with
    a ``profiles/…`` path or a sibling basename.
    """
    p = Path(name)
    if p.is_absolute():
        return p
    if p.parts and p.parts[0] == "profiles":
        return _PROJECT_ROOT / p
    # Try the profile directories first (siblings of the profile); fall back
    # to the project root.
    for base in (_USER_DIR, _EXAMPLES_DIR):
        candidate = base / p.name
        if candidate.is_file():
            return candidate
    candidate_root = _PROJECT_ROOT / p.name
    if candidate_root.is_file():
        return candidate_root
    # Return the most likely location for the error message;
    # ``_resolve_inline_csv`` will then report "not a regular file".
    return candidate_root


# --------------------------------------------------------------------------
# Catalogue
# --------------------------------------------------------------------------

def validate_profile_name(name: str) -> str:
    """Return ``name`` if it is a safe profile/file name, else raise.

    Profile names become filenames, so this is the boundary that keeps a
    request from writing outside the profiles directory.
    """
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ProfileError(
            f"invalid profile name {name!r}: use 1-64 characters from "
            f"A-Z a-z 0-9 _ - , starting with a letter or digit")
    return name


def profile_path(name: str) -> Path | None:
    """Return the file backing ``name``, or ``None`` if there is none.

    ``profiles/user/`` wins over ``profiles/examples/`` so a site can shadow
    a shipped profile without editing a tracked file.
    """
    validate_profile_name(name)
    for base in (_USER_DIR, _EXAMPLES_DIR):
        candidate = base / f"{name}.yaml"
        if candidate.is_file():
            return candidate
    return None


def user_profile_path(name: str) -> Path:
    """Return the ``profiles/user/`` path for ``name`` (may not exist yet)."""
    return _USER_DIR / f"{validate_profile_name(name)}.yaml"


def profile_origin(path: Path) -> str:
    """``"user"`` or ``"example"``, by which directory ``path`` sits in."""
    return "user" if path.parent == _USER_DIR else "example"


def iter_profiles() -> list[dict[str, Any]]:
    """Return catalogue metadata for every profile, user copies first.

    One dict per profile: ``name``, ``origin`` (``user``/``example``),
    ``description``, ``dialect``, the three count fields the picker shows,
    ``path``, and — for a file that fails to parse — ``error``. A broken
    profile must not take the catalogue down with it, so parsing is
    per-entry and defensive; ``load_profile`` still raises for the one
    profile actually being used.
    """
    seen: dict[str, dict[str, Any]] = {}
    for base in (_USER_DIR, _EXAMPLES_DIR):
        if not base.is_dir():
            continue
        for path in sorted(base.glob("*.yaml")):
            name = path.stem
            if name in seen or not path.is_file():
                continue
            seen[name] = _catalogue_entry(name, path)
    return sorted(seen.values(), key=lambda e: (e["origin"] != "user", e["name"]))


def _catalogue_entry(name: str, path: Path) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": name,
        "origin": profile_origin(path),
        "path": str(path),
        "description": "",
        "dialect": "",
        "n_fact_columns": 0,
        "n_dimensions": 0,
        "n_measures": 0,
        "n_group_dims": 0,
        "error": None,
    }
    try:
        raw = yaml.safe_load(path.read_text())
        if not isinstance(raw, dict):
            raise ProfileError("top level is not a mapping")
        src = raw.get("source") or {}
        entry["description"] = str(raw.get("description") or "").strip()
        entry["dialect"] = str(src.get("dialect") or "")
        entry["n_fact_columns"] = len(raw.get("fact_columns") or [])
        entry["n_dimensions"] = len(raw.get("dimensions") or [])
        entry["n_measures"] = len(raw.get("measures") or [])
        entry["n_group_dims"] = len(raw.get("group_dims") or [])
    except Exception as exc:            # noqa: BLE001 — catalogue must not die
        entry["error"] = str(exc)
    return entry


def load_profile(name: str = "oracle_payments") -> ExampleProfile:
    """Load and validate the profile named ``name``."""
    path = profile_path(name)
    if path is None:
        available = [p["name"] for p in iter_profiles()] or ["<none>"]
        raise ProfileError(
            f"profile {name!r} not found in {_USER_DIR} or {_EXAMPLES_DIR}. "
            f"Available profiles: {available}")
    return parse_profile_text(path.read_text(), name)


def parse_profile_text(text: str, name: str) -> ExampleProfile:
    """Validate profile YAML held in memory.

    Split out from ``load_profile`` so an upload can be validated before
    anything touches the filesystem.
    """
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ProfileError(f"profile {name!r} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ProfileError(
            f"profile {name!r} must be a YAML mapping at the top level, "
            f"got {type(raw).__name__}")
    return _parse_profile(name, raw)


def save_profile(name: str, text: str) -> Path:
    """Validate ``text`` and write it to ``profiles/user/<name>.yaml``.

    Validation happens first and on the text as given, so a malformed
    profile never lands on disk.
    """
    validate_profile_name(name)
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_PROFILE_BYTES:
        raise ProfileError(
            f"profile is {len(encoded)} bytes, over the "
            f"{MAX_PROFILE_BYTES}-byte limit")
    parse_profile_text(text, name)
    _USER_DIR.mkdir(parents=True, exist_ok=True)
    path = user_profile_path(name)
    path.write_text(text)
    return path


def delete_profile(name: str) -> Path:
    """Delete a saved profile. Shipped examples are never deletable."""
    path = user_profile_path(name)
    if not path.is_file():
        raise ProfileError(
            f"profile {name!r} is not a saved profile — shipped examples "
            f"cannot be deleted")
    path.unlink()
    return path


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def _parse_profile(name: str, raw: dict[str, Any]) -> ExampleProfile:
    """Validate the parsed YAML dict and return an ``ExampleProfile``."""
    src = raw.get("source") or {}
    if not isinstance(src, dict):
        raise ProfileError(f"profile {name!r}: 'source' must be a mapping")
    for required in ("dialect", "base_table", "watermark_column"):
        if required not in src:
            raise ProfileError(
                f"profile {name!r}: 'source.{required}' is required")

    partition = raw.get("partition") or {}
    if not isinstance(partition, dict):
        raise ProfileError(f"profile {name!r}: 'partition' must be a mapping")
    for required in ("column", "period"):
        if required not in partition:
            raise ProfileError(
                f"profile {name!r}: 'partition.{required}' is required")
    period = str(partition["period"])
    if period not in _VALID_PARTITION_PERIODS:
        raise ProfileError(
            f"profile {name!r}: partition.period={period!r} is not one of "
            f"{sorted(_VALID_PARTITION_PERIODS)}")

    runtime = dict(raw.get("runtime_defaults") or {})
    defaults = dict(raw.get("defaults") or {})

    fact_columns = [_parse_fact_column(name, i, fc)
                    for i, fc in enumerate(raw.get("fact_columns") or [])]
    dimensions = [_parse_dimension(name, i, d)
                  for i, d in enumerate(raw.get("dimensions") or [])]
    fact_indices = [_parse_fact_index(name, i, fi)
                    for i, fi in enumerate(raw.get("fact_indices") or [])]
    measures = [_parse_measure(name, i, m)
                for i, m in enumerate(raw.get("measures") or [])]
    group_dims = [_parse_group_set(name, i, g)
                  for i, g in enumerate(raw.get("group_dims") or [])]

    # Resolve inline-CSV payloads now so the consumer (seed_example,
    # profile_apply) gets a populated string.
    inline_by_dim = _parse_inline_dimensions(name, raw.get("inline_dimensions"))

    # Splice the resolved CSV text into the matching Dimension entries. Two
    # fact columns may share one dim_table (so_peer_1/so_peer_2 → peer), so
    # this is a lookup rather than a zip.
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
                filter_group=dim.filter_group,
            )
        filled_dimensions.append(dim)

    known_fact_columns = {d.fact_column for d in filled_dimensions}
    for gs in group_dims:
        if known_fact_columns and gs.dimension not in known_fact_columns:
            raise ProfileError(
                f"profile {name!r}: group_dims entry {gs.name!r} names "
                f"dimension {gs.dimension!r}, which is not one of this "
                f"profile's dimensions")

    return ExampleProfile(
        name=name,
        description=str(raw.get("description") or "").strip(),
        source_dialect=str(src["dialect"]),
        source=dict(src),
        partition=dict(partition),
        runtime_defaults=runtime,
        defaults=defaults,
        fact_columns=fact_columns,
        dimensions=filled_dimensions,
        fact_indices=fact_indices,
        measures=measures,
        group_dims=group_dims,
    )


def _parse_inline_dimensions(profile: str, raw: Any) -> dict[str, str]:
    """Resolve the ``inline_dimensions`` block to ``{dim_table: csv_text}``.

    Each entry carries exactly one of ``csv:`` (a filename resolved against
    the repo) or ``values:`` (the CSV text inline). ``dump_profile`` always
    writes ``values:`` so an exported profile is one self-contained file;
    ``csv:`` stays supported for hand-written profiles.
    """
    defs = raw or {}
    if not isinstance(defs, dict):
        raise ProfileError(
            f"profile {profile!r}: 'inline_dimensions' must be a mapping")
    out: dict[str, str] = {}
    for dim_table, spec in defs.items():
        if not isinstance(spec, dict):
            raise ProfileError(
                f"profile {profile!r}: inline_dimensions.{dim_table} must be "
                f"a mapping with a 'csv' or 'values' key")
        has_csv = spec.get("csv") is not None
        has_values = spec.get("values") is not None
        if has_csv == has_values:
            raise ProfileError(
                f"profile {profile!r}: inline_dimensions.{dim_table} needs "
                f"exactly one of 'csv' or 'values', got "
                f"{'both' if has_csv else 'neither'}")
        out[str(dim_table)] = (_resolve_inline_csv(str(spec["csv"]))
                               if has_csv else str(spec["values"]))
    return out


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
    filter_group = raw.get("filter_group")
    if filter_group is not None:
        filter_group = str(filter_group).strip() or None
        if filter_group and ":" in filter_group:
            raise ProfileError(
                f"profile {profile!r}: dimensions[{idx}].filter_group="
                f"{filter_group!r} must not contain ':' (collides with the "
                f"<col>:grouped:<slug> filter token)")
    return Dimension(
        fact_column=fact_column,
        dim_table=dim_table,
        source_table=raw.get("source_table"),
        source_key=raw.get("source_key"),
        source_name=raw.get("source_name"),
        source_description=raw.get("source_description"),
        is_primary=bool(raw.get("is_primary", False)),
        inline_values=None,  # populated later from inline_dimensions
        filter_group=filter_group,
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


def _parse_group_set(profile: str, idx: int, raw: Any) -> ProfileGroupSet:
    if not isinstance(raw, dict):
        raise ProfileError(
            f"profile {profile!r}: group_dims[{idx}] must be a mapping")
    try:
        dimension = str(raw["dimension"])
        set_name = str(raw["name"])
    except KeyError as exc:
        raise ProfileError(
            f"profile {profile!r}: group_dims[{idx}] missing {exc.args[0]!r}")
    groups: list[ProfileGroup] = []
    for gidx, g in enumerate(raw.get("groups") or []):
        if not isinstance(g, dict):
            raise ProfileError(
                f"profile {profile!r}: group_dims[{idx}].groups[{gidx}] "
                f"must be a mapping")
        try:
            group_name = str(g["name"])
        except KeyError:
            raise ProfileError(
                f"profile {profile!r}: group_dims[{idx}].groups[{gidx}] "
                f"missing 'name'")
        members = g.get("members") or []
        if not isinstance(members, (list, tuple)):
            raise ProfileError(
                f"profile {profile!r}: group_dims[{idx}].groups[{gidx}]"
                f".members must be a list of dim_index integers")
        try:
            member_ids = tuple(int(m) for m in members)
        except (TypeError, ValueError) as exc:
            raise ProfileError(
                f"profile {profile!r}: group_dims[{idx}].groups[{gidx}]"
                f".members must be integers: {exc}") from exc
        groups.append(ProfileGroup(name=group_name,
                                   color=str(g.get("color") or ""),
                                   members=member_ids))
    return ProfileGroupSet(
        dimension=dimension,
        name=set_name,
        slug=str(raw.get("slug") or ""),
        position=int(raw.get("position") or 0),
        groups=groups,
    )


# --------------------------------------------------------------------------
# Dumping
# --------------------------------------------------------------------------

class _ProfileDumper(yaml.SafeDumper):
    """SafeDumper that writes multi-line strings as literal blocks.

    Source expressions, watermark queries and inline dimension CSVs are all
    multi-line. Without this they come back as one quoted line with ``\\n``
    escapes, which makes an exported profile unreadable and undiffable.
    """


def _represent_str(dumper: yaml.Dumper, data: str):
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_ProfileDumper.add_representer(str, _represent_str)


def _clean(value: Any) -> Any:
    """Normalise a DB value for YAML output."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _prune(mapping: dict[str, Any]) -> dict[str, Any]:
    """Drop keys whose value is ``None`` or an empty string.

    An absent key and a NULL column mean the same thing to the parser, and
    dropping them keeps an exported profile to what is actually configured.
    """
    return {k: v for k, v in mapping.items() if v is not None and v != ""}


def dump_profile(ds: Mapping[str, Any],
                 fact_columns: Iterable[Mapping[str, Any]] = (),
                 dimensions: Iterable[Mapping[str, Any]] = (),
                 fact_indices: Iterable[Mapping[str, Any]] = (),
                 measures: Iterable[Mapping[str, Any]] = (),
                 group_sets: Sequence[Mapping[str, Any]] = (),
                 name: str = "",
                 description: str = "") -> str:
    """Serialise one datasource's ``starcubes_meta`` rows to profile YAML.

    Takes plain mappings rather than a ``core.source.Source`` on purpose:
    ``Source.load`` raises when neither ``import_query`` nor ``base_table``
    is set, so a half-configured datasource would not be exportable. The
    views already read exactly these rows for the detail page.

    ``ds`` is the ``datasource`` row. **``source_dsn`` is never read** — a
    profile carries no credentials, not even masked ones.

    ``group_sets`` entries carry ``dimension`` (the carrying dimension's
    fact column), ``name``, ``slug``, ``position`` and ``groups``, each
    group with ``group_name``/``name``, ``color`` and ``members``.
    """
    source: dict[str, Any] = {"dialect": ds.get("source_dialect") or ""}
    for key in _SOURCE_TEXT_FIELDS:
        source[key] = ds.get(key)
    for key in _SOURCE_INT_FIELDS:
        value = ds.get(key)
        if value:                       # 0 is the default; omit it
            source[key] = int(value)
    source = _prune(source)

    ladder = ds.get("interval_ladder")
    runtime = _prune({
        "chunksize": _clean(ds.get("chunksize")),
        "transform_procs": _clean(ds.get("transform_procs")),
        "interval_ladder": [int(x) for x in ladder] if ladder else None,
        "bar_pixel_target": _clean(ds.get("bar_pixel_target")),
    })

    # Inline dimension payloads move into their own block, keyed by dim
    # table, so two fact columns sharing one dimension table carry the CSV
    # once. Written as ``values:`` (not ``csv:``) to keep the export a
    # single self-contained file.
    inline_dimensions: dict[str, dict[str, str]] = {}
    dim_entries: list[dict[str, Any]] = []
    for dim in dimensions:
        source_table = dim.get("source_table")
        inline_values = dim.get("inline_values")
        if source_table is None and inline_values:
            inline_dimensions.setdefault(str(dim["dim_table"]),
                                         {"values": inline_values})
        entry = {
            "fact_column": dim["fact_column"],
            "dim_table": dim["dim_table"],
            "source_table": source_table,
            "source_key": dim.get("source_key"),
            "source_name": dim.get("source_name"),
            "source_description": dim.get("source_description"),
            "is_primary": bool(dim.get("is_primary")),
        }
        if dim.get("filter_group"):
            entry["filter_group"] = dim["filter_group"]
        dim_entries.append(entry)

    doc: dict[str, Any] = {}
    if name:
        doc["profile"] = name
    if description:
        doc["description"] = description
    doc["source"] = source
    doc["partition"] = _prune({
        "column": ds.get("partition_column"),
        "period": ds.get("partition_period"),
    })
    if runtime:
        doc["runtime_defaults"] = runtime
    suggested = _prune({"name": ds.get("name"),
                        "schema_name": ds.get("schema_name")})
    if suggested:
        doc["defaults"] = suggested
    if inline_dimensions:
        doc["inline_dimensions"] = inline_dimensions

    doc["fact_columns"] = [{
        "name": fc["name"],
        "pg_type": fc["pg_type"],
        "role": fc["role"],
        "ordinal": int(fc["ordinal"]),
        "source_expr": fc.get("source_expr"),
    } for fc in sorted(fact_columns, key=lambda r: (int(r["ordinal"]), r["name"]))]
    doc["dimensions"] = sorted(dim_entries, key=lambda r: r["fact_column"])
    doc["fact_indices"] = [{
        "column_name": fi["column_name"], "method": fi["method"],
    } for fi in sorted(fact_indices,
                       key=lambda r: (r["column_name"], r["method"]))]
    doc["measures"] = [{
        "name": m["name"],
        "agg": m["agg"],
        "graph_style": m["graph_style"],
        "is_default": bool(m["is_default"]),
        "ordinal": int(m["ordinal"]),
        "percentiles": m.get("percentiles"),
    } for m in sorted(measures, key=lambda r: (int(r["ordinal"]), r["name"]))]

    group_entries = []
    for gs in sorted(group_sets, key=lambda r: (r.get("dimension") or "",
                                                int(r.get("position") or 0),
                                                r.get("name") or "")):
        group_entries.append({
            "dimension": gs.get("dimension"),
            "name": gs.get("name"),
            "slug": gs.get("slug") or "",
            "position": int(gs.get("position") or 0),
            "groups": [{
                "name": g.get("group_name") or g.get("name"),
                "color": g.get("color") or "",
                "members": sorted(int(m) for m in (g.get("members") or [])),
            } for g in (gs.get("groups") or [])],
        })
    if group_entries:
        doc["group_dims"] = group_entries

    return yaml.dump(doc, Dumper=_ProfileDumper, sort_keys=False,
                     default_flow_style=False, allow_unicode=True, width=100)
