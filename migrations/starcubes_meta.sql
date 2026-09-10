-- starcubes metadata schema: consolidated initial creation script.
--
-- Combines all previous migrations (0001–0031) into one idempotent script.
-- Safe to re-run: all CREATEs use IF NOT EXISTS, all ALTERs are guarded.
--
-- Per-datasource warehouse tables (fact + dim_*) live in their own schema
-- named by `datasource.schema_name`, created later by core.schema.create_fact
-- and core.schema.import_dimensions.

CREATE SCHEMA IF NOT EXISTS starcubes_meta;

CREATE TABLE IF NOT EXISTS starcubes_meta.datasource (
    id                   serial PRIMARY KEY,
    name                 text UNIQUE NOT NULL,
    schema_name          text UNIQUE NOT NULL,
    source_dialect       text NOT NULL
        CHECK (source_dialect IN ('oracle', 'postgresql', 'mysql', 'mssql')),
    source_dsn           text NOT NULL,
    import_query         text,
    base_table           text,
    watermark_column     text NOT NULL,
    watermark_max_query  text,
    partition_column     text NOT NULL,
    chunksize            int  NOT NULL DEFAULT 10000,
    transform_procs      int  NOT NULL DEFAULT 1,
    small_window_hint    text,
    interval_ladder      int[] DEFAULT '{1,60,3600,86400}',
    bar_pixel_target     real DEFAULT 5,
    pk_format           text,
    partition_period     text NOT NULL DEFAULT 'month'
        CHECK (partition_period IN ('day', 'week', 'month')),
    late_arrival_overlap_s int NOT NULL DEFAULT 0,
    watermark_fact_expr  text,
    watermark_lag_s      int NOT NULL DEFAULT 0,
    wallclock_lag_s      int NOT NULL DEFAULT 0,
    retention_floor      timestamptz,
    dedupe_from          timestamptz,
    housekeeping_interval_s int NOT NULL DEFAULT 21600,
    max_data_lag_s       int NOT NULL DEFAULT 0,
    max_run_s            int NOT NULL DEFAULT 0,
    retention_periods    int NOT NULL DEFAULT 0,
    initial_window_s     int NOT NULL DEFAULT 0,
    dim_refresh_interval_s int NOT NULL DEFAULT 0,
    dim_refreshed_at     timestamptz,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now()
);

-- Add columns that may not exist on older databases (idempotent ALTERs).
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'starcubes_meta' AND table_name = 'datasource'
          AND column_name = 'import_query'
    ) THEN
        -- import_query was NOT NULL in 0001; 0003 made it nullable.
        -- For a fresh install the column is already there (nullable above).
        -- This branch only runs on very old databases that lack it.
        ALTER TABLE starcubes_meta.datasource ADD COLUMN import_query text NOT NULL DEFAULT '';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'starcubes_meta' AND table_name = 'datasource'
          AND column_name = 'base_table'
    ) THEN
        ALTER TABLE starcubes_meta.datasource ADD COLUMN base_table text;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'starcubes_meta' AND table_name = 'datasource'
          AND column_name = 'interval_ladder'
    ) THEN
        ALTER TABLE starcubes_meta.datasource ADD COLUMN interval_ladder int[] DEFAULT '{1,60,3600,86400}';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'starcubes_meta' AND table_name = 'datasource'
          AND column_name = 'bar_pixel_target'
    ) THEN
        ALTER TABLE starcubes_meta.datasource ADD COLUMN bar_pixel_target real DEFAULT 5;
    END IF;

    -- Handle legacy column renames (0006).
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'starcubes_meta' AND table_name = 'datasource'
          AND column_name = 'bars_per_pixel_threshold'
    ) THEN
        ALTER TABLE starcubes_meta.datasource RENAME COLUMN bars_per_pixel_threshold TO bar_pixel_target;
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'starcubes_meta' AND table_name = 'datasource'
          AND column_name = 'max_bar_pixels'
    ) THEN
        ALTER TABLE starcubes_meta.datasource RENAME COLUMN max_bar_pixels TO bar_pixel_target;
    END IF;

    -- bar_pixel_target should be real, not integer.
    ALTER TABLE starcubes_meta.datasource ALTER COLUMN bar_pixel_target TYPE real
        USING bar_pixel_target::real;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'starcubes_meta' AND table_name = 'datasource'
          AND column_name = 'pk_format'
    ) THEN
        ALTER TABLE starcubes_meta.datasource ADD COLUMN pk_format text;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'starcubes_meta' AND table_name = 'datasource'
          AND column_name = 'partition_period'
    ) THEN
        ALTER TABLE starcubes_meta.datasource
            ADD COLUMN partition_period text NOT NULL DEFAULT 'month';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE table_schema = 'starcubes_meta' AND table_name = 'datasource'
          AND constraint_name = 'datasource_partition_period_check'
    ) THEN
        ALTER TABLE starcubes_meta.datasource
            ADD CONSTRAINT datasource_partition_period_check
            CHECK (partition_period IN ('day', 'week', 'month'));
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS starcubes_meta.fact_column (
    id              serial PRIMARY KEY,
    datasource_id   int  NOT NULL REFERENCES starcubes_meta.datasource(id)
                        ON DELETE CASCADE,
    name            text NOT NULL,
    pg_type         text NOT NULL,
    role            text NOT NULL
        CHECK (role IN ('time', 'measure', 'dimension', 'auto-dimension', 'pk')),
    ordinal         int  NOT NULL,
    source_expr     text,
    UNIQUE (datasource_id, name),
    UNIQUE (datasource_id, ordinal)
);

-- Refresh the role CHECK on databases created before 'auto-dimension' was added.
DO $$
DECLARE
    cdef text;
BEGIN
    SELECT pg_get_constraintdef(c.oid) INTO cdef
      FROM pg_constraint c
      JOIN pg_class t ON t.oid = c.conrelid
      JOIN pg_namespace n ON n.oid = t.relnamespace
     WHERE n.nspname = 'starcubes_meta'
       AND t.relname = 'fact_column'
       AND c.conname = 'fact_column_role_check';
    IF cdef IS NOT NULL AND position('auto-dimension' in cdef) = 0 THEN
        ALTER TABLE starcubes_meta.fact_column DROP CONSTRAINT fact_column_role_check;
        ALTER TABLE starcubes_meta.fact_column
            ADD CONSTRAINT fact_column_role_check
            CHECK (role IN ('time', 'measure', 'dimension', 'auto-dimension', 'pk'));
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS starcubes_meta.dimension (
    id                  serial PRIMARY KEY,
    datasource_id       int  NOT NULL REFERENCES starcubes_meta.datasource(id)
                            ON DELETE CASCADE,
    fact_column         text NOT NULL,
    dim_table           text NOT NULL,
    source_table        text,
    source_key          text,
    source_name         text,
    source_description  text,
    is_primary          boolean NOT NULL DEFAULT false,
    inline_values       text,
    filter_group        text,
    UNIQUE (datasource_id, fact_column)
);

-- Add inline_values column if missing (idempotent).
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'starcubes_meta' AND table_name = 'dimension'
          AND column_name = 'inline_values'
    ) THEN
        ALTER TABLE starcubes_meta.dimension ADD COLUMN inline_values text;
    END IF;
END $$;

-- Add source_expr to fact_column if missing (idempotent).
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'starcubes_meta' AND table_name = 'fact_column'
          AND column_name = 'source_expr'
    ) THEN
        ALTER TABLE starcubes_meta.fact_column ADD COLUMN source_expr text;
    END IF;
END $$;

-- At most one primary row per (datasource, dim_table).
CREATE UNIQUE INDEX IF NOT EXISTS ux_dim_primary
    ON starcubes_meta.dimension (datasource_id, dim_table)
    WHERE is_primary;

CREATE TABLE IF NOT EXISTS starcubes_meta.fact_index (
    id              serial PRIMARY KEY,
    datasource_id   int  NOT NULL REFERENCES starcubes_meta.datasource(id)
                        ON DELETE CASCADE,
    column_name     text NOT NULL,
    method          text NOT NULL,
    UNIQUE (datasource_id, column_name)
);

