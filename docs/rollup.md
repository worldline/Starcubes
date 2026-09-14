# Pre-aggregation (rollups)

Read this when touching `core/rollup.py`, `core/rollup_advise.py`, the
`rollup_*` management commands, or the rollup branch in `viz/views.py:plot_json`.

## The problem

Every chart request re-aggregates raw fact rows. `starcubes_meta.query_cache`
(see `core/cache.py`) freezes the finished `plot_json` payload, but only for an
*exactly repeated* request — same datasource, group_by, measure, window,
interval, filter tree and cache epoch. The first look at any window still pays
the full scan. At 20M rows/day that is what limits the chart resolutions
spanning weeks and months.

## Why this is not a cube

Materialising all 2^n marginals with `CUBE` is infeasible. It is also
unnecessary. The base aggregation at bucket granularity over a chosen set of
dimension columns already *contains* every marginal:

```sql
SELECT bucket, cncy_code, response, txn_type, …, count(*)
  FROM fact GROUP BY 1, 2, 3, 4, …
```

Any `group_by` and any filter over those columns falls out of it at query time
through a second, cheap aggregation. Filters work unchanged because
`core.query._filter_sql` resolves every filter — `in`, `not_in`, `starts_with`,
`contains`, grouped tokens — into a set of smallint dim indices before it
reaches SQL. A rollup at index level serves them all.

What cannot be avoided is choosing the column set, and that choice is not
derivable from the schema. Hence `rollup_advise`.

## A rollup is a column set

The core rollup and each marginal are the same mechanism. A marginal is a spec
whose column set has exactly one member. `pick_rollup` returns the **narrowest
spec covering the query**, so there is one notion of coverage in the read path
rather than two.

| Configuration | Table | Serves |
|---|---|---|
| `rollup_columns` | `rollup_1h` | group_by ∈ set **and** all filter columns ∈ set |
| each `rollup_marginals` entry | `rollup_1h_<col>` | group_by = that column, no filter outside it |
| neither | — | `fact` |

The bucket column is **named after `source.partition_column`**. That is what
keeps the query-builder change to three substitutions: to `_build_where` and
the slot expression a rollup looks like a thinner `fact`.

## Multi-tier rollups

There is one bucket width per datasource. `rollup_interval_s` is a scalar,
`rollup_specs` stamps it onto every spec it builds, and `pick_rollup` reads
it from the `Source` rather than from the spec it is testing. A second tier —
`rollup_1h` *and* `rollup_1d` — is not configurable today.

Half the machinery is nevertheless already tier-aware: `RollupSpec.interval_s`
exists, `spec_hash` covers it, and `interval_label` can name `d`/`h`/`m`/`s`
tables. What is missing is the configuration and, less obviously, the pick
rule.

### When a second tier would pay

The compaction a tier buys over the one below it, for a width ratio `f`
(1h to 1d is `f = 24`), follows from the saturation exponent above:

```
factor = f ** (1 - alpha)
```

At `alpha = 0` a daily tier holds a 24th of the hourly rows. At `alpha = 1`
it holds just as many, with a coarser time axis — pure waste. **Factor 10
needs `alpha <= 0.28`.** That is the first thing to measure, and
`rollup_advise` already prints it.

Four more conditions, all of which have to hold:

* **The tier below must still be big in the window people actually open.**
  Rows scanned is buckets x combinations: at ~67k combinations/hour a month
  costs ~48M rows and a tier is worth it; at the ~2.5k/hour of a narrow
  column set the same month is 1.8M rows and already fast.
* **The coarse views must really run at that interval.** `pick_rollup`'s
  first condition is that the bucket width divides the chart interval, so a
  daily tier serves `interval = 86400` and nothing else — 6h bins fall
  straight back to the hourly tier.
* **`query_cache` must not already cover it.** A month view is usually the
  same request every time and hits the cache; a tier only improves its cold
  start. Zooming and panning is where it pays, because no two windows repeat.
* **The column axis must be exhausted first.** Dropping columns was measured
  at 30x on the example schema, and `rollup_marginals` already implements
  exactly that — a second tier on the *column* axis. The time axis can never
  beat 24x. Narrow the columns before widening the buckets.

