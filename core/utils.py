"""Shared utilities used across core, views, and management commands.

This module must not import Django — it's the same constraint as core/.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone


def _parse_dt(s: str) -> datetime:
    """Parse an ISO-ish datetime string. Returns a timezone-aware UTC datetime."""
    s = s.strip().replace(" ", "T")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _pid_alive(pid: int) -> bool:
    """Return True if the process with *pid* is still running."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def tail_file(path: str, max_bytes: int = 8192, max_lines: int = 120) -> str | None:
    """Return the last `max_lines` lines (within `max_bytes`) of a file.

    Used by core/etl.py to capture a failed run's traceback into
    etl_run.log_tail, so it's visible from the GUI without filesystem
    access to the log volume. Returns None if the file can't be read.
    """
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            start = max(0, size - max_bytes)
            f.seek(start)
            data = f.read()
    except OSError:
        return None
    text = data.decode("utf-8", errors="replace")
    if start > 0:
        # We seeked into the middle of the file — drop a possibly
        # truncated first line.
        text = text.split("\n", 1)[-1]
    lines = text.splitlines()
    return "\n".join(lines[-max_lines:])