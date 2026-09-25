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

## Two build branches

There are two Dockerfiles, and which one gets built is one environment
variable.

`Dockerfile` at the repo root is the original and the default: Debian slim
off Docker Hub, `apt-get` for nginx and the compiler, the Python packages
copied out of `/usr/local`. It is what a checkout builds, what
`.github/workflows/release.yml` builds, what the README quickstart runs —
**and what `.gitlab-ci.yml` builds too.** Nothing about it changed when the
second branch arrived.

`docker/Dockerfile.ubi` builds the same image on **UBI 10** —
`docker-reg/wl-u10-python312` by default, `dnf` instead of `apt-get`, and the
Python environment in a venv at `/opt/venv` rather than copied out of
`/usr/local`, because that path is a property of the official `python:*`
images and not of an RPM python.

**It is not in use anywhere today, and that is worth saying plainly.** It
was written for the assumption that the internal GitLab instance cannot
reach Docker Hub. Measured, that assumption was wrong in both directions: a
job pod there reaches Docker Hub, npm and PyPI perfectly well, while
`cdn.redhat.com` does not answer — so the UBI file could never run its own
`dnf` on the very instance it was written for. It stays in the tree as the
branch for an environment that really is cut off from Docker Hub but has a
UBI mirror, and it has been built and smoke-tested against a public UBI 10
base. Until such an environment turns up, it is a spare, not a road.

The two share everything that is not distro-bound: `docker/entrypoint.sh`,
`docker/nginx.conf`, `docker/uwsgi.ini`, the runtime `ENV` blocks, the
build-identity guard, the OCI labels and every `STARCUBES_*` runtime
variable. An operator cannot tell the resulting containers apart except by
`podman inspect`. **That sharing is by copy, not by include** — a Dockerfile
has no way to include another — so a change to any of the shared parts
belongs in both files on the same commit. Nothing checks it.

Four environment variables steer `build.sh`, and every one of them defaults
to the original behaviour:

| Variable | Default | What it does |
|---|---|---|
| `STARCUBES_DOCKERFILE` | `Dockerfile` | Which Dockerfile to build. `.gitlab-ci.yml` sets it to `docker/Dockerfile.ubi`. |
| `STARCUBES_BASE_IMAGE` | *(unset)* | Passed through as the `BASE_IMAGE` build arg, which only `docker/Dockerfile.ubi` declares. Unset, that file uses the internal `wl-u10-python312`; set it to `registry.access.redhat.com/ubi10/ubi:latest` to build the internal branch from outside the Worldline network — same dnf world, no login. |
| `STARCUBES_NPM_REGISTRY` | *(unset)* | An npm mirror for the `jsbuilder` stage. Empty is the public registry. |
| `STARCUBES_PIP_INDEX_URL` | *(unset)* | A PyPI mirror for the builder stage. Empty is the public index. |

These are **build**-time variables, which is why they are not in the
environment variable reference in
[`deployment-config.md`](deployment-config.md): that table lists what a
*running* container reads, with a checkout default and an image default, and
neither exists for these. Nothing in a built image remembers them.

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

### Two versions on one database

Several containers may share one `starcubes_meta`, and a rolling update means
the old image keeps running for a while against a schema the new image has
already migrated. That is only safe while every migration a minor release
ships is **expand-only**:

- add tables, columns (nullable or with a default), indexes, views, and
  values to a `CHECK` list — never remove a value from one;
- no rename and no drop in the same release that stops using the object.
  Stop using it in one release, drop it in a later one;
- no change to what an existing column *means*.

The old image then finds nothing missing and nothing reinterpreted. The
other direction needs no rule: the new image migrates at start, and an ETL
load that finds the schema behind its code refuses to run
(`core/preflight.py`, check `meta_migrations`). A migration that cannot be
written this way is a **major** change, whatever else the release contains.

Concurrent starts are serialised by `manage.py bootstrap` itself (an advisory
lock, see `core/pglock.py`); nothing needs to be staggered by hand.

## Pre-releases

A pre-release is a tag like any other; SemVer's ordering does the rest:

