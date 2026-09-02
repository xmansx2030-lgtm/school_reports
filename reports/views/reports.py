# reports/views/reports.py
# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import timedelta
import hashlib
import json
import logging
import re

from django.contrib.auth.decorators import user_passes_test
from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse
from django.db import IntegrityError
from django.db.models import F, Q
from django.views.decorators.cache import never_cache

from ..report_ai import (
    REPORT_AI_DAILY_LIMIT,
    ReportAIError,
    ReportAIUnavailable,
    improve_report_text as improve_report_text_with_ai,
    release_report_ai_daily_slot,
    report_ai_daily_remaining,
    reserve_report_ai_daily_slot,
    validate_report_text,
)
from ..report_limits import (
    REPORT_DETAILS_MAX_LENGTH,
    REPORT_DETAILS_RECOMMENDED_LENGTH,
)
from ..voice_report import (
    VoiceReportError,
    VoiceReportUnavailable,
    is_enabled as voice_report_is_enabled,
    polish_dictation,
    release_voice_report_daily_slot,
    reserve_voice_report_daily_slot,
    transcribe_audio,
    validate_audio_upload,
    voice_report_daily_limit,
    voice_report_daily_remaining,
)
from ..gender_labels import school_gender_labels, school_gender_template_context
from ..generated_exports import async_exports_enabled, enqueue_generated_export
from ..models import GeneratedExportJob

from core.observability import report_degraded as _degraded, soft_call, soft_fail

from ._helpers import *
from .export_jobs import export_creation_is_limited, generated_export_job_response
# Star imports skip underscore names; the size formatter is needed by name.
from ..services_archive import _human_size
from ._helpers import (
    _is_staff, _is_staff_or_officer, _is_manager_in_school,
    _parse_date_safe, _filter_by_school, _safe_next_url, _safe_redirect,
    _private_comment_role_label, _model_has_field,
    _get_active_school,
    _ensure_achievement_sections,
    _clean_query_value, _clean_query_params,
)


def _report_locked_reason(report, school, *, action: str) -> str:
    """سبب منع إجراءٍ على تقرير خرج من يد مُعِدّه — بصيغة تقول ما العمل.

    ``action`` مصدرٌ صريح («حذف التقرير») لا فعلٌ يُصرَّف في النص. ورسالةُ
    منعٍ بلا مخرج تدفع صاحبها إلى الدعم، وما يحتاجه أن يعرف الطريق: السحب
    بيده ما دام لم يُبَتّ فيه، والإعادة بيد مراجعه بعد ذلك.
    """
    from ..views._helpers import _school_manager_label

    if getattr(report, "is_final", False):
        return f"تعذّر {action}: التقرير معتمَد، والمعتمَد سجلٌّ لا يُغيَّر."
    return (
        f"تعذّر {action}: التقرير مُرسل وينتظر القرار. اسحبه للتعديل من صفحة "
        f"الاعتماد، أو اطلب من {_school_manager_label(school)} إعادته إليك."
    )


def _report_evidence_post_data(request: HttpRequest):
    """طبقة توافق لعميل فتح نموذج التقرير قبل إطلاق formset الشواهد."""
    data = request.POST
    if "evidence-TOTAL_FORMS" in data:
        return data
    data = data.copy()
    data["evidence-TOTAL_FORMS"] = "0"
    data["evidence-INITIAL_FORMS"] = "0"
    data["evidence-MIN_NUM_FORMS"] = "0"
    data["evidence-MAX_NUM_FORMS"] = "8"
    return data

from ..utils import _resolve_department_for_category, _build_head_decision
from core import opmetrics
from ..ai_features import (
    FEATURE_REPORT_IMPROVEMENT,
    FEATURE_VOICE_REPORT,
    platform_ai_toggle_enabled,
)


logger = logging.getLogger(__name__)

_SHARE_EXPIRY_OPTIONS = (1, 7, 14, 30, 90)


def _share_expiry_choices(default_days: int) -> list[int]:
    return sorted({*_SHARE_EXPIRY_OPTIONS, max(1, int(default_days or 7))})


def _requested_share_expiry_days(request, default_days: int) -> int:
    try:
        selected = int(request.POST.get("expiry_days") or default_days)
    except (TypeError, ValueError):
        return default_days
    return selected if selected in _share_expiry_choices(default_days) else default_days


def _leadership_section_for_new_report(request, active_school):
    """Resolve an optional leadership destination without trusting form input."""
    raw_section_id = request.POST.get("leadership_section") or request.GET.get(
        "leadership_section"
    )
    if not raw_section_id:
        return None
    if active_school is None or not is_school_manager(
        request.user, active_school=active_school
    ):
        raise Http404
    return get_object_or_404(
        LeadershipPortfolioSection.objects.select_related("portfolio"),
        pk=raw_section_id,
        portfolio__school=active_school,
        portfolio__academic_year=(active_school.current_academic_year or "").strip(),
    )


def _report_ai_template_context(user) -> dict[str, int | bool]:
    return {
        "report_ai_enabled": bool(
            platform_ai_toggle_enabled(FEATURE_REPORT_IMPROVEMENT)
            and getattr(settings, "REPORT_AI_ENABLED", False)
            and getattr(settings, "OPENAI_API_KEY", "")
        ),
        "report_ai_daily_limit": REPORT_AI_DAILY_LIMIT,
        "report_ai_daily_remaining": report_ai_daily_remaining(user.pk),
        "report_details_recommended_length": REPORT_DETAILS_RECOMMENDED_LENGTH,
        "report_details_max_length": REPORT_DETAILS_MAX_LENGTH,
        **_voice_report_template_context(user),
    }


def _voice_report_feature_enabled() -> bool:
    return bool(platform_ai_toggle_enabled(FEATURE_VOICE_REPORT) and voice_report_is_enabled())


def _voice_report_template_context(user) -> dict[str, int | bool]:
    return {
        "voice_report_enabled": _voice_report_feature_enabled(),
        "voice_report_daily_limit": voice_report_daily_limit(),
        "voice_report_daily_remaining": voice_report_daily_remaining(user.pk),
        "voice_report_max_seconds": int(getattr(settings, "VOICE_REPORT_MAX_SECONDS", 180)),
        "voice_report_max_bytes": int(getattr(settings, "VOICE_REPORT_MAX_BYTES", 10 * 1024 * 1024)),
        "voice_report_pwa_only": bool(getattr(settings, "VOICE_REPORT_PWA_ONLY", True)),
    }


def _request_is_from_installed_app(request: HttpRequest) -> bool:
    """هل جاء الطلب من التطبيق المثبَّت؟

    الترويسة يضعها سكربت المسجّل حين يكون العرض ``standalone``. وهي **إشارة
    منتَج لا حاجز أمني**: العميل يملك ترويساته ويستطيع تزويرها. القيد الذي
    يحمي التكلفة فعلاً هو الحصة اليومية المحسوبة على الخادم، وهذا يمنع ظهور
    الميزة واستعمالها من متصفّح عادي في المسار الطبيعي لا أكثر.
    """
    surface = (request.headers.get("X-Tawtheeq-Surface") or "").strip().lower()
    return surface == "standalone"


def _notify_report_created(report, active_school):
    """إشعار مدير المدرسة ورئيس القسم عند إنشاء تقرير جديد."""
    try:
        from ..utils import create_system_notification

        school = getattr(report, "school", active_school)
        if school is None:
            return

        recipients = set()
        # مدراء المدرسة
        manager_ids = SchoolMembership.objects.filter(
            school=school,
            role_type=SchoolMembership.RoleType.MANAGER,
            is_active=True,
        ).values_list("teacher_id", flat=True)
        recipients.update(manager_ids)

        # رئيس القسم المرتبط بنوع التقرير
        if DepartmentMembership is not None and getattr(report, "category_id", None):
            officer_ids = DepartmentMembership.objects.filter(
                department__reporttypes=report.category_id,
                role_type=DepartmentMembership.OFFICER,
            ).values_list("teacher_id", flat=True)
            recipients.update(officer_ids)

        # لا نشعر صاحب التقرير نفسه
        recipients.discard(getattr(report, "teacher_id", None))

        if not recipients:
            return

        teacher_name = getattr(report.teacher, "name", "") if report.teacher else ""
        category_name = getattr(report.category, "name", "") if getattr(report, "category", None) else ""
        added_verb = "أضافت" if school_gender_labels(school)["is_girls"] else "أضاف"
        create_system_notification(
            title=f"📝 تقرير جديد: {report.title[:80]}",
            message=f"{added_verb} {teacher_name} تقريراً جديداً ({category_name}).",
            school=school,
            teacher_ids=list(recipients),
        )
    except Exception:
        logger.exception("Failed to send report creation notification")


# =========================
# التقارير: إضافة/عرض/إدارة
# =========================
@login_required(login_url="reports:login")
@ratelimit(key="user", rate="30/h", method="POST", block=True)
@require_http_methods(["GET", "POST"])
def add_report(request: HttpRequest) -> HttpResponse:
    active_school = _get_active_school(request)
    leadership_section = _leadership_section_for_new_report(request, active_school)
    response_status = 200

    def _has_report_types(bound_form) -> bool:
        """Empty choices mean the school has not defined report types for this
        teacher yet. The form then cannot be completed, so the page has to say
        why instead of showing a dead select."""
        try:
            return any(value for value, _label in bound_form.fields["category"].choices)
        except Exception:
            return True
    if request.method == "POST":
        form = ReportForm(request.POST, request.FILES, active_school=active_school)
        evidence_formset = ReportEvidenceFormSet(
            _report_evidence_post_data(request), request.FILES, instance=form.instance, prefix="evidence"
        )
        if form.is_valid() and evidence_formset.is_valid():
            submission_key = form.cleaned_data.get("client_submission_id")
            if submission_key:
                duplicate = Report.objects.filter(
                    teacher=request.user,
                    school=active_school,
                    submission_key=submission_key,
                ).first()
                if duplicate is not None:
                    messages.success(request, "التقرير محفوظ مسبقًا، وتم فتح النسخة الموجودة.")
                    return redirect("reports:my_reports")
            capacity_error = archive_storage_capacity_error(active_school, request.FILES.values())
            if capacity_error:
                messages.error(request, capacity_error)
                return render(
                    request,
                    "reports/add_report.html",
                    {
                        "form": form,
                        "evidence_formset": evidence_formset,
                        "leadership_section": leadership_section,
                        "has_report_types": _has_report_types(form),
                        **_report_ai_template_context(request.user),
                    },
                    status=(
                        422
                        if request.headers.get("X-Requested-With") == "XMLHttpRequest"
                        else 200
                    ),
                )

            report = form.save(commit=False)
            if submission_key:
                report.submission_key = submission_key
            report.teacher = request.user
            if hasattr(report, "school") and active_school is not None:
                report.school = active_school

            # حماية حقل "المنفذ": يُحفظ دائمًا باسم المستخدم الحالي ولا نقبل أي قيمة مرسلة من الفورم.
            teacher_name_final = (getattr(request.user, "name", "") or "").strip()
            if not teacher_name_final:
                teacher_name_final = (getattr(request.user, "username", "") or str(request.user) or "").strip()
            teacher_name_final = teacher_name_final[:120]
            if hasattr(report, "teacher_name"):
                report.teacher_name = teacher_name_final

            # دورة الاعتماد اختيار واعٍ لكل مدرسة. مدرسة لم تفعّلها يبقى تقريرها
            # نهائياً بمجرد حفظه كما كان — فترقية المنصة لا يجوز أن تُخفي عمل كل
            # معلّم خلف موافقة لم يطلبها أحد.
            from ..model_parts.approvals import ApprovalState

            if getattr(active_school, "report_approval_enabled", False):
                report.approval_state = ApprovalState.DRAFT
            else:
                report.approval_state = ApprovalState.APPROVED
                report.decided_at = timezone.now()

            try:
                with transaction.atomic():
                    report.save()
                    evidence_formset.instance = report
                    evidence_formset.save()
                    if leadership_section is not None:
                        LeadershipEvidenceReport.objects.get_or_create(
                            section=leadership_section,
                            report=report,
                        )
            except IntegrityError:
                duplicate = (
                    Report.objects.filter(
                        teacher=request.user,
                        school=active_school,
                        submission_key=submission_key,
                    ).first()
                    if submission_key
                    else None
                )
                if duplicate is None:
                    raise
                messages.success(request, "التقرير محفوظ مسبقًا، وتم منع إنشاء نسخة مكررة.")
                return redirect("reports:my_reports")
            sync_school_archive_storage_usage(getattr(report, "school", active_school))

            # إشعار مدير المدرسة ورئيس القسم بتقرير جديد
            _notify_report_created(report, active_school)

            logger.info(
                "Report created report_id=%s user_id=%s school_id=%s trace_id=%s",
                getattr(report, "id", None),
                getattr(request.user, "id", None),
                getattr(getattr(report, "school", None), "id", None),
                getattr(request, "trace_id", None),
            )
            opmetrics.increment("report.create.success")

            if leadership_section is not None:
                messages.success(
                    request,
                    "تم إنشاء التقرير وإضافته إلى محور الأداء القيادي ✅",
                )
                return redirect(
                    "reports:leadership_portfolio_detail",
                    pk=leadership_section.portfolio_id,
                )
            messages.success(request, "تمت إضافة التقرير.")
            return redirect("reports:my_reports")
        logger.warning(
            "Report creation failed validation user_id=%s school_id=%s trace_id=%s errors=%s",
            getattr(request.user, "id", None),
            getattr(active_school, "id", None),
            getattr(request, "trace_id", None),
            str(getattr(form, "errors", ""))[:500],
        )
        opmetrics.increment("report.create.failure")
        messages.error(request, "فضلاً تحقق من الحقول وأعد المحاولة.")
        # The editor submits with XHR.  Returning a validation page as HTTP 200
        # made the client treat it as a successful save and redirect to the
        # blank add page.  A semantic 422 keeps the response body (and inline
        # errors) while making the failure unambiguous to the browser.
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            response_status = 422
    else:
        form = ReportForm(active_school=active_school)
        evidence_formset = ReportEvidenceFormSet(instance=form.instance, prefix="evidence")

    return render(
        request,
        "reports/add_report.html",
        {
            "form": form,
            "evidence_formset": evidence_formset,
            "leadership_section": leadership_section,
            "has_report_types": _has_report_types(form),
            **_report_ai_template_context(request.user),
        },
        status=response_status,
    )


