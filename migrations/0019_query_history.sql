-- Per-user ad-hoc SQL history for the chart's Query console (migration 0019).
--
-- Deduplicated on (user_id, datasource_id, sql_hash): re-running the same
-- statement bumps last_run_at / run_count instead of appending a row, so the
-- history list stays a set of distinct queries ordered by recency.
--
-- sql_hash rather than sql_text in the unique index: a btree index entry is
-- capped at ~2704 bytes and a pasted query easily exceeds that. The hash is
-- sha256 hex over the *normalized* (stripped) SQL, computed in core/adhoc.py,
-- so trailing whitespace does not create duplicates.
--
-- user_id deliberately carries NO foreign key to starcubes_meta.auth_user.
-- SourcesConfig.ready() calls core.schema.ensure_starcubes_meta(), and ready()
-- fires *during* the call_command("migrate") in sources/management/commands/
-- bootstrap.py -- i.e. before Django's own migrations create auth_user. A hard
-- FK would therefore break every fresh install. Rows are cleaned up explicitly
-- by the delete_user branch of sources/views.py:settings_page instead.
--
-- Idempotent: re-running this file is a no-op.

CREATE TABLE IF NOT EXISTS starcubes_meta.query_history (
    id             bigserial   PRIMARY KEY,
    user_id        int         NOT NULL,
    datasource_id  int         NOT NULL REFERENCES starcubes_meta.datasource(id)
                                   ON DELETE CASCADE,
    sql_hash       text        NOT NULL,
    sql_text       text        NOT NULL,
    first_run_at   timestamptz NOT NULL DEFAULT now(),
    last_run_at    timestamptz NOT NULL DEFAULT now(),
    run_count      int         NOT NULL DEFAULT 1
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_query_history_key
    ON starcubes_meta.query_history (user_id, datasource_id, sql_hash);

CREATE INDEX IF NOT EXISTS ix_query_history_recent
    ON starcubes_meta.query_history (user_id, datasource_id, last_run_at DESC);

COMMENT ON TABLE starcubes_meta.query_history IS
  'Per-user ad-hoc SQL history for the chart Query console, deduplicated per (user, datasource, sql).';
COMMENT ON COLUMN starcubes_meta.query_history.user_id IS
  'auth_user.id. No FK on purpose: auth_user is created by manage.py migrate, which runs after ensure_starcubes_meta on a fresh install.';
COMMENT ON COLUMN starcubes_meta.query_history.sql_hash IS
  'sha256 hex of the normalized SQL. Indexed instead of sql_text because btree entries are size-capped at ~2704 bytes.';
COMMENT ON COLUMN starcubes_meta.query_history.run_count IS
  'Incremented by the ON CONFLICT DO UPDATE upsert in core.adhoc.history_touch.';
