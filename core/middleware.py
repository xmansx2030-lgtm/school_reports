from __future__ import annotations

import logging
import secrets
import threading
import time

from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponse, HttpResponseNotFound, JsonResponse

from .client_ip import client_ip_for_ratelimit
from .trace_context import reset_trace_id, set_trace_id
from . import opmetrics

BLOCKED_PREFIXES = (
    "/wp-admin",
    "/wp-content",
    "/wp-includes",
    "/wordpress",
    "/.env",
    "/lander",
    "/cmd_sco",
    "/xmlrpc.php",
    "/vendor/phpunit",
    "/cgi-bin",
    "/boaform",
    "/manager/html",
    "/invoker",
)

BLOCKED_CONTAINS = (
    "jmxinvokerservlet",
    "struts",
    "autodiscover.xml",
    "/.git/",
)

NOISY_PREFIX_LIMITS = {
    "/.well-known/": {"window": 120, "burst": 12},
    "/rest/": {"window": 60, "burst": 20},
}


logger = logging.getLogger(__name__)


class RequestTraceMiddleware:
    """Attach a request correlation id and expose it in response headers/log context."""

    HEADER_NAME = "X-Request-ID"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        incoming = request.headers.get(self.HEADER_NAME) if hasattr(request, "headers") else None
        trace_id = incoming or secrets.token_hex(8)
        request.trace_id = str(trace_id)[:64]
        token = set_trace_id(request.trace_id)
        started = time.monotonic()
        try:
            response = self.get_response(request)
        except Exception:
            opmetrics.increment("http.requests.total")
            opmetrics.increment("http.responses.5xx")
            raise
        finally:
            reset_trace_id(token)
            opmetrics.timing("http.response.duration", (time.monotonic() - started) * 1000)
        opmetrics.increment("http.requests.total")
        status_code = int(getattr(response, "status_code", 0) or 0)
        if status_code >= 500:
            opmetrics.increment("http.responses.5xx")
        elif status_code >= 400:
            opmetrics.increment("http.responses.4xx")
        try:
            response[self.HEADER_NAME] = request.trace_id
        except Exception:
            pass
        return response


class ConcurrencyLimitMiddleware:
    """Shed excess load instead of letting a spike exhaust the process.

    Why this exists
    ---------------
    Django's ASGI handler wraps every request in ``ThreadSensitiveContext``,
    and asgiref then allocates a dedicated ``ThreadPoolExecutor(max_workers=1)``
    per in-flight request. There is no global ceiling: 1,000 simultaneous
    visitors become 1,000 OS threads, each opening its own database connection
    (the connection registry is thread-local, so nothing is shared). PostgreSQL
    refuses new connections past ``max_connections`` — default 100 — so an
    unbounded burst turns a slow page into a full outage, and Celery and the
    beat scheduler lose their connections too.

    ``gunicorn --threads`` does not help: the Uvicorn worker ignores it. So the
    ceiling has to live in the application.

    Behaviour
    ---------
    Requests beyond ``MAX_CONCURRENT_REQUESTS`` get a fast 503 with
    ``Retry-After`` rather than queueing behind a saturated process. Serving a
    subset of visitors correctly beats timing out for all of them.

    ``/healthz/`` is always admitted: a merely busy container must not be
    reported as dead and restarted, which would make the overload worse.
    """

    EXEMPT_PATHS = frozenset({"/healthz/"})

    _lock = threading.Lock()
    _in_flight = 0

    def __init__(self, get_response):
        self.get_response = get_response

    @classmethod
    def _limit(cls) -> int:
        try:
            return max(0, int(getattr(settings, "MAX_CONCURRENT_REQUESTS", 0) or 0))
        except (TypeError, ValueError):
            return 0

    @classmethod
    def in_flight(cls) -> int:
        with cls._lock:
            return cls._in_flight

    @staticmethod
    def _wants_json(request) -> bool:
        try:
            path = (getattr(request, "path", "") or "").lower()
            accept = (request.headers.get("Accept") or "").lower()
            requested_with = (request.headers.get("X-Requested-With") or "").lower()
            return (
                path.startswith("/api/")
                or "application/json" in accept
                or requested_with == "xmlhttprequest"
            )
        except Exception:
            return False

    def _overloaded_response(self, request):
        retry_after = str(int(getattr(settings, "OVERLOAD_RETRY_AFTER_SECONDS", 5) or 5))
        message = "الخدمة مزدحمة حالياً. حاول مرة أخرى بعد لحظات."
        if self._wants_json(request):
            response = JsonResponse({"detail": "server_busy", "message": message}, status=503)
        else:
            response = HttpResponse(
                message, status=503, content_type="text/plain; charset=utf-8"
            )
        response["Retry-After"] = retry_after
        response["Cache-Control"] = "no-store"
        return response

    def __call__(self, request):
        limit = self._limit()
        path = getattr(request, "path", "") or ""
        if limit <= 0 or path in self.EXEMPT_PATHS or path.startswith(("/static/", "/media/")):
            return self.get_response(request)

        cls = type(self)
        with cls._lock:
            if cls._in_flight >= limit:
                current = cls._in_flight
                admitted = False
            else:
                cls._in_flight += 1
                current = cls._in_flight
                admitted = True

        if not admitted:
            opmetrics.increment("http.overload.shed")
            logger.warning(
                "Shedding request over concurrency limit path=%s in_flight=%s limit=%s trace_id=%s",
                path,
                current,
                limit,
                getattr(request, "trace_id", "-"),
            )
            return self._overloaded_response(request)

        try:
            return self.get_response(request)
        finally:
            with cls._lock:
                cls._in_flight -= 1


