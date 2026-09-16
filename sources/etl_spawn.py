"""Helpers for spawning detached ETL and rollup subprocesses.

Shared between sources/views.py (GUI start) and sources/forever_adopt.py
(container-start resume and adoption).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from typing import Optional

from core.log_ship import write_sidecar


# When running under uWSGI, sys.executable points to the uwsgi binary,
# not Python.  Using it to spawn a Django management command makes uwsgi
# try to parse Django's flags as its own (e.g. --name clashes with
# --namespace).  Fall back to a real Python interpreter in that case.
PYTHON_EXE = sys.executable if "uwsgi" not in os.path.basename(sys.executable) else (shutil.which("python3") or "python")

_SLUG_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def log_dir() -> str:
    """Directory spool files are written to (STARCUBES_LOG_DIR)."""
    from django.conf import settings
    return settings.STARCUBES_LOG_DIR


def slugify_ds(name: str) -> str:
    """Filesystem/regex-safe stand-in for a datasource name in a log filename."""
    return _SLUG_RE.sub("_", name).strip("_") or "ds"


def spawn_etl_subprocess(cmd, env=None, *, datasource_name: str | None = None,
                         kind: Optional[str] = None,
                         datasource_id: Optional[int] = None):
    """Spawn a detached ETL subprocess that survives uWSGI worker recycling.

    Without start_new_session=True the child stays in the worker's session
    and process group, so a SIGTERM/SIGHUP delivered to the worker (e.g.
    when uWSGI hits max-requests, or recycles a different worker that
    happens to share the group) reaches the ETL too — which made starting
    one source's ETL appear to stop another's.

    stdout/stderr: when `datasource_name` is given, both go to a fresh spool
    file under STARCUBES_LOG_DIR, exported to the child as
    STARCUBES_ETL_LOG_PATH. Next to it the spawner writes the sidecar
    `<spool>.json` naming the log's `kind` and `datasource_id` (core/log_ship.py)
    *before* the child starts, so even a process that dies during import can
    be attributed. The child ships the file into `starcubes_meta.log_line` and
    removes it (sources/apps.py starts the shipper); a crashed child's file is
    shipped by the node_agent. A regular file opened O_APPEND also keeps the
    original SIGPIPE-avoidance property: unlike the worker's own stdout pipe,
    it never raises EPIPE and is unaffected by uWSGI worker recycling. Without
    a name, output goes to DEVNULL.

    Returns (Popen, log_path | None).
    """
    run_env = dict(env if env is not None else os.environ)
    run_env.setdefault("PYTHONUNBUFFERED", "1")
    run_env.setdefault("PYTHONFAULTHANDLER", "1")

    log_path = None
    out_fd = subprocess.DEVNULL
    opened_fd = None
    if datasource_name:
        if kind is None:
            raise ValueError("spawn_etl_subprocess: a logged spawn needs its kind")
        slug = slugify_ds(datasource_name)
        d = log_dir()
        os.makedirs(d, exist_ok=True)
        log_path = os.path.join(
            d, f"etl-{slug}-{datetime.now():%Y%m%d-%H%M%S}-{os.urandom(2).hex()}.log")
        opened_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        write_sidecar(log_path, kind=kind, datasource_id=datasource_id)
        out_fd = opened_fd
        run_env["STARCUBES_ETL_LOG_PATH"] = log_path

    spawn_cmd = list(cmd)
    if spawn_cmd and spawn_cmd[0] == PYTHON_EXE and "-u" not in spawn_cmd[:2]:
        spawn_cmd = [spawn_cmd[0], "-u", *spawn_cmd[1:]]

    try:
        proc = subprocess.Popen(
            spawn_cmd,
            env=run_env,
            stdin=subprocess.DEVNULL,
            stdout=out_fd,
            stderr=out_fd,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        if opened_fd is not None:
            os.close(opened_fd)  # the child holds its own dup via inheritance
    return proc, log_path
