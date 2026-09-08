from django.conf import settings
import logging
from django.contrib.auth import logout
from django.http import JsonResponse, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse, resolve
from django.utils import timezone
from django.contrib import messages

import secrets
from urllib.parse import urlsplit

from core.observability import report_degraded as _degraded, soft_fail



import threading

_thread_locals = threading.local()

logger = logging.getLogger(__name__)

FORCE_PASSWORD_CHANGE_SESSION_KEY = "force_password_change_required"
_PASSWORD_VERIFIED_OK_SESSION_KEY = "_pw_verified_not_default"

# A renewal is one journey even though it crosses several views.  Keep the
# list in one place so subscription expiry and temporary-password guards do
# not drift apart when another payment helper or gateway route is added.
SUBSCRIPTION_PAYMENT_FLOW_VIEWS = frozenset(
    {
        "reports:my_subscription",
        "reports:payment_create",
        "reports:discount_code_check",
        "reports:moyasar_checkout_create",
        "reports:moyasar_checkout_cancel",
        "reports:moyasar_return",
        "reports:tamara_checkout_create",
        "reports:tamara_checkout_cancel",
        "reports:tamara_return",
    }
)


def _resolved_view_name(request) -> str:
    try:
        match = resolve(request.path_info)
        return f"{match.namespace}:{match.url_name}" if match.namespace else (match.url_name or "")
    except Exception:
        return ""

def get_current_request():
    return getattr(_thread_locals, "request", None)


def set_audit_logging_suppressed(value: bool) -> None:
    """Suppress AuditLog signal writes for the current request/thread."""
    _thread_locals.suppress_audit_logging = bool(value)


def is_audit_logging_suppressed() -> bool:
    return bool(getattr(_thread_locals, "suppress_audit_logging", False))


def has_default_phone_password(user) -> bool:
    """Return True when the current password still matches the user's phone."""
    try:
        if not getattr(user, "is_authenticated", False):
            return False
        phone = (getattr(user, "phone", "") or "").strip()
        if not phone:
            return False
        return bool(user.check_password(phone))
    except Exception:
        return False


def is_force_password_change_required(request) -> bool:
    """Persist and expose the forced-password-change state on the request.

    Caches the bcrypt check_password result in session to avoid ~100ms CPU
    cost on every request for users who already changed their password.
    """
    user = getattr(request, "user", None)
    if not getattr(user, "is_authenticated", False):
        request.force_password_change_required = False
        return False

    try:
        session_required = bool(request.session.get(FORCE_PASSWORD_CHANGE_SESSION_KEY))
    except Exception:
        session_required = False

    if session_required:
        # Already flagged — skip the expensive bcrypt check.
        request.force_password_change_required = True
        return True

    # Fast path: if we already verified password is NOT default in this session,
    # skip the bcrypt call.
    with soft_fail("auth.password_verified_fast_path"):
        if request.session.get(_PASSWORD_VERIFIED_OK_SESSION_KEY):
            request.force_password_change_required = False
            return False

    required = has_default_phone_password(user)
    try:
        if required:
            request.session[FORCE_PASSWORD_CHANGE_SESSION_KEY] = True
            request.session.pop(_PASSWORD_VERIFIED_OK_SESSION_KEY, None)
        else:
            request.session.pop(FORCE_PASSWORD_CHANGE_SESSION_KEY, None)
            request.session[_PASSWORD_VERIFIED_OK_SESSION_KEY] = True
    except Exception:
        _degraded("auth.persist_password_change_flag")

    request.force_password_change_required = required
    return required


def clear_force_password_change_flag(request) -> None:
    # علمٌ لا يُمسح يُبقي المستخدم محبوساً في شاشة تغيير كلمة المرور بعد أن
    # غيّرها فعلاً.
    with soft_fail("auth.clear_password_change_flag"):
        request.session.pop(FORCE_PASSWORD_CHANGE_SESSION_KEY, None)
        # Clear cached verification so next request re-checks with bcrypt.
        request.session.pop(_PASSWORD_VERIFIED_OK_SESSION_KEY, None)
    request.force_password_change_required = False


def _log_denial(request, *, reason: str) -> None:
    try:
        logger.warning(
            "Permission denial reason=%s trace_id=%s user_id=%s path=%s active_school_id=%s",
            reason,
            getattr(request, "trace_id", None),
            getattr(getattr(request, "user", None), "id", None),
            getattr(request, "path", "-"),
            request.session.get("active_school_id") if hasattr(request, "session") else None,
        )
    except Exception:
        # سطرُ سجلٍّ يتعثّر لا يُسقط الطلب، لكنه يعني ضياع أثر منعٍ أمني.
        logger.exception("failed to log tenant denial")

class AuditLogMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        _thread_locals.request = request
        # Ensure suppression does not leak between requests.
        set_audit_logging_suppressed(False)
        try:
            return self.get_response(request)
        finally:
            # تنظيف بعد الطلب — لا بد أن يتم حتى لو رمى الطلب استثناءً،
            # وإلا بقي الطلب القديم مرتبطًا بالـ thread وسُجّلت عمليات لاحقة
            # باسم المستخدم الخطأ.
            if hasattr(_thread_locals, "request"):
                del _thread_locals.request
            if hasattr(_thread_locals, "suppress_audit_logging"):
                delattr(_thread_locals, "suppress_audit_logging")


class CanonicalHostMiddleware:
    """Permanently consolidate legacy public hosts onto ``SITE_URL``."""

    EXEMPT_PATHS = {"/healthz/"}

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if bool(getattr(settings, "CANONICAL_HOST_REDIRECT", False)):
            site_url = str(getattr(settings, "SITE_URL", "") or "").rstrip("/")
            canonical_host = (urlsplit(site_url).hostname or "").lower()
            request_host = request.get_host().split(":", 1)[0].lower()
            if (
                canonical_host
                and request_host != canonical_host
                and request.path_info not in self.EXEMPT_PATHS
                and request.method in {"GET", "HEAD"}
            ):
                from django.http import HttpResponsePermanentRedirect

                return HttpResponsePermanentRedirect(
                    f"{site_url}{request.get_full_path()}"
                )
        return self.get_response(request)


class MaintenanceModeMiddleware:
    """Show a site-wide maintenance screen while keeping superuser access."""

    CACHE_KEY = "platform_maintenance_state_v1"
    CACHE_TIMEOUT = 15

    def __init__(self, get_response):
        self.get_response = get_response

    def _state(self) -> dict:
        try:
            from django.core.cache import cache

            cached = cache.get(self.CACHE_KEY)
            if cached is not None:
                return cached
        except Exception:
            cache = None  # type: ignore

        state = {"enabled": False, "message": ""}
        try:
            from .models import PlatformSettings

            row = (
                PlatformSettings.objects.order_by("id")
                .values("maintenance_mode_enabled", "maintenance_message")
                .first()
            )
            if row:
                state = {
                    "enabled": bool(row.get("maintenance_mode_enabled")),
                    "message": (row.get("maintenance_message") or "").strip(),
                }
        except Exception:
            return state

        with soft_fail("maintenance.cache_state"):
            if cache is not None:
                cache.set(self.CACHE_KEY, state, self.CACHE_TIMEOUT)
        return state

    def _wants_json(self, request) -> bool:
        try:
            accept = (request.headers.get("Accept") or "").lower()
            xrw = (request.headers.get("X-Requested-With") or "").lower()
            path = (getattr(request, "path", "") or "").lower()
            return path.startswith("/api/") or ("application/json" in accept) or (xrw == "xmlhttprequest")
        except Exception:
            return False

    def _is_exempt_path(self, request) -> bool:
        path = getattr(request, "path", "") or ""
        if path.startswith(("/static/", "/media/", "/admin-panel/")):
            return True
        normalized_path = path.rstrip("/") or "/"
        if normalized_path in {
            "/platform-login",
            "/platform-dashboard",
            "/platform/settings",
            "/api/dashboard/platform",
            "/api/dashboard/platform/search",
        }:
            return True
        if path in {
            "/healthz/",
            "/favicon.ico",
            "/favicon.png",
            "/robots.txt",
            "/sitemap.xml",
            "/sw.js",
            "/.well-known/security.txt",
        }:
            return True

        try:
            match = resolve(request.path_info)
            full_name = f"{match.namespace}:{match.url_name}" if match.namespace else (match.url_name or "")
        except Exception:
            full_name = ""

        return full_name in {
            "reports:login",
            "reports:platform_login",
            "reports:logout",
            "reports:landing",
            "reports:faq",
            "reports:privacy_policy",
            "reports:terms_conditions",
            "reports:refund_policy",
            "reports:service_delivery_policy",
            "reports:complaints_policy",
            "reports:platform_admin_dashboard",
            "reports:api_platform_dashboard_data",
            "reports:api_platform_dashboard_search",
            "reports:platform_settings",
            "service_worker",
        }

    def __call__(self, request):
        state = self._state()
        if not bool(state.get("enabled")):
            return self.get_response(request)

        user = getattr(request, "user", None)
        if getattr(user, "is_authenticated", False) and getattr(user, "is_superuser", False):
            return self.get_response(request)

        if self._is_exempt_path(request):
            return self.get_response(request)

        message = (state.get("message") or "").strip()
        if self._wants_json(request):
            return JsonResponse(
                {
                    "detail": "maintenance_mode",
                    "message": message or "الموقع تحت الصيانة والتطوير حالياً.",
                },
                status=503,
            )

        response = render(
            request,
            "reports/maintenance_mode.html",
            {"maintenance_message": message},
            status=503,
        )
        response["Retry-After"] = "300"
        return response