class SchoolRateLimitMiddleware:
    """Bound aggregate traffic from one tenant across all of its accounts.

    User/IP limits do not protect a multi-user tenant: fifty valid accounts can
    each stay below their personal limit while jointly overwhelming the same
    school aggregates. ActiveSchoolGuardMiddleware resolves and authorises the
    school first; this middleware then applies one Redis-backed budget without
    issuing another database query.
    """

    EXEMPT_PREFIXES = ("/static/", "/media/", "/healthz/", "/ops/metrics/")

    def __init__(self, get_response):
        self.get_response = get_response

    @staticmethod
    def _json_request(request) -> bool:
        path = (getattr(request, "path", "") or "").lower()
        accept = (request.headers.get("Accept") or "").lower()
        requested_with = (request.headers.get("X-Requested-With") or "").lower()
        return path.startswith("/api/") or "application/json" in accept or requested_with == "xmlhttprequest"

    def __call__(self, request):
        if not bool(getattr(settings, "SCHOOL_RATE_LIMIT_ENABLED", True)):
            return self.get_response(request)

        path = getattr(request, "path", "") or "/"
        user = getattr(request, "user", None)
        school = getattr(request, "active_school", None)
        if (
            path.startswith(self.EXEMPT_PREFIXES)
            or not getattr(user, "is_authenticated", False)
            or school is None
        ):
            return self.get_response(request)

        try:
            limit = max(1, int(getattr(settings, "SCHOOL_RATE_LIMIT_REQUESTS", 900) or 900))
            window = max(10, int(getattr(settings, "SCHOOL_RATE_LIMIT_WINDOW_SECONDS", 60) or 60))
            bucket = int(time.time() // window)
            key = f"school-rate:v1:{int(school.pk)}:{bucket}"
            created = cache.add(key, 1, timeout=window + 5)
            count = 1 if created else int(cache.incr(key))
        except Exception:
            # A cache incident must not become a platform-wide outage. The
            # process-wide concurrency ceiling remains the final safety net.
            return self.get_response(request)

        if count > limit:
            retry_after = max(1, window - (int(time.time()) % window))
            opmetrics.increment("http.school.rate_limited")
            message = "طلبات المدرسة مرتفعة حاليًا. حاول مرة أخرى بعد لحظات."
            if self._json_request(request):
                response = JsonResponse(
                    {"detail": "school_rate_limited", "message": message}, status=429
                )
            else:
                response = HttpResponse(message, status=429, content_type="text/plain; charset=utf-8")
            response["Retry-After"] = str(retry_after)
            response["Cache-Control"] = "no-store"
            response["X-RateLimit-Limit"] = str(limit)
            response["X-RateLimit-Remaining"] = "0"
            return response

        response = self.get_response(request)
        try:
            response["X-RateLimit-Limit"] = str(limit)
            response["X-RateLimit-Remaining"] = str(max(0, limit - count))
        except Exception:
            pass
        return response


class BlockBadPathsMiddleware:
    """Blocks common scanner/probe paths early before reaching views/DB."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = (request.path or "/").lower()
        if path.startswith("/static/") or path.startswith("/media/"):
            return self.get_response(request)
        noisy_rule = None
        for prefix, rule in NOISY_PREFIX_LIMITS.items():
            if path.startswith(prefix):
                noisy_rule = rule
                break
        if noisy_rule:
            try:
                # العنوان يُحسم عبر ``client_ip_for_ratelimit``: لا يُصدَّق
                # ``X-Forwarded-For`` إلا إذا كان النِّدُّ المباشر وسيطاً موثوقاً.
                # قراءة الترويسة الخام كانت تجعل المفتاح قابلاً للانتحال، فيكفي
                # المهاجمَ أن يبدّل قيمتها في كل طلب ليصير لكل طلبٍ عدّادُه.
                ip = client_ip_for_ratelimit(request)
                key = f"noise-limit:{prefix}:{ip}"
                first_seen = cache.add(key, 0, timeout=int(noisy_rule["window"]))
                count = cache.incr(key) if not first_seen else 1
                if count > int(noisy_rule["burst"]):
                    opmetrics.increment("http.noisy_path.rate_limited")
                    if count in {int(noisy_rule["burst"]) + 1, int(noisy_rule["burst"]) + 10}:
                        logger.info(
                            "Rate-limited noisy path=%s ip=%s trace_id=%s count=%s",
                            path,
                            ip,
                            getattr(request, "trace_id", "-"),
                            count,
                        )
                    response = HttpResponseNotFound()
                    response.status_code = 429
                    return response
            except Exception:
                pass

        blocked = False
        for pref in BLOCKED_PREFIXES:
            if path.startswith(pref):
                blocked = True
                break
        if not blocked:
            for marker in BLOCKED_CONTAINS:
                if marker in path:
                    blocked = True
                    break

        if blocked:
            try:
                ip = client_ip_for_ratelimit(request)
                opmetrics.increment("http.scanner.blocked")
                key = f"scan-block:{ip}:{path[:80]}"
                first_seen = cache.add(key, 1, timeout=300)
                if first_seen:
                    logger.warning(
                        "Blocked suspicious probe path=%s ip=%s trace_id=%s ua=%s",
                        path,
                        ip,
                        getattr(request, "trace_id", "-"),
                        (request.META.get("HTTP_USER_AGENT", "") or "-")[:180],
                    )
            except Exception:
                pass
            # Return 404 to minimize endpoint fingerprinting.
            return HttpResponseNotFound()
        return self.get_response(request)
