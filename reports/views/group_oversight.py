# -*- coding: utf-8 -*-
"""إشراف المدير التنفيذي: تفاصيل المدرسة، وسجل المجموعة، وأرشيفها.

ثلاث شاشات تغلق ما تبقّى من بنود هذا الدور، وكلها **قراءة فقط** — على النهج
الذي أُسّس في ``school_groups.py``: المدير التنفيذي يشرف ويتابع، ولا يتولى
الإدارة اليومية لأي مدرسة ولا يعدّل بياناتها.

والفرق بين «الاطلاع» و«الإدارة» هنا ليس تفصيلاً لغوياً: شاشةٌ تعرض زر تعديل
لمن لا يملك التعديل تُغري باستعماله ثم تردّه — وهي أسوأ من ألا تعرضه.
"""
from __future__ import annotations

from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from ..audit_labels import attach_views
from ..model_parts.approvals import ApprovalState, PENDING_REVIEW_STATES
from ..models import (
    AssignmentTarget,
    AuditLog,
    Document,
    Initiative,
    Meeting,
    MeetingMinutes,
    Plan,
    Report,
    School,
    SchoolArchiveAddon,
    SchoolMembership,
    SchoolSubscription,
    SchoolYearArchive,
    TeacherAchievementFile,
)
from ..services_approval import available_actions
from ..services_archive import attach_school_consumption_rows
from ..permissions import executive_director_schools_qs
from ..view_access import executive_director_groups_or_404 as _director_groups

__all__ = [
    "group_school_detail",
    "group_subscriptions",
    "group_audit_log",
    "group_archive",
    "group_approval_inbox",
]


def _selected_group(request, groups):
    requested = (request.GET.get("group") or "").strip()
    if requested:
        return next((item for item in groups if str(item.pk) == requested), groups[0])
    return groups[0]


