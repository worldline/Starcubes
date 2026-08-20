"""Helpers for spawning detached ETL subprocesses.

Shared between sources/views.py (GUI start) and the etl_resume_forever
management command (container-restart resume).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from datetime import datetime


# When running under uWSGI, sys.executable points to the uwsgi binary,
# not Python.  Using it to spawn a Django management command makes uwsgi
# try to parse Django's flags as its own (e.g. --name clashes with
# --namespace).  Fall back to a real Python interpreter in that case.
PYTHON_EXE = sys.executable if "uwsgi" not in os.path.basename(sys.executable) else (shutil.which("python3") or "python")

_SLUG_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_LOG_NAME_RE = re.compile(r"^etl-(?P<slug>[A-Za-z0-9_.-]+)-\d{8}-\d{6}-[0-9a-f]{4}\.log$")


def log_dir() -> str:
    """Directory ETL run logs are written to (STARCUBES_LOG_DIR)."""
    from django.conf import settings
    return settings.STARCUBES_LOG_DIR


def slugify_ds(name: str) -> str:
    """Filesystem/regex-safe stand-in for a datasource name in a log filename."""
    return _SLUG_RE.sub("_", name).strip("_") or "ds"


def prune_etl_logs(slug: str) -> int:
    """Delete this datasource's oldest log files beyond STARCUBES_LOG_KEEP,
    and any older than STARCUBES_LOG_MAX_DAYS regardless of count.

    Best-effort: a listdir/stat/unlink failure (e.g. log dir not yet
    mounted) never blocks a spawn. Returns the number of files removed.
    """
    from django.conf import settings

    keep = settings.STARCUBES_LOG_KEEP
    max_age_s = settings.STARCUBES_LOG_MAX_DAYS * 86400
    now = datetime.now().timestamp()
    removed = 0
    try:
        d = log_dir()
        prefix = f"etl-{slug}-"
        entries = []
        for name in os.listdir(d):
            if not (name.startswith(prefix) and name.endswith(".log")):
                continue
            path = os.path.join(d, name)
            try:
                mtime = os.stat(path).st_mtime
            except OSError:
                continue
            entries.append((mtime, path))
        entries.sort(reverse=True)  # newest first
        for i, (mtime, path) in enumerate(entries):
            if i >= keep or (now - mtime) > max_age_s:
                try:
                    os.unlink(path)
                    removed += 1
                except OSError:
                    pass
    except OSError:
        pass
    return removed


def spawn_etl_subprocess(cmd, env=None, *, datasource_name: str | None = None):
    """Spawn a detached ETL subprocess that survives uWSGI worker recycling.

    Without start_new_session=True the child stays in the worker's session
    and process group, so a SIGTERM/SIGHUP delivered to the worker (e.g.
    when uWSGI hits max-requests, or recycles a different worker that
    happens to share the group) reaches the ETL too — which made starting
    one source's ETL appear to stop another's.

    stdout/stderr: when `datasource_name` is given, both are pointed at a
    fresh per-run log file under STARCUBES_LOG_DIR (STARCUBES_ETL_LOG_PATH
    is also exported to the child, which core/etl.py records on the
    etl_run row and sources/etl_logging.py's configure_etl_logging()
    reads to decide where its own logging handler writes). A regular file
    opened O_APPEND preserves the original SIGPIPE-avoidance property this
    function existed for — unlike the worker's own stdout/stderr pipe, a
    file never raises EPIPE and is unaffected by uWSGI log rotation or
    worker recycling/exit. Without a name (should not happen from the two
    GUI call sites or etl_resume_forever, all of which always have a
    datasource name), falls back to the historical DEVNULL.

    Returns (Popen, log_path | None).
    """
    run_env = dict(env if env is not None else os.environ)
    run_env.setdefault("PYTHONUNBUFFERED", "1")
    run_env.setdefault("PYTHONFAULTHANDLER", "1")

    log_path = None
    out_fd = subprocess.DEVNULL
    opened_fd = None
    if datasource_name:
        slug = slugify_ds(datasource_name)
        d = log_dir()
        os.makedirs(d, exist_ok=True)
        prune_etl_logs(slug)
        log_path = os.path.join(
            d, f"etl-{slug}-{datetime.now():%Y%m%d-%H%M%S}-{os.urandom(2).hex()}.log")
        opened_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
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
