"""School report writing tools exposed to subscribed personal workspaces."""

from __future__ import annotations

import json
import logging

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST
from django_ratelimit.decorators import ratelimit

from reports.ai_features import (
    FEATURE_REPORT_IMPROVEMENT,
    FEATURE_VOICE_REPORT,
    platform_ai_toggle_enabled,
)
from reports.ai_usage import ai_usage_context
from reports.report_ai import (
    REPORT_AI_DAILY_LIMIT,
    ReportAIError,
    ReportAIUnavailable,
    improve_report_text as improve_report_text_with_ai,
    validate_report_text,
)
from reports.report_limits import REPORT_DETAILS_MAX_LENGTH, REPORT_DETAILS_RECOMMENDED_LENGTH
from reports.voice_report import (
    VOICE_REPORT_DAILY_LIMIT,
    VoiceReportError,
    VoiceReportUnavailable,
    is_enabled as voice_report_is_enabled,
    polish_dictation,
    transcribe_audio,
    validate_audio_upload,
    voice_report_daily_limit,
)

from .assistant_quota import (
    PersonalAssistantQuotaUnavailable,
    daily_remaining,
    release_daily_slot,
    reserve_daily_slot,
)
from .models import PersonalWorkspace
from .services import ensure_personal_subscription


logger = logging.getLogger(__name__)


def personal_assistant_template_context(user, subscription) -> dict[str, int | bool]:
    """Expose only the paid tools available in the current personal plan."""
    is_current = bool(subscription and subscription.is_current)
    plan = subscription.plan if is_current and subscription.plan.price > 0 else None
    ai_limit = min(int(plan.report_ai_daily_limit or 0), REPORT_AI_DAILY_LIMIT) if plan else 0
    voice_limit = min(
        int(plan.voice_report_daily_limit or 0), VOICE_REPORT_DAILY_LIMIT, voice_report_daily_limit()
    ) if plan else 0
    ai_enabled = bool(
        ai_limit
        and platform_ai_toggle_enabled(FEATURE_REPORT_IMPROVEMENT)
        and getattr(settings, "REPORT_AI_ENABLED", False)
        and getattr(settings, "OPENAI_API_KEY", "")
    )
    voice_enabled = bool(
        voice_limit
        and platform_ai_toggle_enabled(FEATURE_VOICE_REPORT)
        and voice_report_is_enabled()
    )
    return {
        "report_ai_enabled": ai_enabled,
        "report_ai_daily_limit": ai_limit,
        "report_ai_daily_remaining": daily_remaining("improvement", user.pk, ai_limit) if ai_enabled else 0,
        "report_details_recommended_length": REPORT_DETAILS_RECOMMENDED_LENGTH,
        "report_details_max_length": REPORT_DETAILS_MAX_LENGTH,
        "voice_report_enabled": voice_enabled,
        "voice_report_daily_limit": voice_limit,
        "voice_report_daily_remaining": daily_remaining("voice", user.pk, voice_limit) if voice_enabled else 0,
        "voice_report_max_seconds": int(getattr(settings, "VOICE_REPORT_MAX_SECONDS", 180)),
        "voice_report_max_bytes": int(getattr(settings, "VOICE_REPORT_MAX_BYTES", 10 * 1024 * 1024)),
        "voice_report_pwa_only": bool(getattr(settings, "VOICE_REPORT_PWA_ONLY", True)),
    }


def _json(payload: dict, *, status: int = 200) -> JsonResponse:
    response = JsonResponse(payload, status=status, json_dumps_params={"ensure_ascii": False})
    response["Cache-Control"] = "no-store"
    return response


def _entitled_limit(request, kind: str) -> tuple[int, JsonResponse | None]:
    workspace = PersonalWorkspace.objects.filter(owner=request.user).first()
    if workspace is None:
        return 0, _json(
            {"ok": False, "reason": "subscription_required", "message": "أنشئ مساحتك الشخصية أولًا."},
            status=403,
        )
    subscription = ensure_personal_subscription(workspace)
    if not subscription.is_current:
        return 0, _json(
            {"ok": False, "reason": "subscription_required", "message": "اشتراك المساحة الشخصية غير نشط حاليًا."},
            status=403,
        )
    if subscription.plan.price <= 0:
        return 0, _json(
            {"ok": False, "reason": "plan_upgrade_required", "message": "هذه الأداة غير مشمولة في باقتك الشخصية."},
            status=403,
        )
    field = "report_ai_daily_limit" if kind == "improvement" else "voice_report_daily_limit"
    entitlement = int(getattr(subscription.plan, field, 0) or 0)
    if entitlement <= 0:
        return 0, _json(
            {"ok": False, "reason": "plan_upgrade_required", "message": "هذه الأداة غير مشمولة في باقتك الشخصية."},
            status=403,
        )
    limit = (
        min(entitlement, VOICE_REPORT_DAILY_LIMIT, voice_report_daily_limit())
        if kind == "voice" else min(entitlement, REPORT_AI_DAILY_LIMIT)
    )
    return limit, None