@login_required(login_url="reports:login")
@never_cache
@require_http_methods(["POST"])
def improve_report_text(request: HttpRequest) -> JsonResponse:
    """Improve one report description without reading or changing saved reports."""
    if not platform_ai_toggle_enabled(FEATURE_REPORT_IMPROVEMENT):
        response = JsonResponse(
            {"ok": False, "message": "ميزة تحسين التقارير غير متاحة حالياً."},
            status=404,
            json_dumps_params={"ensure_ascii": False},
        )
        response["Cache-Control"] = "no-store"
        return response

    if request.content_type != "application/json":
        return JsonResponse(
            {"ok": False, "message": "صيغة الطلب غير صحيحة."},
            status=415,
            json_dumps_params={"ensure_ascii": False},
        )
    if len(request.body) > 30000:
        return JsonResponse(
            {"ok": False, "message": "النص أطول من الحد المسموح."},
            status=413,
            json_dumps_params={"ensure_ascii": False},
        )

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = None
    if not isinstance(payload, dict):
        return JsonResponse(
            {"ok": False, "message": "تعذر قراءة نص التقرير."},
            status=400,
            json_dumps_params={"ensure_ascii": False},
        )

    try:
        original_text = validate_report_text(payload.get("text"))
    except ReportAIError as exc:
        response = JsonResponse(
            {"ok": False, "message": str(exc)},
            status=400,
            json_dumps_params={"ensure_ascii": False},
        )
        response["Cache-Control"] = "no-store"
        return response

    try:
        remaining = reserve_report_ai_daily_slot(request.user.pk)
    except ReportAIUnavailable as exc:
        response = JsonResponse(
            {"ok": False, "message": str(exc)},
            status=503,
            json_dumps_params={"ensure_ascii": False},
        )
        response["Cache-Control"] = "no-store"
        return response
    if remaining is None:
        response = JsonResponse(
            {
                "ok": False,
                "message": "استخدمت تحسيناتك الثلاثة المتاحة اليوم. يعود الرصيد تلقائيًا غدًا.",
                "remaining": 0,
                "daily_limit": REPORT_AI_DAILY_LIMIT,
            },
            status=429,
            json_dumps_params={"ensure_ascii": False},
        )
        response["Cache-Control"] = "no-store"
        return response

    try:
        improved_text = improve_report_text_with_ai(original_text)
    except ReportAIUnavailable as exc:
        release_report_ai_daily_slot(request.user.pk)
        response = JsonResponse(
            {
                "ok": False,
                "message": str(exc),
                "remaining": report_ai_daily_remaining(request.user.pk),
                "daily_limit": REPORT_AI_DAILY_LIMIT,
            },
            status=503,
            json_dumps_params={"ensure_ascii": False},
        )
    except ReportAIError as exc:
        release_report_ai_daily_slot(request.user.pk)
        response = JsonResponse(
            {
                "ok": False,
                "message": str(exc),
                "remaining": report_ai_daily_remaining(request.user.pk),
                "daily_limit": REPORT_AI_DAILY_LIMIT,
            },
            status=400,
            json_dumps_params={"ensure_ascii": False},
        )
    else:
        response = JsonResponse(
            {
                "ok": True,
                "improved_text": improved_text,
                "remaining": remaining,
                "daily_limit": REPORT_AI_DAILY_LIMIT,
                "recommended_length": REPORT_DETAILS_RECOMMENDED_LENGTH,
                "max_length": REPORT_DETAILS_MAX_LENGTH,
            },
            json_dumps_params={"ensure_ascii": False},
        )
    response["Cache-Control"] = "no-store"
    return response

def _voice_json(payload: dict, *, status: int = 200) -> JsonResponse:
    response = JsonResponse(payload, status=status, json_dumps_params={"ensure_ascii": False})
    response["Cache-Control"] = "no-store"
    return response


@login_required(login_url="reports:login")
@never_cache
@ratelimit(key="user", rate="6/m", method="POST", block=True)
@require_http_methods(["POST"])
def transcribe_report_voice(request: HttpRequest) -> JsonResponse:
    """يحوّل تسجيلاً صوتياً إلى نصّ تقرير مقترح، دون حفظ الصوت ولا التقرير.

    الترتيب مقصود: البوابة، ثم التحقق من الملف، ثم حجز الحصة. حجزُ الحصة قبل
    التحقق كان يعني أن ملفاً بصيغة خاطئة يستهلك محاولةً من رصيد المعلّم.
    """
    if not _voice_report_feature_enabled():
        return _voice_json(
            {"ok": False, "message": "خدمة التفريغ الصوتي غير متاحة حاليًا."},
            status=404,
        )

    if getattr(settings, "VOICE_REPORT_PWA_ONLY", True) and not _request_is_from_installed_app(request):
        return _voice_json(
            {
                "ok": False,
                "message": "التسجيل الصوتي متاح داخل تطبيق توثيق المثبَّت على جهازك.",
                "reason": "pwa_required",
            },
            status=403,
        )

    limit = voice_report_daily_limit()
    upload = request.FILES.get("audio")

    try:
        audio_bytes, extension = validate_audio_upload(upload)
    except VoiceReportError as exc:
        return _voice_json(
            {
                "ok": False,
                "message": str(exc),
                "remaining": voice_report_daily_remaining(request.user.pk),
                "daily_limit": limit,
            },
            status=400,
        )

    try:
        remaining = reserve_voice_report_daily_slot(request.user.pk)
    except VoiceReportUnavailable as exc:
        return _voice_json({"ok": False, "message": str(exc)}, status=503)

    if remaining is None:
        return _voice_json(
            {
                "ok": False,
                "message": f"استخدمت تسجيلاتك الـ{limit} المتاحة اليوم. يعود الرصيد تلقائيًا غدًا.",
                "remaining": 0,
                "daily_limit": limit,
            },
            status=429,
        )

    try:
        raw_text = transcribe_audio(audio_bytes, extension)
        text = polish_dictation(raw_text)
    except VoiceReportUnavailable as exc:
        release_voice_report_daily_slot(request.user.pk)
        opmetrics.increment("report.voice.failure")
        return _voice_json(
            {
                "ok": False,
                "message": str(exc),
                "remaining": voice_report_daily_remaining(request.user.pk),
                "daily_limit": limit,
            },
            status=503,
        )
    except VoiceReportError as exc:
        release_voice_report_daily_slot(request.user.pk)
        opmetrics.increment("report.voice.failure")
        return _voice_json(
            {
                "ok": False,
                "message": str(exc),
                "remaining": voice_report_daily_remaining(request.user.pk),
                "daily_limit": limit,
            },
            status=400,
        )

    logger.info(
        "Report voice transcription user_id=%s chars=%s trace_id=%s",
        getattr(request.user, "id", None),
        len(text),
        getattr(request, "trace_id", None),
    )
    opmetrics.increment("report.voice.success")
    # ‏``raw_text`` يصل المعلّم كما وصل من التفريغ: نصٌّ أنيق لا يطابق ما قيل
    # يبدو صحيحاً إن لم يكن بجواره الأصل. وبغيابه لا يستطيع أحد — لا المعلّم
    # ولا السجلّات — تمييز خطأ التفريغ من خطأ التجميل.
    return _voice_json(
        {
            "ok": True,
            "text": text,
            "raw_text": raw_text,
            "remaining": remaining,
            "daily_limit": limit,
        }
    )


@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def my_reports(request: HttpRequest) -> HttpResponse:
    active_school = _get_active_school(request)
    qs = get_teacher_reports_queryset(user=request.user, active_school=active_school)
    start_date = _parse_date_safe(request.GET.get("start_date"))
    end_date = _parse_date_safe(request.GET.get("end_date"))
    q = _clean_query_value(request.GET.get("q"))

    qs = apply_teacher_report_filters(qs, start_date=start_date, end_date=end_date, q=q)
    try:
        ttl = int(getattr(settings, "MY_REPORTS_STATS_CACHE_TTL", 20 if not settings.DEBUG else 5) or 0)
    except Exception:
        ttl = 10

    stats = None
    if ttl > 0:
        try:
            sid = int(getattr(active_school, "id", 0) or 0)
            key_basis = f"u={int(request.user.id)}|s={sid}|sd={start_date}|ed={end_date}|q={q}"
            key_hash = hashlib.sha256(key_basis.encode("utf-8")).hexdigest()
            cache_key = f"reports:my-stats:v1:{key_hash}"
            stats = cache.get(cache_key)
            if stats is None:
                stats = teacher_report_stats(qs)
                cache.set(cache_key, stats, ttl)
        except Exception:
            stats = teacher_report_stats(qs)
    else:
        stats = teacher_report_stats(qs)
    reports_page = svc_paginate(qs, per_page=10, page=request.GET.get("page", 1))

    qs_params = _clean_query_params(request.GET)

    return render(
        request,
        "reports/my_reports.html",
        {
            "reports": reports_page,
            "qs": qs_params,
            "start_date": start_date.isoformat() if start_date else "",
            "end_date": end_date.isoformat() if end_date else "",
            "q": q,
            "stats": stats,
            # كشفُ التقارير هو المكان الذي يقصده المعلّم ليعمل على تقاريره،
            # فحالةُ كلٍّ منها وبابُ إرسالها ينتميان إليه. وقبل هذا كان
            # المفتاح مفعّلاً والكشف صامتاً عنه: يكتب المعلّم فتُحفظ مسودةً
            # ولا شاشةَ تقول له إنها لم تصل أحداً ولا كيف يرسلها.
            "report_approval_enabled": bool(
                getattr(active_school, "report_approval_enabled", False)
            ),
        },
    )

