# -*- coding: utf-8 -*-
"""شاشات الاجتماعات والقرارات.

شاشتان: قائمة الاجتماعات، وصفحة الاجتماع الواحد التي تجمع جدول الأعمال والحضور
والمحضر والقرارات في مكان واحد. تفريقها على أربع صفحات كان سيجعل كتابة محضر
واحد رحلةً بين شاشات، وهي عملٌ يُنجَز في جلسة واحدة.

**اعتماد المحضر لا يُكتب هنا.** ``MeetingMinutes`` يرث ``ApprovalMixin``،
فيمرّ بـ ``ACTION_DISPATCH`` نفسه الذي يخدم التقارير والتكليفات — وقاعدة «لا
يعتمد أحد عمله» تسري على كاتب المحضر بلا سطر جديد.
"""
from __future__ import annotations

import json
import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max, Q
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.templatetags.static import static
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from .. import capabilities as caps
from ..ai_features import (
    FEATURE_REPORT_IMPROVEMENT,
    FEATURE_VOICE_REPORT,
    platform_ai_toggle_enabled,
)
from ..forms_meetings import (
    AgendaItemForm,
    DecisionForm,
    MinutesForm,
    SchoolMeetingForm,
)
from ..models import Meeting, MeetingAgendaItem, MeetingAttendee
from ..permissions import capability_source, is_school_manager
from ..report_ai import (
    REPORT_AI_DAILY_LIMIT,
    ReportAIError,
    ReportAIUnavailable,
    improve_meeting_minutes_text,
    release_report_ai_daily_slot,
    report_ai_daily_remaining,
    reserve_report_ai_daily_slot,
    validate_meeting_minutes_text,
)
from ..services_approval import (
    ACTION_DISPATCH,
    ApprovalError,
    available_actions,
    transitions_for,
)
from ..services_meetings import (
    MeetingError,
    cancel_meeting,
    convert_decision_to_assignment,
    decision_followup_rows,
    ensure_minutes,
    mark_held,
    meetings_for_user,
    set_attendance,
)
from ..voice_report import (
    VoiceReportError,
    VoiceReportUnavailable,
    is_enabled as voice_report_is_enabled,
    polish_meeting_dictation,
    release_voice_report_daily_slot,
    reserve_voice_report_daily_slot,
    transcribe_meeting_audio,
    validate_audio_upload,
    voice_report_daily_limit,
    voice_report_daily_remaining,
)
from ._helpers import *  # noqa: F401,F403
from ._helpers import _get_active_school
from ..ai_usage import ai_usage_context

logger = logging.getLogger(__name__)

__all__ = [
    "meeting_list",
    "meeting_create",
    "meeting_detail",
    "meeting_print",
    "meeting_pdf",
    "meeting_action",
    "improve_meeting_minutes",
    "transcribe_meeting_minutes_voice",
    "minutes_approval_action",
]


def _meeting_ai_feature_enabled() -> bool:
    return bool(
        platform_ai_toggle_enabled(FEATURE_REPORT_IMPROVEMENT)
        and getattr(settings, "REPORT_AI_ENABLED", False)
        and str(getattr(settings, "OPENAI_API_KEY", "") or "").strip()
    )


def _meeting_voice_feature_enabled() -> bool:
    return bool(
        platform_ai_toggle_enabled(FEATURE_VOICE_REPORT)
        and voice_report_is_enabled()
    )


def _meeting_assistant_context(user) -> dict[str, int | bool]:
    return {
        "report_ai_enabled": _meeting_ai_feature_enabled(),
        "report_ai_daily_limit": REPORT_AI_DAILY_LIMIT,
        "report_ai_daily_remaining": report_ai_daily_remaining(user.pk),
        "voice_report_enabled": _meeting_voice_feature_enabled(),
        "voice_report_daily_limit": voice_report_daily_limit(),
        "voice_report_daily_remaining": voice_report_daily_remaining(user.pk),
        "voice_report_max_seconds": int(getattr(settings, "VOICE_REPORT_MAX_SECONDS", 180)),
        "voice_report_max_bytes": int(
            getattr(settings, "VOICE_REPORT_MAX_BYTES", 10 * 1024 * 1024)
        ),
        "voice_report_pwa_only": bool(getattr(settings, "VOICE_REPORT_PWA_ONLY", True)),
    }


