"""Django middleware: login enforcement on protected paths."""
from __future__ import annotations

from django.contrib.auth.views import redirect_to_login
from django.http import HttpRequest, HttpResponse

_PROTECTED_PREFIXES = ("/sources/", "/admin/")
_PUBLIC_PATHS = {"/login/", "/logout/"}


class RequireLoginMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if request.path in _PUBLIC_PATHS:
            return self.get_response(request)
        if any(request.path.startswith(p) for p in _PROTECTED_PREFIXES):
            if not request.user.is_authenticated:
                return redirect_to_login(request.get_full_path(), login_url="/login/")
        return self.get_response(request)