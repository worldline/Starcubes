"""Chart page + JSON endpoint for `timeseries.js` multibar plots.

Mirrors what `scripts/preview_server.py` does, routed through Django so we
get URL reversing, templates, and a place to add auth/forms later. The
JSON shape and filter syntax are identical — same `core.query.timeseries_multibar`
behind both endpoints.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from functools import wraps

from django.conf import settings
from django.http import FileResponse, Http404, HttpResponseBadRequest, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST
from sqlalchemy import text

from core import adhoc
from core.cache import (get_cached_plot, is_cacheable, make_cache_key,
                        put_cached_plot)
from core.query import (build_chart_sql, build_chart_sql_rendered,
                           build_chart_legend_sql, build_chart_legend_sql_rendered,
                        build_group_by_indices, drill_down, grouped_label,
                        grouped_token, LADDER_STYLES,
                        list_transactions, resolve_group_set,
                        split_grouped_col, timeseries_multibar,
                        timeseries_quantiles)
from core.schema import UNKNOWN_SOURCE_ID
from core.source import Source, _get_cached_engine
from core.utils import _parse_dt


def _meta_engine():
    """SQLAlchemy engine for starcubes_meta lookups, shared across requests."""
    return _get_cached_engine(settings.STARCUBES_POSTGRES_DSN)


def _read_hwm_and_epoch(meta, datasource_id: int) -> tuple[int | None, int]:
    """Return (last-ok HWM as unix seconds, cache_epoch) in one round trip.

    HWM is None when the datasource has no successful ETL runs.
    """
    with meta.begin() as conn:
        row = conn.execute(text(
            "SELECT ds.cache_epoch, er.last_end_time "
            "  FROM starcubes_meta.datasource ds "
            "  LEFT JOIN LATERAL ( "
            "    SELECT last_end_time FROM starcubes_meta.etl_run "
            "     WHERE datasource_id = ds.id AND status = 'ok' "
            "     ORDER BY finished_at DESC LIMIT 1 "
            "  ) er ON true "
            " WHERE ds.id = :id"
        ), {"id": datasource_id}).first()
    if row is None:
        return None, 0
    cache_epoch = int(row[0] or 0)
    hwm = row[1]
    if hwm is None:
        return None, cache_epoch
    if hwm.tzinfo is None:
        hwm = hwm.replace(tzinfo=timezone.utc)
    else:
        hwm = hwm.astimezone(timezone.utc)
    return int(hwm.timestamp()), cache_epoch


def _resolve_group_by(source, group_by_raw: str):
    """Resolve a group_by token into (fact_column, dimension, group_set).

    `group_set` is None for a plain column — and also for a grouped token
    whose slug names no set, which is what a bookmark saved before a rename
    looks like. Callers then fall back to the un-grouped breakdown on the
    same column instead of erroring out.
    """
    col, slug, is_grouped = split_grouped_col(group_by_raw)
    dim = next((d for d in source.dimensions if d.fact_column == col), None)
    gset = resolve_group_set(dim, slug) if is_grouped else None
    return col, dim, gset


def _group_index_map(gset) -> dict[int, tuple[str, str]]:
    """Flatten one group set into {dim_index: (group_name, hex_color)}."""
    return {m.dim_index: (g.group_name, g.color)
            for g in gset.groups for m in g.members}


def _parse_filter_spec(spec: str) -> tuple[str, str, list[str]] | None:
    """Parse one filter= query param into (col, op, vals) or None."""
    if "!~=" in spec:
        col, vals = spec.split("!~=", 1); op = "starts_not_with"
    elif "!*=" in spec:
        col, vals = spec.split("!*=", 1); op = "not_contains"
    elif "!=" in spec:
        col, vals = spec.split("!=", 1);  op = "not_in"
    elif "~=" in spec:
        col, vals = spec.split("~=", 1);  op = "starts_with"
    elif "*=" in spec:
        col, vals = spec.split("*=", 1);  op = "contains"
    elif "=" in spec:
        col, vals = spec.split("=", 1);   op = "in"
    else:
        return None
    vs = [v for v in vals.split(",") if v]
    if not vs:
        return None
    return col, op, vs


def _parse_filter_tree(q) -> list[dict]:
    """Parse filter=, andor=, indent= query params into a filter tree.

    Each node: {'andOr': str, 'row': {col, op, vals}, 'children': list}
    Indent levels determine parent-child relationships via a stack parser.
    URLs without indent= params default to all-zero (flat, same as old behaviour).
    Returns the root-level node list.
    """
    filter_specs = q.getlist("filter")
    andor_list   = q.getlist("andor")
    indent_list  = q.getlist("indent")
    if not filter_specs:
        return []

    flat: list[dict] = []
    for i, spec in enumerate(filter_specs):
        parsed = _parse_filter_spec(spec)
        if not parsed:
            continue
        col, op, vs = parsed
        andOr  = andor_list[i - 1] if i > 0 and i - 1 < len(andor_list) else "and"
        indent = int(indent_list[i]) if i < len(indent_list) else 0
        flat.append({"row": {"col": col, "op": op, "vals": vs}, "andOr": andOr, "indent": indent})

    if not flat:
        return []

    root: list[dict] = []
    stack: list[list[dict]] = [root]
    indent_stack: list[int] = [-1]
    for item in flat:
        node: dict = {"andOr": item["andOr"], "row": item["row"], "children": []}
        while item["indent"] <= indent_stack[-1]:
            stack.pop()
            indent_stack.pop()
        stack[-1].append(node)
        stack.append(node["children"])
        indent_stack.append(item["indent"])
    return root


def chart(request):
    """Render the chart shell. The page reads its own URL params and
    fetches `plot.json` — same pattern as the static preview.html."""
    return render(request, "viz/chart.html")


def slides(request):
    """Serve the self-contained reveal.js slide deck (repo-root slides.html).

    The deck loads all its assets from a CDN, so we just stream the file."""
    path = settings.BASE_DIR / "slides.html"
    if not path.exists():
        raise Http404("slides.html not found")
    return FileResponse(open(path, "rb"), content_type="text/html")


def plot_json(request):
    """Build a multibar plot dict from the request query string.

    Required:    ?name=<datasource>
    Defaults:    group_by=response, interval=60, last 24h, count(*).
    Filters:     ?filter=<col>=<v1>,<v2>...  (is in)
                 ?filter=<col>!=<v1>,<v2>...  (is not in)
                 ?filter=<col>~=<pat1>,<pat2>  (starts with)
                 ?filter=<col>*=<pat1>,<pat2>  (contains)
    Measure:     ?measure=<col>              (defaults to sum)
                 ?measure=avg(<col>)         (explicit aggregation)
    """
    try:
        q = request.GET
        name = q.get("name")
        if not name:
            return HttpResponseBadRequest("missing ?name=")
        group_by_raw = q.get("group_by", "response")
        interval     = int(q.get("interval", "60"))
        measure      = q.get("measure") or None
        series_label = q.get("series_label", "source_id")
        now          = datetime.now(timezone.utc)
        t_from = _parse_dt(q.get("from",
                                 (now - timedelta(hours=24)).isoformat()))
        t_to   = _parse_dt(q.get("to", now.isoformat()))

        filter_tree = _parse_filter_tree(q)

        meta = _meta_engine()
        source = Source.load(meta,
                             dest_dsn=settings.STARCUBES_POSTGRES_DSN,
                             name_or_id=name)

        # Server-side cache: only count(*) requests with `to` at a UTC
        # interval boundary, strictly in the past, and behind the last
        # successful HWM are eligible. Misses fall through to the normal
        # path; hits return the stored payload directly.
        t_to_epoch = int(t_to.timestamp())
        hwm_epoch, cache_epoch = _read_hwm_and_epoch(meta, source.id)
        cache_key = None
        if is_cacheable(measure=measure, interval=interval,
                        t_to_epoch=t_to_epoch, hwm_epoch=hwm_epoch,
                        late_arrival_overlap_s=source.late_arrival_overlap_s):
            cache_key = make_cache_key(
                datasource_id=source.id,
                group_by=group_by_raw,
                measure=measure,
                t_from_epoch=int(t_from.timestamp()),
                t_to_epoch=t_to_epoch,
                interval=interval,
                filter_tree=filter_tree,
                series_label=series_label,
                cache_epoch=cache_epoch,
            )
            cached = get_cached_plot(meta, cache_key)
            if cached is not None:
                return JsonResponse(cached, json_dumps_params={"default": str})

        group_by_groups = None
        group_counts = None
        group_by, group_dim, group_set = _resolve_group_by(source, group_by_raw)
        if group_set and group_set.groups:
            group_by_groups = _group_index_map(group_set)
            group_counts = {}
            # Sum dim counts per group for ascending sort order
            if group_dim.dim_table:
                all_indices = set(group_by_groups)
                if all_indices:
                    idx_list = ",".join(str(i) for i in sorted(all_indices))
                    with source.dest_engine().begin() as dconn:
                        rows = dconn.execute(text(
                            f"SELECT index, count "
                            f"  FROM {source.schema_name}.{group_dim.dim_table} "
                            f" WHERE index IN ({idx_list})"
                        )).mappings().all()
                        idx_counts = {r["index"]: r["count"] for r in rows}
                        total_count = dconn.execute(text(
                            f"SELECT COALESCE(SUM(count), 0) "
                            f"  FROM {source.schema_name}.{group_dim.dim_table}"
                        )).scalar()
                    for g in group_set.groups:
                        group_counts[g.group_name] = sum(
                            idx_counts.get(m.dim_index, 0) for m in g.members)
                    group_counts["(other)"] = max(
                        0, int(total_count or 0) - sum(group_counts.values()))

        # Graph style comes from the measure catalog (keyed by the agg form;
        # count(*) when no measure is given). Falls back to multibar.
        agg_form = measure if measure else "count(*)"
        graph_style = source.graph_style_for(agg_form)

        if graph_style in LADDER_STYLES:
            # A ladder-style measure's agg is quantile(<col>); the percentile
            # ladder comes from the measure catalog. percentile_cont isn't
            # cacheable (is_cacheable already rejects non-count measures).
            mq = re.match(r"quantile\((\w+)\)$", agg_form or "")
            if not mq:
                return HttpResponseBadRequest(
                    f"{graph_style} measure needs a quantile(col) agg")
            plot = timeseries_quantiles(
                source,
                measure_col=mq.group(1),
                percentiles=source.quantiles_for(agg_form),
                group_by=group_by,
                t_from=t_from,
                t_to=t_to,
                interval=interval,
                filter_tree=filter_tree or None,
                series_label=series_label,
                name=f"{name}: {group_by or '(all)'}",
                meta_engine=meta,
                group_by_groups=group_by_groups,
                group_counts=group_counts,
                graph_style=graph_style,
            )
        else:
            plot = timeseries_multibar(
                source,
                group_by=group_by,
                t_from=t_from,
                t_to=t_to,
                interval=interval,
                filter_tree=filter_tree or None,
                measure=measure,
                series_label=series_label,
                name=f"{name}: {group_by or '(all)'}",
                meta_engine=meta,
                group_by_groups=group_by_groups,
                group_counts=group_counts,
                graph_style=graph_style,
            )
        if cache_key is not None:
            put_cached_plot(meta, cache_key=cache_key,
                            datasource_id=source.id,
                            cache_epoch=cache_epoch,
                            group_by=group_by_raw,
                            measure=measure,
                            payload=plot)
        return JsonResponse(plot, json_dumps_params={"default": str})
    except Exception as exc:
        return JsonResponse({"error": repr(exc)}, status=500)


def drill_json(request):
    """Drill-down breakdown for a clicked bar.

    Returns top-N values for each remaining dimension column within the bar's
    exact time slot, filtered to the clicked series and any active UI filters.

    Required: ?name=  &group_by=  &group_by_val=  &from=  &to=
    Optional: ?filter=  (same syntax as plot_json)
    """
    try:
        q             = request.GET
        name          = q.get("name")
        group_by_raw = q.get("group_by", "")
        gb_val        = q.get("group_by_val", "")
        t_from_s      = q.get("from")
        t_to_s        = q.get("to")
        # group_by may be empty: a "(none)" bar covers every row in the slot.
        if not all([name, t_from_s, t_to_s]):
            return HttpResponseBadRequest("missing required params")
        t_from = _parse_dt(t_from_s)
        t_to   = _parse_dt(t_to_s)

        filter_tree = _parse_filter_tree(q)

        source = Source.load(_meta_engine(),
                             dest_dsn=settings.STARCUBES_POSTGRES_DSN,
                             name_or_id=name)

        # When group_by names a group dimension, resolve the group name to
        # the dim indices of that set.
        group_by, gb_dim, gb_set = _resolve_group_by(source, group_by_raw)
        group_by_indices = None
        if gb_set and gb_dim.dim_table and gb_set.groups:
            group_by_indices = build_group_by_indices(gb_set)

        dims = drill_down(
            source, source.dest_engine(),
            group_by, gb_val,
            t_from, t_to,
            filter_tree=filter_tree or None,
            group_by_indices=group_by_indices,
        )
        return JsonResponse({
            "group_by":     group_by,
            "group_by_val": gb_val,
            "from":         t_from.isoformat(),
            "to":           t_to.isoformat(),
            "dimensions":   dims,
        })
    except Exception as exc:
        return JsonResponse({"error": repr(exc)}, status=500)


def txn_json(request):
    """Return up to 1000 PK values for a clicked bar.

    Required: ?name=  &group_by=  &group_by_val=  &from=  &to=
    Optional: ?filter=  (same syntax as plot_json)
    """
    try:
        q             = request.GET
        name          = q.get("name")
        group_by_raw = q.get("group_by", "")
        gb_val        = q.get("group_by_val", "")
        t_from_s      = q.get("from")
        t_to_s        = q.get("to")
        # group_by may be empty: a "(none)" bar covers every row in the slot.
        if not all([name, t_from_s, t_to_s]):
            return HttpResponseBadRequest("missing required params")
        t_from = _parse_dt(t_from_s)
        t_to   = _parse_dt(t_to_s)

        filter_tree = _parse_filter_tree(q)

        source = Source.load(_meta_engine(),
                             dest_dsn=settings.STARCUBES_POSTGRES_DSN,
                             name_or_id=name)

        group_by, gb_dim, gb_set = _resolve_group_by(source, group_by_raw)
        group_by_indices = None
        if gb_set and gb_dim.dim_table and gb_set.groups:
            group_by_indices = build_group_by_indices(gb_set)

        raw_ids, display_ids = list_transactions(
            source, source.dest_engine(),
            group_by, gb_val,
            t_from, t_to,
            filter_tree=filter_tree or None,
            group_by_indices=group_by_indices,
            pk_format=source.pk_format,
        )
        return JsonResponse({
            "group_by":            group_by,
            "group_by_val":        gb_val,
            "from":                t_from.isoformat(),
            "to":                  t_to.isoformat(),
            "transaction_ids":     raw_ids,
            "transaction_display": display_ids,
            "limit":               1000,
        })
    except Exception as exc:
        return JsonResponse({"error": repr(exc)}, status=500)


def api_sources(request):
    """List datasources known to starcubes_meta. Drives the chart's source picker."""
    with _meta_engine().begin() as conn:
        rows = conn.execute(text(
            "SELECT id, name, schema_name, is_default "
            "  FROM starcubes_meta.datasource ORDER BY name"
        )).mappings().all()
    return JsonResponse({"sources": [dict(r) for r in rows]})


