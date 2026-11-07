"""core/docs.py — Help-Registry und Link-Rewriting."""
from __future__ import annotations

from core.docs import (
    BY_PATH,
    BY_SLUG,
    GROUP_ORDER,
    HELP_URL_PREFIX,
    _rewrite_url,
    doc_source,
    grouped_docs,
    render_doc,
)


class TestRegistry:
    def test_grouped_docs_folgt_group_order(self):
        groups = grouped_docs()
        assert [g for g, _blurb, _docs in groups] == list(GROUP_ORDER)
        assert all(isinstance(blurb, str) for _g, blurb, _d in groups)

    def test_jeder_slug_ist_eindeutig(self):
        assert len(BY_SLUG) == len(set(BY_SLUG))

    def test_by_path_zeigt_auf_slugs(self):
        for path, slug in BY_PATH.items():
            assert path.endswith((".md", "LICENSE", "NOTICES")) or \
                path.startswith("docs/")
            assert slug in BY_SLUG


class TestRewriteUrl:
    def test_absolut_bleibt(self):
        assert _rewrite_url("https://example.org/x", "docs/a.md") is None
        assert _rewrite_url("/sources/", "docs/a.md") is None
        assert _rewrite_url("#abschnitt", "docs/a.md") is None
        assert _rewrite_url("//cdn/x.js", "docs/a.md") is None
        assert _rewrite_url("mailto:x@example.org", "docs/a.md") is None

    def test_leer(self):
        assert _rewrite_url("", "docs/a.md") is None

    def test_dokument_relativ_zum_verlinkenden_dokument(self):
        # internals.md liegt in docs/; "alarm.md" meint docs/alarm.md.
        assert _rewrite_url("alarm.md", "docs/internals.md") == \
            HELP_URL_PREFIX + "alarm"

    def test_fragment_bleibt(self):
        got = _rewrite_url("alarm.md#episoden", "docs/internals.md")
        assert got == HELP_URL_PREFIX + "alarm#episoden"

    def test_bild_unter_docs(self):
        got = _rewrite_url("img/chart-hero.png", "docs/internals.md")
        assert got == "/docs/img/chart-hero.png"

    def test_unbekanntes_ziel(self):
        assert _rewrite_url("gibt-es-nicht.md", "docs/internals.md") is None

    def test_pfadausbruch(self):
        assert _rewrite_url("../../etc/passwd", "docs/internals.md") is None


class TestRender:
    def test_bekanntes_dokument(self):
        rendered = render_doc("alarm")
        assert rendered is not None
        assert rendered.doc.slug == "alarm"
        assert isinstance(rendered.html, str)
        assert rendered.html

    def test_unbekannter_slug(self):
        assert render_doc("gibt-es-nicht") is None

    def test_doc_source_streift_inhaltsverzeichnis(self):
        text = doc_source("alarm")
        assert text is not None
        assert "#" in text

    def test_unbekannte_quelle(self):
        assert doc_source("gibt-es-nicht") is None
