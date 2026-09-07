# ── Stage 1: JS vendor (npm install the published package) ────────────────────
# Pulls the pre-built dist/timeseries.js from the published npm tarball and
# copies it into a stable path that Django's collectstatic will pick up via
# STATICFILES_DIRS. The published @hgruber/timeseries.js tarball ships a
# pre-built IIFE bundle in dist/, so no esbuild / bundling step is needed.
FROM node:20-slim AS jsbuilder

WORKDIR /js
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund
# The bundle and its licence together: timeseries.js is MIT, which requires
# its copyright notice to ship with the code, and the published bundle is
# minified with no banner of its own. Both land in viz/static/timeseries/
# via the single COPY --from=jsbuilder below. See THIRD-PARTY-NOTICES.
RUN mkdir -p /js/vendor/timeseries && \
    cp node_modules/@hgruber/timeseries.js/dist/timeseries.min.js \
       /js/vendor/timeseries/timeseries.js && \
    cp node_modules/@hgruber/timeseries.js/LICENSE \
       /js/vendor/timeseries/LICENSE

# ── Stage 2: Python build ─────────────────────────────────────────────────────
# slim + gcc instead of the full bookworm image: every requirements.txt wheel
# is manylinux (nothing compiled from source) except uwsgi, which ships as
# sdist only and needs a C compiler. gcc (no recommends) keeps this stage far
# smaller than full bookworm's build-essential toolchain.
FROM python:3.12-slim-bookworm AS builder

# libpcre2-dev is not optional padding: uwsgi silently compiles WITHOUT its
# regex features when it cannot find PCRE, which takes `route` rules and a
# regex `static-map` with it — no error, they just stop matching. The runtime
# stage already carries libpcre2-8-0 (nginx depends on it), so this costs
# nothing in the final image.
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev libpcre2-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/

RUN pip install --no-cache-dir -r /tmp/requirements.txt uwsgi \
    # Runtime image doesn't need pip / setuptools / wheel — strip them after
    # the install, before the site-packages COPY into the runtime stage.
    && rm -rf /usr/local/lib/python3.12/site-packages/pip* \
              /usr/local/lib/python3.12/site-packages/setuptools* \
              /usr/local/lib/python3.12/site-packages/wheel* \
    # No package ships code under tests/ that is needed at runtime; pandas
    # alone ships 36 MB of them. Delete every site-packages tests dir.
    # Plural only — a singular `test` dir matches exactly one thing here, and
    # it is django/test/, Django's own test framework package (520 KB). Not
    # worth deleting, and deleting it breaks any import of django.test.
    && find /usr/local/lib/python3.12/site-packages \
        -name 'tests' -type d -exec rm -rf {} + \
    && true

