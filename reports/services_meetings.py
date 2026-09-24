# -*- coding: utf-8 -*-
"""دورة حياة الاجتماع: الانعقاد والحضور والقرارات.

الفصل عن ``services_approval`` هو الفصل نفسه في التكليفات: ذاك يحكم اعتماد
المحضر، وهذا يحكم ما قبله — أن ينعقد الاجتماع ويُسجَّل حضوره وتُدوَّن قراراته.

وأهم ما فيه :func:`convert_decision_to_assignment` — الجسر الذي يحوّل القرار من
سطر موثَّق إلى عمل قابل للمتابعة. وبدونه يبقى «متابعة تنفيذ القرارات» في
التوصيف بلا مقابل مهما كثُرت المحاضر.
"""
from __future__ import annotations

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from .model_parts.approvals import ApprovalState
from .model_parts.assignments import Assignment, AssignmentTarget
from .model_parts.meetings import Decision, Meeting, MeetingAttendee, MeetingMinutes

__all__ = [
    "MeetingError",
    "assert_meeting_held",
    "mark_held",
    "cancel_meeting",
    "set_attendance",
    "ensure_minutes",
    "convert_decision_to_assignment",
    "meetings_for_user",
    "decision_followup_rows",
]


class MeetingError(ValidationError):
    """إجراء غير مسموح على الاجتماع."""


def _require_organizer(meeting: Meeting, user) -> None:
    if meeting.organizer_id != getattr(user, "pk", None):
        raise PermissionDenied("هذا الاجتماع ليس من تنظيمك.")


def assert_meeting_held(meeting: Meeting) -> None:
    """يفرض أن تكون مخرجات الاجتماع وتسجيلاته بعد انعقاده الفعلي."""
    if not meeting.is_held:
        raise MeetingError("هذا الإجراء متاح بعد تسجيل انعقاد الاجتماع فقط.")


def mark_held(meeting: Meeting, user, *, when=None) -> Meeting:
    """تسجيل انعقاد الاجتماع.

    خطوة صريحة لا مشتقّة من مرور موعده: اجتماعٌ فات وقته ولم ينعقد ليس
    منعقداً، والاشتقاق الزمني كان سيجعل كل اجتماع مؤجَّل يبدو كأنه تمّ.
    """
    _require_organizer(meeting, user)
    if meeting.is_cancelled:
        raise MeetingError("هذا الاجتماع ملغى.")
    if meeting.is_held:
        return meeting

    meeting.status = Meeting.Status.HELD
    meeting.held_at = when or timezone.now()
    meeting.save(update_fields=["status", "held_at"])
    return meeting


def cancel_meeting(meeting: Meeting, user, *, reason: str = "") -> Meeting:
    """إلغاء اجتماع — ولا يُحذف، فالدعوة وصلت المدعوين."""
    _require_organizer(meeting, user)
    if meeting.is_held:
        raise MeetingError("لا يُلغى اجتماع انعقد فعلاً.")
    if meeting.is_cancelled:
        return meeting

    meeting.status = Meeting.Status.CANCELLED
    meeting.cancel_reason = (reason or "")[:255]
    meeting.save(update_fields=["status", "cancel_reason"])
    return meeting


def set_attendance(meeting: Meeting, user, *, rows: dict) -> None:
    """تسجيل الحضور دفعةً واحدة.

    ``rows`` خريطة ``{attendee_id: status}``. تُتجاهَل المفاتيح التي لا تخص هذا
    الاجتماع بدل رفع خطأ: نموذجٌ أُرسل بعد حذف مدعوّ لا يجوز أن يُسقط تسجيل
    البقية.
    """
    _require_organizer(meeting, user)
    assert_meeting_held(meeting)

    valid = {value for value, _label in MeetingAttendee.Status.choices}
    attendees = {item.pk: item for item in meeting.attendees.all()}

    changed = []
    for raw_id, status in (rows or {}).items():
        try:
            attendee = attendees.get(int(raw_id))
        except (TypeError, ValueError):
            continue
        if attendee is None or status not in valid:
            continue
        if attendee.status != status:
            attendee.status = status
            changed.append(attendee)

    if changed:
        MeetingAttendee.objects.bulk_update(changed, ["status"])


