# reports/views/auth.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import re
import logging
from typing import Any

import cbor2
from django.conf import settings
from django.contrib.auth import views as auth_views
from django.db import IntegrityError
from django.db.models import Q
from django.urls import reverse_lazy
from django.utils import timezone
from django.utils.decorators import method_decorator
from django_ratelimit.decorators import ratelimit

from core.observability import report_degraded as _degraded, soft_call, soft_fail

from ._helpers import *
from ._helpers import (
    _safe_next_url, _set_active_school,
    _get_active_school, _user_schools,
)
from ..webauthn import (
    b64url_decode,
    b64url_encode,
    credential_hash,
    json_body,
    origin_from_request,
    parse_authenticator_data,
    parse_client_data,
    random_challenge,
    rp_id_from_request,
    verify_signature,
)
from ..middleware import (
    clear_force_password_change_flag,
    is_force_password_change_required,
)
from ..marketing_attribution import capture_marketing_attribution
from ..models import DepartmentMembership, SchoolMembership, TeacherTotpDevice, WebAuthnCredential
from ..forms import AccountPasswordResetForm, AccountSetPasswordForm
from ..email_identity import format_system_from_email
from ..staff_workspace import build_staff_workspaces
from ..pricing import (
    DEFAULT_SERVICE_PRICING,
    FREE_TRIAL_DAYS,
    SUBSCRIPTION_ADDON_NOTES,
    SUBSCRIPTION_INCLUDED_FEATURES,
)
from ..flexible_pricing import (
    build_flexible_pricing_catalog,
    serialize_flexible_pricing_catalog,
)
from ..moyasar_gateway import is_enabled as moyasar_is_enabled
from core import opmetrics
from core.limits_cache import limits_cache


logger = logging.getLogger(__name__)

WEBAUTHN_REGISTER_CHALLENGE_SESSION_KEY = "_webauthn_register_challenge"
WEBAUTHN_AUTH_CHALLENGE_SESSION_KEY = "_webauthn_auth_challenge"
WEBAUTHN_AUTH_ALLOWED_CREDENTIALS_SESSION_KEY = "_webauthn_auth_allowed_credentials"
WEBAUTHN_AUTH_DISCOVERABLE_SESSION_KEY = "_webauthn_auth_discoverable"
PASSKEY_ENROLL_PROMPT_SESSION_KEY = "passkey_enroll_prompt"
# A temporary reminder is device-specific. A permanent decline is stored on
# the account so it also survives a browser/device change.
PASSKEY_PROMPT_SNOOZE_COOKIE = "pk_offer_snooze"
PASSKEY_PROMPT_SNOOZE_MAX_AGE = 60 * 60 * 24 * 90
PASSKEY_UNSUPPORTED_DEVICE_COOKIE = "pk_device_unsupported"
PASSKEY_UNSUPPORTED_DEVICE_MAX_AGE = 60 * 60 * 24 * 365


def _force_password_change_notice() -> str:
    return (
        "لحماية حسابك وبيانات المدرسة، أضف بريدك الإلكتروني وغيّر كلمة المرور الحالية الآن "
        "لأنها ما زالت مطابقة لرقم الجوال."
    )


def _passkey_response(ok: bool, *, status: int = 200, **payload: Any) -> JsonResponse:
    payload["ok"] = ok
    return JsonResponse(payload, status=status, json_dumps_params={"ensure_ascii": False})


def _passkey_rate_limited(request: HttpRequest) -> JsonResponse | None:
    """Turn a tripped rate limit into JSON.

    These endpoints are called by fetch(), so the default HTML 403 page would
    surface to the user as an unreadable parse error instead of a message.
    """
    if getattr(request, "limited", False):
        return _passkey_response(
            False,
            status=429,
            error="rate_limited",
            message="محاولات كثيرة خلال وقت قصير. انتظر دقيقة ثم أعد المحاولة.",
        )
    return None


_PASSKEY_PLATFORM_LABELS = (
    ("iphone", "آيفون"),
    ("ipad", "آيباد"),
    ("ipod", "آيبود"),
    ("android", "جهاز أندرويد"),
    ("cros", "كروم بوك"),
    ("macintosh", "ماك"),
    ("mac os", "ماك"),
    ("windows", "ويندوز"),
    ("linux", "لينكس"),
)

_PASSKEY_BROWSER_LABELS = (
    ("edg", "Edge"),
    ("samsungbrowser", "Samsung Internet"),
    ("opr", "Opera"),
    ("firefox", "Firefox"),
    ("chrome", "Chrome"),
    ("safari", "Safari"),
)

# Older builds sent the same placeholder for every device, which made the list
# of enabled devices useless for deciding which one to revoke.
_PASSKEY_GENERIC_NAMES = {"جوال المستخدم", "جهاز المستخدم", "جهاز مفعل", "جهاز"}


def _passkey_device_label(request: HttpRequest, provided: str = "") -> str:
    """Name a credential after the device that created it."""
    provided = (provided or "").strip()[:120]
    if provided and provided not in _PASSKEY_GENERIC_NAMES:
        return provided

    agent = (request.META.get("HTTP_USER_AGENT") or "").lower()
    platform = next((label for token, label in _PASSKEY_PLATFORM_LABELS if token in agent), "")
    browser = ""
    for token, label in _PASSKEY_BROWSER_LABELS:
        if token in agent:
            # Every Chromium browser also claims "chrome"/"safari"; first match wins.
            browser = label
            break

    if platform and browser:
        return f"{platform} · {browser}"
    return platform or browser or "جهاز مفعّل"


