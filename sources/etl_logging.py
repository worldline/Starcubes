"""Logging setup for a spawned/CLI ETL process.

Used by ``sources/management/commands/etl_run.py`` and
``etl_resume_forever.py``. ``core/`` itself must not import Django (see
`docs/etl-engine.md`, "ETL logging"), so it only ever does ``logging.getLogger(__name__)`` — this
module is where the handlers those loggers write through get attached, at
the one process entry point that is allowed to know about Django settings.
"""
from __future__ import annotations

import faulthandler
import logging
import signal
import sys

_configured = False


class _BestEffortStreamHandler(logging.StreamHandler):
    """A StreamHandler that quietly stops emitting once its stream breaks.

    Used for the optional mirror onto the container's PID-1 stdout. A dead
    reader on that pipe raises BrokenPipeError/OSError on write — self
    log-configuration must never be able to crash the ETL over a logging
    side channel.
    """

    def emit(self, record):
        try:
            super().emit(record)
        except (BrokenPipeError, OSError):
            try:
                self.stream.close()
            except Exception:
                pass
            # Detach: stop trying on every subsequent record.
            root = logging.getLogger()
            if self in root.handlers:
                root.removeHandler(self)


def configure_etl_logging() -> logging.Logger:
    """Attach handlers for a spawned/CLI ETL process. Idempotent per process.

    Always adds a stderr handler (this is what sources/etl_spawn.py points
    at the per-run log file for a spawned process; for an interactive
    ``manage.py etl_run`` it's just the terminal). When
    ``STARCUBES_ETL_LOG_STDOUT`` is set, additionally mirrors onto the
    container's PID-1 stdout so ``podman logs``/``docker logs`` show ETL
    activity — safe here (unlike the raw fd inheritance this replaced)
    because it targets PID 1's own stdout, not a uWSGI worker's fd, and
    SIGPIPE is SIG_IGN by default in CPython so a dead reader yields a
    catchable exception rather than killing the process.

    Returns the root logger, already configured; core/etl.py's
    ``logging.getLogger(__name__)`` propagates to it.
    """
    global _configured
    from django.conf import settings

    root = logging.getLogger()
    if not _configured:
        root.setLevel(getattr(logging, settings.STARCUBES_LOG_LEVEL, logging.INFO))
        fmt = logging.Formatter(
            "%(asctime)s %(levelname)-5s %(name)s pid=%(process)d %(message)s",
            datefmt="%d/%b/%Y %H:%M:%S")

        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setFormatter(fmt)
        root.addHandler(stderr_handler)

        if getattr(settings, "STARCUBES_ETL_LOG_STDOUT", False):
            try:
                stdout_stream = open("/proc/1/fd/1", "a", buffering=1, errors="replace")
                stdout_handler = _BestEffortStreamHandler(stdout_stream)
                stdout_handler.setFormatter(fmt)
                root.addHandler(stdout_handler)
            except OSError:
                pass  # not running under a container init that exposes this

        # Silence noisy third-party loggers rather than the whole root.
        logging.getLogger("sqlalchemy").setLevel(logging.WARNING)
        logging.getLogger("urllib3").setLevel(logging.WARNING)

        # A handler write error must never propagate into the ETL.
        logging.raiseExceptions = False

        faulthandler.enable()
        if hasattr(signal, "SIGUSR1"):
            faulthandler.register(signal.SIGUSR1, all_threads=True, chain=True)

        _configured = True

    return logging.getLogger("sources.etl")


_shipper = None


def start_log_shipper() -> None:
    """Ship this process's spool file into starcubes_meta.log_line, if it has one.

    A process spawned by sources/etl_spawn.py finds its spool file in
    STARCUBES_ETL_LOG_PATH, with the sidecar the spawner wrote next to it.
    Anything else — uwsgi, a command run by hand in a terminal — has neither,
    and this does nothing. Called from SourcesConfig.ready(), so every
    management command a spawn can start gets it without each one having to
    remember (the rollup commands never called configure_etl_logging).

    The final flush runs at interpreter exit. A forked multiprocessing worker
    inherits neither the thread nor the atexit handler (it leaves through
    os._exit), so the ETL's workers cannot ship the parent's file twice.
    """
    global _shipper
    if _shipper is not None:
        return
    import atexit
    import os

    path = os.environ.get("STARCUBES_ETL_LOG_PATH")
    if not path:
        return
    from django.conf import settings

    from core.log_ship import LogShipper, read_sidecar

    dsn = settings.STARCUBES_POSTGRES_DSN
    if not dsn or read_sidecar(path) is None:
        return
    try:
        _shipper = LogShipper(dsn, path).start()
    except (OSError, ValueError):
        # Another process already ships this file (it holds the lock), or
        # the file is gone. The node_agent's sweep is the backstop either way.
        _shipper = None
        return
    atexit.register(_shipper.stop)
