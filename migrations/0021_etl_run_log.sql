-- Per-run ETL log pointer + failure tail (migration 0021).
--
-- log_path: absolute path of the file the spawner (sources/etl_spawn.py)
--   handed the child as stdout/stderr. NULL for runs started before this
--   migration, or for runs started without a datasource name (should not
--   happen in practice).
-- log_tail: last ~8 KB / ~120 lines of that file, captured in
--   import_fact()'s finally block only when the run fails, so a failed
--   run is diagnosable from the GUI even when the container's
--   filesystem/log volume isn't reachable.
ALTER TABLE starcubes_meta.etl_run
    ADD COLUMN IF NOT EXISTS log_path text,
    ADD COLUMN IF NOT EXISTS log_tail text;

COMMENT ON COLUMN starcubes_meta.etl_run.log_path IS
  'Path of the per-run ETL log file (stdout+stderr of the spawned process). NULL for runs predating migration 0021.';
COMMENT ON COLUMN starcubes_meta.etl_run.log_tail IS
  'Last ~8 KB of the log file, captured on failure only, so the GUI can show a traceback without filesystem access.';
