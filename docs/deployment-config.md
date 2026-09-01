# Deployment & build configuration — deep reference

How the deployment is configured: Docker build, runtime settings, SSO,
CSRF, branding assets. Read before editing `Dockerfile`, `docker/build.sh`,
`starcubes/settings.py`, `starcubes/oidc.py`, `requirements.txt`,
`package.json`, or the bundled nginx/uwsgi setup.

**Highest-value section for the deployment surface** — read before
changing any settings that affect how the app starts, authenticates, or
presents itself. Most of these have a non-obvious failure mode (CSRF
breakage, OIDC clock skew, JS dep rebuild) that a quick scan of the
codebase won't reveal.

## Django and the warehouse

- **Django tables land in `starcubes_meta`, not `public`.** The custom DB backend
  (`starcubes/db_backends.DatabaseWrapper`) sets `search_path=starcubes_meta,public`
  and ensures the schema exists on first connection. `auth_user`,
  `django_session`, `django_migrations`, etc. are created in `starcubes_meta`
  by `manage.py migrate`. The container entrypoint runs `migrate` before
  `collectstatic`. `ensure_starcubes_meta()` checks for the `datasource` table
  (not just the schema) so it still creates starcubes_meta application tables
  when the schema already exists. SQLAlchemy connections use
  schema-qualified names. `ensure_starcubes_meta(engine, force=True)` re-runs
  the consolidated `starcubes_meta.sql` even when `datasource` already
  exists — the *only* path that lets an existing container pick up a new
  idempotent `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` block added after
  first boot (e.g. `etl_run.log_path`/`log_tail`, migration `0021`), since
  the cheap `SourcesConfig.ready()` probe used on every request-serving
  process deliberately skips the 500-line script once the table exists.
  `sources/management/commands/bootstrap.py` calls it with `force=True`
  once per container start, *before* `call_command("migrate")` — and
  captures the return value into a local `freshly_bootstrapped`, not the
  module-level `core.schema._freshly_bootstrapped` global, because
  `migrate` immediately triggers `ready()` → a second, non-forced
  `ensure_starcubes_meta()` call that would otherwise overwrite the global
  back to `False` before the admin-user-creation check reads it.

- **`query_history.user_id` has no foreign key to `auth_user`.** Not an
  oversight: `SourcesConfig.ready()` calls `ensure_starcubes_meta()` — which
  executes `migrations/starcubes_meta.sql` — and `ready()` fires *during* the
  `call_command("migrate")` in `sources/management/commands/bootstrap.py`,
  i.e. before Django's own migrations create `auth_user`. A hard FK would
  break every fresh install. Rows are cleaned up explicitly by the
  `delete_user` branch of `sources/views.py:settings_page` instead.

## Saved profiles need a volume

Source profiles are YAML files under `profiles/` (see
[`docs/source-profiles.md`](source-profiles.md)). Two directories:

- `profiles/examples/` — shipped, tracked in git, arrives via `COPY . /app/`.
- `profiles/user/` — written at runtime by **Save as Profile** and
  **Upload**.

`COPY . /app/` puts `profiles/user/` inside the image, so anything saved
there is **discarded by the next build**. Mount it:

```
-v starcubes-profiles:/app/profiles/user
```

`STARCUBES_PROFILE_DIR` redirects the saved-profile directory somewhere
else entirely, if a volume at that path is inconvenient. It is read from
the environment in `core/profile.py` rather than from Django settings —
`core/` must not import Django.

The shipped examples stay read-only from the GUI: `profile_delete` refuses
anything that is not in the user directory.

## CSRF

- **`CSRF_TRUSTED_ORIGINS` must be set explicitly via `STARCUBES_CSRF_TRUSTED_ORIGINS`
  (comma-separated `scheme://host[:port]` entries) for any deployment behind
  the bundled nginx+uwsgi.** With `ALLOWED_HOSTS=["*"]` and nginx's catch-all
  `server_name _;` in `docker/nginx.conf`, Django's Origin check on POST
  (`request.get_host()`-derived "good origin" vs. the browser's `Origin`
  header) is fragile across the nginx→uwsgi hop and rejects logins with
  "Origin checking failed" even on a same-origin, same-scheme request. The
  explicit allowlist sidesteps the derivation entirely. Empty by default —
  must be set per deployment (e.g. `http://127.0.0.1:8080` for local docker
  access, plus any other hostname/IP used to reach it).

