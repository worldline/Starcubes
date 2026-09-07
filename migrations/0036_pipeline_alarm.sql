-- A second kind of alarm: one whose condition is the pipeline, not the data
-- (migration 0036).
--
-- Migrations 0033-0035 and the preflight built detection. The fact tables
-- are checked before every load, the dimensions and the disk are measured
-- every few hours, and `etl_run.max_wm`/`src_now` can see a source that is
-- reachable and has stopped producing. None of it reaches a person. The
-- findings sit in etl_run.stats, in housekeeping_run.report and in a GUI
-- panel, and are seen when somebody looks.
--
-- The alarm subsystem already owns everything hard about not being seen:
-- channels, per-user opt-in, PagerDuty severity and dedup keys, and — most
-- importantly — the episode semantics in alarm_firing. One row per incident,
-- opened and closed, at most one open per rule. A pipeline condition has
-- exactly that shape: it starts, it persists, it ends. So this is not a new
-- subsystem, it is a second *kind* of row in the existing one.
--
--   rule_kind   'threshold' (the existing measure-over-fact-rows alarm) or
--               'pipeline'.
--   condition   which pipeline condition, NULL for a threshold alarm.
--
-- **The column is called rule_kind and not kind, and that is not a matter of
-- taste.** The transition dicts core/alarm.py hands to the notification
-- dispatcher already carry a key named `kind`, whose values are 'opened' and
-- 'closed'; sources/alarm_notify.py reads exactly that to decide whether a
-- message says OPENED or RESOLVED. Stamping an alarm column named `kind`
-- onto those dicts — which is what the existing stamp loop does with every
-- other alarm column — would silently turn every notification into a
-- resolution. alarm_history.kind and etl_run.kind are two more holders of
-- the name in the same neighbourhood.
--
-- What a pipeline alarm reuses unchanged: threshold and cond_op (every
-- condition below produces a number, so _COND_OPS decides the breach the
-- same way), notify_channels, the two recipient arrays, severity, enabled,
-- alarm_firing, and the whole notification path. What it does not use:
-- measure, window_s and context_filter, which keep their defaults and are
-- ignored — a pipeline condition has no aggregate, no trailing window and
-- no chart filter.
--
-- The five conditions, each evaluated purely against starcubes_meta so that
-- an evaluator outside the ETL never needs the source or the fact database:
--
--   data_stale       seconds of src_now - max_wm on the newest run: the
--                    source is up and has stopped producing (U9).
--   run_failed       failed runs since the last ok one. Threshold 1 means
--                    any failure, 3 means three in a row.
--   no_recent_run    seconds since the last ok run finished. The only way
--                    to see a forever loop that is wedged or gone, because
--                    a wedged process cannot report that it is wedged (U7).
--   preflight_warn   findings at warn or block in the newest run's preflight
--                    report — one rule delivers U1, U2, U5b, U10 and N16.
--   dim_full_pct     the fullest dimension against the 32767 smallserial
--                    ceiling, from the newest housekeeping report (N13).
--
-- Episodes for these are stamped with the wall clock, not the fact clock
-- that threshold alarms use. That is not a compromise: a threshold alarm
-- asks a question about data and must be stamped with the data's own clock,
-- while "how long has nothing arrived" only has an answer in wall-clock
-- time. For the same reason the eval_end skip ("the HWM did not move, so
-- the verdict would be identical") does not apply — a pipeline condition
-- changes without any new data. No replay cursor is needed either: the
-- episode machine is idempotent, a second evaluation extends rather than
-- re-opens, and ux_alarm_firing_open allows only one open episode per rule.
--
-- Idempotent -- re-running on a populated DB is a no-op. Mirror this block
-- in starcubes_meta.sql (which is what core/schema.py runs); keeping the two
-- in lock-step is the contract verified in CLAUDE.md.
ALTER TABLE starcubes_meta.alarm
    ADD COLUMN IF NOT EXISTS rule_kind text NOT NULL DEFAULT 'threshold',
    ADD COLUMN IF NOT EXISTS condition text;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE table_schema = 'starcubes_meta' AND table_name = 'alarm'
          AND constraint_name = 'alarm_rule_kind_check'
    ) THEN
        ALTER TABLE starcubes_meta.alarm
            ADD CONSTRAINT alarm_rule_kind_check
            CHECK (rule_kind IN ('threshold', 'pipeline'));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE table_schema = 'starcubes_meta' AND table_name = 'alarm'
          AND constraint_name = 'alarm_condition_check'
    ) THEN
        ALTER TABLE starcubes_meta.alarm
            ADD CONSTRAINT alarm_condition_check
            CHECK ((rule_kind = 'pipeline' AND condition IN (
                        'data_stale', 'run_failed', 'no_recent_run',
                        'preflight_warn', 'dim_full_pct'))
                   OR (rule_kind = 'threshold' AND condition IS NULL));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_alarm_rule_kind
    ON starcubes_meta.alarm (rule_kind) WHERE enabled;

COMMENT ON COLUMN starcubes_meta.alarm.rule_kind IS
  'threshold = a measure over fact rows in a trailing window; pipeline = a condition over ETL state (etl_run, the preflight report, the housekeeping report). Named rule_kind and not kind on purpose: the transition dicts sent to the notification dispatcher already carry a `kind` key meaning opened/closed, and an alarm column of that name would be stamped over it, turning every notification into a resolution.';
COMMENT ON COLUMN starcubes_meta.alarm.condition IS
  'Which pipeline condition, for rule_kind = pipeline; NULL otherwise. Each produces a number, so cond_op and threshold decide the breach exactly as they do for a threshold alarm. measure, window_s and context_filter are unused by a pipeline alarm and keep their defaults.';
