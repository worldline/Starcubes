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

import re
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text

from .schema import UNKNOWN_SOURCE_ID
from .source import Source
from .utils import format_pks

MEASURE_FUNCS = {"sum", "avg", "min", "max"}


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


def _filter_sql(
    schema: str,
    fact_col: str,
    op: str,
    vals: list,
    param: str,
    dims_by_fact: dict,
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

    flt_dim = dims_by_fact.get(fact_col)

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
            return f"f.{fact_col} {in_op} ({inner})", params
        else:
            return (f"NOT ({or_expr})" if negate else f"({or_expr})"), params
    elif flt_dim and flt_dim.dim_table:
        inner = (f"SELECT index FROM {schema}.{flt_dim.dim_table} "
                 f" WHERE source_id = ANY(:{param})")
        params[param] = [str(v) for v in vals]
        if op == "not_in":
            return f"f.{fact_col} NOT IN ({inner})", params
        else:
            return f"f.{fact_col} IN ({inner})", params
    else:
        params[param] = [int(v) for v in vals]
        if op == "not_in":
            return f"f.{fact_col} != ALL(:{param})", params
        else:
            return f"f.{fact_col} = ANY(:{param})", params


def _build_where_nodes(
    schema: str,
    siblings: list[dict],
    dims_by_fact: dict,
    cond_idx: list,
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
            f"flt{cond_idx[0]}", dims_by_fact,
        )
        cond_idx[0] += 1
        params.update(row_params)

        if sibling["children"]:
            child_parts, child_params = _build_where_nodes(
                schema, sibling["children"], dims_by_fact, cond_idx,
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
            schema, filter_tree, dims_by_fact, [0]
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
                    f"flt{cond_idx}", dims_by_fact,
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
    percentiles: Optional[list[int]] = None,
    measure_col: Optional[str] = None,
    series_label: str = "source_id",
    group_by_groups: Optional[dict[int, tuple[str, str]]] = None,
) -> tuple[str, dict, dict[int, str], dict[str, str]]:
    """Build the same SQL string `timeseries_multibar` / `timeseries_quantiles`
    execute, without running it.

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
    if graph_style == "quantile-bands":
        if not measure_col:
            raise ValueError("quantile-bands needs measure_col")
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
    elif graph_style != "quantile-bands" and group_by_groups and group_dim and group_dim.dim_table:
        # multibar / multiline / multipoint — Python-side merge path.
        group_by_grouped = True
        for idx, (gname, gcolor) in group_by_groups.items():
            index_to_group[idx] = gname
            group_colors[gname] = gcolor
        series_expr = f"f.{group_by}::int"
    elif graph_style == "quantile-bands" and group_by_groups and group_dim and group_dim.dim_table:
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
    epoch_to   = int(t_to.timestamp())
    interval_start = (epoch_from // interval) * interval

    where_parts, flt_params = _build_where(
        schema, pc, t_from, t_to, filter_groups, dims_by_fact,
        filter_tree=filter_tree)
    params: dict = {
        "interval":       interval,
        "interval_start": interval_start,
        **flt_params,
    }

    if graph_style == "quantile-bands":
        pct = sorted({int(p) for p in (percentiles or [])})
        if not pct or any(not (0 < p < 100) for p in pct):
            raise ValueError(
                f"percentiles must be integers in (0, 100), got {percentiles!r}")
        fractions = ",".join(f"{p / 100.0:g}" for p in pct)
        sql = (
            f"SELECT "
            f"  floor((extract(epoch from f.{pc}) - :interval_start) "
            f"        / :interval)::bigint AS slot, "
            f"  {series_expr} AS series, "
            f"  percentile_cont(ARRAY[{fractions}]) "
            f"    WITHIN GROUP (ORDER BY f.{measure_col}) AS qs "
            f"FROM {schema}.fact f "
            f"{joins}"
            f"WHERE {' AND '.join(where_parts)} "
            f"GROUP BY 1, 2 "
            f"ORDER BY 1, 2")
        return sql, params, index_to_group, group_colors

    if measure is None:
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
        f"FROM {schema}.fact f "
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
    percentiles: Optional[list[int]] = None,
    measure_col: Optional[str] = None,
    series_label: str = "source_id",
    group_by_groups: Optional[dict[int, tuple[str, str]]] = None,
) -> tuple[str, dict, dict[int, str], dict[str, str]]:
    """Build the per-series aggregate SQL that `updateLegend()` displays.

    Returns ``(sql, params, index_to_group, group_colors)`` mirroring
    `build_chart_sql`. The result is one row per series — `count(*)`,
    `sum(col)`, `avg(col)` etc. summed or averaged over the entire half-open
    time window `[t_from, t_to)`. For quantile-bands it is `percentile_cont`
    over each series' full distribution (semantically the same median /
    p25–p75 the legend shows after averaging per-slot percentiles).

    `group_by_groups` and the `:grouped` dispatch select the same rows as
    `build_chart_sql`, but the series column carries the label the legend
    shows rather than the key the plot path transports: the dimension's
    `name` (falling back to `source_id` where it is empty, as the legend
    does) and, for `:grouped`, the group name via a SQL-side CASE.
    `series_label='index'` still opts out and yields the warehouse key.
    No slot projection is emitted, so the `interval` parameter is
    intentionally absent.
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
    if graph_style == "quantile-bands":
        if not measure_col:
            raise ValueError("quantile-bands needs measure_col")
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

    where_parts, flt_params = _build_where(
        schema, pc, t_from, t_to, filter_groups, dims_by_fact,
        filter_tree=filter_tree)
    params: dict = {**flt_params}

    if graph_style == "quantile-bands":
        pct = sorted({int(p) for p in (percentiles or [])})
        if not pct or any(not (0 < p < 100) for p in pct):
            raise ValueError(
                f"percentiles must be integers in (0, 100), got {percentiles!r}")
        fractions = ",".join(f"{p / 100.0:g}" for p in pct)
        # Single percentile_cont over each series' full distribution —
        # semantically the per-series median / p25 / p75 the legend shows
        # after averaging per-slot percentiles.
        sql = (
            f"SELECT "
            f"  {series_expr} AS series, "
            f"  percentile_cont(ARRAY[{fractions}]) "
            f"    WITHIN GROUP (ORDER BY f.{measure_col}) AS qs "
            f"FROM {schema}.fact f "
            f"{joins}"
            f"WHERE {' AND '.join(where_parts)} "
            f"GROUP BY 1 "
            f"ORDER BY 1")
        return sql, params, index_to_group, group_colors

    if measure is None:
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
        f"FROM {schema}.fact f "
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
    percentiles: Optional[list[int]] = None,
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
        percentiles=percentiles,
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
    percentiles: Optional[list[int]] = None,
    measure_col: Optional[str] = None,
    series_label: str = "source_id",
    group_by_groups: Optional[dict[int, tuple[str, str]]] = None,
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
        percentiles=percentiles,
        measure_col=measure_col,
        series_label=series_label,
        group_by_groups=group_by_groups,
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

    # Validate filter tree up front (the helper raises the same ValueErrors,
    # but checking here keeps the original error messages and ordering).
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

    dims_by_fact = {d.fact_column: d for d in source.dimensions}

    def _check_filter_col(col: str) -> None:
        if col in fact_cols:
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

    def _validate_tree(nodes: list[dict]) -> None:
        for node in nodes:
            row = node["row"]
            _check_filter_col(row["col"])
            if row["op"] not in _VALID_OPS:
                raise ValueError(
                    f"filter op for {row['col']!r} must be one of "
                    f"{_VALID_OPS}, got {row['op']!r}")
            _validate_tree(node["children"])

    if filter_tree is not None:
        _validate_tree(filter_tree)
    else:
        for group in filter_groups or []:
            for cond in group:
                _check_filter_col(cond["col"])
                if cond["op"] not in _VALID_OPS:
                    raise ValueError(
                        f"filter op for {cond['col']!r} must be one of "
                        f"{_VALID_OPS}, got {cond['op']!r}")

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
    )
    # Read the slot base back out of the bind params rather than recomputing
    # it — that keeps the plot's `interval_start` identical by construction to
    # the `:interval_start` the SQL numbered its slots against.
    interval_start = int(params["interval_start"])
    epoch_to = int(t_to.timestamp())
    group_by_grouped = bool(group_by_groups) and bool(index_to_group)

    with source.dest_engine().connect() as conn:
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
    # value extent; un-stacked plots (multiline, multipoint) plot each series
    # independently, so the extent is the individual series values. The chart
    # recomputes the visible y-range per type too, but a correct min/max keeps
    # the off-screen fallback right.
    if graph_style in ("multiline", "multipoint"):
        vals = [v for slot in data.values() for v in slot.values()]
        v_min = min(vals) if vals else 0
        v_max = max(vals) if vals else 0
    else:
        v_min = min(slot_totals.values()) if slot_totals else 0
        v_max = max(slot_totals.values()) if slot_totals else 0

    plot: dict = {
        "type":           graph_style,
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


def timeseries_quantiles(
    source: Source,
    *,
    measure_col: str,
    percentiles: list[int],
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
) -> dict:
    """Build a 'quantile-bands' plot dict for `timeseries.js`.

    Like `timeseries_multibar`, but instead of one aggregate per slot/series it
    computes a percentile ladder of `measure_col` per slot/series via Postgres
    `percentile_cont`. The plot's `data[slot][series]` is a list of floats
    aligned to the (sorted, ascending) `percentiles` ladder; the renderer draws
    one line per percentile and shades the bands between them.

    Series come from `group_by` exactly as in multibar: empty → a single
    "(all)" series; a plain dimension → one series per dim value; a grouped
    dimension (`group_by_groups` supplied) → one series per named group, with
    "(other)" catching unnamed indices. Grouped percentiles are computed
    SQL-side (a CASE maps the dim index to its group name before aggregation) —
    percentiles cannot be merged in Python the way counts are.

    Args mirror `timeseries_multibar`; `measure_col` is the numeric fact column
    the percentiles are taken over and `percentiles` is the integer ladder.
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

    pct = sorted({int(p) for p in percentiles})
    if not pct or any(not (0 < p < 100) for p in pct):
        raise ValueError(
            f"percentiles must be integers in (0, 100), got {percentiles!r}")

    sql, params, _index_to_group, group_colors = build_chart_sql(
        source,
        graph_style="quantile-bands",
        group_by=group_by,
        measure=None,
        t_from=t_from, t_to=t_to, interval=interval,
        filter_tree=filter_tree,
        filter_groups=filter_groups,
        percentiles=pct,
        measure_col=measure_col,
        series_label=series_label,
        group_by_groups=group_by_groups,
    )
    interval_start = int(params["interval_start"])
    epoch_to = int(t_to.timestamp())

    with source.dest_engine().connect() as conn:
        rows = conn.execute(text(sql), params).all()

    def _safe_key(s: str) -> str:
        return f"_{s}" if s.isdigit() else s

    data: dict[int, dict[str, list[float]]] = {}
    v_min = None
    v_max = None
    for slot, series, qs in rows:
        slot = int(slot)
        vals = [float(x) for x in qs]
        data.setdefault(slot, {})[_safe_key(str(series))] = vals
        lo, hi = vals[0], vals[-1]
        v_min = lo if v_min is None else min(v_min, lo)
        v_max = hi if v_max is None else max(v_max, hi)

    plot: dict = {
        "type":           "quantile-bands",
        "interval_start": interval_start,
        "interval_end":   epoch_to,
        "interval":       interval,
        "count":          len(data),
        "min":            v_min if v_min is not None else 0,
        "max":            v_max if v_max is not None else 0,
        "percentiles":    pct,
        "data":           data,
    }
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

    flt_parts, flt_params = _build_where(
        schema, pc, t_from, t_to, filter_groups, dims_by_fact,
        filter_tree=filter_tree, include_time_range=False)

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
