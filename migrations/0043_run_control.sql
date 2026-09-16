-- Stopping a run from any container (migration 0043).
--
-- 0042 made every container agree on whether a run is alive. A signal still
-- only reaches a process in the same container, so a Kill pressed on a page
-- served by another one had to be refused. This column carries the request
-- instead of the signal:
--
--   control = 'term'  the process stops itself at its next check -- the ETL's
--                     progress thread (every 5 s), a rollup command's progress
--                     report, a forever loop's sleep. Same ending as SIGTERM:
--                     the row is closed by the process, as 'failed', with the
--                     reason.
--   control = 'kill'  for a process too stuck to check anything. The
--                     node_agent of the container that runs it (woken by
--                     NOTIFY starcubes_control, polling as a fallback) sends
--                     SIGKILL to the process group -- only after verifying the
--                     pid against the lease -- and closes the row itself.
--
-- A forever session needs no column: `etl_forever.stop_requested` has been
-- this request all along. The value is left on the finished row, so the
-- history says the run was stopped on request rather than failing by itself.
--
-- The view `run` gains `control` as its last column, with a forever session's
-- stop request shown as 'term'. Dropped and re-created rather than replaced:
-- the consolidated script keeps exactly one definition of the view, and
-- DROP + CREATE survives any later change of shape.

ALTER TABLE starcubes_meta.etl_run
    ADD COLUMN IF NOT EXISTS control text;
ALTER TABLE starcubes_meta.rollup_run
    ADD COLUMN IF NOT EXISTS control text;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'etl_run_control_check') THEN
        ALTER TABLE starcubes_meta.etl_run ADD CONSTRAINT etl_run_control_check
            CHECK (control IN ('term', 'kill'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'rollup_run_control_check') THEN
        ALTER TABLE starcubes_meta.rollup_run ADD CONSTRAINT rollup_run_control_check
            CHECK (control IN ('term', 'kill'));
    END IF;
END $$;

COMMENT ON COLUMN starcubes_meta.etl_run.control IS
  'A stop request from any container: term = the process stops itself at its next check; kill = the node_agent of the container that runs it sends SIGKILL after verifying the pid against the lease. Left on the finished row as a record.';

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
       r.log_path AS run_ref,
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
       (SELECT r.log_path FROM starcubes_meta.etl_run r
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
       u.log_path,
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
