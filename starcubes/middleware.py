"""Django middleware: login enforcement, and the request path as a scheduler."""
from __future__ import annotations

import logging
import threading
import time

from django.contrib.auth.views import redirect_to_login
from django.http import HttpRequest, HttpResponse

logger = logging.getLogger(__name__)

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


class PipelineAlarmMiddleware:
    """Let the request path double as a scheduler for pipeline alarms.

    A wedged ETL loop cannot report that it is wedged, so pipeline alarms
    need an evaluator outside the process being watched. The container runs
    one (`manage.py pipeline_watch`); this is the fallback for every
    deployment that does not — a bare `runserver`, a uwsgi behind somebody
    else's supervisor — and it is honestly weak on its own: notification
    then depends on somebody using the app, and an unattended weekend is
    exactly when that fails and exactly when it matters.

    Two throttles, because uwsgi runs several processes: a process-local
    timestamp that costs nothing, and behind it the advisory lock in
    `core.pipeline_alarm`, which is what actually keeps the ETL loop, this
    and the supervisor from racing.

    **The work happens on a daemon thread, and that is not laziness.** A
    dispatch can take up to a minute per transition (SMTP 10 s, XMPP 40 s,
    PagerDuty 10 s), and nothing may hold a page load open for that. The
    cost is named rather than hidden: if the worker is recycled mid-send
    that one notification is lost. The episode row is already committed, so
    the *state* stays right — only the message goes missing, and the
    Firings view still shows the episode.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self._last = 0.0
        self._lock = threading.Lock()

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        try:
            self._maybe_schedule()
        except Exception:
            # A monitoring side effect must never colour a page.
            logger.exception("pipeline alarm scheduling failed")
        return response

    def _maybe_schedule(self) -> None:
        from core.pipeline_alarm import EVAL_INTERVAL_S

        now = time.monotonic()
        with self._lock:
            if now - self._last < EVAL_INTERVAL_S:
                return
            self._last = now
        threading.Thread(target=self._evaluate, name="pipeline-alarm",
                         daemon=True).start()

    @staticmethod
    def _evaluate() -> None:
        from django.conf import settings

        from core.alarm import run_alarm_cycle
        from core.source import _get_cached_engine
        from sources.alarm_notify import dispatch_transitions

        dsn = settings.STARCUBES_POSTGRES_DSN
        if not dsn:
            return
        try:
            meta = _get_cached_engine(dsn)
            counts = run_alarm_cycle(meta, dsn, kinds=("pipeline",))
            if counts.get("transitions"):
                dispatch_transitions(counts["transitions"], meta)
        except Exception:
            logger.exception("pipeline alarm evaluation failed")
