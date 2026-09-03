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

- **`docker/nginx.conf` must override `HTTP_HOST` with `$http_host` after
  `include uwsgi_params;` — do not remove that line.** Debian's shipped
  `/etc/nginx/uwsgi_params` sets `uwsgi_param HTTP_HOST $host;` since a
  security update (the file in the image is dated 2026-06-18); it previously
  used `$http_host`. The difference is the port: `$host` drops it. Django
  builds `request.get_host()` from `HTTP_HOST`, and **two** things are derived
  from it, so one missing port breaks both at once:

  1. the CSRF "good origin" — becomes `http://example` while the browser
     sends `Origin: http://example:8080`, so every POST 403s with
     "Origin checking failed";
  2. the OIDC `redirect_uri` — `mozilla_django_oidc.utils.absolutify()`
     calls `request.build_absolute_uri()`, yielding
     `http://example/oidc/callback/` without the port, which Keycloak rejects
     with "Invalid parameter: redirect_uri".

  This bites on any rebuild that pulls the newer nginx package, with no
  application-code change in between — the symptom looks like an app
  regression and is not one. nginx's own port-preserving idiom
  (`$host$is_request_port$request_port`) needs nginx ≥ 1.30; the image ships
  1.22.1, so `$http_host` is the only option. Passing the raw `Host` header
  upstream is safe here **only because Django validates it** against
  `ALLOWED_HOSTS` — which makes `STARCUBES_ALLOWED_HOSTS` a security setting,
  not a convenience one (see below).

- **`STARCUBES_ALLOWED_HOSTS` must name the real hostnames on any reachable
  deployment.** Because of the `$http_host` override above, Django's
  `ALLOWED_HOSTS` check is the only thing standing between a forged `Host`
  header and the application — which makes this a security setting rather than
  a convenience one. The container image therefore defaults it to the loopback
  names only (`127.0.0.1,localhost,[::1]`, set in `Dockerfile`), **not** `*`,
  which would switch the check off. Reaching the container under any other
  name — the machine hostname, a LAN address, a proxied domain — means
  overriding it (`-e STARCUBES_ALLOWED_HOSTS=127.0.0.1,localhost,example.internal`);
  otherwise Django answers `400 Invalid HTTP_HOST header` before any view
  runs. The `starcubes/settings.py` fallback stays `*` for `manage.py
  runserver`, which is not exposed.

- **`CSRF_TRUSTED_ORIGINS` can additionally be set via
  `STARCUBES_CSRF_TRUSTED_ORIGINS`** (comma-separated `scheme://host[:port]`
  entries). With `HTTP_HOST` correct it is no longer *required* — Django's
  own Origin derivation works — but it stays useful as an explicit allowlist
  for origins that differ from the `Host` Django sees (e.g. a proxy publishing
  the app under another name). Empty by default. Note that an entry here
  masks a broken `HTTP_HOST` for that one origin while every other hostname
  still 403s; if only one access path works, suspect the `$host`/`$http_host`
  issue above rather than adding more entries.

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
  both password and SSO accounts alike. `filter_users_by_claims()` keeps the
  library's email lookup as its **primary** key — a local password account
  and a Keycloak identity sharing an email still merge into one account on
  first SSO login, which is intentional rather than worked around — but the
  plain lookup has two dead ends it cannot escape, both caused by point one
  above decoupling the username from the matching key: several local
  accounts sharing an email make every login fail with
  `SuspiciousOperation("Multiple users returned")`, and an empty `email`
  claim satisfies `verify_claims()` (`"email" in claims` is true for `""`)
  while matching nothing, so each login retries `create_user()` and hits the
  unique-username constraint from the second attempt on. Both are resolved
  via `preferred_username`: the multi-hit case is narrowed by it, the
  no-email case falls back to it. It is deliberately **not** consulted when
  an identity presents an email matching no local account — that would let
  anyone able to pick their own `preferred_username` in the IdP take over a
  same-named local account, superuser ones included; that case keeps the old
  behaviour, so a local *username* colliding with a `preferred_username`
  claim whose email differs still hits Django's unique-username constraint
  on first SSO login (a 500, not a silent failure). `SECURE_PROXY_SSL_HEADER` is applied
  only when **`STARCUBES_TRUST_PROXY_SSL=1`** is set:
  `mozilla-django-oidc.utils.absolutify()` builds the OIDC `redirect_uri`
  from `request.is_secure()`, so behind a TLS-terminating proxy Django must
  trust `X-Forwarded-Proto` or it computes `http://...` and Keycloak rejects
  the redirect URI as a mismatch. The CSRF origin check fails for the same
  reason on the same deployments — one scheme, two symptoms, exactly like the
  port lost via `HTTP_HOST` above. It stays **opt-in** rather than inferred
  because trusting the header makes *any* request carrying
  `X-Forwarded-Proto: https` count as secure: correct when a proxy sets that
  header itself, forgeable by any client when no such proxy exists. The
  enforcement point in the bundled image is **nginx, not Django**: uwsgi
  derives `wsgi.url_scheme` from `X-Forwarded-Proto` / `X-Forwarded-Ssl` by
  itself before Django is reached, so `docker/entrypoint.sh` writes
  `/etc/nginx/starcubes-scheme.conf` on every start — pinning both headers to
  the scheme nginx actually served when the flag is off, leaving them untouched
  when it is on. `SECURE_PROXY_SSL_HEADER` in `settings.py` expresses the same
  decision at the Django layer, where it would matter under a WSGI server that
  does not do the uwsgi trick. Note the
  knock-on effect once enabled — `request.is_secure()` turning `True` also
  switches CSRF from origin-only to Django's stricter HTTPS path, which
  additionally requires a matching `Referer` header. It does **not** by itself
  set the `Secure` flag on cookies; `CSRF_COOKIE_SECURE` / `SESSION_COOKIE_SECURE`
  are separate settings, still unset, and worth adding for an HTTPS-only
  deployment.

