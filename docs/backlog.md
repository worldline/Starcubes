# Backlog — known unfinished work

Known items the team has acknowledged but not yet addressed. Read when
planning new work or assessing scope; a check against this list is part
of *any* plan that touches `core/etl.py`, `core/audit.py`, `core/adhoc.py`,
the ETL forever session, the Query console, or the schema-name / partition /
auth surface area.

This is the highest-value "should we?" file in the repo — items listed
here have been measured, not guessed, and most have a non-obvious
trap (clock skew, partition-name sorting, $500B kill) that the linked
discussion captures.

## Open items

- **Multi-dialect WHERE construction.** `core/etl.py` emits Oracle
  `to_timestamp(..., 'YYYY-MM-DD HH24:MI:SS')` literals — fine for the
  example Oracle source, wrong for Postgres/MySQL. When a second non-Oracle
  source is wired up, switch to SQLAlchemy bind parameters. (`core/audit.py`
  already uses binds for its source-side counts and avoids any truncation
  function for exactly this reason.)
- **Test coverage.** No unit or integration tests for `core/etl.py`,
  `core/query.py`, or the three-stage multiprocessing pipeline. The
  `_resume_state` deduplication logic and `copy_from` path are especially
  risky to change without tests. (Both the late-arrival overlap work and the
  two-term window bound were verified with throwaway end-to-end scripts
  against a local Postgres acting as both source and warehouse — that shape
  is worth keeping if a suite ever lands. It earns its keep: that run is what
  caught the source-session timezone skew, and what caught
  `audit_id_details` excluding the initial load's `first_end_time IS NULL`
  run and so reporting every first-load row as unattributable. It also
  showed the opposite lesson — a fixture that forces `timezone=UTC` on both
  the source and the warehouse connection cannot see clock-mixing bugs at
  all, and duly hid one in `needed_lag_s` until it was read by hand. Any
  successor suite wants at least one source connection deliberately *not* on
  UTC.)
- **Late arrivals beyond the overlap are only found on demand.** The Audit
  tab has to be run by a human over a chosen window; nothing schedules it,
  and nothing alerts on a gap. A periodic audit of the trailing N hours
  (with the result stored per run) would close that.
- **Backfill re-scans the window even when the exact ids are known.**
  `audit_ids` names the missing rows, but `import_fact(backfill=True)` still
  re-extracts the whole window and drops what it already has. For a sparse
  gap (37 rows in an hour) a targeted `WHERE pk IN (…)` extract would be
  orders of magnitude cheaper. The blocker is handing the id list to the
  spawned subprocess — a CLI argument will not hold it, so it needs a temp
  file or a `starcubes_meta` staging row — plus chunking the IN-list at 1000
  for Oracle.
- **A source whose time columns do not hold UTC.** The pipeline treats every
  naive source timestamp as UTC — `parse_dates={pc: {"utc": True}}` in the
  extract, `_as_utc` in `core/audit.py`, `_parse_dt` in `core/utils.py`, and
  a dozen `replace(tzinfo=timezone.utc)` calls. A source writing local time
  therefore shows up shifted by its UTC offset everywhere: chart, audit
  buckets, partition routing. Nothing detects it. Supporting it needs a
  `source_timezone` column that converts on the way in, and it has to reach
  all of those places at once — declaring it for the wall-clock term alone
  would only move the inconsistency. The `end_time > max_wm` clamp in
  `import_fact` bounds such a source to a freshness problem rather than data
  loss, which is why this is a backlog item and not an open wound.
- **Source schema drift guard.** No pre-flight validation that upstream
  columns still match `source_expr` / `base_table` before the extract loop.
  A dropped/renamed source column fails mid-run.
- **`datasource.schema_name` quoting — resolved by validation, not by
  quoting.** The name is still interpolated raw into ~70 `f"{schema}.fact"`
  sites across `core/` and `sources/`, while pandas' `to_sql` quotes it.
  Rather than convert those sites to `%I` — they are consistently unquoted
  and therefore work, so quoting them would break every installation whose
  schema is already sitting folded on disk — the name is restricted to what
  survives Postgres case-folding unchanged:
  `core/schema.py:validate_schema_name` on `^[a-z_][a-z0-9_]{0,62}$`, called
  from `source_new`, `source_edit`, `profile_store.insert_datasource` and
  `ensure_schema`, with a `NOT VALID` CHECK
  (`migrations/0024_schema_name_check.sql`) as backstop. `source_edit` skips
  the check when the submitted name is unchanged, so a row predating it stays
  editable. Remaining gap: nothing repairs a datasource already holding a
  folded name — its metadata and disk still disagree until someone edits the
  field to the folded spelling.
- **Partition retirement.** `import_fact` now ensures partitions forward of
  the load window exist on every run (`core/schema.py:ensure_partitions_for`,
  see [`docs/etl-engine.md`](etl-engine.md) "Importing ensures partitions
  exist before the worker pool") — the manual `create_future_partitions`
  CLI flag / GUI button is now redundant for the common case. Old partitions
  are still not auto-dropped or archived; that stays manual
  (`etl/drop-partition/`, `etl/truncate-partition/`).
- **Authentication on JSON endpoints.** All `viz/` JSON endpoints
  (`plot_json`, `drill_json`, `txn_json`, `api_sources`, `api_source_meta`)
  are public. The chart's user-tunable settings (Y-axis mode, bar pixel
  target) live in the browser's `localStorage` — every visitor can change
  them, nobody persists per-user server-side.
- **Engine cache fast-fail for dead sources.** `_get_cached_engine` relies
  on `pool_pre_ping=True` and `pool_timeout=30`. Stale engines retry until
  timeout on every checkout. No circuit breaker.
- **A `SELECT`-only DB role for the Query console.** The chart's ad-hoc SQL
  console (`core/adhoc.py`) runs as the app's own Postgres role. The read-only
  transaction blocks writes but not `pg_sleep()`, `pg_read_file()`, or reading
  every table in the schema. The real security boundary would be a second DSN
  pointing at a dedicated reader role; the four read-only layers are
  defence-in-depth, not a substitute.
- **Console concurrency.** uWSGI has `processes = 2 × threads = 4` = 8 request
  slots. Enough simultaneous long console queries stall the whole app —
  *including the cancel requests that would clear them*. The 30 s
  `statement_timeout` bounds it; raising `threads` or a per-user guard would
  bound it better.