```
2.0.0-alpha.1  <  2.0.0-beta.1  <  2.0.0-rc.1  <  2.0.0
```

The current tag is **`v2.0.0-beta.3`**: the 2.0.0 feature set is in place
and the schema, the environment variables and the profile format are
frozen for 2.0.0 — what remains before final is polish and bug fixes.
beta.2 broke that freeze once, for running several containers on one
database (migrations 0042–0046, the log variables replaced, saved profiles
moved into the database); its changelog names every step an operator has
to take. beta.3 kept it — no schema change, no environment variable
moved, no `manage.py` flag touched. Moving on is just another tag —
`v2.0.0-rc.1` for the release candidate, `v2.0.0` to release. Nothing in
the tree changes for any of them.

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

## Releasing from a remote

Two remotes run the procedure above automatically, and both do the same
tag-side work: build the image through `docker/build.sh` — the one supported
route, so all three build args the Dockerfile demands are set — push it,
pack a source zip from the tag with `git archive`, and publish a release
whose body is that version's `CHANGELOG.md` section.

Both pipelines carry the same two guards, and both fail loudly rather than
publish something half-named: a version that does not resolve, or resolves
to something other than the pushed tag, and a `CHANGELOG.md` without a
`## [<version>]` section for it — a release without its changelog entry is
exactly what step 2 above exists to prevent. The changelog guard runs
**before** the build, so a missing section costs a minute rather than a full
image build.

The steps a tag push cannot do stay human: deciding the number (step 1),
writing the changelog (step 2), and the smoke test (step 6) — the pipeline
builds and publishes, it does not judge.

### GitHub — `.github/workflows/release.yml`

The remote at `git@github.com:worldline/Starcubes.git`. Pushing a `v*` tag
fires the workflow, which

- pushes the image to `ghcr.io/worldline/starcubes`. `:latest` is pushed
  only when the version is **not** a pre-release, so `:latest` never points
  at a beta; every tag gets its own version tag either way.
- packs a source zip from the tag, so the zip names its version through the
  `export-subst` placeholders and needs no `.git`.
- publishes the release with the changelog section as the body, the zip as
  its **asset**, and a **pre-release** marker on anything with a hyphen
  before the `+`.

Pulling the image needs a login for a private package: `podman login
ghcr.io` with a classic personal access token (`read:packages`), or the
workflow token from within this repository's own Actions. If the first push
is refused, the organisation's package creation policy has to be enabled
first (Organization → Settings → Packages).

### GitLab — `.gitlab-ci.yml`

The internal instance. A `v*` tag pushed there runs the same procedure in
two jobs — `build` (version guard, changelog guard, image, zip) and
`publish` (the release itself). Five things differ from GitHub, and none of
them is an oversight:

- **A different runner, and therefore a job image.** The instance answers
  with a **Kubernetes shared runner** — `k8s-S Shared Runner on GI/DC*`, via
  the `tags:` line. Every job runs in a pod, so there is no host to install
  anything on: each job names `$STARCUBES_JOB_IMAGE`, Docker Hub's full
  `python:3.12`, which already carries git and curl, and `apt`-installs
  buildah and netavark into it. Full, not `-slim`, and not the internal
  `wl-u10-python312`: **that image carries no package manager at all**, and
  UBI's repositories are unreachable from a job pod anyway. If a job reports
  a non-root uid, that is the runner's pod security context and not
  something this file can set — it says so rather than failing on a
  permission error three lines later.
- **The image is built with buildah, unprivileged.** A shared runner does
  not hand out `privileged = true`, so docker-in-docker is out — and the
  dind image is not in the registry either. buildah with the **vfs** storage
  driver (set in `/etc/containers/storage.conf` during setup, so `tag` and
  `push` look into the same store) and `BUILDAH_ISOLATION=chroot` needs
  neither privileges nor user namespaces. The dind arrangement had a second,
  quieter defect on this executor: `DOCKER_HOST: tcp://docker:2376` cannot
  resolve, because a Kubernetes executor puts services into the job's own
  pod network, reachable as `localhost`, with no alias.
