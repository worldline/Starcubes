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
user, Profiles / Users (superuser-only), My Profile (`sources:me`), theme
toggle, About (`viz:slides`, new tab), Log out. **Both shells include the
same partial** — `sources/_nav_menu.html`, with chart.html passing its
theme-row ids and `include_prefs=1` — so the menus cannot drift; the tab
bar `_global_tabs.html` mirrors the same order and is still hand-synced
against the partial. The menu CSS/JS live in
`sources/static/sources/nav.css` and `nav.js`, and **`chart.html` now loads
those same two files** instead of carrying its own copies — they used to be
near-verbatim mirrors of each other. `nav.js` exports `makePopover` and
`closeTopPopover`; the chart's Escape handler uses the latter. The base wiring
script (theme helper + `makePopover` call) sits inside `{% block extra_js %}`
of `base.html`; templates that override the block must start with
`{{ block.super }}` (list/manage/new/firings do).
Create-new-source and Manage used to be menu rows; they live on their
pages now (`+ New Datasource` on the list; the expert half of the datasource
page behind its Basic/Expert switch).

## Shared panels and scripts

There used to be two datasource pages — `detail.html` and `manage.html` —
holding two verbatim copies of the Group Dims and Measures markup and its
JavaScript: 919 identical lines, kept in step by a rule in `sources/CLAUDE.md`
rather than by a mechanism, which is exactly how the Measures help text and the
`fmtRate` / `fmtLocal` pairs drifted apart. Shared files replaced the rule, and
the two pages have since become one (`source.html`) with a Basic/Expert switch,
which removes the question entirely. The partials stayed, because a 3,900-line
template is worse than four small ones:

| File | What it holds |
|------|---------------|
| `sources/templates/sources/_group_dims_panel.html` | The Group Dims tab panel. No parameters; a caller needing a permission gate wraps the include. |
| `sources/templates/sources/_measures_panel.html` | The Measures tab panel. Optional `measures_help_short` (truthy = the short help line, default = the long one naming every ladder style), the same `{% if %}` / `\|default:` convention `_nav_menu.html` uses. |
| `sources/static/sources/group_dims.js` | The group-dimension editor, ~660 lines. Needs `DS_PK` and `csrfToken()`; defines `GROUP_PALETTE` itself. |
| `sources/static/sources/inline_edit.js` | `startEdit` / `cancelEdit` / `saveRow` / `deleteRow`, plus `ROW_SPECS` — each table's display columns described once, read both by `saveRow` and by source.html's row builders. Needs `csrfToken()`, `postJSON()` and the page's `esc()`. |
| `sources/templates/sources/_cmd_action.html` | One ETL/schema command block (label, description, button, result span). Parameters `title`, `desc`, `btn_id`, `btn_label`, `result_id`, plus optional `btn_class` and `last`. |

Three more files are shared with the **chart app** (`viz/`), which loads them
with `{% static 'sources/…' %}` — Django's AppDirs finder resolves that across
apps, and the repo already includes across the boundary in both directions
(`base.html` includes `viz/about.html`, `chart.html` includes
`sources/_nav_menu.html`):

| File | What it holds |
|------|---------------|
| `sources/static/sources/nav.css` / `nav.js` | The anchored-dropdown mechanic and its styling. `nav.js` exports `makePopover` and `closeTopPopover`. |
| `sources/static/sources/datetimepicker.js` / `.css` | The date/time picker and its popup styling. The two apps had grown separate features — the chart's copy knew a `trigger` button and clamped the popup inside the viewport, the shell's had `getUTCISO()`; the shared file is the union. Field chrome stays per app (`.dtp-label` in the shell, `.dtp-field` in the chart). |

**The colour palettes stay per app** — sources vs. chart. The two do not agree
on `--muted`, `--accent` or `--bg2`, and the shared files use `var(--…)`
throughout, so each page resolves them against its own tokens. Reconciling the palettes would be a
visible design change, not a refactor.