@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def admin_reports(request: HttpRequest) -> HttpResponse:
    """تقارير المدرسة — للمدير كاملةً، ولمن مُنح المراجعة في نطاقه.

    كان الوصول مقصوراً على المدير، فوكيلٌ مُنح ``review_reports`` يراجع التقارير
    **فرداً فرداً** من صندوق الاعتماد ولا يملك كشفاً يسأل منه «ما وثّقه قسمي هذا
    الشهر؟» — والصندوق يعرض ما ينتظر قراراً لا ما أُنجز.

    شاشةٌ واحدة لا شاشتان: الفرق بين المدير والوكيل **مدى** ما يراه وما يملكه
    عليه، لا شكلُ الشاشة. والمدى يُحسم في الاستعلام أدناه، والملكية في أعلام
    كل صف.
    """
    from .. import capabilities as caps
    from ..permissions import capability_source, supervised_department_ids

    active_school = _get_active_school(request)
    is_manager = bool(
        getattr(request.user, "is_superuser", False)
        or is_school_manager(request.user, active_school=active_school)
    )
    may_review = bool(
        active_school is not None
        and capability_source(request.user, caps.REVIEW_REPORTS, active_school) is not None
    )
    if not (is_manager or may_review):
        messages.error(request, "لا تملك صلاحية الوصول إلى هذه الصفحة.")
        return redirect("reports:home")

    cats = allowed_categories_for(request.user, active_school)
    qs = get_admin_reports_queryset(user=request.user, active_school=active_school)

    # النطاق قبل المرشّح: يُضيَّق الاستعلام الأساس أولاً ثم تُبنى فوقه مرشّحات
    # المستخدم. وأقسامٌ فارغة تعني كشفاً فارغاً لا كشف المدرسة كاملاً — القاعدة
    # نفسها في كل موضع يقرأ ``supervised_department_ids``.
    if not is_manager:
        supervised = supervised_department_ids(request.user, active_school)
        qs = (
            qs.filter(category__departments__id__in=supervised).distinct()
            if supervised
            else qs.none()
        )

    start_date = _parse_date_safe(request.GET.get("start_date"))
    end_date = _parse_date_safe(request.GET.get("end_date"))
    teacher_name = _clean_query_value(request.GET.get("teacher_name"))
    category = _clean_query_value(request.GET.get("category")).lower()

    qs = apply_admin_report_filters(
        qs,
        start_date=start_date,
        end_date=end_date,
        teacher_name=teacher_name,
        category=category,
        cats=cats,
    )

    allowed_choices = get_reporttype_choices(active_school=active_school) if (HAS_RTYPE and ReportType is not None) else []
    reports_page = svc_paginate(qs, per_page=20, page=request.GET.get("page", 1))

    # بعد تقييد الـ queryset حسب المدرسة/النطاق، تُحسم الإجراءات مرة واحدة لكل
    # الصفوف بلا استعلام لكل تقرير.
    #
    # **الوكيل يرى ولا يملك.** صلاحيته «مراجعة التقارير وإعادتها **دون اعتماد
    # نهائي**»، فالحذف والتعديل والمشاركة تبقى للمدير — والمراجعة تُمارَس من
    # صندوق الاعتماد حيث تُسجَّل في دورة القرار.
    for report in reports_page:
        report.user_can_delete = is_manager
        report.user_can_edit = is_manager
        report.user_can_share = is_manager

    context = {
        "reports": reports_page,
        "start_date": start_date.isoformat() if start_date else "",
        "end_date": end_date.isoformat() if end_date else "",
        "teacher_name": teacher_name,
        "category": category if (not cats or "all" in cats or category in cats) else "",
        "categories": allowed_choices,
        # القالب يقرأ ``r.user_can_*|default:can_delete``، و``default`` يتراجع
        # عند القيمة الكاذبة — فعلمُ الصف ``False`` يعود إلى هذه القيمة. ولذلك
        # يجب أن تكون هي أيضاً ``False`` للوكيل، وإلا ظهرت أزرارٌ لا يملكها.
        "can_delete": is_manager,
        "is_manager": is_manager,
        "qs": _clean_query_params(request.GET),
    }
    return render(request, "reports/admin_reports.html", context)


@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def school_archive(request: HttpRequest) -> HttpResponse:
    """Manager-friendly archive workspace: live content plus immutable snapshots."""
    active_school = _get_active_school(request)
    if active_school is None:
        messages.error(request, "فضلاً اختر/حدّد مدرسة أولاً.")
        return redirect("reports:select_school")

    is_manager = is_school_manager(request.user, active_school=active_school)
    is_superuser = bool(getattr(request.user, "is_superuser", False))
    school_wide = bool(is_superuser or is_manager)
    can_manage_archive = bool(is_superuser or is_manager)
    archive_enabled = school_archive_enabled(active_school)
    archive_addon = SchoolArchiveAddon.objects.filter(school=active_school).first()
    saved_archives_qs = SchoolYearArchive.objects.filter(
        school=active_school,
        status__in=[
            SchoolYearArchive.Status.READY,
            SchoolYearArchive.Status.PARTIAL,
        ],
    )
    if not school_wide:
        saved_archives_qs = SchoolYearArchive.objects.none()
    has_saved_archives = saved_archives_qs.exists()

    years = archive_available_years(
        school=active_school,
        teacher=request.user,
        school_wide=school_wide,
    )
    selected_year = (request.GET.get("year") or "").strip()
    if selected_year not in years:
        selected_year = years[0] if years else ""

    search = _clean_query_value(request.GET.get("q"))
    teacher_filter = (request.GET.get("teacher") or "").strip()
    category_filter = (request.GET.get("category") or "").strip()
    teacher_id = int(teacher_filter) if school_wide and teacher_filter.isdigit() else None
    category_id = int(category_filter) if school_wide and category_filter.isdigit() else None
    payload = archive_payload(
        school=active_school,
        selected_year=selected_year,
        teacher=request.user,
        school_wide=school_wide,
        search=search,
        teacher_id=teacher_id,
        category_id=category_id,
    )
    snapshot_payload = (
        archive_payload(
            school=active_school,
            selected_year=selected_year,
            teacher=request.user,
            school_wide=True,
        )
        if school_wide
        else payload
    )
    administrative_stats = (
        school_administrative_archive_stats(active_school)
        if school_wide
        else {
            "tickets": 0,
            "circulars": 0,
            "notifications": 0,
            "system_notifications": 0,
            "user_notifications": 0,
            "assignments": 0,
            "meetings": 0,
            "lab_assets": 0,
            "lab_handovers": 0,
            "lab_experiments": 0,
            "total": 0,
        }
    )
    administrative_payload = (
        school_administrative_archive_payload(active_school, search=search)
        if school_wide
        else {
            "tickets_qs": Ticket.objects.none(),
            "circulars_qs": Notification.objects.none(),
            "notifications_qs": Notification.objects.none(),
            "matches": {"tickets": 0, "circulars": 0, "notifications": 0, "total": 0},
        }
    )
    leadership_portfolios = (
        SchoolLeadershipPortfolio.objects.filter(
            school=active_school,
            academic_year=selected_year,
        )
        .select_related("manager")
        .annotate(
            completed_count=Count(
                "sections",
                filter=Q(sections__is_completed=True),
                distinct=True,
            ),
            evidence_count=Count("sections__evidence_images", distinct=True),
        )
        if school_wide and selected_year and selected_year != UNCLASSIFIED_YEAR
        else SchoolLeadershipPortfolio.objects.none()
    )
    leadership_count = leadership_portfolios.count()
    from ..services_export import school_archive_source_counts

    snapshot_source_counts = (
        school_archive_source_counts(
            active_school,
            academic_year=selected_year,
        )
        if school_wide and selected_year
        else {}
    )
    snapshot_total_records = (
        sum(snapshot_source_counts.values())
        if school_wide and selected_year
        else (
            snapshot_payload["report_stats"]["total"]
            + snapshot_payload["achievement_stats"]["total"]
        )
    )

    reports_page = svc_paginate(payload["reports_qs"], per_page=15, page=request.GET.get("reports_page", 1))
    achievement_files_page = svc_paginate(
        payload["achievement_files_qs"],
        per_page=20,
        page=request.GET.get("files_page", 1),
    )
    tickets_page = svc_paginate(
        administrative_payload["tickets_qs"],
        per_page=10,
        page=request.GET.get("tickets_page", 1),
    )
    circulars_page = svc_paginate(
        administrative_payload["circulars_qs"],
        per_page=10,
        page=request.GET.get("circulars_page", 1),
    )
    notifications_page = svc_paginate(
        administrative_payload["notifications_qs"],
        per_page=10,
        page=request.GET.get("notifications_page", 1),
    )

    if archive_addon is not None:
        with soft_fail("archive.sync_storage_usage", school_id=getattr(active_school, "pk", None)):
            sync_school_archive_storage_usage(active_school)
            archive_addon.refresh_from_db(
                fields=["storage_used_bytes", "storage_limit_gb", "end_date", "updated_at"]
            )

    archive_versions_qs = (
        saved_archives_qs.filter(academic_year=selected_year)
        .select_related("created_by")
        .annotate(download_count=Count("downloads"))
        .order_by("-version", "-created_at")
        if selected_year
        else SchoolYearArchive.objects.none()
    )
    latest_archive = archive_versions_qs.first()
    archive_versions = svc_paginate(
        archive_versions_qs,
        per_page=8,
        page=request.GET.get("archive_page", 1),
    )
    pending_archive_payment = (
        Payment.objects.filter(
            school=active_school,
            purpose=Payment.Purpose.ARCHIVE_ADDON,
            status=Payment.Status.PENDING,
        )
        .order_by("-created_at")
        .first()
        if can_manage_archive
        else None
    )
    platform_settings = PlatformSettings.get_solo()
    archive_price = getattr(platform_settings, "archive_addon_annual_price", 0) or 0
    included_storage = getattr(platform_settings, "archive_included_storage_gb", 0) or 0
    teacher_options = (
        Teacher.objects.filter(
            school_memberships__school=active_school,
            school_memberships__role_type__in=SchoolMembership.STAFF_ROLES,
            school_memberships__is_active=True,
        )
        .distinct()
        .order_by("name", "id")
        if school_wide
        else Teacher.objects.none()
    )
    category_options = (
        ReportType.objects.filter(Q(school=active_school) | Q(school__isnull=True))
        .distinct()
        .order_by("name", "id")
        if school_wide and ReportType is not None
        else []
    )

    return render(
        request,
        "reports/school_archive.html",
        {
            "archive_enabled": archive_enabled,
            "archive_access_available": bool(archive_enabled or has_saved_archives),
            "has_saved_archives": has_saved_archives,
            "can_manage_archive": can_manage_archive,
            "can_create_archive": bool(can_manage_archive and archive_enabled),
            "current_school": active_school,
            "archive_addon": archive_addon,
            "pending_archive_payment": pending_archive_payment,
            "archive_price": archive_price,
            "archive_included_storage_gb": included_storage,
            "current_academic_year": (getattr(active_school, "current_academic_year", "") or "").strip(),
            "school_wide": school_wide,
            "years": years,
            "selected_year": selected_year,
            "selected_year_label": archive_year_label(selected_year) if selected_year else "",
            "reports": reports_page,
            "achievement_files": achievement_files_page,
            "report_stats": payload["report_stats"],
            "achievement_stats": payload["achievement_stats"],
            "snapshot_report_stats": snapshot_payload["report_stats"],
            "snapshot_achievement_stats": snapshot_payload["achievement_stats"],
            "leadership_portfolios": leadership_portfolios,
            "leadership_count": leadership_count,
            "snapshot_total_records": snapshot_total_records,
            "snapshot_source_counts": snapshot_source_counts,
            "administrative_stats": administrative_stats,
            "administrative_matches": administrative_payload["matches"],
            "tickets": tickets_page,
            "circulars": circulars_page,
            "archive_notifications": notifications_page,
            "storage_overview": school_storage_overview(active_school),
            # The snapshot bucket is reported separately so a full archive never
            # reads as "the platform is out of space".
            "archive_overview": school_archive_overview(active_school),
            "can_delete_archive": bool(
                getattr(request.user, "is_superuser", False)
                or is_school_manager(request.user, active_school=active_school)
            ),
            "unclassified_year": UNCLASSIFIED_YEAR,
            "archived_at": timezone.localtime(),
            "archive_versions": archive_versions,
            "latest_archive": latest_archive,
            "search": search,
            "teacher_filter": teacher_filter,
            "category_filter": category_filter,
            "teacher_options": teacher_options,
            "category_options": category_options,
            "qs": _clean_query_params(request.GET),
        },
    )


