# Claude agent notes for starcubes

Context for future Claude sessions. Read `README.md` first for the
user-facing picture and `HOWTO.md` for operator guidance; this file
covers the non-obvious design decisions, domain vocabulary, and current
refactor state.

## Sub-docs (read when relevant)

- **[`docs/sources-app.md`](docs/sources-app.md)** — full URL map, gotchas,
  and inline-CRUD UX for the `/sources/` GUI. Read when touching anything
  under `sources/`.
- **[`docs/chart-app.md`](docs/chart-app.md)** — chart.html multi-source UI:
  `sourceMemory`, source-switch flow, natural-window block fetching. Read
  when touching `viz/` or chart JS.

## Repo state

The repo is **mid-rewrite** from a hard-coded Oracle single-file ETL
(`star.py`, removed) into a configurable multi-source app. See the
**Built** and **Backlog** lists below for the current state before making
structural changes.

**Built (high level — read git log / `ls` for specifics):**

- `core/` — framework-agnostic ETL engine (`source.py`, `schema.py`,
  `etl.py`, `query.py`, `cache.py`). Must not import Django.
- `starcubes/` — Django project with custom DB backend, login middleware.
- `sources/` — full datasource CRUD + ETL monitor GUI (see sub-doc).
- `viz/` — multi-source chart UI + JSON endpoints (see sub-doc).
- `migrations/` — numbered `000N_*.sql`, idempotent. The consolidated
  `starcubes_meta.sql` is used by `core/schema.py`.
- Management commands under `sources/management/commands/`:
  `etl_run`, `seed_example`, `query_multibar`, `create_indices`,
  `drop_indices`, `query_cache_clear`.

**Backlog (the known unfinished work):**

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
- **`datasource.schema_name` is unvalidated free text that is interpolated
  raw into SQL.** `sources/views.py` takes it straight from the form (both
  `source_new` and `source_save`), and ~78 call sites across `core/` and
  `sources/` build SQL with `f"{schema}.fact"` and friends — no quoting, no
  charset check, no CHECK constraint on the column. Two consequences. A name
  Postgres would fold (`Test`) is *created* folded by
  `core/schema.py:create_fact`'s `CREATE SCHEMA IF NOT EXISTS {schema}` but
  compared unfolded by every `information_schema` lookup, so the metadata and
  the disk disagree from then on: `create_dim_tables` re-creates tables it
  cannot see and fails with *already exists*, and `migrations/starcubes_meta.sql`
  — the one place in the repo that quotes correctly, with `format('%I')` — was
  the first thing to break outright. A name needing quotes at all (a hyphen, a
  space) breaks the raw interpolation everywhere with a syntax error. The fix
  is to **validate** on `^[a-z_][a-z0-9_]*$` at both write paths (plus a CHECK
  as backstop), which makes the quoting question moot, and explicitly *not* to
  convert the 78 sites to `%I`: they are consistently wrong and therefore
  work, so quoting them would break every installation whose schema is already
  sitting folded on disk. Validation has to skip unchanged values, or every
  existing row with a folded name becomes unsaveable.
- **Partition retirement.** `import_fact` now ensures partitions forward of
  the load window exist on every run (`core/schema.py:ensure_partitions_for`,
  see the design-decision note below) — the manual `create_future_partitions`
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

## Domain vocabulary

- **Fact table** — `<schema_name>.fact`, one row per transaction. `RANGE`
  partitions on the time column, `UNLOGGED`, BRIN on the time column.
  Partition granularity is configurable per datasource (`partition_period`
  ∈ `'day' | 'week' | 'month'`, default `'month'`; weekly partitions start
  on the ISO Monday). Dimension columns are `smallint` foreign keys.
- **Dimension table** — `<schema_name>.<dim>`: `index smallserial PRIMARY
  KEY, source_id text, name text, description text`. Max 32 767 rows per
  dim (signed `smallint` limit). The neutral `source_id` column replaces
  any source-specific key names.
- **Primary dimension** — when several fact columns share a dim table
  (e.g. `so_peer_1/2/3` and `src_peer` all reference `peer`), exactly one
  `dimension` row is `is_primary = true`. That one drives DDL, dim import,
  and the `read_dimensions` mapping; the rest only contribute their
  fact-column-to-dim-table link for the transform stage.
- **Inline dimension** — a dimension backed by a fixed CSV value set
  instead of a source DB lookup. `inline_values` text column on
  `starcubes_meta.dimension` holds `source_id,name[,description]` lines. When
  set (and `source_table` is null), `import_dimensions` populates the dim
  table from the CSV. `source_table` and `inline_values` are mutually
  exclusive.
- **Auto-dimension** — a `fact_column` whose `role = 'auto-dimension'`.
  Same smallint storage as a regular dim, same `dimension` metadata row
  (admin creates one with `dim_table` and `is_primary = true`), but the
  initial value set is unknown. The ETL parent process discovers new
  source values per chunk and INSERTs dim rows on the fly with
  `name = source_id` and `description = 'auto-discovered <UTC ts>'`. The
  32 767 ceiling still applies — use the GUI's cardinality estimate
  before promoting a column.
- **High watermark (HWM)** — `max(watermark_column)` in the source. The
  ETL loads `[last_successful_hwm - late_arrival_overlap_s, current_hwm)`
  on each run; only `kind='incremental'` runs advance it.
- **Late-arrival overlap** — `datasource.late_arrival_overlap_s`, the number
  of seconds the load window reaches back *past* the HWM on every run,
  because the source does not promise that rows become visible in watermark
  order. Rows already loaded are dropped by PK in the extract loop. Default
  0 (historical behaviour). See the design-decision note below.
- **Watermark lag / wall-clock lag** — `datasource.watermark_lag_s` and
  `wallclock_lag_s`. The window's *upper* bound is
  `max(max_wm − watermark_lag_s, src_now − wallclock_lag_s)`, the second
  term only when it is non-zero. Unlike the overlap this never re-fetches
  and never needs a PK check, and nothing is written behind the HWM — which
  is why `core/cache.py` subtracts the overlap but not these. `wallclock_lag_s`
  must exceed `watermark_lag_s` (CHECK `datasource_wallclock_lag_check`).
  Both default 0, which reproduces the pre-0023 window exactly. See the
  design-decision note below.
- **Backfill run** — `import_fact(..., backfill=True)` /
  `manage.py etl_run --backfill --start … --end …`. Re-loads one closed
  historical window, de-duping the whole window by PK, and is recorded with
  `etl_run.kind='backfill'` so it cannot move the HWM.
- **Dimension group** — a named bucket (e.g. "OK", "Error") collecting one
  or more dim rows via `dim_group_member`. Always belongs to exactly one
  group dimension.
- **Group dimension** — a row in `starcubes_meta.dim_group_set`: a *named*
  bucketing of one native dimension, with a user-given `name` and a derived
  URL `slug`. A dimension may carry arbitrarily many (e.g. `peer` bucketed
  both by country and by peer type), so the chart addresses one via the
  token `<fact_column>:grouped:<slug>`. The bare pre-0020 token
  `<fact_column>:grouped` still resolves — to the dimension's first set.
  A dim value may be assigned in several group dimensions at once, but only
  to one group *within* each. Created and edited in the `/sources/<id>/`
  Group Dims tab.
- **ETL forever session** — a background loop (`manage.py etl_run
  --forever`) that sleeps N seconds between incremental passes. Tracked
  in `starcubes_meta.etl_forever` with PID, `stop_requested` flag, and status
  (`running` / `stopping` / `stopped` / `failed`). At most one active
  session per datasource.
- **Measure** — a row in `starcubes_meta.measure`: the user-facing catalog of
  measurements in the chart's measure dropdown. `agg` is an aggregation
  expression — the literal `count(*)` or `func(col)` (func ∈ sum/avg/min/max/
  quantile, col a `role='measure'` fact column). `name` is the dropdown label;
  `graph_style` picks the `timeseries.js` plot type (`'multibar'`, `'multiline'`,
  `'multipoint'`, `'quantile-bands'`; the CHECK widens as types land).
  `percentiles` is a comma-separated ladder (`'25,50,75'` or `'5,25,50,75,95'`)
  used only by `quantile-bands` (see the quantile-bands design note below).
  One row may be `is_default`
  (partial unique index `ux_measure_default`). Distinct from
  `fact_column.role='measure'`, which stays the low-level "aggregatable +
  smallint-clamped" column marker. The dropdown maps the `count(*)` agg to an
  empty `measure=` param (so `core/query._parse_measure` never sees a bare
  `count(*)`); other aggs pass through. `plot_json` resolves `graph_style` via
  `Source.graph_style_for(agg)` → `plot['type']`. A source with **zero**
  measure rows falls back to the historical implicit `count(*)` + auto sum/
  avg/min/max list.
- **Default source** — `datasource.is_default` (partial unique index
  `ux_datasource_default`, at most one true). The chart's `loadSources()`
  picks the flagged source when no `?name=` is given, else first-by-name.
- **No-breakdown group-by** — `group_by` empty (`""`, the chart's `(none)`
  option) means no series split: `timeseries_multibar` emits one constant
  `'(all)'` series per slot. `_build_bar_where` drops the `gb_where` clause
  for an empty `group_by`, so drill/txn endpoints work on an `(all)` bar
  (drill then breaks down by every dimension, since none is excluded).

## Design decisions that are not obvious from reading the code

This is the highest-value section in this file — read it before changing
any of the systems it covers.

- **One Postgres schema per datasource** (`starcubes_<name>`), not one shared
  fact table. Sources have incompatible shapes; a shared fact would force
  a lowest-common-denominator model. Isolation is cheap.

