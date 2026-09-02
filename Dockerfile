# ── Stage 1: JS vendor (npm install the published package) ────────────────────
# Pulls the pre-built dist/timeseries.js from the published npm tarball and
# copies it into a stable path that Django's collectstatic will pick up via
# STATICFILES_DIRS. The published @hgruber/timeseries.js tarball ships a
# pre-built IIFE bundle in dist/, so no esbuild / bundling step is needed.
FROM node:20-slim AS jsbuilder

WORKDIR /js
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund
RUN mkdir -p /js/vendor/timeseries && \
    cp node_modules/@hgruber/timeseries.js/dist/timeseries.min.js \
       /js/vendor/timeseries/timeseries.js

# ── Stage 2: Python build ─────────────────────────────────────────────────────
# slim + gcc instead of the full bookworm image: every requirements.txt wheel
# is manylinux (nothing compiled from source) except uwsgi, which ships as
# sdist only and needs a C compiler. gcc (no recommends) keeps this stage far
# smaller than full bookworm's build-essential toolchain.
FROM python:3.12-slim-bookworm AS builder

RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev \
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
    && find /usr/local/lib/python3.12/site-packages \
        \( -name 'tests' -o -name 'test' \) -type d -exec rm -rf {} + \
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

EXPOSE 8080

ENTRYPOINT ["/entrypoint.sh"]