@login_required(login_url="reports:login")
@role_required({"manager"})
@ratelimit(key="user", rate="12/h", method="POST", block=True)
@require_http_methods(["POST"])
def school_archive_create(request: HttpRequest) -> HttpResponse:
    """Create and persist an immutable versioned ZIP snapshot for one year."""
    from django.core.files import File
    from ..services_export import (
        archive_zip_filename,
        build_school_export_zip_file,
        school_archive_source_counts,
    )

    active_school = _get_active_school(request)
    if active_school is None:
        messages.error(request, "فضلاً اختر/حدّد مدرسة أولاً.")
        return redirect("reports:select_school")
    if not school_archive_enabled(active_school):
        messages.error(request, "يلزم تفعيل أو تجديد إضافة الأرشفة قبل إنشاء نسخة جديدة.")
        return redirect("reports:school_archive")

    years = archive_available_years(school=active_school, teacher=request.user, school_wide=True)
    selected_year = (request.POST.get("year") or "").strip()
    if selected_year not in years:
        messages.error(request, "السنة المطلوبة غير متاحة أو لا تحتوي على بيانات.")
        return redirect("reports:school_archive")

    source_count = sum(
        school_archive_source_counts(
            active_school,
            academic_year=selected_year,
        ).values()
    )
    if source_count <= 0:
        messages.error(request, "لا يمكن إنشاء نسخة فارغة؛ لا توجد بيانات حية لهذه السنة.")
        return redirect(f"{reverse('reports:school_archive')}?year={selected_year}")

    if async_exports_enabled():
        job, created = enqueue_generated_export(
            school=active_school,
            requested_by=request.user,
            kind=GeneratedExportJob.Kind.ARCHIVE_SNAPSHOT,
            parameters={"academic_year": selected_year, "school_wide": True},
        )
        if created:
            messages.info(
                request,
                "بدأ إنشاء نسخة الأرشيف في الخلفية. يمكنك متابعة استخدام المنصة.",
            )
        return redirect(f"{reverse('reports:school_archive_export')}?job={job.pk}")

    zip_file = None
    archive = None
    try:
        zip_file, metadata = build_school_export_zip_file(
            active_school,
            academic_year=selected_year,
            teacher=request.user,
            school_wide=True,
            request=request,
            return_metadata=True,
        )

        # Snapshots draw on their own bucket. Charging them to the work bucket
        # meant one archive run could stop every teacher from uploading.
        capacity_error = archive_snapshot_capacity_error(
            active_school, metadata["archive_size_bytes"]
        )
        if capacity_error:
            messages.error(request, capacity_error)
            return redirect(f"{reverse('reports:school_archive')}?year={selected_year}")

        with transaction.atomic():
            School.objects.select_for_update().get(pk=active_school.pk)
            latest_version = (
                SchoolYearArchive.objects.filter(
                    school=active_school,
                    academic_year=selected_year,
                )
                .order_by("-version")
                .values_list("version", flat=True)
                .first()
                or 0
            )
            archive = SchoolYearArchive(
                school=active_school,
                academic_year=selected_year,
                version=int(latest_version) + 1,
                status=(
                    SchoolYearArchive.Status.PARTIAL
                    if metadata["is_partial"]
                    else SchoolYearArchive.Status.READY
                ),
                archive_sha256=metadata["archive_sha256"],
                file_count=metadata["file_count"],
                missing_file_count=metadata["missing_file_count"],
                failed_pdf_count=metadata["failed_pdf_count"],
                report_count=metadata["report_count"],
                achievement_count=metadata["achievement_count"],
                leadership_count=metadata["leadership_count"],
                ticket_count=metadata["ticket_count"],
                circular_count=metadata["circular_count"],
                notification_count=metadata["notification_count"],
                assignment_count=int(metadata.get("assignment_count") or 0),
                plan_count=int(metadata.get("plan_count") or 0),
                initiative_count=int(metadata.get("initiative_count") or 0),
                lab_asset_count=int(metadata.get("lab_asset_count") or 0),
                lab_handover_count=int(metadata.get("lab_handover_count") or 0),
                lab_experiment_count=int(metadata.get("lab_experiment_count") or 0),
                notes=metadata["notes"],
                created_by=request.user,
            )
            filename = archive_zip_filename(active_school, selected_year)
            archive.archive_file.save(filename, File(zip_file), save=False)
            archive.save()
        sync_school_archive_storage_usage(active_school)
    except Exception:
        if archive is not None and getattr(archive.archive_file, "name", ""):
            try:
                persisted = bool(
                    archive.pk
                    and SchoolYearArchive.objects.filter(pk=archive.pk).exists()
                )
                if not persisted:
                    archive.archive_file.delete(save=False)
            except Exception:
                _degraded("archive.orphan_file_cleanup", archive_id=getattr(archive, "pk", None))
        logger.exception(
            "school_archive_create failed school_id=%s year=%s",
            getattr(active_school, "id", None),
            selected_year,
        )
        messages.error(request, "تعذر إنشاء النسخة المحفوظة. لم تُسجل نسخة ناقصة؛ حاول مرة أخرى.")
        return redirect(f"{reverse('reports:school_archive')}?year={selected_year}")
    finally:
        with soft_fail("archive.close_zip_handle"):
            if zip_file is not None:
                zip_file.close()

    if archive.status == SchoolYearArchive.Status.PARTIAL:
        messages.warning(
            request,
            "تم حفظ النسخة مع ملاحظات اكتمال. راجع عدد الملفات المتعذرة قبل اعتمادها.",
        )
    else:
        messages.success(request, f"تم حفظ نسخة موثقة رقم {archive.version} لهذه السنة بنجاح.")
    return redirect(
        f"{reverse('reports:school_archive')}?year={selected_year}&snapshot={archive.pk}"
    )


@login_required(login_url="reports:login")
@ratelimit(key="user", rate="30/h", method="GET", block=True)
@require_http_methods(["GET"])
def school_archive_download(request: HttpRequest, pk: int) -> HttpResponse:
    """Download a persisted immutable snapshot and record an audit event."""
    active_school = _get_active_school(request)
    if active_school is None:
        messages.error(request, "فضلاً اختر/حدّد مدرسة أولاً.")
        return redirect("reports:select_school")
    allowed_school_wide = bool(
        getattr(request.user, "is_superuser", False)
        or is_school_manager(request.user, active_school=active_school)
    )
    if not allowed_school_wide:
        messages.error(request, "لا تملك صلاحية تنزيل نسخ أرشيف المدرسة.")
        return redirect("reports:school_archive")

    archive = get_object_or_404(SchoolYearArchive, pk=pk, school=active_school)
    if not archive.is_downloadable:
        messages.error(request, "هذه النسخة غير مكتملة ولا يوجد ملف صالح لتنزيله.")
        return redirect(f"{reverse('reports:school_archive')}?year={archive.academic_year}")
    SchoolYearArchiveDownload.objects.create(
        archive=archive,
        downloaded_by=request.user,
    )
    archive.archive_file.open("rb")
    safe_year = re.sub(r"[^0-9A-Za-z\-]+", "-", archive.academic_year).strip("-") or "year"
    return FileResponse(
        archive.archive_file,
        as_attachment=True,
        filename=f"archive-{active_school.code}-{safe_year}-v{archive.version}.zip",
        content_type="application/zip",
    )


@login_required(login_url="reports:login")
@require_http_methods(["POST"])
def school_archive_delete(request: HttpRequest, pk: int) -> HttpResponse:
    """Free archive space by removing a snapshot the manager has downloaded.

    This is the release valve for the snapshot bucket: without it a school that
    fills its archive space can never take another yearly snapshot. Deleting is
    destructive — the snapshot is the school's immutable record of that year —
    so it is restricted to the manager, requires the year to be typed back, and
    is written to the audit log.
    """
    active_school = _get_active_school(request)
    if active_school is None:
        messages.error(request, "فضلاً اختر/حدّد مدرسة أولاً.")
        return redirect("reports:select_school")

    # Deliberately narrower than download: report viewers and platform admins
    # may read a snapshot, only the school's own manager may destroy it.
    if not (
        getattr(request.user, "is_superuser", False)
        or is_school_manager(request.user, active_school=active_school)
    ):
        messages.error(request, "حذف نسخ الأرشيف من صلاحية مدير المدرسة فقط.")
        return redirect("reports:school_archive")

    archive = get_object_or_404(SchoolYearArchive, pk=pk, school=active_school)
    redirect_url = f"{reverse('reports:school_archive')}?year={archive.academic_year}"

    confirmation = (request.POST.get("confirm_year") or "").strip()
    if confirmation != (archive.academic_year or "").strip():
        messages.error(
            request,
            "لتأكيد الحذف اكتب السنة الدراسية للنسخة كما تظهر أمامك.",
        )
        return redirect(redirect_url)

    freed_bytes = int(getattr(archive, "storage_bytes", 0) or 0)
    year = archive.academic_year
    version = archive.version

    try:
        AuditLog.objects.create(
            school=active_school,
            teacher=request.user,
            action="delete",
            model_name="SchoolYearArchive",
            object_id=archive.pk,
            object_repr=f"نسخة أرشيف {year} الإصدار {version}"[:255],
            changes={"freed_bytes": freed_bytes},
            ip_address=request.META.get("REMOTE_ADDR"),
            user_agent=request.META.get("HTTP_USER_AGENT", "")[:500],
        )
    except Exception:
        logger.exception("Archive delete audit failed archive_id=%s", archive.pk)

    archive.delete()

    messages.success(
        request,
        f"تم حذف نسخة {year} من المنصة وتحرير {_human_size(freed_bytes)} من مساحة الأرشيف. "
        "النسخة التي نزّلتها على جهازك تبقى معك.",
    )
    return redirect(redirect_url)


@login_required(login_url="reports:login")
@ratelimit(key="user", rate="720/h", method="GET", block=True)
@require_http_methods(["GET"])
def school_archive_export(request: HttpRequest) -> HttpResponse:
    """Compatibility one-time export; managers should use persisted snapshots."""
    from django.http import FileResponse
    from ..services_export import build_school_export_zip_file, archive_zip_filename

    job_id = request.GET.get("job")
    if job_id:
        return generated_export_job_response(
            request,
            job_id=job_id,
            fallback_url=reverse("reports:school_archive"),
        )
    if export_creation_is_limited(
        request, group="school-archive-export-create", rate="20/h"
    ):
        response = HttpResponse("تجاوزت حد إنشاء ملفات الأرشيف. حاول لاحقًا.", status=429)
        response["Retry-After"] = "3600"
        return response

    active_school = _get_active_school(request)
    if active_school is None:
        messages.error(request, "فضلاً اختر/حدّد مدرسة أولاً.")
        return redirect("reports:select_school")

    if not school_archive_enabled(active_school):
        messages.error(request, "ميزة الأرشفة غير مفعّلة لهذه المدرسة.")
        return redirect("reports:school_archive")

    is_manager = is_school_manager(request.user, active_school=active_school)
    is_superuser = bool(getattr(request.user, "is_superuser", False))
    school_wide = bool(is_superuser or is_manager)

    years = archive_available_years(school=active_school, teacher=request.user, school_wide=school_wide)
    selected_year = (request.GET.get("year") or "").strip()
    if selected_year not in years:
        messages.error(request, "السنة المطلوبة غير متاحة في الأرشيف.")
        return redirect("reports:school_archive")

    if async_exports_enabled():
        job, created = enqueue_generated_export(
            school=active_school,
            requested_by=request.user,
            kind=GeneratedExportJob.Kind.YEAR_ZIP,
            parameters={
                "academic_year": selected_year,
                "school_wide": school_wide,
            },
        )
        if created:
            messages.info(request, "بدأ تجهيز أرشيف السنة في الخلفية.")
        return redirect(f"{reverse('reports:school_archive_export')}?job={job.pk}")

    try:
        zip_file = build_school_export_zip_file(
            active_school,
            academic_year=selected_year,
            teacher=request.user,
            school_wide=school_wide,
            request=request,
        )
    except Exception:
        logger.exception("school_archive_export failed school_id=%s", getattr(active_school, "id", None))
        messages.error(request, "تعذّر إنشاء أرشيف السنة. حاول لاحقًا.")
        return redirect(f"{reverse('reports:school_archive')}?year={selected_year}")

    logger.info(
        "Year archive exported school_id=%s user_id=%s year=%s school_wide=%s",
        getattr(active_school, "id", None), getattr(request.user, "id", None), selected_year, school_wide,
    )
    return FileResponse(
        zip_file,
        as_attachment=True,
        filename=archive_zip_filename(active_school, selected_year),
        content_type="application/zip",
    )