- **Partition naming sorts chronologically as a plain string within one
  period.** `core/schema.py:list_fact_partitions` orders by `relname`;
  `create_future_partitions` parses the last name to find the next
  boundary. Three fixed-width zero-padded shapes:
    - month: `fact_yYYYYmMM`
    - week:  `fact_yIIIIwWW` (ISO year + ISO week)
    - day:   `fact_yYYYYmMMdDD`
  Mixing periods on a single source is not supported — the application
  layer rejects `partition_period` changes once any partition exists.

- **`_floor_to_period` / `_next_period` use naive midnight `datetime`s.**
  Adding `timedelta(days=1)` / `(days=7)` to a naive midnight is exact (no
  tzinfo, no DST drift) — the `partition_column` is read in source-DB
  time and stored as wall-clock. Month arithmetic uses `replace(month=...)`
  with explicit Dec→Jan handling.

- **`smallint` dimension keys** — the point of the whole compact-star model.
  2 bytes per fact row instead of the source's 4–20 byte key. The 32 767
  ceiling is a deliberate tradeoff. High-cardinality dims do not belong here.

- **`cncy_code` has role `dimension` with an inline CSV dimension.** It's a
  currency code stored raw as smallint; no source lookup table. The ETL
  only clamps `role='measure'` integer columns (`stay`, `total` — see
  `SMALLINT_MAX`/`BIGINT_MAX` in `core/etl.py`), so `cncy_code` passes
  through the transform stage mapped to dim indices, unclamped, via the
  `inline_values` field on the dimension row (ISO 4217 mapping, e.g.
  `978,EUR,Euro`). Do **not** reclassify it as `measure` — it would be
  clamped to `[0, 32767]` and both negative values and values above
  32 767 would be silently rewritten.

- **Integer measure columns must be non-negative and in range — the ETL
  enforces this by clamping, silently, not by failing the run.** A
  `role='measure'` fact column with `pg_type` `smallint` or `bigint` is
  assumed to be a count/sum/duration that is never legitimately negative.
  `core/etl.py:_transform_worker` clamps every such column to
  `[0, SMALLINT_MAX]` / `[0, BIGINT_MAX]` via pandas `.clip()` (`NaN`
  passes through unchanged) before the `COPY`. This replaced an
  upper-bound-only clamp after a production run failed with
  `NumericValueOutOfRange` on a negative `total` (an elapsed-duration
  `source_expr` that went negative on clock skew / out-of-order
  timestamps — see `sources/management/commands/seed_example.py`'s
  `TOTAL_EXPR` for the shape of expression that can do this). The clamp
  is silent by design (consistent with the pre-existing upper-bound
  truncation), which is a real tradeoff: a negative or overflowing
  `source_expr` result becomes indistinguishable from a legitimate `0`
  or type-max value instead of surfacing as a data-quality signal. Anyone
  adding a new integer measure column whose `source_expr` could go
  negative or overflow should know it will be clamped, not preserved or
  flagged.

- **Three-stage multiprocessing pipeline** — Extract (main proc,
  `pd.read_sql` chunked), Transform (N child procs, numpy vector
  `Series.replace` + CSV serialisation), Load (1 child proc, psycopg
  `copy_from` on a `raw_connection()`). The *only* way we hit the
  throughput numbers that motivated the design. Do **not** replace
  `copy_from` with SQLAlchemy `INSERT` — orders of magnitude slower.

- **Each load worker builds its own engine inside the child**, rather than
  inheriting the parent's SQLAlchemy pool via `fork()`. The parent calls
  `source_engine.dispose()` before the extract loop. The original
  implementation shared one engine post-fork; worked on Linux but was fragile.

- **HWM lives in `starcubes_meta.etl_run`**, not a per-schema `etl_log` table.
  Lets the GUI show run history across all datasources in one query and
  survives per-schema DDL changes.

- **Auto-dim discovery happens in the parent ETL process, not the workers.**
  Transform workers receive a snapshot `dim_frames` mapping at fork time;
  mutating across processes would require `mp.Manager` (slow per-row IPC)
  or per-worker DB inserts (race on `smallserial`). Instead,
  `core/etl.py:_resolve_auto_dim_chunk` runs in the parent between
  `pd.read_sql` chunk delivery and `transform_q.put(chunk)`. It finds
  `chunk[col].dropna().unique()` not in the local map, INSERTs each new
  value, registers it under str/int/float forms (same as
  `read_dimensions`), and rewrites the chunk column to indices. NaN →
  `-1` sentinel via `fillna(null_idx)`. Auto-dim columns are excluded
  from the `dim_fact_to_table` passed to workers — so the worker `map()`
  step doesn't re-process them. Concurrency assumption: one ETL process
  per datasource (enforced for forever-mode by `ux_etl_forever_active`).

- **`Source.dim_fact_to_table()` excludes auto-dim columns**; auto-dims
  live in `Source.auto_dim_fact_to_table()`. Workers must not see auto-dim
  columns in their mapping dict — the parent has already substituted
  source values with smallint indices, so a second `map()` would NaN them out.

- **`core/query.py:timeseries_drilldown` matches both `'dimension'` and
  `'auto-dimension'`** when iterating fact columns. Auto-dims are
  first-class for chart drill-down.

- **The load window reaches back past the HWM, because the source does not
  deliver rows in watermark order.** `import_fact` used to load
  `[last_end_time, max(watermark_column))` and advance the HWM to that max,
  so a transaction that only became visible *after* the run — with a
  watermark below the new HWM — dropped out of every future window and was
  lost silently and permanently. There is no unique constraint on `fact` to
  catch it and nothing in the run stats that would have shown it.
  `datasource.late_arrival_overlap_s` now pulls the window's lower bound
  back by N seconds on every incremental run; `_resume_state` returns
  `(last_end_time, extract_from, existing_pks)` where `extract_from =
  last_end_time - overlap`, and `_overlap_pks` pre-loads the PKs already in
  fact across that span so the extract loop drops them per chunk. The
  re-fetch is therefore idempotent: rerunning changes nothing.
  Three things this deliberately is *not*:
    - **Not a lag *on its own* — but the lag is now the primary instrument.**
      See the next note. The overlap alone loads the tip immediately and
      collects stragglers on the next pass; the lag holds the tip back and
      never re-fetches. They are complementary, and with a lag configured
      the overlap can usually go to 0, which removes the PK check entirely.
    - **Not a unique index + `ON CONFLICT`.** That is the airtight fix, and
      it costs the `copy_from` fast path (see the three-stage-pipeline note)
      plus a btree on an UNLOGGED billion-row table. The PK set is matched
      in the parent process instead, where it is free.
    - **Not on by default.** The right value is deployment-specific — it is
      *measured* with the Audit tab, not guessed — so the column defaults to
      0, which reproduces the old behaviour exactly.
  Two guards, both raised before the worker pool starts:
  `late_arrival_overlap_s > 0` without a `role='pk'` fact column fails the
  run (silently duplicating rows would be worse than the bug), and a PK set
  above `_MAX_OVERLAP_PKS` (5M, `STARCUBES_MAX_OVERLAP_PKS`) fails rather
  than OOM-killing the parent.

