-- 0028_alarm_context.sql — alarm context as a full filter expression,
-- change history, comment field, firing episodes, global evaluator session.
--
-- The single context_dim/context_value pair could hold exactly one
-- dimension/value pair, but a chart alarm context is the whole filter
-- expression (AND/OR nesting, fan-out group tokens). It is stored as the
-- chart's flat filter rows in context_filter jsonb; grouped tokens
-- (<col>:grouped:<slug>) are rejected at save time because group sets are
-- user-editable and would silently change the alarm's meaning. comment is a
-- plain per-alarm text field — changes to it are tracked through
-- alarm_history's old/new snapshots like any other field. eval_end is the
-- evaluation cursor (end of the last evaluated window on the fact clock);
-- the evaluator writes it without touching updated_at, so runtime progress
-- never masquerades as a user configuration change.
--
-- alarm_history: who changed what and when (created/updated snapshots).
-- There is no 'deleted' kind — all alarm children CASCADE, so a deleted
-- alarm's history is gone by construction.
--
-- alarm_firing: one row per episode, spanning the first to the last breach
-- of the threshold. ux_alarm_firing_open enforces at most one open episode
-- per alarm, so the evaluator needs no hand-rolled locking.
--
-- alarm_forever: the global evaluation loop's session table. The singleton
-- column exists so a partial unique index can pin "at most one active row
-- world-wide" (a partial unique index needs an indexable column; a constant
-- expression would also work but is version-sensitive).
--
-- Idempotent — re-running on a populated DB is a no-op. Mirror this
-- block in starcubes_meta.sql (which is what `core/schema.py` runs);
-- keeping the two in lock-step is the contract verified in CLAUDE.md.

ALTER TABLE starcubes_meta.alarm DROP COLUMN IF EXISTS context_dim;
ALTER TABLE starcubes_meta.alarm DROP COLUMN IF EXISTS context_value;

ALTER TABLE starcubes_meta.alarm
    ADD COLUMN IF NOT EXISTS context_filter jsonb;
ALTER TABLE starcubes_meta.alarm
    ADD COLUMN IF NOT EXISTS comment text;
ALTER TABLE starcubes_meta.alarm
    ADD COLUMN IF NOT EXISTS eval_end timestamptz;

COMMENT ON COLUMN starcubes_meta.alarm.context_filter IS
    'Alarm context: the chart''s flat filter rows [{col, op, vals, andOr, indent}] as jsonb. NULL/[] = the whole datasource. Grouped tokens (<col>:grouped:<slug>) are rejected at save time.';
COMMENT ON COLUMN starcubes_meta.alarm.comment IS
    'Free-text comment on this alarm. Changes are tracked through alarm_history snapshots like any other field.';
COMMENT ON COLUMN starcubes_meta.alarm.eval_end IS
    'Evaluation cursor: end of the last evaluated window (fact clock). NULL = never evaluated; the first evaluation checks exactly one trailing window_s window.';

CREATE TABLE IF NOT EXISTS starcubes_meta.alarm_history (
    id         serial PRIMARY KEY,
    alarm_id   int  NOT NULL REFERENCES starcubes_meta.alarm(id) ON DELETE CASCADE,
    kind       text NOT NULL CHECK (kind IN ('created', 'updated')),
    username   text NOT NULL,
    old_row    jsonb,
    new_row    jsonb,
    changed_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_alarm_history_alarm
    ON starcubes_meta.alarm_history (alarm_id, id DESC);

CREATE TABLE IF NOT EXISTS starcubes_meta.alarm_firing (
    id              serial PRIMARY KEY,
    alarm_id        int  NOT NULL REFERENCES starcubes_meta.alarm(id) ON DELETE CASCADE,
    first_breach_at timestamptz NOT NULL,
    last_breach_at  timestamptz NOT NULL,
    closed_at       timestamptz,
    first_value     double precision,
    last_value      double precision
);

CREATE INDEX IF NOT EXISTS ix_alarm_firing_alarm
    ON starcubes_meta.alarm_firing (alarm_id, id DESC);

-- At most one open episode (open = closed_at IS NULL) per alarm.
CREATE UNIQUE INDEX IF NOT EXISTS ux_alarm_firing_open
    ON starcubes_meta.alarm_firing (alarm_id) WHERE closed_at IS NULL;

CREATE TABLE IF NOT EXISTS starcubes_meta.alarm_forever (
    id             serial PRIMARY KEY,
    singleton      boolean NOT NULL DEFAULT true CHECK (singleton),
    pid            int,
    interval_s     int NOT NULL,
    started_at     timestamptz NOT NULL DEFAULT now(),
    stopped_at     timestamptz,
    status         text NOT NULL
                     CHECK (status IN ('running', 'stopping', 'stopped', 'failed')),
    stop_requested boolean NOT NULL DEFAULT false,
    error          text
);

-- At most one active (running/stopping) evaluator session world-wide.
CREATE UNIQUE INDEX IF NOT EXISTS ux_alarm_forever_active
    ON starcubes_meta.alarm_forever (singleton) WHERE status IN ('running', 'stopping');