The same arithmetic kills the other direction. A *finer* tier (`rollup_1m`)
is `60 ** (1 - alpha)` times larger than the hourly one — around 17x at
`alpha = 0.3`. It only pays when the fact-to-hourly factor is well above
that: at the 338x of a narrow column set a minute tier still beats `fact` by
~19x, while at the 12x of the full 14-dimension set the minute table would
come out *larger than `fact` itself*.

### What building it would take

* A list of widths instead of a scalar, in `rollup_interval_s` and in
  everything that reads it.
* A cascading build: the daily tier is grouped from the hourly one, never
  from `fact`. Coarsening is cheap — and it is the one thing that still
  works below `retention_floor`, so `unreconstructable_span` would not apply
  to the upper tier as long as the lower one survives.
* Cascading invalidation: reconfiguring a tier has to stale every tier
  derived from it, and `rewind_rollup_state` has to rewind them together.
* A cost estimate in `pick_rollup` instead of `min(key=len(columns))`. With
  two axes the ordering is no longer total: a wide daily spec can be cheaper
  to scan than a narrow hourly one, and "narrowest wins" would pick the
  slower table.

### A month is not a bucket

`rollup_interval_s` is epoch-aligned seconds. Months have no fixed length, so
2592000 s drifts through the calendar and 604800 s puts week boundaries on a
Thursday. `interval_label` would call it `30d`, which is what it would
actually be.

Nor is there a monthly bin to serve: `starcubesIntervalLadder = [1, 60, 3600,
86400]` (`viz/templates/viz/chart.html`) stops at the day, and the chart bins
by `Math.floor(epochSec / interval) * interval` — the same epoch arithmetic,
with the same objection.

None of which is a loss, because the month people ask for is a *viewport*,
not a bin: a month-wide window drawn in daily bins, which is what the
interval ladder already picks at that zoom. A daily tier would serve it
exactly.

## What it can never serve

**Percentiles.** They do not sum. A `quantile(col)` measure reads `fact`
permanently, and `validate_config` rejects `quantile(col)` in
`rollup_measures` rather than letting it half-work.

Note what that sentence is *about*: the **agg form**, not the graph style.
`LADDER_STYLES` — `quantile-bands`, `quantile-steps`, `quantile-steps-open`,
`error-bars`, `candlestick` — is a set of *shapes*, and two agg forms draw
into it. The other one is `extent(col)`, and it is served from here like any
scalar, because each of its rungs folds:

| Measure | Derived from |
|---|---|
| `count(*)` | `sum(n)` |
| `sum(col)` | `sum(sum_col)` |
| `avg(col)` | `sum(sum_col)/sum(cnt_col)` — correctly weighted across buckets |
| `min(col)` / `max(col)` | the stored extremes |
| `extent(col)` | `ARRAY[min(min_col), sum(sum_col)/sum(cnt_col), max(max_col)]` |
| `quantile(col)` | — nothing. Always `fact`. |

`cnt_col` is `count(col)` and not the row count — with a NULL in the column
the two differ, and `avg` would be pulled toward zero by rows that never
contributed a value.

### The extent measure's four columns

`extent(col)` materialises **all four** stored columns (`sum_`, `cnt_`,
`min_`, `max_`) whatever its rung list says, and that is load-bearing rather
than lazy. The rung list lives in `starcubes_meta.measure.percentiles`, i.e.
in the measure *catalog* — deliberately not in `RollupSpec` and therefore not
in `spec_hash`. Honouring it in `measure_columns` would mean that editing a
measure from `min,max` to `min,avg,max` leaves the hash valid while the read
path selects `sum_col`/`cnt_col` columns that were never created: a hard SQL
error at chart time, not the graceful degradation to `fact` that every other
reconfiguration gets.

The alternative was putting the rung list into `spec_hash`, which would
couple the spec to the measure catalog so that *any* catalog edit could stale
a rollup. The chosen price is 16 bytes per row when the operator picked a
two-rung ladder; what it buys is a rung list that is a pure presentation
choice — freely editable, no rebuild, no hash involvement.

