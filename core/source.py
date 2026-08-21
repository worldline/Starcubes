"""Datasource abstractions loaded from the starcubes_meta.* tables.

A Source bundles every piece of per-datasource configuration the ETL engine
needs: connection strings, import SQL, fact column layout, dimension map,
and indices. Built as plain dataclasses so the ETL core can be exercised
without a Django or Postgres dependency (hand-construct a Source in tests).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import os

import sqlalchemy
from sqlalchemy import event, exc, text

# Module-level engine cache keyed by (dsn, frozenset(connect_args)).
# Prevents per-request create_engine() calls from leaking connection pools.
_engine_cache: dict[tuple, sqlalchemy.Engine] = {}

_oracle_client_initialized = False


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
    for key, engine in list(_engine_cache.items()):
        engine.dispose()
        del _engine_cache[key]


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
    agg: str           # 'count(*)' | 'sum(stay)' | 'avg(total)' | 'quantile(stay)'
    graph_style: str   # timeseries.js plot type; only 'multibar' wired today
    is_default: bool
    ordinal: int
    percentiles: Optional[str] = None  # 'a,b,c' ladder for graph_style=quantile-bands


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
                "       inline_values "
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

    def source_engine(self) -> sqlalchemy.Engine:
        return _get_cached_engine(self.source_dsn)

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
        """Dialect spelling of the source DB's *naive* wall clock.

        The watermark column is read in source wall-clock time throughout
        (see `_source_counts` in core/audit.py and the ETL's WHERE clause),
        so the second term of the load window's upper bound has to be read
        the same way -- an offset-aware `CURRENT_TIMESTAMP` would compare
        against naive watermarks.

        Spelled from `source_dialect` rather than via
        `sqlalchemy.func.localtimestamp()`: whether the Oracle dialect
        appends its own `FROM dual` would have to be verified at runtime,
        and MSSQL has no LOCALTIMESTAMP at all. `source_dialect` already
        exists and is CHECK-constrained, so this adds no new ground to the
        open multi-dialect item in CLAUDE.md's backlog.

        Returns the bare expression -- callers that need it without a FROM
        clause (see `core/etl.py`, the watermark_max_query branch) must add
        Oracle's `FROM dual` themselves.
        """
        if self.source_dialect == "mssql":
            return "SYSDATETIME()"
        return "LOCALTIMESTAMP"

    def pk_column(self) -> Optional[str]:
        return next((c.name for c in self.fact_columns if c.role == "pk"), None)

    def graph_style_for(self, agg: str) -> str:
        """Return the configured graph_style for a measure agg expression.

        `agg` is the catalog form (``count(*)`` or ``func(col)``). Falls back
        to 'multibar' when the source has no matching measure row, so a source
        with an empty catalog keeps the historical behaviour.
        """
        for m in self.measures:
            if m.agg == agg:
                return m.graph_style
        return "multibar"

    def quantiles_for(self, agg: str) -> list[int]:
        """Return the percentile ladder for a quantile-bands measure agg.

        Parses the matching measure row's `percentiles` ('a,b,c'); falls back
        to [5, 25, 50, 75, 95] when the row is missing or unset. Always sorted
        ascending and de-duplicated.
        """
        raw = None
        for m in self.measures:
            if m.agg == agg:
                raw = m.percentiles
                break
        if not raw:
            return [5, 25, 50, 75, 95]
        return sorted({int(tok) for tok in raw.split(",") if tok.strip()})