**The one non-obvious constraint is load order.** `group_dims.js` binds its
event handlers at top level, not on `DOMContentLoaded`, so it must come after
the panel markup. At the same time the page calls `grpLoadList()` from an inline
script, and an inline script runs while the document is parsed — earlier than
any deferred file. So both tags are plain synchronous `<script src>`, placed
after the markup and before the page's own inline script; adding `defer` would
raise `grpLoadList is not defined`. `source.html` puts them in
`{% block extra_js %}` after `{{ block.super }}`, and `_alarms_js.html` follows
for the same reason: it needs `esc()` and `postJSON()` to exist already.

The group-dimension list loads lazily, on the first click of its tab, in both
detail levels. `startEdit` is wrapped further down to re-sync the dimension
source fields — which works because `inline_edit.js` leaves it a global
function declaration.

`esc()` stays per-page rather than shared, but `saveRow` calls it, so the two
definitions must remain the identical null-safe variant. It only runs on click,
which is why it need not exist when `inline_edit.js` loads.

## Pages at a glance

The `/sources/` UI splits along three axes:

| Audience          | Path                          | Purpose                                       |
|-------------------|-------------------------------|-----------------------------------------------|
| Any user          | `/sources/`                   | List of datasources with last-run status       |
| Any user          | `/sources/<pk>/`              | The datasource page at the remembered detail level. Basic = Basics (read-only) + Group Dims, Measures, Alarms, Firings — the last three full CRUD |
| Any user          | `/sources/alarms/firings/`    | Global firing-episode overview (`?show=open` for the live breaches) |
| Any user          | `/sources/alarms/export/`     | Every datasource's alarm rules as one backup file (`.yaml` attachment) |
| Any user          | `/sources/<pk>/?tab=firings`  | The same overview scoped to one datasource (`&show=open` likewise) |
| Superuser         | `/sources/<pk>/manage/`       | The same page forced to Expert: 12 tabs, adding Fact Columns, Dimensions, Dim Data, Indices, Storage, ETL (sub-tabbed into Run / Maintenance / Schema / Audit / History / Log) and Rollup (sub-tabbed into Status / Advise / Config / Build / Verify) |
| Superuser         | `/sources/new/`, `…/edit/`    | Datasource creation / scalar edit              |
| Superuser         | `/sources/profiles/`          | Profile catalogue + upload (replaces the modal that used to live on `list.html`) |
| Logged-in user    | `/sources/me/`                | Own profile, password change, alarm-channel opt-in (email/XMPP), query history    |
| Superuser         | `/sources/settings/`          | User admin (create / edit / password / delete)  |

The split between Basic and Expert is about *audience*, not about read
versus write. Basic carries the things any signed-in user is meant to
curate — group dimensions, measures and alarms — and they are fully
editable there. Expert adds the schema, storage and ETL surface that only
an operator has any use for.

Two mechanisms enforce that, and they are not interchangeable. Whether the
expert markup is **rendered** is decided server-side by `expert_allowed`
(`request.user.is_superuser`): a non-superuser never receives the admin
panels, the delete form, its csrf token, the expert `<dl>` rows or the ETL
controls. That is the boundary that matters, since every endpoint behind
them is `@_admin_required_json`. Whether *rendered* markup is **shown** is
the `.expert-only` class against `html[data-mode]`, and that exists for
superusers alone — a view preference, remembered in `localStorage` under
`starcubes-source-mode` and applied before first paint by `base.html`'s
`{% block prepaint %}`. The switch itself (`_mode_toggle.html`, right-hand
end of the sub-tab bar) is rendered only for superusers, for the same
reason the old Manage link was.

