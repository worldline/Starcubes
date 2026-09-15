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
from sqlalchemy.exc import OperationalError, ProgrammingError

from core import adhoc
from core.cache import (get_cached_plot, is_cacheable, make_cache_key,
                        put_cached_plot)
from core.query import (build_chart_legend_sql_rendered,
                        build_filter_tree, build_group_by_indices,
                        detect_gaps, drill_down, gap_day_profile,
                        gap_neighbor_rate, gap_profile_bounds,
                        gap_reference_drill,
                        GAP_PROFILE_DAYS, GAP_PROFILE_TTL_S, grouped_label,
                        grouped_token, _profile_integral, LADDER_STYLES,
                        list_transactions, parse_ladder_agg,
                        resolve_group_set, split_grouped_col,
                        timeseries_ladder, timeseries_multibar,
                        validate_filter_tree)
from core.rollup import (measure_expr, pick_profile_rollup, pick_rollup,
                         read_rollup_states, rollup_extent)
from core.schema import UNKNOWN_SOURCE_ID
from core.source import (REQUEST_CONNECT_TIMEOUT_S, Source, _get_cached_engine)
from core.utils import _parse_dt


def _meta_engine():
    """SQLAlchemy engine for starcubes_meta lookups, shared across requests.

    The request path's `connect_timeout`: starcubes_meta sits on the same
    Postgres as the warehouse in the standard deployment, and a dead database
    must fail the request with an OperationalError the chart can translate —
    not hang it to the TCP default for harakiri to reap. A separate cached
    pool, because `_get_cached_engine` keys on `(dsn, connect_args)`; the ETL
    and console pools read starcubes_meta without the timeout.
    """
    return _get_cached_engine(settings.STARCUBES_POSTGRES_DSN,
                              connect_args={
                                  "connect_timeout": REQUEST_CONNECT_TIMEOUT_S
                              })


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
    Indent levels determine parent-child relationships via the stack parser
    in `core.query.build_filter_tree`. URLs without indent= params default
    to all-zero (flat, same as old behaviour).
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

    return build_filter_tree(flat)


def chart(request):
    """Render the chart shell. The page reads its own URL params and
    fetches `plot.json` — same pattern as the static preview.html."""
    return render(request, "viz/chart.html")


def slides(request):
    """Serve the blueprint slide deck (repo-root presentation.html).

    The deck references its assets with paths relative to the repo root, so
    at "/slides" the browser asks for "/presentation/…", "/docs/img/…" and
    "/viz/static/viz/…" — `deck_asset` streams those from disk."""
    path = settings.BASE_DIR / "presentation.html"
    if not path.exists():
        raise Http404("presentation.html not found")
    return FileResponse(open(path, "rb"), content_type="text/html")


# Repo-root directories the deck may pull assets from, keyed by the first
# URL segment the browser resolves them to. Everything else stays unrouted.
DECK_ROOTS = {
    "presentation": "presentation",
    "docs": "docs",
    "viz": "viz/static/viz",
}