## SSO / OIDC

- **Keycloak/OIDC SSO configuration lives in `STARCUBES_OIDC_*` env vars,
  not a `starcubes_meta` table, even though every other piece of
  GUI-editable config in the app is DB-backed.** Two reasons, not one:
  first, there is no existing global/singleton config table anywhere in
  `starcubes_meta.sql` — every table is either per-datasource (FK'd to
  `datasource_id`) or per-user history (`query_history`), so a DB-backed
  version would introduce a wholly new kind of table for one feature.
  Second, and more binding: `mozilla-django-oidc` reads
  `OIDC_RP_CLIENT_ID`/`OIDC_OP_*_ENDPOINT`/etc. from `django.conf.settings`
  once at process start, not per-request from a DB — making it
  DB-configurable would mean abandoning the library for a hand-rolled OIDC
  client, which is exactly the kind of "engine must not import Django, and
  don't hand-roll what a library already does correctly" tradeoff this
  file elsewhere argues against paying without a forcing reason. Presence
  of all three required vars (`STARCUBES_OIDC_ISSUER`,
  `STARCUBES_OIDC_CLIENT_ID`, `STARCUBES_OIDC_CLIENT_SECRET`) *is* the
  on/off switch — no separate boolean — the same shape as
  `STARCUBES_CSRF_TRUSTED_ORIGINS` above being empty meaning "not
  configured". `starcubes/settings.py` derives all four Keycloak OP
  endpoints (`.../protocol/openid-connect/{auth,token,userinfo,certs}`)
  from the single `STARCUBES_OIDC_ISSUER` realm URL by string
  concatenation — no discovery HTTP call at Django boot, no dependency on
  Keycloak being reachable to start the app — mirroring
  `STARCUBES_POSTGRES_DSN`'s "one URL implies everything" shape rather
  than exposing four endpoint settings the operator could get out of sync.
  `OIDC_RP_SIGN_ALGO` is set explicitly to `RS256`: the library defaults
  to `HS256`, and Keycloak signs with `RS256` by default, so leaving it
  unset fails every login's token verification. `starcubes/urls.py` mounts
  `mozilla_django_oidc.urls` under `/oidc/` only when configured, so an
  unconfigured deployment 404s there rather than exposing a route that
  500s on missing settings; `RequireLoginMiddleware`'s
  `_PROTECTED_PREFIXES` (`/sources/`, `/admin/`) don't cover `/oidc/`, so
  no middleware change was needed. `starcubes/oidc.py:KeycloakOIDCBackend`
  overrides `get_username()` to key the Django username on Keycloak's
  `preferred_username` claim (needs `OIDC_RP_SCOPES = "openid email
  profile"` — the library's default scope list omits `profile`, and
  without it Keycloak may not send the claim at all) instead of the
  library's default email-hash username, and force-clears
  `is_superuser`/`is_staff` in `create_user()` so an auto-provisioned SSO
  account can never come back privileged — `sources/views.py:2246`'s
  `settings_page` is still the only place `is_superuser` is checked
  anywhere in the app, and it stays a manually-granted local flag for
  both password and SSO accounts alike. `filter_users_by_claims()` is
  deliberately left at the library default (matches existing users by
  email, case-insensitive): a local password account and a Keycloak
  identity sharing an email merge into one account on first SSO login,
  which is treated as intentional rather than worked around — the one
  sharp edge is a local *username* colliding with a `preferred_username`
  claim whose email differs, which hits Django's unique-username
  constraint on that identity's first SSO login instead of merging (a
  500, not a silent failure). Deliberately **not** set anywhere is
  `SECURE_PROXY_SSL_HEADER`: `mozilla-django-oidc.utils.absolutify()`
  builds the OIDC `redirect_uri` from `request.is_secure()`, so behind a
  TLS-terminating proxy Django needs to trust `X-Forwarded-Proto` or it
  computes `http://...` and Keycloak rejects the redirect URI as a
  mismatch — but that setting also flips CSRF cookie `Secure` flags
  app-wide and would be wrong for a non-proxied (e.g. local) deployment,
  so it's a documented manual step in `HOWTO.md` ("SSO login") rather than
  inferred, same posture as `STARCUBES_CSRF_TRUSTED_ORIGINS` itself.

## Build / dependencies

