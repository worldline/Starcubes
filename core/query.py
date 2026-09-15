"""JSON-shaped query producer for the timeseries.js multibar chart.

`timeseries_multibar` returns a Python dict that matches the plot-object
schema documented at https://github.com/hgruber/timeseries.js — Django's
viz endpoint just needs to `json.dumps` it and the canvas renders it.

Required output fields (per the library's README):

    {
      'type':           'multibar',
      'interval_start':  int,    # Unix seconds, aligned to the interval grid
      'interval_end':    int,    # Unix seconds
      'interval':        int,    # seconds per slot
      'count':           int,    # number of non-empty slots
      'min':             number, # smallest stacked-bar height
      'max':             number, # largest  stacked-bar height
      'data':           {slot_int: {series_id_str: value, ...}},
    }

`name` is added when supplied — handy for legend titles, ignored by the
library if unused.

Filter API: `filters={fact_col: [vals, ...]}`. For fact_cols that map to a
dim table, vals are **source_id strings** (the values a human recognizes,
e.g. `'9999'` for `merch_cat`); the SQL resolves them via the dim table at
query time, so the GUI never has to know warehouse indices. For
pass-through smallint dims (e.g. `cncy_code`, no lookup table), vals are
the raw integers stored in fact.

`filter_ops={fact_col: "in"|"not_in"|"starts_with"|"starts_not_with"|"contains"|"not_contains"}`
selects the comparison operator. Default is "in"; "not_in" produces `NOT IN` / `!= ALL`.
"starts_with"/"contains" generate LIKE patterns (OR'd); "starts_not_with"/"not_contains"
generate the same patterns with NOT. For dim-mapped columns the LIKE runs on `source_id`
inside the subquery; for pass-through columns it runs on `f.col::text`.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import text

from .schema import UNKNOWN_SOURCE_ID
from .source import Source
from .utils import format_pks

MEASURE_FUNCS = {"sum", "avg", "min", "max"}

# The five `measure.graph_style` values that read a *rung array* per slot
# instead of a scalar. `quantile-steps-open` is a starcubes-only config value:
# the library distinguishes it from `quantile-steps` via the `plot["connect"]`
# flag (False → freistehende Segmente ohne Riser).
#
# This is a set of **shapes, not of sources** — the distinction matters, and
# conflating the two is what used to make "ladder" mean "always reads fact".
# Every member requires a ladder agg (`LADDER_AGGS`) and a populated rung
# list; the SQL differs only in the one aggregate expression producing `qs`,
# and `plot["type"]` (plus sometimes `plot["connect"]`) between the styles.
LADDER_STYLES = frozenset({
    "quantile-bands",
    "quantile-steps",
    "quantile-steps-open",
    "error-bars",
    "candlestick",
})

# `LADDER_STYLES` is a set of *shapes*, not of sources. Two agg forms feed it:
# `quantile(col)`, whose numbers come from `percentile_cont` over `fact` and
# can never be pre-aggregated, and `extent(col)`, whose min/avg/max fold
# exactly out of a rollup's stored aggregates. Keeping the two apart is what
# lets a band outlive `retention_floor` where a percentile ladder cannot —
# see `pick_rollup` and docs/rollup.md.
LADDER_AGGS = ("quantile", "extent")

# The rungs an `extent(col)` ladder can carry, in the canonical order they are
# always emitted in. Ascending by construction, which is what keeps the
# `vals[0]` / `vals[-1]` y-extent in `timeseries_ladder` valid without sorting
# the values themselves.
EXTENT_RUNGS = ("min", "avg", "max")

# The ladder styles an `extent(col)` measure may use — a strict subset of
# `LADDER_STYLES`. `error-bars` and `candlestick` stay reserved for real
# percentile ladders: the nested pairs they draw carry a meaning a three-rung
# min/avg/max band does not have (a candle out of min/avg/max has no body).
EXTENT_STYLES = frozenset({
    "quantile-bands",
    "quantile-steps",
    "quantile-steps-open",
})

# --- Gap overlay (6σ event-gap detection over count(*)) ---------------------
#
# With second-resolution fact rows, an empty bucket at measure count(*) is an
# empty second(s) of the event stream: consecutive empty buckets between two
# occupied ones are the inter-event distance. Poisson statistics turn that
# into a threshold: the probability of zero events over a span whose expected
# event count is μ is exp(−μ), so a span is 6σ-suspicious once
# μ = ∫λ̂ dt exceeds −ln(α). T = −ln(α)/λ̂ is the flat-rate special case.
#
# "μ + 6σ on the gap length" would be mcalibrated — the geometric tail
# probability behind μ + 6σ depends on λ itself (e.g. e^{−0.6} ≈ 0.54 at
# λ = 0.01/s) — so the tail quantile is used directly.

# One-sided 6σ tail quantile, Φ(−6) ≈ 9.87e-10.
GAP_ALPHA = 9.87e-10
# The day profile λ̂(h) averages the same filters over the `profile_days` days
# ending at the last full UTC midnight, so the rate an hour "usually" carries
# is measured at the same hour of day.
GAP_PROFILE_DAYS = 7
# Server-side TTL for the cached day profile (core.cache.get_cached_plot).
# Not a data invalidation — the entry is immutable behind a cache_epoch bump —
# but a growth bound: back-scrolling over months would otherwise leave one
# row per tag-aligned key behind, and query_cache has no eviction of its own.
GAP_PROFILE_TTL_S = 900
# Cap on the gaps list (largest first). The client draws a further cap
# (GAP_MAX_DRAWN in chart.html); the sort keeps the cap from dropping the
# interesting end of the distribution.
GAP_MAX_CANDIDATES = 2000
# Cap the μ integration per gap in hours. A gap long enough to hit the cap is
# marked long before it matters — the cap only bounds the loop on a
# pathological span.
GAP_MAX_INTEGRAL_HOURS = 400
# −ln(α) ≈ 20.7557: a span whose expected event count μ exceeds this is
# marked (P(no event over the span) = exp(−μ) ≤ α).
GAP_THRESHOLD_EVENTS = -math.log(GAP_ALPHA)
# The granularity the gap scan always groups at, whatever tier the chart is
# drawing. The overlay's arrow tips sit on the first/last *transaction*, and
# only a 1 s scan sees them — so the scan SQL hardcodes 1 s buckets
# (`floor(extract(epoch from …))::bigint`) and this constant records that
# decision; the client no longer sends an interval at all.
GAP_SCAN_INTERVAL_S = 1
# Cap on one gap scan, in seconds. A pan across months must not turn into one
# unbounded full scan of `fact`: the client splits its demand at 7-day seams
# and this is the server-side mirror, cutting the *end* of the window (the
# fresh edge is the provisional one — the cut part is simply re-requested).
GAP_MAX_SCAN_S = 7 * 86400
# The shortest span the scan can even report. With 1 s buckets,
# `sec − prev_sec >= 2` is the first distance that has at least one fully
# empty second between two occupied ones; `sec − prev_sec == 1` is two
# adjacent occupied seconds, i.e. no gap at all.
GAP_MIN_SPAN_S = 2


def parse_ladder_agg(agg: Optional[str]) -> Optional[tuple[str, str]]:
    """Parse a ladder measure's agg form into ``(kind, column)``.

    ``'quantile(stay)' → ('quantile', 'stay')``,
    ``'extent(stay)' → ('extent', 'stay')``, anything else → ``None``.

    One parser for both kinds, because the shape they produce is identical and
    only the source differs. The two call sites in `viz/views.py` (`plot_json`
    and `chart_sql`) used to carry a hand-rolled `quantile\\((\\w+)\\)$` each;
    they are deliberately mirrored copies of one dispatch, so the regex
    belongs here rather than twice over there.
    """
    m = re.match(r"(quantile|extent)\((\w+)\)$", (agg or "").strip())
    return (m.group(1), m.group(2)) if m else None


def normalize_rungs(kind: str, rungs) -> list:
    """Validate a ladder's rungs and put them in canonical ascending order.

    Percentile ladders sort numerically. Extent ladders are ordered *by*
    `EXTENT_RUNGS` rather than sorted — `'avg'` does not compare to `'min'` as
    a string, and the drawing order is what the renderer reads off
    `plot["percentiles"]`. Both come out ascending, which is the invariant
    `timeseries_ladder` relies on when it takes the y-extent from the first
    and last entry of each slot's array.

    Raises ValueError on anything unusable; every ladder path funnels through
    here so the message is the same wherever the bad ladder came from.
    """
    if kind == "extent":
        toks = [str(t).strip().lower() for t in (rungs or []) if str(t).strip()]
        bad = [t for t in toks if t not in EXTENT_RUNGS]
        if bad:
            raise ValueError(
                f"extent rungs must be from {list(EXTENT_RUNGS)}, got {bad!r}")
        seen = set(toks)
        out = [r for r in EXTENT_RUNGS if r in seen]
        if len(out) < 2:
            raise ValueError(
                f"an extent ladder needs at least two distinct rungs, "
                f"got {rungs!r}")
        return out
    try:
        pct = sorted({int(p) for p in (rungs or [])})
    except (TypeError, ValueError):
        # A token ladder handed to the percentile path — `int('min')` would
        # raise here with a message about string literals that says nothing
        # about the actual mistake, which is a `min,avg,max` list on a
        # `quantile(col)` measure (or the reverse pairing in the catalog).
        raise ValueError(
            f"a quantile ladder takes integer percentiles, got {rungs!r}; "
            f"the {list(EXTENT_RUNGS)} rungs belong to an extent(col) measure")
    if not pct or any(not (0 < p < 100) for p in pct):
        raise ValueError(
            f"percentiles must be integers in (0, 100), got {rungs!r}")
    return pct


def extent_value_expr(rungs, col: str, *, alias: str = "f") -> str:
    """The per-slot ``ARRAY`` expression for an `extent(col)` ladder over `fact`.

    The rollup counterpart is `core.rollup.measure_expr`, which reads the
    stored aggregates instead of the raw column but produces the same array in
    the same rung order. Both cast every element to `double precision`: the
    array needs one element type (`min(integer)` is integer while
    `avg(integer)` is numeric), and that is also what `percentile_cont`
    returns on the other ladder path, so the Python side sees one shape.
    """
    parts = ", ".join(f"{r}({alias}.{col})::double precision" for r in rungs)
    return f"ARRAY[{parts}]"


def _ladder_plot_type(graph_style: str) -> tuple[str, bool]:
    """Map a starcubes `graph_style` to `(lib_type, connect)`.

    `connect=False` requests the open variant of `quantile-steps`
    (freistehende horizontale Segmente ohne Riser). All other ladder
    styles pass `True` and let the library pick its default connector.
    """
    if graph_style == "quantile-steps-open":
        return "quantile-steps", False
    return graph_style, True


# graph_style → plot["step"] for the simple family. `multistep` is the only
# style that is not passed through to the library verbatim: it renders on the
# library's `multiline` renderer with the step-after flag, where each bin
# holds its value across the bin it represents (the run extends one `binW`
# past its last slot, which is exactly the next fetch block's first slot —
# so the staircase bridges fetch-block margins without a `coalesce` entry,
# where a plain polyline leaves a one-bin gap there).
SIMPLE_PLOT_STEP = {"multistep": "after"}


def _simple_plot_type(graph_style: str) -> str:
    """Map a starcubes `graph_style` to the library plot type (simple family).

    Like `_ladder_plot_type`, for the styles that read a scalar per slot.
    `multistep` is the only indirection — everything else IS its lib type.
    """
    return "multiline" if graph_style == "multistep" else graph_style


def _parse_measure(measure: str) -> tuple[str, str]:
    """Parse 'avg(stay)' → ('avg', 'stay'), 'stay' → ('sum', 'stay')."""
    m = re.match(r'(\w+)\((\w+)\)$', measure)
    if m:
        func, col = m.group(1), m.group(2)
        if func not in MEASURE_FUNCS:
            raise ValueError(
                f"unknown measure function {func!r}; "
                f"choose from {sorted(MEASURE_FUNCS)}")
        return func, col
    return "sum", measure


_VALID_OPS = ("in", "not_in", "starts_with", "starts_not_with", "contains", "not_contains")
GROUPED_SUFFIX = ":grouped"

# ä/ö/ü/ß survive `.lower()` but not `[a-z0-9]`, and dropping them silently
# would collapse "Größe" and "Gre" onto the same slug.
_SLUG_TRANSLITERATE = str.maketrans(
    {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


def slugify_group_set(name: str) -> str:
    """Derive a group set's URL slug from its display name.

    Restricted to ``[a-z0-9-]`` because the resulting token travels through
    `viz.views._parse_filter_spec`, which splits filter params on ``=`` and
    value lists on ``,`` — and through the ``:``-delimited token itself.
    """
    s = name.lower().translate(_SLUG_TRANSLITERATE)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "group"


def grouped_token(fact_column: str, slug: str) -> str:
    """The chart's group_by / filter token for one group set."""
    return f"{fact_column}{GROUPED_SUFFIX}:{slug}"


def grouped_label(fact_column: str, name: str) -> str:
    """Dropdown label for a group set: `<col> → <name>`.

    The prefix is dropped when the name already starts with the column, which
    is what the default name `<col> (grouped)` does — otherwise every migrated
    group dimension would read "response → response (grouped)".
    """
    return name if name.startswith(fact_column) else f"{fact_column} → {name}"