CREATE TABLE IF NOT EXISTS starcubes_meta.etl_run (
    id              serial PRIMARY KEY,
    datasource_id   int  NOT NULL REFERENCES starcubes_meta.datasource(id)
                        ON DELETE CASCADE,
    started_at      timestamptz NOT NULL,
    finished_at     timestamptz,
    first_end_time  timestamptz,
    last_end_time   timestamptz,
    rows            bigint,
    runtime_ms      bigint,
    status          text NOT NULL
        CHECK (status IN ('running', 'ok', 'failed')),
    kind            text NOT NULL DEFAULT 'incremental'
        CHECK (kind IN ('incremental', 'backfill')),
    error           text,
    pid             int,
    progress_rows   bigint,
    progress_at     timestamptz,
    rows_per_s             double precision,
    extract_rows_per_s     double precision,
    transform_rows_per_s   double precision,
    load_rows_per_s        double precision,
    stats                  jsonb,
    log_path               text,
    log_tail               text,
    max_wm                 timestamptz,
    src_now                timestamptz
);

CREATE INDEX IF NOT EXISTS ix_etl_run_last_ok
    ON starcubes_meta.etl_run (datasource_id, finished_at DESC)
    WHERE status = 'ok';

CREATE TABLE IF NOT EXISTS starcubes_meta.etl_unmapped (
    id              serial PRIMARY KEY,
    etl_run_id      int  NOT NULL REFERENCES starcubes_meta.etl_run(id)
                        ON DELETE CASCADE,
    dim_table       text NOT NULL,
    fact_column     text NOT NULL,
    source_value    text NOT NULL,
    rows_count      bigint NOT NULL,
    UNIQUE (etl_run_id, fact_column, source_value)
);

-- dim_group_set: a named group dimension, arbitrarily many per dimension
-- (migration 0020). The slug carries the chart token
-- `<fact_column>:grouped:<slug>`.
CREATE TABLE IF NOT EXISTS starcubes_meta.dim_group_set (
    id              serial PRIMARY KEY,
    dimension_id    int  NOT NULL REFERENCES starcubes_meta.dimension(id) ON DELETE CASCADE,
    name            text NOT NULL,
    slug            text NOT NULL,
    position        int  NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_dim_group_set_slug
    ON starcubes_meta.dim_group_set (dimension_id, slug);

CREATE UNIQUE INDEX IF NOT EXISTS ux_dim_group_set_name
    ON starcubes_meta.dim_group_set (dimension_id, name);

CREATE TABLE IF NOT EXISTS starcubes_meta.dim_group (
    id              serial PRIMARY KEY,
    group_set_id    int NOT NULL REFERENCES starcubes_meta.dim_group_set(id) ON DELETE CASCADE,
    group_name      text NOT NULL,
    color           text NOT NULL DEFAULT ''
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_dim_group_set_group_name
    ON starcubes_meta.dim_group (group_set_id, group_name);

CREATE TABLE IF NOT EXISTS starcubes_meta.dim_group_member (
    id              serial PRIMARY KEY,
    group_id        int NOT NULL REFERENCES starcubes_meta.dim_group(id) ON DELETE CASCADE,
    dim_index       smallint NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_dim_group_member
    ON starcubes_meta.dim_group_member (group_id, dim_index);

CREATE TABLE IF NOT EXISTS starcubes_meta.etl_forever (
    id              serial PRIMARY KEY,
    datasource_id   int NOT NULL REFERENCES starcubes_meta.datasource(id)
                        ON DELETE CASCADE,
    pid             int,
    interval_s      int NOT NULL,
    started_at      timestamptz NOT NULL DEFAULT now(),
    stopped_at      timestamptz,
    status          text NOT NULL
        CHECK (status IN ('running', 'stopping', 'stopped', 'failed')),
    stop_requested  boolean NOT NULL DEFAULT false,
    error           text
);

-- At most one active (running/stopping) forever session per datasource.
CREATE UNIQUE INDEX IF NOT EXISTS ux_etl_forever_active
    ON starcubes_meta.etl_forever (datasource_id)
    WHERE status IN ('running', 'stopping');

-- Keep updated_at in step with the row it belongs to.
CREATE OR REPLACE FUNCTION starcubes_meta.set_updated_at()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_datasource_updated_at ON starcubes_meta.datasource;
CREATE TRIGGER trg_datasource_updated_at
    BEFORE UPDATE ON starcubes_meta.datasource
    FOR EACH ROW EXECUTE FUNCTION starcubes_meta.set_updated_at();

-- Add count column to existing dimension tables (idempotent).
-- Tables created later by import_dimensions already include the column.
DO $$
DECLARE
    rec  RECORD;
BEGIN
    FOR rec IN
        SELECT DISTINCT d.dim_table, ds.schema_name
          FROM starcubes_meta.dimension d
          JOIN starcubes_meta.datasource ds ON ds.id = d.datasource_id
         WHERE d.is_primary AND d.dim_table IS NOT NULL
    LOOP
        -- A datasource's metadata rows legitimately precede its DDL: the GUI's
        -- source_new only INSERTs the datasource, and create_fact /
        -- create_dim_tables run later from a separate endpoint -- or never, for
        -- a half-configured source. So schema_name routinely names a schema
        -- that does not exist yet, and dim_table a table that does not exist
        -- yet. to_regclass returns NULL for both without raising.
        --
        -- The information_schema probe below cannot make that distinction on
        -- its own: "no such column" and "no such table" and "no such schema"
        -- all come back as no rows, and this block used to ALTER in all three
        -- cases. The ALTER then failed the *whole* script -- ensure_starcubes_meta
        -- runs it in one transaction, so a single half-created datasource
        -- rolled back every later migration block and, since bootstrap.py runs
        -- this before migrate/collectstatic, stopped the container from
        -- starting at all.
        --
        -- Skipping loses nothing: create_dim_tables spells out `count bigint
        -- NOT NULL DEFAULT 0` in its own CREATE TABLE, so a table that appears
        -- later already has the column. This block is purely retrofitting
        -- tables that predate it.
        CONTINUE WHEN to_regclass(
            format('%I.%I', rec.schema_name, rec.dim_table)) IS NULL;

        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
             WHERE table_schema = rec.schema_name
               AND table_name   = rec.dim_table
               AND column_name  = 'count'
        ) THEN
            EXECUTE format(
                'ALTER TABLE %I.%I ADD COLUMN count bigint NOT NULL DEFAULT 0',
                rec.schema_name, rec.dim_table);
        END IF;
    END LOOP;
END $$;

COMMENT ON COLUMN starcubes_meta.datasource.interval_ladder IS
  'Per-datasource interval ladder for multi-resolution chart display (seconds per slot, finest to coarsest).';
COMMENT ON COLUMN starcubes_meta.datasource.bar_pixel_target IS
  'Minimum bar width in pixels; the chart picks the coarsest interval where bars are at least this wide.';
COMMENT ON COLUMN starcubes_meta.datasource.pk_format IS
  'Python format string for PK display in chart drill-down. {0} receives the raw PK. When null, raw IDs are shown.';
COMMENT ON COLUMN starcubes_meta.datasource.partition_period IS
  'Granularity of fact-table RANGE partitions: day, week (ISO Monday), or month. Cannot be changed once partitions exist.';
COMMENT ON COLUMN starcubes_meta.dimension.inline_values IS
  'CSV of dimension values (source_id,name[,description]) used when source_table is null. One row per line; lines starting with # are comments.';
COMMENT ON TABLE starcubes_meta.dim_group_set IS
  'A named group dimension: one bucketing of a native dimension. Arbitrarily many per dimension.';
COMMENT ON COLUMN starcubes_meta.dim_group_set.slug IS
  'URL-safe [a-z0-9-] form of name, derived on save. Carries the chart token <fact_column>:grouped:<slug>.';
COMMENT ON COLUMN starcubes_meta.dim_group_set.position IS
  'Display order within the dimension; ties broken by id.';
COMMENT ON TABLE starcubes_meta.dim_group IS
  'Named groups that bucket dimension values for chart aggregation, within one dim_group_set. color is a hex string like #e6194b.';
COMMENT ON TABLE starcubes_meta.dim_group_member IS
  'Maps a dim table row (by index PK) to its group. A dim value belongs to at most one group *per group set* (enforced at application level, not by a cross-group unique constraint).';

-- query_cache: server-side cache for frozen plot_json responses (migration 0012)

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'starcubes_meta' AND table_name = 'datasource'
          AND column_name = 'cache_epoch'
    ) THEN
        ALTER TABLE starcubes_meta.datasource
            ADD COLUMN cache_epoch bigint NOT NULL DEFAULT 0;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS starcubes_meta.query_cache (
    cache_key      text PRIMARY KEY,
    datasource_id  int NOT NULL REFERENCES starcubes_meta.datasource(id)
                       ON DELETE CASCADE,
    cache_epoch    bigint NOT NULL,
    payload        json   NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    last_hit_at    timestamptz NOT NULL DEFAULT now(),
    hits           int    NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS ix_query_cache_lru
    ON starcubes_meta.query_cache (datasource_id, last_hit_at);

COMMENT ON TABLE starcubes_meta.query_cache IS
  'Server-side cache for frozen plot_json responses (count(*), past-only, HWM-bounded).';
COMMENT ON COLUMN starcubes_meta.datasource.cache_epoch IS
  'Bumped whenever historical fact data is mutated. Part of the query_cache key, so a bump orphans old entries.';

-- query_cache.group_by / query_cache.measure: queryable breakdown
-- columns so selective deletion by group_by / measure is possible
-- (the cache_key hash alone is opaque). Migration 0013.

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'starcubes_meta' AND table_name = 'query_cache'
          AND column_name = 'group_by'
    ) THEN
        ALTER TABLE starcubes_meta.query_cache
            ADD COLUMN group_by text NOT NULL DEFAULT '',
            ADD COLUMN measure  text NOT NULL DEFAULT 'count(*)';
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_query_cache_ds_gb
    ON starcubes_meta.query_cache (datasource_id, group_by);
CREATE INDEX IF NOT EXISTS ix_query_cache_ds_measure
    ON starcubes_meta.query_cache (datasource_id, measure);

-- datasource.is_default + measure catalog (migration 0015)

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'starcubes_meta' AND table_name = 'datasource'
          AND column_name = 'is_default'
    ) THEN
        ALTER TABLE starcubes_meta.datasource
            ADD COLUMN is_default boolean NOT NULL DEFAULT false;
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS ux_datasource_default
    ON starcubes_meta.datasource (is_default)
    WHERE is_default;

