#!/bin/sh
set -e

# ── Bootstrap (migrations, admin user on fresh install, collectstatic) ───────
python3 /app/manage.py bootstrap

# ── Resume any forever ETL sessions left active by the previous container ────
python3 /app/manage.py etl_resume_forever

# ── Start nginx (foreground, daemon off) + uwsgi ──────────────────────────────
nginx &
exec uwsgi --ini /etc/uwsgi.ini