@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def officer_reports(request: HttpRequest) -> HttpResponse:
    active_school = _get_active_school(request)
    user = request.user
    if user.is_superuser:
        return redirect("reports:admin_reports")

    if not (Department is not None and DepartmentMembership is not None):
        messages.error(request, "صلاحيات المسؤول تتطلب تفعيل الأقسام وعضوياتها.")
        return redirect("reports:home")

    if active_school is None:
        messages.error(request, "فضلاً اختر مدرسة أولاً.")
        return redirect("reports:select_school")

    officer_memberships_qs = DepartmentMembership.objects.select_related("department").filter(
        teacher=user,
        role_type=DM_OFFICER,
        department__is_active=True,
        department__school=active_school,
    )
    membership = officer_memberships_qs.first()

    # ✅ يلزم أن تكون مسؤولاً داخل المدرسة النشطة نفسها (بدون fallback عبر مدرسة أخرى)
    if membership is None:
        messages.error(request, "لا تملك صلاحية مسؤول قسم.")
        return redirect("reports:home")

    dept = membership.department if membership else None

    # ✅ الأنواع المسموحة لمسؤول القسم = اتحاد reporttypes لأقسامه داخل المدرسة النشطة
    allowed_cats_qs = None
    if HAS_RTYPE and ReportType is not None:
        allowed_cats_qs = (
            ReportType.objects.filter(
                is_active=True,
                departments__memberships__teacher=user,
                departments__memberships__role_type=DM_OFFICER,
                departments__school=active_school,
            )
            .distinct()
            .order_by("order", "name")
        )

    if allowed_cats_qs is None or not allowed_cats_qs.exists():
        messages.info(request, "لم يتم ربط قسمك بأي أنواع تقارير بعد.")
        empty_page = Paginator(Report.objects.none(), 25).get_page(1)
        return render(
            request,
            "reports/officer_reports.html",
            {
                "reports": empty_page,
                "categories": [],
                "category": "",
                "teacher_name": "",
                "start_date": "",
                "end_date": "",
                "department": dept,
            },
        )

    start_date = _parse_date_safe(request.GET.get("start_date"))
    end_date = _parse_date_safe(request.GET.get("end_date"))
    teacher_name = _clean_query_value(request.GET.get("teacher_name"))
    category = _clean_query_value(request.GET.get("category"))

    qs = Report.objects.select_related("teacher", "category", "school").prefetch_related("evidences").filter(category__in=allowed_cats_qs)
    qs = _filter_by_school(qs, active_school)

    if start_date is not None:
        qs = qs.filter(report_date__gte=start_date)
    if end_date is not None:
        qs = qs.filter(report_date__lte=end_date)
    if teacher_name:
        qs = qs.filter(Q(teacher__name__icontains=teacher_name) | Q(teacher_name__icontains=teacher_name))
    if category:
        qs = qs.filter(category_id=category)

    qs = qs.order_by("-report_date", "-created_at")

    paginator = Paginator(qs, 25)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)
    
    # ✅ إضافة صلاحيات الحذف والمشاركة لكل تقرير (بدون N+1 على قاعدة البيانات)
    is_superuser = bool(getattr(user, "is_superuser", False))

    allowed_category_ids = set()
    try:
        allowed_category_ids = set(allowed_cats_qs.values_list("id", flat=True))
    except Exception:
        allowed_category_ids = set()

    manager_school_ids = set()
    if not is_superuser:
        try:
            manager_school_ids = set(
                SchoolMembership.objects.filter(
                    teacher=user,
                    role_type=SchoolMembership.RoleType.MANAGER,
                    is_active=True,
                ).values_list("school_id", flat=True)
            )
        except Exception:
            manager_school_ids = set()

    for report in page_obj:
        if is_superuser:
            allowed = True
        else:
            allowed = bool(
                getattr(report, "teacher_id", None) == getattr(user, "id", None)
                or (getattr(report, "school_id", None) in manager_school_ids)
                or (getattr(report, "category_id", None) in allowed_category_ids)
            )
        report.user_can_delete = allowed
        report.user_can_share = allowed

    categories_choices = [(str(c.pk), c.name) for c in allowed_cats_qs.order_by("order", "name")]

    return render(
        request,
        "reports/officer_reports.html",
        {
            "reports": page_obj,
            "categories": categories_choices,
            "category": category,
            "teacher_name": teacher_name,
            "start_date": start_date.isoformat() if start_date else "",
            "end_date": end_date.isoformat() if end_date else "",
            "department": dept,
        },
    )


# =========================
# تقارير القسم للأعضاء (عرض + طباعة فقط)
# =========================
@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def department_reports(request: HttpRequest) -> HttpResponse:
    """تقارير القسم لأعضاء القسم (TEACHER) - بدون حذف/مشاركة."""
    active_school = _get_active_school(request)
    user = request.user

    if getattr(user, "is_superuser", False):
        return redirect("reports:admin_reports")

    if not (Department is not None and DepartmentMembership is not None):
        messages.error(request, "عرض تقارير الأقسام يتطلب تفعيل الأقسام وعضوياتها.")
        return redirect("reports:home")

    if active_school is None:
        messages.error(request, "فضلاً اختر مدرسة أولاً.")
        return redirect("reports:select_school")

    # لو كان مسؤول قسم، نوجهه للوحة المسؤول الحالية (تدعم الحذف/المشاركة حسب الصلاحية)
    officer_memberships_qs = DepartmentMembership.objects.select_related("department").filter(
        teacher=user,
        role_type=DM_OFFICER,
        department__is_active=True,
        department__school=active_school,
    )
    if officer_memberships_qs.exists():
        return redirect("reports:officer_reports")

    member_memberships_qs = DepartmentMembership.objects.select_related("department").filter(
        teacher=user,
        role_type=DM_TEACHER,
        department__is_active=True,
        department__school=active_school,
    )
    membership = member_memberships_qs.first()

    if membership is None:
        messages.error(request, "لا تملك صلاحية عضو قسم ضمن المدرسة الحالية.")
        return redirect("reports:home")

    dept = membership.department

    allowed_cats_qs = None
    if HAS_RTYPE and ReportType is not None:
        allowed_cats_qs = (
            ReportType.objects.filter(
                is_active=True,
                departments__memberships__teacher=user,
                departments__memberships__role_type=DM_TEACHER,
                departments__school=active_school,
            )
            .distinct()
            .order_by("order", "name")
        )

    if allowed_cats_qs is None or not allowed_cats_qs.exists():
        messages.info(request, "لم يتم ربط قسمك بأي أنواع تقارير بعد.")
        empty_page = Paginator(Report.objects.none(), 25).get_page(1)
        return render(
            request,
            "reports/officer_reports.html",
            {
                "page_title": "📄 تقارير قسمي (عرض فقط)",
                "reports": empty_page,
                "categories": [],
                "category": "",
                "teacher_name": "",
                "start_date": "",
                "end_date": "",
                "department": dept,
                "can_delete": False,
                "configuration_missing": True,
            },
        )

    start_date = _parse_date_safe(request.GET.get("start_date"))
    end_date = _parse_date_safe(request.GET.get("end_date"))
    teacher_name = _clean_query_value(request.GET.get("teacher_name"))
    category = _clean_query_value(request.GET.get("category"))

    qs = Report.objects.select_related("teacher", "category", "school").prefetch_related("evidences").filter(category__in=allowed_cats_qs)
    qs = _filter_by_school(qs, active_school)

    if start_date is not None:
        qs = qs.filter(report_date__gte=start_date)
    if end_date is not None:
        qs = qs.filter(report_date__lte=end_date)
    if teacher_name:
        qs = qs.filter(Q(teacher__name__icontains=teacher_name) | Q(teacher_name__icontains=teacher_name))
    if category:
        qs = qs.filter(category_id=category)

    qs = qs.order_by("-report_date", "-created_at")

    paginator = Paginator(qs, 25)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    categories_choices = [(str(c.pk), c.name) for c in allowed_cats_qs.order_by("order", "name")]

    return render(
        request,
        "reports/officer_reports.html",
        {
            "page_title": "📄 تقارير قسمي (عرض فقط)",
            "reports": page_obj,
            "categories": categories_choices,
            "category": category,
            "teacher_name": teacher_name,
            "start_date": start_date.isoformat() if start_date else "",
            "end_date": end_date.isoformat() if end_date else "",
            "department": dept,
            "can_delete": False,
            "configuration_missing": False,
        },
    )

# =========================
# نقل تقرير إلى سلة المحذوفات (لوحة المدير)
# =========================
@login_required(login_url="reports:login")
@require_http_methods(["POST"])
def admin_delete_report(request: HttpRequest, pk: int) -> HttpResponse:
    """
    حذف تقرير مع التحقق من الصلاحيات.
    يسمح للأشخاص التالية بالحذف:
    - السوبر
    - مدير المدرسة
    - رئيس القسم (OFFICER) للتقارير المرتبطة بقسمه
    - صاحب التقرير نفسه
    
    ✅ عضو القسم (TEACHER) لا يستطيع الحذف (عرض فقط)
    """
    active_school = _get_active_school(request)
    user = request.user
    
    try:
        # جلب التقرير مع احترام المدرسة النشطة
        qs = Report.objects.all()
        qs = _filter_by_school(qs, active_school)
        report = get_object_or_404(qs, pk=pk)
        
        # التحقق من صلاحية الحذف
        if not can_delete_report(user, report, active_school=active_school):
            messages.error(request, "لا تملك صلاحية حذف هذا التقرير.")
            return _safe_redirect(request, "reports:admin_reports")
        
        report.move_to_trash(by=request.user)
        messages.success(request, "تم نقل التقرير إلى سلة المحذوفات ويمكن استعادته.")
    except Exception:
        messages.error(request, "تعذّر حذف التقرير.")
    
    return _safe_redirect(request, "reports:admin_reports")

# =========================
# حذف تقرير (لوحة المسؤول Officer)
# =========================
@login_required(login_url="reports:login")
@access_required(_is_staff_or_officer)
@require_http_methods(["POST"])
def officer_delete_report(request: HttpRequest, pk: int) -> HttpResponse:
    """
    حذف تقرير من قبل:
    - رئيس القسم (OFFICER) للتقارير المرتبطة بقسمه
    - مدير المدرسة
    - السوبر
    
    ✅ عضو القسم (TEACHER) لا يستطيع الحذف (عرض فقط)
    """
    active_school = _get_active_school(request)
    user = request.user
    
    try:
        r = _get_report_for_user_or_404(request, pk)
        
        # التحقق من الصلاحية
        if not can_delete_report(user, r, active_school=active_school):
            messages.error(request, "لا تملك صلاحية حذف هذا التقرير.")
            return _safe_redirect(request, "reports:admin_reports")
        
        r.move_to_trash(by=request.user)
        messages.success(request, "تم نقل التقرير إلى سلة المحذوفات ويمكن استعادته.")
    except Exception:
        messages.error(request, "تعذّر حذف التقرير أو لا تملك صلاحية لذلك.")
    
    return _safe_redirect(request, "reports:admin_reports")

# =========================
# الوصول إلى تقرير معيّن (مع احترام المدرسة النشطة)
# =========================
def _get_report_for_user_or_404(request: HttpRequest, pk: int):
    active_school = _get_active_school(request)
    return svc_get_report_for_user_or_404(user=request.user, pk=pk, active_school=active_school)


