"""Check the help section's links, the way ``check_migration_mirror`` checks
the migration mirror: a contract that was only ever stated in prose.

Four things can rot without anyone noticing, because a broken link neither
raises nor breaks the layout — the page simply renders with a link that goes
nowhere:

1. a registry entry naming a file that no longer exists;
2. a Markdown link to another document that the registry does not serve
   (typically because a file was renamed and the registry was not);
3. an anchor link to a heading that no longer exists, or was reworded — the
   corpus links between documents by anchor a lot;
4. an image whose file is gone.

Plus the reverse direction: a ``{% include "sources/_help_link.html" %}``
in a template naming a slug that does not exist.

Exits non-zero on any of those, so it can run in the same breath as the rest
of the verification checklist.
"""

import re
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from core.docs import (BY_PATH, HELP_DOCS, HELP_URL_PREFIX, REPO_ROOT,
                       build_markdown)

# Markdown inline links, plus the raw-HTML attributes the corpus also uses
# (README.md carries its logo in a <picture> block).
_MD_LINK_RE = re.compile(r"\]\(\s*([^)\s]+)")
# Fenced blocks and inline code, stripped before scanning: this corpus
# documents markup for a living, and `<use href="#ic-…">` inside backticks is
# an illustration, not a link to check.
_CODE_RE = re.compile(r"```.*?```|~~~.*?~~~|`[^`\n]*`", re.S)
_ATTR_RE = re.compile(r'\b(?:href|src|srcset)\s*=\s*"([^"]*)"')
_ABSOLUTE_RE = re.compile(r"^(?:[a-z][a-z0-9+.-]*:|//|/|#)", re.I)
# The slug in {% include "sources/_help_link.html" with slug="…" %} and in
# {% url 'viz:help_page' 'slug' %}.
_TEMPLATE_SLUG_RE = re.compile(
    r"""_help_link\.html["']\s+with\s+slug=["']([^"']+)["']"""
    r"""|viz:help_page["']\s+["']([^"']+)["']""")


def _heading_ids(path: Path, source_path: str) -> set:
    """Every anchor id the renderer emits for one document."""
    md = build_markdown(source_path)
    md.convert(path.read_text(encoding="utf-8"))
    ids = set()

    def walk(tokens):
        for t in tokens:
            ids.add(t["id"])
            walk(t["children"])

    walk(getattr(md, "toc_tokens", []))
    return ids


class Command(BaseCommand):
    help = ("Verify every help document, cross-link, anchor and image "
            "resolves (see CLAUDE.md, verification).")

    def add_arguments(self, parser):
        parser.add_argument(
            "--quiet", action="store_true",
            help="only report problems, not the per-document summary")

    def handle(self, *args, **options):
        quiet = options["quiet"]
        problems = []

        # Anchor ids per document, computed once — rendering the corpus twice
        # is the slow way to do this.
        anchors = {}
        for doc in HELP_DOCS:
            if not doc.file.exists():
                problems.append(f"{doc.slug}: missing file {doc.path}")
                continue
            if not doc.plain:
                anchors[doc.path] = _heading_ids(doc.file, doc.path)

        checked = 0
        for doc in HELP_DOCS:
            if not doc.file.exists() or doc.plain:
                continue
            text = _CODE_RE.sub(" ", doc.file.read_text(encoding="utf-8"))
            targets = set(_MD_LINK_RE.findall(text)) | set(_ATTR_RE.findall(text))
            for raw in targets:
                if not raw or _ABSOLUTE_RE.match(raw):
                    continue
                checked += 1
                target, _, fragment = raw.partition("#")
                if not target:
                    continue
                try:
                    rel = ((REPO_ROOT / Path(doc.path).parent / target)
                           .resolve().relative_to(REPO_ROOT).as_posix())
                except (ValueError, OSError):
                    problems.append(f"{doc.path}: link escapes the repo: {raw}")
                    continue

                if rel.endswith(".md"):
                    if rel not in BY_PATH:
                        problems.append(
                            f"{doc.path}: links to {rel}, which the help "
                            f"section does not serve")
                    elif fragment and fragment not in anchors.get(rel, set()):
                        problems.append(
                            f"{doc.path}: no heading '#{fragment}' in {rel}")
                elif not (REPO_ROOT / rel).exists():
                    problems.append(f"{doc.path}: missing file {rel}")

            # In-page anchors have to resolve too — HOWTO.md's own table of
            # contents is 36 of them.
            for raw in targets:
                if raw.startswith("#") and raw[1:] not in anchors.get(doc.path, set()):
                    problems.append(f"{doc.path}: no heading '{raw}' in itself")

        # The other direction: slugs named by templates.
        slugs = {d.slug for d in HELP_DOCS}
        for tpl in sorted(REPO_ROOT.glob("*/templates/**/*.html")):
            for a, b in _TEMPLATE_SLUG_RE.findall(tpl.read_text(encoding="utf-8")):
                slug = a or b
                if slug and slug not in slugs:
                    rel = tpl.relative_to(REPO_ROOT)
                    problems.append(f"{rel}: no help document '{slug}'")

        if not quiet:
            self.stdout.write(
                f"{len(HELP_DOCS)} documents, {checked} internal links checked")

        if problems:
            for p in problems:
                self.stderr.write(self.style.ERROR("  " + p))
            raise CommandError(f"{len(problems)} help-doc problem(s)")

        if not quiet:
            self.stdout.write(self.style.SUCCESS(
                f"All help links resolve. Served under {HELP_URL_PREFIX}"))
