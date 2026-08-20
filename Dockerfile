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
FROM python:3.12-bookworm AS builder

COPY requirements.txt /tmp/

RUN pip install --no-cache-dir -r /tmp/requirements.txt uwsgi \
    && rm -rf /tmp/requirements.txt

# ── Stage 3: runtime ─────────────────────────────────────────────────────────
FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
        nginx \
        libxml2 \
        libaio1 \
    && rm -rf /var/lib/apt/lists/*

# Python packages from builder
COPY --from=builder /usr/local/lib/python3.12/site-packages/ /usr/local/lib/python3.12/site-packages/
COPY --from=builder /usr/local/bin/uwsgi /usr/local/bin/uwsgi

# App
WORKDIR /app
COPY . /app/

# Vendored timeseries.js from JS vendor stage — lands in viz/static/timeseries/
# and is picked up by Django's collectstatic via STATICFILES_DIRS.
COPY --from=jsbuilder /js/vendor/timeseries/ /app/viz/static/timeseries/

# Writable dirs for rootless podman (no chown needed — rootless maps to your UID)
# /app/logs holds per-ETL-run log files (sources/etl_spawn.py); mount it as a
# volume to survive `podman rm` — see CLAUDE.md "ETL logging".
RUN mkdir -p /app/staticfiles /app/logs /run/nginx /var/log/nginx

# ── Config files ──────────────────────────────────────────────────────────────
COPY docker/nginx.conf /etc/nginx/nginx.conf
COPY docker/uwsgi.ini  /etc/uwsgi.ini
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# ── Runtime env ────────────────────────────────────────────────────────────────
# All config via env vars. Defaults shown; override at podman run/create.
# LD_LIBRARY_PATH includes /app/oracle so the dynamic linker and
# oracledb thick mode find Oracle instantclient in all processes
# (including podman exec).  init_oracle_client() scans LD_LIBRARY_PATH
# for libclntsh.so and enables thick mode automatically.
ENV STARCUBES_POSTGRES_DSN="" \
    STARCUBES_SECRET_KEY="change-me-in-production" \
    STARCUBES_ALLOWED_HOSTS="*" \
    STARCUBES_DEBUG="0" \
    LD_LIBRARY_PATH="/app/oracle" \
    STARCUBES_LOG_DIR="/app/logs" \
    STARCUBES_ETL_LOG_STDOUT="1"

EXPOSE 8080

ENTRYPOINT ["/entrypoint.sh"]