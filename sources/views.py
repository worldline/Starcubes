"""Datasource configuration GUI — CRUD + ETL monitor.

All starcubes_meta reads/writes use SQLAlchemy directly (same pattern as viz/views.py).
No Django ORM — starcubes_meta is owned by the migration scripts, not Django.
"""
from __future__ import annotations

import json as _json
import os
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
import re
import signal

import sqlalchemy
from django.conf import settings
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.models import User
from django.http import (HttpResponse, HttpResponseBadRequest,
                         HttpResponseForbidden, JsonResponse)
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST
from sqlalchemy import text

from core.schema import (UNKNOWN_SOURCE_ID, SchemaNameError, create_fact,
                         create_dim_tables, create_indices, drop_indices,
                         drop_schema, sync_schema, import_dimensions,
                         create_future_partitions, list_fact_partitions,
                         vacuum_analyze_fact, refresh_dim_counts,
                         validate_schema_name)
from core.cache import bump_cache_epoch, clear_cache
from core.etl import (truncate_last_partition, truncate_partition,
                      drop_partition, delete_fact_from,
                      clear_etl_history, set_highwatermark)
from core.utils import _parse_dt, _pid_alive
from core.adhoc import history_delete_user, history_list_user
from core.audit import audit_id_details, audit_ids, audit_window
from core.profile import (ProfileError, delete_profile, dump_profile,
                          iter_profiles, load_profile, profile_origin,
                          profile_path, save_profile, validate_profile_name,
                          MAX_PROFILE_BYTES)
from core.alarm import (check_no_grouped_filters, measure_value_expr,
                        resolve_context_labels)
from core.query import (build_filter_tree, grouped_label, grouped_token,
                        MEASURE_FUNCS, slugify_group_set,
                        validate_filter_tree)
from core.source import Source, _get_cached_engine
from sources.profile_store import (delete_profile_children,
                                   insert_profile_children,
                                   update_datasource_scalars)
from sources.etl_spawn import (PYTHON_EXE as _PYTHON_EXE,
                               spawn_etl_subprocess as _spawn_etl_subprocess,
                               log_dir as _log_dir, slugify_ds as _slugify_ds)


def _add_inline_display(dim: dict) -> dict:
    """Add a truncated display string for inline_values."""
    iv = dim.get("inline_values") or ""
    if not iv:
        dim["inline_values_display"] = "—"
    else:
        first_line = iv.split("\n", 1)[0]
        dim["inline_values_display"] = first_line + (" …" if "\n" in iv else "")
    return dim

DIALECTS = ["oracle", "postgresql", "mysql", "mssql"]
PG_TYPES = ["smallint", "bigint", "integer", "timestamptz", "text", "real", "double precision", "boolean"]
ROLES = ["time", "measure", "dimension", "auto-dimension", "pk"]
INDEX_METHODS = ["brin", "btree", "hash"]
GRAPH_STYLES = ["multibar", "multiline", "multipoint",
                "quantile-bands", "quantile-steps", "quantile-steps-open",
                "error-bars", "candlestick"]


# ---------------------------------------------------------------------------
# Auth gates
#
# The split into user vs admin pages (Commit_2) is the page side. The
# endpoint side uses these decorators — every JSON CRUD endpoint that
# mutates starcubes_meta is gated on superuser, so a non-admin who knows
# the URL cannot reach it directly. Reading endpoints are gated on login
# only, because the chart page needs them.
#
# JSON endpoints answer 403 JSON, not a 302 redirect: a fetch() that
# follows the redirect would receive HTML and r.json() would throw an
# opaque SyntaxError instead of "your session expired". Mirror the
# pattern in viz/views.py:_login_required_json.
# ---------------------------------------------------------------------------

def _login_required_json(view):
    """Gate a JSON endpoint on an authenticated session, answering 403 not 302."""
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({"ok": False, "error": "login required"}, status=403)
        return view(request, *args, **kwargs)
    return wrapped


def _admin_required_json(view):
    """Gate a JSON endpoint on superuser. 403 JSON, never a 302."""
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({"ok": False, "error": "login required"}, status=403)
        if not request.user.is_superuser:
            return JsonResponse({"ok": False, "error": "admin access required"}, status=403)
        return view(request, *args, **kwargs)
    return wrapped


def _admin_required_page(view):
    """Gate a full-page view on superuser. 403 HTML is fine here."""
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated or not request.user.is_superuser:
            return HttpResponseForbidden("Admin access required.")
        return view(request, *args, **kwargs)
    return wrapped


def _meta_engine():
    return _get_cached_engine(settings.STARCUBES_POSTGRES_DSN)


def _load_datasource(pk: int) -> dict:
    with _meta_engine().begin() as conn:
        row = conn.execute(
            text("SELECT * FROM starcubes_meta.datasource WHERE id = :id"),
            {"id": pk},
        ).mappings().one_or_none()
    if row is None:
        raise ValueError(f"datasource id={pk} not found")
    return dict(row)


def _ladder_to_str(ladder) -> str:
    if ladder is None:
        return "1,60,3600,86400"
    if isinstance(ladder, (list, tuple)):
        return ",".join(str(x) for x in ladder)
    return str(ladder)


def _str_to_ladder_sql(s: str) -> list[int]:
    return [int(x.strip()) for x in s.split(",") if x.strip()]


def _mask_dsn(dsn: str) -> str:
    """Replace the password portion of a SQLAlchemy DSN with *** for display."""
    if not dsn:
        return dsn
    return re.sub(r'(://[^:@/\s]+:)[^@\s]+(@)', r'\1***\2', dsn)


def source_list(request):
    with _meta_engine().begin() as conn:
        rows = conn.execute(text("""
            SELECT d.id, d.name, d.schema_name, d.source_dialect,
                   r.status      AS run_status,
                   r.finished_at AS run_finished_at,
                   r.rows        AS run_rows,
                   r.error       AS run_error
              FROM starcubes_meta.datasource d
              LEFT JOIN LATERAL (
                  SELECT status, finished_at, rows, error
                    FROM starcubes_meta.etl_run
                   WHERE datasource_id = d.id
                   ORDER BY id DESC
                   LIMIT 1
              ) r ON true
             ORDER BY d.name
        """)).mappings().all()
    return render(request, "sources/list.html", {
        "sources": [dict(r) for r in rows],
        "current_source_id": None,
        "current_source_name": None,
    })


def _lag_pair(p):
    """(watermark_lag_s, wallclock_lag_s) from a POST, validated.

    Mirrors datasource_wallclock_lag_check. Validating here rather than
    catching the IntegrityError keeps the message readable and matches how
    the partition_period change is rejected a few lines down; the CHECK
    stays as the backstop for writes that do not come through this form.
    """
    lag_wm = int(p.get("watermark_lag_s") or 0)
    lag_now = int(p.get("wallclock_lag_s") or 0)
    if lag_wm < 0 or lag_now < 0:
        raise ValueError("Lag values cannot be negative.")
    if lag_now and lag_now <= lag_wm:
        raise ValueError(
            f"Wall-clock lag ({lag_now}s) must be greater than watermark lag "
            f"({lag_wm}s), or 0 to disable it. Otherwise the wall-clock term "
            f"would win even under running traffic and the watermark lag "
            f"would have no effect.")
    return lag_wm, lag_now


@_admin_required_page
def source_new(request):
    if request.method == "POST":
        p = request.POST
        ladder = _str_to_ladder_sql(p.get("interval_ladder", "1,60,3600,86400"))
        # Empty string, not None: the column is NOT NULL, but a datasource
        # may legitimately exist before anyone has a DSN for it — the
        # profile flow is the whole point of allowing that.
        preload = (p.get("preload_profile") or "").strip()
        try:
            # The schema name is interpolated unquoted into the warehouse
            # DDL, so it has to survive Postgres case-folding unchanged.
            validate_schema_name(p.get("schema_name", ""))
            lag_wm, lag_now = _lag_pair(p)
        except (SchemaNameError, ValueError) as exc:
            return HttpResponseBadRequest(str(exc))
        profile = None
        if preload:
            try:
                profile = load_profile(preload)
            except ProfileError as exc:
                return HttpResponseBadRequest(str(exc))
        with _meta_engine().begin() as conn:
            row = conn.execute(text("""
                INSERT INTO starcubes_meta.datasource
                    (name, schema_name, source_dialect, source_dsn,
                     import_query, base_table,
                     watermark_column, watermark_max_query,
                     partition_column, partition_period,
                     chunksize, transform_procs,
                     small_window_hint, interval_ladder, bar_pixel_target, pk_format,
                     late_arrival_overlap_s, watermark_fact_expr,
                     watermark_lag_s, wallclock_lag_s)
                VALUES
                    (:name, :schema_name, :dialect, :dsn,
                     NULLIF(:import_query, ''), NULLIF(:base_table, ''),
                     :watermark_column, NULLIF(:watermark_max_query, ''),
                     :partition_column, :partition_period,
                     :chunksize, :transform_procs,
                     NULLIF(:small_window_hint, ''),
                     CAST(:ladder AS int[]), :bar_pixel_target, NULLIF(:pk_format, ''),
                     :late_arrival_overlap_s, NULLIF(:watermark_fact_expr, ''),
                     :watermark_lag_s, :wallclock_lag_s)
                RETURNING id
            """), {
                "name":               p["name"],
                "schema_name":        p["schema_name"],
                "dialect":            p["source_dialect"],
                "dsn":                p.get("source_dsn", ""),
                "import_query":       p.get("import_query", ""),
                "base_table":         p.get("base_table", ""),
                "watermark_column":   p["watermark_column"],
                "watermark_max_query": p.get("watermark_max_query", ""),
                "partition_column":   p["partition_column"],
                "partition_period":   p.get("partition_period", "month"),
                "chunksize":          int(p.get("chunksize", 10000)),
                "transform_procs":    int(p.get("transform_procs", 1)),
                "small_window_hint":  p.get("small_window_hint", ""),
                "ladder":             "{" + ",".join(str(x) for x in ladder) + "}",
                "bar_pixel_target":   float(p.get("bar_pixel_target", 5)),
                "pk_format":          p.get("pk_format", ""),
                "late_arrival_overlap_s": int(p.get("late_arrival_overlap_s") or 0),
                "watermark_fact_expr": p.get("watermark_fact_expr", ""),
                "watermark_lag_s":    lag_wm,
                "wallclock_lag_s":    lag_now,
            }).mappings().one()
            if profile is not None:
                # Only the child rows: the form fields above were already
                # prefilled from this profile in the browser, so the scalar
                # values the user is looking at are the ones just stored.
                insert_profile_children(conn, row["id"], profile)
        return redirect("sources:manage", pk=row["id"])
    return render(request, "sources/new.html", {
        "dialects": DIALECTS,
        "partition_periods": ["month", "week", "day"],
        "defaults": {
            "chunksize": 10000,
            "transform_procs": 1,
            "interval_ladder": "1,60,3600,86400",
            "bar_pixel_target": 5,
            "partition_period": "month",
        },
        # The profile picker lists every profile under profiles/ — shipped
        # examples and saved ones alike. Selecting one prefills the form
        # from ``profile_detail``; see ``docs/source-profiles.md``.
        "profiles": iter_profiles(),
        "selected_profile": request.GET.get("profile", ""),
        "current_source_id": "new",
        "current_source_name": None,
    })


def _build_source_context(pk: int) -> dict:
    """The full context the Manage page renders, also used by source_detail
    so the two stay in lock-step. Kept separate so source_manage can hand
    the same dict to manage.html and source_detail can carve out a slim
    variant for detail.html.
    """
    ds = _load_datasource(pk)
    schema = ds["schema_name"]
    with _meta_engine().begin() as conn:
        fact_cols = conn.execute(text(
            "SELECT * FROM starcubes_meta.fact_column "
            "WHERE datasource_id = :id ORDER BY ordinal"
        ), {"id": pk}).mappings().all()
        dimensions = conn.execute(text(
            "SELECT * FROM starcubes_meta.dimension "
            "WHERE datasource_id = :id ORDER BY id"
        ), {"id": pk}).mappings().all()
        group_set_rows = conn.execute(text(
            "SELECT d.fact_column, gs.name, gs.slug "
            "  FROM starcubes_meta.dim_group_set gs "
            "  JOIN starcubes_meta.dimension d ON d.id = gs.dimension_id "
            " WHERE d.datasource_id = :id "
            " ORDER BY d.fact_column, gs.position, gs.id"
        ), {"id": pk}).mappings().all()
        indices = conn.execute(text(
            "SELECT * FROM starcubes_meta.fact_index "
            "WHERE datasource_id = :id ORDER BY id"
        ), {"id": pk}).mappings().all()
        measures = conn.execute(text(
            "SELECT * FROM starcubes_meta.measure "
            "WHERE datasource_id = :id ORDER BY ordinal, name"
        ), {"id": pk}).mappings().all()
        runs = conn.execute(text(
            "SELECT id, status, started_at, finished_at, "
            "       first_end_time, last_end_time, rows, runtime_ms, error, "
            "       rows_per_s, extract_rows_per_s, transform_rows_per_s, "
            "       load_rows_per_s, stats, log_path, log_tail "
            "  FROM starcubes_meta.etl_run "
            " WHERE datasource_id = :id ORDER BY id DESC LIMIT 20"
        ), {"id": pk}).mappings().all()

        # Storage stats — pg_stat_user_tables for the warehouse schema
        storage_tables = conn.execute(text("""
            SELECT
                t.relname                                                                             AS tablename,
                pg_size_pretty(pg_total_relation_size(t.schemaname||'.'||quote_ident(t.relname)))    AS total_size,
                pg_size_pretty(pg_relation_size(t.schemaname||'.'||quote_ident(t.relname)))          AS table_size,
                pg_size_pretty(pg_indexes_size(t.schemaname||'.'||quote_ident(t.relname)))           AS index_size,
                pg_total_relation_size(t.schemaname||'.'||quote_ident(t.relname))                    AS total_bytes,
                COALESCE(t.n_live_tup, 0)  AS approx_rows,
                COALESCE(t.n_dead_tup, 0)  AS dead_rows,
                t.last_vacuum,
                t.last_autovacuum,
                t.last_analyze,
                t.last_autoanalyze
            FROM pg_stat_user_tables t
            WHERE t.schemaname = :schema
            ORDER BY t.relname
        """), {"schema": schema}).mappings().all()

        storage_indexes = conn.execute(text("""
            SELECT
                i.indexname,
                i.tablename,
                am.amname                                                                            AS method,
                pg_size_pretty(pg_relation_size(i.schemaname||'.'||quote_ident(i.indexname)))       AS size,
                pg_relation_size(i.schemaname||'.'||quote_ident(i.indexname))                       AS size_bytes,
                ix.indisunique  AS is_unique,
                ix.indisprimary AS is_primary,
                s.idx_scan,
                s.idx_tup_read,
                s.idx_tup_fetch
            FROM pg_indexes i
            JOIN pg_class      ic ON ic.relname = i.indexname
            JOIN pg_namespace  ns ON ns.nspname = i.schemaname AND ns.oid = ic.relnamespace
            JOIN pg_index      ix ON ix.indexrelid = ic.oid
            JOIN pg_am         am ON am.oid = ic.relam
            LEFT JOIN pg_stat_user_indexes s
                   ON s.schemaname = i.schemaname AND s.indexrelname = i.indexname
            WHERE i.schemaname = :schema
            ORDER BY i.indexname
        """), {"schema": schema}).mappings().all()

        total_schema_size = conn.execute(text("""
            SELECT pg_size_pretty(SUM(pg_total_relation_size(t.schemaname||'.'||quote_ident(t.relname))))
            FROM pg_stat_user_tables t
            WHERE t.schemaname = :schema
        """), {"schema": schema}).scalar()

    ds["interval_ladder_str"] = _ladder_to_str(ds.get("interval_ladder"))
    ds["source_dsn_masked"] = _mask_dsn(ds.get("source_dsn", ""))

    dim_fc_names = {d["fact_column"] for d in dimensions if d.get("dim_table")}

    # Partition names for Storage tab action buttons
    try:
        source_obj = Source.load(_meta_engine(),
                                dest_dsn=settings.STARCUBES_POSTGRES_DSN,
                                name_or_id=pk)
        partition_names = set(list_fact_partitions(source_obj,
                                                   source_obj.dest_engine()))
    except Exception:
        partition_names = set()

    # Valid agg expressions for the measure dropdown: count(*) plus
    # func(col) for every measure-role fact column.
    measure_cols = [c["name"] for c in fact_cols if c["role"] == "measure"]
    measure_aggs = ["count(*)"] + [f"{fn}({c})"
                                   for c in measure_cols
                                   for fn in ("sum", "avg", "min", "max", "quantile")]

    # Group dimensions, for the cache-clear dropdown: the chart sends exactly
    # these tokens as its group_by param.
    group_set_tokens = [
        {"token": grouped_token(r["fact_column"], r["slug"]),
         "label": grouped_label(r["fact_column"], r["name"])}
        for r in group_set_rows
    ]

    return {
        "ds":               ds,
        "group_set_tokens": group_set_tokens,
        "fact_cols":        [dict(r) for r in fact_cols],
        "dimensions":       [_add_inline_display(dict(r)) for r in dimensions],
        "dim_fc_names_json": _json.dumps(sorted(dim_fc_names)),
        "indices":          [dict(r) for r in indices],
        "measures":         [dict(r) for r in measures],
        "runs":             [dict(r) for r in runs],
        "storage_tables":   [dict(r) for r in storage_tables],
        "storage_indexes":  [dict(r) for r in storage_indexes],
        "total_schema_size": total_schema_size or "—",
        "partition_names":  partition_names,
        "pg_types":         PG_TYPES,
        "roles":            ROLES,
        "index_methods":    INDEX_METHODS,
        "graph_styles":     GRAPH_STYLES,
        "measure_aggs":     measure_aggs,
        "pg_types_json":    _json.dumps(PG_TYPES),
        "roles_json":       _json.dumps(ROLES),
        "idx_methods_json": _json.dumps(INDEX_METHODS),
        "graph_styles_json": _json.dumps(GRAPH_STYLES),
        "measure_aggs_json": _json.dumps(measure_aggs),
        "current_source_id": pk,
        "current_source_name": ds["name"],
    }


