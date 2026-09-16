"""Static files with content hashes in their names, minus source maps.

`ManifestStaticFilesStorage` rewrites every reference it recognises to the
hashed name of its target, and refuses to finish `collectstatic` when a target
is missing. Vendored bundles end in a `sourceMappingURL` comment naming a
`.map` file that is not shipped (`slides/reveal.js.map`), which aborted the
whole run. A source map is a debugging aid, not something a page loads, so
those comments are left as they are; `url()` and `@import` in CSS are still
rewritten and still checked.

Why hashed names at all: see `STORAGES` in starcubes/settings.py.
"""
from __future__ import annotations

from django.contrib.staticfiles.storage import ManifestStaticFilesStorage


def _without_source_maps(patterns):
    out = []
    for ext, entries in patterns:
        kept = tuple(e for e in entries
                     if "sourceMappingURL" not in (e[0] if isinstance(e, tuple) else e))
        if kept:
            out.append((ext, kept))
    return tuple(out)


class StaticFilesStorage(ManifestStaticFilesStorage):
    patterns = _without_source_maps(ManifestStaticFilesStorage.patterns)
