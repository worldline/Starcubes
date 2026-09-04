#!/bin/sh
set -e

# ── Bootstrap (migrations, admin user on fresh install, collectstatic) ───────
python3 /app/manage.py bootstrap

# ── Resume any forever ETL sessions left active by the previous container ────
python3 /app/manage.py etl_resume_forever

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