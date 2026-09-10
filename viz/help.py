"""The help section at ``/help/`` — the repo's Markdown, served by the app.

A thin Django adapter over ``core.docs``, which owns the registry, the
rendering and the link rewriting. Kept out of ``views.py`` because that file
is already 900 lines of chart and query endpoints and shares nothing with
this.

Deliberately public, like ``/slides``: ``RequireLoginMiddleware`` guards only
``/sources/`` and ``/admin/``, so the login page can point at the help and a
reader never hits a wall. Nothing here reads the database.
"""
from __future__ import annotations

import re

from django.http import Http404, JsonResponse
from django.shortcuts import render

from core.docs import HELP_DOCS, doc_source, grouped_docs, render_doc


def _flatten_toc(tokens, depth=0, out=None):
    """`md.toc_tokens` as a flat list for the sidebar.

    Two levels only. The corpus nests four deep in places, and a sidebar
    that mirrors that becomes a wall of text rather than a way around the
    page. The top-level H1 is unwrapped by the caller, so `depth` counts
    from the H2s.
    """
    if out is None:
        out = []
    for t in tokens:
        out.append({"id": t["id"], "name": t["name"], "depth": depth})
        if depth < 1:
            _flatten_toc(t["children"], depth + 1, out)
    return out


def help_index(request):
    """The landing page: every document, grouped, with its blurb."""
    return render(request, "viz/help_index.html", {
        "groups": grouped_docs(),
        "help_active": True,
    })


def help_page(request, slug):
    """One rendered document, with its sidebar and in-page contents."""
    rendered = render_doc(slug)
    if rendered is None:
        # Either no such slug, or the file is absent — which in a container
        # means the .dockerignore exclusions for *.md were not applied.
        raise Http404("no such help document")

    doc = rendered.doc
    # The document's own H1 becomes the page heading, so the contents list
    # starts at its children; a single top-level token is that H1.
    toc_roots = rendered.toc
    if len(toc_roots) == 1:
        toc_roots = toc_roots[0]["children"]

    group_docs = [d for d in HELP_DOCS if d.group == doc.group]
    return render(request, "viz/help_page.html", {
        "doc": doc,
        "content": rendered.html,
        "toc": _flatten_toc(toc_roots),
        "group_docs": group_docs,
        "groups": grouped_docs(),
        "help_active": True,
    })


# ── search ───────────────────────────────────────────────────────────────────
# Over the raw Markdown rather than the rendered HTML: no tags to strip out of
# a snippet, and the heading a hit sits under is what gives the result its
# deep link. The corpus is ~8k lines, so scanning all of it per query is well
# under a millisecond and needs no index.

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*$")
_FENCE_RE = re.compile(r"^\s*```")
_MAX_RESULTS = 40

# Turning one source line into something worth reading in a result list.
# Without this a hit shows its Markdown — "- [Watermark and partitioning
# fields](#watermark-and-partitioning-fields)" — which is noise around the
# word the reader searched for.
_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")     # [text](url) -> text
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")   # ![alt](url)  -> alt
_EMPH_RE = re.compile(r"(\*\*|__|\*|_|`)")            # bold/italic/code marks
_LEADING_RE = re.compile(r"^\s*(?:[-*+]|\d+\.|>|#{1,6})\s*")
_WS_RE = re.compile(r"\s+")


def _plain(line: str) -> str:
    """One Markdown line as plain prose, for a search snippet."""
    line = _IMAGE_RE.sub(r"\1", line)
    line = _LINK_RE.sub(r"\1", line)
    line = _LEADING_RE.sub("", line)
    # Table rows read as prose once the pipes become separators.
    if line.count("|") >= 2:
        line = " · ".join(c.strip() for c in line.strip("| ").split("|") if c.strip())
    line = _EMPH_RE.sub("", line)
    return _WS_RE.sub(" ", line).strip()


def _slugify_heading(text: str) -> str:
    """python-markdown's toc slugify, on a heading's plain text.

    Imported from the extension rather than reimplemented so the anchors the
    search links to cannot drift from the ones the renderer emits.
    """
    from markdown.extensions.toc import slugify
    # Strip the inline markup a heading may carry; `slugify` works on text.
    plain = re.sub(r"[`*_\[\]]", "", text)
    return slugify(plain, "-")


def _search_doc(slug: str, needle: str):
    """Hits in one document as [{heading, anchor, snippet, in_heading}, ...]."""
    source = doc_source(slug)
    if not source:
        return []

    hits = []
    heading, anchor = "", ""
    in_fence = False
    for line in source.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        m = None if in_fence else _HEADING_RE.match(line)
        if m:
            heading = m.group(2)
            anchor = _slugify_heading(heading)
            if needle in heading.lower():
                hits.append({"heading": _plain(heading), "anchor": anchor,
                             "snippet": _plain(heading), "in_heading": True})
            continue
        if needle in line.lower():
            snippet = _plain(line)
            # A line whose only match was inside the markup just stripped —
            # a link target, say — is not a hit a reader can see.
            if needle not in snippet.lower():
                continue
            hits.append({"heading": _plain(heading), "anchor": anchor,
                         "snippet": snippet, "in_heading": False})
    return hits


def help_search(request):
    """`?q=` over every document. Headings rank above body text."""
    q = (request.GET.get("q") or "").strip()
    if len(q) < 2:
        return JsonResponse({"ok": True, "query": q, "results": []})

    needle = q.lower()
    results = []
    for doc in HELP_DOCS:
        hits = _search_doc(doc.slug, needle)
        if not hits:
            continue
        hits.sort(key=lambda h: not h["in_heading"])
        results.append({
            "slug": doc.slug,
            "title": doc.title,
            "group": doc.group,
            "count": len(hits),
            "hits": hits[:4],
        })

    # Documents with a heading match first, then by how much they have to say
    # about the term.
    results.sort(key=lambda r: (not r["hits"][0]["in_heading"], -r["count"]))
    return JsonResponse({"ok": True, "query": q, "results": results[:_MAX_RESULTS]})
