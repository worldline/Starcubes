# Claude agent notes for starcubes

Context for future Claude sessions. `README.md` is the shop window —
the pitch and the screenshots, nothing an agent needs. Read
`docs/internals.md` for the architecture and the CLI, and `HOWTO.md` for
operator guidance; this file is the index into the agent-/developer-facing
deep references.

## Git workflow

Linear, single-developer project. There is no remote and no `main`
branch — **`master` is the only long-lived branch.**

- **Every commit lands on `master`.** No merge commits, no PRs, no
  long-lived feature branches. `git log --graph` must stay a straight
  line.
- **Don't create a worktree unless the user asks for one.** Background
  jobs may edit the working copy directly — `.claude/settings.json` sets
  `worktree.bgIsolation: "none"`, which lifts the harness's default
  requirement to call `EnterWorktree` before the first edit.
- **A worktree is disposable scratch space.** Once the work in it is
  built and tested, fast-forward `master` onto it and delete both the
  worktree and its branch:

  ```
  git merge --ff-only <branch>              # never a merge commit
  git worktree remove .claude/worktrees/<name>
  git branch -d <branch>                    # -d, not -D: it refuses if anything is unmerged
  ```

  If the fast-forward is refused because `master` moved on, rebase the
  branch onto `master` first — never merge.
- **Never leave a branch behind.** A stale branch invites the next agent
  to keep working there instead of on `master`.

## Sub-docs (read when relevant)

- **[`docs/internals.md`](docs/internals.md)** — the shape of the whole system:
  component map, architecture, repo layout, `manage.py` quick start.
- **[`docs/sources-app.md`](docs/sources-app.md)** — read when touching anything
  under `sources/`: URL map, gotchas, inline-CRUD UX.
- **[`docs/chart-app.md`](docs/chart-app.md)** — read when touching `viz/` or
  chart JS.
- **[`docs/etl-engine.md`](docs/etl-engine.md)** — read when editing
  `core/etl.py`, `sources/etl_logging.py`, `sources/etl_spawn.py`, or
  `sources/management/commands/etl_run.py`.
- **[`docs/logfile-source.md`](docs/logfile-source.md)** — log files as the
  second kind of source. Read when editing `core/logsource.py`,
  `check_logsource`, the `is_logfile` branches in `core/etl.py` /
  `core/source.py` / `core/preflight.py`, or the Log Source card in
  `sources/templates/sources/_logfile_card.html` / `_logfile_js.html`. Its
  load-bearing points: the watermark is a timestamp and not an offset (which
  is what keeps backfill, retention, rollups and alarms working), there is
  deliberately **no primary key**, `_ceil_second` is skipped there, and every
  failure mode in the reader is silent — which is why
  `manage.py check_logsource` exists and why its numbers are literals.
- **[`docs/etl-audit.md`](docs/etl-audit.md)** — read when editing
  `core/audit.py` or the ETL tab's Audit sub-tab in
  `sources/templates/sources/source.html`.
- **[`docs/etl-automation.md`](docs/etl-automation.md)** — what has to be true
  *around* a load: preconditions the engine guarantees itself vs. the ones that
  still hang on an operator, and the evaluation of each. Read when planning
  anything periodic, a preflight check, or a maintenance action.
- **[`docs/star-schema.md`](docs/star-schema.md)** — domain vocabulary and the
  schema design decisions. Read when editing `core/schema.py`, `core/source.py`
  (dataclasses), or adding/removing dimensions or measures.
- **[`docs/deployment-config.md`](docs/deployment-config.md)** — read when
  editing `Dockerfile`, `docker/build.sh`, `starcubes/settings.py`,
  `starcubes/oidc.py`, `requirements.txt`, or `package.json`. Its
  "Environment variable reference" is the one complete list of env vars —
  a new `os.environ.get("STARCUBES_…")` or `Dockerfile` `ENV` needs a row there.