def deck_asset(request, root, rest):
    """Stream one deck asset from its repo-root directory (`DECK_ROOTS`).

    Guarded: resolve under BASE_DIR, no `..` parts; known extensions get
    their content type, everything else octet-stream."""
    base = (settings.BASE_DIR / DECK_ROOTS[root]).resolve()
    path = (base / rest).resolve()
    if base != path and base not in path.parents:
        raise Http404("outside the deck roots")
    if not path.is_file():
        raise Http404("no such deck asset")
    content_type = {
        ".css": "text/css",
        ".js": "text/javascript",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".woff2": "font/woff2",
    }.get(path.suffix, "application/octet-stream")
    return FileResponse(open(path, "rb"), content_type=content_type)


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
        # The catalog's identity is (datasource, name), not the agg form: two
        # measures may share `quantile(stay)` and differ only in graph_style
        # or rung ladder. The chart sends the display label alongside so the
        # right row is resolved; absent (old bookmark, external caller) the
        # agg-only match stands. See `Source.measure_row`.
        measure_name = q.get("measure_name") or None
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
                # The epoch rides on every response, hit or miss — see the
                # note at the store below.
                cached["cache_epoch"] = cache_epoch
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
                    with source.request_engine().begin() as dconn:
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
        graph_style = source.graph_style_for(agg_form, measure_name)

        # A ladder-style measure's agg is quantile(<col>) or extent(<col>);
        # the rung ladder comes from the measure catalog either way. Neither
        # is cacheable (is_cacheable rejects every non-count measure).
        ladder = parse_ladder_agg(agg_form) if graph_style in LADDER_STYLES else None
        if graph_style in LADDER_STYLES and ladder is None:
            return HttpResponseBadRequest(
                f"{graph_style} measure needs a quantile(col) or "
                f"extent(col) agg, got {agg_form!r}")
        rungs = source.rungs_for(agg_form, measure_name) if ladder else None

        # Pre-aggregation, after the cache miss and before the query is
        # built: a rollup that covers this request answers it from far fewer
        # rows. `pick_rollup` returning None is the normal case and not an
        # error — the read path then reads `fact`, which is always correct.
        # Skipped entirely when the datasource has no rollup configured, so
        # the common installation pays no extra round trip.
        #
        # This block sits *above* the ladder branch rather than inside the
        # scalar one, because an extent ladder is served from here too. Only a
        # percentile ladder skips it, and `pick_rollup` refuses that case
        # itself — asking it is how the two stay on one rule.
        rollup_table = rollup_value_expr = None
        if source.rollup_interval_s:
            spec = pick_rollup(
                source, read_rollup_states(meta, source.id),
                group_by=group_by_raw, filter_tree=filter_tree,
                measure=measure, graph_style=graph_style,
                interval=interval, t_from=t_from, t_to=t_to,
                ladder_styles=LADDER_STYLES)
            if spec is not None:
                rollup_table = spec.name
                rollup_value_expr = measure_expr(measure, spec, rungs=rungs)
                if rollup_value_expr is None:
                    # Declined after the pick — fall back to `fact` whole
                    # rather than pointing the FROM at a rollup with no
                    # expression to read out of it.
                    rollup_table = None

        if ladder:
            ladder_kind, ladder_col = ladder
            plot = timeseries_ladder(
                source,
                measure_col=ladder_col,
                rungs=rungs,
                ladder_kind=ladder_kind,
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
                rollup_table=rollup_table,
                rollup_value_expr=rollup_value_expr,
                dest_engine=source.request_engine(),
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
                rollup_table=rollup_table,
                rollup_value_expr=rollup_value_expr,
                dest_engine=source.request_engine(),
            )
        if cache_key is not None:
            put_cached_plot(meta, cache_key=cache_key,
                            datasource_id=source.id,
                            cache_epoch=cache_epoch,
                            group_by=group_by_raw,
                            measure=measure,
                            payload=plot)
        # Every response carries the datasource's cache epoch, because the
        # server-side cache is not the only one. The chart keeps a 50 MB
        # in-page LRU keyed on the query string alone, and a window below a
        # retention floor is the most cacheable shape there is — "final, not
        # trailing" — so an open tab would happily keep painting bars whose
        # partitions have been dropped. Stamped after the store so the
        # persisted blob stays free of a value that would age inside it.
        plot["cache_epoch"] = cache_epoch
        return JsonResponse(plot, json_dumps_params={"default": str})
    except (OperationalError, ProgrammingError, LookupError) as exc:
        message = _friendly_db_error(exc, chart=True)[0]
        if message is None:   # none of the normal states — stay a real 500
            return JsonResponse({"error": repr(exc)}, status=500)
        return JsonResponse({"ok": False, "error": message})
    except ValueError as exc:
        # A user-input error (bad filter column/op, an unparseable filter
        # value, a bad date): the chart surfaces "ok": false the same way it
        # does for _friendly_db_error's states — before, it was a 500 with
        # repr(exc), which made a typo'd filter look like a server fault.
        return JsonResponse({"ok": False, "error": str(exc)})
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
            source, source.request_engine(),
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
    except (OperationalError, ProgrammingError, LookupError) as exc:
        message = _friendly_db_error(exc, chart=True)[0]
        if message is None:   # none of the normal states — stay a real 500
            return JsonResponse({"error": repr(exc)}, status=500)
        return JsonResponse({"ok": False, "error": message})
    except ValueError as exc:
        # A user-input error (bad filter column/op, an unparseable filter
        # value, a bad date): the chart surfaces "ok": false the same way it
        # does for _friendly_db_error's states — before, it was a 500 with
        # repr(exc), which made a typo'd filter look like a server fault.
        return JsonResponse({"ok": False, "error": str(exc)})
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
            source, source.request_engine(),
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
    except (OperationalError, ProgrammingError, LookupError) as exc:
        message = _friendly_db_error(exc, chart=True)[0]
        if message is None:   # none of the normal states — stay a real 500
            return JsonResponse({"error": repr(exc)}, status=500)
        return JsonResponse({"ok": False, "error": message})
    except ValueError as exc:
        # A user-input error (bad filter column/op, an unparseable filter
        # value, a bad date): the chart surfaces "ok": false the same way it
        # does for _friendly_db_error's states — before, it was a 500 with
        # repr(exc), which made a typo'd filter look like a server fault.
        return JsonResponse({"ok": False, "error": str(exc)})
    except Exception as exc:
        return JsonResponse({"error": repr(exc)}, status=500)


