# ETL engine — pipeline deep reference

Everything that runs from `core/etl.py:import_fact` (and the surrounding
scaffolding — `sources/etl_logging.py`, `sources/etl_spawn.py`,
`sources/management/commands/etl_run.py`).

**Highest-value section for the ETL** — read before changing any of the
systems it covers. The window-bound math and the worker-failure
handling in particular are subtle and have been the source of multiple
production incidents.

## Pipeline architecture

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

- **SQLAlchemy engine cache prevents connection leaks.** Before the cache,
  each request called `create_engine()` which created a new pool (5 idle
  connections) that was never disposed. `core/source.py:_get_cached_engine`
  ensures one pool per (dsn, connect_args). Pool sizing: `pool_size=5`,
  `max_overflow=10`, `pool_pre_ping=True`, `pool_recycle=1800`,
  `pool_timeout=30`.

## Before the load — preflight and the datasource lock

- **`import_fact` checks its assumptions before acting on them.** The first
  thing inside the `try:` block is `core/preflight.py:run_preflight` — schema
  objects, metadata version, index/FK coverage, the source projection, and
  the stored watermark against the newest fact row. It runs *before*
  `import_dimensions`, which already assumes the dim tables exist, and
  therefore well before `ensure_partitions_for`. The `etl_run` row is already
  written at that point, so a blocking finding lands as `status='failed'`
  with the reason in `error` through the existing `except`/`finally` — no new
  failure path. The whole report is merged into `etl_run.stats` under
  `preflight`, including for a run that never started a worker.
  `docs/etl-automation.md` has the per-check reasoning and the rule the
  module follows: **detect and block; repair only what is idempotent and
  finishes in milliseconds.** Missing tables are created (loudly), missing
  indices never are.

- **One process at a time owns a datasource, and the lock needs its own
  connection.** `acquire_datasource_lock` takes `pg_try_advisory_lock` keyed
  on the datasource id, before the `etl_run` INSERT, and releases it in the
  `finally` after the terminal UPDATE. A refusal raises `DatasourceLocked`
  and writes **no** run row — a refusal is not a run, and in forever mode a
  failed row per iteration would bury the history.

  The connection is the point. Nothing else in `import_fact` holds one across
  the run: every touch is a short `engine.begin()` and the extract loop
  disposes the source pool outright. A session-scoped lock taken on a pooled
  connection would be silently worthless — `_install_fork_safety` invalidates
  records across pids, `pool_recycle=1800` drops the session after thirty
  minutes, and a `pool_pre_ping` reconnect would carry on lockless without a
  word. So the lock opens a dedicated `NullPool` connection and keeps exactly
  that one, committing straight after acquisition (a session lock survives
  the commit; an open transaction would pin the snapshot for hours).

  Backfill serialises on the same key rather than getting its own — both
  paths write the same dim tables, and auto-dim resolution assumes single
  occupancy. `delete_fact_from`, `truncate_partition`, `drop_partition` and
  `truncate_last_partition` take the same lock briefly, which is what closes
  the hole `_etl_busy_response` never covered: no maintenance view has ever
  called it.

- **A run can bound its own time, and the source statements inside it.**
  `datasource.max_run_s` (off by default) gives the parent a wall-clock
  budget: past it, `_RunBudget.check` raises `EtlAborted`, which the existing
  reaping and `status='failed'` machinery already handles. It is checked
  between chunks and inside `_raise_if_failed`, which both bounded queue
  helpers poll through — so a run wedged on backpressure gives up as
  reliably as one that is merely slow.

  What a deadline in the parent *cannot* catch is a source that accepts the
  connection and then never answers: the parent is blocked inside the DBAPI
  at that point. `pool_pre_ping` and `pool_timeout` bound the checkout, not
  the execution. So the same value becomes a per-dialect statement timeout on
  a separate source engine (`Source.source_engine(statement_timeout_ms=)` →
  `_bounded_source_engine`): a libpq startup option for Postgres, a connect-
  event hook setting `call_timeout` for Oracle, and a warning that statements
  are unbounded for a dialect with no equivalent. One knob rather than two —
  a statement may not outlive the run it belongs to.

  Three run shapes are exempt, all legitimately long: a backfill, an explicit
  `--start/--end` window, and the initial load (N17). The last is decided by
  `_fact_has_rows`, the same probe `_resume_state` uses.

  It is a circuit breaker, not a repair. An aborted run leaves the watermark
  where it was, so the next window starts in the same place and ends later —
  it is *wider*, not smaller. What it buys is that one wedged run stops
  holding the forever loop, and lands as a failed row with a reason in it
  instead of a process that merely looks alive.

