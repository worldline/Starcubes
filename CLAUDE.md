# Claude agent notes for starcubes

Context for future Claude sessions. Read `README.md` first for the
user-facing picture and `HOWTO.md` for operator guidance; this file is
the index into the agent-/developer-facing deep references.

## Git workflow

Linear, single-developer project. There is no remote and no `main`
branch — **`master` is the only long-lived branch.**

- **Every commit lands on `master`.** No merge commits, no PRs, no
  long-lived feature branches. `git log --graph` must stay a straight
  line.
- **Don't create a worktree unless the user asks for one.** Background
  jobs may edit the working copy directly — `.claude/settings.json` sets
  `worktree.bgIsolation: "none"`, which lifts the harness's default
  requirement to call `EnterWorktree` before the first edit.
- **A worktree is disposable scratch space.** Once the work in it is
  built and tested, fast-forward `master` onto it and delete both the
  worktree and its branch:

  ```
  git merge --ff-only <branch>              # never a merge commit
  git worktree remove .claude/worktrees/<name>
  git branch -d <branch>                    # -d, not -D: it refuses if anything is unmerged
  ```

  If the fast-forward is refused because `master` moved on, rebase the
  branch onto `master` first — never merge.
- **Never leave a branch behind.** A stale branch invites the next agent
  to keep working there instead of on `master`.

## Sub-docs (read when relevant)

- **[`docs/sources-app.md`](docs/sources-app.md)** — full URL map, gotchas,
  and inline-CRUD UX for the `/sources/` GUI. Read when touching anything
  under `sources/`.
- **[`docs/chart-app.md`](docs/chart-app.md)** — chart.html multi-source UI:
  `sourceMemory`, source-switch flow, natural-window block fetching,
  floating panels, query console. Read when touching `viz/` or chart JS.
- **[`docs/etl-engine.md`](docs/etl-engine.md)** — three-stage pipeline,
  the two-term window upper bound, late-arrival overlap, throughput
  metrics, ETL logging, partition pre-flight, killed-run and
  dead-worker handling, SQLAlchemy engine cache. Read when editing
  `core/etl.py`, `sources/etl_logging.py`, `sources/etl_spawn.py`, or
  `sources/management/commands/etl_run.py`.
- **[`docs/etl-audit.md`](docs/etl-audit.md)** — the three audit modes
  (count, id set-diff, id details), the ~46 B/id memory bound, the
  two-sided counting strategy. Read when editing `core/audit.py` or the
  Audit tab in `sources/templates/sources/manage.html` (formerly
  `detail.html`).
- **[`docs/star-schema.md`](docs/star-schema.md)** — domain vocabulary
  (fact table, dimension, HWM, overlap, watermark_lag, measure, …) and
  the schema-related design decisions (per-source schema, partition
  naming, smallint keys, cncy_code inline, integer clamping). Read when
  editing `core/schema.py`, `core/source.py` (dataclasses), or
  adding/removing dimensions or measures.
- **[`docs/deployment-config.md`](docs/deployment-config.md)** — Django
  tables in `starcubes_meta`, CSRF trusted origins, Keycloak/OIDC SSO
  env vars, `docker/build.sh`, `requirements.txt` pinning, frontend JS
  deps, light/dark logo asset pairs. Read when editing `Dockerfile`,
  `docker/build.sh`, `starcubes/settings.py`, `starcubes/oidc.py`,
  `requirements.txt`, or `package.json`.
- **[`docs/chart-rendering.md`](docs/chart-rendering.md)** — ladder plot
  types (`quantile-bands`, `error-bars`, `candlestick`, …), partial-bin
  drawing via `setPartialBins`, `interval_end` vs `incomplete_until`,
  `autoFetch` coalescing, init zoom duration `0`. Read when editing
  the plot-related code paths in `viz/templates/viz/chart.html` or
  `core/query.py`.
- **[`docs/backlog.md`](docs/backlog.md)** — known unfinished work:
  multi-dialect WHERE, test coverage, late-arrival audit scheduling,
  targeted backfill, source-timezone support, schema-name validation,
  partition retirement, JSON auth, engine circuit breaker, read-only
  role, console concurrency, alarm notification transports. Read when
  planning new work or assessing scope.
- **[`docs/source-profiles.md`](docs/source-profiles.md)** — the YAML
  schema for `profiles/{examples,user}/<name>.yaml`, the catalogue
  page (`/sources/profiles/`), and the save / download / upload / apply
  flow. Read when touching `core/profile.py`, `sources/profile_store.py`,
  `profiles/`, `seed_example.py`, or the profile UI in `new.html` /
  `profiles.html` / `manage.html`.