- **Always build the container image via `docker/build.sh`, never
  `podman build`/`docker build` directly.** The wrapper exists for two
  reasons: (a) it defaults the container engine to `podman` (`CONTAINER_ENGINE`
  overrides), and (b) it pins the working directory to the repo root before
  delegating. There's no longer a submodule to initialize at build time —
  the JS dependency (`@hgruber/timeseries.js@~0.10.3`) is pinned via the
  top-level `package.json`/`package-lock.json` pair and pulled by the
  `jsbuilder` Dockerfile stage through `npm ci`.

- **Frontend JS dependencies live at the repo root.** Top-level
  `package.json`/`package-lock.json` declare the only frontend JS dep
  (`@hgruber/timeseries.js`, pinned `~0.10.3`). The Dockerfile's `jsbuilder`
  stage runs `npm ci --omit=dev` and copies `dist/timeseries.min.js` into
  `viz/static/timeseries/timeseries.js`, which `STATICFILES_DIRS` exposes to
  Django's `collectstatic`. `chart.html` loads it via
  `{% static 'timeseries/timeseries.js' %}`. No `node_modules` reaches the
  runtime stage — `.dockerignore` blocks it. To bump timeseries.js:
  `npm update @hgruber/timeseries.js` (or edit the version in `package.json`)
  and rebuild.

- **`requirements.txt` is version-pinned (`==`), not left bare.** It
  originally listed bare package names, so every image build re-resolved
  latest-on-the-day — `docker/build.sh` → `Dockerfile`'s builder stage runs
  `pip install --no-cache-dir -r requirements.txt` with no lockfile. That
  silently pulled in Django 6.0 (dropped support for PostgreSQL < 15) against
  a deployment still on PostgreSQL 14.10, and broke `manage.py migrate` at
  container start with `NotSupportedError: PostgreSQL 15 or later is
  required` — the same class of surprise the pinned `@hgruber/timeseries.js`
  dependency above already avoids for the JS side. `django==5.2.17` is
  pinned specifically to the 5.2 LTS line (PostgreSQL 13+) rather than to
  whatever 6.x is newest at pin time; the other seven pins just freeze
  whatever resolved cleanly against it. Bumping Django past 5.2 requires
  confirming the deployment's PostgreSQL version first, not just checking
  the Django changelog.

## Branding

- **The starcubes logo/icon ships as light/dark asset pairs, not a single
  `currentColor` SVG.** `viz/static/viz/starcubes_icon.svg` /
  `starcubes_lockup.svg` (icon-only vs. icon+wordmark) are duotone with
  several fixed hex fills — unlike the pre-rebrand logo they replaced,
  which adapted to theme via a single `currentColor` fill. A blanket
  `currentColor` swap isn't possible without flattening the duotone design,
  so each has a `*_dark.svg` sibling: shape fills `#2B3A55`→`#7C8FC9`,
  `#46567A`→`#93A3D1`, `#5C6B87`→`#AAB7DC` (navy/slate tones lightened for
  contrast against the `#1a1a1a` dark background), the pink accent
  `#CF4F8F` unchanged. The lockup's "star" wordmark is a separate case —
  it needs to read as body text, not as a graphic element, so it doesn't
  follow the shape mapping: it's `#DDDDDD`, matching `--body-color` dark
  exactly (the lightened shape tone `#7C8FC9` tested too low-contrast for
  small nav-height text). Diff the light/dark SVG pairs directly if
  retuning. Two integration patterns follow from that split: the nav-brand
  logo (`sources/base.html`, `viz/templates/registration/login.html`,
  `viz/templates/viz/chart.html`) renders **both** `<img>` variants and
  toggles visibility via `[data-theme="dark"] .nav-brand-logo-{light,dark}`
  CSS (same attribute-selector pattern as `[data-theme="dark"] .errorlist`
  in `login.html`), since the app's theme lives in a `data-theme` attribute
  set from `localStorage`, not `prefers-color-scheme`. The chart watermark
  (`chart.html`, drawn to canvas by `timeseries.js`, no CSS access once
  drawn) instead picks the file server-side-templated-but-JS-branched: the
  `_isDark` check already used for `ts.setColors()` is hoisted before the
  `new TimeSeries({...})` call so `watermark:` can pick between the two
  `{% static %}` paths. Favicons use a third mechanism, native
  `<link rel="icon" media="(prefers-color-scheme: dark)">` pairs, because
  the tab icon reflects OS theme rather than the app's in-page toggle.