def _existing_relations(conn, qualified_names):
    """Subset of ``qualified_names`` that actually exists in the warehouse.

    A datasource is fully configurable before any of its DDL exists: the GUI's
    ``source_new`` only INSERTs the metadata row, and ``create_fact`` /
    ``create_dim_tables`` run later from ``/sources/<id>/`` — or never, for a
    half-configured source. So a ``dimension`` row routinely names a table, and
    a ``datasource`` row a schema, that is not there yet. Callers use this to
    serve such a dimension as empty instead of dying on it.

    The names are passed to ``to_regclass`` **unquoted**, exactly as the
    queries this guards interpolate them (``f"{schema}.{table}"``): the
    existence test has to fold identifiers the same way the access it protects
    does, or it would answer a different question than the one being asked.
    (In ``migrations/starcubes_meta.sql`` the mirror-image is right — the ALTER
    there quotes with ``%I``, so its test quotes too.) ``to_regclass`` returns
    NULL for a missing table and a missing schema alike, without raising.
    """
    names = list(qualified_names)
    if not names:
        return set()
    return {r[0] for r in conn.execute(text(
        "SELECT q FROM unnest(CAST(:names AS text[])) AS t(q) "
        " WHERE to_regclass(q) IS NOT NULL"
    ), {"names": names}).all()}


