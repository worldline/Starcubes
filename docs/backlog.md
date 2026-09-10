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

- **`starcubes_meta.datasource` grows a column per tuning knob.** The table
  is at **35 columns**, and every new per-source parameter adds one. Counted
  honestly, though, the `ALTER TABLE` is the cheap part. A new parameter
  (measured on `max_run_s`, commits `371b5dc` + `4f0c8d3`) repeats its own
  name at **eight** sites: the per-step migration, the mirror block in
  `migrations/starcubes_meta.sql`, the `SELECT` list *and* the dataclass
  field in `core/source.py`, the `UPDATE SET` line plus its bind parameter in
  `sources/views.py` (and often the `INSERT` too), the `form-group` block in
  `edit.html`, sometimes `new.html`, the `_SOURCE_TEXT_FIELDS` /
  `_SOURCE_INT_FIELDS` tuples in `core/profile.py`, and the docs.

  A key/value table (`datasource_id, key, value` — keyed on the id, not
  `schema_name`, so the `ON DELETE CASCADE` and the existing joins survive)
  removes the first two sites and half the third: roughly **a quarter** of
  the per-parameter work. The other three quarters are the form field, the
  validation, the UPDATE mapping and the profile export, none of which the
  schema change touches. `app_config` (migration 0031, resolved by
  `core/config.py`) is the precedent for the KV half and shows the pattern
  works — including "row absent = default", defined once in Python.

  Only **23** of the 35 columns are eligible. Seven are identity/structure
  (`id`, `name`, `schema_name`, `source_dialect`, `source_dsn`,
  `created_at`, `updated_at`) and five carry DB semantics a KV row cannot
  express or should not hold: `is_default` (partial unique index),
  `cache_epoch` (hot `UPDATE … RETURNING` counter), and `retention_floor` /
  `dedupe_from` / `dim_refreshed_at`, which are runtime *state* written
  mid-run, not configuration.

  The costs, so nobody rediscovers them: types collapse to `text`, so
  `interval_ladder int[]`, `bar_pixel_target real`, the timestamps and the
  booleans all need parsing on read, and the three CHECK constraints
  (`partition_period`, the `schema_name` regex, `wallclock_lag_check`) move
  into application code. About eight read sites need a pivot instead of a
  column reference — `core/etl.py` (5×), `core/housekeeping.py`,
  `core/preflight.py`, `core/pipeline_alarm.py`, `sources/alarm_notify.py`,
  `sources/views.py`. The templates, happily, need nothing: `_load_datasource`
  (`sources/views.py:140`, `:3793`) is a `SELECT *` feeding the template
  context, so merging the KV rows into that dict keeps `{{ ds.max_run_s }}`
  working unchanged.

  Three shapes, evaluated:

  * **A — KV table only.** 1–2 days. Buys two SQL blocks per parameter, pays
    with type safety. A bad trade; recorded so it is not proposed again.
  * **B — a parameter registry in Python, columns stay.** One declarative
    table (name, type, default, validator, label, hint, profile-exportable)
    that `Source.load`, the `UPDATE`, the form rendering and the profile
    export are *generated* from. A new parameter becomes one registry row
    plus the one-line `ALTER`. **~1 day, ~75% of the benefit, no data
    migration, types and CHECKs intact.**
  * **C — B plus KV for the 23 knobs.** One more day for the unpivot
    `INSERT … SELECT`, the `DROP COLUMN`s, the pivot sites, `seed_example.py`
    and the files in `profiles/`. Afterwards a parameter costs exactly one
    registry row and the mirror-block contract enforced by
    `check_migration_mirror` stops growing.

  Build **B, leave C open**: B is a pure Python restructuring, reversible,
  and it makes the remaining one-line `ALTER` easy to price honestly. If it
  still grates afterwards, C is a mechanical follow-up because every read
  site already goes through the registry by then.

## Grouped-dimension merge sums averages and extremes

`core/query.py`'s `group_by_grouped` branch merges per-index rows into group
names with `d[gname] = d.get(gname, 0) + v`. That is correct for `count(*)`
and `sum()`, and wrong for the other three `MEASURE_FUNCS`: it adds the
*averages* of the group members, and adds their minima and maxima instead of
taking the extreme of the extremes. The example catalogue contains exactly
this combination (`Avg stay | avg(stay) | multibar`).

Predates pre-aggregation and lives on the `fact` path; `core/rollup.py`
reproduces it deliberately so `rollup_verify`'s equivalence comparison stays
meaningful. Fixing it means changing both paths in one commit: on `fact`
project `sum(col)`/`count(col)` instead of `avg(col)` and merge weighted, and
merge `min`/`max` with `min()`/`max()` instead of `+`. The rollup path is then
the easier of the two, since it already stores `sum_<col>` and `n`.

Not reproducible on the example datasource as it stands — no `dim_group_set`
is defined there, so the branch cannot be reached without creating one first.

## The missing-index warning has no trigger outside an ETL run

U10 (`core/preflight._check_indices`) reports a fact table missing its indices
and names the command that fixes it, but preflight only runs as part of a
load. The state right after the initial load is *by definition* the one
without indices (they are dropped for the load), and the check that would
notice runs at the start of the *next* load. A datasource loaded once and then
left alone never reports it — which is how `payments` sat at 3.9M rows over
~2500 hours with no index on `create_time` and a chart query 25x slower than
it needed to be.

Calling U10 from `run_housekeeping` as well would close it: housekeeping is
periodic, already collects warnings into `report["warnings"]`, and the check
is read-only.
