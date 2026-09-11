# Chart rendering deep reference — ladder plots, partial bins, interval_end, autoFetch coalesce, init zoom

The plot-side semantics that distinguish starcubes from a vanilla
`timeseries.js` consumer. Read before editing the plot-related code paths
in `viz/templates/viz/chart.html` or `core/query.py` shape outputs.

**Highest-value section for chart rendering** — read before changing
plot types or fetch semantics. Most of these notes describe a single
class of bug that took longer than expected to isolate, and whose fix
would be undone by a casual refactor.

Other chart-side mechanics (resolution tiers, y-axis density, floating
panels, query console, provisional blocks) live in
[`docs/chart-app.md`](chart-app.md).

## Filter fan-out over shared dimensions

A filter on a fan-out group (see **Filter group** in
[`docs/star-schema.md`](star-schema.md)) emits one dim-value subquery per
member fact column, OR-ed together. Built in `_filter_sql`
(`core/query.py`), so plot, quantiles, drill-down, transaction list and all
copy-SQL variants inherit it. Shapes, for `so_peer` over
`so_peer_1/2/3` on dim table `peer`:

- `in`: `(f.so_peer_1 IN (SELECT index FROM <schema>.peer WHERE source_id = ANY(:flt0))
  OR f.so_peer_2 IN (…) OR f.so_peer_3 IN (…))` — **one** shared `:flt0`
  param reused by every member's subquery (SQLAlchemy `text()` accepts
  repeated named params; `_inline_bind_params` substitutes all occurrences).
- `not_in`: same OR wrapped in `NOT (…)` — AND of NOT IN, i.e. "none of the
  transaction's peers matches".
- `starts_with`/`contains`/…: one `… IN (SELECT index … WHERE name LIKE
  :fltN_j)` per member, same OR/NOT shape; the LIKE patterns are shared
  per value across members.
- Single member / individual fact column (`src_peer`, `so_peer_2`):
  byte-identical to the pre-fan-out single-column fragment — cache keys
  (`core/cache.py` hashes the filter tree verbatim) and the SQL console
  diff against those strings.

Grouped-token filters (`<col>:grouped:<slug>`) do not fan out — see
docs/backlog.md.

## Plot types

