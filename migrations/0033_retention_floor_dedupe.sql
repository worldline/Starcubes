-- Two columns for what happens to the watermark when fact rows disappear
-- (migration 0033).
--
-- Four operations remove fact rows -- delete_fact_from, truncate_partition,
-- drop_partition, truncate_last_partition -- and until now not one of them
-- told the high watermark. _resume_state reads last_end_time from the newest
-- ok/incremental etl_run row, so after a partial delete the metadata
-- watermark simply stands and the next window starts above the hole. The
-- deleted rows are never requested again. Nothing logs it, nothing shows it;
-- the Audit tab would find it, but only if a human runs it over exactly that
-- window. Same class of bug as the late-arrival hole the window bound was
-- built to close.
--
-- The fix is not one mechanism but two, because deleting means two opposite
-- things:
--
--   delete_fact_from is a *repair* gesture -- the data is wrong, throw it
--   away, load it again. It wants the watermark rewound so the ETL fetches
--   the window a second time.
--
--   drop_partition / truncate_partition are *disposal* gestures -- reclaim
--   the space, the data is not coming back. A rewind there would make the
--   ETL reload everything the operator just deleted, on the next iteration,
--   at full throughput.
--
-- Any automation that treats the two alike breaks one of them. So disposal
-- gets a floor instead of a rewind:
--
--   retention_floor  a lower bound on what this datasource still holds.
--                    core/audit.py clamps its windows up to it, so a retired
--                    month stops reading as a million-row gap. It is only
--                    moved when the *oldest* partition goes: a partition
--                    dropped out of the middle leaves a hole no single floor
--                    can express, and claiming otherwise would be worse than
--                    leaving the floor where it is.
--
-- and the rewind gets an explicit de-dupe arming flag:
--
--   dedupe_from      the instant from which the next incremental run must
--                    filter against the PKs already in fact. A rewind
--                    reloads a window and there is no unique index on fact
--                    to absorb it. As long as partition_column ==
--                    watermark_column the reload is clean -- the delete
--                    removed exactly what will be re-inserted. When they
--                    differ (the example Oracle source watermarks on
--                    end_time but partitions on create_time) rows with
--                    create_time < T <= end_time were *not* deleted and
--                    *will* be reloaded. The engine already owns the answer
--                    -- _overlap_pks plus the per-chunk PK filter -- but
--                    that path arms itself only when late_arrival_overlap_s
--                    > 0 or the previous run ended badly. This column arms
--                    it explicitly for the affected span; the run that
--                    honours it clears it again. Without it a rewind on a
--                    source with a derived watermark trades a silent hole
--                    for silent duplicates.
--
-- Both are timestamptz and both default to NULL, which is exactly the old
-- behaviour: no floor, no arming.
--
-- Idempotent -- re-running on a populated DB is a no-op. Mirror this block
-- in starcubes_meta.sql (which is what core/schema.py runs); keeping the two
-- in lock-step is the contract verified in CLAUDE.md.
ALTER TABLE starcubes_meta.datasource
    ADD COLUMN IF NOT EXISTS retention_floor timestamptz,
    ADD COLUMN IF NOT EXISTS dedupe_from     timestamptz;

COMMENT ON COLUMN starcubes_meta.datasource.retention_floor IS
  'Rows with partition_column below this instant were disposed of on purpose (a dropped or truncated partition), not lost. core/audit.py clamps its windows up to it so a retired window is not reported as a gap. Only advanced when the oldest partition goes -- a hole in the middle cannot be expressed as a floor. NULL means nothing has been retired.';
COMMENT ON COLUMN starcubes_meta.datasource.dedupe_from IS
  'Set by a high-watermark rewind (delete_fact_from): the next incremental run must load with the _overlap_pks PK filter armed from this instant, even when late_arrival_overlap_s is 0, or the reload duplicates the rows the delete did not remove. Cleared by the run that honoured it. NULL means no rewind is pending.';
