-- starcubes metadata schema: consolidated initial creation script.
--
-- Combines all previous migrations (0001–0019) into one idempotent script.
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
    log_tail               text
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
    col  text;
BEGIN
    FOR rec IN
        SELECT DISTINCT d.dim_table, ds.schema_name
          FROM starcubes_meta.dimension d
          JOIN starcubes_meta.datasource ds ON ds.id = d.datasource_id
         WHERE d.is_primary AND d.dim_table IS NOT NULL
    LOOP
        SELECT column_name INTO col
          FROM information_schema.columns
         WHERE table_schema = rec.schema_name
           AND table_name   = rec.dim_table
           AND column_name   = 'count';
        IF col IS NULL THEN
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
        CHECK (graph_style IN ('multibar', 'multiline', 'multipoint', 'quantile-bands')),
    is_default    boolean NOT NULL DEFAULT false,
    ordinal       int  NOT NULL DEFAULT 0,
    percentiles   text,
    UNIQUE (datasource_id, name)
);

-- Widen the graph_style CHECK on databases created before 'quantile-bands'
-- (and before 'multipoint'/'multiline'). Keyed on the newest value so earlier
-- widenings still get upgraded.
DO $$
DECLARE cdef text;
BEGIN
    SELECT pg_get_constraintdef(c.oid) INTO cdef
      FROM pg_constraint c
      JOIN pg_class t ON t.oid = c.conrelid
      JOIN pg_namespace n ON n.oid = t.relnamespace
     WHERE n.nspname = 'starcubes_meta' AND t.relname = 'measure'
       AND c.conname = 'measure_graph_style_check';
    IF cdef IS NOT NULL AND position('quantile-bands' in cdef) = 0 THEN
        ALTER TABLE starcubes_meta.measure DROP CONSTRAINT measure_graph_style_check;
        ALTER TABLE starcubes_meta.measure
            ADD CONSTRAINT measure_graph_style_check
            CHECK (graph_style IN ('multibar', 'multiline', 'multipoint', 'quantile-bands'));
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
  'Aggregation expression. count(*) maps to the empty measure param (count). func(col) is passed through to core.query._parse_measure. For graph_style=quantile-bands the form is quantile(col), naming the numeric column the percentiles are computed over.';
COMMENT ON COLUMN starcubes_meta.measure.percentiles IS
  'Comma-separated ascending percentile ladder for graph_style=quantile-bands, e.g. ''25,50,75'' or ''5,25,50,75,95''. NULL for other styles (the query falls back to 5,25,50,75,95).';

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