The datasource-independent pages — the datasource list, profiles,
firings, transports (notification transports + SSO, editing
`starcubes_meta.app_config` under env-first precedence), settings
(Users) and me (My Profile) — share the link-based tab bar
`_global_tabs.html`, included at the top of each page. Tab order mirrors
the two hamburger menus: Datasources, Profiles, Firings, Transports,
Users, My Profile. The active tab is derived from
`request.resolver_match.url_name`, and the bar is permission-filtered to
mirror the views: superusers see all six tabs, other logged-in users
Datasources, Firings and My Profile.

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
- `GET /sources/me/` — own profile, change password, alarm-channel opt-in, query-console history
- `GET /sources/settings/` — user admin (superuser only)
- `GET /sources/profiles/` — profile catalogue + upload form (superuser only)
- `GET/POST /sources/new/` — create datasource (superuser only)
- `GET /sources/<pk>/` — the datasource page at the remembered detail level
  (Basic: Basics, Group Dims, Measures, Alarms, Firings). Basics is read-only;
  Group Dims, Measures and Alarms are editable by any signed-in user — that is
  the point of the page, and their endpoints are login-only.
- `GET /sources/<pk>/manage/` — the same page forced to Expert (12 tabs; ETL
  run / audit / history / log are sub-tabs of ETL, and the pre-aggregation's
  five commands are sub-tabs of Rollup) (superuser only)
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
  if a Run Forever session is active for the datasource — use Stop Forever
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
- `POST .../etl/preflight/`'s `data_staleness` check compares the newest
  run's `src_now - max_wm` against `datasource.max_data_lag_s`; the ETL
  History table renders the same difference per run.
- `POST .../etl/housekeeping/` — run the periodic maintenance now instead of
  waiting for `housekeeping_interval_s`: targeted `ANALYZE`, BRIN range
  summarisation, dimension fill levels, partition sizes. Returns
  `{report: {...}}`, the same structure written to
  `starcubes_meta.housekeeping_run`. **Not** a dry run, unlike the preflight
  button — and it takes no datasource lock, so it works during forever mode.
- `POST .../etl/preflight/` — run the pre-load checks without loading
  anything. Returns `{report: {checks: [{code, level, message, detail}],
  repairs, blocked, ran_at}}`, the same structure every run writes into
  `etl_run.stats.preflight`. A **dry run**: `repair=False`, so a missing
  fact or dim table is reported rather than created — self-repair belongs to
  the load, where the creation lands on the run row. See
  [`etl-automation.md`](etl-automation.md)
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

