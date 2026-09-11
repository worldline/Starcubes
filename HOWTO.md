# starcubes How-to

Guide to using the starcubes GUI and configuring datasources. See the
[README](README.md) for what starcubes is and what it looks like, and
[`docs/internals.md`](docs/internals.md) for the architecture behind it.

## Table of contents

- [Sources GUI](#sources-gui)
  - [Login](#login)
  - [SSO login (optional, Keycloak/OIDC)](#sso-login-optional-keycloakoidc)
  - [Datasource list](#datasource-list)
  - [Creating a datasource](#creating-a-datasource)
  - [Basic and Expert](#basic-and-expert)
  - [Tab: Basics](#tab-basics)
  - [Tab: Fact Columns](#tab-fact-columns)
  - [Tab: Dimensions](#tab-dimensions)
  - [Tab: Dim Data](#tab-dim-data)
  - [Tab: Group Dims](#tab-group-dims)
  - [Tab: Indices](#tab-indices)
  - [Tab: Storage](#tab-storage)
  - [Tab: ETL](#tab-etl)
  - [Tab: Rollup](#tab-rollup)
  - [Editing and deleting a datasource](#editing-and-deleting-a-datasource)
- [Source configuration reference](#source-configuration-reference)
  - [Identity fields](#identity-fields)
  - [Source connection fields](#source-connection-fields)
  - [Watermark and partitioning fields](#watermark-and-partitioning-fields)
  - [ETL performance fields](#etl-performance-fields)
  - [Fact column fields](#fact-column-fields)
  - [Dimension fields](#dimension-fields)
  - [Index fields](#index-fields)
- [Pre-aggregation (rollups)](#pre-aggregation-rollups)
- [Chart GUI](#chart-gui)
  - [Source tabs](#source-tabs)
  - [Control bar](#control-bar)
  - [Filters](#filters)
  - [Follow mode](#follow-mode)
  - [Keyboard shortcuts](#keyboard-shortcuts)
  - [Legend](#legend)
  - [Drill-down](#drill-down)
  - [Query console](#query-console)
  - [Preferences](#preferences)
  - [URL state and bookmarks](#url-state-and-bookmarks)

---

## Sources GUI

The Sources GUI lives at `/sources/`. It provides full CRUD for datasources,
ETL monitoring, and warehouse inspection — all without Django ORM (pure
SQLAlchemy against `starcubes_meta`).

### Login

`/sources/` and `/admin/` require authentication. The chart page at `/` is
public, but per-user preferences (bar pixel target) require login. Create a
superuser with `manage.py createsuperuser`, then log in at `/login/`.
`RequireLoginMiddleware` enforces authentication; logging out redirects back
to the login page.

### SSO login (optional, Keycloak/OIDC)

The login page can additionally offer "Sign in with Keycloak" next to the
password form — it's a pure add-on: the password form always stays, and the
button only appears once SSO is configured. There is no separate on/off
flag; setting all three required environment variables *is* what enables it.

Required env vars:

- `STARCUBES_OIDC_ISSUER` — the realm URL, no trailing slash, e.g.
  `https://keycloak.example.com/realms/starcubes`. Keycloak's endpoints
  under a realm are derived from this automatically
  (`{issuer}/protocol/openid-connect/{auth,token,userinfo,certs}`) — no
  need to configure them individually.
- `STARCUBES_OIDC_CLIENT_ID`, `STARCUBES_OIDC_CLIENT_SECRET` — from the
  Keycloak client's Credentials tab.
- `STARCUBES_OIDC_DISPLAY_NAME` (optional, default `Keycloak`) — the text
  on the login button, e.g. `"Red Hat SSO"`.

Keycloak-side client configuration:

- **Confidential** client (not public — a client secret is required).
- **Standard flow** (authorization code) enabled.
- **Valid redirect URI**: exactly `https://<host>/oidc/callback/`, matching
  the externally-reachable host of this deployment — **including the port**
  when it is not the scheme default (`http://host:8080/oidc/callback/` for a
  plain-HTTP container run). Django derives this URI from the `Host` header it
  receives; if Keycloak answers "Invalid parameter: redirect_uri", compare the
  `redirect_uri=` value in the browser's address bar against what is registered
  on the client. A *missing port* there points at nginx's `HTTP_HOST` parameter
  rather than at the Keycloak config — see "CSRF" in
  `docs/deployment-config.md`.
- **`STARCUBES_ALLOWED_HOSTS`** must name the host you reach the app under.
  The image defaults to the loopback names only (`127.0.0.1,localhost,[::1]`),
  because this setting is what validates the `Host` header that the redirect
  URI and the CSRF origin are built from. An SSO deployment is reachable under
  a real hostname by definition, so it always has to override this — e.g.
  `-e STARCUBES_ALLOWED_HOSTS=starcubes.example.com`. Without it Django
  answers `400 Invalid HTTP_HOST header` and the login page never renders.

**Behind a TLS-terminating proxy**, Django must be told to trust the
forwarded scheme, or it computes `redirect_uri=http://...` and Keycloak
rejects it as a mismatch — and the CSRF origin check fails the same way,
because the browser sends `Origin: https://<host>` while Django derives
`http://<host>`. Set **`STARCUBES_TRUST_PROXY_SSL=1`**; it applies
`SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")`.

Two conditions must both hold, or this makes things worse rather than better:

- the proxy must **set or overwrite** `X-Forwarded-Proto` on every request.
  If it merely passes a client-supplied value through, anyone can forge it;
- leave it at `0` when there is no TLS proxy. With the flag off, nginx pins
  `X-Forwarded-Proto` / `X-Forwarded-Ssl` to the scheme it actually served, so
  a client cannot claim https. That matters because uwsgi honours those two
  headers on its own when it builds `wsgi.url_scheme` — the flag is what
  decides whether they are believed, not merely a Django setting.

The proxy must also forward the original `Host` header (nginx:
`proxy_set_header Host $host;`), since the hostname and port in the redirect
URI come from it.

First successful SSO login auto-creates a Django user — no pre-created
account needed — always as a plain non-superuser/non-staff account, exactly
like a manually-created password account. A superuser still has to grant
`is_superuser` by hand (via `/admin/` or `manage.py`) for `/sources/settings/`
access; SSO grants no special privileges.

The Django username is taken from Keycloak's `preferred_username` claim. If
a local password account shares its email address with a Keycloak identity,
the two merge into one account on first SSO login — this is intentional.
If instead a local *username* collides with a Keycloak `preferred_username`
but the emails differ, first SSO login for that identity fails with a
uniqueness error; keep Keycloak usernames distinct from pre-existing local
usernames, or align the account's email first.

Two further cases the email lookup cannot resolve on its own, both handled
by falling back to `preferred_username`:

- **Several local accounts share one email address.** The account whose
  username matches the Keycloak identity wins. If none of them matches, the
  login is refused as ambiguous rather than guessed at — fix the duplicate
  addresses in `auth_user`.
- **The Keycloak identity has no email address.** Matching then runs on the
  username alone. Merging with a local account by email obviously cannot
  happen in that case, so give Keycloak users an email address if you rely
  on that merge.

The username is deliberately *not* consulted when an identity presents an
email that simply matches no local account — that would let anyone who can
choose their own `preferred_username` in Keycloak take over a same-named
local account, superuser ones included.

#### When SSO comes back but you are not logged in

Every failure inside the OIDC callback used to redirect to `/`, which is
also where a *successful* login lands — the two were indistinguishable.
Failures now land on **`/login/?sso=failed`** and the login page says so.

The reason is always written to the container log, one line per attempt:

```sh
podman logs <container> 2>&1 | grep -i oidc
```

A failed attempt logs `OIDC login failed:` followed by the fields that tell
the branches apart — whether Keycloak itself reported an `error`, whether
the session still carried its `oidc_states` entry (`had_oidc_states=False`
means the session was lost between the redirect to Keycloak and the return,
i.e. a cookie/host problem, not a Keycloak one), and whether a user was
resolved and active. A successful one logs `OIDC login succeeded:` with the
username. Note that a mismatched `state`, a bad JWT signature or a wrong
nonce are *not* in this list: those abort with HTTP 400, and a broken
token/userinfo endpoint with HTTP 500 — visible errors, not silent ones.

### Datasource list

`/sources/` shows a card with one row per datasource. The page sits in
the shared **Users / Datasources / Profiles / Firings** tab bar
(Datasources active); **+ New Datasource** above the table is
superuser-only:

| Column | Description |
|--------|-------------|
| Name | Links to the detail page |
| Dialect | Oracle, PostgreSQL, MySQL, or MSSQL (green if connection OK, red if failed) |
| Schema | Postgres schema name (`starcubes_<name>`) |
| Last Run | Status badge: green check (ok), red X (failed), pulsing dot (running), grey dash (never). Timestamps shown in local timezone. |
| Rows | Total rows from last successful ETL |
| Actions | View button (links to detail) |

Connection status badges auto-check each source DB on page load. While any
ETL is running, the page polls every 3 seconds and updates the status badge
automatically.

### Alarm firings

The **Firings** tab (and the hamburger menu's Firings item) opens the
global episode
overview at `/sources/alarms/firings/` — one row per threshold breach
episode, across all datasources, newest first:

| Column | Description |
|--------|-------------|
| Alarm / Datasource | The alarm that fired; the name links to its datasource's detail page (Alarms tab) |
| Rule | The alarm's measure, comparison, threshold and window, e.g. `count(*) > 5 (5m)` |
| Status | Pulsing `open` while the threshold is still being breached, `closed` after it cleared |
| First / Last breach | Start and end of the breach period (local timezone) |
| Closed | When the first window fell back under the threshold |
| Duration | Last breach minus first breach |
| Value first → last | The aggregate value in the first and the last breaching window |

**Open only** restricts the list to episodes still breaching — the "what is
firing right now" lens. **Datasource** (the dropdown next to it) restricts
the list to one source's episodes; both filters combine. The same page is
reachable from each datasource's Alarms tab via **All firings →** and from
the chart page's bell button (which arrives pre-filtered to the active
source); per-alarm episodes also appear under the alarm's ⓘ button there.

### Creating a datasource

Click **+ New Datasource** on the list page. The form has four cards:

#### Identity

| Field | Required | Description |
|-------|----------|-------------|
| Name | yes | Short identifier used in URLs and schema names (e.g. `mydb`) |
| Schema Name | yes | Postgres schema for warehouse tables (e.g. `starcubes_mydb`) |
| Dialect | yes | Source database dialect: `oracle`, `postgresql`, `mysql`, or `mssql` |

#### Source Connection

| Field | Required | Description |
|-------|----------|-------------|
| Source DSN | yes | SQLAlchemy connection URL for the source database (shown as password field) |
| Import Query | no | Full `SELECT ... FROM ...` without a WHERE clause. Leave blank and set Base Table to auto-compose. |
| Base Table | no | FROM table used when Import Query is blank. Also used for the default watermark-max query. |
| Small Window Hint | no | Dialect-specific SQL hint injected after `SELECT` for small-window loads (e.g. `/*+ ORDERED INDEX(txn, TXN3) */` for Oracle) |

#### Watermark & Partitioning

| Field | Required | Description |
|-------|----------|-------------|
| Watermark Column | yes | Source-side column used for incremental ETL boundaries (e.g. `end_time`) |
| Partition Column | yes | Fact column used for monthly RANGE partitioning (usually the same as the time column) |
| Watermark Max Query | no | Custom `SELECT max(<wm>) FROM ...` query. Defaults to `SELECT max(<wm>) FROM <base_table>`. |

#### ETL Performance

| Field | Required | Description |
|-------|----------|-------------|
| Chunk Size | no | Rows per extract chunk (default 10 000) |
| Transform Processes | no | Parallel transform workers (default 1) |
| Interval Ladder | no | Comma-separated seconds, finest to coarsest (default `1,60,3600,86400`). Controls which bar intervals the chart offers. |
| Bar Pixel Target | no | Minimum bar width in pixels for auto-interval selection (default 5) |

Click **Create Datasource** to save. You are redirected to the detail page.

#### Profile

The create form has a **Profile** card with a dropdown listing every
profile under `profiles/` — the shipped examples and anything saved from
this installation.

Picking one **prefills the fields above** (base table, watermark column,
partitioning, performance and display settings) and, on Create, seeds the
fact columns, dimensions, indices, measures and group dimensions. The
prefilled fields stay editable, so what you see before clicking Create is
what gets stored. Nothing is written until then.

The bundled `oracle_payments` profile carries 19 fact columns, 14
dimensions and 9 measures covering every ladder style. A warning appears
when the form's dialect disagrees with the profile's, since the profile's
`source_expr` values are dialect-specific SQL.

Source DSN is optional here: a datasource can be configured from a profile
long before anyone has credentials for its source. Everything except
connecting to the source works without one.

The same profiles are loaded by `manage.py seed_example --profile <name>`.
See [`docs/source-profiles.md`](docs/source-profiles.md) for the YAML
schema, saving and applying — drop a new YAML under `profiles/`, no Python
changes required.

#### Saving and applying profiles

- **Hamburger menu → Profiles** (or the Profiles tab) — the catalogue: what is available, what
  each profile contains, and buttons to use, upload or delete one.
- **Detail page → Basics → Save as Profile…** — writes this datasource's
  whole configuration to `profiles/user/<name>.yaml`, or downloads it as a
  file. The source DSN is never included.
- **Detail page → Basics → Apply Profile…** — shows what would change
  first (added, removed and changed rows per section, plus scalar
  settings); a second, red button applies it. This **replaces** the
  configuration — what the profile does not define, the datasource loses —
  but never touches name, schema or DSN.

### Basic and Expert

The datasource page has two detail levels, switched with the
**[ Basic | Expert ]** control at the right-hand end of the tab bar.

**Basic** shows the five tabs anyone signed in is meant to work with:
Basics, Group Dims, Measures, Alarms and Firings. **Expert** adds the seven
an operator needs — Fact Columns, Dimensions, Dim Data, Indices, Storage,
ETL and Rollup — and fills out the Basics tab with the tuning fields.

The switch appears for administrators only; everyone else gets Basic and
never sees a control that would lead nowhere. The choice is remembered in
the browser, so opening another datasource keeps the level you were at.
`/sources/<id>/manage/` still opens straight into Expert, which is where
the links from the profile catalogue and the create/edit forms land.

### Tab: Basics

Read-only display of the datasource's configuration — thirteen fields in
Basic, all twenty-five in Expert (see
[Source configuration reference](#source-configuration-reference)). Click
**Edit** to go to the edit form. The dialect badge shows the connection status
(green = reachable, red = unreachable) — checked on page load.

**Preview Import** button on the Basics tab header runs the import query with
`LIMIT 5` (or `ROWNUM <= 5` for Oracle) and shows the first 5 rows in a
collapsible table below.

### Tab: Fact Columns

A data table with one row per fact column. Each row shows:

- **Ordinal** — display/SELECT order
- **Name** — column name in the warehouse fact table
- **PG Type** — Postgres type (`smallint`, `bigint`, `integer`, `timestamptz`, `text`, `real`, `double precision`, `boolean`)
- **Role** — `time`, `measure`, `dimension`, or `pk`
- **Source Expr** — SQL projection in the source database; used to compose the import query when Base Table is set
- **Actions** — edit (pencil) and delete (X) icons

Click the pencil icon to toggle inline edit mode. The row becomes editable
with Save/Cancel buttons. Delete asks for confirmation.

For columns with role `dimension` that have no dimension table entry yet, a
**cardinality** button appears — it estimates distinct values over the last
24 hours to help decide whether the column belongs in a dim table (max 32 767
values).

**Add row**: the form at the bottom lets you add a new fact column with
ordinal, name, PG type, role, and source expression. All changes happen via
JSON POST — no page reload.

### Tab: Dimensions

**Import Dims** button in the tab header calls `import_dimensions` — populates
or refreshes all dimension tables from the source DB (or inline CSV values).

A data table with one row per dimension mapping:

- **Fact Column** — which fact column this dim maps to
- **Dim Table** — warehouse dimension table name
- **Source Table / Key / Name / Desc** — source-side lookup table and columns
- **Primary** — whether this row drives DDL and dim import (exactly one per dim table)
- **Inline Values** — CSV lines (`source_id,name[,description]`). Lines starting with `#` are comments.
- **Actions** — edit and delete

**Mutual exclusivity**: Source fields (Source Table, Key, Name, Desc) and
Inline Values are mutually exclusive. Typing in Inline Values disables and
clears the source fields, and vice versa.

- **Source-backed dimensions**: populate from a lookup table in the source
  DB (e.g. `peer` table with columns `peer_id`, `peer_name`, `peer_desc`).
- **Inline dimensions**: populate from a fixed CSV value set stored in
  `inline_values`. Ideal for small enumerations like currency codes that
  have no source lookup table (e.g. `978,EUR,Euro`).

When multiple fact columns share one dim table, mark exactly one as Primary.
The primary row drives DDL creation and dimension import; the others just
link their fact column to the dim table for the transform stage.

**Add row**: same fields as the edit form. All mutations via JSON POST.

### Tab: Dim Data

Lazy-loaded when you click the tab. Shows sub-tabs — one per primary
dimension table, labeled `dim_table <-- fact_column` (inline dimensions
get an "inline" badge). Each sub-tab loads on first click and displays:

| Column | Description |
|--------|-------------|
| Index | `smallserial` primary key in the dim table |
| Source ID | Original source identifier |
| Name | Human-readable name |
| Description | Description text |
| Count | Approximate total fact rows referencing this value (refreshed after ETL) |

If the dim table has not been created yet (Import Dims has not been run), the
sub-tab shows "Table not yet created".

### Tab: Group Dims

**Requires login.** This tab is only visible to authenticated users.

A **group dimension** buckets the values of one native dimension into named
groups, so the chart can break down by "Approved / Declined / (other)" instead
of by 1 200 individual response codes. A dimension can carry arbitrarily many
of them — `peer` grouped by country *and* by peer type, side by side — and the
same dim value may be used in each of them.

The tab is split: the list of existing group dimensions on the left, the editor
for the selected one on the right.

1. **Create one**: click **+ New group dimension**. The editor opens with

   - a **Name** — this is what the chart's Group By and filter dropdowns show.
     It is preset to `<dimension> (grouped)` and keeps following the dimension
     dropdown until you type your own name.
   - a **Dimension** dropdown (only dimensions with a dim table are listed).

   Switching the dimension of a group dimension that already has assignments
   clears them after a confirmation — dim indices belong to one dimension.

2. **Create groups**: click **+ Add Group**. Each group has:
   - A color swatch (click to cycle through 12 preset colors)
   - A name input
   - A delete button
   - Draggable chips for assigned values

3. **Assign values** by:
   - **Drag and drop**: drag a value chip from the Unassigned zone or from
     one group into another group's zone.
   - **Bulk add by filter**: in the "Bulk add to group" card, pick a target
     group, choose an operator (starts with / contains), type a filter string,
     and click Apply. All matching values are added to the group.
   - **Search and add**: type in the search box, select from the dropdown
     results, and click "Add selected".
   - **Per-group "+ Add value"**: each group zone has a popover with search
     and checkboxes for assigning individual values.

4. **Unassigned zone**: values not in any group of *this* group dimension
   appear in a collapsible section at the bottom. Drag a chip here to remove
   it from its group.

5. **Save**: click the button to save the name and all groups atomically. The
   name and every group name must be non-empty, and within one group dimension
   a value cannot appear in more than one group. The confirmation shows the
   token the chart will use, e.g. `response:grouped:approved-declined`.

6. **Delete** removes the group dimension and its groups; other group
   dimensions on the same column are untouched.

Values not assigned to any group appear as "(other)" in the grouped chart view.

Renaming a group dimension changes its URL token, so chart links and saved
per-source chart state that pointed at the old name fall back to the plain
column. Links written before group dimensions were named (`?group_by=response:grouped`)
keep working and resolve to the column's first group dimension.


### Tab: Indices

Data table with one row per index:

| Column | Description |
|--------|-------------|
| Column Name | Fact column to index |
| Method | `brin`, `btree`, or `hash` |
| Actions | Edit and delete |

**Create All Indices**: builds all FK constraints and indices defined in this
tab for the datasource. This is a one-shot operation after the first ETL
load — re-running it on existing indices will error. (Indices are created with
`IF NOT EXISTS` so re-running is safe if no conflicting indices exist.)

**Drop All Indices**: removes all FK constraints and indices. Requires you
to type the datasource name to confirm. Useful before a bulk re-load.

Both operations show their result in the action result panel below the header.

### Tab: Storage

Shows Postgres warehouse statistics for the datasource schema:

**Tables card**:

| Column | Description |
|--------|-------------|
| Table | Table name (sortable) |
| Total Size | Total disk usage (sortable) |
| Data Size | Data size (excludes toast/index) |
| Index Size | Index size |
| Approx Rows | Estimated row count |
| Dead Rows | Dead tuples (highlighted if > 1000 — indicates a need for VACUUM) |
| Last Vacuum | Most recent VACUUM date |
| Last Analyze | Most recent ANALYZE date |
| Actions | **Truncate** and **Drop** buttons for partition tables |

Click column headers to sort. Partition tables (monthly fact partitions) have
**Truncate** (empties the partition) and **Drop** (removes the partition
entirely) action buttons. The rollup tables (and their partitions, when the
rollup is on) appear in this list alongside `fact` and the dim tables — the
Rollup tab's Status sub-tab shows their sizes in context.

**Indexes card**:

| Column | Description |
|--------|-------------|
| Index | Index name (sortable) |
| Table | Table name |
| Method | Index method badge |
| Size | Disk size (sortable) |
| Unique | Whether the index is unique |
| Scans | Number of index scans |
| Rows Read | Tuples read via index |
| Rows Fetched | Tuples fetched (heap fetches) |


### Tab: ETL

Everything that happens around a load, in six sub-tabs: **Run** starts one,
**Maintenance** and **Schema** hold the operations around it, and **Audit**,
**History** and **Log** are the read-only views onto what a run did.

All actions that modify data or schema require confirmation.

#### Run

**Run once** — Start an incremental ETL load. Optional fields:
- **Start** — Override the high watermark start time
- **End** — Override the end time (skip the source max query)
- **Limit** — Cap the maximum number of rows to import

**Run Forever** — Run ETL continuously at a fixed interval. Enter the interval
in seconds and click **Start Forever**. The card shows the current status
(running/stopping/stopped/failed) and the latest run result. Click **Stop
Forever** to signal the process to stop; it finishes the current iteration and
then exits. Only one forever session can be active per datasource.

**Preflight** — The checks every ETL run makes before it loads anything:
schema objects, metadata version, index coverage, the source projection, and
the watermark against the newest fact row. A dry run — it reports what a load
would repair and changes nothing, so it is safe to click at any time.

#### Maintenance

**Housekeeping** — Runs the periodic maintenance the forever loop performs
every `housekeeping_interval_s`: targeted `ANALYZE` of the partitions being
written, BRIN range summarisation, dimension fill levels and partition sizes.
Safe alongside a running load, and unlike Preflight it is not a dry run.

**Data Cleanup**:
- **Delete From** — Delete all fact rows at or after a given timestamp
- **Vacuum Analyze** — Run `VACUUM ANALYZE` on the fact table

**Metadata**:
- **Set High Watermark** — Insert an `etl_run` row with `status=ok` and
  `last_end_time` set to `max(partition_column)` from the fact table. Useful
  for skipping historical data that has already been loaded externally.
- **Clear ETL History** — Delete all `etl_run` rows for the datasource.

**Query Cache** — Delete cached plot rows from `starcubes_meta.query_cache`
for this datasource. Leave both filters blank to drop every cached plot, or
pick a group_by and/or measure to delete only that breakdown.

#### Schema
- **Create Schema** — Create the fact table, dimension tables, and initial
  monthly partitions. Must be run before the first ETL. Refused while ETL is
  running.
- **Sync Schema** — Compare the physical warehouse schema with the current
  metadata configuration. Click **Check** for a dry-run diff (added columns,
  type changes, missing dim tables). Then select columns/dim tables to drop
  and click **Confirm** to execute the sync. Refused while ETL is running.
- **Create Partitions** — Create N future partitions. Rarely needed now:
  every ETL run already ensures the partitions its load window (plus one
  period of runway) needs before it starts, including filling in a gap left
  by a source that's been idle a while. This button remains for pre-creating
  further ahead than that, or for a source that hasn't run yet.
- **Import Dimensions** — Re-import all dimension tables from the source DB
  or inline CSV values.

#### Audit

Compares row counts between the source and the fact table per time bucket,
and diffs the primary keys of a window to name the exact rows that are
missing. See [Finding and repairing missed transactions](#finding-and-repairing-missed-transactions)
for the full workflow.

#### History


Shows the last 20 ETL runs:

| Column | Description |
|--------|-------------|
| ID | Run ID |
| Status | Badge: running (pulsing), ok (green), failed (red) |
| Started | Start timestamp |
| Finished | Finish timestamp |
| Window Start | First end_time in the load window |
| Window End | Last end_time in the load window |
| Rows | Number of rows loaded |
| Runtime (ms) | Wall-clock duration |
| Error | Truncated error message (hover for full text) |
| Log | **Log** button opens that run's log file in the **Log** sub-tab. A failed
run also shows a collapsible **tail** with the last ~8 KB of the log
(readable even without shell/volume access to the container). |

The table refreshes live while an ETL is running. While a run is `running`,
the statusbar at the bottom of the page shows live per-stage throughput
(`E … T … L …`) and queue depths (`[tq n lq n]`) read from the run's `stats`
— the four rate columns above are only filled in once the run finishes. If
`no progress for Ns` appears in the statusbar, the run is stalled: something
downstream (usually the load stage) has stopped making progress. Check the
**Log** sub-tab for the traceback.

#### Log


Tails the raw log file of a spawned ETL process — every `manage.py etl_run`
invocation started from the GUI (single run or forever) writes its own
timestamped file under `STARCUBES_LOG_DIR` (`/app/logs` in the container
image; mount a volume there to keep logs across `podman rm`).

- The dropdown lists this datasource's log files, newest first, each
  labelled with the run ID and status it belongs to.
- **Follow** (checked by default) auto-scrolls to the bottom as new lines
  arrive.
- **Refresh** fetches immediately instead of waiting for the next 2s poll.
  Polling only runs while this sub-tab is open and the selected run is still
  active.
- Clicking **Log** on a **History** row jumps here with that run's file
  preselected.

If a run stalls, this is the first place to look — every 30s (tunable via
`STARCUBES_ETL_LOG_INTERVAL_S`) the ETL writes a heartbeat line with each
stage's throughput, the two internal queue depths, and whether each worker
process is still alive. A worker that died shows up here with its full
traceback even though the process itself exited silently otherwise.

Rollup runs log here too — the **log** links in the Rollup tab's Recent runs
jump into this sub-tab with the rollup command's file preselected.

### Tab: Rollup

The GUI front end to the [pre-aggregation workflow](#pre-aggregation-rollups)
— five sub-tabs, one per step of that workflow, plus a status view. Expert
level only. A rollup command and an ETL run share the same per-datasource
slot: starting one while the other is running is refused.

While a rollup command runs, a status strip inside the tab bar shows
`● rollup <advise|build|verify> running…`, then `✓ … finished` or
`✗ … failed`.

#### Status

The overview card: the current configuration (bucket width, core columns,
marginals, measures, total rollup size and the reduction factor against
fact), and a per-table list with *built from / built through* marks, rows,
size and a status badge — hover for the meaning:

| Badge | Meaning |
|-------|---------|
| `usable` | Serving the chart queries it covers |
| `empty` | Configured, nothing built yet |
| `not built` | No table state — run a build |
| `stale` | The configuration changed since these rows were built; every chart reads fact until a rebuild |

A banner appears when the stored configuration is not valid, and below the
retention floor the warning that the named span exists only in the rollups —
those tables belong in the backup.

**Recent runs** lists the last `rollup_advise` / `rollup_build` /
`rollup_verify` invocations, whether started from here or from a shell, with
status, times and last progress or error. Each has a **log** link, which
jumps into the ETL tab's Log sub-tab — rollup commands log to the same files
as ETL runs (see [Log](#log)).

#### Advise

The measurement behind `rollup_advise`, as a form: interval, window buckets,
target factor and — only if the current volume is not what you are sizing
for — an extrapolation. The report shows the per-dimension marginal costs,
the ladder of candidate column sets and the derivable measures;
**Use these** on a ladder stage fills the Config form with that column set.

#### Config

Bucket width, the measures beside `count(*)`, the core columns and the
marginals — the same decisions as `rollup_config`, with the pickers narrowed
for you: non-derivable (percentile) measures are hidden, since they can never
be pre-aggregated. **Save configuration** stores it (the rollups then need a
rebuild), **Turn pre-aggregation off** disables it without dropping anything.
The **Drop rollup tables** card removes them entirely — irreversible for
spans below the retention floor, where the rollup is the only copy.

#### Build

**Build now** advances the rollup to the present, **Backfill history** works
backwards from there in batches (stop-at timestamp, batch size, max
batches), and **Rebuild** is what a changed configuration needs. A progress
card follows the running build and offers Kill.

#### Verify

Runs the comparison matrix over the last N days at chosen intervals. The
report counts *passed · skipped · failed* — a *skipped* case is a query the
rollup cannot serve, which is the fallback working, not a fault.

### Editing and deleting a datasource

- **Edit**: click **Edit** in the Basics tab header (Expert level) to open
  the edit form. Same four cards as the create
  form, pre-populated with current values. Schema name changes do not rename
  the actual Postgres schema. The Source DSN field shows the current value
  as-is (it contains credentials).

- **Delete**: click the Delete button in the header. A confirmation dialog asks
  whether to also drop the Postgres schema. Check **Drop schema and all data**
  to cascade-delete the warehouse schema (`DROP SCHEMA ... CASCADE`); uncheck to
  keep the schema (you must drop it manually later). Deletion always removes
  all metadata: fact columns, dimensions, indices, and ETL history.

---

## Source configuration reference

This section explains what each configuration field does and how to choose
values for a new datasource.

### Identity fields

**Name** — A short identifier used in URLs, the chart's `?name=` parameter,
and as a human-readable label. Must be unique. Example: `mydb`.

**Schema Name** — The Postgres schema where warehouse tables (fact + dim
tables) are created. Must be unique. Example: `starcubes_mydb`. Changing
this in the GUI does **not** rename an existing schema — it only changes
where future operations look for tables.

**Dialect** — The source database dialect. Determines how the ETL engine
generates SQL (e.g. Oracle's `ROWNUM` vs. Postgres `LIMIT`, Oracle's
`to_timestamp` format strings, etc.). One of: `oracle`, `postgresql`,
`mysql`, `mssql`.

### Source connection fields

**Source DSN** — A SQLAlchemy connection URL for the source database.
Example: `oracle+oracledb://user:pass@host:1521/SID`. The password is
masked in the GUI. Use the **Test Connection** button to verify connectivity
before running ETL.

**Import Query** — A bare `SELECT ... FROM ...` that projects every fact
column you want to store. Must **not** include a WHERE clause — the ETL
engine adds the watermark range filter automatically. If left blank, the
engine composes the query from Base Table + per-column `source_expr` values.

**Base Table** — The `FROM` table used when Import Query is blank. Also used
as the default table for `watermark_max_query` and for the **Preview Import**
button's query. Example: `txn`.

**Small Window Hint** — A dialect-specific SQL hint injected after the first
`SELECT` when the ETL load window is within one day. For Oracle, this is
typically `/*+ ORDERED INDEX(txn, TXN3) */` to force index usage on small
time ranges. Leave blank for non-Oracle sources.

### Watermark and partitioning fields

**Watermark Column** — The source-side column used for incremental ETL
boundaries. The ETL loads rows where this column falls in `[last_hwm,
current_hwm)`. Typically a timestamp column like `end_time`. It does **not**
need to be in the SELECT list — the ETL adds it to the WHERE clause
separately.

**Partition Column** — The fact column used for monthly `RANGE` partitioning.
Usually the same as the time fact column (e.g. `end_time`). The partitioning
is declarative — the DDL creates monthly partitions automatically.

**Watermark Max Query** — An optional `SELECT max(<wm>) FROM <base_table>`
query. If blank, the ETL uses `SELECT max(<watermark_column>) FROM
<base_table>`. Provide a custom query when the max watermark is expensive
to compute on the base table (e.g. when the base table is a view or has
complex partitioning).

**Late Arrival Overlap (s)** — How many seconds the load window reaches back
*past* the high watermark on every run. Most source databases do not
guarantee that rows become visible in watermark order: a transaction can
commit after the ETL has already moved past its `end_time`, and with an
overlap of 0 that row is never seen again. With an overlap set, the ETL
re-reads that many seconds on each pass and drops the rows it already has by
primary key, so re-reading costs source-DB I/O but never duplicates data.

Requirements and trade-offs:

- A fact column with `role = 'pk'` is required. Without one the run fails
  rather than inserting duplicates.
- Default is `0`, i.e. the historical behaviour. Do not guess a value — run
  the ETL tab's **Audit** sub-tab over a representative window first (see
  below), see how far back rows actually turn up, and set the overlap above
  that.
- The cost is one extra source-side range scan per run. A few minutes of
  overlap on an indexed watermark column is usually negligible; hours are
  not.
- Blocks within the overlap span are excluded from the server-side plot
  cache, since a later run can still add rows there.

**Watermark Fact Expression** — SQL, evaluated against the *fact* table, that
reproduces the watermark value. Needed only when the watermark is not itself
a fact column. The example Oracle source watermarks on `end_time` but stores
`create_time` plus a `total` duration, so it sets
`create_time + total * interval '1 millisecond'`. Leave blank when the
watermark column *is* the partition column, which is the common case. Used
by the overlap re-fetch and by the Audit tab.

*Internals: see [`docs/etl-audit.md`](docs/etl-audit.md) for how the
three audit modes relate, and [`docs/etl-engine.md`](docs/etl-engine.md)
for the late-arrival overlap mechanism.*

### Finding and repairing missed transactions

The **Audit** sub-tab of a datasource's ETL tab compares row counts between
the source and the fact table, bucket by bucket, over a window you choose:

1. Pick **From** / **To** and a bucket count (max 200), then **Run Audit**.
   The source is counted on its watermark column, the fact table on the
   watermark fact expression (or the partition column).
2. Buckets where the source has more rows than the fact table are flagged as
   *missing*. A bucket whose source query failed shows `?` rather than a
   bogus gap.
3. **Backfill** on a row — or **Backfill all gaps** for the whole span —
   re-loads that window and inserts only the rows that are not already
   present, matched by primary key. It runs as a normal background ETL
   subprocess, so progress shows up in the ETL tab's **Log** and **History**
   sub-tabs.

**Compare IDs** is the precise follow-up, and worth reaching for even when
the counts look clean. Counting cannot tell a healthy bucket from one that
lost 100 rows and had 100 deleted upstream — the two cancel — and it cannot
see duplicate primary keys in the fact table at all. Comparing IDs pulls
both primary-key sets and reports three separate things:

- **Missing** — in the source, never loaded. Backfill inserts exactly these.
- **Surplus** — in the fact table, no longer in the source for this window.
  Usually deleted upstream after being loaded. Backfill will not remove
  them; that is a `Delete From` decision.
- **Duplicate** — the same primary key stored twice in fact. There is no
  unique constraint preventing it.

If `PK Format` is set on the datasource, the listed IDs are rendered with
it, so a format that produces a link takes you straight to the transaction
in the source system.

Comparing IDs holds both key sets in memory (roughly 46 bytes per ID per
side, measured) and is capped at 2 million rows per side. Above that it
refuses rather than exhausting the web worker — narrow the window, or raise
`STARCUBES_MAX_AUDIT_IDS`.

A backfill never moves the high watermark, so it is safe to run against old
windows while incremental loading continues. It does leave the dimension
`count` columns stale (they only affect the chart's stacking order) — run
**Refresh Counts** afterwards if that matters.

Use this both to repair known gaps and to *measure* the right value for
**Late Arrival Overlap**: audit a window that is already fully loaded, and
the oldest bucket that still gains rows tells you how far back the source
reorders.

### ETL performance fields

**Chunk Size** — Number of rows per extract chunk. The ETL reads the source
in chunks of this size, transforms each chunk in a child process, and copies
it to the warehouse. Default: 10 000. Larger chunks reduce overhead but use
more memory.

*Internals: see [`docs/etl-engine.md`](docs/etl-engine.md) for the
three-stage pipeline and throughput model, and
[`docs/star-schema.md`](docs/star-schema.md) for watermark and
late-arrival terminology.*

**Transform Processes** — Number of parallel transform workers. Each worker
receives a chunk, applies dimension lookups and type conversions, and
serialises it for the load stage. Default: 1. Increase for CPU-bound
transform workloads.

**Interval Ladder** — Comma-separated list of bar intervals in seconds,
finest to coarsest. The chart auto-selects an interval from this list based
on the current zoom level and the bar pixel target. Default: `1,60,3600,86400`
(second, minute, hour, day). Example alternatives: `60,300,3600,86400`
(minute, 5-minute, hour, day).

**Bar Pixel Target** — Minimum bar width in pixels. When the chart viewport
changes, it picks the coarsest interval from the ladder where bars would be
at least this wide. Default: 5. A higher value (e.g. 10) favours fewer,
wider bars; a lower value (e.g. 2) allows more, narrower bars.

### Fact column fields

Each fact column defines one column in the warehouse fact table:

| Field | Description |
|-------|-------------|
| Name | Column name in the fact table (e.g. `end_time`, `response`, `amount`) |
| PG Type | Postgres data type. Dimension columns should be `smallint` (they store dim table indices). |
| Role | `time` (timestamp, one per source), `pk` (primary key, one per source), `measure` (numeric value), or `dimension` (smallint FK into a dim table) |
| Ordinal | SELECT order. Determines column position in the fact table DDL. |
| Source Expr | SQL projection in the source DB. When Base Table is set, the import query is composed as `SELECT <source_expr>, ... FROM <base_table>`. Leave blank to use the column Name as-is. |

**Important**: dimension columns with role `dimension` should use PG Type `smallint`.
They store indices into dim tables, not raw source values. The ETL transform
stage maps source values to dim indices. The 32 767-value ceiling is a
deliberate tradeoff — high-cardinality columns do not belong as dimensions.

**Primary key columns** (role `pk`) are used for deduplication and for the
drill-down transaction ID feature in the chart. There should be exactly one
pk column per source.

### Dimension fields

Each dimension row maps a fact column to a dimension table:

| Field | Description |
|-------|-------------|
| Fact Column | The fact column this dimension maps to (e.g. `response` → `dim_response`) |
| Dim Table | Warehouse dimension table name (e.g. `dim_response`). Created by Import Dims. |
| Source Table | Lookup table in the source DB (e.g. `result`). Mutually exclusive with Inline Values. |
| Source Key | Key column in the source table (e.g. `result_id`). Maps to `source_id` in the dim table. |
| Source Name | Name column in the source table (e.g. `result_name`). Maps to `name` in the dim table. |
| Source Desc | Description column in the source table. Maps to `description` in the dim table. |
| Primary | Exactly one dimension row per dim table is primary. It drives DDL creation and dim import. Other rows with the same dim table just link their fact column. |
| Inline Values | CSV lines: `source_id,name[,description]`. Lines starting with `#` are comments. Mutually exclusive with Source Table. |

**Source-backed vs. inline**:

- **Source-backed**: set Source Table, Key, Name, Desc. Import Dims reads the
  source DB and populates the dim table. Example: a `result` lookup table
  with 20-30 rows in the Oracle database.
- **Inline**: set Inline Values. Import Dims parses the CSV and populates the
  dim table without touching the source DB. Example: currency codes
  (`978,EUR,Euro` / `840,USD,US Dollar`).

**Shared dimension tables**: when multiple fact columns map to the same dim
table (e.g. `so_peer_1`, `so_peer_2`, `so_peer_3` all reference `dim_peer`),
create one dimension row per fact column. Mark exactly one as Primary. The
primary row owns the DDL and the source mapping; the others just tell the
transform stage which dim table to look up for their fact column.

### Index fields

Indices define FK constraints and warehouse indexes on the fact table:

| Field | Description |
|-------|-------------|
| Column Name | The fact column to index |
| Method | Index method: `brin` (Block Range Index — best for time-series on large tables), `btree` (general-purpose), or `hash` (equality lookups) |

After the first ETL load, click **Create All Indices** to build them. BRIN
indices on the time/partition column are the default recommendation for
time-series fact tables.

To drop all indices (e.g. before a bulk re-load), click **Drop All Indices**
and type the datasource name to confirm.

---

---

## Pre-aggregation (rollups)

Every chart request aggregates fact rows anew. For the resolutions that span
weeks and months that is the slowest thing the system does, and pre-aggregation
removes it: a rollup table holds hourly (or daily) counts already grouped by a
chosen set of dimension columns, and the chart reads that instead.

Off by default. Turning it on is a deliberate sequence, and it is deliberately
*not* something to do right after the first load — which dimensions carry your
charts and which measures you actually use only becomes visible in use.

The same five steps are the sub-tabs of the datasource page's Rollup tab —
see [Tab: Rollup](#tab-rollup). The CLI commands below do the same from a
shell.

### 1. Measure first

```
manage.py rollup_advise --name <datasource>
```

Prints, per dimension column, how many distinct values it has, its **marginal
cost** (what including it multiplies the rollup by) and how often it has been
used as a breakdown. Then a ladder of candidate column sets with rows/day, size
and speed-up factor, and finally which of your measures a rollup could serve.

Read the marginal cost column first. One high-cardinality dimension routinely
costs more than all the others together — on the example schema a merchant
category column alone accounted for a factor of six.

Two flags matter:

- `--target-factor N` (default 20) — the suggestion is the **widest** column
  set that still reaches this factor, i.e. the most coverage for the size you
  asked for.
- `--extrapolate-to <rows_per_hour>` — only if the current volume is not what
  you are sizing for. On a datasource already carrying its real load, leave it
  off; the measurement is the answer.

If it warns about missing indices, run `manage.py create_indices` first.
Without them every measurement scans whole partitions and the factors come out
flattering.

### 2. Configure

```
manage.py rollup_config --name <datasource> --interval 3600 \
    --columns response,txn_type,card_type --marginals merch_cat \
    --measures 'sum(amount),avg(stay)'
```

- `--columns` — the core set. A chart query is served from the rollup when its
  breakdown **and every column it filters on** are in this set.
- `--marginals` — columns too expensive for the core set that still get their
  own small single-column rollup, so an *unfiltered* breakdown on them stays
  fast. Only a filter on them falls back.
- `--measures` — beside `count(*)`, which is always there. Measures are cheap
  (they widen the row, they do not multiply it), so take everything derivable.
  **Percentiles can never be pre-aggregated** — quantile measures always read
  the fact table.

`--show` prints the current configuration and each table's state, `--off`
disables it without dropping anything.

### 3. Build

```
manage.py rollup_build --name <datasource> --backfill
```

Works backwards from the present in batches. That matters on a large history:
the newest windows become fast first, so the run is useful long before it
finishes. Each batch commits, so Ctrl-C leaves a smaller but entirely valid
rollup and re-running continues where it stopped.

From here every ETL run advances it automatically.

### Rollups outlive retention

If you set `retention_periods`, the fact partitions age out but **the rollups
are left alone**. That is deliberate: their counts were computed while the raw
rows existed and stay correct, and at roughly a hundredth of the size keeping
them is nearly free. Retention can be aggressive on the fact table while the
chart keeps years of history.

Below the retention floor the chart therefore still draws counts and
breakdowns over the rollup's columns — the status line marks this with
*pre-aggregated before … — counts only, no drill-down* — while percentiles,
drill-down into transactions and filters on columns outside the rollup return
nothing, because those read the fact table. The chart's advertised time range
extends down to the rollup's oldest data, so the date picker offers the
rollup-only history rather than cutting off at the retention floor.

Two things follow that are worth knowing before you rely on it:

- **Back up the rollup tables.** The fact table is deliberately unbacked
  (it can be reloaded from the source); a rollup below the floor cannot.
- **`rollup_drop` and `rollup_build --rebuild` refuse that span** and tell you
  what they would destroy. `--force` overrides, and there is no way back.

### Changing your mind

Safe at any time. A changed configuration no longer matches what the stored
rows were built under, so the chart quietly goes back to the fact table until
you rebuild — slower, never wrong.

```
manage.py rollup_build --name <datasource> --rebuild
```

is what a changed column or measure list needs. Note the asymmetry: **removing
a dimension is cheap** (it re-aggregates the small rollup table), **adding one
is not** (it re-reads the whole fact table). When in doubt start wider and
narrow later.

`manage.py rollup_drop` removes the tables entirely; charts simply go back to
reading fact.

### Checking it

```
manage.py rollup_verify --name <datasource>
```

Runs a matrix of chart queries against both the fact table and the rollup and
compares the results cell by cell. Cases the rollup cannot serve count as
*skipped* — that is the fallback working, not a fault.

## Chart GUI

The chart page at `/` (or `/viz/`) renders stacked-bar timeseries with
filtering, drill-down, and follow mode. It supports multiple datasources
with per-source state isolation.

### Source tabs

The navigation bar shows one tab per datasource. Clicking a tab switches
the active source. The chart preserves per-source state:

- **localStorage** (`starcubes-state-<name>`): stores group_by, measure, filters,
  and viewport for each source.
- **In-memory plot cache** (`sourceMemory`): stores non-trailing plot data so
  switching back to a previously visited source restores the canvas instantly
  without re-fetching from the server.
- **Trailing-edge blocks** (data near "now") are always re-fetched fresh and
  never cached, because they change between visits.

When no cached data exists for a source, a loading overlay covers the chart
until the first data arrives.

### Control bar

The bar is three groups, separated by hairlines: **what to plot**, **which
time window**, and **what to open**. Every icon button names itself on hover
and on keyboard focus.

| Control | Description |
|---------|-------------|
| Group By | Dropdown of dimension columns, each followed by its group dimensions (see the Group Dims tab). A group dimension is listed under its own name, prefixed with the underlying column (`peer → Peer countries`). |
| Filters | Show/hide the filter panel. The arrow points down while it is open. |
| Measure | Aggregation: `count(*)` (default) or `sum`/`avg`/`min`/`max` of a measure column. |
| From / To | Datetime fields defining the chart viewport. Type a value (`YYYY-MM-DD HH:MM`) and press Enter to commit and jump to the other field, or click the calendar icon in the field to pick one. |
| Today / Yesterday / Week / Month | Quick ranges. Boundaries are local midnight, the end is exclusive, and the week starts on Monday. The chip stays highlighted only while the window matches it exactly — pan or zoom by any amount and it goes out. |
| Legend | Opens the legend panel showing per-value totals. |
| Follow | Toggles follow mode (seismograph-style auto-pan to "now"). The dot in the corner of the button pulses while it is on. |
| Query | Opens the ad-hoc SQL console over the chart (logged-in users only). See [Query console](#query-console). |
| Keys | Opens the keyboard-shortcut panel. `?` does the same from anywhere on the page. |
| Config | Jumps to `/sources/<id>/` for the source whose tab is active (logged-in users only). |

### The menu (☰)

The top bar carries the source tabs and, on the right, a single menu button.
The entries are the same as in the sources app's menu:

| Entry | Does |
|-------|------|
| Datasources | Opens the datasource overview at `/sources/`. |
| Firings | The global threshold-breach episode overview. |
| Profiles / Users | Superuser-only: the profile catalogue and user admin. |
| Preferences | Y-axis mode, latest-bar trimming and the bar pixel target. See [Preferences](#preferences). Available logged out too — they are browser-local settings. |
| Theme | Switches light/dark. The menu deliberately stays open so the two can be compared; the choice survives a reload. |
| My Profile | Own profile, password change, query history (`/sources/me/`). |
| About starcubes | Opens the slide deck in a new tab. |
| Log out / Log in | |

The menu opens on click or `↓`, moves with `↑`/`↓`, and closes on Escape or a
click outside.

### Filters

Click **Filters** to expand the filter panel. Each filter row has:

| Control | Description |
|---------|-------------|
| AND/OR | Click to toggle. OR groups are highlighted amber. Top-level rows default to AND. |
| Indent / Dedent | Triangle brackets to nest filter rows into logical groups. |
| Column | Dropdown of dimension columns, plus one entry per group dimension. Filtering on a group dimension offers its group names and `(other)` as values. |
| Operator | `is in` (green), `is not in` (red), `starts with` (blue), `starts not with` (amber), `contains` (purple), `does not contain` (pink). |
| Value | For `is in` / `is not in`: tag pills with search dropdown. For text operators: text input with 300ms debounce. |
| Remove | X button to delete the filter row. |

**Collapsed state**: when the panel is collapsed, active filters are shown as
summary tags with remove buttons. OR groups are wrapped in parentheses.

**Add filter**: click **+ Add filter** to append a new row.

Filter state is serialised to URL parameters (`filter=`, `andor=`, `indent=`)
and saved to per-source localStorage.

### Follow mode

Toggle the **Follow** button (or click when the chart is active) to enable
seismograph-style auto-panning. When active:

- The chart continuously pans right to keep the viewport edge at "now".
- Trailing-edge data is polled at `max(5s, interval/3)` intervals.
- Polling only fires when the viewport right edge is within 2x the bar
  interval of the current time — scrolling to historical data stops network
  traffic.

### Keyboard shortcuts

Press `?` (or the keyboard button in the control bar) for this table inside
the app.

Most of these keys belong to the chart itself and only fire **while the plot
has keyboard focus** — click the chart once, or Tab to it. The page keys at the
bottom work anywhere except inside a text field.

**Navigation** (chart focused)

| Key | Action |
|-----|--------|
| `←` / `→` | One window back / forward |
| `Shift`+`←` / `→` | One grid cell back / forward |
| `↑` / `↓` | Zoom in / out, halving or doubling the window |
| `Shift`+`↑` / `↓` | One cell narrower / wider |

**Calendar** (chart focused) — relative to the unit the *middle of the current
window* falls in, not to the present

| Key | Jumps to |
|-----|----------|
| `t` | Today, 00:00–24:00 |
| `d` | That day |
| `w` | That ISO week, Monday to Monday |
| `m` | That month |
| `y` | That year |

**Follow mode** (chart focused)

| Key | Anchors "now" at |
|-----|------------------|
| `f` | The right edge |
| `p` | The left edge |
| `c` | The middle |
| `F` / `P` | The same, but the window is stretched onto "now" first |

**Display** (chart focused)

| Key | Toggles |
|-----|---------|
| `l` | The legend panel |
| `g` | Grid snapping for the arrow keys. Nothing moves when you press it — it changes what the *next* arrow press does. |

**Page**

| Key | Action |
|-----|--------|
| `?` | Open / close the keyboard-shortcut panel |
| `Escape` | Close the front-most menu or panel |
| `Enter` | In *from* / *to*: commit the value and jump to the other field |
| `Ctrl`/`Cmd`+`Enter` | In the query console: run the statement |
| `↑` / `↓`, `Enter`, `Delete` | In the query history: select, load, remove |

Panning uses local-time calendar snapping, not fixed millisecond offsets.
This means a "day" pan across a DST boundary is exactly 23 or 25 hours of
wall-clock time, preserving visual alignment.

### Legend

Click **Legend** in the control bar to open the floating legend panel:

- Sorted by total count (descending).
- Click a color swatch to select/deselect that dimension value.
- Selected values can be bulk-filtered via the **Filter** button.
- **CSV** button copies all legend entries (name + total) to the clipboard.
- The legend is draggable.

### Drill-down

Click any bar on the chart to open a drill-down modal:

- **Header**: dimension name (clickable to add as a filter), value, interval,
  date.
- **Common section**: dimensions with exactly one value in the time slot
  (clickable to add as filter).
- **Per-dimension breakdown**: top-N values with counts (clickable to add as
  filter).
- **IDs button**: shows up to 1000 primary key values for the clicked slot.
  "Copy all" copies them to the clipboard.
- **Alert button**: creates a real threshold alarm on this datasource. The
  context is the chart's active filters plus the clicked series; pick a
  name, metric, condition, threshold and window. The alarm is evaluated by
  a background loop, and every breach period (first to last crossing) is
  recorded on the datasource's Alarms tab (detail page, ⓘ button). Alerts
  need a group-dimension-free view: grouped filters cannot be stored in an
  alarm (group sets are editable and would silently change its meaning).

The modal is draggable. Press Escape to close.

### Query console

Click **Query** in the control bar to open a floating panel over the chart. It
is visible to logged-in users only.

The panel runs one SQL statement at a time against the **warehouse schema of
the currently selected source** — the same `starcubes_<name>` schema the chart
reads, so `fact`, the dimension tables and the partitions are all directly
addressable by their bare names (the connection's `search_path` is already set
for you).

**What you can run.** Exactly one statement, and it must start with `SELECT` or
`WITH`. Semicolons inside strings, dollar-quoted bodies and comments are
understood, so `SELECT 'a;b'` is fine; `SELECT 1; SELECT 2` is not. The
connection is opened read-only and the transaction is rolled back afterwards,
so nothing you type can modify data — a `DELETE` hidden inside a `WITH` clause
is refused by Postgres itself.

That read-only guarantee is about *writes*, not about cost: the console can
still read every table in the schema and can still run an expensive query. It
is a tool for trusted operators, not a public endpoint.

**The action row** is five icon buttons — Run, Cancel, Chart SQL, Format and
CSV — each of which names itself on hover.

**Chart SQL** copies the SQL behind the active chart legend to the clipboard —
the query the chart itself ran, with the window snapped to bar edges. When
pre-aggregation serves the view it may read a rollup table instead of `fact`;
the status line names the table it copied
(`copied chart SQL (fact)` vs. `copied chart SQL (rollup_1h …)`).

**Running and cancelling.** Press **Run** (▶) or `Ctrl`/`Cmd`+`Enter`. Plain
`Enter` still inserts a newline, so multi-line SQL types normally. Nothing runs
while you type. **Cancel** genuinely stops the query on the server, not just in
the browser — it works even if another worker process picked up the request.
Queries are also killed automatically after 30 seconds
(`STARCUBES_QUERY_TIMEOUT_MS`).

**Results** are capped at 1000 rows (`STARCUBES_QUERY_ROW_LIMIT`); the status
line says so when a result was truncated, and also reports the row count and
elapsed time. `NULL` is shown in italics, numbers are right-aligned, and the
full value of a clipped cell is in its tooltip.

**Format** re-indents the statement in the editor: main clauses on their own
lines, one item per line in a multi-item `SELECT` / `GROUP BY` / `ORDER BY`,
subqueries indented two spaces, keywords upper-cased. It never touches the
inside of a string, a dollar-quoted body, a comment or a `"quoted identifier"`,
and formatting an already-formatted statement changes nothing. `Ctrl`+`Z`
takes it back.

**CSV** downloads the current result as a file (`starcubes-<source>-<timestamp>.csv`).
It is disabled until a query has returned something. `NULL` becomes an empty
field; commas, quotes and line breaks inside values are quoted per RFC 4180,
so the file opens correctly in Excel, LibreOffice and `csv.reader`. The file
carries a UTF-8 BOM, which is what makes Excel read non-ASCII values
correctly.

**History** is stored per user and per source on the server, so it follows you
between browsers. Expand **History** at the bottom of the panel to see every
statement you have run against this source, newest first, with the date it last
ran. Click a row to load it back into the editor; click `×` to remove it.

The list is keyboard-navigable. `Tab` into it (expanding **History** focuses it
for you), then:

| Key | Action |
|-----|--------|
| `↑` / `↓` | Move the highlight. It stops at the ends rather than wrapping. |
| `Home` / `End` | Jump to the newest / oldest entry. |
| `Enter` | Load the highlighted statement into the editor and focus it. |
| `Delete` / `Backspace` | Remove the highlighted entry. The highlight stays put, so repeated presses walk down the list. |

`↑`/`↓` inside the SQL editor still move the caret between lines — the history
keys only apply while the list itself has focus.
Running the same statement again updates its date rather than adding a second
entry, and only the 100 most recent are kept. A query that failed or timed out
is still recorded — usually that is exactly the one you want back.

**Panel chrome.** Drag the header to move the panel, drag the bottom-right
corner to resize it. Position, size, open state and whether History is expanded
are remembered in this browser. `Escape` deliberately does *not* close the
panel — it would discard whatever you had typed; use the `×`.

*Internals: see [`docs/chart-app.md`](docs/chart-app.md), "Query console",
for how the four read-only layers and cross-process cancellation are wired.*

### Preferences

**Preferences** in the ☰ menu (top right) opens a popover. It is available
logged out too — every setting in it is browser-local.

**Y axis** — the chart's y-axis is a *density*, so bars of different time
resolutions stay the same height and only the numbers change when the chart
switches resolution. This picks which unit those numbers are in:

| Option | Axis |
|--------|------|
| Rate, unit follows zoom (default) | `txns/min` while minute bars are drawn, `txns/h` once hourly bars take over — the numbers change with a short dissolve, the bars do not move |
| Rate, fixed per second/minute/hour/day | never switches unit; useful when comparing two zoom levels by eye |
| Count per bar | the raw count in each bar; the axis then rescales when the resolution changes |

The setting applies to counts and sums only — an average or a percentile is
already per-unit and is never rescaled. Tooltips, the legend and the drill-down
always show absolute counts; the tooltip adds the rate (`≙ 9.32/min`) when it
differs from the count. The choice is remembered in the browser and can be put
in a shared link with `?yaxis=auto|s|min|h|d|count`. No account needed.

**Bar pixel target** — the source ships with a default (set via **Sources →
Edit**). The chart's preferences popover lets every visitor override it for
the current browser via **Save** (stored in `localStorage['starcubes-bar-target']`)
or clear it via **Reset** (falls back to the source default). Per-browser,
no account needed.

### URL state and bookmarks

All chart parameters are serialised to the URL query string:

```
?name=mydb&group_by=response&measure=count(*)&from=2026-04-27+00:00&to=2026-04-27+12:00&filter=response:is_in:ok&andor=AND&indent=0
```

- `name` — datasource
- `group_by` — dimension column
- `measure` — aggregation function
- `from` / `to` — viewport time range
- `filter` / `andor` / `indent` — active filters
- `yaxis` — y-axis mode (omitted when it is the default `auto`)

Bookmarking or sharing this URL restores the exact chart state. The browser
back/forward buttons also work — `popstate` events re-apply the saved
parameters.