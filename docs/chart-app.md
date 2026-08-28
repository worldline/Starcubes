# viz/chart.html — multi-source UI deep reference

The chart page supports multiple datasources with full per-source isolation.
Read this when editing `viz/templates/viz/chart.html`, `viz/views.py`, or
anything that touches `starcubesPush` / `loadedBlocks` / `sourceMemory`.

## Nav bar

Single `#nav-tabs` flex row: `[Source1] [Source2] … [spacer] | Sources  ☀`.
Source tabs are injected before `#nav-spacer` by `loadSources()`. Clicking a
tab calls `setActiveSource(name)`.

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

## Init and the start window

`initTs()` builds the `TimeSeries` instance, and by then the window it should
show is already decided: the boot IIFE awaits `loadSources()` and `loadMeta()`
first, and `loadMeta()` sets `currentViewport` to today 00:00 → tomorrow 00:00
whenever it is still null; `applyUrlParams()` then lets `?from=`/`?to=` override
it. `resetAndFetch()` is the second entry point and runs just as late.

The library has **no constructor option for a start window**. It always comes up
on its own `tmin = now - 24h` / `tmax = now` defaults and paints that
synchronously before the constructor returns, so starcubes has to correct the
window afterwards — which is why `initTs()` stashes `_initTmin`/`_initTmax`
before the `new TimeSeries({...})` call: constructing it fires
`onViewportChange`, whose handler overwrites `currentViewport` with the
library's 24 h default.

The correction is `ts.zoom(_initTmin, _initTmax, 0)`, and **the `0` is the whole
point**. `zoom()` without a duration animates over `zoomDuration` (500 ms), so
the page opened on "last 24 hours" and visibly slid to the real window. A
zero-duration zoom instead runs to completion inside the library's `animate()`
on the spot — span 0 → factor 1, no `requestAnimationFrame`, no `setTimeout` —
so the corrected picture is drawn in the same task as the constructor's, and the
browser never composites the 24 h frame. Anything that reintroduces a duration
here, or moves the call behind an `await` / `setTimeout` / `requestAnimationFrame`,
brings the jump straight back.

Two smaller consequences of the same shape:

- `ts.stop()` follows the zoom because the constructor does not start in a
  stopped state; it also fires the `onStop` callback that clears the follow
  toggle's `active` class.
- If `currentViewport` were ever null at this point (unreachable today — both
  callers run after `loadMeta()`), the `else` branch adopts `ts.getViewport()`
  rather than leaving it null. Otherwise `_autoFetchOnce()` bails at `if (!vp)
  return` and the 300 ms-throttled `onViewportChange` fills it in late, which is
  the same jump in slower motion.

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

`starcubes-state-{name}` stores `{ group_by, measure, filterRows, viewport }`.

- `saveSourceState(name)` writes to localStorage (called before leaving a source)
- `applySourceState(name)` reads and restores. Validates options exist before
  setting; reconstructs filter `Set` objects; calls `ts.zoom()` if viewport
  saved.

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

A floating, resizable ad-hoc SQL panel, toggled by the `Query` button that sits
immediately right of `Follow` in `form#ctrl`. Both button and panel are wrapped
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
Backlog note in `CLAUDE.md`.

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
history. This is the only panel whose position survives a reload, which is the
whole content of its `onMove` hook; placement and the resize re-clamp are the
shared ones.

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
`timeseries_quantiles`) that inlines every bind parameter as a literal
before returning: timestamps as `'YYYY-MM-DD HH:MM:SS+00'::timestamptz`,
filter values as typed array literals (`ARRAY[…]::smallint[]` for
pass-through / grouped columns, `ARRAY[…]::text[]` for dim-mapped
subqueries), LIKE patterns as `'…'`-quoted strings. The console path runs
statements via `adhoc.run_query → exec_driver_sql(sql)` with no parameter
substitution (its own comment at `core/adhoc.py:277-280` spells out why),
so without the inlining the Run button would fail with "A value is
required for bind parameter …". The result is a self-contained SELECT that
returns one row per series — `sum(col)`, `count(*)`, `avg(col)`,
`min`/`max` (or `percentile_cont` for quantile-bands) over the entire
`[t_from, t_to)` window, matching what `updateLegend()` shows in the
right-hand legend of the chart.

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
`currentViewport` (name, group_by, measure, filters with separators, andor,
indent). Called by both `resetAndFetch` and the memory-restore path in
`setActiveSource`.

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
`CLAUDE.md` design-decision note "`interval_end` is the requested window end,
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

The library (`@hgruber/timeseries.js` ≥ 0.10.0) does the rest: `setPartialBins`
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

## Resolution tiers and the cross-fade

Blocks at different `interval`s live side by side in `ts.data[]` (`pushData`
keeps both), and `prepare_grid` draws the finest tier whose bars are at least
`fadeHi` px wide. Across the `fadeHi` → `fadeLo` band both tiers are drawn and
dissolved into each other via `plot._fade` (see the library's CLAUDE.md).

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
  reads that as `sum`); `avg`/`min`/`max`/`quantile` are already per-unit and
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

Three draggable boxes float over the canvas — the legend, the drill-down panel
and the query console — and there will be more. They share one CSS ruleset
(`.fpanel`, `.fpanel-header`, `.fpanel-title`, `.fpanel-actions`,
`.fpanel-close`) and one JS registration (`makeFloatingPanel(el, opts)`), both
in `chart.html`. A new panel is two classes in the markup and one call; it must
not bring its own drag handler, z-index, clamp or Escape wiring.

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
