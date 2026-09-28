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

There are two Dockerfiles, and they are built by different things.

`Dockerfile` at the repo root is the original: Debian slim off Docker Hub,
`apt-get` for nginx and the compiler, the Python packages copied out of
`/usr/local`. **It is what the official build uses** — a checkout, the README
quickstart, and `.github/workflows/release.yml`. Leave it alone unless a
change is meant for everyone.

`docker/Dockerfile.ubi` builds the same image on the internal **UBI 10** base
out of `oci.registry.csms.worldline-solutions.com/docker-reg/`, with
`microdnf` instead of `apt-get`, the Python environment in a venv at
`/opt/venv` rather than copied out of `/usr/local` (that path belongs to the
official `python:*` images, not to an RPM python), and nodejs from the base
rather than from a node image. **The GitLab release builds this one**, through
the OCI Builder — see "GitLab — `.gitlab-ci.yml`" below.

### Why the base is written into the file, not passed in

`docker/Dockerfile.ubi` names its base image literally on every stage
directive, and there is no build argument for it. That is forced, and the
reason is worth knowing before anyone improves it back into an argument:

The OCI Builder's security linter reads the Dockerfile **statically** and
rejects any stage whose source image is not one of a handful of internal
registries. An argument — even one with a sensible default — hides the host
behind a variable, and the linter reports two errors per stage: "Sources image
should come from …" and "Invalid parameters for command". Measured on
2026-09-28, pipeline 19865629. There is no way to have both the indirection
and the build.

The cost is that this branch is buildable **only inside the corporate
network**, and that `STARCUBES_BASE_IMAGE` — which used to point it at a
public UBI base for a smoke test from outside — went with the argument it fed.
That was judged cheap: the official build is GitHub's, and no other GitLab has
to build this file.

### Why the base has no Python

The internal image that ships Python 3.12, `docker-reg/wl-u10-python312`, has
**no package manager at all** — measured on 2026-09-28 in pipeline 19865870,
where the first package step died with `dnf: command not found`. Nothing can
be added to it afterwards, and nginx and a C compiler for uwsgi have to come
from somewhere. So this branch builds on `docker-reg/wl-ubi10`, which carries
UBI 10 with `microdnf` and no interpreter, and installs what each stage needs.
On RHEL 10 `python3` *is* 3.12, so no package line needs a version suffix —
and `requirements.txt` stays exactly what the GitHub build reads, which is the
point. Reaching for an older Python instead would have meant Django 4.2 and
pandas 2.2, both below what this application pins, and the first of them out
of support.

The two files share everything that is not distro-bound:
`docker/entrypoint.sh`, `docker/nginx.conf`, `docker/uwsgi.ini`, the runtime
`ENV` blocks, the build-identity guard, the OCI labels and every `STARCUBES_*`
runtime variable. An operator cannot tell the resulting containers apart
except by `podman inspect`. **That sharing is by copy, not by include** — a
Dockerfile has no way to include another — so a change to any of the shared
parts belongs in both files on the same commit. Nothing checks it.

The `wl.maintainer` label is the one deliberate exception, and it lives only
in the UBI file: the OCI Builder lints that file a second time at promotion,
in a **blocking** pass, and a missing label there fails a release that built
cleanly.

Three environment variables steer `build.sh`, and every one of them defaults
to the original behaviour:

| Variable | Default | What it does |
|---|---|---|
| `STARCUBES_DOCKERFILE` | `Dockerfile` | Which Dockerfile a *checkout* builds. The GitLab pipeline no longer uses this script — the OCI Builder builds `docker/Dockerfile.ubi` itself, and `DOCKERFILE` in `.gitlab-ci.yml` is what tells it so. |
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

The current tag is **`v2.0.0-beta.4`**: the 2.0.0 feature set is in place
and the schema, the environment variables and the profile format are
frozen for 2.0.0 — what remains before final is polish and bug fixes.
beta.2 broke that freeze once, for running several containers on one
database (migrations 0042–0046, the log variables replaced, saved profiles
moved into the database); its changelog names every step an operator has
to take. beta.3 and beta.4 kept it — no schema change, no environment
variable moved, no `manage.py` flag touched. beta.4 changes one thing
outside the freeze and names it in its changelog: the GitLab image is
built by the OCI Builder now, and an operator pulls it from a new path.
Moving on is just another tag — `v2.0.0-rc.1` for the release candidate,
`v2.0.0` to release. Nothing in the tree changes for any of them.

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
tag-side work: publish an image, pack a source zip from the tag with
`git archive`, and cut a release whose body is that version's `CHANGELOG.md`
section. How the image is produced is where they part: GitHub builds it here,
through `docker/build.sh` — the one route that sets all three build args the
root Dockerfile demands — while GitLab hands the job to the OCI Builder.

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

