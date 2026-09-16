-- Who runs a process, and whether it still does, as every container sees it
-- (migration 0042).
--
-- Until now a run's liveness was `os.kill(pid, 0)`. A PID names a process only
-- inside its own PID namespace, so with two containers on one database each
-- one judged the other's runs dead: a status poll marked a healthy forever
-- session failed, a container start "resumed" it a second time, and a kill
-- button could signal an unrelated local process that happened to hold the
-- same small number.
--
-- The replacement is a *lease*: every managed process opens one database
-- connection of its own when it starts and keeps it until it exits
-- (core/lease.py). Postgres drops that backend from pg_stat_activity the
-- moment the connection dies -- on exit, on SIGKILL, on OOM, and after the
-- TCP keepalives (core/pglock.py) when a host vanishes -- and every container
-- can see pg_stat_activity. No heartbeat, no timeout to tune, no table.
--
--   lease_pid, lease_start  pg_backend_pid() and backend_start of that
--                           connection. The pair is unique: a backend pid can
--                           be reused, a (pid, start time) pair cannot.
--   instance_id             the container (core/node.py). What decides whether
--                           a local signal may be sent at all.
--   hostname                for the operator: "running on <host>".
--
-- A row written by an older image has lease_pid NULL; core/lease.py keeps the
-- old rules for exactly those rows, so a rolling update does not reap runs it
-- cannot judge.
--
-- The view `run` is the one list of every run of every kind -- the five
-- history tables stay what they are, because the code reads them as working
-- state (the watermark, the pipeline alarms, the backfill ids). `alive` is
-- true/false for a process that has a lease, NULL where there is no process
-- to ask (audit, housekeeping) or no lease to ask about (a legacy row).
-- `kind` uses the same vocabulary the log will.

ALTER TABLE starcubes_meta.etl_run     ADD COLUMN IF NOT EXISTS hostname    text;
ALTER TABLE starcubes_meta.etl_run     ADD COLUMN IF NOT EXISTS instance_id text;
ALTER TABLE starcubes_meta.etl_run     ADD COLUMN IF NOT EXISTS lease_pid   int;
ALTER TABLE starcubes_meta.etl_run     ADD COLUMN IF NOT EXISTS lease_start timestamptz;

ALTER TABLE starcubes_meta.etl_forever ADD COLUMN IF NOT EXISTS hostname    text;
ALTER TABLE starcubes_meta.etl_forever ADD COLUMN IF NOT EXISTS instance_id text;
ALTER TABLE starcubes_meta.etl_forever ADD COLUMN IF NOT EXISTS lease_pid   int;
ALTER TABLE starcubes_meta.etl_forever ADD COLUMN IF NOT EXISTS lease_start timestamptz;

ALTER TABLE starcubes_meta.rollup_run  ADD COLUMN IF NOT EXISTS hostname    text;
ALTER TABLE starcubes_meta.rollup_run  ADD COLUMN IF NOT EXISTS instance_id text;
ALTER TABLE starcubes_meta.rollup_run  ADD COLUMN IF NOT EXISTS lease_pid   int;
ALTER TABLE starcubes_meta.rollup_run  ADD COLUMN IF NOT EXISTS lease_start timestamptz;

-- A forever session's log is the log of its loads; the view finds the newest
-- load of the same lease.
CREATE INDEX IF NOT EXISTS ix_etl_run_lease
    ON starcubes_meta.etl_run (lease_pid, lease_start)
    WHERE lease_pid IS NOT NULL;

COMMENT ON COLUMN starcubes_meta.etl_run.lease_pid IS
  'pg_backend_pid() of the lease connection the writing process holds for its whole life (core/lease.py). Alive while (lease_pid, lease_start) is in pg_stat_activity. NULL = written by an image older than 0042.';
COMMENT ON COLUMN starcubes_meta.etl_run.instance_id IS
  'The container that runs this process (core/node.py). Signals are only ever sent by the same instance.';

CREATE OR REPLACE VIEW starcubes_meta.run AS
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
       r.instance_id, r.lease_pid, r.lease_start
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
       f.instance_id, f.lease_pid, f.lease_start
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
       u.instance_id, u.lease_pid, u.lease_start
  FROM starcubes_meta.rollup_run u
UNION ALL
SELECT 'audit',
       a.id, a.datasource_id, a.status, a.started_at, a.finished_at,
       NULL::text, NULL::int, NULL::boolean, NULL::text,
       a.error,
       coalesce(a.missing_count, 0) || ' missing',
       NULL::text, NULL::int, NULL::timestamptz
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
       NULL::text, NULL::int, NULL::timestamptz
  FROM starcubes_meta.housekeeping_run h;

COMMENT ON VIEW starcubes_meta.run IS
  'Every run of every kind in one list: etl, backfill, forever, rollup_advise/build/verify, audit, housekeeping. (kind, id) is unique. alive: true/false where a lease exists, NULL where there is no process (audit, housekeeping) or a legacy row without a lease. run_ref points at the run''s log.';