def api_source_meta(request):
    """Per-source metadata for the chart form: fact columns (role-tagged so
    the UI can split into time / pk / measure / dimension), the dim values
    available for each primary dim filter, and a default time range derived
    from the etl_run history.

    The dim payload omits the '__unknown__' sentinel rows — they'd never be a
    useful filter choice, only a destination for unmapped values.
    """
    name = request.GET.get("name")
    if not name:
        return HttpResponseBadRequest("missing ?name=")

    # Same shape as plot_json / drill_json / txn_json: a failure here has to
    # reach the chart as JSON. Without it Django answers with its HTML 500
    # page, and the status line prints "<!doctype html> ..." instead of the
    # error -- which is how a missing dim table read as an unrelated problem.
    try:
        source = Source.load(_meta_engine(),
                             dest_dsn=settings.STARCUBES_POSTGRES_DSN,
                             name_or_id=name)

        dim_for_fc = {d.fact_column: d.dim_table
                      for d in source.dimensions if d.dim_table}
        fact_cols = [{
            "name":      c.name,
            "role":      c.role,
            "pg_type":   c.pg_type,
            "ordinal":   c.ordinal,
            "dim_table": dim_for_fc.get(c.name),
        } for c in sorted(source.fact_columns, key=lambda c: c.ordinal)]

        dimensions: dict[str, list] = {}
        with source.dest_engine().begin() as conn:
            present = _existing_relations(
                conn, [f"{source.schema_name}.{d.dim_table}"
                       for d in source.primary_dimensions()])
            for d in source.primary_dimensions():
                if f"{source.schema_name}.{d.dim_table}" not in present:
                    dimensions[d.dim_table] = []
                    continue
                rows = conn.execute(text(
                    f"SELECT source_id, name, description "
                    f"  FROM {source.schema_name}.{d.dim_table} "
                    f" WHERE source_id != :unk "
                    f" ORDER BY name NULLS LAST, source_id"
                ), {"unk": UNKNOWN_SOURCE_ID}).all()
                dimensions[d.dim_table] = [[r[0], r[1], r[2]] for r in rows]

        # One entry per group dimension (dim_group_set), in the order the
        # dimension carries them. `token` is what the chart puts in group_by and
        # in filter columns; `label` is how it prints in both dropdowns. Counts
        # are fetched once per dimension, not once per set, since several sets on
        # one column all read the same dim table.
        dim_group_sets: list[dict] = []
        for d in source.dimensions:
            if not d.group_sets:
                continue
            idx_counts: dict[int, int] = {}
            if d.dim_table:
                all_indices = {m.dim_index for gs in d.group_sets
                               for g in gs.groups for m in g.members}
                if all_indices:
                    idx_list = ",".join(str(i) for i in sorted(all_indices))
                    with source.dest_engine().begin() as dconn:
                        # Same guard as the dim-value loop above: a group dimension
                        # can be configured before the dim table it buckets exists.
                        if _existing_relations(
                                dconn, [f"{source.schema_name}.{d.dim_table}"]):
                            rows = dconn.execute(text(
                                f"SELECT index, count "
                                f"  FROM {source.schema_name}.{d.dim_table} "
                                f" WHERE index IN ({idx_list})"
                            )).mappings().all()
                            idx_counts = {r["index"]: r["count"] for r in rows}
            for gs in d.group_sets:
                group_list = []
                for g in gs.groups:
                    members = [m.dim_index for m in g.members]
                    group_list.append({
                        "group_name": g.group_name,
                        "color":      g.color,
                        "members":    members,
                        "count":      sum(idx_counts.get(i, 0) for i in members),
                    })
                dim_group_sets.append({
                    "col":    d.fact_column,
                    "slug":   gs.slug,
                    "name":   gs.name,
                    "token":  grouped_token(d.fact_column, gs.slug),
                    "label":  grouped_label(d.fact_column, gs.name),
                    "groups": group_list,
                })

        # Fan-out filter groups (dimension.filter_group, e.g. so_peer over
        # so_peer_1/2/3): one merged chart-filter row per group, ORing over
        # every member fact column. fact_columns stays untouched, so
        # non-grouped dimensions keep their per-column filter rows.
        dim_by_fc = {d.fact_column: d for d in source.dimensions}
        dim_filters = []
        for token, fact_cols in source.filter_fanout().items():
            members = [dim_by_fc[c] for c in fact_cols]
            dim_table = next(
                (m.dim_table for m in members
                 if m.is_primary and m.dim_table), None) or members[0].dim_table
            if not dim_table:
                continue
            dim_filters.append({
                "token":     token,
                "label":     token,
                "dim_table": dim_table,
                "fact_cols": fact_cols,
            })
        dim_filters.sort(key=lambda f: f["token"])

        with _meta_engine().begin() as conn:
            wm = conn.execute(text(
                "SELECT min(first_end_time) AS lo, "
                "       max(last_end_time)  AS hi "
                "  FROM starcubes_meta.etl_run "
                " WHERE datasource_id = :ds AND status = 'ok'"
            ), {"ds": source.id}).mappings().one()

        return JsonResponse({
            "name":                     source.name,
            "schema_name":              source.schema_name,
            "partition_column":         source.partition_column,
            "fact_columns":             fact_cols,
            "dimensions":               dimensions,
            "dim_group_sets":           dim_group_sets,
            "dim_filters":              dim_filters,
            "measures":                 [{
                "name":        m.name,
                "agg":         m.agg,
                "graph_style": m.graph_style,
                "is_default":  m.is_default,
            } for m in source.measures],
            "interval_ladder":          source.interval_ladder,
            "bar_pixel_target":          source.bar_pixel_target,
            "pk_format":                source.pk_format,
            "time_range": {
                "min": wm["lo"].isoformat() if wm["lo"] else None,
                "max": wm["hi"].isoformat() if wm["hi"] else None,
            },
        }, json_dumps_params={"default": str})
    except Exception as exc:
        return JsonResponse({"error": repr(exc)}, status=500)


