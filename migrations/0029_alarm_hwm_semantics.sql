-- 0029_alarm_hwm_semantics.sql — alarms evaluate at the data high watermark,
-- driven by the ETL load; the global evaluator loop is retired.
--
-- Evaluation used to be a standalone process (alarm_run --forever, one
-- session pinned by alarm_forever) that chained disjoint window_s windows
-- from an eval_end cursor up to the fact clock. It is now the ETL that
-- evaluates: etl_run calls run_alarm_cycle for its own datasource after
-- every load. A load is the only thing that moves the data high watermark,
-- so it is the only moment an alarm's answer can have changed — a separate
-- timer either re-asked the same question or arrived after the fact.
--
-- Each load evaluates one trailing window [hwm - window_s, hwm). Consecutive
-- loads overlap; a load that did not move the HWM is skipped outright.
-- eval_end therefore no longer means "end of the last evaluated window" but
-- "the HWM we last evaluated at". Accepted consequence: a failed or delayed
-- load takes its evaluation with it, and a breach living entirely between
-- two loads is never seen.
--
-- alarm_firing's three timestamps change meaning with it: they are the data
-- high watermark at the load that observed the transition, never a window
-- boundary and never the wall clock. Existing rows keep their old
-- window-derived values — they are not migrated; an episode still open is
-- simply continued (or closed) under the new rule by the next load.
--
-- No column is added or dropped: only the comments and the retired table.
-- Idempotent — re-running on a populated DB is a no-op. Mirror this block in
-- starcubes_meta.sql (which is what `core/schema.py` runs); keeping the two
-- in lock-step is the contract verified in CLAUDE.md.

-- The global evaluator session table: nothing writes it any more, and
-- alarm_resume_forever (its only spawner) is gone.
DROP TABLE IF EXISTS starcubes_meta.alarm_forever;

COMMENT ON COLUMN starcubes_meta.alarm.eval_end IS
    'The data high watermark this alarm was last evaluated at (fact clock). NULL = never evaluated. Every ETL load evaluates one trailing window [eval_end - window_s, eval_end); a load that does not move the HWM is skipped. Written by the evaluator only, never touching updated_at.';

COMMENT ON COLUMN starcubes_meta.alarm_firing.first_breach_at IS
    'The data high watermark at the load that first saw the threshold breached — the start of the episode on the data''s own time axis.';
COMMENT ON COLUMN starcubes_meta.alarm_firing.last_breach_at IS
    'The data high watermark at the most recent load that still saw the threshold breached.';
COMMENT ON COLUMN starcubes_meta.alarm_firing.closed_at IS
    'The data high watermark at the first load that saw the value back under the threshold. NULL = the episode is still open.';
