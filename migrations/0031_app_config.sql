-- 0031_app_config.sql — one key-value table for global app configuration
-- that the operator would rather not pass as environment variables:
-- the notification transports (STARCUBES_SMTP_* / XMPP_* / PAGERDUTY_*)
-- and the SSO/OIDC login (STARCUBES_OIDC_*).
--
-- The key IS the environment variable name it shadows. Resolution is
-- env-first everywhere (`core/config.py`): an env var that is set wins, a
-- row here is the fallback, and the hardcoded default in
-- starcubes/settings.py is the last resort. An absent row therefore means
-- "not configured here" — the table ships empty and upgrades change no
-- behaviour.
--
-- This is deliberately ONE table for every DB-editable global setting;
-- per-feature config tables were rejected. Per-datasource configuration
-- (connection, watermark, the PagerDuty routing key) keeps living on
-- starcubes_meta.datasource — this table is only for the global singleton
-- kind. Transports apply live (consumers re-read through a short-TTL
-- cache); SSO enable/disable needs a process restart, because the /oidc/
-- route mount and the authentication backend list are fixed at process
-- start. Secrets (SMTP_PASSWORD, XMPP_PASSWORD, OIDC_CLIENT_SECRET) are
-- stored like datasource.source_dsn: plaintext in the meta schema, masked
-- in every listing, never logged.
--
-- Idempotent — re-running on a populated DB is a no-op. Mirror this
-- block in starcubes_meta.sql (which is what `core/schema.py` runs);
-- keeping the two in lock-step is the contract verified in CLAUDE.md.

CREATE TABLE IF NOT EXISTS starcubes_meta.app_config (
    key        text PRIMARY KEY,
    value      text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by text NOT NULL
);

COMMENT ON TABLE starcubes_meta.app_config IS
    'Global app configuration as key-value rows, one table for every setting an operator may edit in the GUI instead of passing an environment variable: notification transports (STARCUBES_SMTP_*, STARCUBES_XMPP_*, STARCUBES_PAGERDUTY_*) and SSO/OIDC (STARCUBES_OIDC_*). The key is the env-var name it shadows.';

COMMENT ON COLUMN starcubes_meta.app_config.key IS
    'The STARCUBES_* environment variable name this row shadows. Only the 17 transport/SSO keys are read; writing any other key has no effect. Resolution is env-first: a set env var beats the row, the row beats the hardcoded default.';
COMMENT ON COLUMN starcubes_meta.app_config.value IS
    'The setting''s value as text. Deleting a row falls back to the environment/default — there is no empty-string state.';
COMMENT ON COLUMN starcubes_meta.app_config.updated_by IS
    'Username of the admin who last wrote the row, for the same provenance alarm_history keeps. Credentials are stored like datasource.source_dsn: plaintext here, masked in every listing, never logged.';