def _normalize_gap_profile(payload: dict) -> dict:
    """gap_day_profile / JSON-cache payload → the internal shape every reader
    (detect_gaps, gap_estimate_json) consumes: int-keyed `hours` and
    `per_day`, numeric `days`, and `source` naming the table the numbers
    came from. The cached payload's keys are JSON strings (the store keeps
    string keys so the payload survives the JSON roundtrip byte-identical);
    re-keying happens here and only here.
    """
    return {
        "prof_from": int(payload["prof_from"]),
        "prof_to": int(payload["prof_to"]),
        "days": float(payload["days"]),
        "hours": {int(k): float(v) for k, v in payload["hours"].items()},
        "per_day": {int(k): [float(x) for x in v]
                    for k, v in (payload.get("per_day") or {}).items()},
        "source": payload.get("source") or "fact",
    }


def _load_gap_profile(meta, source, filter_tree, hwm, horizon_epoch,
                      profile_days, cache_epoch):
    """The gap overlay's day profile λ̂(h): rollup-first with fact fallback.

    Resolution order: `gap_profile_bounds` (day-aligned window) →
    `read_rollup_states` → `pick_profile_rollup` (only when
    `source.rollup_interval_s` is set) → cache roundtrip under
    kind="gap_profile" → `gap_day_profile` on a miss. The cache key encodes
    the source table via `series_label` ("fact" vs. the rollup's name): both
    payloads have the same shape, but a rollup profile over the same window
    and filters is a *different* λ̂ (non-final edge, bucket-granular count),
    and a hit across tables would be a silently wrong number.

    Returns the normalized internal shape (see `_normalize_gap_profile`);
    `per_day` is what `gap_estimate_json`'s hour table reads, `source` is
    what the response's `profile.source` reports.
    """
    prof_from, prof_to = gap_profile_bounds(
        source, horizon_epoch, hwm, profile_days)
    picked = None
    rollup_ok = bool(getattr(source, "rollup_interval_s", 0))
    if rollup_ok:
        states = read_rollup_states(meta, source.id)
        picked = pick_profile_rollup(source, states, filter_tree=filter_tree,
                                     prof_from=prof_from, prof_to=prof_to)
    if picked is not None:
        table, count_expr = picked
    else:
        table, count_expr = None, "count(*)"
    series_label = table or "fact"

    prof_key = make_cache_key(
        datasource_id=source.id,
        group_by="",
        measure=None,
        t_from_epoch=prof_from,
        t_to_epoch=prof_to,
        interval=86400,
        filter_tree=filter_tree,
        series_label=series_label,
        cache_epoch=cache_epoch,
        kind="gap_profile")
    cached = get_cached_plot(meta, prof_key, max_age_s=GAP_PROFILE_TTL_S)
    if cached is not None:
        return _normalize_gap_profile(cached)

    payload = gap_day_profile(
        source, prof_from=prof_from, prof_to=prof_to,
        filter_tree=filter_tree or None,
        table=table, count_expr=count_expr,
        dest_engine=source.request_engine())
    put_cached_plot(meta, cache_key=prof_key, datasource_id=source.id,
                    cache_epoch=cache_epoch, group_by="", measure=None,
                    payload={"kind": "gap_profile", **payload})
    return _normalize_gap_profile(payload)