# ─────────────────────────────────────────────────────────────────────────────
# تفاصيل مدرسة واحدة
# ─────────────────────────────────────────────────────────────────────────────
@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def group_school_detail(request, pk: int):
    """تفاصيل مدرسة واحدة من مدارس المجموعة — اطّلاعاً لا إدارة.

    **ما يُعرض وما لا يُعرض**: تُعرض المؤشرات المجمَّعة وما رفعته المدرسة إلى
    المجموعة. ولا يُعرض محتوى تقرير معلّم ولا ملف إنجازه — فالمدير التنفيذي
    يتابع أداء المدرسة لا يفتّش أعمال منسوبيها، والتوصيف يمنعه صراحةً من
    تعديل تقارير المنسوبين أو حذف وثائقهم.
    """
    groups = _director_groups(request)
    allowed = executive_director_schools_qs(request.user)
    school = get_object_or_404(allowed.select_related("subscription"), pk=pk)
    group = school.group

    now = timezone.now()
    since = now - timedelta(days=30)

    reports = Report.objects.filter(school=school)
    targets = AssignmentTarget.objects.filter(
        Q(school=school) | Q(assignment__school=school)
    )

    stats = {
        "seats": SchoolMembership.seats_used(school),
        "reports_total": reports.count(),
        "reports_recent": reports.filter(created_at__gte=since).count(),
        "reports_previous": reports.filter(
            created_at__gte=since - timedelta(days=30), created_at__lt=since
        ).count(),
        "reports_pending": reports.filter(
            approval_state__in=PENDING_REVIEW_STATES
        ).count(),
        "achievements": TeacherAchievementFile.objects.filter(school=school).count(),
        "assignments": targets.count(),
        "assignments_done": targets.filter(
            approval_state=ApprovalState.APPROVED
        ).count(),
        "assignments_overdue": targets.filter(
            assignment__due_at__lt=now, assignment__cancelled_at__isnull=True
        ).exclude(approval_state=ApprovalState.APPROVED).count(),
        "meetings": Meeting.objects.filter(
            school=school, status=Meeting.Status.HELD
        ).count(),
        "plans": Plan.objects.filter(school=school).count(),
        "documents": Document.objects.filter(
            school=school, approval_state=ApprovalState.APPROVED
        ).count(),
    }
    stats["completion"] = (
        round(stats["assignments_done"] * 100 / stats["assignments"])
        if stats["assignments"]
        else 0
    )
    stats["reports_delta"] = stats["reports_recent"] - stats["reports_previous"]

    # ما رفعته المدرسة إلى المجموعة — وهو ما يخص المدير التنفيذي فعلاً.
    group_targets = list(
        targets.filter(assignment__group__isnull=False)
        .select_related("assignment", "assignee")
        .order_by("-assignment__due_at")[:15]
    )
    shared_practices = list(
        Initiative.objects.filter(
            school=school,
            approval_state=ApprovalState.APPROVED,
            shared_at__isnull=False,
        ).select_related("teacher")[:10]
    )

    manager = (
        SchoolMembership.objects.filter(
            school=school,
            role_type=SchoolMembership.RoleType.MANAGER,
            is_active=True,
        )
        .select_related("teacher")
        .first()
    )

    # بطاقة صحة واحدة تجمع الإشارات المتناثرة، حتى لا يفسّر المدير التنفيذي
    # تسعة أرقام قبل أن يعرف هل المدرسة مستقرة أم تحتاج تدخلاً.
    attach_school_consumption_rows([school])
    subscription_row = _group_subscription_row(
        school,
        getattr(school, "subscription", None),
        SchoolArchiveAddon.objects.filter(school=school).first(),
        getattr(manager, "teacher", None),
    )
    risk_score = 0
    risk_reasons = []
    state_key = subscription_row["state"]["key"]
    if state_key in {"none", "expired", "cancelled"}:
        risk_score += 50
        risk_reasons.append("الاشتراك متوقف")
    elif state_key in {"urgent", "soon"}:
        risk_score += 25
        risk_reasons.append("الاشتراك يقترب من الانتهاء")
    if subscription_row["consumption"]["storage"]["percent"] >= 90:
        risk_score += 30
        risk_reasons.append("مساحة التخزين حرجة")
    if subscription_row["consumption"]["seats"]["percent"] >= 90:
        risk_score += 20
        risk_reasons.append("المقاعد قاربت الامتلاء")
    if stats["assignments_overdue"]:
        risk_score += 25
        risk_reasons.append("تكليفات متأخرة")
    if stats["reports_pending"]:
        risk_score += 10
        risk_reasons.append("تقارير تنتظر الاعتماد")
    if not stats["reports_recent"]:
        risk_score += 10
        risk_reasons.append("لا نشاط تقارير خلال 30 يوماً")
    risk_score = min(risk_score, 100)
    if risk_score >= 50:
        risk_tone, risk_label = "danger", "تحتاج تدخلاً"
    elif risk_score >= 20:
        risk_tone, risk_label = "warning", "تحتاج متابعة"
    else:
        risk_tone, risk_label = "stable", "مستقرة"

    return render(
        request,
        "reports/group_school_detail.html",
        {
            "active": "executive_dashboard",
            "groups": groups,
            "group": group,
            "school": school,
            "manager": getattr(manager, "teacher", None),
            "stats": stats,
            "subscription_row": subscription_row,
            "risk_score": risk_score,
            "risk_reasons": risk_reasons,
            "risk_tone": risk_tone,
            "risk_label": risk_label,
            "group_targets": group_targets,
            "shared_practices": shared_practices,
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# اشتراكات المجموعة
# ─────────────────────────────────────────────────────────────────────────────
# نافذتان لا واحدة: «عاجل» ما يُتصل بشأنه اليوم، و«قريب» ما يُخطَّط له.
# دمجهما في رقم واحد كان يجعل مدرسةً تنتهي بعد ثلاثة أيام ومدرسةً تنتهي بعد
# ثلاثين تقرآن بالإلحاح نفسه.
SUBSCRIPTION_URGENT_DAYS = 7
SUBSCRIPTION_SOON_DAYS = 30

_SUBSCRIPTION_PRIORITY_ORDER = {
    "none": 0,
    "expired": 1,
    "cancelled": 2,
    "urgent": 3,
    "soon": 4,
    "active": 5,
}


def _group_subscription_row(school, subscription, addon, manager) -> dict:
    """صف اشتراك مدرسة واحدة كما يقرؤه المدير التنفيذي.

    الحالة تُحسب هنا لا في القالب: القالب لا يعرف الطرح بين تاريخين، وحسابُها
    في مكانين يجعل بطاقة العدّاد تخالف الصف الذي تحتها.
    """
    if subscription is None:
        state = {"key": "none", "label": "بلا اشتراك", "days": None}
    elif getattr(subscription, "is_cancelled", False):
        state = {"key": "cancelled", "label": "ملغى", "days": 0}
    elif getattr(subscription, "is_expired", False):
        state = {"key": "expired", "label": "منتهٍ", "days": 0}
    else:
        try:
            days = int(subscription.days_remaining or 0)
        except Exception:
            days = 0
        if days <= SUBSCRIPTION_URGENT_DAYS:
            state = {"key": "urgent", "label": f"ينتهي خلال {days} يوماً", "days": days}
        elif days <= SUBSCRIPTION_SOON_DAYS:
            state = {"key": "soon", "label": f"يتبقى {days} يوماً", "days": days}
        else:
            state = {"key": "active", "label": f"سارٍ · {days} يوماً", "days": days}

    consumption = getattr(school, "consumption_row", None) or {
        "storage": {"percent": 0, "is_unlimited": True, "label": "—"},
        "seats": {"percent": 0, "is_unlimited": True, "label": "—"},
        "archive": {"percent": 0, "is_subscribed": False, "label": "—"},
    }

    archive_active = bool(addon and getattr(addon, "is_active", False))
    return {
        "school": school,
        "subscription": subscription,
        "manager": manager,
        "state": state,
        "order": _SUBSCRIPTION_PRIORITY_ORDER.get(state["key"], 9),
        "consumption": consumption,
        "archive_addon": addon,
        "archive_active": archive_active,
        # ضغط السعة والمساحة يوقف العمل كما يوقفه انتهاء الاشتراك، فيُعرض معه.
        "is_pressured": (
            consumption["storage"]["percent"] >= 90
            or consumption["seats"]["percent"] >= 90
        ),
    }


@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def group_subscriptions(request):
    """اشتراكات مدارس المجموعة في كشف واحد — اطّلاعاً لا شراءً.

    **لماذا لا يوجد زر دفع هنا.** الاشتراك يُشترى على مدرسة بعينها ويُحسب على
    سعتها ومساحتها، وشراؤه من غير مديرها يجعل المدير يُفاجأ بسعةٍ لم يخترها.
    فما يخص المدير التنفيذي هو **أن يعرف قبل أن ينقطع العمل**: أي مدرسة اشتراكها
    ينتهي هذا الأسبوع، وأيها تجاوزت سعتها، وبمن يتصل في كل حالة.

    ولذلك يحمل كل صف اسم مدير المدرسة ورقمه: التنبيه بلا وجهةٍ للاتصال تنبيهٌ
    ناقص.
    """
    groups = _director_groups(request)
    group = _selected_group(request, groups)

    schools = list(
        executive_director_schools_qs(request.user)
        .filter(group=group)
        .order_by("name")
    )
    # أربعة استعلامات مجمّعة لكل المدارس مهما كان عددها — الدالة نفسها التي
    # تغذّي لوحة المجموعة، فلا يفترق رقمٌ بين الشاشتين.
    attach_school_consumption_rows(schools)

    school_ids = [school.pk for school in schools]
    subscriptions = {
        item.school_id: item
        for item in SchoolSubscription.objects.filter(
            school_id__in=school_ids
        ).select_related("plan")
    }
    addons = {
        item.school_id: item
        for item in SchoolArchiveAddon.objects.filter(school_id__in=school_ids)
    }
    managers = {}
    for membership in (
        SchoolMembership.objects.filter(
            school_id__in=school_ids,
            role_type=SchoolMembership.RoleType.MANAGER,
            is_active=True,
        )
        .select_related("teacher")
        .order_by("school_id", "id")
    ):
        managers.setdefault(membership.school_id, membership.teacher)

    rows = [
        _group_subscription_row(
            school,
            subscriptions.get(school.pk),
            addons.get(school.pk),
            managers.get(school.pk),
        )
        for school in schools
    ]

    state_filter = (request.GET.get("state") or "all").strip().lower()
    known_states = {"all", "attention", "active", "expired"}
    if state_filter not in known_states:
        state_filter = "all"

    def _matches(row) -> bool:
        if state_filter == "attention":
            return row["order"] <= 4 or row["is_pressured"]
        if state_filter == "active":
            return row["state"]["key"] == "active"
        if state_filter == "expired":
            return row["state"]["key"] in {"none", "expired", "cancelled"}
        return True

    visible = [row for row in rows if _matches(row)]
    # ما يحتاج إجراءً يُقدَّم، ثم الأقرب انتهاءً — والاسم يكسر التعادل.
    visible.sort(key=lambda row: (row["order"], row["state"]["days"] if row["state"]["days"] is not None else 0, row["school"].name))

    totals = {
        "schools": len(rows),
        "active": sum(1 for row in rows if row["state"]["key"] == "active"),
        "urgent": sum(1 for row in rows if row["state"]["key"] == "urgent"),
        "soon": sum(1 for row in rows if row["state"]["key"] == "soon"),
        "stopped": sum(
            1 for row in rows if row["state"]["key"] in {"none", "expired", "cancelled"}
        ),
        "archive_active": sum(1 for row in rows if row["archive_active"]),
        "pressured": sum(1 for row in rows if row["is_pressured"]),
    }
    totals["attention"] = sum(
        1 for row in rows if row["order"] <= 4 or row["is_pressured"]
    )

    return render(
        request,
        "reports/group_subscriptions.html",
        {
            "active": "group_subscriptions",
            "groups": groups,
            "group": group,
            "rows": visible,
            "totals": totals,
            "state_filter": state_filter,
            "urgent_days": SUBSCRIPTION_URGENT_DAYS,
            "soon_days": SUBSCRIPTION_SOON_DAYS,
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# سجل إجراءات المجموعة
# ─────────────────────────────────────────────────────────────────────────────
@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def group_audit_log(request):
    """سجل الإجراءات عبر مدارس المجموعة.

    بند صريح في التوصيف: «الاطلاع على سجل الإجراءات والاعتمادات». وهو **قراءة
    محضة**: السجل لا يُعدَّل ولا يُحذف — لا منه ولا من مدير المدرسة — وذلك
    مفروض في النموذج نفسه لا في هذه الشاشة.
    """
    groups = _director_groups(request)
    group = _selected_group(request, groups)
    school_ids = list(
        executive_director_schools_qs(request.user)
        .filter(group=group)
        .values_list("id", flat=True)
    )

    logs = AuditLog.objects.filter(school_id__in=school_ids).select_related(
        "teacher", "school"
    )

    school_filter = (request.GET.get("school") or "").strip()
    action_filter = (request.GET.get("action") or "").strip()
    allowed_actions = {value for value, _label in AuditLog.Action.choices}

    if school_filter.isdigit() and int(school_filter) in school_ids:
        logs = logs.filter(school_id=int(school_filter))
    else:
        school_filter = ""
    if action_filter in allowed_actions:
        logs = logs.filter(action=action_filter)
    else:
        action_filter = ""

    page = Paginator(logs, 50).get_page(request.GET.get("page"))
    attach_views(page)

    params = request.GET.copy()
    params.pop("page", None)

    return render(
        request,
        "reports/group_audit_log.html",
        {
            "active": "group_audit_log",
            "groups": groups,
            "group": group,
            "page_obj": page,
            "schools": School.objects.filter(id__in=school_ids).order_by("name"),
            "actions": AuditLog.Action.choices,
            "q_school": school_filter,
            "q_action": action_filter,
            "qs": params.urlencode(),
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# أرشيف المجموعة
# ─────────────────────────────────────────────────────────────────────────────
@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def group_archive(request):
    """نسخ الأرشيف السنوي لمدارس المجموعة، في مكان واحد.

    **اطّلاع لا إنشاء ولا حذف.** إنشاء نسخة الأرشيف وحذفها يبقيان بيد مدير
    المدرسة: النسخة تُبنى من بيانات مدرسته وتُحسب على سعتها، وتركُ إنشائها
    لغيره يجعله يفاجأ بامتلاء مساحته.

    وما يخص المدير التنفيذي هنا هو **معرفة أي مدرسة أرشفت سنتها وأيها لم
    تفعل** — وهي متابعة إشرافية لا إدارة تخزين.
    """
    groups = _director_groups(request)
    group = _selected_group(request, groups)
    schools = list(
        executive_director_schools_qs(request.user).filter(group=group).order_by("name")
    )
    school_ids = [item.pk for item in schools]

    archives = list(
        SchoolYearArchive.objects.filter(school_id__in=school_ids)
        .select_related("school")
        .order_by("school__name", "-academic_year", "-version")[:200]
    )

    by_school: dict[int, list] = {}
    for archive in archives:
        by_school.setdefault(archive.school_id, []).append(archive)

    rows = [
        {
            "school": school,
            "archives": by_school.get(school.pk, []),
            "latest": (by_school.get(school.pk) or [None])[0],
        }
        for school in schools
    ]
    # مدرسة بلا أرشيف هي ما يستحق التنبيه، فتُقدَّم لا تُذيَّل.
    rows.sort(key=lambda row: (1 if row["archives"] else 0, row["school"].name))

    return render(
        request,
        "reports/group_archive.html",
        {
            "active": "group_archive",
            "groups": groups,
            "group": group,
            "rows": rows,
            "totals": {
                "schools": len(rows),
                "archived": sum(1 for row in rows if row["archives"]),
                "missing": sum(1 for row in rows if not row["archives"]),
                "copies": len(archives),
            },
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# صندوق اعتماد المجموعة
# ─────────────────────────────────────────────────────────────────────────────
_GROUP_STATE_ORDER = {
    ApprovalState.RECOMMENDED: 0,
    ApprovalState.SUBMITTED: 1,
    ApprovalState.UNDER_REVIEW: 2,
    ApprovalState.NEEDS_INFO: 3,
    ApprovalState.RETURNED: 4,
}


@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def group_approval_inbox(request):
    """ما ينتظر قرار المدير التنفيذي على مستوى المجموعة.

    **الاستثناء الوحيد من «قراءة فقط» في هذا الملف** — وهو ليس نقضاً للقاعدة بل
    تطبيقٌ لها: القاعدة أنه لا يتولّى الإدارة اليومية لأي **مدرسة**، وهذا صندوقٌ
    لما **أصدره هو** على مستوى مجموعته. واعتمادُ ردٍّ على تكليفٍ أصدرتَه ليس
    إدارةَ مدرسةٍ، بل إتمامُ عملك أنت.

    وكان هذا العمل مبثوثاً: يفتح كل تكليف على حدة ليرى هل ردّت مدارسه، ويفتح كل
    جلسة مجلس ليرى هل اعتُمد محضرها. فما لم يُفتَح لا يُعرَف أنه ينتظر — وهو
    الفرق نفسه الذي بُني لأجله ``approval_inbox`` لمدير المدرسة.

    **بندٌ واحد لا بندان لنفس الشيء:** حصص التكليف تُعرض مجمَّعةً بتكليفها لا
    صفّاً لكل مدرسة، فتكليفٌ على عشر مدارس بندٌ واحد فيه عشرة ردود — وعرضُها
    عشرة بنود يجعل الصندوق يبدو ممتلئاً بعملٍ واحد.

    و``available_actions`` هي مصدر الأزرار كما في كل صندوق: لا يُعرض إجراء لا
    تذكره، والإجراء نفسه يُنفَّذ من شاشة التكليف أو المحضر حيث يُسجَّل في دورة
    القرار.
    """
    groups = _director_groups(request)
    group = _selected_group(request, groups)

    # ── حصص تكليفات المجموعة التي أصدرها، مجمَّعةً بتكليفها ──
    targets = (
        AssignmentTarget.objects.filter(
            assignment__group=group,
            assignment__issuer=request.user,
            approval_state__in=PENDING_REVIEW_STATES,
        )
        .select_related("assignment", "assignee", "school")
        .order_by("assignment__due_at", "assignment_id", "id")
    )

    assignment_rows: dict[int, dict] = {}
    for target in targets[:400]:
        row = assignment_rows.setdefault(
            target.assignment_id,
            {
                "assignment": target.assignment,
                "targets": [],
                "actions": set(),
                "order": 9,
            },
        )
        row["targets"].append(target)
        row["actions"] |= set(
            available_actions(target, request.user, school=target.school)
        )
        row["order"] = min(
            row["order"], _GROUP_STATE_ORDER.get(target.approval_state, 9)
        )

    assignment_list = sorted(
        assignment_rows.values(), key=lambda row: (row["order"], -row["assignment"].pk)
    )

    # ── محاضر جلسات المجلس التي ينتظرها قرار ──
    minutes = (
        MeetingMinutes.objects.filter(
            meeting__group=group,
            meeting__scope=Meeting.Scope.GROUP,
            approval_state__in=PENDING_REVIEW_STATES,
        )
        .select_related("meeting", "recorder")
        .order_by("-submitted_at", "-id")
    )
    minutes_rows = [
        {
            "minutes": item,
            # محضر المجلس لا مدرسة له، فتُمرَّر ``school=None`` — والخطّاف
            # ``can_review_approval`` يقبل منظّم الاجتماع بلا صلاحية مدرسية،
            # وهو ما يتيح لرئيس المجلس اعتماد محضره دون عضوية في أي مدرسة.
            "actions": available_actions(item, request.user, school=None),
            "order": _GROUP_STATE_ORDER.get(item.approval_state, 9),
        }
        for item in minutes[:100]
    ]
    minutes_rows.sort(key=lambda row: (row["order"], -row["minutes"].pk))

    # ── جلسات انعقدت ولم يُكتب محضرها ──
    minutes_missing = list(
        Meeting.objects.filter(
            group=group,
            scope=Meeting.Scope.GROUP,
            status=Meeting.Status.HELD,
            minutes__isnull=True,
        ).order_by("-scheduled_at", "-id")[:20]
    )

    total = len(assignment_list) + len(minutes_rows) + len(minutes_missing)
    mine = sum(
        1 for row in assignment_list if {"approve", "recommend"} & row["actions"]
    ) + sum(1 for row in minutes_rows if {"approve", "issue"} & set(row["actions"]))

    return render(
        request,
        "reports/group_approval_inbox.html",
        {
            "active": "group_approval_inbox",
            "group": group,
            "groups": groups,
            "assignment_rows": assignment_list,
            "minutes_rows": minutes_rows,
            "minutes_missing": minutes_missing,
            "total": total,
            "mine_count": mine,
        },
    )