def source_detail(request, pk: int):
    """User-facing detail page: Basics header + Group Dims + Measures + Alarms.

    Admin-only configuration lives at ``source_manage`` and renders the
    full ``manage.html`` with all 12 sub-tabs. Keeping the two apart
    means a non-admin can edit group dim memberships and inspect
    measures without seeing storage/audit/ETL history tabs that have
    nothing for them to act on.
    """
    ctx = _build_source_context(pk)
    # Carve the slim context detail.html needs. The keys not in this set
    # are present in ``manage.html`` only. measure_aggs / graph_styles are
    # passed so admins get the same inline-edit / add-form pattern here
    # that manage.html uses — see detail.html#L170-L236.
    return render(request, "sources/detail.html", {
        "ds":          ctx["ds"],
        "dimensions":  ctx["dimensions"],
        "measures":    ctx["measures"],
        "measure_aggs": ctx["measure_aggs"],
        "graph_styles": ctx["graph_styles"],
        "current_source_id":   ctx["current_source_id"],
        "current_source_name": ctx["current_source_name"],
    })


@_admin_required_page
def source_manage(request, pk: int):
    """Admin-only configuration page — superset of source_detail.

    Renders the full 12-tab ``manage.html`` with the same context
    builder. ``source_manage`` itself does not gate: routing or the
    decorator wrap does (see Commit_3).
    """
    ctx = _build_source_context(pk)
    return render(request, "sources/manage.html", ctx)


@_admin_required_page
def source_edit(request, pk: int):
    if request.method == "POST":
        p = request.POST
        ladder = _str_to_ladder_sql(p.get("interval_ladder", "1,60,3600,86400"))
        new_period = p.get("partition_period", "month")
        try:
            # Only validate a schema name the user is actually changing.
            # Rows predating the check may hold a folded name that is now
            # rejected; refusing to save them would make every other field
            # on those datasources uneditable too.
            submitted_schema = p.get("schema_name", "")
            if submitted_schema != _load_datasource(pk)["schema_name"]:
                validate_schema_name(submitted_schema)
            lag_wm, lag_now = _lag_pair(p)
        except (SchemaNameError, ValueError) as exc:
            return HttpResponseBadRequest(str(exc))
        with _meta_engine().begin() as conn:
            current_period = conn.execute(text(
                "SELECT partition_period FROM starcubes_meta.datasource WHERE id = :id"
            ), {"id": pk}).scalar()
            if current_period and new_period != current_period:
                # Reject mid-life period change once any fact partition exists.
                try:
                    source_obj = Source.load(_meta_engine(),
                                             dest_dsn=settings.STARCUBES_POSTGRES_DSN,
                                             name_or_id=pk)
                    has_parts = bool(list_fact_partitions(
                        source_obj, source_obj.dest_engine()))
                except Exception:
                    has_parts = False
                if has_parts:
                    return HttpResponseBadRequest(
                        "Cannot change partition_period once fact partitions exist. "
                        "Drop the warehouse schema first.")
            conn.execute(text("""
                UPDATE starcubes_meta.datasource SET
                    name               = :name,
                    schema_name        = :schema_name,
                    source_dialect     = :dialect,
                    source_dsn         = :dsn,
                    import_query       = NULLIF(:import_query, ''),
                    base_table         = NULLIF(:base_table, ''),
                    watermark_column   = :watermark_column,
                    watermark_max_query = NULLIF(:watermark_max_query, ''),
                    partition_column   = :partition_column,
                    partition_period   = :partition_period,
                    chunksize          = :chunksize,
                    transform_procs    = :transform_procs,
                    small_window_hint  = NULLIF(:small_window_hint, ''),
                    interval_ladder    = CAST(:ladder AS int[]),
                    bar_pixel_target   = :bar_pixel_target,
                    pk_format          = NULLIF(:pk_format, ''),
                    late_arrival_overlap_s = :late_arrival_overlap_s,
                    watermark_fact_expr = NULLIF(:watermark_fact_expr, ''),
                    watermark_lag_s    = :watermark_lag_s,
                    wallclock_lag_s    = :wallclock_lag_s,
                    updated_at         = now()
                WHERE id = :id
            """), {
                "id":                 pk,
                "name":               p["name"],
                "schema_name":        p["schema_name"],
                "dialect":            p["source_dialect"],
                "dsn":                p["source_dsn"],
                "import_query":       p.get("import_query", ""),
                "base_table":         p.get("base_table", ""),
                "watermark_column":   p["watermark_column"],
                "watermark_max_query": p.get("watermark_max_query", ""),
                "partition_column":   p["partition_column"],
                "partition_period":   new_period,
                "chunksize":          int(p.get("chunksize", 10000)),
                "transform_procs":    int(p.get("transform_procs", 1)),
                "small_window_hint":  p.get("small_window_hint", ""),
                "ladder":             "{" + ",".join(str(x) for x in ladder) + "}",
                "bar_pixel_target":   float(p.get("bar_pixel_target", 5)),
                "pk_format":          p.get("pk_format", ""),
                "late_arrival_overlap_s": int(p.get("late_arrival_overlap_s") or 0),
                "watermark_fact_expr": p.get("watermark_fact_expr", ""),
                "watermark_lag_s":    lag_wm,
                "wallclock_lag_s":    lag_now,
            })
        return redirect("sources:manage", pk=pk)
    ds = _load_datasource(pk)
    ds["interval_ladder_str"] = _ladder_to_str(ds.get("interval_ladder"))
    # Lock the partition_period dropdown if any partitions already exist.
    try:
        source_obj = Source.load(_meta_engine(),
                                 dest_dsn=settings.STARCUBES_POSTGRES_DSN,
                                 name_or_id=pk)
        period_locked = bool(list_fact_partitions(
            source_obj, source_obj.dest_engine()))
    except Exception:
        period_locked = False
    return render(request, "sources/edit.html", {
        "ds": ds,
        "dialects": DIALECTS,
        "partition_periods": ["month", "week", "day"],
        "period_locked": period_locked,
        "current_source_id": pk,
        "current_source_name": ds["name"],
    })


@require_POST
@_admin_required_json
def source_delete(request, pk: int):
    should_drop = request.POST.get("drop_schema") == "1"
    if should_drop:
        with _meta_engine().begin() as conn:
            row = conn.execute(
                text("SELECT name, schema_name FROM starcubes_meta.datasource WHERE id = :id"),
                {"id": pk},
            ).mappings().first()
        if row:
            source = Source.load(_meta_engine(), dest_dsn=settings.STARCUBES_POSTGRES_DSN,
                                 name_or_id=pk)
            drop_schema(source, source.dest_engine())
    with _meta_engine().begin() as conn:
        conn.execute(
            text("DELETE FROM starcubes_meta.datasource WHERE id = :id"),
            {"id": pk},
        )
    return redirect("sources:list")


# ---------------------------------------------------------------------------
# Async JSON views
# ---------------------------------------------------------------------------

def _test_source_connection(source_dsn: str) -> tuple[bool, str]:
    """Try connecting to the source DB with a short timeout.

    Returns (ok, error_message).  Uses ``connect_timeout=10`` via
    ``connect_args`` so unreachable hosts fail fast instead of hanging
    for the full TCP timeout.
    """
    try:
        # connect_timeout is a libpq/psycopg2 parameter; oracledb doesn't accept it.
        dialect = sqlalchemy.engine.make_url(source_dsn).drivername.split("+")[0]
        connect_args = {"connect_timeout": 10} if dialect != "oracle" else {}
        engine = sqlalchemy.create_engine(
            source_dsn,
            connect_args=connect_args,
        )
        try:
            with engine.begin() as conn:
                dialect = engine.dialect.name
                conn.execute(text(
                    "SELECT 1 FROM dual" if dialect == "oracle" else "SELECT 1"
                ))
        finally:
            engine.dispose()
        return True, ""
    except Exception as exc:
        return False, str(exc)


def _resolve_source_dsn(pk: int) -> tuple[str | None, str]:
    """Return (dsn, error). dsn is None if the datasource is missing or has no DSN."""
    try:
        ds = _load_datasource(pk)
    except ValueError as exc:
        return None, str(exc)
    dsn = ds.get("source_dsn")
    if not dsn:
        # A datasource may be configured from a profile long before anyone
        # has credentials for its source, so this is an expected state, not
        # a failure — say what to do about it.
        return None, "No source DSN configured — add one on the Edit page."
    return dsn, ""


@_admin_required_json
def test_connection(request, pk: int):
    dsn, error = _resolve_source_dsn(pk)
    if dsn is None:
        return JsonResponse({"ok": False, "error": error})
    ok, error = _test_source_connection(dsn)
    if ok:
        return JsonResponse({"ok": True})
    return JsonResponse({"ok": False, "error": error})


@_admin_required_json
def connection_status(request, pk: int):
    """GET endpoint: test source DB connection and return {ok, error}."""
    dsn, error = _resolve_source_dsn(pk)
    if dsn is None:
        return JsonResponse({"ok": False, "error": error})
    ok, error = _test_source_connection(dsn)
    if ok:
        return JsonResponse({"ok": True})
    return JsonResponse({"ok": False, "error": error})


@require_POST
@_admin_required_json
def estimate_cardinality(request, pk: int, fc_id: int):
    try:
        source = Source.load(_meta_engine(),
                             dest_dsn=settings.STARCUBES_POSTGRES_DSN,
                             name_or_id=pk)
        fc = next((c for c in source.fact_columns if c.id == fc_id), None)
        if fc is None:
            return JsonResponse({"ok": False, "error": f"fact_column {fc_id} not found"})
        if not source.base_table or not source.watermark_column:
            return JsonResponse({"ok": False, "error": "base_table and watermark_column required"})

        expr = fc.source_expr or fc.name
        wm = source.watermark_column
        dialect = source.source_dialect

        if dialect == "oracle":
            q = (f"SELECT count(DISTINCT {expr}) "
                 f"FROM {source.base_table} "
                 f"WHERE {wm} >= SYSTIMESTAMP - INTERVAL '24' HOUR")
        elif dialect == "mysql":
            q = (f"SELECT count(DISTINCT {expr}) "
                 f"FROM {source.base_table} "
                 f"WHERE {wm} >= NOW() - INTERVAL 24 HOUR")
        elif dialect == "mssql":
            q = (f"SELECT count(DISTINCT {expr}) "
                 f"FROM {source.base_table} "
                 f"WHERE {wm} >= DATEADD(hour, -24, GETDATE())")
        else:
            q = (f"SELECT count(DISTINCT {expr}) "
                 f"FROM {source.base_table} "
                 f"WHERE {wm} >= now() - interval '24 hours'")

        engine = source.source_engine()
        try:
            with engine.begin() as conn:
                count = conn.execute(text(q)).scalar()
        finally:
            engine.dispose()
        return JsonResponse({"ok": True, "count": count})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


@require_POST
@_admin_required_json
def preview_import(request, pk: int):
    try:
        source = Source.load(_meta_engine(),
                             dest_dsn=settings.STARCUBES_POSTGRES_DSN,
                             name_or_id=pk)
        if source.import_query:
            q = source.import_query
        elif source.base_table:
            q = f"SELECT * FROM {source.base_table}"
        else:
            return JsonResponse({"ok": False, "error": "no import_query or base_table configured"})

        if source.source_dialect == "oracle":
            limited = f"SELECT * FROM ({q}) WHERE ROWNUM <= 5"
        else:
            limited = f"SELECT * FROM ({q}) AS _preview LIMIT 5"

        with source.source_engine().begin() as conn:
            result = conn.execute(text(limited))
            cols = list(result.keys())
            rows = [list(r) for r in result.fetchall()]
        return JsonResponse({"ok": True, "columns": cols, "rows": rows},
                            json_dumps_params={"default": str})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


def _etl_busy_response(pk: int):
    """409 JsonResponse if an ETL is already running for this datasource.

    Also reaps stale `running` rows whose process died without updating
    the status. Returns None when the datasource is free to start a run.
    """
    with _meta_engine().begin() as conn:
        # Mark stale runs as failed (process killed/crashed before updating).
        conn.execute(text(
            "UPDATE starcubes_meta.etl_run SET status = 'failed', "
            "  error = 'stale run: process did not update status' "
            " WHERE datasource_id = :id AND status = 'running' "
            "   AND started_at < now() - interval '1 hour'"
        ), {"id": pk})
        running = conn.execute(text(
            "SELECT id FROM starcubes_meta.etl_run "
            "WHERE datasource_id = :id AND status = 'running' LIMIT 1"
        ), {"id": pk}).one_or_none()
        forever_active = conn.execute(text(
            "SELECT 1 FROM starcubes_meta.etl_forever "
            " WHERE datasource_id = :id AND status IN ('running','stopping')"
        ), {"id": pk}).scalar()
    if running:
        return JsonResponse({"ok": False, "error": "ETL already running"},
                            status=409)
    if forever_active:
        return JsonResponse(
            {"ok": False, "error": "Forever ETL is active — stop it first"},
            status=409)
    return None


@require_POST
@_admin_required_json
def etl_run(request, pk: int):
    try:
        # Parse optional window overrides from POST data.
        start = request.POST.get("start")
        end = request.POST.get("end")
        limit = request.POST.get("limit")

        busy = _etl_busy_response(pk)
        if busy is not None:
            return busy

        ds = _load_datasource(pk)
        env = os.environ.copy()
        cmd = [_PYTHON_EXE, "manage.py", "etl_run", "--name", ds["name"]]
        if start:
            cmd += ["--start", start]
        if end:
            cmd += ["--end", end]
        if limit:
            cmd += ["--limit", str(limit)]
        _proc, log_path = _spawn_etl_subprocess(cmd, env, datasource_name=ds["name"])
        return JsonResponse({"ok": True, "status": "started", "log_path": log_path})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


@_login_required_json
def etl_status(request, pk: int):
    try:
        with _meta_engine().begin() as conn:
            # Mark stale running rows as failed.
            conn.execute(text(
                "UPDATE starcubes_meta.etl_run SET status = 'failed', "
                "  error = 'stale run: process did not update status' "
                " WHERE datasource_id = :id AND status = 'running' "
                "   AND started_at < now() - interval '1 hour'"
            ), {"id": pk})
            runs = conn.execute(text(
                "SELECT id, status, started_at, finished_at, "
                "       first_end_time, last_end_time, rows, runtime_ms, error, "
                "       pid, progress_rows, progress_at, "
                "       rows_per_s, extract_rows_per_s, transform_rows_per_s, "
                "       load_rows_per_s, stats, log_path, log_tail "
                "  FROM starcubes_meta.etl_run "
                " WHERE datasource_id = :id ORDER BY id DESC LIMIT 20"
            ), {"id": pk}).mappings().all()
        return JsonResponse(
            {"runs": [dict(r) for r in runs]},
            json_dumps_params={"default": str},
        )
    except Exception as exc:
        return JsonResponse({"error": str(exc)}, status=500)


@_admin_required_json
def etl_logs(request, pk: int):
    """List this datasource's per-run log files, newest first.

    Joined against the last 20 etl_run rows by log_path so the GUI can
    show status/run_id alongside each file without a second round trip.
    """
    try:
        ds = _load_datasource(pk)
        slug = _slugify_ds(ds["name"])
        d = _log_dir()
        prefix = f"etl-{slug}-"
        files = []
        try:
            for name in os.listdir(d):
                if name.startswith(prefix) and name.endswith(".log"):
                    path = os.path.join(d, name)
                    try:
                        st = os.stat(path)
                    except OSError:
                        continue
                    files.append({"name": name, "size": st.st_size,
                                 "mtime": st.st_mtime})
        except OSError:
            pass  # log dir not present yet — no runs spawned since deploy
        files.sort(key=lambda f: f["mtime"], reverse=True)

        with _meta_engine().begin() as conn:
            runs = conn.execute(text(
                "SELECT id, status, log_path FROM starcubes_meta.etl_run "
                " WHERE datasource_id = :id AND log_path IS NOT NULL "
                " ORDER BY id DESC LIMIT 20"
            ), {"id": pk}).mappings().all()
        by_path = {r["log_path"]: r for r in runs}
        for f in files:
            r = by_path.get(os.path.join(d, f["name"]))
            f["run_id"] = r["id"] if r else None
            f["run_status"] = r["status"] if r else None

        return JsonResponse({"ok": True, "files": files})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=500)