def split_grouped_col(col: str) -> tuple[str, Optional[str], bool]:
    """Split a group_by / filter column into (fact_column, slug, is_grouped).

    ``'peer:grouped:peer-types'`` → ``('peer', 'peer-types', True)``
    ``'peer:grouped'``            → ``('peer', None, True)`` — the legacy
    token, which resolves to the dimension's first group set.
    ``'peer'``                    → ``('peer', None, False)``
    """
    if col.endswith(GROUPED_SUFFIX):
        return col[:-len(GROUPED_SUFFIX)], None, True
    head, sep, slug = col.partition(GROUPED_SUFFIX + ":")
    if sep:
        return head, (slug or None), True
    return col, None, False


def resolve_group_set(dim, slug: Optional[str]):
    """Look up the `DimGroupSet` a token addresses; `None` when unknown."""
    if dim is None:
        return None
    return dim.group_set(slug)


def build_group_by_indices(gset) -> dict[str, list[int]]:
    """Build a mapping of group name → sorted dim indices for one group set.

    Only the *named* groups appear; the pseudo-group "(other)" is the
    complement and is handled by the callers (`_build_bar_where`, the
    quantile-bands CASE) as an ELSE branch.
    """
    group_by_indices: dict[str, list[int]] = {}
    for g in gset.groups:
        group_by_indices[g.group_name] = sorted(m.dim_index for m in g.members)
    return group_by_indices


def _indices_by_group(
    group_by_groups: dict[int, tuple[str, str]],
) -> dict[str, list[int]]:
    """Invert ``{dim_index: (group_name, color)}`` into ``{group_name: indices}``.

    Same shape `build_group_by_indices` returns — the SQL builders already
    receive the flattened map, so they need not carry the `DimGroupSet` too.
    Insertion order matches the set's group order, which keeps the emitted
    CASE branches byte-identical to the previous implementation.
    """
    out: dict[str, list[int]] = {}
    for idx, (gname, _color) in group_by_groups.items():
        out.setdefault(gname, []).append(idx)
    for idxs in out.values():
        idxs.sort()
    return out


def _resolve_grouped_filter(
    flt_set,
    op: str,
    vals: list,
) -> tuple[set[int], bool, bool]:
    """Translate a grouped filter into (matching dim indices, include_other, negate).

    Group names are matched against `vals` per the operator (in/not_in: exact,
    starts_with/contains: substring on group_name). Group "(other)" matches
    everything not in any named group of `flt_set`; it can be selected
    explicitly in `in`/`not_in`, or implicitly when the literal "(other)"
    matches the LIKE pattern.
    """
    negate = op in ("not_in", "starts_not_with", "not_contains")
    if op in ("in", "not_in"):
        wanted = set(vals)
        include_other = "(other)" in wanted
        named_wanted = wanted - {"(other)"}
        matching_indices = {
            m.dim_index for g in flt_set.groups
            if g.group_name in named_wanted for m in g.members
        }
    else:
        matched_names: set[str] = set()
        include_other = False
        for v in vals:
            if op in ("starts_with", "starts_not_with"):
                test = (lambda name, p=v: name.startswith(p))
            else:
                test = (lambda name, p=v: p in name)
            for g in flt_set.groups:
                if test(g.group_name):
                    matched_names.add(g.group_name)
            if test("(other)"):
                include_other = True
        matching_indices = {
            m.dim_index for g in flt_set.groups
            if g.group_name in matched_names for m in g.members
        }
    return matching_indices, include_other, negate


def _filter_fanout(dims_by_fact: dict) -> dict[str, list[str]]:
    """{filter_group: sorted member fact columns} — see Source.filter_fanout.

    Rebuilt per condition (O(#dims) per filter row — negligible). `getattr`
    keeps it tolerant of any caller-built stub dimensions.
    """
    out: dict[str, list[str]] = {}
    for d in dims_by_fact.values():
        if getattr(d, "filter_group", None):
            out.setdefault(d.filter_group, []).append(d.fact_column)
    return {k: sorted(v) for k, v in out.items()}


def filter_target_columns(fact_col: str, dims_by_fact: dict) -> list[str]:
    """The fact columns a filter on `fact_col` actually reads.

    Three tokens resolve differently and `_filter_sql` below encodes all
    three: a grouped token (`<col>:grouped:<slug>`) only ever touches its real
    column, a filter-group name fans out to every member column, and anything
    else is itself.

    Extracted so `core.rollup.pick_rollup` can ask the same question without
    building SQL. Keeping the two on one implementation is the point: a rollup
    chosen against a different column set than the WHERE clause reads would
    filter on a column the table does not have — a hard error at best, and at
    worst a silently unfiltered chart.
    """
    real_col, _slug, is_grouped = split_grouped_col(fact_col)
    if is_grouped:
        return [real_col]
    return _filter_fanout(dims_by_fact).get(fact_col) or [fact_col]


def filter_tree_columns(nodes: list[dict], dims_by_fact: dict) -> set[str]:
    """Every fact column a filter tree reads, children included."""
    out: set[str] = set()
    for node in nodes or []:
        col = (node.get("row") or {}).get("col")
        if col:
            out.update(filter_target_columns(col, dims_by_fact))
        out |= filter_tree_columns(node.get("children") or [], dims_by_fact)
    return out


def build_filter_tree(flat_rows: list[dict]) -> list[dict]:
    """Stack-parse flat filter rows into the nested tree `_build_where` takes.

    Accepts either flat shape in the wild:
      * the chart's serialization ``{col, op, vals, andOr, indent}``
        (``gatherParams``; also what an alarm stores in
        ``starcubes_meta.alarm.context_filter``),
      * the URL parser's ``{'row': {col, op, vals}, 'andOr', indent}``.
    Missing ``andOr`` defaults to 'and', missing ``indent`` to 0. Returns the
    root node list ``{'andOr', 'row': {col, op, vals}, 'children': [...]}``.
    """
    root: list[dict] = []
    stack: list[list[dict]] = [root]
    indent_stack: list[int] = [-1]
    for entry in flat_rows:
        row = entry.get("row", entry)
        node: dict = {
            "andOr": entry.get("andOr", "and"),
            "row": {"col": row["col"], "op": row["op"], "vals": row["vals"]},
            "children": [],
        }
        indent = entry.get("indent", 0)
        while indent <= indent_stack[-1]:
            stack.pop()
            indent_stack.pop()
        stack[-1].append(node)
        stack.append(node["children"])
        indent_stack.append(indent)
    return root


def check_filter_col(source, col: str) -> None:
    """Raise ValueError unless `col` is a filterable token for `source`.

    Valid: a fact column, a fan-out filter_group token, or a grouped token
    ``<col>:grouped:<slug>`` (incl. the legacy ``<col>:grouped``) resolving
    to a dim-mapped column with an existing group set. The error message is
    the one the chart surfaces verbatim, so it must not drift from what
    `timeseries_multibar` used to raise from its closure.
    """
    fact_cols = {c.name for c in source.fact_columns}
    dims_by_fact = {d.fact_column: d for d in source.dimensions}
    if col in fact_cols:
        return
    if col in _filter_fanout(dims_by_fact):
        return
    real, slug, is_grouped = split_grouped_col(col)
    if is_grouped:
        d = dims_by_fact.get(real)
        if (real in fact_cols and d and d.dim_table
                and resolve_group_set(d, slug) is not None):
            return
    raise ValueError(
        f"filter col {col!r} is not a valid fact column "
        f"of {source.name!r}")


def validate_filter_tree(source, nodes: list[dict]) -> None:
    """Validate a nested filter tree (see `build_filter_tree` for the shape).

    Raises the same ValueErrors `timeseries_multibar` raises for bad trees;
    alarm contexts reuse this at save time and at evaluation time.
    """
    for node in nodes:
        row = node["row"]
        check_filter_col(source, row["col"])
        if row["op"] not in _VALID_OPS:
            raise ValueError(
                f"filter op for {row['col']!r} must be one of "
                f"{_VALID_OPS}, got {row['op']!r}")
        validate_filter_tree(source, node["children"])


def validate_filters(source, filter_tree=None, filter_groups=None) -> None:
    """Validate whichever filter form was supplied — tree (web) or groups (CLI).

    `filter_tree` takes precedence when both are given, the same rule
    `_build_where` applies when it picks which one to turn into SQL. Called
    from the four `_build_where` wrappers (`build_chart_sql`,
    `build_chart_legend_sql`, `_gap_where`, `_build_bar_where`), so no filter
    reaches SQL unchecked and every query function is covered by the wrapper
    it already goes through — a bad filter column used to reach Postgres and
    come back as a 42703 the chart's error translation does not know, i.e. an
    HTTP 500 for what is a typo in the filter box.
    """
    if filter_tree is not None:
        validate_filter_tree(source, filter_tree)
        return
    for group in filter_groups or []:
        for cond in group:
            check_filter_col(source, cond["col"])
            if cond["op"] not in _VALID_OPS:
                raise ValueError(
                    f"filter op for {cond['col']!r} must be one of "
                    f"{_VALID_OPS}, got {cond['op']!r}")


def _coerce_filter_vals(pg_type: Optional[str], vals: list) -> list:
    """Coerce filter values to the plain fact column's type.

    The non-dim branches of `_filter_sql` compare the column against one
    bound array (`= ANY` / `!= ALL`), and psycopg2 sends the Python list as
    a single array literal — so its element type must match the column's or
    PostgreSQL refuses the comparison. A dim-less fact column may well be
    text (an ISO code, a status string): the LIKE branch above already
    assumes that with its `::text` cast, and coercing those values with
    int() turned any filter on such a column into a 500. Unknown or missing
    type falls through to strings, the type PostgreSQL casts most broadly.
    """
    t = (pg_type or "").strip().split()
    t = t[0] if t else ""
    if t in ("smallint", "integer", "bigint"):
        return [int(v) for v in vals]
    if t in ("numeric", "double", "real"):
        return [float(v) for v in vals]
    return [str(v) for v in vals]


def _filter_sql(
    schema: str,
    fact_col: str,
    op: str,
    vals: list,
    param: str,
    dims_by_fact: dict,
    fact_types: Optional[dict[str, str]] = None,
) -> tuple[str, dict]:
    """Return (sql_fragment, params) for a single filter condition.

    Each condition is self-contained with its own parameter names so it can
    be freely combined with AND/OR in a group expression.

    Grouped filter (`<col>:grouped:<slug>`, or the legacy `<col>:grouped`):
    vals are group names from the addressed `DimGroupSet`; the SQL filters by
    the dim_indices that belong to the named groups. The pseudo-name "(other)"
    matches dim values not in any named group *of that set*.
    """
    params: dict = {}

    real_col, slug, is_grouped = split_grouped_col(fact_col)
    if is_grouped:
        flt_dim = dims_by_fact.get(real_col)
        if not flt_dim or not flt_dim.dim_table:
            raise ValueError(
                f"grouped filter on {real_col!r} requires a dim-mapped column")
        flt_set = resolve_group_set(flt_dim, slug)
        if flt_set is None:
            which = f"{slug!r}" if slug else "defined"
            raise ValueError(
                f"grouped filter on {real_col!r}: no group dimension {which}")
        matching_indices, include_other, negate = _resolve_grouped_filter(
            flt_set, op, vals)
        params[param] = sorted(matching_indices)
        clause = f"f.{real_col} = ANY(:{param})"
        if include_other:
            other_param = f"{param}_oth"
            params[other_param] = sorted(
                {m.dim_index for g in flt_set.groups for m in g.members})
            clause = f"({clause} OR f.{real_col} != ALL(:{other_param}))"
        return (f"NOT ({clause})" if negate else clause), params

    cols = filter_target_columns(fact_col, dims_by_fact)
    flt_dim = dims_by_fact.get(cols[0])

    if op in ("starts_with", "starts_not_with", "contains", "not_contains"):
        negate = op in ("starts_not_with", "not_contains")
        like_parts: list[str] = []
        for j, v in enumerate(vals):
            pname = f"{param}_{j}"
            params[pname] = f"{v}%" if op in ("starts_with", "starts_not_with") else f"%{v}%"
            if flt_dim and flt_dim.dim_table:
                like_parts.append(f"name LIKE :{pname}")
            else:
                like_parts.append(f"f.{fact_col}::text LIKE :{pname}")
        or_expr = " OR ".join(like_parts)
        if flt_dim and flt_dim.dim_table:
            inner = (f"SELECT index FROM {schema}.{flt_dim.dim_table} "
                     f" WHERE {or_expr}")
            in_op = "NOT IN" if negate else "IN"
            if len(cols) == 1:
                return f"f.{fact_col} {in_op} ({inner})", params
            # Fan-out: the positive IN is built per member column and the
            # whole OR negated — NOT((a IN …) OR (b IN …)) = "no member
            # matches", never a double negation.
            per_col = [f"f.{c} IN ({inner})" for c in cols]
            expr = "(" + " OR ".join(per_col) + ")"
            return (f"NOT ({expr})" if negate else expr), params
        else:
            return (f"NOT ({or_expr})" if negate else f"({or_expr})"), params
    elif flt_dim and flt_dim.dim_table:
        params[param] = [str(v) for v in vals]
        inner = (f"SELECT index FROM {schema}.{flt_dim.dim_table} "
                 f" WHERE source_id = ANY(:{param})")
        if op == "not_in":
            if len(cols) == 1:
                return f"f.{fact_col} NOT IN ({inner})", params
            per_col = [f"f.{c} IN ({inner})" for c in cols]
            expr = "(" + " OR ".join(per_col) + ")"
            return f"NOT {expr}", params
        if len(cols) == 1:
            return f"f.{fact_col} IN ({inner})", params
        per_col = [f"f.{c} IN ({inner})" for c in cols]
        return "(" + " OR ".join(per_col) + ")", params
    else:
        params[param] = _coerce_filter_vals(
            (fact_types or {}).get(fact_col), vals)
        if op == "not_in":
            return f"f.{fact_col} != ALL(:{param})", params
        else:
            return f"f.{fact_col} = ANY(:{param})", params