COMMENT ON COLUMN starcubes_meta.datasource.is_default IS
  'When true, the chart selects this datasource when no ?name= is supplied. At most one row per database (partial unique index). Falls back to first-by-name when none is flagged.';

CREATE TABLE IF NOT EXISTS starcubes_meta.measure (
    id            serial PRIMARY KEY,
    datasource_id int  NOT NULL REFERENCES starcubes_meta.datasource(id)
                      ON DELETE CASCADE,
    name          text NOT NULL,
    agg           text NOT NULL,
    graph_style   text NOT NULL DEFAULT 'multibar'
        CHECK (graph_style IN ('multibar', 'multiline', 'multipoint',
                               'quantile-bands', 'quantile-steps',
                               'quantile-steps-open', 'error-bars', 'candlestick')),
    is_default    boolean NOT NULL DEFAULT false,
    ordinal       int  NOT NULL DEFAULT 0,
    percentiles   text,
    UNIQUE (datasource_id, name)
);

-- Widen the graph_style CHECK on databases created before 'quantile-bands'
-- (and before 'multipoint'/'multiline' / 'quantile-steps' / 'error-bars' /
-- 'candlestick'). Keyed on the newest value so earlier widenings still get
-- upgraded.
DO $$
DECLARE cdef text;
BEGIN
    SELECT pg_get_constraintdef(c.oid) INTO cdef
      FROM pg_constraint c
      JOIN pg_class t ON t.oid = c.conrelid
      JOIN pg_namespace n ON n.oid = t.relnamespace
     WHERE n.nspname = 'starcubes_meta' AND t.relname = 'measure'
       AND c.conname = 'measure_graph_style_check';
    IF cdef IS NOT NULL AND position('candlestick' in cdef) = 0 THEN
        ALTER TABLE starcubes_meta.measure DROP CONSTRAINT measure_graph_style_check;
        ALTER TABLE starcubes_meta.measure
            ADD CONSTRAINT measure_graph_style_check
            CHECK (graph_style IN ('multibar', 'multiline', 'multipoint',
                                   'quantile-bands', 'quantile-steps',
                                   'quantile-steps-open', 'error-bars', 'candlestick'));
    END IF;
END $$;

-- percentiles: per-measure percentile ladder for quantile-bands (migration 0016)
ALTER TABLE starcubes_meta.measure
    ADD COLUMN IF NOT EXISTS percentiles text;

CREATE UNIQUE INDEX IF NOT EXISTS ux_measure_default
    ON starcubes_meta.measure (datasource_id)
    WHERE is_default;

COMMENT ON TABLE starcubes_meta.measure IS
  'User-facing catalog of measurements offered in the chart measure dropdown. agg is an aggregation expression: the literal count(*) or func(col) where func in sum/avg/min/max and col is a fact_column with role=measure. graph_style picks the timeseries.js plot type.';
COMMENT ON COLUMN starcubes_meta.measure.agg IS
  'Aggregation expression. count(*) maps to the empty measure param (count). func(col) is passed through to core.query._parse_measure. For ladder styles (quantile-bands, quantile-steps, quantile-steps-open, error-bars, candlestick) the form is quantile(col), naming the numeric column the percentiles are computed over.';
COMMENT ON COLUMN starcubes_meta.measure.percentiles IS
  'Comma-separated ascending percentile ladder used by all ladder styles (quantile-bands, quantile-steps, quantile-steps-open, error-bars, candlestick), e.g. ''25,50,75'' or ''5,25,50,75,95''. NULL for other styles (the query falls back to 5,25,50,75,95).';

-- ETL throughput metrics + live progress (migration 0017). Same columns
-- exist in CREATE TABLE above so fresh installs get them; this block
-- covers installations whose etl_run table predates this migration.
ALTER TABLE starcubes_meta.etl_run
    ADD COLUMN IF NOT EXISTS pid                    int,
    ADD COLUMN IF NOT EXISTS progress_rows          bigint,
    ADD COLUMN IF NOT EXISTS progress_at            timestamptz,
    ADD COLUMN IF NOT EXISTS rows_per_s             double precision,
    ADD COLUMN IF NOT EXISTS extract_rows_per_s     double precision,
    ADD COLUMN IF NOT EXISTS transform_rows_per_s   double precision,
    ADD COLUMN IF NOT EXISTS load_rows_per_s        double precision,
    ADD COLUMN IF NOT EXISTS stats                  jsonb;
-- Per-run ETL log pointer + failure tail (migration 0021). Same columns
-- exist in CREATE TABLE above so fresh installs get them; this block
-- covers installations whose etl_run table predates this migration.
ALTER TABLE starcubes_meta.etl_run
    ADD COLUMN IF NOT EXISTS log_path               text,
    ADD COLUMN IF NOT EXISTS log_tail               text;
-- query_history: per-user ad-hoc SQL history for the chart Query console
-- (migration 0019). Deduplicated on (user_id, datasource_id, sql_hash) --
-- re-running the same statement bumps last_run_at / run_count instead of
-- appending a row. sql_hash is indexed rather than sql_text because a btree
-- entry is capped at ~2704 bytes.
--
-- user_id deliberately carries NO foreign key to starcubes_meta.auth_user:
-- SourcesConfig.ready() runs ensure_starcubes_meta() (which executes THIS
-- file) during call_command("migrate"), i.e. before Django's own migrations
-- create auth_user. A hard FK would break every fresh install. Cleanup on user
-- deletion happens explicitly in sources/views.py:settings_page.
CREATE TABLE IF NOT EXISTS starcubes_meta.query_history (
    id             bigserial   PRIMARY KEY,
    user_id        int         NOT NULL,
    datasource_id  int         NOT NULL REFERENCES starcubes_meta.datasource(id)
                                   ON DELETE CASCADE,
    sql_hash       text        NOT NULL,
    sql_text       text        NOT NULL,
    first_run_at   timestamptz NOT NULL DEFAULT now(),
    last_run_at    timestamptz NOT NULL DEFAULT now(),
    run_count      int         NOT NULL DEFAULT 1
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_query_history_key
    ON starcubes_meta.query_history (user_id, datasource_id, sql_hash);

CREATE INDEX IF NOT EXISTS ix_query_history_recent
    ON starcubes_meta.query_history (user_id, datasource_id, last_run_at DESC);

COMMENT ON TABLE starcubes_meta.query_history IS
  'Per-user ad-hoc SQL history for the chart Query console, deduplicated per (user, datasource, sql).';
COMMENT ON COLUMN starcubes_meta.query_history.user_id IS
  'auth_user.id. No FK on purpose: auth_user is created by manage.py migrate, which runs after ensure_starcubes_meta on a fresh install.';
COMMENT ON COLUMN starcubes_meta.query_history.sql_hash IS
  'sha256 hex of the normalized SQL. Indexed instead of sql_text because btree entries are size-capped at ~2704 bytes.';
COMMENT ON COLUMN starcubes_meta.query_history.run_count IS
  'Incremented by the ON CONFLICT DO UPDATE upsert in core.adhoc.history_touch.';

-- Late-arriving transactions: overlap re-fetch + backfill runs (migration 0022).
--
-- The source DB does not guarantee that rows become visible in
-- watermark_column order. The ETL loads [last_end_time, max(watermark_column))
-- and advances the HWM to that max, so a row that only becomes visible
-- afterwards with a watermark below the HWM drops out of every future window
-- and is lost for good.
--
--   late_arrival_overlap_s  pulls the window's lower bound back by N seconds
--                           on every incremental run. Rows already in fact are
--                           dropped by PK in core/etl.py's extract loop, so
--                           the re-fetch is idempotent. Default 0 keeps the
--                           historical behaviour for existing datasources --
--                           the right value is deployment-specific and is
--                           measured with the Audit tab, not guessed.
--
--   watermark_fact_expr     fact-side SQL reproducing watermark_column, used
--                           to select the already-loaded rows of the overlap
--                           window. NULL falls back to partition_column.
--                           Needed because the watermark is not always a
--                           projected fact column: the example Oracle source
--                           watermarks on end_time but stores create_time plus
--                           a `total` duration. core/etl.py:_resume_state used
--                           to hard-code exactly that expression, which broke
--                           every datasource without a `total` column.
--
--   etl_run.kind            'backfill' runs re-load a closed historical window
--                           and must not move the HWM. _resume_state picks the
--                           newest ok run by finished_at, so without this flag
--                           a backfill of an old window would rewind the
--                           watermark and skip everything after it.
ALTER TABLE starcubes_meta.datasource
    ADD COLUMN IF NOT EXISTS late_arrival_overlap_s int NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS watermark_fact_expr    text;

ALTER TABLE starcubes_meta.etl_run
    ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'incremental';

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE table_schema = 'starcubes_meta' AND table_name = 'etl_run'
          AND constraint_name = 'etl_run_kind_check'
    ) THEN
        ALTER TABLE starcubes_meta.etl_run
            ADD CONSTRAINT etl_run_kind_check
            CHECK (kind IN ('incremental', 'backfill'));
    END IF;
