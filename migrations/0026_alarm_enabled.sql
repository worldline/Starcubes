-- 0026_alarm_enabled.sql — the alarm.enabled flag that 0025 was meant to ship.
--
-- The alarm design called for an on/off switch per rule, so an operator can
-- silence a noisy alarm without losing its definition. 0025 created the table
-- without the column, which left the flag unreachable: the GUI had no toggle
-- and the CRUD endpoints had nothing to write. Adding it here rather than
-- editing 0025 keeps already-migrated installs correct.
--
-- Existing rows default to enabled — that matches how they behaved while the
-- column did not exist. Evaluation is still backlog; nothing reads this flag
-- yet beyond the GUI that displays it.
--
-- Idempotent — re-running on a populated DB is a no-op. Mirror this block in
-- starcubes_meta.sql (which is what `core/schema.py` runs); keeping the two
-- in lock-step is the contract verified in CLAUDE.md.

ALTER TABLE starcubes_meta.alarm
    ADD COLUMN IF NOT EXISTS enabled boolean NOT NULL DEFAULT true;

COMMENT ON COLUMN starcubes_meta.alarm.enabled IS
    'Rule is active. Persisted and editable, but no evaluation loop reads it yet — see docs/backlog.md.';
