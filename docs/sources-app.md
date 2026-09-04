# sources/ app — deep reference

Full datasource CRUD + ETL monitoring GUI at `/sources/`. No Django ORM —
all DB access via SQLAlchemy (same `_get_cached_engine` / `text()` /
`.mappings()` pattern as `viz/views.py`).

Read this when editing anything under `sources/`.

## Nav bar

Single `#nav-tabs` flex row, same pattern as the chart page (see
`docs/chart-app.md`): source tabs, spacer, then everything else collapsed
into `#nav-menu`, a `makePopover` dropdown behind the `#nav-menu-toggle`
hamburger. Menu rows: username header (display only), Datasources
(`sources:list`) and Firings (`sources:alarm_firings`) for any logged-in
user, Profiles / Users (superuser-only), theme toggle, My Profile
(`sources:me`), About (`viz:slides`, new tab), Log out. The entry list is
deliberately identical to chart.html's `#nav-menu` (same rows, same
order) so the two apps read as one product — change both together. The
menu CSS/JS live in `sources/static/sources/nav.css` and
`nav.js` — near-verbatim mirrors of the chart's inline copies, kept
deliberately separate so the two apps don't share a static include. The
base wiring script (theme helper + `makePopover` call) sits inside
`{% block extra_js %}` of `base.html`; templates that override the block
must start with `{{ block.super }}` (list/manage/new/firings do).
Create-new-source and Manage used to be menu rows; they live on their
pages now (`+ New Datasource` on the list, the Manage link in the detail
header).

## Pages at a glance

The `/sources/` UI splits along three axes:

| Audience          | Path                          | Purpose                                       |
|-------------------|-------------------------------|-----------------------------------------------|
| Any user          | `/sources/`                   | List of datasources with last-run status       |
| Any user          | `/sources/<pk>/`              | Editable detail (Basics read-only; Group Dims, Measures, Alarms are CRUD) |
| Any user          | `/sources/alarms/firings/`    | Global firing-episode overview (`?show=open` for the live breaches) |
| Superuser         | `/sources/<pk>/manage/`       | Full configuration (12 sub-tabs incl. ETL run / log) |
| Superuser         | `/sources/new/`, `…/edit/`    | Datasource creation / scalar edit              |
| Superuser         | `/sources/profiles/`          | Profile catalogue + upload (replaces the modal that used to live on `list.html`) |
| Logged-in user    | `/sources/me/`                | Own profile, password change, query history    |
| Superuser         | `/sources/settings/`          | User admin (create / edit / password / delete)  |

The split between `detail` and `manage` is about *audience*, not about
read versus write. `detail` carries the three things any signed-in user is
meant to curate — group dimensions, measures and alarms — and they are
fully editable there. `manage` adds the schema, storage and ETL surface
that only an operator has any use for, behind a superuser gate. The link
to `manage` is rendered only for superusers.

The four datasource-independent pages — settings (Users), the datasource
list, firings, profiles and settings — share the link-based tab bar
`_global_tabs.html`, included at the top of each page. Tab order mirrors
the two hamburger menus: Datasources, Firings, then the superuser-only
Profiles, Users. The active tab is derived
from `request.resolver_match.url_name`, and the bar is
permission-filtered to mirror the views: superusers see all four tabs,
other logged-in users Datasources and Firings.

## Role gates

Three decorator helpers live near the top of `sources/views.py`. There is
no `_login_required_page`: pages that any signed-in user may see rely on
`RequireLoginMiddleware` alone, since a login-only page decorator would
have nothing left to refuse. Apply them as the outermost decorator:

| Decorator                | Effect on anonymous       | Effect on logged-in non-admin  | Effect on superuser |
|--------------------------|---------------------------|-------------------------------|--------------------|
| `@_login_required_json`  | 403 JSON `{ok:false}`     | passes through                | passes through     |
| `@_admin_required_json`  | 403 JSON `{ok:false}`     | 403 JSON `{ok:false}`         | passes through     |
| `@_admin_required_page`  | middleware redirect       | 403 HTML                      | passes through     |

Notes:
- The middleware redirect (anonymous) is owned by
  `starcubes/middleware.py:RequireLoginMiddleware`, not by the decorators.
  The page decorators therefore cannot help anonymous users — they just
  refuse non-admins.