def _build_where_nodes(
    schema: str,
    siblings: list[dict],
    dims_by_fact: dict,
    cond_idx: list,
    fact_types: Optional[dict[str, str]] = None,
) -> tuple[list[str], dict]:
    """Recursively build WHERE parts for a filter-tree sibling list.

    siblings: list of node dicts from _parse_filter_tree, each with
              {'andOr': str, 'row': {col, op, vals}, 'children': list}
    cond_idx: single-element list used as a mutable integer counter for
              unique parameter names across recursive calls.
    Returns (where_parts, params) where where_parts are AND-connectable.
    """
    exprs: list[tuple[str, str]] = []  # (andOr, sql_expr)
    params: dict = {}

    for sibling in siblings:
        row = sibling["row"]
        row_sql, row_params = _filter_sql(
            schema, row["col"], row["op"], row["vals"],
            f"flt{cond_idx[0]}", dims_by_fact, fact_types,
        )
        cond_idx[0] += 1
        params.update(row_params)

        if sibling["children"]:
            child_parts, child_params = _build_where_nodes(
                schema, sibling["children"], dims_by_fact, cond_idx, fact_types,
            )
            params.update(child_params)
            children_sql = " AND ".join(child_parts)
            child_connector = sibling["children"][0]["andOr"].upper()
            if child_connector == "OR":
                sibling_sql = f"({row_sql} OR ({children_sql}))"
            else:
                sibling_sql = f"{row_sql} AND ({children_sql})"
        else:
            sibling_sql = row_sql

        exprs.append((sibling["andOr"], sibling_sql))

    # Group consecutive OR-connected expressions; AND between groups.
    or_groups: list[list[str]] = []
    if exprs:
        current: list[str] = [exprs[0][1]]
        for andor, sql in exprs[1:]:
            if andor.lower() == "or":
                current.append(sql)
            else:
                or_groups.append(current)
                current = [sql]
        or_groups.append(current)

    where_parts: list[str] = []
    for group in or_groups:
        if len(group) == 1:
            where_parts.append(group[0])
        else:
            where_parts.append(f"({' OR '.join(group)})")

    return where_parts, params


def _build_where(
    schema: str,
    pc: str,
    t_from: datetime,
    t_to: datetime,
    filter_groups: Optional[list[list[dict]]],
    dims_by_fact: dict,
    *,
    filter_tree: Optional[list[dict]] = None,
    include_time_range: bool = True,
    fact_types: Optional[dict[str, str]] = None,
) -> tuple[list[str], dict]:
    """Build WHERE clause parts and params.

    Accepts either filter_groups (flat AND-of-OR-groups, used by the CLI) or
    filter_tree (nested nodes from _parse_filter_tree, used by the web views).
    filter_tree takes precedence when both are supplied.

    If include_time_range is True (default), prepends the time-range
    conditions f.{pc} >= :t_from AND f.{pc} < :t_to.
    """
    where_parts: list[str] = []
    params: dict = {}
    if include_time_range:
        where_parts += [f"f.{pc} >= :t_from", f"f.{pc} < :t_to"]
        params["t_from"] = t_from
        params["t_to"] = t_to

    if filter_tree is not None:
        tree_parts, tree_params = _build_where_nodes(
            schema, filter_tree, dims_by_fact, [0], fact_types,
        )
        where_parts.extend(tree_parts)
        params.update(tree_params)
    else:
        cond_idx = 0
        for group in filter_groups or []:
            cond_sqls = []
            for cond in group:
                sql, cparams = _filter_sql(
                    schema, cond["col"], cond["op"], cond["vals"],
                    f"flt{cond_idx}", dims_by_fact, fact_types,
                )
                cond_sqls.append(sql)
                params.update(cparams)
                cond_idx += 1
            if len(cond_sqls) == 1:
                where_parts.append(cond_sqls[0])
            elif len(cond_sqls) > 1:
                where_parts.append(f"({' OR '.join(cond_sqls)})")

    return where_parts, params