def _request_is_from_installed_app(request: HttpRequest) -> bool:
    surface = (request.headers.get("X-Tawtheeq-Surface") or "").strip().lower()
    return surface == "standalone"


def _meeting_ai_json(payload: dict, *, status: int = 200) -> JsonResponse:
    response = JsonResponse(payload, status=status, json_dumps_params={"ensure_ascii": False})
    response["Cache-Control"] = "no-store"
    return response


def _school_or_redirect(request):
    school = _get_active_school(request)
    if school is None:
        messages.error(request, "فضلاً اختر مدرسة أولاً.")
        return None, redirect("reports:select_school")
    return school, None


def _may_organize(user, school) -> bool:
    if is_school_manager(user, active_school=school):
        return True
    return capability_source(user, caps.MANAGE_MEETINGS, school) is not None


def _meeting_for(request, pk: int, school) -> Meeting:
    """الاجتماع الذي يحق لهذا المستخدم رؤيته.

    منظّمه، أو مدعوّ إليه، أو مدير المدرسة. وما عدا ذلك يُعامَل كغير موجود — لا
    كممنوع، لئلا يُكشف انعقاد اجتماع لمن لا يحق له معرفة أنه انعقد.
    """
    meeting = get_object_or_404(
        Meeting.objects.select_related("organizer", "department", "school", "group"),
        pk=pk,
    )
    if meeting.organizer_id == request.user.pk:
        return meeting
    if meeting.attendees.filter(person=request.user).exists():
        return meeting
    if meeting.school_id == getattr(school, "pk", None) and is_school_manager(
        request.user, active_school=school
    ):
        return meeting
    raise Http404


@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def meeting_list(request):
    """اجتماعاتي: ما نظّمته وما دُعيت إليه."""
    school, redirect_response = _school_or_redirect(request)
    if redirect_response is not None:
        return redirect_response

    meetings = list(meetings_for_user(request.user, school=school)[:100])
    upcoming = [m for m in meetings if m.status == Meeting.Status.SCHEDULED]
    held = [m for m in meetings if m.status == Meeting.Status.HELD]

    # محاضر تنتظر كتابةً أو اعتماداً — أول ما يهمّ من يفتح الشاشة.
    pending_minutes = [
        m
        for m in held
        if getattr(m, "minutes", None) is None
        or not getattr(m.minutes, "is_final", False)
    ]

    return render(
        request,
        "reports/meeting_list.html",
        {
            "active": "meeting_list",
            "active_school": school,
            "upcoming": upcoming,
            "held": held,
            "pending_minutes_count": len(pending_minutes),
            "can_organize": _may_organize(request.user, school),
        },
    )


@login_required(login_url="reports:login")
@require_http_methods(["GET", "POST"])
def meeting_create(request):
    """تنظيم اجتماع جديد داخل المدرسة."""
    school, redirect_response = _school_or_redirect(request)
    if redirect_response is not None:
        return redirect_response

    if not _may_organize(request.user, school):
        messages.error(request, "لا تملك صلاحية تنظيم الاجتماعات.")
        return redirect("reports:home")

    form = SchoolMeetingForm(request.POST or None, school=school, organizer=request.user)

    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            meeting = form.save(commit=False)
            meeting.organizer = request.user
            meeting.save()
            for person in form.cleaned_data["attendees"]:
                MeetingAttendee.objects.create(meeting=meeting, person=person)
        messages.success(request, "أُنشئ الاجتماع ووُجّهت الدعوات.")
        target = reverse("reports:meeting_detail", kwargs={"pk": meeting.pk})
        draft_key = f"meeting-create-u{request.user.pk}-s{getattr(school, 'pk', 0) or 0}"
        return redirect(f"{target}?draft_saved={draft_key}")

    if request.method == "POST":
        messages.error(request, "تعذّر إنشاء الاجتماع — تحقّق من الحقول.")

    return render(
        request,
        "reports/meeting_create.html",
        {"active": "meeting_list", "active_school": school, "form": form},
    )


