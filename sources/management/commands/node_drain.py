"""Hand this container's work over before it shuts down.

    manage.py node_drain [--timeout SECONDS]

Called by docker/entrypoint.sh when the container receives SIGTERM, before
uwsgi is stopped. With several containers on one database, the forever
sessions running here should move to another container at once, not after
the process is killed and its lease times out.

1. **Stop this container's node_agent first.** Otherwise it would see the
   sessions released in step 2 and adopt them right back into the container
   that is going away.
2. **SIGTERM every run process of this container**, found through the view
   `run` (alive, same `instance_id`) and verified against its lease before
   the signal (core/lease.py). A forever loop answers by finishing its
   current load and writing `released` (migration 0044), which wakes every
   other container's node_agent to adopt it. A single run or a rollup
   command stops at its next chunk boundary and closes its row as failed.
   Runs that appear while draining get the same signal.
3. **Wait** until none is left or `--timeout` (default
   `STARCUBES_DRAIN_TIMEOUT_S`, 25 s) passes. What is still running then is
   killed with the container; its lease ends with its connection, and the
   other containers take the forever session over by the dead-lease rule —
   later, and with one failed load, but correctly.

The container runtime's stop timeout has to be longer than this timeout, or
step 3 is cut short: podman's default is 10 s (`--stop-timeout`), Kubernetes'
`terminationGracePeriodSeconds` 30 s.

Never fails the shutdown: every error is logged and the command exits 0.
"""
from __future__ import annotations

import logging
import os
import signal
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from sqlalchemy import bindparam, text

from core.lease import APPLICATION_NAME_PREFIX, verify_local_pid
from core.node import instance_id, short_instance_id
from core.source import _get_cached_engine
from sources.etl_logging import configure_etl_logging

log = logging.getLogger("sources.node_drain")

_RUN_KINDS = ("etl", "backfill", "forever",
              "rollup_advise", "rollup_build", "rollup_verify")


class Command(BaseCommand):
    help = "Hand this container's runs over before shutdown (SIGTERM + wait)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--timeout", type=int,
            default=int(os.environ.get("STARCUBES_DRAIN_TIMEOUT_S", "25")),
            help="seconds to wait for runs to end (default "
                 "STARCUBES_DRAIN_TIMEOUT_S or 25)")
        parser.add_argument("--postgres-dsn", default=None,
                            help="SQLAlchemy URL for Postgres "
                                 "(default: settings.STARCUBES_POSTGRES_DSN)")

    def handle(self, *args, **options):
        configure_etl_logging()
        dsn = options["postgres_dsn"] or settings.STARCUBES_POSTGRES_DSN
        if not dsn:
            return
        try:
            self._drain(_get_cached_engine(dsn), max(0, options["timeout"]))
        except Exception:
            log.exception("node_drain: failed — shutting down without a "
                          "hand-over; the dead-lease rule still applies")

    def _drain(self, meta, timeout: int) -> None:
        me = instance_id()
        self._stop_local_agent(meta)
        signalled: set = set()
        deadline = time.monotonic() + timeout
        while True:
            with meta.begin() as conn:
                rows = [dict(r) for r in conn.execute(text(
                    "SELECT kind, id, pid, hostname, instance_id, lease_pid, "
                    "       lease_start "
                    "  FROM starcubes_meta.run "
                    " WHERE alive AND instance_id = :me AND kind IN :kinds"
                ).bindparams(bindparam("kinds", expanding=True)),
                    {"me": me, "kinds": list(_RUN_KINDS)}).mappings()]
                for row in rows:
                    key = (row["kind"], row["id"])
                    if key in signalled:
                        continue
                    signalled.add(key)
                    if not verify_local_pid(conn, row):
                        log.warning("node_drain: %s %s: pid %s does not hold "
                                    "its lease — not signalling it",
                                    row["kind"], row["id"], row["pid"])
                        continue
                    try:
                        os.kill(int(row["pid"]), signal.SIGTERM)
                        log.info("node_drain: SIGTERM %s %s (pid %s)",
                                 row["kind"], row["id"], row["pid"])
                    except ProcessLookupError:
                        pass
            if not rows:
                log.info("node_drain: nothing left running in this container")
                return
            if time.monotonic() >= deadline:
                log.warning("node_drain: %d run(s) still alive after %d s — "
                            "leaving them to the container stop; other "
                            "containers adopt their forever sessions once the "
                            "leases end", len(rows), timeout)
                return
            time.sleep(1)

    def _stop_local_agent(self, meta) -> None:
        """SIGTERM this container's node_agent, found by its lease's name."""
        prefix = f"{APPLICATION_NAME_PREFIX}{short_instance_id()}/"
        with meta.begin() as conn:
            names = conn.execute(text(
                "SELECT application_name FROM pg_stat_activity "
                " WHERE application_name LIKE :p AND application_name LIKE '%/node_agent'"
            ), {"p": prefix + "%"}).scalars().all()
        for name in names:
            try:
                pid = int(name[len(prefix):].split("/", 1)[0])
                os.kill(pid, signal.SIGTERM)
                log.info("node_drain: stopped the local node_agent (pid %s) so "
                         "it cannot adopt the sessions released here", pid)
            except (ValueError, ProcessLookupError):
                continue