- Configuration-shaped mutations (schema, ETL, storage, profiles) take
  `@_admin_required_json`. The endpoints behind the user page —
  `measures/*`, `group-dims/*`, `alarms/*` — take `@_login_required_json`
  instead: gating them as admin makes the user page read-only and
  defeats its purpose. Read-only JSON endpoints are login-only too, so
  polling works for any logged-in user but never leaks to anonymous
  traffic. Every endpoint carries a gate explicitly; none relies on
  `RequireLoginMiddleware` alone.
- Pages that build a configuration form (`source_new`, `source_manage`,
  `source_edit`) take `@_admin_required_page`. The list and detail pages
  stay accessible to any authenticated user.

## URL structure

72 URL patterns total in `sources/urls.py`. Grouped by purpose:

### Pages
- `GET /sources/` — list with last-run status badge per datasource
- `GET /sources/me/` — own profile, change password, query-console history
- `GET /sources/settings/` — user admin (superuser only)
- `GET /sources/profiles/` — profile catalogue + upload form (superuser only)
- `GET/POST /sources/new/` — create datasource (superuser only)
- `GET /sources/<pk>/` — user page (4 sub-tabs: Basics, Group Dims, Measures,
  Alarms). Basics is read-only; the other three are editable by any signed-in
  user — that is the point of the page, and their endpoints are login-only.
- `GET /sources/<pk>/manage/` — full detail (12 sub-tabs incl. ETL run / log) (superuser only)
- `GET/POST /sources/<pk>/edit/` — edit scalar fields (superuser only)
- `POST /sources/<pk>/delete/` — delete (CASCADE in DB) (superuser only)

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
- `POST .../etl/audit-ids/` — `from_time`, `to_time`, `sample`. Pulls both
  PK sets and diffs them, returning `{source_rows, fact_rows, fact_distinct,
  duplicate_rows, missing_count, missing_ids, missing_display, surplus_count,
  surplus_ids, surplus_display, sample, truncated}`. Counts are always
  exact; the `*_ids` lists are capped at `sample`. `*_display` has
  `datasource.pk_format` applied (via `core/utils.py:format_pks`, shared
  with the chart drill-down), so it may carry HTML — the GUI injects it as
  markup only when it differs from the raw id, the same test
  `chart.html`'s drill list uses. Bounded by `STARCUBES_MAX_AUDIT_IDS`
  (2M/side).
- `POST .../etl/audit-id-details/` — `from_time`, `to_time`, repeated `ids`
  (falls back to a comma-joined `ids_csv`). Targeted second pass over ids
  `audit-ids/` already named: one `WHERE <pk_expr> IN (…)` per 1000 ids
  against the source — chunked for Oracle's IN-list limit, with the time
  window kept in the predicate so the watermark index carries it — plus one
  `etl_run` lookup. Returns `{rows: [{pk, pk_display, txn_wm, run_id,
  run_watermark, run_started_at, needed_lag_s, covering_runs}],
  watermark_column, resolved, unresolved, not_in_source,
  needed_lag: {median, p99, max, n}}`.
  `txn_wm` is the transaction's own value in `watermark_column`;
  `run_watermark` is `etl_run.last_end_time`, the upper bound of the WHERE
  that run read the source with. `needed_lag_s` is the difference between
  them: how far the window's upper edge had already moved past the
  transaction while it still was not visible.
  **Both operands come from the source's watermark column**, so the figure
  carries no clock skew. `run_started_at` is returned for diagnostics only —
  it is the starcubes host's clock and must not be subtracted from a source
  timestamp. `watermark_column` is echoed back so the GUI can head the
  column with the name the source really uses.
  The number cannot be negative (a run is only attributed when
  `txn_wm < last_end_time`), is a *lower* bound, and sizes an **increase**
  of `watermark_lag_s` rather than an absolute target. Rows in no run window
  land in `unresolved` and stay out of the percentiles.
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

