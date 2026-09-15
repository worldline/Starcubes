# viz/chart.html — multi-source UI deep reference

The chart page supports multiple datasources with full per-source isolation.
Read this when editing `viz/templates/viz/chart.html`, `viz/views.py`, or
anything that touches `starcubesPush` / `loadedBlocks` / `sourceMemory`.

## UI primitives

Four things everything else on the page is built from, all inline in
`chart.html` except the sprite. Inline is deliberate: the project runs without
a manifest static-files storage (`starcubes/settings.py`), so an external
`ui.css` would be served with no cache-busting hash.

- **`viz/templates/viz/_icons.html`** — a hand-drawn `<symbol>` sprite,
  `{% include %}`d right after `<body>`. Icons are
  `<svg class="ic"><use href="#ic-…"></use></svg>`; `.ic` sets `fill: none;
  stroke: currentColor`, both inherited properties, so a symbol needs no
  dark-mode variant — it takes the colour of whatever button it sits in. No
  icon font, no CDN, no licence question. Kept as its own template so
  `sources/` can include the same sprite later.
- **`.ibtn`** — the icon button (28 px square, 15 px glyph), with `:hover`,
  `.active`, `:disabled` and a `:focus-visible` ring. Variants: `.ibtn--plain`
  (borderless, for the top bar), `.ibtn--primary` (accented, for Run). It
  replaced four near-identical ghost-button declarations that had drifted
  apart. `.chip` is the same metrics in text form, and `.chip-group` welds a
  row of them into one segmented control.
- **`[data-tip]`** — a CSS tooltip, `::after` for the bubble and `::before`
  for the arrow, `data-tip-pos="above"` to flip it, `data-tip-align="end"`
  for triggers at the viewport's right edge (a centred tooltip there would
  create a horizontal scrollbar; those anchor to the trigger's trailing edge
  instead). It exists because `title` waits ~700 ms, cannot be themed, cannot
  wrap and never appears on keyboard
  focus. **`data-tip` is not a label**: every icon button also carries an
  `aria-label` with the same string.
- **`makePopover(btn, panel, opts)`** — from
  `sources/static/sources/nav.js`, shared with the sources shell (the page
  used to carry its own copy). `closeTopPopover()` comes from there too.
  Anchored dropdowns: toggle, outside
  click, Escape, placement (flipping above the anchor when the viewport is
  tight), `aria-expanded`, and `menu: true` for arrow-key roving over
  `[role=menuitem]`. Used by `#nav-menu` and `#prefs-popover`. Deliberately
  **not** a `.fpanel` — a dropdown is anchored, never dragged, never resized
  and not part of the click-order stack. The shared Escape handler asks
  `closeTopPopover()` first, so one key press closes one thing.

## Nav bar

Single `#nav-tabs` flex row: `[Source1] [Source2] … [spacer] [☰]`. Source tabs
are injected before `#nav-spacer` by `loadSources()`; clicking one calls
`setActiveSource(name)`. The row clips (`overflow: hidden`) rather than
scrolling: tabs shrink with ellipsis (`.source-tab-label`, full name on
`btn.title`) and the brand and `#nav-menu-toggle` never give up space.

