#!/bin/sh
# Wrapper around `podman build` / `docker build`. The frontend JS dependency
# (`@hgruber/timeseries.js`) comes from the top-level package.json /
# package-lock.json pair via `npm ci` inside the Dockerfile's jsbuilder
# stage — no submodule dance is needed.
#
# This is also where a release gets its identity. The version comes from the
# git tag, resolved by starcubes/version.py: this script *asks* that module
# rather than reimplementing `git describe` in shell, so there is exactly one
# definition of what a version is. That module is stdlib-only and free of
# Django precisely so this call works in a checkout with nothing installed
# and no STARCUBES_POSTGRES_DSN set — and, since the python3 found here is the
# build host's rather than the image's, that module is held to Python 3.6.
#
# The commit and the build timestamp are passed in for the same reason the
# version is: `.dockerignore` keeps `.git` out of the build context, so
# inside the image none of it is knowable. See the "Build identity" block at
# the end of the Dockerfile, and docs/releases.md for the release procedure.
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -- "$script_dir/.." && pwd)
cd "$repo_root"

version=$(python3 -c 'from starcubes.version import __version__; print(__version__)') || {
    echo "build.sh: could not read the version from starcubes/version.py" >&2
    echo "  (that module targets python 3.6+; check what \`python3\` is here)" >&2
    exit 1
}

# A container tag may not contain '+', which every version off a non-tagged
# commit has (2.0.0-alpha.1+3.g1ae985c). Swap it for '_' in the tag only —
# the OCI label below keeps the real version, '+' and all.
image_tag=$(printf '%s' "$version" | tr '+' '_')

build_ref=$(git rev-parse --short HEAD 2>/dev/null || true)
build_date=$(date -u +%Y-%m-%dT%H:%M:%SZ)

engine="${CONTAINER_ENGINE:-podman}"
echo "building starcubes:$image_tag (${build_ref:-no commit}) with $engine" >&2
case "$version" in
    unknown)
        echo "  note: no release tag found — this image cannot name its version" >&2 ;;
    *+*)
        echo "  note: not built from a tagged commit; see docs/releases.md" >&2 ;;
esac

# Two tags: the version this build is, and :latest for the dev loop — with no
# registry in play, :latest simply means "the one I built last", which is what
# the README quickstart runs. Extra "$@" args are appended, so an explicit -t
# still works.
exec "$engine" build -f Dockerfile \
    --build-arg "STARCUBES_VERSION=$version" \
    --build-arg "STARCUBES_BUILD_REF=$build_ref" \
    --build-arg "STARCUBES_BUILD_DATE=$build_date" \
    -t "starcubes:$image_tag" \
    -t starcubes:latest \
    "$@" .
