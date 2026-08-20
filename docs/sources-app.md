# sources/ app — deep reference

Full datasource CRUD + ETL monitoring GUI at `/sources/`. No Django ORM —
all DB access via SQLAlchemy (same `_get_cached_engine` / `text()` /
`.mappings()` pattern as `viz/views.py`).

Read this when editing anything under `sources/`.

## URL structure

50 URL patterns total in `sources/urls.py`. Grouped by purpose:

### Pages
- `GET /sources/` — list with last-run status badge per datasource
- `GET/POST /sources/new/` — create datasource
- `GET /sources/<pk>/` — detail (11 tabs: Basics | Fact Columns | Dimensions | Dim Data | Group Dims | Measures | Indices | Storage | ETL Commands | Audit | ETL History | ETL Log)
- `GET/POST /sources/<pk>/edit/` — edit scalar fields
- `POST /sources/<pk>/delete/` — delete (CASCADE in DB)

### JSON: connection / preview
- `POST .../test-connection/` — temporary engine, `SELECT 1`, dispose
- `POST .../connection-status/` — current connectivity badge
- `POST .../preview-import/` — first 5 rows via `Source.load()`

### JSON: ETL run / forever / status
- `POST .../etl/run/` — spawn ETL subprocess. Accepts optional `start`, `end`, `limit` JSON fields.
- `GET  .../etl/status/` — last 20 `etl_run` rows (polled every 3s while running),
  including `pid`, `progress_rows`, `progress_at`, `rows_per_s`,
  `extract_rows_per_s`, `transform_rows_per_s`, `load_rows_per_s`, `stats`,
  `log_path`, `log_tail` (see the throughput-metrics gotcha below). For a
  `running` row the three `*_rows_per_s` columns are always NULL (only the
  terminal UPDATE writes them) — the GUI reads live per-stage rates and
  queue depths from `stats.{extract,transform,load}.rows_s` and
  `stats.queues.{transform,load}` instead.
- `GET  .../etl/logs/` — list this datasource's per-run log files
  (`sources/etl_spawn.py` writes one per spawn under `STARCUBES_LOG_DIR`),
  newest first, joined against the last 20 `etl_run` rows for `run_id`/
  `run_status`.
- `GET  .../etl/log/?file=<name>&offset=<n>` — incremental tail of one log
  file from byte `offset` (capped at 256 KB per response). Without `file`,
  defaults to the newest log this datasource owns, falling back to the
  latest `etl_run.log_path`. `name` is validated against
  `^etl-[A-Za-z0-9_.-]+\.log$` and must start with this datasource's slug
  prefix — the one thing standing between this public-ish JSON endpoint
  and an arbitrary path read.
- `POST .../etl/kill/` — kill the running single-shot ETL run. Plain POST
  sends `SIGTERM` to the parent PID (which writes its own `etl_run` row as
  `failed`, error `killed by user...`, via `EtlAborted`); `{force: 1}`
  escalates to `SIGKILL` on the whole process group and the view finishes
  the row itself, since a killed parent can no longer write it. Returns 409
  if an ETL Forever session is active for the datasource — use Stop Forever
  instead.
- `POST .../etl/forever/start/` — start continuous ETL loop (`--forever`)
- `POST .../etl/forever/stop/` — request stop of running forever loop
- `GET  .../etl/forever/status/` — current forever session state

Sessions survive container restarts: `docker/entrypoint.sh` runs
`manage.py etl_resume_forever` after `bootstrap`, which finds any
`etl_forever` row still in status `running`/`stopping` with a dead PID,
marks it `failed` (error `'container restarted'`), and re-spawns a fresh
forever subprocess at the original interval. Stopping via the GUI sets
`status='stopped'`, which is excluded from the resume scan — only
crashed/orphaned sessions are revived.

### JSON: ETL maintenance
- `POST .../etl/delete-from/` — delete fact rows from timestamp
- `POST .../etl/truncate-last/` — truncate oldest fact partition
- `POST .../etl/truncate-partition/` — truncate a named partition
- `POST .../etl/drop-partition/` — drop a named partition
- `POST .../etl/vacuum-analyze/` — VACUUM ANALYZE fact table
- `POST .../etl/set-highwatermark/` — set HWM from max fact timestamp
- `POST .../etl/clear-history/` — delete `etl_run` rows for datasource
- `POST .../etl/create-partitions/` — create N future partitions
- `POST .../etl/refresh-counts/` — refresh dim count columns