- **[`docs/multi-container.md`](docs/multi-container.md)** — several containers
  on one database: what must hold outside the app, and how leases, stop
  requests, adoption, the drain and the one log fit together. Read before
  touching `core/lease.py`, `core/pglock.py`, `core/node.py`,
  `core/log_ship.py`, `sources/forever_adopt.py`, the `node_agent`/`node_drain`
  commands, `docker/entrypoint.sh`'s shutdown, or anything that decides
  whether a process is alive.
- **[`docs/chart-rendering.md`](docs/chart-rendering.md)** — ladder plot types
  and partial-bin drawing. Read when editing the plot code paths in
  `viz/templates/viz/chart.html` or `core/query.py`.
- **[`docs/rollup.md`](docs/rollup.md)** — pre-aggregation: hourly rollup
  tables, how their column set is chosen and measured, what they can never
  serve. Read when editing `core/rollup.py`, `core/rollup_advise.py`, the
  `rollup_*` management commands, or the rollup branch in `viz/views.py`.
- **[`docs/bitmap-indexes.md`](docs/bitmap-indexes.md)** — research note: could
  PostgreSQL have a persistent bitmap index, and would a fact table gain from
  one. Measured, with the answer being mostly no (B-tree deduplication since
  PG 13 already banks 3.2× for free; the remaining win needs physically
  clustered data). Read before proposing an index-level change, and for §6,
  which is what the dim indexes were before they went full — the source of
  the partial-index defect, closed in the backlog's closed items 2026-09-14.
- **[`docs/stream-character.md`](docs/stream-character.md)** — research note:
  is a source's stream online traffic or a batch load, and does the gap
  detector's Poisson model hold for it. Read when editing `core/stream_stats.py`,
  `stream_check`, or anything that assumes arrivals are Poisson — which
  includes `detect_gaps` and short-window `count(*) < 1` alarms. Its §2.3 is
  the inhomogeneity trap that cost two rewrites; §2.5 is why the periodogram
  is not the periodicity estimator.
- **[`docs/backlog.md`](docs/backlog.md)** — known unfinished work. Read when
  planning new work or assessing scope.
- **[`docs/releases.md`](docs/releases.md)** — the git tag *is* the version
  (no number is stored in the tree), what a major/minor/patch bump promises
  an operator, how pre-release tags work, and the steps that cut a release.
  Read before tagging a release, editing `docker/build.sh` or
  `starcubes/version.py`, or changing what the About dialog reports.
- **[`docs/source-profiles.md`](docs/source-profiles.md)** — read when touching
  `core/profile.py`, `sources/profile_store.py`, `profiles/`, `seed_example.py`,
  or the profile UI in `new.html` / `profiles.html` / `source.html`.
- **[`docs/alarm.md`](docs/alarm.md)** — alarm evaluation. Read when editing
  `core/alarm.py`, `sources/management/commands/alarm_run.py`, the alarm hook
  in `etl_run.py`, or the alarm UI in `_alarms_panel.html` /
  `_alarms_js.html` / `chart.html`.


## Repo state

The repo is **mid-rewrite** from a hard-coded Oracle single-file ETL
(`star.py`, removed) into a configurable multi-source app. `core/` is the
framework-agnostic engine; `sources/` and `viz/` are thin Django adapters on
top of it. Run `ls` and `git log` for what exists today.

**Backlog:** see [`docs/backlog.md`](docs/backlog.md).

`viz/views.py:_parse_dt` is also in `query_multibar` — duplication is
deliberate (the management command is a standalone entry point that
must not import from `viz/`, same constraint as `core/`).


## Code conventions

- SQLAlchemy 2.x idioms: `text()`, `engine.begin() as conn`, `.mappings()`
  for row dicts.
- All per-source configuration comes from a `Source` dataclass loaded from
  `starcubes_meta`. Never hard-code dimensions, column lists, or SQL into a helper.
