-- 0030_alarm_notify.sql — alarm notification transports: email, XMPP and
-- PagerDuty become first-class channels next to `log`.
--
-- Until now `alarm.notify_channels` was stored vocabulary — only `log` was
-- wired (the alarm_firing episode plus the evaluator's log line). This
-- migration carries the addressing the three real transports need:
--
-- 1. Recipients are the login users. There is deliberately no free-form
--    recipient table: the e-mail address already lives on the account
--    (auth_user.email, maintained on the settings page), and the XMPP
--    address joins it as a new auth_user column. The accepted limitation
--    is that only accounts can be notified — no distribution lists, no
--    external addresses.
-- 2. Per-alarm recipient selection is two int[] columns of auth_user ids,
--    one per address-carrying channel. Postgres supports foreign keys on
--    array elements in neither this form nor as jsonb, so the delete side
--    is hand-rolled: settings_page removes a deleted user's id from both
--    arrays in the same request.
-- 3. The PagerDuty integration token (Events API v2 routing key) is a
--    property of the datasource, not of the alarm: one service per source
--    is the natural PagerDuty shape, and several alarms fan into it.
--
-- Global transport configuration (SMTP server, XMPP bot account, PagerDuty
-- API base) lives in STARCUBES_* env vars, like every other container-level
-- setting — see docs/deployment-config.md. `alarm.notify_channels` itself
-- is unchanged: it stays the channel switch the evaluator reads.
--
-- The auth_user block is guarded because bootstrap runs this script BEFORE
-- `migrate`, when Django's tables do not exist yet on a fresh install;
-- `sources/management/commands/bootstrap.py` therefore runs the whole
-- script a second time after `migrate` to pick the column up there.
-- Idempotent — re-running on a populated DB is a no-op. Mirror this
-- block in starcubes_meta.sql (which is what `core/schema.py` runs);
-- keeping the two in lock-step is the contract verified in CLAUDE.md.

-- The XMPP address on the account. Django's model does not know this
-- column; sources/views.py reads and writes it via raw SQL. Both the ALTER
-- and the COMMENT are guarded: bootstrap runs this script BEFORE `migrate`,
-- when auth_user does not exist yet on a fresh install — a COMMENT on a
-- missing table is an error, not a notice, and would abort the script.
DO $$ BEGIN
  IF to_regclass('starcubes_meta.auth_user') IS NOT NULL
     AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                     WHERE table_schema = 'starcubes_meta'
                       AND table_name   = 'auth_user'
                       AND column_name  = 'xmpp_jid')
  THEN ALTER TABLE starcubes_meta.auth_user ADD COLUMN xmpp_jid text;
  END IF;
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_schema = 'starcubes_meta'
               AND table_name   = 'auth_user'
               AND column_name  = 'xmpp_jid')
  THEN
    EXECUTE 'COMMENT ON COLUMN starcubes_meta.auth_user.xmpp_jid IS'
            '''XMPP/Jabber address of this account, used as the alarm-notification target for the xmpp channel. Maintained on the settings page; the Django model does not know this column. NULL = this account cannot be reached over XMPP.''';
  END IF;
END $$;

ALTER TABLE starcubes_meta.alarm
    ADD COLUMN IF NOT EXISTS notify_email_recipients int[] NOT NULL DEFAULT '{}';
ALTER TABLE starcubes_meta.alarm
    ADD COLUMN IF NOT EXISTS notify_xmpp_recipients int[] NOT NULL DEFAULT '{}';

COMMENT ON COLUMN starcubes_meta.alarm.notify_email_recipients IS
    'auth_user ids selected as the e-mail recipients of this alarm, resolved through auth_user.email at dispatch time. Empty = no e-mail recipients (an alarm may then not select the email channel). No foreign key is possible on array elements; settings_page removes a deleted user''s id from here.';
COMMENT ON COLUMN starcubes_meta.alarm.notify_xmpp_recipients IS
    'auth_user ids selected as the XMPP recipients of this alarm, resolved through auth_user.xmpp_jid at dispatch time. Empty = no XMPP recipients (an alarm may then not select the xmpp channel). No foreign key is possible on array elements; settings_page removes a deleted user''s id from here.';

ALTER TABLE starcubes_meta.datasource
    ADD COLUMN IF NOT EXISTS pagerduty_routing_key text;

COMMENT ON COLUMN starcubes_meta.datasource.pagerduty_routing_key IS
    'PagerDuty Events API v2 routing key (integration token) of the PagerDuty service that carries this datasource''s alarms. One service per source: every alarm on the datasource triggers into the same key. NULL = PagerDuty disabled for this source. Credential — maintained on the source edit page, never exported in a profile.';