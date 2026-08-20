-- Rename schema gstar_meta -> starcubes_meta (project rename gstar -> starcubes).
--
-- Run this against an existing pre-rename deployment BEFORE starting the app
-- with the new code. Reason: starcubes/db_backends/base.py runs
-- `CREATE SCHEMA IF NOT EXISTS starcubes_meta` on every Django DB connection,
-- and sources/apps.py:SourcesConfig.ready() calls ensure_starcubes_meta() on
-- every app start, which populates a full (empty) table set via the
-- consolidated migrations/starcubes_meta.sql if the `datasource` table is
-- missing. If the app was already started against this database after the
-- code upgrade but before this migration ran, an empty `starcubes_meta`
-- shadow schema (0 rows) now sits next to the real, still-untouched
-- `gstar_meta`. This script detects and safely drops that empty shadow
-- schema before renaming; it refuses to touch `starcubes_meta` if it
-- already holds real data.
--
-- Idempotent: safe to re-run.

DO $$
DECLARE
  old_exists boolean;
  new_exists boolean;
  new_has_datasource_table boolean;
  new_has_data boolean;
BEGIN
  SELECT EXISTS (
    SELECT 1 FROM information_schema.schemata WHERE schema_name = 'gstar_meta'
  ) INTO old_exists;

  IF NOT old_exists THEN
    RAISE NOTICE 'gstar_meta schema not found -- nothing to migrate (already renamed, or a fresh install).';
    RETURN;
  END IF;

  SELECT EXISTS (
    SELECT 1 FROM information_schema.schemata WHERE schema_name = 'starcubes_meta'
  ) INTO new_exists;

  IF new_exists THEN
    SELECT EXISTS (
      SELECT 1 FROM information_schema.tables
      WHERE table_schema = 'starcubes_meta' AND table_name = 'datasource'
    ) INTO new_has_datasource_table;

    new_has_data := false;
    IF new_has_datasource_table THEN
      EXECUTE 'SELECT EXISTS (SELECT 1 FROM starcubes_meta.datasource LIMIT 1)' INTO new_has_data;
    END IF;

    IF new_has_data THEN
      RAISE EXCEPTION 'starcubes_meta already exists and contains data -- refusing to auto-rename gstar_meta over it. Resolve manually (verify which schema is authoritative, then drop the stale one) before re-running this migration.';
    END IF;

    RAISE NOTICE 'Dropping empty starcubes_meta schema (auto-created by app bootstrap) before rename.';
    DROP SCHEMA starcubes_meta CASCADE;
  END IF;

  ALTER SCHEMA gstar_meta RENAME TO starcubes_meta;
  RAISE NOTICE 'Renamed schema gstar_meta -> starcubes_meta.';
END $$;
