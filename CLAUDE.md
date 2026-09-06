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
- **[`docs/etl-audit.md`](docs/etl-audit.md)** — read when editing
  `core/audit.py` or the Audit tab in `sources/templates/sources/manage.html`.
- **[`docs/star-schema.md`](docs/star-schema.md)** — domain vocabulary and the
  schema design decisions. Read when editing `core/schema.py`, `core/source.py`
  (dataclasses), or adding/removing dimensions or measures.
- **[`docs/deployment-config.md`](docs/deployment-config.md)** — read when
  editing `Dockerfile`, `docker/build.sh`, `starcubes/settings.py`,
  `starcubes/oidc.py`, `requirements.txt`, or `package.json`.
- **[`docs/chart-rendering.md`](docs/chart-rendering.md)** — ladder plot types
  and partial-bin drawing. Read when editing the plot code paths in
  `viz/templates/viz/chart.html` or `core/query.py`.
- **[`docs/backlog.md`](docs/backlog.md)** — known unfinished work. Read when
  planning new work or assessing scope.
- **[`docs/releases.md`](docs/releases.md)** — the git tag *is* the version
  (no number is stored in the tree), what a major/minor/patch bump promises
  an operator, how pre-release tags work, and the steps that cut a release.
  Read before tagging a release, editing `docker/build.sh` or
  `starcubes/version.py`, or changing what the About dialog reports.
- **[`docs/source-profiles.md`](docs/source-profiles.md)** — read when touching
  `core/profile.py`, `sources/profile_store.py`, `profiles/`, `seed_example.py`,
  or the profile UI in `new.html` / `profiles.html` / `manage.html`.
- **[`docs/alarm.md`](docs/alarm.md)** — alarm evaluation. Read when editing
  `core/alarm.py`, `sources/management/commands/alarm_run.py`, the alarm hook
  in `etl_run.py`, or the alarm UI in `detail.html` / `chart.html`.


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
| `migrations/starcubes_meta.sql` | Consolidated idempotent meta-schema (0001–0029 combined — 0025 adds the per-datasource alarm table, 0026 the `enabled` flag 0025 omitted, 0028 replaces `context_dim`/`context_value` with `context_filter jsonb` + `comment` + `eval_end` and adds `alarm_history`/`alarm_firing`/`alarm_forever`, 0029 drops `alarm_forever` again and re-comments `eval_end`/`alarm_firing` for the HWM semantics). Used by `core/schema.py`. Per-step migrations live in `migrations/000N_*.sql`. |
| `examples.sql` | Reference SQL. Lines 43–62 are the template for `core/query.timeseries_multibar`. |
| `docker/build.sh` | The only supported way to build the container image — defaults `CONTAINER_ENGINE=podman` and delegates to the build engine. The Dockerfile's `jsbuilder` stage pulls `@hgruber/timeseries.js` from npm via `npm ci`. See [`docs/deployment-config.md`](docs/deployment-config.md). |

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
3. **SQL** — `psql -f migrations/0001_starcubes_meta.sql` must be idempotent.
   Re-running is part of the contract; verify by running twice.
4. **Seed round-trip** — `manage.py seed_example --force --skip-initial-ddl`
   followed by `Source.load(meta, dsn, 'source')` must return a `Source`
   whose `fact_columns` / `dimensions` / `indices` match the seed tuples.
5. **ETL parity** — when the first real ETL runs, compare row counts for
   the same `[last_end_time, end_time)` window. Throughput should be
   within 10% — the copy path is unchanged.
