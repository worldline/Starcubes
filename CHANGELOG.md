# Changelog

Notable changes per release. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[semantic versioning as this project reads it](docs/releases.md) — where
"breaking" means *the operator has to do something by hand to upgrade*.

## [Unreleased]

Nothing yet. What 2.1.x will contain is not decided; candidates live in
[`docs/backlog.md`](docs/backlog.md), and nothing there is committed to a
release until it is listed here. The road to a final 2.0.0 runs through
further pre-release tags — see [`docs/releases.md`](docs/releases.md).

## [2.0.0-alpha.1] — 2026-09-07

First tagged version. Everything before it shipped unversioned — the history
is in `git log`, not here, so this entry describes what the release *is*
rather than what changed inside it.

An alpha, not a finished 2.0.0: the feature set below is in place, but the
schema, the environment variables and the profile format may still move
before 2.0.0 is final.

### Added

- **Version identity.** The git tag *is* the version — `starcubes/version.py`
  derives it from `git describe`, so a tag and a version cannot disagree.
  `docker/build.sh` resolves it, tags the image with it and stamps the
  commit and build time in; the GUI's **About starcubes** menu row opens a
  dialog naming all of it, with a **pre-release** pill on versions like this
  one. See [`docs/releases.md`](docs/releases.md).

### The feature set

- **Configurable multi-source ETL.** A source is a `Source` dataclass loaded
  from `starcubes_meta` — base table, watermark column, fact columns,
  dimensions — with nothing hard-coded. Incremental loads run on a high
  watermark, resumable, with a `COPY FROM` load path.
  ([`docs/etl-engine.md`](docs/etl-engine.md))
- **Source profiles.** A configured source can be saved as a YAML profile
  and re-applied to the next install in one click.
  ([`docs/source-profiles.md`](docs/source-profiles.md))
- **Compact star schema on Postgres.** Smallint-keyed dimensions, range
  partitions, BRIN on the time column.
  ([`docs/star-schema.md`](docs/star-schema.md))
- **Row-count audit** of every loaded window against the source.
  ([`docs/etl-audit.md`](docs/etl-audit.md))
- **Chart GUI.** Stacked-bar timeseries with filters, grouping, drill-down
  into single transactions, ladder plots and quantiles.
  ([`docs/chart-app.md`](docs/chart-app.md),
  [`docs/chart-rendering.md`](docs/chart-rendering.md))
- **Alarms.** Per-datasource thresholds evaluated against the ETL high
  watermark, with a firings list in the GUI. ([`docs/alarm.md`](docs/alarm.md))
- **SQL console** against the warehouse, with per-user history.
- **One container.** Single image, Postgres alongside it, local login or
  Keycloak/OIDC SSO. ([`docs/deployment-config.md`](docs/deployment-config.md))