- Migrations numbered `000N_*.sql`, idempotent (`IF NOT EXISTS`, `OR REPLACE`).
- Django management commands live under `sources/management/commands/`.
  They import from `core/` but never the reverse. `core/` must not import
  Django. Use `manage.py <command>` for all CLI operations.
- **`{# … #}` in a Django template is single-line only.** A newline between
  the delimiters is not a comment: Django stops recognising it, and the text
  is served to the browser as page content. It fails silently — the page
  still renders, the prose just shows up in the GUI — so nothing but reading
  the rendered output catches it. Use `{% comment %} … {% endcomment %}` for
  anything spanning more than one line, and keep `{# … #}` for true
  one-liners. This has bitten us repeatedly; see the check below.

## Key files (most-touched, top of mind)

The file-by-file notes are split by directory and load only when work
touches that directory: [`core/CLAUDE.md`](core/CLAUDE.md),
[`sources/CLAUDE.md`](sources/CLAUDE.md), [`viz/CLAUDE.md`](viz/CLAUDE.md),
[`starcubes/CLAUDE.md`](starcubes/CLAUDE.md). Read the one for the corner
you are working in. What is left here belongs to no single directory:

| File | Purpose |
|------|---------|
| `migrations/starcubes_meta.sql` | Consolidated idempotent meta-schema (0001–0039 combined — 0025 adds the per-datasource alarm table, 0026 the `enabled` flag 0025 omitted, 0028 replaces `context_dim`/`context_value` with `context_filter jsonb` + `comment` + `eval_end` and adds `alarm_history`/`alarm_firing`/`alarm_forever`, 0029 drops `alarm_forever` again and re-comments `eval_end`/`alarm_firing` for the HWM semantics, 0030 wires the notification transports: `auth_user.xmpp_jid` (guarded — see the bootstrap second pass), `alarm.notify_email_recipients`/`notify_xmpp_recipients int[]` and `datasource.pagerduty_routing_key`, 0031 adds `app_config` — the one KV table for DB-editable global config, key = env-var name, env-first resolution via `core/config.py`, 0032 adds `alarm.severity` (per-alarm PagerDuty severity, CHECK-constrained, global fallback) and the guarded `auth_user.notify_email`/`notify_xmpp` opt-in, deliberate default off, 0033 adds `datasource.retention_floor` + `dedupe_from` — the two halves of "deleting means two opposite things": disposal records a floor the audit clamps to, repair rewinds the watermark and arms the PK filter for the span it reloads, 0034 adds `datasource.housekeeping_interval_s` — default 21600 and deliberately *on*, unlike every other tuning column here — plus the `housekeeping_run` history table, 0035 adds `etl_run.max_wm`/`src_now` — the two source-clock readings whose *difference* is the only way to see a source that is up and has stopped producing — plus `datasource.max_data_lag_s` and `max_run_s`, both off by default, 0036 adds `alarm.rule_kind`/`condition` — the second alarm kind, watching the pipeline instead of the data; called `rule_kind` because the transition dicts already use `kind` for opened/closed and an alarm column of that name would turn every notification into a resolution, 0037 adds `datasource.retention_periods` + `initial_window_s` + `dim_refresh_interval_s`/`dim_refreshed_at` — the three knobs that change what the rest of the system may assume about the fact table, all off by default, 0038 adds `datasource.rollup_interval_s`/`rollup_columns`/`rollup_marginals`/`rollup_measures` + `rollup_state` — the pre-aggregation, whose column set is an operator decision because it is not derivable from the schema: one high-cardinality dimension can cost more than all the others together. `rollup_state.spec_hash` is what makes a reconfiguration degrade to `fact` instead of returning numbers computed under the old shape, and `built_from`/`built_through` are two marks rather than one so a multi-hour backfill working backwards is usable while it runs. Retention deliberately does **not** clamp a rollup: its rows stay correct after the raw ones are disposed of, so aggregates outlive them — at the price that below `retention_floor` the rollup is the only copy, which is why `rollup_drop`/`--rebuild` refuse that span without `--force`; 0039 adds `rollup_run` — one row per `rollup_advise`/`rollup_build`/`rollup_verify`, written by the command itself so a run started from a shell shows up in the GUI history like one started from a button. It exists because those three cannot run inside a web request: uwsgi's `harakiri = 120` kills the whole worker. `report jsonb` is what makes it a table rather than a log file — an advise produces a ladder the operator picks a stage from, and a pick that lives only as long as a log file vanishes on the next reload; 0042 adds the process-lease owner columns (`hostname`, `instance_id`, `lease_pid`, `lease_start`) to `etl_run`/`etl_forever`/`rollup_run` and the view `run` — every run of every kind in one list with an `alive` column; the five history tables stay, the view is how an operator and the code find them, see `core/lease.py`; 0043 adds `control` for stop requests from any container, 0044 the forever status `released`, 0045 `profile` (saved profiles), 0046 `log_line` — the one log — and redefines `run` with `run_ref` as the log's basename. 0047 adds the second *kind* of source: `source_dialect` gains `'logfile'`, plus the six `datasource` columns that describe a log (`log_line_regex`, `log_record_start`, `log_time_format`, `log_time_zone`, `log_encoding`, `log_min_match_pct`), the two `fact_column` columns `transform_pattern`/`transform_replace`, and `etl_unparsed` — `etl_unmapped`'s counterpart for lines that did not match. Three things about it are load-bearing and easy to undo by accident: `source_dsn` carries the *transport* (`file://` glob, `sftp://`, `https://`, `exec://`) rather than gaining a column of its own, which is what keeps DSN masking, the connection test and the profiles working; `watermark_column` holds the **name of the time group** and `partition_column` the fact column, so both NOT NULLs hold without being loosened; and a log source has deliberately **no** `role='pk'` column, because `last_end_time` is the *upper*, half-open bound and a log is kept in timestamp order, so `[last_end, until)` joins on with nothing loaded twice. The transform runs **before** the cast, which is what lets it do both of its jobs — collapsing an unbounded value into a usable dimension (an OSM tile path against the 32767 `smallserial` ceiling) and mapping a format's non-value onto a number (nginx `-`, HAProxy `-1`). **The view `run` is defined exactly once in the consolidated script**, in the newest block that changed it, as `DROP VIEW IF EXISTS` + `CREATE VIEW`: Postgres cannot drop a view column, so an older `CREATE OR REPLACE` re-run after a newer one breaks idempotency). Used by `core/schema.py`. Per-step migrations live in `migrations/000N_*.sql`. |
| `core/docs.py` | The registry and renderer behind `/help/` — which Markdown files the app serves, and the link rewriting that maps their repo-relative links and images onto URLs it answers. **The registry is curated on purpose**: a scan would sweep in the `CLAUDE.md` family, and the order, grouping and one-line blurbs are editorial. Two traps are written up in the file itself. The link rewriting is a **postprocessor, not a treeprocessor** — python-markdown parks raw HTML in its `htmlStash`, so a treeprocessor never sees the `<picture>` block README.md keeps its logo in. And the `toc` extension is configured `permalink`, not `anchorlink`: the latter wraps every heading's text in an `<a>`, which makes a page of headings look like a page of links. The corpus's hand-written anchors all resolve under the default slugify (verified across all 19 documents) — do not swap it. `manage.py check_help_docs` is the machine check for the whole contract. |
| `core/preflight.py` | Everything checked before a load and the datasource advisory lock — see [`core/CLAUDE.md`](core/CLAUDE.md) and [`docs/etl-automation.md`](docs/etl-automation.md). Listed here only because a new agent looking for "why did my run refuse to start" will not guess the filename. |
| `examples.sql` | Reference SQL. Lines 43–62 are the template for `core/query.timeseries_multibar`. |
| `.gitattributes` | One line, `starcubes/version.py export-subst`: `git archive` fills the version placeholders in that file as it packs, so a source zip handed to a test environment can name its version with no `.git`. No `export-ignore` on purpose. See [`docs/releases.md`](docs/releases.md). |
| `.dockerignore` | Note the matching rule before editing: `*` does not cross a `/`, so the long-standing `*.md` line only ever excluded the four repo-root Markdown files — every `docs/*.md` and each per-app `CLAUDE.md` shipped in the image regardless. That is why the documentation now needs explicit `!` exceptions (for the files `/help/` serves) *and* an explicit `**/CLAUDE.md` (for the agent notes, which must not ship). Verified by building a throwaway context and listing the layer; a guess about this file is worth nothing. |
| `docker/build.sh` | The only supported way to build the container image — defaults `CONTAINER_ENGINE=podman` and delegates to the build engine. The Dockerfile's `jsbuilder` stage pulls `@hgruber/timeseries.js` from npm via `npm ci`. Four env vars steer it and every one defaults to the original behaviour: `STARCUBES_DOCKERFILE` picks the Dockerfile, `STARCUBES_BASE_IMAGE`/`STARCUBES_NPM_REGISTRY`/`STARCUBES_PIP_INDEX_URL` are passed through as build args only when set. They are **build**-time, which is why they live in [`docs/releases.md`](docs/releases.md) "Two build branches" and not in the env-var table of `deployment-config.md`. See [`docs/deployment-config.md`](docs/deployment-config.md). |
| `Dockerfile`, `docker/Dockerfile.ubi` | **Two build branches, and only one of them is the default.** `Dockerfile` is the original — Debian slim off Docker Hub — and is what a checkout, the README quickstart and the GitHub release build. Leave it alone unless the change is meant for everyone. `docker/Dockerfile.ubi` is the same image on UBI 10 out of the internal Worldline registry, for the GitLab instance that cannot reach Docker Hub; `.gitlab-ci.yml` selects it. Exactly three things differ (one base image for all stages via `$BASE_IMAGE`, `dnf` instead of `apt-get`, and the Python environment in a venv at `/opt/venv` instead of copied out of `/usr/local` — that path belongs to the official `python:*` images, not to an RPM python). **Everything else is a verbatim copy and has to stay one**: the runtime `ENV` blocks, the build-identity guard, `LABEL`, `EXPOSE`, `ENTRYPOINT`, and the order of the `COPY` lines. A Dockerfile cannot include another, so the sharing is by copy and nothing checks it — a change to a shared part belongs in both files on the same commit, or two images claim the same version and behave differently. Trap found by building it: an RPM python has no `Python.h`, so the builder stage needs `python3-devel` or uwsgi's build stops dead. |
| `.github/workflows/release.yml`, `.gitlab-ci.yml` | **Two release pipelines, deliberately kept in step** — a `v*` tag pushed to either remote builds the image through `docker/build.sh`, publishes it, packs a source zip from the tag and cuts a release whose body is that version's `CHANGELOG.md` section. Both carry the same two guards (version must resolve *and* equal the tag; the changelog section must exist, checked before the build). **Edit one, edit the other** — divergence here is invisible until a release is already half-published. That rule is about the *guards and the output*; how the two get there diverges on purpose and must not be "harmonised". GitHub runs on `ubuntu-latest` with Docker and the repo-root `Dockerfile`. GitLab runs on a **Kubernetes shared runner** (`k8s-S Shared Runner on GI/DC3`, reached by the `tags:` line) — hence a `default:` `image:` from the internal registry with git/curl/buildah from `dnf`, no `services:`, no `DOCKER_*` variables, the repo-root `Dockerfile` as the build (`STARCUBES_DOCKERFILE` is empty there — `docker/Dockerfile.ubi` is a spare, not the GitLab path; measurement showed that instance reaches Docker Hub, npm and PyPI but not `cdn.redhat.com`), **buildah unprivileged** (vfs + `BUILDAH_ISOLATION=chroot`, and a `[storage]` section must carry `runroot`/`graphroot` or buildah refuses every command) because a shared runner grants no `privileged`, and the release created through the Releases API because the `release-cli` image the `release:` keyword needs does not exist in that registry (neither does `docker:cli` or `docker:dind`). Do not "fix" the file back to a host-tooling shape: `image:` and pod-installed tooling are what a Kubernetes executor needs, and the `doctor` job is what tells you which executor answered. Three further GitLab-side differences are load-bearing and written up in the file: the zip is an asset *link* into the generic package registry (GitLab releases host no uploads), there is no pre-release flag (the name says it, `:latest` does the work), and `GIT_DEPTH: 0` plus `safe.directory` are what make `version.py` able to describe at all. The push target is the variable `$STARCUBES_IMAGE` — nothing else in the tree names a registry. See [`docs/releases.md`](docs/releases.md). |