- **`LOGIN_REDIRECT_URL_FAILURE` and `OIDC_CALLBACK_CLASS` are set on
  purpose — do not drop them as redundant.** Every failure branch of
  `mozilla_django_oidc`'s callback view redirects to
  `LOGIN_REDIRECT_URL_FAILURE`, whose library default is `/` — which is
  exactly where a *successful* login lands (`LOGIN_REDIRECT_URL`). Left at
  the default, a broken SSO login is byte-for-byte indistinguishable from a
  working one, which is how "Keycloak says yes but I am not logged in" went
  undiagnosed. Failures now go to `/login/?sso=failed`, which the login
  template renders as an error. `OIDC_CALLBACK_CLASS` points at
  `starcubes.oidc.LoggingOIDCCallbackView`, which overrides only
  `login_failure()` / `login_success()` — every exit of the library's `get()`
  funnels through exactly one of the two, so that covers all of them without
  duplicating its flow control. `mozilla_django_oidc/urls.py` reads the
  setting via `import_from_settings` at import time, so no second route is
  needed in `starcubes/urls.py` (which would collide on the `reverse()`
  name). Both live inside the `if OIDC_ENABLED:` block so the SSO-off path is
  unchanged. `LOGGING` gains handlers for the `mozilla_django_oidc` and
  `starcubes.oidc` loggers for the same reason: without them both propagate
  to a handler-less root and fall through to `logging.lastResort`.

- **The `$host` regression predates `1a26763` ("Slim the container image").**
  A starcubes image built before that commit already carries
  `uwsgi_param HTTP_HOST $host` — the nginx package change came in with an
  earlier rebuild. The slimming commit is not implicated in any login
  breakage; what it did break was uwsgi's PCRE/routing support and
  `django/test/` (see "Build / dependencies").

## Build / dependencies

- **Always build the container image via `docker/build.sh`, never
  `podman build`/`docker build` directly.** The wrapper exists for two
  reasons: (a) it defaults the container engine to `podman` (`CONTAINER_ENGINE`
  overrides), and (b) it pins the working directory to the repo root before
  delegating. There's no longer a submodule to initialize at build time —
  the JS dependency (`@hgruber/timeseries.js@~0.10.4`) is pinned via the
  top-level `package.json`/`package-lock.json` pair and pulled by the
  `jsbuilder` Dockerfile stage through `npm ci`.

- **Frontend JS dependencies live at the repo root.** Top-level
  `package.json`/`package-lock.json` declare the only frontend JS dep
  (`@hgruber/timeseries.js`, pinned `~0.10.4`). The Dockerfile's `jsbuilder`
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

- **The image-slimming step has two traps that were sprung once already.**
  `1a26763` shrank the builder to `python:3.12-slim-bookworm` and stripped
  site-packages, and took two things with it that nothing complained about:

  1. **uwsgi lost PCRE.** uwsgi compiles *without* its regex features when
     the build host has no PCRE headers — no error, no warning in the image,
     `route` rules and a regex `static-map` simply stop matching at runtime.
     The builder therefore installs `libpcre2-dev` alongside `gcc` /
     `libc6-dev`. It costs nothing in the final image: the runtime stage
     already carries `libpcre2-8-0` as an nginx dependency. Verify with
     `uwsgi --help | grep -ci pcre` inside the built image — it must be `>0`.
  2. **`django/test/` was deleted.** The strip ran
     `find … \( -name 'tests' -o -name 'test' \) -type d -exec rm -rf {} +`,
     and the singular `test` matches exactly one directory in site-packages:
     Django's own `django/test/` package (520 KB). The `find` is now plural
     only. Verify with `ls -d …/site-packages/django/test` in the image.

  Both were found by diffing a pre-slimming image against the current one —
  the same technique that found the `HTTP_HOST` regression. Keep an image
  from before a risky base-image change around long enough to diff it.

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