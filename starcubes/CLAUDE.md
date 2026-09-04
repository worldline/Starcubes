# `starcubes` — file notes

File-by-file notes for `starcubes`, split out of the root `CLAUDE.md` so they
load only when work touches this directory. The project-wide rules — git
workflow, code conventions and the verification checklist — stay in the
root [`CLAUDE.md`](../CLAUDE.md); read that one too.

| File | Purpose |
|------|---------|
| `starcubes/settings.py` | Reads `STARCUBES_POSTGRES_DSN` from env. Custom DB backend, `CsrfViewMiddleware` in MIDDLEWARE. See [`docs/deployment-config.md`](../docs/deployment-config.md). |
| `starcubes/db_backends/` | Custom PG backend package — ensures `starcubes_meta` schema exists, sets search_path. |
| `starcubes/middleware.py` | `RequireLoginMiddleware` — gates `/sources/` and `/admin/`. |
| `starcubes/oidc.py` | `KeycloakOIDCBackend` — optional SSO auth backend, active only when `STARCUBES_OIDC_*` env vars are set. Maps username from `preferred_username`, force-clears `is_superuser`/`is_staff` on auto-created users. See [`docs/deployment-config.md`](../docs/deployment-config.md). |
