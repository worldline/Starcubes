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

# ── The per-container agent ──────────────────────────────────────────────────
# Carries out what only this container can do for its own processes: a forced
# kill requested from any container sharing the database. Same caveat as
# pipeline_watch — nothing supervises it. See sources/management/commands/
# node_agent.py.
python3 /app/manage.py node_agent &

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
uwsgi --ini /etc/uwsgi.ini &
UWSGI_PID=$!

# ── Shutdown: hand this container's runs over, then stop uwsgi ───────────────
# This shell stays PID 1 instead of exec'ing uwsgi, because PID 1 is the only
# process a container stop signals. On SIGTERM, node_drain stops this
# container's node_agent and asks every ETL/rollup process here to finish, so
# forever sessions are released to another container at once instead of after
# their leases time out (sources/management/commands/node_drain.py). The
# runtime's stop timeout must exceed STARCUBES_DRAIN_TIMEOUT_S (default 25 s):
# podman defaults to 10 s (--stop-timeout), Kubernetes to 30 s.
#
# PID 1 also reaps orphans, which uwsgi's master did when it was PID 1: an ETL
# process outlives the uwsgi worker that spawned it and is reparented here.
# dash's `wait` collects any exited child, which is why the loop below keeps
# waiting rather than returning after the first interruption (verified in the
# image: no zombies left).
shutdown() {
    trap - TERM INT
    python3 /app/manage.py node_drain || true
    kill -TERM "$UWSGI_PID" 2>/dev/null || true
}
trap shutdown TERM INT

while kill -0 "$UWSGI_PID" 2>/dev/null; do
    wait "$UWSGI_PID" || true
done