# ── Query console ───────────────────────────────────────────────────────────
#
# Ad-hoc read-only SQL against the warehouse schema of the chart's current
# source. The heavy lifting (validation, read-only execution, cancellation,
# history) lives in `core/adhoc.py`; these views are the thin Django adapter.
#
# Two deliberate deviations from the rest of this module:
#
#   * Auth. Every other viz view is public — RequireLoginMiddleware only gates
#     /sources/ and /admin/ — so these four gate themselves. Not with
#     @login_required: that *redirects*, so fetch() would follow the 302 to
#     /login/, receive HTML, and r.json() would throw an opaque SyntaxError
#     instead of surfacing "your session expired". A 403 with a JSON body is
#     what the client can actually act on.
#
#   * Error status. The rest of viz answers failures with
#     {"error": repr(exc)} + HTTP 500. For a SQL console a user's syntax error
#     is the *normal* case, not a server fault, and a 500 pollutes logs and
#     monitoring. These endpoints follow the sources/ convention instead:
#     {"ok": False, "error": ...} with HTTP 200 for anything the user caused,
#     500 reserved for genuine surprises. Please do not "fix" this back.

_QUERY_TOKEN_RE = re.compile(r"^[0-9a-f]{8,32}$")

#: Hard ceiling on the configurable statement timeout, independent of
#: STARCUBES_QUERY_TIMEOUT_MS. uwsgi's harakiri is 120 s and kills the whole
#: worker process (4 threads), so no single query may be allowed near it.
_QUERY_TIMEOUT_CEILING_MS = 90_000


