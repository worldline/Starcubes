# Changelog

Notable changes per release. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[semantic versioning as this project reads it](docs/releases.md) — where
"breaking" means *the operator has to do something by hand to upgrade*.

## [Unreleased]

### Added

- **Alarms can notify by e-mail, XMPP and PagerDuty.** Each alarm picks
  recipients per channel from the login users (e-mail from the account, XMPP
  via a new per-account JID field on the settings page); PagerDuty speaks
  Events API v2 with a per-datasource integration key (routing key, source
  edit page) and closes its incidents when an episode resolves. Transport
  configuration is env-var based — see `docs/deployment-config.md`. Purely
  additive schema, self-applying at container start (minor release).
- **The notification configuration is reachable in the GUI.** A global
  configuration tab (in the tab strip and both hamburger menus) shows the
  transport status, the per-datasource routing keys masked, and a
  test-send button per source; the per-source routing key sits under
  "Basics" on the edit page; the own profile page shows and edits the
  account's XMPP JID.
- **Global configuration is DB-editable.** One key-value table
  (`starcubes_meta.app_config`, key = the env-var name) backs the
  notification transports and the SSO/OIDC login; resolution is
  environment-first everywhere (`core/config.py`), so env vars still win
  and the GUI edits only what the environment does not set. The global
  tab is now called **Transports** (was "PagerDuty") and edits SMTP, XMPP,
  the PagerDuty endpoint and the SSO trio in place; transports apply
  live, SSO on/off on restart.
- The About dialog's **timeseries.js** row now links to the project on
  GitHub.

### Fixed

- **The Users page's "Edit user" form persisted nothing** — a duplicated
  `update_user` branch shadowed the real one (regression of the alarm
  notification change; e-mail, XMPP JID and flags were silently dropped).

What 2.1.x will contain is not decided; candidates live in
[`docs/backlog.md`](docs/backlog.md), and nothing there is committed to a
release until it is listed here. The road to a final 2.0.0 runs through
further pre-release tags — see [`docs/releases.md`](docs/releases.md).

## [2.0.0-alpha.2] — 2026-09-07

### Fixed

- **A build that bypasses `docker/build.sh` is now refused** instead of
  quietly producing an image that reports its version as `unknown`. The
  version cannot be discovered from inside the image — `.dockerignore` keeps
  `.git` out of the build context — so a bare `podman build` left it empty
  and the About dialog had nothing to name. Building without git on purpose
  still works, it just has to say so:
  `--build-arg STARCUBES_VERSION=unknown`.
- **Builds on a host with Python 3.6 work again.** `docker/build.sh` reads
  the version by running `starcubes/version.py` with the build machine's
  `python3`, and that module opened with a 3.7-only `__future__` import, so
  the build died on a `SyntaxError` before reaching podman. The module is
  now held to Python 3.6 — it is the only file in the tree that is.
- **A source archive names its version too.** Code handed over as a
  `git archive` zip carries no `.git` and gets no build argument, so the
  About dialog there reported `unknown`. `git archive` now fills the version
  and the commit in as it packs — the same command as before, no extra flag.
- **The copyright notices are right, and complete.** starcubes is
  Copyright © 2026 Worldline. The vendored `@hgruber/timeseries.js` is MIT,
  Copyright © 2022–2026 Holger Gruber, and its notice now actually ships:
  the published bundle is minified with no banner, so the image carries the
  package's own `LICENSE` next to it, the About dialog names both, and
  `THIRD-PARTY-NOTICES` reproduces the text in full.

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