- **[`docs/alarm.md`](docs/alarm.md)** — alarm evaluation: the
  `context_filter` jsonb shape (the chart's flat filter rows, grouped
  tokens forbidden), the `eval_end` cursor model and its late-arrival
  trade-off, the `alarm_firing` episode state machine, the global
  `alarm_forever` loop. Read when editing `core/alarm.py`,
  `sources/management/commands/alarm_run.py`, or the alarm UI in
  `detail.html` / `chart.html`.

## Repo state

The repo is **mid-rewrite** from a hard-coded Oracle single-file ETL
(`star.py`, removed) into a configurable multi-source app.

**Built (high level — read git log / `ls` for specifics):**

- `core/` — framework-agnostic ETL engine (`source.py`, `schema.py`,
  `etl.py`, `query.py`, `cache.py`, `audit.py`, `adhoc.py`, `utils.py`,
  `profile.py`, `alarm.py`). Must not import Django.
- `starcubes/` — Django project with custom DB backend package
  (`db_backends/`), login middleware, optional OIDC SSO backend.
- `sources/` — full datasource CRUD + ETL monitor GUI (see sub-doc).
- `viz/` — multi-source chart UI + JSON endpoints (see sub-doc).
- `migrations/` — numbered `000N_*.sql`, idempotent. The consolidated
  `starcubes_meta.sql` is used by `core/schema.py`.
- Management commands under `sources/management/commands/`:
  `etl_run`, `etl_resume_forever`, `alarm_run`, `alarm_resume_forever`,
  `seed_example`, `query_multibar`, `create_indices`, `drop_indices`,
  `query_cache_clear`, `bootstrap`.

**Backlog:** see [`docs/backlog.md`](docs/backlog.md).

