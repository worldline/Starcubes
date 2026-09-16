"""The help documentation: which Markdown files the app serves, and how.

starcubes carries its documentation as Markdown in the repo, and the GUI
used to show none of it. This module turns those files into the pages
behind ``/help/`` — the ``.md`` files stay the single source of truth, so
editing one is all it takes for the app to serve the new text.

Framework-agnostic like the rest of ``core/``: no Django import, and the
repo root comes from ``__file__`` (same idiom as ``schema.py``), not from
``settings.BASE_DIR``.

**The registry is curated, not scanned.** Order, grouping and the one-line
blurbs are editorial decisions a directory listing cannot make, and a scan
would sweep in the ``CLAUDE.md`` family, which is agent instrumentation
rather than anything a user of the app wants to read. Most blurbs come
verbatim from the documentation table in ``README.md``.

**Links are rewritten in a postprocessor, not a treeprocessor** — see
``_LinkRewriter``. That is not a style preference; a treeprocessor cannot
see the raw HTML blocks, and ``README.md`` hides its logo in one.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from pathlib import Path

import markdown

REPO_ROOT = Path(__file__).resolve().parent.parent

# The URL prefix the rewritten links point at. Kept here rather than
# reversed from Django's URLconf so this module stays importable without
# Django; `viz/urls.py` must agree, and `check_help_docs` verifies it does.
HELP_URL_PREFIX = "/help/"

# Asset roots `viz.views.deck_asset` already serves, keyed by the repo-relative
# directory a Markdown link would name. The slide deck established these routes
# and their resolve guards; the help pages reuse them rather than growing a
# second file-streaming view. Order matters: longest prefix first.
_ASSET_ROOTS = (
    ("viz/static/viz/", "/viz/static/viz/"),
    ("docs/", "/docs/"),
)


@dataclass(frozen=True)
class HelpDoc:
    """One document in the help section.

    `path` is repo-relative. `plain` marks a file that is not Markdown and
    is shown verbatim in a <pre> — the two licence texts, which exist here
    so that every link in the corpus resolves and so the MIT notice the
    project is obliged to ship is actually reachable from the running app.
    """
    slug: str
    path: str
    title: str
    blurb: str
    group: str
    plain: bool = False

    @property
    def file(self) -> Path:
        return REPO_ROOT / self.path


# Groups in reading order. A newcomer starts at the top and never needs to
# know that "docs/" is a directory.
GROUP_GETTING_STARTED = "Getting started"
GROUP_OPERATING = "Operating starcubes"
GROUP_CONCEPTS = "How it works"
GROUP_DEVELOPMENT = "Development"
GROUP_LEGAL = "Legal"

GROUP_ORDER = (
    GROUP_GETTING_STARTED,
    GROUP_OPERATING,
    GROUP_CONCEPTS,
    GROUP_DEVELOPMENT,
    GROUP_LEGAL,
)

GROUP_BLURBS = {
    GROUP_GETTING_STARTED: "What starcubes is, and how to drive it.",
    GROUP_OPERATING: "Running an installation: loads, alarms, profiles, deployment.",
    GROUP_CONCEPTS: "The design underneath — schema, pipeline, plots.",
    GROUP_DEVELOPMENT: "Deep references for changing the code.",
    GROUP_LEGAL: "Licences, and the notices shipped with the software.",
}

HELP_DOCS = (
    HelpDoc("readme", "README.md", "Overview",
            "What starcubes is, what it looks like, and the five-minute start",
            GROUP_GETTING_STARTED),
    HelpDoc("howto", "HOWTO.md", "User guide",
            "Driving the GUI, and every configuration field explained",
            GROUP_GETTING_STARTED),
    HelpDoc("changelog", "CHANGELOG.md", "What's new",
            "Notable changes per release",
            GROUP_GETTING_STARTED),

    HelpDoc("internals", "docs/internals.md", "Architecture",
            "Component map, repo layout, and the command-line quick start",
            GROUP_OPERATING),
    HelpDoc("etl-automation", "docs/etl-automation.md", "ETL automation",
            "What has to be true around a load, and what still needs an operator",
            GROUP_OPERATING),
    HelpDoc("source-profiles", "docs/source-profiles.md", "Source profiles",
            "The profile format, and reusing a datasource configuration",
            GROUP_OPERATING),
    HelpDoc("alarm", "docs/alarm.md", "Alarms",
            "Alarm evaluation, firing episodes and notification transports",
            GROUP_OPERATING),
    HelpDoc("rollup", "docs/rollup.md", "Pre-aggregation",
            "Rollup tables: how their columns are chosen, and what they cannot serve",
            GROUP_OPERATING),
    HelpDoc("deployment-config", "docs/deployment-config.md", "Deployment",
            "Container, environment variables, SSO",
            GROUP_OPERATING),
    HelpDoc("multi-container", "docs/multi-container.md", "Several containers",
            "One database, many containers: requirements, handover, one log",
            GROUP_OPERATING),
    HelpDoc("releases", "docs/releases.md", "Releases and versioning",
            "What a version bump promises, and the steps that cut a release",
            GROUP_OPERATING),

    HelpDoc("star-schema", "docs/star-schema.md", "Star schema",
            "Domain vocabulary and schema decisions",
            GROUP_CONCEPTS),
    HelpDoc("etl-engine", "docs/etl-engine.md", "ETL engine",
            "The three-stage pipeline in detail",
            GROUP_CONCEPTS),
    HelpDoc("etl-audit", "docs/etl-audit.md", "Audit modes",
            "The three audit modes, and why each exists",
            GROUP_CONCEPTS),
    HelpDoc("chart-rendering", "docs/chart-rendering.md", "Chart rendering",
            "Ladder plot types and partial-bin drawing",
            GROUP_CONCEPTS),
    HelpDoc("bitmap-indexes", "docs/bitmap-indexes.md", "Bitmap indexes",
            "Research note: could Postgres have one, and would a fact table gain",
            GROUP_CONCEPTS),

    HelpDoc("sources-app", "docs/sources-app.md", "The sources GUI",
            "URL map, gotchas and the inline-CRUD behaviour",
            GROUP_DEVELOPMENT),
    HelpDoc("chart-app", "docs/chart-app.md", "The chart UI",
            "The multi-source chart page, end to end",
            GROUP_DEVELOPMENT),
    HelpDoc("backlog", "docs/backlog.md", "Backlog",
            "Known unfinished work",
            GROUP_DEVELOPMENT),

    HelpDoc("license", "LICENSE", "License",
            "The MIT licence starcubes is distributed under",
            GROUP_LEGAL, plain=True),
    HelpDoc("third-party-notices", "THIRD-PARTY-NOTICES", "Third-party notices",
            "Components shipped with starcubes, and their licences",
            GROUP_LEGAL, plain=True),
)

BY_SLUG = {d.slug: d for d in HELP_DOCS}
# Repo-relative path -> slug, for resolving one document's link to another.
BY_PATH = {d.path: d.slug for d in HELP_DOCS}


def grouped_docs():
    """`HELP_DOCS` as [(group, blurb, [doc, ...]), ...] in GROUP_ORDER."""
    return [
        (g, GROUP_BLURBS.get(g, ""), [d for d in HELP_DOCS if d.group == g])
        for g in GROUP_ORDER
    ]


# ── link rewriting ───────────────────────────────────────────────────────────
# Every href/src/srcset in the generated HTML, whether it came from Markdown
# syntax or from a raw HTML block. Attribute values in this corpus are always
# double-quoted; `check_help_docs` would catch a link this misses.
_ATTR_RE = re.compile(r'(?P<head>\b(?:href|src|srcset)\s*=\s*")(?P<value>[^"]*)"')
_ABSOLUTE_RE = re.compile(r'^(?:[a-z][a-z0-9+.-]*:|//|/|#)', re.I)


def _rewrite_url(value: str, source_path: str) -> str | None:
    """Map one relative URL from `source_path` onto its served location.

    Returns None when the URL is absolute, an in-page anchor, or names
    something the help section does not serve — the caller then leaves it
    alone, so an unresolvable link degrades to a dead link on the page
    rather than an exception in the view. `check_help_docs` reports them.
    """
    if not value or _ABSOLUTE_RE.match(value):
        return None

    target, _, fragment = value.partition("#")
    if not target:
        return None

    # Resolve relative to the *linking document*: "alarm.md" means
    # docs/alarm.md inside docs/, and nothing at the repo root.
    base = Path(source_path).parent
    try:
        resolved = (REPO_ROOT / base / target).resolve()
        rel = resolved.relative_to(REPO_ROOT).as_posix()
    except (ValueError, OSError):
        return None

    slug = BY_PATH.get(rel)
    if slug:
        return HELP_URL_PREFIX + slug + (("#" + fragment) if fragment else "")

    for prefix, served in _ASSET_ROOTS:
        if rel.startswith(prefix) and (REPO_ROOT / rel).is_file():
            return served + rel[len(prefix):]

    return None


class _LinkRewriter(markdown.postprocessors.Postprocessor):
    """Point the corpus's relative links at what the app actually serves.

    A postprocessor, deliberately. python-markdown parks raw HTML blocks in
    its `htmlStash` and only substitutes them back at the end, so a
    treeprocessor walking the element tree never sees them — and the logo in
    `README.md` lives in exactly such a block, as `<picture>` with a
    `srcset`. Running over the finished HTML string catches Markdown links
    and raw HTML alike, in one place instead of two.
    """

    def __init__(self, md, source_path: str):
        super().__init__(md)
        self.source_path = source_path

    def run(self, text: str) -> str:
        def sub(m: re.Match) -> str:
            new = _rewrite_url(m.group("value"), self.source_path)
            return m.group(0) if new is None else f'{m.group("head")}{new}"'

        return _ATTR_RE.sub(sub, text)


class _HelpExtension(markdown.extensions.Extension):
    def __init__(self, source_path: str):
        super().__init__()
        self.source_path = source_path

    def extendMarkdown(self, md):
        # After `raw_html` (priority 30) has put the stashed HTML back, or the
        # rewriter would only ever see the placeholders it is here to catch.
        md.postprocessors.register(
            _LinkRewriter(md, self.source_path), "starcubes_links", 10)


_EXTENSIONS = ["tables", "fenced_code", "codehilite", "toc", "attr_list", "sane_lists"]
_EXTENSION_CONFIGS = {
    # No language guessing: only a third of the corpus's fenced blocks name a
    # language, and guessing colours the rest wrong rather than leaving them
    # plain. `use_pygments` needs the stylesheet in viz/static/viz/help.css.
    "codehilite": {"guess_lang": False, "css_class": "codehilite"},
    # `permalink`, not `anchorlink`: anchorlink wraps the whole heading text
    # in an <a>, which makes every heading on the page look like a link.
    # permalink appends one quiet marker instead — the `.headerlink` that
    # help.css keeps invisible until the heading is hovered.
    #
    # Default slugify, deliberately: it already reproduces every hand-written
    # anchor in the corpus (HOWTO.md's table of contents is 36 of them), so a
    # custom one could only break what works.
    "toc": {"permalink": "#", "permalink_title": "Link to this section"},
}


def build_markdown(source_path: str) -> markdown.Markdown:
    """A configured converter for one document. One instance per render —
    `markdown.Markdown` carries per-document state (`toc_tokens`)."""
    return markdown.Markdown(
        extensions=_EXTENSIONS + [_HelpExtension(source_path)],
        extension_configs=_EXTENSION_CONFIGS,
        output_format="html",
    )


# ── rendering, with an mtime cache ───────────────────────────────────────────
# Rendering the corpus costs ~1.2 s (HOWTO.md alone ~300 ms), which is per
# request without this. The cache is process-local — uwsgi runs two workers,
# so a document is rendered at most twice — and keyed on mtime so editing a
# file during development shows up on the next reload with no restart.
_cache: dict[str, tuple[float, "RenderedDoc"]] = {}


@dataclass(frozen=True)
class RenderedDoc:
    doc: HelpDoc
    html: str
    toc: list


# A hand-written contents section, as `HOWTO.md` carries one. It earns its
# place in a file read on GitHub, where there is no other way around 1300
# lines; on a help page the sidebar already lists the same headings, so
# leaving it in means the reader scrolls past a duplicate to reach the text.
# Matched by heading text rather than by filename, so any document that grows
# one is handled the same way.
_TOC_SECTION_RE = re.compile(
    r"^##\s+Table of contents\s*$.*?(?=^##\s)", re.M | re.S | re.I)


def _strip_contents_section(text: str) -> str:
    return _TOC_SECTION_RE.sub("", text)


def _render(doc: HelpDoc) -> RenderedDoc:
    text = doc.file.read_text(encoding="utf-8")
    if doc.plain:
        # Not Markdown, and must not be treated as it: a licence text is full
        # of characters Markdown would eat.
        return RenderedDoc(doc, f"<pre class=\"plaintext\">{html.escape(text)}</pre>", [])
    md = build_markdown(doc.path)
    body = md.convert(_strip_contents_section(text))
    return RenderedDoc(doc, body, getattr(md, "toc_tokens", []))


def render_doc(slug: str) -> RenderedDoc | None:
    """Render one document by slug, or None if there is no such document.

    The slug is a registry key, never a path segment, so no URL can reach a
    file the registry does not list.
    """
    doc = BY_SLUG.get(slug)
    if doc is None:
        return None
    try:
        mtime = doc.file.stat().st_mtime
    except OSError:
        # Missing on disk. In a container that means the .dockerignore
        # exclusions were not applied; the caller renders a 404.
        return None

    hit = _cache.get(slug)
    if hit is not None and hit[0] == mtime:
        return hit[1]
    rendered = _render(doc)
    _cache[slug] = (mtime, rendered)
    return rendered


def doc_source(slug: str) -> str | None:
    """The Markdown of one document, as the page shows it — what the search
    reads. The contents section is stripped here too, so the search cannot
    offer a hit on a table of contents the reader will not find on the page.
    """
    doc = BY_SLUG.get(slug)
    if doc is None:
        return None
    try:
        text = doc.file.read_text(encoding="utf-8")
    except OSError:
        return None
    return text if doc.plain else _strip_contents_section(text)
