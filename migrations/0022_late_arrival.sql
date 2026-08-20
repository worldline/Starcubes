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