### JSON: source profiles
A **profile** is a datasource's whole configuration as one YAML file, minus
the DSN. See [`docs/source-profiles.md`](source-profiles.md).
- `GET  /sources/profiles/list/` — catalogue: every profile in `profiles/user/`
  and `profiles/examples/` with `origin`, description, dialect and counts.
  A profile that fails to parse is listed with an `error` instead of taking
  the catalogue down. Login-only (any authenticated user — the catalogue
  backs the new-datasource "Use profile" picker).
- `GET  /sources/profiles/<slug>/` — one profile plus the `scalars` the
  create form prefills from (`core.profile.datasource_scalars`). Admin-only
  (the page that uses this is the Manage-handoff, which itself is gated).
- `GET  /sources/profiles/<slug>/download/` — the raw YAML file as an
  attachment. Serves the bytes as they sit on disk, so a
  download/upload trip round-trips losslessly. Login-only.
- `POST /sources/profiles/upload/` — `request.FILES["file"]`; validated by
  `parse_profile_text` **before** anything is written, capped at 2 MB.
  Admin-only (the form sits on `/sources/profiles/`).
- `POST /sources/profiles/<slug>/delete/` — saved profiles only; a shipped
  example returns 400. Admin-only.
- `POST .../profile/save/` — `name`, `description`, `overwrite`. Returns 409
  with `exists: true` when the name is taken and `overwrite` is not set.
  Admin-only.