**Why this is the interesting one.** A rollup's aggregates outlive the rows
they came from (see *Historical mutation* below), so below `retention_floor`
an `extent(col)` band still draws while a percentile ladder there is
necessarily empty. It is also the only ladder whose min/max are honest at
every resolution: each tier is its own `GROUP BY`, so the extremes come from
the raw values — or from the stored extremes, which are the same numbers —
never from the averages of a finer tier.

## Measures are cheap, dimensions are not

A measure changes the row *width* (~8-16 B), never the row *count*. A
dimension column multiplies the row count by however many distinct values it
brings into combination with the others. Measured on the example schema:

| Column set | rows/day @ 20M | factor |
|---|---:|---:|
| all 14 dimensions | ~1.6M | 12x |
| without `merch_cat` | ~500k | 40x |
| 11 low-cardinality columns | ~59k | 338x |

One column, `merch_cat` (556 values), costs more than the other thirteen
together. This is why the operator decision is about columns and why
`rollup_advise` exists.

**Coarsening is cheap, refining is not.** Removing a column is a `GROUP BY`
over the remaining columns of the small rollup table; adding one needs a
rebuild from `fact`, because the information is no longer there. Start wide,
narrow later.

| Change | Cost | Reads |
|---|---|---|
| measure added | full rebuild | `fact` |
| measure removed | `DROP COLUMN` | — |
| dimension added | full rebuild | `fact` |
| dimension removed | cheap rebuild | the rollup |

## Time zones: buckets are UTC, partitions are not

Everything that computes a *value* is UTC epoch arithmetic — the bucket
expression, `floor_to_bucket`, the slot expression, `interval_start`, the
cache key, and the chart's own `Math.floor(epochSec / interval) * interval`.
There is no `date_trunc` anywhere in the rollup path, deliberately: it follows
the server's `TimeZone` setting and would put the day boundary somewhere the
chart never asks about.

Partition boundaries are the exception and are *local*: `ensure_partitions_for`
runs its arithmetic through `_to_naive_local`, so a monthly partition on a
Europe/Berlin server is bounded at `2026-04-01 00:00+02`, not at UTC midnight.
That is harmless and deliberate — Postgres routes a row by its value, so where
a boundary sits is a storage decision, not a correctness one. Rollup tables
inherit exactly the same boundaries as `fact`, which is what keeps retention
and housekeeping on one mechanism.

**But the bucket width is also a time-zone decision.** A day bin is a *UTC*
day: for a reader in Berlin it runs 02:00 to 02:00 local. That predates
pre-aggregation and the rollup reproduces it exactly — but it hardens it. With
hourly buckets, moving the product to local day boundaries later would only
change how the query aggregates them. With `rollup_interval_s = 86400` it
becomes a full rebuild, and below `retention_floor` it becomes impossible.
Prefer 3600 unless there is a reason not to.

A related edge, for completeness: in a zone with a half-hour offset (+05:30,
+05:45, +09:30) the partition boundaries do not land on hour boundaries at
all, so an hourly bucket can straddle one. Still harmless for the same reason
— the bucket row is routed by its own start value, and a query reads every
partition its time filter overlaps.

## Correctness: the three guards

**`spec_hash`** over the column list, measure list and bucket width. When it
disagrees with the live configuration the rollup is simply invisible to
`pick_rollup` — the read path goes to `fact`. That is what makes
reconfiguration safe at any moment: a slower chart, never a wrong one. Same
role `cache_epoch` plays for `query_cache`, except derived from the
configuration rather than bumped by an event.

**`built_from` / `built_through`** bound the finished buckets;
`pick_rollup` requires `built_from <= t_from AND t_to <= built_through`. Two
marks, not one, because the initial backfill of a large history runs for hours:
it works *backwards* and pushes `built_from` down, so the newest windows —
the ones people actually look at — become fast first, and a half-finished
backfill can never answer for a range it does not hold.

**Finality.** A bucket is only rolled up once it is below
`hwm - late_arrival_overlap_s`, the same rule `core.cache.is_cacheable`
applies. Above that line an incremental run may still insert late rows.

## Historical mutation

Two opposite gestures, two different answers — both hooked into the single
place each already goes through:

