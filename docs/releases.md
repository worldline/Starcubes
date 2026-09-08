# Releases and versioning

Where the version number comes from, what it promises, and the steps that
turn a commit into a release. Read before cutting a release, editing
`docker/build.sh`, or changing what the About dialog reports.

## The tag is the version

**There is no version number stored anywhere in the source tree.**
`starcubes/version.py` derives it from `git describe` at import, so a tag and
a version cannot disagree and no release can forget to bump anything.

| Repository state | Version |
|------------------|---------|
| exactly on the tag, clean tree | `2.0.0-alpha.1` |
| three commits past it | `2.0.0-alpha.1+3.g1ae985c` |
| …plus uncommitted changes | `2.0.0-alpha.1+3.g1ae985c.dirty` |
| no release tag, no git, no env var | `unknown` |

Everything after the `+` is SemVer build metadata: it says *which build*,
while the part in front of it stays exactly the tag — the release identity.
So a working copy always names a version, and only a build made on a clean
tagged commit names a bare one.

### Two questions, not one parse

`git describe --tags` answers `v2.0.0-alpha.1-3-g1ae985c`, and that string
must not be split on hyphens: the pre-release part contains hyphens of its
own. `version.py` therefore asks two narrow questions instead —
`git describe --tags --abbrev=0 --match 'v[0-9]*'` for the tag alone, and
`git rev-list --count <tag>..HEAD` for the distance. Nothing ambiguous is
left to parse.

The `--match 'v[0-9]*'` is load-bearing. Without it, the first unrelated tag
someone parks on a commit silently becomes the version number.

### No fallback constant

When neither the environment nor git can answer, the version is `unknown` —
deliberately, rather than some baseline number written into the file. A
constant would be a second source of truth able to drift from the tag, which
is the exact failure this design removes. Both cases that occur in practice
are covered: in a container the environment answers, in a checkout git does.

### What is *not* kept in sync

`package.json` keeps `"version": "0.0.0"` forever. It is a private manifest
whose only job is to vendor `@hgruber/timeseries.js`, and any edit to its
version field has to be mirrored into `package-lock.json` — get that wrong
and the Dockerfile's `npm ci` aborts on the mismatch. A number that means
nothing is not worth that failure mode.

## How the version reaches a container

Nothing inside the image can run `git describe`: `.dockerignore` keeps `.git`
out of the build context on purpose. So `docker/build.sh` resolves the
version *outside* and passes it in, along with the commit and the build
timestamp, as build args. The Dockerfile's "Build identity" block parks all
three in the environment, and `version.py` reads them back — the environment
always wins over the git lookup.

`build.sh` resolves the version by *running* `version.py`:

```
version=$(python3 -c 'from starcubes.version import __version__; print(__version__)')
```

It does not reimplement `git describe` in shell. One definition of what a
version is, and that module is stdlib-only and Django-free precisely so this
call works with nothing installed and no DSN set.

That `python3` is the build host's, not the image's. A build host is old — ours
runs 3.6 — so `version.py` is held to **Python 3.6 syntax and API**: no
`from __future__ import annotations` (3.7), no builtin generics in an
annotation that gets evaluated, no `subprocess.run(capture_output=…, text=…)`
(3.7). It is the only file in the tree under that constraint; everything else
runs under the image's 3.12. To check it after an edit:

```
podman run --rm -v "$PWD":/w:z -w /w python:3.6-slim \
    python -c 'from starcubes.version import build_info; print(build_info())'
```

**A build that passes no version is refused.** The Dockerfile checks
`STARCUBES_VERSION` and stops if it is empty, because an image that cannot
name its own version is the one failure this whole design exists to prevent —
and a bare `podman build` used to produce exactly that, quietly. Empty means
"you forgot"; building deliberately without git is a matter of saying so:

```
podman build --build-arg STARCUBES_VERSION=unknown ...
```

The check lives at the very end of the Dockerfile, and that placement is
deliberate. A `RUN` that reads an ARG takes its value into its cache key, and
the version changes with every commit — in front of `npm ci` or
`pip install`, the guard would rebuild the world each time. Behind them it
sits in front of nothing but `LABEL`, `EXPOSE` and `ENTRYPOINT`. The cost is
that the build fails late rather than early, which is a fair trade for a
failure that only occurs when someone bypasses `build.sh`.

**Container tags may not contain `+`,** which every version off a non-tagged
commit has. `build.sh` swaps it for `_` in the image tag only
(`starcubes:2.0.0-alpha.1_3.g1ae985c`); the OCI label keeps the real version,
`+` and all. It also tags `starcubes:latest`: with no registry in play,
`:latest` simply means "the one I built last", which is what the README
quickstart runs.

## How the version reaches a source archive

`git archive --format=zip HEAD -o starcubes.zip` hands a test environment the
source with no `.git` in it, and unlike a container build nothing passes a
version in. Left alone the About dialog there says `unknown`.

`.gitattributes` closes that:

```
starcubes/version.py export-subst
```

