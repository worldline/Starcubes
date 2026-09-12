#!/bin/bash
# Test the local, unreleased timeseries.js working copy inside the running
# starcubes container — cut no release for every small change.
#
#   docker/refresh-timeseries.sh
#
# 1. rebuilds dist/timeseries.min.js in ~/Build/timeseries.js (src/ changes
#    always land), 2. copies it to the local vendor path (viz/static/ — what
#    a container rebuild bakes in) AND into the RUNNING container, 3. re-runs
#    collectstatic there and verifies what :8080 serves byte-matches the
#    fresh bundle.
#
# Why the touch: podman cp preserves the source's mtime, and Django's
# collectstatic skips any target that is not OLDER than its source — after a
# container restart the staticfiles copy is YOUNGER than the freshly copied
# vendor file, so collectstatic silently keeps the stale bundle ("0 copied,
# N unmodified"). touch() the source inside the container first. The cmp at
# the end catches any remaining case instead of trusting either tool.
set -euo pipefail

TS_DIR=${TS_DIR:-$HOME/Build/timeseries.js}
SC_DIR=${SC_DIR:-$HOME/Build/starcubes}
URL=${URL:-http://localhost:8080/static/timeseries/timeseries.js}

SRC="$TS_DIR/dist/timeseries.min.js"
DST="$SC_DIR/viz/static/timeseries/timeseries.js"

[ -f "$SRC" ] || { echo "kein $SRC — liegt $TS_DIR richtig?"; exit 1; }

CTR=$(podman ps --filter "ancestor=localhost/starcubes:latest" --format '{{.Names}}')
[ -n "$CTR" ] || { echo "kein laufender starcubes-Container (podman ps?)"; exit 1; }
if [ "$(echo "$CTR" | wc -l)" -gt 1 ]; then
  echo "mehrere starcubes-Container laufen: $CTR — es wird der erste bedient" >&2
  CTR=$(echo "$CTR" | head -1)
fi

(cd "$TS_DIR" && npm run build:min >/dev/null)
cp "$SRC" "$DST"
podman cp "$SRC" "$CTR:/app/viz/static/timeseries/timeseries.js"
podman exec "$CTR" touch /app/viz/static/timeseries/timeseries.js
podman exec "$CTR" sh -c 'python3 /app/manage.py collectstatic --noinput' | tail -1

curl -fsS "$URL" | cmp -s - "$SRC" \
  && echo "ausgeliefert: aktuell ($(stat -c%s "$SRC") Bytes, Container $CTR)" \
  || { echo "FEHLER: $URL serviert nicht das frische Bundle"; exit 1; }