END $$;

COMMENT ON COLUMN starcubes_meta.datasource.late_arrival_overlap_s IS
  'Seconds the incremental load window reaches back past the high watermark, to catch rows that became visible out of watermark order. Requires a role=pk fact column; duplicates are filtered by PK in core/etl.py. 0 disables the overlap.';
COMMENT ON COLUMN starcubes_meta.datasource.watermark_fact_expr IS
  'Fact-side SQL expression reproducing watermark_column (e.g. create_time + total * interval ''1 millisecond''). Used to find already-loaded rows in the overlap window and by the Audit tab. NULL falls back to partition_column.';
COMMENT ON COLUMN starcubes_meta.etl_run.kind IS
  'incremental (advances the HWM) or backfill (re-loads a closed historical window; ignored by core/etl.py:_resume_state so it cannot rewind the watermark).';

-- Two-term upper bound for the incremental load window (migration 0023).
--
-- Migration 0022 pulled the window's *lower* bound back past the HWM
-- (late_arrival_overlap_s) so late-arriving rows get re-fetched. That works,
-- but it costs a PK scan of the fact window on every run, re-extracts rows
-- only to throw them away, and leaves the HWM dishonest -- rows can still
-- appear behind it, which is why core/cache.py has to shorten the cacheable
-- horizon by the overlap.
--
-- Holding the window's *upper* bound back instead is idempotent by
-- construction: the window advances monotonically, every watermark value
-- falls in exactly one window, duplicates are impossible. The price is tip
-- freshness. The two mechanisms are complementary, not alternatives -- the
-- lag covers a bounded visibility delay outright, the overlap catches the
-- tail beyond it after the fact.
--
-- The upper bound is the later of two terms:
--
--     end_time = max( max(watermark_column) - watermark_lag_s,
--                     source_wall_clock     - wallclock_lag_s )
--
--   watermark_lag_s  a relative buffer below the data tip. Takes the tip as
--                    proof of freshness and is therefore fast -- but only
--                    works while the tip moves. If source traffic stops, this
--                    term freezes and the last watermark_lag_s seconds would
--                    sit unloaded until traffic resumes.
--
--   wallclock_lag_s  an absolute deadline: every row gets this many seconds
--                    of source wall-clock time to become visible. Independent
--                    of traffic volume, so it keeps the window advancing when
--                    the first term freezes.
--
-- Taking the maximum picks whichever is more aggressive. The crossover point
-- is not configured -- it falls out: setting the terms equal gives
-- now - max(wm) = wallclock_lag_s - watermark_lag_s, so the wall clock takes
-- over exactly once the standstill exceeds the difference of the two lags.
-- That is why wallclock_lag_s must exceed watermark_lag_s: otherwise the wall
-- clock term would win even under running traffic (where max(wm) ~ now) and
-- watermark_lag_s would have no effect at all.
--
-- The result is then clamped: end_time never exceeds max(watermark). This is
-- a hard invariant, not a refinement -- letting the HWM advance over a range
-- the source has not produced yet means every row later arriving in that
-- range sits below the HWM and is lost silently and permanently.
--
-- The clamp exists because of a real incident. The wall-clock term reads
-- LOCALTIMESTAMP on the *source session*, and nothing tells us which timezone
-- the watermark column was written in. An Oracle source writing end_time in
-- UTC while its session sat in Europe/Berlin skewed src_now by +7200s, so the
-- wall-clock term won every run and dragged the HWM two hours past the data;
-- every subsequent run then loaded an empty future window while live rows
-- fell below the watermark. An earlier revision argued the term was "a named
-- deadline and may therefore carry the HWM" -- that argument silently assumed
-- both values share a clock, and was wrong.
--
-- The clamp costs nothing real: under a genuine standstill the wall-clock
-- term still closes the window right up to max(watermark), which is all there
-- is to load. It only stops it running off into empty time.
--
-- The wall clock is read as UTC on the source connection (Source.
-- source_now_sql(): SYS_EXTRACT_UTC(SYSTIMESTAMP), CURRENT_TIMESTAMP AT TIME
-- ZONE 'UTC', ...), so the source session's timezone does not matter. It has
-- to be UTC because naive source timestamps are treated as UTC everywhere
-- else in the pipeline. An earlier revision used LOCALTIMESTAMP -- the
-- *session* clock -- which put a production Oracle source 7203s ahead of its
-- own data.
--
-- What is still not detectable is a source whose time columns do not hold
-- UTC at all. The clamp above bounds the damage to freshness, and core/etl.py
-- logs the measured src_now - max_wm skew next to both candidates so the
-- misconfiguration is at least visible.
--
-- wallclock_lag_s = 0 drops the second term entirely rather than setting it
-- to a zero-length deadline. max(max_wm, source_now) would be source_now,
-- i.e. no lag at all but its opposite. With both columns 0 the window is
-- computed exactly as it was before this migration.
ALTER TABLE starcubes_meta.datasource
    ADD COLUMN IF NOT EXISTS watermark_lag_s int NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS wallclock_lag_s int NOT NULL DEFAULT 0;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE table_schema = 'starcubes_meta' AND table_name = 'datasource'
          AND constraint_name = 'datasource_wallclock_lag_check'
    ) THEN
        ALTER TABLE starcubes_meta.datasource
            ADD CONSTRAINT datasource_wallclock_lag_check
            CHECK (wallclock_lag_s = 0 OR wallclock_lag_s > watermark_lag_s);
    END IF;
END $$;

COMMENT ON COLUMN starcubes_meta.datasource.watermark_lag_s IS
  'Seconds the load window''s upper bound stays below max(watermark_column). Trades tip freshness for idempotence: no re-fetch, no PK de-dupe, and the HWM stays honest. Complements late_arrival_overlap_s, which instead reaches back below the HWM and pays a per-run PK check. 0 disables.';
COMMENT ON COLUMN starcubes_meta.datasource.wallclock_lag_s IS
  'Absolute deadline in source wall-clock seconds: every row gets this long to become visible. The window''s upper bound is the later of max(watermark) - watermark_lag_s and source_now - wallclock_lag_s, so this term keeps the window advancing when source traffic pauses and the first term freezes. Must exceed watermark_lag_s. 0 disables the term entirely -- that is not the same as a zero-length deadline.';