def _password_reset_validity_text() -> str:
    """Say how long the link lives, in words, from the configured timeout.

    The email states a duration; leaving it hardcoded means it silently lies
    the day someone changes ``PASSWORD_RESET_TIMEOUT``.
    """
    try:
        seconds = int(getattr(settings, "PASSWORD_RESET_TIMEOUT", 3600) or 3600)
    except (TypeError, ValueError):
        seconds = 3600

    if seconds < 3600:
        minutes = max(1, seconds // 60)
        if minutes == 1:
            return "دقيقة واحدة"
        if minutes == 2:
            return "دقيقتين"
        return f"{minutes} دقيقة" if minutes > 10 else f"{minutes} دقائق"

    hours = seconds // 3600
    if hours == 1:
        return "ساعة واحدة"
    if hours == 2:
        return "ساعتين"
    if hours <= 10:
        return f"{hours} ساعات"
    days = hours // 24
    if days >= 1:
        return "يومًا واحدًا" if days == 1 else f"{days} أيام"
    return f"{hours} ساعة"


@method_decorator(
    ratelimit(key="ip", rate="5/10m", method="POST", block=True),
    name="dispatch",
)
class AccountPasswordResetView(auth_views.PasswordResetView):
    template_name = "reports/password_reset_form.html"
    email_template_name = "reports/emails/password_reset_email.txt"
    html_email_template_name = "reports/emails/password_reset_email.html"
    subject_template_name = "reports/emails/password_reset_subject.txt"
    form_class = AccountPasswordResetForm
    success_url = reverse_lazy("reports:password_reset_done")
    # Drives the progress rail in password_reset_base.html.
    extra_context = {"recovery_step": 1}

    @property
    def from_email(self) -> str:
        return format_system_from_email()

    @property
    def extra_email_context(self) -> dict[str, str]:
        """Brand the recovery email — Django's default context has no identity."""
        return {
            "platform_name": "منصة توثيق",
            "logo_url": self._brand_logo_url(),
            "expiry_text": _password_reset_validity_text(),
            "support_email": (
                getattr(settings, "SECURITY_CONTACT_EMAIL", "") or ""
            ).strip(),
        }

    def _brand_logo_url(self) -> str:
        """Absolute logo URL, or "" so the header renders without it.

        Inboxes block remote images by default anyway, so this is decoration:
        never let a missing SITE_URL or a broken static path raise inside a
        password-recovery request.
        """
        try:
            from django.templatetags.static import static

            path = static("img/logo1.png")
            site_url = (getattr(settings, "SITE_URL", "") or "").strip().rstrip("/")
            if site_url:
                return f"{site_url}{path}"
            return self.request.build_absolute_uri(path)
        except Exception:
            return ""


class AccountPasswordResetDoneView(auth_views.PasswordResetDoneView):
    template_name = "reports/password_reset_done.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["recovery_step"] = 2
        # The page tells the visitor how long the link lives; read it from the
        # same setting the email does so the two can never disagree.
        context["expiry_text"] = _password_reset_validity_text()
        return context


class AccountPasswordResetConfirmView(auth_views.PasswordResetConfirmView):
    template_name = "reports/password_reset_confirm.html"
    form_class = AccountSetPasswordForm
    success_url = reverse_lazy("reports:password_reset_complete")
    extra_context = {"recovery_step": 3}


class AccountPasswordResetCompleteView(auth_views.PasswordResetCompleteView):
    template_name = "reports/password_reset_complete.html"
    extra_context = {"recovery_step": 4}


def _offer_passkey_enrollment(request: HttpRequest, user: Teacher) -> None:
    """Show the optional passkey prompt after a successful password login.

    The invitation itself lives only in account security settings. Temporary
    choices are device-specific cookies, while a permanent decline follows the
    account across every browser and device.
    """
    try:
        if getattr(user, "passkey_prompt_opt_out", False) or request.COOKIES.get(
            PASSKEY_PROMPT_SNOOZE_COOKIE
        ) or request.COOKIES.get(PASSKEY_UNSUPPORTED_DEVICE_COOKIE):
            request.session.pop(PASSKEY_ENROLL_PROMPT_SESSION_KEY, None)
            return

        has_passkey = WebAuthnCredential.objects.filter(
            teacher=user,
            is_active=True,
        ).exists()
        if has_passkey:
            request.session.pop(PASSKEY_ENROLL_PROMPT_SESSION_KEY, None)
        else:
            request.session[PASSKEY_ENROLL_PROMPT_SESSION_KEY] = True
    except Exception:
        # Passkey enrollment is an optional enhancement and must never block login.
        request.session.pop(PASSKEY_ENROLL_PROMPT_SESSION_KEY, None)


def _default_login_redirect_name(user, *, active_school=None) -> str:
    """Return the landing page for the role held in the active context.

    A user may manage one school and teach in another. Treating ``manager`` as
    an account-wide flag strands that user whenever the active school is the
    teaching school. With an active school, the role in that school is therefore
    authoritative. The account-wide check remains only for the no-context case
    so a manager is still guided to school selection.
    """
    if getattr(user, "is_superuser", False):
        return "reports:platform_admin_dashboard"
    if active_school is not None:
        if is_school_manager(user, active_school=active_school):
            return "reports:admin_dashboard"
        # A school membership in the active context wins over an additional
        # group-level role (for example, an executive director who also teaches).
        if SchoolMembership.objects.filter(
            school=active_school,
            teacher=user,
            is_active=True,
        ).exists():
            return "reports:home"
    elif is_school_manager(user):
        return "reports:admin_dashboard"
    # المدير التنفيذي يُسأل عنه بعد الإدارة المدرسية لا قبلها: من جمع الصفتين
    # يبقى مديراً في مدرسته. وقبل هذا الشرط كان يهبط على لوحة المعلّم — صفحةٌ
    # لا تخصّه ولا تعرض من مجموعته شيئاً.
    if is_executive_director(user):
        return "reports:executive_dashboard"
    return "reports:home"


def _is_owner_only_path(value: str | None) -> bool:
    path = (value or "").split("?", 1)[0].rstrip("/") or "/"
    return path in {"/platform-dashboard", "/platform/settings"}


def _complete_passkey_login(request: HttpRequest, user: Teacher, *, next_url: str | None = None) -> JsonResponse:
    if not getattr(user, "is_active", False):
        return _passkey_response(False, status=403, error="inactive_user", message="عذرًا، حسابك موقوف.")

    if not getattr(user, "is_superuser", False):
        memberships = (
            SchoolMembership.objects.filter(teacher=user, is_active=True)
            .select_related("school", "school__subscription")
            .order_by("id")
        )

        if memberships.exists():
            active_school = None
            active_manager_school = None
            any_active_subscription = False
            is_any_manager = False
            manager_school = None
            first_school_name = None

            for m in memberships:
                if first_school_name is None:
                    first_school_name = getattr(getattr(m, "school", None), "name", None)
                if m.role_type == SchoolMembership.RoleType.MANAGER:
                    is_any_manager = True
                    if manager_school is None:
                        manager_school = m.school

                sub = None
                try:
                    sub = getattr(m.school, "subscription", None)
                except Exception:
                    sub = None

                if sub is not None and not bool(sub.is_expired) and bool(getattr(m.school, "is_active", True)):
                    any_active_subscription = True
                    if active_school is None:
                        active_school = m.school
                    if (
                        m.role_type == SchoolMembership.RoleType.MANAGER
                        and active_manager_school is None
                    ):
                        active_manager_school = m.school

            if not any_active_subscription:
                if is_any_manager and manager_school is not None:
                    login(request, user)
                    is_force_password_change_required(request)
                    _set_active_school(request, manager_school)
                    return _passkey_response(True, redirect=reverse("reports:subscription_expired"))

                school_label = f" ({first_school_name})" if first_school_name else ""
                return _passkey_response(
                    False,
                    status=403,
                    error="subscription_expired",
                    message=f"عذرًا، اشتراك المدرسة{school_label} منتهي. لا يمكن الدخول حتى يتم تجديد الاشتراك.",
                )

            active_school = active_manager_school or active_school
            login(request, user)
            if active_school is not None:
                _set_active_school(request, active_school)
        else:
            login(request, user)
            messages.warning(request, "تنبيه: حسابك غير مرتبط بمدرسة فعّالة. تواصل مع إدارة النظام لربط الحساب بالمدرسة.")
    else:
        login(request, user)

    try:
        schools = _user_schools(user)
        if len(schools) == 1:
            _set_active_school(request, schools[0])
        elif user.is_superuser:
            qs = School.objects.filter(is_active=True)
            if qs.count() == 1:
                s = qs.first()
                if s is not None:
                    _set_active_school(request, s)
    except Exception:
        # بلا مدرسة نشطة يُردّ المستخدم من كل شاشة تسأل عنها — يبدو كأن
        # عضويته أُلغيت.
        _degraded("auth.autoselect_active_school", user_id=getattr(user, "pk", None))

    if is_force_password_change_required(request):
        messages.warning(request, _force_password_change_notice())
        return _passkey_response(True, redirect=reverse("reports:my_profile"))

    safe_next = _safe_next_url(next_url)
    if safe_next and _is_owner_only_path(safe_next) and not getattr(user, "is_superuser", False):
        return _passkey_response(
            False,
            status=403,
            error="admin_only",
            message="هذا الدخول خاص بمدير النظام فقط.",
        )
    return _passkey_response(
        True,
        redirect=safe_next
        or reverse(
            _default_login_redirect_name(
                user,
                active_school=_get_active_school(request),
            )
        ),
    )


def _landing_duration_label(days: int) -> str:
    days = int(days or 0)
    if days <= 0:
        return "مدة مرنة"
    if days % 365 == 0:
        years = days // 365
        if years == 1:
            return "لمدة سنة"
        if years == 2:
            return "لمدة سنتين"
        if years <= 10:
            return f"لمدة {years} سنوات"
        return f"لمدة {years} سنة"
    if days % 30 == 0:
        months = days // 30
        if months == 1:
            return "لمدة شهر"
        if months == 2:
            return "لمدة شهرين"
        if months <= 10:
            return f"لمدة {months} أشهر"
        return f"لمدة {months} شهر"
    if days == 1:
        return "لمدة يوم"
    if days == 2:
        return "لمدة يومين"
    if days <= 10:
        return f"لمدة {days} أيام"
    return f"لمدة {days} يوم"


def _landing_default_features(is_trial: bool) -> list[str]:
    if is_trial:
        return [
            "تفعيل مباشر من صفحة التسجيل",
            "تجربة حقيقية للتقارير وملفات الإنجاز وروابط المشاركة",
            "بدء سريع قبل اتخاذ قرار التفعيل",
        ]
    return [
        "إدارة التقارير والتذاكر والتعاميم من مكان واحد",
        "ملفات إنجاز للمعلمين مع PDF وشواهد منظمة",
        "روابط مشاركة مؤقتة بصلاحية محددة للتقارير والإنجاز",
    ]


def _landing_parse_features(description: str, is_trial: bool) -> list[str]:
    text = (description or "").replace("\r", "").strip()
    if not text:
        return _landing_default_features(is_trial)

    raw_parts = []
    for line in text.split("\n"):
        cleaned = re.sub(r"^[\s\-\*\u2022\u25aa\u25cf\u2023]+", "", line or "").strip()
        if cleaned:
            raw_parts.append(cleaned)

    if len(raw_parts) <= 1:
        split_parts = [p.strip() for p in re.split(r"[؛\n]+", text) if p.strip()]
        if split_parts:
            raw_parts = split_parts

    unique_parts: list[str] = []
    seen = set()
    for item in raw_parts:
        key = item.strip().lower()
        if key and key not in seen:
            seen.add(key)
            unique_parts.append(item.strip())

    if not unique_parts:
        unique_parts = _landing_default_features(is_trial)

    defaults = _landing_default_features(is_trial)
    for fallback in defaults:
        if len(unique_parts) >= 3:
            break
        if fallback not in unique_parts:
            unique_parts.append(fallback)

    return unique_parts[:3]


def _landing_fit_text(capacity: int, is_trial: bool, is_unlimited: bool) -> str:
    if is_trial:
        return "مناسبة لاختبار المنتج داخل المدرسة قبل التوسع"
    if is_unlimited:
        return "مناسبة للتشغيل الموسع مع سعة مستخدمين مرنة"
    if capacity <= 25:
        return "مناسبة للمدارس الصغيرة أو فرق الإدارة المحدودة"
    if capacity <= 50:
        return "الأنسب لغالبية المدارس عند التشغيل الكامل"
    if capacity <= 75:
        return "مناسبة للمدارس الأكبر أو الفرق متعددة الأدوار"
    return "مناسبة للتشغيل الواسع داخل المدرسة"


def _landing_segment_label(users: int) -> str:
    if users <= 25:
        return "فريق صغير"
    if users <= 50:
        return "مدرسة متوسطة"
    if users <= 75:
        return "مدرسة كبيرة"
    return "تشغيل موسع"


def _landing_period_key(days: int, is_trial: bool) -> str | None:
    if is_trial:
        return "trial"
    days = int(days or 0)
    if days >= 300:
        return "1y"
    if days >= 45:
        return "6m"
    if days >= 20:
        return "1m"
    return None


def _landing_card_title(capacity: int, is_unlimited: bool) -> str:
    if is_unlimited:
        return "باقة مخصصة"
    if capacity <= 0:
        return "باقة مخصصة"
    if capacity <= 25:
        return "انطلاقة"
    if capacity <= 50:
        return "تشغيل"
    if capacity <= 100:
        return "قيادة"
    return "باقة تشغيل موسعة"


# ─────────────────────────────────────────────────────────────────────────────
# خنق المحاولات على مستوى الحساب
# ─────────────────────────────────────────────────────────────────────────────
# حدُّ الـ IP وحده لا يحمي حساباً بعينه: حشو بيانات الاعتماد يأتي من آلاف
# العناوين، فيصيب كل عنوانٍ عشر محاولات في الدقيقة دون أن يلمس الحدَّ، ويجرّب
# على الحساب نفسه آلافاً في الساعة. فالعدّاد الثاني يُمسك بما يفلت من الأول:
# مفتاحه المُعرِّف لا العنوان.
#
# ولا يُستعمل وحده أيضاً: عدّادٌ بالمعرِّف يمكّن مهاجماً من إقفال حساب غيره
# عمداً (denial of service). ولذلك النافذة قصيرة والتهدئة قصيرة — تُبطئ
# التخمين إلى ما لا يُجدي دون أن تحرم صاحب الحساب من العودة بعد دقائق.
LOGIN_ACCOUNT_MAX_FAILURES = 8
LOGIN_ACCOUNT_WINDOW_SECONDS = 15 * 60
LOGIN_ACCOUNT_LOCKOUT_SECONDS = 15 * 60


def _login_throttle_key(identifier: str) -> str:
    """مفتاح ثابت للمعرِّف مهما اختلفت صيغته المكتوبة.

    يُجزَّأ لأن المعرِّف رقم جوال أو هوية — بيانات شخصية لا توضع مفتاحاً في
    Redis مشترك، ولأن التجزئة تحدّ طول المفتاح مهما أرسل المهاجم.
    """
    import hashlib

    normalized = (identifier or "").strip().lower().lstrip("+")
    return "login:fail:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


def _identifier_for_log(identifier: str) -> str:
    """معرِّف صالح للربط في التحقيق، غير قابل للعكس إلى بيانات شخصية.

    المعرِّف رقم جوال أو رقم هوية وطنية. وسجلّات التطبيق تُقرأ من دائرةٍ أوسع
    ممن يقرأ قاعدة البيانات — فكتابته خاماً تُخرج البيانات الشخصية من ضوابطها
    إلى ``stdout`` الحاوية وإلى كل نظام تجميع سجلات خلفها. والتجزئة تكفي
    للربط بين محاولات المهاجم الواحد، وهي الغرض الوحيد من التسجيل هنا.
    """
    return _login_throttle_key(identifier)[-12:] if identifier else "-"


def _login_account_locked(identifier: str) -> bool:
    """هل تجاوز هذا المعرِّف حدَّ المحاولات الفاشلة؟

    **يفشل مغلقاً.** تعذُّر قراءة العدّاد لا يعني «لم يتجاوز»، بل يعني «لا
    نعرف» — والفرق حاسم. وحدُّ الـ IP لا يسدّ الفجوة: المهاجم الموزَّع يبقى
    تحته وهو يجرّب على الحساب الواحد بلا سقف، وهو بالضبط ما وُجد هذا العدّاد
    ليمنعه.

    وخطورة الفشل المفتوح هنا أنه صامت ومتزامن مع الضغط: سياسة Redis
    ``volatile-lru`` تُخلي مفاتيح العدّادات — وهي وحدها الحاملة لـ TTL —
    فتختفي الحماية بلا استثناء يُرفع ولا سطر يُكتب، في اللحظة التي تلزم فيها.

    والمقايضة مقصودة: سقوط مخزن الحدود يمنع الدخول. ولذلك يجب أن يقترن هذا
    بمخزن حدود مستقل بسياسة ``noeviction`` وبتنبيه على العدّاد أدناه، وإلا
    صارت طبقة تشديدٍ سبباً في تعطّل.
    """
    if not identifier:
        return False
    try:
        store = limits_cache()
        return int(store.get(_login_throttle_key(identifier)) or 0) >= LOGIN_ACCOUNT_MAX_FAILURES
    except Exception:
        logger.error(
            "Login throttle store unavailable — failing closed identifier_hash=%s",
            _identifier_for_log(identifier),
            exc_info=True,
        )
        opmetrics.increment("auth.login.throttle_store_unavailable")
        return bool(getattr(settings, "LOGIN_THROTTLE_FAIL_CLOSED", True))


def _register_login_failure(identifier: str) -> None:
    if not identifier:
        return
    key = _login_throttle_key(identifier)
    store = limits_cache()
    try:
        if store.add(key, 1, timeout=LOGIN_ACCOUNT_WINDOW_SECONDS):
            return
        count = int(store.incr(key))
        if count == LOGIN_ACCOUNT_MAX_FAILURES:
            # عند بلوغ الحدّ تُمدَّد المهلة لتصير تهدئة كاملة لا بقية نافذة.
            store.touch(key, LOGIN_ACCOUNT_LOCKOUT_SECONDS)
            logger.warning("Login throttle engaged for identifier hash=%s", key[-12:])
    except Exception:
        # تعذُّر **التسجيل** لا يُفشل الطلب: القراءة في ``_login_account_locked``
        # هي الحارس، وهي وحدها التي تفشل مغلقة.
        logger.error("Login throttle store unavailable on write", exc_info=True)
        opmetrics.increment("auth.login.throttle_store_unavailable")


def _clear_login_failures(identifier: str) -> None:
    if not identifier:
        return
    try:
        limits_cache().delete(_login_throttle_key(identifier))
    except Exception:
        # عدّادُ إخفاقاتٍ لا يُمسح بعد دخولٍ ناجح يُبقي الحساب يقترب من القفل
        # بلا سبب. ولا يجوز أن يُسقط الدخول — لكنه يجب أن يُرى.
        _degraded("auth.clear_login_failures")


def _resolve_login_candidate(identifier: str):
    """يجد الحساب المقصود باستعلام واحد، دون تجربة كلمة المرور مرة لكل صيغة.

    كان الدخول يستدعي ``authenticate`` مرةً لكل صيغة محتملة لرقم الجوال ثم مرةً
    للهوية — حتى خمس عمليات تجزئة لكلمة المرور في الطلب الواحد. وتجزئة كلمة
    المرور مكلفة عمداً، فكان كل طلب فاشل يشتري من المعالج خمسة أضعاف ما يشتريه
    طلبٌ صحيح: تضخيمٌ يحوّل حدَّ العشر محاولات في الدقيقة إلى خمسين تجزئة.

    الصيغ تُحسم بالاستعلام — وهو رخيص ومفهرس — والتجزئة تقع مرة واحدة على
    الحساب الذي عُثر عليه.
    """
    identifier = (identifier or "").strip()
    if not identifier:
        return None

    attempts: list[str] = [identifier]
    ident_no_plus = identifier.lstrip("+")
    if ident_no_plus != identifier:
        attempts.append(ident_no_plus)
    if identifier.isdigit() and len(identifier) == 9:
        attempts.append("0" + identifier)
    if ident_no_plus.isdigit() and ident_no_plus.startswith("966") and len(ident_no_plus) >= 12:
        # +9665XXXXXXXX -> 05XXXXXXXX
        attempts.append("0" + ident_no_plus[-9:])
    attempts = list(dict.fromkeys([item for item in attempts if item]))

    try:
        candidate = Teacher.objects.filter(
            Q(phone__in=attempts) | Q(national_id=identifier)
        ).order_by("id").first()
    except Exception:
        logger.exception("Login candidate lookup failed")
        candidate = None
    return candidate


@ratelimit(key="ip", rate="10/m", method="POST", block=True)
@never_cache
@cache_control(no_cache=True, must_revalidate=True, no_store=True, max_age=0)
@require_http_methods(["GET", "POST"])
def login_view(request: HttpRequest, admin_only: bool = False) -> HttpResponse:
    default_next = reverse("reports:platform_admin_dashboard") if admin_only else ""
    next_value = _safe_next_url(
        request.POST.get("next")
        or request.GET.get("next")
        or default_next
    )

    if request.user.is_authenticated:
        if admin_only and not getattr(request.user, "is_superuser", False):
            logout(request)
            messages.error(request, "هذا الدخول خاص بمدير النظام فقط. سجّل الدخول بحساب السوبر آدمن.")
            return redirect("reports:platform_login")
        if is_force_password_change_required(request):
            return redirect("reports:my_profile")
        # وجهة الهبوط تُقرَّر من دالة واحدة: أربع نسخ من الاشتراط نفسها كانت
        # تعني أن إضافة دور جديد تُنسى في ثلاث منها.
        return redirect(
            _default_login_redirect_name(
                request.user,
                active_school=_get_active_school(request),
            )
        )

    if request.method == "POST":
        identifier = (
            request.POST.get("phone")
            or request.POST.get("username")
            or request.POST.get("identifier")
            or ""
        ).strip()
        password = request.POST.get("password") or ""

        # يدعم تسجيل الدخول عبر رقم الجوال (USERNAME_FIELD) أو رقم الهوية، مع
        # تطبيع صيغ الجوال الشائعة. الحساب يُحسم باستعلام واحد ثم تُجرَّب كلمة
        # المرور مرة واحدة — راجع ``_resolve_login_candidate``.
        if _login_account_locked(identifier):
            logger.warning(
                "Login blocked by account throttle identifier_hash=%s trace_id=%s",
                _identifier_for_log(identifier),
                getattr(request, "trace_id", None),
            )
            opmetrics.increment("auth.login.throttled")
            messages.error(
                request,
                "تم إيقاف محاولات الدخول لهذا الحساب مؤقتاً بعد عدة محاولات فاشلة. "
                "حاول بعد ربع ساعة أو استعد كلمة المرور.",
            )
            return redirect("reports:platform_login" if admin_only else "reports:login")

        potential_user = _resolve_login_candidate(identifier)

        user = None
        if potential_user is not None and getattr(potential_user, "phone", None):
            user = authenticate(request, username=potential_user.phone, password=password)
        elif identifier:
            # لا حساب مطابق: نستدعي المصادقة بمعرِّف لا وجود له عمداً كي يبقى
            # زمن الرد مشابهاً لزمن كلمة مرور خاطئة (ModelBackend يجزّئ تجزئة
            # وهمية في هذه الحالة). بدونها يصير فرق التوقيت كاشفاً للحسابات.
            authenticate(request, username=identifier, password=password)

        if user is not None:
            # كلمة المرور صحيحة: يسقط عدّاد الإخفاق كي لا يُقفل صاحب الحساب
            # نفسه بمحاولاته الخاطئة قبل أن يتذكّرها.
            _clear_login_failures(identifier)

            if admin_only and not getattr(user, "is_superuser", False):
                logger.warning(
                    "Admin login rejected non-superuser user_id=%s identifier_hash=%s trace_id=%s",
                    getattr(user, "id", None),
                    _identifier_for_log(identifier),
                    getattr(request, "trace_id", None),
                )
                opmetrics.increment("auth.login.failure")
                messages.error(request, "هذا الدخول خاص بمدير النظام فقط.")
                return redirect("reports:platform_login")

            # ── بوّابة العامل الثاني ─────────────────────────────────────
            # **موضعُها هنا وحدها.** ما بعد هذا السطر ينادي ``login()`` في سبعة
            # فروع مختلفة بحسب الدور والاشتراك والعضوية. ووضعُ الفحص في كل فرع
            # يعني أن نسيان فرعٍ واحد يفتح باباً كاملاً لا يكشفه شيء — لأن بقية
            # الفروع تعمل. فالبوابة قبلها جميعاً: من له عامل ثانٍ نافذ لا تُنشأ
            # له جلسة مصادَق عليها هنا إطلاقاً.
            from .totp import start_totp_challenge
            from ..models import TeacherTotpDevice

            if TeacherTotpDevice.objects.filter(
                teacher=user, confirmed_at__isnull=False
            ).exists():
                start_totp_challenge(request, user, next_url=next_value or "")
                return redirect("reports:totp_challenge")

            # ✅ قواعد الاشتراك عند تسجيل الدخول:
            # - السوبر: يتجاوز دائمًا.
            # - مدير المدرسة: يُسمح له بالدخول حتى لو انتهى الاشتراك، لكن يُوجّه لصفحة (انتهاء الاشتراك)
            #   ولا يستطيع استخدام المنصة إلا لصفحات التجديد (يُفرض ذلك عبر SubscriptionMiddleware).
            # - بقية المستخدمين: إن لم توجد أي مدرسة باشتراك ساري → نمنع تسجيل الدخول.

            if not getattr(user, "is_superuser", False):
                try:
                    memberships = (
                        SchoolMembership.objects.filter(teacher=user, is_active=True)
                        .select_related("school", "school__subscription")
                        .order_by("id")
                    )

                    # إن لم تكن هناك أي عضوية مدرسة، لا نمنع تسجيل الدخول برسالة اشتراك (لأننا لا نستطيع ربطه بمدرسة).
                    # هذا يحدث أحياناً لحسابات قديمة أو حسابات لم تُربط بعد.
                    if not memberships.exists():
                        login(request, user)
                        force_password_change = is_force_password_change_required(request)
                        # المدير التنفيذي **لا يملك عضوية مدرسة بحكم تصميمه** —
                        # عضويته على المجموعة، وبقاؤه خارج ``SchoolMembership``
                        # هو ما يجعله لا يستهلك مقعداً مدفوعاً. فتحذير «حسابك غير
                        # مرتبط بمدرسة» كان يستقبله عند كل دخول برسالة عطلٍ عن
                        # حالةٍ صحيحة، ويدفعه إلى مراسلة الدعم بلا سبب.
                        if not is_executive_director(user):
                            messages.warning(request, "تنبيه: حسابك غير مرتبط بمدرسة فعّالة. تواصل مع إدارة النظام لربط الحساب بالمدرسة.")
                        if force_password_change:
                            messages.warning(request, _force_password_change_notice())
                            return redirect("reports:my_profile")
                        _offer_passkey_enrollment(request, user)
                        next_url = next_value
                        default_name = _default_login_redirect_name(
                            user,
                            active_school=_get_active_school(request),
                        )
                        return redirect(next_url or default_name)

                    active_school = None
                    active_manager_school = None
                    any_active_subscription = False
                    is_any_manager = False
                    manager_school = None
                    first_school_name = None

                    for m in memberships:
                        if first_school_name is None:
                            first_school_name = getattr(getattr(m, "school", None), "name", None)
                        if m.role_type == SchoolMembership.RoleType.MANAGER:
                            is_any_manager = True
                            if manager_school is None:
                                manager_school = m.school

                        sub = None
                        try:
                            sub = getattr(m.school, "subscription", None)
                        except Exception:
                            sub = None

                        # عدم وجود اشتراك = منتهي
                        if sub is not None and not bool(sub.is_expired) and bool(getattr(m.school, "is_active", True)):
                            any_active_subscription = True
                            if active_school is None:
                                active_school = m.school
                            if (
                                m.role_type == SchoolMembership.RoleType.MANAGER
                                and active_manager_school is None
                            ):
                                active_manager_school = m.school

                    if not any_active_subscription:
                        if is_any_manager and manager_school is not None:
                            # المدير يُسمح له بالدخول للتجديد فقط
                            login(request, user)
                            is_force_password_change_required(request)
                            _set_active_school(request, manager_school)
                            logger.info(
                                "Login allowed for renewal-only manager user_id=%s school_id=%s trace_id=%s",
                                getattr(user, "id", None),
                                getattr(manager_school, "id", None),
                                getattr(request, "trace_id", None),
                            )
                            return redirect("reports:subscription_expired")

                        school_label = f" ({first_school_name})" if first_school_name else ""
                        logger.warning(
                            "Login blocked due to expired subscriptions user_id=%s identifier_hash=%s trace_id=%s",
                            getattr(user, "id", None),
                            _identifier_for_log(identifier),
                            getattr(request, "trace_id", None),
                        )
                        messages.error(request, f"عذرًا، اشتراك المدرسة{school_label} منتهي. لا يمكن الدخول حتى يتم تجديد الاشتراك.")
                        return redirect("reports:login")

                    # الإدارة هي وجهة الدخول الافتراضية متى كانت مدرسة الإدارة
                    # نفسها فعّالة. ويمكن للمستخدم بعد ذلك تبديل السياق إلى
                    # مدرسة يدرّس فيها، فتتحول الواجهة إلى رحلة المعلم.
                    active_school = active_manager_school or active_school
                    login(request, user)
                    if active_school is not None:
                        _set_active_school(request, active_school)
                except Exception:
                    # في حال أي مشكلة في تحقق الاشتراك، لا نكسر تسجيل الدخول (سيتولى Middleware المنع لاحقاً)
                    login(request, user)
            else:
                login(request, user)

            # بعد تسجيل الدخول مباشرةً: اختيار مدرسة افتراضية عند توفر مدرسة واحدة فقط
            try:
                # إن كان للمستخدم مدرسة واحدة فقط ضمن عضوياته نعتبرها المدرسة النشطة
                schools = _user_schools(user)
                if len(schools) == 1:
                    _set_active_school(request, schools[0])
                # أو إن كان مالك النظام وهناك مدرسة واحدة فقط مفعّلة في النظام
                elif user.is_superuser:
                    qs = School.objects.filter(is_active=True)
                    if qs.count() == 1:
                        s = qs.first()
                        if s is not None:
                            _set_active_school(request, s)
            except Exception:
                _degraded(
                    "auth.autoselect_active_school_post_login",
                    user_id=getattr(user, "pk", None),
                )

            force_password_change = is_force_password_change_required(request)
            if force_password_change:
                messages.warning(request, _force_password_change_notice())
                return redirect("reports:my_profile")

            _offer_passkey_enrollment(request, user)
            next_url = next_value
            # الوجهة الافتراضية حسب الدور
            default_name = _default_login_redirect_name(
                user,
                active_school=_get_active_school(request),
            )
            logger.info(
                "Login success user_id=%s is_superuser=%s active_school_id=%s redirect=%s trace_id=%s",
                getattr(user, "id", None),
                bool(getattr(user, "is_superuser", False)),
                request.session.get("active_school_id"),
                (next_url or default_name),
                getattr(request, "trace_id", None),
            )
            opmetrics.increment("auth.login.success")
            return redirect(next_url or default_name)

        # فشل المصادقة: نُحصي المحاولة على الحساب قبل أي شيء آخر.
        _register_login_failure(identifier)

        # ثم نتحقق هل السبب أن الحساب موقوف (is_active=False).
        #
        # الكشف عن الإيقاف مشروطٌ بصحة كلمة المرور عمداً: من يعرف كلمة المرور
        # يستحق أن يُقال له لِمَ رُفض، ومن لا يعرفها لا يستفيد من الرسالة شيئاً
        # في تعداد الحسابات. وهي التجزئة الثانية والأخيرة في هذا المسار.
        try:
            if potential_user is not None and (not potential_user.is_active) and potential_user.check_password(password):
                logger.warning("Login failed inactive-user user_id=%s identifier_hash=%s trace_id=%s", getattr(potential_user, "id", None), _identifier_for_log(identifier), getattr(request, "trace_id", None))
                opmetrics.increment("auth.login.failure")
                messages.error(request, "عذرًا، حسابك موقوف. يرجى التواصل مع الإدارة.")
            else:
                logger.warning("Login failed invalid-credentials identifier_hash=%s trace_id=%s", _identifier_for_log(identifier), getattr(request, "trace_id", None))
                opmetrics.increment("auth.login.failure")
                messages.error(request, "رقم الجوال/الهوية أو كلمة المرور غير صحيحة")
        except Exception:
            logger.warning("Login failed (exception path) identifier_hash=%s trace_id=%s", _identifier_for_log(identifier), getattr(request, "trace_id", None))
            opmetrics.increment("auth.login.failure")
            messages.error(request, "رقم الجوال/الهوية أو كلمة المرور غير صحيحة")

    context = {
        "next": next_value,
        "admin_login": admin_only,
        "login_action_name": "reports:platform_login" if admin_only else "reports:login",
    }
    return render(request, "reports/login.html", context)


@login_required(login_url="reports:login")
@require_http_methods(["POST"])
@ratelimit(key="user", rate="15/m", method="POST", block=False)
def passkey_register_options(request: HttpRequest) -> JsonResponse:
    limited = _passkey_rate_limited(request)
    if limited is not None:
        return limited

    if is_force_password_change_required(request):
        return _passkey_response(
            False,
            status=403,
            error="password_change_required",
            message="غيّر كلمة المرور أولاً ثم فعّل الدخول بالبصمة.",
        )

    user = request.user
    challenge = random_challenge()
    request.session[WEBAUTHN_REGISTER_CHALLENGE_SESSION_KEY] = challenge

    existing = []
    for credential in WebAuthnCredential.objects.filter(teacher=user, is_active=True).only("credential_id", "transports"):
        existing.append(
            {
                "type": "public-key",
                "id": b64url_encode(bytes(credential.credential_id)),
                "transports": credential.transports or ["internal"],
            }
        )

    options = {
        "challenge": challenge,
        "rp": {
            "name": "منصة توثيق",
            "id": rp_id_from_request(request),
        },
        "user": {
            "id": b64url_encode(str(user.pk).encode("utf-8")),
            "name": getattr(user, "phone", "") or str(user.pk),
            "displayName": getattr(user, "name", "") or getattr(user, "phone", "") or "مستخدم",
        },
        "pubKeyCredParams": [
            {"type": "public-key", "alg": -7},
            {"type": "public-key", "alg": -257},
        ],
        "timeout": 120000,
        "attestation": "none",
        "excludeCredentials": existing,
        "authenticatorSelection": {
            "residentKey": "preferred",
            "requireResidentKey": False,
            "userVerification": "required",
        },
    }
    return _passkey_response(True, publicKey=options)


@login_required(login_url="reports:login")
@require_http_methods(["POST"])
@ratelimit(key="user", rate="15/m", method="POST", block=False)
def passkey_register_verify(request: HttpRequest) -> JsonResponse:
    limited = _passkey_rate_limited(request)
    if limited is not None:
        return limited

    challenge = request.session.get(WEBAUTHN_REGISTER_CHALLENGE_SESSION_KEY)
    if not challenge:
        return _passkey_response(False, status=400, error="challenge_missing", message="انتهت صلاحية محاولة التفعيل.")

    try:
        payload = json_body(request)
        response = payload.get("response") or {}
        parse_client_data(
            client_data_json_b64=response.get("clientDataJSON") or "",
            expected_type="webauthn.create",
            expected_challenge=challenge,
            expected_origin=origin_from_request(request),
        )

        attestation = cbor2.loads(b64url_decode(response.get("attestationObject") or ""))
        auth_data = bytes(attestation.get("authData") or b"")
        parsed = parse_authenticator_data(
            auth_data,
            rp_id=rp_id_from_request(request),
            require_attested_credential=True,
            require_user_verification=True,
        )
        if not parsed.credential_id or not parsed.public_key_cose:
            raise ValueError("credential_invalid")

        raw_id = b64url_decode(payload.get("rawId") or payload.get("id") or "")
        credential_id = raw_id or parsed.credential_id
        if credential_id != parsed.credential_id:
            raise ValueError("credential_id_mismatch")

        transports = response.get("transports")
        if not isinstance(transports, list):
            transports = ["internal"]

        device_name = _passkey_device_label(request, payload.get("deviceName") or "")
        WebAuthnCredential.objects.create(
            teacher=request.user,
            credential_id=credential_id,
            credential_id_hash=credential_hash(credential_id),
            public_key_cose=parsed.public_key_cose,
            sign_count=parsed.sign_count,
            device_name=device_name,
            transports=transports,
        )
        request.session.pop(WEBAUTHN_REGISTER_CHALLENGE_SESSION_KEY, None)
        request.session.pop(PASSKEY_ENROLL_PROMPT_SESSION_KEY, None)
        return _passkey_response(
            True,
            message=f"تم تفعيل الدخول بالبصمة على «{device_name}».",
            deviceName=device_name,
        )
    except IntegrityError:
        return _passkey_response(False, status=409, error="credential_exists", message="هذا الجهاز مفعّل مسبقاً.")
    except Exception:
        logger.exception("Passkey registration failed user_id=%s", getattr(request.user, "id", None))
        return _passkey_response(False, status=400, error="registration_failed", message="تعذر تفعيل الدخول بالبصمة.")


@login_required(login_url="reports:login")
@require_http_methods(["POST"])
def passkey_enroll_prompt_dismiss(request: HttpRequest) -> JsonResponse:
    if request.content_type == "application/json":
        try:
            action = str(json_body(request).get("action") or "snooze").strip().lower()
        except ValueError:
            return _passkey_response(False, status=400, error="json_invalid", message="تعذر قراءة الاختيار.")
    else:
        # Keep the previous empty form POST working for cached pages and older
        # clients; it has always meant "remind me later".
        action = str(request.POST.get("action") or "snooze").strip().lower()

    if action not in {"snooze", "never", "unsupported"}:
        return _passkey_response(False, status=400, error="action_invalid", message="اختيار غير صالح.")

    request.session.pop(PASSKEY_ENROLL_PROMPT_SESSION_KEY, None)
    if action == "never":
        Teacher.objects.filter(pk=request.user.pk).update(passkey_prompt_opt_out=True)
        request.user.passkey_prompt_opt_out = True
        response = _passkey_response(
            True,
            action=action,
            message="لن نعرض دعوة التفعيل مجددًا. يمكنك التفعيل في أي وقت من إعدادات الأمان.",
        )
        response.delete_cookie(PASSKEY_PROMPT_SNOOZE_COOKIE, samesite="Lax")
        return response

    is_unsupported = action == "unsupported"
    cookie_name = PASSKEY_UNSUPPORTED_DEVICE_COOKIE if is_unsupported else PASSKEY_PROMPT_SNOOZE_COOKIE
    max_age = PASSKEY_UNSUPPORTED_DEVICE_MAX_AGE if is_unsupported else PASSKEY_PROMPT_SNOOZE_MAX_AGE
    response = _passkey_response(
        True,
        action=action,
        message=(
            "لن نعرض الدعوة على هذا الجهاز غير المدعوم."
            if is_unsupported
            else "حسنًا، سنذكّرك بعد 90 يومًا."
        ),
    )
    response.set_cookie(
        cookie_name,
        "1",
        max_age=max_age,
        samesite="Lax",
        secure=request.is_secure(),
        httponly=True,
    )
    return response


@login_required(login_url="reports:login")
@require_http_methods(["POST"])
def passkey_delete(request: HttpRequest, pk: int) -> JsonResponse:
    """Revoke one enabled device.

    The row is removed rather than deactivated: ``credential_id`` is unique, so
    a lingering inactive row would block the same device from enrolling again.
    """
    credential = WebAuthnCredential.objects.filter(pk=pk, teacher=request.user).first()
    if credential is None:
        return _passkey_response(False, status=404, error="credential_not_found", message="لم يعد هذا الجهاز موجودًا في قائمتك.")

    device_name = credential.device_name or "هذا الجهاز"
    credential.delete()
    remaining = WebAuthnCredential.objects.filter(teacher=request.user, is_active=True).count()
    return _passkey_response(
        True,
        message=f"تم إلغاء الدخول بالبصمة من «{device_name}».",
        remaining=remaining,
    )


@require_http_methods(["POST"])
# Generous on purpose: a whole school shares one NAT address, and autofill fires
# this once per page load. It still stops scripted enumeration of phone numbers.
@ratelimit(key="ip", rate="60/m", method="POST", block=False)
def passkey_login_options(request: HttpRequest) -> JsonResponse:
    """Start a sign-in ceremony.

    Two shapes are supported. Without an identifier the ceremony is
    *discoverable*: the authenticator offers whatever passkey it holds for this
    site and tells us who it belongs to — that is what makes one-tap sign-in and
    browser autofill possible. With an identifier we narrow the ceremony to that
    account's credentials, which gives a clearer error when none exist.
    """
    limited = _passkey_rate_limited(request)
    if limited is not None:
        return limited

    allow_credentials = []
    try:
        payload = json_body(request)
    except ValueError:
        payload = {}

    identifier = (payload.get("identifier") or "").strip()
    if not identifier:
        challenge = random_challenge()
        request.session[WEBAUTHN_AUTH_CHALLENGE_SESSION_KEY] = challenge
        request.session[WEBAUTHN_AUTH_ALLOWED_CREDENTIALS_SESSION_KEY] = []
        request.session[WEBAUTHN_AUTH_DISCOVERABLE_SESSION_KEY] = True
        return _passkey_response(
            True,
            discoverable=True,
            publicKey={
                "challenge": challenge,
                "rpId": rp_id_from_request(request),
                "timeout": 120000,
                "userVerification": "required",
                "allowCredentials": [],
            },
        )

    attempts = [identifier]
    ident_no_plus = identifier.lstrip("+")
    if ident_no_plus != identifier:
        attempts.append(ident_no_plus)
    if identifier.isdigit() and len(identifier) == 9:
        attempts.append("0" + identifier)
    if ident_no_plus.isdigit() and ident_no_plus.startswith("966") and len(ident_no_plus) >= 12:
        attempts.append("0" + ident_no_plus[-9:])
    attempts = list(dict.fromkeys([item for item in attempts if item]))

    user = Teacher.objects.filter(Q(phone__in=attempts) | Q(national_id=identifier)).first()
    if user is not None:
        for credential in WebAuthnCredential.objects.filter(teacher=user, is_active=True).only("credential_id", "transports"):
            credential_id = bytes(credential.credential_id)
            allow_credentials.append(
                {
                    "type": "public-key",
                    "id": b64url_encode(credential_id),
                    "transports": credential.transports or ["internal"],
                    "hash": credential_hash(credential_id),
                }
            )

    if not allow_credentials:
        request.session.pop(WEBAUTHN_AUTH_CHALLENGE_SESSION_KEY, None)
        request.session.pop(WEBAUTHN_AUTH_ALLOWED_CREDENTIALS_SESSION_KEY, None)
        request.session.pop(WEBAUTHN_AUTH_DISCOVERABLE_SESSION_KEY, None)
        return _passkey_response(
            False,
            status=404,
            error="passkey_not_enabled",
            message="لا توجد بصمة مفعلة لهذا الرقم أو الهوية. سجّل الدخول بكلمة المرور ثم فعّل البصمة من الملف الشخصي.",
        )

    challenge = random_challenge()
    request.session[WEBAUTHN_AUTH_CHALLENGE_SESSION_KEY] = challenge
    request.session[WEBAUTHN_AUTH_ALLOWED_CREDENTIALS_SESSION_KEY] = [item["hash"] for item in allow_credentials]
    request.session.pop(WEBAUTHN_AUTH_DISCOVERABLE_SESSION_KEY, None)
    for item in allow_credentials:
        item.pop("hash", None)

    public_key = {
        "challenge": challenge,
        "rpId": rp_id_from_request(request),
        "timeout": 120000,
        "userVerification": "required",
        "allowCredentials": allow_credentials,
    }
    return _passkey_response(True, publicKey=public_key)


@require_http_methods(["POST"])
@ratelimit(key="ip", rate="20/m", method="POST", block=False)
def passkey_login_verify(request: HttpRequest) -> JsonResponse:
    limited = _passkey_rate_limited(request)
    if limited is not None:
        return limited

    challenge = request.session.get(WEBAUTHN_AUTH_CHALLENGE_SESSION_KEY)
    if not challenge:
        return _passkey_response(False, status=400, error="challenge_missing", message="انتهت صلاحية محاولة الدخول.")

    allowed_hashes = request.session.get(WEBAUTHN_AUTH_ALLOWED_CREDENTIALS_SESSION_KEY) or []
    discoverable = bool(request.session.get(WEBAUTHN_AUTH_DISCOVERABLE_SESSION_KEY))

    # التحدّي يُستهلك هنا لا عند النجاح: كان يُمسح في مسار النجاح وحده، فتبقى
    # المحاولة الفاشلة تاركةً تحدياً حياً في الجلسة يُعاد التوقيع عليه مراراً.
    # ومعيار WebAuthn يشترط أن يكون التحدي **لمرة واحدة**، والواجهة تطلب
    # ``options`` جديدة قبل كل محاولة أصلاً، فلا يخسر المستخدم شيئاً.
    request.session.pop(WEBAUTHN_AUTH_CHALLENGE_SESSION_KEY, None)
    request.session.pop(WEBAUTHN_AUTH_ALLOWED_CREDENTIALS_SESSION_KEY, None)
    request.session.pop(WEBAUTHN_AUTH_DISCOVERABLE_SESSION_KEY, None)

    try:
        payload = json_body(request)
        response = payload.get("response") or {}
        raw_id = b64url_decode(payload.get("rawId") or payload.get("id") or "")
        raw_id_hash = credential_hash(raw_id)
        # A discoverable ceremony never claimed an identity up front, so there is
        # no allow-list to match against; the signature below is what proves it.
        if not discoverable and raw_id_hash not in allowed_hashes:
            return _passkey_response(False, status=403, error="credential_not_allowed", message="مفتاح البصمة لا يطابق الرقم أو الهوية المدخلة.")

        credential = (
            WebAuthnCredential.objects.select_related("teacher")
            .filter(credential_id_hash=raw_id_hash, is_active=True)
            .first()
        )
        if credential is None:
            return _passkey_response(False, status=404, error="credential_not_found", message="لم يتم العثور على مفتاح بصمة لهذا الجهاز.")

        user_handle_b64 = response.get("userHandle")
        if user_handle_b64:
            try:
                user_handle = b64url_decode(user_handle_b64).decode("utf-8", "ignore").strip()
            except Exception:
                user_handle = ""
            if user_handle and user_handle != str(credential.teacher_id):
                return _passkey_response(
                    False,
                    status=403,
                    error="user_handle_mismatch",
                    message="مفتاح البصمة لا يطابق حساب المستخدم.",
                )

        client_data_hash = parse_client_data(
            client_data_json_b64=response.get("clientDataJSON") or "",
            expected_type="webauthn.get",
            expected_challenge=challenge,
            expected_origin=origin_from_request(request),
        )
        auth_data_b64 = response.get("authenticatorData") or ""
        auth_data = b64url_decode(auth_data_b64)
        parsed = parse_authenticator_data(
            auth_data,
            rp_id=rp_id_from_request(request),
            require_user_verification=True,
        )
        verify_signature(
            public_key_cose=bytes(credential.public_key_cose),
            signature_b64=response.get("signature") or "",
            authenticator_data_b64=auth_data_b64,
            client_data_hash=client_data_hash,
        )

        old_count = int(credential.sign_count or 0)
        new_count = int(parsed.sign_count or 0)
        if old_count and new_count and new_count <= old_count:
            return _passkey_response(False, status=403, error="sign_count_invalid", message="تعذر التحقق من مفتاح البصمة.")
        if new_count > old_count:
            credential.sign_count = new_count
        credential.last_used_at = timezone.now()
        credential.save(update_fields=["sign_count", "last_used_at"])

        return _complete_passkey_login(
            request,
            credential.teacher,
            next_url=(payload.get("next") or request.GET.get("next") or ""),
        )
    except Exception:
        logger.exception("Passkey login failed")
        return _passkey_response(False, status=400, error="login_failed", message="تعذر تسجيل الدخول بالبصمة.")


@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def logout_view(request: HttpRequest) -> HttpResponse:
    _set_active_school(request, None)
    logout(request)
    return redirect("reports:login")


def _profile_membership_contexts(
    user,
    memberships: list[SchoolMembership],
    *,
    active_school,
) -> list[dict[str, Any]]:
    """Build read-only school context without per-membership department queries."""

    grouped: dict[int, dict[str, Any]] = {}
    for membership in memberships:
        school_id = int(membership.school_id)
        context = grouped.setdefault(
            school_id,
            {
                "school": membership.school,
                "is_current": bool(active_school and active_school.pk == school_id),
                "roles": [],
                "job_titles": [],
                "departments": [],
            },
        )
        role_label = membership.get_role_type_display()
        if role_label not in context["roles"]:
            context["roles"].append(role_label)
        job_title = membership.get_job_title_display()
        if job_title not in context["job_titles"]:
            context["job_titles"].append(job_title)

    if grouped:
        department_names: dict[int, list[str]] = {school_id: [] for school_id in grouped}
        department_memberships = (
            DepartmentMembership.objects.filter(
                teacher=user,
                department__school_id__in=grouped,
                department__is_active=True,
            )
            .select_related("department")
            .order_by("department__school__name", "department__name", "id")
        )
        for department_membership in department_memberships:
            school_id = int(department_membership.department.school_id)
            name = department_membership.department.name
            if name not in department_names[school_id]:
                department_names[school_id].append(name)
        for school_id, names in department_names.items():
            grouped[school_id]["departments"] = names

    return list(grouped.values())


@login_required(login_url="reports:login")
@require_http_methods(["GET", "POST"])
def my_profile(request: HttpRequest) -> HttpResponse:
    """بروفايل المستخدم الحالي.

    - متاح لكل المستخدمين.
    - يعرض الاسم + المدارس المسندة.
    - يسمح بتغيير رقم الجوال + تغيير كلمة المرور، ويطلب البريد الإلكتروني عند الدخول الأول.
    """

    active_school = _get_active_school(request)
    force_password_change = is_force_password_change_required(request)

    memberships = list(
        SchoolMembership.objects.filter(teacher=request.user, is_active=True)
        .select_related("school")
        .order_by("school__name", "id")
    )
    membership_contexts = _profile_membership_contexts(
        request.user,
        memberships,
        active_school=active_school,
    )
    current_membership_context = next(
        (context for context in membership_contexts if context["is_current"]),
        None,
    )

    phone_form = MyProfilePhoneForm(instance=request.user, prefix="phone")
    email_form = MyProfileEmailForm(instance=request.user, prefix="email")
    pwd_form = MyPasswordChangeForm(
        request.user,
        prefix="pwd",
        require_email=force_password_change,
    )

    if request.method == "POST":
        if "update_email" in request.POST:
            email_form = MyProfileEmailForm(request.POST, instance=request.user, prefix="email")
            if email_form.is_valid():
                email_form.save()
                messages.success(request, "تم تحديث البريد الإلكتروني بنجاح.")
                return redirect("reports:my_profile")
        elif "update_phone" in request.POST:
            if force_password_change:
                messages.info(request, "لتأمين الحساب أولاً، غيّر كلمة المرور ثم سيصبح تحديث رقم الجوال متاحًا مباشرة.")
                return redirect("reports:my_profile")
            phone_form = MyProfilePhoneForm(request.POST, instance=request.user, prefix="phone")
            if phone_form.is_valid():
                try:
                    phone_form.save()
                    messages.success(request, "تم تحديث رقم الجوال بنجاح.")
                    return redirect("reports:my_profile")
                except IntegrityError:
                    messages.error(request, "تعذر تحديث رقم الجوال (قد يكون مستخدمًا بالفعل).")
        elif "update_password" in request.POST:
            pwd_form = MyPasswordChangeForm(
                request.user,
                request.POST,
                prefix="pwd",
                require_email=force_password_change,
            )
            if pwd_form.is_valid():
                user = pwd_form.save()
                update_session_auth_hash(request, user)
                # مفتاحُ جلسةٍ لا يُحدَّث بعد تغيير كلمة المرور يُبطل قاعدة
                # الجلسة الواحدة صامتاً.
                with soft_fail("auth.session_key_after_password_change", user_id=user.pk):
                    new_session_key = request.session.session_key or ""
                    if new_session_key and getattr(user, "current_session_key", "") != new_session_key:
                        user.current_session_key = new_session_key
                        user.save(update_fields=["current_session_key"])
                clear_force_password_change_flag(request)

                # إرسال إيميل تأكيد تغيير كلمة المرور (في الخلفية)
                with soft_fail("auth.password_change_email", user_id=user.pk):
                    from ..utils import run_task_safe
                    from ..tasks import send_password_change_email_task
                    run_task_safe(send_password_change_email_task, user.pk)

                if force_password_change:
                    messages.success(request, "تم حفظ البريد الإلكتروني وتحديث كلمة المرور بنجاح.")
                else:
                    messages.success(request, "تم تحديث كلمة المرور بنجاح.")
                return redirect("reports:my_profile")

    ctx = {
        "active_school": active_school,
        "memberships": memberships,
        "membership_contexts": membership_contexts,
        "current_membership_context": current_membership_context,
        "phone_form": phone_form,
        "email_form": email_form,
        "pwd_form": pwd_form,
        "force_password_change": force_password_change,
        "totp_device": TeacherTotpDevice.objects.filter(
            teacher=request.user,
            confirmed_at__isnull=False,
        ).first(),
        "passkey_credentials": WebAuthnCredential.objects.filter(teacher=request.user, is_active=True).order_by("-created_at"),
        **build_staff_workspaces(request.user, active_school),
    }
    return render(request, "reports/my_profile.html", ctx)



LANDING_PRICING_CACHE_KEY = "landing:pricing-context:v2"


def _build_landing_pricing_context() -> dict[str, Any]:
    """Compute the landing page's pricing model from the active plans.

    Pure function of ``SubscriptionPlan`` rows plus settings — it holds no
    per-request state, which is what makes the result cacheable.
    """

    plans_qs = SubscriptionPlan.objects.filter(is_active=True).order_by("price", "max_teachers", "days_duration", "id")
    source_plans = list(plans_qs)
    trial_days_target = FREE_TRIAL_DAYS

    def serialize_plan(plan: SubscriptionPlan, *, is_trial: bool) -> dict[str, Any]:
        raw_price = float(getattr(plan, "price", 0) or 0)
        raw_capacity = int(getattr(plan, "max_teachers", 0) or 0)
        capacity = raw_capacity
        if is_trial and capacity <= 0:
            capacity = 5
        is_unlimited = (raw_capacity <= 0) and (not is_trial)

        description = (getattr(plan, "description", "") or "").strip()
        if is_trial:
            description = re.sub(
                r"(?:لمدة\s*)?[0-9٠-٩]+\s+(?:يوم(?:اً|ًا)?|أيام)",
                f"لمدة {FREE_TRIAL_DAYS} يومًا",
                description,
            )
        summary = description.split("\n", 1)[0].strip() if description else ""
        if not summary:
            summary = _landing_fit_text(capacity, is_trial, is_unlimited)

        if abs(raw_price - round(raw_price)) < 0.001:
            price_display = f"{int(round(raw_price)):,}"
            price_int = int(round(raw_price))
        else:
            price_display = f"{raw_price:,.2f}".rstrip("0").rstrip(".")
            price_int = int(round(raw_price))

        if is_unlimited:
            capacity_label = "معلمون غير محدودين"
            capacity_hint = 999999
        else:
            capacity_label = f"حتى {capacity} معلماً"
            capacity_hint = capacity

        return {
            "id": int(getattr(plan, "id", 0) or 0),
            "source_name": (getattr(plan, "name", "") or "").strip() or "باقة",
            "summary": summary,
            "features": _landing_parse_features(description, is_trial),
            "fit_text": _landing_fit_text(capacity, is_trial, is_unlimited),
            "price_value": raw_price,
            "price_int": price_int,
            "price_display": price_display,
            "duration_days": int(getattr(plan, "days_duration", 0) or 0),
            "duration_label": _landing_duration_label(int(getattr(plan, "days_duration", 0) or 0)),
            "capacity": capacity,
            "capacity_hint": capacity_hint,
            "capacity_label": capacity_label,
            "is_trial": is_trial,
            "is_unlimited": is_unlimited,
            "period_key": _landing_period_key(int(getattr(plan, "days_duration", 0) or 0), is_trial),
            "cta_label": "سجّل المدرسة الآن" if is_trial else "ابدأ بالتجربة ثم فعّل",
        }

    trial_candidates = [plan for plan in source_plans if float(getattr(plan, "price", 0) or 0) <= 0]
    trial_source = None
    if trial_candidates:
        trial_source = min(
            trial_candidates,
            key=lambda p: (
                abs(int(getattr(p, "days_duration", 0) or 0) - trial_days_target),
                0 if int(getattr(p, "max_teachers", 0) or 0) <= 5 else 1,
                int(getattr(p, "days_duration", 0) or 0),
                int(getattr(p, "id", 0) or 0),
            ),
        )

    pricing_trial_plan = serialize_plan(trial_source, is_trial=True) if trial_source is not None else None
    if pricing_trial_plan is not None:
        # Normalise legacy zero-price rows to the approved public duration.
        pricing_trial_plan["duration_days"] = FREE_TRIAL_DAYS
        pricing_trial_plan["duration_label"] = _landing_duration_label(FREE_TRIAL_DAYS)
        pricing_trial_plan["name"] = "التجربة المجانية"
        pricing_trial_plan["badge"] = f'{pricing_trial_plan["duration_label"]} تجريبية'
        pricing_trial_plan["cta_secondary_label"] = "لديك حساب بالفعل؟"

    paid_source = [plan for plan in source_plans if float(getattr(plan, "price", 0) or 0) > 0]
    paid_groups: dict[str, dict[str, Any]] = {}
    available_periods = {"1m": False, "6m": False, "1y": False}

    for source_plan in paid_source:
        plan = serialize_plan(source_plan, is_trial=False)
        period_key = plan["period_key"]
        if period_key not in {"1m", "6m", "1y"}:
            continue

        available_periods[period_key] = True
        group_key = str(plan["capacity_hint"])
        group = paid_groups.setdefault(
            group_key,
            {
                "capacity_hint": plan["capacity_hint"],
                "capacity_label": plan["capacity_label"],
                "fit_text": plan["fit_text"],
                "is_unlimited": plan["is_unlimited"],
                "plans": {},
            },
        )
        existing = group["plans"].get(period_key)
        target_days = {"1m": 30, "6m": 180, "1y": 365}[period_key]
        if existing is None or (
            abs(plan["duration_days"] - target_days),
            plan["price_value"],
            plan["id"],
        ) < (
            abs(existing["duration_days"] - target_days),
            existing["price_value"],
            existing["id"],
        ):
            group["plans"][period_key] = plan

    pricing_cards: list[dict[str, Any]] = []
    for group in sorted(
        paid_groups.values(),
        key=lambda item: (
            1 if int(item["capacity_hint"]) >= 999999 else 0,
            int(item["capacity_hint"]),
        ),
    ):
        plans_by_period = group["plans"]
        default_plan = plans_by_period.get("1m") or plans_by_period.get("6m") or plans_by_period.get("1y")
        if default_plan is None:
            continue

        for period_key, months in (("1m", 1), ("6m", 6), ("1y", 12)):
            period_plan = plans_by_period.get(period_key)
            if period_plan is None:
                continue
            monthly_equivalent = float(period_plan["price_value"]) / months
            period_plan["monthly_equivalent_display"] = f"{int(round(monthly_equivalent)):,}"

        semiannual_plan = plans_by_period.get("6m")
        annual_plan = plans_by_period.get("1y")
        monthly_plan = plans_by_period.get("1m")
        annual_savings = 0
        annual_discount_percent = 0
        if monthly_plan is not None and annual_plan is not None:
            comparison_price = float(monthly_plan["price_value"]) * 12
            annual_savings = max(0, int(round(comparison_price - float(annual_plan["price_value"]))))
            if comparison_price > 0:
                annual_discount_percent = int(round((annual_savings / comparison_price) * 100))
            annual_plan["savings_display"] = f"{annual_savings:,}"
            annual_plan["discount_percent"] = annual_discount_percent

        if monthly_plan is not None and semiannual_plan is not None:
            comparison_price = float(monthly_plan["price_value"]) * 6
            semiannual_savings = max(0, int(round(comparison_price - float(semiannual_plan["price_value"]))))
            semiannual_plan["savings_display"] = f"{semiannual_savings:,}"

        card = {
            "capacity_hint": group["capacity_hint"],
            "capacity_label": group["capacity_label"],
            "fit_text": group["fit_text"],
            "name": _landing_card_title(int(default_plan["capacity"]), bool(default_plan["is_unlimited"])),
            "cta_label": "ابدأ بالتجربة ثم فعّل",
            "period_1m": plans_by_period.get("1m"),
            "period_6m": plans_by_period.get("6m"),
            "period_1y": plans_by_period.get("1y"),
            "periods": {
                "1m": plans_by_period.get("1m"),
                "6m": plans_by_period.get("6m"),
                "1y": plans_by_period.get("1y"),
            },
            "is_featured": False,
            "is_recommended": False,
            "badge": "",
            "annual_savings": annual_savings,
            "annual_discount_percent": annual_discount_percent,
        }
        pricing_cards.append(card)

    initial_period = "1y" if available_periods["1y"] else ("6m" if available_periods["6m"] else "1m")
    paid_view = [card for card in pricing_cards if card["periods"].get(initial_period) is not None]
    if not paid_view:
        paid_view = pricing_cards[:]

    recommended_plan = None
    if paid_view:
        recommended_plan = min(
            paid_view,
            key=lambda card: (
                abs((card["capacity_hint"] if card["capacity_hint"] < 999999 else 75) - 50),
                float((card["periods"].get(initial_period) or card["periods"].get("1y") or card["periods"].get("6m") or card["periods"].get("1m"))["price_value"]),
                int(card["capacity_hint"]),
            ),
        )

    if recommended_plan is not None:
        recommended_plan["is_featured"] = True
        recommended_plan["is_recommended"] = True

    cheapest_paid = None
    if paid_view:
        cheapest_paid = min(
            paid_view,
            key=lambda card: (
                float((card["periods"].get(initial_period) or card["periods"].get("1y") or card["periods"].get("6m") or card["periods"].get("1m"))["price_value"]),
                int(card["capacity_hint"]),
            ),
        )

    for card in pricing_cards:
        if recommended_plan is not None and card is recommended_plan:
            card["badge"] = "الأكثر طلباً"
        elif cheapest_paid is not None and card is cheapest_paid:
            card["badge"] = "اقتصادية"

    known_caps = [int(card["capacity_hint"]) for card in pricing_cards if int(card["capacity_hint"]) < 999999]
    known_max = max(known_caps) if known_caps else 75
    for card in pricing_cards:
        if int(card["capacity_hint"]) >= 999999:
            card["capacity_hint"] = known_max + 25

    slider_min = 5
    slider_max = max([int(card["capacity_hint"]) for card in pricing_cards], default=100)
    slider_max = max(slider_max, 25)
    slider_step = 5
    initial_users = int(recommended_plan["capacity_hint"]) if recommended_plan is not None else 50
    initial_users = max(slider_min, min(initial_users, slider_max))

    mark_values = sorted({int(card["capacity_hint"]) for card in pricing_cards})
    if not mark_values:
        mark_values = [25, 50, 75, 100]

    if len(mark_values) > 4:
        index_set = {0, len(mark_values) // 3, (2 * len(mark_values)) // 3, len(mark_values) - 1}
        mark_values = [mark_values[i] for i in sorted(index_set)]

    active_mark = min(mark_values, key=lambda v: abs(v - initial_users))
    annual_discount_max = max(
        [int(card.get("annual_discount_percent") or 0) for card in pricing_cards],
        default=0,
    )
    advisor_marks = [
        {
            "value": v,
            "label": _landing_segment_label(v),
            "active": v == active_mark,
        }
        for v in mark_values
    ]

    flexible_catalog = build_flexible_pricing_catalog()

    ctx = {
        "trial_days": trial_days_target,
        "pricing_trial_plan": pricing_trial_plan,
        "pricing_cards": pricing_cards,
        "pricing_plans": pricing_cards,
        "pricing_recommended": recommended_plan,
        "pricing_initial_period": initial_period,
        "pricing_periods": [
            {"key": "1m", "label": "شهري", "available": available_periods["1m"], "active": initial_period == "1m"},
            {"key": "6m", "label": "6 أشهر", "available": available_periods["6m"], "active": initial_period == "6m"},
            {
                "key": "1y",
                "label": f"سنة · وفّر حتى {annual_discount_max}%" if annual_discount_max else "سنة",
                "available": available_periods["1y"],
                "active": initial_period == "1y",
            },
        ],
        "pricing_slider": {
            "min": slider_min,
            "max": slider_max,
            "step": slider_step,
            "initial": active_mark,
        },
        "advisor_marks": advisor_marks,
        "service_pricing": DEFAULT_SERVICE_PRICING,
        "flexible_pricing_catalog": flexible_catalog,
        "flexible_pricing_json": serialize_flexible_pricing_catalog(flexible_catalog),
        # Same source as the manager's pre-payment panel, so a visitor and a
        # paying manager never read two different promises.
        "subscription_included_features": SUBSCRIPTION_INCLUDED_FEATURES,
        "subscription_addon_notes": SUBSCRIPTION_ADDON_NOTES,
    }

    return ctx


def landing_pricing_context() -> dict[str, Any]:
    """Return the landing pricing context, recomputing it at most once per TTL.

    ``/`` is where every campaign click lands, and the page is deliberately
    ``no-store`` so platform toggles apply immediately. That makes it the one
    page guaranteed to run in full for every visitor, so the queries and the
    pricing maths behind it must not run per visit.

    Only the pricing model is cached — never the rendered HTML, which carries
    a per-request CSP nonce and the separately-cached AI feature switches. A
    ``SubscriptionPlan`` change clears this immediately (see the signal in
    ``reports.model_parts.signals``); the TTL is just a backstop.
    """

    try:
        ttl = int(getattr(settings, "LANDING_PRICING_CACHE_TTL_SECONDS", 60) or 0)
    except (TypeError, ValueError):
        ttl = 60

    if ttl <= 0:
        return _build_landing_pricing_context()

    cached = soft_call(
        "landing.pricing_cache_get",
        lambda: cache.get(LANDING_PRICING_CACHE_KEY),
        default=None,
    )
    if isinstance(cached, dict):
        return cached

    ctx = _build_landing_pricing_context()
    soft_call(
        "landing.pricing_cache_set",
        lambda: cache.set(LANDING_PRICING_CACHE_KEY, ctx, ttl),
        default=None,
    )
    return ctx


@never_cache
@cache_control(no_cache=True, must_revalidate=True, no_store=True, max_age=0)
@require_http_methods(["GET"])
def platform_landing(request: HttpRequest) -> HttpResponse:
    """الصفحة الرئيسية العامة للمنصة (تعريف + مميزات + زر دخول).

    - المستخدِم المسجّل بالفعل يُعاد توجيهه مباشرةً للواجهة المناسبة.
    - الزر الأساسي يقود إلى شاشة تسجيل الدخول العادية.
    """

    if getattr(request.user, "is_authenticated", False):
        return redirect(
            _default_login_redirect_name(
                request.user,
                active_school=_get_active_school(request),
            )
        )

    capture_marketing_attribution(request)

    ctx = dict(landing_pricing_context())
    # A gateway's brand mark is a claim that we accept it. Show each one only
    # while its gateway is actually switched on, so the footer can never
    # advertise a payment method a visitor cannot use. Kept out of the cached
    # pricing dict so a settings change takes effect on the next request.
    ctx["moyasar_enabled"] = moyasar_is_enabled()

    response = render(request, "reports/landing.html", ctx)

    # The landing HTML contains runtime-controlled content (including Mansour's
    # visibility).  Some CDN cache rules can ignore the standard ``never_cache``
    # response header, so send the CDN-specific directives as well.  Otherwise
    # an edge can keep serving the version rendered before a platform toggle
    # changed even though Django is already returning the updated page.
    response["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0, private"
    response["CDN-Cache-Control"] = "no-store"
    response["Cloudflare-CDN-Cache-Control"] = "no-store"
    return response