`sources/` and `viz/` are thin Django adapters on top of `core/`.
`viz/views.py:_parse_dt` is also in `query_multibar` — duplication is
deliberate (the management command is a standalone entry point that
must not import from `viz/`, same constraint as `core/`).

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
| `core/source.py` | `Source` / `Dimension` / `FactColumn` / `FactIndex` dataclasses + `Source.load` + `_get_cached_engine` + `init_oracle_client` + `watermark_fact_sql()` (fact-side watermark expression, falls back to `partition_column`) + `source_now_sql()` (the source's current time as naive **UTC**, per dialect, for the window's second upper-bound term — deliberately not the session clock). Splits `dim_fact_to_table()` (regular) from `auto_dim_fact_to_table()` (auto-dims, resolved in the parent ETL process). See [`docs/etl-engine.md`](docs/etl-engine.md). |
| `core/schema.py` | `create_fact`, `create_indices`, `drop_indices`, `import_dimensions`, `read_dimensions`, `ensure_starcubes_meta(engine, force=)`. `validate_schema_name` + `ensure_schema` — the schema name goes unquoted into ~70 f-string SQL sites, so it is constrained to `^[a-z_][a-z0-9_]{0,62}$` rather than quoted (see [`docs/star-schema.md`](docs/star-schema.md)); `ensure_schema` is called by `create_fact`, `create_dim_tables` and `import_dimensions` alike. Partition lifecycle: `list_fact_partitions`, `create_future_partitions` (append-only, manual), `ensure_partitions_for` (gap-filling, called every `import_fact` run), `vacuum_analyze_fact`. See [`docs/star-schema.md`](docs/star-schema.md). |
| `core/etl.py` | `import_fact` — the three-stage pipeline; calls `ensure_partitions_for` before starting workers. `_resume_state` returns `(last_end_time, extract_from, existing_pks)`; `_overlap_pks` loads the PK set for the late-arrival overlap. The window's upper bound is `max(max_wm − watermark_lag_s, src_now − wallclock_lag_s)`, clamped up to the HWM so it can never rewind. `backfill=True` re-loads a closed window without moving the HWM. `_load_auto_dim_maps` + `_resolve_auto_dim_chunk` for parent-process auto-dim discovery. `EtlAborted` for user-initiated kills; `EtlStageFailed` (+ `_put_with_abort`/`_join_with_abort`/`_raise_if_failed`) for a dead transform/load worker. Throughput metrics: `_snapshot_stats`/`_build_stats_payload` read the shared `mp.Array` into the `stats` jsonb shape (now including live `queues`/`workers`); `_rate`/`_fmt_rate` compute/format rows-per-second; `_bound_log_size` truncates the process's own log file in place. Uses `logging.getLogger(__name__)`, never `print()`. Maintenance: `truncate_last_partition`, `delete_fact_from`, `clear_etl_history`, `set_highwatermark`. See [`docs/etl-engine.md`](docs/etl-engine.md). |
| `core/query.py` | `timeseries_multibar` — multibar JSON producer. Bars ordered ascending by dim count. `split_grouped_col`/`resolve_group_set`/`slugify_group_set`/`grouped_token`/`grouped_label` own the `<col>:grouped:<slug>` token; `build_group_by_indices` resolves group names for grouped drill-down. `build_filter_tree` (flat rows → nested tree) + `validate_filter_tree`/`check_filter_col` are the shared filter validation — chart endpoints, the alarm CRUD and the evaluator all use them, so error messages must not drift. See [`docs/chart-rendering.md`](docs/chart-rendering.md) for ladder-plot specifics. |
| `core/alarm.py` | Alarm evaluation (Django-free): `run_alarm_cycle` loads all enabled alarms and `Source.load`s once per datasource; `evaluate_alarm` advances the `alarm.eval_end` cursor in `window_s` steps to the fact clock (`fact_max_time`) and runs the `alarm_firing` episode state machine; `check_no_grouped_filters` enforces the grouped-token ban at save time *and* in the evaluator; `measure_value_expr` owns the `count(*)` special case. See [`docs/alarm.md`](docs/alarm.md). |
| `core/audit.py` | `audit_window` — per-bucket source-vs-fact row-count reconciliation; `audit_ids` — PK set difference naming the exact missing/surplus/duplicate rows; `audit_id_details` — targeted second pass giving each named id its own watermark-column value, the `etl_run` that should have caught it, and `last_end_time − that value` as the lower bound on the needed lag (both operands source-clock, so no skew); `_fetch_id_array` — the streaming id reader (see the memory note); `gap_span` folds the gap buckets into one backfill window. No Django imports. See [`docs/etl-audit.md`](docs/etl-audit.md). |
| `core/cache.py` | Server-side `plot_json` cache helpers. `make_cache_key`, `is_cacheable`, `get_cached_plot`, `put_cached_plot`, `bump_cache_epoch`, `clear_cache`. No Django imports. |
| `core/utils.py` | `_parse_dt`, `_pid_alive`, `tail_file`, `format_pks` (applies `datasource.pk_format` to a list of raw PKs — shared by the chart drill-down and the Audit tab's id lists). No Django imports. |
| `core/adhoc.py` | Query console backend: `validate_sql` (single-statement SELECT/WITH lexer), `run_query` (read-only txn + server-side cursor + `application_name` tag), `cancel_query` (`pg_stat_activity` → `pg_cancel_backend`), `history_list` / `history_list_user` / `history_delete` / `history_delete_user`. No Django imports. See [`docs/deployment-config.md`](docs/deployment-config.md). |
| `migrations/starcubes_meta.sql` | Consolidated idempotent meta-schema (0001–0028 combined — 0025 adds the per-datasource alarm table, 0026 the `enabled` flag 0025 omitted, 0028 replaces `context_dim`/`context_value` with `context_filter jsonb` + `comment` + `eval_end` and adds `alarm_history`/`alarm_firing`/`alarm_forever`). Used by `core/schema.py`. Per-step migrations live in `migrations/000N_*.sql`. |
| `starcubes/settings.py` | Reads `STARCUBES_POSTGRES_DSN` from env. Custom DB backend, `CsrfViewMiddleware` in MIDDLEWARE. See [`docs/deployment-config.md`](docs/deployment-config.md). |
| `starcubes/db_backends/` | Custom PG backend package — ensures `starcubes_meta` schema exists, sets search_path. |
| `starcubes/middleware.py` | `RequireLoginMiddleware` — gates `/sources/` and `/admin/`. |
| `starcubes/oidc.py` | `KeycloakOIDCBackend` — optional SSO auth backend, active only when `STARCUBES_OIDC_*` env vars are set. Maps username from `preferred_username`, force-clears `is_superuser`/`is_staff` on auto-created users. See [`docs/deployment-config.md`](docs/deployment-config.md). |
| `viz/views.py` | Chart page, plot/drill/txn JSON endpoints (handle `:grouped`), source list + meta APIs (source_meta carries the datasource `id` the chart's Alert form needs for `sources:alarm_add`). `plot_json` consults `core.cache` for cacheable `count(*)`. The four `query_*` console endpoints gate themselves with `_login_required_json` (403 JSON, not a 302) and answer user SQL errors with HTTP 200. |
| `viz/templates/viz/chart.html` | Multi-source chart UI — see [`docs/chart-app.md`](docs/chart-app.md) and [`docs/chart-rendering.md`](docs/chart-rendering.md). The drill-down's Alert form (`openAlarmView`/`alarmContextRows`/`saveAlarmForm`) creates real alarms with the active filters + clicked series as context; grouped tokens are blocked up front. |
| `sources/views.py` | Datasource CRUD + ETL JSON endpoints. Three role-gating decorators near the top — `@_login_required_json`, `@_admin_required_json`, `@_admin_required_page` — wrap every endpoint that needs one (the user-facing *pages* rely on `RequireLoginMiddleware` instead); the list / detail / me pages are open to logged-in users, everything configuration-shaped is superuser-only. The detail page is split: `source_detail` (4 sub-tabs: Basics, Group Dims, Measures, Alarms) is open; `source_manage` (12 sub-tabs incl. ETL run / log) is admin-only and reads `?apply=<slug>` / `?save-profile=1` for deep-links from the profile catalogue. Group dimensions live under `group-dims/`. Alarms under `alarms/` — login-only CRUD with `context_filter` jsonb + `alarm_history` snapshot on every write, plus read-only `history/`/`firings/` endpoints; `_validate_alarm_context` reuses `core.query`'s filter validation and `check_no_grouped_filters`. `profiles_page` renders `/sources/profiles/`; `profiles_list` is its JSON twin. Six historical-mutation endpoints call `bump_cache_epoch` (alarm endpoints deliberately do not — alarms feed no cache key). `etl_kill` sends SIGTERM/SIGKILL and cleans up a dead-PID or force-killed `etl_run` row (a live-SIGTERM'd parent writes its own row via `EtlAborted`). `etl_logs`/`etl_log_tail` serve the per-run log files under `etl/logs/`/`etl/log/`. See [`docs/sources-app.md`](docs/sources-app.md). |
| `sources/templates/sources/detail.html` | Lean 4-tab datasource view (Basics, Group Dims, Measures, Alarms) accessible to every logged-in user — and **editable** by them: Group Dims, Measures and Alarms are full CRUD, gated login-only, which is the whole reason the page exists. Its Group Dims and inline-edit JS is a verbatim copy of `manage.html`'s; keep it that way. Connection / ETL status polling, inline CRUD for the Alarms tab (context shown read-only as a summary — expressions are created from the chart, not edited inline; ⓘ sub-row with history diff + firing episodes), drag-and-drop in the Group Dims tab. The Manage link in the header is superuser-only and points to `source_manage`. |
| `sources/templates/sources/manage.html` | Superuser-only 12-tab view: Basics, Group Dims, Measures plus Fact Columns, Dimensions, Dim Data, Indices, Storage, ETL Commands, Audit, ETL History, ETL Log. **No Alarms tab** — alarms are a user concern and live on `detail.html` only. ETL run / kill / log lives here; Save as Profile / Apply Profile fire the catalogue handoff (`?apply=<slug>` / `?save-profile=1`). The Group Dims and inline-edit JS is the original that `detail.html` copies verbatim — fix bugs here first, then re-copy. |
| `sources/etl_spawn.py` | `spawn_etl_subprocess(cmd, env=, datasource_name=)` — returns `(Popen, log_path \| None)`. Opens a per-run log file under `STARCUBES_LOG_DIR` and points the child's stdout+stderr at it (instead of the historical `DEVNULL`); `prune_etl_logs`/`slugify_ds`/`log_dir` are its helpers. See [`docs/etl-engine.md`](docs/etl-engine.md). |
| `sources/etl_logging.py` | `configure_etl_logging()` — the one place allowed to wire Django settings into `core/etl.py`'s `logging.getLogger`. Stderr handler always; optional PID-1-stdout mirror when `STARCUBES_ETL_LOG_STDOUT` is set (for `podman logs`); registers `faulthandler` on `SIGUSR1`. See [`docs/etl-engine.md`](docs/etl-engine.md). |
| `core/profile.py` | Source profiles: `load_profile`/`parse_profile_text` (YAML → `ExampleProfile`), `dump_profile` (meta rows → YAML, **never emits `source_dsn`**), `iter_profiles` (catalogue over `profiles/user/` then `profiles/examples/`, user wins), `save_profile`/`delete_profile`, `validate_profile_name` (the filesystem boundary). No Django imports. See [`docs/source-profiles.md`](docs/source-profiles.md). |
| `sources/profile_store.py` | The INSERTs that write a profile into `starcubes_meta`, shared by `seed_example` and the GUI so the two paths cannot drift: `insert_datasource`, `insert_profile_children`, `delete_profile_children`, `update_datasource_scalars`. |
| `examples.sql` | Reference SQL. Lines 43–62 are the template for `core/query.timeseries_multibar`. |
| `docker/build.sh` | The only supported way to build the container image — defaults `CONTAINER_ENGINE=podman` and delegates to the build engine. The Dockerfile's `jsbuilder` stage pulls `@hgruber/timeseries.js` from npm via `npm ci`. See [`docs/deployment-config.md`](docs/deployment-config.md). |

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
