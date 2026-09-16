#!/bin/sh
set -e

# ── Instance id: one per container start ─────────────────────────────────────
# Names this container to every other one sharing the database (core/node.py).
# Always generated here, never taken from the environment: replicas of one
# deployment share their environment, and a shared id would make two
# containers indistinguishable. Exported first so bootstrap, pipeline_watch,
# uwsgi and every ETL process spawned later inherit the same value.
STARCUBES_INSTANCE_ID="$(cat /proc/sys/kernel/random/uuid)"
export STARCUBES_INSTANCE_ID

# ── Bootstrap (migrations, admin user on fresh install, collectstatic) ───────
python3 /app/manage.py bootstrap

# ── Resume any forever ETL sessions left active by the previous container ────
python3 /app/manage.py etl_resume_forever

# ── Watch the pipeline from outside the process being watched ───────────────
# A wedged ETL loop cannot report that it is wedged, and a datasource whose
# ETL stopped entirely produces no loads for an in-loop evaluator to ride on.
# So pipeline alarms get a supervisor of their own -- the only thing here
# that keeps working when nobody is using the app.
#
# Started the same way nginx is, and with the same honest caveat: nothing
# supervises it. If it dies, nothing restarts it and nothing says so. That is
# the price named in docs/etl-automation.md (U7); the request-path middleware
# in starcubes/middleware.py is the second layer underneath it, and the ETL
# loop evaluates them too after every load.
python3 /app/manage.py pipeline_watch &

# ── Trust (or not) the proxy's forwarded scheme ──────────────────────────────
# uwsgi honours X-Forwarded-Proto / X-Forwarded-Ssl by itself when setting
# wsgi.url_scheme, which is what Django's request.is_secure() reads. That is
# what a TLS-terminating proxy needs — and a forgery vector without one, since
# the headers then come straight from the client. Default: overwrite both with
# the scheme nginx actually served, so a client cannot claim https.
if [ "${STARCUBES_TRUST_PROXY_SSL:-0}" = "1" ]; then
    cat > /etc/nginx/starcubes-scheme.conf <<'EOF'
# STARCUBES_TRUST_PROXY_SSL=1 — forwarded scheme passed through untouched.
EOF
else
    cat > /etc/nginx/starcubes-scheme.conf <<'EOF'
# STARCUBES_TRUST_PROXY_SSL is off — pin the forwarded scheme to what nginx
# actually served so a client-supplied header cannot fake https.
uwsgi_param HTTP_X_FORWARDED_PROTO $scheme;
uwsgi_param HTTP_X_FORWARDED_SSL   "";
EOF
fi

# ── Start nginx (foreground, daemon off) + uwsgi ──────────────────────────────
nginx &
exec uwsgi --ini /etc/uwsgi.ini