class SearchEngineIndexingMiddleware:
    """Keep account, school, and shared-document pages out of search results.

    Only the deliberate public marketing/help pages are indexable.  Using an
    HTTP header also protects standalone templates and downloadable documents
    that do not inherit the application's base template.
    """

    INDEXABLE_VIEWS = {
        "reports:landing",
        "reports:faq",
        "reports:privacy_policy",
        "reports:terms_conditions",
        "reports:refund_policy",
        "reports:service_delivery_policy",
        "reports:complaints_policy",
        "reports:user_guide",
    }
    INDEXABLE_PATHS = {
        "/robots.txt",
        "/sitemap.xml",
        "/.well-known/security.txt",
    }

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        path = getattr(request, "path_info", "") or ""

        if path.startswith("/static/") or path in self.INDEXABLE_PATHS:
            return response

        match = getattr(request, "resolver_match", None)
        view_name = getattr(match, "view_name", "") if match is not None else ""
        if view_name not in self.INDEXABLE_VIEWS:
            response.headers.setdefault(
                "X-Robots-Tag",
                "noindex, nofollow, noarchive",
            )
        return response


class IdleLogoutMiddleware:
    """يسجل خروج المستخدم تلقائياً بعد مدة خمول.

    الخمول هنا يعني: عدم وجود تفاعل/تنقل فعلي داخل الصفحة.
    طلبات الخلفية (polling/AJAX/fetch) لا تُحتسب كنشاط.
    """

    SESSION_KEY = "_last_activity_ts"

    def __init__(self, get_response):
        self.get_response = get_response
        self.timeout_seconds = int(getattr(settings, "IDLE_LOGOUT_SECONDS", 30 * 60))

    def _is_interactive_request(self, request) -> bool:
        """Heuristic لتحديد ما إذا كان الطلب ناتجاً عن تفاعل المستخدم.

        - Navigations لصفحات HTML تُحتسب نشاطاً
        - Submits التقليدية للنماذج (form) تُحتسب نشاطاً
        - طلبات الخلفية (XHR/fetch/json) لا تُحتسب
        """

        headers = request.headers
        sec_fetch_mode = (headers.get("Sec-Fetch-Mode") or "").lower()
        sec_fetch_dest = (headers.get("Sec-Fetch-Dest") or "").lower()
        x_requested_with = (headers.get("X-Requested-With") or "").lower()
        accept = (headers.get("Accept") or "").lower()
        content_type = (headers.get("Content-Type") or "").lower()

        is_navigate = sec_fetch_mode == "navigate" or sec_fetch_dest == "document"
        is_xhr = x_requested_with == "xmlhttprequest"
        wants_html = "text/html" in accept
        wants_json = "application/json" in accept

        if is_navigate:
            return True

        # غالباً GET/HEAD الخلفية تكون fetch/XHR أو JSON؛ لا نحتسبها
        if request.method in {"GET", "HEAD"}:
            return wants_html and not wants_json and not is_xhr

        # Submits نماذج HTML التقليدية تُحتسب نشاطاً
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            if content_type.startswith("application/x-www-form-urlencoded"):
                return True
            if content_type.startswith("multipart/form-data"):
                return True

        return False

    def _is_background_request(self, request) -> bool:
        return not self._is_interactive_request(request)

    def __call__(self, request):
        # السماح بالملفات الثابتة والوسائط بدون احتسابها كنشاط
        if request.path.startswith("/static/") or request.path.startswith("/media/"):
            return self.get_response(request)

        # لو غير مسجل دخول، لا شيء نفعله
        if not request.user.is_authenticated:
            return self.get_response(request)

        # لا نطبق فحص الخمول على صفحة تسجيل الدخول/الخروج لتجنب أي حلقات
        with soft_fail("auth.resolve_login_logout_paths"):
            login_path = reverse("reports:login")
            logout_path = reverse("reports:logout")
            if request.path in {login_path, logout_path}:
                return self.get_response(request)

        now_ts = timezone.now().timestamp()
        last_ts = request.session.get(self.SESSION_KEY)

        if last_ts is not None:
            try:
                last_ts_f = float(last_ts)
                if now_ts - last_ts_f > self.timeout_seconds:
                    # logout() ينهى الجلسة (flush) ويُسقط المستخدم
                    logout(request)
                    if self._is_background_request(request):
                        return JsonResponse({"detail": "session_expired"}, status=401)
                    return redirect(settings.LOGIN_URL)
            except Exception:
                _degraded("auth.idle_logout_check")
                # في حال كانت القيمة غير صالحة لأي سبب، نعيد ضبطها
                pass

        # تحديث النشاط فقط لو كان تفاعل فعلي (لا نحتسب polling/AJAX كنشاط)
        if self._is_interactive_request(request):
            request.session[self.SESSION_KEY] = now_ts
            request.session.set_expiry(self.timeout_seconds)
        return self.get_response(request)