_LOG_FILE_RE = re.compile(r"^etl-[A-Za-z0-9_.-]+\.log$")
_LOG_TAIL_MAX_BYTES = 256 * 1024


@_admin_required_json
def etl_log_tail(request, pk: int):
    """Incremental tail of one of this datasource's log files.

    ?file=<basename>&offset=<int> -> the file's bytes from `offset`
    onward (capped at 256 KB — seeks forward if the caller is further
    behind than that). Without `file`, defaults to the newest log this
    datasource owns, falling back to the most recent etl_run row's
    log_path.
    """
    try:
        ds = _load_datasource(pk)
        slug = _slugify_ds(ds["name"])
        d = _log_dir()
        prefix = f"etl-{slug}-"

        name = request.GET.get("file")
        if name:
            # Must be a plain log filename belonging to this datasource —
            # this is the one thing standing between a public JSON
            # endpoint and arbitrary path reads.
            if not _LOG_FILE_RE.match(name) or not name.startswith(prefix):
                return JsonResponse({"ok": False, "error": "invalid file"}, status=400)
            path = os.path.realpath(os.path.join(d, name))
            if not path.startswith(os.path.realpath(d) + os.sep):
                return JsonResponse({"ok": False, "error": "invalid file"}, status=400)
        else:
            candidates = []
            try:
                for fname in os.listdir(d):
                    if fname.startswith(prefix) and fname.endswith(".log"):
                        candidates.append(fname)
            except OSError:
                candidates = []
            if candidates:
                candidates.sort(reverse=True)  # timestamp is in the name
                name = candidates[0]
                path = os.path.join(d, name)
            else:
                with _meta_engine().begin() as conn:
                    row = conn.execute(text(
                        "SELECT log_path FROM starcubes_meta.etl_run "
                        " WHERE datasource_id = :id AND log_path IS NOT NULL "
                        " ORDER BY id DESC LIMIT 1"
                    ), {"id": pk}).scalar()
                if not row:
                    return JsonResponse({"ok": False, "error": "no logs yet"})
                path = row
                name = os.path.basename(path)

        try:
            size = os.path.getsize(path)
        except OSError:
            return JsonResponse({"ok": False, "error": "log file not found"}, status=404)

        try:
            offset = int(request.GET.get("offset", "0"))
        except ValueError:
            offset = 0
        truncated = False
        if offset > size:
            # _bound_log_size() in core/etl.py truncates the file in
            # place once it exceeds STARCUBES_LOG_MAX_BYTES.
            offset = 0
            truncated = True
        if size - offset > _LOG_TAIL_MAX_BYTES:
            offset = size - _LOG_TAIL_MAX_BYTES

        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read(_LOG_TAIL_MAX_BYTES)

        return JsonResponse({
            "ok": True, "name": name, "size": size,
            "offset": offset + len(data),
            "text": data.decode("utf-8", errors="replace"),
            "truncated": truncated,
        })
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=500)


@require_POST
@_admin_required_json
def etl_kill(request, pk: int):
    """Kill the currently-running single-shot ETL run for this datasource.

    SIGTERM lets the parent's finally block reap workers and write the
    etl_run row itself (see EtlAborted in core/etl.py + etl_run.py). A
    second click with force=1 escalates to SIGKILL on the whole process
    group — the parent can no longer write its own row, so this view
    finishes it instead.
    """
    try:
        with _meta_engine().begin() as conn:
            forever_active = conn.execute(text(
                "SELECT 1 FROM starcubes_meta.etl_forever "
                " WHERE datasource_id = :id AND status IN ('running','stopping')"
            ), {"id": pk}).scalar()
            if forever_active:
                return JsonResponse(
                    {"ok": False, "error": "Forever ETL is active — use Stop Forever"},
                    status=409)

            row = conn.execute(text(
                "SELECT id, pid FROM starcubes_meta.etl_run "
                " WHERE datasource_id = :id AND status = 'running' "
                " ORDER BY id DESC LIMIT 1"
            ), {"id": pk}).mappings().one_or_none()
            if not row:
                return JsonResponse({"ok": False, "error": "No ETL running"}, status=404)

            pid = row["pid"]
            force = request.POST.get("force") == "1"

            if not pid or not _pid_alive(pid):
                conn.execute(text(
                    "UPDATE starcubes_meta.etl_run "
                    "   SET status = 'failed', "
                    "       error = 'killed by user (process gone)', "
                    "       finished_at = now(), "
                    "       runtime_ms = extract(epoch from now() - started_at) * 1000, "
                    "       progress_rows = NULL, progress_at = NULL "
                    " WHERE id = :id"
                ), {"id": row["id"]})
                return JsonResponse({"ok": True, "signal": None, "pid": pid})

        if force:
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            with _meta_engine().begin() as conn:
                conn.execute(text(
                    "UPDATE starcubes_meta.etl_run "
                    "   SET status = 'failed', error = 'killed by user (force)', "
                    "       finished_at = now(), "
                    "       runtime_ms = extract(epoch from now() - started_at) * 1000, "
                    "       progress_rows = NULL, progress_at = NULL "
                    " WHERE id = :id"
                ), {"id": row["id"]})
            return JsonResponse({"ok": True, "signal": "KILL", "pid": pid})

        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            return JsonResponse({"ok": False, "error": "Permission denied"}, status=500)
        return JsonResponse({"ok": True, "signal": "TERM", "pid": pid})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


@require_POST
@_admin_required_json
def etl_forever_start(request, pk: int):
    try:
        interval = int(request.POST.get("interval", 60))
        if interval < 1:
            return JsonResponse({"ok": False, "error": "interval must be >= 1 second"},
                                status=400)

        # Check + insert in one transaction so two concurrent requests cannot
        # both pass the guard and spawn duplicate forever processes.
        with _meta_engine().begin() as conn:
            # Mark stale forever sessions as failed
            conn.execute(text(
                "UPDATE starcubes_meta.etl_forever "
                " SET status = 'failed', pid = NULL, "
                "     stopped_at = now(), "
                "     error = 'stale: process did not update status' "
                " WHERE datasource_id = :id AND status IN ('running','stopping') "
                "   AND started_at < now() - interval '1 hour'"
            ), {"id": pk})

            active = conn.execute(text(
                "SELECT id, pid FROM starcubes_meta.etl_forever "
                " WHERE datasource_id = :id AND status IN ('running','stopping')"
            ), {"id": pk}).mappings().one_or_none()

            if active:
                pid = active["pid"]
                if pid and _pid_alive(pid):
                    return JsonResponse(
                        {"ok": False, "error": "Forever ETL already running"},
                        status=409)
                conn.execute(text(
                    "UPDATE starcubes_meta.etl_forever "
                    " SET status = 'failed', pid = NULL, "
                    "     stopped_at = now(), error = 'stale process' "
                    " WHERE id = :id"
                ), {"id": active["id"]})

            # Create the row *now* so the unique index prevents a second
            # request from inserting a competing row before the subprocess
            # has even started.
            forever_id = conn.execute(text(
                "INSERT INTO starcubes_meta.etl_forever "
                "  (datasource_id, interval_s, status) "
                " VALUES (:ds, :interval, 'running') "
                " RETURNING id"
            ), {"ds": pk, "interval": interval}).scalar()

        ds = _load_datasource(pk)
        env = os.environ.copy()
        cmd = [_PYTHON_EXE, "manage.py", "etl_run",
               "--name", ds["name"], "--forever", str(interval),
               "--forever-id", str(forever_id)]
        try:
            _spawn_etl_subprocess(cmd, env, datasource_name=ds["name"])
        except OSError:
            # Popen failed — mark the row so the status endpoint reports it.
            with _meta_engine().begin() as conn:
                conn.execute(text(
                    "UPDATE starcubes_meta.etl_forever "
                    " SET status = 'failed', pid = NULL, "
                    "     stopped_at = now(), "
                    "     error = 'subprocess failed to start' "
                    " WHERE id = :id"
                ), {"id": forever_id})
            raise

        return JsonResponse(
            {"ok": True, "status": "starting", "interval": interval,
             "forever_id": forever_id})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


@require_POST
@_admin_required_json
def etl_forever_stop(request, pk: int):
    try:
        with _meta_engine().begin() as conn:
            row = conn.execute(text(
                "SELECT id, pid FROM starcubes_meta.etl_forever "
                " WHERE datasource_id = :id AND status IN ('running','stopping')"
            ), {"id": pk}).mappings().one_or_none()

        if not row:
            return JsonResponse({"ok": False, "error": "No forever ETL running"},
                                status=404)

        with _meta_engine().begin() as conn:
            conn.execute(text(
                "UPDATE starcubes_meta.etl_forever "
                " SET stop_requested = true, status = 'stopping' "
                " WHERE id = :id"
            ), {"id": row["id"]})

        killed = False
        pid = row["pid"]
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
                killed = True
            except ProcessLookupError:
                killed = False
            except PermissionError:
                killed = True

        return JsonResponse({"ok": True, "killed": killed, "pid": pid})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


@_admin_required_json
def etl_forever_status(request, pk: int):
    try:
        with _meta_engine().begin() as conn:
            row = conn.execute(text(
                "SELECT id, pid, interval_s, started_at, stopped_at, "
                "       status, stop_requested, error "
                "  FROM starcubes_meta.etl_forever "
                " WHERE datasource_id = :id "
                " ORDER BY id DESC LIMIT 1"
            ), {"id": pk}).mappings().one_or_none()

            latest_run = conn.execute(text(
                "SELECT id, status, started_at, finished_at, progress_at, "
                "       rows, runtime_ms, error, "
                "       rows_per_s, extract_rows_per_s, transform_rows_per_s, "
                "       load_rows_per_s "
                "  FROM starcubes_meta.etl_run "
                " WHERE datasource_id = :id "
                " ORDER BY id DESC LIMIT 1"
            ), {"id": pk}).mappings().one_or_none()

        if not row:
            return JsonResponse({"active": False})

        result = dict(row)
        # Verify PID liveness
        if result["status"] in ("running", "stopping") and result["pid"]:
            if not _pid_alive(result["pid"]):
                with _meta_engine().begin() as conn:
                    conn.execute(text(
                        "UPDATE starcubes_meta.etl_forever "
                        " SET status = 'failed', pid = NULL, "
                        "     stopped_at = now(), "
                        "     error = 'process died unexpectedly' "
                        " WHERE id = :id"
                    ), {"id": result["id"]})
                result["status"] = "failed"
                result["pid"] = None
        # pid=NULL with status running: subprocess hasn't stamped its PID yet.
        # If started more than 30 s ago the process is stuck or crashed.
        if (result["status"] in ("running", "stopping")
                and not result["pid"]
                and result.get("started_at")):
            age = datetime.now(timezone.utc) - result["started_at"]
            if age > timedelta(seconds=30):
                with _meta_engine().begin() as conn:
                    conn.execute(text(
                        "UPDATE starcubes_meta.etl_forever "
                        " SET status = 'failed', pid = NULL, "
                        "     stopped_at = now(), "
                        "     error = 'subprocess never started (pid NULL)' "
                        " WHERE id = :id"
                    ), {"id": result["id"]})
                result["status"] = "failed"

        result["active"] = result["status"] in ("running", "stopping")
        if latest_run:
            result["latest_run"] = dict(latest_run)
            # Stale detection: if forever is active and either (a) the
            # latest completed run finished more than 3× the loop interval
            # ago, or (b) the latest run is still 'running' but its
            # progress_at (which core/etl.py's progress writer only
            # advances when rows actually move — see the heartbeat) is
            # more than 3× the interval old, something is wrong. Case (b)
            # is what a wedged load worker looks like: without it, a
            # hung run's finished_at stays NULL forever and this check
            # never fired, so the strip just said "running" indefinitely.
            if result["active"]:
                threshold = timedelta(seconds=(result.get("interval_s") or 60) * 3)
                reference = None
                if latest_run["status"] == "running":
                    reference = latest_run.get("progress_at") or latest_run.get("started_at")
                elif latest_run.get("finished_at"):
                    reference = latest_run["finished_at"]
                if reference is not None:
                    if reference.tzinfo is None:
                        reference = reference.replace(tzinfo=timezone.utc)
                    age = datetime.now(timezone.utc) - reference
                    if age > threshold:
                        result["stale"] = True
                        result["stale_seconds"] = int(age.total_seconds())

        return JsonResponse(result, json_dumps_params={"default": str})
    except Exception as exc:
        return JsonResponse({"error": str(exc)}, status=500)


def _load_source(pk: int):
    """Load a Source object for ETL command views."""
    ds = _load_datasource(pk)
    dsn = os.environ.get("STARCUBES_POSTGRES_DSN", settings.STARCUBES_POSTGRES_DSN)
    meta = _get_cached_engine(dsn)
    return Source.load(meta, dest_dsn=dsn, name_or_id=ds["id"]), meta


@require_POST
@_admin_required_json
def etl_delete_from(request, pk: int):
    try:
        from_time = _parse_dt(request.POST["from_time"])
        source, meta = _load_source(pk)
        n = delete_fact_from(source, source.dest_engine(), from_time)
        bump_cache_epoch(meta, source.id)
        return JsonResponse({"ok": True, "deleted": n})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


@require_POST
@_admin_required_json
def etl_truncate_last(request, pk: int):
    try:
        source, meta = _load_source(pk)
        name = truncate_last_partition(source, source.dest_engine())
        bump_cache_epoch(meta, source.id)
        return JsonResponse({"ok": True, "partition": name})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


@require_POST
@_admin_required_json
def etl_truncate_partition(request, pk: int):
    try:
        partition = request.POST.get("partition", "").strip()
        if not partition:
            return JsonResponse({"ok": False, "error": "partition name required"})
        source, meta = _load_source(pk)
        truncate_partition(source, source.dest_engine(), partition)
        bump_cache_epoch(meta, source.id)
        return JsonResponse({"ok": True, "partition": partition})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


@require_POST
@_admin_required_json
def etl_drop_partition(request, pk: int):
    try:
        partition = request.POST.get("partition", "").strip()
        if not partition:
            return JsonResponse({"ok": False, "error": "partition name required"})
        source, meta = _load_source(pk)
        drop_partition(source, source.dest_engine(), partition)
        bump_cache_epoch(meta, source.id)
        return JsonResponse({"ok": True, "partition": partition})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


@require_POST
@_admin_required_json
def etl_vacuum_analyze(request, pk: int):
    try:
        source, _ = _load_source(pk)
        vacuum_analyze_fact(source, source.dest_engine())
        return JsonResponse({"ok": True})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


@require_POST
@_admin_required_json
def etl_set_highwatermark(request, pk: int):
    try:
        source, meta = _load_source(pk)
        hwm = set_highwatermark(source, source.dest_engine(), meta)
        bump_cache_epoch(meta, source.id)
        return JsonResponse({"ok": True, "highwatermark": str(hwm)})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


@require_POST
@_admin_required_json
def etl_clear_history(request, pk: int):
    try:
        source, meta = _load_source(pk)
        n = clear_etl_history(source, meta)
        bump_cache_epoch(meta, source.id)
        return JsonResponse({"ok": True, "cleared": n})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


@require_POST
@_admin_required_json
def etl_create_partitions(request, pk: int):
    try:
        n = int(request.POST.get("n", 1))
        source, _ = _load_source(pk)
        parts = create_future_partitions(source, source.dest_engine(), n)
        return JsonResponse({"ok": True, "partitions": parts})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


@require_POST
@_admin_required_json
def etl_audit(request, pk: int):
    """Compare source vs fact row counts per time bucket over a window.

    The ETL's overlap re-fetch (`datasource.late_arrival_overlap_s`) covers
    a bounded amount of out-of-order arrival; this finds whatever exceeded
    it — and is also how an operator measures what the overlap should be.
    """
    try:
        from_time = _parse_dt(request.POST["from_time"])
        to_time = _parse_dt(request.POST["to_time"])
        buckets = int(request.POST.get("buckets", 24))
        source, _ = _load_source(pk)
        rows = audit_window(source, source.dest_engine(),
                            start=from_time, end=to_time, buckets=buckets)
        total_missing = sum(r["missing"] or 0 for r in rows)
        total_surplus = sum(r["surplus"] or 0 for r in rows)
        unreadable = sum(1 for r in rows if r["source_rows"] is None)
        return JsonResponse({"ok": True, "buckets": rows,
                             "total_missing": total_missing,
                             "total_surplus": total_surplus,
                             "unreadable_buckets": unreadable})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


