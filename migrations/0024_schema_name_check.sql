-- Backstop for datasource.schema_name.
--
-- The name is interpolated unquoted into ~70 f-string SQL statements across
-- core/, while pandas' to_sql quotes it. An unquoted identifier is
-- case-folded by Postgres, so any name that is not already lower-case makes
-- the two disagree: CREATE SCHEMA Test creates `test`, then
-- to_sql(schema="Test") looks for `"Test"` and fails with "schema does not
-- exist". A name needing quotes at all (hyphen, space) breaks the raw
-- interpolation with a syntax error, and an unvalidated name is a DDL
-- injection vector besides.
--
-- The real guard is core.schema.validate_schema_name() at every write path;
-- this constraint is the backstop for anything writing the table directly.
--
-- Added NOT VALID on purpose: installations predating the check may hold a
-- folded name, and validating retroactively would fail the migration and
-- make those rows unsaveable. New and updated rows are checked; existing
-- ones are left alone until someone edits them.
--
-- Idempotent: re-running is part of the contract.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
          JOIN pg_class t ON t.oid = c.conrelid
          JOIN pg_namespace n ON n.oid = t.relnamespace
         WHERE n.nspname = 'starcubes_meta'
           AND t.relname = 'datasource'
           AND c.conname = 'datasource_schema_name_check'
    ) THEN
        ALTER TABLE starcubes_meta.datasource
            ADD CONSTRAINT datasource_schema_name_check
            CHECK (schema_name ~ '^[a-z_][a-z0-9_]{0,62}$') NOT VALID;
    END IF;
END $$;