### JSON: audit / backfill
- `POST .../etl/audit/` — `from_time`, `to_time`, `buckets` (≤ 200). Counts
  source rows (N range `count(*)`s on `watermark_column`) against fact rows
  (one bucketed pass over `watermark_fact_expr`) and returns
  `{buckets: [{start, end, source_rows, fact_rows, missing, surplus}],
  total_missing, total_surplus, unreadable_buckets}`. A bucket whose source
  query failed carries `source_rows: null` rather than a bogus gap.
- `POST .../etl/backfill/` — `from_time`, `to_time`. Spawns
  `manage.py etl_run --backfill --start … --end …` through the same
  `spawn_etl_subprocess` path as a normal run, so the per-run log file, ETL
  History and the live monitor all work unchanged. Guarded by the same
  "already running / forever active" 409 check as `etl/run/`
  (`_etl_busy_response`, shared between the two views).

### JSON: schema
- `POST .../create-fact/` — create the fact table + initial partitions
- `POST .../sync-schema/` — sync schema after DDL changes
- `POST .../import-dims/` — import dimension values from source or CSV
- `POST .../indices/create-all/` — create all defined indices + FKs
- `POST .../indices/drop-all/` — drop all indices + FKs

### JSON: dim browsing
- `GET .../dim-data/` — all dimension tables + row counts
- `GET .../dim-data/<dim_table>/` — paginated dim rows for a single table
- `GET .../dimensions/<dim_id>/values/` — paginated dim values

### JSON: group dimensions
A **group dimension** (`starcubes_meta.dim_group_set`) is a named bucketing of
one native dimension; a dimension may have arbitrarily many. The chart
addresses one as `<fact_column>:grouped:<slug>`.
- `GET  .../group-dims/` — every group dimension of the datasource, plus the
  list of dimensions that can carry one
- `GET  .../group-dims/<gs_id>/` — one group dimension with its groups,
  members and per-group row counts
- `POST .../group-dims/add/` — `{name, dimension_id}`; the slug is derived
  from the name and suffixed `-2`, `-3`… on collision within the dimension
- `POST .../group-dims/<gs_id>/save/` — `{name, dimension_id, groups}`;
  replaces **this set's** groups only. Moving the set to another dimension
  drops its groups (dim indices are dimension-specific)
- `POST .../group-dims/<gs_id>/delete/` — delete the set (groups CASCADE)

### JSON: inline CRUD
Fact columns, dimensions, indices each have `/add/`, `/<id>/edit/`, `/<id>/delete/`.
- `POST .../fact-columns/<fc_id>/estimate-cardinality/` — sample source column cardinality

## Gotchas

- **`interval_ladder` Postgres array**: use `CAST(:ladder AS int[])` with value
  `"{1,60,3600,86400}"`. The `::` cast syntax conflicts with SQLAlchemy named
  params (`:param::type` → syntax error from psycopg2).
- **`pg_stat_user_tables` uses `relname`**, not `tablename`. Alias it:
  `t.relname AS tablename` in the Storage tab query.
- **DSN password masking**: `ds["source_dsn_masked"]` is added to the detail
  context via `_mask_dsn()` (regex replaces password between `://user:` and `@`).
  The raw DSN is never sent to the template.
- **Oracle driver is `oracledb`**, not `cx_Oracle`. DSN dialect:
  `oracle+oracledb://...`. `oracledb` defaults to thin mode (pure Python).
  `init_oracle_client()` in `core/source.py` scans `LD_LIBRARY_PATH` for
  `libclntsh.so` and calls `oracledb.init_oracle_client(lib_dir=...)` to
  enable thick mode. Idempotent (guarded by a module flag); must happen
  before any Oracle connection. In the container, mount instantclient at
  `/app/oracle` and `LD_LIBRARY_PATH=/app/oracle` (set in Dockerfile `ENV`).
- **Python lists in Django templates**: pass `json.dumps(list)` from the
  view (e.g. `pg_types_json`, `roles_json`, `idx_methods_json`) and use
  `{{ var|safe }}` in template JS. `{{ python_list|safe }}` renders Python
  repr (single quotes) which is not valid JSON.
- **Login middleware**: `RequireLoginMiddleware` (in `starcubes/middleware.py`)
  intercepts any request starting with `/sources/` or `/admin/` and redirects
  unauthenticated users to `/login/`. Public paths (`/login/`, `/logout/`)
  bypass. `viz/` views are **not** protected by this middleware.
