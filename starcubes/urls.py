"""Root URL conf."""
from django.conf import settings
from django.contrib import admin
from django.contrib.auth.views import LoginView, LogoutView
from django.urls import include, path

urlpatterns = [
    path("", include("viz.urls")),
    path("sources/", include("sources.urls")),
    path(
        "login/",
        LoginView.as_view(
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