@require_POST
@_admin_required_json
def etl_audit_ids(request, pk: int):
    """Name the exact rows that differ between source and fact in a window.

    The count-based audit is the cheap sweep; this is the precise follow-up.
    It also sees what counting cannot: a window that lost N rows and had N
    deleted upstream nets to zero and reads as clean, and duplicate PKs in
    fact are invisible to counting entirely.
    """
    try:
        from_time = _parse_dt(request.POST["from_time"])
        to_time = _parse_dt(request.POST["to_time"])
        sample = int(request.POST.get("sample", 200))
        source, _ = _load_source(pk)
        result = audit_ids(source, source.dest_engine(),
                           start=from_time, end=to_time, sample=sample)
        return JsonResponse({"ok": True, **result})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


@require_POST
@_admin_required_json
def etl_audit_id_details(request, pk: int):
    """Source watermark + responsible ETL run for ids the audit already named.

    A separate endpoint rather than more fields on `etl_audit_ids`: the set
    difference has to stay cheap enough to run over a wide window, and this
    hits the source once more per id batch. Identify first, then look up.
    """
    try:
        from_time = _parse_dt(request.POST["from_time"])
        to_time = _parse_dt(request.POST["to_time"])
        ids = [i for i in request.POST.getlist("ids") if i != ""]
        if not ids:
            # The browser may send one comma-joined field instead of a list.
            ids = [i.strip() for i in request.POST.get("ids_csv", "").split(",")
                   if i.strip()]
        source, _ = _load_source(pk)
        result = audit_id_details(source, _meta_engine(), ids=ids,
                                  start=from_time, end=to_time)
        return JsonResponse({"ok": True, **result})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


@require_POST
@_admin_required_json
def etl_backfill(request, pk: int):
    """Re-load one closed window, inserting only rows missing from fact.

    Spawns the same `manage.py etl_run` subprocess as a normal run (with
    `--backfill`), so the per-run log file, ETL History and the live
    monitor all work unchanged. The run is recorded with kind='backfill'
    and therefore cannot move the high watermark backwards.
    """
    try:
        start = request.POST["from_time"]
        end = request.POST["to_time"]
        # Validate before spawning — a bad timestamp should be a message
        # here, not a failed run row the user has to go read the log for.
        if _parse_dt(end) <= _parse_dt(start):
            return JsonResponse(
                {"ok": False, "error": "backfill end must be after start"})

        busy = _etl_busy_response(pk)
        if busy is not None:
            return busy

        ds = _load_datasource(pk)
        env = os.environ.copy()
        cmd = [_PYTHON_EXE, "manage.py", "etl_run", "--name", ds["name"],
               "--backfill", "--start", start, "--end", end]
        _proc, log_path = _spawn_etl_subprocess(cmd, env,
                                                datasource_name=ds["name"])
        return JsonResponse({"ok": True, "status": "started",
                             "log_path": log_path})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


@require_POST
@_admin_required_json
def etl_refresh_counts(request, pk: int):
    try:
        source, _ = _load_source(pk)
        refresh_dim_counts(source, source.dest_engine())
        return JsonResponse({"ok": True})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


@require_POST
@_admin_required_json
def cache_clear(request, pk: int):
    """Delete query_cache rows for this datasource.

    Optional POST fields ``group_by`` and ``measure`` restrict the
    delete to a single breakdown — finer-grained than the implicit
    epoch-bump invalidation, which orphans every cached plot for the
    datasource regardless of breakdown.
    """
    try:
        group_by = request.POST.get("group_by") or None
        measure = request.POST.get("measure") or None
        n = clear_cache(_meta_engine(), datasource_id=pk,
                        group_by=group_by, measure=measure)
        return JsonResponse({"ok": True, "deleted": n})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


# ---------------------------------------------------------------------------

@require_POST
@_admin_required_json
def fc_add(request, pk: int):
    try:
        p = request.POST
        with _meta_engine().begin() as conn:
            row = conn.execute(text("""
                INSERT INTO starcubes_meta.fact_column
                    (datasource_id, name, pg_type, role, ordinal, source_expr)
                VALUES (:ds, :name, :pg_type, :role, :ordinal, NULLIF(:source_expr, ''))
                RETURNING *
            """), {
                "ds":          pk,
                "name":        p["name"],
                "pg_type":     p["pg_type"],
                "role":        p["role"],
                "ordinal":     int(p["ordinal"]),
                "source_expr": p.get("source_expr", ""),
            }).mappings().one()
        return JsonResponse({"ok": True, "row": dict(row)}, json_dumps_params={"default": str})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


@require_POST
@_admin_required_json
def fc_edit(request, pk: int, fc_id: int):
    try:
        p = request.POST
        with _meta_engine().begin() as conn:
            row = conn.execute(text("""
                UPDATE starcubes_meta.fact_column SET
                    name        = :name,
                    pg_type     = :pg_type,
                    role        = :role,
                    ordinal     = :ordinal,
                    source_expr = NULLIF(:source_expr, '')
                WHERE id = :id AND datasource_id = :ds
                RETURNING *
            """), {
                "id":          fc_id,
                "ds":          pk,
                "name":        p["name"],
                "pg_type":     p["pg_type"],
                "role":        p["role"],
                "ordinal":     int(p["ordinal"]),
                "source_expr": p.get("source_expr", ""),
            }).mappings().one_or_none()
        if row is None:
            return JsonResponse({"ok": False, "error": "not found"}, status=404)
        return JsonResponse({"ok": True, "row": dict(row)}, json_dumps_params={"default": str})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


@require_POST
@_admin_required_json
def fc_delete(request, pk: int, fc_id: int):
    try:
        with _meta_engine().begin() as conn:
            conn.execute(
                text("DELETE FROM starcubes_meta.fact_column WHERE id = :id AND datasource_id = :ds"),
                {"id": fc_id, "ds": pk},
            )
        return JsonResponse({"ok": True})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


# ---------------------------------------------------------------------------
# Dimension CRUD
# ---------------------------------------------------------------------------

@require_POST
@_admin_required_json
def dim_add(request, pk: int):
    try:
        p = request.POST
        inline_values = p.get("inline_values", "").strip()
        # Mutual exclusivity: inline_values clears source fields
        if inline_values:
            source_table = source_key = source_name = source_desc = None
        else:
            source_table = p.get("source_table", "") or None
            source_key = p.get("source_key", "") or None
            source_name = p.get("source_name", "") or None
            source_desc = p.get("source_description", "") or None
            inline_values = None
        with _meta_engine().begin() as conn:
            row = conn.execute(text("""
                INSERT INTO starcubes_meta.dimension
                    (datasource_id, fact_column, dim_table,
                     source_table, source_key, source_name, source_description,
                     is_primary, inline_values, filter_group)
                VALUES
                    (:ds, :fact_column, :dim_table,
                     :source_table, :source_key, :source_name, :source_desc,
                     :is_primary, :inline_values, :filter_group)
                RETURNING *
            """), {
                "ds":                 pk,
                "fact_column":        p["fact_column"],
                "dim_table":          p["dim_table"],
                "source_table":       source_table,
                "source_key":         source_key,
                "source_name":        source_name,
                "source_desc":        source_desc,
                "is_primary":         p.get("is_primary") == "on",
                "inline_values":      inline_values,
                "filter_group":       p.get("filter_group", "").strip() or None,
            }).mappings().one()
        return JsonResponse({"ok": True, "row": dict(row)}, json_dumps_params={"default": str})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


@require_POST
@_admin_required_json
def dim_edit(request, pk: int, dim_id: int):
    try:
        p = request.POST
        inline_values = p.get("inline_values", "").strip()
        # Mutual exclusivity: inline_values clears source fields, and vice versa
        if inline_values:
            source_table = source_key = source_name = source_desc = None
        else:
            source_table = p.get("source_table", "") or None
            source_key = p.get("source_key", "") or None
            source_name = p.get("source_name", "") or None
            source_desc = p.get("source_description", "") or None
            inline_values = None
        with _meta_engine().begin() as conn:
            row = conn.execute(text("""
                UPDATE starcubes_meta.dimension SET
                    fact_column        = :fact_column,
                    dim_table          = :dim_table,
                    source_table       = :source_table,
                    source_key         = :source_key,
                    source_name        = :source_name,
                    source_description = :source_desc,
                    is_primary         = :is_primary,
                    inline_values      = :inline_values,
                    filter_group       = :filter_group
                WHERE id = :id AND datasource_id = :ds
                RETURNING *
            """), {
                "id":                 dim_id,
                "ds":                 pk,
                "fact_column":        p["fact_column"],
                "dim_table":          p["dim_table"],
                "source_table":       source_table,
                "source_key":         source_key,
                "source_name":        source_name,
                "source_desc":        source_desc,
                "is_primary":         p.get("is_primary") == "on",
                "inline_values":      inline_values,
                "filter_group":       p.get("filter_group", "").strip() or None,
            }).mappings().one_or_none()
        if row is None:
            return JsonResponse({"ok": False, "error": "not found"}, status=404)
        return JsonResponse({"ok": True, "row": dict(row)}, json_dumps_params={"default": str})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


@require_POST
@_admin_required_json
def dim_delete(request, pk: int, dim_id: int):
    try:
        with _meta_engine().begin() as conn:
            conn.execute(
                text("DELETE FROM starcubes_meta.dimension WHERE id = :id AND datasource_id = :ds"),
                {"id": dim_id, "ds": pk},
            )
        return JsonResponse({"ok": True})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


# ---------------------------------------------------------------------------
# Index CRUD
# ---------------------------------------------------------------------------

@require_POST
@_admin_required_json
def idx_add(request, pk: int):
    try:
        p = request.POST
        with _meta_engine().begin() as conn:
            row = conn.execute(text("""
                INSERT INTO starcubes_meta.fact_index (datasource_id, column_name, method)
                VALUES (:ds, :column_name, :method)
                RETURNING *
            """), {
                "ds":          pk,
                "column_name": p["column_name"],
                "method":      p["method"],
            }).mappings().one()
        return JsonResponse({"ok": True, "row": dict(row)}, json_dumps_params={"default": str})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


@require_POST
@_admin_required_json
def idx_edit(request, pk: int, idx_id: int):
    try:
        p = request.POST
        with _meta_engine().begin() as conn:
            row = conn.execute(text("""
                UPDATE starcubes_meta.fact_index SET
                    column_name = :column_name,
                    method      = :method
                WHERE id = :id AND datasource_id = :ds
                RETURNING *
            """), {
                "id":          idx_id,
                "ds":          pk,
                "column_name": p["column_name"],
                "method":      p["method"],
            }).mappings().one_or_none()
        if row is None:
            return JsonResponse({"ok": False, "error": "not found"}, status=404)
        return JsonResponse({"ok": True, "row": dict(row)}, json_dumps_params={"default": str})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


@require_POST
@_admin_required_json
def idx_delete(request, pk: int, idx_id: int):
    try:
        with _meta_engine().begin() as conn:
            conn.execute(
                text("DELETE FROM starcubes_meta.fact_index WHERE id = :id AND datasource_id = :ds"),
                {"id": idx_id, "ds": pk},
            )
        return JsonResponse({"ok": True})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


# ---------------------------------------------------------------------------
# Measures CRUD (JSON)
# ---------------------------------------------------------------------------

def _measure_columns(conn, pk: int) -> set[str]:
    """Fact-column names eligible for sum/avg/min/max aggregation."""
    return set(conn.execute(text(
        "SELECT name FROM starcubes_meta.fact_column "
        " WHERE datasource_id = :ds AND role = 'measure'"
    ), {"ds": pk}).scalars().all())


def _validate_agg(agg: str, measure_cols: set[str]) -> str:
    """Validate/normalise a measure agg expression.

    Accepts the literal ``count(*)`` or ``func(col)`` where func is one of
    sum/avg/min/max/quantile and col is a fact column with role='measure'.
    ``quantile(col)`` is only meaningful with one of the ladder graph styles
    (quantile-bands, quantile-steps, quantile-steps-open, error-bars,
    candlestick); the pairing is enforced in `_resolve_measure_fields`.
    """
    agg = (agg or "").strip()
    if agg == "count(*)":
        return agg
    m = re.match(r"(\w+)\((\w+)\)$", agg)
    if not m:
        raise ValueError(f"agg must be 'count(*)' or func(col), got {agg!r}")
    fn, col = m.group(1), m.group(2)
    if fn not in ("sum", "avg", "min", "max", "quantile"):
        raise ValueError(
            f"unknown function {fn!r}; choose sum/avg/min/max/quantile")
    if col not in measure_cols:
        raise ValueError(
            f"{col!r} is not a measure column; have {sorted(measure_cols)}")
    return agg


def _validate_percentiles(s: str) -> str | None:
    """Validate/normalise a percentile ladder string.

    Accepts a comma-separated list of integers in (0, 100); returns them
    sorted ascending and de-duplicated as a comma string, or None when empty.
    """
    s = (s or "").strip()
    if not s:
        return None
    try:
        vals = sorted({int(tok) for tok in s.split(",") if tok.strip()})
    except ValueError:
        raise ValueError(f"percentiles must be comma-separated integers, got {s!r}")
    if not vals or any(not (0 < v < 100) for v in vals):
        raise ValueError(f"percentiles must be integers in (0, 100), got {s!r}")
    return ",".join(str(v) for v in vals)


def _resolve_measure_fields(p, conn, pk: int) -> tuple[str, str, str | None]:
    """Validate (graph_style, agg, percentiles) for a measure create/edit.

    Ladder styles (quantile-bands, quantile-steps, quantile-steps-open,
    error-bars, candlestick) require a ``quantile(col)`` agg and carry a
    percentile ladder (defaulting to 5,25,50,75,95); other styles store NULL
    percentiles.
    """
    graph_style = p.get("graph_style", "multibar")
    if graph_style not in GRAPH_STYLES:
        raise ValueError(f"graph_style must be one of {GRAPH_STYLES}")
    agg = _validate_agg(p.get("agg", ""), _measure_columns(conn, pk))
    is_quantile = agg.startswith("quantile(")
    is_ladder = graph_style in (
        "quantile-bands", "quantile-steps", "quantile-steps-open",
        "error-bars", "candlestick")
    if is_ladder:
        if not is_quantile:
            raise ValueError(
                f"{graph_style} measures need a quantile(col) agg")
        percentiles = _validate_percentiles(p.get("percentiles", "")) or "5,25,50,75,95"
    else:
        if is_quantile:
            raise ValueError(
                f"quantile(col) agg requires a ladder graph_style "
                f"(quantile-bands/quantile-steps/quantile-steps-open/"
                f"error-bars/candlestick), got {graph_style!r}")
        percentiles = None
    return graph_style, agg, percentiles


def _clear_other_defaults(conn, pk: int, keep_id: int | None) -> None:
    """Unset is_default on all measures of this datasource except keep_id.

    Keeps the partial unique index ux_measure_default satisfiable when a row
    is (re)flagged as default.
    """
    conn.execute(text(
        "UPDATE starcubes_meta.measure SET is_default = false "
        " WHERE datasource_id = :ds AND is_default "
        "   AND (:keep IS NULL OR id <> :keep)"
    ), {"ds": pk, "keep": keep_id})


@require_POST
@_login_required_json
def measure_add(request, pk: int):
    try:
        p = request.POST
        is_default = p.get("is_default") in ("on", "true", "1")
        with _meta_engine().begin() as conn:
            graph_style, agg, percentiles = _resolve_measure_fields(p, conn, pk)
            if is_default:
                _clear_other_defaults(conn, pk, None)
            row = conn.execute(text("""
                INSERT INTO starcubes_meta.measure
                    (datasource_id, name, agg, graph_style, is_default, ordinal,
                     percentiles)
                VALUES (:ds, :name, :agg, :gs, :def, :ord, :pct)
                RETURNING *
            """), {
                "ds": pk, "name": p["name"].strip(), "agg": agg, "gs": graph_style,
                "def": is_default, "ord": int(p.get("ordinal") or 0),
                "pct": percentiles,
            }).mappings().one()
        return JsonResponse({"ok": True, "row": dict(row)}, json_dumps_params={"default": str})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


@require_POST
@_login_required_json
def measure_edit(request, pk: int, measure_id: int):
    try:
        p = request.POST
        is_default = p.get("is_default") in ("on", "true", "1")
        with _meta_engine().begin() as conn:
            graph_style, agg, percentiles = _resolve_measure_fields(p, conn, pk)
            if is_default:
                _clear_other_defaults(conn, pk, measure_id)
            row = conn.execute(text("""
                UPDATE starcubes_meta.measure SET
                    name = :name, agg = :agg, graph_style = :gs,
                    is_default = :def, ordinal = :ord, percentiles = :pct
                WHERE id = :id AND datasource_id = :ds
                RETURNING *
            """), {
                "id": measure_id, "ds": pk, "name": p["name"].strip(), "agg": agg,
                "gs": graph_style, "def": is_default, "ord": int(p.get("ordinal") or 0),
                "pct": percentiles,
            }).mappings().one_or_none()
        if row is None:
            return JsonResponse({"ok": False, "error": "not found"}, status=404)
        return JsonResponse({"ok": True, "row": dict(row)}, json_dumps_params={"default": str})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