Use `ls`, `grep`, and `git log` for anything not listed — don't rely on
this table as a manifest.

## Verification before reporting work done

1. **Syntax / import** — every Python file in `core/` and
   `sources/management/commands/` must `ast.parse` cleanly. Imports
   additionally require `sqlalchemy`, `pandas`, `tqdm`.
   `ast.parse` alone is not enough after a refactor that moves code between
   functions: it accepts a name that no longer has a binding in its new
   scope, and the `except Exception` in `viz/views.py` turns the resulting
   `NameError` into an HTTP 500 the chart only shows in its status line.
   Run an undefined-name check too — `ruff check --select F821 core/ viz/
   sources/` or `python -m pyflakes` — whenever a function's locals were
   extracted into a helper.
2. **Templates** — after editing any `.html`, check that no `{# … #}`
   comment spans a line break, or its text ships to the browser:

   ```
   grep -rn '{#' --include=*.html . | grep -v '#}'
   ```

   Any hit is a leaking comment (rewrite it as `{% comment %}`). Rendering
   the page is the only other way to see it, since a leak neither raises
   nor breaks the layout — the prose simply appears on the page.
3. **Stream statistics** — after touching `core/stream_stats.py`, run
   `manage.py check_stream_stats`. It measures six synthetic streams whose
   batch share, batch size and beat are known, needs no database, and is
   the only thing in the tree that can tell a plausible-looking inference
   from a correct one — every number in that module was wrong once, and
   none of the four failures raised anything.