- **Ladder plot types (`quantile-bands`, `quantile-steps`, `quantile-steps-open`,
  `error-bars`, `candlestick`) are a distinct plot family, not multibar measures.**
  A measure with one of these `graph_style` values and `agg='quantile(col)'`
  routes `plot_json` to `core/query.timeseries_quantiles` (not
  `timeseries_multibar`). The plot's `data[slot][series]` is a **list** of
  `percentile_cont` values aligned to `plot['percentiles']` (the per-measure
  ladder from `Source.quantiles_for`), not a scalar — every ladder style reads
  the same shape and only differs in `plot["type"]` (and `plot["connect"]` for
  `quantile-steps-open` → `False`, no risers). The library-side renderers:
    - `quantile-bands` — shaded bands between consecutive percentile lines
      (darkest at the median).
    - `quantile-steps` — same lines, drawn as a step (default `connect=True`
      draws risers; `connect=False` drops them — starcubes' `quantile-steps-open`).
    - `error-bars` — central marker + symmetric whiskers over a percentile pair.
    - `candlestick` — boxplot from the ladder (outer pair = whiskers, inner
      pair = body, median tick); no `plot.roles`, so no OHLC shape.
  Since 0.10 the library centralises the array-per-slot discriminator via the
  `values: 'array'` renderer-plugin contract, exposed as
  `TimeSeries.isBandedType(type)` and `TimeSeries.ladderPairs(n)`. Ladder
  blocks are hit-testable from 0.10 on (hidden series excluded), so the chart's
  `openDrillModal` keeps its `plot.type !== 'multibar'` guard — ladder blocks
  have an array `value` and the modal would receive the wrong shape.
  Because the per-slot value is an array, three spots in `timeseries.js` branch
  on the type: `prepare_grid` y-range (max/min array entry per slot),
  `pushData` block-concat min/max, and `get_element` hit-testing (skipped for
  0.9.1, hit-testable from 0.10 on — but starcubes still opts out via the
  multibar guard). Grouped (`:grouped`) percentiles are computed **SQL-side**
  via a `CASE` mapping dim index → group name before `percentile_cont` —
  percentiles cannot be merged in Python the way counts are. Not
  server-cacheable (`is_cacheable` rejects non-`count(*)` measures). The
  vendored bundle lives in `viz/static/timeseries/timeseries.js` (rebuild via
  `./docker/build.sh`). From 0.10.2 on the constructor accepts
  `initialView: [tmin, tmax]` directly (synchronous, before first paint), so
  `chart.html:initTs` no longer needs the post-construction
  `ts.zoom(_, _, 0); ts.stop()` workaround;
  Adjacent fetch blocks are stored as **separate** plot objects in `data[]`
  (only multibar/ladder are concat-trimmed on *overlap*, not on plain
  adjacency). Connected renderers would otherwise leave a one-slot hole at each
  block margin, so `renderers.js:plotData` supports an opt-in `coalesce(plot)`
  key: active blocks of one type sharing the key are merged via `coalesceBlocks`
  (rebasing each block's slots onto the group's earliest `interval_start`, which
  is always a multiple of `interval`) into one synthetic plot drawn once.
  `quantile-bands` opts in with key `name|interval` (`name` is `"{source}:
  {group_by}"`), so only same-source/same-group-by/same-interval blocks merge.
  `quantile-steps` opts in with the same key (step shape, like bands, is
  contiguous); `error-bars` and `candlestick` are bin-local and do not opt in.
  Multibar bars are discrete so they do not need this.
  **`multistep` needs no coalesce either.** The simple family's one
  step-based style draws on the library's `multiline` renderer with
  `plot.step = 'after'` (`core/query.py:SIMPLE_PLOT_STEP`), and `traceRun`
  extends an after-step run by exactly one `binW` past its last slot — which
  lands on the x of the next block's first slot, so the staircase is
  continuous across fetch-block margins, where a plain polyline leaves the
  one-slot hole described above.

## Drawing consequences

- **The bin holding the HWM is drawn narrow and area-true, not full width.**
  The clamp described in the next note is about *coverage* — which spans count
  as loaded. The same fact has a drawing consequence that took longer to
  address: a partly-loaded bin drawn at full width is simultaneously too short
  (its count is incomplete) and too long (it reaches past the data into the
  future). `chart.html:fetchRange` hands the horizon to the canvas as
  `plot.data_until = hwmSeen` — set beside `plot.extensive`, which is why the
  HWM read moved *above* `starcubesPush` — and the library
  (`@hgruber/timeseries.js` ≥ 0.10.2, `setPartialBins('scale')`) draws that bar
  as wide as the bin is filled and divides its height by the same fraction, so
  the bar's area still equals its count. The monotonic `hwmSeen` is used rather
  than the per-response `hwm` so a briefly regressing watermark cannot flicker
  the bar's width. Only `extensive` blocks are scaled — an `avg`/`quantile`
  clips but is never extrapolated, the same line the rate axis draws. Toggle
  follows the `yAxisMode` pattern (`localStorage`, `?partialbins=0`, gear
  popover); it defaults **on** in the chart and `'full'` in the library, which
  must not change any existing consumer's picture. See `docs/chart-app.md`,
  "Drawing the partial bin".

- **The drill-down bar outline shares the same geometry as the ink it frames.**
  The persistent selection (`ts.setSelection({ slotSec, key })`,
  `@hgruber/timeseries.js` ≥ 0.10.4) is painted inside the same draw pass as
  the bars, by the same `highlight_multibar` geometry — so a partial bin's
  outline is as narrow as the bar, and a tier cross-fade never draws two
  outlines. It repaints only while the referenced bar is actually drawn
  (visible series, dominant block, non-skipped partial bin, inside the
  viewport); state survives without painting until the bar returns.

## Fetch semantics

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

## Init

- **The chart's initial zoom must have an explicit duration of `0`, and the
  library's start window is not configurable.** `TimeSeries` always constructs
  on `tmin = now - 24h` / `tmax = now` — there is no constructor option for a
  concrete window, and `initialView` only names a navigation *method*, dispatched
  from a `setTimeout(…, 0)` after the first paint. So `chart.html:initTs()`
  corrects the window after construction, with `ts.zoom(_initTmin, _initTmax, 0)`.
  Without the `0` the call falls back to `zoomDuration` (500 ms) and the page
  opens on "last 24 hours" and visibly slides to the real window — the bug this
  replaced. A zero-duration zoom runs to completion inside the library's
  `animate()` synchronously (span 0 → factor 1, no rAF, no `setTimeout`), so the
  corrected frame is painted in the same task as the constructor's and the 24 h
  one is never composited. Reintroducing a duration, or moving the call behind an
  `await`/`setTimeout`/`requestAnimationFrame`, brings the jump back. The
  `_initTmin`/`_initTmax` locals exist for the same reason: constructing the
  instance fires `onViewportChange`, whose handler would otherwise overwrite
  `currentViewport` with the library's default before it can be read.
  `initTs()` also deliberately has **no** bootstrap instance any more. It used to
  build a throwaway `TimeSeries` on an off-screen canvas before calling
  `TimeSeries.registerSource(...)`, back when the statics only existed once an
  instance had been constructed; they are module-scope since 0.9.1 (every static
  the chart uses — `registerSource`, `themes`, `attachTooltip`, `seriesColor`).
  It was not merely redundant: it passed no `initialView`, so it got `'last24'`,
  which starts a self-rearming `follower_tick` loop — one `plotAll()` per tick,
  for the lifetime of the page, on a canvas already detached by `tmp.remove()`.
  See `docs/chart-app.md`, "Init and the start window".