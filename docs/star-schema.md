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
  `dimension` row is `is_primary = true`. That one supplies the group's
  `dim_table` for the meta API's `dim_filters` payload; it still drives DDL,
  dim import, and the `read_dimensions` mapping. The rest only contribute
  their fact-column-to-dim-table link for the transform stage.
- **Filter group** — `starcubes_meta.dimension.filter_group` (text, NULL):
  rows sharing a non-null value form one **fan-out chart filter** — the UI
  offers a single merged row (token = the group name, e.g. `so_peer` over
  `so_peer_1/2/3`), and the server emits
  `(f.so_peer_1 IN (…) OR f.so_peer_2 IN (…) OR f.so_peer_3 IN (…))`
  (`NOT (… OR …)` for negative ops — "kein Member der Txn trifft zu").
  Group membership is deliberately **not** dim_table-based: `src_peer`
  shares `peer` but keeps its own single-column filter. Invariants (validated
  in `Source.load`, core/source.py): members share one `dim_table`; the group
  name must not contain `:` (collides with the `<col>:grouped:<slug>`
  grammar) and must not equal a fact-column name. Grouped-token filters
  (`<col>:grouped:<slug>`) do **not** fan out — see docs/backlog.md.
  NULL = ordinary single-column behaviour.
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

- **Schema names are validated, not quoted.** `datasource.schema_name` is
  interpolated **unquoted** into ~70 f-string SQL statements across `core/`
  (`f"{schema}.fact"` and friends), while pandas' `to_sql` quotes whatever it
  is handed. Postgres case-folds an unquoted identifier, so a name that is
  not already lower-case makes the two disagree: `CREATE SCHEMA Test` creates
  `test`, every `information_schema` lookup then compares against `Test` and
  finds nothing, and `to_sql` finally tries to create its table in a `"Test"`
  that does not exist. The fix constrains the name rather than quoting the
  call sites — `core/schema.py:validate_schema_name` on
  `^[a-z_][a-z0-9_]{0,62}$`, with a `NOT VALID` CHECK as backstop. Those
  sites are *consistently* unquoted and therefore correct for every name that
  survives case-folding unchanged; quoting them would break each installation
  whose schema already sits folded on disk. Validation also closes the
  DDL-injection hole the raw interpolation leaves open. `source_edit` skips
  the check when the name is unchanged, so rows predating it stay editable.

- **`ensure_schema` is the one place that creates a datasource's schema.**
  Called from `create_fact`, `create_dim_tables` *and* `import_dimensions`:
  importing dimensions before the fact table exists is a legitimate order of
  operations, and an inline dimension needs no source connection at all, so
  no single one of them can be assumed to have run first.

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

- **A dimension column holds surrogate indices, never the source's own
  values — and once the two are mixed they cannot be told apart.**
  `fk_<fact_column>` references `<dim_table>.index`, the `smallserial`
  surrogate, not the source's code. A column loaded *before* it was
  configured as a dimension holds the raw value instead, and nothing notices
  until `core/schema.py:create_indices` first adds the key:

  ```
  (psycopg2.errors.ForeignKeyViolation) insert or update on table
  "fact_y2026m04" violates foreign key constraint "fk_cncy_code"
  DETAIL: Key (cncy_code)=(978) is not present in table "cncy_code".
  [SQL: ALTER TABLE ... ADD CONSTRAINT fk_cncy_code
   FOREIGN KEY(cncy_code) REFERENCES ...(index)]
  ```

  Two things in that message mislead. The statement is `ADD CONSTRAINT`
  *without* `NOT VALID`, so Postgres is validating rows that are already
  there and merely words it as an insert — nothing is being loaded, and the
  named partition is where the offending row sits, not where a write went.
  And the reported value is a `source_id` (978 is ISO 4217 for EUR), which
  *is* present in the dim table — just not in the column the key points at,
  which is why "but 978 is definitely in there" and the error are both true.

  A running ETL cannot produce this state. `core/etl.py:_map_dimensions`
  (~line 433) routes every unmappable value to the `__unknown__` sentinel and
  records it in `starcubes_meta.etl_unmapped`, so a raw value never reaches
  `fact` while the column is configured as a dimension. Finding one means the
  rows predate the dimension.

  Diagnosis — which values, in which partitions:

  ```sql
  SELECT f.tableoid::regclass AS partition, f.<col>, count(*)
    FROM <schema>.fact f
    LEFT JOIN <schema>.<dim> d ON d.index = f.<col>
   WHERE d.index IS NULL
   GROUP BY 1, 2 ORDER BY 1, 3 DESC;
  ```

  **A `--backfill` does not repair this.** It de-dupes the whole window
  against the PKs already in `fact` (`core/etl.py`, `import_fact` docstring),
  so the broken rows survive and the corrected ones are discarded as
  duplicates. Either the rows go first — Truncate Partition, then backfill,
  remembering that truncating sets `datasource.retention_floor` — or they are
  rewritten in place by `core/schema.py:_backfill_fact_column`.

  **The trap in rewriting in place:** that function's
  `WHERE f.<col>::text = d.source_id` matches *every* row whose value reads
  like a `source_id`, including rows that are already correct. It is safe
  only where it is called from — immediately after a dim table was newly
  created (`if created:` in `import_dimensions`), when nothing is mapped yet.
  Invoked at any later point it re-maps correct rows: a row holding index `3`
  becomes the index of `source_id = '3'`. Restricting it to rows with no
  partner in `dim.index` fixes that much, but leaves a second class
  untouched — a raw value that happens to collide with a valid index
  violates no key, cannot be found, and stays silently wrong while the
  foreign key reports green. Whether that class can exist at all is
  decidable:

  ```sql
  SELECT count(*) FROM <schema>.<dim>
   WHERE index::text IN (SELECT source_id FROM <schema>.<dim>);
  ```

  Zero means every row is unambiguous and an in-place repair is complete.
  Non-zero means only reloading the affected range gives certainty. For
  ISO-code dimensions the two ranges are disjoint in practice; for
  auto-dimensions they routinely are not, since their `source_id`s are often
  small integers and the indices start at 1. See
  [`backlog.md`](backlog.md) for why no automatic repair exists.