@login_required(login_url="reports:login")
@never_cache
@ratelimit(key="user", rate="20/m", method="POST", block=True)
@require_POST
def improve_report_text(request):
    if not (
        platform_ai_toggle_enabled(FEATURE_REPORT_IMPROVEMENT)
        and getattr(settings, "REPORT_AI_ENABLED", False)
        and getattr(settings, "OPENAI_API_KEY", "")
    ):
        return _json({"ok": False, "message": "ميزة تحسين التقارير غير متاحة حاليًا."}, status=404)
    limit, denied = _entitled_limit(request, "improvement")
    if denied is not None:
        return denied
    if request.content_type != "application/json":
        return _json({"ok": False, "message": "صيغة الطلب غير صحيحة."}, status=415)
    if len(request.body) > 30000:
        return _json({"ok": False, "message": "النص أطول من الحد المسموح."}, status=413)
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = None
    if not isinstance(payload, dict):
        return _json({"ok": False, "message": "تعذر قراءة نص التقرير."}, status=400)
    try:
        original_text = validate_report_text(payload.get("text"))
    except ReportAIError as exc:
        return _json({"ok": False, "message": str(exc)}, status=400)

    try:
        remaining = reserve_daily_slot("improvement", request.user.pk, limit)
    except PersonalAssistantQuotaUnavailable:
        return _json({"ok": False, "message": "تعذر التحقق من رصيد التحسينات الآن. حاول مرة أخرى بعد قليل."}, status=503)
    if remaining is None:
        return _json({
            "ok": False,
            "message": "استخدمت تحسيناتك المتاحة اليوم. يعود الرصيد تلقائيًا غدًا.",
            "remaining": 0,
            "daily_limit": limit,
        }, status=429)

    try:
        with ai_usage_context(school=None, teacher=request.user):
            improved_text = improve_report_text_with_ai(original_text)
    except (ReportAIUnavailable, ReportAIError) as exc:
        release_daily_slot("improvement", request.user.pk)
        return _json({
            "ok": False,
            "message": str(exc),
            "remaining": daily_remaining("improvement", request.user.pk, limit),
            "daily_limit": limit,
        }, status=503 if isinstance(exc, ReportAIUnavailable) else 400)
    except Exception:
        release_daily_slot("improvement", request.user.pk)
        logger.exception("Unexpected personal report improvement failure")
        return _json({"ok": False, "message": "تعذر تحسين النص الآن. حاول مرة أخرى بعد قليل."}, status=503)

    return _json({
        "ok": True,
        "improved_text": improved_text,
        "remaining": remaining,
        "daily_limit": limit,
        "recommended_length": REPORT_DETAILS_RECOMMENDED_LENGTH,
        "max_length": REPORT_DETAILS_MAX_LENGTH,
    })


@login_required(login_url="reports:login")
@never_cache
@ratelimit(key="user", rate="6/m", method="POST", block=True)
@require_POST
def transcribe_report_voice(request):
    if not (
        platform_ai_toggle_enabled(FEATURE_VOICE_REPORT)
        and voice_report_is_enabled()
        and voice_report_daily_limit() > 0
    ):
        return _json({"ok": False, "message": "خدمة التفريغ الصوتي غير متاحة حاليًا."}, status=404)
    limit, denied = _entitled_limit(request, "voice")
    if denied is not None:
        return denied
    if getattr(settings, "VOICE_REPORT_PWA_ONLY", True) and (
        request.headers.get("X-Tawtheeq-Surface") or ""
    ).strip().lower() != "standalone":
        return _json({
            "ok": False,
            "message": "التسجيل الصوتي متاح داخل تطبيق توثيق المثبَّت على جهازك.",
            "reason": "pwa_required",
        }, status=403)

    try:
        audio_bytes, extension = validate_audio_upload(request.FILES.get("audio"))
    except VoiceReportError as exc:
        return _json({
            "ok": False,
            "message": str(exc),
            "remaining": daily_remaining("voice", request.user.pk, limit),
            "daily_limit": limit,
        }, status=400)
    try:
        remaining = reserve_daily_slot("voice", request.user.pk, limit)
    except PersonalAssistantQuotaUnavailable:
        return _json({"ok": False, "message": "تعذر التحقق من رصيد التفريغ الآن. حاول مرة أخرى بعد قليل."}, status=503)
    if remaining is None:
        return _json({
            "ok": False,
            "message": f"استخدمت تسجيلاتك الـ{limit} المتاحة اليوم. يعود الرصيد تلقائيًا غدًا.",
            "remaining": 0,
            "daily_limit": limit,
        }, status=429)

    try:
        with ai_usage_context(school=None, teacher=request.user):
            raw_text = transcribe_audio(audio_bytes, extension)
            text = polish_dictation(raw_text)
    except (VoiceReportUnavailable, VoiceReportError) as exc:
        release_daily_slot("voice", request.user.pk)
        return _json({
            "ok": False,
            "message": str(exc),
            "remaining": daily_remaining("voice", request.user.pk, limit),
            "daily_limit": limit,
        }, status=503 if isinstance(exc, VoiceReportUnavailable) else 400)
    except Exception:
        release_daily_slot("voice", request.user.pk)
        logger.exception("Unexpected personal report voice failure")
        return _json({"ok": False, "message": "تعذر تفريغ التسجيل الآن. حاول مرة أخرى بعد قليل."}, status=503)

    logger.info("Personal report voice transcription user_id=%s chars=%s", request.user.pk, len(text))
    return _json({
        "ok": True,
        "text": text,
        "raw_text": raw_text,
        "remaining": remaining,
        "daily_limit": limit,
    })
