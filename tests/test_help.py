"""viz/help.py — Suche und TOC-Helfer."""
from __future__ import annotations

from viz.help import (
    _flatten_toc,
    _plain,
    _search_doc,
    _slugify_heading,
)


class TestPlain:
    def test_link_wird_text(self):
        assert _plain("siehe [Dokument](docs/x.md)") == "siehe Dokument"

    def test_bild_wird_alttext(self):
        assert _plain("![Logo](img/x.png)") == "Logo"

    def test_tabellenzeile_wird_prosa(self):
        assert _plain("| a | b | c |") == "a · b · c"

    def test_aufzaehlung_und_betonung(self):
        assert _plain("- **fett** und `code`") == "fett und code"

    def test_mehrfache_leerzeichen(self):
        assert _plain("a   b") == "a b"


class TestSlugifyHeading:
    def test_markup_wird_entfernt(self):
        assert _slugify_heading("Der **Alarm**") == "der-alarm"

    def test_code_und_klammern(self):
        assert _slugify_heading("`core/alarm.py` (Episoden)") == \
            "corealarmpy-episoden"

    def test_umlaute(self):
        # python-markdowns slugify normalisiert nach ASCII: ö→o, ß fällt weg.
        assert _slugify_heading("Größe") == "groe"


class TestFlattenToc:
    def _tokens(self):
        return [{
            "id": "a", "name": "A",
            "children": [
                {"id": "a1", "name": "A1", "children": [
                    {"id": "a11", "name": "A11", "children": []}]},
            ],
        }]

    def test_nur_zwei_ebenen(self):
        flat = _flatten_toc(self._tokens())
        assert [(t["id"], t["depth"]) for t in flat] == [
            ("a", 0), ("a1", 1)]

    def test_leer(self):
        assert _flatten_toc([]) == []


class TestSearchDoc:
    def test_ueberschrift_treffer_zuerst(self):
        hits = _search_doc("alarm", "alarm")
        assert hits
        assert any(h["in_heading"] for h in hits)

    def test_codebloecke_werden_ignoriert(self):
        hits = _search_doc("alarm", "definitely_not_in_a_doc_xyz")
        assert hits == []

    def test_unbekannter_slug(self):
        assert _search_doc("gibt-es-nicht", "alarm") == []

    def test_snippet_enthaelt_kein_markup(self):
        hits = _search_doc("alarm", "episode")
        assert hits
        assert all("[" not in h["snippet"] for h in hits)