Everything else collapsed into `#nav-menu`, a `makePopover` dropdown behind
`#nav-menu-toggle`: username header (display only), Datasources
(`sources:list`) and Firings (`sources:alarm_firings`) for any logged-in
user, Profiles / Users (superuser-only), Preferences, Theme, My Profile
(`sources:me`), About starcubes, Log out. Anonymously it shows Preferences,
Theme, About and Log in — the y-axis preferences are client-side and stay
reachable without an account. **The rows are the shared partial**
`sources/templates/sources/_nav_menu.html` (included with
`include_prefs=1` and the chart's theme-row ids) — the sources app
includes the same partial with its defaults, so the menus cannot drift;
only Preferences is the chart-only extra.

Three things that break if they are touched carelessly:

- **Logout must stay a POST** (Django's `LogoutView` requires it). The `<form>`
  is hidden in the bar and the menu row is a `<button type="submit"
  form="logout-form">`; the `form` attribute is what lets it live outside.
- **`#theme-toggle` keeps its id and its handler**, but it is a menu row now.
  `applyTheme()` no longer writes `textContent` — `setThemeToggleUi()` swaps
  the `<use href>` and the label, both naming the theme the click switches
  *to*. The row carries `data-keep-open`, so the menu stays up and the two
  themes can be compared without reopening it.
- The theme is still applied before paint by the inline `<head>` script and
  persisted in `localStorage['starcubes-theme']`.

## Source control bar (`form#ctrl`)

Three groups separated by `.ctrl-sep` hairlines, wrapping between groups
rather than inside one:

```
[group by ▾] [Filters] [measure ▾] │ [from 🗓] [to 🗓] [Today][Yesterday][Week][Month] │ [≡][~][⫽][>_][⌨][⚙]
```

The last group is the icon buttons — Legend (≡), Follow (~), Clamp outliers
(⫽), Query console (>_, logged-in only), Keyboard shortcuts (⌨) — then the
three configuration links (Group Dims, Measures, Alarms bell, logged-in only).
The Clamp button toggles outlier clamping (new in timeseries.js 0.10.6); the
library has no callback for it, so the button re-syncs its active state from
`ts.getClampOutliers()` inside the `onViewportChange` tracker.

- **from / to** are the same `DateTimePicker` they always were — the class now
  lives in `sources/static/sources/datetimepicker.js`, shared with the sources
  shell and carrying both sides' features (this page's `trigger` option and
  viewport clamping, the shell's `getUTCISO()`). It was
  fully built and simply invisible. Input and calendar button now share one
  `.dtp-field` frame, so the field looks like the picker it is while keyboard
  entry keeps working. The button is passed to the picker as `trigger`, which
  exempts it from the outside-click dismiss (otherwise a press would close and
  instantly reopen).
- **Range presets** set half-open `[start, end)` bounds in local time, week
  starting Monday (matching the picker grid and `naturalBlock`'s `isoweek`).
  Day arithmetic goes through `setDate()`, so a span across a DST switch is 23
  or 25 hours. A chip is `.active` only while the window matches its range
  exactly.
- **`applyRange(fromMs, toMs)`** is the single entry point for a host-driven
  viewport change — both date fields and all four presets. It zooms once and
  fires one fetch cycle. Before it existed each `DateTimePicker.onChange`
  called `resetAndFetch()` itself, so a change moving both edges ran two full
  cycles.
- **Three configuration links** — Group Dims (`#source-groupdims-link`),
  Measures (`#source-measures-link`) and the Alarms bell (`#alarms-link`),
  logged-in only. They follow the active tab in `setActiveSourceTab()` and are
  built together in `updateSourceLinks()`, which `gatherParams()` calls — the
  one point every filter change passes through. Their URLs are reversed from
  `sources/urls.py` through a sentinel pk (`{% url 'sources:detail' 987654321 %}`),
  not assembled as `'/sources/' + id + '/'`, which used to break silently
  whenever the route moved. There used to be a single Config link landing on
  Basics; the two tabs a chart user actually edits are worth their own doors,
  and Basics is read-only anyway.
- **All three carry the live filter** as an alarm context, so the context
  survives any trip from the chart into the configuration, not just the bell's.
  The bell adds `new-alarm=1` and lands on `?tab=alarms` with the add form
  open; Group Dims and Measures land on their own tab and leave the context
  waiting in a collapsed card above the alarm table (see
  [`docs/sources-app.md`](sources-app.md)).
  `alarmContextParam()` decides how the context travels **once** for all three
  — they share one `sessionStorage` key, and a context that goes by URL for one
  link must not go by session for the next. A filter row over a group set
  cannot be an alarm context and travels as `context-blocked=grouped`; a
  context past `ALARM_CTX_URL_MAX` (4000 chars) goes through
  `sessionStorage['starcubes-alarm-context']` as `context=session`, and if that
  fails too, `context-blocked=toolong`. `back=` (the chart's own query string)
  is appended last and only if it fits.
  The bell used to point at the firing episodes; that view is still one click
  away from the menu and from the Alarms tab's own per-datasource link.
- **The return trip** is `chartUrl()` in `sources/templates/sources/_alarms_js.html`:
  every alarm row links back into the chart with its stored `context_filter`
  re-serialized as `filter=`/`andor=`/`indent=`, mirroring
  `buildHistoryParams()`. It always writes `group_by`, even empty — the boot
  IIFE restores per-source state from localStorage exactly when the URL has
  none, and that restore would replace the filter rows it just handed over.

## Hover tooltip

The tooltip card is **not** starcubes's any more — it comes from the library via
`TimeSeries.attachTooltip(ts, …)` in `attachBarTooltip()`, which owns the DOM,
the cursor-following placement and the theming. starcubes supplies only the two
things the library cannot know:

- `labelFor` — strips the backend's `_` prefix and runs `_resolveDimName()`, so
  the card shows the dimension label rather than a raw `source_id`.
- `colorFor` — `_seriesColor()`, which prefers the server's `starcubesSeriesColors`
  group map over the library's golden-angle fallback.

Everything else (swatch, `(value · interval)`, timestamp, edge flipping) is the
library default. Theming rides on the `ts.setColors()` call `applyTheme()`
already makes: the library's `tooltip*` palette keys carry the same values the
old `--legend-*` variables did, so light and dark look unchanged.

`attachBarTooltip()` returns a controller kept in `barTooltip`; `openDrillModal()`
calls `barTooltip.hide()` when a click opens the modal.

Note `_barHeaderHtml()` and the `.btt-*` CSS classes **stay** — the drill-down
modal header (`#drill-header-info`) still renders through them, as does
`fmtInterval()` for the alarm UI. Only the `#bar-tooltip` element, its CSS and
`showBarTooltip()` were removed.

The drill-down's Alert form is real since 0028: `openAlarmView()` renders it,
`alarmContextRows(includeDrill)` serializes the active filter rows plus the
clicked series (the `context` POST field — `includeDrill === false` leaves the
series out, which is what the control bar's bell asks for), and
`saveAlarmForm()` POSTs to `sources:alarm_add` (CSRF via cookie / page token,
409 and 400 shown inline — see `docs/alarm.md`).

Only a grouped **filter** blocks the save (`alarmContextBlocked()` /
`alarmBlockedCols()`); a grouped **group_by** is not stored anywhere, so it
merely drops the clicked series and says so (`alarmIgnoredGrouping()`). That
is the same line the server draws.

`checkAlarmDuplicates()` asks `sources:alarm_match_context` for alarms already
watching this context with this metric and lists them above the save button.
Advisory only — two alarms over one slice are the normal case — and it follows
the metric dropdown. Both alarm endpoints reverse through a sentinel pk
(`alarmAddUrl`/`alarmMatchUrl`), like `sourceDetailUrl`.

## Bar selection

Clicking a bar both opens the drill-down panel and marks the bar: the library's
persistent selection (`ts.setSelection({ slotSec, key })` in `openDrillModal()`)
draws a 2px outline in the `selection` palette colour, and it stays until
`closeDrillModal()` clears it (`ts.clearSelection()`), or another bar is clicked.
`closeDrillModal()` is the single close path — Close button, Escape (via the
shared panel handler) all funnel through it, so one `clearSelection()` line is
complete. No outside-click close exists anymore.

`drillAddFilter()` deliberately does nothing to the selection: the panel stays
open and `resetAndFetch(true)` runs, and the library re-resolves the outline
every frame — a filter change that removes the bar removes the outline with it;
one that keeps it keeps the outline. That is the whole reason no validation
logic lives in chart.html. (`_drillCtx` additionally stores `selKey` — the raw
click key with its `_` prefix — so a future feature can re-anchor the panel
without reconstructing it from `rawKey`.)

The momentary fill highlight the library's default click handler would draw is
deliberately not used: it would overpaint the persistent outline and vanish on
the next poll.

## Init and the start window

`initTs()` builds the `TimeSeries` instance, and by then the window it should
show is already decided: the boot IIFE awaits `loadSources()` and `loadMeta()`
first, and `loadMeta()` sets `currentViewport` to today 00:00 → tomorrow 00:00
whenever it is still null; `applyUrlParams()` then lets `?from=`/`?to=` override
it. `resetAndFetch()` is the second entry point and runs just as late.

`initTs()` stashes `_initTmin`/`_initTmax` before the `new TimeSeries({...})`
call and hands them to the constructor as **`initialView`**, a `[tmin, tmax]`
pair the library applies synchronously before its first paint (≥ 0.10.2). The
capture has to happen first because constructing the instance fires
`onViewportChange`, whose handler overwrites `currentViewport`.

This replaced an older post-construction correction, `ts.zoom(_initTmin,
_initTmax, 0)` followed by `ts.stop()`. Back then the library always came up on
its own `tmin = now - 24h` / `tmax = now` default and painted it before the
constructor returned, so the window had to be fixed afterwards — and the `0`
was the whole point: `zoom()` without a duration animates over `zoomDuration`
(500 ms), so the page opened on "last 24 hours" and visibly slid to the real
window. Worth knowing because the failure mode survives the mechanism: anything
that makes the start window arrive *after* the first frame — passing no
`initialView`, or reintroducing a zoom behind an `await` / `setTimeout` /
`requestAnimationFrame` — brings the visible jump straight back.

`_initialView` is `null` when either bound is missing (unreachable today — both
callers run after `loadMeta()`), and the library then falls back to its own
`'last24'` default rather than to an unpainted canvas.

There is deliberately **no bootstrap instance**. `initTs()` used to construct a
throwaway `TimeSeries` on an off-screen canvas before calling
`TimeSeries.registerSource(...)`, from a time when the statics only existed once
an instance had been built. They have been module-scope since 0.9.1
(`registerSource`, `themes`, `attachTooltip`, `seriesColor` — every static the
chart uses), and the throwaway instance was actively harmful: it passed no
`initialView`, so it got the `'last24'` default, which starts a self-rearming
`follower_tick` loop on a canvas that `tmp.remove()` had already detached — one
`plotAll()` per tick for the lifetime of the page.

## Per-source localStorage state

`starcubes-state-{name}` stores
`{ group_by, measure, measure_name, filterRows, viewport }`.

- `saveSourceState(name)` writes to localStorage (called before leaving a source)
- `applySourceState(name)` reads and restores. Validates options exist before
  setting; reconstructs filter `Set` objects; sets `currentViewport` from the
  saved window.
- **Read on both paths**: the source switch in `setActiveSource()`, and the boot
  IIFE — the latter only when the URL carries no `group_by`.
  `buildHistoryParams()` writes that parameter unconditionally, so its absence
  marks a URL that names a source and nothing else (the brand link back from
  `/sources/`, or a bare visit); a shared link carrying filters still wins.
  Until this existed, booting went through `applyUrlParams()` alone — which
  clears `filterRows` unconditionally — so switching tabs restored state while
  reloading the same URL did not.
- Order matters: `applySourceState` runs **before** `initTs()`, which pins its
  first frame to `currentViewport` (`initialView`). Restoring afterwards would
  paint today's default window and then jump.
- Filter columns are normalized on restore via `normalizeFilterCol()` — a
  saved member fact column (`so_peer_1`) rewrites to its fan-out group token
  (`so_peer`). Applies to both restore paths (localStorage and URL params);
  the URL path additionally accepts old member tokens *and* the group token.
  The meta API's `dim_filters` payload
  (`[{token, label, dim_table, fact_cols}]`) drives `getFilterColumns()`:
  group members collapse into one merged dropdown entry, non-grouped
  dimensions keep their per-column rows, and the value list for a merged
  entry comes from `currentMeta.dimensions[dim_table]` as usual. The server
  still accepts individual member fact columns as filter cols (single-column
  fragment unchanged) — the merged token is what the UI offers.

## Per-source plot memory (`sourceMemory`)

`sourceMemory` is an in-memory `Map(name → { plots: Plot[] })`. It enables
instant canvas restoration when switching back to a previously-visited source.

- **`sourceMemoryPut(name, plot)`** — upserts a plot keyed by
  `interval_start|interval`. Called from `fetchRange` for every non-trailing
  block that is also HWM-final (trailing blocks are volatile and always
  re-fetched from the server; a non-trailing block whose own `interval_end`
  outran the ETL's HWM is provisional and must not be memoized either — see
  "Provisional blocks and the ETL high-water mark").
- **`sourceMemoryRestore(name)`** — pushes all stored plots to `starcubesPush`,
  then reconstructs `loadedBlocks` from the plots (same extent-computation
  logic as `fetchRange`). Returns `true` if plots were available.
- **Invalidation**: `resetAndFetch()` calls
  `sourceMemory.delete(starcubesParams.name)` immediately after `gatherParams()`.
  Covers all param-change paths (group_by, measure, filter changes,
  popstate). Source-switch does NOT call `resetAndFetch` when memory is
  available — it calls `autoFetch()` directly to fetch only the trailing edge.

## Source-switch flow

**`setActiveSource(name)`**:

1. `saveSourceState(prev)` — persist current source's form state to localStorage
2. `ts.clearAll()` — wipe canvas immediately (prevents old source data showing
   under new source's overlay, and prevents mixed data in `ts.data[]`)
3. If `starcubesPush && sourceMemoryRestore(name)` → canvas re-populated instantly,
   no overlay; call `loadMeta`, `applySourceState`, `gatherParams`,
   `updateLegend`, `history.pushState`, `autoFetch` (trailing edge only)
4. Else → `showLoadingOverlay()`, then `loadMeta`, `applySourceState`,
   `resetAndFetch`

## Loading overlay

`#loading-overlay` is an absolutely-positioned full-cover `div` inside `#wrap`
(`position: relative`). Hidden by default (`display: none`), shown via
`.visible` class (`display: flex`). `showLoadingOverlay()` /
`hideLoadingOverlay()` toggle it.

- Shown on initial page load (IIFE) and on first visit to a source (no memory)
- Hidden on first `starcubesPush` in `fetchRange` (`hideLoadingOverlay()` after push)
- Hidden on fetch error in `autoFetch` catch block (canvas cleared first)

## Load indicator timing

The other loading indicator — the determinate stripe on `#info::before` — is
ref-counted separately from `#loading-overlay`, driven by wrapping
`_autoFetchOnce` (the actual per-call body; `autoFetch` itself is only the
coalescing shell below) and `fetchRange`. Its visibility is gated by a
400 ms show delay and a 500 ms minimum-visible duration, not by the raw
ref-count: follow mode drives `autoFetch` every 300 ms via
`scheduleViewportChange`'s throttle, and most of those calls settle in well
under that, so an ungated ref-count strobed the bar three times a second. A
genuine multi-block load (zoom-out, source switch, cold cache) exceeds the
delay and shows steadily; once shown it can't blink back off inside the
minimum-visible window. The coverage fraction (`--info-progress`) keeps
updating on every wrapped call regardless of visibility, so the bar is never
stale when it does appear.

`autoFetch` itself **coalesces** rather than aborts: while a call is in
flight, a re-trigger just marks "run again with the latest viewport" and
returns, instead of calling `activeController.abort()`. Follow mode's 300 ms
cadence made the old per-call abort a livelock on any backend slower than
300 ms — the in-flight fetch was killed before it could ever complete.
`abort()` survives as `abortInFlight()`, called explicitly only from
`resetAndFetch` and `setActiveSource` (both branches) where a genuine
param/source change makes a stale in-flight response actively wrong:
`fetchRange` stamps `_starcubesGroupBy`/`_starcubesMeasure` *after* its
`await`, so an unaborted response landing after a switch would be
mislabelled with the new params and painted onto the just-cleared canvas.

## Query console

A floating, resizable ad-hoc SQL panel, toggled by the terminal icon button
that sits right of the Clamp button in `form#ctrl`. Its action row is five
`.ibtn`s — Run, Cancel, Chart SQL, Format, CSV — followed by `#query-status`
and the `Ctrl+⏎` hint. Both button and panel are wrapped
in `{% if user.is_authenticated %}` — anonymous visitors get neither, and the
whole JS block early-returns on a missing `#query-panel`.

**Target.** The warehouse schema of the currently selected source, reached via
`Source.console_engine()` (`core/source.py`) — a *separate* cached pool from
`dest_engine()`, so a slow ad-hoc query cannot starve `plot_json` of pooled
connections. Backend logic lives in `core/adhoc.py`; `viz/views.py` is a thin
adapter.

**Read-only is four layers**, each redundant because each alone has a hole:

1. `console_engine()` passes `-cdefault_transaction_read_only=on` as a libpq
   startup option, so it holds even without the per-transaction SETs.
2. `run_query` issues `SET TRANSACTION READ ONLY` first — it must precede the
   transaction's first query, and `SELECT set_config(...)` already counts.
3. `adhoc.validate_sql` rejects multi-statement text and anything not starting
   with `SELECT` / `WITH`. It is a lexer (quote-, dollar-quote- and
   nested-comment-aware), not a parser.
4. `stream_results=True` makes psycopg2 open a named server-side cursor, and
   Postgres refuses `DECLARE CURSOR` over a data-modifying CTE — the one
   writing shape that gets past layer 3, since
   `WITH x AS (DELETE … RETURNING 1) SELECT * FROM x` legitimately starts with
   `WITH`.

Measured, so nobody re-derives it: a multi-statement string that bypassed layer
3 is **not** refused by the cursor (`SELECT 1; SELECT 2` runs), but every write
inside one is still rejected by layers 1+2 with SQLSTATE 25006. Layer 3 is the
only thing enforcing "one statement"; layers 1+2 are the only thing enforcing
"no writes". None of it substitutes for a `SELECT`-only Postgres role — see the
Backlog note in `docs/backlog.md`.

**Cancel is cross-process, via `pg_stat_activity`.** uWSGI runs
`processes = 2`, so an in-memory `{token: pid}` map would frequently be
consulted in the wrong worker. Instead `run_query` tags its backend with
`set_config('application_name', 'sq:<user_id>:<token>', true)` and
`cancel_query` finds it with
`pg_cancel_backend(pid) … WHERE application_name = :tag`. Three details:

- `set_config(…, is_local => true)`, not `SET LOCAL` — `SET` takes no bind
  parameters. `is_local` gives `SET LOCAL` semantics, so the tag reverts at
  transaction end and never leaks to the next user of the pooled connection.
- The tag is rebuilt server-side from `request.user.id`, never from the request
  body, so a guessed token cannot reach another user's backend. It must also
  stay under 63 bytes — Postgres truncates `application_name` there, and a
  truncated tag would silently never match.
- The cancel is issued on the **meta** engine, not the console engine. Both
  point at the same database (a datasource's `dest_dsn` *is*
  `STARCUBES_POSTGRES_DSN`), but the console pool may be saturated by the very
  queries being cancelled, and a checkout there would block for
  `pool_timeout=30` first.

Client side, `cancelQuery()` POSTs to `/query/cancel` **before**
`AbortController.abort()`. Aborting the fetch alone does nothing server-side —
uWSGI never notices the dropped client and the query would run to
`statement_timeout`. `STARCUBES_QUERY_TIMEOUT_MS` (default 30 000) is that net,
clamped to 90 000 in the view because uwsgi's `harakiri = 120` kills the whole
worker process and its three sibling threads' requests with it.

**Results** are capped at `STARCUBES_QUERY_ROW_LIMIT` (1000); `fetchmany(limit
+ 1)` detects truncation. The server-side cursor is what makes the cap real —
without `stream_results` libpq buffers the entire result before `fetchmany`
returns. Cells are written with `textContent`, never `innerHTML`: warehouse
values are arbitrary strings and would otherwise be an XSS vector in our own
page. `bytea` is hex-abbreviated in `core.adhoc._jsonable` because
`default=str` renders a memoryview as `<memory at 0x7f…>`; everything else
rides the house `json_dumps_params={"default": str}`.

One trap worth knowing: the user statement goes through `exec_driver_sql`,
never `text()`. `text()` reads `:name` as a bind parameter, so a plain
`SELECT '{"a":1}'::jsonb` would die with *"A value is required for bind
parameter '1'"*.

**History** is per (user, datasource) in `starcubes_meta.query_history`,
deduplicated on `sha256(sql)` — re-running a statement bumps `last_run_at` /
`run_count` rather than appending, and each write trims to the newest 100.
Recorded *before* execution, so a query that times out is still recoverable.
`user_id` has no FK to `auth_user`: `SourcesConfig.ready()` runs
`ensure_starcubes_meta()` *during* `call_command("migrate")`, i.e. before
Django creates `auth_user`, so a hard FK would break every fresh install.
Cleanup on account deletion is therefore explicit, in
`sources/views.py:settings_page`.

**Endpoints** (all `@_login_required_json`, which answers 403 with a JSON body
rather than redirecting — a 302 to `/login/` would hand `fetch` an HTML page and
`r.json()` would throw an opaque `SyntaxError`):

| URL | name | method |
|---|---|---|
| `/query/run` | `viz:query_run` | POST |
| `/query/cancel` | `viz:query_cancel` | POST |
| `/query/history` | `viz:query_history` | GET |
| `/query/history/delete` | `viz:query_history_delete` | POST |
| `/chart/sql` | `viz:chart_sql` | GET |

These five also deviate from viz's `{"error": …} + 500` convention: a user's
SQL error is the normal case, not a server fault, so they answer HTTP 200 with
`{"ok": false, "error": …}` and reserve 500 for genuine surprises.
`/chart/sql` returns the SQL string whose result matches what the chart's
legend currently displays: one row per series, aggregated across the entire
half-open time window. Built by `core.query.build_chart_legend_sql` —
same routing (joins, group-dimension dispatch, filter tree) as the slot-by-slot
`build_chart_sql` but without the slot projection or `GROUP BY slot, series`,
and labelling each row the way the legend labels it rather than by the
stable key the plot path transports (see **Copy chart SQL** below). Quantile-bands use `percentile_cont` over each
series' full distribution (which the legend finalizes to its
median / p25–p75 anyway). It does not run anything; the user still has to
click Run.

**Format** (`#query-format`) is a vanilla pretty-printer, `lexSql()` +
`formatSql()` in `chart.html`. It knows the same lexical shapes
`adhoc.validate_sql` knows — `''` escapes, dollar-quoting, `--` and nested
`/* */` comments, `"quoted identifiers"` — and copies each of them through
character for character; nothing inside one is ever wrapped or re-cased. It
re-lays out whitespace and upper-cases a fixed keyword list, and that is all.

Two properties it is required to have:

- **Idempotent**, by construction rather than by testing: the output is a pure
  function of the token stream, whitespace tokens are dropped before that
  stream exists, and every emitted token re-lexes to itself. A `--` comment is
  always followed by a line break, so it can never swallow the rest of a line
  on a second pass.
- **Undoable**: it inserts through `document.execCommand('insertText')`, not
  `sqlBox.value = …`, so the browser's own undo stack keeps it and `Ctrl+Z`
  takes the formatting back. Deprecated but unreplaced — no standard API
  writes to a textarea undoably.

Subquery parens indent against the line the `(` landed on, not against the
clause depth: an `IN (` sits on an `AND` continuation line, one level in, and
its `)` has to come back to that same column.

**CSV** (`#query-csv`) downloads the last result as a file. It builds from the
payload the server sent, never from the rendered table — the renderer turns
NULL into the word "NULL" and every value into a string, and reading it back
would launder both. Quoting is the shared `csvEscape()` (RFC 4180: quote only
on `"`, `,`, CR or LF; double an embedded quote), NULL becomes an empty field,
lines end CRLF, and a leading BOM is what makes Excel read the file as UTF-8
rather than as the system code page. The button is disabled until a result
exists. A file rather than the clipboard because `STARCUBES_QUERY_ROW_LIMIT`
is 1000 and nothing useful happens when that lands in a paste buffer.

**Panel chrome.** The console is one of the shared floating panels — chrome,
header, drag, stacking and Escape all come from `.fpanel` /
`makeFloatingPanel()`. See **Floating panels** below; only geometry
persistence and `resize: both` are console-specific.

Resizing is CSS `resize: both` — the first use of `resize:` in the repo. Two
mechanics: it is ignored unless `overflow` is something other than `visible`
(hence `overflow: hidden` on the panel, `auto` on the two inner scroll
regions), and the native gripper writes **inline** `width`/`height`, which is
what reduces persistence to a debounced `ResizeObserver` reading
`panel.style.width`. A custom handle would be ~40 lines of `setPointerCapture`
arithmetic for behaviour the browser already ships. Dragging is the shared
panel handler, which *does* use pointer capture.

Geometry lives in `localStorage['starcubes-query-panel']` as
`{open, historyOpen, left, top, width, height}` — per browser, unlike the
history. This is the only panel whose **position** survives a reload, which is the
whole content of its `onMove` hook; placement and the resize re-clamp are the
shared ones. Size persistence (`width`/`height`) is now shared by all three
floating panels through the `persistPanelSize` / `applyPersistedSize` helpers
in `chart.html`, which read and write the same per-panel
`localStorage['starcubes-{panel-id}']` slot using the same 200 ms debounce.

Escape closes the console like any other panel — but **only from outside the
textarea**. The shared handler ignores the key when the event target is an
`INPUT`/`TEXTAREA`/`SELECT`, which is what keeps it from discarding a
half-typed query; before the panels were unified, the console was simply left
out of the Escape wiring for that one reason. `Ctrl/Cmd+Enter` in the textarea
runs; plain `Enter` is left alone so multi-line SQL still types normally, and
`Shift+Enter` is deliberately not bound — a textarea treats it as a plain
newline, which is what someone mid-statement expects.

**History keyboard navigation.** `#query-history-wrap` carries `tabindex="0"`
and `role="listbox"`, so the scroll container is the single tab stop and
`histIndex` is the caret inside it. The keydown handler is bound to that
container, not to the document or the panel: the SQL editor's own `↑`/`↓` must
keep moving the caret between lines, and a panel-level handler would steal
them. `↑`/`↓`/`Home`/`End` move, `Enter` loads into the editor, `Delete` /
`Backspace` removes. Movement **clamps** rather than wrapping — wrapping in a
recency-ordered list silently teleports you from newest to oldest. After a
delete the caret holds its index (clamped to the new length) so repeated
presses walk down the list instead of snapping back to the top.
`scrollIntoView({block: 'nearest'})` scrolls by one row rather than recentring.
`renderHistory` resets the caret to -1, and focusing an unvisited list starts
it at 0 so the first arrow press moves.

**History mouse interaction.** Single-click on a history row only moves the
caret (`histIndex`) — it does **not** copy SQL into the textarea, so focus
stays in the listbox and the user can page through history without
re-acquiring the textarea on every click. Double-click loads the SQL into the
editor (`loadSelectedHistory` semantics — caret at the end) **and** immediately
runs it, mirroring the keyboard `Enter`-then-`Ctrl+Enter` two-step in one
motion. The keyboard `Enter` key stays "load only", so mouse and keyboard are
intentionally non-equivalent — a keyboard user reviewing several candidates
can `Enter` to peek at each without running them.

**Copy chart SQL.** The button labelled `SQL` next to `Cancel` (`#chart-sql`)
fetches the per-series cumulative SQL for the current source / group_by /
measure / filter context from the `/chart/sql` endpoint and copies it into
the SQL editor (caret at the end, no run). It is disabled until the chart
has loaded (`ts && currentMeta`), so the button cannot request SQL for a
viewport that is not yet plotted. The server returns the SQL built by
`core.query.build_chart_legend_sql_rendered` — a wrapper around
`build_chart_legend_sql` (the legend-aligned counterpart of
`build_chart_sql`, the helper that backs `timeseries_multibar` and
`timeseries_ladder`) that inlines every bind parameter as a literal
before returning: timestamps as `'YYYY-MM-DD HH:MM:SS+00'::timestamptz`,
filter values as typed array literals (`ARRAY[…]::smallint[]` for
pass-through / grouped columns, `ARRAY[…]::text[]` for dim-mapped
subqueries), LIKE patterns as `'…'`-quoted strings. The console path runs
statements via `adhoc.run_query → exec_driver_sql(sql)` with no parameter
substitution (its own comment at `core/adhoc.py:277-280` spells out why),
so without the inlining the Run button would fail with "A value is
required for bind parameter …". The result is a self-contained SELECT that
returns one row per series — `sum(col)`, `count(*)`, `avg(col)`,
`min`/`max` (or the rung array for a ladder measure) over the entire
`[t_from, t_to)` window, matching what `updateLegend()` shows in the
right-hand legend of the chart.

**The window is the viewport snapped to bar edges, not the raw viewport.**
`updateLegend()` counts a bar fully once it overlaps the render bounds — a
bar only has to touch the viewport to be counted whole — and the underlying
data is fetched in whole blocks anyway. The request therefore snaps
`tmin`/`tmax` to the bar width before it is sent: `[floor(tmin/iv)*iv,
ceil(tmax/iv)*iv)`. That is exactly the union of the bar spans the legend
counts, so the SQL reproduces the legend totals exactly, including the
half-visible bars at the edges. The same edge alignment is what
`core.rollup.pick_rollup` requires, so the copied SQL and the chart's own
data path agree on the rollup decision.

**The statement may read a rollup table instead of `fact`.** The endpoint
consults `pick_rollup` with the legend's own parameters and passes the
result through, same conditions as the chart's data path: both window
edges on a rollup bucket boundary, the measure derivable from stored
aggregates (`count(*)`/`sum`/`avg`/`min`/`max`/`extent` — percentiles can
never be, so a `quantile(col)` graph always reads `fact`; an `extent(col)`
band folds exactly and is served like any scalar), group_by and every filtered
column covered by the table's column set, and the window inside
`[built_from, built_through)`. The status line names what the copied SQL
reads — `copied chart SQL (rollup_1h_y2026m04)` or `copied chart SQL
(fact)`. When a rollup is picked, `count(*)` becomes `sum(f.n)`,
`avg(col)` the row-weighted `sum(sum_col)/NULLIF(sum(cnt_col), 0)` — the
same mean the fact-side `avg(col)` computes, just folded from the
per-bucket aggregates.

**The series column carries the displayed label, not the transported key.**
This is the one place `build_chart_legend_sql` deliberately departs from
`build_chart_sql`. The plot path projects `source_id` (and, for `:grouped`,
the raw dim index) because the chart translates in the browser —
`_resolveDimName()` looks the name up in the `api_source_meta` payload and
the `:grouped` merge maps index → group name via `index_to_group` — and
because `dataCache` keys, `starcubesSeriesColors` and the drill-down
`group_by_val` all depend on that key staying stable across re-imports. A
statement pasted into the console has none of that, so the legend helper does
both translations in SQL instead:

| group_by | series column |
|----------|---------------|
| plain dimension | `COALESCE(NULLIF(_g.name, ''), _g.source_id)::text` — the dim name, falling back to the raw key exactly as the legend does, since `name` is nullable in the dim DDL |
| group dimension (`<col>:grouped:<slug>`, any graph style) | `CASE WHEN f.<col> = ANY(ARRAY[…]::smallint[]) THEN '<group>' … ELSE '(other)' END`, so `GROUP BY 1` aggregates per group. The index arrays come from the addressed set only, so two group dimensions on one column yield different CASE ladders — and different `(other)` residuals |
| pass-through column (no `dim_table`) | `f.<col>::text` — no dim table exists, and the legend shows the raw value too |
| no group_by | `'(all)'` |

`series_label=index` opts out and yields `_g.index::text` for callers that
want the warehouse key. Note the group-dimension row applies to multibar as well
as quantile-bands: the plot path folds counts per index in Python (cheaper
than a per-row CASE across every slot), but a legend row is one row per group
either way, so grouping on the name directly costs nothing here.

`setActiveSource()` calls `window.onQuerySourceChanged()`, which clears the
results table and reloads history — the list is filtered per datasource, and
stale results under a new title would mislead.

## History / URL

`buildHistoryParams()` constructs `URLSearchParams` from `starcubesParams` +
`currentViewport` (name, group_by, measure, measure_name, filters with
separators, andor, indent). Called by both `resetAndFetch` and the
memory-restore path in `setActiveSource`.

**`measure` does not identify a measure; `measure_name` does.** The dropdown's
option value is the agg form, because that is what `?measure=` has always
meant. But the catalog is unique on `(datasource, name)`, so one agg may
appear several times — `quantile(stay)` as bands, as steps, as error bars.
Both the URL and the fetch therefore carry `measure_name`, and the server
resolves the catalog row through `Source.measure_row(agg, name)`; without the
parameter it falls back to the first agg match, which is what an old bookmark
gets. Two options can share a value, so the selection helpers
`selectMeasure(value, name)` / `currentMeasureName()` exist: assigning
`select.value` would always land on the first of them.

## `ts.clearAll()`

`timeseries.js` exposes `ts.clearAll()` which sets `data = []` and redraws.
Must be called on every source switch to prevent mixed data from multiple
sources accumulating in `ts.data[]` (since `starcubesPush` / `pushData` always
appends).

## Natural-window blocks (`enumerateNaturalBlocks`)

`autoFetch` walks UTC-anchored calendar windows instead of just bin-aligned
ones, so the server-side `plot_json` cache hits across users who request the
same historic period.

| Bin interval | Historic block (past) | Trailing block (inside current historic block) |
|---|---|---|
| `1s` (second)  | hour    | 10 minutes |
| `60s` (minute) | day     | 4 hours    |
| `3600s` (hour) | iso-week | 1 day      |
| `86400s` (day) | month   | iso-week   |

- `NATURAL_WINDOWS` maps interval → `{ historic, trailing }`. Intervals not
  in this map fall back to a single `floor/ceil`-snapped block (legacy).
- `utcWindowContaining(kind, t)` returns `[startSec, endSec)` for kinds
  `tenmin | hour | fourhour | day | isoweek | month`. ISO week starts
  Monday 00:00 UTC; month uses `Date.UTC(y, m, 1)`.
- `enumerateNaturalBlocks(interval, vpFrom, vpTo, nowSec)` returns
  `{from, to, live}` blocks:
  - Historic blocks (entirely past the natural window containing `now`):
    fetched whole, cacheable.
  - Trailing-sized blocks inside the current natural window: also cacheable
    when entirely past; `live: true` for any block that **contains `now` or
    lies entirely in the future**. A future block must stay live rather than
    historic — as `live: false` it would be fetched whole, come back empty
    (nothing has happened there yet), and get recorded as fully covered, so
    once `now` reached it it was never fetched again and stayed permanently
    blank. Each trailing block is clipped to the bounds of the current
    historic window so it never overlaps adjacent historic blocks (matters
    for day resolution: the iso-week containing `now` can extend into the
    previous month, so it gets clipped to month-start).
- `fetchTier(interval, vpFrom, vpTo, nowSec, gen, ctrl)` per block — one call
  per resolution tier, see below:
  - `live: false` — skip if any existing `loadedBlocks` entry already
    covers it (`b.actualFrom <= block.from && b.actualTo >= block.to`);
    otherwise `fetchRange(block.from, block.to)`.
  - `live: true` — gap-fetch within `[block.from, min(block.to,
    ceil(now/interval)*interval)]` using `rangeGaps`. A future block's
    `liveTo` clips to `<= block.from`, so `rangeGaps` returns no gaps and the
    block costs nothing until `now` actually reaches it. See "Provisional
    blocks and the ETL high-water mark" below for how the claimed coverage
    of each fetched gap is computed.
  - Returns `false` when `fetchGeneration` has moved on, so `autoFetch`
    abandons the remaining tiers instead of fetching for a stale viewport.

## A source that is not there yet

A datasource whose warehouse side is not reachable is a *normal operating
state*, not a server fault: the GUI's source page creates the metadata row
first and runs Create Schema later — or never, for a half-configured source,
and a warehouse can go away between two requests. Both halves of the chart
answer it the same way the SQL console answers a user's syntax error — with
the reason, and without a 500 in anyone's logs:

- **Proactively, in the meta payload.** `/source-meta` probes `<schema>.fact`
  along the primary dim tables it already probes for the dim guard — one
  `to_regclass` round trip, no extra cost — and adds the answer as
  `not_ready: {reason, missing}` (null on a ready source). `loadMeta` shows
  the reason via `updateSourceNotice()` in the status line's own
  `#source-notice` span (own span, not `#info-text`, which `updateInfo()`
  rewrites on every fetch tick — same reason `#rollup-notice` is one), and
  `_autoFetchOnce` gates on it: no `autoFetch`, no poll, no fetch storm
  against a source that cannot answer. Rollup tables are not probed —
  `pick_rollup` consults `rollup_state`, and a genuinely missing one
  surfaces through the request's own 42P01 instead.
- **Defensively, in the four JSON endpoints.** What the meta call cannot
  cover — a direct URL call, a table dropped between meta call and plot, a
  rollup `pick_rollup` chooses that does not exist, or a database that dies
  after the meta call — the endpoints themselves translate, in
  `_friendly_db_error(chart=True)`. Dispatch is by exception type and
  `orig.pgcode` only, never by message text (the relation name in the note
  is display-only extraction with a generic fallback): `42P01` → "missing in
  schema … — run Create Schema on the source page", `OperationalError` →
  "warehouse not reachable", `LookupError` → datasource not found. All three
  answer HTTP 200 with `{"ok": false, "error": …}`, like the console's five;
  everything else keeps `{"error": repr(exc)} + 500`. No empty plot object is
  returned — the client would book the span as covered via
  `pushData`/`loadedBlocks` and no gap scan would ever repair it.
- **The fetch side knows both shapes.** `fetchRange` throws on
  `plot.ok === false` (marked `friendly`, so the status line shows the
  message without the "fetch failed:" prefix and the payload stays out of the
  coverage bookkeeping), and `loadMeta` does the same for the meta payload
  while *dropping* `currentMeta` — the previous source's dropdowns, legend
  and notices must not survive a failed switch. `setActiveSource` wraps its
  `await loadMeta()` in `try/catch` that hides the loading overlay and shows
  the reason: without it, the click on a source tab — the likeliest way to
  reach a broken source — would leave the overlay up for ever, an unhandled
  rejection with nothing on screen.
- **The connect_timeout underneath.** An unreachable warehouse only gets to
  *be* an `OperationalError` within a bounded time: the request-path engines
  (`Source.request_engine()`, `_meta_engine()`) carry a libpq
  `connect_timeout` (`REQUEST_CONNECT_TIMEOUT_S`, 10 s) and are separate
  cached pools, so the ETL pools — which must keep waiting for a busy
  warehouse — are untouched. `connect_timeout` is not a circuit breaker; the
  backlog's "Engine cache fast-fail" entry stays open.

## The in-page data cache and the epoch

`dataCache` is a 50 MB LRU keyed on the request query string. That key knows
nothing about the *state* of the datasource, and a historical block is the
most cacheable shape there is — final, not trailing — so anything that
changes data the server already considered final would otherwise keep being
painted from this cache for as long as the tab stays open. A retention drop
is the sharp case; a backfill and a watermark rewind have the same shape.

So every `plot_json` response carries `cache_epoch`, the counter the
server-side cache already keys on, and `noteCacheEpoch` clears `dataCache`
whenever the value moves. There is no other reset path: entries otherwise
leave only by LRU eviction.

## Provisional blocks and the ETL high-water mark

The server answers with two different facts about the same request:
`interval_end` (`core/query.py:1139`) is always the **requested** `to`,
regardless of how far the ETL has actually loaded; `incomplete_until`
(`core/query.py:1164`) is the last successful ETL watermark (HWM). A block
whose `interval_end` is past `incomplete_until` is **provisional** — its
trailing bins may still receive rows on a later fetch.

`fetchRange` computes two extents from the response:

- `rawTo` — the raw `interval_end` (or the slot-derived fallback). This is
  what `pushData` in `timeseries.js` used to delete slots of any overlapping
  older block, so the `loadedBlocks` trim loop must use `rawTo` too — trimming
  with a narrower value would leave a neighbour claiming coverage over slots
  that were just erased from the canvas, a hole no gap scan could ever find.
- `coveredTo` — what we actually claim as loaded. Only **trailing** fetches
  clamp it down to `floor(incomplete_until / interval) * interval` when the
  HWM is behind `rawTo` — floor, not ceil, because the bin containing the HWM
  is only partially loaded. Non-trailing (historic) blocks keep the raw
  extent: clamping them eagerly would fail `fetchTier`'s `covered` test on
  every 300 ms follow-mode tick and refetch the whole viewport on any source
  whose ETL isn't continuous.

A past block that turns out to have been provisional (its own
`incompleteUntil` was behind its `actualTo`) is reopened — its `actualTo`
pulled back to its own HWM-floor — the next time a *newer* response reports a
larger HWM (`hwmSeen`, a monotonic per-source max). This only fires on a
genuine HWM advance, not on every tick, so a source whose ETL is stalled or
down leaves every already-covered block alone.

Provisional plots are **not** written to `dataCache` or `sourceMemory`. A
reopened block would otherwise cache-hit its own stale copy and re-close
immediately, never repairing once the ETL actually catches up. The server
never caches them either — `core/cache.is_cacheable` already requires
`t_to <= hwm` — so every server-cache hit already satisfies the client-side
"final" predicate too.

This replaced an older one-interval back-extension in `fetchTier` (rewind
`gap.from` by exactly one bin when the previous block had a truthy
`incompleteUntil`), which could only ever compensate for an ETL lag of one
bin — useless against the 30–120 s lag a normal periodic ETL run leaves
between passes.

**Do not** cap `interval_end` at the HWM server-side instead — see the
`docs/chart-rendering.md` design-decision note "`interval_end` is the requested window end,
not the data horizon" for why that breaks `pushData`, the `covered` test, and
the client's ability to tell "no rows here" from "not loaded yet".

### Drawing the partial bin

Everything above draws the *coverage* consequence of "the bin containing the HWM
is only partially loaded". The drawing consequence is a separate, later step:
that bin holds part of a bin's worth of data, so at full width the bar is both
too short and too long at once — too short because the count is incomplete, too
long because it runs past where the data ends, into the near future.

`fetchRange` therefore also hands the horizon to the canvas, as
`plot.data_until = hwmSeen`, set next to `plot.extensive` just before
`starcubesPush`. The HWM read moved above the push for exactly this reason; the
monotonic `hwmSeen` rather than the per-response `hwm`, so a briefly regressing
watermark cannot make the bar flicker wider and back on every poll.

The library (`@hgruber/timeseries.js` ≥ 0.10.2) does the rest: `setPartialBins`
`'scale'` draws that one bar only as wide as its bin is filled and divides its
height by the same fraction, so its **area** is still the count it carries and
its height reads as the same density as its neighbours. That factor is also the
rate-correct one — a count accumulated over `interval * f` seconds *is* a rate of
`count / (interval * f)` — so it composes with `setRateUnit` without a special
case. Only `extensive` blocks are scaled; an `avg`/`quantile` merely clips. A bin
filled below 10 % is dropped entirely, since `1/f` explodes there.

The toggle follows the `yAxisMode` pattern exactly: `partialBins` state,
`localStorage['starcubes-partial-bins']`, a `?partialbins=0` override that is not
written back, a checkbox in the gear popover. It defaults **on** here while the
library defaults to `'full'` — the library must not change any existing
consumer's picture, whereas in the chart this bar is on screen whenever anyone
watches a live source.

## The gap overlay

The `#gap-toggle` button (hidden until the source meta has arrived, disabled
while a real measure is selected) asks a question the bars cannot: which
stretches of the transaction stream are an **outage**, not just quiet? The
answer is drawn as black brackets over the bars (white in the dark theme — `--gap-mark`) — one `gantt` span block
(`category: 'span'`, `appearance: 'bracket'`) pinned near the top of the plot
(`GAP_YPOS = 0.9` — a bracket annotates the bars rather than being one of
them, and down in the lower third it crossed the very bars it spans), so the
library owns pan/zoom, drawing and hit-testing and
`chart.html` only maintains the event list. The overlay exists for
`count(*)` only: the test is defined on "no row fell into this second", so
any real measure would silently compute a different question, and
`syncGapToggle()` (wired into `gatherParams`) disables the button while one
is selected. Session-only by decision — an overlay that turns itself back on
after a reload would be a surprise.

**The model is Poisson over a day profile.** For a candidate span,
μ = ∫λ̂ dt — the expected transaction count, integrated hour segment by hour
segment (`_profile_integral`) with the hour-of-day rate λ̂(h) measured over
the last 7 days (`GAP_PROFILE_DAYS`; the window is
day-aligned and ends at the last fully-watermarked day, `gap_profile_bounds`,
clamped up at the retention floor). The span is marked when μ exceeds
`GAP_THRESHOLD_EVENTS` = −ln(α) ≈ 20.7557 (α = Φ(−6)), i.e. when
P(no event over the span) = exp(−μ) falls below the one-sided 6σ tail — the
tail quantile is tested directly because "μ + 6σ on the gap length" would be
miscalibrated (the geometric tail depends on λ itself). An hour-of-day that
never saw traffic has λ̂(h) = 0 and contributes nothing: a span covering only
silent hours stays unmarked *by construction*, not by special case.

**Both halves of that sentence are in UTC, and it takes work to keep them
there.** λ̂ is keyed by hour-of-day, the integral asks for it by UTC hour, and
the SQL that derives it groups with `extract(hour from …)` — which over a
`timestamptz` is evaluated in the **session** time zone. On a server running
Europe/Berlin the profile answered for 19:00 UTC when asked about 21:00 UTC,
so λ̂ was wrong by whatever the day profile does across two hours (a factor of
2.4 on this data) and the error moved twice a year with DST. `_utc_ts`
normalises the grouping expression, and `gap_reference_drill` restricts its
reference hours through the same helper, so the profile, the integral and the
breakdown share one frame. A `timestamp without time zone` column is left
alone — it is taken as UTC, which is what the bound parameters have always
assumed.

**The scan runs in the backend at a fixed 1 s granularity** whatever tier
the chart is showing — `gaps.json` no longer reads `?interval=` at all. The
bracket tips sit on the first and last *transaction*, and only a 1-second
grouping sees them: the scan SQL takes `left_txn_ms`/`right_txn_ms` from
`max`/`min` of the two edge buckets and the μ-test runs over the
transaction distance, not the bucket distance. The scan itself always reads
`fact`, never a rollup — a rollup carries min/max only as its own bucket
starts, so exact edge timestamps would be wrong there. One request answers
at most `GAP_MAX_SCAN_S` = 7 days and reports `scan_truncated` beyond that,
cut at the **end** (the fresh edge is the provisional one; the remainder is
simply re-requested). The client pre-splits its demand at the same constant
(`splitSpansMax` over `GAP_SCAN_MAX_S`), so every request comes back
answered in full and the per-piece coverage booking stays honest. The scan
is also pre-filtered in SQL with `min_len_s = GAP_THRESHOLD_EVENTS / λ̂max` —
a gap shorter than that cannot pass the μ integral even at the day's peak
rate — and `λ̂max = 0` skips the scan entirely (nothing is expected anywhere
in the day, so nothing can be an outage).

**The density estimate prefers a rollup.** The profile λ̂(h) is the
expensive multi-day part, and it is a coarse count — an hourly rollup
answers it fine. `viz.views._load_gap_profile` resolves the source:
`core.rollup.pick_profile_rollup` returns the first spec in `rollup_specs`
order whose bucket width divides the hour (`3600 % interval_s == 0` —
otherwise `extract(hour from bucket_start)` reports the hour a bucket
*starts* in, not the one its rows sat in), whose stored `spec_hash` matches
the configured spec, whose `[built_from, built_through]` covers the
day-aligned profile window (less than a day of usable window means fact —
a sub-day profile is a rate measured at nothing), and whose column set
covers every column the chart's filters actually read (`filter_tree_columns`,
the same resolution the WHERE clause uses, so the coverage test cannot
drift apart from it). Anything else falls back to `fact` with `count(*)`.
The choice travels into the cache key via `series_label` ("fact" vs. the
rollup's name): both payloads have the same shape, but a rollup profile over
the same window and filters is a *different* λ̂ (bucket-granular count,
non-final edge), and a hit across tables would be a silently wrong number.
**The profile window is anchored at the watermark**, not at the request's
`to`. Keying λ̂ on the request window made the same gap score differently
depending on which slice the client asked for — and the client splits its own
demand, so nobody controls that. Measured on this data, a gap on 01.04.
scored μ = 25.1, 40.9 or 63.3 for a `to` of 03.04., 05.04. or 08.04., and was
missed entirely for 02.04., whose window reaches back before the first row in
`fact`. With one anchor per source and filter set the verdict on a gap no
longer depends on how it was looked at, `gaps.json` and `gap_estimate.json`
report the same μ by construction, and the two share the cache entry always
instead of only when their windows happened to end on the same day. The price
is that a gap months back is measured against the *current* day profile.

The payload is cached server-side under `kind="gap_profile"` with
`GAP_PROFILE_TTL_S` = 15 min — not a data invalidation (the entry dies with
a `cache_epoch` bump anyway) but a growth bound: back-scrolling over months
would otherwise leave one row per key behind, and `query_cache` has no
eviction of its own. The gap rows themselves are never cached server-side —
they are the incremental part.

**The client keeps one global map.** The detection is tier-independent, so
there is no per-tier `gapStore`: `gapMap` is keyed on the exact
`left_txn_ms` (the merge key of every response — the same gap seen from two
seam-widened windows lands on one entry, merged unconditionally because each
row carries its complete txn anchors; there is no bucket-truncated sighting
left to protect), and `gapCoverage` records the spans already scanned so
pan/zoom re-asks only new demand. Demand = plot coverage over **all**
loaded blocks at every tier (the scan is server-side and needs no tier data
of its own) minus gap coverage, clipped to viewport, HWM and retention
floor, and widened by `GAP_SEAM_S` = 1 s so a gap straddling the seam of two
requests is seen complete by at least one of them — the margin itself is
never booked. Once seen, a gap stays in the map: `refreshGapPlot()` runs in
the `onViewportChange` tracker as well as after every fetch, so a bracket
cached in a previously scanned region reappears the moment the viewport
returns to it — through pan, zoom and tier changes alike. A HWM advance
reopens the trailing edge (`invalidateGapsFromHorizon`: entries computed
against provisional trailing slots are cut back), a `cache_epoch` change
clears the whole store (a backfill or retention drop rewrites exactly the
buckets the gaps were computed from), and the toggle-off aborts and drops.

**The toggle also opens the gap list** (`#gap-panel`), a `.fpanel` like the
rest. A bracket is a few pixels wide on a week view and invisible until
stumbled over, and a scan that has found nothing looks exactly like one that
has not started; the panel answers both. It lists **everything the session has
found** — `gapMap`, not the viewport — because the first thing a reader does
is jump to a gap, and a viewport-filtered list would collapse to that one row
on arrival. What the list is about is therefore stated rather than implied:
`gapScannedText()` prints the outer bounds of `gapCoverage` plus the total
scanned duration, and deliberately does not claim the span is contiguous
(coverage can hold several pieces once the chart has jumped across ranges).

`refreshGapList()` renders three states — searching, nothing found, and the
list — and is called where the data or the scan state changes: after the
merge in `fetchGapSpan`, at both ends of `maybeFetchGaps`, in
`clearGapStore` and `invalidateGapsFromHorizon`. **Never from
`refreshGapPlot()`**, which runs in the viewport tracker on every frame: the
list is not viewport-dependent and would be rebuilt per panned pixel. Rows
are chronological, unlike the plot's widest-first ordering — that one sorts
to cap pixel density under `GAP_MAX_DRAWN`, this one is a timeline to read
down.

Clicking a row calls `gapZoomTo()`: the window is three times the gap's own
length, centred on it, floored at `GAP_ZOOM_MIN_S` (180 s) so a
ten-second outage does not arrive in a ten-second window with no traffic
around it to judge it against. It goes through `applyRange`, the single host
entry point for a viewport change. The store survives that jump — but not
because `applyRange` is gentle: it runs `resetAndFetch`, which used to call
`clearGapStore()` and so threw away every gap found so far on each jump out
of the list. The store is now dropped by `syncGapStore()` in `gatherParams`,
on a change of the **parameter signature** (source + the filters, built from
the same `appendGapFilters` the requests use) rather than on any refetch. A
viewport is not a parameter: a gap is anchored to an absolute instant and
stays true whatever window it is looked at through. The panel's `×` and Escape both route through
`setGapOverlay(false)`: brackets and list are one state, and closing either
closes both.

**Hovering a bracket names the number it is about.** `gapTooltipNodes`
prints `~ <μ> expected` ahead of the duration and the 6σ threshold. It costs
no request: `detect_gaps` computes each gap's `expected_events` with the very
`_profile_integral` that `gap_estimate_json` computes its μ with, and the
value rides on the event object `refreshGapPlot` stamps. `fmtExpected` is
shared with the dialog so the hover and the click cannot disagree about the
same number.

**A click on a bracket opens the estimate dialog** — the shared
`#drill-modal` (a generic `.fpanel`, so drag/Escape/resize come free),
filled by `openGapModal` instead of `openDrillModal`, with the txn and alarm
header buttons hidden and restored by `openDrillModal`, symmetrically. Every
number is computed in the backend by `gap_estimate.json`, so the client only
lays it out: μ over the gap's exact txn anchors, the profile's per-hour
table (Ø/min/max per covered hour-of-day, from the `per_day` series the
shared profile payload carries), and the neighbour rate — the rows/s the
stream actually carried in the ±5 minutes either side (`GAP_NEIGHBOR_S`),
read from `fact` because a rollup is bucket-granular and cannot see a
5-minute rate. The response carries `clamped` flags (a neighbour window
shortened by the retention floor or the watermark — a window the data cannot
support would otherwise read as a suspicious 0/s — and a profile window
resting on less history than requested) and `profile_source`. The profile
part shares the gaps.json cache key, so the warm case is one meta round trip
and two small fact COUNTs.

**The dialog breaks the gap down like a bar.** It leads with the same
per-dimension layout `renderDrillBody` draws for a clicked bar — both go
through `_drillSectionsHtml` — and the derivation behind μ (hour table,
neighbour rates, clamp notes) follows in a collapsed `<details>`. A gap holds
no rows by definition, so the counts cannot be counted: `dimensions` in the
`gap_estimate.json` payload comes from `core.query.gap_reference_drill`,
which reads the **shares** out of the profile window — the same window and
the same `extract(hour from …)` grouping λ̂ is derived from, restricted to the
hours-of-day the gap touches — and scales them by μ. Shares and magnitude
therefore rest on one body of data. The neighbour windows either side would
answer a different question (what happened to be running just then) and would
freeze a passing anomaly into the estimate. Counts are rendered with a `~`
prefix and values whose share rounds to zero are dropped rather than listed
as `0`, which would read as a measurement instead of a share too small to
show. The gap is rarely aligned to hour boundaries; taking the whole touched
hour as the reference is deliberate, since only ratios are read from it.

Because the dialog now carries clickable filter rows like a bar's, it has to
remember which question it is answering: `_drillCtx.kind` (`'bar'` or
`'gap'`) routes `drillAddFilter`/`drillRemoveFilter` through
`_refetchDrillView`, or a filter clicked inside a gap would reload the modal
as a drill-down of whatever bar was clicked before it.

Two things that are not self-evident:

- **`GAP_YPOS` needs timeseries.js > 0.10.8 to actually pin.** `yPos`
  promises a place that does not move, but through 0.10.8 `gantt.js barRect()`
  resolved it as `yPos × laneCount` through `Y()` — the *shared* value axis,
  whose `ymax` is a weighted blend over the active blocks (the span block
  contributes its `laneCount`, the multibar its data maximum). Every zoom
  moved that blend, so the bracket wandered vertically and, through `ppv`,
  resized with it. The fix places pinned events against the plot's pixel box
  and derives their glyph metrics from `plotHeight`; where a span block owns
  the axis alone the two expressions are arithmetically identical, so only
  the mixed case changes. Without that library version the overlay still
  works — it just will not hold still.
- **The bracket glyph ships with timeseries.js 0.10.8.** `appearance:
  'bracket'` is drawn by the released package (`package.json` pins
  `~0.10.8`); before that release the overlay could only run against the
  library's working copy, which is what `docker/refresh-timeseries.sh` is
  for — pushing a pre-release working copy into a running container without
  cutting a release for every small change. Nothing in the overlay depends
  on it staying a dev build any more.
- **The `dropData` exemption in `fetchRange` (~2532).** After every plot
  push, `ts.dropData()` removes residual blocks left over from a previous
  measure — and the gap overlay's span block carries no
  `_starcubesMeasure`, so without the `category !== 'span'` guard every
  subsequent plot push dropped the brackets and the overlay vanished on the
  first pan or zoom. Measured, not hypothetical: the drop came through that
  exact line, not through `pushData`'s trim.

## Resolution tiers and the cross-fade

Blocks at different `interval`s live side by side in `ts.data[]` (`pushData`
keeps both), and `prepare_grid` draws the finest tier whose bars are at least
`fadeHi` px wide. Across the `fadeHi` → `fadeLo` band both tiers are drawn and
dissolved into each other via `plot._fade` (see the library's docs).

**starcubes owns the threshold, the library owns the transition.** starcubes used to
call `ts.setRenderInterval(interval)` on every `autoFetch`, which pins one tier
and switches the cross-fade off entirely. It no longer does. Instead:

- `bar_pixel_target` (per source, default 5) is the
  switch point, and `fadeLoPx()` = half of it is the band's low edge.
  `loadMeta()` pushes both into the canvas with `ts.setFadeBand()`; `initTs()`
  passes the same pair as `fadeHi`/`fadeLo` constructor options for the case
  where it runs first. The chart's preferences popover exposes a Save/Reset
  pair that overrides the source value via `localStorage['starcubes-bar-target']`
  for the current browser; reset clears the override and falls back to the
  source's default. Applies to every visitor, logged in or not.
- `fadeTiers(ppms)` mirrors `prepare_grid`'s selection on the starcubes side and
  returns `{ primary, fading, prog }`. **`bestInterval()` and the library's
  rule must stay identical** — if they drift, one tier is drawn while a
  different one is kept topped up, and panning puts holes in the visible one.
- `autoFetch` calls `fetchTier` for `primary` and, while `fading !== null`, for
  the outgoing tier too. That extra round of blocks is the price of the band and
  only applies for the one zoom octave it spans.
- The **dominant** tier is the one above `_fade` 0.5. `updateInfo`, the y-axis
  label (`yAxisLabelFor`) and `updateLegend` all cut at that same 0.5, matching
  the library's `get_element` hit test — so info line, axis unit, legend totals,
  tooltip and drill-down all refer to the same resolution. The legend cut is not
  cosmetic: without it the totals would be counts/minute *plus* counts/hour.
- `schedulePoll` takes its cadence from the finer of the two active tiers (it
  goes stale faster), but keeps the "is now still in view" test on the coarse
  one — that is a viewport question, not a freshness one.

Note that starcubes's ladder (`interval_ladder`, default `[1, 60, 3600, 86400]`)
steps by 60×, so the dissolve blends a tier of 1–2px bars into one of 60–120px
bars. It smooths the switch; it does not make the resolution jump small — the
density axis below is what keeps the *bars* the same size across it.

## The y-axis is a density

Counts per bin scale with the bin: an hourly bar is worth ~60 minute bars. Mid
dissolve both tiers are on screen, so `blendExtents` had to travel the axis
between two extents 60× apart — the fine tier was squashed to a sliver and the
coarse one drawn twice too tall. Measured on a real source, at one fixed
viewport at `_fade` 0.64/0.36, the axis had to span **43×** between the two
tiers.

So the values are plotted as a **rate** (count ÷ interval) via the library's
`ts.setRateUnit(seconds, {label, transition})`. Both tiers then hold the same
number and draw at the same height; the same measurement now spans **1.39×**,
which is only the genuine peak-vs-average difference between a minute bin and
the hour containing it. The tier switch is left to change nothing but the
numbers printed on the axis, over a 300 ms dissolve of the tick set.

- **`applyYAxisUnit(transition)`** resolves the unit and pushes it into the
  canvas. Called from `autoFetch` right where `setYAxisLabel` used to be, so it
  still runs on the same `_fade < 0.5` dominant-tier cut as `updateInfo` and
  `updateLegend`. Deliberately **one** unit for both active tiers — that is what
  makes their bars the same height through the dissolve.
- **Only additive measures are scaled.** `isExtensiveMeasure()` accepts
  `count(*)`, `sum(…)` and the bare-column form (`core/query._parse_measure`
  reads that as `sum`); `avg`/`min`/`max`/`quantile`/`extent` are already per-unit and
  are marked `plot.extensive = false` at push time, so the library leaves them
  at scale 1. Their axis label also drops the `/unit` suffix — "avg(stay)/h"
  would be a lie.
- **`yAxisMode`** picks the unit policy: `auto` (default — the dominant tier's
  natural unit via `rateUnitFor`), a fixed `s`/`min`/`h`/`d`, or `count` (raw
  per-bar values, i.e. the pre-density behaviour, axis travel included). Global
  per browser in `localStorage['starcubes-yaxis']`, overridable per link with
  `?yaxis=`; a link never writes back to localStorage. The control sits in the
  gear popover.
- **Tooltip, legend, drill-down and CSV keep absolute counts.** The library's
  hit test returns the raw value by design. The tooltip appends `≙ 9.32/min`
  through `attachTooltip`'s `formatter` hook so the axis and the bin count are
  visibly the same fact — suppressed when the axis unit equals the bin length,
  where the two numbers are identical anyway. `updateLegend` reads `plot.data`
  raw and is untouched: a window total is tier-independent.


## Floating panels

Five draggable boxes float over the canvas — the legend, the drill-down panel,
the query console, the keyboard-shortcut panel and the gap list — and there
will be more. They share one CSS ruleset
(`.fpanel`, `.fpanel-header`, `.fpanel-title`, `.fpanel-actions`,
`.fpanel-close`) and one JS registration (`makeFloatingPanel(el, opts)`), both
in `chart.html`. A new panel is two classes in the markup and one call; it must
not bring its own drag handler, z-index, clamp or Escape wiring. Closing the
drill-down panel also clears the canvas's persistent bar outline
(`closeDrillModal()` is the one place that calls `ts.clearSelection()`).

```
<div id="my-panel" class="fpanel">
  <div class="fpanel-header">
    <span class="fpanel-title">Title</span>
    <span class="fpanel-actions">…buttons…</span>
    <button class="fpanel-close" type="button">&times;</button>
  </div>
  <div id="my-body">…</div>
</div>
```

```js
makeFloatingPanel(el, { onClose, onMove });   // onMove only if the position persists
el.classList.add('fp-open'); fpBringToFront(el);
```

What the shared layer decides, and why:

- **They are body-level `position: fixed` siblings**, never children of
  `#wrap`. `#wrap` has padding and is a `flex: 1` item, so a child would be
  laid out against a box that moves with the toolbar and the filter panel, and
  dragging would be clamped to the chart area.

- **None of them is modal, and there is no backdrop.** The drill-down used to
  live inside a `#drill-overlay` that greyed the whole canvas out and closed on
  a background click. Both were wrong for this panel: it exists to be read
  *against* the chart it came from, and "background" is the canvas, so panning,
  zooming or picking the next bar destroyed its contents. Closing is the `×` or
  Escape. Removing the overlay also removed the only element that was capturing
  canvas clicks while a panel was open.

- **Stacking is click order, not a ladder of magic numbers.** Every panel sits
  at the same base `z-index: 1000` and `fpBringToFront()` raises whichever was
  clicked last. The listener is on the panel in the **capture** phase, so a
  click on a button inside it raises the panel too, even where the button's own
  handler stops propagation. The old fixed ladder (legend 1000, console 1500,
  drill 3000) meant the drill panel could never be put behind the legend.

- **A panel may be dragged off the left, right and bottom edge**, keeping
  `FP_KEEP_VISIBLE` (56 px) on screen — but its top never goes above 0. The
  header *is* the drag handle, so a panel whose header left the viewport could
  never be dragged back. That asymmetry is the whole clamp: `fpMoveTo()` is the
  only place any of it is expressed, and `fpReclamp()` re-applies it after a
  window resize or a content change.

- **`fpMoved` marks a position as the user's.** Panels that place themselves
  (the legend anchors to the plot area's right edge; the drill panel centres on
  the plot area) check the flag first and stop moving once the user has dragged
  them — the drill panel re-renders its body on every navigation step and would
  otherwise jump back to the centre each time. It is also why the resize
  re-clamp skips unmoved panels: rewriting the legend's `right` anchor to a
  `left` would silently break its edge tracking.

- **Escape closes the top-most open panel**, chosen by the same `z-index` the
  click order produced, and skips the key entirely when it was typed into an
  `INPUT`/`TEXTAREA`/`SELECT`.

The legend is the one behaviour change from before the unification: it used to
be draggable by its whole body (with a hand-maintained list of child elements
that were exempt), and now drags by its header like the other two. It gained
the header rule the other two already had.

`#keys-panel` is what that contract looks like in practice: two classes in the
markup, one `makeFloatingPanel(el, {onClose})`, and `applyPersistedSize` /
`persistPanelSize` for its geometry — no drag handler, no `z-index`, no Escape
wiring of its own. It centres itself on first open and stops doing so once
`fpMoved` is set. `?` opens it from anywhere, with the same
INPUT/TEXTAREA/SELECT exclusion the Escape handler uses; the canvas's own
keydown handler ignores `?` without calling `preventDefault`, so the key still
arrives with the chart focused.

Its content is a static copy of the library's key table
(`doc/configuration.md`) and carries the footnote that makes the list honest:
navigation, calendar, follow and display keys are bound to `canvas.onkeydown`
by the library and only fire while the plot has focus. One of them, `l`, was
listed by the library but dead here — `ts.toggleLegend()` needs a controller,
and `chart.html` never registered one. `initTs()` now calls
`ts.setLegend({ toggle: toggleLegendPanel })`, so the key opens starcubes's own
legend panel.

The `c` key (and the Clamp button in the right-hand button cluster) toggles outlier clamping,
new in timeseries.js 0.10.6. The library exposes no callback for a clamp
change, so the button keeps itself honest by reading `ts.getClampOutliers()`
inside the `onViewportChange` tracker — `setClampOutliers()` redraws through
`plotAll()`, which broadcasts the viewport, so that hook fires for both the
button and the key. Clamping is session-only: the library default (`off`)
applies on every load, nothing is persisted.

All panels except the legend are user-resizable via CSS `resize: both` plus the
browser-native grip in the bottom-right corner; the legend is width-only
(`resize: horizontal`). The legend's height is deliberately not user-resizable
and not persisted: it is content-driven — it collapses to exactly the entries
it shows — and capped at 75 % of the plot area's height (computed in
`updateLegend()`, with a small floor so the header survives a tiny plot area;
`#legend-items` scrolls once the cap bites). The legend shows "no data" when
there are no series to list. The legend and drill-down panels were made
resizable to match the query console; their sizes are persisted across reloads
by `localStorage` (keys `starcubes-legend` — width only —, `starcubes-drill-modal`,
`starcubes-keys-panel`), written through a 200 ms debounced `ResizeObserver`
(the `applyPersistedSize`/`persistPanelSize` helpers take an `axes` argument,
`'wh'` by default, `'w'` for the legend). Min-width and min-height prevent the
other panels from being squashed below usable sizes; the other ceilings stay in
CSS so a programmatic `applyPersistedSize` and a browser drag honour the same
limit.
