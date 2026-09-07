"""Pre-load checks for one datasource.

Everything that has to be true *before* an `import_fact` starts, expressed
as catalogue queries and one round trip against the source. The rules this
module follows come from `docs/etl-automation.md`:

    Preflight detects and blocks. It only repairs what is idempotent and
    finishes in milliseconds. Everything else is reported and waits for a
    human gesture.

So a missing fact/dim table is created here (the creators are idempotent and
the alternative is a foreign-key violation deep inside the transform), while
a missing index is only ever *reported* — `create_indices` takes ACCESS
EXCLUSIVE on the partitioned parent and there is no `CONCURRENTLY` anywhere
in this repo, so an unattended index build on a billion-row fact table would
block every COPY for hours while looking, from the outside, exactly like a
wedged ETL.

Django-free, like everything in `core/`. Returns a structured report rather
than raising — the shape follows `core/audit.py`: plain dicts, ISO strings,
`None` for "this probe failed" rather than a bogus zero. `import_fact` is
what turns a `block` into a failed run; nothing in here decides that.
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

import sqlalchemy
from sqlalchemy import text
from sqlalchemy.pool import NullPool

from .schema import create_dim_tables, create_fact
from .source import Source

logger = logging.getLogger(__name__)

CHECK_OK = "ok"
CHECK_WARN = "warn"
CHECK_BLOCK = "block"

# Meta columns the running code needs. Probed against information_schema so a
# code update that skipped `manage.py bootstrap` fails with one sentence
# instead of an UndefinedColumn out of an unrelated query (N16).
_REQUIRED_META_COLUMNS: tuple[tuple[str, str], ...] = (
    ("alarm", "severity"),
    ("datasource", "retention_floor"),
    ("datasource", "dedupe_from"),
)

# The index check is time-aware on purpose: the first load deliberately runs
# without indices (fastest COPY) and the operator builds them afterwards, so
# "no indices" is the legitimate initial state. The real complaint is "this
# has been syncing for weeks and the indices are still missing", which needs
# both a row count and an age to express.
_INDEX_WARN_ROWS = int(os.environ.get("STARCUBES_INDEX_WARN_ROWS", 1_000_000))
_INDEX_WARN_HOURS = float(os.environ.get("STARCUBES_INDEX_WARN_HOURS", 24))

# How far the stored high watermark may stand above the fact tip before it is
# worth a line. A quiet source legitimately widens this gap, so it can only
# ever warn (see _check_watermark_tip for the second reason).
_HWM_GAP_WARN_S = float(os.environ.get("STARCUBES_HWM_GAP_WARN_S", 86400))


def _check(code: str, level: str, message: str, **detail) -> dict:
    return {"code": code, "level": level, "message": message,
            "detail": detail}


def relation_exists(conn, schema: str, relname: str) -> bool:
    """True if `schema.relname` exists as a relation.

    Shared with core/etl.py's partition maintenance, which needs the same
    probe before a TRUNCATE/DROP.
    """
    return bool(conn.execute(text(
        "SELECT 1 FROM pg_class c "
        "  JOIN pg_namespace n ON n.oid = c.relnamespace "
        " WHERE n.nspname = :s AND c.relname = :t"
    ), {"s": schema, "t": relname}).scalar())


def _check_schema_objects(source: Source, dest_engine, *,
                          repair: bool) -> tuple[dict, list[str]]:
    """U2 — fact table and dim tables exist.

    `create_fact` / `create_dim_tables` are fully idempotent but are only
    ever invoked when a datasource is created or edited; `import_fact`
    assumes they ran. A dropped dim table otherwise surfaces late and badly,
    as a foreign-key violation or a KeyError inside the transform.
    """
    schema = source.schema_name
    wanted_dims = [d.dim_table for d in source.primary_dimensions()
                   if d.dim_table]
    repairs: list[str] = []
    with dest_engine.connect() as conn:
        fact_missing = not relation_exists(conn, schema, "fact")
        missing_dims = [t for t in wanted_dims
                        if not relation_exists(conn, schema, t)]

    if not fact_missing and not missing_dims:
        return _check("schema_objects", CHECK_OK,
                      f"{schema}.fact and {len(wanted_dims)} dim table(s) "
                      f"present",
                      dim_tables=wanted_dims), repairs

    missing = (["fact"] if fact_missing else []) + missing_dims
    if not repair:
        return _check("schema_objects", CHECK_BLOCK,
                      f"missing in schema {schema}: {', '.join(missing)}",
                      missing=missing), repairs

    # Creating a fact table mid-life is never routine — say so loudly, and
    # record it so the GUI can show that this run had to repair its own
    # schema. The cost of auto-creation is that a mistyped schema_name now
    # yields a second empty schema instead of a loud failure; the WARNING
    # and the stats entry are what keep that visible.
    if fact_missing:
        logger.warning("%s: %s.fact is missing — creating it",
                       source.name, schema)
        create_fact(source, dest_engine)
        repairs.append(f"created {schema}.fact")
    if missing_dims:
        logger.warning("%s: dim table(s) missing — creating: %s",
                       source.name, ", ".join(missing_dims))
        created = create_dim_tables(source, dest_engine, quiet=True)
        repairs.extend(f"created {schema}.{t}" for t in created)
    return _check("schema_objects", CHECK_WARN,
                  f"created missing object(s) in schema {schema}: "
                  f"{', '.join(missing)}",
                  missing=missing, repaired=True), repairs


def _check_meta_migrations(meta_engine) -> dict:
    """N16 — the metadata schema is at least as new as the code."""
    with meta_engine.begin() as conn:
        present = {(r[0], r[1]) for r in conn.execute(text(
            "SELECT table_name, column_name "
            "  FROM information_schema.columns "
            " WHERE table_schema = 'starcubes_meta'"
        )).all()}
    missing = [f"{t}.{c}" for (t, c) in _REQUIRED_META_COLUMNS
               if (t, c) not in present]
    if not missing:
        return _check("meta_migrations", CHECK_OK,
                      "starcubes_meta is up to date")
    return _check("meta_migrations", CHECK_BLOCK,
                  f"starcubes_meta is behind the code (missing: "
                  f"{', '.join(missing)}) — run `manage.py bootstrap`",
                  missing=missing)


def _expected_indices(source: Source) -> tuple[list[str], list[str]]:
    """The index and FK names `create_indices` builds, in its own order."""
    names = [f"ix_{ix.column_name}" for ix in source.indices]
    names += [f"ix_{d.fact_column}" for d in source.dimensions if d.dim_table]
    fks = [f"fk_{d.fact_column}" for d in source.primary_dimensions()]
    return names, fks


def _check_indices(source: Source, dest_engine, meta_engine) -> dict:
    """U10 — the indices and foreign keys `create_indices` would build.

    Report only, never build. Suppressed while the datasource is young or
    small, so the legitimate initial-load state does not nag; escalates to a
    warning once real volume has been loaded over real time without them.
    """
    schema = source.schema_name
    want_ix, want_fk = _expected_indices(source)
    with dest_engine.connect() as conn:
        if not relation_exists(conn, schema, "fact"):
            return _check("indices", CHECK_OK,
                          "skipped — fact table does not exist yet")
        have_ix = set(conn.execute(text(
            "SELECT i.relname "
            "  FROM pg_index x "
            "  JOIN pg_class i ON i.oid = x.indexrelid "
            "  JOIN pg_class t ON t.oid = x.indrelid "
            "  JOIN pg_namespace n ON n.oid = t.relnamespace "
            " WHERE n.nspname = :s AND t.relname = 'fact'"
        ), {"s": schema}).scalars().all())
        have_fk = set(conn.execute(text(
            "SELECT conname FROM pg_constraint "
            " WHERE conrelid = CAST(:rel AS regclass) AND contype = 'f'"
        ), {"rel": f"{schema}.fact"}).scalars().all())

    missing_ix = [n for n in want_ix if n not in have_ix]
    missing_fk = [n for n in want_fk if n not in have_fk]
    if not missing_ix and not missing_fk:
        return _check("indices", CHECK_OK,
                      f"{len(want_ix)} index/indices and {len(want_fk)} "
                      f"foreign key(s) present")

    with meta_engine.begin() as conn:
        row = conn.execute(text(
            "SELECT coalesce(sum(rows), 0) AS rows, min(started_at) AS since "
            "  FROM starcubes_meta.etl_run "
            " WHERE datasource_id = :ds AND status = 'ok'"
        ), {"ds": source.id}).mappings().first()
    loaded = int((row or {}).get("rows") or 0)
    since = (row or {}).get("since")
    age_h = 0.0
    if since is not None:
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        age_h = (datetime.now(timezone.utc) - since).total_seconds() / 3600.0

    detail = {"missing_indices": missing_ix, "missing_foreign_keys": missing_fk,
              "rows_loaded": loaded, "age_hours": round(age_h, 1)}
    if loaded >= _INDEX_WARN_ROWS and age_h >= _INDEX_WARN_HOURS:
        return _check("indices", CHECK_WARN,
                      f"{len(missing_ix) + len(missing_fk)} index/FK "
                      f"missing after {loaded} rows over {age_h:.0f}h — run "
                      f"`manage.py create_indices --name {source.name}` "
                      f"(missing: "
                      f"{', '.join(missing_ix + missing_fk)})",
                      **detail)
    return _check("indices", CHECK_OK,
                  f"{len(missing_ix) + len(missing_fk)} index/FK not built "
                  f"yet — normal during the initial load",
                  **detail)


def _check_source_projection(source: Source, source_engine) -> dict:
    """U5b — the upstream columns behind source_expr / base_table still exist.

    One `WHERE 1=0` round trip turns a rename that would otherwise fail in
    the middle of the extract loop — after the run row is written and the
    workers are up — into a refusal with a column name in it.
    """
    if source_engine is None:
        # Either the caller did not want a source round trip, or it could
        # not even build the engine (a missing driver, an unparseable DSN).
        # Both are "not checked", and neither is this check's finding.
        return _check("source_projection", CHECK_OK,
                      "skipped — no source connection available")
    probe = f"SELECT * FROM ({source.import_query}) q WHERE 1=0"
    try:
        with source_engine.begin() as src:
            # Two different failures live here and they need opposite
            # answers. A source that cannot be reached at all is the
            # extract's own problem and it reports it better than we can, so
            # that only warns. A query the source *rejects* is the drift this
            # check exists for — a renamed or dropped column raises exactly
            # like a connection error, and warning about it would let the run
            # proceed and die mid-extract, which is what we are here to stop.
            # Opening the connection separates the two.
            try:
                got = [str(c) for c in src.execute(text(probe)).keys()]
            except Exception as exc:
                return _check("source_projection", CHECK_BLOCK,
                              f"the source rejected the import query — "
                              f"check base_table / source_expr against the "
                              f"source schema: {exc}",
                              error=str(exc))
    except Exception as exc:
        return _check("source_projection", CHECK_WARN,
                      f"source not reachable for the projection probe: {exc}",
                      error=str(exc))

    have = {c.lower() for c in got}
    want = [c.name for c in source.fact_columns]
    missing = [c for c in want if c.lower() not in have]
    extra = sorted(have - {c.lower() for c in want})
    if missing:
        return _check("source_projection", CHECK_BLOCK,
                      f"source no longer projects: {', '.join(missing)} "
                      f"— check base_table / source_expr against the source "
                      f"schema",
                      missing=missing, extra=extra)
    return _check("source_projection", CHECK_OK,
                  f"source projects all {len(want)} fact column(s)",
                  extra=extra)


def _check_watermark_tip(source: Source, dest_engine, meta_engine) -> dict:
    """U1 — the stored high watermark against the fact tip.

    A fact tip materially *below* the watermark means rows disappeared
    without the watermark following: a manual DELETE, a restore, a partition
    dropped outside the GUI. Two reasons this can only ever warn:

    * A quiet source widens the same gap legitimately — the window's upper
      bound is where the source was *read to*, not where rows arrived.
    * With `watermark_fact_expr` set, the tip (`partition_column`) and the
      watermark are two different clocks on the same rows. `partition_column`
      is used anyway: it is BRIN-indexed and partition-pruned, while
      `max(watermark_fact_expr)` is the expensive scan docs/etl-audit.md
      flags — and the delete gesture this looks for runs on
      `partition_column` too.
    """
    schema = source.schema_name
    with meta_engine.begin() as conn:
        hwm = conn.execute(text(
            "SELECT last_end_time FROM starcubes_meta.etl_run "
            " WHERE datasource_id = :ds AND status = 'ok' "
            "   AND kind = 'incremental' "
            " ORDER BY finished_at DESC LIMIT 1"
        ), {"ds": source.id}).scalar()
    if hwm is None:
        return _check("watermark_tip", CHECK_OK,
                      "skipped — no high watermark stored yet")

    with dest_engine.connect() as conn:
        if not relation_exists(conn, schema, "fact"):
            return _check("watermark_tip", CHECK_OK,
                          "skipped — fact table does not exist yet")
        tip = conn.execute(text(
            f"SELECT max({source.partition_column}) FROM {schema}.fact"
        )).scalar()
    if tip is None:
        return _check("watermark_tip", CHECK_OK,
                      "skipped — fact table is empty")

    if hwm.tzinfo is None:
        hwm = hwm.replace(tzinfo=timezone.utc)
    if tip.tzinfo is None:
        tip = tip.replace(tzinfo=timezone.utc)
    gap_s = (hwm - tip).total_seconds()
    detail = {"highwatermark": hwm.isoformat(), "fact_tip": tip.isoformat(),
              "gap_s": round(gap_s, 1),
              "partition_column": source.partition_column}
    if gap_s > _HWM_GAP_WARN_S:
        return _check("watermark_tip", CHECK_WARN,
                      f"the high watermark stands {gap_s / 3600:.1f}h above "
                      f"the newest fact row — either the source stopped "
                      f"producing, or rows were removed without rewinding "
                      f"the watermark",
                      **detail)
    return _check("watermark_tip", CHECK_OK,
                  f"high watermark is {gap_s / 3600:.1f}h above the fact tip",
                  **detail)


def run_preflight(source: Source, meta_engine, dest_engine, *,
                  source_engine=None, repair: bool = True) -> dict:
    """Run every pre-load check, cheapest first, and return the report.

    ``repair=True`` lets the schema-object check call the idempotent
    creators for anything missing; ``repair=False`` (the GUI's dry run)
    reports the same finding as a block and changes nothing.

    Never raises for a *finding* — a probe that itself explodes is caught
    and reported as a warning. The caller decides what ``blocked`` means.
    """
    checks: list[dict] = []
    repairs: list[str] = []

    check, done = _check_schema_objects(source, dest_engine, repair=repair)
    checks.append(check)
    repairs.extend(done)
    checks.append(_check_meta_migrations(meta_engine))
    checks.append(_check_indices(source, dest_engine, meta_engine))
    checks.append(_check_source_projection(source, source_engine))
    checks.append(_check_watermark_tip(source, dest_engine, meta_engine))

    blocked = [c for c in checks if c["level"] == CHECK_BLOCK]
    for c in checks:
        if c["level"] == CHECK_WARN:
            logger.warning("%s: preflight %s: %s",
                           source.name, c["code"], c["message"])
        elif c["level"] == CHECK_BLOCK:
            logger.error("%s: preflight %s: %s",
                         source.name, c["code"], c["message"])
    return {"checks": checks,
            "repairs": repairs,
            "blocked": bool(blocked),
            "ran_at": datetime.now(timezone.utc).isoformat()}


def blocking_message(report: dict) -> Optional[str]:
    """One sentence naming every blocking finding, or None."""
    blocked = [c for c in report.get("checks", [])
               if c["level"] == CHECK_BLOCK]
    if not blocked:
        return None
    return "preflight blocked the run: " + "; ".join(
        f"{c['code']}: {c['message']}" for c in blocked)


# ---------------------------------------------------------------------------
# Single occupancy per datasource
# ---------------------------------------------------------------------------

# Advisory-lock class id, shared by every starcubes process. Any int32 does;
# this one spells "STAR" so it is recognisable in pg_locks.
_LOCK_CLASS = 0x53544152


class DatasourceLocked(RuntimeError):
    """Raised when somebody else already holds this datasource.

    Forever mode has been protected by `ux_etl_forever_active` and the GUI
    by its 409 all along, but a plain `manage.py etl_run --name x` from a
    shell had no interlock whatsoever and would happily run alongside a live
    forever session. That is not theoretical: auto-dimension resolution
    states single occupancy as a precondition (two parents inserting into
    one dim table race on the smallserial), and with no unique index on
    `fact` two overlapping windows silently duplicate rows.
    """


class _LockHandle:
    def __init__(self, engine, conn, datasource_id: int):
        self.engine = engine
        self.conn = conn
        self.datasource_id = datasource_id


def acquire_datasource_lock(dsn: str, datasource_id: int, *,
                            holder: str = "etl") -> _LockHandle:
    """Take the datasource's advisory lock, or raise `DatasourceLocked`.

    A Postgres advisory lock is cross-process, cross-host and released the
    moment the connection dies — no PID file, no stale state to reap.

    **It is session-scoped, and that dictates the connection.** Nothing in
    the ETL holds a connection across a run: every DB touch is a short
    `engine.begin()` block, and the extract loop even disposes the source
    pool on purpose. Taking the lock on a pooled connection would be
    silently worthless — `_install_fork_safety` invalidates records across
    pids, `pool_recycle` drops the session after 30 minutes, and a
    `pool_pre_ping` reconnect would carry on without the lock and without a
    word. So this opens its own NullPool connection and keeps exactly that
    one.

    The acquiring transaction is committed straight away: a session lock
    survives the commit, whereas leaving the transaction open would pin the
    snapshot for the whole run.
    """
    engine = sqlalchemy.create_engine(dsn, poolclass=NullPool)
    conn = engine.connect()
    try:
        got = conn.execute(text("SELECT pg_try_advisory_lock(:c, :d)"),
                           {"c": _LOCK_CLASS,
                            "d": int(datasource_id)}).scalar()
        conn.commit()
    except Exception:
        conn.close()
        engine.dispose()
        raise
    if not got:
        conn.close()
        engine.dispose()
        raise DatasourceLocked(
            f"datasource {datasource_id} is already held by another "
            f"starcubes process — refusing to run a second {holder} on it. "
            f"Stop the running ETL first.")
    return _LockHandle(engine, conn, int(datasource_id))


def release_datasource_lock(handle: Optional[_LockHandle]) -> None:
    """Give the lock back. Closing the connection would do it too; the
    explicit unlock keeps pg_locks tidy while the process is still alive."""
    if handle is None:
        return
    try:
        handle.conn.execute(text("SELECT pg_advisory_unlock(:c, :d)"),
                            {"c": _LOCK_CLASS, "d": handle.datasource_id})
        handle.conn.commit()
    except Exception:
        logger.debug("advisory unlock failed; closing the connection "
                     "releases it anyway", exc_info=True)
    finally:
        try:
            handle.conn.close()
        finally:
            handle.engine.dispose()


@contextmanager
def datasource_lock(dsn: str, datasource_id: int, *, holder: str = "etl"):
    """Scoped form of acquire/release, for the short maintenance gestures."""
    handle = acquire_datasource_lock(dsn, datasource_id, holder=holder)
    try:
        yield handle
    finally:
        release_datasource_lock(handle)