class ActiveSchoolGuardMiddleware:
    """Defense-in-depth: ensure session.active_school_id is actually accessible.

    Why:
    - Prevent stale/tampered session values from selecting a school the user cannot access.
    - Make downstream code safer even if a view forgets to validate membership.

    Policy (simple + safe):
    - If active_school_id is invalid/inaccessible, we remove it from the session.
    - We do NOT auto-pick a school here (leave UX to existing select/switch flows).
    """

    SESSION_KEY = "active_school_id"

    def __init__(self, get_response):
        self.get_response = get_response

    def _wants_json(self, request) -> bool:
        try:
            accept = (request.headers.get("Accept") or "").lower()
            xrw = (request.headers.get("X-Requested-With") or "").lower()
            return ("application/json" in accept) or (xrw == "xmlhttprequest")
        except Exception:
            return False

    def __call__(self, request):
        # Allow static/media without touching session
        if request.path.startswith("/static/") or request.path.startswith("/media/"):
            return self.get_response(request)

        user = getattr(request, "user", None)
        if not getattr(user, "is_authenticated", False):
            request.active_school = None
            return self.get_response(request)

        # Superusers can access all schools; keep whatever is set.
        if getattr(user, "is_superuser", False):
            # Still resolve the school for downstream reuse.
            try:
                from .models import School as _School
                sid_raw = request.session.get(self.SESSION_KEY)
                if sid_raw:
                    request.active_school = _School.objects.filter(pk=int(sid_raw), is_active=True).first()
                else:
                    request.active_school = None
            except Exception:
                request.active_school = None
            return self.get_response(request)

        try:
            sid_raw = request.session.get(self.SESSION_KEY)
        except Exception:
            sid_raw = None

        if not sid_raw:
            request.active_school = None
            return self.get_response(request)

        try:
            sid = int(sid_raw)
        except (TypeError, ValueError):
            self._clear_active_school(request)
            request.active_school = None
            return self.get_response(request)

        from .models import School, SchoolMembership

        # ── Fetch the school once and cache on request ──
        # تعثّرُ الجلب لا يُقرأ «المدرسة غير موجودة»: الأول عطلٌ عابر والثاني
        # قرارُ منع. وخلطُهما كان يمسح المدرسة النشطة من جلسة المستخدم لأن
        # القاعدة تلعثمت لحظة.
        try:
            school_obj = School.objects.filter(id=sid, is_active=True).first()
        except Exception:
            _degraded("tenant.load_active_school", school_id=sid, user_id=getattr(user, "pk", None))
            request.active_school = None
            return self._deny(request, reason="active_school_lookup_failed")

        # If the school itself is not active/doesn't exist, clear.
        if school_obj is None:
            request.active_school = None
            if self._wants_json(request):
                _log_denial(request, reason="active_school_invalid")
                return JsonResponse({"detail": "invalid_active_school"}, status=403)
            self._clear_active_school(request)
            return self.get_response(request)

        # Normal user: must have an active membership in that school.
        #
        # ── لماذا يُصفَّر ``active_school`` في كل مسار فشل ────────────────────
        # كانت الدالة تنتهي بـ ``request.active_school = school_obj`` **مهما
        # كان مسار الفحص**. فمن لا عضوية له — أو من تعثّر فحص عضويته — كان
        # يُمسح مفتاحُه من الجلسة ثم يُكمل الطلبَ نفسه ومعه ``active_school``
        # لمدرسة ليست له.
        #
        # وهذا يناقض العقد المكتوب في ``config/settings.py``: «ActiveSchoolGuard
        # قد خوّل وأرفق ``request.active_school``» — وعليه بُني إسقاطُ
        # ``SchoolRateLimitMiddleware`` لاستعلامه، وعليه تعتمد عروضٌ تقرأ
        # ``request.active_school`` بوصفه مُخوَّلاً.
        #
        # فالقاعدة الآن واحدة ولا استثناء لها: **لا يُرفَق إلا ما ثبتت عضويته.**
        try:
            membership = SchoolMembership.objects.filter(
                teacher=user,
                school_id=sid,
                is_active=True,
            ).select_related("school").first()
        except Exception:
            _degraded("tenant.membership_check", school_id=sid, user_id=getattr(user, "pk", None))
            request.active_school = None
            return self._deny(request, reason="active_school_membership_exception")

        if membership is None:
            request.active_school = None
            return self._deny(request, reason="active_school_membership_forbidden")

        # Cache membership for SubscriptionMiddleware downstream
        request._active_membership = membership

        # ── Cache the resolved school on the request for downstream reuse ──
        request.active_school = school_obj

        return self.get_response(request)

    def _clear_active_school(self, request) -> None:
        """يزيل المدرسة النشطة من الجلسة دون أن يُسقط الطلب."""
        with soft_fail("tenant.clear_session_school"):
            request.session.pop(self.SESSION_KEY, None)

    def _deny(self, request, *, reason: str):
        """يمنع الوصول للمستأجر: 403 لطلبات JSON، ومسحٌ للجلسة لغيرها.

        ولا يُرفَق ``active_school`` في الحالتين — المُستدعي صفّره قبل النداء.
        """
        _log_denial(request, reason=reason)
        if self._wants_json(request):
            return JsonResponse({"detail": "forbidden"}, status=403)
        self._clear_active_school(request)
        return self.get_response(request)

class SubscriptionMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # 1) تجاوز الفحص للمستخدمين غير المسجلين أو المدراء النظام (Superusers)
        if not request.user.is_authenticated or getattr(request.user, "is_superuser", False):
            return self.get_response(request)

        # 2) السماح بالملفات الثابتة والوسائط
        if request.path.startswith('/static/') or request.path.startswith('/media/'):
            return self.get_response(request)

        # 3) تحديد المسارات المسموح بها عند انتهاء الاشتراك
        #    - للجميع: صفحة انتهاء الاشتراك + تسجيل الخروج
        #    - للمدير فقط: صفحات التجديد/رفع الإيصال
        base_allowed = {
            reverse('reports:logout'),
            reverse('reports:subscription_expired'),
            # السماح بالتبديل حتى لا يعلق المستخدم على مدرسة منتهية
            reverse('reports:switch_school'),
        }

        # 4) جلب المدرسة النشطة (إن وُجدت) ثم عضوية المستخدم داخلها.
        #    هذا مهم لمنع ثغرة: مدير لديه أكثر من مدرسة، يجدد واحدة ثم يبدّل لأخرى منتهية.
        #    عدم وجود اشتراك يُعامل كمنتهي.
        from .models import SchoolMembership, School

        # ── Reuse school from ActiveSchoolGuardMiddleware if available ──
        active_school = getattr(request, "active_school", None)
        if active_school is None:
            try:
                sid = request.session.get("active_school_id")
                if sid:
                    active_school = School.objects.filter(pk=sid, is_active=True).first()
            except Exception:
                active_school = None

        # ── Reuse membership cached by ActiveSchoolGuardMiddleware ──
        membership = getattr(request, "_active_membership", None)
        if membership is not None and active_school is not None:
            # Verify it matches the active school
            if membership.school_id != active_school.pk:
                membership = None

        if membership is None:
            memberships_qs = (
                SchoolMembership.objects.filter(teacher=request.user, is_active=True)
                .select_related('school')
            )
            if active_school is not None:
                membership = memberships_qs.filter(school=active_school).first()
            if membership is None:
                membership = memberships_qs.first()

        # إن لم تكن لديه عضوية مدرسة، لا نطبق هذا المنع (نترك الصلاحيات الأخرى تتعامل)
        if membership is None:
            return self.get_response(request)

        # المدرسة التي سنفحص اشتراكها (المدرسة النشطة إن أمكن وإلا مدرسة العضوية الأولى)
        school = membership.school
        is_manager = membership.role_type == SchoolMembership.RoleType.MANAGER
        allowed_paths = set(base_allowed)
        if is_manager:
            allowed_paths |= {
                reverse('reports:my_subscription'),
                reverse('reports:role_guidance'),
                reverse('reports:school_health'),
                reverse('reports:payment_create'),
                reverse('reports:school_addition_requests'),
                reverse('reports:school_archive'),
                reverse('reports:school_archive_create'),
            }

        # السماح بهذه المسارات دائمًا لتجنب حلقات redirect
        archive_download_prefix = reverse("reports:school_archive").rstrip("/") + "/download/"
        if request.path in allowed_paths or (
            is_manager and request.path.startswith(archive_download_prefix)
        ) or (
            is_manager and _resolved_view_name(request) in SUBSCRIPTION_PAYMENT_FLOW_VIEWS
        ):
            return self.get_response(request)

        # 5) فحص انتهاء الاشتراك/غيابه
        subscription = None
        try:
            subscription = getattr(school, 'subscription', None)
        except Exception:
            subscription = None

        is_expired = True
        try:
            if subscription is not None:
                is_expired = bool(subscription.is_expired)
            else:
                # عدم وجود اشتراك يعني منتهي
                is_expired = True
        except Exception:
            is_expired = True

        if is_expired:
            # لو كان الطلب JSON/AJAX نرجع 403 بدل redirect
            try:
                accept = (request.headers.get("Accept") or "").lower()
                xrw = (request.headers.get("X-Requested-With") or "").lower()
                wants_json = "application/json" in accept or xrw == "xmlhttprequest"
            except Exception:
                wants_json = False
            if wants_json:
                _log_denial(request, reason="subscription_expired")
                return JsonResponse({"detail": "subscription_expired"}, status=403)
            return redirect('reports:subscription_expired')

        return self.get_response(request)


