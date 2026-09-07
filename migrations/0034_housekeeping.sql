-- Periodic maintenance that cannot live inside a load (migration 0034).
--
-- The forever session is the only periodic engine in this system: no cron, no
-- systemd timer, no Celery. Anything that must happen regularly therefore
-- either happens inside a load or does not happen at all. Some work does not
-- belong inside a load — it is too expensive to pay per iteration, and it is
-- only worth doing every few hours. That is what the housekeeping command is
-- for, and this migration gives it the two things it needs: a cadence and a
-- place to record what it found.
--
--   housekeeping_interval_s   seconds between automatic housekeeping runs,
--                             checked by the forever loop after each load.
--                             0 disables it.
--
-- The default is deliberately *on* (6 hours), which is unusual for this
-- schema — late_arrival_overlap_s, watermark_lag_s and wallclock_lag_s all
-- default to the old behaviour because the right value is deployment-specific
-- and has to be measured. This one is different: what it fixes degrades
-- silently on every installation in the same way, and an operator cannot
-- measure a problem they cannot see.
--
-- Specifically, the load is COPY-only. Rows are inserted, never updated,
-- never deleted, so dead tuples barely accumulate and autovacuum's scale
-- factors are effectively never reached on a partition holding hundreds of
-- millions of rows. Two things then rot quietly:
--
--   * Planner statistics go stale on exactly the partition being written,
--     which is the one every chart query touches.
--   * BRIN ranges are never summarised. A BRIN index only covers page ranges
--     that have been summarised, and summarisation happens during VACUUM or
--     an explicit brin_summarize_new_values(). Freshly COPY'd blocks are
--     therefore not covered by the index at all until something summarises
--     them -- the index the entire storage design rests on silently stops
--     applying to the newest data.
--
-- Neither produces an error. Both make everything slower in a way nobody can
-- attribute. Defaulting the interval to 0 would have shipped a command that
-- nobody runs, which is the same outcome as not writing it.
--
-- housekeeping_run stores one row per run. The report is jsonb for the same
-- reason etl_run.stats is: the shape will grow (retention, U3b) and a column
-- per measurement would not survive that. Its real purpose is history --
-- the Storage tab already shows current partition sizes, so a single
-- measurement adds nothing; a series of them turns "the disk filled up
-- overnight" into something an operator can see coming, and gives a future
-- retention policy an evidence base instead of a guess.
--
-- Idempotent -- re-running on a populated DB is a no-op. Mirror this block
-- in starcubes_meta.sql (which is what core/schema.py runs); keeping the two
-- in lock-step is the contract verified in CLAUDE.md.
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