def ensure_minutes(meeting: Meeting, *, recorder=None) -> MeetingMinutes:
    """محضر الاجتماع، يُنشأ مسودةً عند أول فتح."""
    assert_meeting_held(meeting)
    minutes = getattr(meeting, "minutes", None)
    if minutes is not None:
        return minutes
    return MeetingMinutes.objects.create(
        meeting=meeting,
        recorder=recorder,
        approval_state=ApprovalState.DRAFT,
    )


@transaction.atomic
def convert_decision_to_assignment(decision: Decision, user) -> Assignment:
    """تحويل قرار إلى تكليف قابل للمتابعة.

    الشرطان — مسؤول وموعد — ليسا تعنّتاً: تكليفٌ بلا مسؤول لا يُنفّذه أحد،
    وبلا موعد لا يتأخر أبداً فلا يُتابَع أبداً. والقرار الذي لا يستوفيهما يبقى
    موثَّقاً في المحضر ولا يدّعي أنه متابَع.

    والتحويل يقع مرة واحدة: قرارٌ يولّد تكليفين يجعل المسؤول يرى المطلوب
    مرتين ويظن أحدهما زائداً.
    """
    meeting = decision.meeting
    assert_meeting_held(meeting)
    if decision.assignment_id is not None:
        raise MeetingError("هذا القرار محوَّل إلى تكليف بالفعل.")
    if meeting.organizer_id != getattr(user, "pk", None):
        raise PermissionDenied("تحويل القرار إلى تكليف لمنظّم الاجتماع.")
    if decision.responsible_id is None:
        raise MeetingError("حدّد المسؤول عن التنفيذ أولاً.")
    if decision.due_at is None:
        raise MeetingError("حدّد موعد التنفيذ أولاً — قرارٌ بلا موعد لا يُتابَع.")
    if not meeting.attendees.filter(person_id=decision.responsible_id).exists():
        raise MeetingError("مسؤول التنفيذ يجب أن يكون من مدعوي الاجتماع.")

    assignment = Assignment.objects.create(
        scope=(
            Assignment.Scope.GROUP
            if meeting.scope == Meeting.Scope.GROUP
            else Assignment.Scope.SCHOOL
        ),
        school=meeting.school,
        group=meeting.group,
        department=meeting.department,
        issuer=user,
        # المصدر يبقى مقروءاً في لوحة التكليفات: تكليفٌ منبثق عن قرار مجلس
        # ليس كتكليف مباشر، ومعرفةُ أصله تغيّر كيف يُقرأ تأخره.
        source=Assignment.Source.DECISION,
        title=decision.title[:200],
        description=decision.body,
        due_at=decision.due_at,
    )

    target_school = meeting.school
    if target_school is None:
        # قرار مجلس مجموعة: المسؤول مدير مدرسة، فتُنسب حصته لمدرسته هو.
        from .model_parts.schools import SchoolMembership

        membership = (
            SchoolMembership.objects.filter(
                teacher_id=decision.responsible_id,
                role_type=SchoolMembership.RoleType.MANAGER,
                is_active=True,
            )
            .select_related("school")
            .first()
        )
        target_school = getattr(membership, "school", None)

    AssignmentTarget.objects.create(
        assignment=assignment,
        assignee_id=decision.responsible_id,
        school=target_school,
    )

    decision.assignment = assignment
    decision.save(update_fields=["assignment"])
    return assignment


# ─────────────────────────────────────────────────────────────────────────────
# استعلامات العرض
# ─────────────────────────────────────────────────────────────────────────────
def meetings_for_user(user, *, school=None, group=None):
    """اجتماعات يراها المستخدم: ما نظّمه أو ما دُعي إليه."""
    from django.db.models import Q

    qs = (
        Meeting.objects.filter(Q(organizer=user) | Q(attendees__person=user))
        .select_related(
            "organizer",
            "department",
            "school",
            "group",
            "minutes",
            "minutes__recorder",
        )
        .prefetch_related("attendees", "decisions")
        .distinct()
        .order_by("-scheduled_at", "-id")
    )
    if school is not None:
        qs = qs.filter(school=school)
    if group is not None:
        qs = qs.filter(group=group)
    return qs


def decision_followup_rows(meeting: Meeting) -> list[dict]:
    """قرارات الاجتماع مع حالة تنفيذ كلٍّ منها."""
    decisions = (
        meeting.decisions.select_related("responsible", "assignment", "agenda_item")
        .prefetch_related("assignment__targets")
        .order_by("order", "id")
    )
    return [
        {
            "decision": decision,
            "state": decision.execution_state,
            "convertible": decision.can_become_assignment(),
        }
        for decision in decisions
    ]