# =========================
# طباعة التقرير (نسخة مُحسّنة)
# =========================
@login_required(login_url="reports:login")
@require_http_methods(["GET", "POST"])
def report_print(request: HttpRequest, pk: int) -> HttpResponse:
    try:
        active_school = _get_active_school(request)
        user = request.user

        # ✅ المدير/الموظف/السوبر يجب أن يستطيع طباعة أي تقرير ضمن نطاق المدرسة النشطة
        if getattr(user, "is_superuser", False) or _is_staff(user):
            qs = Report.objects.select_related("teacher", "category").prefetch_related("evidences")
            if (not getattr(user, "is_superuser", False)) and active_school is None:
                messages.error(request, "فضلاً اختر مدرسة أولاً.")
                return redirect("reports:select_school")
            if active_school is not None:
                qs = qs.filter(school=active_school)
            r = get_object_or_404(qs, pk=pk)
        else:
            r = _get_report_for_user_or_404(request, pk)

        school_scope = getattr(r, "school", None) or active_school

        # ===== تعليقات خاصة (تظهر للمعلم فقط) =====
        show_comments = False
        is_report_owner = False
        can_add_private_comment = False
        private_comments = TeacherPrivateComment.objects.none()
        comment_form = None
        try:
            is_report_owner = getattr(r, "teacher_id", None) == getattr(user, "id", None)
            is_manager = _is_manager_in_school(user, school_scope)
            is_staff_user = _is_staff(user)
            can_add_private_comment = bool(is_manager or is_staff_user)
            show_comments = bool(is_report_owner or can_add_private_comment)

            # عرض سجل التعليقات للمعلم + أصحاب الصلاحية (ولا تظهر في الطباعة/المشاركة)
            if is_report_owner or can_add_private_comment:
                private_comments = (
                    TeacherPrivateComment.objects.select_related("created_by")
                    .filter(report=r, teacher=getattr(r, "teacher", None))
                    .order_by("-created_at", "-id")
                )

            try:
                if private_comments is not None:
                    for c in private_comments:
                        try:
                            c.created_by_role_label = _private_comment_role_label(getattr(c, "created_by", None), school_scope)
                        except Exception:
                            _degraded("reports.private_comment_role_label", comment_id=getattr(c, "pk", None))
                            c.created_by_role_label = ""
            except Exception:
                _degraded("reports.private_comments_block")

            # السماح بإضافة تعليق (يصل للمعلم فقط)
            if can_add_private_comment:
                if request.method == "POST":
                    action = (request.POST.get("action") or "").strip() or "private_comment_create"

                    # create (default)
                    if action == "private_comment_create":
                        comment_form = PrivateCommentForm(request.POST)
                        if comment_form.is_valid():
                            body = comment_form.cleaned_data["body"]
                            with transaction.atomic():
                                TeacherPrivateComment.objects.create(
                                    teacher=r.teacher,
                                    created_by=user,
                                    school=school_scope,
                                    report=r,
                                    body=body,
                                )
                                n = Notification.objects.create(
                                    title="تعليق خاص على تقرير",
                                    message=body,
                                    is_important=True,
                                    school=school_scope,
                                    created_by=user,
                                )
                                NotificationRecipient.objects.create(notification=n, teacher=r.teacher)
                            return redirect(request.get_full_path())

                    # update/delete (only comment author, or superuser)
                    if action in {"private_comment_update", "private_comment_delete"}:
                        comment_id = request.POST.get("comment_id")
                        try:
                            comment_id_int = int(comment_id) if comment_id else None
                        except (TypeError, ValueError):
                            comment_id_int = None

                        if not comment_id_int:
                            return redirect(request.get_full_path())

                        comment = TeacherPrivateComment.objects.filter(
                            pk=comment_id_int,
                            report=r,
                            teacher=getattr(r, "teacher", None),
                        ).first()
                        if comment is None:
                            return redirect(request.get_full_path())

                        is_owner_of_comment = getattr(comment, "created_by_id", None) == getattr(user, "id", None)

                        if action == "private_comment_update":
                            # تعديل: لصاحب التعليق فقط
                            if not is_owner_of_comment:
                                return HttpResponse(status=403)

                        if action == "private_comment_delete":
                            # حذف: لصاحب التعليق فقط، والسوبر يمكنه حذف أي تعليق
                            if not (is_owner_of_comment or getattr(user, "is_superuser", False)):
                                return HttpResponse(status=403)

                        if action == "private_comment_delete":
                            # حذفٌ يفشل صامتاً يُعيد المستخدم إلى صفحة يظهر
                            # فيها ما ظنّ أنه حذفه.
                            with soft_fail("reports.delete_private_comment", comment_id=comment.pk):
                                comment.delete()
                            return redirect("reports:report_print", pk=r.pk)

                        body = (request.POST.get("body") or "").strip()
                        if body:
                            with soft_fail("reports.edit_private_comment", comment_id=comment.pk):
                                TeacherPrivateComment.objects.filter(pk=comment.pk).update(body=body)
                        return redirect(request.get_full_path())
                else:
                    comment_form = PrivateCommentForm()
        except Exception:
            show_comments = False
            is_report_owner = False
            can_add_private_comment = False
            private_comments = TeacherPrivateComment.objects.none()
            comment_form = None

        # اختيار القسم يدويًا عبر ?dept=slug-or-id (اختياري)
        dept = None
        if Department is not None:
            pref = request.GET.get("dept")
            if pref:
                dept_qs = Department.objects.all()
                # فلترةٌ لم تُطبَّق تعرض أقسام مدارس أخرى في القائمة.
                with soft_fail("reports.department_school_filter", school_id=getattr(school_scope, "pk", None)):
                    if school_scope is not None:
                        dept_qs = dept_qs.filter(school=school_scope)

                dept = dept_qs.filter(Q(slug=pref) | Q(id=pref)).first() or dept

                # لا نسمح باختيار قسم لا يرتبط بتصنيف التقرير
                cat = getattr(r, "category", None)
                if dept is not None and cat is not None:
                    try:
                        if hasattr(dept, "reporttypes") and getattr(cat, "pk", None) is not None:
                            if not dept.reporttypes.filter(pk=cat.pk).exists():
                                dept = None
                    except Exception:
                        dept = None

        if dept is None:
            cat = getattr(r, "category", None)
            dept = _resolve_department_for_category(cat, school_scope)
            # حماية إضافية: تأكد أن القسم من نفس مدرسة التقرير/المدرسة النشطة
            if dept is not None and school_scope is not None:
                try:
                    dept_school = getattr(dept, "school", None)
                    if dept_school is not None and dept_school != school_scope:
                        dept = None
                except Exception:
                    dept = None

        head_decision = _build_head_decision(dept)

        # اسم مدير المدرسة
        school_principal = ""
        try:
            school_for_principal = getattr(r, "school", None) or _get_active_school(request)
            if school_for_principal is not None:
                principal_membership = (
                    SchoolMembership.objects.select_related("teacher")
                    .filter(
                        school=school_for_principal,
                        role_type=SchoolMembership.RoleType.MANAGER,
                        is_active=True,
                    )
                    .order_by("-id")
                    .first()
                )
                if principal_membership and principal_membership.teacher:
                    school_principal = getattr(principal_membership.teacher, "name", "") or ""
        except Exception:
            school_principal = ""

        if not school_principal:
            school_principal = getattr(settings, "SCHOOL_PRINCIPAL", "")

        # مسمّى المنفّذ حسب نوع المدرسة (بنين/بنات)
        executor_label = school_gender_labels(school_scope)["executor"]

        # إعدادات المدرسة (الاسم + المرحلة + الشعار)
        school_name = getattr(school_scope, "name", "") if school_scope else getattr(settings, "SCHOOL_NAME", "منصة توثيق")
        school_stage = ""
        school_logo_url = ""
        if school_scope:
            try:
                school_stage = getattr(school_scope, "get_stage_display", lambda: "")() or ""
            except Exception:
                school_stage = getattr(school_scope, "stage", "") or ""
            # تم حذف شعارات المدارس (logo_file/logo_url) نهائيًا من النظام
            school_logo_url = ""

        moe_logo_url = (getattr(settings, "MOE_LOGO_URL", "") or "").strip()
        # Optional fallback: allow providing a static path via env/settings
        if not moe_logo_url:
            try:
                moe_logo_static_path = (getattr(settings, "MOE_LOGO_STATIC", "") or "").strip()
                if moe_logo_static_path:
                    moe_logo_url = static(moe_logo_static_path)
            except Exception:
                moe_logo_url = ""

        # Final fallback: always use the bundled ministry logo for printing
        if not moe_logo_url:
            moe_logo_url = static("img/UntiTtled-1.png")

        # تحديد URL الرجوع الذكي حسب دور المستخدم
        back_url = "reports:my_reports"  # الافتراضي للمعلم
        is_manager = _is_manager_in_school(user, school_scope)
        is_staff_user = _is_staff(user)
        is_superuser_val = bool(getattr(user, "is_superuser", False))
        
        if is_superuser_val or is_manager or is_staff_user:
            back_url = "reports:admin_reports"
        
        from ..pdf_report import build_report_evidence_context

        evidence_context = build_report_evidence_context(r)

        return render(
            request,
            "reports/report_print.html",
            {
                "r": r,
                "head_decision": head_decision,
                "SCHOOL_PRINCIPAL": school_principal,
                "executor_label": executor_label,
                **school_gender_template_context(school_scope),
                "SCHOOL_NAME": school_name,
                "SCHOOL_STAGE": school_stage,
                "SCHOOL_LOGO_URL": school_logo_url,
                "MOE_LOGO_URL": moe_logo_url,
                "show_comments": show_comments,
                "is_report_owner": is_report_owner,
                "can_add_private_comment": can_add_private_comment,
                "current_user_id": getattr(user, "id", None),
                "is_superuser": is_superuser_val,
                "private_comments": private_comments,
                "comment_form": comment_form,
                "back_url": back_url,
                # المعاينة هي ما يفتحه المعلّم من لوحة الرئيسية، فتقف عندها
                # مسودتُه. شريطُها يعرض «طباعة» و«رجوع» فقط، فيطبع تقريراً لم
                # يصل أحداً — لذلك يُعرض الباب هنا أيضاً لصاحبه وحده.
                "report_approval_enabled": bool(
                    getattr(school_scope, "report_approval_enabled", False)
                ),
                **evidence_context,
            },
        )
    except Http404:
        raise
    except Exception as e:
        logger.exception(f"Error in report_print view for report {pk}: {e}")
        return render(request, "500.html", {"error": str(e)}, status=500)


def _valid_sharelink_or_404(token: str, *, kind: str) -> ShareLink:
    link = (
        ShareLink.objects.select_related("report", "achievement_file", "school")
        .filter(token=token, kind=kind)
        .first()
    )
    if not link or (not link.is_active) or link.is_expired:
        raise Http404
    return link


@login_required(login_url="reports:login")
@require_http_methods(["GET", "POST"])
def report_share_manage(request: HttpRequest, pk: int) -> HttpResponse:
    """
    تفعيل/إلغاء مشاركة تقرير عبر رابط عام صالح لمدة محددة.
    
    الصلاحيات:
    - صاحب التقرير
    - مدير المدرسة
    - رئيس القسم (OFFICER) للتقارير المرتبطة بقسمه
    - السوبر
    
    ✅ عضو القسم (TEACHER) لا يستطيع المشاركة (عرض فقط)
    """
    active_school = _get_active_school(request)
    user = request.user
    
    qs = Report.objects.select_related("school")
    if not getattr(user, "is_superuser", False) and active_school is not None:
        qs = qs.filter(school=active_school)
    report = get_object_or_404(qs, pk=pk)
    
    # التحقق من الصلاحية
    if not can_share_report(user, report, active_school=active_school):
        messages.error(request, "لا تملك صلاحية مشاركة هذا التقرير.")
        return redirect("reports:admin_reports" if _is_staff(user) else "reports:my_reports")

    expiry_days = get_share_link_default_days(school=report.school)

    now = timezone.now()
    active_link = (
        ShareLink.objects.filter(
            kind=ShareLink.Kind.REPORT,
            report=report,
            is_active=True,
            expires_at__gt=now,
        )
        .order_by("-id")
        .first()
    )

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip().lower()
        if action == "enable":
            selected_days = _requested_share_expiry_days(request, expiry_days)
            with transaction.atomic():
                ShareLink.objects.filter(kind=ShareLink.Kind.REPORT, report=report, is_active=True).update(is_active=False)

                created = None
                for _ in range(6):
                    token = ShareLink.generate_token()
                    try:
                        created = ShareLink.objects.create(
                            token=token,
                            kind=ShareLink.Kind.REPORT,
                            created_by=request.user,
                            school=getattr(report, "school", None),
                            report=report,
                            expires_at=timezone.now() + timedelta(days=selected_days),
                            is_active=True,
                        )
                        break
                    except IntegrityError:
                        created = None
                        continue

                if created is None:
                    messages.error(request, "تعذر إنشاء رابط مشاركة الآن. حاول مرة أخرى.")
                    return redirect("reports:report_share_manage", pk=report.pk)

            public_url = request.build_absolute_uri(reverse("reports:share_public", args=[created.token]))
            messages.success(
                request,
                f"تم تفعيل مشاركة التقرير لمدة {selected_days} أيام حتى {timezone.localtime(created.expires_at).strftime('%Y-%m-%d %H:%M')}.",
            )
            messages.info(request, f"رابط المشاركة: {public_url}")
            return redirect("reports:report_share_manage", pk=report.pk)

        if action == "disable" and active_link is not None:
            ShareLink.objects.filter(pk=active_link.pk).update(is_active=False)
            messages.success(request, "تم إيقاف رابط مشاركة التقرير.")
            return redirect("reports:report_share_manage", pk=report.pk)

        messages.error(request, "طلب غير صالح.")
        return redirect("reports:report_share_manage", pk=report.pk)

    public_url = ""
    expires_at_str = ""
    if active_link is not None:
        public_url = request.build_absolute_uri(reverse("reports:share_public", args=[active_link.token]))
        expires_at_str = timezone.localtime(active_link.expires_at).strftime("%Y-%m-%d %H:%M")

    return render(
        request,
        "reports/report_share_manage.html",
        {
            "report": report,
            "active_link": active_link,
            "public_url": public_url,
            "expires_at_str": expires_at_str,
            "expiry_days": expiry_days,
            "expiry_choices": _share_expiry_choices(expiry_days),
        },
    )