* **Repair** (`delete_fact_from`) rewinds the watermark. The rollup only has
  to forget the same span: `rewind_rollup_state` pulls `built_through` back,
  and the next ETL hook rebuilds it from the reloaded rows. A rewind, not an
  invalidation — nothing is deleted and nothing has to be scheduled.
* **Disposal** (retention, `drop_partition`, `truncate_partition`) removes
  fact rows on purpose and deliberately **leaves the rollups alone**. Their
  rows were computed while the raw data existed, so they stay correct; at two
  orders of magnitude smaller than `fact`, keeping them costs almost nothing.
  Aggregates outliving the rows they came from is the point of this, not an
  accident: retention can be aggressive on `fact` while the chart keeps years
  of counts.

### Below the floor a rollup is the only copy

That is the price, and it inverts the safety of two commands. Above
`retention_floor` a rollup is a cache: `rollup_drop` and `rollup_build
--rebuild` are cheap and fully reversible. Below it `fact` no longer holds the
rows, so a rebuild — which reads `fact` — cannot restore those buckets.

`unreconstructable_span` reports that span, and both commands refuse it
without `--force`. Two further consequences:

* The rollup tables belong in the backup. `fact` is UNLOGGED and unbacked
  because it is reproducible from the source; below the floor a rollup is not.
* Below the floor the answer depends on the question. Counts, breakdowns and
  every derivable measure over the rollup's columns work — including an
  `extent(col)` band, which is the reason that form is worth having;
  percentiles, drill-down to transactions and filters on columns outside the
  spec return nothing, because they read `fact`. `chart_meta` reports
  `rollup_only_below` and the chart shows a one-line note when the viewport
  reaches into that span, so a legitimately empty plot does not read as data
  loss. The note names the missing percentiles only for a `quantile(col)`
  measure (`updateRollupNotice`); for everything else it says no more than
  "no drill-down", which is all that is actually gone.

## Lifecycle

A rollup cannot be configured right after the initial load: which dimensions
carry the load and which measures matter only becomes visible in use. The
sequence is deliberately staggered.

1. **Initial ETL.** `rollup_interval_s = 0` — the default, off.
2. **Use.** Charts read `fact`, the measure catalogue grows, `query_cache`
   accumulates group_by statistics.
3. `manage.py rollup_advise --name <ds>` — the numbers.
4. `manage.py rollup_config --name <ds> --columns … --marginals … --measures …`
5. `manage.py rollup_build --name <ds> --backfill`
6. From here the ETL hook advances it after every load.

The same five steps are the five sub-tabs of the **Rollup tab** on
`/sources/<pk>/manage/` — Status, Advise, Config, Build, Verify — in that
order, and the Advise report's ladder carries a button per stage that fills
the Config form with it. The GUI is a second front end on these commands, not
a second implementation: `advise`, `build` and `verify` are spawned as
detached subprocesses (they run for minutes to hours, and a web request
cannot), so a run started from either side looks the same from the other.
`starcubes_meta.rollup_run` is where they say so — one row per run, with the
`Advice` and the verify matrix stored as JSON, which is what lets a
measurement outlive the terminal it was printed in. See
[`docs/sources-app.md`](sources-app.md) for the endpoints and the two traps
(`rollup_drop` prompts on stdin; `rollup_build` holds the datasource lock).

`manage.py rollup_config --show` prints the configuration and each table's
state (`usable`, `empty`, `STALE (config changed)`, `not built`).

### The measurement

`rollup_advise` does **not** sample. Estimating distinct combinations from a
sample is the unseen-species problem and the error runs the wrong way — a
sample undercounts rare combinations and so flatters the rollup. Instead: one
exact pass over a real window of `fact` into a temp table at bucket
granularity, after which every reported number is an aggregation over that
small table. The same asymmetry the rollup itself exploits.

Two densities give the saturation exponent: combination counts saturate, so a
measurement at one density says little about another. Re-grouping the base
table at 24x the bucket width is an observation at 24x the density.

#### The saturation exponent

The two observations give one number:

```
alpha = log(combos_coarse / combos_fine) / log(coarse_factor)
```

`combos_fine` is distinct tuples per bucket at the configured width,
`combos_coarse` the same at `coarse_factor` (default 24) times that width.
Read the ends:

