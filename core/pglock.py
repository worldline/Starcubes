"""Dedicated Postgres connections, and the one registry of advisory-lock keys.

Two things every cross-process interlock in starcubes needs, kept in one place
so the next one does not reinvent them.

**A connection of its own.** A session-scoped advisory lock (or anything else
whose meaning is "this backend is still here") is worthless on a pooled
connection: `core.source._install_fork_safety` invalidates records across
pids, `pool_recycle` drops the session after thirty minutes, and a
`pool_pre_ping` reconnect carries on without the lock and without a word. So
`open_dedicated` builds a `NullPool` engine and keeps exactly one connection.

**Keepalives on that connection.** Postgres releases a session lock when it
notices the client is gone. A killed process closes its socket and the server
notices at once; a container whose host vanished, or a network partition,
closes nothing, and without TCP keepalives the server would keep the lock for
the kernel's default of two hours. The values below bound that to roughly a
minute. libpq ignores them on a Unix-domain socket, which is correct: a local
socket cannot be partitioned.

**The key registry.** Every advisory lock in the application uses the
two-int form under one of these class ids, so `pg_locks` shows them together:

    classid 0x53544152 ("STAR")
      objid  > 0   the datasource lock, objid = datasource.id (core.preflight)
      objid    0   pipeline-alarm evaluation              (core.pipeline_alarm)
      objid   -1   bootstrap, held for the whole command  (manage.py bootstrap)
      objid   -2   applying starcubes_meta.sql            (core.schema)

    classid 0x53544144 ("STAD")
      objid  > 0   adopting a forever session, objid = datasource.id
                   (sources/forever_adopt.py, transaction-scoped)

The negative ids are free because `datasource.id` is a serial and starts at 1.
Adoption has a class of its own because its objid is a datasource id too, and
must not collide with the datasource lock a running load holds.

No Django imports — same constraint as the rest of `core/`.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Callable, Iterator, Optional

import sqlalchemy
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

logger = logging.getLogger(__name__)

LOCK_CLASS = 0x53544152
PIPELINE_EVAL_OBJID = 0
BOOTSTRAP_OBJID = -1
META_SQL_OBJID = -2
ADOPT_CLASS = 0x53544144

# idle 30 s, then a probe every 10 s, dead after 3 missed probes.
KEEPALIVE_CONNECT_ARGS = {
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
    "keepalives_count": 3,
}

_LIBPQ_DRIVERS = {"psycopg2", "psycopg"}


def keepalive_connect_args(dsn: str) -> dict:
    """The keepalive `connect_args` for a libpq-based URL, else `{}`.

    Only libpq drivers understand these keywords; handing them to anything
    else would fail the connect.
    """
    url = make_url(dsn)
    if url.get_backend_name() != "postgresql":
        return {}
    if url.get_driver_name() not in _LIBPQ_DRIVERS:
        return {}
    return dict(KEEPALIVE_CONNECT_ARGS)


class DedicatedConnection:
    """One connection on its own `NullPool` engine, closed together."""

    def __init__(self, engine, conn):
        self.engine = engine
        self.conn = conn

    def close(self) -> None:
        try:
            self.conn.close()
        finally:
            self.engine.dispose()


def open_dedicated(dsn: str, *, application_name: Optional[str] = None
                   ) -> DedicatedConnection:
    """Open a connection nothing else shares, with keepalives where possible."""
    connect_args = keepalive_connect_args(dsn)
    if application_name and make_url(dsn).get_backend_name() == "postgresql":
        # 63 bytes is Postgres' NAMEDATALEN limit; longer is silently cut.
        connect_args["application_name"] = application_name[:63]
    engine = sqlalchemy.create_engine(dsn, poolclass=NullPool,
                                      connect_args=connect_args)
    try:
        conn = engine.connect()
    except Exception:
        engine.dispose()
        raise
    return DedicatedConnection(engine, conn)


@contextmanager
def session_lock(dsn: str, objid: int, *, classid: int = LOCK_CLASS,
                 on_wait: Optional[Callable[[], None]] = None
                 ) -> Iterator[DedicatedConnection]:
    """Hold a blocking session advisory lock for the duration of the block.

    Tries first without waiting so `on_wait` can say why nothing is
    happening — a container waiting on another container's bootstrap should
    not look hung. The lock goes with the connection, so an exception or a
    killed process releases it too.
    """
    handle = open_dedicated(dsn)
    params = {"c": classid, "o": int(objid)}
    try:
        got = handle.conn.execute(
            text("SELECT pg_try_advisory_lock(:c, :o)"), params).scalar()
        if not got:
            if on_wait is not None:
                on_wait()
            handle.conn.execute(text("SELECT pg_advisory_lock(:c, :o)"), params)
        # The lock is session-scoped and survives the commit; an open
        # transaction would pin a snapshot for as long as the block runs.
        handle.conn.commit()
    except Exception:
        handle.close()
        raise
    try:
        yield handle
    finally:
        try:
            handle.conn.execute(text("SELECT pg_advisory_unlock(:c, :o)"), params)
            handle.conn.commit()
        except Exception:
            logger.debug("advisory unlock failed; closing the connection "
                         "releases it anyway", exc_info=True)
        finally:
            handle.close()
