# Star schema — domain vocabulary and schema decisions

The conceptual model of the data warehouse: fact/dim tables, the
per-datasource schema layout, the partitioning scheme, and the
clamping/encoding rules. **Highest-value section for the schema** —
read before changing `core/schema.py`, `core/source.py` (the dataclasses),
or anything that adds or removes dimensions, measures, or partitions.

This is the reference for what a *starcubes* schema *is*; the runtime
behaviour of the ETL that *populates* it lives in
[`docs/etl-engine.md`](etl-engine.md).

## Domain vocabulary

- **Fact table** — `<schema_name>.fact`, one row per transaction. `RANGE`
  partitions on the time column, `UNLOGGED`, BRIN on the time column.
  Partition granularity is configurable per datasource (`partition_period`
  ∈ `'day' | 'week' | 'month'`, default `'month'`; weekly partitions start
  on the ISO Monday). Dimension columns are `smallint` foreign keys.
- **Dimension table** — `<schema_name>.<dim>`: `index smallserial PRIMARY
  KEY, source_id text, name text, description text`. Max 32 767 rows per
  dim (signed `smallint` limit). The neutral `source_id` column replaces
  any source-specific key names.
- **Primary dimension** — when several fact columns share a dim table
  (e.g. `so_peer_1/2/3` and `src_peer` all reference `peer`), exactly one
  `dimension` row is `is_primary = true`. That one drives DDL, dim import,
  and the `read_dimensions` mapping; the rest only contribute their
  fact-column-to-dim-table link for the transform stage.
- **Inline dimension** — a dimension backed by a fixed CSV value set
  instead of a source DB lookup. `inline_values` text column on
  `starcubes_meta.dimension` holds `source_id,name[,description]` lines. When
  set (and `source_table` is null), `import_dimensions` populates the dim
  table from the CSV. `source_table` and `inline_values` are mutually
  exclusive.
- **Auto-dimension** — a `fact_column` whose `role = 'auto-dimension'`.
  Same smallint storage as a regular dim, same `dimension` metadata row
  (admin creates one with `dim_table` and `is_primary = true`), but the
  initial value set is unknown. The ETL parent process discovers new
  source values per chunk and INSERTs dim rows on the fly with
  `name = source_id` and `description = 'auto-discovered <UTC ts>'`. The
  32 767 ceiling still applies — use the GUI's cardinality estimate
  before promoting a column.
- **High watermark (HWM)** — `max(watermark_column)` in the source. The
  ETL loads `[last_successful_hwm - late_arrival_overlap_s, current_hwm)`
  on each run; only `kind='incremental'` runs advance it.
- **Late-arrival overlap** — `datasource.late_arrival_overlap_s`, the number
  of seconds the load window reaches back *past* the HWM on every run,
  because the source does not promise that rows become visible in watermark
  order. Rows already loaded are dropped by PK in the extract loop. Default
  0 (historical behaviour). See [`docs/etl-engine.md`](etl-engine.md),
  "Load window reaches back past the HWM".
- **Watermark lag / wall-clock lag** — `datasource.watermark_lag_s` and
  `wallclock_lag_s`. The window's *upper* bound is
  `max(max_wm − watermark_lag_s, src_now − wallclock_lag_s)`, the second
  term only when it is non-zero. Unlike the overlap this never re-fetches
  and never needs a PK check, and nothing is written behind the HWM — which
  is why `core/cache.py` subtracts the overlap but not these. `wallclock_lag_s`
  must exceed `watermark_lag_s` (CHECK `datasource_wallclock_lag_check`).
  Both default 0, which reproduces the pre-0023 window exactly. See
  [`docs/etl-engine.md`](etl-engine.md), "Window's upper bound is the
  later of two terms".
- **Backfill run** — `import_fact(..., backfill=True)` /
  `manage.py etl_run --backfill --start … --end …`. Re-loads one closed
  historical window, de-duping the whole window by PK, and is recorded with
  `etl_run.kind='backfill'` so it cannot move the HWM.
- **Dimension group** — a named bucket (e.g. "OK", "Error") collecting one
  or more dim rows via `dim_group_member`. Always belongs to exactly one
  group dimension.
- **Group dimension** — a row in `starcubes_meta.dim_group_set`: a *named*
  bucketing of one native dimension, with a user-given `name` and a derived
  URL `slug`. A dimension may carry arbitrarily many (e.g. `peer` bucketed
  both by country and by peer type), so the chart addresses one via the
  token `<fact_column>:grouped:<slug>`. The bare pre-0020 token
  `<fact_column>:grouped` still resolves — to the dimension's first set.
  A dim value may be assigned in several group dimensions at once, but only
  to one group *within* each. Created and edited in the `/sources/<id>/`
  Group Dims tab.
- **ETL forever session** — a background loop (`manage.py etl_run
  --forever`) that sleeps N seconds between incremental passes. Tracked
  in `starcubes_meta.etl_forever` with PID, `stop_requested` flag, and status
  (`running` / `stopping` / `stopped` / `failed`). At most one active
  session per datasource.