### JSON: rollup (pre-aggregation)
The five `rollup_*` management commands, split by how long they run. See
[`docs/rollup.md`](rollup.md) for what the numbers mean and
[the gotcha below](#gotchas) for why the split is not a style choice.
- `GET  .../rollup/state/` — the configuration plus per-table build state:
  what `rollup_config --show` prints, as JSON, with the table sizes
  (summed over the partition tree, not read off the parent) and
  `unreconstructable_span` added. Login-only, like every status endpoint.
- `POST .../rollup/config/` — `interval`, `columns`, `marginals`, `measures`
  (comma-separated), or `off=1`. Validates against a `dataclasses.replace`
  copy of the `Source` before writing, exactly as `rollup_config` does, and
  returns `{ok: false, errors: [...]}` from `validate_config` on rejection.
  Then `ensure_rollup_tables` and, for every spec whose hash changed, a
  `write_rollup_state` with both bounds cleared.
- `POST .../rollup/advise/` — spawns `rollup_advise`
- `POST .../rollup/build/` — spawns `rollup_build`; `backfill`, `rebuild`,
  `force`, `from_time`, `batch_buckets`, `max_batches`
- `POST .../rollup/verify/` — spawns `rollup_verify`; `days`, `interval`
- `GET  .../rollup/status/` — the last 20 `rollup_run` rows plus the newest
  successful `advise` and `verify` report. The reports are fetched separately
  from the history rows on purpose: an advise report carries every dimension
  and every ladder stage, and repeating it twenty times would make this poll
  heavier than what it polls.
- `POST .../rollup/kill/` — SIGTERM the running command, `force=1` for SIGKILL
- `POST .../rollup/drop/` — `drop_rollup_tables`. Refuses a span below
  `retention_floor` with 409 and the span named, unless `force=1`.

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
watermark — see [`docs/alarm.md`](alarm.md). The Alarms tab (partials
`_alarms_panel.html` + `_alarms_js.html`) shows in **both** detail levels:
alarms are a user concern, and its endpoints are login-gated, so hiding it
from an operator would only have cost them a tab. It used to live on
`detail.html` alone, which is why the merged page had to gain it. The chart
drill-down's Alert form posts here too (with `context`).

The tab is one half of a round trip with the chart:

- **Into the chart**: every alarm row carries a `↗` link built by `chartUrl()`,
  which re-serializes the stored `context_filter` into the chart's
  `filter=`/`andor=`/`indent=` parameters (mirroring `buildHistoryParams()` in
  `chart.html`) plus the alarm's own measure. `group_by` is always written,
  even empty — without it the chart restores its per-source state from
  localStorage and drops the filter. It names the first context column that is
  a groupable dimension (`GROUPABLE_COLS`, from the datasource's `dimension`
  rows); a fan-out filter token like `so_peer` is not one, and there the link
  falls back to no breakdown — one series over exactly what the alarm measures.
- **Out of the chart**: the three configuration links there (Group Dims,
  Measures, the bell) all carry the live filter as `?context=<json>`. The add
  card above the alarm table has three states — `hidden` (nothing carried in,
  and there is no add button), `collapsed` (a context is waiting: the card
  names it and offers to open) and `open` (the bell's `new-alarm=1`, Clone, or
  the collapsed card's button). Closing or saving returns to `collapsed` with
  the context intact, so a second alarm over the same slice needs no trip back;
  **Clear** is the only way to drop it. While a context waits in a tab the user
  is not on, the Alarms tab button carries a `.has-ctx` dot.
- **Which alarms the context belongs to**: with a context held, the tab asks
  `match-context?any_measure=1` once and tints those rows (`.ctx-match`, an
  accent bar plus a light wash) with a title saying why; the collapsed head
  counts them ("2 alarms below watch it"). Only a context that was carried in
  highlights — an empty one matches every global alarm, which would say
  nothing. The lookup repeats after a save, delete or inline edit, not on a
  plain tab-click refresh: nothing changed there.

All six endpoints are **login-only**: any signed-in user may manage the
alarms of any datasource. Every create/update also writes an
`alarm_history` snapshot row (`kind` created/updated, `username`,
`changed_at`, old/new row jsonb) — which is how comment changes are
tracked, there is no separate comment history.
- `GET  .../alarms/` — list this datasource's alarms (POST, despite the name).
  Carries `context_labels` — `{filter_column: {source_id: name}}` for every
  context on the page, resolved server-side in one pass
  (`core.alarm.resolve_context_labels`), so the Context column reads as names
  rather than the ids it stores. A column that resolves to a dim table is
  always present in the map, which is what lets the tab flag an id whose
  dimension row is gone instead of letting it pass for a name. Cosmetic: an
  unreachable fact database degrades to raw ids, never to an error.
- `POST .../alarms/match-context/` — alarms already watching a given context
  with a given measure (`context` + `measure`, same shapes as add). Read-only,
  behind the duplicate hint in the chart's Alert form and the Alarms tab's add
  form; it never blocks a save. "Already watched" is normalized context
  (`core.alarm.normalize_context_rows`) **plus** measure — the context alone
  would flag every alarm on a busy slice, the whole rule only an exact copy
  the name uniqueness already refuses. Also returns `context_labels` for the
  submitted context, which the list's map cannot cover.
  `any_measure=1` drops the measure half and answers with every alarm watching
  the slice — that is the highlight lookup described below. Each match carries
  its own `measure`, so the Alarms tab gets both answers from one request and
  filters the duplicate hint client-side; changing the form's measure costs no
  round trip.
The Alarms tab has **no add button**. An alarm without a context watches the
whole datasource, which is rarely meant, and a form that could not carry a
filter was the most inviting way to create one by accident. The form opens on
request only — from the chart's bell (`?new-alarm=1`) or from Clone — and a
hint pointing at the chart stands in its place until then. A deliberately
global alarm stays reachable: open a chart with no filters and press the bell.
**Clone** (⧉ per row) fills that same form from an existing alarm, including
its context, with a `(copy)` name made unique against the loaded names; nothing
is written until the user saves, so a clone exists only once its metric has
been changed.

- `POST .../alarms/add/` — `name, comment, measure, cond_op, threshold,
  window_s, notify_channels` (comma-separated string, not a JSON array),
  `severity` (optional PagerDuty override, one of critical/error/warning/info;
  absent/empty = global default), `enabled` (checkbox semantics: absent means
  "field not in this form" and defaults to true; present-but-empty is
  unchecked) and the optional `context` (JSON string, see above).
- `POST .../alarms/<alarm_id>/edit/` — same field set, except the context:
  an absent `context` field keeps the stored one (the inline editor never
  sends it — a filter expression is not inline-editable; delete +
  re-create from the chart instead), a present one replaces it wholesale.
  `severity` follows absent-keeps/empty-clears; the UI always submits it.
- `POST .../alarms/<alarm_id>/delete/` — idempotent; history, comments
  values and firings CASCADE away with the alarm.
- `POST .../alarms/<alarm_id>/history/` — change history, newest first.
- `POST .../alarms/<alarm_id>/firings/` — firing episodes, newest first
  (`open: true` while `closed_at` is NULL).
- `GET  .../alarms/export/` — this datasource's alarm rules as a `.yaml`
  attachment; `GET /sources/alarms/export/` is the same for every
  datasource at once.
- `POST .../alarms/import/` — restore rules from an export file (`file`
  upload or `content` text). Two-phase like `profile/apply/`: without
  `confirm` it answers with the plan (`added`/`changed`/`unchanged`/
  `skipped` per datasource, plus `warnings`), with `confirm=true` it
  writes them in one transaction. An **upsert on the alarm name**, never a
  replace — `alarm_history` and `alarm_firing` survive an update, which a
  replace would cascade away. A malformed file is a 400 and writes
  nothing; a rule that does not fit this installation (unknown recipient,
  no PagerDuty key, a column the schema no longer has) is skipped with a
  warning rather than failing the restore. See
  [`docs/alarm.md`](alarm.md).

The field validation these endpoints share lives in
`sources/alarm_fields.py`, not in `views.py`: the alarm import runs a rule
through the very same `parse_alarm_fields` a form save does, so there is
one truth about what a valid alarm is. `views.py` keeps two thin wrappers
that turn its `(error, fields, status)` tuples back into the JSON bodies
these endpoints have always sent.

`name` is unique per datasource (DB enforces). `window_s` must be `> 0`
(CHECK constraint). `cond_op` is one of `>`, `>=`, `<`, `<=`, `=`
(CHECK constraint — there is no `!=`). `measure` is `count(*)`, a bare
fact column, or `func(col)` with `func` in `sum/avg/min/max` (`quantile`
is not comparable and is rejected). `notify_channels` is a Postgres
`text[]` (email/xmpp/pagerduty — the log side is always on, not a channel;
`slack` stays reserved vocabulary without a transport); dispatch and its
opt-in rules live in `docs/alarm.md`.

The **global episode overview** lives at `/sources/alarms/firings/`
(`alarm_firings_page`, server-rendered, newest first, LIMIT 500,
`?show=open` for the live breaches, `?ds=<id>` for one datasource — the
`Datasource` dropdown submits both as GET params) — every `alarm_firing`
episode joined with its alarm rule and datasource. It is linked from the
shared tab bar (`_global_tabs.html`) and from the hamburger menu
("Firings", any logged-in user). The per-alarm episodes remain in
the Alarms tab's ⓘ sub-row.

The same list is a **Firings tab on the datasource page**, and it is the
same code twice over:

- `views._firing_episodes(show, ds)` reads and formats the episodes for
  both, so an episode means the same thing on either page;
- `sources/_firings.html` renders both, with `views.firings_context()`
  filling it. Its one switch is `scoped`: on a datasource's own page the
  `Datasource` dropdown makes no sense, so it becomes an "All firings ↗"
  link to the global page and the `Datasource` column drops out of the
  table. The status column's amber "open" pill (with its blinking inner
  dot) is the only signal here — the blinking red dot at the start of the
  row was dropped and lives on the Alarms tab instead (`.dot-live` in
  `base.html`, emitted by `_alarms_js.html` when `has_open_episode`).
  `url_all` / `url_open` come from the caller because each page
  filters inside its own URL — `?show=` globally, `?tab=firings[&show=open]`
  on the tab, which reloads the detail page and lands back on the tab
  through the existing `?tab=` deep-link.

The tab is rendered with the page rather than fetched, because the sub-tabs
are toggled client-side and there is no navigation to hang a lazy load off;
the cost is one extra meta query per detail page load. The Alarms tab's
header links to it with a single "Firings →" (`?tab=firings`) — the tab
itself carries the way out to the global overview. The `firings_context`
key is `firings_ds`, not `ds`: the page's `ds` is the datasource row.

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
- **Thick mode requests Oracle Net compression.** `init_oracle_client()`
  also registers `_request_oracle_compression` as a `do_connect` listener on
  the `Engine` *class* — Oracle connections come from five `create_engine()`
  sites, so a per-engine hook would be one somebody forgets. It inserts
  `(COMPRESSION=on)` into every `(DESCRIPTION=…)`, which covers the
  documented URL form because SQLAlchemy builds a descriptor from
  `host:port/SID` and `?service_name=`. Descriptor, not `sqlnet.ora`: the
  only config dir the client reads is the operator's `/app/oracle` mount,
  not ours to write. A descriptor that already names `COMPRESSION` is left
  alone; a tnsnames alias or `?dsn=` Easy Connect string has no descriptor
  to edit and logs one warning per DSN. Thin mode cannot compress at all.
  Compression is *negotiated* — the database server needs
  `SQLNET.COMPRESSION = on` too, or it stays off without any error.
- **The mode is logged by `SourcesConfig.ready()`, not by
  `init_oracle_client()`**: settings.py calls the latter before `LOGGING` is
  applied, so an INFO record there goes to `lastResort` and is dropped. The
  sentence comes from `core.source.oracle_client_mode()` and goes to the
  `starcubes.oracle` logger, pinned to INFO in `LOGGING` — one line per
  process (uwsgi master, each `manage.py` command, each ETL run).
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
- **ETL throughput metrics + kill button** (source.html, the ETL tab's History
  sub-tab and
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
  to the Log sub-tab (`openEtlLogFile()` switches both tiers and preselects the
  file by basename)
  and, for a failed run, a `<details>` showing `log_tail`. The **Kill**
  button next to Run once only appears while `status === 'running'` **and**
  no Forever session is active (mirrors the 409 the view returns); the
  first click sends a plain `SIGTERM`, the second (after `confirm()`) sends
  `{force: 1}`. "Clear ETL History" is duplicated as a second button in the
  Run once card (`etlCmdClearHistBtn2`) — both wire to the shared
  `clearEtlHistory()` function, which also calls `refreshHistTable([])` so
  the table doesn't keep showing deleted rows after a clear.
- **Audit sub-tab** (`etlsub-audit`, source.html): reconciles source vs fact row counts per time
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
- **Log sub-tab** (`etlsub-log`, source.html): lazy-loaded on first activation
  via `etlLogActivate()` — populates the `<select>` from `etl/logs/`, then
  tails the selected file from `etl/log/`. Polls every 2s only while the
  sub-tab is active — `activateEtlSub()` starts and stops it, and the
  top-level tab switcher stops it whenever the ETL tab is left (mirrors `startEtlPoll`/`stopEtlPoll`'s idiom) **and** the last
  known status is `running`; `pollEtlStatus()` calls `etlLogStopPoll()`
  alongside `stopEtlPoll()` once a run resolves. `etlLogOffsets` is a
  per-filename byte offset so re-fetches are incremental; a `truncated`
  response (the server-side `core/etl.py:_bound_log_size` shrank the file)
  resets to offset 0 and clears the pane instead of appending garbage.
- **Rollup tab: three commands run out of process, two run in it.**
  `docker/uwsgi.ini` sets `harakiri = 120`, which kills the whole worker
  *process* and takes its other in-flight requests with it. `rollup_advise`
  (one exact pass over a window of `fact`), `rollup_build --backfill` (hours)
  and `rollup_verify` (a matrix of chart queries, half of them against `fact`)
  therefore go out through `spawn_etl_subprocess` — the same detached-spawn
  path an ETL run uses, argv and all — while `rollup_config`, `rollup_drop`
  and the state read stay inside the request. Two further traps behind that:
    - **`rollup_drop` the command calls `input()`** (`rollup_drop.py:75`) for
      its confirmation. A spawned process has no stdin, so it would hang. The
      view calls `drop_rollup_tables()` directly and confirms in the browser.
    - **`rollup_build` holds the datasource advisory lock for the whole run**
      (`holder="rollup_build"`), so it collides with an ETL. Its view runs
      `_etl_busy_response` as well as the rollup's own busy check, which turns
      "blocked on a lock" into a message that names the reason. `advise` and
      `verify` are read-only and take no lock.
- **A killed rollup command closes its own row** — `install_abort_handler`
  in `core/rollup_run.py` turns SIGTERM/SIGINT into a `KeyboardInterrupt`, the
  same trick `etl_run.py:_abort_signal_handler` uses. Without it the default
  disposition kills the command outright, `finish_run` never happens, and the
  row is stuck on `running` until its process is gone. Liveness is the
  process lease since migration 0042 (`core/lease.py`, see "Runs across
  containers" below), and a zombie still holds nothing: its lease connection
  closed when it exited. For a legacy row without a lease `reap_stale` still
  falls back to `os.kill(pid, 0)` plus `/proc/<pid>/stat`, because a zombie
  answers signal 0.
- **Runs across containers.** Every kill, stop and status endpoint judges a
  run by its lease (`core/lease.py:reap_dead_runs`), never by a PID, so a
  request landing on a container other than the one running the process sees
  the same truth. A *signal* can only reach a process in the same container:
  `views._signal_target` sends one only when the row's `instance_id` is this
  container's and the pid is verified against the lease connection's
  `application_name` — a bare PID is a small number any process may hold
  after a restart. A run on another container gets its stop request written
  onto the row instead (`core/lease.py:request_stop`, column `control`): the
  process ends itself at its next check, and a forced kill is carried out by
  that container's `node_agent`. Stop Forever works from anywhere, because
  the loop reads `stop_requested` before every iteration and every 5 s while
  it sleeps.
- **`.action-result` is `display:none` without a modifier.** Only `.ok`,
  `.err`, `.banner` and (since the Rollup tab) `.warn` make it visible. A
  banner written with the class alone renders into the DOM and is simply
  never seen — which is exactly how the retention-floor notice first shipped.
- **Rollup and fact sizes are summed over `pg_partition_tree`**, not read
  off the parent row: both are range-partitioned, and `pg_total_relation_size`
  and `n_live_tup` on a partitioned parent are 0. The Storage tab gets away
  with the parent because it lists every partition as its own line.
- **Profile-handoff query params**: `source.html` reads
  `?apply=<slug>` and `?save-profile=1` once its profile IIFE has wired
  `saveDialog` / `applyDialog`. The catalogue page can therefore deep-link
  straight into the apply flow against a chosen datasource; it builds the
  URL as a string against `/manage/`, not through `{% url %}`, so that path
  has to keep working. The handoff triggers are no-ops if the user isn't a
  superuser (the buttons aren't rendered), so non-admin sessions land on a
  plain Basic page.
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
- JS functions: `startEdit(tr)` / `cancelEdit(tr)` / `saveRow(tr, url)` / `deleteRow(tr, url)`,
  shared by both pages from `sources/static/sources/inline_edit.js`
- All mutations via `fetch()` POST → JSON response → in-place DOM update
