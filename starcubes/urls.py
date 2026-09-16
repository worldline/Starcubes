"""Root URL conf."""
from django.conf import settings
from django.contrib import admin
from django.contrib.auth.views import LoginView, LogoutView
from django.urls import include, path

from starcubes import health


class LoginViewWithDbConfig(LoginView):
    """The login page's SSO button follows the DB-stored configuration
    (starcubes_meta.app_config) at request time: `extra_context` frozen
    into as_view() would bake the env-derived values at URLconf import.
    `oidc_enabled` (the button) reads the boot-resolved OIDC_ENABLED —
    which the boot override (starcubes/oidc.py:apply_db_overrides) has
    already refreshed from the DB — while the display name comes live
    from the resolver."""

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        try:
            from core.config import effective_oidc
            ctx["oidc_display_name"] = (
                effective_oidc().get("STARCUBES_OIDC_DISPLAY_NAME")
                or settings.STARCUBES_OIDC_DISPLAY_NAME)
        except Exception:
            pass
        return ctx


urlpatterns = [
    # Readiness probe, no login (starcubes/middleware.py only gates /sources/
    # and /admin/). Before the viz include so no chart route can shadow it.
    path("healthz", health.healthz, name="healthz"),
    path("", include("viz.urls")),
    # The containers sharing this database, for the About dialog. Under
    # /sources/, so RequireLoginMiddleware gates it like the rest.
    path("sources/nodes/", health.nodes, name="nodes"),
    path("sources/", include("sources.urls")),
    path(
        "login/",
        LoginViewWithDbConfig.as_view(
            template_name="registration/login.html",
            extra_context={
                "oidc_enabled": settings.OIDC_ENABLED,
                "oidc_display_name": settings.STARCUBES_OIDC_DISPLAY_NAME,
            },
        ),
        name="login",
    ),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("admin/", admin.site.urls),
]

# Mounted only when actually configured — with OIDC unconfigured, /oidc/*
# doesn't exist at all (404), not a route that exists but 500s on missing
# settings. See starcubes/settings.py "Keycloak / OIDC SSO".
if settings.OIDC_ENABLED:
    urlpatterns.append(path("oidc/", include("mozilla_django_oidc.urls")))