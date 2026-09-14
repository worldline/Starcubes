"""Datasource abstractions loaded from the starcubes_meta.* tables.

A Source bundles every piece of per-datasource configuration the ETL engine
needs: connection strings, import SQL, fact column layout, dimension map,
and indices. Built as plain dataclasses so the ETL core can be exercised
without a Django or Postgres dependency (hand-construct a Source in tests).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import logging
import os

import sqlalchemy
from sqlalchemy import event, exc, text

# Module-level engine cache keyed by (dsn, frozenset(connect_args)).
# Prevents per-request create_engine() calls from leaking connection pools.
_engine_cache: dict[tuple, sqlalchemy.Engine] = {}

_oracle_client_initialized = False

# Whitelist of `measure.graph_style` values accepted by `Source.load`.
# Ladder styles are duplicated here (rather than imported from `core.query`)
# to avoid the circular import `query` → `source` → `query`. `quantile-steps`
# is the canonical library type; the `-open` variant is a starcubes-side
# spelling mapped to `quantile-steps` with `connect=False` in `query._ladder_plot_type`.
_VALID_GRAPH_STYLES = frozenset({
    # simple / non-ladder renderers
    "multibar", "multiline", "multipoint", "multistep", "stackarea", "waterfall",
    # ladder renderers (one entry per distinct plot the library can draw)
    "quantile-bands", "quantile-steps", "quantile-steps-open",
    "error-bars", "candlestick",
})


def init_oracle_client() -> None:
    """Initialize oracledb thick mode if Oracle client libs are on
    ``LD_LIBRARY_PATH``.  Looks for ``libclntsh.so`` in every directory
    listed there.  If found, calls ``oracledb.init_oracle_client()`` with
    that directory so thick mode is used.  If not found, oracledb stays
    in thin mode (pure Python, no client libs needed).
    """
    global _oracle_client_initialized
    if _oracle_client_initialized:
        return
    _oracle_client_initialized = True

    for d in os.environ.get("LD_LIBRARY_PATH", "").split(os.pathsep):
        if os.path.isfile(os.path.join(d, "libclntsh.so")):
            import oracledb
            oracledb.init_oracle_client(lib_dir=d)
            return


logger = logging.getLogger(__name__)


def _get_cached_engine(dsn: str, connect_args: dict | None = None) -> sqlalchemy.Engine:
    """Return a shared SQLAlchemy engine for the given DSN + connect_args.

    Engines are long-lived, thread-safe, and manage their own connection pool.
    Without caching, every ``Source.dest_engine()`` / ``Source.source_engine()``
    call would create a new pool (default 5 idle connections) that never gets
    disposed — leaking Postgres connections across requests.
    """
    key = (dsn, frozenset((connect_args or {}).items()))
    if key not in _engine_cache:
        engine = sqlalchemy.create_engine(
            dsn,
            connect_args=connect_args or {},
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
            pool_recycle=1800,
            pool_timeout=30,
        )
        _install_fork_safety(engine)
        _engine_cache[key] = engine
    return _engine_cache[key]


_bounded_engine_cache: dict = {}


# How each dialect is told to put its session on UTC. A dialect absent from
# this map has no session-level equivalent at all -- that is a property of
# the product, not a gap here, and `_pin_session_utc` says so out loud.
_SESSION_UTC_SQL: dict[str, str] = {
    "postgresql": "SET TIME ZONE 'UTC'",
    "oracle": "ALTER SESSION SET TIME_ZONE = 'UTC'",
    "mysql": "SET time_zone = '+00:00'",
}


def _pin_session_utc(engine: sqlalchemy.Engine, dialect: str) -> None:
    """Put every connection this source engine opens on a UTC session clock.

    The rest of this codebase already asserts that source timestamps are
    naive and hold UTC -- `source_now_sql()` reads UTC explicitly rather
    than trusting the session, `core/etl.py` binds its window bounds as
    naive UTC, `core/audit.py` binds the same way, and the extract parses
    with ``utc=True``. This makes that assertion true at the connection
    instead of hoping for it.

    It is not cosmetic. A predicate mixing a naive bind with a *timezone-
    aware* watermark column is resolved by converting one side with the
    session clock, so the whole window slides by the session offset. Measured
    on Postgres, session `Europe/Berlin`, against a `timestamptz` watermark:
    an incremental run with nothing to do re-extracted two hours it already
    held and inserted 16665 duplicate rows. Under a UTC session the same bind
    matches a naive and an aware column alike -- one setting, both shapes.

    A connect event rather than a dialect-specific connect argument
    (Postgres would take `-ctimezone=UTC` alongside its statement timeout)
    because this is the one place that serves all four dialects; Oracle needs
    an event either way, and two mechanisms for one rule drift apart.

    **mssql has no session time zone.** `AT TIME ZONE` is per-expression
    there, so nothing can be pinned and an aware watermark column stays
    dangerous -- which is what `preflight._check_watermark_tz` blocks on.
    Logged once per engine, the same courtesy the statement timeout extends.
    """
    stmt = _SESSION_UTC_SQL.get(dialect)
    if stmt is None:
        logger.warning(
            "no session time zone can be set for the %s dialect — source "
            "timestamps are read on whatever clock the session happens to "
            "carry; a timezone-aware watermark column is refused by "
            "preflight for this reason", dialect)
        return

    @event.listens_for(engine, "connect")
    def _set_session_utc(dbapi_conn, _record):
        # One statement per newly pooled connection. Not guarded the way the
        # Oracle call_timeout is: a driver that cannot set its session clock
        # cannot be trusted with the window either, and failing at connect
        # time is far better than loading against an unknown clock.
        cur = dbapi_conn.cursor()
        try:
            cur.execute(stmt)
        finally:
            cur.close()


def _bounded_source_engine(dsn: str, dialect: str,
                           timeout_ms: Optional[int]) -> sqlalchemy.Engine:
    """The engine for a source database, session pinned to UTC.

    Every source connection goes through here, including the untimed one:
    `Source.source_engine()` used to hand that case to `_get_cached_engine`,
    which is dialect-blind and — worse — shares its cache with
    `dest_engine()`/the meta engine. A datasource whose `source_dsn` names
    the same database as the warehouse (a legitimate shape, and the one a
    Postgres-as-source test fixture takes) therefore got *one* engine for
    both roles, so a source-only setting would have leaked onto the
    warehouse. Routing both cases here gives the source its own cache, which
    is what it should have had all along.

    With `timeout_ms`, statements additionally cannot outrun that budget. A
    source that accepts the connection and then never answers blocks the
    extract indefinitely: `pool_pre_ping` and `pool_timeout` bound the
    *checkout*, not the execution. There is no portable way to say this, so
    it is said per dialect, and a dialect with no equivalent says so out loud
    rather than silently returning an unbounded engine:

    * **postgresql** — a libpq startup option, the same mechanism
      `console_engine` uses.
    * **oracle** — `Connection.call_timeout` (milliseconds), which is an
      attribute on the DBAPI connection rather than a connect argument, so it
      is applied on the connect event.
    * **mysql / mssql** — no equivalent wired up here. Logged once per
      engine, because "unbounded" is a fact the operator should know rather
      than a detail to discover during an incident.

    `timeout_ms=None` means no budget was asked for — not that one was
    wanted and could not be given, so it draws no warning.

    Cached on `(dsn, dialect, timeout_ms)`, so the ETL does not build a pool
    per run and a differing budget is naturally a different engine.
    """
    key = (dsn, dialect, timeout_ms)
    if key in _bounded_engine_cache:
        return _bounded_engine_cache[key]

    connect_args: dict = {}
    if timeout_ms:
        if dialect == "postgresql":
            connect_args = {"options": f"-cstatement_timeout={timeout_ms}"}
        elif dialect not in ("oracle",):
            logger.warning(
                "no statement timeout is wired up for the %s dialect — source "
                "statements in this run are unbounded", dialect)

    engine = sqlalchemy.create_engine(
        dsn,
        connect_args=connect_args,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
        pool_recycle=1800,
        pool_timeout=30,
    )
    _install_fork_safety(engine)
    _pin_session_utc(engine, dialect)
    if timeout_ms and dialect == "oracle":
        @event.listens_for(engine, "connect")
        def _set_call_timeout(dbapi_conn, _record):
            # cx_Oracle/oracledb 7+. Guarded: an older driver simply has no
            # such attribute, and losing the timeout is better than losing
            # the connection.
            try:
                dbapi_conn.call_timeout = timeout_ms
            except Exception:
                logger.warning("could not set call_timeout on the Oracle "
                               "connection — source statements are unbounded",
                               exc_info=True)
    _bounded_engine_cache[key] = engine
    return engine


def _install_fork_safety(engine: sqlalchemy.Engine) -> None:
    """Invalidate pooled connections that were inherited across a fork().

    Under uWSGI (and any forking WSGI server), the engine pool is created in
    the master process and inherited by workers. Each worker then shares the
    parent's open sockets, leading to ``PGRES_TUPLES_OK and no message from
    libpq`` when two processes interleave on the same FD. ``pool_pre_ping``
    does not catch this — the socket itself is healthy on the parent's side.

    Tag every connection with the pid that opened it; on checkout, if the pid
    differs we raise ``DisconnectionError`` so SQLAlchemy invalidates the
    record and transparently opens a fresh connection in this process.
    """
    @event.listens_for(engine, "connect")
    def _on_connect(_dbapi_conn, record):
        record.info["pid"] = os.getpid()

    @event.listens_for(engine, "checkout")
    def _on_checkout(_dbapi_conn, record, _proxy):
        if record.info.get("pid") != os.getpid():
            record.dbapi_connection = None
            raise exc.DisconnectionError(
                "connection record was created in pid "
                f"{record.info.get('pid')}, current pid is {os.getpid()}"
            )


def dispose_all_engines() -> None:
    """Dispose every cached SQLAlchemy engine (close all pooled connections).

    Call from a Django shutdown signal or test teardown.
    """
    for cache in (_engine_cache, _bounded_engine_cache):
        for key, engine in list(cache.items()):
            engine.dispose()
            del cache[key]


@dataclass
class FactColumn:
    name: str
    pg_type: str
    role: str  # 'time' | 'measure' | 'dimension' | 'auto-dimension' | 'pk'
    ordinal: int
    source_expr: Optional[str] = None  # SELECT projection in the source DB.
                                       # Composed into import_query at
                                       # Source.load when datasource has a
                                       # base_table set (instead of a full
                                       # import_query string).


@dataclass
class DimGroupMember:
    dim_index: int


@dataclass
class DimGroup:
    id: int
    group_name: str
    color: str = ""
    members: list[DimGroupMember] = field(default_factory=list)


@dataclass
class DimGroupSet:
    """A named group dimension: one bucketing of a native dimension.

    Arbitrarily many per `Dimension`.  `slug` carries the chart's
    ``<fact_column>:grouped:<slug>`` token.
    """
    id: int
    name: str
    slug: str
    groups: list[DimGroup] = field(default_factory=list)


@dataclass
class Dimension:
    id: int
    fact_column: str
    dim_table: str
    source_table: str
    source_key: str
    source_name: str
    source_description: str
    is_primary: bool  # several fact cols may share one dim_table; one is primary
    inline_values: Optional[str] = None  # CSV: source_id,name[,description] per line
    filter_group: Optional[str] = None  # non-null: one chart filter ORs over all members
    group_sets: list[DimGroupSet] = field(default_factory=list)

    def default_group_set(self) -> Optional[DimGroupSet]:
        """The set the bare legacy ``<col>:grouped`` token resolves to."""
        return self.group_sets[0] if self.group_sets else None

    def group_set(self, slug: Optional[str]) -> Optional[DimGroupSet]:
        """Look up a group set by slug; `None` slug means the default one."""
        if slug is None:
            return self.default_group_set()
        return next((gs for gs in self.group_sets if gs.slug == slug), None)


@dataclass
class FactIndex:
    column_name: str
    method: str  # 'brin' | 'btree' | ...


@dataclass
class Measure:
    """A user-facing measurement offered in the chart's measure dropdown.

    Distinct from a fact column: one numeric column yields several
    aggregations and ``count(*)`` has no column at all.
    """
    name: str          # display label, e.g. "Transaction count"
    # 'count(*)' | 'sum(stay)' | 'avg(total)' | 'quantile(stay)' | 'extent(stay)'
    agg: str
    graph_style: str   # timeseries.js plot type; must be in _VALID_GRAPH_STYLES
    is_default: bool
    ordinal: int
    # The rung ladder for a ladder graph_style, in either of its two forms:
    # '5,25,50,75,95' for a quantile(col) agg, 'min,avg,max' for an
    # extent(col) one. Read through `Source.rungs_for`.
    percentiles: Optional[str] = None


@dataclass
class Source:
    id: int
    name: str
    schema_name: str
    source_dialect: str
    source_dsn: str
    dest_dsn: str                  # Set by caller (from settings/env), not queried from metadata.
    import_query: str          # bare SELECT ... FROM ... (no WHERE/GROUP/ORDER).
                               # Built from base_table + per-column source_expr
                               # at load time when datasource.import_query is NULL.
                               # Executed through SQLAlchemy's text(), so a
                               # `:name` in it is read as a bind parameter and
                               # the statement fails for want of a value. Not
                               # new with the bound load window -- preflight's
                               # projection probe (core/preflight.py) and
                               # core/audit.py already wrap it the same way, so
                               # such a query never got past preflight -- but
                               # nowhere written down until now. `::text` casts
                               # and 'HH24:MI:SS' masks are safe: SQLAlchemy's
                               # bind regex skips a colon that follows a word
                               # character or another colon.
    base_table: Optional[str]  # FROM-table for the composed import_query and a
                               # smarter default for watermark_max_query.
    watermark_column: str      # high-watermark column name in the source base table
    watermark_max_query: Optional[str]  # optional SELECT max(<wm>)…; default uses base_table
    partition_column: str      # fact column used for RANGE partitioning
    partition_period: str      # 'day' | 'week' | 'month'
    chunksize: int
    transform_procs: int
    small_window_hint: Optional[str]  # optional dialect hint for small-window loads
    interval_ladder: list[int]        # seconds per slot, finest to coarsest
    bar_pixel_target: float           # min bar width in px; switch coarser below this
    fact_columns: list[FactColumn]
    dimensions: list[Dimension]
    indices: list[FactIndex]
    measures: list[Measure] = field(default_factory=list)
    is_default: bool = False          # chart selects this source when no ?name= given
    pk_format: Optional[str] = None   # Python format string for PK display in chart drill-down
    late_arrival_overlap_s: int = 0   # seconds the load window reaches back past the HWM
    watermark_fact_expr: Optional[str] = None  # fact-side SQL reproducing watermark_column
    watermark_lag_s: int = 0          # seconds the window's upper bound stays below max(watermark)
    wallclock_lag_s: int = 0          # absolute visibility deadline in source wall-clock seconds
    retention_floor: Optional[datetime] = None  # rows below this were disposed
                                      # of on purpose; core/audit.py clamps to it
    # Pre-aggregation. See core/rollup.py and docs/rollup.md; `rollup_state`
    # is deliberately *not* here — it changes on every load, while everything
    # in a Source is configuration that only an operator edits.
    rollup_interval_s: int = 0        # bucket width in seconds; 0 = off
    rollup_columns: list[str] = field(default_factory=list)    # core column set
    rollup_marginals: list[str] = field(default_factory=list)  # one-column rollups
    rollup_measures: list[str] = field(default_factory=list)   # agg forms beside count(*)

    @classmethod
    def load(cls, meta_engine, dest_dsn: str, name_or_id) -> "Source":
        """Load a Source from starcubes_meta.* by id (int) or name (str)."""
        key_sql = "id = :k" if isinstance(name_or_id, int) else "name = :k"
        with meta_engine.begin() as conn:
            row = conn.execute(text(
                f"SELECT id, name, schema_name, source_dialect, source_dsn, "
                f"       import_query, base_table, "
                f"       watermark_column, watermark_max_query, "
                f"       partition_column, partition_period, "
                f"       chunksize, transform_procs, "
                f"       small_window_hint, "
                f"       interval_ladder, bar_pixel_target, pk_format, "
                f"       late_arrival_overlap_s, watermark_fact_expr, "
                f"       watermark_lag_s, wallclock_lag_s, "
                f"       retention_floor, "
                f"       rollup_interval_s, rollup_columns, "
                f"       rollup_marginals, rollup_measures, "
                f"       is_default "
                f"  FROM starcubes_meta.datasource WHERE {key_sql}"
            ), {"k": name_or_id}).mappings().first()
            if row is None:
                raise LookupError(f"datasource {name_or_id!r} not found")
            ds = dict(row)

            fact_columns = [FactColumn(**r) for r in conn.execute(text(
                "SELECT name, pg_type, role, ordinal, source_expr "
                "  FROM starcubes_meta.fact_column "
                " WHERE datasource_id = :ds "
                " ORDER BY ordinal"
            ), {"ds": ds["id"]}).mappings().all()]

            dimensions = [Dimension(**r) for r in conn.execute(text(
                "SELECT id, fact_column, dim_table, source_table, source_key, "
                "       source_name, source_description, is_primary, "
                "       inline_values, filter_group "
                "  FROM starcubes_meta.dimension "
                " WHERE datasource_id = :ds"
            ), {"ds": ds["id"]}).mappings().all()]

            # Attach group sets (named group dimensions) with their groups and
            # members. A dimension may carry arbitrarily many sets; ordering
            # follows dim_group_set.position, so the first one is what the
            # legacy `<col>:grouped` token resolves to. The LEFT JOINs keep
            # empty sets and memberless groups, hence the None guards.
            dim_by_id = {d.id: d for d in dimensions}
            if dim_by_id:
                dim_id_list = list(dim_by_id.keys())
                id_placeholders = ", ".join(f":did{i}" for i in range(len(dim_id_list)))
                id_params = {f"did{i}": did for i, did in enumerate(dim_id_list)}
                group_rows = conn.execute(text(
                    "SELECT gs.id AS set_id, gs.dimension_id, gs.name AS set_name, "
                    "       gs.slug, dg.id AS group_id, dg.group_name, dg.color, "
                    "       dgm.dim_index "
                    "  FROM starcubes_meta.dim_group_set gs "
                    "  LEFT JOIN starcubes_meta.dim_group dg "
                    "       ON dg.group_set_id = gs.id "
                    "  LEFT JOIN starcubes_meta.dim_group_member dgm "
                    "       ON dgm.group_id = dg.id "
                    f" WHERE gs.dimension_id IN ({id_placeholders}) "
                    " ORDER BY gs.position, gs.id, dg.id, dgm.dim_index"
                ), id_params).mappings().all()
                sets_by_id: dict[int, DimGroupSet] = {}
                groups_by_id: dict[int, DimGroup] = {}
                for r in group_rows:
                    gset = sets_by_id.get(r["set_id"])
                    if gset is None:
                        gset = DimGroupSet(id=r["set_id"], name=r["set_name"],
                                           slug=r["slug"])
                        sets_by_id[r["set_id"]] = gset
                        dim = dim_by_id.get(r["dimension_id"])
                        if dim is not None:
                            dim.group_sets.append(gset)
                    if r["group_id"] is None:
                        continue
                    grp = groups_by_id.get(r["group_id"])
                    if grp is None:
                        grp = DimGroup(id=r["group_id"],
                                       group_name=r["group_name"],
                                       color=r["color"] or "")
                        groups_by_id[r["group_id"]] = grp
                        gset.groups.append(grp)
                    if r["dim_index"] is not None:
                        grp.members.append(DimGroupMember(dim_index=r["dim_index"]))

            indices = [FactIndex(**r) for r in conn.execute(text(
                "SELECT column_name, method "
                "  FROM starcubes_meta.fact_index "
                " WHERE datasource_id = :ds"
            ), {"ds": ds["id"]}).mappings().all()]

            measures = [Measure(**r) for r in conn.execute(text(
                "SELECT name, agg, graph_style, is_default, ordinal, percentiles "
                "  FROM starcubes_meta.measure "
                " WHERE datasource_id = :ds "
                " ORDER BY ordinal, name"
            ), {"ds": ds["id"]}).mappings().all()]

        for m in measures:
            if m.graph_style not in _VALID_GRAPH_STYLES:
                raise ValueError(
                    f"datasource {ds['name']!r}: measure {m.name!r} has "
                    f"graph_style={m.graph_style!r}; choose from "
                    f"{sorted(_VALID_GRAPH_STYLES)}")

        fc_names = {c.name for c in fact_columns}
        groups: dict[str, list[Dimension]] = {}
        for d in dimensions:
            if not d.filter_group:
                continue
            if ":" in d.filter_group:
                raise ValueError(
                    f"datasource {ds['name']!r}: dimension "
                    f"{d.fact_column!r} filter_group={d.filter_group!r} "
                    f"must not contain ':' (collides with the "
                    f"<col>:grouped:<slug> filter token)")
            groups.setdefault(d.filter_group, []).append(d)
        for gname, members in groups.items():
            if gname in fc_names:
                fact_cols = ", ".join(sorted(
                    d.fact_column for d in members))
                raise ValueError(
                    f"datasource {ds['name']!r}: filter_group {gname!r} "
                    f"collides with a fact column of the same name "
                    f"(members: {fact_cols})")
            tables = {m.dim_table for m in members}
            if len(tables) > 1:
                raise ValueError(
                    f"datasource {ds['name']!r}: filter_group {gname!r} "
                    f"spans dim tables {sorted(tables)}; all members must "
                    f"share one dim_table")

        if not ds["import_query"]:
            if not ds["base_table"]:
                raise ValueError(
                    f"datasource {ds['name']!r}: neither import_query nor "
                    f"base_table is set; cannot compose a SELECT.")
            missing = [c.name for c in fact_columns if not c.source_expr]
            if missing:
                raise ValueError(
                    f"datasource {ds['name']!r}: fact_column.source_expr "
                    f"missing on: {missing}")
            cols = ", ".join(
                f"{c.source_expr} {c.name}"
                for c in sorted(fact_columns, key=lambda c: c.ordinal))
            ds["import_query"] = f"SELECT {cols} FROM {ds['base_table']}"

        return cls(
            **ds,
            dest_dsn=dest_dsn,
            fact_columns=fact_columns,
            dimensions=dimensions,
            indices=indices,
            measures=measures,
        )

    def source_engine(self, *,
                      statement_timeout_ms: int | None = None
                      ) -> sqlalchemy.Engine:
        """Engine for the source database, session pinned to UTC.

        With `statement_timeout_ms` the engine is a *separate* cached pool
        whose statements cannot outlive that budget; `_bounded_source_engine`
        keys on the timeout, so a differing budget is naturally a different
        engine. Without it, the pool every other source caller shares.

        Both cases go through `_bounded_source_engine` — the untimed one used
        to take `_get_cached_engine`, which knows no dialect and shares its
        cache with `dest_engine()`. See that function's docstring for why
        that mattered.
        """
        return _bounded_source_engine(
            self.source_dsn, self.source_dialect,
            int(statement_timeout_ms) if statement_timeout_ms else None)

    def dest_engine(self) -> sqlalchemy.Engine:
        return _get_cached_engine(
            self.dest_dsn,
            connect_args={"options": f"-csearch_path={self.schema_name}"},
        )

    def console_engine(self, *, statement_timeout_ms: int = 30_000) -> sqlalchemy.Engine:
        """Engine for ad-hoc read-only user SQL against this source's warehouse schema.

        Deliberately a *separate* pool from :meth:`dest_engine`: a user running
        a slow ad-hoc query in the chart's Query console must not starve the
        chart's own plot/drill queries of pooled connections. Because
        ``_get_cached_engine`` keys on ``(dsn, connect_args)``, the differing
        ``options`` string is what makes it a distinct cached engine.

        Read-only and the statement timeout are set as libpq *startup* options
        so they hold even if a caller forgets the per-transaction SETs in
        ``core.adhoc.run_query``. That is the outermost of the read-only layers
        described there.
        """
        return _get_cached_engine(
            self.dest_dsn,
            connect_args={"options": (
                f"-csearch_path={self.schema_name} "
                f"-cdefault_transaction_read_only=on "
                f"-cstatement_timeout={int(statement_timeout_ms)}"
            )},
        )

    def primary_dimensions(self) -> list[Dimension]:
        return [d for d in self.dimensions if d.is_primary]

    def filter_fanout(self) -> dict[str, list[str]]:
        """{filter_group: sorted member fact columns} for non-null filter_group.

        Group members share one dim_table (enforced in ``load``), so a chart
        filter on the group token can OR the same dim-value subquery over
        every member column. A transaction that passes through several peers
        (so_peer_1/2/3) then matches once per peer role instead of forcing
        the user to filter each column separately.
        """
        out: dict[str, list[str]] = {}
        for d in self.dimensions:
            if d.filter_group:
                out.setdefault(d.filter_group, []).append(d.fact_column)
        return {k: sorted(v) for k, v in out.items()}

    def dim_fact_to_table(self) -> dict[str, str]:
        """Mapping {fact_column: dim_table} for regular dim-mapped fact columns.

        Auto-dimension columns are excluded — the parent process resolves their
        source values to indices before the chunk reaches a transform worker,
        so workers must not try to ``map()`` them again.

        Includes non-primary rows so fact cols that share a dim_table
        (e.g. so_peer_1/2/3 → peer) all get translated.
        """
        auto = self._auto_dim_fact_columns()
        return {d.fact_column: d.dim_table
                for d in self.dimensions
                if d.dim_table and d.fact_column not in auto}

    def _auto_dim_fact_columns(self) -> set[str]:
        return {c.name for c in self.fact_columns if c.role == "auto-dimension"}

    def auto_dim_fact_to_table(self) -> dict[str, str]:
        """Mapping {fact_column: dim_table} for auto-dimension fact columns.

        Resolved in the parent process during the extract loop: new source
        values discovered in a chunk are inserted into the dim table and the
        chunk column is rewritten with the resulting smallint indices before
        the chunk is enqueued for transform.
        """
        auto = self._auto_dim_fact_columns()
        return {d.fact_column: d.dim_table
                for d in self.dimensions
                if d.dim_table and d.fact_column in auto}

    def fact_column_order(self) -> list[str]:
        return [c.name for c in sorted(self.fact_columns, key=lambda c: c.ordinal)]

    def smallint_measure_columns(self) -> list[str]:
        return [c.name for c in self.fact_columns
                if c.pg_type == "smallint" and c.role == "measure"]

    def bigint_measure_columns(self) -> list[str]:
        return [c.name for c in self.fact_columns
                if c.pg_type == "bigint" and c.role == "measure"]

    def watermark_fact_sql(self) -> str:
        """Fact-side SQL expression that reproduces ``watermark_column``.

        The watermark is not always a projected fact column -- the example
        Oracle source watermarks on ``end_time`` but stores ``create_time``
        plus a ``total`` duration -- so the expression is configurable.
        Used to select the already-loaded rows of the overlap window
        (`core/etl.py:_resume_state`) and by `core/audit.py`.
        """
        return self.watermark_fact_expr or self.partition_column

    def source_now_sql(self) -> str:
        """Dialect spelling of the source DB's current time, **as naive UTC**.

        The return value is compared against ``max(watermark_column)`` to form
        the second term of the load window's upper bound, so it has to sit on
        the same clock as that column. And that clock is UTC: naive source
        timestamps are treated as UTC everywhere in this codebase --
        ``pd.read_sql(..., parse_dates={pc: {"utc": True}})`` in
        ``core/etl.py``, ``_as_utc`` in ``core/audit.py``,
        ``replace(tzinfo=timezone.utc)`` in ``core/utils.py:_parse_dt`` and a
        dozen more. So this must read UTC too.

        **The source connection's session timezone is irrelevant, and that is
        the point.** This used to return ``LOCALTIMESTAMP``, which reads the
        *session* clock rather than the content of the time columns. On a
        production Oracle source writing ``end_time`` in UTC from a session
        sitting in ``Europe/Berlin``, that came back +7203s ahead, the
        wall-clock term won every run, and ``watermark_lag_s`` was left with
        no effect at all (see `docs/etl-engine.md`, "Window's upper bound is the
        later of two terms"). Every
        dialect has a direct UTC expression; using it removes the assumption
        instead of documenting it, which is also why no ``source_timezone``
        config column is needed -- the code already asserts UTC everywhere.

        All four return a *naive* timestamp, matching how the watermark
        column itself is read.

        `_pin_session_utc` now puts the session clock on UTC as well. That
        does not make this expression redundant — it reads UTC whatever the
        session says, which is the stronger guarantee, and mssql cannot be
        pinned at all — but the two now agree instead of one compensating
        for the other.

        Returns the bare expression -- callers that need it without a FROM
        clause (see `core/etl.py`, the watermark_max_query branch) must add
        Oracle's `FROM dual` themselves.
        """
        return {
            "oracle": "SYS_EXTRACT_UTC(SYSTIMESTAMP)",
            "postgresql": "CURRENT_TIMESTAMP AT TIME ZONE 'UTC'",
            "mysql": "UTC_TIMESTAMP()",
            "mssql": "SYSUTCDATETIME()",
        }.get(self.source_dialect, "CURRENT_TIMESTAMP AT TIME ZONE 'UTC'")

    def watermark_probe_sql(self) -> tuple[str, Optional[str]]:
        """SQL that reads `max(watermark_column)` and the source's own clock.

        Returns ``(max_query, standalone_now)``. The first yields
        ``max(watermark_column)`` in column 0 and, where it can, the source
        clock in column 1; the second is a separate statement for the clock
        when the first could not carry it, or ``None`` when it did.

        Three shapes, cheapest first: an operator-supplied
        `watermark_max_query` (opaque, so the clock needs its own trivial
        round trip — Oracle's `FROM dual` included), a direct `max()` against
        `base_table`, and finally the whole `import_query` as a subquery,
        which is the slow one and the reason the first two exist.

        Lives here rather than in `core/etl.py` because two callers need the
        *same* statement: the engine, which uses the values to bound the load
        window, and `core/preflight.py:_check_watermark_tz`, which reads
        `max(watermark_column)` to see whether the column is timezone-aware.
        Preflight runs first and hands its reading forward, so the pair is
        read once per run rather than twice — but only if both ask the source
        the same question, which is what this method guarantees.
        """
        wm = self.watermark_column
        now_expr = self.source_now_sql()
        if self.watermark_max_query:
            return self.watermark_max_query, (
                f"SELECT {now_expr} FROM dual"
                if self.source_dialect == "oracle"
                else f"SELECT {now_expr}")
        frm = self.base_table or f"({self.import_query}) q"
        return f"SELECT max({wm}), {now_expr} FROM {frm}", None

    def pk_column(self) -> Optional[str]:
        return next((c.name for c in self.fact_columns if c.role == "pk"), None)

    def measure_row(self, agg: str, name: Optional[str] = None) -> Optional["Measure"]:
        """Resolve one catalog row from the chart's measure selection.

        The agg form alone does **not** identify a measure: the catalog's
        uniqueness is ``UNIQUE (datasource_id, name)``, so an operator may
        offer the same ``quantile(stay)`` twice under two names to get two
        graph styles (or two rung ladders) out of one aggregation. Matching on
        `agg` alone always returned the first such row, which is why every one
        of those measures drew as whichever style happened to be configured
        first.

        `name` is the display label the chart sends alongside the agg
        (``?measure_name=``). When it names a row, that row wins. Without it —
        an old bookmark, an external caller, a management command — the
        historical agg-only match stands, so a catalog with no duplicates
        behaves exactly as before.
        """
        if name:
            for m in self.measures:
                if m.name == name and m.agg == agg:
                    return m
        for m in self.measures:
            if m.agg == agg:
                return m
        return None

    def graph_style_for(self, agg: str, name: Optional[str] = None) -> str:
        """Return the configured graph_style for a measure agg expression.

        `agg` is the catalog form (``count(*)`` or ``func(col)``), `name` the
        optional display label that disambiguates two rows sharing one agg —
        see `measure_row`. Falls back to 'multibar' when the source has no
        matching measure row, so a source with an empty catalog keeps the
        historical behaviour.
        """
        m = self.measure_row(agg, name)
        return m.graph_style if m else "multibar"

    def rungs_for(self, agg: str, name: Optional[str] = None) -> list:
        """Return the rung ladder for a ladder-style measure agg.

        Two forms, picked by the agg's own prefix — the same discriminator the
        read path uses (`core.query._parse_ladder_agg`):

        * ``quantile(col)`` → integer percentiles, sorted ascending and
          de-duplicated. Falls back to ``[5, 25, 50, 75, 95]``.
        * ``extent(col)``   → the tokens ``min``/``avg``/``max`` in canonical
          order. Falls back to all three.

        Parses the matching measure row's `percentiles` column, which carries
        either form. A missing or unset row yields the respective default, so
        a source whose catalog predates the column still draws. The row is
        resolved through `measure_row`, so `name` picks between two catalog
        entries sharing one agg — two ladders over the same column is exactly
        the case that needs it.

        The parsing is duplicated here rather than imported from
        `core.query.normalize_rungs` for the same reason `_VALID_GRAPH_STYLES`
        is duplicated: `query` imports `source`, so the reverse import would
        close the cycle. This copy is deliberately permissive — it *resolves*
        a ladder, while `normalize_rungs` *validates* one at the point of use.
        """
        is_extent = agg.startswith("extent(")
        m = self.measure_row(agg, name)
        raw = m.percentiles if m else None
        if is_extent:
            canonical = ["min", "avg", "max"]
            if not raw:
                return canonical
            toks = {t.strip().lower() for t in raw.split(",") if t.strip()}
            picked = [r for r in canonical if r in toks]
            return picked if len(picked) >= 2 else canonical
        if not raw:
            return [5, 25, 50, 75, 95]
        return sorted({int(tok) for tok in raw.split(",") if tok.strip()})