@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def meeting_detail(request, pk: int):
    """صفحة الاجتماع: جدول الأعمال والحضور والمحضر والقرارات."""
    school, redirect_response = _school_or_redirect(request)
    if redirect_response is not None:
        return redirect_response

    meeting = _meeting_for(request, pk, school)
    is_organizer = meeting.organizer_id == request.user.pk

    minutes = getattr(meeting, "minutes", None)
    if minutes is None and is_organizer and meeting.is_held:
        minutes = ensure_minutes(meeting, recorder=request.user)

    minutes_actions = (
        available_actions(minutes, request.user, school=meeting.school)
        if minutes is not None
        else []
    )
    can_edit_minutes = bool(
        is_organizer and minutes is not None and minutes.is_editable_by_owner
    )

    return render(
        request,
        "reports/meeting_detail.html",
        {
            "active": "meeting_list",
            "active_school": school,
            "meeting": meeting,
            "is_organizer": is_organizer,
            "can_edit_minutes": can_edit_minutes,
            "agenda": list(meeting.agenda_items.all()),
            "attendees": list(meeting.attendees.select_related("person")),
            "attendance": meeting.attendance_summary,
            "attendance_choices": MeetingAttendee.Status.choices,
            "minutes": minutes,
            "minutes_form": MinutesForm(instance=minutes) if minutes is not None else None,
            "minutes_actions": minutes_actions,
            "minutes_timeline": list(transitions_for(minutes)) if minutes is not None else [],
            "agenda_form": AgendaItemForm(),
            "decision_form": DecisionForm(meeting=meeting),
            "decisions": decision_followup_rows(meeting),
            **(_meeting_assistant_context(request.user) if can_edit_minutes else {}),
        },
    )


@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def meeting_print(request, pk: int):
    """محضر الاجتماع على ورق رسمي A4.

    الوثيقة التي تُوقَّع وتُحفَظ. لا تُقيَّد باعتماد المحضر: اجتماعٌ انعقد
    ومحضره قيد المراجعة يحتاج نسخةً تُعرَض على الحاضرين ليوقّعوا، وحالة
    الاعتماد تُطبع في ترويسة النسخة حتى لا تُقرأ مسوّدةٌ على أنها معتمدة.
    """
    school, redirect_response = _school_or_redirect(request)
    if redirect_response is not None:
        return redirect_response

    meeting = _meeting_for(request, pk, school)

    from ..pdf_meeting import build_meeting_print_context

    return render(
        request,
        "reports/meeting_print.html",
        build_meeting_print_context(meeting, active_school=school),
    )


@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def meeting_pdf(request, pk: int):
    """تنزيل محضر PDF مولّد من القالب الرسمي نفسه."""
    school, redirect_response = _school_or_redirect(request)
    if redirect_response is not None:
        return redirect_response
    meeting = _meeting_for(request, pk, school)
    try:
        from ..pdf_meeting import generate_meeting_pdf

        pdf_bytes, filename = generate_meeting_pdf(request=request, meeting=meeting)
    except Exception:
        logger.exception("Failed to render meeting PDF meeting_id=%s", meeting.pk)
        return HttpResponse(
            "تعذر توليد ملف PDF حاليًا.",
            status=503,
            content_type="text/plain; charset=utf-8",
        )
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def _editable_minutes_or_error(request, meeting):
    minutes = getattr(meeting, "minutes", None)
    if meeting.organizer_id != request.user.pk:
        return None, _meeting_ai_json(
            {"ok": False, "message": "تحرير المحضر متاح لمنظّم الاجتماع فقط."},
            status=403,
        )
    if minutes is None or not meeting.is_held:
        return None, _meeting_ai_json(
            {"ok": False, "message": "سجّل انعقاد الاجتماع أولًا لفتح المحضر."},
            status=409,
        )
    if not minutes.is_editable_by_owner:
        return None, _meeting_ai_json(
            {"ok": False, "message": "المحضر ليس في حالة تسمح بتعديله."},
            status=409,
        )
    return minutes, None