def _login_required_json(view):
    """Gate a JSON endpoint on an authenticated session, answering 403 not 302."""
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({"ok": False, "error": "login required"}, status=403)
        return view(request, *args, **kwargs)
    return wrapped


def _query_timeout_ms() -> int:
    return min(int(getattr(settings, "STARCUBES_QUERY_TIMEOUT_MS", 30_000)),
               _QUERY_TIMEOUT_CEILING_MS)


def _console_source(request):
    """Load the Source named by the request's `name` param."""
    name = request.POST.get("name") or request.GET.get("name")
    if not name:
        raise ValueError("no source given")
    return Source.load(_meta_engine(),
                       dest_dsn=settings.STARCUBES_POSTGRES_DSN,
                       name_or_id=name)


def _friendly_db_error(exc) -> tuple[str, bool]:
    """Turn a SQLAlchemy DBAPI error into (message, was_cancelled).

    Postgres' own message carries the error position and is far more useful to
    someone editing SQL than repr() of the SQLAlchemy wrapper, so it is passed
    through rather than summarised.
    """
    orig = getattr(exc, "orig", None)
    code = getattr(orig, "pgcode", None)
    if code == "57014":      # query_canceled — also what statement_timeout raises
        return "Query cancelled or timed out.", True
    if code == "25006":      # read_only_sql_transaction
        return "This console is read-only.", False
    return str(orig or exc).strip(), False