class ForcePasswordChangeMiddleware:
    """Redirect users with the default phone-based password to the profile page."""

    def __init__(self, get_response):
        self.get_response = get_response

    def _wants_json(self, request) -> bool:
        try:
            accept = (request.headers.get("Accept") or "").lower()
            xrw = (request.headers.get("X-Requested-With") or "").lower()
            return ("application/json" in accept) or (xrw == "xmlhttprequest")
        except Exception:
            return False

    def _wants_html(self, request) -> bool:
        try:
            headers = request.headers
            accept = (headers.get("Accept") or "").lower()
            sec_fetch_mode = (headers.get("Sec-Fetch-Mode") or "").lower()
            sec_fetch_dest = (headers.get("Sec-Fetch-Dest") or "").lower()
            if "text/html" in accept:
                return True
            if sec_fetch_mode == "navigate" or sec_fetch_dest == "document":
                return True
        except Exception:
            _degraded("http.detect_navigation_request")
        return False

    def __call__(self, request):
        if request.path.startswith("/static/") or request.path.startswith("/media/"):
            return self.get_response(request)

        user = getattr(request, "user", None)
        if not getattr(user, "is_authenticated", False):
            request.force_password_change_required = False
            return self.get_response(request)

        if not is_force_password_change_required(request):
            return self.get_response(request)

        allowed_names = {
            "reports:my_profile",
            "reports:logout",
            "reports:unread_notifications_count",
            "reports:subscription_expired",
            "reports:switch_school",
            "service_worker",
        }
        allowed_names.update(SUBSCRIPTION_PAYMENT_FLOW_VIEWS)

        full_name = _resolved_view_name(request)

        if full_name in allowed_names:
            return self.get_response(request)

        if self._wants_json(request):
            _log_denial(request, reason="password_change_required")
            return JsonResponse({"detail": "password_change_required"}, status=403)

        if not self._wants_html(request):
            return HttpResponse("password_change_required", status=403, content_type="text/plain")

        messages.warning(
            request,
            "لأمان حسابك، أضف بريدك الإلكتروني وغيّر كلمة المرور الحالية لأنها مطابقة لرقم الجوال.",
        )
        return redirect("reports:my_profile")


