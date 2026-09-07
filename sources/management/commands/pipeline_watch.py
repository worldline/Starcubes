"""Evaluate pipeline alarms from outside the ETL process.

The half of U7 that cannot be solved inside the process being watched: a
wedged forever loop cannot report that it is wedged, and a datasource whose
ETL has stopped entirely produces no loads for an in-loop evaluator to ride
on. Something outside has to look.

`docs/etl-automation.md` names the two honest options and their price. This
is option one — a small loop of its own. It is the only thing that keeps
working when nobody is using the app, and it costs a process to supervise,
restart and reason about, which is exactly what this architecture had
avoided until now. Option two, the request-path middleware, ships alongside
it (`starcubes/middleware.py`) and covers deployments that do not run this.

Touches `starcubes_meta` only — no `Source.load`, no source connection, no
fact query — so it keeps working for a datasource whose source database is
down, which is one of the states it exists to report.
"""
from __future__ import annotations

import logging
import signal
import time

from django.conf import settings
from django.core.management.base import BaseCommand

from core.alarm import run_alarm_cycle
from core.pipeline_alarm import EVAL_INTERVAL_S
from core.source import _get_cached_engine
from sources.alarm_notify import dispatch_transitions
from sources.etl_logging import configure_etl_logging

log = logging.getLogger("sources.pipeline_watch")

_stopping = False


def _signal_handler(_signum, _frame):
    global _stopping
    _stopping = True


class Command(BaseCommand):
    help = ("Evaluate pipeline alarms in a loop, outside the ETL process. "
            "Started by docker/entrypoint.sh; runnable by hand.")

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=int, default=EVAL_INTERVAL_S,
                            help=f"seconds between cycles "
                                 f"(default {EVAL_INTERVAL_S}, env "
                                 f"STARCUBES_PIPELINE_ALARM_INTERVAL_S)")
        parser.add_argument("--once", action="store_true",
                            help="run a single cycle and exit")
        parser.add_argument("--postgres-dsn", default=None,
                            help="SQLAlchemy URL for Postgres "
                                 "(default: settings.STARCUBES_POSTGRES_DSN)")

    def handle(self, *args, **options):
        configure_etl_logging()
        dsn = options["postgres_dsn"] or settings.STARCUBES_POSTGRES_DSN
        if not dsn:
            self.stderr.write(
                "Error: --postgres-dsn or STARCUBES_POSTGRES_DSN is required")
            self.stderr.flush()
            raise SystemExit(1)

        signal.signal(signal.SIGTERM, _signal_handler)
        signal.signal(signal.SIGINT, _signal_handler)

        meta = _get_cached_engine(dsn)
        interval = max(1, int(options["interval"]))
        if not options["once"]:
            log.info("pipeline_watch: every %ds", interval)

        while True:
            self._cycle(meta, dsn)
            if options["once"] or _stopping:
                break
            # Interruptible sleep: a container stop should not wait out a
            # whole interval before the process exits.
            for _ in range(interval):
                if _stopping:
                    break
                time.sleep(1)
            if _stopping:
                break
        if not options["once"]:
            log.info("pipeline_watch: stopped")

    def _cycle(self, meta, dsn: str) -> None:
        """One evaluation. Never raises — a supervisor that dies on a
        transient database error is worse than no supervisor, because it
        looks like one."""
        try:
            counts = run_alarm_cycle(meta, dsn, kinds=("pipeline",))
        except Exception:
            log.exception("pipeline alarm cycle failed, continuing")
            return
        if counts["episodes_opened"] or counts["episodes_closed"] \
                or counts["errors"]:
            log.info("pipeline alarms: %d rule(s), opened=%d closed=%d "
                     "errors=%d", counts["alarms"],
                     counts["episodes_opened"], counts["episodes_closed"],
                     counts["errors"])
        if not counts.get("transitions"):
            return
        try:
            notify = dispatch_transitions(counts["transitions"], meta)
            log.info("notify: email=%d xmpp=%d pagerduty=%d errors=%d",
                     notify["email"], notify["xmpp"], notify["pagerduty"],
                     notify["errors"])
        except Exception:
            log.exception("pipeline alarm notification failed, continuing")