-- Backstop for datasource.schema_name (0024). The name is interpolated
-- unquoted into the warehouse DDL while pandas' to_sql quotes it, so a name
-- that is not case-fold-stable makes the two disagree ("CREATE SCHEMA Test"
-- creates `test`; to_sql then looks for `"Test"`). The real guard is
-- core.schema.validate_schema_name() at every write path. NOT VALID so
-- installations holding an older folded name stay saveable.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
          JOIN pg_class t ON t.oid = c.conrelid
          JOIN pg_namespace n ON n.oid = t.relnamespace
         WHERE n.nspname = 'starcubes_meta'
           AND t.relname = 'datasource'
           AND c.conname = 'datasource_schema_name_check'
    ) THEN
        ALTER TABLE starcubes_meta.datasource
            ADD CONSTRAINT datasource_schema_name_check
            CHECK (schema_name ~ '^[a-z_][a-z0-9_]{0,62}$') NOT VALID;
    END IF;
END $$;


-- 0025_alarm.sql — per-datasource threshold alarms. See migrations/0025_alarm.sql.
-- Mirror block: keeping starcubes_meta.sql in sync with the per-step file is
-- the contract verified in CLAUDE.md §Verification.

CREATE TABLE IF NOT EXISTS starcubes_meta.alarm (
    id              serial PRIMARY KEY,
    datasource_id   int  NOT NULL REFERENCES starcubes_meta.datasource(id) ON DELETE CASCADE,
    name            text NOT NULL,
    context_filter  jsonb,
    comment         text,
    eval_end        timestamptz,
    measure         text NOT NULL DEFAULT 'count(*)',
    cond_op         text NOT NULL DEFAULT '>'
                        CHECK (cond_op IN ('>', '<', '=', '>=', '<=')),
    threshold       double precision NOT NULL,
    window_s        int  NOT NULL DEFAULT 300
                        CHECK (window_s > 0),
    notify_channels text[] NOT NULL DEFAULT '{}',
    enabled         boolean NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (datasource_id, name)
);

CREATE INDEX IF NOT EXISTS ix_alarm_datasource
    ON starcubes_meta.alarm (datasource_id);


-- 0026_alarm_enabled.sql — backstop for installs created by the 0025 block
-- above, which shipped without `enabled`. CREATE TABLE IF NOT EXISTS leaves
-- an existing table alone, so the column has to be added explicitly.

ALTER TABLE starcubes_meta.alarm
    ADD COLUMN IF NOT EXISTS enabled boolean NOT NULL DEFAULT true;

COMMENT ON COLUMN starcubes_meta.alarm.enabled IS
    'Rule is active. Persisted and editable, but no evaluation loop reads it yet — see docs/backlog.md.';


-- 0027_dimension_filter_group.sql — backstop for installs created by the
-- dimension block above, whose CREATE TABLE IF NOT EXISTS leaves an existing
-- table alone, so the column has to be added explicitly.

ALTER TABLE starcubes_meta.dimension
    ADD COLUMN IF NOT EXISTS filter_group text;

COMMENT ON COLUMN starcubes_meta.dimension.filter_group IS
    'Fan-out filter group: rows sharing a non-null value form one chart filter that ORs over every member fact column (e.g. filter_group=so_peer over so_peer_1/2/3). NULL = ordinary single-column filter.';


-- 0028_alarm_context.sql — backstop for installs created by the 0025 block
-- above, whose CREATE TABLE IF NOT EXISTS leaves an existing table alone:
-- the context pair is dropped and the new columns added explicitly. The
-- three new tables are byte-identical to the per-step file. See
-- migrations/0028_alarm_context.sql for the semantics.

ALTER TABLE starcubes_meta.alarm DROP COLUMN IF EXISTS context_dim;
ALTER TABLE starcubes_meta.alarm DROP COLUMN IF EXISTS context_value;

ALTER TABLE starcubes_meta.alarm
    ADD COLUMN IF NOT EXISTS context_filter jsonb;
ALTER TABLE starcubes_meta.alarm
    ADD COLUMN IF NOT EXISTS comment text;
ALTER TABLE starcubes_meta.alarm
    ADD COLUMN IF NOT EXISTS eval_end timestamptz;

COMMENT ON COLUMN starcubes_meta.alarm.context_filter IS
    'Alarm context: the chart''s flat filter rows [{col, op, vals, andOr, indent}] as jsonb. NULL/[] = the whole datasource. Grouped tokens (<col>:grouped:<slug>) are rejected at save time.';
COMMENT ON COLUMN starcubes_meta.alarm.comment IS
    'Free-text comment on this alarm. Changes are tracked through alarm_history snapshots like any other field.';
COMMENT ON COLUMN starcubes_meta.alarm.eval_end IS
    'The data high watermark this alarm was last evaluated at (fact clock). NULL = never evaluated. Every ETL load evaluates one trailing window [eval_end - window_s, eval_end); a load that does not move the HWM is skipped. Written by the evaluator only, never touching updated_at.';

CREATE TABLE IF NOT EXISTS starcubes_meta.alarm_history (
    id         serial PRIMARY KEY,
    alarm_id   int  NOT NULL REFERENCES starcubes_meta.alarm(id) ON DELETE CASCADE,
    kind       text NOT NULL CHECK (kind IN ('created', 'updated')),
    username   text NOT NULL,
    old_row    jsonb,
    new_row    jsonb,
    changed_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_alarm_history_alarm
    ON starcubes_meta.alarm_history (alarm_id, id DESC);

CREATE TABLE IF NOT EXISTS starcubes_meta.alarm_firing (
    id              serial PRIMARY KEY,
    alarm_id        int  NOT NULL REFERENCES starcubes_meta.alarm(id) ON DELETE CASCADE,
    first_breach_at timestamptz NOT NULL,
    last_breach_at  timestamptz NOT NULL,
    closed_at       timestamptz,
    first_value     double precision,
    last_value      double precision
);

CREATE INDEX IF NOT EXISTS ix_alarm_firing_alarm
    ON starcubes_meta.alarm_firing (alarm_id, id DESC);

-- At most one open episode (open = closed_at IS NULL) per alarm.
CREATE UNIQUE INDEX IF NOT EXISTS ux_alarm_firing_open
    ON starcubes_meta.alarm_firing (alarm_id) WHERE closed_at IS NULL;

-- 0029_alarm_hwm_semantics.sql — alarms are evaluated by the ETL load, at
-- the data high watermark. See migrations/0029_alarm_hwm_semantics.sql.
-- Mirror block.

-- The global evaluator session table (alarm_forever) is retired: etl_run
-- evaluates each datasource's alarms after its own load, so there is no
-- standalone loop left to pin a session for.
DROP TABLE IF EXISTS starcubes_meta.alarm_forever;

COMMENT ON COLUMN starcubes_meta.alarm_firing.first_breach_at IS
    'The data high watermark at the load that first saw the threshold breached — the start of the episode on the data''s own time axis.';
COMMENT ON COLUMN starcubes_meta.alarm_firing.last_breach_at IS
    'The data high watermark at the most recent load that still saw the threshold breached.';
COMMENT ON COLUMN starcubes_meta.alarm_firing.closed_at IS
    'The data high watermark at the first load that saw the value back under the threshold. NULL = the episode is still open.';


-- 0030_alarm_notify.sql — alarm notification transports: email, XMPP and
-- PagerDuty. Recipients are the login users (auth_user.email, new
-- auth_user.xmpp_jid); per-alarm selection is two int[] columns; the
-- PagerDuty routing key is per datasource. See migrations/0030_alarm_notify.sql
-- for the semantics. Mirror block.

-- The guard: bootstrap runs this script BEFORE `migrate`, when auth_user
-- does not exist yet on a fresh install; bootstrap.py runs it a second
-- time after `migrate` to pick the column up there. Both the ALTER and the
-- COMMENT are guarded — a COMMENT on a missing table is an error, not a
-- notice, and would abort the script.
DO $$ BEGIN
  IF to_regclass('starcubes_meta.auth_user') IS NOT NULL
     AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                     WHERE table_schema = 'starcubes_meta'
                       AND table_name   = 'auth_user'
                       AND column_name  = 'xmpp_jid')
  THEN ALTER TABLE starcubes_meta.auth_user ADD COLUMN xmpp_jid text;
  END IF;
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_schema = 'starcubes_meta'
               AND table_name   = 'auth_user'
               AND column_name  = 'xmpp_jid')
  THEN
    EXECUTE 'COMMENT ON COLUMN starcubes_meta.auth_user.xmpp_jid IS'
            '''XMPP/Jabber address of this account, used as the alarm-notification target for the xmpp channel. Maintained on the settings page; the Django model does not know this column. NULL = this account cannot be reached over XMPP.''';
  END IF;
