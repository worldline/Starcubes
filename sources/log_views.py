"""The one log: every process's lines, and every run, behind one set of filters.

Backs `/sources/logs/` and the ETL tab's Log sub-tab, which embeds the same
viewer (`sources/_log_viewer.html` + `sources/static/sources/log_viewer.js`)
with the datasource fixed. Two JSON endpoints, both superuser-only like the
log files they replace:

- `logs/api/lines/` reads `starcubes_meta.log_line` (migration 0046). Filters
  `datasource`, `kind` (repeatable), `host`, `level` (minimum; a raw line
  without a level counts as INFO), `run`, `from`/`to`, `q` (ILIKE — limited
  to the last 24 hours unless a `from` is given, so a text search cannot
  scan the whole table). Paging by id: `after_id` follows the tail,
  `before_id` pages back, neither returns the newest page. Every page is
  newest first, like the runs — the viewer shows both in that order. Each
  page also names the run behind every `run_ref` on it, so a line can link
  to its run.
- `logs/api/runs/` reads the view `starcubes_meta.run` — every run of every
  kind, the five history tables stay where they are — with the same
  datasource/kind/host/time filters plus `status` and `alive`, newest first,
  paged by `(started_at, kind, id)`.

The filter vocabulary is deliberately the same in both, so switching the
viewer between Lines and Runs keeps every filter.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from django.http import JsonResponse
from django.shortcuts import render
from sqlalchemy import text

from core.log_ship import KINDS, LEVELS
from core.utils import _parse_dt
from sources.views import _admin_required_json, _admin_required_page, _meta_engine

LINES_LIMIT = 1000
RUNS_LIMIT = 100
RUN_KINDS = KINDS + ("audit", "housekeeping")
STATUSES = ("running", "stopping", "ok", "failed", "stopped", "released")


class _BadFilter(ValueError):
    pass


def _int(value, name):
    try:
        return int(value)
    except (TypeError, ValueError):
        raise _BadFilter(f"{name} must be an integer")


def _ts(value, name):
    try:
        return _parse_dt(value)
    except Exception:
        raise _BadFilter(f"{name} is not a timestamp")


def _common_filters(request, *, alias: str, time_col: str, kinds_allowed,
                    host_col: str = "hostname"):
    """WHERE clauses and params shared by both endpoints."""
    where, params = [], {}
    g = request.GET
    if g.get("datasource"):
        where.append(f"{alias}.datasource_id = :ds")
        params["ds"] = _int(g["datasource"], "datasource")
    kinds = [k for k in g.getlist("kind") if k]
    if kinds:
        bad = [k for k in kinds if k not in kinds_allowed]
        if bad:
            raise _BadFilter(f"unknown kind {bad[0]!r}")
        where.append(f"{alias}.kind = ANY(:kinds)")
        params["kinds"] = kinds
    if g.get("host"):
        where.append(f"{alias}.{host_col} = :host")
        params["host"] = g["host"]
    if g.get("from"):
        where.append(f"{alias}.{time_col} >= :from_ts")
        params["from_ts"] = _ts(g["from"], "from")
    if g.get("to"):
        where.append(f"{alias}.{time_col} < :to_ts")
        params["to_ts"] = _ts(g["to"], "to")
    return where, params


def _recent_hosts(conn, datasource_id=None) -> list[str]:
    return conn.execute(text(
        "SELECT DISTINCT hostname FROM starcubes_meta.run "
        " WHERE hostname IS NOT NULL "
        "   AND started_at > now() - interval '30 days' "
        + ("AND datasource_id = :ds " if datasource_id is not None else "")
        + "ORDER BY 1"), {"ds": datasource_id}).scalars().all()


def viewer_context(datasource_id: int) -> dict:
    """What source.html's embedded viewer needs, pinned to one datasource."""
    with _meta_engine().begin() as conn:
        hosts = _recent_hosts(conn, datasource_id)
    return {"log_kinds": RUN_KINDS, "log_levels": LEVELS, "log_hosts": hosts}


@_admin_required_page
def logs_page(request):
    with _meta_engine().begin() as conn:
        datasources = [dict(r) for r in conn.execute(text(
            "SELECT id, name FROM starcubes_meta.datasource ORDER BY name"
        )).mappings()]
        hosts = _recent_hosts(conn)
    return render(request, "sources/logs.html", {
        "datasources": datasources, "hosts": hosts,
        "kinds": RUN_KINDS, "levels": LEVELS,
    })


