# reports/views/totp.py
# -*- coding: utf-8 -*-
"""تسجيل العامل الثاني والتحقق منه عند الدخول.

**بوابةٌ واحدة لا سبع.** مسار الدخول ينادي ``login()`` في سبعة فروع (بحسب
الدور والاشتراك والعضوية). ووضعُ فحصٍ للعامل الثاني في كل فرع يعني أن نسيان
فرعٍ واحد يفتح باباً كاملاً — ولا شيء يكشفه لأن الفروع الأخرى تعمل.

فالتصميم هنا: عند نجاح كلمة المرور، **إن كان للمستخدم عامل ثانٍ نافذ فلا
يُنادى ``login()`` إطلاقاً**. يُخزَّن معرّفه في الجلسة ويُوجَّه إلى شاشة
التحقق. والجلسة المصادَق عليها لا تُنشأ إلا بعد اجتياز العامل الثاني.
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods
from django_ratelimit.decorators import ratelimit

from core.observability import soft_fail

from ._helpers import _safe_login_next_url
from ..models import TeacherTotpDevice, TotpRecoveryCode
from ..totp import (
    decrypt_secret,
    encrypt_secret,
    generate_recovery_codes,
    generate_secret,
    hash_recovery_code,
    provisioning_uri,
    verify_code,
)

# مفاتيح الجلسة أثناء ما بين العاملين.
PENDING_USER_SESSION_KEY = "_totp_pending_user_id"
PENDING_NEXT_SESSION_KEY = "_totp_pending_next"
ENROLL_SECRET_SESSION_KEY = "_totp_enroll_secret"
NEW_RECOVERY_SESSION_KEY = "_totp_new_recovery_codes"


def _issuer() -> str:
    return "منصة توثيق"


# ─────────────────────────────────────────────────────────────────────────
# التسجيل (داخل حساب مسجَّل الدخول)
# ─────────────────────────────────────────────────────────────────────────
@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def totp_settings(request: HttpRequest) -> HttpResponse:
    device = TeacherTotpDevice.objects.filter(teacher=request.user).first()
    remaining = 0
    if device is not None:
        remaining = device.recovery_codes.filter(used_at__isnull=True).count()

    return render(
        request,
        "reports/totp_settings.html",
        {
            "active": "totp_settings",
            "device": device,
            "remaining_recovery_codes": remaining,
            # يُعرض مرة واحدة بعد التفعيل ثم يُمسح.
            "new_recovery_codes": request.session.pop(NEW_RECOVERY_SESSION_KEY, None),
        },
    )


@login_required(login_url="reports:login")
@ratelimit(key="user", rate="10/h", method="POST", block=True)
@require_http_methods(["POST"])
def totp_begin_enrollment(request: HttpRequest) -> HttpResponse:
    """يولّد سرّاً ويعرضه — ولا يُفعّل شيئاً بعد.

    السرّ يعيش في الجلسة حتى التأكيد، لا في القاعدة: تسجيلٌ بدأ ولم يكتمل يجب
    ألا يترك أثراً يُطالَب به صاحبه عند الدخول التالي.
    """
    if TeacherTotpDevice.objects.filter(teacher=request.user, confirmed_at__isnull=False).exists():
        messages.info(request, "العامل الثاني مفعَّل بالفعل.")
        return redirect("reports:totp_settings")

    secret = generate_secret()
    request.session[ENROLL_SECRET_SESSION_KEY] = secret

    return render(
        request,
        "reports/totp_enroll.html",
        {
            "active": "totp_settings",
            "secret": secret,
            "otpauth_uri": provisioning_uri(
                secret, account=request.user.phone or request.user.name, issuer=_issuer()
            ),
        },
    )


@login_required(login_url="reports:login")
@ratelimit(key="user", rate="20/h", method="POST", block=True)
@require_http_methods(["POST"])
def totp_confirm_enrollment(request: HttpRequest) -> HttpResponse:
    """يُفعّل الجهاز بعد أن يُثبت المستخدم أنه يولّد رموزاً صحيحة."""
    secret = request.session.get(ENROLL_SECRET_SESSION_KEY)
    if not secret:
        messages.error(request, "انتهت جلسة التسجيل. ابدأ من جديد.")
        return redirect("reports:totp_settings")

    counter = verify_code(secret, request.POST.get("code", ""))
    if counter is None:
        messages.error(request, "الرمز غير صحيح. تحقّق من الوقت في هاتفك وأعد المحاولة.")
        return render(
            request,
            "reports/totp_enroll.html",
            {
                "active": "totp_settings",
                "secret": secret,
                "otpauth_uri": provisioning_uri(
                    secret, account=request.user.phone or request.user.name, issuer=_issuer()
                ),
            },
        )

    device, _created = TeacherTotpDevice.objects.update_or_create(
        teacher=request.user,
        defaults={
            "secret_encrypted": encrypt_secret(secret),
            "confirmed_at": timezone.now(),
            "last_used_counter": counter,
            "last_used_at": timezone.now(),
        },
    )

    # رموز استرجاع جديدة مع كل تفعيل — والقديمة تسقط، فلا يبقى رمزٌ من تسجيلٍ
    # سابق صالحاً بعد إعادة التسجيل.
    device.recovery_codes.all().delete()
    codes = generate_recovery_codes()
    TotpRecoveryCode.objects.bulk_create(
        [TotpRecoveryCode(device=device, code_hash=hash_recovery_code(code)) for code in codes]
    )

    request.session.pop(ENROLL_SECRET_SESSION_KEY, None)
    request.session[NEW_RECOVERY_SESSION_KEY] = codes
    messages.success(request, "فُعِّل العامل الثاني. احفظ رموز الاسترجاع الآن.")
    return redirect("reports:totp_settings")


@login_required(login_url="reports:login")
@ratelimit(key="user", rate="10/h", method="POST", block=True)
@require_http_methods(["POST"])
def totp_disable(request: HttpRequest) -> HttpResponse:
    """تعطيل العامل الثاني — بكلمة المرور.

    **بلا كلمة مرور يصير التعطيل أضعف من التفعيل.** من جلس على جهازٍ مفتوح
    يستطيع إزالة الحماية كلها بنقرة، فتصير قيمةُ العامل الثاني صفراً أمام أكثر
    ما يُفترض أن يحمي منه.
    """
    if not request.user.check_password(request.POST.get("password", "")):
        messages.error(request, "كلمة المرور غير صحيحة.")
        return redirect("reports:totp_settings")

    TeacherTotpDevice.objects.filter(teacher=request.user).delete()
    messages.success(request, "أُوقف العامل الثاني.")
    return redirect("reports:totp_settings")


# ─────────────────────────────────────────────────────────────────────────
# التحقق عند الدخول
# ─────────────────────────────────────────────────────────────────────────
def start_totp_challenge(request: HttpRequest, user, *, next_url: str = "") -> None:
    """يضع المستخدم في حالة «ما بين العاملين» دون إنشاء جلسة مصادَق عليها."""
    request.session[PENDING_USER_SESSION_KEY] = user.pk
    request.session[PENDING_NEXT_SESSION_KEY] = _safe_login_next_url(next_url) or ""


def _pending_user(request: HttpRequest):
    from ..models import Teacher

    user_id = request.session.get(PENDING_USER_SESSION_KEY)
    if not user_id:
        return None
    return Teacher.objects.filter(pk=user_id, is_active=True).first()


def _clear_pending(request: HttpRequest) -> None:
    request.session.pop(PENDING_USER_SESSION_KEY, None)
    request.session.pop(PENDING_NEXT_SESSION_KEY, None)


@ratelimit(key="ip", rate="20/h", method="POST", block=True)
@require_http_methods(["GET", "POST"])
def totp_challenge(request: HttpRequest) -> HttpResponse:
    """شاشة الرمز بعد كلمة المرور.

    الحدّ على العنوان لا على الحساب: الرمز ستة أرقام، ومليونُ احتمالٍ يُستنفد
    بالتخمين الآلي في دقائق بلا حدّ.
    """
    user = _pending_user(request)
    if user is None:
        return redirect("reports:login")

    if request.method == "GET":
        return render(request, "reports/totp_challenge.html", {})

    device = TeacherTotpDevice.objects.filter(
        teacher=user, confirmed_at__isnull=False
    ).first()
    if device is None:
        # حالةٌ لا ينبغي أن تقع: البوابة لا تُستدعى إلا لمن له جهاز نافذ.
        _clear_pending(request)
        return redirect("reports:login")

    submitted = (request.POST.get("code") or "").strip()
    if _consume_code(device, submitted) or _consume_recovery_code(device, submitted):
        next_url = _safe_login_next_url(request.session.get(PENDING_NEXT_SESSION_KEY))
        _clear_pending(request)
        login(request, user)
        return redirect(next_url or "reports:home")

    messages.error(request, "الرمز غير صحيح أو مستعمَل.")
    return render(request, "reports/totp_challenge.html", {})


def _consume_code(device: TeacherTotpDevice, submitted: str) -> bool:
    secret = decrypt_secret(device.secret_encrypted)
    if secret is None:
        # مفتاح التعمية تغيّر: لا يمكن التحقق، فلا يُقبل.
        return False

    counter = verify_code(secret, submitted, last_used_counter=device.last_used_counter)
    if counter is None:
        return False

    # يُختم العدّاد فوراً: الرمز نفسه لا يُقبل مرة ثانية داخل نافذته.
    TeacherTotpDevice.objects.filter(pk=device.pk).update(
        last_used_counter=counter, last_used_at=timezone.now()
    )
    return True


def _consume_recovery_code(device: TeacherTotpDevice, submitted: str) -> bool:
    if not submitted:
        return False

    match = device.recovery_codes.filter(
        code_hash=hash_recovery_code(submitted), used_at__isnull=True
    ).first()
    if match is None:
        return False

    # ``update`` مشروطٌ بأنه ما زال غير مستعمَل: طلبان متزامنان بالرمز نفسه
    # لا يمرّان معاً.
    claimed = TotpRecoveryCode.objects.filter(pk=match.pk, used_at__isnull=True).update(
        used_at=timezone.now()
    )
    if not claimed:
        return False

    with soft_fail("totp.recovery_used_notice", user_id=device.teacher_id):
        from ..utils import create_system_notification

        create_system_notification(
            title="دخول برمز استرجاع",
            message=(
                "استُعمل أحد رموز الاسترجاع للدخول إلى حسابك. إن لم تكن أنت، "
                "غيّر كلمة المرور وأعد تفعيل العامل الثاني فوراً."
            ),
            teacher_ids=[device.teacher_id],
            is_important=True,
        )
    return True