END $$;

ALTER TABLE starcubes_meta.alarm
    ADD COLUMN IF NOT EXISTS notify_email_recipients int[] NOT NULL DEFAULT '{}';
ALTER TABLE starcubes_meta.alarm
    ADD COLUMN IF NOT EXISTS notify_xmpp_recipients int[] NOT NULL DEFAULT '{}';

COMMENT ON COLUMN starcubes_meta.alarm.notify_email_recipients IS
    'auth_user ids selected as the e-mail recipients of this alarm, resolved through auth_user.email at dispatch time. Empty = no e-mail recipients (an alarm may then not select the email channel). No foreign key is possible on array elements; settings_page removes a deleted user''s id from here.';
COMMENT ON COLUMN starcubes_meta.alarm.notify_xmpp_recipients IS
    'auth_user ids selected as the XMPP recipients of this alarm, resolved through auth_user.xmpp_jid at dispatch time. Empty = no XMPP recipients (an alarm may then not select the xmpp channel). No foreign key is possible on array elements; settings_page removes a deleted user''s id from here.';

ALTER TABLE starcubes_meta.datasource
    ADD COLUMN IF NOT EXISTS pagerduty_routing_key text;

COMMENT ON COLUMN starcubes_meta.datasource.pagerduty_routing_key IS
    'PagerDuty Events API v2 routing key (integration token) of the PagerDuty service that carries this datasource''s alarms. One service per source: every alarm on the datasource triggers into the same key. NULL = PagerDuty disabled for this source. Credential — maintained on the password-type field on the source edit page, never exported in a profile.';


-- 0031_app_config.sql — one key-value table for global app configuration
-- the operator edits in the GUI instead of passing env vars: notification
-- transports and SSO/OIDC. Key = the env-var name it shadows; resolution
-- is env-first (`core/config.py`), an absent row means "not configured
-- here". Secrets plaintext like datasource.source_dsn. See
-- migrations/0031_app_config.sql for the semantics. Mirror block.

CREATE TABLE IF NOT EXISTS starcubes_meta.app_config (
    key        text PRIMARY KEY,
    value      text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by text NOT NULL
);

COMMENT ON TABLE starcubes_meta.app_config IS
    'Global app configuration as key-value rows, one table for every setting an operator may edit in the GUI instead of passing an environment variable: notification transports (STARCUBES_SMTP_*, STARCUBES_XMPP_*, STARCUBES_PAGERDUTY_*) and SSO/OIDC (STARCUBES_OIDC_*). The key is the env-var name it shadows.';

COMMENT ON COLUMN starcubes_meta.app_config.key IS
    'The STARCUBES_* environment variable name this row shadows. Only the 17 transport/SSO keys are read; writing any other key has no effect. Resolution is env-first: a set env var beats the row, the row beats the hardcoded default.';
COMMENT ON COLUMN starcubes_meta.app_config.value IS
    'The setting''s value as text. Deleting a row falls back to the environment/default — there is no empty-string state.';
COMMENT ON COLUMN starcubes_meta.app_config.updated_by IS
    'Username of the admin who last wrote the row, for the same provenance alarm_history keeps. Credentials are stored like datasource.source_dsn: plaintext here, masked in every listing, never logged.';


-- 0032_alarm_severity_optin.sql — PagerDuty severity moves to the alarm;
-- recipients get an opt-in for the two personal channels. Mirror block; see
-- migrations/0032_alarm_severity_optin.sql for the semantics.

ALTER TABLE starcubes_meta.alarm
    ADD COLUMN IF NOT EXISTS severity text;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                 WHERE conname = 'alarm_severity_check'
                   AND conrelid = 'starcubes_meta.alarm'::regclass)
  THEN ALTER TABLE starcubes_meta.alarm
           ADD CONSTRAINT alarm_severity_check
           CHECK (severity IS NULL
                  OR severity IN ('critical', 'error', 'warning', 'info'));
  END IF;
END $$;

COMMENT ON COLUMN starcubes_meta.alarm.severity IS
    'PagerDuty severity of this alarm''s trigger/resolve events: critical, error, warning or info. NULL/empty = fall back to STARCUBES_PAGERDUTY_SEVERITY at dispatch time (send_pagerduty_test always uses the global value — it tests the transport, not an alarm''s routing choice).';

-- The Django model does not know these columns; sources/views.py and
-- sources/alarm_notify.py read and write them via raw SQL. Both the ALTER
-- and the COMMENT are guarded: bootstrap runs this script BEFORE `migrate`,
-- when auth_user does not exist yet on a fresh install — a COMMENT on a
-- missing table is an error, not a notice, and would abort the script.
DO $$ BEGIN
  IF to_regclass('starcubes_meta.auth_user') IS NOT NULL
     AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                     WHERE table_schema = 'starcubes_meta'
                       AND table_name   = 'auth_user'
                       AND column_name  = 'notify_email')
  THEN ALTER TABLE starcubes_meta.auth_user
           ADD COLUMN notify_email boolean NOT NULL DEFAULT false;
  END IF;
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_schema = 'starcubes_meta'
               AND table_name   = 'auth_user'
               AND column_name  = 'notify_email')
  THEN
    EXECUTE 'COMMENT ON COLUMN starcubes_meta.auth_user.notify_email IS'
            '''Deliberate default-off opt-in: the account receives alarm e-mail only when this is true AND the alarm lists the account in notify_email_recipients. Maintained on My Profile; the Django model does not know this column.''';
  END IF;
  IF to_regclass('starcubes_meta.auth_user') IS NOT NULL
     AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                     WHERE table_schema = 'starcubes_meta'
                       AND table_name   = 'auth_user'
                       AND column_name  = 'notify_xmpp')
  THEN ALTER TABLE starcubes_meta.auth_user
           ADD COLUMN notify_xmpp boolean NOT NULL DEFAULT false;
  END IF;
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_schema = 'starcubes_meta'
               AND table_name   = 'auth_user'
               AND column_name  = 'notify_xmpp')
  THEN
    EXECUTE 'COMMENT ON COLUMN starcubes_meta.auth_user.notify_xmpp IS'
            '''Deliberate default-off opt-in: the account receives alarm XMPP messages only when this is true AND the alarm lists the account in notify_xmpp_recipients. Maintained on My Profile; the Django model does not know this column.''';
  END IF;
END $$;


-- 0033_retention_floor_dedupe.sql — what the watermark does when fact rows
-- disappear. Deleting means two opposite things: delete_fact_from is a
-- repair (rewind the watermark, reload the window), drop/truncate-partition
-- is disposal (keep the watermark, record a floor). retention_floor is that
-- floor, dedupe_from arms the existing _overlap_pks filter for the span a
-- rewind reloads. Mirror block; see migrations/0033_retention_floor_dedupe.sql
-- for the semantics.
ALTER TABLE starcubes_meta.datasource
    ADD COLUMN IF NOT EXISTS retention_floor timestamptz,
    ADD COLUMN IF NOT EXISTS dedupe_from     timestamptz;

COMMENT ON COLUMN starcubes_meta.datasource.retention_floor IS
  'Rows with partition_column below this instant were disposed of on purpose (a dropped or truncated partition), not lost. core/audit.py clamps its windows up to it so a retired window is not reported as a gap. Only advanced when the oldest partition goes -- a hole in the middle cannot be expressed as a floor. NULL means nothing has been retired.';
COMMENT ON COLUMN starcubes_meta.datasource.dedupe_from IS
  'Set by a high-watermark rewind (delete_fact_from): the next incremental run must load with the _overlap_pks PK filter armed from this instant, even when late_arrival_overlap_s is 0, or the reload duplicates the rows the delete did not remove. Cleared by the run that honoured it. NULL means no rewind is pending.';


-- 0034_housekeeping.sql — the periodic work that cannot live inside a load:
-- targeted ANALYZE and BRIN summarisation (COPY-only tables never trigger
-- autovacuum's scale factors, so statistics rot and BRIN ranges are never
-- summarised — the index silently stops covering the newest data),
-- dimension fill levels against the smallserial ceiling, and partition
-- sizes recorded as a series. Interval defaults to 6h and is deliberately
-- *on*: the damage is silent and identical everywhere, so off-by-default
-- would just mean nobody runs it. Mirror block; see
-- migrations/0034_housekeeping.sql for the semantics.
ALTER TABLE starcubes_meta.datasource
    ADD COLUMN IF NOT EXISTS housekeeping_interval_s int NOT NULL DEFAULT 21600;

