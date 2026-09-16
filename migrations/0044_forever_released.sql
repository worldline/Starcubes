-- A forever session handed over at shutdown (migration 0044).
--
-- With several containers on one database, a container that shuts down should
-- not take its forever sessions with it: another container can run them. Until
-- now a loop ended by a signal wrote `stopped`, the same word a user's Stop
-- writes, so nothing could tell "the operator wanted this over" from "the
-- container went away".
--
--   released   the loop left on SIGTERM without a stop request -- the container
--              is draining (docker/entrypoint.sh -> manage.py node_drain).
--              Any container's node_agent adopts it at once (NOTIFY
--              starcubes_control) or within a minute, and a starting container
--              does the same.
--   stopped    a user asked: Stop Forever (stop_requested), or SIGINT from a
--              shell. Never adopted.
--
-- Not part of ux_etl_forever_active: a released session is not running, and a
-- user may start a new one before anybody adopts it -- adoption then loses on
-- the unique index and skips.

DO $$
DECLARE cdef text;
BEGIN
    SELECT pg_get_constraintdef(c.oid) INTO cdef
      FROM pg_constraint c
     WHERE c.conrelid = 'starcubes_meta.etl_forever'::regclass
       AND c.conname = 'etl_forever_status_check';
    IF cdef IS NULL OR position('released' in cdef) = 0 THEN
        ALTER TABLE starcubes_meta.etl_forever
            DROP CONSTRAINT IF EXISTS etl_forever_status_check;
        ALTER TABLE starcubes_meta.etl_forever
            ADD CONSTRAINT etl_forever_status_check
            CHECK (status IN ('running', 'stopping', 'stopped', 'failed',
                              'released'));
    END IF;
END $$;

COMMENT ON COLUMN starcubes_meta.etl_forever.status IS
  'running/stopping: active (one per datasource, ux_etl_forever_active). stopped: ended on request, never adopted. failed: the process died or never started. released: left on SIGTERM without a stop request while its container drained -- adopted by another container.';