@login_required(login_url="reports:login")
@never_cache
@require_http_methods(["POST"])
def improve_meeting_minutes(request: HttpRequest, pk: int) -> JsonResponse:
    """يعيد معاينة محسنة للمحضر دون تعديل المسودة المحفوظة."""
    if not _meeting_ai_feature_enabled():
        return _meeting_ai_json(
            {"ok": False, "message": "ميزة تحسين المحاضر غير متاحة حاليًا."},
            status=404,
        )

    school, redirect_response = _school_or_redirect(request)
    if redirect_response is not None:
        return redirect_response
    meeting = _meeting_for(request, pk, school)
    _minutes, error = _editable_minutes_or_error(request, meeting)
    if error is not None:
        return error

    if request.content_type != "application/json":
        return _meeting_ai_json(
            {"ok": False, "message": "صيغة الطلب غير صحيحة."}, status=415
        )
    if len(request.body) > 30000:
        return _meeting_ai_json(
            {"ok": False, "message": "نص المحضر أطول من الحد المسموح."},
            status=413,
        )
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = None
    if not isinstance(payload, dict):
        return _meeting_ai_json(
            {"ok": False, "message": "تعذر قراءة نص المحضر."}, status=400
        )

    try:
        original_text = validate_meeting_minutes_text(payload.get("text"))
    except ReportAIError as exc:
        return _meeting_ai_json({"ok": False, "message": str(exc)}, status=400)

    try:
        remaining = reserve_report_ai_daily_slot(request.user.pk)
    except ReportAIUnavailable as exc:
        return _meeting_ai_json({"ok": False, "message": str(exc)}, status=503)
    if remaining is None:
        return _meeting_ai_json(
            {
                "ok": False,
                "message": "استخدمت تحسيناتك الثلاثة المتاحة اليوم. يعود الرصيد تلقائيًا غدًا.",
                "remaining": 0,
                "daily_limit": REPORT_AI_DAILY_LIMIT,
            },
            status=429,
        )

    try:
        with ai_usage_context(school=_get_active_school(request), teacher=request.user):
            improved_text = improve_meeting_minutes_text(original_text)
    except ReportAIUnavailable as exc:
        release_report_ai_daily_slot(request.user.pk)
        return _meeting_ai_json(
            {
                "ok": False,
                "message": str(exc),
                "remaining": report_ai_daily_remaining(request.user.pk),
                "daily_limit": REPORT_AI_DAILY_LIMIT,
            },
            status=503,
        )
    except ReportAIError as exc:
        release_report_ai_daily_slot(request.user.pk)
        return _meeting_ai_json(
            {
                "ok": False,
                "message": str(exc),
                "remaining": report_ai_daily_remaining(request.user.pk),
                "daily_limit": REPORT_AI_DAILY_LIMIT,
            },
            status=400,
        )

    return _meeting_ai_json(
        {
            "ok": True,
            "improved_text": improved_text,
            "remaining": remaining,
            "daily_limit": REPORT_AI_DAILY_LIMIT,
        }
    )