def gaps_json(request):
    """Gap overlay endpoint: 6σ event gaps behind the chart's filters.

    Runs `core.query.detect_gaps` on the chart's exact window and filters,
    `count(*)` only. `group_by` is deliberately ignored (and deliberately
    not a parameter of detect_gaps): a gap is a bucket **no row falls into**
    — the total transaction stream — not a per-series question.

    Required: ?name=  &from=  &to=
    Optional: ?filter=  (same syntax as plot_json)  &profile_days= (1..30)

    `?interval=` is deliberately **no longer read** and simply ignored, not
    a 400: the client of this deployment stopped sending it, and ignoring
    keeps a stale bookmark or a half-deployed client working instead of
    failing the whole overlay. `?measure=` still 400s — the 6σ test is
    defined on count(*), and a measure request here would silently compute
    a different question.

    The day profile λ̂(h) behind the 6σ test is loaded via
    `_load_gap_profile` — rollup-first with fact fallback, cached
    server-side under `kind="gap_profile"` — and detect_gaps no longer
    runs its own profile query. The gap rows themselves are never cached
    server-side: they are the incremental part, the client re-requests only
    new coverage.
    """
    try:
        q             = request.GET
        name          = q.get("name")
        t_from_s      = q.get("from")
        t_to_s        = q.get("to")
        # `group_by` is deliberately not read — detect_gaps runs on the total
        # transaction stream, not per series.
        if not all([name, t_from_s, t_to_s]):
            return HttpResponseBadRequest("missing required params")
        if q.get("measure"):
            return HttpResponseBadRequest("gaps needs count(*) (no ?measure=)")
        t_from = _parse_dt(t_from_s)
        t_to   = _parse_dt(t_to_s)
        if t_to <= t_from:
            return HttpResponseBadRequest("to must be after from")

        profile_days = GAP_PROFILE_DAYS
        if q.get("profile_days"):
            try:
                profile_days = int(q.get("profile_days"))
            except ValueError:
                profile_days = GAP_PROFILE_DAYS
        profile_days = max(1, min(30, profile_days))

        filter_tree = _parse_filter_tree(q)

        meta = _meta_engine()
        source = Source.load(meta,
                             dest_dsn=settings.STARCUBES_POSTGRES_DSN,
                             name_or_id=name)
        # Every query function validates its own filters now (core.query's
        # four `_build_where` wrappers call `validate_filters`), but not one
        # of them runs before `_load_gap_profile` builds its cache key from
        # this tree and possibly returns a hit. Keeping the check here is what
        # makes the answer independent of whether the profile was cached.
        # Same order as gap_estimate_json.
        validate_filter_tree(source, filter_tree)
        hwm, cache_epoch = _read_hwm_and_epoch(meta, source.id)
        hwm_dt = (datetime.fromtimestamp(hwm, timezone.utc)
                  if hwm is not None else None)

        # --- profile load (rollup-first, cached) ----------------------------
        # Anchored at the **watermark**, not at this request's `to`. λ̂ keyed
        # on the request window made the same gap score differently depending
        # on which slice the client happened to ask for — and the client
        # splits its own demand, so that is not under anyone's control. On
        # this data a gap on 01.04. scored μ = 25.1, 40.9 or 63.3 for a `to`
        # of 03.04., 05.04. or 08.04., and went undetected entirely for
        # 02.04., whose profile window reaches back before the first row in
        # `fact`. One anchor per source and filter set: the verdict on a gap
        # no longer depends on how it was looked at, and the profile cache is
        # hit instead of being re-keyed per window. The price is that a gap
        # months back is measured against the *current* day profile.
        profile = _load_gap_profile(
            meta, source, filter_tree, hwm,
            hwm if hwm is not None else int(t_to.timestamp()),
            profile_days, cache_epoch)
        result = detect_gaps(
            source, t_from=t_from, t_to=t_to,
            filter_tree=filter_tree or None, profile_days=profile_days,
            hwm=hwm_dt, profile=profile,
            dest_engine=source.request_engine())

        result["cache_epoch"] = cache_epoch
        return JsonResponse(result, json_dumps_params={"default": str})
    except (OperationalError, ProgrammingError, LookupError) as exc:
        message = _friendly_db_error(exc, chart=True)[0]
        if message is None:   # none of the normal states — stay a real 500
            return JsonResponse({"error": repr(exc)}, status=500)
        return JsonResponse({"ok": False, "error": message})
    except ValueError as exc:
        # A user-input error (bad filter column/op, an unparseable filter
        # value, a bad date): the chart surfaces "ok": false the same way it
        # does for _friendly_db_error's states — before, it was a 500 with
        # repr(exc), which made a typo'd filter look like a server fault.
        return JsonResponse({"ok": False, "error": str(exc)})
    except Exception as exc:
        return JsonResponse({"error": repr(exc)}, status=500)


# Width of the gap estimate's neighbour windows, in seconds: the rate the
# stream actually carried in the ~±5 minutes either side of the gap, read
# from `fact` (rollups are bucket-granular and cannot see a 5-minute rate).
GAP_NEIGHBOR_S = 300


