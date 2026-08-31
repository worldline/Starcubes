# Source profiles

A **source profile** is a YAML file holding one datasource's complete
`starcubes_meta` configuration: the source-side scalars plus its fact
columns, dimensions, indices, measures and group dimensions. It is the unit
in which a configuration is saved, moved between installations, and applied.

**A profile never contains the source DSN.** Credentials are a deployment
secret, not configuration — which is also why a profile applies fine to a
datasource that has no DSN yet.

## Where profiles live

| Directory | Role |
|---|---|
| `profiles/examples/` | Shipped with the repo, tracked in git, read-only from the GUI |
| `profiles/user/` | Saved from a running installation; redirect with `STARCUBES_PROFILE_DIR` |

A name present in both resolves to the **user** copy, so a site can shadow a
shipped example without editing a tracked file.

In a container, `profiles/user/` belongs on a volume — `COPY . /app/` would
otherwise put saved profiles inside the image, where the next build discards
them. See [`docs/deployment-config.md`](deployment-config.md).

## Entry points

- **`manage.py seed_example --profile <name>`** (CLI) — registers a
  datasource and, unless `--skip-initial-ddl` is set, creates the fact table
  and loads dimensions.
- **Profile picker in `/sources/new/`** (GUI) — selecting a profile prefills
  the form from it and seeds the child rows on submit. The form fields stay
  editable, so what you see before clicking Create is what gets stored.
- **Save as Profile / Apply Profile** on the datasource detail page (Basics
  tab) — export the current configuration, or replace it with another.
- **Profiles… dialog** on `/sources/` — the catalogue: use, download,
  upload, delete. Download serves the file as it sits on disk, so an
  upload/download trip round-trips losslessly.

The writes all go through `sources/profile_store.py`, so the CLI and the
browser cannot write a profile differently.

## Adding a profile by hand

1. Drop `profiles/examples/<name>.yaml` (or `profiles/user/<name>.yaml`).
   The filename stem is the profile name; `iter_profiles()` discovers it.
2. Make sure `source.dialect` matches the SQL in
   `fact_columns[*].source_expr`. The GUI warns when the form's dialect and
   the profile's disagree.
3. Reference inline CSVs by basename (`csv:`) or carry them in the file
   (`values:`).

No Python changes required.

## Schema

Top-level keys:

| Key | Required | Purpose |
|-----|----------|---------|
| `profile` | optional | self-identification; the loader uses the filename stem |
| `description` | optional | shown in the picker and the catalogue |
| `source` | yes | source-side scalars (`dialect`, `base_table`, `watermark_*`, …) |
| `partition` | yes | `column`, `period` (month / week / day) |
| `runtime_defaults` | optional | `chunksize`, `transform_procs`, `interval_ladder`, `bar_pixel_target` |
| `defaults` | optional | suggested `name` / `schema_name` for the create form |
| `inline_dimensions` | optional | `{dim_table: {csv: <file>}}` or `{dim_table: {values: <csv text>}}` |
| `fact_columns` | optional | list of fact column entries |
| `dimensions` | optional | list of dimension entries |
| `fact_indices` | optional | list of index entries |
| `measures` | optional | list of measure entries |
| `group_dims` | optional | list of group dimensions |

### `source`

```yaml
source:
  dialect: oracle                 # drives the GUI's dialect-mismatch hint
  base_table: txn
  small_window_hint: "/*+ ORDERED INDEX(txn, TXN3) */"
  watermark_column: end_time
  watermark_max_query: "SELECT max(end_time) FROM txn"
  watermark_fact_expr: "create_time + total * interval '1 millisecond'"
  import_query: null              # null composes it from base_table
  pk_format: null
  late_arrival_overlap_s: 0
  watermark_lag_s: 0
  wallclock_lag_s: 0
```

`dialect`, `base_table` and `watermark_column` are required. Anything else
falls back to the historical defaults (`month` partitions, `10000` chunks,
`1` transform proc, ladder `[1,60,3600,86400]`, `bar_pixel_target=5`).

### `defaults`

```yaml
defaults:
  name: payments
  schema_name: starcubes_payments
```

Suggestions only. `name` and `schema_name` are `UNIQUE` in the database, so
the create form fills them in only when the field is still empty, and
`profile_apply` never touches them on an existing datasource.

### `fact_columns`

```yaml
- {name: txn_id, pg_type: bigint, role: pk, ordinal: 2,
   source_expr: "to_number(txn_id)"}
```

`role` must be one of `time`, `pk`, `measure`, `dimension`,
`auto-dimension`. `pg_type` is one of the strings in
`sources/views.py:PG_TYPES`.

### `dimensions` and `inline_dimensions`

```yaml
- {fact_column: so_peer_1, dim_table: peer, source_table: peer,
   source_key: peer_id, source_name: srv_name, source_description: remark,
   is_primary: true}
```

