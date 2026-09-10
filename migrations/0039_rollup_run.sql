-- Rollup run history: the record of what advise/build/verify did
-- (migration 0039).
--
-- 0038 gave the pre-aggregation five management commands and no way to reach
-- them except a shell. Putting them in the GUI is not a matter of wiring five
-- buttons: three of the five run for minutes to hours, so they cannot be a
-- request -- uwsgi's harakiri kills the whole worker at 120s, taking its other
-- in-flight requests with it. They are spawned as detached subprocesses the
-- same way an ETL run is, and a spawned process needs somewhere to say what it
-- is doing and what it found. That is this table, cut to the shape of
-- `etl_run` for the same reason: the GUI already knows how to poll one.
--
-- Only the three long commands are recorded. `rollup_config` and
-- `rollup_drop` finish in seconds and run inside the request, so a history
-- row would only ever be written after the fact.
--
--   kind        which command. Not `command`, and not the bare word `type`:
--               the read path already speaks of a rollup *spec*, and a run is
--               an execution of one of three verbs against it.
--
--   report      the structured result. This is what makes the table worth its
--               migration rather than tailing the log file: `advise` returns
--               an Advice dataclass whose ladder the operator picks a stage
--               from, and a pick that survives only as long as a log file is
--               a pick that vanishes on the next reload. `verify` stores its
--               matrix summary here for the same reason.
--
--   progress    the last progress line the command emitted. `rollup_build`
--               already prints one per batch; a backfill working backwards
--               through years of history is otherwise indistinguishable from
--               a hung process.

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
