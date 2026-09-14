-- What the gap says about the lag, and permission to close small ones
-- (migration 0041).
--
-- 0040 made the audit find gaps and name the rows. It did not say *why* they
-- were missing, and that is the question the operator actually has. The
-- answer is already computed: `core/audit.py:audit_id_details` pairs each
-- missing row's own watermark value with the etl_run whose window covered it
-- and reports
--
--     needed_lag_s = etl_run.last_end_time - the row's watermark value
--
-- read as: the window's upper edge had already moved this far past the
-- transaction, and it still was not visible. Until now that ran only when
-- somebody clicked Details, and was never stored.
--
-- **Which knob it proposes.** Both of them, by the same amount, at different
-- prices. With end_time_N = max_wm_N - watermark_lag_s, raising that lag by
-- D puts the tip at end_time_N - D; for run N not to swallow the row it takes
-- D >= needed_lag_s. The overlap takes the same number (needed_lag_s <=
-- late_arrival_overlap_s is its exact re-read criterion). The difference is
-- what each costs: watermark_lag_s holds *every* row back, so the whole
-- datasource is that much less fresh; late_arrival_overlap_s re-reads a
-- trailing span and leaves freshness alone. That is an operator's trade-off,
-- not a derivation, which is why the alarm reports the raw seconds and the
-- ladder in the report says what a given increment would have bought.
--
--   needed_lag_p99_s   columns, not report fields, for the same reason 0040's
--   needed_lag_*       counts are: core/pipeline_alarm.py answers every
--                      condition with a number read from starcubes_meta
--                      alone, with no Source.load and no source connection.
--
--   audit_autofill_max the other half of the trade-off. Keeping
--                      watermark_lag_s low means accepting a few stragglers;
--                      accepting them should not mean somebody has to press a
--                      button for 37 rows. Up to this many missing rows, a
--                      forever-mode audit repairs itself with the targeted
--                      backfill. 0 = off, and off is the default: it writes
--                      to fact without being asked.
--
--                      Capped rather than unbounded because a large gap is
--                      not a handful of stragglers, it is a symptom -- and it
--                      costs heap order as well. Measured on a 500k-row
--                      table: backfilling 37 rows moves pg_stats.correlation
--                      from 1.0000 to 0.9997, backfilling 10% of it moves it
--                      to 0.8263, and that is what BRIN range exclusion rests
--                      on (docs/bitmap-indexes.md).

ALTER TABLE starcubes_meta.audit_run
    ADD COLUMN IF NOT EXISTS needed_lag_p99_s    double precision;
ALTER TABLE starcubes_meta.audit_run
    ADD COLUMN IF NOT EXISTS needed_lag_median_s double precision;
ALTER TABLE starcubes_meta.audit_run
    ADD COLUMN IF NOT EXISTS needed_lag_max_s    double precision;
ALTER TABLE starcubes_meta.audit_run
    ADD COLUMN IF NOT EXISTS needed_lag_n        int;
ALTER TABLE starcubes_meta.audit_run
    ADD COLUMN IF NOT EXISTS autofilled          boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN starcubes_meta.audit_run.needed_lag_p99_s IS
  'How much further back the window would have had to reach to catch 99% of the rows this run found missing. Both operands come from the source watermark column -- etl_run.started_at must never enter the subtraction, it is the starcubes host clock (see core/audit.py:audit_id_details).';
COMMENT ON COLUMN starcubes_meta.audit_run.needed_lag_n IS
  'Missing rows the lag could actually be resolved for. Smaller than missing_count: a row whose covering etl_run cannot be identified, or which has since vanished from the source, contributes nothing.';
COMMENT ON COLUMN starcubes_meta.audit_run.autofilled IS
  'This run repaired itself with a targeted backfill (datasource.audit_autofill_max). Recorded so a silent repair is still visible afterwards.';

ALTER TABLE starcubes_meta.datasource
    ADD COLUMN IF NOT EXISTS audit_autofill_max int NOT NULL DEFAULT 0;

COMMENT ON COLUMN starcubes_meta.datasource.audit_autofill_max IS
  'Up to this many missing rows, a forever-mode audit repairs itself with the targeted backfill instead of only raising the alarm. 0 = off. The point is to keep watermark_lag_s low without a human pressing a button for every handful of stragglers; the cap is where that stops being true.';

-- The pipeline-alarm condition whitelist gains `needed_lag_p99`. Dropped and
-- re-added rather than guarded, because widening it is the point; re-running
-- lands on the same definition, so this stays idempotent.
ALTER TABLE starcubes_meta.alarm
    DROP CONSTRAINT IF EXISTS alarm_condition_check;
ALTER TABLE starcubes_meta.alarm
    ADD CONSTRAINT alarm_condition_check
    CHECK ((rule_kind = 'pipeline' AND condition IN (
                'data_stale', 'run_failed', 'no_recent_run',
                'preflight_warn', 'dim_full_pct', 'audit_gap',
                'needed_lag_p99'))
           OR (rule_kind = 'threshold' AND condition IS NULL));
