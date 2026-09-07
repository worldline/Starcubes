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

`build.sh` runs this module with whatever `python3` the build host happens
to have, and a build host is not the runtime: ours has 3.6. So this file —
alone in the tree — stays **syntax- and API-compatible with Python 3.6**. No
`from __future__ import annotations`, no builtin generics in an evaluated
annotation, no `subprocess.run(capture_output=…, text=…)`. Everything else
runs only under the 3.12 of the image and may use anything.

`package.json`'s version field is *not* kept in sync on purpose: it is a
private vendoring manifest for `@hgruber/timeseries.js`, and any edit there
has to be mirrored into `package-lock.json` or the Dockerfile's `npm ci`
aborts on the mismatch. See docs/releases.md.

Inside a container none of the git machinery below can run — `.dockerignore`
keeps `.git` out of the build context — so `docker/build.sh` resolves the
version at build time and passes it in as `STARCUBES_VERSION`, alongside the
commit and the build timestamp. The environment always wins over git.

A source archive is the third way this code travels: `git archive` hands a
test environment a zip with no `.git` in it and no one to set the variable.
That case answers for itself — the two placeholder constants below are
rewritten by `git archive` as it packs, because `.gitattributes` marks this
file `export-subst`. In a checkout they keep their own literal text, are
recognised as unsubstituted and ignored, so neither path above changes.

**Never write such a placeholder anywhere else in this file, this docstring
included.** `git archive` would rewrite that occurrence just as happily, and
a docstring that mutates in an archive is a trap for whoever reads it there.
"""
import json
import os
import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

# Only tags that look like releases may name a version. Without the match,
# the first unrelated tag someone parks on a commit silently becomes the
# version number.
_TAG_GLOB = "v[0-9]*"

# Rewritten by `git archive` at pack time — this is how a source archive
# knows its version with no `.git` to ask. Each has to stay on one line: the
# substitution does not cross a newline. The match pattern is `_TAG_GLOB`
# spelled git-describe's way; the two must select the same tags, or an
# archive and the checkout it came from would name different versions.
_ARCHIVE_DESCRIBE = "$Format:%(describe:match=v[0-9]*)$"
_ARCHIVE_REF = "$Format:%h$"


def _git(*args: str) -> str:
    """Run one git command in the repo, or return "" if it cannot answer.

    Every git path in this module is best-effort by design: an image has no
    `.git`, an exported tarball has no git at all, and neither is an error —
    they just mean the answer has to come from the environment instead.
    """
    try:
        out = subprocess.run(
            ("git",) + args,
            cwd=str(_ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=2, check=True,
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


def _substituted(placeholder: str) -> str:
    """What `git archive` put in a placeholder, or "" if it is still one.

    In a checkout the constants above hold their own literal text; only in an
    archive do they hold an answer. A leading "$" tells the two apart — no
    substituted value starts with one — and testing for that avoids spelling
    the placeholder syntax out here, which `git archive` would substitute in
    turn.
    """
    value = placeholder.strip()
    return "" if value.startswith("$") else value


def _version_from_archive() -> str:
    """The version an archive was packed at, or "" outside one.

    `git describe` answers `v2.0.0-alpha.2` on the tag and
    `v2.0.0-alpha.2-3-gacacfe3` past it. Read from the left that is the
    ambiguous parse `_version_from_git` avoids by asking two questions — but
    an archive leaves only this one string, and from the *right* it is not
    ambiguous at all: the suffix is always `-<distance>-g<commit>`, and a
    string without one is the tag entire.

    No `dirty` marker can appear here. An archive is packed from a commit,
    never from a working tree.
    """
    described = _substituted(_ARCHIVE_DESCRIBE)
    if not described:
        return ""

    parts = described.rsplit("-", 2)
    if len(parts) == 3 and parts[1].isdigit() and parts[2].startswith("g"):
        tag, distance, commit = parts
        local = f"+{distance}.{commit}"
    else:
        tag, local = described, ""

    version = tag[1:] if tag.startswith("v") else tag
    return version + local


def _version() -> str:
    """The version, from the environment, the tag or the archive it was
    packed into — "unknown" if none of them can answer.

    There is deliberately **no hard-coded fallback number**. A constant here
    would be a second source of truth, able to drift from the tag — exactly
    the failure this module exists to remove. All three cases that occur in
    practice are covered: in a container the environment answers, in a
    checkout git does, and in a source archive the value `git archive` left
    behind does.
    """
    env = os.environ.get("STARCUBES_VERSION", "").strip()
    if env:
        return env
    if (_ROOT / ".git").exists():
        version = _version_from_git()
        if version:
            return version
    return _version_from_archive() or "unknown"


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
    if (_ROOT / ".git").exists():
        ref = _git("rev-parse", "--short", "HEAD")
        if ref:
            return ref
    return _substituted(_ARCHIVE_REF)


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


def build_info() -> "dict[str, object]":
    """Everything the About dialog names about this build. Computed once,
    at import — none of it can change while the process runs."""
    version = _version()
    return {
        "version": version,
        "is_prerelease": _is_prerelease(version),
        "build_ref": _build_ref(),
        # No archive fallback: this is a *build* date, and an archive was
        # not built. The commit's date is a different claim, and putting it
        # under this name would say something untrue quietly.
        "build_date": os.environ.get("STARCUBES_BUILD_DATE", "").strip(),
        "timeseries_version": _timeseries_version(),
    }


BUILD_INFO = build_info()
__version__ = BUILD_INFO["version"]
