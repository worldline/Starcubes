-- A log file as a second kind of source (migration 0047).
--
-- Up to here a datasource was a SQL database and nothing else: source_dialect
-- was constrained to four dialects, the projection was a SELECT composed from
-- base_table plus each fact_column.source_expr, and the load window was a
-- WHERE clause. A great deal of operationally interesting timeseries data
-- never reaches a database, though -- access logs, HAProxy logs, syslog --
-- and nobody wants to stand up an ETL into Postgres first just to ask "how
-- many of these, by that, filtered on this, over the last month?".
--
-- So: source_dialect gains a fifth value, 'logfile'. For such a datasource
-- the measures and dimensions are **named groups of a regular expression**,
-- and every dimension is an auto-dimension -- a log file's value set is not
-- knowable up front, and the auto-dimension mechanism already discovers
-- values per chunk and is entirely independent of what the source was.
--
-- One field carries where the log is: source_dsn, with the scheme deciding
-- the transport (file:// glob, sftp://, https://, exec://). Reusing that
-- column rather than adding one keeps DSN masking, the connection test and
-- the profile machinery working unchanged.
--
-- The watermark stays a timestamp. That is the load-bearing decision here:
-- the natural position in a file is (file, byte offset), but etl_run.
-- last_end_time is a timestamptz and backfill, --start/--end, retention,
-- rewind/repair, the rollups, the alarms and the gap overlay all read it. A
-- time window therefore costs a scan of the files it touches and buys
-- everything else unchanged. watermark_column holds the *name of the time
-- group*, partition_column the fact column as always, so both NOT NULLs are
-- satisfied without loosening them; base_table, import_query,
-- watermark_max_query, small_window_hint and watermark_fact_expr stay NULL.
--
-- There is deliberately **no role='pk' column** for a log source. Log lines
-- carry no stable key, and the window arithmetic alone is enough because a
-- log file is kept in timestamp order: last_end_time is the *upper*,
-- half-open bound, so [last_end, until) joins on seamlessly with nothing
-- loaded twice. late_arrival_overlap_s must stay 0 (it needs a PK), the audit
-- contributes nothing to a log source and is switched off for it, and the
-- chart's drill-down transaction list degrades to empty on its own.
--
--   log_line_regex     the regex whose named groups are the fact columns.
--   log_record_start   optional: recognises the start of a new record, so a
--                      line matching neither it nor log_line_regex is
--                      appended to the previous record. Empty = one line is
--                      one record, which is every access-log format.
--   log_time_format    strptime for the time group. Empty = ISO-8601 is
--                      detected; '@epoch'/'@epoch_ms' for numeric stamps.
--   log_time_zone      IANA name, applied when the line carries no offset.
--                      Empty = UTC. This is the file-side counterpart of
--                      core/source.py:_pin_session_utc -- a file has no
--                      session to pin, so the zone is configuration, and the
--                      normalisation to naive UTC happens in exactly one
--                      place in the reader.
--   log_encoding       empty = utf-8. Read with errors='replace', because one
--                      bad byte must not end a load.
--   log_min_match_pct  0 = off, only count. Otherwise the match rate below
--                      which the run ends 'failed'. This is what makes a
--                      changed log line format loud instead of a silent drop
--                      in row count.
--
-- Two fact_column columns for the value transform. It has to act *before*
-- the type cast, and it earns its place twice over:
--
--   * Cardinality. A dim table is keyed smallserial -- 32767 values, hard.
--     An auto-dimension on an HTTP path where coordinates are encoded in the
--     path (proxied OSM tiles being the obvious case) fills that in minutes.
--     A substitution collapses millions of paths into one value.
--   * The non-values these formats write. nginx puts '-' in bytes, HAProxy
--     -1 in a timing field whose phase was never reached. Neither is an
--     exception; both are the normal case, and without a transform the cast
--     fails or every avg and min is skewed.
--
-- Two columns rather than one s/…/…/ string because paths contain slashes
-- and every delimiter choice eventually collides. Formally these are not
-- log-specific; practically they are, since a SQL source does the same thing
-- in source_expr. They stay NULL there and only the reader reads them.
--
-- etl_unparsed is the exact counterpart of etl_unmapped: that table holds the
-- dimension values that could not be resolved, this one the lines that could
-- not be parsed, both per run and both shown in the ETL tab. Capped per run
-- by STARCUBES_UNPARSED_MAX, and the raw line truncated on write -- the point
-- is to show the operator *which shape* stopped matching, not to mirror the
-- log into the warehouse.
--
-- Idempotent -- re-running on a populated DB is a no-op. Mirror this block
-- in starcubes_meta.sql (which is what core/schema.py runs); keeping the two
-- in lock-step is the contract verified in CLAUDE.md.

-- Refresh the source_dialect CHECK on databases created before 'logfile'.
-- The constraint is inline and unnamed in the CREATE TABLE, so Postgres
-- auto-named it datasource_source_dialect_check. Probing the definition
-- rather than dropping unconditionally is what keeps this re-runnable.
DO $$
DECLARE
    cdef text;
BEGIN
    SELECT pg_get_constraintdef(c.oid) INTO cdef
      FROM pg_constraint c
      JOIN pg_class t ON t.oid = c.conrelid
      JOIN pg_namespace n ON n.oid = t.relnamespace
     WHERE n.nspname = 'starcubes_meta'
       AND t.relname = 'datasource'
       AND c.conname = 'datasource_source_dialect_check';
    IF cdef IS NOT NULL AND position('logfile' in cdef) = 0 THEN
        ALTER TABLE starcubes_meta.datasource
            DROP CONSTRAINT datasource_source_dialect_check;
        ALTER TABLE starcubes_meta.datasource
            ADD CONSTRAINT datasource_source_dialect_check
            CHECK (source_dialect IN ('oracle', 'postgresql', 'mysql',
                                      'mssql', 'logfile'));
    END IF;
END $$;

ALTER TABLE starcubes_meta.datasource
    ADD COLUMN IF NOT EXISTS log_line_regex    text,
    ADD COLUMN IF NOT EXISTS log_record_start  text,
    ADD COLUMN IF NOT EXISTS log_time_format   text,
    ADD COLUMN IF NOT EXISTS log_time_zone     text,
    ADD COLUMN IF NOT EXISTS log_encoding      text,
    ADD COLUMN IF NOT EXISTS log_min_match_pct real NOT NULL DEFAULT 0;

COMMENT ON COLUMN starcubes_meta.datasource.log_line_regex IS
  'source_dialect=logfile: the regex whose named groups every fact_column.source_expr refers to. One named group per fact column, including the time one. Source.load refuses a log datasource whose fact columns name a group the regex does not have.';
COMMENT ON COLUMN starcubes_meta.datasource.log_record_start IS
  'source_dialect=logfile: optional regex recognising the first line of a record. A line matching neither this nor log_line_regex is appended to the previous record and it is re-parsed, which is how multi-line stack traces stay one row. Empty (the default) = one line is one record, and a non-matching line goes to etl_unparsed.';
COMMENT ON COLUMN starcubes_meta.datasource.log_time_format IS
  'source_dialect=logfile: strptime format for the time group. Empty = ISO-8601 is auto-detected. @epoch / @epoch_ms for numeric stamps. A format without a year (syslog RFC3164) takes the year from the file mtime.';
COMMENT ON COLUMN starcubes_meta.datasource.log_time_zone IS
  'source_dialect=logfile: IANA zone name applied when the parsed timestamp carries no offset; an offset in the line (%z) always wins. Empty = UTC. This is configuration because a file has no session to pin, unlike a SQL source (core/source.py:_pin_session_utc) -- and a naive value carries no evidence about its zone, so nothing can detect a wrong setting here.';
COMMENT ON COLUMN starcubes_meta.datasource.log_encoding IS
  'source_dialect=logfile: text encoding of the log, empty = utf-8. Always read with errors=replace: one bad byte in a gigabyte must not end a load.';
COMMENT ON COLUMN starcubes_meta.datasource.log_min_match_pct IS
  'source_dialect=logfile: the share of lines that must match log_line_regex, in percent. 0 = off (the default), lines are only counted into etl_unparsed and stats. Set it and a changed log line format ends the run failed with the rate in the error text, instead of silently dropping the row count.';

ALTER TABLE starcubes_meta.fact_column
    ADD COLUMN IF NOT EXISTS transform_pattern text,
    ADD COLUMN IF NOT EXISTS transform_replace text;

COMMENT ON COLUMN starcubes_meta.fact_column.transform_pattern IS
  'Log sources only: regex applied to this column''s raw value with re.sub after the line matched and *before* the type cast. Empty = no transform. Two uses, both routine rather than exotic: collapsing an unbounded value into a usable dimension (an OSM tile path is millions of distinct values against a 32767 smallserial ceiling), and mapping a format''s non-value onto a number (nginx writes - for bytes, HAProxy -1 for a phase never reached).';
COMMENT ON COLUMN starcubes_meta.fact_column.transform_replace IS
  'The replacement for transform_pattern; backreferences (\1) allowed. NULL with a pattern set means the empty string. Two columns rather than one s/…/…/ expression because paths contain slashes and every delimiter choice eventually collides with the data.';

CREATE TABLE IF NOT EXISTS starcubes_meta.etl_unparsed (
    id          serial PRIMARY KEY,
    etl_run_id  int  NOT NULL REFERENCES starcubes_meta.etl_run(id)
                    ON DELETE CASCADE,
    file_name   text NOT NULL,
    line_no     bigint NOT NULL,
    raw_line    text NOT NULL,
    seen_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_etl_unparsed_run
    ON starcubes_meta.etl_unparsed (etl_run_id);

COMMENT ON TABLE starcubes_meta.etl_unparsed IS
  'Log lines a run could not parse, the counterpart of etl_unmapped for dimension values. Capped per run by STARCUBES_UNPARSED_MAX and the raw line truncated on write: the purpose is to show which line shape stopped matching, not to mirror the log into the warehouse. Shown in the ETL tab; the aggregate figures live in etl_run.stats (parsed_rows, unparsed_rows, match_pct).';