- **The window's upper bound is the later of two terms, and that is the
  primary late-arrival defence — the overlap is the second line.** The
  overlap costs a fact-side PK scan on every run (no BRIN, no partition
  pruning when `watermark_fact_expr` is derived), re-extracts rows only to
  drop them, and leaves the HWM dishonest — which is *why* `core/cache.py`
  has to shorten the cacheable horizon by it. Holding the *upper* bound
  back instead is idempotent by construction: the window advances
  monotonically, every watermark value falls in exactly one window,
  duplicates are impossible. `core/etl.py:import_fact` computes

      end_time = max(max_wm  - watermark_lag_s,
                     src_now - wallclock_lag_s)   # 2nd term only if > 0

  The two terms are two separate guarantees, not a value plus a trigger:
    - **`max(wm) − watermark_lag_s`** takes the data tip as proof of
      freshness and is therefore fast — but only advances while the tip
      moves. If source traffic stops, this term freezes and the last
      `watermark_lag_s` seconds would sit unloaded until traffic resumes.
    - **`src_now − wallclock_lag_s`** is an absolute deadline: every row
      gets that long in source wall-clock time to become visible.
      Independent of traffic, so it keeps the window moving when the first
      term freezes.
  Four things that are not obvious:
    - **The crossover is not configured, it falls out.** Setting the terms
      equal gives `now − max_wm = wallclock_lag_s − watermark_lag_s`: the
      wall clock takes over exactly once the standstill exceeds the
      difference of the two lags. No trigger, no threshold, no hysteresis —
      the formula is continuous and monotonic across runs. An earlier design
      with an explicit idle trigger plus a minimum-idle column needed three
      columns to express what these two express by construction.
    - **`wallclock_lag_s` must exceed `watermark_lag_s`** (CHECK
      `datasource_wallclock_lag_check`, pre-validated in
      `sources/views.py:_lag_pair` so the GUI gets a sentence rather than a
      500). Otherwise the wall-clock term would win even under running
      traffic, where `max_wm ≈ now`, and `watermark_lag_s` would have no
      effect at all.
    - **The result is clamped: `end_time` never exceeds `max(wm)`.** A hard
      invariant, not a refinement — if the HWM advances over a range the
      source has not produced yet, every row later arriving in that range is
      below the HWM and lost silently and permanently. An earlier revision
      argued the wall-clock term was "a named deadline and may therefore
      carry the HWM"; that assumed both values share a clock, and a
      production incident proved it wrong within a day. An Oracle source
      writing `end_time` in UTC while its session sat in `Europe/Berlin`
      skewed `src_now` by +7200s, so the wall-clock term won every run and
      pulled the HWM two hours past the data — after which every run loaded
      an empty future window while live rows fell below the watermark. The
      clamp costs nothing: under a real standstill the term still closes the
      window right up to `max(wm)`, which is all there is to load. It only
      stops it running off into empty time. When it fires, the run logs a
      warning naming the measured skew.
    - **`wallclock_lag_s = 0` drops the second term rather than zeroing
      it.** `max(max_wm, src_now)` would be `src_now` — not "no lag" but its
      opposite. With both columns 0 the window is computed exactly as it was
      before migration 0023.
  The wall clock is read from the *source* DB (`Source.source_now_sql()`,
  spelled from the existing `source_dialect`), because watermarks are source
  wall-clock time throughout; it rides along as a second column on the
  existing max-query, so it costs no extra round-trip except when
  `watermark_max_query` is set and therefore opaque.
  **The source connection's session timezone is irrelevant, and making it so
  was the whole fix.** `source_now_sql()` used to return `LOCALTIMESTAMP`,
  which reads the *session* clock rather than the content of the time
  columns. Every dialect has a direct UTC expression instead
  (`SYS_EXTRACT_UTC(SYSTIMESTAMP)`, `CURRENT_TIMESTAMP AT TIME ZONE 'UTC'`,
  `UTC_TIMESTAMP()`, `SYSUTCDATETIME()`), and using it removes the
  assumption rather than documenting it — which is also why there is no
  `source_timezone` config column: **naive source timestamps are already
  treated as UTC everywhere** (`parse_dates={pc: {"utc": True}}` in the
  extract, `_as_utc` in `core/audit.py`, `_parse_dt` in `core/utils.py`, and
  a dozen `replace(tzinfo=timezone.utc)` calls). `source_now_sql()` was the
  single place that broke that, and the cost was not theoretical: the first
  production source it was enabled on (Oracle, `end_time` in UTC, session in
  `Europe/Berlin`) came back +7203 s ahead, so the wall-clock term won every
  run, pulled the HWM two hours past the data, and left `watermark_lag_s`
  with no effect at all.
  What remains, and is *not* fixed by this, is a source whose time columns
  genuinely do not hold UTC. That cannot be detected — nothing tells you what
  clock the writer used, and "wall clock wins while rows flow" is also the
  legitimate sporadic-source case the term exists for. Two things cover it:
    - **The damage is bounded** by the `end_time > max_wm` clamp above. Such
      a source costs freshness, never data.
    - **The diagnosis is possible**: the window log line prints both
      candidates, which one won, and the measured `src_now − max_wm` skew,
      plus a warning whenever the clamp fires. Since `src_now` is now read
      as UTC, a skew parked near a whole-hour offset while rows keep
      arriving means the watermark column does not hold UTC — the response
      is `wallclock_lag_s = 0`. `watermark_lag_s` is unaffected either way,
      as it never touches a wall clock.
  A clock *behind* its own data is the one unambiguous case, and there the
  term is dropped before it is even considered, with a warning.
  Note also that `sources/management/commands/etl_run.py` loads the `Source`
  **once, before** the forever loop — so changing either column in the GUI
  does not affect a running session; it has to be restarted.
  The known residual risk: a hung feeder is indistinguishable from a genuine
  traffic pause — both just freeze `max(wm)` — so a backlog arriving with old
  watermarks after `wallclock_lag_s` has elapsed lands below the HWM.
  `wallclock_lag_s` *is* the knob for that: it is the maximum grace a row is
  granted. The overlap remains the net underneath.
  One guard survives from the overlap-only design and is still needed:
  `import_fact` clamps `end_time` up to `last_end_time` when it would fall
  below it. Both terms are normally monotonic, so this is not the common
  path — it catches upstream deletions lowering `max(wm)`, a
  `watermark_lag_s` raised at runtime, and a preceding `--start/--end` run
  that set a higher HWM. It has to *clamp* rather than return early: the
  final `UPDATE` writes `last_end_time = end_time` from the `finally` block,
  so a bare `return 0` would still write the withdrawn bound and rewind the
  watermark. Neither term applies on the `end_time_override` path
  (`--start/--end`, every backfill) — there the user owns the window.

- **`datasource.watermark_fact_expr` exists because the watermark is not
  always a projected fact column.** The example Oracle source watermarks on
  `end_time` but stores `create_time` plus a `total` duration, so finding
  the already-loaded rows of an overlap window means recomputing the
  watermark fact-side. `core/etl.py:_resume_state` used to hard-code
  literally `pc + total * interval '1 millisecond'` — the one place in the
  engine that assumed a non-configurable fact column, and it broke the crash
  -resume path outright for any datasource without a `total` column.
  `Source.watermark_fact_sql()` returns the configured expression or falls
  back to `partition_column`. `seed_example` sets it for the example source,
  which is what preserves that source's previous behaviour.

- **A backfill must never move the high watermark backwards.** `_resume_state`
  picks the newest `status='ok'` run by `finished_at`, so re-loading an old
  window would have rewound the watermark and skipped everything after it —
  a trap that already existed for a plain `--start`/`--end` run.
  `etl_run.kind` ('incremental' | 'backfill') is the fix; the HWM query
  filters on it. Backfill runs also skip `_increment_dim_counts` (their
  window overlaps rows an earlier run already counted) and call
  `bump_cache_epoch`, because they insert rows *behind* the HWM, inside
  ranges the plot cache already considers final. For the same reason
  `is_cacheable` now subtracts `late_arrival_overlap_s` from the cacheable
  horizon: an ordinary incremental run can insert into that span too, with
  no invalidation event to catch it.

- **The overlap de-dupe filter stays armed for the whole run.** The previous
  crash-resume filter disabled itself as soon as one full-`chunksize` chunk
  arrived clean — which silently assumes the source delivers rows in
  watermark order. `Source.import_query` carries no `ORDER BY`, so a
  re-fetched row can surface in any chunk; that assumption is the very bug
  the filter exists to paper over. `_increment_dim_counts` is still handed
  the *HWM*, not `extract_from`: counting from the overlap boundary would
  re-add the same rows on every pass. Late arrivals below the HWM therefore
  go uncounted — dim counts only drive the chart's stacking order, are
  approximate by contract, and `--refresh-counts` recomputes them.

