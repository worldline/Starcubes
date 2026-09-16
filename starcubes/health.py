"""Health check and container list, for running several containers.

`/healthz` answers a load balancer's or Kubernetes' readiness probe without a
login: 200 when this container can reach the metadata database **and** its
`manage.py node_agent` is alive, 503 otherwise. The agent is part of the
answer on purpose: nothing supervises it, and a container without one cannot
carry out a forced kill or adopt a forever session — so a probe that restarts
such a container is the supervision it otherwise lacks. Set
`STARCUBES_HEALTHZ_REQUIRE_AGENT=0` where no agent runs (a bare `runserver`).
The body names only which check failed; it carries no host names, versions or
credentials, because it is served to anyone who can reach the port.

`/sources/nodes/` (login required) lists the containers sharing this database,
for the About dialog: every live `node_agent` lease in `pg_stat_activity`
(core/lease.py names it `starcubes/<instance8>/<pid>/node_agent`), with the
host name its runs recorded.
"""
from __future__ import annotations

import os

from django.conf import settings
from django.http import JsonResponse
from sqlalchemy import text

from core.lease import APPLICATION_NAME_PREFIX
from core.node import hostname, short_instance_id
from core.source import _get_cached_engine


def _agent_pattern(short: str = "%") -> str:
    return f"{APPLICATION_NAME_PREFIX}{short}/%/node_agent"


def healthz(request):
    checks = {"database": False, "node_agent": None}
    require_agent = os.environ.get("STARCUBES_HEALTHZ_REQUIRE_AGENT", "1") != "0"
    try:
        with _get_cached_engine(settings.STARCUBES_POSTGRES_DSN).begin() as conn:
            conn.execute(text("SELECT 1"))
            checks["database"] = True
            checks["node_agent"] = bool(conn.execute(text(
                "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                " WHERE application_name LIKE :p)"),
                {"p": _agent_pattern(short_instance_id())}).scalar())
    except Exception:
        pass
    ok = checks["database"] and (checks["node_agent"] or not require_agent)
    return JsonResponse({"status": "ok" if ok else "unavailable", "checks": checks},
                        status=200 if ok else 503)


def nodes(request):
    if not request.user.is_authenticated:
        return JsonResponse({"ok": False, "error": "login required"}, status=403)
    mine = short_instance_id()
    with _get_cached_engine(settings.STARCUBES_POSTGRES_DSN).begin() as conn:
        rows = conn.execute(text(
            "SELECT split_part(a.application_name, '/', 2) AS instance, "
            "       split_part(a.application_name, '/', 3) AS pid, "
            "       a.backend_start AS since, "
            "       (SELECT r.hostname FROM starcubes_meta.run r "
            "         WHERE r.hostname IS NOT NULL "
            "           AND left(replace(r.instance_id, '-', ''), 8) "
            "               = split_part(a.application_name, '/', 2) "
            "         ORDER BY r.started_at DESC LIMIT 1) AS hostname "
            "  FROM pg_stat_activity a "
            " WHERE a.application_name LIKE :p "
            " ORDER BY a.backend_start"), {"p": _agent_pattern()}).mappings().all()
    out = []
    for r in rows:
        this = r["instance"] == mine
        out.append({"instance": r["instance"], "this": this,
                    "hostname": hostname() if this else r["hostname"],
                    "since": r["since"].isoformat() if r["since"] else None})
    return JsonResponse({"ok": True, "nodes": out})