@login_required(login_url="reports:login")
@require_http_methods(["GET", "POST"])
def achievement_share_manage(request: HttpRequest, pk: int) -> HttpResponse:
    """تفعيل/إلغاء مشاركة ملف الإنجاز (PDF) عبر رابط عام صالح لمدة محددة (اختياري للمعلم)."""
    ach_file = get_object_or_404(TeacherAchievementFile.objects.select_related("school"), pk=pk, teacher=request.user)

    expiry_days = get_share_link_default_days(school=ach_file.school)

    now = timezone.now()
    active_link = (
        ShareLink.objects.filter(
            kind=ShareLink.Kind.ACHIEVEMENT,
            achievement_file=ach_file,
            is_active=True,
            expires_at__gt=now,
        )
        .order_by("-id")
        .first()
    )

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip().lower()
        if action == "enable":
            selected_days = _requested_share_expiry_days(request, expiry_days)
            with transaction.atomic():
                ShareLink.objects.filter(kind=ShareLink.Kind.ACHIEVEMENT, achievement_file=ach_file, is_active=True).update(is_active=False)

                created = None
                for _ in range(6):
                    token = ShareLink.generate_token()
                    try:
                        created = ShareLink.objects.create(
                            token=token,
                            kind=ShareLink.Kind.ACHIEVEMENT,
                            created_by=request.user,
                            school=getattr(ach_file, "school", None),
                            achievement_file=ach_file,
                            expires_at=timezone.now() + timedelta(days=selected_days),
                            is_active=True,
                        )
                        break
                    except IntegrityError:
                        created = None
                        continue

                if created is None:
                    messages.error(request, "تعذر إنشاء رابط مشاركة الآن. حاول مرة أخرى.")
                    return redirect("reports:achievement_share_manage", pk=ach_file.pk)

            public_url = request.build_absolute_uri(reverse("reports:share_public", args=[created.token]))
            messages.success(
                request,
                f"تم تفعيل مشاركة ملف الإنجاز لمدة {selected_days} أيام حتى {timezone.localtime(created.expires_at).strftime('%Y-%m-%d %H:%M')}.",
            )
            messages.info(request, f"رابط المشاركة: {public_url}")
            return redirect("reports:achievement_share_manage", pk=ach_file.pk)

        if action == "disable" and active_link is not None:
            ShareLink.objects.filter(pk=active_link.pk).update(is_active=False)
            messages.success(request, "تم إيقاف رابط مشاركة ملف الإنجاز.")
            return redirect("reports:achievement_share_manage", pk=ach_file.pk)

        messages.error(request, "طلب غير صالح.")
        return redirect("reports:achievement_share_manage", pk=ach_file.pk)

    public_url = ""
    expires_at_str = ""
    if active_link is not None:
        public_url = request.build_absolute_uri(reverse("reports:share_public", args=[active_link.token]))
        expires_at_str = timezone.localtime(active_link.expires_at).strftime("%Y-%m-%d %H:%M")

    return render(
        request,
        "reports/achievement_share_manage.html",
        {
            "file": ach_file,
            "active_link": active_link,
            "public_url": public_url,
            "expires_at_str": expires_at_str,
            "expiry_days": expiry_days,
            "expiry_choices": _share_expiry_choices(expiry_days),
        },
    )


@login_required(login_url="reports:login")
@require_http_methods(["GET", "POST"])
def share_links_dashboard(request: HttpRequest) -> HttpResponse:
    """Manage public links without widening access to their underlying records."""
    active_school = _get_active_school(request)
    user = request.user
    is_manager = bool(
        active_school
        and (
            getattr(user, "is_superuser", False)
            or is_school_manager(user, active_school=active_school)
        )
    )

    links_qs = ShareLink.objects.select_related(
        "school", "created_by", "report", "achievement_file"
    )
    if getattr(user, "is_superuser", False) and active_school is None:
        pass
    elif is_manager:
        links_qs = links_qs.filter(school=active_school)
    else:
        links_qs = links_qs.filter(
            Q(created_by=user)
            | Q(report__teacher=user)
            | Q(achievement_file__teacher=user)
        ).distinct()

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
        target_qs = links_qs.filter(is_active=True)
        if action == "disable_selected":
            selected_ids = [
                int(value)
                for value in request.POST.getlist("link_ids")[:200]
                if str(value).isdigit()
            ]
            target_qs = target_qs.filter(pk__in=selected_ids)
        elif action != "disable_all":
            messages.error(request, "تعذر تنفيذ الإجراء. اختر روابط صالحة ثم أعد المحاولة.")
            return redirect("reports:share_links_dashboard")

        changed = target_qs.update(is_active=False)
        if changed:
            messages.success(request, f"تم إيقاف {changed} رابط مشاركة.")
        else:
            messages.info(request, "لا توجد روابط نشطة مطابقة لإيقافها.")
        return redirect("reports:share_links_dashboard")

    now = timezone.now()
    status = (request.GET.get("status") or "all").strip().lower()
    kind = (request.GET.get("kind") or "").strip().lower()
    query = _clean_query_value(request.GET.get("q"))[:120]
    if status == "active":
        links_qs = links_qs.filter(is_active=True, expires_at__gt=now)
    elif status == "expired":
        links_qs = links_qs.filter(expires_at__lte=now)
    elif status == "disabled":
        links_qs = links_qs.filter(is_active=False)
    else:
        status = "all"
    if kind in {ShareLink.Kind.REPORT, ShareLink.Kind.ACHIEVEMENT}:
        links_qs = links_qs.filter(kind=kind)
    else:
        kind = ""
    if query:
        links_qs = links_qs.filter(
            Q(report__title__icontains=query)
            | Q(achievement_file__teacher_name__icontains=query)
            | Q(created_by__name__icontains=query)
        )

    links_qs = links_qs.order_by("-created_at")
    paginator = Paginator(links_qs, 40)
    page = paginator.get_page(request.GET.get("page"))
    for link in page:
        link.ui_status = (
            "disabled" if not link.is_active else "expired" if link.is_expired else "active"
        )
        link.ui_title = (
            getattr(link.report, "title", "")
            if link.kind == ShareLink.Kind.REPORT
            else getattr(link.achievement_file, "teacher_name", "") or "ملف إنجاز"
        )
        link.public_url = request.build_absolute_uri(
            reverse("reports:share_public", args=[link.token])
        )

    params = request.GET.copy()
    params.pop("page", None)
    return render(
        request,
        "reports/share_links_dashboard.html",
        {
            "active": "share_links_dashboard",
            "active_school": active_school,
            "links": page,
            "status": status,
            "kind": kind,
            "q": query,
            "qs": params.urlencode(),
            "is_manager_view": is_manager,
            "now": now,
        },
    )


# مسار عام بلا حساب: التوكن ٣٢ بايتاً عشوائية فلا يُخمَّن، لكن الحدَّ يلزم
# لسببين آخرين — رابطٌ تسرّب لا يُستنزف بلا سقف، وكل فتحة تُشغّل استعلامات
# وتصييراً كاملاً للصفحة. السقف واسع عمداً كي لا يُعاقَب فصلٌ يفتح الرابط معاً
# من شبكة مدرسة واحدة (عنوان NAT واحد).
@ratelimit(key="ip", rate="120/h", method="GET", block=True)
@require_http_methods(["GET"])
def share_public(request: HttpRequest, token: str) -> HttpResponse:
    """عرض عام حسب توكن: تقرير كامل + الصور، أو صفحة تحميل PDF لملف الإنجاز."""
    link = ShareLink.objects.select_related("report", "achievement_file", "school").filter(token=token).first()
    if not link or (not link.is_active) or link.is_expired:
        return render(request, "reports/share_invalid.html", status=404)

    ShareLink.objects.filter(pk=link.pk).update(
        last_accessed_at=timezone.now(),
        access_count=F("access_count") + 1,
    )

    if link.kind == ShareLink.Kind.REPORT:
        r = link.report
        if r is None or r.trashed_at is not None:
            return render(request, "reports/share_invalid.html", status=404)

        school_scope = getattr(r, "school", None) or getattr(link, "school", None)
        cat = getattr(r, "category", None)
        dept = _resolve_department_for_category(cat, school_scope)
        head_decision = _build_head_decision(dept)

        # اسم مدير المدرسة
        school_principal = ""
        try:
            if school_scope is not None:
                principal_membership = (
                    SchoolMembership.objects.select_related("teacher")
                    .filter(
                        school=school_scope,
                        role_type=SchoolMembership.RoleType.MANAGER,
                        is_active=True,
                    )
                    .order_by("-id")
                    .first()
                )
                if principal_membership and principal_membership.teacher:
                    school_principal = getattr(principal_membership.teacher, "name", "") or ""
        except Exception:
            school_principal = ""
        if not school_principal:
            school_principal = getattr(settings, "SCHOOL_PRINCIPAL", "")

        # إعدادات المدرسة
        school_name = getattr(school_scope, "name", "") if school_scope else getattr(settings, "SCHOOL_NAME", "منصة توثيق")
        school_stage = ""
        school_logo_url = ""
        if school_scope:
            try:
                school_stage = getattr(school_scope, "get_stage_display", lambda: "")() or ""
            except Exception:
                school_stage = getattr(school_scope, "stage", "") or ""
            # تم حذف شعارات المدارس (logo_file/logo_url) نهائيًا من النظام
            school_logo_url = ""

        moe_logo_url = (getattr(settings, "MOE_LOGO_URL", "") or "").strip()
        if not moe_logo_url:
            try:
                moe_logo_static_path = (getattr(settings, "MOE_LOGO_STATIC", "") or "").strip()
                if moe_logo_static_path:
                    moe_logo_url = static(moe_logo_static_path)
            except Exception:
                moe_logo_url = ""

        # Final fallback: always use the bundled ministry logo for printing
        if not moe_logo_url:
            moe_logo_url = static("img/UntiTtled-1.png")

        # مسمّى المنفّذ حسب نوع المدرسة (بنين/بنات)
        executor_label = school_gender_labels(school_scope)["executor"]

        from ..pdf_report import build_report_evidence_context

        evidence_context = build_report_evidence_context(r)
        for index, item in enumerate(evidence_context["EVIDENCE_ITEMS"], start=1):
            item["src"] = reverse("reports:share_report_image", args=[token, index])

        return render(
            request,
            "reports/report_print.html",
            {
                "r": r,
                "head_decision": head_decision,
                "SCHOOL_PRINCIPAL": school_principal,
                "executor_label": executor_label,
                **school_gender_template_context(school_scope),
                "SCHOOL_NAME": school_name,
                "SCHOOL_STAGE": school_stage,
                "SCHOOL_LOGO_URL": school_logo_url,
                "MOE_LOGO_URL": moe_logo_url,
                "show_comments": False,
                "private_comments": [],
                "comment_form": None,
                **evidence_context,
                "image1_url": reverse("reports:share_report_image", args=[token, 1]),
                "image2_url": reverse("reports:share_report_image", args=[token, 2]),
                "image3_url": reverse("reports:share_report_image", args=[token, 3]),
                "image4_url": reverse("reports:share_report_image", args=[token, 4]),
            },
        )

    if link.kind == ShareLink.Kind.ACHIEVEMENT:
        ach_file = link.achievement_file
        if ach_file is None:
            return render(request, "reports/share_invalid.html", status=404)

        # نفس تجربة مشاركة التقارير: فتح الرابط يعرض "الملف" مباشرة (صفحة طباعة/معاينة)، مع خيار تنزيل PDF.
        _ensure_achievement_sections(ach_file)
        try:
            from django.db.models import Prefetch

            ev_reports_qs = AchievementEvidenceReport.objects.select_related(
                "report",
                "report__category",
            ).order_by("id")
            sections = (
                AchievementSection.objects.filter(file=ach_file)
                .prefetch_related("evidence_images", Prefetch("evidence_reports", queryset=ev_reports_qs))
                .order_by("code", "id")
            )
            has_evidence_reports = AchievementEvidenceReport.objects.filter(section__file=ach_file).exists()
        except Exception:
            sections = (
                AchievementSection.objects.filter(file=ach_file)
                .prefetch_related("evidence_images", "evidence_reports")
                .order_by("code", "id")
            )
            has_evidence_reports = False

        school = ach_file.school
        primary = (getattr(school, "print_primary_color", None) or "").strip() or "#2563eb"

        # تم حذف شعارات المدارس (logo_file/logo_url) نهائيًا من النظام
        school_logo_url = ""

        try:
            from ..pdf_achievement import _static_png_as_data_uri

            ministry_logo_src = _static_png_as_data_uri("img/UntiTtled-1.png")
        except Exception:
            ministry_logo_src = None

        download_url = request.build_absolute_uri(reverse("reports:share_achievement_pdf", args=[token]))
        return render(
            request,
            "reports/pdf/achievement_file.html",
            {
                "file": ach_file,
                "school": school,
                "sections": sections,
                "has_evidence_reports": has_evidence_reports,
                "theme": {"brand": primary},
                "now": timezone.localtime(timezone.now()),
                "public_mode": True,
                "public_download_url": download_url,
                "school_logo_url": school_logo_url,
                "ministry_logo_src": ministry_logo_src,
            },
        )

    return render(request, "reports/share_invalid.html", status=404)