4. **Log sources** — after touching `core/logsource.py`, run
   `manage.py check_logsource`. Thirteen fixture scenarios, 55 checks, no
   database needed. It exists for the same reason as the check above: every
   failure mode in that module is **silent**. A zone applied in the wrong
   direction, a year taken from the wrong end of a December/January
   boundary, a sub-second watermark rounded up, a transform that never
   fires — none of them raises, and all of them produce a chart that looks
   entirely plausible and is wrong. Two of its scenarios have already earned
   their keep: `re.MULTILINE` on `log_line_regex` (without it a joined
   multi-line record cannot match its own pattern, because `$` anchors at
   the end of the whole text and `.*` does not cross a newline) and the
   sub-second boundary case, which only bites on a *growing* file — on a
   static fixture both the ceiled and the unceiled watermark are green.
5. **SQL** — `psql -f migrations/starcubes_meta.sql` must be idempotent.
   Re-running is part of the contract; verify by running twice. The mirror
   contract itself — every per-step `migrations/000N_*.sql` has its marker
   block in the consolidated file — is machine-checked:
   `manage.py check_migration_mirror`.
6. **Seed round-trip** — `manage.py seed_example --force --skip-initial-ddl`
   followed by `Source.load(meta, dsn, 'source')` must return a `Source`
   whose `fact_columns` / `dimensions` / `indices` match the seed tuples.
7. **ETL parity** — when the first real ETL runs, compare row counts for
   the same `[last_end_time, end_time)` window. Throughput should be
   within 10% — the copy path is unchanged.
