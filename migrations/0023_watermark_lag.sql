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
-- The wall-clock term is deliberately NOT capped at max(watermark). It is a
-- named deadline, not a by-product: a row whose watermark is below
-- now - wallclock_lag_s and which is still invisible has missed its deadline
-- by definition -- that is a case for the overlap and the Audit tab, not for
-- the lag. So the HWM may advance past the newest row that actually exists.
--
-- The wall clock is LOCALTIMESTAMP on the *source* connection, so that
-- connection's session timezone must match the one the watermark column is
-- written in. A session in Europe/Berlin against watermarks written in UTC
-- shifts the term by the full UTC offset and makes it win every run. It is
-- not automatically detectable -- nothing says what clock the writer used --
-- so core/etl.py logs the measured src_now - max_wm skew alongside both
-- candidates instead.
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