- **The audit has three modes, because each one sees what the previous
  cannot.**
  `audit_window` compares row *counts* per bucket: cheap (both DBs
  aggregate, only totals cross the wire) and unbounded in window size, so it
  is the sweep that says *where* to look. But equal counts do not prove a
  bucket is intact — a window that lost 100 rows and had 100 deleted
  upstream nets to zero and reads as clean, and duplicate PKs in fact (there
  is no unique constraint to prevent them) are invisible to it entirely.
  `audit_ids` pulls both PK sets and takes the set difference, so it names
  the exact rows and separates the three cases: *missing* (in the source,
  never loaded), *surplus* (in fact, no longer in the source) and
  *duplicate* (same PK stored twice). The count mode stays the default entry
  point because the ID mode is bounded by RAM; every bucket row in the GUI
  carries an `IDs` button, deliberately including the ones the counts call
  clean.
  `audit_id_details` is the third: a *targeted* follow-up on ids `audit_ids`
  has already named — kept separate precisely so the set difference stays at
  the ~46 B/id that lets it run over a wide window. It fetches each row's
  source watermark (one `WHERE <pk_expr> IN (…)` per 1000 ids, with the time
  window kept in the predicate because `<pk_expr>` is an expression and only
  the watermark column is indexed) and pairs it with the `etl_run` whose
  window covered that watermark:

      needed_lag_s = etl_run.last_end_time − the transaction's watermark value

  `last_end_time` is exactly the upper bound of the WHERE that run read the
  source with (`import_fact` stores the same `end_time` it put in the
  clause; `first_end_time` is the lower bound), so the difference reads:
  *the window's upper edge had already moved this far past the transaction,
  and it still was not visible.* That is the measurement `watermark_lag_s`
  is sized from instead of guessed.
  **The single most important property is that both operands come from the
  source's own watermark column.** The obvious-looking `started_at −
  watermark` — which this first shipped as — subtracts two different clocks:
  the watermark is source wall-clock time that `_as_utc` only *labels* UTC,
  while `etl_run.started_at` is `datetime.now(timezone.utc)` on the
  starcubes host. Against a source writing in `Europe/Berlin` it is off by
  the full UTC offset and goes negative. Worse, the end-to-end test *hid*
  it by forcing `timezone=UTC` on both connections — a test that normalises
  away the very axis it is checking. `started_at` is still returned and
  shown, muted and labelled as the local clock, purely as "when did this
  actually run"; it must never re-enter the subtraction.
  Three consequences worth keeping straight:
    - **It cannot be negative**, since a run is only attributed when
      `txn_wm < last_end_time`. The clock-mixing version would have needed a
      sanity check; this one is correct by construction.
    - **It sizes an *increase*.** With lag `L_old` in force,
      `last_end_time = M − L_old`, so catching the row needs
      `L_new ≥ M − w = (last_end_time − w) + L_old`. In the normal case —
      measuring *before* setting a lag, `L_old = 0` — the difference is the
      target outright, which is why the GUI says "raise by", not "set to".
    - **The *last* covering run is used, not the first.** Every covering run
      missed the row, and the latest has the largest `last_end_time`, hence
      the tightest bound. With `late_arrival_overlap_s = 0` the windows do
      not overlap and there is only one anyway.
  It stays a **lower bound** — the row may have remained invisible far
  longer — and rows that fall in *no* run window are counted separately and
  kept out of the percentiles, since those are a hole in the run history, a
  different failure entirely. When the wall-clock term set the bound, the
  figure is still valid (it is what the WHERE compared against either way),
  it just no longer maps onto one specific knob; recording `max_wm` per run
  would disambiguate that at the price of a column, without making the
  figure itself any sounder.

- **Reading a column of ids costs ~46 B/id, not the 8 B an int64 suggests —
  and `Result.partitions()` does not stream.** Measured over 1M int64 PKs,
  peak RSS per id: `pd.read_sql` 264 B, buffered `.all()` 193 B,
  `stream_results` + `partitions()` 193 B, `stream_results` +
  `fetchmany()` 46 B, raw psycopg2 named cursor + `fetchmany` 31.5 B.
  `core/audit.py:_fetch_id_array` therefore puts `stream_results` on the
  *connection* and pulls batches with `fetchmany`, folding each batch
  straight into an int64 array via `np.fromiter` — one batch of driver
  tuples alive at a time, plus the array's own 8 B/id and a transient
  doubling at the final `concatenate`. The named cursor is faster still but
  is psycopg2-only, and the same function reads the (possibly Oracle) source
  side, where `stream_results` maps onto the driver's arraysize.
  `MAX_AUDIT_IDS` (2M/side, ~190 MB across both) is set from that measured
  46 B, not from the nominal 8. A Python `set` of the same ids measures
  73 B/id on its own, which is why this path uses arrays — while the ETL's
  overlap filter keeps its set, because there the access pattern is repeated
  per-chunk membership tests and a hash table earns its overhead.

- **`core/audit.py` counts the two sides differently on purpose.** Any
  *bounded* overlap can be exceeded, so the Audit tab compares source and
  fact row counts per time bucket — and is also how an operator measures
  what `late_arrival_overlap_s` should be in the first place. The fact side
  is one query bucketed server-side by integer division on the epoch; the
  source side is N plain `count(*)` range predicates on `watermark_column`,
  deliberately *not* `date_trunc`/`TRUNC`, which would drag the open
  multi-dialect item in the backlog into a new place. When
  `watermark_fact_expr` is derived, the fact-side count can use neither BRIN
  nor partition pruning and scans the range — hence the bucket cap (200) and
  the `statement_timeout`, since uWSGI only has 8 request slots.

- **Duplicate detection only runs when the last `etl_run` row has
  `status != 'ok'`.** A crashed run may have partially written a chunk;
  the next run reconstructs a conservative HWM from the fact table and
  PK-filters the first chunks until one arrives clean. Once a full chunk
  passes without overlaps, the dedupe set is cleared. See
  `core/etl.py:_resume_state`.

- **`small_window_hint`** is a per-source optional string inserted after
  the first `SELECT` when the load window is within a day. For the example
  Oracle source: `/*+ ORDERED INDEX(txn, TXN3) */` — Oracle-specific. Do not
  hard-code this into the engine.

- **ETL throughput is measured as net busy time per stage, not wall time.**
  Each of the three pipeline stages (extract in the parent, N transform
  workers, 1 load worker) writes `(rows, busy_s, wait_s)` per chunk into a
  shared `mp.Array('d', ...)`, `lock=False` — one writer per slot, so a
  slightly stale read is harmless, and it's ~2 orders of magnitude cheaper
  than an `mp.Manager()` proxy on the hot path (per-chunk, not per-row).
  `busy_s` excludes queue block time (`transform_q.put()` backpressure,
  `in_queue.get()` wait) so a slow downstream stage doesn't make an upstream
  stage look slow. Transform throughput uses `rows / max_i(busy_i)` — the
  stage is only as fast as its slowest worker, since all N run in parallel;
  per-worker rates still land in `stats.transform.procs`. A **progress-writer
  thread** in the parent (started *after* `w.start()` — forking with a live
  thread is risky) snapshots the array into `etl_run.progress_rows` /
  `progress_at` / `stats` every 5 s, because the parent can block for minutes
  in `transform_q.put()` and the GUI needs a live signal regardless.
  `progress_at` only advances when `rows_total` or the load stage's row
  count actually moved since the last tick — stamping it unconditionally
  (the original behaviour) made a fully wedged pipeline look perpetually
  alive, which is exactly how a dead load worker went unnoticed for
  hours. `stats` also carries `queues.{transform,load}` (current
  `qsize()`), `workers[].{name,alive,exitcode}`, and `processed_through`
  (an ISO timestamp: the running max of the `partition_column` value seen
  across extracted chunks so far), and a heartbeat log line
  (`STARCUBES_ETL_LOG_INTERVAL_S`, default 30s) prints the same
  numbers — signal that used to exist only in the (usually discarded)
  tqdm bar description. `processed_through` is deliberately not called
  "watermark" — that term already means `max(watermark_column)` in the
  *source* (see "High watermark (HWM)" below), a static per-run target,
  not a live in-progress position; `watermark_column` also isn't always
  even a projected fact column, so exposing that value would need new
  SELECT-projection plumbing. `partition_column` is already extracted and
  UTC-parsed per chunk (`parse_dates={pc: ...}`), so reusing it is free.
  Since `Source.import_query` has no `ORDER BY`, this is a best-effort
  "furthest point seen so far" (a running max, guarding against one
  out-of-order chunk making it jump backward), not a strict monotonic
  guarantee.

- **A killed ETL run must land as `status='failed'`, never `'ok'`.**
  `sources/management/commands/etl_run.py` registers `_abort_signal_handler`
  for SIGTERM/SIGINT only in single-run mode (forever mode keeps its own
  flag-based `_signal_handler`, which must not be overwritten); it raises
  `EtlAborted` (`core/etl.py`), which `import_fact`'s existing
  `except`/`finally` treats like any other run failure — reaps the workers
  via the join→terminate→kill escalation and writes the row. This is not
  cosmetic: `_resume_state`'s dedupe logic keys off "last run != ok", so a
  cleanly-terminated kill must look like an interrupted run to the next
  invocation, or the resume window would skip the PK-overlap check on data
  that was only partially loaded. The GUI's `etl_kill` view (`sources/
  views.py`) sends the SIGTERM and lets the parent write its own row; only
  on force-kill (`SIGKILL` via `os.killpg`, since `sources/etl_spawn.py`
  spawns with `start_new_session=True` making the parent the process group
  leader) does the view finish the row itself, because a `SIGKILL`ed parent
  can't.

- **A dead transform/load worker fails the run instead of deadlocking it.**
  `transform_q`/`load_q` are bounded (`maxsize=8`); before `EtlStageFailed`
  existed, a worker that raised or was killed left its queue undrained —
  the other stage's `put()` then filled *its* queue too, and the parent's
  own `transform_q.put(chunk)` blocked forever with no timeout anywhere in
  that path. `_transform_worker`/`_load_worker` (`core/etl.py`) now catch
  every exception, log the traceback, `abort_evt.set()`, and push
  `(stage, message, traceback)` onto an unbounded `error_q` before exiting.
  The parent never calls a bare `put()`/`join()` on either queue —
  `_put_with_abort`/`_join_with_abort` poll with a short timeout and call
  `_raise_if_failed` (checks `abort_evt` and every worker's `exitcode`)
  between attempts, raising `EtlStageFailed` (sibling of `EtlAborted`) into
  the existing `except`/`finally` machinery. A worker that dies before it
  can report (SIGKILL, OOM) still gets caught: `_raise_if_failed` falls
  back to `"<worker> exited with code N without reporting an error"` when
  `error_q` is empty but a worker's `exitcode` is non-`None`. The load
  worker specifically had **no** exception handling at all previously —
  `sqlalchemy.create_engine`/`raw_connection()` even sat outside its
  `try:` — so e.g. a `no partition of relation "fact" found for row` (see
  the partition pre-flight note below) killed it silently.

- **ETL logging goes to a per-run file, not `/dev/null`.**
  `sources/etl_spawn.py:spawn_etl_subprocess` used to detach stdin/stdout/
  stderr to `DEVNULL` — deliberately, to stop a uWSGI worker's SIGTERM/
  SIGHUP or log rotation from reaching the child (still true) — which also
  meant every `print()`/traceback from a spawned ETL vanished, and
  `podman logs` showed nothing about the ETL ever. It now opens
  `STARCUBES_LOG_DIR/etl-<slug>-<timestamp>-<rand>.log` with `O_APPEND`
  and points both fds at it (a regular file has the same SIGPIPE immunity
  a pipe/DEVNULL had, so the original fix survives), exports
  `STARCUBES_ETL_LOG_PATH` to the child, and forces `PYTHONUNBUFFERED=1` +
  a `-u` flag (the runtime image is `python3.12-slim-bookworm`; a
  redirected stdout is 8 KB block-buffered otherwise, so a killed child
  could lose its last, most diagnostic lines). `core/etl.py` uses
  `logging.getLogger(__name__)` — never `print()` on the hot/error paths
  — configured by `sources/etl_logging.py:configure_etl_logging()` at the
  one process entry point allowed to know about Django settings
  (`etl_run.py`/`etl_resume_forever.py`); `core/` itself stays
  Django-free. `STARCUBES_ETL_LOG_STDOUT=1` (set in the Dockerfile)
  additionally mirrors every log line onto the container's PID-1 stdout
  via `/proc/1/fd/1` so `podman logs`/`docker logs` show ETL activity —
  safe unlike the old raw-fd approach because it targets PID 1's own
  stdout (not a uWSGI worker's fd) and CPython leaves `SIGPIPE` as
  `SIG_IGN`, so a dead reader raises a catchable `BrokenPipeError`
  instead of killing the process. `etl_run.log_path`/`log_tail`
  (migration `0021`) record the file path on every run and the last ~8 KB
  on failure, so the GUI's ETL Log tab and history table work even
  without shell access to the log volume. `core/etl.py:_bound_log_size`
  `ftruncate`s the file in place once it exceeds
  `STARCUBES_LOG_MAX_BYTES` — safe specifically because the fd is
  `O_APPEND`, so the next write lands back at offset 0.

- **`import_fact` ensures partitions exist before starting the worker
  pool** (`core/schema.py:ensure_partitions_for`, called from
  `core/etl.py:import_fact` right after `first_end_time` is known). There
  is no DEFAULT partition (see the function's own docstring for the three
  reasons — string-sortable-name contract, ACCESS EXCLUSIVE lock
  contention with the ETL's own COPY, and silent misrouting), and nothing
  previously called `create_future_partitions` automatically — it was a
  manual CLI flag / GUI button. A source whose newest partition fell
  behind the calendar (e.g. `partition_period='month'`, last partition
  July, ETL run in August) made every COPY fail with `no partition of
  relation "fact" found for row`; combined with the load-worker silence
  above, this was invisible and permanent. Unlike
  `create_future_partitions(n)`, which anchors on `existing[-1]` and can
  only append, `ensure_partitions_for(start, end, back=1, forward=1)`
  **gap-fills**: it walks every period in `[start-back, end+forward]` and
  creates whichever ones are missing, even when a later partition already
  exists. `back=1` covers skew between `partition_column` and
  `watermark_column` when they're different fact columns; `forward=1`
  keeps one period of runway past `end_time` so the next run doesn't
  immediately need another partition. Runs on every `import_fact` call,
  including every forever-mode iteration, so partition coverage never
  depends on a cron job again. Capped at `_MAX_ENSURE_PARTITIONS = 512`
  partitions per call — a stale HWM combined with `partition_period='day'`
  would otherwise silently create thousands of tables; it raises instead.

- **Django tables land in `starcubes_meta`, not `public`.** The custom DB backend
  (`starcubes/db_backends.DatabaseWrapper`) sets `search_path=starcubes_meta,public`
  and ensures the schema exists on first connection. `auth_user`,
  `django_session`, `django_migrations`, etc. are created in `starcubes_meta`
  by `manage.py migrate`. The container entrypoint runs `migrate` before
  `collectstatic`. `ensure_starcubes_meta()` checks for the `datasource` table
  (not just the schema) so it still creates starcubes_meta application tables
  when the schema already exists. SQLAlchemy connections use
  schema-qualified names. `ensure_starcubes_meta(engine, force=True)` re-runs
  the consolidated `starcubes_meta.sql` even when `datasource` already
  exists — the *only* path that lets an existing container pick up a new
  idempotent `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` block added after
  first boot (e.g. `etl_run.log_path`/`log_tail`, migration `0021`), since
  the cheap `SourcesConfig.ready()` probe used on every request-serving
  process deliberately skips the 500-line script once the table exists.
  `sources/management/commands/bootstrap.py` calls it with `force=True`
  once per container start, *before* `call_command("migrate")` — and
  captures the return value into a local `freshly_bootstrapped`, not the
  module-level `core.schema._freshly_bootstrapped` global, because
  `migrate` immediately triggers `ready()` → a second, non-forced
  `ensure_starcubes_meta()` call that would otherwise overwrite the global
  back to `False` before the admin-user-creation check reads it.

- **`CSRF_TRUSTED_ORIGINS` must be set explicitly via `STARCUBES_CSRF_TRUSTED_ORIGINS`
  (comma-separated `scheme://host[:port]` entries) for any deployment behind
  the bundled nginx+uwsgi.** With `ALLOWED_HOSTS=["*"]` and nginx's catch-all
  `server_name _;` in `docker/nginx.conf`, Django's Origin check on POST
  (`request.get_host()`-derived "good origin" vs. the browser's `Origin`
  header) is fragile across the nginx→uwsgi hop and rejects logins with
  "Origin checking failed" even on a same-origin, same-scheme request. The
  explicit allowlist sidesteps the derivation entirely. Empty by default —
  must be set per deployment (e.g. `http://127.0.0.1:8080` for local docker
  access, plus any other hostname/IP used to reach it).