`source_table: null` marks an inline dimension; its values come from the
matching `inline_dimensions` entry, which carries **exactly one** of:

```yaml
inline_dimensions:
  cncy_code:
    csv: iso4217.csv        # a file, resolved against the repo
  txn_type:
    values: |               # or the CSV text itself
      1,Payment
      2,Refund
```

`dump_profile` always writes `values:`, so an exported profile is a single
self-contained file that survives a download/upload trip. `csv:` stays
supported for hand-written profiles. Two fact columns may share one
`dim_table`; the CSV is then carried once and spliced into both.

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
`quantile-steps-open`, `error-bars`, `candlestick`} — the same eight the
`CHECK` on `starcubes_meta.measure` allows. `percentiles` is a
comma-separated string (e.g. `"5,25,50,75,95"`) or `null`.

### `group_dims`

```yaml
group_dims:
  - dimension: so_peer_1        # fact_column of the carrying dimension
    name: "Peer groups"
    slug: peer-groups           # derived from name when omitted
    position: 0
    groups:
      - {name: Internal, color: "#4c78a8", members: [1, 2, 5]}
```

A profile has no row ids, so the carrying dimension is named by its
`fact_column`. A set naming a fact column the profile does not define is
rejected at parse time; one naming a column absent from the *target*
datasource is skipped at write time.

> **Portability caveat.** `members` are `dim_index` values — positions
> assigned when dimension values are imported, not stable identifiers. For
> an inline dimension (values shipped in the profile) they reproduce. For a
> dimension imported from a live source they do not, so the same group set
> applied elsewhere may bucket the wrong values. Apply Profile warns about
> exactly this, naming the affected dimensions; verify the groups after
> importing dimensions.

## Applying a profile to an existing datasource

`POST /sources/<pk>/profile/apply/` is two-phase, the same shape as
`sync-schema`:

- without `confirm` — a dry-run diff, `{added, removed, changed}` per
  section plus the scalar field changes, and any warnings;
- with `confirm=true` — one transaction that rewrites the scalars, deletes
  every child row and inserts the profile's, then bumps `cache_epoch`.

It is a **replace**, not a merge: what the profile does not define, the
datasource loses. `name`, `schema_name` and `source_dsn` are never touched.
The same ETL interlock as `etl/run/` applies — a 409 while a run or a
forever session is active.

Two warnings are surfaced before the confirm click:

- the fact table holds rows and the profile removes or retypes fact columns
  → run Sync Schema afterwards;
- group dimensions ride on source-imported dimensions → the `dim_index`
  caveat above.

## Validation

`core.profile.parse_profile_text()` raises `ProfileError` (a `ValueError`
subclass) on:

- malformed YAML, or a top level that is not a mapping
- missing `source.dialect` / `source.base_table` / `source.watermark_column`
- missing `partition.column` / `partition.period`, or an unknown period
- unknown `fact_columns[*].role` / `fact_indices[*].method` /
  `measures[*].graph_style`
- missing required keys per row
- an `inline_dimensions` entry with neither or both of `csv` / `values`
- an inline CSV file not resolvable to a regular file
- a `group_dims` entry naming a dimension the profile does not define

Profile **names** are validated separately by `validate_profile_name()`
against `^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$` — no dots, no separators. Names
become filenames, so this is the boundary that keeps a request from writing
outside the profiles directory. Uploads and saves are additionally capped at
`MAX_PROFILE_BYTES` (2 MB) and validated *before* anything reaches disk.

`manage.py seed_example` re-raises `ProfileError` as `CommandError`. The GUI
reports it as a 400 with the message. A broken profile in the catalogue is
listed with its `error` rather than taking the whole catalogue down — and it
no longer prevents the server from starting, as it did when the example
profile was loaded at module import.

## Files

| File | Role |
|------|------|
| `core/profile.py` | loader, catalogue, validation, `dump_profile` |
| `sources/profile_store.py` | the INSERTs, shared by CLI and GUI |
| `profiles/examples/oracle_payments.yaml` | the shipped profile (Oracle payment-transaction demo) |
| `sources/management/commands/seed_example.py` | CLI consumer (`--profile`) |
| `sources/views.py` | the eight profile endpoints (see `docs/sources-app.md`) |
| `sources/templates/sources/new.html` | profile picker + form prefill |
| `sources/templates/sources/list.html` | Profiles… catalogue dialog |
| `sources/templates/sources/detail.html` | Save as Profile / Apply Profile |

## Related

- [`docs/star-schema.md`](star-schema.md) — `fact_column.role`,
  `dimension.*` semantics.
- [`docs/sources-app.md`](sources-app.md) — the `/sources/` URL map.
- [`docs/deployment-config.md`](deployment-config.md) —
  `STARCUBES_PROFILE_DIR` and the container volume.
- [`docs/etl-engine.md`](etl-engine.md) — how `watermark_fact_expr` is
  consumed by the ETL window's upper bound.
