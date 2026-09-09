"""Read model for the school manager's cross-feature approval queue.

The platform has several approval-capable models, but the original inbox only
queried reports and documents.  Keeping this inventory in one service lets the
dashboard counter and the inbox render the exact same set of actionable work.
"""
from __future__ import annotations

from django.urls import reverse
from django.utils import timezone

from .model_parts.approvals import ApprovalState, PENDING_REVIEW_STATES
from .models import (
    AssignmentTarget,
    CircularDraft,
    Document,
    Initiative,
    LabExperiment,
    MeetingMinutes,
    Plan,
    Report,
    TeacherAchievementFile,
)
from .services_approval import available_actions


_ACTIONABLE_ACTIONS = frozenset({"approve", "issue", "recommend", "start_review"})
_STATE_ORDER = {
    ApprovalState.RECOMMENDED: 0,
    ApprovalState.SUBMITTED: 1,
    ApprovalState.UNDER_REVIEW: 2,
}


def _person_name(person, snapshot: str = "") -> str:
    return (snapshot or getattr(person, "name", "") or "غير محدد").strip()


def _approval_row(
    *,
    item,
    user,
    school,
    kind: str,
    kind_label: str,
    kind_icon: str,
    title: str,
    subtitle: str,
    detail_url: str,
    action_url: str = "",
    inline_action: bool = False,
) -> dict:
    actions = available_actions(item, user, school=school)
    submitted_at = getattr(item, "submitted_at", None)
    waiting_days = (
        max(0, (timezone.now() - submitted_at).days) if submitted_at else 0
    )
    return {
        "item": item,
        "kind": kind,
        "kind_label": kind_label,
        "kind_icon": kind_icon,
        "title": title,
        "subtitle": subtitle,
        "detail_url": detail_url,
        "action_url": action_url,
        "inline_action": inline_action,
        "actions": actions,
        "is_mine": bool(_ACTIONABLE_ACTIONS.intersection(actions)),
        "state": item.approval_state,
        "state_label": item.get_approval_state_display(),
        "tone": item.approval_tone,
        "submitted_at": submitted_at,
        "waiting_days": waiting_days,
        "pk": item.pk,
    }


