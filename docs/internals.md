# starcubes internals

Developer- and operator-facing reference for starcubes. Read
[`../README.md`](../README.md) first for what the app is and what it looks
like, and [`../HOWTO.md`](../HOWTO.md) for driving the GUI. This document
covers the architecture, the repo layout, the command-line quick start and
the anatomy of a datasource definition.

A single-app datawarehouse. Pulls rows from arbitrary SQL sources (Oracle,
Postgres, MySQL, MSSQL) into a compact star schema on Postgres and serves them
as stacked-bar timeseries with arbitrary dimension filters.

Successor to the earlier hard-coded ETL in `star.py`. Optimised for
**minimal storage** (smallint-keyed dimensions, BRIN index, unlogged monthly
partitions), **maximum load throughput** (three-stage extract / transform /
load pipeline with psycopg `COPY FROM`), and **configurability** (every
datasource lives as metadata rows in `starcubes_meta`, editable from a GUI).

## Component map
- `core/` — framework-agnostic ETL engine. Must not import Django.
  - `source.py` — `Source` / `FactColumn` / `Dimension` / `FactIndex`
    dataclasses, `Source.load`, the shared SQLAlchemy engine cache
    (`_get_cached_engine`), and the per-dialect watermark/clock expressions.
  - `schema.py` — DDL (`create_fact`, `create_dim_tables`, `create_indices`,
    `drop_indices`, `sync_schema`, `drop_schema`), dimension import/read, the
    partition lifecycle, and `ensure_starcubes_meta`.
  - `etl.py` — the three-stage extract / transform / load pipeline, window
    resolution and high-watermark handling, throughput metrics, and the
    maintenance operations the GUI's ETL tab calls.
  - `query.py` — `timeseries_multibar`, the JSON producer behind the chart,
    plus the shared filter-tree builder and its validation.
  - `alarm.py` — alarm evaluation over the trailing window, driven by the ETL
    after every load. See [`alarm.md`](alarm.md).
  - `audit.py` — source-vs-fact reconciliation in three modes. See
    [`etl-audit.md`](etl-audit.md).
  - `cache.py` — server-side cache for frozen `plot_json` responses.
  - `adhoc.py` — read-only ad-hoc SQL behind the chart's query console.
  - `profile.py` — source profiles (YAML ⇄ metadata rows). See
    [`source-profiles.md`](source-profiles.md).
  - `utils.py` — shared helpers (`_parse_dt`, `_pid_alive`, `tail_file`,
    `format_pks`).
- `starcubes/` — the Django project: settings, URLs, WSGI, the custom Postgres
  backend package, `RequireLoginMiddleware`, and the optional Keycloak/OIDC
  auth backend.
- `sources/` — datasource CRUD, the ETL monitor, alarms and firings. See
  [`sources-app.md`](sources-app.md).
- `viz/` — the chart page, its JSON endpoints and the vendored
  `@hgruber/timeseries.js`. See [`chart-app.md`](chart-app.md) and
  [`chart-rendering.md`](chart-rendering.md).
- `migrations/` — numbered, idempotent SQL migrations plus the consolidated
  `starcubes_meta.sql`.
- Management commands under `sources/management/commands/` — see the
  reference below.

Known unfinished work is tracked in one place: [`backlog.md`](backlog.md).

## Why this exists
No single OSS project delivers "configure an arbitrary SQL source → compact
star schema with smallint-keyed dimensions → fast filterable stacked-bar
timeseries" as one self-hosted app. Airbyte / Meltano handle the source side
but not the star-schema shape. Superset / Metabase / Grafana handle viz but
expect data already modelled. The closest combo — Airbyte + dbt + Superset —
is three tools, three UIs, and you still write the star models by hand.

## Architecture
```
                              starcubes_meta.*
                        (one datasource per row,
                         edited from the GUI)
                                   |
                                   v
  +------- source DB ------+  +-- core/etl.py --+  +----- Postgres ------+
  | Oracle / PG / MySQL /  |  |  extract  ----> |  |  starcubes_meta         |
  | MSSQL (SQLAlchemy URL) |->|  transform ---> |->|  starcubes_<name>       |
  +------------------------+  |  load (COPY)    |  |    fact (part.)     |
                              +-----------------+  |    dim_*            |
                                                   +---------------------+
                                                             ^
                                                             |
                                                      viz / filter UI
                                                      (Django + timeseries.js)
```

- One Postgres schema per datasource (`starcubes_<name>`). Different sources
  have incompatible shapes, so they stay isolated — same DB, separate schemas.
- Per-datasource fact table: monthly `RANGE` partitions, unlogged, BRIN on the
  time column, smallint foreign keys into dim tables.