@require_POST
@_login_required_json
def query_run(request):
    """Run one read-only statement against the current source's warehouse schema."""
    try:
        token = (request.POST.get("token") or "").strip()
        if not _QUERY_TOKEN_RE.match(token):
            return JsonResponse({"ok": False, "error": "bad query token"})
        sql = adhoc.validate_sql(request.POST.get("sql", ""))
        source = _console_source(request)
    except (adhoc.SqlRejected, ValueError, LookupError) as exc:
        return JsonResponse({"ok": False, "error": str(exc)})

    # Recorded *before* execution on purpose: a query that times out is exactly
    # the one a user most wants to pull back out of the history and edit.
    try:
        adhoc.history_touch(_meta_engine(), user_id=request.user.id,
                            datasource_id=source.id, sql=sql)
    except Exception:
        pass  # history is a convenience; never fail the query over it

    timeout_ms = _query_timeout_ms()
    try:
        result = adhoc.run_query(
            source.console_engine(statement_timeout_ms=timeout_ms),
            sql,
            tag=adhoc.make_tag(request.user.id, token),
            row_limit=int(getattr(settings, "STARCUBES_QUERY_ROW_LIMIT", 1000)),
            timeout_ms=timeout_ms,
        )
    except Exception as exc:
        message, cancelled = _friendly_db_error(exc)
        return JsonResponse({"ok": False, "cancelled": cancelled, "error": message})

    return JsonResponse({"ok": True, **result}, json_dumps_params={"default": str})


