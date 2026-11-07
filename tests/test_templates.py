"""Template-Falle: ``{# … #}`` ist in Django nur einzeilig.

Ein mehrzeiliger ``{#``-Kommentar ist kein Kommentar — Django erkennt ihn
nicht, und der Text wird als Seiteninhalt ausgeliefert. Das fällt weder beim
Rendern noch im Layout auf. Der Test ist der maschinelle Ersatz für „die
gerenderte Seite ansehen".
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _templates():
    return sorted(ROOT.glob("**/templates/**/*.html"))


def test_es_gibt_templates():
    assert _templates(), "keine Templates gefunden — Pfad stimmt nicht"


def test_kein_mehrzeiliger_kommentar():
    leaks = []
    for path in _templates():
        for lineno, line in enumerate(path.read_text(encoding="utf-8")
                                      .splitlines(), 1):
            start = line.find("{#")
            if start < 0:
                continue
            if line.find("#}", start + 2) < 0:
                leaks.append(f"{path.relative_to(ROOT)}:{lineno}")
    assert not leaks, (
        "mehrzeiliger {# … #}-Kommentar (Text würde im Browser erscheinen): "
        + ", ".join(leaks))