COMMENT ON COLUMN starcubes_meta.datasource.housekeeping_interval_s IS
  'Seconds between automatic `manage.py etl_housekeeping` runs, checked by the forever loop after each load. Targeted ANALYZE + BRIN summarisation + dimension fill levels + partition sizes. Default 6h and deliberately on: stale statistics and unsummarised BRIN ranges degrade silently on every installation, so an off-by-default setting would only mean nobody runs it. 0 disables the automatic run; the command still works by hand.';

CREATE TABLE IF NOT EXISTS starcubes_meta.housekeeping_run (
    id              serial PRIMARY KEY,
    datasource_id   int NOT NULL REFERENCES starcubes_meta.datasource(id)
                        ON DELETE CASCADE,
    ran_at          timestamptz NOT NULL DEFAULT now(),
    runtime_ms      bigint,
    trigger         text NOT NULL DEFAULT 'manual'
        CHECK (trigger IN ('manual', 'forever')),
    report          jsonb
);

CREATE INDEX IF NOT EXISTS ix_housekeeping_run_ds
    ON starcubes_meta.housekeeping_run (datasource_id, ran_at DESC);

COMMENT ON TABLE starcubes_meta.housekeeping_run IS
  'One row per `manage.py etl_housekeeping` run. The forever loop reads the newest ran_at per datasource to decide whether housekeeping_interval_s has elapsed.';
COMMENT ON COLUMN starcubes_meta.housekeeping_run.trigger IS
  'manual (CLI or GUI button) or forever (the loop reached the interval).';
COMMENT ON COLUMN starcubes_meta.housekeeping_run.report IS
  'What was done and measured: the ANALYZEd partitions, BRIN ranges summarised per index, dimension fill levels against the smallserial ceiling of 32767, and pg_total_relation_size per fact partition. Kept as history -- the single newest measurement is already on the Storage tab; the series is the point.';


-- 0035_pipeline_observability.sql — watch the pipeline, not the data. The
-- one failure nothing could see was a healthy source that stopped
-- producing: the HWM freezes, so alarms cannot fire by construction and the
-- ETL just logs rows=0. etl_run.max_wm / src_now persist the two numbers
-- every run already reads (and src_now is now read on every run, not only
-- with wallclock_lag_s set); datasource.max_data_lag_s is the threshold on
-- their difference, datasource.max_run_s a wall-clock budget for one run
-- that also bounds the source statements inside it. Both off by default.
-- Mirror block; see migrations/0035_pipeline_observability.sql for the
-- semantics.
ALTER TABLE starcubes_meta.etl_run
    ADD COLUMN IF NOT EXISTS max_wm  timestamptz,
    ADD COLUMN IF NOT EXISTS src_now timestamptz;

ALTER TABLE starcubes_meta.datasource
    ADD COLUMN IF NOT EXISTS max_data_lag_s int NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS max_run_s      int NOT NULL DEFAULT 0;

COMMENT ON COLUMN starcubes_meta.etl_run.max_wm IS
  'max(watermark_column) as read from the source at the start of this run -- the data tip, in the source''s own clock. NULL on a --start/--end or backfill run, which does not ask.';
COMMENT ON COLUMN starcubes_meta.etl_run.src_now IS
  'The source database''s own wall clock at that same moment, read as UTC (Source.source_now_sql, deliberately not the session clock). src_now - max_wm is how far the newest row lags the source''s present: unbounded growth means the source stopped producing (U9), a value parked near a whole-hour offset while rows keep arriving means the watermark column is not UTC (N12).';
COMMENT ON COLUMN starcubes_meta.datasource.max_data_lag_s IS
  'Seconds of src_now - max_wm above which the source counts as having stopped producing. Reported by the preflight. 0 = unset, which is the honest setting for any source that does not produce continuously -- a batch source would otherwise raise a false alarm every night.';
COMMENT ON COLUMN starcubes_meta.datasource.max_run_s IS
  'Wall-clock budget for one run. Past it the parent raises EtlAborted, the workers are reaped and the row lands as failed; the same value also bounds every source statement inside the run (statement_timeout / call_timeout per dialect). Never applied to a backfill or to the initial load, which are legitimately long. 0 = unset. A circuit breaker, not self-healing: the next window is wider, not smaller, so pair it with --limit if runs are genuinely too big.';


-- 0036_pipeline_alarm.sql — a second kind of alarm, whose condition is the
-- pipeline rather than the data. Everything the preflight, the housekeeping
-- and etl_run.max_wm/src_now detect had no way of reaching a person; the
-- alarm subsystem already owns episodes, channels, opt-in and dedup, so this
-- is a second kind of row in it rather than a new subsystem. Reuses
-- threshold/cond_op (every condition yields a number), the notification
-- path and the Firings view; ignores measure/window_s/context_filter.
-- Called rule_kind, NOT kind — the transition dicts already carry a `kind`
-- meaning opened/closed and an alarm column of that name would be stamped
-- over it, turning every notification into a resolution. Episodes are
-- stamped with the wall clock, which is the only clock a "nothing has
-- arrived since" question has. Mirror block; see
-- migrations/0036_pipeline_alarm.sql for the semantics.
ALTER TABLE starcubes_meta.alarm
    ADD COLUMN IF NOT EXISTS rule_kind text NOT NULL DEFAULT 'threshold',
    ADD COLUMN IF NOT EXISTS condition text;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE table_schema = 'starcubes_meta' AND table_name = 'alarm'
          AND constraint_name = 'alarm_rule_kind_check'
    ) THEN
        ALTER TABLE starcubes_meta.alarm
            ADD CONSTRAINT alarm_rule_kind_check
            CHECK (rule_kind IN ('threshold', 'pipeline'));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE table_schema = 'starcubes_meta' AND table_name = 'alarm'
          AND constraint_name = 'alarm_condition_check'
    ) THEN
        ALTER TABLE starcubes_meta.alarm
            ADD CONSTRAINT alarm_condition_check
            CHECK ((rule_kind = 'pipeline' AND condition IN (
                        'data_stale', 'run_failed', 'no_recent_run',
                        'preflight_warn', 'dim_full_pct'))
                   OR (rule_kind = 'threshold' AND condition IS NULL));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_alarm_rule_kind
    ON starcubes_meta.alarm (rule_kind) WHERE enabled;

COMMENT ON COLUMN starcubes_meta.alarm.rule_kind IS
  'threshold = a measure over fact rows in a trailing window; pipeline = a condition over ETL state (etl_run, the preflight report, the housekeeping report). Named rule_kind and not kind on purpose: the transition dicts sent to the notification dispatcher already carry a `kind` key meaning opened/closed, and an alarm column of that name would be stamped over it, turning every notification into a resolution.';
COMMENT ON COLUMN starcubes_meta.alarm.condition IS
  'Which pipeline condition, for rule_kind = pipeline; NULL otherwise. Each produces a number, so cond_op and threshold decide the breach exactly as they do for a threshold alarm. measure, window_s and context_filter are unused by a pipeline alarm and keep their defaults.';


-- 0037_retention_and_first_load.sql — the three knobs that change what the
-- rest of the system may assume about the fact table. retention_periods
-- finally disposes of old partitions (four subsystems had to learn about the
-- floor: the audit, the server cache, the chart's browser cache, and alarms
-- whose window would otherwise be silently truncated — `count(*) <` then
-- reports an outage retention itself caused). initial_window_s walks a
-- catching-up load forward in bounded steps so the forever loop gets its
-- turn in between, and gives the first load a real lower bound via a new
-- min(watermark) probe. dim_refresh_interval_s throttles the per-run
-- dimension import, off by default because throttling costs freshness.
-- Mirror block; see migrations/0037_retention_and_first_load.sql for the
-- semantics.
ALTER TABLE starcubes_meta.datasource
    ADD COLUMN IF NOT EXISTS retention_periods      int NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS initial_window_s       int NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS dim_refresh_interval_s int NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS dim_refreshed_at       timestamptz;

COMMENT ON COLUMN starcubes_meta.datasource.retention_periods IS
  'Keep this many partition periods; the housekeeping drops older ones, raises retention_floor and clears the plot cache. 0 = keep everything (the default). Irreversible: fact partitions are UNLOGGED and there is no backup. At most a couple of partitions go per housekeeping run, so a mistyped value walks visibly instead of clearing the table in one pass.';
COMMENT ON COLUMN starcubes_meta.datasource.initial_window_s IS
  'Width of each load window while the datasource is still catching up, in seconds. 0 = one unbounded window (the default), which on a large source is a single run of hours or days during which the forever loop evaluates no alarms and runs no housekeeping. Applies to every step of the catch-up, not only the first run -- otherwise step two loads the whole remainder. A caught-up datasource never reaches the cap.';