`git archive` rewrites two placeholder constants in that file as it packs,
filling in `git describe` and the short commit. The command above needs no
flag and no wrapper — it is already correct, and that is the point: a
handover path you have to remember is one that gets forgotten.

In a checkout the placeholders keep their own literal text. `version.py`
recognises that (no substituted value starts with `$`) and ignores them, so
the environment → git order above is untouched.

Two things an archive cannot report:

- **No build date.** Nothing built it. The dialog leaves the row empty rather
  than passing the commit's date off as a build time.
- **No `dirty`.** An archive is packed from a commit, so uncommitted edits in
  the tree it came from never travel with it.

And if no `v*` tag is reachable from the packed commit — a shallow clone, or
a commit older than the first tag — the version is `unknown` again. That is
the honest answer rather than a failure: there is no tag to name.

## What the number promises

The audience for a starcubes version is the **operator**, not a library
consumer, so the public surface is what an upgrade can break for them:

- container environment variables
- `manage.py` commands and their flags
- the `starcubes_meta` schema and how it migrates
- the profile YAML format under `profiles/`
- the JSON endpoints (`plot.json`, `drill.json`, `txn.json`, `api/*`)

Read that way:

| Bump | Means | Upgrade cost for the operator |
|------|-------|-------------------------------|
| **MAJOR** (`3.0.0`) | An upgrade needs a hand: a manual migration, a renamed or removed env var, a `manage.py` flag that is gone, a profile that has to be rewritten. | Read the changelog and do the work before restarting. |
| **MINOR** (`2.1.0`) | New capability. Schema changes are additive and idempotent, so they apply themselves at start. | Pull the image, restart. |
| **PATCH** (`2.0.1`) | Bug fixes and polish. No new capability, no schema change, no config change. | Pull the image, restart. |

**Downgrades are not covered by any of these.** Once a minor release has
applied its migrations, the previous image is not guaranteed to read the
schema it finds. Rolling back means restoring the database alongside the
image — plan a major upgrade with a backup, not with a rollback.

## Pre-releases

A pre-release is a tag like any other; SemVer's ordering does the rest:

```
2.0.0-alpha.1  <  2.0.0-beta.1  <  2.0.0-rc.1  <  2.0.0
```

The current tag is **`v2.0.0-alpha.5`**: the 2.0.0 feature set is in place,
but the schema, the env vars and the profile format may still move before
2.0.0 is final. Moving on is just another tag — `v2.0.0-alpha.2` for another
alpha, `v2.0.0-beta.1` to promote it, `v2.0.0` to release. Nothing in the
tree changes for any of them.

The About dialog marks any such version with a **pre-release** pill, so an
operator can see at a glance that they are not running a final release.

## Cutting a release

The tag comes *before* the build — the build reads the version from it.
There is no bump step and no release commit; the release is a tag on the
commit that is already there.

1. **Decide the number.** Walk the changes since the last tag
   (`git log --oneline v2.0.0-alpha.1..HEAD`) against the table above. The
   most invasive change in the range picks the bump.
2. **Write the changelog.** In `CHANGELOG.md`, turn the `## [Unreleased]`
   heading into `## [2.0.0-beta.1] — YYYY-MM-DD` and make sure every entry is
   something an operator would want to know. Add a fresh `## [Unreleased]`
   above it.
3. **Commit the changelog, then tag it.**

   ```
   git commit -am "Changelog for 2.0.0-beta.1"
   git tag -a v2.0.0-beta.1 -m "starcubes 2.0.0-beta.1"
   ```

   Tags carry the `v`; image tags do not (`starcubes:2.0.0-beta.1`).
4. **Check the tree is clean, then build.** A dirty tree lands in the version
   string as `.dirty`, and a release must not carry it:

   ```
   git status --porcelain      # must print nothing
   ./docker/build.sh
   ```

   Untracked files count as dirty, and that is the right call rather than an
   oversight: the Dockerfile's `COPY . /app/` puts them in the image too, so
   an image built over them is genuinely not the tagged commit.

   `build.sh` prints the version and commit it is building before it starts,
   and warns if the version is `unknown` or carries a `+` — either means it
   is not building a release.
5. **Verify the image knows what it is.** The label must be the bare tag,
   with no `+` part:

   ```
   podman inspect starcubes:2.0.0-beta.1 \
     --format '{{index .Config.Labels "org.opencontainers.image.version"}}'
   ```
6. **Smoke-test the running container.** Start it, open the menu, and check
   that **About starcubes** names the version and the same commit. That
   single dialog exercises the whole chain: tag → `version.py` → build args →
   image → context processor.

## Which version is that container running?

Three answers, in order of how little access they need:

- **From the GUI:** menu → **About starcubes**. Names the version, whether it
  is a pre-release, the commit, the build time, and the Python / Django /
  timeseries.js versions underneath.
- **From the image, without running it:**
  `podman inspect starcubes:latest --format '{{json .Config.Labels}}'` —
  the OCI labels carry version, revision and creation time.
- **From inside a running container:**
  `podman exec <name> python -c "from starcubes.version import BUILD_INFO; print(BUILD_INFO)"`