- `GET  .../profile/download/` — the same YAML as an attachment. Admin-only.
- `POST .../profile/apply/` — `profile`, `confirm`. Without `confirm`, a
  dry-run diff (`{added, removed, changed}` per section plus scalar
  changes) and warnings; with `confirm=true`, one transaction that rewrites
  the scalars and replaces every child row, then bumps `cache_epoch`.
  Guarded by the same `_etl_busy_response` 409 as `etl/run/`.
  Admin-only. **Manage-handoff**: deep-linking to
  `/sources/<pk>/manage/?apply=<slug>` triggers the apply dialog once
  the page is wired (the catalogue's "Use" button on an existing
  datasource lands here via the profile's `Use` button).

`<slug>` is validated against `^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`
(`core.profile.validate_profile_name`) — no dots, no separators. Profile
names become filenames, so this is what stands between these endpoints and
a write outside `profiles/`.

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

### JSON: alarms (per-datasource threshold alarms)
A row in `starcubes_meta.alarm` binds a measure, comparison and threshold
to a sliding window, an optional **filter context** and a notification
channel list. The context is the chart's whole filter expression — the
flat filter rows `[{col, op, vals, andOr, indent}]` as a JSON string in
the `context` field, stored verbatim in `alarm.context_filter` jsonb;
`NULL`/`[]` means the whole datasource. Grouped tokens
(`<col>:grouped:<slug>`) are rejected at save time (400): group sets are
user-editable and a stored grouped reference would silently change the
alarm's meaning. Fan-out filter_group tokens stay allowed. The evaluator
(`core/alarm.py`, driven by `etl_run` after every load) records firings as
episodes in `starcubes_meta.alarm_firing`, stamped with the data high
watermark — see [`docs/alarm.md`](alarm.md). The Alarms tab lives on `detail.html` only —
alarms are a user concern, so `manage.html` does not carry a copy. The
chart drill-down's Alert form posts here too (with `context`).

All six endpoints are **login-only**: any signed-in user may manage the
alarms of any datasource. Every create/update also writes an
`alarm_history` snapshot row (`kind` created/updated, `username`,
`changed_at`, old/new row jsonb) — which is how comment changes are
tracked, there is no separate comment history.
- `GET  .../alarms/` — list this datasource's alarms (POST, despite the name).
- `POST .../alarms/add/` — `name, comment, measure, cond_op, threshold,
  window_s, notify_channels` (comma-separated string, not a JSON array),
  `enabled` (checkbox semantics: absent means "field not in this form" and
  defaults to true; present-but-empty is unchecked) and the optional
  `context` (JSON string, see above).
- `POST .../alarms/<alarm_id>/edit/` — same field set, except the context:
  an absent `context` field keeps the stored one (the inline editor never
  sends it — a filter expression is not inline-editable; delete +
  re-create from the chart instead), a present one replaces it wholesale.
- `POST .../alarms/<alarm_id>/delete/` — idempotent; history, comments
  values and firings CASCADE away with the alarm.
- `POST .../alarms/<alarm_id>/history/` — change history, newest first.
- `POST .../alarms/<alarm_id>/firings/` — firing episodes, newest first
  (`open: true` while `closed_at` is NULL).

`name` is unique per datasource (DB enforces). `window_s` must be `> 0`
(CHECK constraint). `cond_op` is one of `>`, `>=`, `<`, `<=`, `=`
(CHECK constraint — there is no `!=`). `measure` is `count(*)`, a bare
fact column, or `func(col)` with `func` in `sum/avg/min/max` (`quantile`
is not comparable and is rejected). `notify_channels` is a Postgres
`text[]`; only `log` has plumbing today (the episode row plus the loop's
log file) — the rest is vocabulary, see backlog.

The **global episode overview** lives at `/sources/alarms/firings/`
(`alarm_firings_page`, server-rendered, newest first, LIMIT 500,
`?show=open` for the live breaches, `?ds=<id>` for one datasource — the
`Datasource` dropdown submits both as GET params) — every `alarm_firing`
episode joined with its alarm rule and datasource. It is linked from the
shared tab bar (`_global_tabs.html`), from the hamburger menu ("Firings",
any logged-in user), from the Alarms tab ("All firings") and from the
chart toolbar's firings button (pre-filtered via `?ds=`); the per-alarm
episodes remain in detail.html's ⓘ sub-row.

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
- **No import-time profile cache**: `sources/views.py` used to hold
  `_EXAMPLE_PROFILE = load_profile()` at module scope, so a malformed
  profile broke server startup. Profiles are user-writable now, which made
  that unacceptable — the catalogue and every profile endpoint load on
  demand and report a bad profile as data (`error` in the listing, 400 from
  the endpoint).
- **Profile apply is a replace, not a merge**: `profile_apply` deletes every
  child row before inserting the profile's. It never touches `name`,
  `schema_name` or `source_dsn` — a profile carries no credentials, and
  applying a configuration must not rename a live datasource.
- **Login middleware**: `RequireLoginMiddleware` (in `starcubes/middleware.py`)
  intercepts any request starting with `/sources/` or `/admin/` and redirects
  unauthenticated users to `/login/`. Public paths (`/login/`, `/logout/`)
  bypass. `viz/` views are **not** protected by this middleware.
- **Django tables in `starcubes_meta`**: custom DB backend
  (`starcubes/db_backends/DatabaseWrapper`) sets
  `search_path=starcubes_meta,public`, so `auth_user`, `django_session`,
  `django_migrations`, etc. land in `starcubes_meta`. SQLAlchemy connections
  are unaffected — they use schema-qualified names.
- **CSRF**: `CsrfViewMiddleware` is in `MIDDLEWARE`. All POST endpoints use
  the `csrfToken()` helper in `sources/base.html`.
- **ETL subprocess**: uses `sys.executable` so the venv Python is used, and
  inherits `STARCUBES_POSTGRES_DSN` from the environment.
- **ETL throughput metrics + kill button** (manage.html, ETL History tab and
  the persistent statusbar): `pollEtlStatus()`/`pollForeverStatus()` render
  `rows_per_s`/`extract_rows_per_s`/`transform_rows_per_s`/`load_rows_per_s`
  via the `fmtRate()`/`fmtCount()` JS helpers (k/M-suffixed). Those four
  columns are only written by the terminal UPDATE — for `status ===
  'running'` they're always NULL, so the statusbar instead reads
  `run.stats.{extract,transform,load}.rows_s` and
  `run.stats.queues.{transform,load}` for a live `E … T … L … [tq n lq n]`
  line, and shows a `⚠ no progress for Ns` badge (switches the strip to the
  `failed` styling) once `run.progress_at` is more than 60s old — see
  `core/etl.py`'s design-decision note in `docs/etl-engine.md` ("ETL
  throughput is measured as net busy time per stage, not wall time") for why `progress_at`
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
- **Audit tab** (`tab-audit`, manage.html): reconciles source vs fact row counts per time
  bucket, so late-arriving transactions that slipped past
  `datasource.late_arrival_overlap_s` become visible. The From/To fields — and
  every other `DateTimePicker` in the ETL tabs — are read in the **browser's
  local timezone**, matching the tables, and sent as ISO UTC via
  `DateTimePicker.getUTCISO()`. Sending the raw field text instead used to
  make every dialog off by the UTC offset, since `core/utils.py:_parse_dt`
  reads a naive string as UTC while the picker formats local parts. Bucket edges come back
  from the server as ISO strings and are handed *straight back* to
  `etl/backfill/` — the window round-trips exactly instead of being
  re-derived in JS, where a rounding difference would backfill the wrong
  span. Rows with `missing > 0` get the `.audit-gap` class and a per-row
  Backfill button; "Backfill all gaps" spans the first to the last gap
  bucket. Running an audit is the intended way to *measure*
  `late_arrival_overlap_s` before setting it.
  Every bucket row also carries an **IDs** button — deliberately including
  the ones the counts call clean, because equal counts do not prove a bucket
  is intact (N lost plus N deleted upstream cancel out, and duplicate PKs
  are invisible to counting). "Compare IDs" does the same for the whole
  window. The result card separates *missing*, *surplus* and *duplicate*,
  and only *missing* offers a Backfill — a backfill inserts, it never
  deletes.
  The missing list additionally carries a **Details** button
  (`auditIdDetails`), which is how `watermark_lag_s` is measured rather than
  guessed. Six columns, each headed by whose value it is — a transaction has
  no watermark, it has a value in the watermark column; the *run* is what
  has a watermark, and naming both the same made the first version of this
  table unreadable:
  `PK | Txn <watermark_column> | Run watermark | Run | Run started (local) |
  Watermark − <watermark_column>`.
  Columns 2, 3 and 6 are source-clock; column 5 is the starcubes host's
  clock, muted and carrying a `title=` saying so, because it deliberately
  plays no part in the difference. "Run watermark" carries a `title=` noting
  it is the WHERE's upper bound, equal to `max(<watermark_column>)` at that
  moment when no lag is configured.
  The summary says **raise by**, not set to: with a lag already in force the
  figure is the additional amount, and it is a lower bound either way.
- **ETL Log tab** (`tab-etl-log`, manage.html): lazy-loaded on first activation
  via `etlLogActivate()` — populates the `<select>` from `etl/logs/`, then
  tails the selected file from `etl/log/`. Polls every 2s only while the tab
  is active (mirrors `startEtlPoll`/`stopEtlPoll`'s idiom) **and** the last
  known status is `running`; `pollEtlStatus()` calls `etlLogStopPoll()`
  alongside `stopEtlPoll()` once a run resolves. `etlLogOffsets` is a
  per-filename byte offset so re-fetches are incremental; a `truncated`
  response (the server-side `core/etl.py:_bound_log_size` shrank the file)
  resets to offset 0 and clears the pane instead of appending garbage.
- **Manage-handoff query params**: `manage.html` reads
  `?apply=<slug>` and `?save-profile=1` once its profile IIFE has wired
  `saveDialog` / `applyDialog`. The catalogue page can therefore deep-link
  straight into the apply flow against a chosen datasource. The handoff
  triggers are no-ops if the user isn't a superuser (the buttons aren't
  rendered), so non-admin sessions land on a normal Manage page.
- **User admin (`settings.html`)**: every mutation goes through
  `settings_page`'s `action=` dispatch (`create_user`, `update_user`,
  `set_password`, `change_password`, `delete_user`). Self-demotion
  protection refuses `update_user` that would set `is_superuser=false` on
  the requesting user if they are the last superuser in
  `auth_user` — both for safety and so the admin gate never locks itself
  out by accident. `change_password` re-checks the current password before
  setting the new one (same constraint Django's password form enforces).

## Inline CRUD UX pattern

Each sub-entity `<tr>` has two modes toggled by the `.editing` class:

- `.row-display` spans visible in display mode, hidden in edit mode
- `.row-edit` inputs visible in edit mode
- JS functions: `startEdit(tr)` / `cancelEdit(tr)` / `saveRow(tr, url)` / `deleteRow(tr, url)`
- All mutations via `fetch()` POST → JSON response → in-place DOM update
