"""Evaluate threshold alarms once, over every datasource.

This is the *manual* entry point. In normal operation alarms are evaluated
by the ETL itself — ``etl_run`` calls ``run_alarm_cycle`` for its own
datasource after every load, because a load is the only thing that moves the
data high watermark an alarm window slides with. There is deliberately no
forever loop here any more: a second, independently timed evaluator would
race the ETL for the same episodes and answer questions whose data had not
changed.

    manage.py alarm_run          # one cycle over every enabled alarm
    manage.py alarm_run --once   # the same thing, spelled out

The evaluation logic itself is Django-free in ``core/alarm.py``.
"""
from __future__ import annotations

import os
import sys

from django.conf import settings
from django.core.management.base import BaseCommand

from core.alarm import run_alarm_cycle
from core.source import _get_cached_engine
from sources.alarm_notify import dispatch_transitions
from sources.etl_logging import configure_etl_logging


class Command(BaseCommand):
    help = "Evaluate all enabled threshold alarms once."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true",
                            help="run one evaluation cycle and exit "
                                 "(the default, and the only mode)")
        parser.add_argument("--postgres-dsn", default=None,
                            help="SQLAlchemy URL for Postgres "
                                 "(default: settings.STARCUBES_POSTGRES_DSN)")

    def handle(self, *args, **options):
        log = configure_etl_logging()
        log_path = os.environ.get("STARCUBES_ETL_LOG_PATH")
        log.info("alarm_run start: argv=%s pid=%d log=%s",
                 " ".join(sys.argv[1:]), os.getpid(), log_path or "<stderr>")

        dsn = options["postgres_dsn"] or settings.STARCUBES_POSTGRES_DSN
        if not dsn:
            self.stderr.write("Error: --postgres-dsn or STARCUBES_POSTGRES_DSN is required")
            self.stderr.flush()
            raise SystemExit(1)

        meta = _get_cached_engine(dsn)
        counts = run_alarm_cycle(meta, dsn)
        self.stdout.write(
            f"alarms={counts['alarms']} "
            f"windows={counts['windows']} breaches={counts['breaches']} "
            f"episodes +{counts['episodes_opened']}"
            f"/~{counts['episodes_extended']}"
            f"/closed {counts['episodes_closed']} "
            f"errors={counts['errors']}")

        # Transports run on the Django side, next to the evaluator's own
        # try/except: a notification failure neither re-runs an evaluation
        # nor stops the command with a non-zero exit.
        if counts.get("transitions"):
            try:
                nc = dispatch_transitions(counts["transitions"], meta)
                self.stdout.write(
                    f"notify: email={nc['email']} xmpp={nc['xmpp']} "
                    f"pagerduty={nc['pagerduty']} errors={nc['errors']}")
            except Exception:
                log.exception("alarm notification failed, continuing")
