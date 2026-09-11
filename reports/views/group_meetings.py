# -*- coding: utf-8 -*-
"""مجلس مجموعة المدارس.

يستكمل ما بدأته تكليفات المجموعة: المدير التنفيذي يدعو مديري مدارسه، ويعتمد
جدول الأعمال، ويوثّق محضر المجلس، ويصدر قراراته — ثم يحوّلها إلى تكليفات
تُتابَع بمواعيدها. وهي الحلقة التي تربط «القرار» بـ«التنفيذ» على مستوى
المجموعة.

**لا يمرّ من هنا سياق مدرسة** — على نهج بقية شاشات المجموعة.

ولا يُكتب هنا منطق اعتماد ولا تحويل قرار: ``MeetingMinutes`` يمرّ بـ
``ACTION_DISPATCH``، و``convert_decision_to_assignment`` تخدم المستويين معاً —
وهي تعرف بنفسها أن قرار المجلس يُنسب لمدرسة المسؤول عنه.
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from ..forms_meetings import AgendaItemForm, DecisionForm, GroupMeetingForm, MinutesForm
from ..models import Meeting, MeetingAgendaItem, MeetingAttendee
from ..permissions import executive_director_schools_qs
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
    set_attendance,
)
from ..view_access import executive_director_groups_or_404 as _director_groups

__all__ = [
    "council_list",
    "council_create",
    "council_detail",
    "council_action",
    "council_minutes_action",
]


def _selected_group(request, groups):
    requested = (request.GET.get("group") or request.POST.get("group") or "").strip()
    if requested:
        return next((item for item in groups if str(item.pk) == requested), groups[0])
    return groups[0]


def _council_or_404(request, pk: int, groups) -> Meeting:
    group_ids = [item.pk for item in groups]
    return get_object_or_404(
        Meeting.objects.select_related("organizer", "group"),
        pk=pk,
        scope=Meeting.Scope.GROUP,
        group_id__in=group_ids,
    )


@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def council_list(request):
    """جلسات المجلس: القادمة والمنعقدة."""
    groups = _director_groups(request)
    group = _selected_group(request, groups)

    meetings = list(
        Meeting.objects.filter(group=group, scope=Meeting.Scope.GROUP)
        .select_related("organizer")
        .prefetch_related("attendees", "decisions")
        .order_by("-scheduled_at", "-id")[:100]
    )
    upcoming = [m for m in meetings if m.status == Meeting.Status.SCHEDULED]
    held = [m for m in meetings if m.status == Meeting.Status.HELD]
    pending_minutes = [
        m
        for m in held
        if getattr(m, "minutes", None) is None or not getattr(m.minutes, "is_final", False)
    ]

    return render(
        request,
        "reports/council_list.html",
        {
            "active": "council_list",
            "group": group,
            "groups": groups,
            "upcoming": upcoming,
            "held": held,
            "pending_minutes_count": len(pending_minutes),
        },
    )


@login_required(login_url="reports:login")
@require_http_methods(["GET", "POST"])
def council_create(request):
    """عقد جلسة مجلس جديدة."""
    groups = _director_groups(request)
    group = _selected_group(request, groups)
    allowed = executive_director_schools_qs(request.user).filter(group=group).order_by("name")

    form = GroupMeetingForm(
        request.POST or None,
        group=group,
        organizer=request.user,
        allowed_schools=allowed,
    )

    if request.method == "POST" and form.is_valid():
        managers, unreachable = form.resolve_attendees()
        if not managers:
            messages.error(
                request,
                "لا يوجد من يُدعى: المدارس المختارة بلا مدير نشط. "
                "عيّن مديراً لكل مدرسة ثم أعد المحاولة.",
            )
        else:
            with transaction.atomic():
                meeting = form.save(commit=False)
                meeting.organizer = request.user
                meeting.save()
                for manager in managers:
                    MeetingAttendee.objects.create(meeting=meeting, person=manager)

            messages.success(request, f"أُنشئت الجلسة ووُجّهت الدعوة إلى {len(managers)} مديراً.")
            if unreachable:
                names = "، ".join(school.name for school in unreachable)
                messages.warning(request, f"لم تُوجَّه دعوة إلى: {names} — لا مدير نشط لها.")
            return redirect("reports:council_detail", pk=meeting.pk)

    elif request.method == "POST":
        messages.error(request, "تعذّر إنشاء الجلسة — تحقّق من الحقول.")

    return render(
        request,
        "reports/council_create.html",
        {"active": "council_list", "group": group, "groups": groups, "form": form},
    )


@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def council_detail(request, pk: int):
    """جلسة المجلس: جدول الأعمال والحضور والمحضر والقرارات."""
    groups = _director_groups(request)
    meeting = _council_or_404(request, pk, groups)
    is_organizer = meeting.organizer_id == request.user.pk

    minutes = getattr(meeting, "minutes", None)
    if minutes is None and is_organizer and meeting.is_held:
        minutes = ensure_minutes(meeting, recorder=request.user)

    return render(
        request,
        "reports/council_detail.html",
        {
            "active": "council_list",
            "groups": groups,
            "group": meeting.group,
            "meeting": meeting,
            "is_organizer": is_organizer,
            "agenda": list(meeting.agenda_items.all()),
            "attendees": list(meeting.attendees.select_related("person")),
            "attendance": meeting.attendance_summary,
            "attendance_choices": MeetingAttendee.Status.choices,
            "minutes": minutes,
            "minutes_form": MinutesForm(instance=minutes) if minutes is not None else None,
            "minutes_actions": (
                available_actions(minutes, request.user, school=None) if minutes else []
            ),
            "minutes_timeline": list(transitions_for(minutes)) if minutes else [],
            "agenda_form": AgendaItemForm(),
            "decision_form": DecisionForm(meeting=meeting),
            "decisions": decision_followup_rows(meeting),
        },
    )


@login_required(login_url="reports:login")
@require_http_methods(["POST"])
def council_action(request, pk: int):
    """إجراءات المدير التنفيذي على جلسته."""
    groups = _director_groups(request)
    meeting = _council_or_404(request, pk, groups)
    action = (request.POST.get("meeting_action") or "").strip()

    try:
        if action == "add_agenda":
            form = AgendaItemForm(request.POST)
            if meeting.organizer_id != request.user.pk:
                raise PermissionDenied("جدول الأعمال يعدّه منظّم الجلسة.")
            if not form.is_valid():
                messages.error(request, "اكتب عنوان البند.")
            else:
                item = form.save(commit=False)
                item.meeting = meeting
                item.order = (meeting.agenda_items.aggregate(top=Max("order"))["top"] or 0) + 1
                item.save()
                messages.success(request, "أُضيف بند إلى جدول الأعمال.")

        elif action == "remove_agenda":
            if meeting.organizer_id != request.user.pk:
                raise PermissionDenied("جدول الأعمال يعدّه منظّم الجلسة.")
            get_object_or_404(
                MeetingAgendaItem, pk=request.POST.get("item_id"), meeting=meeting
            ).delete()
            messages.success(request, "حُذف البند.")

        elif action == "mark_held":
            mark_held(meeting, request.user)
            ensure_minutes(meeting, recorder=request.user)
            messages.success(request, "سُجِّل انعقاد الجلسة، وفُتح المحضر للكتابة.")

        elif action == "cancel":
            cancel_meeting(meeting, request.user, reason=request.POST.get("reason", ""))
            messages.success(request, "أُلغيت الجلسة. تبقى دعوتها في السجل.")

        elif action == "attendance":
            rows = {
                key.split("attendance_", 1)[1]: value
                for key, value in request.POST.items()
                if key.startswith("attendance_")
            }
            set_attendance(meeting, request.user, rows=rows)
            messages.success(request, "سُجِّل الحضور.")

        elif action == "save_minutes":
            minutes = ensure_minutes(meeting, recorder=request.user)
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

        elif action == "add_decision":
            if meeting.organizer_id != request.user.pk:
                raise PermissionDenied("إصدار القرارات لرئيس المجلس.")
            form = DecisionForm(request.POST, meeting=meeting)
            if not form.is_valid():
                messages.error(request, "تعذّر تسجيل القرار — تحقّق من الحقول.")
            else:
                decision = form.save(commit=False)
                decision.meeting = meeting
                decision.order = (meeting.decisions.aggregate(top=Max("order"))["top"] or 0) + 1
                decision.save()
                messages.success(request, "صدر القرار.")

        elif action == "track_decision":
            decision = get_object_or_404(
                meeting.decisions.all(), pk=request.POST.get("decision_id")
            )
            convert_decision_to_assignment(decision, request.user)
            messages.success(
                request,
                "حُوِّل القرار إلى تكليف على مدرسة المسؤول — يُتابَع الآن بموعده.",
            )

        else:
            messages.error(request, "إجراء غير معروف.")

    except PermissionDenied as exc:
        messages.error(request, str(exc) or "لا تملك هذا الإجراء.")
    except (MeetingError, ApprovalError, ValidationError) as exc:
        detail = getattr(exc, "messages", None) or [str(exc)]
        messages.error(request, detail[0])

    return redirect("reports:council_detail", pk=pk)


@login_required(login_url="reports:login")
@require_http_methods(["POST"])
def council_minutes_action(request, pk: int):
    """اعتماد محضر المجلس — بالمكوّن المشترك."""
    groups = _director_groups(request)
    meeting = _council_or_404(request, pk, groups)
    minutes = getattr(meeting, "minutes", None)
    if minutes is None:
        messages.error(request, "لم يُفتح محضر لهذه الجلسة بعد.")
        return redirect("reports:council_detail", pk=pk)

    action = (request.POST.get("approval_action") or "").strip()
    note = (request.POST.get("note") or "").strip()

    handler = ACTION_DISPATCH.get(action)
    if handler is None or action not in available_actions(minutes, request.user, school=None):
        messages.error(request, "هذا الإجراء غير متاح على المحضر الآن.")
        return redirect("reports:council_detail", pk=pk)

    try:
        handler(minutes, request.user, school=None, note=note)
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
                "withdraw": "سُحب المحضر للتعديل.",
                "approve": "اعتُمد محضر الجلسة.",
            }.get(action, "نُفِّذ الإجراء."),
        )

    return redirect("reports:council_detail", pk=pk)