class ContentSecurityPolicyMiddleware:
    """Adds a Content Security Policy header in production.

    Notes:
    - Inline scripts use a per-request nonce; a regression test enforces this.
    - Inline styles still require ``unsafe-inline`` until the remaining template
      styles are moved to static stylesheets.
    - External fonts/icons are loaded via Google Fonts + cdnjs.
    - Media assets may be served from signed HTTPS URLs.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def _is_enabled(self) -> bool:
        try:
            if getattr(settings, "ENV", "development") == "production":
                return bool(getattr(settings, "CSP_ENABLED", True))
            return bool(getattr(settings, "CSP_ENABLED", False))
        except Exception:
            return False

    def _policy(self) -> str:
        # Kept for backwards-compat; prefer _policy_for_request
        return ""

    # A hosted checkout is reached by redirecting the payment form to the
    # gateway's own domain. Browsers enforce ``form-action`` across the whole
    # redirect chain, not just the form's own target, so an enabled gateway
    # whose origin is missing here has its checkout silently blocked by the
    # browser: the order is created server-side and the user never leaves the
    # page. Every gateway that can be switched on must be listed.
    PAYMENT_CHECKOUT_ORIGINS = (
        ("MOYASAR_ENABLED", "https://checkout.moyasar.com"),
        ("TAMARA_ENABLED", "https://checkout.tamara.co"),
    )

    @classmethod
    def _enabled_checkout_origins(cls) -> list[str]:
        return [
            origin
            for setting_name, origin in cls.PAYMENT_CHECKOUT_ORIGINS
            if bool(getattr(settings, setting_name, False))
        ]

    def _policy_for_request(self, request) -> str:
        sbc_seal_origin = "https://eauthenticate.saudibusiness.gov.sa"
        checkout_origins = self._enabled_checkout_origins()
        is_landing_page = getattr(request, "path", "") == "/"

        # Allow override via env/settings for emergency tweaks.
        # If you provide a custom policy, you may include "{nonce}" placeholder.
        custom = (getattr(settings, "CONTENT_SECURITY_POLICY", "") or "").strip()
        if custom:
            nonce = getattr(request, "csp_nonce", "")
            policy = custom.replace("{nonce}", nonce)
            nonce_source = f"'nonce-{nonce}'"
            directives = []
            script_directives = {"script-src", "script-src-elem"}
            seen_script_directives: set[str] = set()
            seen_frame_src = False
            seen_form_action = False

            for raw_directive in policy.split(";"):
                parts = raw_directive.strip().split()
                if not parts:
                    continue
                directive_name = parts[0].lower()
                if directive_name in script_directives:
                    seen_script_directives.add(directive_name)
                    sources = [
                        source
                        for source in parts[1:]
                        if source != "'none'"
                    ]
                    if nonce_source not in sources:
                        sources.append(nonce_source)
                    if is_landing_page and sbc_seal_origin not in sources:
                        sources.append(sbc_seal_origin)
                    parts = [parts[0], *sources]
                elif directive_name == "frame-src":
                    seen_frame_src = True
                    if is_landing_page and sbc_seal_origin not in parts[1:]:
                        parts.append(sbc_seal_origin)
                elif directive_name == "form-action":
                    seen_form_action = True
                    for origin in checkout_origins:
                        if origin not in parts[1:]:
                            parts.append(origin)
                directives.append(" ".join(parts))

            for directive_name in script_directives - seen_script_directives:
                sources = ["'self'", nonce_source]
                if is_landing_page:
                    sources.append(sbc_seal_origin)
                directives.append(f"{directive_name} {' '.join(sources)}")
            if is_landing_page and not seen_frame_src:
                directives.append(f"frame-src 'self' {sbc_seal_origin}")
            if checkout_origins and not seen_form_action:
                directives.append(
                    "form-action 'self' " + " ".join(checkout_origins)
                )
            return "; ".join(directives)

        nonce = getattr(request, "csp_nonce", "")
        seal_script_source = f" {sbc_seal_origin}" if is_landing_page else ""
        frame_src = "frame-src 'self'"
        form_action = "form-action 'self'"
        if is_landing_page:
            frame_src = f"{frame_src} {sbc_seal_origin}"
        if checkout_origins:
            form_action = f"{form_action} " + " ".join(checkout_origins)

        # Default policy: safe baseline.
        #
        # ── ``style-src`` أُغلقت ─────────────────────────────────────────────
        # كانت تحمل ``'unsafe-inline'`` سنواتٍ، وسببُها مكتوبٌ هنا: «القوالب
        # تستعمل ``style="..."``». وكان في القوالب 806 سمة، فالإذن باقٍ ما بقيت
        # واحدة — لأن الإذن مفتوحٌ أو مغلق، لا درجة بينهما. ومعناه أن أي حقن
        # HTML (حقلٌ لم يُهرَّب، أو رسالة خطأ تُردّد مدخل المستخدم) يستطيع حقن
        # أنماط: إخفاءُ زرٍّ حقيقي، أو رسمُ نموذج دخولٍ فوق الصفحة.
        #
        # وقد أُزيلت السمات كلها على أربع دفعات:
        #   * ما له مقابل مباشر → أصناف ``static/css/utilities.css``.
        #   * ما كان تركيبة إعلانات → قائمة أصناف (كلٌّ أو لا شيء لكل سمة).
        #   * نِسَبُ أشرطة التقدّم → ``data-progress`` يقرؤها الجافاسكربت ويضبط
        #     العرض عبر CSSOM، ومسارُ CSSOM لا يحكمه ``style-src``.
        #   * الباقي الفريد لكل صفحة → ``static/css/extracted.css``.
        #
        # وكل عنصر ``<style>`` في القوالب صار يحمل ``nonce`` — وهو ما يجعل
        # الإغلاق ممكناً أصلاً، إذ أن الـ nonce يغطّي العنصر ولا يغطّي السمة.
        #
        # يحرس ذلك ``reports/tests/test_inline_style_budget.py``: أي سمة جديدة
        # تُفشل الاختبار قبل أن تصل الإنتاج وتكسر الصفحة.
        #
        # والنطاقات الخارجية زالت كذلك: الخط صار محلياً، وFont Awesome كان
        # مُستضافاً محلياً أصلاً بينما تُحمّله بعض القوالب من cdnjs بلا سبب.
        base = [
            "default-src 'self'",
            "base-uri 'self'",
            form_action,
            "object-src 'none'",
            "frame-ancestors 'none'",
            # لا نطاق CDN عام هنا. ``cdn.jsdelivr.net`` يخدم كل حزمة npm وكل
            # مستودع GitHub، فإدراجه يحوّل ``script-src`` من حصرٍ على ما تملكه
            # المنصة إلى إذنٍ بأي سكربت منشور — وأي بدائية حقن مستقبلية تتخطى
            # الـ nonce عبره. وChart.js صار مُستضافاً في ``static/js/vendor/``.
            f"script-src 'self' 'nonce-{nonce}'{seal_script_source}",
            f"script-src-elem 'self' 'nonce-{nonce}'{seal_script_source}",
            f"style-src 'self' 'nonce-{nonce}'",
            f"style-src-elem 'self' 'nonce-{nonce}'",
            # ``style-src-attr 'none'`` صريحةً: المتصفّحات التي تدعمها تمنع
            # سمة ``style`` نهائياً حتى لو عاد ``'unsafe-inline'`` سهواً إلى
            # ``style-src``. حزامٌ فوق الحمّالة.
            "style-src-attr 'none'",
            "font-src 'self' data:",
            "img-src 'self' data: blob: https:",
            # التسجيل الصوتي يُراجَع قبل إرساله، وعنصر ``audio`` يقرأه من
            # ``blob:`` في الذاكرة. وبلا هذا التوجيه يرثه من ``default-src``
            # فيُحجب الاستماع ويبقى الزرّ بلا وظيفة.
            "media-src 'self' blob:",
            "connect-src 'self'",
            frame_src,
            "upgrade-insecure-requests",
        ]
        return "; ".join(base)

    @staticmethod
    def _admin_policy() -> str:
        """CSP compatible with Django admin's remaining inline assets."""
        return "; ".join([
            "default-src 'self'",
            "base-uri 'self'",
            "form-action 'self'",
            "object-src 'none'",
            "frame-ancestors 'none'",
            "script-src 'self' 'unsafe-inline'",
            "style-src 'self' 'unsafe-inline'",
            "font-src 'self' data:",
            "img-src 'self' data:",
            "connect-src 'self'",
            "upgrade-insecure-requests",
        ])

    def __call__(self, request):
        # Generate per-request nonce early (so templates can use it)
        try:
            request.csp_nonce = secrets.token_urlsafe(16)
        except Exception:
            request.csp_nonce = ""

        response = self.get_response(request)

        if not self._is_enabled():
            return response

        # Avoid spending time on static/media responses
        with soft_fail("csp.skip_static_paths"):
            if request.path.startswith("/static/") or request.path.startswith("/media/"):
                return response

        header_name = "Content-Security-Policy"
        with soft_fail("csp.read_report_only_flag"):
            if bool(getattr(settings, "CSP_REPORT_ONLY", False)):
                header_name = "Content-Security-Policy-Report-Only"

        # Don't override if already set by upstream/proxy
        if header_name not in response:
            try:
                is_admin = request.path.startswith("/admin-panel/")
            except Exception:
                is_admin = False
            response[header_name] = self._admin_policy() if is_admin else self._policy_for_request(request)

        return response