- **Measure** — a row in `starcubes_meta.measure`: the user-facing catalog of
  measurements in the chart's measure dropdown. `agg` is an aggregation
  expression — the literal `count(*)` or `func(col)` (func ∈ sum/avg/min/max/
  quantile, col a `role='measure'` fact column). `name` is the dropdown label;
  `graph_style` picks the `timeseries.js` plot type. The ten accepted values
  fall into two families:
    - **Simple family** (one scalar per slot/series): `'multibar'`,
      `'multiline'`, `'multipoint'`, `'stackarea'` (stacked filled bands,
      sums visible series per slot), `'waterfall'` (cumulative bars; each
      starts where the last ended).
    - **Ladder family** (one percentile array per slot/series, fed by
      `agg = quantile(col)` + `percentiles` ladder): `'quantile-bands'`,
      `'quantile-steps'`, `'quantile-steps-open'`, `'error-bars'`,
      `'candlestick'`. All five read the same per-slot array; only the
      renderer differs (see [`docs/chart-rendering.md`](chart-rendering.md),
      "Ladder plot types").
  `Source.load` rejects any other `graph_style` value with a `ValueError`
  (see `_VALID_GRAPH_STYLES` in `core/source.py`); the whitelist is kept
  local to avoid the `query` → `source` → `query` circular import.
  `percentiles` is a comma-separated ladder (`'25,50,75'` or `'5,25,50,75,95'`)
  consumed by every ladder style.
  One row may be `is_default`
  (partial unique index `ux_measure_default`). Distinct from
  `fact_column.role='measure'`, which stays the low-level "aggregatable +
  smallint-clamped" column marker. The dropdown maps the `count(*)` agg to an
  empty `measure=` param (so `core/query._parse_measure` never sees a bare
  `count(*)`); other aggs pass through. `plot_json` resolves `graph_style` via
  `Source.graph_style_for(agg)` → `plot['type']`. A source with **zero**
  measure rows falls back to the historical implicit `count(*)` + auto sum/
  avg/min/max list.
- **Default source** — `datasource.is_default` (partial unique index
  `ux_datasource_default`, at most one true). The chart's `loadSources()`
  picks the flagged source when no `?name=` is given, else first-by-name.
- **No-breakdown group-by** — `group_by` empty (`""`, the chart's `(none)`
  option) means no series split: `timeseries_multibar` emits one constant
  `'(all)'` series per slot. `_build_bar_where` drops the `gb_where` clause
  for an empty `group_by`, so drill/txn endpoints work on an `(all)` bar
  (drill then breaks down by every dimension, since none is excluded).

## Schema-level design decisions

These are the constraints and conventions that shape every `<schema_name>`
in the warehouse. They are stable unless this document changes.

- **One Postgres schema per datasource** (`starcubes_<name>`), not one shared
  fact table. Sources have incompatible shapes; a shared fact would force
  a lowest-common-denominator model. Isolation is cheap.

- **Partition naming sorts chronologically as a plain string within one
  period.** `core/schema.py:list_fact_partitions` orders by `relname`;
  `create_future_partitions` parses the last name to find the next
  boundary. Three fixed-width zero-padded shapes:
    - month: `fact_yYYYYmMM`
    - week:  `fact_yIIIIwWW` (ISO year + ISO week)
    - day:   `fact_yYYYYmMMdDD`
  Mixing periods on a single source is not supported — the application
  layer rejects `partition_period` changes once any partition exists.

- **`_floor_to_period` / `_next_period` use naive midnight `datetime`s.**
  Adding `timedelta(days=1)` / `(days=7)` to a naive midnight is exact (no
  tzinfo, no DST drift) — the `partition_column` is read in source-DB
  time and stored as wall-clock. Month arithmetic uses `replace(month=...)`
  with explicit Dec→Jan handling.

- **`smallint` dimension keys** — the point of the whole compact-star model.
  2 bytes per fact row instead of the source's 4–20 byte key. The 32 767
  ceiling is a deliberate tradeoff. High-cardinality dims do not belong here.

- **`cncy_code` has role `dimension` with an inline CSV dimension.** It's a
  currency code stored raw as smallint; no source lookup table. The ETL
  only clamps `role='measure'` integer columns (`stay`, `total` — see
  `SMALLINT_MAX`/`BIGINT_MAX` in `core/etl.py`), so `cncy_code` passes
  through the transform stage mapped to dim indices, unclamped, via the
  `inline_values` field on the dimension row (ISO 4217 mapping, e.g.
  `978,EUR,Euro`). Do **not** reclassify it as `measure` — it would be
  clamped to `[0, 32767]` and both negative values and values above
  32 767 would be silently rewritten.

- **Integer measure columns must be non-negative and in range — the ETL
  enforces this by clamping, silently, not by failing the run.** A
  `role='measure'` fact column with `pg_type` `smallint` or `bigint` is
  assumed to be a count/sum/duration that is never legitimately negative.
  `core/etl.py:_transform_worker` clamps every such column to
  `[0, SMALLINT_MAX]` / `[0, BIGINT_MAX]` via pandas `.clip()` (`NaN`
  passes through unchanged) before the `COPY`. This replaced an
  upper-bound-only clamp after a production run failed with
  `NumericValueOutOfRange` on a negative `total` (an elapsed-duration
  `source_expr` that went negative on clock skew / out-of-order
  timestamps — see `sources/management/commands/seed_example.py`'s
  `TOTAL_EXPR` for the shape of expression that can do this). The clamp
  is silent by design (consistent with the pre-existing upper-bound
  truncation), which is a real tradeoff: a negative or overflowing
  `source_expr` result becomes indistinguishable from a legitimate `0`
  or type-max value instead of surfacing as a data-quality signal. Anyone
  adding a new integer measure column whose `source_expr` could go
  negative or overflow should know it will be clamped, not preserved or
  flagged.