@require_POST
@_login_required_json
def measure_delete(request, pk: int, measure_id: int):
    try:
        with _meta_engine().begin() as conn:
            conn.execute(
                text("DELETE FROM starcubes_meta.measure WHERE id = :id AND datasource_id = :ds"),
                {"id": measure_id, "ds": pk},
            )
        return JsonResponse({"ok": True})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


# ---------------------------------------------------------------------------
# Alarms (JSON, login-only)
#
# Per-datasource threshold rules. The CRUD endpoints accept and return
# starcubes_meta.alarm rows verbatim. The context is the chart's whole
# filter expression (flat filter rows as a JSON string in the `context`
# field → alarm.context_filter jsonb); grouped tokens are rejected at save
# time. Every create/update writes an alarm_history snapshot row so
# changes are traceable to a user and a timestamp — including comment
# changes, which is why there is no separate comment table. The evaluator
# is `core/alarm.py`, driven by the alarm_run management command; the only
# wired notify_channel is `log` (the alarm_firing episode + the loop's
# log file).
#
# No bump_cache_epoch here on purpose: plot cache keys are built from chart
# params only (core/cache.py:make_cache_key) — alarm rows feed none of
# them, unlike the six historical-mutation endpoints above.
# ---------------------------------------------------------------------------

# Comparison operators accepted by starcubes_meta.alarm.cond_op's CHECK
# constraint — the DB enforces it, but validating here gives a friendlier
# 400 than a Postgres exception bubbling up through the generic except.
_ALARM_OPS = {">", "<", "=", ">=", "<="}

# Notification channels — `log` is wired (the alarm_firing episode plus
# the evaluator's log). `email`, `slack`, `pagerduty` are reserved values
# accepted by the form, but have no transport yet — see docs/backlog.md.
_ALARM_CHANNELS = {"email", "slack", "pagerduty", "log"}

# Filter ops an alarm context row may carry — the same six the chart
# serializes (core.query._VALID_OPS).
_ALARM_CONTEXT_OPS = {"in", "not_in", "starts_with", "starts_not_with",
                      "contains", "not_contains"}


def _parse_alarm_fields(request) -> tuple[dict, dict]:
    """Validate POST fields for an alarm and return (values, errors).

    Both alarms_list (which builds an HTML row) and the add/edit endpoints
    call this so the validation lives in one place. ``errors`` keys are
    field names; values are short user-facing messages. ``values`` carries
    the typed-and-clamped payload to feed straight into an INSERT/UPDATE.

    ``context`` arrives as a JSON string (the chart's flat filter rows) —
    FormData-compatible for both callers. Absent on edit means "keep the
    stored context" (the inline editor never sends it — a filter expression
    is not inline-editable); an explicit empty array means a global alarm.
    Semantic validation (col/measure resolution) happens afterwards in
    `_validate_alarm_context`, which needs the Source.
    """
    values: dict = {
        "datasource_id": None,
        "name": "",
        "comment": None,
        "context_rows": None,
        "measure": "count(*)",
        "cond_op": ">",
        "threshold": 0.0,
        "window_s": 300,
        "notify_channels": [],
        "enabled": True,
    }
    errors: dict = {}

    name = (request.POST.get("name") or "").strip()
    if not name:
        errors["name"] = "name is required"
    elif len(name) > 200:
        errors["name"] = "name is too long (max 200 characters)"
    else:
        values["name"] = name

    comment = (request.POST.get("comment") or "").strip()
    if len(comment) > 10000:
        errors["comment"] = "comment is too long (max 10000 characters)"
    else:
        values["comment"] = comment or None

    cond_op = (request.POST.get("cond_op") or ">").strip()
    if cond_op not in _ALARM_OPS:
        errors["cond_op"] = f"cond_op must be one of {sorted(_ALARM_OPS)}"
    else:
        values["cond_op"] = cond_op

    try:
        values["threshold"] = float(request.POST.get("threshold") or 0)
    except (TypeError, ValueError):
        errors["threshold"] = "threshold must be a number"

    try:
        window_s = int(request.POST.get("window_s") or 300)
        if window_s <= 0:
            errors["window_s"] = "window_s must be > 0"
        else:
            values["window_s"] = window_s
    except (TypeError, ValueError):
        errors["window_s"] = "window_s must be an integer"

    # Syntactic measure check — the semantic column check needs the Source
    # and lives in _validate_alarm_context. quantile(col) is rejected here:
    # it is not a scalar a threshold can compare against.
    measure = (request.POST.get("measure") or "count(*)").strip() or "count(*)"
    m = re.match(r"(\w+)\((\w+)\)$", measure)
    if (measure != "count(*)" and not m
            and not re.match(r"^\w+$", measure)):
        errors["measure"] = ("measure must be count(*), a column, or "
                             "func(col) with func in sum/avg/min/max")
    elif m and m.group(1) not in MEASURE_FUNCS:
        errors["measure"] = (f"measure function {m.group(1)!r} is not "
                             f"comparable; choose from count(*), "
                             f"{sorted(MEASURE_FUNCS)} or a bare column")
    else:
        values["measure"] = measure

    if "context" in request.POST:
        raw_ctx = (request.POST.get("context") or "").strip()
        if not raw_ctx:
            values["context_rows"] = []
        else:
            try:
                parsed = _json.loads(raw_ctx)
            except ValueError:
                errors["context"] = "context must be a JSON array of filter rows"
            else:
                if not isinstance(parsed, list) or not all(
                        isinstance(r, dict) and isinstance(r.get("col"), str)
                        and r.get("op") in _ALARM_CONTEXT_OPS
                        and isinstance(r.get("vals"), list)
                        and all(isinstance(v, (str, int)) for v in r["vals"])
                        for r in parsed):
                    errors["context"] = ("context rows must be "
                                         "{col, op, vals[, andOr, indent]} "
                                         "with op one of "
                                         f"{sorted(_ALARM_CONTEXT_OPS)}")
                else:
                    values["context_rows"] = parsed

    raw_channels = request.POST.get("notify_channels", "")
    if isinstance(raw_channels, str):
        channels = [c.strip() for c in raw_channels.split(",") if c.strip()]
    else:
        # List field — defensive in case a caller sends it as an array.
        channels = [str(c).strip() for c in raw_channels if str(c).strip()]
    bad = [c for c in channels if c not in _ALARM_CHANNELS]
    if bad:
        errors["notify_channels"] = (f"unknown channel(s) {bad}; allowed: "
                                     f"{sorted(_ALARM_CHANNELS)}")
    else:
        values["notify_channels"] = channels

    # Checkbox semantics, but only when the field was actually submitted:
    # an absent "enabled" means "not part of this form" (the add form used
    # to omit it entirely), not "disabled". Present-but-empty is the
    # unchecked box that saveRow sends.
    if "enabled" in request.POST:
        values["enabled"] = request.POST.get("enabled") in ("on", "true", "1")

    return values, errors


