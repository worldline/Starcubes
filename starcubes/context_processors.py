"""Template context that is the same for every request on every page.

Only `about_info` so far: the version and build facts the About dialog names.
It is a context processor rather than per-view context because the dialog is
included from two independent template trees — `sources/base.html` and
`viz/chart.html` — rendered by a dozen views between them, and none of them
should have to remember to pass it.

The cost is one dict lookup per request: everything is computed once, at
import, since nothing here can change while the process runs.
"""
from __future__ import annotations

import platform

import django

from starcubes.version import BUILD_INFO

_ABOUT = dict(
    BUILD_INFO,
    python_version=platform.python_version(),
    django_version=django.get_version(),
)


def about_info(request):
    return {"about": _ABOUT}