The internal instance. A `v*` tag pushed there publishes the same three
things GitHub does — image, source zip, release — but **the image is built by
someone else**. The OCI Builder (`paas/oci-builder/pipelines/builder`) is a
central service: it clones this repository, builds `docker/Dockerfile.ubi`
with buildah on its own infrastructure, pushes to Harbor with credentials of
its own, and signs the result with cosign. This project holds no registry
password any more, which is the whole reason for the arrangement.

**The jobs, in order.** A trigger job cannot have a `script:`, which is why
the work this file used to do in two jobs now spans six:

| Job | Stage | What it does |
|---|---|---|
| `prepare` | check | version guard, changelog guard, source zip, and the handover the later jobs read |
| `build-image` | build | triggers `ACTION=build` |
| `promote` | release | triggers `ACTION=promote` — staging to production |
| `latest` | release | triggers `ACTION=tag` with `NEW_TAG=latest`, final releases only |
| `publish` | release | the release itself, through the Releases API |
| `doctor` | check | the hand-checked questions, below |

**What is proven and what is not.** `build` has run end to end — 2026-09-28,
pipeline 19872671: the internal base pulls, the packages install, npm and PyPI
answer, the build args arrive, the image is pushed and signed. **`promote` and
`tag` have never been run from here.** They are written from the builder's own
sources (`build-pipelines/includes/promote_image.yaml`,
`includes/manage_tags.yaml`, and the `libs/` scripts those call), so the
variable names are read rather than guessed — but the first real tag is their
test, and a failure there stops the release before `publish`.

### The trigger contract, and its two traps

Everything the builder knows comes from `variables:` on the trigger job. Two
of them are worth naming before anyone edits them:

- **`DOCKERFILE` and `DOCKERFILE_DIR`.** The file is named in full
  (`docker/Dockerfile.ubi`) and `DOCKERFILE_DIR` is **empty**, because the
  builder uses that directory as the build **context** as well as the
  directory: `docker` there would make `COPY . /app/` copy only that
  subdirectory. The image would build, and be wrong.
- **`BUILDER_BUILD_ARGS`** is the builder's syntax, not Docker's: one string
  of `buildarg-KEY=VALUE` pairs joined with `&`. A pair whose value is **empty
  is silently dropped** — it does not fail the build. An unset variable would
  therefore not report itself; it would strip a build argument and leave the
  Dockerfile's identity guard to complain about a missing version, which is
  the wrong diagnosis. That is why `prepare` composes the whole string in one
  piece, and why the values it feeds are known to be set.

`GIT_BRANCH` takes the **tag**, not a branch name — the builder runs `git
checkout $GIT_BRANCH`, and git takes a tag there as happily as a branch.
Building the tag is what makes the image the tagged commit.

One more variable is worth knowing about: **`FORCE`**. The builder refuses to
build a tag that already exists on the registry, and `overwritable_tags` in
its configuration is a deliberately short list (`dev`, `devel`, `develop`,
`latest`, `*-snapshot`) so that the flag does not end up set everywhere — its
own comment says why: *"without it, users will always set this flag, leaving
the security disabled for all builds."* So the pipeline passes `FORCE` from a
project variable, `OCI_FORCE_OVERWRITE`, **`False` unless a project variable of
that name overrides it**. That default lives in the pipeline's own variables
and has to live *somewhere*, because "unset" is not an option: the builder
reads `FORCE` with a strict boolean parser that accepts only
`yes`/`1`/`true`/`no`/`0`/`false`, so an empty value is a parse error rather
than an "off", and GitLab expands an undefined variable to the empty string.
Set it to `True`, run, remove it again. Re-cutting a tag whose release was
botched is the case that needs it. An image that is already published under a
release tag is otherwise a thing to look at, not to overwrite.

### Where the image goes, and what an operator has to change

`ACTION=build` leaves the image in the **staging** registry,
`registry-scl-staging.svc.meshcore.net/starcubes/starcubes:<version>`;
`ACTION=promote` copies it to production,
`registry-production.svc.meshcore.net/starcubes/starcubes:<version>` — the same
service this project used to push to directly, under its other name
`oci.registry.csms.worldline-solutions.com`.

**The path changed, and that is the one thing an operator has to act on.** The
repository segment is the object code now (`starcubes`), not `docker-reg`. A
host pulling `…/docker-reg/starcubes:<tag>` has to be pointed at
`…/starcubes/starcubes:<tag>`. Pre-releases are promoted too, deliberately:
before the switch a beta landed in the same Harbor a final release did, and
only `:latest` told them apart.

`:latest` moves only for a final release. That rule is a regex on the tag
rather than the `$PRERELEASE` flag `prepare` computes — not a shortcut, but
because `rules:` are evaluated when the pipeline is created, long before any
dotenv artifact exists.

### What GitLab still does differently from GitHub

