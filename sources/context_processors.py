from __future__ import annotations

from django.conf import settings
from sqlalchemy import text

from core.source import _get_cached_engine


def topbar_sources(request):
    # The help pages render in sources/base.html too, and a nav bar with no
    # source tabs on them would read as "this install has no datasources".
    if not request.path.startswith(("/sources/", "/help/")):
        return {"topbar_sources": []}
    try:
        with _get_cached_engine(settings.STARCUBES_POSTGRES_DSN).begin() as conn:
            rows = conn.execute(text(
                "SELECT id, name FROM starcubes_meta.datasource ORDER BY name"
            )).mappings().all()
        return {"topbar_sources": [dict(r) for r in rows]}
    except Exception:
        return {"topbar_sources": []}
