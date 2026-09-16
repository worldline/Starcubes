"""Take over forever sessions whose container is gone.

Shared by `manage.py etl_resume_forever` (once, at container start) and
`manage.py node_agent` (on NOTIFY and once a minute), so a container start and
a running container follow exactly the same rules.

A session is adopted when

- its process is gone — `running`/`stopping` with a lease that is no longer in
  `pg_stat_activity` (core/lease.py), or
- it was **released**: its loop left on SIGTERM without a stop request because
  its container drained (migration 0044). Only the newest session of a
  datasource counts; a newer one means somebody has already moved on.

and it is not adopted when a stop was requested — that session is closed as
`stopped`, because an operator's Stop must never be undone by an adoption.

A row without a lease was written by an image older than migration 0042 and
can only be judged by its PID, i.e. only in the container that wrote it. That
rule (`include_legacy`) is for `etl_resume_forever` at container start, where
it has always applied, and never for the node_agent of a running container.

**Two containers may try at once.** Each datasource is claimed under a
transaction-scoped advisory lock (`core.pglock.ADOPT_CLASS`) and the candidate
is re-read inside it, the new row is inserted in the same transaction under
`ux_etl_forever_active`, and the loser of either race skips — nobody fails,
and nobody starts a second loop. The subprocess is spawned only after that
transaction has committed.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from django.conf import settings
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from core.lease import STARTING_GRACE_S, alive_sql
from core.node import hostname
from core.pglock import ADOPT_CLASS
from core.utils import _pid_alive
from sources.etl_spawn import PYTHON_EXE, spawn_etl_subprocess

logger = logging.getLogger(__name__)

_CANDIDATES_SQL = (
    "SELECT f.id, f.datasource_id, f.pid, f.interval_s, f.status, "
    "       f.stop_requested, f.hostname, f.lease_pid, d.name, "
    "       CASE WHEN f.lease_pid IS NULL THEN NULL "
    f"            ELSE {alive_sql('f')} END AS alive, "
    "       f.started_at > now() - make_interval(secs => :grace) AS young "
    "  FROM starcubes_meta.etl_forever f "
    "  JOIN starcubes_meta.datasource d ON d.id = f.datasource_id "
    " WHERE {where} "
    " ORDER BY f.id")

_WHERE_ANY = (
    "(f.status IN ('running', 'stopping') "
    " OR (f.status = 'released' AND NOT EXISTS ("
    "       SELECT 1 FROM starcubes_meta.etl_forever g "
    "        WHERE g.datasource_id = f.datasource_id AND g.id > f.id)))")


def adopt_orphans(meta, *, include_legacy: bool,
                  say: Optional[Callable[[str], None]] = None) -> tuple[int, int]:
    """Adopt what can be adopted. Returns (adopted, skipped)."""
    say = say or logger.info
    with meta.begin() as conn:
        rows = conn.execute(text(_CANDIDATES_SQL.format(where=_WHERE_ANY)),
                            {"grace": STARTING_GRACE_S}).mappings().all()
    adopted = skipped = 0
    for row in rows:
        verdict = _verdict(dict(row), include_legacy)
        if verdict == "alive":
            skipped += 1
            continue
        if verdict == "stop":
            _close_stopped(meta, row)
            say(f"datasource {row['name']!r}: etl_forever id={row['id']} was "
                f"asked to stop and its process is gone — closed, not adopted")
            continue
        new_id = _claim(meta, dict(row), include_legacy)
        if new_id is None:
            skipped += 1
            continue
        if _spawn(meta, row, new_id, say):
            adopted += 1
    return adopted, skipped


def _verdict(row: dict, include_legacy: bool) -> str:
    """'adopt', 'stop' (close, do not adopt) or 'alive' (leave alone)."""
    if row["status"] == "released":
        return "adopt"
    if row["alive"]:
        return "alive"
    if row["alive"] is None:
        # No lease: a legacy row, or one whose subprocess is still starting.
        if not include_legacy:
            return "alive"
        if row["pid"] and _pid_alive(row["pid"]):
            return "alive"
        if not row["pid"] and row["young"]:
            return "alive"
    if row["stop_requested"] or row["status"] == "stopping":
        return "stop"
    return "adopt"


def _close_stopped(meta, row) -> None:
    with meta.begin() as conn:
        conn.execute(text(
            "UPDATE starcubes_meta.etl_forever "
            "   SET status = 'stopped', pid = NULL, stopped_at = now(), "
            "       stop_requested = false "
            " WHERE id = :id AND status IN ('running', 'stopping')"),
            {"id": row["id"]})


def _claim(meta, row: dict, include_legacy: bool) -> Optional[int]:
    """Close the orphan and insert its successor, atomically. None = lost."""
    try:
        with meta.begin() as conn:
            got = conn.execute(text("SELECT pg_try_advisory_xact_lock(:c, :o)"),
                               {"c": ADOPT_CLASS,
                                "o": row["datasource_id"]}).scalar()
            if not got:
                return None
            # Re-read under the lock: another container may have adopted it,
            # or the session may have come back, between the scan and now.
            fresh = conn.execute(text(
                _CANDIDATES_SQL.format(where="f.id = :id AND " + _WHERE_ANY)),
                {"id": row["id"], "grace": STARTING_GRACE_S}).mappings().one_or_none()
            if fresh is None or _verdict(dict(fresh), include_legacy) != "adopt":
                return None
            note = f"adopted by {hostname()}"
            if row["status"] == "released":
                # Stays `released` — that is what happened to it. The newer
                # row inserted below is what keeps it from being adopted twice.
                conn.execute(text(
                    "UPDATE starcubes_meta.etl_forever "
                    "   SET error = :note WHERE id = :id"),
                    {"id": row["id"], "note": note})
            else:
                conn.execute(text(
                    "UPDATE starcubes_meta.etl_forever "
                    "   SET status = 'failed', pid = NULL, stopped_at = now(), "
                    "       error = :note "
                    " WHERE id = :id AND status IN ('running', 'stopping')"),
                    {"id": row["id"],
                     "note": f"process gone; {note}"})
            return conn.execute(text(
                "INSERT INTO starcubes_meta.etl_forever "
                "  (datasource_id, interval_s, status) "
                " VALUES (:ds, :interval, 'running') RETURNING id"),
                {"ds": row["datasource_id"],
                 "interval": row["interval_s"]}).scalar()
    except IntegrityError:
        # A session for this datasource is already active — started by a
        # user or adopted by another container in the meantime.
        return None


def _spawn(meta, row, new_id: int, say) -> bool:
    cmd = [PYTHON_EXE, str(settings.BASE_DIR / "manage.py"), "etl_run",
           "--name", row["name"], "--forever", str(row["interval_s"]),
           "--forever-id", str(new_id)]
    try:
        spawn_etl_subprocess(cmd, datasource_name=row["name"])
    except OSError as exc:
        with meta.begin() as conn:
            conn.execute(text(
                "UPDATE starcubes_meta.etl_forever "
                "   SET status = 'failed', pid = NULL, stopped_at = now(), "
                "       error = 'subprocess failed to start on adoption' "
                " WHERE id = :id"), {"id": new_id})
        say(f"datasource {row['name']!r}: failed to spawn the adopted "
            f"session: {exc}")
        return False
    say(f"datasource {row['name']!r}: adopted forever ETL "
        f"(interval={row['interval_s']}s, etl_forever id={new_id}, "
        f"replaced id={row['id']}, {row['status']} on "
        f"{row['hostname'] or 'unknown host'})")
    return True
