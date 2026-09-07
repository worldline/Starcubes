-- 0032_alarm_severity_optin.sql — PagerDuty severity moves to the alarm;
-- recipients get an opt-in for the two personal channels.
--
-- 1. alarm.severity: the PagerDuty trigger severity was global
--    (STARCUBES_PAGERDUTY_SEVERITY, default 'critical'); now an alarm can
--    carry its own. One of PagerDuty's four severities or NULL = fall back
--    to the global value at dispatch time (sources/alarm_notify.py).
--    The column is plain text + CHECK rather than an enum: PagerDuty's
--    vocabulary is the only contract here and enums complicate the mirror.
-- 2. auth_user.notify_email / notify_xmpp: the account-level opt-in for
--    the two personal channels. DELIBERATE DEFAULT OFF: being listed on an
--    alarm is the alarm author's decision, receiving is the user's — a
--    user who never opts in (here or on My Profile) is silently dropped
--    from e-mail/XMPP dispatch, so existing alarms fall quiet for them
--    until they opt in. PagerDuty and log are untouched by this flag.
--
-- The auth_user block is guarded because bootstrap runs this script BEFORE
-- `migrate`, when Django's tables do not exist yet on a fresh install;
-- `sources/management/commands/bootstrap.py` therefore runs the whole
-- script a second time after `migrate` to pick the column up there.
-- Idempotent — re-running on a populated DB is a no-op. Mirror this
-- block in starcubes_meta.sql (which is what `core/schema.py` runs);
-- keeping the two in lock-step is the contract verified in CLAUDE.md.

ALTER TABLE starcubes_meta.alarm
    ADD COLUMN IF NOT EXISTS severity text;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                 WHERE conname = 'alarm_severity_check'
                   AND conrelid = 'starcubes_meta.alarm'::regclass)
  THEN ALTER TABLE starcubes_meta.alarm
           ADD CONSTRAINT alarm_severity_check
           CHECK (severity IS NULL
                  OR severity IN ('critical', 'error', 'warning', 'info'));
  END IF;
END $$;

COMMENT ON COLUMN starcubes_meta.alarm.severity IS
    'PagerDuty severity of this alarm''s trigger/resolve events: critical, error, warning or info. NULL/empty = fall back to STARCUBES_PAGERDUTY_SEVERITY at dispatch time (send_pagerduty_test always uses the global value — it tests the transport, not an alarm''s routing choice).';
-- The Django model does not know these columns; sources/views.py and
-- sources/alarm_notify.py read and write them via raw SQL. Both the ALTER
-- and the COMMENT are guarded: bootstrap runs this script BEFORE `migrate`,
-- when auth_user does not exist yet on a fresh install — a COMMENT on a
-- missing table is an error, not a notice, and would abort the script.
DO $$ BEGIN
  IF to_regclass('starcubes_meta.auth_user') IS NOT NULL
     AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                     WHERE table_schema = 'starcubes_meta'
                       AND table_name   = 'auth_user'
                       AND column_name  = 'notify_email')
  THEN ALTER TABLE starcubes_meta.auth_user
           ADD COLUMN notify_email boolean NOT NULL DEFAULT false;
  END IF;
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_schema = 'starcubes_meta'
               AND table_name   = 'auth_user'
               AND column_name  = 'notify_email')
  THEN
    EXECUTE 'COMMENT ON COLUMN starcubes_meta.auth_user.notify_email IS'
            '''Deliberate default-off opt-in: the account receives alarm e-mail only when this is true AND the alarm lists the account in notify_email_recipients. Maintained on My Profile; the Django model does not know this column.''';
  END IF;
  IF to_regclass('starcubes_meta.auth_user') IS NOT NULL
     AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                     WHERE table_schema = 'starcubes_meta'
                       AND table_name   = 'auth_user'
                       AND column_name  = 'notify_xmpp')
  THEN ALTER TABLE starcubes_meta.auth_user
           ADD COLUMN notify_xmpp boolean NOT NULL DEFAULT false;
  END IF;
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_schema = 'starcubes_meta'
               AND table_name   = 'auth_user'
               AND column_name  = 'notify_xmpp')
  THEN
    EXECUTE 'COMMENT ON COLUMN starcubes_meta.auth_user.notify_xmpp IS'
            '''Deliberate default-off opt-in: the account receives alarm XMPP messages only when this is true AND the alarm lists the account in notify_xmpp_recipients. Maintained on My Profile; the Django model does not know this column.''';
  END IF;
END $$;