- **Keycloak/OIDC SSO configuration lives in `STARCUBES_OIDC_*` env vars,
  not a `starcubes_meta` table, even though every other piece of
  GUI-editable config in the app is DB-backed.** Two reasons, not one:
  first, there is no existing global/singleton config table anywhere in
  `starcubes_meta.sql` — every table is either per-datasource (FK'd to
  `datasource_id`) or per-user history (`query_history`), so a DB-backed
  version would introduce a wholly new kind of table for one feature.
  Second, and more binding: `mozilla-django-oidc` reads
  `OIDC_RP_CLIENT_ID`/`OIDC_OP_*_ENDPOINT`/etc. from `django.conf.settings`
  once at process start, not per-request from a DB — making it
  DB-configurable would mean abandoning the library for a hand-rolled OIDC
  client, which is exactly the kind of "engine must not import Django, and
  don't hand-roll what a library already does correctly" tradeoff this
  file elsewhere argues against paying without a forcing reason. Presence
  of all three required vars (`STARCUBES_OIDC_ISSUER`,
  `STARCUBES_OIDC_CLIENT_ID`, `STARCUBES_OIDC_CLIENT_SECRET`) *is* the
  on/off switch — no separate boolean — the same shape as
  `STARCUBES_CSRF_TRUSTED_ORIGINS` above being empty meaning "not
  configured". `starcubes/settings.py` derives all four Keycloak OP
  endpoints (`.../protocol/openid-connect/{auth,token,userinfo,certs}`)
  from the single `STARCUBES_OIDC_ISSUER` realm URL by string
  concatenation — no discovery HTTP call at Django boot, no dependency on
  Keycloak being reachable to start the app — mirroring
  `STARCUBES_POSTGRES_DSN`'s "one URL implies everything" shape rather
  than exposing four endpoint settings the operator could get out of sync.
  `OIDC_RP_SIGN_ALGO` is set explicitly to `RS256`: the library defaults
  to `HS256`, and Keycloak signs with `RS256` by default, so leaving it
  unset fails every login's token verification. `starcubes/urls.py` mounts
  `mozilla_django_oidc.urls` under `/oidc/` only when configured, so an
  unconfigured deployment 404s there rather than exposing a route that
  500s on missing settings; `RequireLoginMiddleware`'s
  `_PROTECTED_PREFIXES` (`/sources/`, `/admin/`) don't cover `/oidc/`, so
  no middleware change was needed. `starcubes/oidc.py:KeycloakOIDCBackend`
  overrides `get_username()` to key the Django username on Keycloak's
  `preferred_username` claim (needs `OIDC_RP_SCOPES = "openid email
  profile"` — the library's default scope list omits `profile`, and
  without it Keycloak may not send the claim at all) instead of the
  library's default email-hash username, and force-clears
  `is_superuser`/`is_staff` in `create_user()` so an auto-provisioned SSO
  account can never come back privileged — `sources/views.py:2246`'s
  `settings_page` is still the only place `is_superuser` is checked
  anywhere in the app, and it stays a manually-granted local flag for
  both password and SSO accounts alike. `filter_users_by_claims()` is
  deliberately left at the library default (matches existing users by
  email, case-insensitive): a local password account and a Keycloak
  identity sharing an email merge into one account on first SSO login,
  which is treated as intentional rather than worked around — the one
  sharp edge is a local *username* colliding with a `preferred_username`
  claim whose email differs, which hits Django's unique-username
  constraint on that identity's first SSO login instead of merging (a
  500, not a silent failure). Deliberately **not** set anywhere is
  `SECURE_PROXY_SSL_HEADER`: `mozilla_django_oidc.utils.absolutify()`
  builds the OIDC `redirect_uri` from `request.is_secure()`, so behind a
  TLS-terminating proxy Django needs to trust `X-Forwarded-Proto` or it
  computes `http://...` and Keycloak rejects the redirect URI as a
  mismatch — but that setting also flips CSRF cookie `Secure` flags
  app-wide and would be wrong for a non-proxied (e.g. local) deployment,
  so it's a documented manual step in `HOWTO.md` ("SSO login") rather than
  inferred, same posture as `STARCUBES_CSRF_TRUSTED_ORIGINS` itself.

- **Always build the container image via `docker/build.sh`, never
  `podman build`/`docker build` directly.** The wrapper exists for two
  reasons: (a) it defaults the container engine to `podman` (`CONTAINER_ENGINE`
  overrides), and (b) it pins the working directory to the repo root before
  delegating. There's no longer a submodule to initialize at build time —
  the JS dependency (`@hgruber/timeseries.js@~0.9.0`) is pinned via the
  top-level `package.json`/`package-lock.json` pair and pulled by the
  `jsbuilder` Dockerfile stage through `npm ci`.

- **Frontend JS dependencies live at the repo root.** Top-level
  `package.json`/`package-lock.json` declare the only frontend JS dep
  (`@hgruber/timeseries.js`, pinned `~0.9.0`). The Dockerfile's `jsbuilder`
  stage runs `npm ci --omit=dev` and copies `dist/timeseries.min.js` into
  `viz/static/timeseries/timeseries.js`, which `STATICFILES_DIRS` exposes to
  Django's `collectstatic`. `chart.html` loads it via
  `{% static 'timeseries/timeseries.js' %}`. No `node_modules` reaches the
  runtime stage — `.dockerignore` blocks it. To bump timeseries.js:
  `npm update @hgruber/timeseries.js` (or edit the version in `package.json`)
  and rebuild.

- **The starcubes logo/icon ships as light/dark asset pairs, not a single
  `currentColor` SVG.** `viz/static/viz/starcubes_icon.svg` /
  `starcubes_lockup.svg` (icon-only vs. icon+wordmark) are duotone with
  several fixed hex fills — unlike the pre-rebrand logo they replaced,
  which adapted to theme via a single `currentColor` fill. A blanket
  `currentColor` swap isn't possible without flattening the duotone design,
  so each has a `*_dark.svg` sibling: shape fills `#2B3A55`→`#7C8FC9`,
  `#46567A`→`#93A3D1`, `#5C6B87`→`#AAB7DC` (navy/slate tones lightened for
  contrast against the `#1a1a1a` dark background), the pink accent
  `#CF4F8F` unchanged. The lockup's "star" wordmark is a separate case —
  it needs to read as body text, not as a graphic element, so it doesn't
  follow the shape mapping: it's `#DDDDDD`, matching `--body-color` dark
  exactly (the lightened shape tone `#7C8FC9` tested too low-contrast for
  small nav-height text). Diff the light/dark SVG pairs directly if
  retuning. Two integration patterns follow from that split: the nav-brand
  logo (`sources/base.html`, `viz/templates/registration/login.html`,
  `viz/templates/viz/chart.html`) renders **both** `<img>` variants and
  toggles visibility via `[data-theme="dark"] .nav-brand-logo-{light,dark}`
  CSS (same attribute-selector pattern as `[data-theme="dark"] .errorlist`
  in `login.html`), since the app's theme lives in a `data-theme` attribute
  set from `localStorage`, not `prefers-color-scheme`. The chart watermark
  (`chart.html`, drawn to canvas by `timeseries.js`, no CSS access once
  drawn) instead picks the file server-side-templated-but-JS-branched: the
  `_isDark` check already used for `ts.setColors()` is hoisted before the
  `new TimeSeries({...})` call so `watermark:` can pick between the two
  `{% static %}` paths. Favicons use a third mechanism, native
  `<link rel="icon" media="(prefers-color-scheme: dark)">` pairs, because
  the tab icon reflects OS theme rather than the app's in-page toggle.