def build_chart_sql(
    source: Source,
    *,
    graph_style: str,
    group_by: str,
    measure: Optional[str],
    t_from: datetime,
    t_to: datetime,
    interval: int,
    filter_tree: Optional[list[dict]],
    filter_groups: Optional[list[list[dict]]] = None,
    rungs: Optional[list] = None,
    ladder_kind: Optional[str] = None,
    measure_col: Optional[str] = None,
    series_label: str = "source_id",
    group_by_groups: Optional[dict[int, tuple[str, str]]] = None,
    rollup_table: Optional[str] = None,
    rollup_value_expr: Optional[str] = None,
) -> tuple[str, dict, dict[int, str], dict[str, str]]:
    """Build the same SQL string `timeseries_multibar` / `timeseries_ladder`
    execute, without running it.

    `rollup_table` / `rollup_value_expr` redirect the statement at a
    pre-aggregation table (see `core/rollup.py`). Only two things change: the
    relation in the FROM clause, and the aggregate producing the value — the
    filter SQL, the joins, the slot expression and the grouped-dimension
    handling are identical, because a rollup carries the same column names as
    `fact`. The two arrive pre-resolved rather than as a `RollupSpec` on
    purpose: it keeps this module free of any import from `core.rollup`, which
    imports *this* one for `filter_tree_columns`. That contract is also what
    carries the ladder's array expression — widening what the string may hold
    costs nothing and needed no new import.

    `ladder_kind` selects which feeder a `LADDER_STYLES` request uses:
    `'quantile'` (percentile_cont over `fact`, never foldable) or `'extent'`
    (min/avg/max, foldable and therefore rollup-servable). `rungs` is that
    ladder's rung list — integers for the former, `EXTENT_RUNGS` tokens for
    the latter.

    Returns ``(sql, params, index_to_group, group_colors)``. The two dicts are
    the same Python-side state the multibar path uses to merge per-index rows
    into per-group rows after the query (``index_to_group`` is empty for the
    un-grouped path; ``group_colors`` is populated whenever groups are used).
    Quantile-bands does not need either — the SQL-side CASE already returns
    group names — so the dicts are returned but unused by that caller.
    """
    if t_from.tzinfo is None or t_to.tzinfo is None:
        raise ValueError("t_from and t_to must be timezone-aware")
    if t_to <= t_from:
        raise ValueError(f"t_to ({t_to}) must be after t_from ({t_from})")
    if interval <= 0:
        raise ValueError(f"interval must be positive, got {interval}")
    if series_label not in ("source_id", "name", "index"):
        raise ValueError(
            f"series_label must be 'source_id'|'name'|'index', "
            f"got {series_label!r}")

    group_by_none = not group_by
    fact_cols = {c.name for c in source.fact_columns}
    if graph_style in LADDER_STYLES:
        if not measure_col:
            raise ValueError(f"{graph_style} needs measure_col")
        if measure_col not in fact_cols:
            raise ValueError(
                f"measure column {measure_col!r} is not a fact column of "
                f"{source.name!r}")
    else:
        if measure is not None:
            _mfunc, mcol = _parse_measure(measure)
            if mcol not in fact_cols:
                raise ValueError(
                    f"measure column {mcol!r} is not a fact column of "
                    f"{source.name!r}")
    if not group_by_none and group_by not in fact_cols:
        raise ValueError(
            f"group_by={group_by!r} is not a fact column of "
            f"{source.name!r}; have {sorted(fact_cols)}")

    dims_by_fact = {d.fact_column: d for d in source.dimensions}
    schema = source.schema_name
    pc = source.partition_column

    # Series projection — mirrors the two callers' existing logic exactly.
    # The `:grouped` distinction lives here in one place: the multibar path
    # SELECTs the raw smallint and merges per-index rows in Python (cheaper
    # than evaluating a CASE 3× per row), while the quantile-bands path
    # builds the CASE SQL-side because `percentile_cont` cannot be merged.
    group_dim = None if group_by_none else dims_by_fact.get(group_by)
    joins = ""
    group_by_grouped = False
    index_to_group: dict[int, str] = {}
    group_colors: dict[str, str] = {}
    series_expr = ""
    if group_by_none:
        series_expr = "'(all)'"
    elif graph_style not in LADDER_STYLES and group_by_groups and group_dim and group_dim.dim_table:
        # multibar / multiline / multipoint / multistep — Python-side merge path.
        group_by_grouped = True
        for idx, (gname, gcolor) in group_by_groups.items():
            index_to_group[idx] = gname
            group_colors[gname] = gcolor
        series_expr = f"f.{group_by}::int"
    elif graph_style in LADDER_STYLES and group_by_groups and group_dim and group_dim.dim_table:
        # quantile-bands — SQL-side CASE mapping (percentile_cont can't merge).
        name_to_indices = _indices_by_group(group_by_groups)
        for idx, (gname, gcolor) in group_by_groups.items():
            group_colors[gname] = gcolor
        whens = []
        for gname, idxs in name_to_indices.items():
            if not idxs:
                continue
            arr = ",".join(str(i) for i in idxs)
            safe = gname.replace("'", "''")
            whens.append(
                f"WHEN f.{group_by} = ANY(ARRAY[{arr}]::smallint[]) "
                f"THEN '{safe}'")
        series_expr = (
            "CASE " + " ".join(whens) + " ELSE '(other)' END"
            if whens else "'(other)'")
    elif group_dim and group_dim.dim_table:
        joins = (f" JOIN {schema}.{group_dim.dim_table} _g "
                 f"ON f.{group_by} = _g.index ")
        series_expr = f"_g.{series_label}::text"
    else:
        series_expr = f"f.{group_by}::text"

    epoch_from = int(t_from.timestamp())
    interval_start = (epoch_from // interval) * interval

    validate_filters(source, filter_tree, filter_groups)
    where_parts, flt_params = _build_where(
        schema, pc, t_from, t_to, filter_groups, dims_by_fact,
        filter_tree=filter_tree,
        fact_types={c.name: c.pg_type for c in source.fact_columns})
    params: dict = {
        "interval":       interval,
        "interval_start": interval_start,
        **flt_params,
    }

    if graph_style in LADDER_STYLES:
        kind = ladder_kind or "quantile"
        rung_list = normalize_rungs(kind, rungs)
        if kind == "extent":
            if graph_style not in EXTENT_STYLES:
                raise ValueError(
                    f"graph_style={graph_style!r} cannot draw an extent "
                    f"ladder; choose from {sorted(EXTENT_STYLES)}")
            # `rollup_value_expr` already carries the ARRAY over the stored
            # aggregates when a rollup was picked (core.rollup.measure_expr);
            # otherwise aggregate the raw column.
            rung_expr = rollup_value_expr or extent_value_expr(
                rung_list, measure_col)
        else:
            if rollup_table or rollup_value_expr:
                # Not reachable through `pick_rollup`, which refuses a
                # percentile ladder outright — this catches a future caller
                # that resolves the rollup before it knows the ladder kind.
                # Percentiles of pre-aggregated buckets are not the
                # percentiles of the rows, and there is no correction for it.
                raise ValueError(
                    "a percentile ladder can never read a rollup — "
                    "percentiles do not fold; only extent(col) ladders may")
            fractions = ",".join(f"{p / 100.0:g}" for p in rung_list)
            rung_expr = (f"percentile_cont(ARRAY[{fractions}]) "
                         f"WITHIN GROUP (ORDER BY f.{measure_col})")
        sql = (
            f"SELECT "
            f"  floor((extract(epoch from f.{pc}) - :interval_start) "
            f"        / :interval)::bigint AS slot, "
            f"  {series_expr} AS series, "
            f"  {rung_expr} AS qs "
            f"FROM {schema}.{rollup_table or 'fact'} f "
            f"{joins}"
            f"WHERE {' AND '.join(where_parts)} "
            f"GROUP BY 1, 2 "
            f"ORDER BY 1, 2")
        return sql, params, index_to_group, group_colors

    if rollup_value_expr:
        # Pre-aggregated: the value comes from the rollup's stored aggregates
        # (sum(n) for a count, sum(sum_col) for a sum, and so on). Already
        # cast where it needs to be, so no second cast here.
        value_expr = rollup_value_expr
        value_cast = ""
    elif measure is None:
        value_expr = "count(*)"
        value_cast = "::bigint"
    else:
        mfunc, mcol = _parse_measure(measure)
        value_expr = f"{mfunc}(f.{mcol})"
        value_cast = ""

    sql = (
        f"SELECT "
        f"  floor((extract(epoch from f.{pc}) - :interval_start) "
        f"        / :interval)::bigint AS slot, "
        f"  {series_expr} AS series, "
        f"  {value_expr}{value_cast} AS v "
        f"FROM {schema}.{rollup_table or 'fact'} f "
        f"{joins}"
        f"WHERE {' AND '.join(where_parts)} "
        f"GROUP BY 1, 2"
        f"{', _g.count' if group_dim and group_dim.dim_table and not group_by_grouped else ''} "
        f"ORDER BY"
        f"{' _g.count,' if group_dim and group_dim.dim_table and not group_by_grouped else ''}"
        f"{' 2,' if not (group_dim and group_dim.dim_table) else ''}"
        f" 1")

    return sql, params, index_to_group, group_colors


# ── Legend SQL: per-series cumulative over the time window ─────────────
#
# `updateLegend()` in the chart reads `ts.getActiveData()` and aggregates
# the per-slot values in JavaScript: each series in the legend shows the
# sum (or mean-of-percentiles for quantile-bands) across the visible window.
# The "Copy chart SQL" button should hand the user a statement that, when
# run, returns the same per-series numbers directly — i.e. one row per
# series, no slot breakdown.
#
# `build_chart_legend_sql` follows the routing logic of `build_chart_sql`
# (same joins, same filter tree, same graph-style dispatch) but the SELECT
# body aggregates over the whole half-open time window instead of
# `GROUP BY slot, series`. Filters reduce rows but never narrow the SELECT
# projection.
#
# The one place it deliberately diverges is the series label. `build_chart_sql`
# projects the stable key the chart transports (`source_id`, or the raw dim
# index for `:grouped`) because `_resolveDimName()`, the `dataCache` keys and
# the drill-down `group_by_val` all depend on it. This helper feeds the Copy-
# chart-SQL button instead, whose output a human reads in the console with no
# browser-side translation available — so it projects the *displayed* label.

def build_chart_legend_sql(
    source: Source,
    *,
    graph_style: str,
    group_by: str,
    measure: Optional[str],
    t_from: datetime,
    t_to: datetime,
    filter_tree: Optional[list[dict]],
    filter_groups: Optional[list[list[dict]]] = None,
    rungs: Optional[list] = None,
    ladder_kind: Optional[str] = None,
    measure_col: Optional[str] = None,
    series_label: str = "source_id",
    group_by_groups: Optional[dict[int, tuple[str, str]]] = None,
    rollup_table: Optional[str] = None,
    rollup_value_expr: Optional[str] = None,
) -> tuple[str, dict, dict[int, str], dict[str, str]]:
    """Build the per-series aggregate SQL that `updateLegend()` displays.

    Returns ``(sql, params, index_to_group, group_colors)`` mirroring
    `build_chart_sql`. The result is one row per series — `count(*)`,
    `sum(col)`, `avg(col)` etc. summed or averaged over the entire half-open
    time window `[t_from, t_to)`. For a percentile ladder it is
    `percentile_cont` over each series' full distribution (semantically the
    same median / p25–p75 the legend shows after averaging per-slot
    percentiles — close, but an approximation of it). For an extent ladder it
    is the min/avg/max over the window, which is not an approximation of
    anything: those are the very numbers the per-slot arrays average to.

    `group_by_groups` and the `:grouped` dispatch select the same rows as
    `build_chart_sql`, but the series column carries the label the legend
    shows rather than the key the plot path transports: the dimension's
    `name` (falling back to `source_id` where it is empty, as the legend
    does) and, for `:grouped`, the group name via a SQL-side CASE.
    `series_label='index'` still opts out and yields the warehouse key.
    No slot projection is emitted, so the `interval` parameter is
    intentionally absent.

    `rollup_table` / `rollup_value_expr` redirect the non-ladder statement
    at a pre-aggregation table (see `core/rollup.py`), pre-resolved exactly
    as for `build_chart_sql` so this module stays import-free from
    `core.rollup`. Folding a per-slot rollup into a whole-window aggregate
    per series is exact — `sum(n)` for a count, `sum(sum_col)` for a sum,
    `sum(sum_col)/sum(cnt_col)` for the row-weighted mean this helper
    already computes on `fact`, extremes of extremes for min/max. An
    `extent(col)` ladder is built from exactly those folds and is exact for
    the same reason. Only a *percentile* ladder is exempt: percentiles do not
    fold, so it always reads `fact` and refuses a rollup outright.
    """
    if t_from.tzinfo is None or t_to.tzinfo is None:
        raise ValueError("t_from and t_to must be timezone-aware")
    if t_to <= t_from:
        raise ValueError(f"t_to ({t_to}) must be after t_from ({t_from})")
    if series_label not in ("source_id", "name", "index"):
        raise ValueError(
            f"series_label must be 'source_id'|'name'|'index', "
            f"got {series_label!r}")

    group_by_none = not group_by
    fact_cols = {c.name for c in source.fact_columns}
    if graph_style in LADDER_STYLES:
        if not measure_col:
            raise ValueError(f"{graph_style} needs measure_col")
        if measure_col not in fact_cols:
            raise ValueError(
                f"measure column {measure_col!r} is not a fact column of "
                f"{source.name!r}")
    else:
        if measure is not None:
            _mfunc, mcol = _parse_measure(measure)
            if mcol not in fact_cols:
                raise ValueError(
                    f"measure column {mcol!r} is not a fact column of "
                    f"{source.name!r}")
    if not group_by_none and group_by not in fact_cols:
        raise ValueError(
            f"group_by={group_by!r} is not a fact column of "
            f"{source.name!r}; have {sorted(fact_cols)}")

    dims_by_fact = {d.fact_column: d for d in source.dimensions}
    schema = source.schema_name
    pc = source.partition_column

    # Series projection — same joins and :grouped dispatch as
    # build_chart_sql, but the label is what the legend *displays*, not the
    # stable key the plot path transports. The chart fetches series as
    # `source_id` and translates in the browser (`_resolveDimName()` looks the
    # name up in the meta payload; the :grouped path merges dim index → group
    # name via `index_to_group`). A statement pasted into the console has
    # neither step, so it does both here instead — otherwise the copied query
    # returns the right numbers under labels nobody recognises.
    group_dim = None if group_by_none else dims_by_fact.get(group_by)
    joins = ""
    index_to_group: dict[int, str] = {}
    group_colors: dict[str, str] = {}
    series_expr = ""
    if group_by_none:
        series_expr = "'(all)'"
    elif group_by_groups and group_dim and group_dim.dim_table:
        # :grouped — SQL-side CASE mapping, for every graph style. The plot
        # path can afford to return raw indices and fold them in Python
        # (cheaper than a per-row CASE), but a legend row is one row per
        # group either way, so grouping on the name directly costs nothing
        # and keeps the statement self-explanatory.
        name_to_indices = _indices_by_group(group_by_groups)
        for idx, (gname, gcolor) in group_by_groups.items():
            index_to_group[idx] = gname
            group_colors[gname] = gcolor
        whens = []
        for gname, idxs in name_to_indices.items():
            if not idxs:
                continue
            arr = ",".join(str(i) for i in idxs)
            safe = gname.replace("'", "''")
            whens.append(
                f"WHEN f.{group_by} = ANY(ARRAY[{arr}]::smallint[]) "
                f"THEN '{safe}'")
        series_expr = (
            "CASE " + " ".join(whens) + " ELSE '(other)' END"
            if whens else "'(other)'")
    elif group_dim and group_dim.dim_table:
        joins = (f" JOIN {schema}.{group_dim.dim_table} _g "
                 f"ON f.{group_by} = _g.index ")
        if series_label == "index":
            # Explicitly asked for the warehouse key — hand it over as-is.
            series_expr = "_g.index::text"
        else:
            # `name` is nullable in the dim DDL, and the legend falls back to
            # the raw key when it is empty (`_resolveDimName`). NULLIF catches
            # the empty-string case the ETL can leave behind, COALESCE the NULL.
            series_expr = "COALESCE(NULLIF(_g.name, ''), _g.source_id)::text"
    else:
        series_expr = f"f.{group_by}::text"

    validate_filters(source, filter_tree, filter_groups)
    where_parts, flt_params = _build_where(
        schema, pc, t_from, t_to, filter_groups, dims_by_fact,
        filter_tree=filter_tree,
        fact_types={c.name: c.pg_type for c in source.fact_columns})
    params: dict = {**flt_params}

    if graph_style in LADDER_STYLES:
        kind = ladder_kind or "quantile"
        rung_list = normalize_rungs(kind, rungs)
        if kind == "extent":
            if graph_style not in EXTENT_STYLES:
                raise ValueError(
                    f"graph_style={graph_style!r} cannot draw an extent "
                    f"ladder; choose from {sorted(EXTENT_STYLES)}")
            # Unlike the percentile branch below, this one is *exact* over the
            # whole window whichever relation it reads: min of mins, max of
            # maxes, and a count-weighted mean are the same numbers the
            # per-slot arrays average to. That is the same property that lets
            # it read a rollup at all.
            rung_expr = rollup_value_expr or extent_value_expr(
                rung_list, measure_col)
        else:
            if rollup_table or rollup_value_expr:
                raise ValueError(
                    "a percentile ladder can never read a rollup — "
                    "percentiles do not fold; only extent(col) ladders may")
            fractions = ",".join(f"{p / 100.0:g}" for p in rung_list)
            # Single percentile_cont over each series' full distribution —
            # semantically the per-series median / p25 / p75 the legend shows
            # after averaging per-slot percentiles.
            rung_expr = (f"percentile_cont(ARRAY[{fractions}]) "
                         f"WITHIN GROUP (ORDER BY f.{measure_col})")
        sql = (
            f"SELECT "
            f"  {series_expr} AS series, "
            f"  {rung_expr} AS qs "
            f"FROM {schema}.{rollup_table or 'fact'} f "
            f"{joins}"
            f"WHERE {' AND '.join(where_parts)} "
            f"GROUP BY 1 "
            f"ORDER BY 1")
        return sql, params, index_to_group, group_colors

    if rollup_value_expr:
        # Pre-aggregated: the value comes from the rollup's stored aggregates
        # (sum(n) for a count, sum(sum_col) for a sum, the sum/cnt ratio for
        # an avg — measure_expr emits it pre-cast, so no second cast here).
        # Folding rollup rows over the whole window per series is exact for
        # every additive measure; percentiles (ladder branch above) never
        # reach this branch — they read fact unconditionally.
        value_expr = rollup_value_expr
        value_cast = ""
    elif measure is None:
        # count(*): legend sums the per-slot count over the window, which
        # is mathematically identical to count(*) over the whole window.
        value_expr = "count(*)"
        value_cast = "::bigint"
    else:
        mfunc, mcol = _parse_measure(measure)
        if mfunc == "avg":
            # For an avg-measure, the legend averages per-slot means. The
            # SQL aggregate AVG over the whole window has the same mean
            # but is mathematically different; we pick the SQL aggregate
            # because it preserves additive / non-additive distinction
            # expected for an `avg`-typed query at the console.
            value_expr = f"avg(f.{mcol})"
            value_cast = ""
        elif mfunc in ("sum", "min", "max"):
            agg = mfunc
            value_expr = f"{agg}(f.{mcol})"
            value_cast = ""
        else:
            # Defensive — the parser already validated `mfunc` membership.
            value_expr = f"{mfunc}(f.{mcol})"
            value_cast = ""

    sql = (
        f"SELECT "
        f"  {series_expr} AS series, "
        f"  {value_expr}{value_cast} AS v "
        f"FROM {schema}.{rollup_table or 'fact'} f "
        f"{joins}"
        f"WHERE {' AND '.join(where_parts)} "
        f"GROUP BY 1 "
        f"ORDER BY 1")

    return sql, params, index_to_group, group_colors


# ── Literal rendering for the SQL console ──────────────────────────────
#
# `build_chart_sql` returns `(sql, params, …)` and `plot_json`/`ad-hoc`
# paths bind those params via SQLAlchemy's `text(sql), params`. The SQL
# console, however, hands the user statement to psycopg2 via
# `exec_driver_sql(sql)` with no parameter substitution
# (`core/adhoc.py:run_query`) — so `:t_from`, `:t_to`, `:interval`,
# `:interval_start` and `:fltN…` placeholders would die with
# "A value is required for bind parameter …".
#
# `build_chart_sql_rendered` builds the *same* SQL via the helper above
# and then substitutes every bind parameter as a literal in the string.
# Timestamps become `'YYYY-MM-DD HH:MM:SS+00'::timestamptz`, interval
# sizes become bare bigints, filter values become typed
# `ARRAY[…]::smallint[]` (pass-through / grouped) or `ARRAY[…]::text[]`
# (dim-mapped subquery) array literals, LIKE patterns become
# `'…'`-quoted strings with `'` doubled and `\` escaped. The result is
# a single self-contained SELECT that the Run button can execute
# without any further binding.


def _literal_ts(dt: datetime) -> str:
    """Format a tz-aware datetime as a Postgres timestamptz literal."""
    dt = dt.astimezone(timezone.utc)
    return "'{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}+00'::timestamptz".format(
        dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)


def _literal_text(s: str) -> str:
    """SQL string literal: double single-quotes, escape backslashes.

    Order matters — backslash first, otherwise the doubled escape itself
    would be doubled.
    """
    return "'" + s.replace("\\", "\\\\").replace("'", "''") + "'"


def _literal_smallint_array(vals: list[int]) -> str:
    """A Postgres smallint[] literal for an int list."""
    return "ARRAY[" + ",".join(str(int(v)) for v in vals) + "]::smallint[]"


def _literal_text_array(vals: list[str]) -> str:
    """A Postgres text[] literal — each element through `_literal_text`."""
    return "ARRAY[" + ",".join(_literal_text(v) for v in vals) + "]::text[]"


def _render_param(value, suffix: str) -> str:
    """Render one bind-param value as a SQL literal.

    `suffix` is the empty string or `'_oth'` / `'_<j>'` — the trailing
    part of the parameter name after the matched base key (e.g. for
    `:flt0_0` we are called with `value=params['flt0']` and
    `suffix='_0'`). The suffix only matters here for disambiguation in
    the search loop; the value's Python type drives the rendering.
    """
    # _literal_text is also called for LIKE pattern strings (`_filter_sql`
    # always wraps starts_with/contains values in `<v>%` or `%<v>%`).
    if isinstance(value, datetime):
        return _literal_ts(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return _literal_text(value)
    if isinstance(value, list):
        if not value:
            return "ARRAY[]::smallint[]"
        # Element type decides the array cast.
        if all(isinstance(v, bool) or isinstance(v, int) for v in value):
            return _literal_smallint_array([int(v) for v in value])
        return _literal_text_array([str(v) for v in value])
    raise TypeError(f"unsupported bind-param type: {type(value).__name__}")


def _inline_bind_params(sql: str, params: dict) -> str:
    """Replace every `:<key>` placeholder in `sql` with its rendered literal.

    Keys are matched longest-first so that, e.g., `:flt0_oth` and
    `:flt0_0` win over `:flt0`. A `:` followed by a non-identifier char
    (whitespace, punctuation) is left alone — those are not bind
    parameters.
    """
    if not params:
        return sql
    keys_by_len = sorted(params.keys(), key=len, reverse=True)
    ident_re = re.compile(r"[A-Za-z_]\w*")
    out: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        if sql[i] != ":":
            out.append(sql[i])
            i += 1
            continue
        m = ident_re.match(sql, i + 1)
        if not m:
            out.append(sql[i])
            i += 1
            continue
        name = m.group(0)
        matched_key = None
        for k in keys_by_len:
            if name == k or name.startswith(k + "_"):
                matched_key = k
                break
        if matched_key is None:
            # Not a bind param we know about (e.g. literal `:name` inside
            # a JSONB cast). Leave it untouched.
            out.append(sql[i:m.end()])
            i = m.end()
            continue
        out.append(_render_param(params[matched_key], name[len(matched_key):]))
        i = m.end()
    return "".join(out)


def build_chart_sql_rendered(
    source: Source,
    *,
    graph_style: str,
    group_by: str,
    measure: Optional[str],
    t_from: datetime,
    t_to: datetime,
    interval: int,
    filter_tree: Optional[list[dict]],
    filter_groups: Optional[list[list[dict]]] = None,
    rungs: Optional[list] = None,
    ladder_kind: Optional[str] = None,
    measure_col: Optional[str] = None,
    series_label: str = "source_id",
    group_by_groups: Optional[dict[int, tuple[str, str]]] = None,
) -> str:
    """Build the same SQL as `build_chart_sql` but with every bind parameter
    inlined as a literal. The output is a single self-contained SELECT
    that `adhoc.run_query → exec_driver_sql(sql)` can execute without any
    further binding.
    """
    sql, params, _index_to_group, _group_colors = build_chart_sql(
        source,
        graph_style=graph_style,
        group_by=group_by,
        measure=measure,
        t_from=t_from, t_to=t_to, interval=interval,
        filter_tree=filter_tree,
        filter_groups=filter_groups,
        rungs=rungs,
        ladder_kind=ladder_kind,
        measure_col=measure_col,
        series_label=series_label,
        group_by_groups=group_by_groups,
    )
    return _inline_bind_params(sql, params)


def build_chart_legend_sql_rendered(
    source: Source,
    *,
    graph_style: str,
    group_by: str,
    measure: Optional[str],
    t_from: datetime,
    t_to: datetime,
    filter_tree: Optional[list[dict]],
    filter_groups: Optional[list[list[dict]]] = None,
    rungs: Optional[list] = None,
    ladder_kind: Optional[str] = None,
    measure_col: Optional[str] = None,
    series_label: str = "source_id",
    group_by_groups: Optional[dict[int, tuple[str, str]]] = None,
    rollup_table: Optional[str] = None,
    rollup_value_expr: Optional[str] = None,
) -> str:
    """Build the per-series cumulative SQL `updateLegend()` displays.

    Like `build_chart_sql_rendered` but aggregates per series across the
    entire time window — one row per series, no slot projection. Used by
    the "Copy chart SQL" button so the editor receives a self-contained
    SELECT that returns the legend totals, not the bars.
    """
    sql, params, _index_to_group, _group_colors = build_chart_legend_sql(
        source,
        graph_style=graph_style,
        group_by=group_by,
        measure=measure,
        t_from=t_from, t_to=t_to,
        filter_tree=filter_tree,
        filter_groups=filter_groups,
        rungs=rungs,
        ladder_kind=ladder_kind,
        measure_col=measure_col,
        series_label=series_label,
        group_by_groups=group_by_groups,
        rollup_table=rollup_table,
        rollup_value_expr=rollup_value_expr,
    )
    return _inline_bind_params(sql, params)


def timeseries_multibar(
    source: Source,
    *,
    group_by: str,
    t_from: datetime,
    t_to: datetime,
    interval: int,
    filter_groups: Optional[list[list[dict]]] = None,
    filter_tree: Optional[list[dict]] = None,
    measure: Optional[str] = None,
    name: Optional[str] = None,
    series_label: str = "source_id",
    meta_engine=None,
    group_by_groups: Optional[dict[int, tuple[str, str]]] = None,
    group_counts: Optional[dict[str, int]] = None,
    graph_style: str = "multibar",
    rollup_table: Optional[str] = None,
    rollup_value_expr: Optional[str] = None,
    dest_engine=None,
) -> dict:
    """Build the multibar plot dict for `timeseries.js`.

    Args:
      source:        loaded `Source` (provides schema, dim layout, dest engine).
      group_by:      fact column to split into series (one stack segment per
                     distinct value). Empty/None means no breakdown — a single
                     "(all)" series counting every matching row per slot.
      t_from, t_to:  half-open time window [t_from, t_to). Must be tz-aware.
      interval:      seconds per slot. The chart's bar width.
      filter_groups: optional list of filter groups. Each group is a list of
                     conditions OR'd together; groups are AND'd together.
                     Each condition: {"col": str, "op": str, "vals": list}.
                     op is one of: "in", "not_in", "starts_with",
                     "starts_not_with", "contains", "not_contains".
      measure:       aggregation to apply, e.g. 'avg(stay)' or just 'stay'
                     (defaults to sum). None → count(*).
      name:          optional plot label, attached as `name` for the legend.
      series_label:  what to emit as the series key in the JSON.
                     'source_id' (default, stable across re-imports),
                     'name' (human label from the dim table),
                     'index' (warehouse smallint, fastest, opaque).
      meta_engine:   optional SQLAlchemy engine for starcubes_meta lookups.
                     If provided, the response includes `incomplete_until`
                     (unix epoch of the last ETL watermark — slots extending
                     past this may still receive data).
      group_by_groups: optional mapping of dim_index → (group_name, hex_color).
                     When provided, series labels are group names (CASE expr)
                     instead of dim values. Ungrouped indices map to "(other)".
      group_counts:   optional mapping of group_name → total count.
                     Used for ascending sort order (smallest group at bottom).
                     Should include "(other)" with the residual count (sum of
                     dim counts not in any named group); defaults to 0 when
                     missing, which would stack "(other)" at the bottom.
      dest_engine:    optional engine to run the slot query against. Defaults
                     to `source.dest_engine()` — the ETL pool. The chart's
                     request path (viz/views.py) passes `source.request_engine()`
                     instead: a separate pool carrying a libpq connect_timeout,
                     so an unreachable warehouse fails the request with an
                     OperationalError the status line can translate instead of
                     hanging it until harakiri kills the worker.

    Returns the plot dict; an empty result still has the required scaffolding
    (count/min/max = 0, data = {}).
    """
    if t_from.tzinfo is None or t_to.tzinfo is None:
        raise ValueError("t_from and t_to must be timezone-aware")
    if t_to <= t_from:
        raise ValueError(f"t_to ({t_to}) must be after t_from ({t_from})")
    if interval <= 0:
        raise ValueError(f"interval must be positive, got {interval}")
    if series_label not in ("source_id", "name", "index"):
        raise ValueError(
            f"series_label must be 'source_id'|'name'|'index', "
            f"got {series_label!r}")

    # Validate filter tree up front — same rules and error messages the
    # alarm CRUD and evaluator enforce through the module-level helpers.
    group_by_none = not group_by
    fact_cols = {c.name for c in source.fact_columns}
    if not group_by_none and group_by not in fact_cols:
        raise ValueError(
            f"group_by={group_by!r} is not a fact column of "
            f"{source.name!r}; have {sorted(fact_cols)}")
    if measure is not None:
        _mfunc, mcol = _parse_measure(measure)
        if mcol not in fact_cols:
            raise ValueError(
                f"measure column {mcol!r} is not a fact column of "
                f"{source.name!r}")

    # Filter validation lives at module level (validate_filters /
    # check_filter_col / validate_filter_tree) so the alarm CRUD and evaluator
    # reuse the exact same rules and error messages. `build_chart_sql` below
    # validates too — this call only moves the failure ahead of the SQL build,
    # which is where the group_by / measure checks above already fail.
    validate_filters(source, filter_tree, filter_groups)

    sql, params, index_to_group, group_colors = build_chart_sql(
        source,
        graph_style=graph_style,
        group_by=group_by,
        measure=measure,
        t_from=t_from, t_to=t_to, interval=interval,
        filter_tree=filter_tree,
        filter_groups=filter_groups,
        series_label=series_label,
        group_by_groups=group_by_groups,
        rollup_table=rollup_table,
        rollup_value_expr=rollup_value_expr,
    )
    # Read the slot base back out of the bind params rather than recomputing
    # it — that keeps the plot's `interval_start` identical by construction to
    # the `:interval_start` the SQL numbered its slots against.
    interval_start = int(params["interval_start"])
    epoch_to = int(t_to.timestamp())
    group_by_grouped = bool(group_by_groups) and bool(index_to_group)

    with (dest_engine or source.dest_engine()).connect() as conn:
        rows = conn.execute(text(sql), params).all()

    # Prefix integer-like series keys with '_' so JavaScript's JSON.parse
    # treats them as non-integer-index strings and preserves insertion order
    # (JS sorts pure-numeric keys numerically, destroying the count sort).
    def _safe_key(s: str) -> str:
        return f"_{s}" if s.isdigit() else s

    data: dict[int, dict[str, int | float]] = {}
    slot_totals: dict[int, int | float] = {}

    if group_by_grouped:
        # Grouped dimension: SQL returned per-index rows; merge into group
        # names.  This is trivially cheap — a small dict lookup per row —
        # compared to evaluating a per-row CASE expression in SQL.
        for slot, dim_idx, v in rows:
            slot = int(slot)
            v = float(v) if isinstance(v, (float,)) else int(v)
            gname = index_to_group.get(int(dim_idx), "(other)")
            d = data.setdefault(slot, {})
            d[gname] = d.get(gname, 0) + v
            slot_totals[slot] = slot_totals.get(slot, 0) + v
        # Sort series within each slot by ascending group count (smallest at
        # bottom), matching the ungrouped dim-count sort order.
        for slot in data:
            data[slot] = {
                k: data[slot][k]
                for k in sorted(data[slot], key=lambda k: (group_counts or {}).get(k, 0))
            }
    else:
        for slot, series, v in rows:
            slot = int(slot)
            v = float(v) if isinstance(v, (float,)) else int(v)
            data.setdefault(slot, {})[_safe_key(series)] = v
            slot_totals[slot] = slot_totals.get(slot, 0) + v

    # Stacked plots (multibar) report the per-slot stacked total as the
    # value extent; un-stacked plots (multiline, multipoint, stackarea,
    # waterfall) plot each series independently, so the extent is the
    # individual series values. The chart recomputes the visible y-range per
    # type too, but a correct min/max keeps the off-screen fallback right.
    # `multistep` draws on the library's multiline renderer but is un-stacked
    # like multiline, so it belongs in this branch too.
    if graph_style in ("multiline", "multipoint", "multistep",
                       "stackarea", "waterfall"):
        vals = [v for slot in data.values() for v in slot.values()]
        v_min = min(vals) if vals else 0
        v_max = max(vals) if vals else 0
    else:
        v_min = min(slot_totals.values()) if slot_totals else 0
        v_max = max(slot_totals.values()) if slot_totals else 0

    plot: dict = {
        "type":           _simple_plot_type(graph_style),
        "interval_start": interval_start,
        "interval_end":   epoch_to,
        "interval":       interval,
        "count":          len(slot_totals),
        "min":            v_min,
        "max":            v_max,
        "data":           data,
    }
    if name is not None:
        plot["name"] = name

    step = SIMPLE_PLOT_STEP.get(graph_style)
    if step:
        plot["step"] = step

    if group_colors:
        plot["series_colors"] = group_colors

    if meta_engine is not None:
        with meta_engine.begin() as meta:
            hwm = meta.execute(text(
                "SELECT last_end_time FROM starcubes_meta.etl_run "
                " WHERE datasource_id = :ds AND status = 'ok' "
                " ORDER BY finished_at DESC LIMIT 1"
            ), {"ds": source.id}).scalar()
        if hwm is not None:
            if hwm.tzinfo is None:
                hwm = hwm.replace(tzinfo=timezone.utc)
            else:
                hwm = hwm.astimezone(timezone.utc)
            plot["incomplete_until"] = int(hwm.timestamp())

    return plot


def timeseries_ladder(
    source: Source,
    *,
    measure_col: str,
    rungs: list,
    ladder_kind: str = "quantile",
    group_by: str,
    t_from: datetime,
    t_to: datetime,
    interval: int,
    filter_groups: Optional[list[list[dict]]] = None,
    filter_tree: Optional[list[dict]] = None,
    name: Optional[str] = None,
    series_label: str = "source_id",
    meta_engine=None,
    group_by_groups: Optional[dict[int, tuple[str, str]]] = None,
    group_counts: Optional[dict[str, int]] = None,
    graph_style: str = "quantile-bands",
    rollup_table: Optional[str] = None,
    rollup_value_expr: Optional[str] = None,
    dest_engine=None,
) -> dict:
    """Build a ladder plot dict for `timeseries.js`.

    Like `timeseries_multibar`, but instead of one aggregate per slot/series it
    computes a whole *rung array* of `measure_col` per slot/series. The plot's
    `data[slot][series]` is a list aligned to the (ascending) `rungs` ladder;
    the renderer draws one line per rung (and shades the bands between them
    for ``quantile-bands``).

    Two feeders produce that array, selected by ``ladder_kind``:

    * ``'quantile'`` — Postgres `percentile_cont` over `fact`. Percentiles do
      not fold, so this one can never read a rollup.
    * ``'extent'`` — `min`/`avg`/`max`, each of which folds across buckets
      exactly. This one *can* read a rollup, via `rollup_table` /
      `rollup_value_expr` exactly as `timeseries_multibar` does.

    ``graph_style`` selects one of the ladder styles in `LADDER_STYLES`:
    ``quantile-bands``, ``quantile-steps`` (+ ``-open`` → no risers),
    ``error-bars`` and ``candlestick``. All of them read the same per-slot
    array — only ``plot["type"]`` (and sometimes ``plot["connect"]``)
    differs. See `_ladder_plot_type`. An extent ladder is restricted to
    `EXTENT_STYLES`, the three that do not read the rungs as nested pairs.

    Series come from `group_by` exactly as in multibar: empty → a single
    "(all)" series; a plain dimension → one series per dim value; a grouped
    dimension (`group_by_groups` supplied) → one series per named group, with
    "(other)" catching unnamed indices. Grouped percentiles are computed
    SQL-side (a CASE maps the dim index to its group name before aggregation) —
    percentiles cannot be merged in Python the way counts are.

    Args mirror `timeseries_multibar`; `measure_col` is the numeric fact column
    the ladder is taken over and `rungs` is the ladder itself — integer
    percentiles for ``'quantile'``, `EXTENT_RUNGS` tokens for ``'extent'``.
    """
    if t_from.tzinfo is None or t_to.tzinfo is None:
        raise ValueError("t_from and t_to must be timezone-aware")
    if t_to <= t_from:
        raise ValueError(f"t_to ({t_to}) must be after t_from ({t_from})")
    if interval <= 0:
        raise ValueError(f"interval must be positive, got {interval}")
    if series_label not in ("source_id", "name", "index"):
        raise ValueError(
            f"series_label must be 'source_id'|'name'|'index', "
            f"got {series_label!r}")
    if graph_style not in LADDER_STYLES:
        raise ValueError(
            f"graph_style={graph_style!r} is not a ladder style; "
            f"choose from {sorted(LADDER_STYLES)}")

    rung_list = normalize_rungs(ladder_kind, rungs)

    sql, params, _index_to_group, group_colors = build_chart_sql(
        source,
        graph_style=graph_style,
        group_by=group_by,
        measure=None,
        t_from=t_from, t_to=t_to, interval=interval,
        filter_tree=filter_tree,
        filter_groups=filter_groups,
        rungs=rung_list,
        ladder_kind=ladder_kind,
        measure_col=measure_col,
        series_label=series_label,
        group_by_groups=group_by_groups,
        rollup_table=rollup_table,
        rollup_value_expr=rollup_value_expr,
    )
    interval_start = int(params["interval_start"])
    epoch_to = int(t_to.timestamp())

    with (dest_engine or source.dest_engine()).connect() as conn:
        rows = conn.execute(text(sql), params).all()

    def _safe_key(s: str) -> str:
        return f"_{s}" if s.isdigit() else s

    data: dict[int, dict[str, list[float]]] = {}
    v_min = None
    v_max = None
    for slot, series, qs in rows:
        slot = int(slot)
        # A rung can be NULL where `percentile_cont` never was: an extent
        # ladder's avg rung is NULL for a bin whose column held only NULLs —
        # `count(col) = 0` → `NULLIF(…, 0)` → NULL on the rollup, and `avg()`
        # over no non-NULL row → NULL on `fact`. Carried through as None
        # rather than coerced to 0.0, which would draw a band reaching down
        # to zero for a bin that measured nothing at all.
        vals = [None if x is None else float(x) for x in qs]
        data.setdefault(slot, {})[_safe_key(str(series))] = vals
        present = [x for x in vals if x is not None]
        if not present:
            continue
        # Still first-and-last: dropping NULLs out of an ascending ladder
        # leaves it ascending.
        lo, hi = present[0], present[-1]
        v_min = lo if v_min is None else min(v_min, lo)
        v_max = hi if v_max is None else max(v_max, hi)

    lib_type, connect = _ladder_plot_type(graph_style)
    plot: dict = {
        "type":           lib_type,
        "interval_start": interval_start,
        "interval_end":   epoch_to,
        "interval":       interval,
        "count":          len(data),
        "min":            v_min if v_min is not None else 0,
        "max":            v_max if v_max is not None else 0,
        # The library's field is `percentiles` for *both* ladder kinds, and it
        # accepts a string array — `renderers.js:ladderPairs` documents
        # `[min, avg, max]` as its first example and `tooltip.js:rungLabel`
        # prints a token rung verbatim where it prints a numeric one as `p50`.
        # Do not rename this key to match the Python parameter: it is the
        # library contract, and nothing server-side would catch the break.
        "percentiles":    rung_list,
        "data":           data,
    }
    if not connect:
        plot["connect"] = False
    if name is not None:
        plot["name"] = name
    if group_colors:
        plot["series_colors"] = group_colors

    if meta_engine is not None:
        with meta_engine.begin() as meta:
            hwm = meta.execute(text(
                "SELECT last_end_time FROM starcubes_meta.etl_run "
                " WHERE datasource_id = :ds AND status = 'ok' "
                " ORDER BY finished_at DESC LIMIT 1"
            ), {"ds": source.id}).scalar()
        if hwm is not None:
            if hwm.tzinfo is None:
                hwm = hwm.replace(tzinfo=timezone.utc)
            else:
                hwm = hwm.astimezone(timezone.utc)
            plot["incomplete_until"] = int(hwm.timestamp())

    return plot


def _gap_where(
    source: Source,
    *,
    filter_tree=None,
    filter_groups=None,
    include_time_range: bool = True,
    t_from: Optional[datetime] = None,
    t_to: Optional[datetime] = None,
) -> tuple[list[str], dict]:
    """Return (where_parts, params) for the gap queries.

    Thin wrapper around `_build_where` so the gap scan and the day profile
    share one filter-SQL builder with the chart queries — the chart's filters
    and the profile's must be the same filter, or λ̂(h) describes a different
    stream than the one the chart displays.
    """
    validate_filters(source, filter_tree, filter_groups)
    dims_by_fact = {d.fact_column: d for d in source.dimensions}
    return _build_where(
        source.schema_name, source.partition_column,
        t_from, t_to, filter_groups, dims_by_fact,
        filter_tree=filter_tree,
        include_time_range=include_time_range,
        fact_types={c.name: c.pg_type for c in source.fact_columns},
    )


def _profile_integral(
    rate_per_s: dict[int, float],
    start_epoch: int,
    end_epoch: int,
) -> float:
    """Integrate the day profile λ̂(h) over [start_epoch, end_epoch).

    μ = Σ λ̂(h)·overlap_s over the UTC hours the span touches. λ̂ = 0 for an
    hour-of-day that never saw traffic contributes nothing — a span that
    covers only silent hours stays unmarked by construction ("no traffic
    expected") instead of via a special case. `GAP_MAX_INTEGRAL_HOURS` bounds
    the loop; a span that long is marked long before the cap matters.
    """
    if end_epoch <= start_epoch:
        return 0.0
    mu = 0.0
    seg = start_epoch
    # Walk hour segments; cap the loop at GAP_MAX_INTEGRAL_HOURS segments.
    # `seg = seg_end` at the foot is what advances the walk: without it the
    # loop re-added the *first* segment's contribution GAP_MAX_INTEGRAL_HOURS
    # times over. A span that ended inside its own hour was unaffected (the
    # `seg_end >= end_epoch` break fires on the first pass), so only spans
    # crossing an hour boundary were wrong — and wrong by a factor of 400,
    # which turned a few seconds of quiet just before the full hour into a
    # 6σ outage.
    for _ in range(GAP_MAX_INTEGRAL_HOURS):
        seg_start = (seg // 3600) * 3600
        h = (seg_start % 86400) // 3600
        seg_end = seg_start + 3600
        overlap = min(end_epoch, seg_end) - max(start_epoch, seg_start)
        if overlap <= 0:
            break
        mu += rate_per_s.get(h, 0.0) * overlap
        if seg_end >= end_epoch:
            break
        seg = seg_end
    return mu


def gap_profile_bounds(
    source: Source,
    t_to_epoch: int,
    hwm_epoch: Optional[int],
    profile_days: int,
) -> tuple[int, int]:
    """Return (prof_from, prof_to) epoch bounds for the gap day profile.

    UTC-midnight of the last fully-watermarked day (the profile window ends
    at the last fully-seen day, never at a partial one); it looks at the
    `profile_days` days before that, clamped up at the retention floor (same
    precedence as core/audit._clamp_to_floor) and never below its own end.

    Module-level so `viz.views.gaps_json` can build the profile's cache key
    before `detect_gaps` runs — the profile is the expensive multi-day scan,
    the gap rows the incremental part, so the two take different cache
    treatment.
    """
    horizon = t_to_epoch if hwm_epoch is None else min(t_to_epoch, hwm_epoch)
    prof_to = (horizon // 86400) * 86400
    prof_from = prof_to - profile_days * 86400
    floor_dt = getattr(source, "retention_floor", None)
    if floor_dt is not None:
        if floor_dt.tzinfo is None:
            floor_dt = floor_dt.replace(tzinfo=timezone.utc)
        prof_from = max(prof_from, int(floor_dt.timestamp()))
    return min(prof_from, prof_to), prof_to


def gap_scan_bounds(
    source: Source,
    t_from: datetime,
    t_to: datetime,
    hwm: Optional[datetime],
) -> tuple[int, int, bool]:
    """Return (scan_from, scan_to, truncated) epoch bounds for one gap scan.

    Mirror piece of `gap_profile_bounds` and extracted from `detect_gaps`'s
    inline clamping: the scan's lower bound rises to the retention floor
    (below it `fact` is empty *on purpose* — reading that span would report
    the disposal as a gap), the upper bound falls to the watermark (behind it
    there is by definition no data to be a gap), and the whole span is capped
    at `GAP_MAX_SCAN_S` — cut at the **end**, not the start, because the fresh
    edge of the window is the provisional one and the cut remainder is simply
    re-requested by the client.

    Naive datetimes are UTC by project convention (same normalization the
    HWM readers in this file apply).
    """
    def _utc(dt: datetime) -> datetime:
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    scan_from = _utc(t_from)
    scan_to = _utc(t_to)
    floor_dt = getattr(source, "retention_floor", None)
    if floor_dt is not None:
        floor_dt = _utc(floor_dt)
        if scan_from < floor_dt:
            scan_from = floor_dt
    if hwm is not None:
        scan_to = min(scan_to, _utc(hwm))
    truncated = False
    if (scan_to - scan_from).total_seconds() > GAP_MAX_SCAN_S:
        scan_to = scan_from + timedelta(seconds=GAP_MAX_SCAN_S)
        truncated = True
    return int(scan_from.timestamp()), int(scan_to.timestamp()), truncated


def _utc_ts(source: Source, expr: str) -> str:
    """`expr` normalised to a naive UTC timestamp, for hour/day grouping.

    `extract(hour from …)` and `date_trunc('day', …)` over a `timestamptz` are
    evaluated in the **session** time zone, not in UTC: a server running on
    Europe/Berlin reports hour 21 for 19:00 UTC. Everything that reads those
    groupings back — `_profile_integral`, the estimate dialog's hour table,
    `gap_reference_drill`'s hour restriction — keys on the *UTC* hour, so the
    two frames have to be forced together or λ̂(h) describes a different hour
    than the one it is asked about (and the offset changes twice a year with
    DST). A column that is already `timestamp without time zone` is left alone:
    it is taken as UTC, which is how this module has always bound its
    parameters.
    """
    pc_type = next((c.pg_type for c in source.fact_columns
                    if c.name == source.partition_column), None)
    tz_aware = bool(pc_type) and 'with time zone' in pc_type.replace('tz', ' with time zone')
    return f"({expr} AT TIME ZONE 'UTC')" if tz_aware else expr


def gap_day_profile(
    source: Source,
    *,
    prof_from: int,
    prof_to: int,
    filter_tree=None,
    filter_groups=None,
    table: Optional[str] = None,
    count_expr: str = "count(*)",
    dest_engine=None,
) -> dict:
    """Day profile λ̂(h) for the gap overlay: rate per hour-of-day in 1/s.

    One aggregation over the profile window [prof_from, prof_to) (epoch
    seconds, day-aligned by `gap_profile_bounds`), grouped by UTC hour
    (forced there by `_utc_ts` — the session time zone would otherwise
    decide, and every reader of λ̂ keys on the UTC hour) and
    day so the response can carry both the rate and the per-day series the
    estimate dialog needs. `table=None` reads `{schema}.fact` with
    `count(*)`; otherwise `{schema}.{table}` with the given `count_expr`
    (e.g. `sum(f.n)` — **never** `count(*)` over rollup rows, each rollup
    row already aggregates a bucket's worth of fact rows; see
    `core.rollup.measure_expr`). The *filters* are the chart's, shared with
    the scan through `_gap_where`; only the window differs.

    **The denominator is the window length, not `count(distinct day)`.** The
    previous profile query divided each hour's count by the number of days
    that hour saw traffic, so a completely silent day inside the window — a
    source outage, a weekend the stream never sees — lifted every hour's λ̂
    by the same factor. The window length is one denominator for the rate
    and for the zero-fill of `per_day` alike: a day the whole stream was
    down enters `per_day` as zeros, which is exactly what min/max in the
    estimate dialog should report for it.

    Payload shape (string keys — it goes through JSON caching verbatim):

        {"prof_from": int, "prof_to": int, "days": float,
         "hours":   {str h: rate_per_s},
         "per_day": {str h: [n per day, zero-filled to the day count]},
         "source":  "fact" | table}

    `days` is `(prof_to - prof_from) / 86400` — the window length, which for
    a floor-clamped `prof_from` can be fractional.
    """
    schema = source.schema_name
    pc = source.partition_column
    tbl = f"{schema}.{table}" if table else f"{schema}.fact"
    utc_pc = _utc_ts(source, f"f.{pc}")
    expr = count_expr or "count(*)"

    prof_from = int(prof_from)
    prof_to = int(prof_to)
    n_slots = max(1, int(math.ceil((prof_to - prof_from) / 86400.0)))
    days = (prof_to - prof_from) / 86400.0

    time_parts = [f"f.{pc} >= :prof_from", f"f.{pc} < :prof_to"]
    flt_parts, flt_params = _gap_where(
        source, filter_tree=filter_tree, filter_groups=filter_groups,
        include_time_range=False)
    where = time_parts + flt_parts
    params = {"prof_from": datetime.fromtimestamp(prof_from, tz=timezone.utc),
              "prof_to": datetime.fromtimestamp(prof_to, tz=timezone.utc),
              **flt_params}

    sql = f"""
        SELECT extract(hour from {utc_pc})::int AS h,
               date_trunc('day', {utc_pc}) AS d,
               {expr} AS n
          FROM {tbl} f
         WHERE {" AND ".join(where) or "TRUE"}
         GROUP BY 1, 2
    """
    with (dest_engine or source.dest_engine()).connect() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
    per_day: dict[int, list[float]] = {}
    for row in rows:
        h = int(row["h"])
        d = row["d"]
        d_ts = d.timestamp() if d.tzinfo is not None else \
            d.replace(tzinfo=timezone.utc).timestamp()
        slot = int(round((d_ts - prof_from) / 86400.0))
        if not 0 <= slot < n_slots:
            continue
        n = row["n"]
        if not isinstance(n, int):           # Decimal (sum over bigint) → float
            n = float(n)
        per_day.setdefault(h, [0.0] * n_slots)[slot] = n

    hours: dict[int, float] = {}
    for h, series in per_day.items():
        hours[h] = sum(series) / (days * 3600.0)
    return {
        "prof_from": prof_from,
        "prof_to": prof_to,
        "days": days,
        # String keys so the payload survives a JSON cache roundtrip
        # byte-identical; readers re-key to int (`_normalize_gap_profile`).
        "hours": {str(h): r for h, r in hours.items()},
        "per_day": {str(h): s for h, s in per_day.items()},
        "source": table or "fact",
    }


def gap_neighbor_rate(
    source: Source,
    *,
    t_from: int,
    t_to: int,
    filter_tree=None,
    filter_groups=None,
    dest_engine=None,
) -> Optional[float]:
    """Mean event rate (rows per second) over [t_from, t_to) from `fact`.

    The gap estimate dialog's neighbour interpolation: the rate the stream
    actually carried just before/after the gap, as one small COUNT over the
    chart's filters (shared with the scan and the profile via `_gap_where`).
    Returns None for an empty or inverted window — the caller decides what
    an unavailable neighbour means; it does not silently become 0.
    """
    t_from = int(t_from)
    t_to = int(t_to)
    if t_to <= t_from:
        return None
    where, params = _gap_where(
        source, filter_tree=filter_tree, filter_groups=filter_groups,
        include_time_range=True,
        t_from=datetime.fromtimestamp(t_from, tz=timezone.utc),
        t_to=datetime.fromtimestamp(t_to, tz=timezone.utc))
    sql = f"""
        SELECT count(*) FROM {source.schema_name}.fact f
         WHERE {" AND ".join(where) or "TRUE"}
    """
    with (dest_engine or source.dest_engine()).connect() as conn:
        n = conn.execute(text(sql), params).scalar()
    return (n or 0) / (t_to - t_from)


def detect_gaps(
    source: Source,
    *,
    t_from: datetime,
    t_to: datetime,
    filter_tree=None,
    filter_groups=None,
    profile_days: int = GAP_PROFILE_DAYS,
    alpha: float = GAP_ALPHA,
    max_gaps: int = GAP_MAX_CANDIDATES,
    hwm: Optional[datetime] = None,
    profile: Optional[dict] = None,
    dest_engine=None,
) -> dict:
    """Detect 6σ event gaps for the chart's gap overlay (count(*) only).

    An empty second of the event stream behind the chart's filters is what
    the overlay draws; the scan groups `fact` at second resolution
    (`GAP_SCAN_INTERVAL_S`) whatever tier the chart is showing, so the gap's
    edges are the first/last *transaction*, not bucket boundaries.
    `group_by` is deliberately not a parameter: the test runs on the
    **total** stream (a second is empty when no row falls into it, whatever
    the chart's breakdown is) — that is the question the chart visually asks,
    and it keeps the scan from scaling with the series count.

    Reads `{schema}.fact` always, never a rollup: a rollup carries min/max
    only as bucket-start values of its own bucket column, not as
    transaction timestamps, so exact edge timestamps would be wrong. (The
    *profile* λ̂(h) may come from a rollup — that decision lives in
    `viz.views._load_gap_profile`, which hands it in pre-loaded.)

    The 6σ test is an integral over the day profile λ̂(h) (hour-of-day rate
    over the last `profile_days` days, same filters as the chart):
    μ = ∫λ̂ dt must exceed −ln(α) ≈ 20.7557 expected events. λ̂(h) = 0 hours
    contribute 0 — a span over silent hours stays unmarked by construction.

    `profile` is a **required** interface in the normal request path: the
    caller (viz.views._load_gap_profile) loads it rollup-first with fact
    fallback and caches it server-side under kind="gap_profile". When None,
    the profile is computed here via `gap_day_profile` **without any
    caching** — the profile is the expensive multi-day scan and the view is
    its caching layer, so a caller that skips the view pays the scan every
    time; management commands may do exactly that.

    A λ̂max = 0 profile means no traffic is expected anywhere in the day, so
    the 6σ integral can never fire and the scan is skipped entirely
    (gaps = [], candidate_gaps = 0). Otherwise the scan SQL prefilters with
    `min_len_s = GAP_THRESHOLD_EVENTS / λ̂max`: a gap shorter than that
    cannot reach μ > −ln(α) even at the day's highest rate, so carrying it
    through the window function and the Python μ-test would be work the
    integral discards anyway.

    Returns a dict as documented in `viz/views.py gaps_json`.
    """
    if t_from.tzinfo is None or t_to.tzinfo is None:
        raise ValueError("t_from and t_to must be timezone-aware")
    if t_to <= t_from:
        raise ValueError(f"t_to ({t_to}) must be after t_from ({t_from})")
    if not (0 < alpha < 1):
        alpha = GAP_ALPHA
    if not (1 <= profile_days <= 30):
        profile_days = GAP_PROFILE_DAYS
    # Ahead of the profile query and the scan, both of which reach SQL
    # through `_gap_where` — which validates as well; this is the fail-fast.
    validate_filters(source, filter_tree, filter_groups)

    epoch_from_req = int(t_from.timestamp())
    epoch_to_req = int(t_to.timestamp())
    schema = source.schema_name
    pc = source.partition_column

    # Naive datetimes are UTC by project convention (same normalization the
    # HWM readers in this file apply).
    if hwm is not None and hwm.tzinfo is None:
        hwm = hwm.replace(tzinfo=timezone.utc)

    def _epoch(dt: datetime) -> int:
        if dt.tzinfo is None:
            return int(dt.replace(tzinfo=timezone.utc).timestamp())
        return int(dt.timestamp())

    floor_dt = getattr(source, "retention_floor", None)
    if floor_dt is not None and floor_dt.tzinfo is None:
        floor_dt = floor_dt.replace(tzinfo=timezone.utc)

    # --- clamps (extracted into gap_scan_bounds) --------------------------
    scan_from_epoch, scan_to_epoch, scan_truncated = gap_scan_bounds(
        source, t_from, t_to, hwm)
    if scan_to_epoch <= scan_from_epoch:
        # Nothing to scan (window entirely below the floor or behind the
        # watermark): the full scaffolding with empty gaps. The pre-loaded
        # profile is echoed so the client keeps its λ̂ context; without one
        # the scaffolding stays as bare as before.
        if profile is not None:
            prof_from = int(profile["prof_from"])
            prof_to = int(profile["prof_to"])
            days_total = float(profile["days"])
            rate_per_s = {int(h): float(r)
                          for h, r in profile["hours"].items()}
            prof_source = profile.get("source") or "fact"
        else:
            prof_from = prof_to = None
            days_total = 0.0
            rate_per_s = {}
            prof_source = None
        return {
            "from": epoch_from_req,
            "to": epoch_to_req,
            "scanned_from": scan_from_epoch,
            "scanned_to": scan_to_epoch,
            "scan_truncated": scan_truncated,
            "data_until": _epoch(hwm) if hwm is not None else None,
            "floor": int(floor_dt.timestamp()) if floor_dt is not None else None,
            "alpha": alpha,
            "expected_threshold": -math.log(alpha),
            "profile": {"prof_from": prof_from, "prof_to": prof_to,
                        "days": float(days_total), "rate_per_s": {},
                        "source": prof_source},
            "gaps": [],
            "candidate_gaps": 0,
            "truncated": False,
        }

    # --- day profile λ̂(h) -------------------------------------------------
    if profile is not None:
        prof_from = int(profile["prof_from"])
        prof_to = int(profile["prof_to"])
        days_total = float(profile["days"])
        rate_per_s = {int(h): float(r)
                      for h, r in profile["hours"].items()}
        prof_source = profile.get("source") or "fact"
    else:
        prof_from, prof_to = gap_profile_bounds(
            source, epoch_to_req,
            int(hwm.timestamp()) if hwm is not None else None, profile_days)
        payload = gap_day_profile(
            source, prof_from=prof_from, prof_to=prof_to,
            filter_tree=filter_tree, filter_groups=filter_groups,
            dest_engine=dest_engine)
        prof_from = int(payload["prof_from"])
        prof_to = int(payload["prof_to"])
        days_total = float(payload["days"])
        rate_per_s = {int(h): float(r)
                      for h, r in payload["hours"].items()}
        prof_source = payload["source"]

    # --- gap scan ---------------------------------------------------------
    gaps: list[dict] = []
    candidate_gaps = 0
    lam_max = max(rate_per_s.values(), default=0.0)
    if lam_max > 0 and scan_to_epoch > scan_from_epoch:
        # SQL prefilter: a gap of L seconds can reach μ = λ̂·L at best (the
        # day's peak rate), so anything below GAP_THRESHOLD_EVENTS / λ̂max
        # cannot pass the integral test even at the most favourable hour —
        # running it through the window function and the per-gap integral
        # would be pure discard. GAP_MIN_SPAN_S floors it, because with 1 s
        # buckets a distance of 1 s is two adjacent occupied seconds: no
        # empty second at all.
        min_len_s = max(GAP_THRESHOLD_EVENTS / lam_max, GAP_MIN_SPAN_S)

        gap_where, gap_params = _gap_where(
            source, filter_tree=filter_tree, filter_groups=filter_groups,
            include_time_range=True,
            t_from=datetime.fromtimestamp(scan_from_epoch, tz=timezone.utc),
            t_to=datetime.fromtimestamp(scan_to_epoch, tz=timezone.utc))
        gap_sql = f"""
            WITH occ AS (
              SELECT floor(extract(epoch from f.{pc}))::bigint AS sec,
                     min(f.{pc}) AS first_txn, max(f.{pc}) AS last_txn
                FROM {schema}.fact f
               WHERE {" AND ".join(gap_where)}
               GROUP BY 1)
            SELECT prev_sec, sec, prev_last_txn, first_txn
              FROM (SELECT sec, first_txn, last_txn,
                           lag(sec) OVER (ORDER BY sec) AS prev_sec,
                           lag(last_txn) OVER (ORDER BY sec)
                               AS prev_last_txn
                      FROM occ) g
             WHERE prev_sec IS NOT NULL AND sec - prev_sec >= :min_len_s
             ORDER BY (sec - prev_sec) DESC
             LIMIT :max_gaps
        """
        gap_params = {**gap_params,
                      "min_len_s": min_len_s,
                      "max_gaps": max_gaps}
        with (dest_engine or source.dest_engine()).connect() as conn:
            gap_rows = conn.execute(text(gap_sql),
                                    gap_params).mappings().all()

        # Widest-first from the SQL keeps the cap's survivors the wide ones;
        # the response is ascending so the client can merge seam-to-seam.
        candidate_gaps = len(gap_rows)
        for row in gap_rows:
            left_ms = _epoch_ms(row["prev_last_txn"])
            right_ms = _epoch_ms(row["first_txn"])
            # The μ-test runs over the transaction distance, not the bucket
            # distance: the gap is the empty span *between* the last
            # transaction before it and the first one after.
            left_s = left_ms // 1000
            right_s = right_ms // 1000
            span_s = round((right_ms - left_ms) / 1000.0, 3)
            mu = _profile_integral(rate_per_s, left_s, right_s)
            if mu <= GAP_THRESHOLD_EVENTS:
                continue
            gaps.append({
                "left_txn_ms": left_ms,
                "right_txn_ms": right_ms,
                "span_s": span_s,
                "expected_events": mu,
                # Equivalent flat-rate threshold for the tooltip: the gap
                # length at which −ln(α) events would have been expected at
                # the span's own average rate μ/span_s.
                "threshold_s": (GAP_THRESHOLD_EVENTS / (mu / span_s))
                               if span_s > 0 else None,
            })
    gaps.sort(key=lambda g: g["left_txn_ms"])

    return {
        "from": epoch_from_req,
        "to": epoch_to_req,
        "scanned_from": scan_from_epoch,
        "scanned_to": scan_to_epoch,
        "scan_truncated": scan_truncated,
        "data_until": _epoch(hwm) if hwm is not None else None,
        "floor": int(floor_dt.timestamp()) if floor_dt is not None else None,
        "alpha": alpha,
        "expected_threshold": -math.log(alpha),
        "profile": {
            "prof_from": prof_from,
            "prof_to": prof_to,
            "days": float(days_total),
            "rate_per_s": {str(h): r for h, r in rate_per_s.items()},
            "source": prof_source,
        },
        "gaps": gaps,
        "candidate_gaps": candidate_gaps,
        "truncated": candidate_gaps >= max_gaps,
    }


def _epoch_ms(dt: datetime) -> int:
    """Transaction timestamp → integer epoch milliseconds (tz-safe)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(round(dt.timestamp() * 1000))


def _build_bar_where(
    source: Source,
    group_by: str,
    group_by_val: str,
    t_from: datetime,
    t_to: datetime,
    filter_groups: Optional[list[list[dict]]],
    filter_tree: Optional[list[dict]] = None,
    group_by_indices: Optional[dict] = None,
) -> tuple[list[str], dict]:
    """Return (where_parts, params) for a bar-scoped fact query.

    Shared by drill_down and list_transactions so filter logic isn't duplicated.

    group_by_indices: when the group_by column is a grouped dimension, this
    maps the clicked group name to the dim indices it contains. Keys are
    group names (including "(other)"); values are sorted lists of ints.
    When provided, gb_where uses = ANY(:_gb_indices) instead of source_id
    lookup. When group_by_val is "(other)", all named-group indices are
    excluded.
    """
    schema = source.schema_name
    pc = source.partition_column
    dims_by_fact = {d.fact_column: d for d in source.dimensions}

    gb_dim = dims_by_fact.get(group_by) if group_by else None
    if not group_by:
        # No breakdown: the bar covers every row in the slot, no gb filter.
        gb_where = None
        base_params = {"_t_from": t_from, "_t_to": t_to}
    elif group_by_indices is not None:
        # Grouped dimension: group_by_val is a group name, not a source_id.
        if group_by_val == "(other)":
            # "(other)" matches all indices NOT in any named group *of the
            # addressed group set* — group_by_indices already covers exactly
            # that set, so no second lookup on the dimension is needed.
            all_named = sorted(
                {i for idxs in group_by_indices.values() for i in idxs})
            gb_where = f"f.{group_by} != ALL(:_gb_named_indices)"
            base_params: dict = {
                "_t_from": t_from, "_t_to": t_to,
                "_gb_named_indices": all_named,
            }
        else:
            indices = group_by_indices.get(group_by_val, [])
            gb_where = f"f.{group_by} = ANY(:_gb_indices)"
            base_params: dict = {
                "_t_from": t_from, "_t_to": t_to,
                "_gb_indices": indices,
            }
    elif gb_dim and gb_dim.dim_table:
        gb_where = (
            f"f.{group_by} IN "
            f"(SELECT index FROM {schema}.{gb_dim.dim_table} "
            f" WHERE source_id = :_gb_val)"
        )
        base_params: dict = {"_t_from": t_from, "_t_to": t_to, "_gb_val": group_by_val}
    else:
        gb_where = f"f.{group_by}::text = :_gb_val"
        base_params: dict = {"_t_from": t_from, "_t_to": t_to, "_gb_val": group_by_val}

    base_parts = [f"f.{pc} >= :_t_from", f"f.{pc} < :_t_to"]
    if gb_where is not None:
        base_parts.append(gb_where)

    validate_filters(source, filter_tree, filter_groups)
    flt_parts, flt_params = _build_where(
        schema, pc, t_from, t_to, filter_groups, dims_by_fact,
        filter_tree=filter_tree, include_time_range=False,
        fact_types={c.name: c.pg_type for c in source.fact_columns})

    return base_parts + flt_parts, {**base_params, **flt_params}


def drill_down(
    source: Source,
    engine,
    group_by: str,
    group_by_val: str,
    t_from: datetime,
    t_to: datetime,
    filter_groups: Optional[list[list[dict]]] = None,
    limit: int = 20,
    filter_tree: Optional[list[dict]] = None,
    group_by_indices: Optional[dict] = None,
) -> list[dict]:
    """For each dimension column with a dim table (excluding group_by), return
    the top `limit` values by count within the given time window and filter
    context.

    group_by_indices: when group_by is a grouped dimension, maps group names
    to sorted lists of dim indices. Passed through to _build_bar_where.

    Returns list of:
      {"column": str, "dim_table": str,
       "values": [{"source_id": str, "name": str, "count": int}]}
    """
    schema = source.schema_name
    dims_by_fact = {d.fact_column: d for d in source.dimensions}

    shared_where, shared_params = _build_bar_where(
        source, group_by, group_by_val, t_from, t_to, filter_groups,
        filter_tree=filter_tree, group_by_indices=group_by_indices,
    )
    where_base = " AND ".join(shared_where)
    results = []

    with engine.begin() as conn:
        for fc in sorted(source.fact_columns, key=lambda c: c.ordinal):
            if fc.role not in ("dimension", "auto-dimension"):
                continue
            dm = dims_by_fact.get(fc.name)
            if not dm or not dm.dim_table:
                continue
            if fc.name == group_by:
                continue
            params = dict(shared_params)
            params["_limit"] = limit
            params["_unk"]   = UNKNOWN_SOURCE_ID
            sql = text(
                f"SELECT d.source_id, d.name, count(*) AS cnt "
                f"FROM {schema}.fact f "
                f"JOIN {schema}.{dm.dim_table} d ON f.{fc.name} = d.index "
                f"WHERE {where_base} AND d.source_id != :_unk "
                f"GROUP BY d.source_id, d.name "
                f"ORDER BY cnt DESC, d.name NULLS LAST "
                f"LIMIT :_limit"
            )
            rows = conn.execute(sql, params).all()
            if rows:
                results.append({
                    "column":    fc.name,
                    "dim_table": dm.dim_table,
                    "values":    [
                        {"source_id": r[0], "name": r[1] or r[0], "count": int(r[2])}
                        for r in rows
                    ],
                })
    return results


def gap_reference_drill(
    source: Source,
    engine,
    *,
    prof_from: int,
    prof_to: int,
    hours: list[int],
    mu: float,
    filter_tree=None,
    filter_groups=None,
    limit: int = 20,
) -> list[dict]:
    """Dimension breakdown for a gap, interpolated from the profile window.

    A gap holds no rows by definition, so there is nothing to count — the
    breakdown has to come from a comparison set and be scaled to the expected
    count μ. The comparison set is the **profile window** (`gap_profile_bounds`)
    restricted to the hours-of-day the gap touches: the same window and the
    same `extract(hour from …)` grouping `gap_day_profile` derives λ̂ from, so
    the shares and the total they are scaled by come from one body of data
    rather than two. A neighbour window either side of the gap would answer a
    different question — what happened to be running — and freeze a passing
    anomaly into the estimate.

    The gap rarely sits on hour boundaries (14:03 → 14:41); taking the whole
    touched hour as the reference is deliberate. Only the *ratios* are read
    here, and μ — computed over the exact transaction distance — supplies the
    magnitude.

    Shares are read per dimension in one query each, the denominator coming
    from `sum(count(*)) OVER ()` in the same pass: window functions run before
    LIMIT, so the total covers every group, not just the top `limit` rows. The
    unknown-source row is excluded exactly as `drill_down` excludes it, which
    keeps the two breakdowns comparable — the point of the whole exercise.

    Returns `drill_down`'s shape, so the client renders both through one path:
      {"column": str, "dim_table": str,
       "values": [{"source_id": str, "name": str, "count": int}]}
    `count` is round(share · μ) and is therefore an estimate; it is the caller's
    job to present it as one.
    """
    if not hours or mu <= 0:
        return []
    schema = source.schema_name
    pc = source.partition_column

    flt_parts, flt_params = _gap_where(
        source, filter_tree=filter_tree, filter_groups=filter_groups,
        include_time_range=False)
    where_parts = [f"f.{pc} >= :_prof_from", f"f.{pc} < :_prof_to",
                   f"extract(hour from {_utc_ts(source, f'f.{pc}')})::int "
                   f"= ANY(:_hours)"] + flt_parts
    where_base = " AND ".join(where_parts)
    base_params = {
        "_prof_from": datetime.fromtimestamp(int(prof_from), tz=timezone.utc),
        "_prof_to": datetime.fromtimestamp(int(prof_to), tz=timezone.utc),
        "_hours": sorted({int(h) for h in hours}),
        **flt_params,
    }

    dims_by_fact = {d.fact_column: d for d in source.dimensions}
    results = []
    with engine.begin() as conn:
        for fc in sorted(source.fact_columns, key=lambda c: c.ordinal):
            if fc.role not in ("dimension", "auto-dimension"):
                continue
            dm = dims_by_fact.get(fc.name)
            if not dm or not dm.dim_table:
                continue
            params = dict(base_params)
            params["_limit"] = limit
            params["_unk"] = UNKNOWN_SOURCE_ID
            sql = text(
                f"SELECT d.source_id, d.name, count(*) AS cnt, "
                f"       sum(count(*)) OVER () AS total "
                f"FROM {schema}.fact f "
                f"JOIN {schema}.{dm.dim_table} d ON f.{fc.name} = d.index "
                f"WHERE {where_base} AND d.source_id != :_unk "
                f"GROUP BY d.source_id, d.name "
                f"ORDER BY cnt DESC, d.name NULLS LAST "
                f"LIMIT :_limit"
            )
            rows = conn.execute(sql, params).all()
            if not rows:
                continue
            total = float(rows[0][3] or 0)
            if total <= 0:
                continue
            # Values whose share rounds to zero are dropped rather than listed.
            # A count of 0 reads as a measurement ("none of these came through")
            # when it is really the opposite — a share too small to show at this
            # gap's size. They are also the bulk of the list on a short gap: a
            # 20-second gap over a 20-value dimension rounds most of it to zero.
            values = [
                {"source_id": r[0], "name": r[1] or r[0],
                 "count": int(round(int(r[2]) / total * mu))}
                for r in rows
            ]
            values = [v for v in values if v["count"] > 0]
            if not values:
                continue
            results.append({
                "column":    fc.name,
                "dim_table": dm.dim_table,
                "values":    values,
            })
    return results


def list_transactions(
    source: Source,
    engine,
    group_by: str,
    group_by_val: str,
    t_from: datetime,
    t_to: datetime,
    filter_groups: Optional[list[list[dict]]] = None,
    limit: int = 1000,
    filter_tree: Optional[list[dict]] = None,
    group_by_indices: Optional[dict] = None,
    pk_format: Optional[str] = None,
) -> tuple[list[str], list[str]]:
    """Return up to `limit` PK values for the given bar's exact time slot.

    group_by_indices: when group_by is a grouped dimension, maps group names
    to sorted lists of dim indices. Passed through to _build_bar_where.
    pk_format: optional Python format string applied to each PK for display.
        {0} receives the raw PK value. Returns (raw_ids, display_ids).
        When pk_format is None, both lists are identical.
    """
    pk = source.pk_column()
    if not pk:
        return [], []
    where, params = _build_bar_where(
        source, group_by, group_by_val, t_from, t_to, filter_groups,
        filter_tree=filter_tree, group_by_indices=group_by_indices,
    )
    params["_limit"] = limit
    sql = text(
        f"SELECT f.{pk} FROM {source.schema_name}.fact f "
        f"WHERE {' AND '.join(where)} "
        f"ORDER BY f.{pk} LIMIT :_limit"
    )
    with engine.begin() as conn:
        rows = conn.execute(sql, params).all()
    raw_ids = [str(r[0]) for r in rows]

    return raw_ids, format_pks(raw_ids, pk_format)
