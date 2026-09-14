-- Audit run history, and the interval that schedules it (migration 0040).
--
-- Two backlog items that read as separate work turn out to need the same
-- thing. "Late arrivals beyond the overlap are only found on demand" asks for
-- a periodic audit "with the result stored per run". "Backfill re-scans the
-- window even when the exact ids are known" names its blocker as "handing the
-- id list to the spawned subprocess -- a CLI argument will not hold it, so it
-- needs a temp file or a starcubes_meta staging row". That staging row and
-- that stored result are one table: the audit produces exactly the input the
-- targeted backfill consumes.
--
--   trigger        manual (CLI or GUI button) or forever (the loop reached
--                  audit_interval_s). Same vocabulary as housekeeping_run,
--                  because it is the same mechanism -- the forever loop is
--                  the only scheduler this system has.
--
--   missing_count  and its siblings are columns rather than report fields on
--   surplus_count  purpose. core/pipeline_alarm.py answers every condition
--   duplicate_rows with a number read from starcubes_meta *only* -- no
--                  Source.load, no source connection -- which is what lets a
--                  pipeline alarm be evaluated for a datasource whose source
--                  is unreachable, or whose ETL is the thing that stopped.
--                  A jsonb lookup would work; a plain column is what that
--                  module's shape asks for.
--
--   missing_ids    the ids the ETL lost, as a jsonb array of strings --
--                  strings because a PK is not always an integer and a
--                  round trip through JSON must not silently narrow one.
--                  Capped (STARCUBES_BACKFILL_IDS_MAX, default 100000); past
--                  that ids_truncated is set and the operator falls back to a
--                  window backfill. The cap is not thrift: chunked at 1000
--                  for Oracle's IN-list limit, 100000 ids are a hundred
--                  queries, and once the gap approaches the size of the
--                  window the window scan wins anyway.
--
--   report         the bucket rows from audit_window -- stage one of the two.
--                  Kept even when nothing is missing: that is the series that
--                  shows a gap appearing, and a clean run is evidence too.
--
-- Why two stages at all: audit_window is counts, cheap and bucketed;
-- audit_ids pulls both sides' primary keys into memory and is not. The normal
-- case is "nothing is missing", so the expensive half runs only over the
-- buckets core/audit.py:gap_span named.

ALTER TABLE starcubes_meta.datasource
    ADD COLUMN IF NOT EXISTS audit_interval_s int NOT NULL DEFAULT 0;
ALTER TABLE starcubes_meta.datasource
    ADD COLUMN IF NOT EXISTS audit_window_s   int NOT NULL DEFAULT 0;

COMMENT ON COLUMN starcubes_meta.datasource.audit_interval_s IS
  'Seconds between periodic audits, checked by the forever loop. 0 = off, and off is the default -- unlike housekeeping_interval_s, because an audit reads the source and the warehouse over a window and is not free.';
COMMENT ON COLUMN starcubes_meta.datasource.audit_window_s IS
  'How far back a periodic audit looks. 0 means "fall back to the interval", so an operator who sets only the interval still gets a window that cannot leave a hole between consecutive runs.';

CREATE TABLE IF NOT EXISTS starcubes_meta.audit_run (
    id             serial PRIMARY KEY,
    datasource_id  int  NOT NULL REFERENCES starcubes_meta.datasource(id)
                        ON DELETE CASCADE,
    trigger        text NOT NULL DEFAULT 'manual'
        CHECK (trigger IN ('manual', 'forever')),
    status         text NOT NULL
        CHECK (status IN ('running', 'ok', 'failed')),
    started_at     timestamptz NOT NULL DEFAULT now(),
    finished_at    timestamptz,
    window_start   timestamptz NOT NULL,
    window_end     timestamptz NOT NULL,
    source_rows    bigint,
    fact_rows      bigint,
    missing_count  bigint,
    surplus_count  bigint,
    duplicate_rows bigint,
    missing_ids    jsonb,
    ids_truncated  boolean NOT NULL DEFAULT false,
    report         jsonb,
    error          text
);

CREATE INDEX IF NOT EXISTS ix_audit_run_ds
    ON starcubes_meta.audit_run (datasource_id, started_at DESC);

COMMENT ON TABLE starcubes_meta.audit_run IS
  'One execution of manage.py etl_audit, or of the forever loop reaching audit_interval_s. Both the record the "periodic audit" backlog item asked for and the staging row the targeted backfill reads its id list from.';
COMMENT ON COLUMN starcubes_meta.audit_run.trigger IS
  'manual (CLI or GUI button) or forever (the loop reached audit_interval_s).';
COMMENT ON COLUMN starcubes_meta.audit_run.missing_count IS
  'Rows present in the source and absent from fact over the window -- what the ETL lost. A column rather than a report field so core/pipeline_alarm.py can read it with a plain aggregate over starcubes_meta alone.';
COMMENT ON COLUMN starcubes_meta.audit_run.missing_ids IS
  'The missing primary keys as a jsonb array of strings, capped at STARCUBES_BACKFILL_IDS_MAX. Consumed by `etl_run --backfill --audit-run <id>`, which extracts exactly these rows instead of re-scanning the window. NULL when stage two did not run because nothing was missing.';
COMMENT ON COLUMN starcubes_meta.audit_run.ids_truncated IS
  'True when more ids were missing than the cap allows. The targeted backfill refuses such a run and points at the window backfill, rather than repairing a part of the gap and reporting success.';
COMMENT ON COLUMN starcubes_meta.audit_run.report IS
  'The per-bucket rows from core/audit.py:audit_window -- stage one. Stored for a clean run too: the series is what shows a gap appearing.';

-- The pipeline-alarm condition whitelist gains `audit_gap`. Dropped and
-- re-added rather than guarded with IF NOT EXISTS, because the constraint
-- already exists from 0036 and the point here is to *widen* it; re-running
-- lands on the same definition either way, so this stays idempotent.
ALTER TABLE starcubes_meta.alarm
    DROP CONSTRAINT IF EXISTS alarm_condition_check;
ALTER TABLE starcubes_meta.alarm
    ADD CONSTRAINT alarm_condition_check
    CHECK ((rule_kind = 'pipeline' AND condition IN (
                'data_stale', 'run_failed', 'no_recent_run',
                'preflight_warn', 'dim_full_pct', 'audit_gap'))
           OR (rule_kind = 'threshold' AND condition IS NULL));