@require_POST
@_login_required_json
def query_cancel(request):
    """Cancel this user's running console query, whichever worker started it.

    The tag is rebuilt here from `request.user.id` — never from the request
    body — so a guessed token cannot reach another user's backend.
    """
    try:
        token = (request.POST.get("token") or "").strip()
        if not _QUERY_TOKEN_RE.match(token):
            return JsonResponse({"ok": False, "error": "bad query token"})
        n = adhoc.cancel_query(_meta_engine(),
                               adhoc.make_tag(request.user.id, token))
        return JsonResponse({"ok": True, "cancelled": n})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


@_login_required_json
def query_history(request):
    """This user's saved statements for the named source, most recent first."""
    try:
        source = _console_source(request)
        entries = adhoc.history_list(_meta_engine(), user_id=request.user.id,
                                     datasource_id=source.id)
        return JsonResponse({"ok": True, "entries": entries},
                            json_dumps_params={"default": str})
    except (ValueError, LookupError) as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


@require_POST
@_login_required_json
def query_history_delete(request):
    """Drop one of this user's history entries."""
    try:
        entry_id = int(request.POST.get("id", ""))
    except (TypeError, ValueError):
        return JsonResponse({"ok": False, "error": "bad entry id"})
    try:
        n = adhoc.history_delete(_meta_engine(), user_id=request.user.id,
                                 entry_id=entry_id)
        return JsonResponse({"ok": True, "deleted": n})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