# الشواهد مرتبة، وسقف المعدل يسمح بمعاينة التقرير دون كشف رابط التخزين.
@ratelimit(key="ip", rate="480/h", method="GET", block=True)
@require_http_methods(["GET"])
def share_report_image(request: HttpRequest, token: str, slot: int) -> HttpResponse:
    link = _valid_sharelink_or_404(token, kind=ShareLink.Kind.REPORT)
    r = link.report
    if r is None or r.trashed_at is not None:
        raise Http404

    if slot < 1 or slot > 8:
        raise Http404

    evidences = list(r.evidences.order_by("order", "id")[:8])
    field = evidences[slot - 1].image if len(evidences) >= slot else None
    if not field and slot <= 4:
        field = getattr(r, f"image{slot}", None)
    if not field:
        raise Http404

    try:
        f = field.open("rb")
        resp = FileResponse(f)
        with soft_fail("files.image_content_disposition"):
            filename = os.path.basename(getattr(field, "name", "") or "") or f"image{slot}"
            resp["Content-Disposition"] = f'inline; filename="{filename}"'
        return resp
    except Exception:
        url = getattr(field, "url", None)
        if url:
            return redirect(url)
        raise


@ratelimit(key="ip", rate="30/h", method="GET", block=True)
@require_http_methods(["GET"])
def share_achievement_pdf(request: HttpRequest, token: str) -> HttpResponse:
    link = _valid_sharelink_or_404(token, kind=ShareLink.Kind.ACHIEVEMENT)
    ach_file = link.achievement_file
    if ach_file is None:
        raise Http404

    # إذا لم يكن الـ PDF مخزنًا بعد، ولّدْه عند الطلب واحتفظ به لتعمل المشاركة دائمًا.
    if not getattr(ach_file, "pdf_file", None):
        try:
            from django.core.files.base import ContentFile
            from ..pdf_achievement import achievement_pdf_filename, generate_achievement_pdf
            from ..pdf_offload import render_pdf_offloaded
            from ..tasks import render_achievement_pdf_task

            base_url = request.build_absolute_uri("/")
            filename = achievement_pdf_filename(ach_file)
            pdf_bytes = render_pdf_offloaded(
                task=render_achievement_pdf_task,
                task_args=[ach_file.pk, base_url],
                render_locally=lambda: generate_achievement_pdf(
                    request=request,
                    ach_file=ach_file,
                )[0],
                label=f"shared-achievement:{ach_file.pk}",
            )

            try:
                ach_file.pdf_file.save(filename, ContentFile(pdf_bytes), save=False)
                ach_file.pdf_generated_at = timezone.now()
                ach_file.save(update_fields=["pdf_file", "pdf_generated_at"])
            except Exception:
                _degraded("achievements.persist_generated_pdf", file_id=ach_file.pk)
                # حتى لو فشل التخزين (S3/permissions..)، نُرجع الملف للمستخدم.
                pass

            resp = HttpResponse(pdf_bytes, content_type="application/pdf")
            resp["Content-Disposition"] = f'inline; filename="{filename}"'
            return resp
        except OSError as ex:
            # WeasyPrint قد يفشل بسبب مكتبات النظام (خصوصًا على Windows).
            msg = str(ex) or ""
            if "libgobject" in msg or "gobject-2.0" in msg:
                return HttpResponse(
                    "تعذر توليد ملف PDF حاليًا بسبب نقص مكتبات الطباعة على الخادم.",
                    status=503,
                    content_type="text/plain; charset=utf-8",
                )
            if settings.DEBUG:
                raise
            return HttpResponse(
                "تعذر توليد ملف PDF حاليًا.",
                status=503,
                content_type="text/plain; charset=utf-8",
            )
        except Exception:
            if settings.DEBUG:
                raise
            return HttpResponse(
                "تعذر توليد ملف PDF حاليًا.",
                status=503,
                content_type="text/plain; charset=utf-8",
            )

    try:
        f = ach_file.pdf_file.open("rb")
        resp = FileResponse(f, content_type="application/pdf")
        with soft_fail("files.pdf_content_disposition"):
            filename = os.path.basename(getattr(ach_file.pdf_file, "name", "") or "") or "achievement.pdf"
            resp["Content-Disposition"] = f'inline; filename="{filename}"'
        return resp
    except Exception:
        url = getattr(ach_file.pdf_file, "url", None)
        if url:
            return redirect(url)
        raise


@login_required(login_url="reports:login")
@require_http_methods(["GET", "POST"])
def edit_my_report(request: HttpRequest, pk: int) -> HttpResponse:
    """
    تعديل تقرير مع التحقق من الصلاحيات.
    يسمح للأشخاص التالية بالتعديل:
    - السوبر
    - مدير المدرسة
    - رئيس القسم (OFFICER) للتقارير المرتبطة بقسمه
    - صاحب التقرير نفسه
    
    ✅ عضو القسم (TEACHER) لا يستطيع التعديل (عرض فقط)
    """
    user = request.user
    active_school = _get_active_school(request)

    # جلب التقرير باستخدام restrict_queryset (للتأكد من أن المستخدم يستطيع رؤيته)
    qs = restrict_queryset_for_user(Report.objects.all(), user, active_school)
    qs = _filter_by_school(qs, active_school)
    r = get_object_or_404(qs, pk=pk)
    
    # التحقق من صلاحية التعديل
    if not can_edit_report(user, r, active_school=active_school):
        is_owner = getattr(r, "teacher_id", None) == getattr(user, "id", None)
        if is_owner:
            # صاحبُ التقرير يملكه ولكن الدورة أخرجته من يده مؤقتاً: يُقال له
            # السبب ويُردّ إلى تقاريره — لا إلى شاشة إدارةٍ لا يراها أصلاً.
            messages.error(
                request, _report_locked_reason(r, active_school, action="تعديل التقرير")
            )
            return redirect("reports:my_reports")
        messages.error(request, "لا تملك صلاحية تعديل هذا التقرير.")
        return redirect("reports:admin_reports")

    # لا نجبر تغيير المدرسة النشطة بالجَلسة، لكن نستخدم مدرسة التقرير لتصفية الأنواع عند الحاجة.
    form_school = active_school or getattr(r, "school", None)
    response_status = 200

    if request.method == "POST":
        form = ReportForm(request.POST, request.FILES, instance=r, active_school=form_school)
        evidence_submitted = "evidence-TOTAL_FORMS" in request.POST
        evidence_formset = ReportEvidenceFormSet(
            request.POST if evidence_submitted else None,
            request.FILES if evidence_submitted else None,
            instance=r,
            prefix="evidence",
        )
        if form.is_valid() and (not evidence_submitted or evidence_formset.is_valid()):
            report_school = form_school or getattr(r, "school", None)
            replacing_files = [
                evidence_form.instance.image
                for evidence_form in evidence_formset.forms
                if evidence_submitted
                if evidence_form.instance.pk
                and (
                    evidence_form.cleaned_data.get("DELETE")
                    or "image" in evidence_form.changed_data
                )
            ]
            capacity_error = archive_storage_capacity_error(
                report_school,
                request.FILES.values(),
                replacing_files=replacing_files,
            )
            if capacity_error:
                messages.error(request, capacity_error)
                return render(
                    request,
                    "reports/edit_report.html",
                    {
                        "form": form,
                        "report": r,
                        "evidence_formset": evidence_formset,
                        **_report_ai_template_context(request.user),
                    },
                    status=(
                        422
                        if request.headers.get("X-Requested-With") == "XMLHttpRequest"
                        else 200
                    ),
                )

            with transaction.atomic():
                form.save()
                if evidence_submitted:
                    evidence_formset.save()
            sync_school_archive_storage_usage(report_school)
            messages.success(request, "تم تحديث التقرير بنجاح.")
            nxt = _safe_next_url(request.POST.get("next") or request.GET.get("next"))
            if nxt:
                return redirect(nxt)
            # إذا كان المستخدم ليس صاحب التقرير، يعود لـ admin_reports
            if getattr(r, "teacher_id", None) != getattr(user, "id", None):
                return redirect("reports:admin_reports")
            return redirect("reports:my_reports")
        messages.error(request, "تحقّق من الحقول.")
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            response_status = 422
    else:
        form = ReportForm(instance=r, active_school=form_school)
        evidence_formset = ReportEvidenceFormSet(instance=r, prefix="evidence")

    return render(
        request,
        "reports/edit_report.html",
        {
            "form": form,
            "report": r,
            "evidence_formset": evidence_formset,
            **_report_ai_template_context(request.user),
        },
        status=response_status,
    )

@login_required(login_url="reports:login")
@require_http_methods(["POST"])
def delete_my_report(request: HttpRequest, pk: int) -> HttpResponse:
    active_school = _get_active_school(request)
    qs = Report.objects.filter(teacher=request.user)
    qs = _filter_by_school(qs, active_school)
    r = get_object_or_404(qs, pk=pk)

    # المرسَل والمعتمَد خرجا من يد مُعِدّهما. والفحص هنا لا في الاستعلام
    # ليقرأ المستخدم سبب المنع بدل أن يقابله 404 يوحي بأن تقريره اختفى.
    if not can_delete_report(request.user, r, active_school=active_school):
        messages.error(request, _report_locked_reason(r, active_school, action="حذف التقرير"))
        return redirect("reports:my_reports")

    r.move_to_trash(by=request.user)
    messages.success(request, "تم نقل التقرير إلى سلة المحذوفات ويمكن استعادته.")
    nxt = _safe_next_url(request.POST.get("next") or request.GET.get("next"))
    return redirect(nxt or "reports:my_reports")


@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def report_trash(request: HttpRequest) -> HttpResponse:
    active_school = _get_active_school(request)
    if active_school is None:
        messages.error(request, "اختر مدرسة لعرض سلة المحذوفات.")
        return redirect("reports:select_school")

    is_manager = bool(
        getattr(request.user, "is_superuser", False)
        or is_school_manager(request.user, active_school=active_school)
    )
    reports_qs = Report.all_objects.filter(
        school=active_school,
        trashed_at__isnull=False,
    ).select_related("teacher", "category", "trashed_by")
    if not is_manager:
        reports_qs = reports_qs.filter(teacher=request.user)
    reports_qs = reports_qs.order_by("-trashed_at", "-id")
    page = Paginator(reports_qs, 30).get_page(request.GET.get("page"))
    return render(
        request,
        "reports/report_trash.html",
        {
            "active": "report_trash",
            "active_school": active_school,
            "reports": page,
            "is_manager_view": is_manager,
        },
    )


@login_required(login_url="reports:login")
@require_http_methods(["POST"])
def report_restore(request: HttpRequest, pk: int) -> HttpResponse:
    active_school = _get_active_school(request)
    if active_school is None:
        messages.error(request, "اختر مدرسة لاستعادة التقرير.")
        return redirect("reports:select_school")

    report = get_object_or_404(
        Report.all_objects.select_related("teacher"),
        pk=pk,
        school=active_school,
        trashed_at__isnull=False,
    )
    is_manager = bool(
        getattr(request.user, "is_superuser", False)
        or is_school_manager(request.user, active_school=active_school)
    )
    if not (is_manager or report.teacher_id == request.user.pk):
        messages.error(request, "لا تملك صلاحية استعادة هذا التقرير.")
        return redirect("reports:report_trash")

    report.restore_from_trash()
    messages.success(request, "تمت استعادة التقرير وإعادته إلى قائمة التقارير.")
    return redirect("reports:report_trash")