- **The release is created through the API.** The `release:` keyword is
  implemented by the `release-cli` image, which this instance does not carry.
  The `publish` job `POST`s to `/projects/:id/releases` with the job token
  instead: same tag, same name, same description, same asset link. The body is
  assembled with `python3` and `json.dumps` rather than `printf` — a changelog
  section is full of backticks, quotes and newlines, and hand-rolled JSON in
  `sh` breaks on the first of them.
- **The zip is a link, not an attachment, and it lives in a job artifact.**
  GitLab releases carry asset *links*; they do not host uploads. The obvious
  home for the file would be the generic package registry, and **this
  instance does not have one** — the endpoint answers `404` while `releases`
  and `jobs/artifacts` both answer (measured 2026-09-28). So `prepare` keeps
  the zip among its artifacts, with a retention of years rather than the
  project default, and the release links to
  `…/-/jobs/artifacts/<tag>/raw/.release/starcubes-<version>.zip?job=prepare`.
  The link is stable for a tag, but it is an artifact: if the retention is
  ever lowered, the link stops resolving.
- **There is no pre-release flag.** GitLab releases have none, so a
  pre-release says so in its **name** (`starcubes 2.0.0-rc.1 (pre-release)`).
  The distinction that matters operationally is unaffected: `:latest` still
  moves only for a final release, and the About dialog still draws its
  pre-release pill, because that comes from `version.py`.
- **`GIT_DEPTH: 0` is load-bearing.** GitLab clones shallow by default and
  `version.py` describes from the tag history. Without it every release stops
  at the version guard, reporting a version problem that is really a checkout
  setting. The two shell jobs also mark the checkout `safe.directory`, or git
  refuses to read it from inside the job image — quietly, since every git call
  in `version.py` is best-effort by design.

### Who pushes the tag decides whether the build happens

**The `build-image` job triggers the OCI Builder as the pipeline's user**, not
as the project: GitLab checks that user's permission on the downstream project
before it creates the downstream pipeline. The job token on its own is not
enough — it cannot even see the builder project, so the API answers `404`.

Two cases, measured on 2026-09-28:

| The pipeline was started by | Result |
|---|---|
| a member of the builder project, from the UI | the trigger works — this is how the pipeline was rehearsed |
| a tag push authenticated with a project access token | the bridge fails with `insufficient_bridge_permissions`, before anything is built |

So a release tag has to be pushed by **a user the builder knows**. The durable
fix is on the builder's side — allowing this project's job token in its
*Settings → CI/CD → Job token permissions* — and it is worth asking for,
because a tag that cannot be pushed by a robot will catch the next person too.
Until then, `doctor` reports the job token as blind to the builder without
failing on it: that is what the answer means, a hint at which of the two cases
you are in rather than a verdict.

### Trying it without cutting a release

`doctor` (*Build → Pipelines → Run pipeline*, on any ref) checks what this
repository can still get wrong on its own — that `version.py` resolves, that
the changelog has an `[Unreleased]` section, and that the Dockerfile is where
`OCI_DOCKERFILE` says it is. It publishes nothing. It also *reports* whether
the trigger project is visible to the job token, without failing on it: that
question has no yes-or-no answer, as the next section explains.

**It can no longer check the build, and that is worth saying plainly.** The
base image, the package installs, npm, PyPI and the build arguments belong to
the OCI Builder, which runs them on its own infrastructure; no job here can
ask about them. The cheap rehearsal is a throwaway branch whose
`.gitlab-ci.yml` triggers `ACTION=build` with `TAG=probe` and an `IMAGE_NAME`
that is not `starcubes`. It is how this pipeline was written, over four runs
on 2026-09-28. A failed `v*` tag, by contrast, has to be deleted and set again
on the fixed commit — a queued job always runs the file as of the commit its
tag points at.

The image's identity is its build args and OCI labels, not its name — and the
builder composes both from the trigger variables, so nothing in this tree
decides them any more.

**What this environment is.** `prepare` and `publish` run on a Kubernetes
shared runner — a pod, with no host to install anything on — so both name
`$STARCUBES_JOB_IMAGE`, Docker Hub's full `python:3.12`, which the runner
pulls through its Artifactory mirror and which already carries git and curl.
Neither job builds anything, so the questions that used to decide the
Dockerfile — whether each package source answers, whether a CDN is reachable
— no longer arise here at all. They belong to the OCI Builder now, on its own
infrastructure, and the answers it needs are the ones measured on 2026-09-28:
the internal base pulls, and microdnf, npm and PyPI all answer from there.

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

The `build-image`, `promote` and `latest` jobs do no work on this instance's own
runners at all: they hand a request to the **OCI Builder's** queue, which is
separate and has its own capacity limit — 25 concurrent builds across the
whole company, distributed over two sites. A `build_image` job that sits in
`pending` up there is waiting for that queue, not for anything in this file.
The queue is also why the trigger uses `strategy: depend`: this pipeline
waits for the downstream to finish, so a failed build fails the release
rather than leaving it silently unbuilt.

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
