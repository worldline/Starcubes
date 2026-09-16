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

## Saved profiles need no volume

Source profiles (see [`docs/source-profiles.md`](source-profiles.md)) come
from two places:

- `profiles/examples/` — shipped, tracked in git, arrives via `COPY . /app/`.
- `starcubes_meta.profile` — written at runtime by **Save as Profile** and
  **Upload** (migration 0045).

They used to be files under `profiles/user/`, which needed a volume
(`COPY . /app/` put the directory inside the image, where the next build
discarded it) and could not be shared by several containers. **An existing
volume can stay mounted for one upgrade:** `manage.py bootstrap` imports its
files into the table — once, never over an existing row, without deleting
anything — and reports each one in the container log. After that the mount
(and `STARCUBES_PROFILE_DIR`, which only that import still reads) can go.

The shipped examples stay read-only from the GUI: `profile_delete` refuses
anything that is not a saved profile.

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

## Several containers behind a reverse proxy

Any number of identical containers may share one database behind a load
balancer — round robin, no sticky sessions. Sessions live in the database,
mozilla-django-oidc keeps its `state`/`nonce` in the session (so the SSO
callback may land on another container than the login redirect), and every
run, stop request, log line and saved profile is in the database too. What
has to be true outside the application:

- **The same `STARCUBES_SECRET_KEY` everywhere.** A session or CSRF token is
  signed with it; a container with another key rejects what its neighbours
  issued, which shows as sporadic logouts and 403s. `manage.py bootstrap`
  therefore records an HMAC fingerprint of the key (never the key) in
  `app_config` under `STARCUBES_SECRET_KEY_FINGERPRINT`, and a container with a
  different key **refuses to start** with a message saying so. To change the
  key on purpose, start one container with the new key and
  `manage.py bootstrap --rotate-secret`, then the rest with the same key.
  Skipped with `STARCUBES_DEBUG=1`, so a developer's `runserver` against a
  shared database keeps working.
- **The same `STARCUBES_ALLOWED_HOSTS`, `STARCUBES_CSRF_TRUSTED_ORIGINS`
  (the public proxy origin), `STARCUBES_TRUST_PROXY_SSL=1` when TLS ends at
  the proxy, and all `STARCUBES_OIDC_*`** — Keycloak knows one redirect URI,
  the public one, for all containers.
- **The proxy passes `Host` unchanged, port included, and sets
  `X-Forwarded-Proto`.** Django does not read `X-Forwarded-Host`; the
  container's own nginx (section CSRF above) stays as it is. Proxy timeouts at
  least 120 s, matching uwsgi's `harakiri`.
- **Every container connects as the same database role, directly or through
  session pooling** — never a transaction pooler. Run liveness is read from
  `pg_stat_activity`, which hides another role's `backend_start`, and advisory
  locks are session-scoped (`core/lease.py`, `core/pglock.py`).
- **`GET /healthz`** is the readiness probe: no login, 200 when the metadata
  database answers and this container's `node_agent` is alive, else 503 with
  the failing check named (`STARCUBES_HEALTHZ_REQUIRE_AGENT=0` drops the
  agent check). The body names no host, version or credential.
- **A stop timeout longer than the drain** (see Processes below): podman
  `--stop-timeout 35`, Kubernetes' default 30 s grace against the 25 s
  `STARCUBES_DRAIN_TIMEOUT_S`.

Static files carry a content hash in their names
(`starcubes/storage.py`, `STORAGES` in settings), because nginx serves
`/static/` as `immutable` for 30 days and a rolling update puts two image
versions behind one proxy: without hashes a page from the new image could be
given the old image's script by an old container, under the same name.
Source-map comments in vendored bundles are left alone — their `.map` files
are not shipped and would otherwise abort `collectstatic`.

