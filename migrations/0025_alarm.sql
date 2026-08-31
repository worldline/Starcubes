-- 0025_alarm.sql — per-datasource threshold alarms.
--
-- An alarm is a rule attached to one datasource: "if count(*) over the
-- last <window_s> seconds is <cond_op> <threshold> for <measure> grouped
-- by <context_dim>/<context_value>, notify <channels>". The four
-- notification channels (email, slack, pagerduty, log) are the value
-- space of notify_channels text[]; only `log` is wired today (every
-- other channel is on the backlog).
--
-- Idempotent — re-running on a populated DB is a no-op. Mirror this
-- block in starcubes_meta.sql (which is what `core/schema.py` runs);
-- keeping the two in lock-step is the contract verified in CLAUDE.md.

CREATE TABLE IF NOT EXISTS starcubes_meta.alarm (
    id              serial PRIMARY KEY,
    datasource_id   int  NOT NULL REFERENCES starcubes_meta.datasource(id) ON DELETE CASCADE,
    name            text NOT NULL,
    context_dim     text,
    context_value   text,
    measure         text NOT NULL DEFAULT 'count(*)',
    cond_op         text NOT NULL DEFAULT '>'
                        CHECK (cond_op IN ('>', '<', '=', '>=', '<=')),
    threshold       double precision NOT NULL,
    window_s        int  NOT NULL DEFAULT 300
                        CHECK (window_s > 0),
    notify_channels text[] NOT NULL DEFAULT '{}',
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (datasource_id, name)
);

CREATE INDEX IF NOT EXISTS ix_alarm_datasource
    ON starcubes_meta.alarm (datasource_id);