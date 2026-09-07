-- Two measurements the ETL already makes, and two budgets it never had
-- (migration 0035).
--
-- The failure mode this is aimed at is the one most likely to go unnoticed
-- for a full day, and the one an operator would most want to hear about
-- within minutes: **the source is reachable and healthy, and has stopped
-- producing data.** Nothing sees it today. The load worker, the connection
-- and the schema are all fine; the ETL logs rows=0 and sleeps. Alarms cannot
-- see it by construction either — a total standstill freezes the high
-- watermark, so the trailing evaluation window stops sliding and
-- `count(*) < 1` never becomes true (docs/alarm.md states the limitation
-- outright).
--
-- The answer is to watch the *pipeline* rather than the data, and every run
-- already computes both numbers needed for that on the same query:
--
--   etl_run.max_wm     max(watermark_column) as read from the source
--   etl_run.src_now    the source's own wall clock at that moment, UTC
--
-- Storing them costs a column each and buys three things. A standstill
-- becomes `src_now - max_wm` growing without bound, compared against
-- datasource.max_data_lag_s. Clock skew (N12) becomes a series instead of a
-- log line nobody read — a skew parked near a whole-hour offset while rows
-- keep arriving means the watermark column does not hold UTC, which the rest
-- of the pipeline assumes it does. And docs/etl-audit.md's open wish
-- ("recording max_wm per run would disambiguate that at the price of a
-- column") is granted.
--
-- Both operands come from the source's own clock, so the comparison carries
-- no skew of its own — the same discipline docs/etl-audit.md insists on for
-- needed_lag_s. Note that src_now is now read on *every* run, not only when
-- wallclock_lag_s is set: for the two generated query shapes it rides along
-- as a second column and costs nothing, and only a user-supplied
-- watermark_max_query pays one extra trivial round trip.
--
--   datasource.max_data_lag_s   src_now - max_wm above this means the source
--                               has stopped producing. 0 = unset.
--
-- Deliberately a per-datasource absolute threshold and deliberately off by
-- default: for a source with continuous traffic it is a real instrument, and
-- for a batch source that produces twice a day it would be a false alarm
-- generator. The temptation there is time-of-day profiles; that is a
-- calendar engine in disguise — holidays, timezones, maintenance windows,
-- and an ever-growing table of exceptions — and it never finishes.
--
--   datasource.max_run_s        the parent aborts a run past this budget,
--                               and no single source statement inside it may
--                               outlive it either. 0 = unset.
--
-- Off by default and measured, not guessed, the same discipline
-- late_arrival_overlap_s follows: a cap that is too tight kills healthy long
-- runs, and because the forever loop retries, it does so repeatedly.
--
-- One honest limitation, because the design note this came from got it
-- wrong: a run killed by the cap does *not* make the next iteration retry a
-- smaller window. The high watermark did not move, so the next window starts
-- in the same place and ends later — it is *wider*. The cap is a circuit
-- breaker and a diagnosis, not a self-healing mechanism: it converts a run
-- that would have held the forever loop indefinitely into a failed row with
-- a reason in it. Narrowing the work is what --limit is for.
--
-- The same value bounds the source statements within the run, which is why
-- there is no separate timeout column: a statement may not outlive the run
-- it belongs to, and a second knob nobody can size independently would only
-- be a second thing to get wrong. Applied per dialect (Postgres
-- statement_timeout, Oracle call_timeout); a dialect with no equivalent logs
-- that it is unbounded rather than pretending otherwise.
--
-- Idempotent -- re-running on a populated DB is a no-op. Mirror this block
-- in starcubes_meta.sql (which is what core/schema.py runs); keeping the two
-- in lock-step is the contract verified in CLAUDE.md.
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