@_admin_required_json
def logs_lines_api(request):
    g = request.GET
    try:
        # Audit and housekeeping are valid kinds that simply have no lines:
        # the viewer keeps one kind filter across Lines and Runs.
        where, params = _common_filters(request, alias="l", time_col="ts",
                                        kinds_allowed=RUN_KINDS)
        if g.get("run"):
            where.append("l.run_ref = :run")
            params["run"] = g["run"]
        if g.get("level"):
            if g["level"] not in LEVELS:
                raise _BadFilter(f"unknown level {g['level']!r}")
            where.append("coalesce(l.level, 'INFO') = ANY(:levels)")
            params["levels"] = list(LEVELS[LEVELS.index(g["level"]):])
        if g.get("q"):
            where.append("l.line ILIKE :q")
            params["q"] = "%" + g["q"].replace("\\", "\\\\").replace(
                "%", "\\%").replace("_", "\\_") + "%"
            if not g.get("from"):
                where.append("l.ts >= :q_floor")
                params["q_floor"] = datetime.now(timezone.utc) - timedelta(hours=24)
        limit = min(max(_int(g.get("limit") or LINES_LIMIT, "limit"), 1), LINES_LIMIT)
        order = "DESC"
        if g.get("after_id"):
            where.append("l.id > :after")
            params["after"] = _int(g["after_id"], "after_id")
            order = "ASC"
        elif g.get("before_id"):
            where.append("l.id < :before")
            params["before"] = _int(g["before_id"], "before_id")
    except _BadFilter as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)

    params["limit"] = limit
    sql = ("SELECT l.id, l.ts, l.datasource_id, d.name AS datasource, l.kind, "
           "       l.run_ref, l.hostname, l.level, l.line "
           "  FROM starcubes_meta.log_line l "
           "  LEFT JOIN starcubes_meta.datasource d ON d.id = l.datasource_id "
           + (" WHERE " + " AND ".join(where) if where else "")
           + f" ORDER BY l.id {order} LIMIT :limit")
    with _meta_engine().begin() as conn:
        rows = [dict(r) for r in conn.execute(text(sql), params).mappings()]
        if order == "ASC":
            rows.reverse()
        refs = sorted({r["run_ref"] for r in rows})
        runs = {}
        if refs:
            # A forever session and each of its loads share one log; the
            # session is the more useful thing to link to, so it wins.
            for r in conn.execute(text(
                    "SELECT run_ref, kind, id, status, alive "
                    "  FROM starcubes_meta.run WHERE run_ref = ANY(:refs) "
                    " ORDER BY (kind = 'forever') DESC, started_at DESC"),
                    {"refs": refs}).mappings():
                runs.setdefault(r["run_ref"], {"kind": r["kind"], "id": r["id"],
                                               "status": r["status"],
                                               "alive": r["alive"]})
    for r in rows:
        r["ts"] = r["ts"].isoformat()
    return JsonResponse({
        "ok": True, "lines": rows, "runs": runs,
        "more_before": order == "DESC" and len(rows) == limit,
    })


@_admin_required_json
def logs_runs_api(request):
    g = request.GET
    try:
        where, params = _common_filters(request, alias="r", time_col="started_at",
                                        kinds_allowed=RUN_KINDS)
        if g.get("status"):
            if g["status"] not in STATUSES:
                raise _BadFilter(f"unknown status {g['status']!r}")
            where.append("r.status = :status")
            params["status"] = g["status"]
        if g.get("alive") == "1":
            where.append("r.alive")
        if g.get("before_started"):
            where.append("(r.started_at, r.kind, r.id) < (:bs, :bk, :bi)")
            params["bs"] = _ts(g["before_started"], "before_started")
            params["bk"] = g.get("before_kind") or ""
            params["bi"] = _int(g.get("before_id"), "before_id")
    except _BadFilter as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    params["limit"] = RUNS_LIMIT
    sql = ("SELECT r.kind, r.id, r.datasource_id, d.name AS datasource, r.status, "
           "       r.started_at, r.finished_at, r.hostname, r.pid, r.alive, "
           "       r.run_ref, r.error, r.summary "
           "  FROM starcubes_meta.run r "
           "  LEFT JOIN starcubes_meta.datasource d ON d.id = r.datasource_id "
           + (" WHERE " + " AND ".join(where) if where else "")
           + " ORDER BY r.started_at DESC, r.kind DESC, r.id DESC LIMIT :limit")
    with _meta_engine().begin() as conn:
        rows = [dict(r) for r in conn.execute(text(sql), params).mappings()]
    for r in rows:
        for col in ("started_at", "finished_at"):
            if r[col] is not None:
                r[col] = r[col].isoformat()
    return JsonResponse({"ok": True, "runs": rows,
                         "more": len(rows) == RUNS_LIMIT})