- Dim tables: `smallserial` primary key, `source_id text`, `name`, `description`.
  Max 32 767 values per dim.
- Three flavours of dimension: **regular** (values imported from a source
  lookup table), **inline** (CSV in metadata, e.g. ISO 4217 codes), and
  **auto-dimension** (no upfront values; the ETL discovers new source values
  on the fly and inserts dim rows for them).

## Repo layout
```
core/                       framework-agnostic ETL core (no Django imports)
  source.py                 Source / Dimension / FactColumn / FactIndex dataclasses
  schema.py                 DDL + dim import/read + sync_schema + ensure_starcubes_meta
  etl.py                    three-stage extract/transform/load engine + maintenance ops
  query.py                  timeseries_multibar JSON producer
  cache.py                  server-side cache for frozen plot_json responses
  adhoc.py                  read-only ad-hoc SQL for the chart's Query console
  utils.py                  shared utilities (_parse_dt, _pid_alive)
starcubes/                      Django project (settings, urls, wsgi, middleware)
  middleware.py              RequireLoginMiddleware + SQLAlchemyPoolMiddleware
viz/                        Django app: chart page + JSON endpoint
  views.py                  chart, plot_json, api_sources, api_source_meta, query_* console endpoints
  urls.py
  apps.py
  templates/viz/chart.html  filter bar + canvas + follow-mode toggle + keyboard pan + prefs popover + query console
  templates/registration/login.html  login page (theme-aware)
  static/                    starcubes static assets (logo, slides, vendored timeseries.js)
migrations/
  starcubes_meta.sql        consolidated meta schema (idempotent) — what a fresh install applies
  000N_*.sql                numbered incremental migrations, idempotent, applied in order
sources/                      Django app: datasource CRUD + ETL monitor GUI
  management/commands/
    etl_run.py               manage.py etl_run — incremental ETL, forever mode, maintenance actions
    etl_housekeeping.py      manage.py etl_housekeeping — targeted ANALYZE + BRIN summarisation, dim fill, partition sizes
    seed_example.py           manage.py seed_example — seed example Oracle payment-transaction datasource
    query_multibar.py        manage.py query_multibar — multibar JSON for offline testing
    create_indices.py         manage.py create_indices — build FKs + indices
    drop_indices.py           manage.py drop_indices — drop FKs + indices
    query_cache_clear.py     manage.py query_cache_clear — flush server-side plot cache
examples.sql                reference SQL for the viz JSON shape
Dockerfile                  multi-stage: JS vendor (npm ci @hgruber/timeseries.js) → Python deps → nginx+uwsgi runtime
package.json + package-lock.json  frontend JS dependency, pinned ~0.10.3
docker/
  build.sh                  build wrapper — defaults CONTAINER_ENGINE=podman, delegates to the build engine
  entrypoint.sh              runs manage.py bootstrap (migrate + collectstatic), then nginx+uwsgi
requirements.txt
```

## Quick start
Prerequisites: a Postgres DB for the warehouse, and an Oracle source DB
(for the example seed). The Dockerfile sets all three up.

To build the container image, always use `docker/build.sh` rather than
calling `podman build`/`docker build` directly — the wrapper defaults the
container engine to `podman` and pins the working directory to the repo
root:

```bash
./docker/build.sh                 # uses podman by default
CONTAINER_ENGINE=docker ./docker/build.sh
```

The `jsbuilder` Dockerfile stage runs `npm ci` against the top-level
`package.json`/`package-lock.json` to fetch `@hgruber/timeseries.js@~0.10.3`
from the npm registry and drops the pre-built bundle into
`viz/static/timeseries/timeseries.js`.

```bash
# 1. Apply the metadata schema (idempotent — safe to re-run).
#    The consolidated starcubes_meta.sql creates the full schema including
#    incremental tables; you can also run the numbered 000N_*.sql files
#    in order if upgrading from an older install.
psql "$STARCUBES_POSTGRES_DSN" -f migrations/starcubes_meta.sql

# 2. Seed the example datasource (registers config + creates fact/dim DDL +
#    loads dimensions from Oracle).
export STARCUBES_POSTGRES_DSN=postgresql://user:pw@pg:5432/starcubes
export STARCUBES_SOURCE_DSN='oracle+oracledb://user:pw@oracle:1521/SID'
.venv/bin/python manage.py seed_example

# 3. Run the first ETL pass (no indices yet — fastest COPY).
.venv/bin/python manage.py etl_run --name source

# 4. After the first ETL, build the BRIN/btree indices and FKs.
.venv/bin/python manage.py create_indices --name source

# 5. Run the Django dev server.
.venv/bin/python manage.py runserver
# open http://localhost:8000/?name=source&group_by=app_loc
#                            &from=2026-04-27+00:00&to=2026-04-27+12:00
```