Configuration edited in the GUI (`app_config`) reaches the other containers
within 30 s (`core/config.py`'s cache). The About dialog lists the containers
currently sharing the database (`/sources/nodes/`).

## SSO / OIDC

- **Keycloak/OIDC SSO configuration is env-first with a DB fallback**
  (`starcubes_meta.app_config`, see "Global configuration" above — the
  GUI's Transports tab edits those rows). Historically the OIDC settings
  were env-only, because `mozilla-django-oidc` reads
  `OIDC_RP_CLIENT_ID`/`OIDC_OP_*_ENDPOINT`/etc. from `django.conf.settings`
  once at process start, not per-request from a DB. The compromise now:
  `SourcesConfig.ready()` applies the DB trio onto the settings before the
  URLconf mounts `/oidc/`, so DB-configured SSO works — but **enabling or
  disabling it through the GUI applies on restart**, the one honest limit
  of reading a library that freezes its route mount at process start.
  Presence of all three required vars (`STARCUBES_OIDC_ISSUER`,
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

## Global configuration: DB-first, environment overrides

The global settings an operator may want to edit without redeploying — the
notification transports and the SSO/OIDC login — live in **one** table,
`starcubes_meta.app_config` (migration 0031). One row per setting, and the
row's `key` **is** the env-var name it shadows. Resolution is env-first
everywhere (`core/config.py`):

1. env var set → the env value wins;
2. otherwise the `app_config` row;
3. otherwise the hardcoded default.

The env vars (listed under [Alarm transports and SSO](#alarm-transports-and-sso)
in the reference below) are therefore still the "how to configure at first
boot" path — the GUI (global tab **Transports**, superuser-only) edits the
DB rows for exactly these keys. Env-set keys render disabled on that page
(a GUI edit would be invisible); secrets are stored like
`datasource.source_dsn` — plaintext in the meta schema, masked in every
listing, never logged. Transports apply live (consumers resolve through a
30 s-TTL cache; `etl_run`/`alarm_run` resolve the same way, since the DB is
the shared state). SSO enable/disable applies on **restart** (the `/oidc/`
route mount and the backend list are fixed at process start); the client
secret and endpoints re-read per request, so a corrected secret works
immediately.

Everything else in the [reference](#environment-variable-reference) is
**env-only** — the infrastructure an instance cannot ask its own database
about, and the tuning knobs read once at import.

PagerDuty has **no global on/off**: it is enabled per datasource by its
`pagerduty_routing_key` (the Events API v2 integration token, maintained on
the source edit page — a credential, so it is a password-type field and is
never exported in a profile).

Worked examples (env at first boot, or rows in `app_config` afterwards —
the GUI writes the same keys):

```
# Internal relay, no auth, STARTTLS on the plain submission port:
STARCUBES_SMTP_HOST=relay.internal.example  STARCUBES_SMTP_FROM=starcubes@internal.example

# Google-style submission: implicit TLS on 465 with login:
STARCUBES_SMTP_HOST=smtp.gmail.com STARCUBES_SMTP_PORT=465 STARCUBES_SMTP_TLS=ssl \
STARCUBES_SMTP_USER=bot@example.com STARCUBES_SMTP_PASSWORD=… \
STARCUBES_SMTP_FROM=bot@example.com
```

## Environment variable reference

Every environment variable starcubes reads, and the few it sets for itself.
Two default columns, because there are two fallbacks: **Default** is what
the code assumes when the variable is unset — a checkout under `manage.py
runserver` — and **Image** is what a container gets from the `Dockerfile`'s
`ENV` unless you pass `-e`. `=` means the image ends up with the code default.
Four pairs differ on purpose — `STARCUBES_DEBUG`, `STARCUBES_ALLOWED_HOSTS`,
`STARCUBES_SECRET_KEY`, `STARCUBES_ETL_LOG_STDOUT`: the checkout defaults suit
a developer, the image defaults something reachable.

A process reads its environment once, so a change takes a restart. The one
live path is the GUI, and only for the
[alarm transports and SSO](#alarm-transports-and-sso).

### Database, HTTP and Django

| Variable | Default | Image | Effect |
|---|---|---|---|
| `STARCUBES_POSTGRES_DSN` | `""` | `""` | **Required.** URL of the PostgreSQL database holding `starcubes_meta` and the fact tables, e.g. `postgresql://user:pw@host:5432/starcubes`. Django and `core/` both read it; empty leaves Django on a dummy backend and nothing works. |
| `STARCUBES_SECRET_KEY` | `dev-key-change-me` | `change-me-in-production` | Django's signing key for sessions and CSRF tokens. Set it on every real deployment, the same in every container — `bootstrap` refuses a key that does not match the recorded fingerprint ([Several containers](#several-containers-behind-a-reverse-proxy)). |
| `STARCUBES_DEBUG` | `1` | `0` | Django `DEBUG`. `1` also skips the secret-key fingerprint check. |
| `STARCUBES_ALLOWED_HOSTS` | `*` | `127.0.0.1,localhost,[::1]` | Comma-separated host names Django answers. A security setting, not a convenience one — see [CSRF](#csrf). |
| `STARCUBES_CSRF_TRUSTED_ORIGINS` | `""` | = | Comma-separated `scheme://host[:port]` origins trusted besides the one Django derives from `Host`. |
| `STARCUBES_TRUST_PROXY_SSL` | `0` | `0` | `1` trusts `X-Forwarded-Proto`; only behind a TLS-terminating proxy that sets it. `docker/entrypoint.sh` reads it too, for nginx's scheme pinning — see [SSO / OIDC](#sso-oidc). |
| `STARCUBES_STATIC_ROOT` | `<repo>/staticfiles` | = | Target directory of `collectstatic`. The image's nginx serves `/static/` from `/app/staticfiles` whatever this says, so leave it alone in a container. |

### Alarm transports and SSO

Also editable in the GUI (global tab **Transports**, stored in `app_config`),
with the environment winning — see
[Global configuration](#global-configuration-db-first-environment-overrides).
An **empty** value counts as unset in both places: an empty environment
variable falls through to the stored row, and clearing a field in the GUI
deletes the row, so the code default comes back.

A container gets the code defaults too: the `Dockerfile` sets these keys
empty or not at all, never to a value, because a non-empty environment value
counts as set and locks the field on the Transports page.

| Variable | Default | Image | Effect |
|---|---|---|---|
| `STARCUBES_SMTP_HOST` | `""` | = | SMTP relay. With `STARCUBES_SMTP_FROM` also set, the e-mail transport is on. |
| `STARCUBES_SMTP_PORT` | `25` | = | |
| `STARCUBES_SMTP_USER` | `""` | = | With `STARCUBES_SMTP_PASSWORD` also set, the connection logs in (`AUTH LOGIN`) after STARTTLS. |
| `STARCUBES_SMTP_PASSWORD` | `""` | = | See `STARCUBES_SMTP_USER`. |
| `STARCUBES_SMTP_FROM` | `""` | = | Envelope From and the `From:` header; required for e-mail to be on. |
| `STARCUBES_SMTP_TLS` | `starttls` | = | `starttls`, `ssl` (implicit TLS, port 465) or `none`. |
| `STARCUBES_SMTP_SUBJECT_PREFIX` | `[starcubes]` | = | Subject prefix. `none` sends subjects without one — an empty value would bring the default back. |
| `STARCUBES_XMPP_JID` | `""` | = | The bot account's JID. With `STARCUBES_XMPP_PASSWORD` also set, the XMPP transport is on. |
| `STARCUBES_XMPP_PASSWORD` | `""` | = | |
| `STARCUBES_XMPP_HOST` | `""` | = | Host override; skips the SRV lookup of the JID's domain. |
| `STARCUBES_XMPP_PORT` | `5222` | = | Used only together with `STARCUBES_XMPP_HOST`. |
| `STARCUBES_PAGERDUTY_EVENTS_URL` | `https://events.pagerduty.com/v2/enqueue` | = | Point it at the sandbox for a dry run. PagerDuty itself is switched on per datasource, by its routing key. |
| `STARCUBES_PAGERDUTY_SEVERITY` | `critical` | = | Severity for an alarm that sets none (`alarm.severity`). |
| `STARCUBES_OIDC_ISSUER` | `""` | = | Keycloak realm URL, no trailing slash. With client id and secret also set, SSO is on (after a restart). |
| `STARCUBES_OIDC_CLIENT_ID` | `""` | = | |
| `STARCUBES_OIDC_CLIENT_SECRET` | `""` | = | |
| `STARCUBES_OIDC_DISPLAY_NAME` | `Keycloak` | = | Button text on the login page. |

### Chart query console

| Variable | Default | Image | Effect |
|---|---|---|---|
| `STARCUBES_QUERY_TIMEOUT_MS` | `30000` | = | Statement timeout of an ad-hoc query. Clamped to 90 000 whatever is set, because uwsgi's `harakiri` kills the whole worker at 120 s. |
| `STARCUBES_QUERY_ROW_LIMIT` | `1000` | = | Rows a query result returns at most. |

### Logging

| Variable | Default | Image | Effect |
|---|---|---|---|
| `STARCUBES_LOG_LEVEL` | `INFO` | = | Level of the root logger in the background commands (`etl_run`, `alarm_run`, `etl_audit`, `etl_housekeeping`, `node_agent`, `node_drain`, `pipeline_watch`, `etl_resume_forever`), and of the SSO loggers (`mozilla_django_oidc`, `starcubes.oidc`). A Python level name. |
| `STARCUBES_LOG_DIR` | `<repo>/logs` | `/app/logs` | Local spool directory: each spawned process writes its log here and ships it into `log_line`. Ephemeral scratch — an `emptyDir` is enough, the table is the log. Must be writable. |
| `STARCUBES_LOG_MAX_BYTES` | `67108864` (64 MiB) | = | Size at which a spool file is truncated in place. |
| `STARCUBES_LOG_RETENTION_DAYS` | `14` | = | Age after which the `node_agent` deletes log lines. |
| `STARCUBES_LOG_MAX_LINES_PER_RUN` | `200000` | = | Lines one run may keep; the oldest go, with a marker line. |
| `STARCUBES_ETL_LOG_STDOUT` | `0` | `1` | `1` mirrors ETL log lines onto PID 1's stdout, so `podman logs` shows them. |
| `STARCUBES_ETL_LOG_INTERVAL_S` | `30` | = | Interval of the ETL's progress heartbeat line (queue depths, per-stage rates). |

See [`etl-engine.md`](etl-engine.md), "ETL logging", for how spool files and
the table fit together.

### Checks, limits and alarms

The context for these is [`etl-automation.md`](etl-automation.md). All have
sane defaults and none is normally set.

| Variable | Default | Image | Effect |
|---|---|---|---|
| `STARCUBES_INDEX_WARN_ROWS` | `1000000` | = | Rows loaded before a missing index is worth a warning. |
| `STARCUBES_INDEX_WARN_HOURS` | `24` | = | And how long the datasource must have existed. |
| `STARCUBES_HWM_GAP_WARN_S` | `86400` | = | Watermark-above-fact-tip gap that becomes a warning. |
| `STARCUBES_DIM_FULL_WARN_PCT` | `80` | = | Dimension fill level that becomes a warning. |
| `STARCUBES_MAX_RETENTION_DROPS` | `2` | = | Partitions retention may drop per housekeeping run. |
| `STARCUBES_MAX_OVERLAP_PKS` | `5000000` | = | Ceiling on a de-duplicated reload. |
| `STARCUBES_MAX_AUDIT_IDS` | `2000000` | = | Per-side ceiling on an audit's id comparison (about 46 bytes per id per side, measured). Above it the comparison refuses rather than exhaust a web worker. |
| `STARCUBES_BACKFILL_IDS_MAX` | `100000` | = | Missing ids an audit will store for a targeted backfill. Past it the run is marked `ids_truncated` and only a window backfill is offered. |
| `STARCUBES_ALARM_TIMEOUT_MS` | `30000` | = | Statement timeout on the alarm evaluator's fact queries. |
| `STARCUBES_PIPELINE_ALARM_INTERVAL_S` | `60` | = | Interval of `pipeline_watch`, and the throttle of the request-path pipeline-alarm evaluator. |

### Containers and processes

| Variable | Default | Image | Effect |
|---|---|---|---|
| `STARCUBES_HEALTHZ_REQUIRE_AGENT` | `1` | = | `0` drops the `node_agent` check from `GET /healthz`. |
| `STARCUBES_FOREVER_ADOPT` | `1` | = | `0` stops this container's `node_agent` from adopting other containers' forever sessions. A container start still resumes dead ones. |
| `STARCUBES_DRAIN_TIMEOUT_S` | `25` | = | How long `node_drain` waits at shutdown for this container's runs to hand over. Must be shorter than the runtime's stop timeout. |

### Build, command line and upgrades

| Variable | Default | Image | Effect |
|---|---|---|---|
| `STARCUBES_VERSION` | from git | from `docker/build.sh` | The version the About dialog names. Passed in as a build argument; an image built without one is refused. See [`releases.md`](releases.md). |
| `STARCUBES_BUILD_REF` | from git | from `docker/build.sh` | Short commit the image was built from. |
| `STARCUBES_BUILD_DATE` | `""` | from `docker/build.sh` | Build time, UTC. |
| `CONTAINER_ENGINE` | `podman` | — | Read by `docker/build.sh` on the build host: `docker` builds with Docker. |
| `LD_LIBRARY_PATH` | unset | `/app/oracle` | Where `init_oracle_client()` looks for `libclntsh.so`. Found: oracledb runs in thick mode and can compress; not found: thin mode, no compression. |
| `STARCUBES_SOURCE_DSN` | `""` | = | Fallback for `seed_example --source-dsn`. |
| `STARCUBES_PROFILE_DIR` | `<repo>/profiles/user` | = | Directory of file-based saved profiles from before migration 0045. Read only by the one-time import in `bootstrap`; see [Saved profiles need no volume](#saved-profiles-need-no-volume). |

### Set by starcubes itself

These show up in a process's environment, but are not configuration —
setting them has no useful effect.

| Variable | Set by | Purpose |
|---|---|---|
| `STARCUBES_INSTANCE_ID` | `docker/entrypoint.sh`, else the first `core/node.py:instance_id()` call | One id per container start, inherited by every child process. The entrypoint overwrites any value passed in. |
| `STARCUBES_ETL_LOG_PATH` | `sources/etl_spawn.py` | The spool file a spawned child writes its log to. |
| `STARCUBES_BOOTSTRAPPED` | `manage.py` | Makes `runserver` bootstrap only once, not again in its autoreload child. |
| `DJANGO_SETTINGS_MODULE` | `manage.py`, `starcubes/wsgi.py` | `starcubes.settings`, as a default only. |

Two names look like environment variables and are not:
`STARCUBES_SECRET_KEY_FINGERPRINT` is a row in `app_config`, written by
`bootstrap`, and `STARCUBES_ALARM_EMAIL_ENABLED` / `_XMPP_ENABLED` are Django
settings derived from the transport variables.

## Processes in the container

`docker/entrypoint.sh` runs `bootstrap`, then `etl_resume_forever`, then
starts **four** long-lived things: `pipeline_watch &`, `node_agent &`,
`nginx &` and `exec uwsgi`. The last of those was the whole architecture until
pipeline alarms landed.

`manage.py node_agent` is the one thing a container must do for its own
processes when several containers share a database: a forced Kill pressed on
any of them is written onto the run row (`control = 'kill'`), and only the
container that runs the process can send the SIGKILL. The agent is woken by
`NOTIFY starcubes_control`, verifies the pid against the run's lease before
signalling, and closes the row. Without it a forced kill across containers
waits forever; a plain Kill does not need it, because the process reads the
request itself. Same caveat as below: nothing supervises it.

`manage.py pipeline_watch` evaluates pipeline alarms and dispatches their
notifications on an interval (`STARCUBES_PIPELINE_ALARM_INTERVAL_S`, default
60 s). It exists because of a boundary rather than a preference: a wedged
ETL loop cannot report that it is wedged, and a datasource whose ETL has
stopped produces no loads for an in-loop evaluator to ride on. See U7 in
[`etl-automation.md`](etl-automation.md), which sets out the two honest
options and their prices.

**Shutdown hands the work over.** The entrypoint no longer `exec`s uwsgi: the
shell stays PID 1, because PID 1 is the only process a container stop
signals. On SIGTERM it runs `manage.py node_drain` — stop this container's
`node_agent`, SIGTERM this container's ETL and rollup processes, wait up to
`STARCUBES_DRAIN_TIMEOUT_S` (25 s) — and only then stops uwsgi. A forever
session released that way is adopted by another container at once. Give the
runtime a longer stop timeout than the drain: **podman's default is 10 s**
(`podman run --stop-timeout 35`), Kubernetes' `terminationGracePeriodSeconds`
is 30 s. PID 1 also reaps orphaned ETL processes, as uwsgi's master did.

**Nothing supervises it.** If it dies, nothing restarts it and nothing says
so — exactly as for the `nginx &` on the next line. That is stated here and
in the entrypoint rather than left to be discovered. Two mitigations exist
without adding a supervisor: `PipelineAlarmMiddleware` evaluates the same
rules from the request path, and the ETL loop evaluates them after every
load, so the container losing this process degrades rather than goes blind.
It touches `starcubes_meta` only — no source connection — so it keeps
working for a datasource whose source database is down, which is one of the
states it exists to report.

## Build / dependencies

- **Always build the container image via `docker/build.sh`, never
  `podman build`/`docker build` directly.** The wrapper exists for two
  reasons: (a) it defaults the container engine to `podman` (`CONTAINER_ENGINE`
  overrides), and (b) it pins the working directory to the repo root before
  delegating. There's no longer a submodule to initialize at build time —
  the JS dependency (`@hgruber/timeseries.js@~0.10.6`) is pinned via the
  top-level `package.json`/`package-lock.json` pair and pulled by the
  `jsbuilder` Dockerfile stage through `npm ci`.

- **Frontend JS dependencies live at the repo root.** Top-level
  `package.json`/`package-lock.json` declare the only frontend JS dep
  (`@hgruber/timeseries.js`, pinned `~0.10.6`). The Dockerfile's `jsbuilder`
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