# ── Stage 3: runtime ─────────────────────────────────────────────────────────
FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
        nginx \
        libaio1 \
    && rm -rf /var/lib/apt/lists/*

# Python packages from builder
COPY --from=builder /usr/local/lib/python3.12/site-packages/ /usr/local/lib/python3.12/site-packages/
COPY --from=builder /usr/local/bin/uwsgi /usr/local/bin/uwsgi

# App
WORKDIR /app
COPY . /app/

# Drop the .gitkeep placeholder that `COPY .` brought in for
# `viz/static/timeseries/`; the bundle below replaces it. Without this,
# `collectstatic` would see two paths for `timeseries.js` (the `.gitkeep`
# parent and the jsbuilder drop-in) and emit `Found another file`
# warnings.
RUN rm -rf /app/viz/static/timeseries

# Vendored timeseries.js from JS vendor stage — lands in viz/static/timeseries/
# and is picked up by Django's collectstatic via the `viz` app's
# staticfiles finder (INSTALLED_APPS + AppDirectoriesFinder).
COPY --from=jsbuilder /js/vendor/timeseries/ /app/viz/static/timeseries/

# Writable dirs for rootless podman (no chown needed — rootless maps to your UID)
# /app/logs holds per-ETL-run log files (sources/etl_spawn.py); mount it as a
# volume to survive `podman rm` — see CLAUDE.md "ETL logging".
RUN mkdir -p /app/staticfiles /app/logs /run/nginx /var/log/nginx

# ── Config files ──────────────────────────────────────────────────────────────
COPY docker/nginx.conf /etc/nginx/nginx.conf
# Placeholder so `nginx -t` works on the bare image; entrypoint.sh rewrites it
# on every start according to STARCUBES_TRUST_PROXY_SSL.
RUN printf 'uwsgi_param HTTP_X_FORWARDED_PROTO $scheme;\nuwsgi_param HTTP_X_FORWARDED_SSL   "";\n' \
      > /etc/nginx/starcubes-scheme.conf
COPY docker/uwsgi.ini  /etc/uwsgi.ini
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# ── Runtime env ────────────────────────────────────────────────────────────────
# All config via env vars. Defaults shown; override at podman run/create.
# LD_LIBRARY_PATH includes /app/oracle so the dynamic linker and
# oracledb thick mode find Oracle instantclient in all processes
# (including podman exec).  init_oracle_client() scans LD_LIBRARY_PATH
# for libclntsh.so and enables thick mode automatically.
# STARCUBES_ALLOWED_HOSTS is a security setting, not a convenience one:
# docker/nginx.conf forwards the raw client Host header (uwsgi_param HTTP_HOST
# $http_host) so Django sees the port it needs for the CSRF origin and the OIDC
# redirect_uri, and Django's ALLOWED_HOSTS check is what makes trusting that
# header safe. `*` would switch that check off, so the default is the loopback
# names only. Reaching the container under any other name — the machine
# hostname, a LAN address, a proxied domain — requires overriding this, or
# Django answers 400 "Invalid HTTP_HOST header".
# STARCUBES_TRUST_PROXY_SSL=1 makes Django trust `X-Forwarded-Proto: https`,
# which it needs to build https:// URLs (CSRF origin, OIDC redirect_uri) when
# TLS is terminated by a proxy in front of this container. Only enable it when
# such a proxy exists and sets that header itself — see HOWTO.md "SSO login".
ENV STARCUBES_POSTGRES_DSN="" \
    STARCUBES_SECRET_KEY="change-me-in-production" \
    STARCUBES_ALLOWED_HOSTS="127.0.0.1,localhost,[::1]" \
    STARCUBES_DEBUG="0" \
    STARCUBES_TRUST_PROXY_SSL="0" \
    LD_LIBRARY_PATH="/app/oracle" \
    STARCUBES_LOG_DIR="/app/logs" \
    STARCUBES_ETL_LOG_STDOUT="1"

# Keycloak/OIDC SSO — off by default (empty ISSUER/CLIENT_ID/CLIENT_SECRET
# means the login page shows password-only). See HOWTO.md "SSO login".
ENV STARCUBES_OIDC_ISSUER="" \
    STARCUBES_OIDC_CLIENT_ID="" \
    STARCUBES_OIDC_CLIENT_SECRET="" \
    STARCUBES_OIDC_DISPLAY_NAME="Keycloak"

# ── Build identity ────────────────────────────────────────────────────────────
# The version comes from the git tag, and the tag cannot travel in the COPY
# above: `.dockerignore` keeps `.git` out of the build context on purpose, so
# nothing inside the image can run `git describe`. docker/build.sh resolves
# the version, the commit and the build time outside and passes all three in;
# starcubes/version.py reads them back out of the environment, where they
# take precedence over the git lookup it would do in a checkout.
#
# A build that bypasses build.sh and passes nothing is refused below rather
# than quietly producing an image that has lost its identity — that is how a
# container ended up reporting its version as "unknown". Empty means "you
# forgot"; an explicit --build-arg STARCUBES_VERSION=unknown means "I know,
# I am building without git" and is let through. Only the version is
# required: the commit and the build time are additions to it.
#
# Last in the file for two reasons. A new commit changes only these three
# values, so nothing above them is invalidated — and the guard's RUN takes
# the version into its cache key, which past `npm ci` and `pip install`
# would rebuild the world on every commit. Here it sits in front of nothing
# but LABEL, EXPOSE and ENTRYPOINT, so it costs nothing.
ARG STARCUBES_VERSION=""
ARG STARCUBES_BUILD_REF=""
ARG STARCUBES_BUILD_DATE=""

RUN test -n "$STARCUBES_VERSION" || { \
      echo "" >&2; \
      echo "STARCUBES_VERSION is empty — this image could not name its own version." >&2; \
      echo "" >&2; \
      echo "  Build with ./docker/build.sh, which resolves it from the git tag." >&2; \
      echo "  To build without a version on purpose, say so:" >&2; \
      echo "      podman build --build-arg STARCUBES_VERSION=unknown ..." >&2; \
      echo "" >&2; \
      echo "  See docs/releases.md." >&2; \
      echo "" >&2; \
      exit 1; \
    }

ENV STARCUBES_VERSION=$STARCUBES_VERSION \
    STARCUBES_BUILD_REF=$STARCUBES_BUILD_REF \
    STARCUBES_BUILD_DATE=$STARCUBES_BUILD_DATE

LABEL org.opencontainers.image.title="starcubes" \
      org.opencontainers.image.description="Timeseries analytics over a SQL source: ETL, star schema and chart in one container" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="$STARCUBES_VERSION" \
      org.opencontainers.image.revision="$STARCUBES_BUILD_REF" \
      org.opencontainers.image.created="$STARCUBES_BUILD_DATE"

EXPOSE 8080

ENTRYPOINT ["/entrypoint.sh"]