def _alarm_row_to_dict(row) -> dict:
    """One starcubes_meta.alarm row → JSON-safe dict.

    Kept separate from the SELECTs so list/add/edit can all render the
    same shape (the editor reads it back the same way the list does).
    context_filter arrives already decoded (psycopg2 turns jsonb into a
    Python list). eval_end is the evaluator's runtime HWM marker, not user
    configuration — it is deliberately left out.
    """
    return {
        "id": row.id,
        "datasource_id": row.datasource_id,
        "name": row.name,
        "context_filter": row.context_filter,
        "comment": row.comment,
        "measure": row.measure,
        "cond_op": row.cond_op,
        "threshold": float(row.threshold),
        "window_s": row.window_s,
        "notify_channels": list(row.notify_channels or []),
        "enabled": bool(row.enabled),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _context_labels(pk: int, contexts) -> dict:
    """{filter_column: {source_id: name}} for a set of alarm contexts.

    A context stores dimension ids; the names live in the dim tables and are
    resolved for display only (see core.alarm.resolve_context_labels). Purely
    cosmetic, so every failure degrades to raw ids: the alarm list has to
    render even when the fact database is unreachable or a dim table has not
    been created yet.
    """
    if not any(contexts):
        return {}
    try:
        source = Source.load(_meta_engine(),
                             dest_dsn=settings.STARCUBES_POSTGRES_DSN,
                             name_or_id=pk)
        return resolve_context_labels(source, contexts)
    except Exception:
        return {}


def _validate_alarm_context(pk: int, values: dict):
    """Semantically validate an alarm's context + measure against the Source.

    The stored expression is only as trustworthy as its write-time
    validation: this resolves every filter col through the same rules the
    chart uses (core.query.validate_filter_tree), forbids grouped tokens
    (user-editable group sets would silently change the alarm's meaning),
    and checks the measure column. Returns a 400/404 JsonResponse on
    failure, else None. Callers pass the value of `values["context_rows"]`
    inside — None means "not submitted" (edit keeps the stored context) and
    skips context validation; the measure check always runs.
    """
    try:
        source = Source.load(_meta_engine(),
                             dest_dsn=settings.STARCUBES_POSTGRES_DSN, name_or_id=pk)
    except LookupError:
        return JsonResponse({"ok": False, "error":
            f"datasource {pk} not found"}, status=404)
    try:
        measure_value_expr(source, values["measure"])
        if values["context_rows"] is not None:
            tree = build_filter_tree(values["context_rows"])
            check_no_grouped_filters(tree)
            validate_filter_tree(source, tree)
    except ValueError as exc:
        return JsonResponse({"ok": False, "error": str(exc),
                             "fields": {"context": str(exc)}}, status=400)
    return None


@require_POST
@_login_required_json
def alarms_list(request, pk: int):
    """All alarms for one datasource, as a JSON array.

    Kept on POST for consistency with the other JSON CRUD endpoints — the
    UI uses it as a one-shot fetch rather than a long-poll. 404 when the
    datasource itself does not exist, so a stale tab does not silently
    render an empty list against a deleted source.
    """
    try:
        with _meta_engine().begin() as conn:
            ds_exists = conn.execute(text(
                "SELECT 1 FROM starcubes_meta.datasource WHERE id = :ds"
            ), {"ds": pk}).scalar()
            if not ds_exists:
                return JsonResponse({"ok": False, "error":
                    f"datasource {pk} not found"}, status=404)
            rows = conn.execute(text(
                "SELECT id, datasource_id, name, context_filter, comment, "
                "       measure, cond_op, threshold, window_s, notify_channels, "
                "       enabled, created_at, updated_at "
                "  FROM starcubes_meta.alarm "
                " WHERE datasource_id = :ds "
                " ORDER BY name"
            ), {"ds": pk}).mappings().all()
        alarms = [_alarm_row_to_dict(r) for r in rows]
        # One label map for the whole page — the ids are resolved in a single
        # pass over every context, so the cost does not grow with the list.
        return JsonResponse({"ok": True, "alarms": alarms,
                             "context_labels": _context_labels(
                                 pk, [a["context_filter"] for a in alarms])})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


@require_POST
@_login_required_json
def alarm_add(request, pk: int):
    """Create one alarm for the given datasource.

    The creation and its alarm_history snapshot land in one transaction —
    a row without provenance should not exist. context_rows is a list of
    flat filter rows; an empty list is stored as NULL (global alarm).
    """
    values, errors = _parse_alarm_fields(request)
    if errors:
        return JsonResponse({"ok": False, "error":
            "; ".join(f"{k}: {v}" for k, v in errors.items()),
            "fields": errors}, status=400)
    failed = _validate_alarm_context(pk, values)
    if failed is not None:
        return failed
    ctx_json = (_json.dumps(values["context_rows"])
                if values["context_rows"] else None)
    try:
        with _meta_engine().begin() as conn:
            ds_exists = conn.execute(text(
                "SELECT 1 FROM starcubes_meta.datasource WHERE id = :ds"
            ), {"ds": pk}).scalar()
            if not ds_exists:
                return JsonResponse({"ok": False, "error":
                    f"datasource {pk} not found"}, status=404)
            row = conn.execute(text(
                "INSERT INTO starcubes_meta.alarm "
                "  (datasource_id, name, context_filter, comment, "
                "   measure, cond_op, threshold, window_s, notify_channels, "
                "   enabled) "
                "VALUES (:ds, :name, CAST(:ctx AS jsonb), :cmt, "
                "        :m, :op, :thr, :ws, CAST(:nc AS text[]), :en) "
                "RETURNING id, datasource_id, name, context_filter, comment, "
                "          measure, cond_op, threshold, window_s, notify_channels, "
                "          enabled, created_at, updated_at"
            ), {"ds": pk, "name": values["name"], "ctx": ctx_json,
                "cmt": values["comment"],
                "m": values["measure"], "op": values["cond_op"],
                "thr": values["threshold"], "ws": values["window_s"],
                "nc": list(values["notify_channels"]),
                "en": values["enabled"]}).mappings().one()
            conn.execute(text(
                "INSERT INTO starcubes_meta.alarm_history "
                "  (alarm_id, kind, username, old_row, new_row) "
                "VALUES (:aid, 'created', :user, NULL, CAST(:new AS jsonb))"
            ), {"aid": row.id, "user": request.user.get_username(),
                "new": _json.dumps(_alarm_row_to_dict(row))})
        return JsonResponse({"ok": True, "alarm": _alarm_row_to_dict(row)})
    except sqlalchemy.exc.IntegrityError:
        # UNIQUE (datasource_id, name) — friendly name rather than the raw
        # constraint text.
        return JsonResponse({"ok": False, "error":
            f"an alarm named {values['name']!r} already exists for "
            f"this datasource", "fields": {"name": "name already in use"}},
            status=409)
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


@require_POST
@_login_required_json
def alarm_edit(request, pk: int, alarm_id: int):
    """Update one alarm. Same field set as add — except the context.

    The context (a whole filter expression) is not inline-editable: when
    the `context` field is absent the stored context_filter is kept as-is;
    when present (chart / API callers) it replaces it wholesale. The
    evaluator's eval_end marker is never touched here — edits do not reset
    evaluation progress. Old and new snapshots land in alarm_history in
    the same transaction as the UPDATE.
    """
    values, errors = _parse_alarm_fields(request)
    if errors:
        return JsonResponse({"ok": False, "error":
            "; ".join(f"{k}: {v}" for k, v in errors.items()),
            "fields": errors}, status=400)
    failed = _validate_alarm_context(pk, values)
    if failed is not None:
        return failed
    ctx_json = (_json.dumps(values["context_rows"])
                if values["context_rows"] else None)
    set_context = "context" in request.POST
    try:
        with _meta_engine().begin() as conn:
            old = conn.execute(text(
                "SELECT id, datasource_id, name, context_filter, comment, "
                "       measure, cond_op, threshold, window_s, notify_channels, "
                "       enabled, created_at, updated_at "
                "  FROM starcubes_meta.alarm "
                " WHERE id = :id AND datasource_id = :ds"
            ), {"id": alarm_id, "ds": pk}).mappings().one_or_none()
            if old is None:
                return JsonResponse({"ok": False, "error":
                    f"alarm {alarm_id} not found for datasource {pk}"},
                    status=404)
            if set_context:
                row = conn.execute(text(
                    "UPDATE starcubes_meta.alarm SET "
                    "       name = :name, context_filter = CAST(:ctx AS jsonb), "
                    "       comment = :cmt, measure = :m, cond_op = :op, "
                    "       threshold = :thr, window_s = :ws, "
                    "       notify_channels = CAST(:nc AS text[]), "
                    "       enabled = :en, updated_at = now() "
                    " WHERE id = :id AND datasource_id = :ds "
                    "RETURNING id, datasource_id, name, context_filter, comment, "
                    "          measure, cond_op, threshold, window_s, notify_channels, "
                    "          enabled, created_at, updated_at"
                ), {"ds": pk, "id": alarm_id, "name": values["name"],
                    "ctx": ctx_json, "cmt": values["comment"],
                    "m": values["measure"], "op": values["cond_op"],
                    "thr": values["threshold"], "ws": values["window_s"],
                    "nc": list(values["notify_channels"]),
                    "en": values["enabled"]}).mappings().one()
            else:
                row = conn.execute(text(
                    "UPDATE starcubes_meta.alarm SET "
                    "       name = :name, comment = :cmt, measure = :m, "
                    "       cond_op = :op, threshold = :thr, window_s = :ws, "
                    "       notify_channels = CAST(:nc AS text[]), "
                    "       enabled = :en, updated_at = now() "
                    " WHERE id = :id AND datasource_id = :ds "
                    "RETURNING id, datasource_id, name, context_filter, comment, "
                    "          measure, cond_op, threshold, window_s, notify_channels, "
                    "          enabled, created_at, updated_at"
                ), {"ds": pk, "id": alarm_id, "name": values["name"],
                    "cmt": values["comment"],
                    "m": values["measure"], "op": values["cond_op"],
                    "thr": values["threshold"], "ws": values["window_s"],
                    "nc": list(values["notify_channels"]),
                    "en": values["enabled"]}).mappings().one()
            conn.execute(text(
                "INSERT INTO starcubes_meta.alarm_history "
                "  (alarm_id, kind, username, old_row, new_row) "
                "VALUES (:aid, 'updated', :user, "
                "        CAST(:old AS jsonb), CAST(:new AS jsonb))"
            ), {"aid": alarm_id, "user": request.user.get_username(),
                "old": _json.dumps(_alarm_row_to_dict(old)),
                "new": _json.dumps(_alarm_row_to_dict(row))})
        return JsonResponse({"ok": True, "alarm": _alarm_row_to_dict(row)})
    except sqlalchemy.exc.NoResultFound:
        return JsonResponse({"ok": False, "error":
            f"alarm {alarm_id} not found for datasource {pk}"}, status=404)
    except sqlalchemy.exc.IntegrityError:
        return JsonResponse({"ok": False, "error":
            f"an alarm named {values['name']!r} already exists for "
            f"this datasource", "fields": {"name": "name already in use"}},
            status=409)
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


@require_POST
@_login_required_json
def alarm_delete(request, pk: int, alarm_id: int):
    """Delete one alarm. Idempotent — 200 even if the row was already gone.

    CASCADE removes the alarm's history, firings and evaluation cursor
    along with it (the history of a deleted alarm is worthless — hence
    there is no 'deleted' kind in alarm_history). Uniqueness is per
    datasource, so re-creating the same name afterwards starts fresh.
    """
    try:
        with _meta_engine().begin() as conn:
            result = conn.execute(text(
                "DELETE FROM starcubes_meta.alarm "
                " WHERE id = :id AND datasource_id = :ds"
            ), {"ds": pk, "id": alarm_id})
        return JsonResponse({"ok": True, "deleted": result.rowcount})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


@require_POST
@_login_required_json
def alarm_history(request, pk: int, alarm_id: int):
    """Change history for one alarm — newest first, bounded.

    kind is 'created' (old_row NULL) or 'updated' (old/new snapshots); a
    per-field diff is rendered client-side in detail.html.
    """
    try:
        with _meta_engine().begin() as conn:
            rows = conn.execute(text(
                "SELECT kind, username, old_row, new_row, changed_at "
                "  FROM starcubes_meta.alarm_history "
                " WHERE alarm_id = :aid AND alarm_id IN "
                "       (SELECT id FROM starcubes_meta.alarm "
                "         WHERE id = :aid AND datasource_id = :ds) "
                " ORDER BY id DESC LIMIT 500"
            ), {"aid": alarm_id, "ds": pk}).mappings().all()
            if not rows:
                # 404 when the alarm itself is gone, [] when it exists but
                # has no history — same guard style as alarms_list.
                exists = conn.execute(text(
                    "SELECT 1 FROM starcubes_meta.alarm "
                    " WHERE id = :aid AND datasource_id = :ds"
                ), {"aid": alarm_id, "ds": pk}).scalar()
                if not exists:
                    return JsonResponse({"ok": False, "error":
                        f"alarm {alarm_id} not found for datasource {pk}"},
                        status=404)
        return JsonResponse({"ok": True, "history": [{
            "kind": r.kind,
            "username": r.username,
            "old_row": r.old_row,
            "new_row": r.new_row,
            "changed_at": r.changed_at.isoformat() if r.changed_at else None,
        } for r in rows]})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


@require_POST
@_login_required_json
def alarm_firings(request, pk: int, alarm_id: int):
    """Firing episodes for one alarm — newest first, bounded.

    An episode spans the first to the last breach of the threshold;
    closed_at IS NULL means it is still open.
    """
    try:
        with _meta_engine().begin() as conn:
            rows = conn.execute(text(
                "SELECT first_breach_at, last_breach_at, closed_at, "
                "       first_value, last_value "
                "  FROM starcubes_meta.alarm_firing "
                " WHERE alarm_id = :aid AND alarm_id IN "
                "       (SELECT id FROM starcubes_meta.alarm "
                "         WHERE id = :aid AND datasource_id = :ds) "
                " ORDER BY id DESC LIMIT 200"
            ), {"aid": alarm_id, "ds": pk}).mappings().all()
            if not rows:
                exists = conn.execute(text(
                    "SELECT 1 FROM starcubes_meta.alarm "
                    " WHERE id = :aid AND datasource_id = :ds"
                ), {"aid": alarm_id, "ds": pk}).scalar()
                if not exists:
                    return JsonResponse({"ok": False, "error":
                        f"alarm {alarm_id} not found for datasource {pk}"},
                        status=404)
        return JsonResponse({"ok": True, "firings": [{
            "first_breach_at": r.first_breach_at.isoformat() if r.first_breach_at else None,
            "last_breach_at": r.last_breach_at.isoformat() if r.last_breach_at else None,
            "closed_at": r.closed_at.isoformat() if r.closed_at else None,
            "open": r.closed_at is None,
            "first_value": float(r.first_value) if r.first_value is not None else None,
            "last_value": float(r.last_value) if r.last_value is not None else None,
        } for r in rows]})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


def alarm_firings_page(request):
    """The global firing-episode overview, across every datasource.

    The evaluator is one global process, so its output is one global page:
    every alarm_firing episode joined with its alarm rule and datasource,
    newest first. `?show=open` restricts it to episodes still open — the
    "what is firing right now" lens. `?ds=<id>` restricts it to one
    datasource (the chart page's firings button links here pre-filtered;
    anything non-numeric just means unfiltered). Login-only like every
    /sources/ page (RequireLoginMiddleware gates the path prefix; no role
    split needed — alarms are a user concern). Episodes of deleted alarms
    are gone with the alarm (ON DELETE CASCADE), so the page needs no
    orphan handling.
    """
    show = request.GET.get("show", "all")
    ds_raw = request.GET.get("ds", "")
    ds = int(ds_raw) if ds_raw.isdigit() else None
    conds = []
    if show == "open":
        conds.append("f.closed_at IS NULL")
    if ds is not None:
        conds.append("a.datasource_id = :ds")
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    params = {"ds": ds} if ds is not None else {}
    with _meta_engine().begin() as conn:
        rows = conn.execute(text(
            "SELECT f.id, f.first_breach_at, f.last_breach_at, f.closed_at, "
            "       f.first_value, f.last_value, "
            "       a.id AS alarm_id, a.name AS alarm_name, a.measure, "
            "       a.cond_op, a.threshold, a.window_s, "
            "       d.id AS ds_id, d.name AS ds_name "
            "  FROM starcubes_meta.alarm_firing f "
            "  JOIN starcubes_meta.alarm a ON a.id = f.alarm_id "
            "  JOIN starcubes_meta.datasource d ON d.id = a.datasource_id "
            f" {where} "
            " ORDER BY f.id DESC LIMIT 500"
        ), params).mappings().all()

    episodes = []
    for r in rows:
        span = (r["last_breach_at"] - r["first_breach_at"]).total_seconds()
        episodes.append({
            "alarm_id": r["alarm_id"],
            "alarm_name": r["alarm_name"],
            "ds_id": r["ds_id"],
            "ds_name": r["ds_name"],
            "rule": (f"{r['measure']} {r['cond_op']} "
                     f"{r['threshold']:g} ({fmt_window_s(r['window_s'])})"),
            "open": r["closed_at"] is None,
            "first_breach_at": r["first_breach_at"],
            "last_breach_at": r["last_breach_at"],
            "closed_at": r["closed_at"],
            "first_value": r["first_value"],
            "last_value": r["last_value"],
            "duration": fmt_episode_span(span),
        })
    return render(request, "sources/firings.html",
                  {"episodes": episodes, "show": show, "ds": ds,
                   "total": len(episodes)})


def fmt_episode_span(seconds: float) -> str:
    """Episode length as a compact human string (60 -> 1m, 3700 -> 1h 1m).

    The span is measured on the data high watermark (last_breach_at minus
    first_breach_at), so an episode seen by a single load has length 0 —
    shown as the "≤ one window" hint by the caller, not here.
    """
    s = int(seconds)
    if s <= 0:
        return "—"
    out = []
    for unit, div in (("d", 86400), ("h", 3600), ("m", 60)):
        n, s = divmod(s, div)
        if n:
            out.append(f"{n}{unit}")
    if not out or s:
        out.append(f"{s}s")
    return " ".join(out[:2])


def fmt_window_s(window_s: int) -> str:
    """window_s 300 -> '5m' — same shape detail.html's fmtWindow produces."""
    if window_s % 86400 == 0:
        return f"{window_s // 86400}d"
    if window_s % 3600 == 0:
        return f"{window_s // 3600}h"
    if window_s % 60 == 0:
        return f"{window_s // 60}m"
    return f"{window_s}s"


# ---------------------------------------------------------------------------
# Dimension groups (JSON)
# ---------------------------------------------------------------------------

@_login_required_json
def dim_values(request, pk: int, dim_id: int):
    """Return all rows of a dimension table for the group-assignment UI."""
    try:
        source = Source.load(_meta_engine(), dest_dsn=settings.STARCUBES_POSTGRES_DSN,
                             name_or_id=pk)
        dim = next((d for d in source.dimensions if d.id == dim_id), None)
        if dim is None:
            return JsonResponse({"ok": False, "error": f"dimension {dim_id} not found"})
        if not dim.dim_table:
            return JsonResponse({"ok": False, "error": "dimension has no dim table"})
        with source.dest_engine().begin() as conn:
            rows = [dict(r) for r in conn.execute(text(
                f"SELECT index, source_id, name, description, count "
                f"  FROM {source.schema_name}.{dim.dim_table} "
                f" WHERE source_id != :unk "
                f" ORDER BY index"
            ), {"unk": UNKNOWN_SOURCE_ID}).mappings().all()]
        return JsonResponse({"ok": True, "dim_table": dim.dim_table,
                             "fact_column": dim.fact_column, "values": rows},
                            json_dumps_params={"default": str})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


def _group_set_slug(conn, dimension_id: int, name: str,
                    exclude_id: int | None = None) -> str:
    """Derive a slug from `name`, suffixed until unique within the dimension.

    Uniqueness is per dimension because the chart token already carries the
    fact column (`<fact_column>:grouped:<slug>`), so two dimensions may reuse
    the same slug without colliding.
    """
    base = slugify_group_set(name)
    taken = {r[0] for r in conn.execute(text(
        "SELECT slug FROM starcubes_meta.dim_group_set "
        " WHERE dimension_id = :d AND (:ex IS NULL OR id != :ex)"
    ), {"d": dimension_id, "ex": exclude_id}).all()}
    slug = base
    n = 2
    while slug in taken:
        slug = f"{base}-{n}"
        n += 1
    return slug


@_login_required_json
def group_dims_list(request, pk: int):
    """List every group dimension of a datasource, with its native dimension."""
    try:
        source = Source.load(_meta_engine(), dest_dsn=settings.STARCUBES_POSTGRES_DSN,
                             name_or_id=pk)
        sets = []
        for dim in source.dimensions:
            if not dim.dim_table:
                continue
            for gs in dim.group_sets:
                sets.append({
                    "id":           gs.id,
                    "name":         gs.name,
                    "slug":         gs.slug,
                    "dimension_id": dim.id,
                    "fact_column":  dim.fact_column,
                    "dim_table":    dim.dim_table,
                    "token":        grouped_token(dim.fact_column, gs.slug),
                    "group_count":  len(gs.groups),
                })
        sets.sort(key=lambda s: (s["fact_column"], s["name"]))
        dims = [{"id": d.id, "fact_column": d.fact_column, "dim_table": d.dim_table}
                for d in source.dimensions if d.dim_table]
        dims.sort(key=lambda d: d["fact_column"])
        return JsonResponse({"ok": True, "group_sets": sets, "dimensions": dims},
                            json_dumps_params={"default": str})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


def _group_set_payload(source, gs, dim):
    """Groups + members of one set, with per-group row counts from the dim table."""
    groups = [{"group_name": g.group_name, "color": g.color or "",
               "members": [m.dim_index for m in g.members], "count": 0}
              for g in gs.groups]
    all_indices = {i for g in groups for i in g["members"]}
    if all_indices and dim.dim_table:
        idx_list = ",".join(str(i) for i in sorted(all_indices))
        with source.dest_engine().begin() as conn:
            rows = conn.execute(text(
                f"SELECT index, count "
                f"  FROM {source.schema_name}.{dim.dim_table} "
                f" WHERE index IN ({idx_list})"
            )).mappings().all()
        idx_counts = {r["index"]: r["count"] for r in rows}
        for g in groups:
            g["count"] = sum(idx_counts.get(i, 0) for i in g["members"])
    return {
        "id":           gs.id,
        "name":         gs.name,
        "slug":         gs.slug,
        "dimension_id": dim.id,
        "fact_column":  dim.fact_column,
        "dim_table":    dim.dim_table,
        "token":        grouped_token(dim.fact_column, gs.slug),
        "groups":       groups,
    }


def _find_group_set(source, gs_id: int):
    """(group_set, dimension) for an id belonging to this datasource, else (None, None)."""
    for dim in source.dimensions:
        for gs in dim.group_sets:
            if gs.id == gs_id:
                return gs, dim
    return None, None


@_login_required_json
def group_dim_detail(request, pk: int, gs_id: int):
    """Return one group dimension with its groups, members and counts."""
    try:
        source = Source.load(_meta_engine(), dest_dsn=settings.STARCUBES_POSTGRES_DSN,
                             name_or_id=pk)
        gs, dim = _find_group_set(source, gs_id)
        if gs is None:
            return JsonResponse({"ok": False, "error": "group dimension not found"})
        return JsonResponse({"ok": True, "group_set": _group_set_payload(source, gs, dim)},
                            json_dumps_params={"default": str})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


@require_POST
@_login_required_json
def group_dim_add(request, pk: int):
    """Create an empty group dimension on one native dimension."""
    try:
        data = _json.loads(request.body)
        dimension_id = int(data.get("dimension_id") or 0)
        name = (data.get("name") or "").strip()
        if not name:
            return JsonResponse({"ok": False, "error": "name is required"})
        with _meta_engine().begin() as conn:
            dim_row = conn.execute(text(
                "SELECT id FROM starcubes_meta.dimension "
                " WHERE id = :id AND datasource_id = :ds"
            ), {"id": dimension_id, "ds": pk}).mappings().one_or_none()
            if dim_row is None:
                return JsonResponse({"ok": False, "error": "dimension not found"})
            if conn.execute(text(
                "SELECT 1 FROM starcubes_meta.dim_group_set "
                " WHERE dimension_id = :d AND name = :n"
            ), {"d": dimension_id, "n": name}).first():
                return JsonResponse({"ok": False,
                    "error": f"a group dimension named {name!r} already exists "
                             f"on this dimension"})
            slug = _group_set_slug(conn, dimension_id, name)
            pos = conn.execute(text(
                "SELECT COALESCE(MAX(position), -1) + 1 "
                "  FROM starcubes_meta.dim_group_set WHERE dimension_id = :d"
            ), {"d": dimension_id}).scalar()
            new_id = conn.execute(text(
                "INSERT INTO starcubes_meta.dim_group_set "
                "       (dimension_id, name, slug, position) "
                "VALUES (:d, :n, :s, :p) RETURNING id"
            ), {"d": dimension_id, "n": name, "s": slug, "p": pos}).scalar()
        return group_dim_detail(request, pk, new_id)
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


@require_POST
@_login_required_json
def group_dim_save(request, pk: int, gs_id: int):
    """Rename a group dimension and replace *its* groups atomically.

    Only this set's groups are touched — other group dimensions on the same
    native dimension keep theirs, and the same dim value may be assigned in
    each of them independently. Within one set a value still belongs to at
    most one group.

    Moving the set to another dimension clears its groups: dim indices are
    dimension-specific, so carrying them over would silently mislabel rows.
    """
    try:
        data = _json.loads(request.body)
        name = (data.get("name") or "").strip()
        if not name:
            return JsonResponse({"ok": False, "error": "name is required"})
        groups_data = data.get("groups", [])
        new_dim_id = data.get("dimension_id")
        new_dim_id = int(new_dim_id) if new_dim_id else None

        seen_indices = set()
        for g in groups_data:
            for idx in g.get("dim_indices", []):
                if idx in seen_indices:
                    return JsonResponse({"ok": False,
                        "error": f"dim_index {idx} appears in more than one group"})
                seen_indices.add(idx)

        with _meta_engine().begin() as conn:
            row = conn.execute(text(
                "SELECT gs.id, gs.dimension_id "
                "  FROM starcubes_meta.dim_group_set gs "
                "  JOIN starcubes_meta.dimension d ON d.id = gs.dimension_id "
                " WHERE gs.id = :id AND d.datasource_id = :ds"
            ), {"id": gs_id, "ds": pk}).mappings().one_or_none()
            if row is None:
                return JsonResponse({"ok": False, "error": "group dimension not found"})

            dim_id = row["dimension_id"]
            if new_dim_id and new_dim_id != dim_id:
                if conn.execute(text(
                    "SELECT 1 FROM starcubes_meta.dimension "
                    " WHERE id = :id AND datasource_id = :ds"
                ), {"id": new_dim_id, "ds": pk}).first() is None:
                    return JsonResponse({"ok": False, "error": "dimension not found"})
                dim_id = new_dim_id
                groups_data = []  # indices belong to the old dimension

            if conn.execute(text(
                "SELECT 1 FROM starcubes_meta.dim_group_set "
                " WHERE dimension_id = :d AND name = :n AND id != :id"
            ), {"d": dim_id, "n": name, "id": gs_id}).first():
                return JsonResponse({"ok": False,
                    "error": f"a group dimension named {name!r} already exists "
                             f"on this dimension"})

            conn.execute(text(
                "UPDATE starcubes_meta.dim_group_set "
                "   SET name = :n, slug = :s, dimension_id = :d "
                " WHERE id = :id"
            ), {"n": name, "s": _group_set_slug(conn, dim_id, name, exclude_id=gs_id),
                "d": dim_id, "id": gs_id})

            # Replace this set's groups (CASCADE removes their members).
            conn.execute(text(
                "DELETE FROM starcubes_meta.dim_group WHERE group_set_id = :id"
            ), {"id": gs_id})
            for g in groups_data:
                group_name = g.get("group_name", "").strip()
                if not group_name:
                    continue
                gid = conn.execute(text(
                    "INSERT INTO starcubes_meta.dim_group "
                    "       (group_set_id, group_name, color) "
                    "VALUES (:gs, :n, :c) RETURNING id"
                ), {"gs": gs_id, "n": group_name,
                    "c": g.get("color", "") or ""}).scalar()
                for idx in g.get("dim_indices", []):
                    conn.execute(text(
                        "INSERT INTO starcubes_meta.dim_group_member "
                        "       (group_id, dim_index) VALUES (:gid, :idx)"
                    ), {"gid": gid, "idx": idx})

        return group_dim_detail(request, pk, gs_id)
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


@require_POST
@_login_required_json
def group_dim_delete(request, pk: int, gs_id: int):
    """Delete one group dimension; its groups and members go with it."""
    try:
        with _meta_engine().begin() as conn:
            deleted = conn.execute(text(
                "DELETE FROM starcubes_meta.dim_group_set gs "
                " USING starcubes_meta.dimension d "
                " WHERE gs.id = :id AND d.id = gs.dimension_id "
                "   AND d.datasource_id = :ds "
                " RETURNING gs.id"
            ), {"id": gs_id, "ds": pk}).first()
        if deleted is None:
            return JsonResponse({"ok": False, "error": "group dimension not found"})
        return JsonResponse({"ok": True})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


# ---------------------------------------------------------------------------
# Schema actions (create fact, import dims, dim data, create/drop indices)
# ---------------------------------------------------------------------------

@require_POST
@_admin_required_json
def create_fact_table(request, pk: int):
    """Create the fact table, dimension tables, and initial monthly partitions."""
    try:
        # ETL interlock: refuse while ETL is running
        with _meta_engine().begin() as conn:
            running = conn.execute(text(
                "SELECT id FROM starcubes_meta.etl_run "
                " WHERE datasource_id = :id AND status = 'running' LIMIT 1"
            ), {"id": pk}).one_or_none()
            forever_active = conn.execute(text(
                "SELECT 1 FROM starcubes_meta.etl_forever "
                " WHERE datasource_id = :id AND status IN ('running','stopping')"
            ), {"id": pk}).scalar()
        if running or forever_active:
            return JsonResponse(
                {"ok": False, "error": "ETL is currently running. Stop it before modifying schema."},
                status=409)

        source = Source.load(_meta_engine(), dest_dsn=settings.STARCUBES_POSTGRES_DSN,
                             name_or_id=pk)
        dest = source.dest_engine()
        create_fact(source, dest)
        dims = create_dim_tables(source, dest)
        return JsonResponse({"ok": True,
                             "fact": f"{source.schema_name}.fact",
                             "dims": dims})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


@require_POST
@_admin_required_json
def sync_schema_view(request, pk: int):
    """Reconcile physical warehouse schema with starcubes_meta configuration.

    Without confirm param: returns a dry-run diff (add_columns, alter_columns,
    unsafe_type_changes, drop_columns, create_dims, drop_dims).
    With confirm=true: executes the sync (adds columns, alters types, drops
    confirmed columns/dim tables, recreates FKs and indices).
    """
    try:
        source = Source.load(_meta_engine(), dest_dsn=settings.STARCUBES_POSTGRES_DSN,
                             name_or_id=pk)
        dest = source.dest_engine()

        is_execute = request.POST.get("confirm") == "true"
        drop_columns = request.POST.getlist("drop_columns")
        drop_dims = request.POST.getlist("drop_dims")

        # ETL interlock for execute phase
        if is_execute:
            with _meta_engine().begin() as conn:
                running = conn.execute(text(
                    "SELECT id FROM starcubes_meta.etl_run "
                    " WHERE datasource_id = :id AND status = 'running' LIMIT 1"
                ), {"id": pk}).one_or_none()
                forever_active = conn.execute(text(
                    "SELECT 1 FROM starcubes_meta.etl_forever "
                    " WHERE datasource_id = :id AND status IN ('running','stopping')"
                ), {"id": pk}).scalar()
            if running or forever_active:
                return JsonResponse(
                    {"ok": False, "error": "ETL is currently running. Stop it before syncing schema."},
                    status=409)

        result = sync_schema(source, dest,
                             drop_columns=drop_columns if is_execute else None,
                             drop_dims=drop_dims if is_execute else None)
        result["ok"] = True
        return JsonResponse(result, json_dumps_params={"default": str})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


@require_POST
@_admin_required_json
def import_dims(request, pk: int):
    """Import/update dimension tables. Works without source DB for inline dims."""
    try:
        source = Source.load(_meta_engine(), dest_dsn=settings.STARCUBES_POSTGRES_DSN,
                             name_or_id=pk)
        has_inline = any(d.is_primary and d.inline_values for d in source.dimensions)
        has_source = any(d.is_primary and d.source_table for d in source.dimensions)

        # Try source DB connection; proceed without it for inline-only cases.
        src_engine = None
        source_err = None
        if has_source:
            try:
                src_engine = source.source_engine()
                # Verify the connection actually works.
                with src_engine.connect() as conn:
                    conn.execute(text(
                        "SELECT 1 FROM dual"
                        if src_engine.dialect.name == "oracle"
                        else "SELECT 1"
                    ))
            except Exception as e:
                source_err = str(e)
                if not has_inline:
                    # No inline dims to fall back on — must have source DB.
                    return JsonResponse({"ok": False, "error":
                        f"Cannot reach source DB: {source_err}"})
                # Source DB unreachable but we have inline dims — proceed with those.

        import_dimensions(source, src_engine, source.dest_engine())

        # Summarize dim table row counts
        dims = []
        with source.dest_engine().begin() as conn:
            for d in source.primary_dimensions():
                if not d.dim_table:
                    continue
                cnt = conn.execute(text(
                    f"SELECT count(*) FROM {source.schema_name}.{d.dim_table}"
                )).scalar()
                dims.append({
                    "dim_table": d.dim_table,
                    "fact_column": d.fact_column,
                    "rows": cnt,
                    "inline": bool(d.inline_values),
                })

        result = {"ok": True, "dims": dims}
        if source_err:
            result["warning"] = f"Source DB unreachable ({source_err}); " \
                                f"only inline dimensions were imported."
        return JsonResponse(result)
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


@_login_required_json
def dim_data(request, pk: int):
    """Return list of primary dimension names (for building tabs) plus per-dim data."""
    try:
        source = Source.load(_meta_engine(), dest_dsn=settings.STARCUBES_POSTGRES_DSN,
                             name_or_id=pk)
        dims = []
        with source.dest_engine().begin() as conn:
            for d in source.primary_dimensions():
                if not d.dim_table:
                    continue
                dims.append({
                    "dim_table": d.dim_table,
                    "fact_column": d.fact_column,
                    "inline": bool(d.inline_values),
                })
        return JsonResponse({"ok": True, "dims": dims},
                            json_dumps_params={"default": str})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


@_login_required_json
def dim_data_single(request, pk: int, dim_table: str):
    """Return all rows of a single dimension table."""
    try:
        source = Source.load(_meta_engine(), dest_dsn=settings.STARCUBES_POSTGRES_DSN,
                             name_or_id=pk)
        # Find the matching dimension
        dim = next((d for d in source.primary_dimensions()
                    if d.dim_table == dim_table), None)
        if dim is None:
            return JsonResponse({"ok": False, "error": f"dimension {dim_table!r} not found"})
        with source.dest_engine().begin() as conn:
            table_exists = conn.execute(text(
                "SELECT 1 FROM information_schema.tables "
                " WHERE table_schema = :s AND table_name = :t"
            ), {"s": source.schema_name, "t": dim_table}).scalar()
            rows = []
            if table_exists:
                rows = [dict(r) for r in conn.execute(text(
                    f"SELECT index, source_id, name, description, count "
                    f"  FROM {source.schema_name}.{dim_table} "
                    f" ORDER BY index"
                )).mappings().all()]
        return JsonResponse({"ok": True, "dim_table": dim_table,
                             "fact_column": dim.fact_column,
                             "inline": bool(dim.inline_values),
                             "rows": rows},
                            json_dumps_params={"default": str})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


@require_POST
@_admin_required_json
def create_all_indices(request, pk: int):
    """Create all FK constraints and indexes for a datasource."""
    try:
        source = Source.load(_meta_engine(), dest_dsn=settings.STARCUBES_POSTGRES_DSN,
                             name_or_id=pk)
        create_indices(source, source.dest_engine())
        return JsonResponse({"ok": True})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


@require_POST
@_admin_required_json
def drop_all_indices(request, pk: int):
    """Drop all FK constraints and indexes for a datasource. Requires confirmation."""
    try:
        ds = _load_datasource(pk)
        confirm = request.POST.get("confirm", "")
        if confirm != ds["name"]:
            return JsonResponse({"ok": False, "error":
                f"Confirmation does not match datasource name '{ds['name']}'"})
        source = Source.load(_meta_engine(), dest_dsn=settings.STARCUBES_POSTGRES_DSN,
                             name_or_id=pk)
        drop_indices(source, source.dest_engine())
        return JsonResponse({"ok": True})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)})


