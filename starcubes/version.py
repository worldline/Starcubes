"""What version of starcubes is this, and which build of it.

**The git tag is the version.** There is no number to maintain in this file:
`__version__` is derived from `git describe` at import, so a tag and the
version can never disagree, and no release can forget to bump anything.

Everything that names a version reads it from here:

* the GUI's About dialog, via `starcubes.context_processors.about_info`
* the container image tag and OCI labels, via `docker/build.sh`, which runs
  this module with a bare `python3 -c` rather than reimplementing the
  describe logic in shell

Deliberately stdlib-only and free of Django — that is precisely what lets
`build.sh` ask it, from a checkout with nothing installed and no
`STARCUBES_POSTGRES_DSN` set.

`package.json`'s version field is *not* kept in sync on purpose: it is a
private vendoring manifest for `@hgruber/timeseries.js`, and any edit there
has to be mirrored into `package-lock.json` or the Dockerfile's `npm ci`
aborts on the mismatch. See docs/releases.md.

Inside a container none of the git machinery below can run — `.dockerignore`
keeps `.git` out of the build context — so `docker/build.sh` resolves the
version at build time and passes it in as `STARCUBES_VERSION`, alongside the
commit and the build timestamp. The environment always wins over git.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

# Only tags that look like releases may name a version. Without the match,
# the first unrelated tag someone parks on a commit silently becomes the
# version number.
_TAG_GLOB = "v[0-9]*"


def _git(*args: str) -> str:
    """Run one git command in the repo, or return "" if it cannot answer.

    Every git path in this module is best-effort by design: an image has no
    `.git`, an exported tarball has no git at all, and neither is an error —
    they just mean the answer has to come from the environment instead.
    """
    try:
        out = subprocess.run(
            ("git",) + args,
            cwd=_ROOT, capture_output=True, text=True, timeout=2, check=True,
        )
        return out.stdout.strip()
    except Exception:
        return ""


def _version_from_git() -> str:
    """Build a SemVer string out of the nearest release tag.

    Two narrow questions rather than one `git describe` to parse: describe
    answers `v2.0.0-alpha.1-3-g1ae985c`, and that cannot be split on the
    hyphen, because the pre-release part contains hyphens of its own. Asking
    for the tag alone (`--abbrev=0`) and counting the distance separately
    leaves nothing ambiguous to parse.

    Anything that is not the release itself — distance, commit, dirt — goes
    after the `+` as SemVer build metadata, so the release identity in front
    of it stays exactly the tag:

        on the tag, clean      2.0.0-alpha.1
        three commits later    2.0.0-alpha.1+3.g1ae985c
        with local edits       2.0.0-alpha.1+3.g1ae985c.dirty
    """
    tag = _git("describe", "--tags", "--abbrev=0", "--match", _TAG_GLOB)
    if not tag:
        return ""
    version = tag[1:] if tag.startswith("v") else tag

    local = []
    distance = _git("rev-list", "--count", f"{tag}..HEAD")
    if distance.isdigit() and int(distance) > 0:
        local.append(f"{distance}.g{_git('rev-parse', '--short', 'HEAD')}")
    if _git("status", "--porcelain"):
        local.append("dirty")

    return version + ("+" + ".".join(local) if local else "")


def _version() -> str:
    """The version, from the environment or from the tag — "unknown" if
    neither can answer.

    There is deliberately **no hard-coded fallback number**. A constant here
    would be a second source of truth, able to drift from the tag — exactly
    the failure this module exists to remove. Both cases that occur in
    practice are covered: in a container the environment answers, in a
    checkout git does.
    """
    env = os.environ.get("STARCUBES_VERSION", "").strip()
    if env:
        return env
    if not (_ROOT / ".git").exists():
        return "unknown"
    return _version_from_git() or "unknown"


def _is_prerelease(version: str) -> bool:
    """True for a version that is on its way to a release rather than being
    one. SemVer's own rule: a hyphen in the part before the build metadata.

    So `2.0.0-alpha.1` is a pre-release, `2.0.0` is not, and `2.0.0+3.g…` is
    not either — that one is a build *after* 2.0.0, which its `+` part says
    plainly enough.
    """
    return "-" in version.split("+", 1)[0]


def _build_ref() -> str:
    """Short commit the running code was built from, or "" if unknowable.

    Just the commit: whether the tree was dirty is part of the version
    string, and saying it twice invites the two to disagree.
    """
    env = os.environ.get("STARCUBES_BUILD_REF", "").strip()
    if env:
        return env
    if not (_ROOT / ".git").exists():
        return ""
    return _git("rev-parse", "--short", "HEAD")


def _timeseries_version() -> str:
    """Version of the vendored @hgruber/timeseries.js bundle, or "".

    Read from package-lock.json, which pins the resolved version ("0.10.5"),
    not from package.json, which only carries the range ("~0.10.5").
    """
    try:
        lock = json.loads((_ROOT / "package-lock.json").read_text())
        pkg = lock.get("packages", {}).get("node_modules/@hgruber/timeseries.js", {})
        return str(pkg.get("version", ""))
    except Exception:
        return ""


def build_info() -> dict[str, object]:
    """Everything the About dialog names about this build. Computed once,
    at import — none of it can change while the process runs."""
    version = _version()
    return {
        "version": version,
        "is_prerelease": _is_prerelease(version),
        "build_ref": _build_ref(),
        "build_date": os.environ.get("STARCUBES_BUILD_DATE", "").strip(),
        "timeseries_version": _timeseries_version(),
    }


BUILD_INFO = build_info()
__version__ = BUILD_INFO["version"]