- **The same Dockerfile as everywhere else.** `STARCUBES_DOCKERFILE` is
  empty here on purpose, so `build.sh` builds the repo-root `Dockerfile` —
  see [Two build branches](#two-build-branches). `docker/Dockerfile.ubi` was
  written for the assumption that this instance cannot reach Docker Hub, and
  measurement overturned that assumption in both directions (below).
- **A push target that is not GitLab's.** This GitLab has **no container
  registry**, so `$CI_REGISTRY_IMAGE` is empty and a push against it pings
  `https:///v2/`. `STARCUBES_IMAGE` names the Harbor this environment
  already pulls from, and `STARCUBES_REGISTRY_USER` /
  `STARCUBES_REGISTRY_PASSWORD` are its credentials. The job's own
  `$CI_REGISTRY_*` are **not** a fallback for it: GitLab defines them even
  with no registry configured, so an unconditional fallback is never empty
  and quietly throws a job token at a foreign registry.
- **The release is created through the API.** The `release:` keyword is
  implemented by the `release-cli` image, which this registry does not carry
  and this executor could not run anyway. The `publish` job `POST`s to
  `/projects/:id/releases` with the job token instead: same tag, same name,
  same description, same asset link. The body is assembled with `python3`
  and `json.dumps` rather than `printf` — a changelog section is full of
  backticks, quotes and newlines, and hand-rolled JSON in `sh` breaks on the
  first of them.

- **The zip is a link, not an attachment.** GitLab releases carry asset
  *links*; they do not host uploads. The job publishes the zip to the
  project's **generic package registry** under
  `…/packages/generic/starcubes/<version>/starcubes-<version>.zip` and links
  the release to it. Same file, same name, one hop further away.
- **There is no pre-release flag.** GitLab releases have none, so a
  pre-release says so in its **name** (`starcubes 2.0.0-rc.1 (pre-release)`).
  The distinction that matters operationally is unaffected: `:latest` still
  moves only for a final release, and the About dialog still draws its
  pre-release pill, because that comes from `version.py`.
- **`GIT_DEPTH: 0` is load-bearing.** GitLab clones shallow by default and
  `version.py` describes from the tag history. Without it every release
  stops at the version guard, reporting a version problem that is really a
  checkout setting. The job also marks the checkout `safe.directory`, or git
  refuses to read it from inside the build image — quietly, since every git
  call in `version.py` is best-effort by design.

**Before you tag anything, run `doctor`.** *Build → Pipelines → Run
pipeline*, on any ref. It is the only job a hand-started pipeline runs, it
publishes nothing, and it answers — in one pass, continuing past each
failure — the six questions this pipeline cannot answer for itself:

1. can this pod build at all — buildah builds a throwaway image off the
   real base, with **two** `-t`, because `docker/build.sh` passes two and an
   engine that accepts only one would fail the release rather than this
   check;
2. does `$STARCUBES_BASE_IMAGE` pull — the registry lists repositories
   without tags, so `:latest` is an assumption until something proves it;
3. can `dnf` inside that image provide nodejs, npm, python3-devel, gcc,
   nginx, libaio and pcre2-devel — a hardened image with no repositories
   configured fails here rather than three minutes into a release;
4. are npm and PyPI (or the mirrors in `STARCUBES_NPM_REGISTRY` /
   `STARCUBES_PIP_INDEX_URL`) reachable;
5. do the job credentials open the container registry, the package registry
   and the Releases API;
6. does `version.py` resolve in this clone.

It exists because the alternative is to push a `v*` tag and read the
failure — and a failed tag has to be deleted and set again on the fixed
commit, since a queued job always runs the file as of the commit its tag
points at. The job costs a click.

**Where the image goes is one variable.** Nothing in the tree names a
registry — the image's identity is its build args and OCI labels, not its
name — so the pipeline pushes to `$STARCUBES_IMAGE`, defaulting to this
project's own registry. Pointing it elsewhere is a change under *Settings →
CI/CD → Variables*, not a commit; a registry other than the project's own
needs its own credentials at the login step, since `$CI_REGISTRY_USER` and
`$CI_REGISTRY_PASSWORD` are only good for this one.

Pulling needs `docker login $CI_REGISTRY` with a deploy token or a personal
access token carrying `read_registry`.

**What this environment actually is**, measured on 2026-09-25 rather than
assumed — the pipeline prints this map in every job, because three rounds
were spent on beliefs that one measurement overturned:

| From a job pod | |
|---|---|
| `deb.debian.org`, `registry-1.docker.io`, `registry.npmjs.org`, `pypi.org` | reachable |
| `artifactory.core.emea.giservices.io`, the GitLab itself | reachable |
| `cdn.redhat.com`, `dl-cdn.alpinelinux.org` | **unreachable** |

So a job pod's egress is an **allowlist, not a block** — the first failure,
`apk add` against Alpine's CDN, looked like the latter. Image pulls are a
separate path again: the runner rewrites them to an Artifactory mirror, so
`image:` resolves even for things a job could not fetch itself.

Two consequences. The repo-root `Dockerfile` has every source it needs
(`npm ci`, `pip install`, its Debian bases), so it is what gets built.
And `docker/Dockerfile.ubi` could never work here at all: it installs with
`dnf` from the one CDN that does not answer.

The instance has to be able to build, and that means four things the
pipeline cannot supply itself: a **runner** whose tags match the `tags:`
line, **a pod that may run as root** (for `apt`) and **may build
unprivileged** (buildah with vfs and chroot isolation), a **push target
with credentials**, and those package sources staying reachable. No dind
image and no `release-cli` image are needed; neither exists in that
registry, which is why the file is shaped the way it is. `doctor` checks
every one of these in a single run, and the guards check the cheap ones
before the build rather than after it — a missing variable that costs a
full image build before it speaks up is a guard in the wrong place.

**Where a runner comes from.** A self-hosted instance has exactly the
runners its operator has put there — GitLab ships none. There is no
equivalent of GitHub's `runs-on: ubuntu-latest`, which quietly means "take
one of the machines github.com runs for you"; that is a hosted-service
convenience, not a property of either product. GitLab CI works the same way
underneath, so if the instance offers no runner, nothing in this file can
make a release happen.

**"This job is stuck because the project doesn't have any runners online
assigned to it."** The pipeline is fine — the jobs are queued and nothing
will pick them up. Two causes, in the order worth checking:

1. **No runner reaches the project at all.** *Settings → CI/CD → Runners*
   lists what the project can see, in three tiers: instance, group and
   project. Instance-wide runners often exist but have to be enabled for
   the project. If all three are empty, the operator of the instance is the
   only one who can change that.
2. **The release tag is protected and the runner is not.** This is the one
   that catches release pipelines specifically: `v*` is a natural thing to
   protect, and a pipeline on a protected ref is only offered to runners
   marked *protected*. The job then waits forever while the same runner
   happily builds every branch. Either mark the runner protected, or stop
   protecting the release tags.

Once a runner answers, a stuck job can be retried — **unless
`.gitlab-ci.yml` itself changed**. A pipeline always runs the file as of the
commit the tag points at, so a fix to the pipeline never reaches a job that
is already queued. Delete the tag and set it again on the new commit.

Which runner it is decides the shape of `.gitlab-ci.yml`. It is written for
the **Kubernetes shared runner** this instance answers with: a `default:`
`image:` from the internal registry, tooling from `dnf`, no `services:`, no
`DOCKER_*` variables, and `CONTAINER_ENGINE=buildah` building unprivileged.
A shell runner with podman would want the opposite — no `image:`, no `dnf`
step, tools on the host — and a docker executor would want a `docker:dind`
service plus `privileged = true`, which a shared runner does not give and
whose image is not in the registry anyway. Switching is therefore a runner
decision first and an edit second, and `doctor` is how you find out which
one you have.

### Two remotes, two builds

Both remotes are pushed by hand and both pipelines run:

```
git push origin --tags
git push gitlab --tags
```

The two images are built separately, so they carry the same version, the
same commit and different digests — `STARCUBES_BUILD_DATE` alone guarantees
that. That is the price of publishing in two places, and it is worth knowing
before someone compares digests and concludes something is wrong. The
version and the commit are what identify a release; the digest identifies a
build of it.
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
