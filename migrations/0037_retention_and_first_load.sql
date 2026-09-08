-- The two things that change assumptions other subsystems hold, plus one
-- knob that trades freshness for round trips (migration 0037).
--
-- These were sequenced last on purpose. Everything before them added
-- machinery around a load; these three change what the rest of the system is
-- allowed to assume about the fact table.
--
--   retention_periods        keep this many partition periods, drop older
--                            ones. 0 = keep everything, which is what every
--                            installation does today.
--
-- Forward coverage has been automatic since ensure_partitions_for; backward
-- disposal never happened. Fact partitions are UNLOGGED, so the storage is
-- cheap to write and completely unbounded. What made retention an M rather
-- than an S is not the drop loop — it is that *other parts of the system
-- assume fact covers everything since the beginning of time*, and four of
-- them have to learn otherwise:
--
--   * core/audit.py compares source counts against fact counts. Below the
--     floor that comparison reports 100% loss (clamped since migration
--     0033).
--   * The server-side plot cache holds responses for ranges it considers
--     final. The drop gestures bump cache_epoch, which makes the old keys
--     unreachable -- but never deletes them, so a *periodic* retention would
--     grow query_cache monotonically. Retention therefore also clears it.
--   * The chart's own 50 MB browser cache keys on the query string alone. A
--     window below the floor is "final, not trailing" -- the most cacheable
--     shape there is -- so an open tab would keep painting dropped data.
--   * Alarms. This is the sharp one. The evaluator's window is
--     [hwm - window_s, hwm) filtered on partition_column, the same clock the
--     floor is written in. Truncate the older half and `count(*) <` and
--     `sum(col) <` -- exactly the conditions people write to detect an
--     outage -- report an outage that retention itself caused. `>` and `>=`
--     merely miss breaches. So an alarm whose window reaches below the floor
--     is not evaluated at all.
--
--   initial_window_s         width of each step while the load is catching
--                            up. 0 = one unbounded window, as before.
--
-- An empty fact table makes _resume_state ignore the stored watermark, so
-- the first window is "everything the source has" with no lower bound in the
-- WHERE clause at all. On a large source that is one run of many hours or
-- days, during which the forever loop does nothing else: no alarm
-- evaluation, no housekeeping, no chance to notice anything.
--
-- With this set, the load walks forward in steps and the loop gets its turn
-- between them. Note the cap necessarily applies to every step of the catch
-- up rather than only to the literally first run -- otherwise step two loads
-- the entire remainder and nothing has been gained. A datasource that has
-- caught up never notices it, because its window is narrower anyway.
--
-- Setting it also gives the first load a real lower bound: the engine now
-- asks the source for min(watermark_column), which nothing ever did before.
--
--   dim_refresh_interval_s   seconds between dimension imports. 0 = every
--                            run, which is the behaviour up to now.
--   dim_refreshed_at         when the last import actually ran.
--
-- import_dimensions runs on every import_fact. At a 60-second forever
-- interval that is a full lookup scan per regular dimension, sixty times an
-- hour, against the source, to discover changes that arrive maybe weekly.
--
-- The reason this defaults to off, unlike housekeeping_interval_s: throttling
-- *costs* something real. A new value in a regular dimension maps to
-- __unknown__ until the next refresh, which is a visibly wrong series in the
-- chart for up to one interval. Auto-dimensions are unaffected -- they are
-- resolved during the extract and never depended on the periodic import.
--
-- Idempotent -- re-running on a populated DB is a no-op. Mirror this block
-- in starcubes_meta.sql (which is what core/schema.py runs); keeping the two
-- in lock-step is the contract verified in CLAUDE.md.
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
