"""DDL + dimension-mapping helpers.

Generalized DDL + dimension-mapping helpers. Every helper takes an explicit Source and
engines — no module-level state. The per-datasource schema is `source.schema_name`;
dimension tables use a neutral `source_id` key column.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from sqlalchemy import text

from .source import Source

logger = logging.getLogger(__name__)

UNKNOWN_SOURCE_ID = "__unknown__"  # sentinel dim row for source values not
                                   # present at dim-import time. Distinct
                                   # from the "-1" row which represents
                                   # source NULL via nvl in import_query.

_MIGRATION_SQL = Path(__file__).resolve().parent.parent / "migrations" / "starcubes_meta.sql"
MIGRATIONS_DIR = _MIGRATION_SQL.parent

# A datasource's schema name is interpolated into ~70 f-string SQL statements
# across core/, and pandas' ``to_sql`` quotes it while those do not. An
# unquoted identifier is case-folded by Postgres, so a name that is not
# already lower-case means the two disagree: ``CREATE SCHEMA Test`` makes
# ``test``, then ``to_sql(schema="Test")`` looks for ``"Test"`` and fails
# with "schema does not exist". Restricting the name to what survives
# case-folding unchanged makes quoting irrelevant everywhere.
_SCHEMA_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class SchemaNameError(ValueError):
    """Raised for a schema name that is not safe to interpolate unquoted."""


def validate_schema_name(name: str) -> str:
    """Return ``name`` if it is a safe, case-fold-stable schema identifier."""
    if not isinstance(name, str) or not _SCHEMA_NAME_RE.match(name):
        raise SchemaNameError(
            f"invalid schema name {name!r}: use 1-63 characters, lower-case "
            f"letters, digits and underscore, starting with a letter or "
            f"underscore (e.g. 'starcubes_payments')")
    return name


def ensure_schema(conn, schema: str) -> None:
    """Create the datasource's schema if it is not there yet.

    Called by every entry point that creates objects inside it, not just
    ``create_fact`` — importing dimensions into a datasource whose fact
    table has not been created yet is a legitimate order of operations.
    """
    conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {validate_schema_name(schema)}"))

# --- partition period helpers ------------------------------------------------
#
# A source's partition_period is one of 'day', 'week', 'month'. All four
# helpers below assume naive midnight datetimes anchored at a partition
# boundary; week boundaries are ISO Mondays.

_MONTH_NAME_RE = re.compile(r"^fact_y(\d{4})m(\d{2})$")
_WEEK_NAME_RE  = re.compile(r"^fact_y(\d{4})w(\d{2})$")
_DAY_NAME_RE   = re.compile(r"^fact_y(\d{4})m(\d{2})d(\d{2})$")


def _floor_to_period(dt: datetime, period: str) -> datetime:
    dt = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "day":
        return dt
    if period == "week":
        return dt - timedelta(days=dt.weekday())   # ISO Monday
    if period == "month":
        return dt.replace(day=1)
    raise ValueError(f"unknown partition_period {period!r}")


def _next_period(dt: datetime, period: str) -> datetime:
    if period == "day":
        return dt + timedelta(days=1)
    if period == "week":
        return dt + timedelta(days=7)
    if period == "month":
        if dt.month == 12:
            return dt.replace(year=dt.year + 1, month=1)
        return dt.replace(month=dt.month + 1)
    raise ValueError(f"unknown partition_period {period!r}")


def _prev_period(dt: datetime, period: str) -> datetime:
    if period == "day":
        return dt - timedelta(days=1)
    if period == "week":
        return dt - timedelta(days=7)
    if period == "month":
        if dt.month == 1:
            return dt.replace(year=dt.year - 1, month=12)
        return dt.replace(month=dt.month - 1)
    raise ValueError(f"unknown partition_period {period!r}")


def _partition_name(period: str, dt: datetime) -> str:
    if period == "month":
        return f"fact_y{dt.year}m{dt.month:02d}"
    if period == "week":
        iso_y, iso_w, _ = dt.isocalendar()
        return f"fact_y{iso_y}w{iso_w:02d}"
    if period == "day":
        return f"fact_y{dt.year}m{dt.month:02d}d{dt.day:02d}"
    raise ValueError(f"unknown partition_period {period!r}")


def _parse_partition_name(period: str, name: str) -> datetime:
    if period == "month":
        m = _MONTH_NAME_RE.match(name)
        if not m:
            raise ValueError(f"partition name {name!r} does not match monthly pattern")
        return datetime(int(m.group(1)), int(m.group(2)), 1)
    if period == "week":
        m = _WEEK_NAME_RE.match(name)
        if not m:
            raise ValueError(f"partition name {name!r} does not match weekly pattern")
        return datetime.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
    if period == "day":
        m = _DAY_NAME_RE.match(name)
        if not m:
            raise ValueError(f"partition name {name!r} does not match daily pattern")
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    raise ValueError(f"unknown partition_period {period!r}")


def _default_window(period: str) -> tuple[int, int]:
    """(back, forward) partition counts to pre-create for a fresh source.

    forward is the number of partitions starting at the current period
    boundary; additional_partitions adds to forward at the call site.
    """
    if period == "month":
        return (4, 1)
    if period == "week":
        return (14, 4)
    if period == "day":
        return (30, 7)
    raise ValueError(f"unknown partition_period {period!r}")


_freshly_bootstrapped: bool = False


def ensure_starcubes_meta(engine, *, force: bool = False) -> bool:
    """Create the starcubes_meta schema and tables if they do not exist.

    Checks for the ``datasource`` table in ``starcubes_meta``; if absent, runs the
    consolidated migration SQL at ``migrations/starcubes_meta.sql``.  The script is
    fully idempotent (``IF NOT EXISTS`` / guarded ``ALTER``), so running it on
    an existing database is safe.

    The schema itself may already exist because Django's custom database
    backend creates it on first connection (so that Django's auth tables land
    in starcubes_meta instead of public).  We check for a table, not the schema,
    to ensure the starcubes_meta application tables are still created.

    ``force=True`` re-runs the consolidated SQL even when ``datasource``
    already exists. This is the only way an existing installation ever picks
    up new idempotent ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS`` blocks
    added to ``starcubes_meta.sql`` after its first boot — the cheap
    ``SourcesConfig.ready()`` probe (see ``sources/apps.py``) deliberately
    skips the 500-line script on every request-serving process, so
    ``manage.py bootstrap`` calls this with ``force=True`` once per container
    start instead.

    Returns True if the starcubes_meta tables were freshly created (first boot),
    False if they already existed.  The module-level ``_freshly_bootstrapped``
    flag is set accordingly and can be read by callers in the same process.
    """
    global _freshly_bootstrapped
    with engine.begin() as conn:
        exists = conn.execute(text(
            "SELECT 1 FROM information_schema.tables "
            " WHERE table_schema = 'starcubes_meta' AND table_name = 'datasource'"
        )).scalar()
    if exists and not force:
        _freshly_bootstrapped = False
        return False
    sql = _MIGRATION_SQL.read_text()
    with engine.begin() as conn:
        conn.execute(text(sql))
    _freshly_bootstrapped = not exists
    return _freshly_bootstrapped


def create_fact(source: Source, dest_engine, *, additional_partitions: int = 2) -> None:
    """Create {schema}.fact and its RANGE partitions.

    Partition size is taken from ``source.partition_period`` (day / week /
    month). ``_default_window`` picks how many back- and forward-buffer
    partitions to create; ``additional_partitions`` adds to the forward
    count.

    Idempotent: re-running adds any missing partitions without touching data.
    """
    cols = sorted(source.fact_columns, key=lambda c: c.ordinal)
    col_ddl = ", ".join(f"{c.name} {c.pg_type}" for c in cols)
    schema = source.schema_name
    pc = source.partition_column
    period = source.partition_period

    back, forward = _default_window(period)
    forward += additional_partitions

    cur = _floor_to_period(datetime.now(), period)
    for _ in range(back):
        cur = _prev_period(cur, period)
    boundaries = [cur]
    for _ in range(back + forward):
        boundaries.append(_next_period(boundaries[-1], period))

    with dest_engine.begin() as conn:
        ensure_schema(conn, schema)
        conn.execute(text(
            f"CREATE TABLE IF NOT EXISTS {schema}.fact ({col_ddl}) "
            f"PARTITION BY RANGE ({pc})"
        ))
        for i in range(len(boundaries) - 1):
            lo, hi = boundaries[i], boundaries[i + 1]
            part = _partition_name(period, lo)
            conn.execute(text(
                f"CREATE UNLOGGED TABLE IF NOT EXISTS {schema}.{part} "
                f"PARTITION OF {schema}.fact "
                f"FOR VALUES FROM ('{lo}') TO ('{hi}')"
            ))


def create_dim_tables(source: Source, dest_engine, *, quiet: bool = False) -> list[str]:
    """Create empty dimension tables from the Source config.

    For each primary dimension with a ``dim_table``, creates the table if it
    does not already exist and inserts the ``__unknown__`` sentinel row.
    Inline dimensions are also populated from their CSV values.

    Returns a list of created dim table names.
    """
    schema = source.schema_name
    created: list[str] = []
    with dest_engine.begin() as conn:
        ensure_schema(conn, schema)
        for d in source.primary_dimensions():
            if not d.dim_table:
                continue
            exists = conn.execute(text(
                "SELECT 1 FROM information_schema.tables "
                " WHERE table_schema = :s AND table_name = :t"
            ), {"s": schema, "t": d.dim_table}).scalar()
            if exists:
                continue
            conn.execute(text(
                f"CREATE TABLE {schema}.{d.dim_table} ("
                f"  index SMALLSERIAL PRIMARY KEY, "
                f"  source_id text, name text, description text, "
                f"  count bigint NOT NULL DEFAULT 0)"
            ))
            if d.inline_values:
                rows = _parse_inline_values(d.inline_values)
                has_minus_one = any(r[0] == "-1" for r in rows)
                if not has_minus_one:
                    rows = [("-1", "NULL", "source contained NULL value")] + rows
                df = pd.DataFrame(rows, columns=["source_id", "name", "description"])
                df.to_sql(d.dim_table, conn, schema=schema,
                          if_exists="append", index=False)
            _ensure_unknown_row(conn, schema, d.dim_table)
            created.append(d.dim_table)
            if not quiet:
                print(f"{source.name}: created dim table {d.dim_table}"
                      + (" (inline)" if d.inline_values else ""))
    return created


def create_indices(source: Source, dest_engine, *, quiet: bool = False) -> None:
    schema = source.schema_name
    with dest_engine.begin() as conn:
        conn.execute(text("SET max_parallel_maintenance_workers TO 8"))
        primaries = source.primary_dimensions()
        if primaries:
            if not quiet:
                print(f"{source.name}: creating fact foreign keys")
            for d in primaries:
                exists = conn.execute(text(
                    "SELECT 1 FROM pg_constraint "
                    "WHERE conname = :name AND conrelid = CAST(:rel AS regclass)"
                ), {"name": f"fk_{d.fact_column}", "rel": f"{schema}.fact"}).scalar()
                if exists:
                    continue
                conn.execute(text(
                    f"ALTER TABLE {schema}.fact "
                    f"ADD CONSTRAINT fk_{d.fact_column} "
                    f"FOREIGN KEY({d.fact_column}) REFERENCES {schema}.{d.dim_table}(index)"
                ))
        for ix in source.indices:
            if not quiet:
                print(f"{source.name}: creating fact index {ix.column_name} ({ix.method})")
            conn.execute(text(
                f"CREATE INDEX IF NOT EXISTS ix_{ix.column_name} ON {schema}.fact "
                f"USING {ix.method}({ix.column_name})"
            ))
        for d in source.dimensions:
            if not d.dim_table:
                continue
            if not quiet:
                print(f"{source.name}: creating fact index {d.fact_column} (btree)")
            conn.execute(text(
                f"CREATE INDEX IF NOT EXISTS ix_{d.fact_column} ON {schema}.fact "
                f"USING btree({d.fact_column}) WHERE {d.fact_column} != 1"
            ))


_SAFE_CASTS: set[tuple[str, str]] = {
    ("smallint", "integer"),
    ("smallint", "bigint"),
    ("integer", "bigint"),
    ("real", "double precision"),
    ("timestamp", "timestamptz"),
}


def _is_safe_cast(from_type: str, to_type: str) -> bool:
    """Return True if ALTER COLUMN from from_type to to_type is a safe widening."""
    if from_type == to_type:
        return False
    if to_type in ("text", "character varying"):
        return True
    return (from_type, to_type) in _SAFE_CASTS


def sync_schema(source: Source, dest_engine, *,
                 drop_columns: list[str] | None = None,
                 drop_dims: list[str] | None = None,
                 quiet: bool = False) -> dict:
    """Compare metadata against physical schema and reconcile differences.

    When called with drop_columns/drop_dims as None (dry run), returns a diff
    without making changes. When called with explicit lists (may be empty),
    executes the full sync — adding missing columns, altering safe type changes,
    dropping confirmed columns/dim tables, and recreating FKs and indices.

    Returns a dict with keys:
      add_columns, alter_columns, unsafe_type_changes,
      drop_columns, create_dims, drop_dims
    """
    schema = source.schema_name
    meta_cols = {c.name: c.pg_type for c in source.fact_columns}
    expected_dim_tables = {d.dim_table for d in source.primary_dimensions()
                          if d.dim_table}

    with dest_engine.begin() as conn:
        # Use pg_type catalog for exact type names (information_schema normalizes)
        pg_rows = conn.execute(text(
            "SELECT a.attname, t.typname "
            "  FROM pg_class c "
            "  JOIN pg_namespace n ON n.oid = c.relnamespace "
            "  JOIN pg_attribute a ON a.attrelid = c.oid "
            "  JOIN pg_type t ON t.oid = a.atttypid "
            " WHERE n.nspname = :s AND c.relname = 'fact' "
            "   AND a.attnum > 0 AND NOT a.attisdropped"
        ), {"s": schema}).mappings().all()
        # pg_type name mapping: int4 → integer, int2 → smallint, int8 → bigint, etc.
        _pg_type_map = {
            "int2": "smallint", "int4": "integer", "int8": "bigint",
            "float4": "real", "float8": "double precision",
            "numeric": "numeric", "varchar": "character varying",
            "bpchar": "character", "text": "text",
            "timestamp": "timestamp", "timestamptz": "timestamptz",
            "bool": "boolean",
        }
        existing = {}
        for row in pg_rows:
            raw = row["typname"]
            existing[row["attname"]] = _pg_type_map.get(raw, raw)

    add_columns = [{"name": n, "pg_type": t}
                    for n, t in meta_cols.items() if n not in existing]
    alter_columns = []
    unsafe_type_changes = []
    for name, want_type in meta_cols.items():
        if name not in existing or name not in meta_cols:
            continue
        have_type = existing.get(name)
        if have_type and have_type != want_type:
            if _is_safe_cast(have_type, want_type):
                alter_columns.append({"name": name,
                                      "from_type": have_type,
                                      "to_type": want_type})
            else:
                unsafe_type_changes.append({"name": name,
                                            "from_type": have_type,
                                            "to_type": want_type})
    orphan_columns = [n for n in existing if n not in meta_cols]

    # 2. Compare dim tables against metadata
    with dest_engine.begin() as conn:
        all_tables = set(conn.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname = :s"
        ), {"s": schema}).scalars().all())
    # Exclude 'fact' and partition tables from dim-table consideration
    fact_tables = {t for t in all_tables
                   if t == "fact" or t.startswith("fact_y")}
    non_fact_tables = all_tables - fact_tables
    create_dims = sorted(expected_dim_tables - non_fact_tables)
    drop_dims = sorted(non_fact_tables - expected_dim_tables)

    # Build the diff result
    result = {
        "add_columns": add_columns,
        "alter_columns": alter_columns,
        "unsafe_type_changes": unsafe_type_changes,
        "drop_columns": orphan_columns,
        "create_dims": create_dims,
        "drop_dims": drop_dims,
    }

    # Dry run — return diff only
    if drop_columns is None:
        return result

    # Execute phase
    # 1. Drop FKs and indices (each opens its own transaction)
    if not quiet:
        print(f"{source.name}: dropping FKs and indices for schema sync")
    drop_indices(source, dest_engine, quiet=quiet)

    # 2-4. Column alters (single transaction for atomicity)
    with dest_engine.begin() as conn:
        for col in add_columns:
            if not quiet:
                print(f"{source.name}: ADD COLUMN {col['name']} {col['pg_type']}")
            conn.execute(text(
                f"ALTER TABLE {schema}.fact ADD COLUMN "
                f"{col['name']} {col['pg_type']}"
            ))
        for col in alter_columns:
            if not quiet:
                print(f"{source.name}: ALTER COLUMN {col['name']} "
                      f"{col['from_type']} → {col['to_type']}")
            conn.execute(text(
                f"ALTER TABLE {schema}.fact "
                f"ALTER COLUMN {col['name']} TYPE {col['to_type']}"
            ))
        for col_name in drop_columns:
            if col_name not in orphan_columns:
                continue
            if not quiet:
                print(f"{source.name}: DROP COLUMN {col_name}")
            conn.execute(text(
                f"ALTER TABLE {schema}.fact DROP COLUMN {col_name}"
            ))

    # 5. Create missing dim tables
    create_dim_tables(source, dest_engine, quiet=quiet)

    # 6. Drop confirmed orphan dim tables
    if drop_dims:
        with dest_engine.begin() as conn:
            for dim_name in drop_dims:
                if dim_name not in (non_fact_tables - expected_dim_tables):
                    continue
                if not quiet:
                    print(f"{source.name}: DROP TABLE {schema}.{dim_name}")
                conn.execute(text(
                    f"DROP TABLE IF EXISTS {schema}.{dim_name}"
                ))

    # 7. Re-create FKs and indices
    if not quiet:
        print(f"{source.name}: re-creating FKs and indices")
    create_indices(source, dest_engine, quiet=quiet)

    result["executed"] = True
    return result


def drop_indices(source: Source, dest_engine, *, quiet: bool = False) -> None:
    schema = source.schema_name
    with dest_engine.begin() as conn:
        primaries = source.primary_dimensions()
        if primaries:
            if not quiet:
                print(f"{source.name}: dropping fact foreign keys")
            fks = ", ".join(
                f"DROP CONSTRAINT IF EXISTS fk_{d.fact_column}" for d in primaries
            )
            conn.execute(text(f"ALTER TABLE {schema}.fact {fks}"))
        for ix in source.indices:
            conn.execute(text(f"DROP INDEX IF EXISTS {schema}.ix_{ix.column_name}"))
        for d in source.dimensions:
            if not d.dim_table:
                continue
            conn.execute(text(f"DROP INDEX IF EXISTS {schema}.ix_{d.fact_column}"))


def import_dimensions(source: Source, source_engine, dest_engine,
                      *, quiet: bool = False) -> None:
    """Ensure dim tables exist and append any new keys found in the source.

    Only `is_primary` Dimension rows drive table creation; non-primary rows only
    provide the fact-column→dim-table mapping (several fact cols can share a
    dim_table, e.g. so_peer_1/2/3 → peer).

    Dimensions with ``inline_values`` (and no ``source_table``) are populated
    from the CSV instead of querying the source DB.

    When a dim table is newly created for a fact column that already has raw
    source values in the fact table, those values are backfilled — replaced
    with the dim table indices so the query layer can join correctly.
    """
    schema = source.schema_name
    # Dimensions can legitimately be imported before the fact table exists
    # (inline dimensions need no source connection at all), so this path
    # cannot assume create_fact has already made the schema.
    with dest_engine.begin() as conn:
        ensure_schema(conn, schema)
    auto_dim_cols = {c.name for c in source.fact_columns
                     if c.role == "auto-dimension"}
    for d in source.dimensions:
        if not d.is_primary:
            continue

        # Inline values path: populate from CSV, no source DB query needed.
        if d.inline_values:
            rows = _parse_inline_values(d.inline_values)
            created = _populate_dim_table(dest_engine, schema, d.dim_table, rows,
                                           source_name=source.name, quiet=quiet)
            if created:
                _backfill_fact_column(dest_engine, schema, d.fact_column,
                                      d.dim_table, source_name=source.name,
                                      quiet=quiet)
            continue

        # Auto-dim path: ensure the dim table exists with the -1 and
        # __unknown__ sentinels. New values are discovered and inserted
        # during the ETL transform stage.
        #
        # Deliberately no _backfill_fact_column call here, unlike the two
        # paths around it. An auto-dim table keeps growing, so on every run
        # after the first, `fact` already holds mapped indices and the
        # backfill would re-map them (a row holding index 3 becomes the index
        # of source_id '3'). A column promoted to 'auto-dimension' after rows
        # were loaded therefore keeps its raw values — read
        # docs/star-schema.md ("A dimension column holds surrogate indices")
        # and docs/backlog.md before trying to close that here.
        if d.fact_column in auto_dim_cols:
            _populate_dim_table(dest_engine, schema, d.dim_table, [],
                                source_name=source.name, quiet=quiet)
            continue

        # Source DB path.
        if not d.source_table:
            continue
        if source_engine is None:
            continue

        with source_engine.begin() as src:
            df = pd.read_sql(text(
                f"SELECT {d.source_key} AS source_id, "
                f"       {d.source_name} AS name, "
                f"       {d.source_description} AS description "
                f"  FROM {d.source_table} "
                f" ORDER BY source_id"
            ), src)

        rows = list(zip(df["source_id"].astype(str),
                        df["name"].astype(str),
                        df["description"].astype(str)))
        created = _populate_dim_table(dest_engine, schema, d.dim_table, rows,
                                       source_name=source.name, quiet=quiet)
        if created:
            _backfill_fact_column(dest_engine, schema, d.fact_column,
                                  d.dim_table, source_name=source.name,
                                  quiet=quiet)


def _parse_inline_values(csv_text: str) -> list[tuple[str, str, str]]:
    """Parse inline_values CSV into [(source_id, name, description), ...].

    Format: one row per line, ``source_id,name`` or ``source_id,name,description``.
    Lines starting with ``#`` and empty lines are skipped.  Whitespace around
    fields is stripped.
    """
    rows: list[tuple[str, str, str]] = []
    for line in csv_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        sid = parts[0]
        name = parts[1] if len(parts) > 1 else sid
        desc = parts[2] if len(parts) > 2 else ""
        rows.append((sid, name, desc))
    return rows


def _populate_dim_table(dest_engine, schema: str, dim_table: str,
                         rows: list[tuple[str, str, str]],
                         *, source_name: str = "", quiet: bool = False) -> bool:
    """Create (if missing) and populate a dimension table from (source_id, name, desc) rows.

    Returns True if the dim table was newly created (fact column may need backfill),
    False if it already existed (only new keys were appended).

    Handles: table creation, ``-1`` sentinel for source NULLs, ``__unknown__``
    sentinel, and appending new keys to an existing table.
    """
    # Ensure the -1 sentinel row is present (represents source NULL via nvl).
    has_minus_one = any(r[0] == "-1" for r in rows)
    if not has_minus_one:
        rows = [("-1", "NULL", "source contained NULL value")] + rows

    df = pd.DataFrame(rows, columns=["source_id", "name", "description"])

    with dest_engine.begin() as dst:
        exists = dst.execute(text(
            "SELECT 1 FROM information_schema.tables "
            " WHERE table_schema = :s AND table_name = :t"
        ), {"s": schema, "t": dim_table}).scalar()

        if not exists:
            if not quiet and len(df) == 1:
                print(f"{source_name}: dim table {dim_table} has no values")
            dst.execute(text(
                f"CREATE TABLE {schema}.{dim_table} ("
                f"  index SMALLSERIAL PRIMARY KEY, "
                f"  source_id text, name text, description text, "
                f"  count bigint NOT NULL DEFAULT 0)"
            ))
            df.to_sql(dim_table, dst, schema=schema,
                      if_exists="append", index=False)
            _ensure_unknown_row(dst, schema, dim_table)
            return True  # newly created — fact column may need backfill

        _ensure_unknown_row(dst, schema, dim_table)
        existing = pd.read_sql(text(
            f"SELECT source_id FROM {schema}.{dim_table}"
        ), dst)
        new_keys = set(df["source_id"].astype(str)) - set(existing["source_id"])
        if not new_keys:
            return False
        delta = df[df["source_id"].astype(str).isin(new_keys)].copy()
        if not quiet:
            print(f"{source_name}: +{len(delta)} rows → {dim_table}")
        delta.to_sql(dim_table, dst, schema=schema,
                     if_exists="append", index=False)
        return False  # table existed, only new keys appended


def _backfill_fact_column(dest_engine, schema: str, fact_col: str,
                           dim_table: str, *, source_name: str = "",
                           quiet: bool = False) -> None:
    """Replace raw source values in a fact column with dim table indices.

    When a dimension is added to a fact column that already contains raw source
    values (e.g. cncy_code = 978 for EUR), those values must be replaced with
    the dim table's SMALLSERIAL indices (e.g. 1, 2, 3) so the query layer can
    join correctly.  This is a no-op when the fact column doesn't exist yet
    (next ETL load will populate it correctly via the transform stage).

    Only safe on a dim table that was *just* created, which is why both
    callers gate it on ``created``: the UPDATE matches every row whose value
    reads like a ``source_id``, so on a table that already has mapped rows it
    re-maps the correct ones as well.  See docs/star-schema.md before calling
    it from anywhere else.
    """
    with dest_engine.begin() as conn:
        col_exists = conn.execute(text(
            "SELECT 1 FROM information_schema.columns "
            " WHERE table_schema = :s AND table_name = 'fact' "
            "   AND column_name = :c"
        ), {"s": schema, "c": fact_col}).scalar()

        if not col_exists:
            return

        n = conn.execute(text(
            f"UPDATE {schema}.fact f "
            f"   SET {fact_col} = d.index "
            f"  FROM {schema}.{dim_table} d "
            f" WHERE f.{fact_col}::text = d.source_id "
            f"   AND d.source_id NOT IN (:unk, '-1')"
        ), {"unk": UNKNOWN_SOURCE_ID}).rowcount

        if not quiet:
            print(f"{source_name}: backfilled {fact_col} — {n} rows updated")


def _ensure_unknown_row(conn, schema: str, dim_table: str) -> None:
    """Idempotently insert the catch-all dim row keyed by UNKNOWN_SOURCE_ID."""
    conn.execute(text(
        f"INSERT INTO {schema}.{dim_table} (source_id, name, description) "
        f"SELECT :sid, 'unknown', "
        f"       'source value not present in dim at import time' "
        f" WHERE NOT EXISTS ("
        f"   SELECT 1 FROM {schema}.{dim_table} WHERE source_id = :sid)"
    ), {"sid": UNKNOWN_SOURCE_ID})


def read_dimensions(source: Source, dest_engine
                    ) -> tuple[dict[str, dict], dict[str, int]]:
    """Return ({dim_table: {source_id: index}}, {dim_table: unknown_index}).

    The mapping dict for each dim_table includes str/int/float forms of every
    source_id so the transform stage matches regardless of whether the chunk
    column came back from the source as text or numeric. The unknown index
    is the row inserted by _ensure_unknown_row — used as the fallback when a
    chunk value isn't in the mapping.
    """
    frames: Dict[str, dict] = {}
    unknowns: Dict[str, int] = {}
    schema = source.schema_name
    with dest_engine.begin() as conn:
        for d in source.primary_dimensions():
            df = pd.read_sql(text(
                f"SELECT source_id, index FROM {schema}.{d.dim_table}"
            ), conn)
            unk = df.loc[df["source_id"] == UNKNOWN_SOURCE_ID, "index"]
            if unk.empty:
                raise RuntimeError(
                    f"{schema}.{d.dim_table} is missing the "
                    f"{UNKNOWN_SOURCE_ID!r} sentinel row — re-run "
                    f"core.schema.import_dimensions to add it.")
            unknowns[d.dim_table] = int(unk.iloc[0])

            mapping: dict = {}
            reg = df[df["source_id"] != UNKNOWN_SOURCE_ID]
            for sid, idx in zip(reg["source_id"], reg["index"]):
                idx_int = int(idx)
                mapping[sid] = idx_int
                # also accept the same key under int/float, in case the chunk
                # column comes back numeric (e.g. response from res_code).
                try:
                    mapping[int(sid)] = idx_int
                    mapping[float(sid)] = idx_int
                except (ValueError, TypeError):
                    pass
            frames[d.dim_table] = mapping
    return frames, unknowns


def list_fact_partitions(source: Source, dest_engine) -> list[str]:
    """Return names of all fact child partitions, oldest first.

    Queries pg_inherits + pg_class for child tables of ``{schema}.fact``.
    All three naming conventions (``fact_yYYYYmMM``, ``fact_yIIIIwWW``,
    ``fact_yYYYYmMMdDD``) sort chronologically via plain string order
    within a single partition_period; mixing periods on one source is
    not supported.
    """
    schema = source.schema_name
    with dest_engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT child.relname "
            "  FROM pg_inherits i "
            "  JOIN pg_class parent ON i.inhparent = parent.oid "
            "  JOIN pg_class child  ON i.inhrelid = child.oid "
            "  JOIN pg_namespace ns ON parent.relnamespace = ns.oid "
            " WHERE ns.nspname = :s AND parent.relname = 'fact' "
            " ORDER BY child.relname"
        ), {"s": schema}).scalars().all()
    return list(rows)


def create_future_partitions(source: Source, dest_engine, n: int = 1,
                              *, quiet: bool = False) -> list[str]:
    """Add *n* future partitions (sized by ``source.partition_period``)
    beyond the last existing one.

    Returns the names of newly created partitions.  Idempotent: if a
    partition already exists (``IF NOT EXISTS``), it is silently skipped
    but still included in the return list (the caller asked for n).
    """
    schema = source.schema_name
    period = source.partition_period
    existing = list_fact_partitions(source, dest_engine)
    if not existing:
        raise RuntimeError(f"{schema}.fact has no partitions — run create_fact first")

    last_start = _parse_partition_name(period, existing[-1])
    cur = _next_period(last_start, period)

    created: list[str] = []
    with dest_engine.begin() as conn:
        for _ in range(n):
            nxt = _next_period(cur, period)
            name = _partition_name(period, cur)
            conn.execute(text(
                f"CREATE UNLOGGED TABLE IF NOT EXISTS {schema}.{name} "
                f"PARTITION OF {schema}.fact "
                f"FOR VALUES FROM ('{cur}') TO ('{nxt}')"
            ))
            created.append(name)
            cur = nxt
    if not quiet:
        print(f"{source.name}: created {len(created)} partition(s): "
              f"{', '.join(created)}")
    return created


_MAX_ENSURE_PARTITIONS = 512


def _to_naive_local(dt: datetime) -> datetime:
    """Convert an aware datetime to the naive local-time form the
    partition-boundary arithmetic (_floor_to_period/_next_period, and
    create_fact's own ``datetime.now()``) expects. ``.astimezone()`` with
    no argument converts to the process's system timezone — the same
    thing bare ``datetime.now()`` reflects — so this stays consistent
    with how the existing partitions were bounded without hard-coding a
    timezone name.
    """
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    return dt.replace(tzinfo=None)


def ensure_partitions_for(source: Source, dest_engine, start: datetime, end: datetime,
                          *, back: int = 1, forward: int = 1,
                          quiet: bool = False) -> list[str]:
    """Create every partition covering [start, end] plus a back/forward buffer.

    Unlike ``create_future_partitions(n)``, which appends *n* periods past
    ``existing[-1]``, this takes dates and gap-fills: it creates any
    missing period inside the range even when a later partition already
    exists. That gap is exactly the failure mode this exists for — no
    DEFAULT partition exists (see the note below), partition creation was
    otherwise entirely manual, and a source whose newest partition fell
    behind the calendar had every subsequent ETL run's COPY fail with
    "no partition of relation \"fact\" found for row" — silently, since the
    load worker had no exception handling and its stderr went to
    /dev/null (see EtlStageFailed). ``import_fact`` calls this once per
    run before starting the worker pool, so the partition window is kept
    current automatically instead of depending on a cron job or a GUI
    button click.

    Idempotent (``CREATE TABLE IF NOT EXISTS``) and naming-contract safe:
    every name comes from ``_partition_name()``, so string order stays
    chronological and ``_parse_partition_name(existing[-1])`` still parses
    for ``create_future_partitions``. Returns the names actually created
    (skips ones that already existed).

    ``back``/``forward`` absorb two kinds of skew: ``back`` covers the gap
    between ``source.partition_column`` (what the fact table is ranged on)
    and ``source.watermark_column`` (what the extract window is computed
    from) when they're different columns, and ``forward`` guarantees at
    least one full period of runway past ``end`` so a run that lands
    exactly on a boundary doesn't immediately need another partition next
    time.

    No DEFAULT partition, deliberately: (1) ``list_fact_partitions``
    orders by ``relname`` and every consumer — including
    ``create_future_partitions``'s ``_parse_partition_name(existing[-1])``
    anchor and ``truncate_last_partition``'s ``parts[0]`` — assumes the
    result is chronological and name-parseable; a ``fact_default`` breaks
    both. (2) once a DEFAULT partition holds rows, every future
    ``CREATE TABLE ... PARTITION OF`` must take an ACCESS EXCLUSIVE lock on
    it and scan it to prove no row belongs in the new bound — i.e. this
    function's own per-run partition check would start blocking behind the
    ETL's own COPY stream. (3) it would convert a loud, correct failure
    into silent misrouting: rows would land in an unindexed catch-all,
    partition pruning would stop working for them, and nobody would notice
    for months.
    """
    schema = source.schema_name
    period = source.partition_period
    existing = set(list_fact_partitions(source, dest_engine))
    if not existing:
        raise RuntimeError(f"{schema}.fact has no partitions — run create_fact first")

    cur = _floor_to_period(_to_naive_local(start), period)
    for _ in range(back):
        cur = _prev_period(cur, period)
    stop = _floor_to_period(_to_naive_local(end), period)
    for _ in range(forward):
        stop = _next_period(stop, period)

    created: list[str] = []
    n = 0
    with dest_engine.begin() as conn:
        while cur <= stop:
            n += 1
            if n > _MAX_ENSURE_PARTITIONS:
                raise RuntimeError(
                    f"{source.name}: refusing to create more than "
                    f"{_MAX_ENSURE_PARTITIONS} partitions for window "
                    f"[{start}, {end}] at period {period!r} — check the "
                    f"high watermark / partition_period configuration")
            name = _partition_name(period, cur)
            nxt = _next_period(cur, period)
            if name not in existing:
                conn.execute(text(
                    f"CREATE UNLOGGED TABLE IF NOT EXISTS {schema}.{name} "
                    f"PARTITION OF {schema}.fact "
                    f"FOR VALUES FROM ('{cur}') TO ('{nxt}')"
                ))
                created.append(name)
                existing.add(name)
            cur = nxt
    if created and not quiet:
        logger.info("%s: created %d partition(s): %s",
                   source.name, len(created), ", ".join(created))
    return created


def vacuum_analyze_fact(source: Source, dest_engine, *,
                        quiet: bool = False) -> None:
    """Run VACUUM ANALYZE on the fact table to reclaim space and update stats.

    Uses AUTOCOMMIT isolation because VACUUM cannot run inside a
    transaction block.
    """
    schema = source.schema_name
    with dest_engine.connect() as conn:
        conn.execution_options(isolation_level="AUTOCOMMIT")
        conn.execute(text(f"VACUUM ANALYZE {schema}.fact"))
    if not quiet:
        print(f"{source.name}: VACUUM ANALYZE completed")


def refresh_dim_counts(source: Source, dest_engine, *, quiet: bool = False) -> None:
    """Update the ``count`` column in every dimension table.

    For each primary dimension, sets ``dim.count = (SELECT count(*) FROM fact
    WHERE fact.<col> = dim.index)``.  Used by the timeseries query to order
    series by ascending total count — the chart stacks the smallest series at
    the bottom.  Counts are approximate until the next refresh (typically
    after each ETL run).
    """
    schema = source.schema_name
    with dest_engine.begin() as conn:
        for d in source.primary_dimensions():
            if not d.dim_table:
                continue
            conn.execute(text(
                f"UPDATE {schema}.{d.dim_table} d "
                f"   SET count = sub.c "
                f"  FROM (SELECT {d.fact_column} AS idx, count(*) AS c "
                f"          FROM {schema}.fact "
                f"         GROUP BY {d.fact_column}) sub "
                f" WHERE d.index = sub.idx"
            ))


def drop_schema(source, dest_engine):
    """Drop the per-datasource schema and all its objects (CASCADE)."""
    schema = source.schema_name
    with dest_engine.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
