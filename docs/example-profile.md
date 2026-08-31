# Example profiles

An **example profile** is a YAML file under `examples/<name>.yaml` that
describes one example datasource — its source-side scalars, fact columns,
dimensions, indices and measures. Profiles power two entry points:

- **`manage.py seed_example`** (CLI) — `--profile <name>` (default
  `oracle_payments`). Registers a datasource in `starcubes_meta` and (unless
  `--skip-initial-ddl` is set) creates the fact table + loads dimensions.
- **"Preload example … configuration"** checkbox in `/sources/new/` (GUI) —
  inserts the same profile into `starcubes_meta` when creating a
  datasource via the create form.

Both call `core.profile.load_profile()` and share the same loader, so the
two paths cannot drift.

The profile is **meta only**: it carries no example fact rows. Dimension
content still comes from the source (`core.schema.import_dimensions`) or
from an inline CSV listed under `inline_dimensions`.

## Adding a new profile

1. Drop `examples/<name>.yaml`. The filename stem is the profile name —
   `iter_profiles()` discovers it; `--profile <name>` selects it.
2. Make sure `source.dialect` matches the source SQL you write in
   `fact_columns[*].source_expr`. The GUI warning in `/sources/new/`
   surfaces the profile's dialect, so a non-Oracle profile gets a
   different warning text for free.
3. Reference inline CSVs by basename or relative path; the loader
   resolves them against the project root or the `examples/` dir
   (`core.profile._resolve_inline_csv`).

No Python changes required.

## Schema

Top-level keys:

| Key | Required | Purpose |
|-----|----------|---------|
| `profile` | optional | self-identification; loader uses the filename stem |
| `description` | optional | shown in the GUI as the Seed Data hint |
| `source` | yes | source-side scalars (`dialect`, `base_table`, `watermark_*`, …) |
| `partition` | yes | `column`, `period` (month / week / day) |
| `runtime_defaults` | optional | `chunksize`, `transform_procs`, `interval_ladder`, `bar_pixel_target` |
| `inline_dimensions` | optional | `{dim_table: {csv: <filename>}}` |
| `fact_columns` | optional | list of fact column entries |
| `dimensions` | optional | list of dimension entries |
| `fact_indices` | optional | list of index entries |
| `measures` | optional | list of measure entries |

### `source`

```yaml
source:
  dialect: oracle                 # used by the GUI's "warn if non-Oracle" hint
  base_table: txn
  small_window_hint: "/*+ ORDERED INDEX(txn, TXN3) */"
  watermark_column: end_time
  watermark_max_query: "SELECT max(end_time) FROM txn"
  watermark_fact_expr: "create_time + total * interval '1 millisecond'"
```

`dialect`, `base_table` and `watermark_column` are required. Anything else
falls back to the historical defaults (`month` partitions, `10000` chunks,
`1` transform proc, ladder `[1,60,3600,86400]`, `bar_pixel_target=5`).

### `fact_columns`

```yaml
- {name: txn_id, pg_type: bigint, role: pk, ordinal: 2,
   source_expr: "to_number(txn_id)"}
```

`role` must be one of `time`, `pk`, `measure`, `dimension`,
`auto-dimension` — the same set `core/source.py:_load_dimensions` reads.
`pg_type` is one of the strings in `sources/views.py:PG_TYPES`.

### `dimensions`

```yaml
- {fact_column: so_peer_1, dim_table: peer, source_table: peer,
   source_key: peer_id, source_name: srv_name, source_description: remark,
   is_primary: true, inline_values: null}
```

`source_table: null` marks an inline dimension; `inline_values` is then
populated from the matching entry under `inline_dimensions`.

### `fact_indices` / `measures`

```yaml
fact_indices:
  - {column_name: create_time, method: brin}

measures:
  - {name: "Total amount", agg: "sum(amount)", graph_style: multibar,
     is_default: false, ordinal: 2, percentiles: null}
```

`method` ∈ {`brin`, `btree`, `hash`}; `graph_style` ∈ {`multibar`,
`multiline`, `multipoint`, `quantile-bands`, `quantile-steps`,
`quantile-steps-open`, `error-bars`, `candlestick`}. `percentiles` is a
comma-separated string (e.g. `"5,25,50,75,95"`) or `null`.

## Validation

`core.profile.load_profile()` raises `ProfileError` (a `ValueError`
subclass) on:

- missing or unreadable YAML file
- malformed YAML
- top-level not a mapping
- missing `source.dialect` / `source.base_table` / `source.watermark_column`
- missing `partition.column` / `partition.period`
- unknown `fact_columns[*].role` / `fact_indices[*].method` /
  `measures[*].graph_style`
- missing required keys per row
- inline CSV file not resolvable to a regular file

`manage.py seed_example` re-raises `ProfileError` as `CommandError` so the
message reaches stderr. The GUI path catches it during module import
(`_EXAMPLE_PROFILE = _load_example_profile()`) — a broken profile fails
fast at server startup, not on the first POST.

## Files

| File | Role |
|------|------|
| `core/profile.py` | loader (`load_profile`, `iter_profiles`), dataclasses, validation |
| `examples/oracle_payments.yaml` | the shipped profile (Oracle payment-transaction demo) |
| `sources/management/commands/seed_example.py` | CLI consumer (`--profile` flag) |
| `sources/views.py` (`_insert_seed_children`, `source_new`) | GUI consumer |
| `sources/templates/sources/new.html` | Seed Data card; warning text uses `example_profile.dialect` |

## Related

- `docs/star-schema.md` — `fact_column.role`, `dimension.*` semantics.
- `docs/sources-app.md` — `/sources/new/` URL structure.
- `docs/etl-engine.md` — how `watermark_fact_expr` is consumed by the
  ETL window's upper bound.