| alpha | means | a coarser bucket holds |
|---|---|---|
| 0 | fully saturated — every bucket already sees every combination | the same tuples as a fine one |
| 1 | no saturation — each bucket brings tuples no other one had | the sum of its parts |

Real schemas sit between, usually low: the combinations that occur at all
tend to occur every hour.

**What it is for.** Exactly one thing: `_project`, i.e. `--extrapolate-to`.
Without an extrapolation alpha changes no number in the report — the
measurement *is* the answer and the ladder is exact. It is reported anyway
because it is the one figure that says how far the measurement can be
carried to another volume, and because it answers the multi-tier question
below.

**When it is missing.** `advise` refuses to fit on too little evidence:
fewer buckets than `coarse_factor`, or `combos_coarse <= combos_fine` (noise,
not saturation). Then `saturation_exponent` is `None`, a note says so, and
`--extrapolate-to` silently degrades to the identity. The fix is
`--window-buckets`, large enough that the window holds at least
`coarse_factor` of them.

Only alpha is exported, not the two counts it came from — `combos_fine` and
`combos_coarse` stay local to `advise`. It shows up in the CLI's `fit` line,
in `--json`, in `starcubes_meta.rollup_run.report`, and in the Rollup tab's
Advise report.

`--extrapolate-to <rows_per_hour>` projects onto an expected future load. On a
datasource already carrying its real volume, leave it alone — the measurement
*is* the answer, and extrapolating from it can only add error.

The projection is capped at rows-per-bucket, **not** at K (the distinct tuples
in the measured window). K was the obvious cap and is wrong: a longer window
keeps finding combinations a shorter one never saw, so K measured over a day
is a lower bound on the real asymptote. Capping with it understates
combinations and overstates the speed-up — error in the dangerous direction.
Measured against a 27-day hand count it turned a factor of 14 into 48.

### Missing indices distort the measurement

`rollup_advise` calls `core.preflight._check_indices` first and warns. Without
BRIN on the partition column every measurement scans the whole partition
instead of the window, and the operator would be comparing a rollup against a
fact table in its worst possible state. That state is exactly where a
datasource sits right after its initial load — which is when this command gets
run. Run `manage.py create_indices` first.

## Verification

`manage.py rollup_verify --name <ds>` is the sharpest check: it runs a matrix
of chart queries twice, once against `fact` and once against the rollup
`pick_rollup` chose, and compares the plot payloads cell by cell. The matrix
covers each group_by (plain and grouped), each filter form, both interval
steps and every derivable measure.

A case the rollup cannot serve counts as **skipped**, not failed — a skip is
the read path correctly falling back to `fact`.

`--days` bounds the comparison window (default 3). Correctness does not depend
on window size, but every case runs the fact query too.

The chart's advertised time extent follows the same rule: `api_source_meta`
clamps `time_range.min` to `retention_floor`, except where a rollup reaches
further back, in which case it widens to the oldest bucket held.

## The read path, in order

```
plot_json
  → query_cache          (exact repeat of a frozen payload)
  → pick_rollup          (narrowest covering spec, or None)
  → fact                 (always correct)
```

`pick_rollup`'s seven conditions, cheapest rejection first: bucket width
divides the interval; window on bucket boundaries; spec hash current and
window inside `[built_from, built_through)`; not a *percentile* ladder (an
`extent(col)` one passes — the gate is the agg form, not the style); measure
derivable *and* materialised; group_by column (after `split_grouped_col`) in
the spec; every column a filter reads in the spec — resolved through
`filter_target_columns`, the helper `_filter_sql` itself uses, so the coverage
test and the WHERE clause cannot drift apart.

## Known limitation

The Python-side merge for grouped dimensions (`core/query.py`, the
`group_by_grouped` branch) **sums** per-index values. That is right for
`count(*)` and `sum()`, wrong for `avg`, `min` and `max` — it adds the
averages or the extremes of the group members. This predates pre-aggregation
and lives on the `fact` path; the rollup reproduces it deliberately so
`rollup_verify` stays meaningful. Fixing it belongs in both paths at once and
is in `docs/backlog.md`.