def manager_approval_rows(user, school) -> list[dict]:
    """Return every pending school item on which the manager can act.

    Rows are deliberately normalized for presentation.  Only reports and
    documents keep the existing one-click action because the other item types
    carry operational or publication consequences that should be reviewed in
    their detail screen before a final decision.
    """
    if school is None:
        return []

    rows: list[dict] = []

    reports = (
        Report.objects.filter(school=school, approval_state__in=PENDING_REVIEW_STATES)
        .select_related("teacher", "category", "reviewed_by")
        .order_by("submitted_at", "id")[:200]
    )
    for report in reports:
        category = getattr(report, "category", None)
        subtitle = f"بواسطة {report.teacher_display_name}"
        if category is not None:
            subtitle += f" · {category.name}"
        rows.append(
            _approval_row(
                item=report,
                user=user,
                school=school,
                kind="report",
                kind_label="تقرير",
                kind_icon="fa-chart-line",
                title=report.title,
                subtitle=subtitle,
                detail_url=reverse("reports:approval_detail", args=[report.pk]),
                action_url=reverse("reports:approval_action", args=[report.pk]),
                inline_action=True,
            )
        )

    documents = (
        Document.objects.filter(school=school, approval_state__in=PENDING_REVIEW_STATES)
        .select_related("owner", "department", "reviewed_by")
        .order_by("submitted_at", "id")[:200]
    )
    for document in documents:
        subtitle = f"بواسطة {_person_name(document.owner, document.owner_name)}"
        if document.department_id:
            subtitle += f" · {document.department.name}"
        subtitle += f" · {document.get_kind_display()}"
        rows.append(
            _approval_row(
                item=document,
                user=user,
                school=school,
                kind="document",
                kind_label="وثيقة",
                kind_icon="fa-file-lines",
                title=document.title,
                subtitle=subtitle,
                detail_url=reverse("reports:document_detail", args=[document.pk]),
                action_url=reverse("reports:document_action", args=[document.pk]),
                inline_action=True,
            )
        )

    targets = (
        AssignmentTarget.objects.filter(
            school=school,
            approval_state__in=PENDING_REVIEW_STATES,
            assignment__cancelled_at__isnull=True,
        )
        .select_related("assignment", "assignee", "reviewed_by")
        .order_by("submitted_at", "id")[:200]
    )
    for target in targets:
        rows.append(
            _approval_row(
                item=target,
                user=user,
                school=school,
                kind="assignment",
                kind_label="تنفيذ تكليف",
                kind_icon="fa-list-check",
                title=target.assignment.title,
                subtitle=f"تنفيذ {_person_name(target.assignee)} · الإنجاز {target.progress_percent}%",
                detail_url=reverse("reports:assignment_detail", args=[target.pk]),
            )
        )

    plans = (
        Plan.objects.filter(
            school=school,
            scope=Plan.Scope.SCHOOL,
            approval_state__in=PENDING_REVIEW_STATES,
        )
        .select_related("owner", "reviewed_by")
        .order_by("submitted_at", "id")[:200]
    )
    for plan in plans:
        rows.append(
            _approval_row(
                item=plan,
                user=user,
                school=school,
                kind="plan",
                kind_label="خطة",
                kind_icon="fa-compass-drafting",
                title=plan.title,
                subtitle=f"أعدّها {_person_name(plan.owner, plan.owner_name)} · {plan.get_stage_display()}",
                detail_url=reverse("reports:plan_detail", args=[plan.pk]),
            )
        )

    minutes_rows = (
        MeetingMinutes.objects.filter(
            meeting__school=school,
            approval_state__in=PENDING_REVIEW_STATES,
        )
        .select_related("meeting", "recorder", "reviewed_by")
        .order_by("submitted_at", "id")[:200]
    )
    for minutes in minutes_rows:
        rows.append(
            _approval_row(
                item=minutes,
                user=user,
                school=school,
                kind="minutes",
                kind_label="محضر اجتماع",
                kind_icon="fa-users-rectangle",
                title=minutes.meeting.title,
                subtitle=f"كتبه {_person_name(minutes.recorder)}",
                detail_url=reverse("reports:meeting_detail", args=[minutes.meeting_id]),
            )
        )

    initiatives = (
        Initiative.objects.filter(
            school=school,
            approval_state__in=PENDING_REVIEW_STATES,
        )
        .select_related("teacher", "reviewed_by")
        .order_by("submitted_at", "id")[:200]
    )
    for initiative in initiatives:
        rows.append(
            _approval_row(
                item=initiative,
                user=user,
                school=school,
                kind="initiative",
                kind_label="مبادرة",
                kind_icon="fa-lightbulb",
                title=initiative.title,
                subtitle=f"قدّمها {_person_name(initiative.teacher, initiative.teacher_name)}",
                detail_url=f"{reverse('reports:initiative_list')}#initiative-{initiative.pk}",
            )
        )

    drafts = (
        CircularDraft.objects.filter(
            school=school,
            approval_state__in=PENDING_REVIEW_STATES,
        )
        .select_related("owner", "department", "reviewed_by")
        .order_by("submitted_at", "id")[:200]
    )
    for draft in drafts:
        rows.append(
            _approval_row(
                item=draft,
                user=user,
                school=school,
                kind="circular",
                kind_label="مسودة تعميم",
                kind_icon="fa-file-pen",
                title=draft.title,
                subtitle=f"أعدّها {_person_name(draft.owner, draft.owner_name)} · الاعتماد ينشرها",
                detail_url=reverse("reports:circular_draft_detail", args=[draft.pk]),
            )
        )

    experiments = (
        LabExperiment.objects.filter(
            school=school,
            approval_state__in=PENDING_REVIEW_STATES,
        )
        .select_related("recorder", "department", "reviewed_by")
        .order_by("submitted_at", "id")[:200]
    )
    for experiment in experiments:
        subtitle = f"سجّلها {_person_name(experiment.recorder)}"
        if experiment.lab_kind:
            subtitle += f" · {experiment.get_lab_kind_display()}"
        rows.append(
            _approval_row(
                item=experiment,
                user=user,
                school=school,
                kind="experiment",
                kind_label="تجربة مختبر",
                kind_icon="fa-flask-vial",
                title=experiment.title or "تجربة بلا عنوان",
                subtitle=subtitle,
                detail_url=reverse("reports:lab_experiment_detail", args=[experiment.pk]),
            )
        )

    achievement_files = (
        TeacherAchievementFile.objects.filter(
            school=school,
            status=TeacherAchievementFile.Status.SUBMITTED,
        )
        .exclude(teacher=user)
        .select_related("teacher")
        .order_by("submitted_at", "id")[:200]
    )
    for achievement in achievement_files:
        submitted_at = achievement.submitted_at
        waiting_days = (
            max(0, (timezone.now() - submitted_at).days) if submitted_at else 0
        )
        rows.append(
            {
                "item": achievement,
                "kind": "achievement",
                "kind_label": "ملف إنجاز",
                "kind_icon": "fa-award",
                "title": f"ملف إنجاز {_person_name(achievement.teacher, achievement.teacher_name)}",
                "subtitle": f"السنة الدراسية {achievement.academic_year}",
                "detail_url": reverse("reports:achievement_file_detail", args=[achievement.pk]),
                "action_url": "",
                "inline_action": False,
                "actions": [],
                "is_mine": True,
                "state": ApprovalState.SUBMITTED,
                "state_label": "بانتظار الاعتماد",
                "tone": "pending",
                "submitted_at": submitted_at,
                "waiting_days": waiting_days,
                "pk": achievement.pk,
            }
        )

    # Do not show read-only rows in a manager decision queue.  Ownership rules
    # can make a submitted manager-authored record visible but not actionable;
    # its correct next step remains on its feature page, not in "دورك الآن".
    rows = [row for row in rows if row["is_mine"]]
    rows.sort(
        key=lambda row: (
            _STATE_ORDER.get(row["state"], 9),
            -row["waiting_days"],
            -row["pk"],
        )
    )
    return rows


def manager_approval_count(user, school) -> int:
    """The dashboard badge, sourced from the same rows as the inbox."""
    return len(manager_approval_rows(user, school))