@login_required(login_url="reports:login")
@never_cache
@ratelimit(key="user", rate="6/m", method="POST", block=True)
@require_http_methods(["POST"])
def transcribe_meeting_minutes_voice(request: HttpRequest, pk: int) -> JsonResponse:
    """يفرّغ التسجيل وينظّمه كمحضر مقترح دون حفظ الملف أو النص."""
    if not _meeting_voice_feature_enabled():
        return _meeting_ai_json(
            {"ok": False, "message": "خدمة التسجيل الصوتي للمحاضر غير متاحة حاليًا."},
            status=404,
        )

    school, redirect_response = _school_or_redirect(request)
    if redirect_response is not None:
        return redirect_response
    meeting = _meeting_for(request, pk, school)
    _minutes, error = _editable_minutes_or_error(request, meeting)
    if error is not None:
        return error

    if getattr(settings, "VOICE_REPORT_PWA_ONLY", True) and not _request_is_from_installed_app(request):
        return _meeting_ai_json(
            {
                "ok": False,
                "message": "التسجيل الصوتي متاح داخل تطبيق توثيق المثبّت على جهازك.",
                "reason": "pwa_required",
            },
            status=403,
        )

    limit = voice_report_daily_limit()
    try:
        audio_bytes, extension = validate_audio_upload(request.FILES.get("audio"))
    except VoiceReportError as exc:
        return _meeting_ai_json(
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
        return _meeting_ai_json({"ok": False, "message": str(exc)}, status=503)
    if remaining is None:
        return _meeting_ai_json(
            {
                "ok": False,
                "message": f"استخدمت تسجيلاتك الـ{limit} المتاحة اليوم. يعود الرصيد تلقائيًا غدًا.",
                "remaining": 0,
                "daily_limit": limit,
            },
            status=429,
        )

    try:
        with ai_usage_context(school=_get_active_school(request), teacher=request.user):
            raw_text = transcribe_meeting_audio(audio_bytes, extension)
            text = polish_meeting_dictation(raw_text)
    except VoiceReportUnavailable as exc:
        release_voice_report_daily_slot(request.user.pk)
        return _meeting_ai_json(
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
        return _meeting_ai_json(
            {
                "ok": False,
                "message": str(exc),
                "remaining": voice_report_daily_remaining(request.user.pk),
                "daily_limit": limit,
            },
            status=400,
        )

    return _meeting_ai_json(
        {
            "ok": True,
            "text": text,
            "raw_text": raw_text,
            "remaining": remaining,
            "daily_limit": limit,
        }
    )


@login_required(login_url="reports:login")
@require_http_methods(["POST"])
def meeting_action(request, pk: int):
    """إجراءات المنظّم على اجتماعه."""
    school, redirect_response = _school_or_redirect(request)
    if redirect_response is not None:
        return redirect_response

    meeting = _meeting_for(request, pk, school)
    action = (request.POST.get("meeting_action") or "").strip()
    saved_draft_key = ""

    try:
        if action == "add_agenda":
            form = AgendaItemForm(request.POST)
            if meeting.organizer_id != request.user.pk:
                raise PermissionDenied("جدول الأعمال يعدّه منظّم الاجتماع.")
            if not form.is_valid():
                messages.error(request, "اكتب عنوان البند.")
            else:
                item = form.save(commit=False)
                item.meeting = meeting
                item.order = (
                    meeting.agenda_items.aggregate(top=Max("order"))["top"] or 0
                ) + 1
                item.save()
                messages.success(request, "أُضيف بند إلى جدول الأعمال.")

        elif action == "remove_agenda":
            if meeting.organizer_id != request.user.pk:
                raise PermissionDenied("جدول الأعمال يعدّه منظّم الاجتماع.")
            item = get_object_or_404(
                MeetingAgendaItem, pk=request.POST.get("item_id"), meeting=meeting
            )
            item.delete()
            messages.success(request, "حُذف البند.")

        elif action == "mark_held":
            mark_held(meeting, request.user)
            ensure_minutes(meeting, recorder=request.user)
            messages.success(request, "سُجِّل انعقاد الاجتماع، وفُتح المحضر للكتابة.")

        elif action == "cancel":
            cancel_meeting(meeting, request.user, reason=request.POST.get("reason", ""))
            messages.success(request, "أُلغي الاجتماع. تبقى دعوته في السجل.")

        elif action == "attendance":
            rows = {
                key.split("attendance_", 1)[1]: value
                for key, value in request.POST.items()
                if key.startswith("attendance_")
            }
            set_attendance(meeting, request.user, rows=rows)
            messages.success(request, "سُجِّل الحضور.")

        elif action == "save_minutes":
            if meeting.organizer_id != request.user.pk:
                raise PermissionDenied("تحرير المحضر متاح لمنظّم الاجتماع فقط.")
            minutes = ensure_minutes(meeting, recorder=request.user)
            if minutes.recorder_id not in (None, request.user.pk):
                raise PermissionDenied("المحضر يكتبه من فُتح باسمه.")
            if not minutes.is_editable_by_owner:
                raise MeetingError("المحضر ليس في حالة تسمح بتعديله.")
            form = MinutesForm(request.POST, instance=minutes)
            if not form.is_valid():
                messages.error(request, "تعذّر حفظ المحضر.")
            else:
                obj = form.save(commit=False)
                if obj.recorder_id is None:
                    obj.recorder = request.user
                obj.save()
                messages.success(request, "حُفظ المحضر.")
                saved_draft_key = f"meeting-minutes-{meeting.pk}-u{request.user.pk}"

        elif action == "add_decision":
            if meeting.organizer_id != request.user.pk:
                raise PermissionDenied("تسجيل القرارات لمنظّم الاجتماع.")
            form = DecisionForm(request.POST, meeting=meeting)
            if not form.is_valid():
                messages.error(request, "تعذّر تسجيل القرار — تحقّق من الحقول.")
            else:
                decision = form.save(commit=False)
                decision.meeting = meeting
                decision.order = (
                    meeting.decisions.aggregate(top=Max("order"))["top"] or 0
                ) + 1
                decision.save()
                messages.success(request, "سُجِّل القرار.")

        elif action == "track_decision":
            decision = get_object_or_404(
                meeting.decisions.all(), pk=request.POST.get("decision_id")
            )
            convert_decision_to_assignment(decision, request.user)
            messages.success(
                request, "حُوِّل القرار إلى تكليف — يُتابَع الآن بموعده وشواهده."
            )

        else:
            messages.error(request, "إجراء غير معروف.")

    except PermissionDenied as exc:
        messages.error(request, str(exc) or "لا تملك هذا الإجراء.")
    except (MeetingError, ApprovalError, ValidationError) as exc:
        detail = getattr(exc, "messages", None) or [str(exc)]
        messages.error(request, detail[0])

    target = reverse("reports:meeting_detail", kwargs={"pk": pk})
    if saved_draft_key:
        target = f"{target}?draft_saved={saved_draft_key}"
    return redirect(target)


@login_required(login_url="reports:login")
@require_http_methods(["POST"])
def minutes_approval_action(request, pk: int):
    """دورة اعتماد المحضر — بالمكوّن المشترك نفسه."""
    school, redirect_response = _school_or_redirect(request)
    if redirect_response is not None:
        return redirect_response

    meeting = _meeting_for(request, pk, school)
    minutes = getattr(meeting, "minutes", None)
    if minutes is None:
        messages.error(request, "لم يُفتح محضر لهذا الاجتماع بعد.")
        return redirect("reports:meeting_detail", pk=pk)

    action = (request.POST.get("approval_action") or "").strip()
    note = (request.POST.get("note") or "").strip()

    handler = ACTION_DISPATCH.get(action)
    if handler is None or action not in available_actions(
        minutes, request.user, school=meeting.school
    ):
        messages.error(request, "هذا الإجراء غير متاح على المحضر الآن.")
        return redirect("reports:meeting_detail", pk=pk)

    try:
        handler(minutes, request.user, school=meeting.school, note=note)
    except PermissionDenied as exc:
        messages.error(request, str(exc) or "لا تملك هذا الإجراء.")
    except (ApprovalError, ValidationError) as exc:
        detail = getattr(exc, "messages", None) or [str(exc)]
        messages.error(request, detail[0])
    else:
        messages.success(
            request,
            {
                "submit": "أُرسل المحضر للاعتماد.",
                "issue": "أُصدر المحضر بصفته وثيقة نهائية.",
                "withdraw": "سُحب المحضر للتعديل.",
                "start_review": "بدأت مراجعة المحضر.",
                "request_info": "طُلب استكمال من كاتب المحضر.",
                "return": "أُعيد المحضر لكاتبه مع ملاحظتك.",
                "approve": "اعتُمد المحضر.",
            }.get(action, "نُفِّذ الإجراء."),
        )

    return redirect("reports:meeting_detail", pk=pk)