- **Django tables in `starcubes_meta`**: custom DB backend
  (`starcubes/db_backends.DatabaseWrapper`) sets `search_path=starcubes_meta,public`,
  so `auth_user`, `django_session`, `django_migrations`, etc. land in
  `starcubes_meta`. SQLAlchemy connections are unaffected — they use
  schema-qualified names.
- **CSRF**: `CsrfViewMiddleware` is in `MIDDLEWARE`. All POST endpoints use
  the `csrfToken()` helper in `sources/base.html`.
- **ETL subprocess**: uses `sys.executable` so the venv Python is used, and
  inherits `STARCUBES_POSTGRES_DSN` from the environment.
- **ETL throughput metrics + kill button** (detail.html, ETL History tab and
  the persistent statusbar): `pollEtlStatus()`/`pollForeverStatus()` render
  `rows_per_s`/`extract_rows_per_s`/`transform_rows_per_s`/`load_rows_per_s`
  via the `fmtRate()`/`fmtCount()` JS helpers (k/M-suffixed). Those four
  columns are only written by the terminal UPDATE — for `status ===
  'running'` they're always NULL, so the statusbar instead reads
  `run.stats.{extract,transform,load}.rows_s` and
  `run.stats.queues.{transform,load}` for a live `E … T … L … [tq n lq n]`
  line, and shows a `⚠ no progress for Ns` badge (switches the strip to the
  `failed` styling) once `run.progress_at` is more than 60s old — see
  `core/etl.py`'s design-decision note in `CLAUDE.md` for why `progress_at`
  only advances when rows actually move. The ETL History table's `E / T / L`
  column carries a `title=` tooltip built from the per-stage `busy_s`/
  `wait_s` in the `stats` jsonb column — see the same note for the shape and
  why it's measured as net busy time, not wall time. Its `Log` column links
  to the ETL Log tab (`openEtlLogFile()`, preselects the file by basename)
  and, for a failed run, a `<details>` showing `log_tail`. The **Kill**
  button next to Run ETL only appears while `status === 'running'` **and**
  no Forever session is active (mirrors the 409 the view returns); the
  first click sends a plain `SIGTERM`, the second (after `confirm()`) sends
  `{force: 1}`. "Clear ETL History" is duplicated as a second button in the
  Run ETL card (`etlCmdClearHistBtn2`) — both wire to the shared
  `clearEtlHistory()` function, which also calls `refreshHistTable([])` so
  the table doesn't keep showing deleted rows after a clear.
- **Audit tab** (`tab-audit`): reconciles source vs fact row counts per time
  bucket, so late-arriving transactions that slipped past
  `datasource.late_arrival_overlap_s` become visible. Bucket edges come back
  from the server as ISO strings and are handed *straight back* to
  `etl/backfill/` — the window round-trips exactly instead of being
  re-derived in JS, where a rounding difference would backfill the wrong
  span. Rows with `missing > 0` get the `.audit-gap` class and a per-row
  Backfill button; "Backfill all gaps" spans the first to the last gap
  bucket. Running an audit is the intended way to *measure*
  `late_arrival_overlap_s` before setting it.
- **ETL Log tab** (`tab-etl-log`): lazy-loaded on first activation via
  `etlLogActivate()` — populates the `<select>` from `etl/logs/`, then tails
  the selected file from `etl/log/`. Polls every 2s only while the tab is
  active (mirrors `startEtlPoll`/`stopEtlPoll`'s idiom) **and** the last
  known status is `running`; `pollEtlStatus()` calls `etlLogStopPoll()`
  alongside `stopEtlPoll()` once a run resolves. `etlLogOffsets` is a
  per-filename byte offset so re-fetches are incremental; a `truncated`
  response (the server-side `core/etl.py:_bound_log_size` shrank the file)
  resets to offset 0 and clears the pane instead of appending garbage.

## Inline CRUD UX pattern

Each sub-entity `<tr>` has two modes toggled by the `.editing` class:

- `.row-display` spans visible in display mode, hidden in edit mode
- `.row-edit` inputs visible in edit mode
- JS functions: `startEdit(tr)` / `cancelEdit(tr)` / `saveRow(tr, url)` / `deleteRow(tr, url)`
- All mutations via `fetch()` POST → JSON response → in-place DOM update