The ETL is incremental. On first run it fills from the beginning; on every
subsequent run it picks up from the last successful high watermark recorded
in `starcubes_meta.etl_run`. Step 4 is idempotent — it probes `pg_constraint`
for each foreign key and uses `CREATE INDEX IF NOT EXISTS`, so re-running it
is safe and is how you check that nothing is missing. Use
`manage.py drop_indices --name source` if you ever need a clean re-bulk-load.

### Login

The Sources GUI and admin require login. `RequireLoginMiddleware` protects
`/sources/` and `/admin/`; the chart page at `/` is public (but per-user
preferences like bar pixel target require authentication). Create a
superuser with `manage.py createsuperuser`, then log in at `/login/`.

### Management commands reference

`manage.py etl_run` supports several modes beyond a plain incremental pass:

| Flag | Description |
|------|-------------|
| `--forever SECONDS` | Re-run ETL continuously at the given interval |
| `--forever-id ID` | Resume an existing forever session (set by the GUI) |
| `--start TIMESTAMP` | Override the high watermark start time |
| `--end TIMESTAMP` | Override the end time (skip source max query) |
| `--limit N` | Cap rows extracted per run |
| `--refresh-counts` | Refresh dim count columns from the fact table |
| `--import-dims` | Import/update dimension tables |
| `--truncate-last` | Truncate the oldest fact partition |
| `--delete-from TIMESTAMP` | Delete fact rows at or after the given time |
| `--clear-etl-history` | Delete all etl_run rows for the datasource |
| `--set-highwatermark` | Set HWM to max(partition_column) in fact |
| `--vacuum-analyze` | VACUUM ANALYZE the fact table |
| `--create-partitions N` | Create N future monthly partitions |

If `--name` is omitted and only one datasource exists, it is auto-detected.

```
.venv/bin/python manage.py seed_example --help

  --postgres-dsn DSN     Postgres URL (or env STARCUBES_POSTGRES_DSN)
  --source-dsn DSN       Oracle URL (or env STARCUBES_SOURCE_DSN)
  --name NAME            datasource name (default: source)
  --schema SCHEMA        target Postgres schema (default: starcubes_source)
  --profile NAME         profile under profiles/ (default: oracle_payments;
                         see docs/source-profiles.md and profiles/*/*.yaml)
  --force                replace any existing datasource with the same name
  --skip-initial-ddl     register config only; skip create_fact + import_dimensions
                         (useful for offline setup / re-hydration from a dump)
```

### Producing a multibar JSON for offline testing

```bash
.venv/bin/python manage.py query_multibar --name source \
    --group-by response --interval 60 \
    --from '2026-04-24 00:00+02' --to '2026-04-25 00:00+02' \
    --indent 2 > plot.json
```

The output matches the shape documented in `examples.sql:43–62` and consumed
by `timeseries.js`.

## Adding a new datasource
Use the GUI at `/sources/new/` or write a seed script modelled on
`manage.py seed_example`. You need

- a `base_table` (or full `import_query`) that projects every fact column you
  want to store;
- a `watermark_column` (source-side column used for the incremental boundary,
  does **not** need to be in the SELECT list) and optionally a
  `watermark_max_query` (`SELECT max(<wm>) FROM <base_table>`) that lets the
  ETL skip the wrap-subquery scan;
- optionally a `late_arrival_overlap_s`, if the source can make rows visible
  out of watermark order — the load window then reaches that many seconds
  back past the high watermark and drops what it already has by primary key.
  Pair it with `watermark_fact_expr` when the watermark is not itself a fact
  column. The ETL tab's Audit sub-tab measures how much overlap a
  given source actually needs, and backfills whatever slipped past it;
- `fact_column` rows describing the warehouse layout (one `time` role, one
  `pk` role, N `measure` / `dimension` / `auto-dimension` roles) — each with
  a `source_expr` projection;
- `dimension` rows mapping each fact column to its source lookup table. Where
  several fact columns share one dim table (e.g. `so_peer_1/2/3` → `peer`),
  mark exactly one row `is_primary = true`. For `auto-dimension` columns,
  set `dim_table` and `is_primary = true`; leave `source_table` and
  `inline_values` null and the ETL will populate the table on first sight
  of each value;
- optional `fact_index` rows.

## Acknowledgements

- Chart rendering by [`hgruber/timeseries.js`](https://github.com/hgruber/timeseries.js).

## License

MIT — see [`LICENSE`](../LICENSE).
- Current JSON shape the viz consumes is documented in `examples.sql:43–62`.