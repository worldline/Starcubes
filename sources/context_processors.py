from __future__ import annotations

from django.conf import settings
from sqlalchemy import text

from core.source import _get_cached_engine


def topbar_sources(request):
    if not request.path.startswith("/sources/"):
        return {"topbar_sources": []}
    try:
        with _get_cached_engine(settings.STARCUBES_POSTGRES_DSN).begin() as conn:
            rows = conn.execute(text(
                "SELECT id, name FROM starcubes_meta.datasource ORDER BY name"
            )).mappings().all()
        return {"topbar_sources": [dict(r) for r in rows]}
    except Exception:
        return {"topbar_sources": []}