COMMENT ON COLUMN starcubes_meta.datasource.dim_refresh_interval_s IS
  'Seconds between dimension imports. 0 = every run (the default). Throttling costs freshness: a new value in a *regular* dimension maps to __unknown__ until the next refresh, which is a visibly wrong series in the chart for up to one interval. Auto-dimensions are unaffected, they resolve during the extract. --import-dims and the GUI button always force an import.';
COMMENT ON COLUMN starcubes_meta.datasource.dim_refreshed_at IS
  'When import_dimensions last actually ran, for dim_refresh_interval_s to count from. NULL = never, so the first run always imports.';


-- 0038_rollup.sql — pre-aggregation. Every chart request re-aggregates the
-- raw rows today; query_cache only helps an exactly repeated one. A rollup is
-- a *column set*, not a cube: the base aggregation at bucket granularity over
-- a chosen set of dimension columns, out of which every group_by and every
-- filter falls at query time. The core set and each marginal are the same
-- mechanism, and pick_rollup takes the narrowest spec covering the query.
-- spec_hash makes a reconfiguration degrade to fact rather than return wrong
-- numbers, and built_from/built_through let a backfill that runs for hours be
-- useful while it is still running -- it works backwards, so the newest
-- windows get fast first. Coarsening the column set is a GROUP BY over the
-- small table; refining needs a rebuild from fact. Mirror block; see
-- migrations/0038_rollup.sql for the semantics.
ALTER TABLE starcubes_meta.datasource
    ADD COLUMN IF NOT EXISTS rollup_interval_s int    NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS rollup_columns    text[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS rollup_marginals  text[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS rollup_measures   text[] NOT NULL DEFAULT '{}';

COMMENT ON COLUMN starcubes_meta.datasource.rollup_interval_s IS
  'Bucket width of the pre-aggregation in seconds; 0 = off (the default). Must divide every chart interval it is meant to serve, so 3600 covers hour and day bins while 86400 covers only day bins. Changing it invalidates every rollup of this datasource (it is part of the spec hash) and needs a rebuild.';
COMMENT ON COLUMN starcubes_meta.datasource.rollup_columns IS
  'Dimension fact columns held in the core rollup. A chart query is served from it only when its group_by and *all* of its filter columns are in this set -- anything else falls back to fact. Sized with `manage.py rollup_advise`: one high-cardinality column can cost more than all the others together.';
COMMENT ON COLUMN starcubes_meta.datasource.rollup_marginals IS
  'Dimension fact columns that get their own single-column rollup instead of a place in rollup_columns. Serves an unfiltered breakdown on a column too expensive for the core set. A marginal is not a second mechanism -- it is a rollup whose column set has one member, and pick_rollup chooses the narrowest spec that covers the query.';
COMMENT ON COLUMN starcubes_meta.datasource.rollup_measures IS
  'Aggregation forms materialised beside the mandatory count(*), as they appear in starcubes_meta.measure.agg (sum(col), avg(col), min(col), max(col)). quantile(col) is rejected: percentiles do not sum, so ladder styles can never be served from a rollup. Cheap to add in storage terms, but adding one needs a rebuild because the raw values are gone.';

CREATE TABLE IF NOT EXISTS starcubes_meta.rollup_state (
    datasource_id int  NOT NULL REFERENCES starcubes_meta.datasource(id)
                       ON DELETE CASCADE,
    rollup_name   text NOT NULL,
    spec_hash     text NOT NULL,
    built_from    timestamptz,
    built_through timestamptz,
    rows_built    bigint      NOT NULL DEFAULT 0,
    last_built_at timestamptz,
    PRIMARY KEY (datasource_id, rollup_name)
);

COMMENT ON TABLE starcubes_meta.rollup_state IS
  'Build state of one pre-aggregation table. Rows are created by manage.py rollup_config and maintained by rollup_build and the ETL hook. Dropping them is harmless as long as fact still covers the same span -- the rollup is purely additive in the read path, so without state every query simply reads fact again. Below datasource.retention_floor it is not: fact no longer holds those rows and no rebuild can restore them.';
COMMENT ON COLUMN starcubes_meta.rollup_state.rollup_name IS
  'Table name inside the datasource schema, e.g. rollup_1h for the core set or rollup_1h_merch_cat for a marginal.';
COMMENT ON COLUMN starcubes_meta.rollup_state.spec_hash IS
  'sha256 over the sorted column list, measure list and bucket width. A mismatch against the live datasource configuration marks the rollup unusable, which is how a configuration change degrades to fact instead of returning wrong numbers.';
COMMENT ON COLUMN starcubes_meta.rollup_state.built_from IS
  'Oldest bucket held. The backfill works backwards and pushes this down, so the newest windows become fast first. NULL = nothing built. Retention deliberately does not raise it: rollup rows were computed while the raw data existed and stay correct after disposal, so aggregates outlive the rows they came from -- which is the point, not an accident. Below datasource.retention_floor the rollup is therefore the *only* copy, so rollup_drop and rollup_build --rebuild refuse that span without --force and the tables belong in the backup.';
COMMENT ON COLUMN starcubes_meta.rollup_state.built_through IS
  'Exclusive upper bound of the finished buckets, advanced by the ETL hook once a bucket falls below hwm - late_arrival_overlap_s. Rewound in place when a repair rewinds the watermark, which is what makes historical mutation a rewind rather than an invalidation.';


-- 0039_rollup_run.sql -- rollup run history. 0038 gave the pre-aggregation
-- five management commands and no way to reach them except a shell. Three of
-- the five run for minutes to hours, so a GUI cannot run them inside a
-- request -- uwsgi's harakiri kills the whole worker at 120s, taking its other
-- in-flight requests with it. They are spawned as detached subprocesses the
-- way an ETL run is, and a spawned process needs somewhere to say what it is
-- doing and what it found. Cut to the shape of `etl_run` because the GUI
-- already knows how to poll one. `report` is what makes it worth a table
-- rather than a log file: advise returns a ladder the operator picks a stage
-- from, and a pick that survives only as long as a log file vanishes on the
-- next reload. See migrations/0039_rollup_run.sql for the full rationale.

CREATE TABLE IF NOT EXISTS starcubes_meta.rollup_run (
    id            serial PRIMARY KEY,
    datasource_id int  NOT NULL REFERENCES starcubes_meta.datasource(id)
                       ON DELETE CASCADE,
    kind          text NOT NULL
        CHECK (kind IN ('advise', 'build', 'verify')),
    status        text NOT NULL
        CHECK (status IN ('running', 'ok', 'failed')),
    started_at    timestamptz NOT NULL,
    finished_at   timestamptz,
    pid           int,
    args          jsonb,
    report        jsonb,
    progress      text,
    progress_at   timestamptz,
    error         text,
    log_path      text,
    log_tail      text
);

CREATE INDEX IF NOT EXISTS ix_rollup_run_ds
    ON starcubes_meta.rollup_run (datasource_id, started_at DESC);

COMMENT ON TABLE starcubes_meta.rollup_run IS
  'One execution of manage.py rollup_advise, rollup_build or rollup_verify. Written by the command itself, so a run started from a shell appears in the GUI history exactly like one started from a button. rollup_config and rollup_drop are not recorded: they finish inside a request.';
COMMENT ON COLUMN starcubes_meta.rollup_run.kind IS
  'advise = measure and propose a column set; build = advance or backfill the rollup tables; verify = compare chart answers from fact against the rollup.';
COMMENT ON COLUMN starcubes_meta.rollup_run.pid IS
  'PID of the command process. What tells a stale running row (process gone, status never updated) from a live one, and what the GUI kill button signals. NULL for a run that never spawned.';
COMMENT ON COLUMN starcubes_meta.rollup_run.args IS
  'The options the run was started with, so a report can be read back knowing what produced it -- an advise measured over one day and one extrapolated to a future rate are not comparable.';
COMMENT ON COLUMN starcubes_meta.rollup_run.report IS
  'advise: dataclasses.asdict of the Advice -- dimensions, candidate ladder, measures. verify: {passed, skipped, failed, failures[]}. build: NULL, its result is rollup_state. Skips in a verify report are not failures: a skip is the read path correctly falling back to fact.';
COMMENT ON COLUMN starcubes_meta.rollup_run.progress IS
  'Last progress line emitted, with progress_at as its timestamp. A backfill runs for hours and moves built_from downwards batch by batch; without this the only visible difference between slow progress and a hung process is the log file.';