- **The Query console is read-only by four independent mechanisms, and
  cancellable across worker processes.** `core/adhoc.py` runs user SQL under
  (a) a connection with `-cdefault_transaction_read_only=on`, (b) an explicit
  `SET TRANSACTION READ ONLY`, (c) `validate_sql`'s single-statement +
  SELECT/WITH check, and (d) a named server-side cursor, which Postgres
  refuses over a data-modifying CTE — the one writing shape that starts with
  `WITH` and so slips past (c). Measured: the cursor does *not* refuse
  `SELECT 1; SELECT 2`, so (c) alone enforces "one statement", while (a)+(b)
  alone enforce "no writes" (SQLSTATE 25006). Cancellation cannot use an
  in-memory `{token: pid}` map because uWSGI runs `processes = 2` and the
  cancel request routinely lands in the other worker; instead the query tags
  its backend via `set_config('application_name', 'sq:<user_id>:<token>',
  true)` and the cancel endpoint finds it in `pg_stat_activity`.
  `set_config(..., is_local => true)` rather than `SET LOCAL` because `SET`
  takes no bind parameters — and its transaction-local scope is also what
  keeps the tag from leaking to the next user of the pooled connection. The
  cancel is issued on the *meta* engine, since the console pool may be
  saturated by the very queries being cancelled. User SQL goes through
  `exec_driver_sql`, never `text()`: `text()` would read the `:1` in
  `SELECT '{"a":1}'::jsonb` as a bind parameter. See `docs/chart-app.md`.

- **`query_history.user_id` has no foreign key to `auth_user`.** Not an
  oversight: `SourcesConfig.ready()` calls `ensure_starcubes_meta()` — which
  executes `migrations/starcubes_meta.sql` — and `ready()` fires *during* the
  `call_command("migrate")` in `sources/management/commands/bootstrap.py`,
  i.e. before Django's own migrations create `auth_user`. A hard FK would
  break every fresh install. Rows are cleaned up explicitly by the
  `delete_user` branch of `sources/views.py:settings_page` instead.

- **`core/` must not import Django.** The ETL engine has to be runnable
  from a cron job or a management command without the Django runtime
  loaded. `sources/` and `viz/` are thin Django adapters on top of `core/`.

- **`viz/views.py:_parse_dt` is also in `query_multibar` management command.**
  Duplication is deliberate — the management command is a standalone entry
  point that must not import from `viz/` (same constraint as `core/`).

- **SQLAlchemy engine cache prevents connection leaks.** Before the cache,
  each request called `create_engine()` which created a new pool (5 idle
  connections) that was never disposed. `core/source.py:_get_cached_engine`
  ensures one pool per (dsn, connect_args). Pool sizing: `pool_size=5`,
  `max_overflow=10`, `pool_pre_ping=True`, `pool_recycle=1800`,
  `pool_timeout=30`.

- **`scheduleViewportChange` in timeseries.js is a throttle, not a debounce.**
  A pure trailing-edge debounce starves under fast tick rates: at high zoom
  `mspp` can be < 300 ms, so `follower_tick` fires faster than the debounce
  window and resets the timer on every call — the callback never elapses.
  The throttle (skip if pending, fire latest values after 300 ms) guarantees
  `onViewportChange` fires at most once per 300 ms regardless of caller
  cadence. Single trigger for `currentViewport` updates, `autoFetch`, legend
  refresh, and DT input sync.

- **`follower_tick` and `follow_view` call `scheduleViewportChange()`.**
  They mutate `tmin`/`tmax` directly without going through the user-input
  paths that already call it. Without these calls, follow mode would never
  emit viewport-change events.

- **Stacked bars are sorted ascending by dimension count.** Each dim table
  has a `count` column (total fact rows per value, refreshed after each
  ETL). The multibar query uses `ORDER BY _g.count, slot` so series stack
  smallest-at-bottom. The count is approximate (last ETL refresh, not
  current viewport) but provides a globally consistent sort order across
  all fetched blocks. Pass-through columns (no dim table) fall back to
  alphabetical order.

- **The chart does not pin a render interval — the library cross-fades between
  resolution tiers.** `chart.html` used to call `ts.setRenderInterval(interval)`
  on every `autoFetch`, which makes `prepare_grid` draw exactly that tier and
  disables the dissolve. It now lets the library choose, and instead hands it
  the *same* threshold starcubes fetches on: `ts.setFadeBand(bar_pixel_target,
  bar_pixel_target / 2)` from `loadMeta` (plus `fadeHi`/`fadeLo` constructor
  options in `initTs` for the case where it runs first). The two rules must not
  drift — `bestInterval()` and `prepare_grid`'s tier pick are the same formula,
  and if they disagree one resolution is drawn while another is kept topped up,
  so panning holes out whatever is visible. `fadeTiers(ppms)` mirrors the
  library's choice locally so `autoFetch` fetches *both* tiers while both are
  on screen. Everything that reports a single number (`updateInfo`, the y-axis
  label, `updateLegend`) cuts at `_fade < 0.5`, the same cutoff the library's
  `get_element` hit test uses. See `docs/chart-app.md`.

- **The chart's y-axis is a density, not a per-bar count.** Counts scale with
  the bin, so mid cross-fade the two tiers' extents are 60× apart (measured:
  43× at one real viewport) and the axis has to travel between them — the bars
  breathe through what should be a plain resolution swap. `chart.html` therefore
  plots a rate via the library's `ts.setRateUnit(seconds, {label, transition})`:
  both tiers land on one scale (measured: 1.39×, which is only the real
  peak-vs-average difference) and the switch changes nothing but the printed
  numbers, dissolved over 300 ms. **Only additive measures may be scaled** —
  `isExtensiveMeasure()` accepts `count(*)`, `sum(…)` and the bare-column form
  (`core/query._parse_measure` reads that as `sum`); an `avg`/`min`/`max`/
  `quantile` is already per-unit and is pushed with `plot.extensive = false`.
  Dividing one of those by the bin length would silently be off by the tier
  ratio, the same class of mistake as reclassifying `cncy_code` as a measure.
  Tooltip, legend, drill-down and CSV keep absolute counts throughout. The
  `yAxisMode` setting (`auto` | `s`/`min`/`h`/`d` | `count`) lives in
  `localStorage` with a `?yaxis=` URL override — no server state, no migration.
  See `docs/chart-app.md`.

- **Follow-mode polling interval is `max(5s, interval/3)`.** A 60s bar
  interval polls every 20s; a 5s interval clamps to 5s. Only fires when the
  viewport is near "now" (tmax within 2× interval of `Date.now()`), so
  scrolling to historical data stops network traffic.

- **`ts.pan(dir)` uses local-time calendar snapping, not fixed-ms offsets.**
  DST spring-forward days have 23h, fall-back days 25h. All seven snap
  units use `Date` setters (`setHours`, `setDate`, `setMonth`,
  `setFullYear`) so a day pan crosses DST as a calendar day, not
  86 400 000 ms. Both viewport edges snap independently (floor tmin, ceil
  tmax), then shift by the same calendar-unit count — no overlap between
  successive pans.

- **Watermark is drawn after `background()`, before the data.**
  `background()` fills the plot area with opaque `plotBg`; drawing the
  watermark before would hide it entirely. After `background()` puts it
  above the grid stripes but behind the data series.

- **`dataCache` fetch cache is keyed by sorted URLSearchParams** (name,
  group_by, measure, from, to, interval, all filter params). Naturally
  per-source and per-param-set. `sourceMemory` is a separate in-memory
  per-source plot store (not a URL cache) used for instant canvas
  restoration on source switch.

- **Trailing-edge blocks bypass both caches.** A block is "trailing" if
  `to >= Date.now()/1000 - interval`. Such blocks are not stored in
  `dataCache` (server data is volatile near now) and not stored in
  `sourceMemory` (must always be re-fetched). `autoFetch` always re-fetches.

- **Server-side `plot_json` cache** (`starcubes_meta.query_cache`) is keyed
  on sha256 of `(datasource_id, group_by_raw, measure, interval_start_epoch,
  to_epoch, interval, filter_tree, series_label, cache_epoch)`. Eligibility
  (`core.cache.is_cacheable`): `measure` must be `count(*)` (None or the
  literal), `to` must be at a UTC interval boundary, `to <= now - interval`,
  and `to <= last successful HWM`. Bin aggregation in `timeseries_multibar`
  uses `extract(epoch from f.{pc})` — UTC-anchored regardless of session
  `TimeZone`. `chart.html` aligns fetch windows to natural calendar
  boundaries (see `docs/chart-app.md`) so cache keys collide across users.

- **Cache invalidation = `datasource.cache_epoch` bump.** The epoch is part
  of the cache key, so an UPDATE on the datasource row implicitly orphans
  every old entry without a DELETE. Bumped from six endpoints that touch
  historical fact rows: `etl_delete_from`, `etl_truncate_last`,
  `etl_truncate_partition`, `etl_drop_partition`, `etl_set_highwatermark`,
  `etl_clear_history`. Append-only ETL does **not** bump (cached data
  behind HWM stays correct). Orphans are cleaned by `manage.py
  query_cache_clear` or an LRU sweep on `last_hit_at`.