- **A catching-up load walks forward instead of leaping.**
  `datasource.initial_window_s` caps the width of each window while the
  datasource is behind. Without it, an empty fact table means the first
  window is "everything the source has" — literally, with no lower bound in
  the WHERE clause — and the forever loop does nothing else for its whole
  duration. Off by default.

  Two details that are easy to get wrong. The cap applies to every step of
  the catch-up, not only the first run, or step two loads the entire
  remainder. And it measures from the high watermark rather than from
  `extract_from`: the late-arrival overlap re-reads already-loaded ground,
  so capping from there would shrink each window by the overlap, and with a
  cap smaller than the overlap the resulting `end_time` would fall below the
  watermark and be pushed straight back to the tip by the monotonicity
  clamp. Stepping the first load also needs a lower bound, so the engine
  asks the source for `min(watermark_column)` — the one question it never
  had to ask before.

- **`import_dimensions` can be throttled.**
  `datasource.dim_refresh_interval_s`, default 0 (every run, as before).
  Unlike the housekeeping interval this default is not timidity: throttling
  costs freshness, because a new value in a *regular* dimension maps to
  `__unknown__` until the next refresh. Auto-dimensions are unaffected.
  `--import-dims` and the GUI button always import and reset the clock.

- **Every run records the data tip and the source's own clock.**
  `etl_run.max_wm` and `etl_run.src_now`, read together on the max-watermark
  query. `src_now` is read on *every* run now, not only when
  `wallclock_lag_s` is set — it rides along as a second column on the two
  generated query shapes, so it is free there, and only a hand-written
  `watermark_max_query` pays an extra trivial round trip. The window
  arithmetic is unchanged: the wall-clock term is still gated on the lag
  being configured.

  Their difference is the only thing in the system that can see a source
  which is up and has stopped producing — a standstill freezes the HWM, so
  the alarm evaluator's window stops sliding and can never fire. The
  preflight compares it against `datasource.max_data_lag_s`; the ETL History
  table shows it per run. A *negative* value is not a small lag but a clock
  behind its own data, which means the watermark column does not hold UTC.

- **A delete now tells the watermark, and which thing it tells depends on
  what kind of delete it was.** `delete_fact_from` is a *repair*: it rewinds
  the high watermark to the delete boundary by inserting a new `etl_run` row
  (never updating an old one — `_resume_state` orders by `finished_at DESC`,
  so a fresh row wins even carrying a lower `last_end_time`). The partition
  gestures are *disposal*: they leave the watermark alone and raise
  `datasource.retention_floor`, which `core/audit.py` clamps its windows to.
  A rewind below the floor is refused. All four bump the cache epoch.

  A rewind reloads a window and there is no unique index on `fact`. While
  `partition_column` and `watermark_fact_sql()` are the same column that is
  harmless — the delete removed exactly what comes back. When they differ,
  `datasource.dedupe_from` arms `_overlap_pks` for the reloaded span even
  with `late_arrival_overlap_s` at 0, and the run that honours it clears the
  flag. `import_fact` reads that column fresh from the metadata rather than
  off the `Source`, because a forever session loads its `Source` once, before
  the loop.

## Window bound — the late-arrival defence

The load window's lower *and* upper bound are non-trivial. The two
following notes are complementary: the **lower** bound reaches back past
the HWM (to catch stragglers via re-fetch), the **upper** bound is held
back so the window advances monotonically. With a lag configured, the
overlap can usually go to 0, which removes the PK check entirely.

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
      *measured* with the Audit sub-tab, not guessed — so the column defaults to
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

## Window bound — supporting machinery

- **Both bounds go into the extract as bind parameters, normalised to naive
  UTC.** `:_sc_from` / `:_sc_until`, through
  `pd.read_sql(text(query + where), params=…)`. They used to be rendered
  into `to_timestamp('…', 'YYYY-MM-DD HH24:MI:SS')` literals, which is
  Oracle spelling — MySQL and MSSQL have no such function, and Postgres has
  one that returns *timestamptz* parsed in the session timezone.

  The normalisation is the part that actually carries the correctness, and
  it is not obvious: the two bounds reach this point from *different
  connections*. The lower one is the stored high watermark, read back from
  `starcubes_meta.etl_run.last_end_time` (a `timestamptz`, so psycopg2
  returns it in the **warehouse** session zone); the upper one is
  `max(watermark_column)` off the **source**. Stripping the offset without
  converting first — `.replace(tzinfo=None)` — renders the same instant two
  different ways depending on which connection produced it, and the run log
  shows it as a window whose two ends carry different offsets. `_as_utc`
  warns about exactly this in its docstring; `_as_naive_utc` is what the
  bound values go through.

  Naive rather than aware because source timestamps hold UTC and are naive
  throughout (see `Source.source_now_sql` above). A source whose watermark
  column is `timestamptz` therefore does *not* work — see
  [`backlog.md`](backlog.md).

  What stays dialect-dispatched, because a bind carries a value and never a
  syntax element: `FROM dual`, row caps (`ROWNUM` / `TOP` / `LIMIT`),
  interval arithmetic, and `source_now_sql()` itself.

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

## Operational behaviour

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
  *source* (see "High watermark (HWM)" in
  [`docs/star-schema.md`](star-schema.md)), a static per-run target,
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
  on failure, so the GUI's ETL Log sub-tab and history table work even
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