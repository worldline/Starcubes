# `starcubes` — file notes

File-by-file notes for `starcubes`, split out of the root `CLAUDE.md` so they
load only when work touches this directory. The project-wide rules — git
workflow, code conventions and the verification checklist — stay in the
root [`CLAUDE.md`](../CLAUDE.md); read that one too.

| File | Purpose |
|------|---------|
| `starcubes/version.py` | **Derives the version from the git tag** — no number is stored in the tree. Asks `git describe --abbrev=0` plus `rev-list --count` rather than parsing describe's output, because the pre-release part carries hyphens of its own; `STARCUBES_VERSION` from the env wins, which is how a container (no `.git`) learns its version; a source archive is served by two `export-subst` placeholders that `git archive` fills in (hence the repo-root `.gitattributes`) — **never write another such placeholder into this file, the docstring included**, or the archive rewrites that too. Stdlib-only and Django-free on purpose: `docker/build.sh` resolves the version by *running* this module — with the build *host's* `python3`, which is why this one file is kept **Python 3.6-compatible** (no `__future__` annotations import, no evaluated builtin generics, no `subprocess.run(capture_output=…)`). See [`docs/releases.md`](../docs/releases.md). |
| `starcubes/context_processors.py` | `about_info` — feeds `viz/templates/viz/about.html` (the About dialog), included by both page shells. Computed once at import, so it costs one dict lookup per request. |
| `starcubes/settings.py` | Reads `STARCUBES_POSTGRES_DSN` from env. Custom DB backend, `CsrfViewMiddleware` in MIDDLEWARE. See [`docs/deployment-config.md`](../docs/deployment-config.md). |
| `starcubes/db_backends/` | Custom PG backend package — ensures `starcubes_meta` schema exists, sets search_path. |
| `starcubes/middleware.py` | `RequireLoginMiddleware` — gates `/sources/` and `/admin/`. `PipelineAlarmMiddleware` — makes the request path double as a scheduler for pipeline alarms, because a wedged ETL loop cannot report that it is wedged. Throttled twice (a process-local timestamp, then `core.pipeline_alarm.pipeline_eval_lock`, since uwsgi runs several processes) and the work runs on a **daemon thread** — a dispatch can take a minute per transition and must not hold a page open; the cost is that a recycled worker loses that one message, though the episode row is already committed. The container also runs `manage.py pipeline_watch`; this is the layer for deployments that do not. |
| `starcubes/oidc.py` | `KeycloakOIDCBackend` — optional SSO auth backend, active only when `STARCUBES_OIDC_*` env vars are set. Maps username from `preferred_username`, force-clears `is_superuser`/`is_staff` on auto-created users. See [`docs/deployment-config.md`](../docs/deployment-config.md). |