- **Grouped dimensions aggregate by raw dim index, then merge in Python.**
  When `group_by` names a group dimension, `timeseries_multibar`
  groups by the raw `smallint` fact column (`f.{col}::int`) and sorts by
  slot only — no CASE expression, no dim-table JOIN. After the query,
  Python maps each dim index to its group name via `index_to_group` and
  sums per-index counts into per-group counts per slot. Grouping on a
  2-byte integer is the cheapest possible aggregation; the Python merge
  is O(rows) with a single dict lookup per row.

- **Drill-down on grouped dimensions resolves group names to dim indices.**
  Under a group dimension the chart uses group names ("OK", "Error",
  "(other)") as series keys. Drill-down and transaction-list endpoints
  detect the grouped token, call `build_group_by_indices()` to map group
  names → dim indices, pass the result to `_build_bar_where`. Named
  groups use `f.{col} = ANY(:_gb_indices)`; "(other)" uses
  `f.{col} != ALL(:_gb_named_indices)`, whose index list is derived from
  `group_by_indices` itself so it stays scoped to the addressed set — a
  second group dimension on the same column has a different complement.
  Without this translation, the group name would be sent as `group_by_val`
  and treated as a `source_id` in a dim-table subquery, returning no rows.

- **The grouped token is parsed in exactly one place.** `<col>`,
  `<col>:grouped` and `<col>:grouped:<slug>` all arrive through
  `core/query.py:split_grouped_col()` → `resolve_group_set()`; `viz/views.py`
  wraps both in `_resolve_group_by()`. Before 0020 the `:grouped` suffix was
  hand-stripped in five Python and six JS spots, each with its own slicing
  style. The slug charset is `[a-z0-9-]` (`slugify_group_set`, with German
  umlaut transliteration) because the token travels through
  `_parse_filter_spec`, which splits filter params on `=` and value lists on
  `,`. Slugs are re-derived on rename — chart bookmarks and the per-source
  `localStorage` state break by design (the alternative, opaque numeric ids,
  would have made every shared chart URL unreadable). `chart.html`
  compensates for the *old* break only: `normalizeGbToken()` rewrites a bare
  `<col>:grouped` onto the column's first set.

- **The hover tooltip belongs to the library, not to starcubes.** starcubes used to
  build its own `#bar-tooltip` card; the same box existed twice more in the
  `timeseries.js` demos. It now ships with the library as
  `TimeSeries.attachTooltip(ts, …)` (opt-in — the library still creates no DOM
  unless asked), and `attachBarTooltip()` in `chart.html` passes only the two
  app-specific hooks: `labelFor` for `_resolveDimName()` and `colorFor` for
  `_seriesColor()`/`starcubesSeriesColors`. Theming needs no code — the library's
  `tooltip*` palette keys hold the former `--legend-*` values and re-apply on
  the `ts.setColors()` that `applyTheme()` already calls. Do **not** re-add a
  local tooltip; extend it through the library's `formatter`/`labelFor` options
  instead. `_barHeaderHtml()` and the `.btt-*` classes survive because the
  drill-modal header still uses them. See `docs/chart-app.md`.

- **A datasource's metadata legitimately precedes its DDL, and code that
  touches the warehouse has to survive that.** `sources/views.py:source_new`
  only INSERTs the `datasource` row (plus whatever `fact_column` / `dimension`
  rows the admin then configures); `create_fact` / `create_dim_tables` run
  later from a separate endpoint under `/sources/<id>/` — or never, for a
  source someone half-configured and left. So `schema_name` routinely names a
  schema that does not exist and `dimension.dim_table` a table that does not
  exist. This has bitten twice, in code written years apart, both times fatally
  out of proportion to the cause:
    - `migrations/starcubes_meta.sql`'s count-column retrofit ALTERed such a
      table, and since `ensure_starcubes_meta` runs the whole script in one
      transaction, that rolled back every later migration block *and* — via
      `bootstrap.py` step 0, ahead of `migrate`/`collectstatic` — kept the
      container from starting at all.
    - `viz/views.py:api_source_meta` SELECTed from such a table. The chart
      awaits it during init, so one unprovisioned datasource killed the whole
      page, including every healthy source on it — and because it sorts
      first by name it was picked by default.
  Both now test with `to_regclass` first (`viz/views.py:_existing_relations`
  for the Python side). **The test has to use the same quoting as the access it
  protects**: `api_source_meta` interpolates `f"{schema}.{table}"` unquoted, so
  its probe passes the name unquoted; the migration's `ALTER` uses `format('%I')`,
  so its probe quotes. Getting that backwards silently answers a different
  question — see the `schema_name` backlog entry for why the two halves of the
  codebase disagree about quoting in the first place.

- **Every draggable box over the canvas is one `.fpanel`, not three
  implementations.** The legend, the drill-down panel and the query console
  (and whatever lands next) share one CSS ruleset and one
  `makeFloatingPanel()` registration in `chart.html`; a new panel is two
  classes in the markup plus one call, and must not bring its own drag
  handler, clamp, z-index or Escape wiring. Four decisions live in that shared
  layer and are easy to re-break individually:
    - **No backdrop, and none of them is modal.** The drill-down used to sit
      inside a `#drill-overlay` that greyed the whole canvas and closed on a
      background click. A panel whose job is to explain one bar has to be
      readable *against* the chart, and here "background" is the canvas — so
      panning, zooming or picking the next bar threw its contents away.
    - **Stacking is the user's click order**, via one counter raised on a
      capture-phase `pointerdown` (so a click on a button *inside* a panel
      raises it too). The previous fixed ladder — legend 1000, console 1500,
      drill 3000 — meant the drill panel could never be put behind the legend.
    - **Panels may hang off the left, right and bottom edge but never above
      `top: 0`.** The header is the drag handle, so a panel dragged past the
      top edge could never be dragged back. `fpMoveTo()` is the only place the
      clamp is expressed.
    - **`dataset.fpMoved` marks a position as the user's**, which is what stops
      the self-placing panels from overriding it — the drill panel re-renders
      its body on every navigation step and used to re-centre itself each time,
      and the resize re-clamp skips unmoved panels so the legend keeps its
      `right` anchor to the plot area. See `docs/chart-app.md`.

- **Quantile-bands are a distinct plot type, not a multibar measure.** A
  measure with `graph_style='quantile-bands'` and `agg='quantile(col)'` routes
  `plot_json` to `core/query.timeseries_quantiles` (not `timeseries_multibar`).
  The plot's `data[slot][series]` is a **list** of `percentile_cont` values
  aligned to `plot['percentiles']` (the per-measure ladder from
  `Source.quantiles_for`), not a scalar. The `quantile-bands` renderer in the
  `timeseries.js` vendored bundle draws one line per percentile from each slot's
  **center** and shades the bands at fixed per-band alpha (darkest at the
  median). Because the per-slot value is an array, three spots in
  `timeseries.js` branch on the type: `prepare_grid` y-range (max/min array
  entry per slot), `pushData` block-concat min/max, and `get_element`
  hit-testing (skipped — no per-bar target, so no drill/tooltip). Grouped
  (`:grouped`) percentiles are computed **SQL-side** via a `CASE` mapping dim
  index → group name before `percentile_cont` — percentiles cannot be merged
  in Python the way counts are. Not server-cacheable (`is_cacheable` rejects
  non-`count(*)` measures). The vendored bundle lives in
  `viz/static/timeseries/timeseries.js` (rebuild via `./docker/build.sh`);
  Adjacent fetch blocks are stored as **separate** plot objects in `data[]`
  (only multibar/quantile-bands are concat-trimmed on *overlap*, not on plain
  adjacency). Connected renderers would otherwise leave a one-slot hole at each
  block margin, so `renderers.js:plotData` supports an opt-in `coalesce(plot)`
  key: active blocks of one type sharing the key are merged via `coalesceBlocks`
  (rebasing each block's slots onto the group's earliest `interval_start`, which
  is always a multiple of `interval`) into one synthetic plot drawn once.
  quantile-bands opts in with key `name|interval` (`name` is `"{source}:
  {group_by}"`), so only same-source/same-group-by/same-interval blocks merge.
  Multibar bars are discrete so it does not need this.

- **`interval_end` is the requested window end, not the data horizon —
  deliberately.** `core/query.py` reports both `interval_end` (the client's
  requested `to`) and `incomplete_until` (the last `status='ok'` ETL
  watermark, the HWM) and never caps the former by the latter. Capping it
  server-side looks like the obvious fix for the class of bug where a chart
  in follow mode shows a data gap the width of the ETL lag, and breaks three
  things instead: `pushData` in `timeseries.js` deliberately prefers
  `interval_end` to delete slots of a superseded older block, so an empty
  capped result would stop clearing stale bars past the HWM; `chart.html`'s
  non-live `covered` test in `fetchTier` would never be satisfied for any
  block past the HWM, refetching the whole viewport on every 300 ms
  follow-mode tick; and the client loses the ability to distinguish "no rows
  here (yet)" from "not loaded yet", which is the entire basis of the
  provisional-block logic below. The clamp belongs client-side, in
  `fetchRange`, per block, and only for **trailing** fetches — see
  `docs/chart-app.md`, "Provisional blocks and the ETL high-water mark".

