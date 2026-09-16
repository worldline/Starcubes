-- One log for every process, filterable (migration 0046).
--
-- A spawned ETL, forever or rollup process used to write a file under
-- STARCUBES_LOG_DIR that only the container holding it could serve, and the
-- GUI's log viewer picked one file at a time. The operator's question is
-- rarely "show me file X"; it is "what happened", across processes, kinds,
-- datasources and containers. So the lines go into one table and the viewer
-- filters.
--
--   One row per line, not per chunk: a filter by level or text, and several
--   runs shown interleaved in time, both need the line as the unit.
--   Measured on real runs: 10-45 lines each; a 60 s forever loop writes
--   ~15-20k lines a day per datasource.
--
--   ts            when the line was shipped (the log format carries seconds
--                 and no time zone, so it cannot be the source of order).
--                 Within one shipment, id orders.
--   kind          the process kind -- the same vocabulary as the view `run`:
--                 etl, backfill, forever, rollup_advise/_build/_verify.
--   run_ref       the log's name (the spool file's basename, which is also
--                 the basename of etl_run.log_path / rollup_run.log_path).
--   datasource_id nullable, so a process that belongs to no datasource can
--                 log here later without a schema change.
--   level         DEBUG..CRITICAL parsed from the log format; a traceback's
--                 continuation lines inherit it; raw output is NULL.
--
-- Deliberately a LOGGED table: an UNLOGGED one is truncated by crash
-- recovery and not replicated. Retention is STARCUBES_LOG_RETENTION_DAYS
-- (node_agent) and STARCUBES_LOG_MAX_LINES_PER_RUN (the shipper).
--
-- The view `run` is redefined so its run_ref is that basename rather than a
-- container-local path; it is the join key between a run and its lines.

CREATE TABLE IF NOT EXISTS starcubes_meta.log_line (
    id            bigserial PRIMARY KEY,
    ts            timestamptz NOT NULL DEFAULT now(),
    datasource_id int REFERENCES starcubes_meta.datasource(id) ON DELETE CASCADE,
    kind          text NOT NULL
                  CHECK (kind IN ('etl', 'backfill', 'forever', 'rollup_advise',
                                  'rollup_build', 'rollup_verify')),
    run_ref       text NOT NULL,
    hostname      text NOT NULL,
    level         text CHECK (level IN ('DEBUG', 'INFO', 'WARNING', 'ERROR',
                                        'CRITICAL')),
    line          text NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_log_line_ts ON starcubes_meta.log_line (ts);
CREATE INDEX IF NOT EXISTS ix_log_line_ds_ts
    ON starcubes_meta.log_line (datasource_id, ts);
CREATE INDEX IF NOT EXISTS ix_log_line_run
    ON starcubes_meta.log_line (run_ref, id);

COMMENT ON TABLE starcubes_meta.log_line IS
  'Every line every spawned ETL/forever/rollup process wrote, from every container. Shipped from a local spool file by the process itself (core/log_ship.py), the remainder of a crashed one by its container''s node_agent. Filter by kind, datasource, hostname, level, run_ref, ts.';

DROP VIEW IF EXISTS starcubes_meta.run;
CREATE VIEW starcubes_meta.run AS
SELECT CASE WHEN r.kind = 'backfill' THEN 'backfill' ELSE 'etl' END AS kind,
       r.id, r.datasource_id, r.status, r.started_at, r.finished_at,
       r.hostname, r.pid,
       CASE WHEN r.status <> 'running' THEN false
            WHEN r.lease_pid IS NULL THEN NULL
            ELSE EXISTS (SELECT 1 FROM pg_stat_activity sa
                          WHERE sa.pid = r.lease_pid
                            AND sa.backend_start = r.lease_start)
       END AS alive,
       regexp_replace(r.log_path, '^.*/', '') AS run_ref,
       r.error,
       CASE WHEN r.status = 'running'
            THEN coalesce(r.progress_rows, 0) || ' rows so far'
            ELSE coalesce(r.rows, 0) || ' rows'
       END AS summary,
       r.instance_id, r.lease_pid, r.lease_start,
       r.control
  FROM starcubes_meta.etl_run r
UNION ALL
SELECT 'forever',
       f.id, f.datasource_id, f.status, f.started_at, f.stopped_at,
       f.hostname, f.pid,
       CASE WHEN f.status NOT IN ('running', 'stopping') THEN false
            WHEN f.lease_pid IS NULL THEN NULL
            ELSE EXISTS (SELECT 1 FROM pg_stat_activity sa
                          WHERE sa.pid = f.lease_pid
                            AND sa.backend_start = f.lease_start)
       END,
       (SELECT regexp_replace(r.log_path, '^.*/', '') FROM starcubes_meta.etl_run r
         WHERE r.lease_pid = f.lease_pid AND r.lease_start = f.lease_start
         ORDER BY r.id DESC LIMIT 1),
       f.error,
       'every ' || f.interval_s || ' s',
       f.instance_id, f.lease_pid, f.lease_start,
       CASE WHEN f.stop_requested THEN 'term' END
  FROM starcubes_meta.etl_forever f
UNION ALL
SELECT 'rollup_' || u.kind,
       u.id, u.datasource_id, u.status, u.started_at, u.finished_at,
       u.hostname, u.pid,
       CASE WHEN u.status <> 'running' THEN false
            WHEN u.lease_pid IS NULL THEN NULL
            ELSE EXISTS (SELECT 1 FROM pg_stat_activity sa
                          WHERE sa.pid = u.lease_pid
                            AND sa.backend_start = u.lease_start)
       END,
       regexp_replace(u.log_path, '^.*/', ''),
       u.error,
       left(u.progress, 200),
       u.instance_id, u.lease_pid, u.lease_start,
       u.control
  FROM starcubes_meta.rollup_run u
UNION ALL
SELECT 'audit',
       a.id, a.datasource_id, a.status, a.started_at, a.finished_at,
       NULL::text, NULL::int, NULL::boolean, NULL::text,
       a.error,
       coalesce(a.missing_count, 0) || ' missing',
       NULL::text, NULL::int, NULL::timestamptz,
       NULL::text
  FROM starcubes_meta.audit_run a
UNION ALL
-- A housekeeping row is written once the work is done: ran_at is its end,
-- and a finding is a warning in the report, never a failed run.
SELECT 'housekeeping',
       h.id, h.datasource_id, 'ok',
       h.ran_at - make_interval(secs => coalesce(h.runtime_ms, 0) / 1000.0),
       h.ran_at,
       NULL::text, NULL::int, NULL::boolean, NULL::text,
       NULL::text,
       coalesce(jsonb_array_length(h.report -> 'warnings'), 0) || ' warnings',
       NULL::text, NULL::int, NULL::timestamptz,
       NULL::text
  FROM starcubes_meta.housekeeping_run h;

COMMENT ON VIEW starcubes_meta.run IS
  'Every run of every kind in one list: etl, backfill, forever, rollup_advise/build/verify, audit, housekeeping. (kind, id) is unique. alive: true/false where a lease exists, NULL where there is no process (audit, housekeeping) or a legacy row without a lease. run_ref points at the run''s log. control: a pending or recorded stop request.';
