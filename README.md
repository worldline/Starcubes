# starcubes

A single-app datawarehouse. Pulls rows from arbitrary SQL sources (Oracle,
Postgres, MySQL, MSSQL) into a compact star schema on Postgres and serves them
as stacked-bar timeseries with arbitrary dimension filters.

Successor to the earlier hard-coded ETL in `star.py`. Optimised for
**minimal storage** (smallint-keyed dimensions, BRIN index, unlogged monthly
partitions), **maximum load throughput** (three-stage extract / transform /
load pipeline with psycopg `COPY FROM`), and **configurability** (every
datasource lives as metadata rows in `starcubes_meta`, editable from a GUI).

## Status

**Built**

- `core/` — framework-agnostic ETL engine:
  - `source.py` — `Source` / `FactColumn` / `Dimension` / `FactIndex` dataclasses + `Source.load` + shared engine cache (`_get_cached_engine`).
  - `schema.py` — `create_fact`, `create_dim_tables`, `create_indices`, `drop_indices`, `drop_schema`, `sync_schema`, `import_dimensions`, `read_dimensions`, `ensure_starcubes_meta`.
  - `etl.py` — the three-stage pipeline port of `star.py:import_fact`, parameterised by a `Source`. Supports window overrides (`--start`/`--end`/`--limit`), HWM rounding (`_ceil_second`), and forever mode. Maintenance functions: `truncate_last_partition`, `truncate_partition`, `drop_partition`, `delete_fact_from`, `clear_etl_history`, `set_highwatermark`.
  - `query.py` — `timeseries_multibar` JSON producer for the viz front end (bars ordered ascending per slot).
  - `cache.py` — server-side cache for frozen `plot_json` responses (count(\*), past-only, HWM-bounded). `make_cache_key`, `is_cacheable`, `get_cached_plot`, `put_cached_plot`, `bump_cache_epoch`, `clear_cache`.
  - `utils.py` — shared utilities (`_parse_dt`, `_pid_alive`). No Django imports.
- `migrations/` — consolidated metadata schema (`starcubes_meta.sql`) + incremental migrations (`0002`–`0012` including `etl_forever`, `partition_period`, `auto-dimension` role, and `query_cache`).
- `manage.py seed_example` — seeds an example Oracle payment-transaction datasource with hard-coded config.
- `manage.py etl_run` — incremental ETL pass, forever mode, and maintenance actions (see below).
- `manage.py query_multibar` — produces a multibar JSON file for offline testing against `timeseries.js`.
- `manage.py create_indices` / `manage.py drop_indices` — build or remove FK constraints + indices for a datasource.
- `manage.py query_cache_clear` — flush the server-side plot cache (whole DB or single datasource).
- `starcubes/` — Django project (`settings.py`, `urls.py`, `wsgi.py`, `middleware.py`). Login system with `RequireLoginMiddleware` (protects `/sources/` and `/admin/`), plus optional Keycloak/OIDC SSO (`mozilla-django-oidc`, `starcubes/oidc.py`) alongside password login — off by default, auto-provisions non-privileged users on first login when configured. `SQLAlchemyPoolMiddleware` logs warnings for leaked connections.
- `sources/` — Django app: full datasource CRUD + ETL monitor GUI. Inline CRUD for fact columns, dimensions, indices. ETL Commands tab with Run ETL (window overrides), Forever ETL, Data Cleanup, Metadata, and Schema actions. Schema creation, sync (dry-run/confirm), and drop. Connection status badges. Seed-data preload on create.
- `viz/` — Django app: filter UI + JSON endpoint + vendored `@hgruber/timeseries.js@~0.10.0` (npm dep). Chart has follow-mode toggle, seismograph auto-refresh, arrow-key viewport panning, per-user bar preferences, and login-gated Group Dims tab.
- SQLAlchemy engine cache prevents connection leaks — one pool per (dsn, connect_args) pair.

**Not yet**

- Multi-dialect `WHERE`-clause construction is still Oracle-only (`to_timestamp` literals). When a second non-Oracle source is wired up, switch to SQLAlchemy bind parameters.

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
  starcubes_meta.sql             consolidated initial schema (idempotent)
  0002_etl_unmapped.sql      unmapped-value tracking per run
  0003_per_column_projection.sql  per-column source_expr + base_table
  0004_inline_values.sql     inline_values on dimension
  0008_etl_forever.sql       forever ETL session tracking
  0010_partition_period.sql  per-datasource partition granularity (day/week/month)
  0011_auto_dimension_role.sql  add 'auto-dimension' to fact_column.role
  0012_query_cache.sql       starcubes_meta.query_cache + datasource.cache_epoch
  0019_query_history.sql     per-user SQL history for the chart's Query console
  0021_etl_run_log.sql      etl_run.log_path / log_tail — per-run ETL log file pointer
sources/                      Django app: datasource CRUD + ETL monitor GUI
  management/commands/
    etl_run.py               manage.py etl_run — incremental ETL, forever mode, maintenance actions
    seed_example.py           manage.py seed_example — seed example Oracle payment-transaction datasource
    query_multibar.py        manage.py query_multibar — multibar JSON for offline testing
    create_indices.py         manage.py create_indices — build FKs + indices
    drop_indices.py           manage.py drop_indices — drop FKs + indices
    query_cache_clear.py     manage.py query_cache_clear — flush server-side plot cache
examples.sql                reference SQL for the viz JSON shape
Dockerfile                  multi-stage: JS vendor (npm ci @hgruber/timeseries.js) → Python deps → nginx+uwsgi runtime
package.json + package-lock.json  frontend JS dependency, pinned ~0.10.0
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
`package.json`/`package-lock.json` to fetch `@hgruber/timeseries.js@~0.10.0`
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
in `starcubes_meta.etl_run`. Step 4 is one-shot — re-running it errors on the
existing indices. Use `manage.py drop_indices --name source` if you ever need
a clean re-bulk-load.

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
  column. The Audit tab on the datasource page measures how much overlap a
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

MIT — see [`LICENSE`](LICENSE).
- Current JSON shape the viz consumes is documented in `examples.sql:43–62`.