- **`autoFetch` coalesces re-triggers instead of aborting the in-flight
  fetch; `AbortController.abort()` is reserved for genuine param/source
  changes.** Follow mode drives `autoFetch` every 300 ms
  (`scheduleViewportChange` in `timeseries.js` is a throttle, not a
  debounce). The original code called `activeController.abort()` on every
  single invocation, so on any backend slower than 300 ms the in-flight
  fetch was killed before it could ever complete — a permanent livelock, not
  just wasted requests. `autoFetch` is now a thin coalescing shell around
  `_autoFetchOnce` (its `run again if a call arrived while I was in flight`
  wrapper); `abort()` survives as `abortInFlight()`, called explicitly only
  from `resetAndFetch` and both branches of `setActiveSource`. Do **not**
  drop those explicit calls when refactoring: `fetchRange` stamps
  `_starcubesGroupBy`/`_starcubesMeasure` onto the response *after* its
  `await`, so an unaborted response landing after a switch would be
  mislabelled with the new params and painted onto the just-cleared canvas.

## Code conventions

- SQLAlchemy 2.x idioms: `text()`, `engine.begin() as conn`, `.mappings()`
  for row dicts.
- All per-source configuration comes from a `Source` dataclass loaded from
  `starcubes_meta`. Never hard-code dimensions, column lists, or SQL into a helper.
- Migrations numbered `000N_*.sql`, idempotent (`IF NOT EXISTS`, `OR REPLACE`).
- Django management commands live under `sources/management/commands/`.
  They import from `core/` but never the reverse. `core/` must not import
  Django. Use `manage.py <command>` for all CLI operations.

## Key files (most-touched, top of mind)

| File | Purpose |
|------|---------|
| `core/source.py` | `Source` / `Dimension` / `FactColumn` / `FactIndex` dataclasses + `Source.load` + `_get_cached_engine` + `init_oracle_client` + `watermark_fact_sql()` (fact-side watermark expression, falls back to `partition_column`) + `source_now_sql()` (the source's current time as naive **UTC**, per dialect, for the window's second upper-bound term — deliberately not the session clock). Splits `dim_fact_to_table()` (regular) from `auto_dim_fact_to_table()` (auto-dims, resolved in the parent ETL process). |
| `core/schema.py` | `create_fact`, `create_indices`, `drop_indices`, `import_dimensions`, `read_dimensions`, `ensure_starcubes_meta(engine, force=)`. Partition lifecycle: `list_fact_partitions`, `create_future_partitions` (append-only, manual), `ensure_partitions_for` (gap-filling, called every `import_fact` run), `vacuum_analyze_fact`. |
| `core/etl.py` | `import_fact` — the three-stage pipeline; calls `ensure_partitions_for` before starting workers. `_resume_state` returns `(last_end_time, extract_from, existing_pks)`; `_overlap_pks` loads the PK set for the late-arrival overlap. The window's upper bound is `max(max_wm − watermark_lag_s, src_now − wallclock_lag_s)`, clamped up to the HWM so it can never rewind. `backfill=True` re-loads a closed window without moving the HWM. `_load_auto_dim_maps` + `_resolve_auto_dim_chunk` for parent-process auto-dim discovery. `EtlAborted` for user-initiated kills; `EtlStageFailed` (+ `_put_with_abort`/`_join_with_abort`/`_raise_if_failed`) for a dead transform/load worker. Throughput metrics: `_snapshot_stats`/`_build_stats_payload` read the shared `mp.Array` into the `stats` jsonb shape (now including live `queues`/`workers`); `_rate`/`_fmt_rate` compute/format rows-per-second; `_bound_log_size` truncates the process's own log file in place. Uses `logging.getLogger(__name__)`, never `print()`. Maintenance: `truncate_last_partition`, `delete_fact_from`, `clear_etl_history`, `set_highwatermark`. |
| `core/query.py` | `timeseries_multibar` — multibar JSON producer. Bars ordered ascending by dim count. `split_grouped_col`/`resolve_group_set`/`slugify_group_set`/`grouped_token`/`grouped_label` own the `<col>:grouped:<slug>` token; `build_group_by_indices` resolves group names for grouped drill-down. |
| `core/audit.py` | `audit_window` — per-bucket source-vs-fact row-count reconciliation; `audit_ids` — PK set difference naming the exact missing/surplus/duplicate rows; `audit_id_details` — targeted second pass giving each named id its own watermark-column value, the `etl_run` that should have caught it, and `last_end_time − that value` as the lower bound on the needed lag (both operands source-clock, so no skew); `_fetch_id_array` — the streaming id reader (see the memory note); `gap_span` folds the gap buckets into one backfill window. No Django imports. |
| `core/cache.py` | Server-side `plot_json` cache helpers. `make_cache_key`, `is_cacheable`, `get_cached_plot`, `put_cached_plot`, `bump_cache_epoch`, `clear_cache`. No Django imports. |
| `core/utils.py` | `_parse_dt`, `_pid_alive`, `tail_file`, `format_pks` (applies `datasource.pk_format` to a list of raw PKs — shared by the chart drill-down and the Audit tab's id lists). No Django imports. |
| `core/adhoc.py` | Query console backend: `validate_sql` (single-statement SELECT/WITH lexer), `run_query` (read-only txn + server-side cursor + `application_name` tag), `cancel_query` (`pg_stat_activity` → `pg_cancel_backend`), `history_*`. No Django imports. |
| `migrations/starcubes_meta.sql` | Consolidated idempotent meta-schema (0001–0019 combined). Used by `core/schema.py`. Per-step migrations live in `migrations/000N_*.sql`. |
| `starcubes/settings.py` | Reads `STARCUBES_POSTGRES_DSN` from env. Custom DB backend, `CsrfViewMiddleware` in MIDDLEWARE. |
| `starcubes/db_backends.py` | Custom PG backend — ensures `starcubes_meta` schema exists, sets search_path. |
| `starcubes/middleware.py` | `RequireLoginMiddleware` — gates `/sources/` and `/admin/`. |
| `starcubes/oidc.py` | `KeycloakOIDCBackend` — optional SSO auth backend, active only when `STARCUBES_OIDC_*` env vars are set. Maps username from `preferred_username`, force-clears `is_superuser`/`is_staff` on auto-created users. |
| `viz/views.py` | Chart page, plot/drill/txn JSON endpoints (handle `:grouped`), source list + meta APIs. `plot_json` consults `core.cache` for cacheable `count(*)`. The four `query_*` console endpoints gate themselves with `_login_required_json` (403 JSON, not a 302) and answer user SQL errors with HTTP 200. |
| `viz/templates/viz/chart.html` | Multi-source chart UI — see `docs/chart-app.md`. |
| `sources/views.py` | Datasource CRUD + ETL JSON endpoints. Group dimensions live under `group-dims/` (list/detail/add/save/delete). Six historical-mutation endpoints call `bump_cache_epoch`. `etl_kill` sends SIGTERM/SIGKILL and cleans up a dead-PID or force-killed `etl_run` row (a live-SIGTERM'd parent writes its own row via `EtlAborted`). `etl_logs`/`etl_log_tail` serve the per-run log files under `etl/logs/`/`etl/log/`. See `docs/sources-app.md`. |
| `sources/etl_spawn.py` | `spawn_etl_subprocess(cmd, env=, datasource_name=)` — returns `(Popen, log_path \| None)`. Opens a per-run log file under `STARCUBES_LOG_DIR` and points the child's stdout+stderr at it (instead of the historical `DEVNULL`); `prune_etl_logs`/`slugify_ds`/`log_dir` are its helpers. |
| `sources/etl_logging.py` | `configure_etl_logging()` — the one place allowed to wire Django settings into `core/etl.py`'s `logging.getLogger`. Stderr handler always; optional PID-1-stdout mirror when `STARCUBES_ETL_LOG_STDOUT` is set (for `podman logs`); registers `faulthandler` on `SIGUSR1`. |
| `sources/templates/sources/detail.html` | 10-tab detail page (Basics, Fact Columns, Dimensions, Dim Data, Group Dims, Measures, Indices, Storage, ETL Commands, ETL History, ETL Log — nav and panel order are two independent lists in the template, kept in sync manually, not driven by one JS array) with inline CRUD + ETL controls, including an ETL Log tab that tails the per-run log file. `fmtRate`/`fmtCount` format throughput in the statusbar and ETL History table; for a `running` row the statusbar now reads live per-stage rates + queue depths + `processed_through` from `stats` (the stored `rows_per_s` columns are NULL until the run finishes) and shows a stall badge when `progress_at` goes stale; `etlKillBtn` escalates SIGTERM → SIGKILL across two clicks. |
| `examples.sql` | Reference SQL. Lines 43–62 are the template for `core/query.timeseries_multibar`. |
| `docker/build.sh` | The only supported way to build the container image — defaults `CONTAINER_ENGINE=podman` and delegates to the build engine. The Dockerfile's `jsbuilder` stage pulls `@hgruber/timeseries.js` from npm via `npm ci`. See the design-decision note above. |

Use `ls`, `grep`, and `git log` for anything not listed — don't rely on
this table as a manifest.

## Verification before reporting work done

1. **Syntax / import** — every Python file in `core/` and
   `sources/management/commands/` must `ast.parse` cleanly. Imports
   additionally require `sqlalchemy`, `pandas`, `tqdm`.
   `ast.parse` alone is not enough after a refactor that moves code between
   functions: it accepts a name that no longer has a binding in its new
   scope, and the `except Exception` in `viz/views.py` turns the resulting
   `NameError` into an HTTP 500 the chart only shows in its status line.
   Run an undefined-name check too — `ruff check --select F821 core/ viz/
   sources/` or `python -m pyflakes` — whenever a function's locals were
   extracted into a helper.
2. **SQL** — `psql -f migrations/0001_starcubes_meta.sql` must be idempotent.
   Re-running is part of the contract; verify by running twice.
3. **Seed round-trip** — `manage.py seed_example --force --skip-initial-ddl`
   followed by `Source.load(meta, dsn, 'source')` must return a `Source`
   whose `fact_columns` / `dimensions` / `indices` match the seed tuples.
4. **ETL parity** — when the first real ETL runs, compare row counts for
   the same `[last_end_time, end_time)` window. Throughput should be
   within 10% — the copy path is unchanged.
