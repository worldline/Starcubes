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

Operational preconditions around a load — the ones that are still manual or
unwatched — are collected and evaluated in
[`etl-automation.md`](etl-automation.md) rather than listed one by one here.
Three items below are cross-referenced from it.

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
  (with the result stored per run) would close that. See
  [`etl-automation.md`](etl-automation.md) — the same document covers why a
  *total* standstill is invisible to both the audit and the alarms (U9).
- **Backfill re-scans the window even when the exact ids are known.**
  `audit_ids` names the missing rows, but `import_fact(backfill=True)` still
  re-extracts the whole window and drops what it already has. For a sparse
  gap (37 rows in an hour) a targeted `WHERE pk IN (…)` extract would be
  orders of magnitude cheaper. The blocker is handing the id list to the
  spawned subprocess — a CLI argument will not hold it, so it needs a temp
  file or a `starcubes_meta` staging row — plus chunking the IN-list at 1000
  for Oracle.
- **Source schema drift guard** — closed by `core/preflight.py`. The
  `WHERE 1=0` probe runs before the worker pool starts and blocks the run
  with the offending column named, instead of failing mid-extract. What is
  worth remembering from it: a rejected query and an unreachable source
  raise identically and need opposite answers (block vs. warn), so the
  connection is opened before the query is executed. U5b in
  [`etl-automation.md`](etl-automation.md).
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
- **Partition retirement** — closed. `datasource.retention_periods` drops
  aged-out partitions from the housekeeping, at most a couple per run so a
  mistyped value walks visibly instead of clearing the table in one pass.
  What made it an M rather than an S was never the drop loop: four
  subsystems assumed `fact` covered everything since the beginning of time
  and had to learn about the floor — the audit, the server-side plot cache,
  the chart's own in-page cache (which keys on the query string and had
  never heard of the cache epoch), and alarms, where a truncated window
  makes `count(*) <` report an outage retention itself caused. See
  [`etl-automation.md`](etl-automation.md).
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
- **Alarm notification transports** — closed by migration 0030:
  `email` (stdlib smtplib), `xmpp` (slixmpp bot account) and `pagerduty`
  (Events API v2, per-datasource routing key) are real transports now;
  dispatch lives in `sources/alarm_notify.py` — see `docs/alarm.md`.
  Remaining gaps: `slack` still has no transport, and recipients are the
  login users only (no external/distribution-list addresses — accepted
  limitation). Known accepted trade-off on the evaluation side unchanged:
  rows arriving late into an already-evaluated window are never
  re-evaluated (see `docs/alarm.md`).
- **Source-side timezone awareness.** The pipeline treats every naive source
  timestamp as UTC — `parse_dates={pc: {"utc": True}}` in the extract,
  `_as_utc` in `core/audit.py`, `_parse_dt` in `core/utils.py`, and a dozen
  `replace(tzinfo=timezone.utc)` calls. A source writing local time
  rather than data loss, which is why this is a backlog item and not an open
  wound.
- **Grouped-token filters do not fan out.** A filter_group
  (`starcubes_meta.dimension.filter_group`, see docs/star-schema.md) fans
  plain `in`/`not_in`/LIKE filters over every member fact column, but a
  grouped token (`<col>:grouped:<slug>`) still resolves against the *one*
  dimension row its group set is attached to. Group sets attach to a single
  `dimension` row, so members of a filter_group can carry different (or no)
  group sets — fanning out would need a "whose set wins" rule that is not
  worth inventing until a real query needs it. Plain filters on the same
  columns fan out normally.

- **A fact column promoted to a dimension is only repaired on two of the
  three paths — never for auto-dimensions.** `import_dimensions` calls
  `core/schema.py:_backfill_fact_column` on the inline path and on the
  source-DB path, but the auto-dimension branch only ensures the `-1` /
  `__unknown__` sentinels and `continue`s, so a column promoted to
  `role='auto-dimension'` keeps its raw source values until a foreign key or
  a wrong chart makes it visible. All three calls are additionally gated on
  `created` — on the dim table being brand new — so none of them repairs raw
  values that appear after the fact either. The symptom, the diagnosis, and
  why `--backfill` does not fix it are written up in
  [`star-schema.md`](star-schema.md).

  Closing this is **not** "call the same function on the third path". That
  entry explains why calling it on a live auto-dimension actively corrupts
  correctly mapped rows, and a safe form needs two things it does not have: a
  restriction to rows with no partner in `dim.index`, and an answer for the
  case where the index and `source_id` ranges overlap — which for
  auto-dimensions is the normal case, not the exception, because their
  `source_id`s are typically small integers and the indices start at 1. Where
  the ranges do overlap, no repair can be complete: rows whose raw value
  collides with a valid index are unfindable, and repairing the rest turns a
  loud foreign-key failure into a green one over data that is still wrong.

  Evaluated, not built. The shape worth building is a diagnosis plus an
  explicitly confirmed repair, never a silent automatism — `sync_schema_view`
  in `sources/views.py` is the precedent (dry-run diff, confirmation,
  ETL interlock), and `docs/etl-automation.md` rules the preflight out as the
  home for it: it "only repairs what is idempotent and finishes in
  milliseconds", which an UPDATE across a fact table is not. Also note that
  `create_indices` builds foreign keys only for `primary_dimensions()`, while
  several fact columns can share one dim table — a repair has to cover every
  column pointing at the table, or the non-primary ones stay broken.