def gap_estimate_json(request):
    """Backend numbers for the gap overlay's click-through estimate dialog.

    Answers for one detected gap: the expected event count μ over the
    transaction distance, the profile's per-hour-of-day detail (Ø/Min/Max
    from the per-day series the shared profile payload carries), and the
    rate the stream actually carried just before/after the gap from two
    small fact COUNTs. Computed entirely in the backend so the client only
    renders.

    Required: ?name=  &left_txn=  &right_txn=  (epoch seconds, the gap's
              txn anchors exactly as gaps.json reported them)
    Optional: ?filter=  (same syntax as gaps.json)  &profile_days= (1..30)

    The profile part shares the gaps.json cache key (same profile window,
    filters, series_label and epoch), so on a warm cache this endpoint
    costs one meta round trip and two small fact COUNTs.

    Response:
      left_txn, right_txn, span_s, expected_events,
      hours[]  ({h, avg, min, max, expected} per covered hour-of-day;
                avg = λ̂(h)·3600, min/max from the profile's per-day series),
      dimensions[]  (drill.json's exact shape, but the counts are
                round(share · μ) — shares read from the profile window over
                the hours this gap touches, see `gap_reference_drill`. The
                client renders it through the same path as a bar's drill-down,
                which is the point: a gap should break down like a bar does),
      neighbor_rate_l_s / neighbor_rate_r_s (rows/s just before/after),
      neighbor_rate_s (both neighbours combined, None when neither side
                could be measured),
      clamped {left, right, profile} — True when that neighbour window was
                shortened by the retention floor / watermark, and `profile`
                when the profile window itself was shortened (fewer days of
                history than requested, i.e. λ̂ rests on less data),
      profile_source ("fact" | rollup table name), cache_epoch.
    """
    try:
        q = request.GET
        name      = q.get("name")
        left_s    = q.get("left_txn")
        right_s   = q.get("right_txn")
        if not all([name, left_s, right_s]):
            return HttpResponseBadRequest("missing required params")
        if q.get("measure"):
            return HttpResponseBadRequest("gap estimate needs count(*) (no ?measure=)")
        try:
            left_txn = int(left_s)
            right_txn = int(right_s)
        except ValueError:
            return HttpResponseBadRequest(
                "left_txn/right_txn must be epoch seconds")
        if right_txn <= left_txn:
            return HttpResponseBadRequest("right_txn must be after left_txn")

        profile_days = GAP_PROFILE_DAYS
        if q.get("profile_days"):
            try:
                profile_days = int(q.get("profile_days"))
            except ValueError:
                profile_days = GAP_PROFILE_DAYS
        profile_days = max(1, min(30, profile_days))

        filter_tree = _parse_filter_tree(q)

        meta = _meta_engine()
        source = Source.load(meta,
                             dest_dsn=settings.STARCUBES_POSTGRES_DSN,
                             name_or_id=name)
        validate_filter_tree(source, filter_tree)

        hwm, cache_epoch = _read_hwm_and_epoch(meta, source.id)

        # The same watermark anchor gaps_json uses — and it has to be the
        # same one, or this dialog would report a different μ than the
        # bracket that opened it and the list it was picked from. As a
        # by-product the two now share the profile cache entry *always*,
        # where before they only did when their windows happened to end on
        # the same day.
        profile = _load_gap_profile(
            meta, source, filter_tree, hwm,
            hwm if hwm is not None else right_txn,
            profile_days, cache_epoch)

        # (a) μ = ∫λ̂ dt over the transaction distance
        mu = _profile_integral(profile["hours"], left_txn, right_txn)

        # (b) the covered hours-of-day with their per-day detail
        hours = []
        seen = set()
        seg = (left_txn // 3600) * 3600
        while seg < right_txn and len(seen) < 24:
            h = (seg % 86400) // 3600
            if h not in seen:
                seen.add(h)
                rate = profile["hours"].get(h, 0.0)
                per = profile["per_day"].get(h) or []
                hours.append({
                    "h": h,
                    "avg": rate * 3600.0,
                    "min": min(per) if per else 0.0,
                    "max": max(per) if per else 0.0,
                    "expected": rate * 3600.0,
                })
            seg += 3600

        # (b2) the interpolated breakdown over those same hours. Shares come
        # from the profile window μ itself rests on, so the two halves of the
        # estimate — how many and of what — cannot disagree about their basis.
        dimensions = gap_reference_drill(
            source, source.request_engine(),
            prof_from=profile["prof_from"], prof_to=profile["prof_to"],
            hours=[h["h"] for h in hours], mu=mu,
            filter_tree=filter_tree or None)

        # (c) neighbour rates ±GAP_NEIGHBOR_S out of fact, clamped at the
        # retention floor and the watermark — a window the data cannot
        # support would otherwise read as a suspicious 0/s.
        floor_dt = getattr(source, "retention_floor", None)
        if floor_dt is not None and floor_dt.tzinfo is None:
            floor_dt = floor_dt.replace(tzinfo=timezone.utc)
        floor_epoch = (int(floor_dt.timestamp())
                       if floor_dt is not None else None)

        clamped_l = clamped_r = False
        rate_l = rate_r = None
        span_l = span_r = 0

        l_from = left_txn - GAP_NEIGHBOR_S
        if floor_epoch is not None and l_from < floor_epoch:
            l_from = floor_epoch
            clamped_l = True
        span_l = left_txn - l_from
        if span_l > 0:
            rate_l = gap_neighbor_rate(
                source, t_from=l_from, t_to=left_txn,
                filter_tree=filter_tree or None,
                dest_engine=source.request_engine())

        r_to = right_txn + GAP_NEIGHBOR_S
        if hwm is not None and r_to > hwm:
            r_to = hwm
            clamped_r = True
        span_r = r_to - right_txn
        if span_r > 0:
            rate_r = gap_neighbor_rate(
                source, t_from=right_txn, t_to=r_to,
                filter_tree=filter_tree or None,
                dest_engine=source.request_engine())

        measured = [(r, s) for r, s in ((rate_l, span_l), (rate_r, span_r))
                    if r is not None]
        neighbor_rate_s = (sum(r * s for r, s in measured)
                           / sum(s for _r, s in measured)) if measured else None

        # (d) profile-edge clamp flag: the profile window is day-aligned at
        # the last full day, so λ̂ is keyed by hour-of-day and a gap may
        # legally reach past its edge — but when the *window itself* was
        # shortened (retention floor ate part of it), λ̂ rests on fewer days
        # than requested and every rate carries that uncertainty.
        clamped_profile = profile["days"] < profile_days

        return JsonResponse({
            "left_txn": left_txn,
            "right_txn": right_txn,
            "span_s": right_txn - left_txn,
            "expected_events": mu,
            "hours": hours,
            "dimensions": dimensions,
            "neighbor_rate_s": neighbor_rate_s,
            "neighbor_rate_l_s": rate_l,
            "neighbor_rate_r_s": rate_r,
            "clamped": {"left": clamped_l, "right": clamped_r,
                        "profile": clamped_profile},
            "profile_source": profile["source"],
            "cache_epoch": cache_epoch,
        }, json_dumps_params={"default": str})
    except (OperationalError, ProgrammingError, LookupError) as exc:
        message = _friendly_db_error(exc, chart=True)[0]
        if message is None:   # none of the normal states — stay a real 500
            return JsonResponse({"error": repr(exc)}, status=500)
        return JsonResponse({"ok": False, "error": message})
    except ValueError as exc:
        # A user-input error (bad filter column/op, an unparseable filter
        # value, a bad date): the chart surfaces "ok": false the same way it
        # does for _friendly_db_error's states — before, it was a 500 with
        # repr(exc), which made a typo'd filter look like a server fault.
        return JsonResponse({"ok": False, "error": str(exc)})
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
        with source.request_engine().begin() as conn:
            # Readiness rides the dim guard's probe rather than costing its own
            # round trip: fact's name joins the primary dim tables in the one
            # unnest(text[]) pass, and its absence is what `not_ready` below
            # reports. Rollup tables are deliberately not probed — `pick_rollup`
            # consults rollup_state, whose spec_hash already degrades a changed
            # config to `fact`, and a genuinely missing table surfaces through
            # the request's own 42P01 instead (see `_friendly_db_error`).
            dim_names = [f"{source.schema_name}.{d.dim_table}"
                         for d in source.primary_dimensions()]
            fact_name = f"{source.schema_name}.fact"
            present = _existing_relations(conn, dim_names + [fact_name])
            not_ready = None
            if fact_name not in present:
                not_ready = {
                    "reason": "fact table not created yet — run Create Schema "
                              "on the source page",
                    "missing": ["fact"],
                }
            else:
                missing = [d.dim_table
                           for d in source.primary_dimensions()
                           if f"{source.schema_name}.{d.dim_table}" not in present]
                if missing:
                    not_ready = {
                        "reason": f"missing in schema {source.schema_name}: "
                                  f"{', '.join(missing)} — run Create Schema "
                                  "on the source page",
                        "missing": missing,
                    }
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
                    with source.request_engine().begin() as dconn:
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
        # NOTE: do not shadow `fact_cols` here — it still feeds the
        # "fact_columns" response key below.
        for token, group_cols in source.filter_fanout().items():
            members = [dim_by_fc[c] for c in group_cols]
            dim_table = next(
                (m.dim_table for m in members
                 if m.is_primary and m.dim_table), None) or members[0].dim_table
            if not dim_table:
                continue
            dim_filters.append({
                "token":     token,
                "label":     token,
                "dim_table": dim_table,
                "fact_cols": group_cols,
            })
        dim_filters.sort(key=lambda f: f["token"])

        with _meta_engine().begin() as conn:
            wm = conn.execute(text(
                "SELECT min(first_end_time) AS lo, "
                "       max(last_end_time)  AS hi "
                "  FROM starcubes_meta.etl_run "
                " WHERE datasource_id = :ds AND status = 'ok'"
            ), {"ds": source.id}).mappings().one()
        # The run history remembers windows whose partitions have since been
        # retired. Clamp the advertised extent to the retention floor, or the
        # chart offers a range where fact is empty on purpose and a user
        # reads it as data loss.
        #
        # Rollups are the exception, and the reason this is not a plain clamp
        # any more: retention deliberately leaves them alone, so below the
        # floor they hold counts that are still correct — computed while the
        # raw rows existed. The extent therefore reaches to whichever is
        # older, and `rollup_only_below` tells the chart where the answers
        # get thinner: counts and breakdowns over the rollup's columns still
        # work there, percentiles, drill-down and filters on columns outside
        # it do not.
        wm = dict(wm)
        rollup_only_below = None
        if source.retention_floor is not None and wm["lo"] is not None:
            floor = source.retention_floor
            if floor.tzinfo is None:
                floor = floor.replace(tzinfo=timezone.utc)
            lo = wm["lo"]
            if lo.tzinfo is None:
                lo = lo.replace(tzinfo=timezone.utc)
            limit = floor
            if source.rollup_interval_s:
                oldest, _newest = rollup_extent(
                    read_rollup_states(_meta_engine(), source.id))
                if oldest is not None and oldest < floor:
                    limit = oldest
                    rollup_only_below = int(floor.timestamp())
            wm["lo"] = max(lo, limit)

        return JsonResponse({
            "id":                       source.id,
            "name":                     source.name,
            "schema_name":              source.schema_name,
            # Non-null when the warehouse side is not there yet: the schema,
            # `fact` or a primary dim table missing. The chart shows the
            # reason in its own status-line span and gates its fetch cycle on
            # it (chart.html's updateSourceNotice / _autoFetchOnce). Rollup
            # tables are not probed — see the probe block above.
            "not_ready":                not_ready,
            "partition_column":         source.partition_column,
            # Unix seconds below which only the rollups answer, or null.
            # Everything under it still has counts and breakdowns over the
            # rollup's columns; percentiles, drill-down and filters on other
            # columns return nothing there, because fact is empty on purpose.
            "rollup_only_below":        rollup_only_below,
            "fact_columns":             fact_cols,
            "dimensions":               dimensions,
            "dim_group_sets":           dim_group_sets,
            "dim_filters":              dim_filters,
            "measures":                 [{
                "name":        m.name,
                "agg":         m.agg,
                "graph_style": m.graph_style,
                "is_default":  m.is_default,
            } for m in source.measures],   # `name` is the row's identity — see measure_row
            "interval_ladder":          source.interval_ladder,
            "bar_pixel_target":          source.bar_pixel_target,
            "pk_format":                source.pk_format,
            "time_range": {
                "min": wm["lo"].isoformat() if wm["lo"] else None,
                "max": wm["hi"].isoformat() if wm["hi"] else None,
            },
        }, json_dumps_params={"default": str})
    except (OperationalError, ProgrammingError, LookupError) as exc:
        message = _friendly_db_error(exc, chart=True)[0]
        if message is None:   # none of the normal states — stay a real 500
            return JsonResponse({"error": repr(exc)}, status=500)
        return JsonResponse({"ok": False, "error": message})
    except ValueError as exc:
        # A user-input error (bad filter column/op, an unparseable filter
        # value, a bad date): the chart surfaces "ok": false the same way it
        # does for _friendly_db_error's states — before, it was a 500 with
        # repr(exc), which made a typo'd filter look like a server fault.
        return JsonResponse({"ok": False, "error": str(exc)})
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


def _friendly_db_error(exc, *, chart: bool = False) -> tuple[str | None, bool]:
    """Turn a SQLAlchemy DBAPI error into (message, was_cancelled).

    Postgres' own message carries the error position and is far more useful to
    someone editing SQL than repr() of the SQLAlchemy wrapper, so it is passed
    through rather than summarised.

    With ``chart=True`` — the chart's four JSON endpoints — the states a
    half-configured source is *allowed* to be in translate into the status
    line's normal-case messages, and the caller answers them with HTTP 200 +
    ``{"ok": False, ...}`` instead of the 500 + repr(exc) convention the rest
    of viz keeps (see the comment block above the query console: a source
    without DDL is the same kind of case as a console user's syntax error).
    None as the message signals "none of the normal states": the caller falls
    through to the unchanged 500 convention, so a genuine program error stays
    a 500. The console (chart=False, the default) keeps its own translations —
    a 42P01 there is a table the *user's SQL* named, and Postgres' own
    message with its error position is the useful answer.
    """
    orig = getattr(exc, "orig", None)
    code = getattr(orig, "pgcode", None)
    if code == "57014":      # query_canceled — also what statement_timeout raises
        return "Query cancelled or timed out.", True
    if code == "25006":      # read_only_sql_transaction
        return "This console is read-only.", False
    if chart:
        if code == "42P01":  # undefined_table
            return _missing_relation_note(orig), False
        if isinstance(exc, OperationalError):
            return "warehouse not reachable — check the database connection", False
        if isinstance(exc, LookupError):
            # Raised only by `Source.load` — see the single raise in
            # core/source.py — so "datasource … not found" is the honest text.
            return str(exc).strip(), False
        return None, False
    return str(orig or exc).strip(), False


def _missing_relation_note(orig) -> str:
    """The 'run Create Schema' note for an undefined_table failure.

    The relation's name is display only, pulled from Postgres' own message so
    the note can name what is missing — the dispatch above decided on
    ``orig.pgcode`` alone, never on this text. Both locales quote the name
    (English as "schema.table", the German one as »schema.table«); when the
    wording does not cooperate the note degrades to its generic form rather
    than misnaming anything.
    """
    m = re.search(r'[»"]([A-Za-z0-9_.]+)[«"]', str(orig))
    name = m.group(1) if m else None
    if name and "." in name:
        schema, rel = name.split(".", 1)
        return (f"missing in schema {schema}: {rel} — "
                "run Create Schema on the source page")
    return ("missing relation in the warehouse — "
            "run Create Schema on the source page")


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

    Like `plot_json`, the statement may read a rollup instead of `fact`:
    `pick_rollup` is consulted with the legend's own parameters and its
    decision returned as the `table` field so the console status line can
    name it.
    """
    try:
        source = _console_source(request)
        q = request.GET
        if not q.get("from") or not q.get("to"):
            return HttpResponseBadRequest("missing ?from / ?to")

        group_by_raw = q.get("group_by", "")
        measure      = q.get("measure") or None
        # Same disambiguation as plot_json — the legend has to resolve the
        # same catalog row the chart drew, or the copied SQL is for the other
        # measure of that agg.
        measure_name = q.get("measure_name") or None
        series_label = q.get("series_label", "source_id")
        t_from = _parse_dt(q["from"])
        t_to   = _parse_dt(q["to"])

        filter_tree = _parse_filter_tree(q)

        # Graph style / ladder routing — mirrors plot_json's dispatch
        # (viz/views.py around the agg_form + graph_style switch). The
        # helper expects `measure=None` and explicit `rungs` / `ladder_kind` /
        # `measure_col` for ladder-style branches. `measure` is cleared only
        # *after* the rollup pick below, which still needs the agg form.
        agg_form = measure if measure else "count(*)"
        graph_style = source.graph_style_for(agg_form, measure_name)
        ladder = parse_ladder_agg(agg_form) if graph_style in LADDER_STYLES else None
        if graph_style in LADDER_STYLES and ladder is None:
            return HttpResponseBadRequest(
                f"{graph_style} needs quantile(col) or extent(col), "
                f"got {agg_form!r}")
        ladder_kind, measure_col = ladder if ladder else (None, None)
        rungs = source.rungs_for(agg_form, measure_name) if ladder else None

        # Multibar / quantile-bands :grouped — map dim index → group name.
        # Same shape plot_json builds (viz/views.py). The helper uses this
        # for the per-index Python merge (multibar) or the SQL-side CASE
        # (quantile-bands).
        group_by, _gb_dim, gb_set = _resolve_group_by(source, group_by_raw)
        group_by_groups = None
        if gb_set and gb_set.groups:
            group_by_groups = _group_index_map(gb_set)

        # Same conditions as the chart's own data path (`plot_json`'s
        # pick_rollup call): when they hold, the legend aggregate is exact
        # from rollup rows — measure_expr's folding expressions are the
        # whole-window counterparts of the per-slot ones build_chart_sql
        # binds. Evaluated independently of what the chart's slot query
        # received (a 15-min-slot chart against a 1-h rollup serves its
        # bars from fact but its legend SQL from the rollup; the numbers
        # are identical either way). The legend query has no slot width of
        # its own, so interval gets the rollup's own width: condition 1
        # (`interval % iv == 0`) becomes vacuous, and the ones that actually
        # decide — both window edges on a bucket boundary, no *percentile*
        # ladder, spec_hash, [built_from, built_through) coverage,
        # measure_expr, column coverage of group_by + the filter tree — stay
        # fully effective. A misaligned edge → None → fact: the correct
        # fallback, not a bug to work around.
        rollup_table = rollup_value_expr = None
        if source.rollup_interval_s:
            spec = pick_rollup(
                source, read_rollup_states(_meta_engine(), source.id),
                group_by=group_by_raw, filter_tree=filter_tree,
                measure=measure, graph_style=graph_style,
                interval=source.rollup_interval_s,
                t_from=t_from, t_to=t_to,
                ladder_styles=LADDER_STYLES)
            if spec is not None:
                rollup_table = spec.name
                rollup_value_expr = measure_expr(measure, spec, rungs=rungs)
                if rollup_value_expr is None:
                    rollup_table = None

        # Only now: the legend builder reads `measure_col` + `rungs` for a
        # ladder and must not also see an agg form. Cleared after the pick,
        # which needed it.
        if ladder:
            measure = None

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
            rungs=rungs, ladder_kind=ladder_kind, measure_col=measure_col,
            series_label=series_label,
            group_by_groups=group_by_groups,
            rollup_table=rollup_table,
            rollup_value_expr=rollup_value_expr,
        )
        # `table` lets the console status line name what the statement
        # reads, so a user sees which copy asked fact and which the rollup.
        return JsonResponse({"ok": True, "sql": sql,
                             "table": rollup_table or "fact"})
    except (ValueError, LookupError) as exc:
        # Same convention as query_run: user-side mistake → 200 + JSON.
        return JsonResponse({"ok": False, "error": str(exc)})
    except Exception as exc:
        return JsonResponse({"error": repr(exc)}, status=500)