def settings_page(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Superuser access required.")

    msg = request.GET.get("msg", "")
    err = request.GET.get("err", "")

    if request.method == "POST":
        action = request.POST.get("action", "")

        if action == "create_user":
            username = request.POST.get("username", "").strip()
            password = request.POST.get("password", "")
            email = (request.POST.get("email") or "").strip()
            if not username or not password:
                return redirect(f"{request.path}?err=Username+and+password+are+required.")
            if User.objects.filter(username=username).exists():
                return redirect(f"{request.path}?err=Username+already+exists.")
            User.objects.create_user(username=username, password=password, email=email)
            return redirect(f"{request.path}?msg=User+%27{username}%27+created.")

        elif action == "update_user":
            user_id = request.POST.get("user_id", "")
            try:
                target = User.objects.get(pk=int(user_id))
            except (User.DoesNotExist, ValueError):
                return redirect(f"{request.path}?err=User+not+found.")
            # Email — only write when the field was actually submitted; a
            # missing field means "leave it alone". An empty string clears it.
            email = request.POST.get("email")
            if email is not None:
                target.email = email.strip()
            # is_active — checkbox: absent means False. Guarded the same
            # way as is_superuser below: deactivating your own account logs
            # you out and leaves nobody holding this page.
            if "is_active" in request.POST:
                new_active = (request.POST.get("is_active") == "on")
                if target.pk == request.user.pk and not new_active:
                    return redirect(f"{request.path}?err=Cannot+deactivate+your+own+account.")
                target.is_active = new_active
            # is_superuser — never let an admin demote themselves, that
            # would lock everyone out of the page that revokes the right.
            if "is_superuser" in request.POST:
                new_super = (request.POST.get("is_superuser") == "on")
                if target.pk == request.user.pk and not new_super:
                    return redirect(f"{request.path}?err=Cannot+demote+your+own+admin+rights.")
                target.is_superuser = new_super
            # Django's is_superuser implies is_staff, but the two are set
            # independently here: an operator may want staff without admin,
            # and clearing superuser leaves staff alone.
            if "is_staff" in request.POST:
                target.is_staff = (request.POST.get("is_staff") == "on")
            target.save()
            return redirect(f"{request.path}?msg=User+%27{target.username}%27+updated.")

        elif action == "delete_user":
            user_id = request.POST.get("user_id", "")
            try:
                target = User.objects.get(pk=int(user_id))
            except (User.DoesNotExist, ValueError):
                return redirect(f"{request.path}?err=User+not+found.")
            if target.pk == request.user.pk:
                return redirect(f"{request.path}?err=Cannot+delete+your+own+account.")
            name = target.username
            # query_history.user_id has no FK to auth_user (see migration 0019
            # for why), so its rows do not cascade — drop them by hand.
            try:
                history_delete_user(_meta_engine(), user_id=target.pk)
            except Exception:
                pass
            target.delete()
            return redirect(f"{request.path}?msg=User+%27{name}%27+deleted.")

        elif action == "set_password":
            user_id = request.POST.get("user_id", "")
            new_password = request.POST.get("new_password", "")
            if not new_password:
                return redirect(f"{request.path}?err=New+password+cannot+be+empty.")
            try:
                target = User.objects.get(pk=int(user_id))
            except (User.DoesNotExist, ValueError):
                return redirect(f"{request.path}?err=User+not+found.")
            target.set_password(new_password)
            target.save()
            return redirect(f"{request.path}?msg=Password+updated+for+%27{target.username}%27.")

        return redirect(request.path)

    users = User.objects.all().order_by("username")
    return render(request, "sources/settings.html", {
        "users": users,
        "msg": msg,
        "err": err,
    })


def my_profile(request):
    """The signed-in user's own profile page.

    Per the Commit_4 plan, the page takes ``request.user`` only — there is
    no user_id in the URL and no admin-only data on it. Anyone signed in
    can see their own email, their last-login history (via the query
    console's ``query_history`` table — useful for picking up where they
    left off) and a form to change their own password.

    Password change is the only mutation; it deliberately lives outside
    the admin ``settings_page`` because a non-admin can still change
    their own password (and only their own).
    """
    if not request.user.is_authenticated:
        return redirect("/login/?next=/sources/me/")
    msg = request.GET.get("msg", "")
    err = request.GET.get("err", "")
    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "change_password":
            current = request.POST.get("current_password", "")
            new = request.POST.get("new_password", "")
            confirm = request.POST.get("confirm_password", "")
            if not (current and new and confirm):
                err = "All three password fields are required."
            elif new != confirm:
                err = "New password and confirmation do not match."
            elif not request.user.check_password(current):
                err = "Current password is incorrect."
            elif len(new) < 8:
                err = "New password must be at least 8 characters."
            else:
                request.user.set_password(new)
                request.user.save()
                # Changing the password rotates the hash the session cookie
                # is signed against; without this the user is logged out on
                # the very next request, right after being told it worked.
                update_session_auth_hash(request, request.user)
                return redirect("/sources/me/?msg=Password+updated.")
        return redirect(f"{request.path}?err={err}" if err else f"{request.path}?msg=Updated.")

    history = history_list_user(_meta_engine(), user_id=request.user.pk, limit=200)
    return render(request, "sources/me.html", {
        "me": request.user,
        "history": history,
        "msg": msg,
        "err": err,
    })


# ==========================================================================
# Source profiles — catalogue, save/download, upload, apply
#
# A profile is a datasource's whole starcubes_meta configuration as one YAML
# file, with the source DSN deliberately left out. See core/profile.py and
# docs/source-profiles.md.
# ==========================================================================

def _profile_error(exc: Exception, status: int = 400):
    return JsonResponse({"ok": False, "error": str(exc)}, status=status)


def _read_profile_rows(pk: int) -> dict:
    """Read everything ``dump_profile`` needs for one datasource.

    Deliberately reads the meta tables directly instead of going through
    ``Source.load``, which raises when neither ``import_query`` nor
    ``base_table`` is set — a half-configured datasource must still be
    exportable, that being how a config gets moved somewhere it can be
    finished.
    """
    with _meta_engine().begin() as conn:
        ds = conn.execute(text(
            "SELECT * FROM starcubes_meta.datasource WHERE id = :id"
        ), {"id": pk}).mappings().one_or_none()
        if ds is None:
            raise ValueError(f"datasource id={pk} not found")
        fact_cols = conn.execute(text(
            "SELECT name, pg_type, role, ordinal, source_expr "
            "  FROM starcubes_meta.fact_column "
            " WHERE datasource_id = :id ORDER BY ordinal, name"
        ), {"id": pk}).mappings().all()
        dimensions = conn.execute(text(
            "SELECT fact_column, dim_table, source_table, source_key, "
            "       source_name, source_description, is_primary, inline_values "
            "  FROM starcubes_meta.dimension "
            " WHERE datasource_id = :id ORDER BY fact_column"
        ), {"id": pk}).mappings().all()
        indices = conn.execute(text(
            "SELECT column_name, method FROM starcubes_meta.fact_index "
            " WHERE datasource_id = :id ORDER BY column_name, method"
        ), {"id": pk}).mappings().all()
        measures = conn.execute(text(
            "SELECT name, agg, graph_style, is_default, ordinal, percentiles "
            "  FROM starcubes_meta.measure "
            " WHERE datasource_id = :id ORDER BY ordinal, name"
        ), {"id": pk}).mappings().all()
        group_rows = conn.execute(text(
            "SELECT d.fact_column AS dimension, gs.id AS set_id, "
            "       gs.name AS set_name, gs.slug, gs.position, "
            "       dg.id AS group_id, dg.group_name, dg.color, dgm.dim_index "
            "  FROM starcubes_meta.dim_group_set gs "
            "  JOIN starcubes_meta.dimension d ON d.id = gs.dimension_id "
            "  LEFT JOIN starcubes_meta.dim_group dg ON dg.group_set_id = gs.id "
            "  LEFT JOIN starcubes_meta.dim_group_member dgm ON dgm.group_id = dg.id "
            " WHERE d.datasource_id = :id "
            " ORDER BY d.fact_column, gs.position, gs.id, dg.id, dgm.dim_index"
        ), {"id": pk}).mappings().all()
    return {
        "ds": dict(ds),
        "fact_columns": [dict(r) for r in fact_cols],
        "dimensions": [dict(r) for r in dimensions],
        "fact_indices": [dict(r) for r in indices],
        "measures": [dict(r) for r in measures],
        "group_sets": _assemble_group_sets(group_rows),
    }


def _assemble_group_sets(rows) -> list[dict]:
    """Fold the flat set/group/member join into nested dicts."""
    sets: dict[int, dict] = {}
    groups: dict[int, dict] = {}
    for r in rows:
        gs = sets.get(r["set_id"])
        if gs is None:
            gs = sets[r["set_id"]] = {
                "dimension": r["dimension"], "name": r["set_name"],
                "slug": r["slug"] or "", "position": r["position"] or 0,
                "groups": [],
            }
        if r["group_id"] is None:
            continue
        g = groups.get(r["group_id"])
        if g is None:
            g = groups[r["group_id"]] = {
                "group_name": r["group_name"], "color": r["color"] or "",
                "members": [],
            }
            gs["groups"].append(g)
        if r["dim_index"] is not None:
            g["members"].append(r["dim_index"])
    return list(sets.values())


def _datasource_profile_yaml(pk: int, name: str = "", description: str = "") -> str:
    rows = _read_profile_rows(pk)
    return dump_profile(rows["ds"], rows["fact_columns"], rows["dimensions"],
                        rows["fact_indices"], rows["measures"],
                        rows["group_sets"], name=name, description=description)


@_admin_required_json
def profiles_list(request):
    """Catalogue of every profile, shipped and saved. Login-only."""
    if not request.user.is_authenticated:
        return JsonResponse({"ok": False, "error": "login required"}, status=403)
    return JsonResponse({"ok": True, "profiles": iter_profiles()})


@_admin_required_page
def profiles_page(request):
    """The full Profile admin page.

    Sits at /sources/profiles/ and lists every saved and shipped profile
    with Use (→ /sources/new/?profile=<slug>), Download and Delete
    actions. Upload is on the same page.

    ``?apply=<slug>&pk=<datasource_id>`` opens the apply-confirm dialog
    directly on a datasource's manage page, so an admin can pick a
    profile here and have it pre-selected there. ``?save-profile=1&pk=…``
    flips the manage page into Save-as-Profile mode.
    """
    msg = request.GET.get("msg", "")
    err = request.GET.get("err", "")
    profiles = iter_profiles()
    # The apply/save flows are implemented once, on the manage page. This page
    # only needs enough to build the target picker that deep-links there.
    with _meta_engine().begin() as conn:
        sources = [dict(r) for r in conn.execute(text(
            "SELECT id, name FROM starcubes_meta.datasource ORDER BY name"
        )).mappings().all()]
    return render(request, "sources/profiles.html", {
        "profiles": profiles,
        "sources": sources,
        "msg": msg,
        "err": err,
    })


@_admin_required_json
def profile_file_download(request, slug: str):
    """Stream one catalogue profile's YAML file as an attachment.

    Serves the file as it sits on disk — the same bytes an upload stored —
    so a download/upload trip round-trips losslessly. Profiles exported via
    ``dump_profile`` carry their inline dimensions as ``values:``, so they
    are self-contained by construction; a hand-written profile using
    ``csv:`` references keeps those references, exactly as it reads here.
    """
    try:
        path = profile_path(validate_profile_name(slug))
    except ProfileError as exc:
        return _profile_error(exc, status=404)
    if path is None:
        return _profile_error(ValueError(f"profile {slug!r} not found"),
                              status=404)
    response = HttpResponse(path.read_bytes(), content_type="application/x-yaml")
    response["Content-Disposition"] = f'attachment; filename="{slug}.yaml"'
    return response


@_admin_required_json
def profile_detail(request, slug: str):
    """One profile's metadata plus the scalars the create form prefills from."""
    try:
        profile = load_profile(validate_profile_name(slug))
    except ProfileError as exc:
        return _profile_error(exc, status=404)
    path = profile_path(slug)
    return JsonResponse({"ok": True, "profile": {
        "name": profile.name,
        "origin": profile_origin(path) if path else "example",
        "description": profile.description,
        "dialect": profile.source_dialect,
        "scalars": profile.datasource_scalars(),
        "defaults": profile.defaults,
        "n_fact_columns": len(profile.fact_columns),
        "n_dimensions": len(profile.dimensions),
        "n_measures": len(profile.measures),
        "n_group_dims": len(profile.group_dims),
    }}, json_dumps_params={"default": str})


@require_POST
@_admin_required_json
def profile_upload(request):
    """Validate an uploaded YAML and store it under ``profiles/user/``."""
    upload = request.FILES.get("file")
    if upload is None:
        return _profile_error(ValueError("no file uploaded"))
    if upload.size > MAX_PROFILE_BYTES:
        return _profile_error(ValueError(
            f"file is {upload.size} bytes, over the "
            f"{MAX_PROFILE_BYTES}-byte limit"))
    name = (request.POST.get("name") or "").strip() or Path(upload.name).stem
    try:
        raw = upload.read().decode("utf-8")
    except UnicodeDecodeError as exc:
        return _profile_error(ValueError(f"file is not UTF-8 text: {exc}"))
    try:
        # Validates before anything is written; an invalid profile never
        # reaches the filesystem.
        path = save_profile(name, raw)
    except ProfileError as exc:
        return _profile_error(exc)
    return JsonResponse({"ok": True, "name": name, "path": str(path),
                         "profiles": iter_profiles()})


@require_POST
@_admin_required_json
def profile_delete(request, slug: str):
    """Delete a saved profile. Shipped examples are not deletable."""
    try:
        delete_profile(slug)
    except ProfileError as exc:
        return _profile_error(exc)
    return JsonResponse({"ok": True, "profiles": iter_profiles()})


@require_POST
@_admin_required_json
def profile_save(request, pk: int):
    """Save this datasource's configuration as ``profiles/user/<name>.yaml``."""
    name = (request.POST.get("name") or "").strip()
    description = (request.POST.get("description") or "").strip()
    overwrite = request.POST.get("overwrite") == "true"
    try:
        validate_profile_name(name)
    except ProfileError as exc:
        return _profile_error(exc)
    existing = profile_path(name)
    if existing is not None and not overwrite:
        return JsonResponse({
            "ok": False, "exists": True, "origin": profile_origin(existing),
            "error": f"profile {name!r} already exists",
        }, status=409)
    if existing is not None and profile_origin(existing) == "example":
        return _profile_error(ValueError(
            f"{name!r} is a shipped example; choose another name"))
    try:
        yaml_text = _datasource_profile_yaml(pk, name=name, description=description)
        path = save_profile(name, yaml_text)
    except (ProfileError, ValueError) as exc:
        return _profile_error(exc)
    return JsonResponse({"ok": True, "name": name, "path": str(path),
                         "bytes": len(yaml_text.encode("utf-8")),
                         "profiles": iter_profiles()})


@_admin_required_json
def profile_download(request, pk: int):
    """Stream this datasource's configuration as a .yaml attachment."""
    ds = _load_datasource(pk)
    # The datasource name is not a validated profile name, so derive a safe
    # filename from it rather than trusting it into a header.
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", ds["name"]).strip("-") or f"datasource-{pk}"
    name = (request.GET.get("name") or "").strip()
    if name:
        try:
            stem = validate_profile_name(name)
        except ProfileError as exc:
            return _profile_error(exc)
    yaml_text = _datasource_profile_yaml(
        pk, name=stem, description=(request.GET.get("description") or "").strip())
    response = HttpResponse(yaml_text, content_type="application/x-yaml")
    response["Content-Disposition"] = f'attachment; filename="{stem}.yaml"'
    return response


def _diff_rows(existing, incoming, key, fields) -> dict:
    """Compare two row lists by a natural key.

    Returns ``{added, removed, changed}``; ``changed`` names the differing
    fields with their old and new values, so the GUI can show what an apply
    would actually do rather than just a count.
    """
    def norm(value):
        # A NULL column and an omitted YAML key mean the same thing here.
        return "" if value is None else value

    cur = {key(r): r for r in existing}
    new = {key(r): r for r in incoming}
    added = [k for k in new if k not in cur]
    removed = [k for k in cur if k not in new]
    changed = []
    for k in new:
        if k not in cur:
            continue
        deltas = {f: [norm(cur[k].get(f)), norm(new[k].get(f))]
                  for f in fields
                  if norm(cur[k].get(f)) != norm(new[k].get(f))}
        if deltas:
            changed.append({"key": k, "fields": deltas})
    return {
        "added": sorted(str(k) for k in added),
        "removed": sorted(str(k) for k in removed),
        "changed": sorted(changed, key=lambda c: str(c["key"])),
        "n_added": len(added), "n_removed": len(removed),
        "n_changed": len(changed),
    }


def _group_set_signature(gs: dict) -> str:
    """Canonical text for one group set, for change detection."""
    groups = sorted(
        (g.get("group_name") or g.get("name") or "",
         g.get("color") or "",
         tuple(sorted(int(m) for m in (g.get("members") or []))))
        for g in (gs.get("groups") or []))
    return _json.dumps(groups, sort_keys=True, default=str)


def _profile_diff(rows: dict, profile) -> dict:
    """What applying ``profile`` would change about datasource ``rows``."""
    scalars = profile.datasource_scalars()
    ds = rows["ds"]
    scalar_changes = {}
    for field_name, new_value in scalars.items():
        old_value = ds.get(field_name)
        if field_name == "interval_ladder":
            old_cmp = list(old_value) if old_value else []
            new_cmp = list(new_value)
        elif isinstance(new_value, float) or isinstance(old_value, float):
            old_cmp = float(old_value) if old_value is not None else None
            new_cmp = float(new_value)
        else:
            old_cmp = old_value
            new_cmp = new_value
        if old_cmp != new_cmp:
            scalar_changes[field_name] = [old_value, new_value]

    incoming_group_sets = [{
        "dimension": gs.dimension, "name": gs.name,
        "slug": gs.slug or slugify_group_set(gs.name),
        "position": gs.position,
        "groups": [{"group_name": g.name, "color": g.color,
                    "members": list(g.members)} for g in gs.groups],
    } for gs in profile.group_dims]

    def with_signature(sets):
        return [dict(gs, signature=_group_set_signature(gs)) for gs in sets]

    return {
        "scalars": scalar_changes,
        "fact_columns": _diff_rows(
            rows["fact_columns"],
            [{"name": fc.name, "pg_type": fc.pg_type, "role": fc.role,
              "ordinal": fc.ordinal, "source_expr": fc.source_expr}
             for fc in profile.fact_columns],
            key=lambda r: r["name"],
            fields=("pg_type", "role", "ordinal", "source_expr")),
        "dimensions": _diff_rows(
            rows["dimensions"],
            [{"fact_column": d.fact_column, "dim_table": d.dim_table,
              "source_table": d.source_table, "source_key": d.source_key,
              "source_name": d.source_name,
              "source_description": d.source_description,
              "is_primary": d.is_primary} for d in profile.dimensions],
            key=lambda r: r["fact_column"],
            fields=("dim_table", "source_table", "source_key", "source_name",
                    "source_description", "is_primary")),
        "fact_indices": _diff_rows(
            rows["fact_indices"],
            [{"column_name": fi.column_name, "method": fi.method}
             for fi in profile.fact_indices],
            key=lambda r: (r["column_name"], r["method"]),
            fields=()),
        "measures": _diff_rows(
            rows["measures"],
            [{"name": m.name, "agg": m.agg, "graph_style": m.graph_style,
              "is_default": m.is_default, "ordinal": m.ordinal,
              "percentiles": m.percentiles} for m in profile.measures],
            key=lambda r: r["name"],
            fields=("agg", "graph_style", "is_default", "ordinal", "percentiles")),
        "group_dims": _diff_rows(
            with_signature(rows["group_sets"]),
            with_signature(incoming_group_sets),
            key=lambda r: (r["dimension"], r["name"]),
            fields=("slug", "position", "signature")),
    }


def _profile_warnings(rows: dict, profile, diff: dict) -> list[str]:
    """Non-blocking cautions the apply dialog shows before the second click."""
    warnings = []
    fc_diff = diff["fact_columns"]
    if fc_diff["n_removed"] or fc_diff["n_changed"]:
        approx = _fact_row_estimate(rows["ds"]["schema_name"])
        if approx:
            warnings.append(
                f"The fact table holds roughly {approx:,} rows. This profile "
                f"removes or retypes fact columns — run Sync Schema afterwards "
                f"to reconcile the physical table.")
    # dim_index is assigned at import time, so group membership only travels
    # between installations for dimensions whose values ship with the profile.
    source_backed = {d.fact_column for d in profile.dimensions
                     if d.source_table is not None}
    risky = sorted({gs.dimension for gs in profile.group_dims
                    if gs.dimension in source_backed})
    if risky:
        warnings.append(
            f"Group dimensions on {', '.join(risky)} store membership as "
            f"dim_index, which is assigned when dimension values are "
            f"imported. Verify the groups after importing dimensions.")
    return warnings


def _fact_row_estimate(schema: str) -> int:
    """Approximate live row count of ``<schema>.fact``, 0 if it has none."""
    try:
        with _meta_engine().begin() as conn:
            return conn.execute(text(
                "SELECT COALESCE(n_live_tup, 0) FROM pg_stat_user_tables "
                " WHERE schemaname = :s AND relname = 'fact'"
            ), {"s": schema}).scalar() or 0
    except Exception:
        return 0


@require_POST
@_admin_required_json
def profile_apply(request, pk: int):
    """Apply a profile to an existing datasource.

    Without ``confirm``: a dry-run diff, same two-phase shape as
    ``sync_schema_view``. With ``confirm=true``: replaces the datasource's
    child rows and scalar fields in one transaction. The source DSN, name
    and schema are never touched — a profile carries no credentials, and
    renaming a live datasource is not what "apply a configuration" means.
    """
    slug = (request.POST.get("profile") or "").strip()
    try:
        profile = load_profile(validate_profile_name(slug))
    except ProfileError as exc:
        return _profile_error(exc)
    try:
        rows = _read_profile_rows(pk)
    except ValueError as exc:
        return _profile_error(exc, status=404)

    diff = _profile_diff(rows, profile)
    warnings = _profile_warnings(rows, profile, diff)
    if request.POST.get("confirm") != "true":
        return JsonResponse({"ok": True, "applied": False, "diff": diff,
                             "warnings": warnings, "profile": profile.name},
                            json_dumps_params={"default": str})

    busy = _etl_busy_response(pk)
    if busy is not None:
        return busy

    meta = _meta_engine()
    with meta.begin() as conn:
        update_datasource_scalars(conn, pk, profile)
        delete_profile_children(conn, pk)
        insert_profile_children(conn, pk, profile)
    # Measures, dimensions and group sets all feed cached plot JSON.
    bump_cache_epoch(meta, pk)
    return JsonResponse({"ok": True, "applied": True, "diff": diff,
                         "warnings": warnings, "profile": profile.name},
                        json_dumps_params={"default": str})