@require_GET
@_login_required_json
def chart_sql(request):
    """Return the SQL string the legend would display for the current
    source / group_by / measure / filter context.

    Powers the "Copy chart SQL" button next to Cancel in the SQL console:
    the button POSTs nothing (the chart already knows its own state) and
    fetches a per-series aggregate statement matching the legend —
    one row per series summed/averaged over the visible time window, not
    a slot-by-slot multibar/quantile dump. Built by
    `core.query.build_chart_legend_sql` with the same joins, series_expr,
    filter tree, and graph-style dispatch that `timeseries_multibar` /
    `timeseries_quantiles` use, minus the slot projection. Bind params
    are inlined as literals so `adhoc.run_query → exec_driver_sql(sql)`
    can execute the result without substitution. We never run anything
    here.
    """
    try:
        source = _console_source(request)
        q = request.GET
        if not q.get("from") or not q.get("to"):
            return HttpResponseBadRequest("missing ?from / ?to")

        group_by_raw = q.get("group_by", "")
        measure      = q.get("measure") or None
        interval     = int(q.get("interval", "60"))
        series_label = q.get("series_label", "source_id")
        t_from = _parse_dt(q["from"])
        t_to   = _parse_dt(q["to"])

        filter_tree = _parse_filter_tree(q)

        # Graph style / quantile routing — mirrors plot_json's dispatch
        # (viz/views.py around the agg_form + graph_style switch). The
        # helper expects `measure=None` and explicit `percentiles` /
        # `measure_col` for ladder-style branches (quantile-bands /
        # quantile-steps / quantile-steps-open / error-bars / candlestick).
        agg_form = measure if measure else "count(*)"
        graph_style = source.graph_style_for(agg_form)
        measure_col = percentiles = None
        if graph_style in LADDER_STYLES:
            mq = re.match(r"quantile\((\w+)\)$", agg_form)
            if not mq:
                return HttpResponseBadRequest(
                    f"{graph_style} needs quantile(col), got {agg_form!r}")
            measure_col = mq.group(1)
            percentiles = source.quantiles_for(agg_form)
            measure = None

        # Multibar / quantile-bands :grouped — map dim index → group name.
        # Same shape plot_json builds (viz/views.py). The helper uses this
        # for the per-index Python merge (multibar) or the SQL-side CASE
        # (quantile-bands).
        group_by, _gb_dim, gb_set = _resolve_group_by(source, group_by_raw)
        group_by_groups = None
        if gb_set and gb_set.groups:
            group_by_groups = _group_index_map(gb_set)

        sql, _itg, _gc = None, None, None  # noqa: F841 — kept for symmetry w/ helper
        # Inline every bind param as a literal so the statement is
        # self-contained — `exec_driver_sql(sql)` in the console path
        # does no parameter substitution. Use the legend-specific helper
        # (no slot projection) so the result has one row per series,
        # matching what the legend displays.
        sql = build_chart_legend_sql_rendered(
            source,
            graph_style=graph_style,
            group_by=group_by,
            measure=measure,
            t_from=t_from, t_to=t_to,
            filter_tree=filter_tree,
            percentiles=percentiles, measure_col=measure_col,
            series_label=series_label,
            group_by_groups=group_by_groups,
        )
        return JsonResponse({"ok": True, "sql": sql})
    except (ValueError, LookupError) as exc:
        # Same convention as query_run: user-side mistake → 200 + JSON.
        return JsonResponse({"ok": False, "error": str(exc)})
    except Exception as exc:
        return JsonResponse({"error": repr(exc)}, status=500)
