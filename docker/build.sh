#!/bin/sh
# Wrapper around `podman build` / `docker build`. The frontend JS dependency
# (`@hgruber/timeseries.js`) comes from the top-level package.json /
# package-lock.json pair via `npm ci` inside the Dockerfile's jsbuilder
# stage — no submodule dance is needed.
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -- "$script_dir/.." && pwd)
cd "$repo_root"

engine="${CONTAINER_ENGINE:-podman}"
exec "$engine" build -f Dockerfile -t starcubes:latest "$@" .
