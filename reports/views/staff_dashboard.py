# -*- coding: utf-8 -*-
"""مؤشرات نطاق الوكيل والموظف الإداري.

**لماذا شاشة مستقلة لا لوحة المدير نفسها؟** لأن ``view_school_dashboard`` نصُّها
«يرى مؤشرات المدرسة ضمن نطاقه **دون بيانات خارج إشرافه**»، ولوحة المدير تحمل
الاشتراك والفوترة والمقاعد والمساحة وتنبيهات التجديد — وهي بيانات المدرسة
كمنشأة، لا مؤشرات عمل. ففتحُها للوكيل يعطيه أكثر مما مُنح، وتقليمُها بشروط
داخلها يحوّل ١٧٠٠ سطر إلى شاشتين متشابكتين في ملف واحد.

وقبل هذه الشاشة كانت الصلاحية **معرَّفة ولا يفحصها سطر واحد**: يمنحها المدير في
شاشة الأدوار، ويظنّها نافذة، ولا يحدث شيء.

**كل رقم هنا مقصور على أقسام النطاق.** ونطاقٌ بلا أقسام يعني لا شيء لا كل شيء —
وهي القاعدة نفسها في كل موضع يقرأ ``supervised_department_ids``. ولذلك تُعلن
الشاشة عدد أقسامها: صفرٌ يُقرأ «لم يُضبط نطاقك بعد» لا «مدرستك فارغة».

قراءة فقط: لا ``POST`` ولا زرّ إجراء. المراجعة تُمارَس من صندوق الاعتماد،
والتكليف من لوحة التكليفات — وهذه الشاشة تقول «أين تقف الأمور» لا تحرّكها.
"""
from __future__ import annotations

from ._helpers import *  # noqa: F401,F403
from ._helpers import _get_active_school
from .. import capabilities as caps
from ..model_parts.approvals import ApprovalState, PENDING_REVIEW_STATES
from ..model_parts.assignments import AssignmentTarget
from ..model_parts.documents import Document
from ..model_parts.meetings import Meeting
from ..model_parts.plans import PlanTask
from ..coverage import pending_documenters, school_staff_queryset
from ..models import Report, SchoolMembership, TeacherAchievementFile
from ..permissions import (
    capability_source,
    is_school_manager,
    supervised_department_ids,
)

__all__ = ["staff_dashboard"]


def _scoped_teacher_ids(supervised) -> set[int]:
    """منسوبو الأقسام المشمولة بالنطاق."""
    if not supervised:
        return set()
    try:
        from ..models import DepartmentMembership
    except Exception:
        return set()
    return {
        int(pk)
        for pk in DepartmentMembership.objects.filter(
            department_id__in=supervised
        ).values_list("teacher_id", flat=True)
        if pk
    }


@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def staff_dashboard(request: HttpRequest) -> HttpResponse:
    """مؤشرات ما يقع تحت إشراف هذا المنسوب."""
    school = _get_active_school(request)
    if school is None:
        messages.error(request, "فضلاً اختر مدرسة أولاً.")
        return redirect("reports:select_school")

    # مدير المدرسة له لوحته الكاملة، فلا يُحوَّل إلى نسخة مصغَّرة منها.
    if is_school_manager(request.user, active_school=school):
        return redirect("reports:admin_dashboard")

    if capability_source(request.user, caps.VIEW_SCHOOL_DASHBOARD, school) is None:
        messages.error(request, "لا تملك صلاحية الاطلاع على مؤشرات المدرسة.")
        return redirect("reports:home")

    supervised = supervised_department_ids(request.user, school)
    scoped_teacher_ids = _scoped_teacher_ids(supervised)

    # ما يملكه صاحب الشاشة يحكم ما تعرضه: بطاقةٌ عن التكليفات لمن لا يكلّف
    # تخبره عن عملٍ لا يملك عليه إجراءً، وهو ما يُدرّبه على تجاهل اللوحة.
    granted = {
        "review": capability_source(request.user, caps.REVIEW_REPORTS, school) is not None,
        "assign": capability_source(request.user, caps.ASSIGN_TASKS, school) is not None,
        "meetings": capability_source(request.user, caps.MANAGE_MEETINGS, school) is not None,
        "plans": capability_source(request.user, caps.TRACK_PLANS, school) is not None,
        "achievements": capability_source(request.user, caps.VIEW_ACHIEVEMENTS, school) is not None,
        "requests": capability_source(request.user, caps.HANDLE_REQUESTS, school) is not None,
        "documents": capability_source(request.user, caps.ARCHIVE_DOCUMENTS, school) is not None,
    }

    now = timezone.now()
    cards: list[dict] = []

    try:
        if granted["review"]:
            pending_reports = (
                Report.objects.filter(
                    school=school, approval_state__in=PENDING_REVIEW_STATES
                )
                .filter(category__departments__id__in=supervised)
                .distinct()
                .count()
                if supervised
                else 0
            )
            cards.append(
                {
                    "key": "reports",
                    "label": "تقارير تنتظر مراجعتك",
                    "value": pending_reports,
                    "url": reverse("reports:approval_inbox"),
                    "icon": "fa-clipboard-check",
                    "tone": "warn" if pending_reports else "calm",
                }
            )

        if granted["assign"]:
            issued = AssignmentTarget.objects.filter(
                assignment__school=school, assignment__issuer=request.user
            ).exclude(approval_state=ApprovalState.APPROVED)
            cards.append(
                {
                    "key": "assignments",
                    "label": "تكليفات أصدرتَها ولم تُنجَز",
                    "value": issued.count(),
                    "extra": issued.filter(assignment__due_at__lt=now).count(),
                    "extra_label": "منها متأخر",
                    "url": reverse("reports:assignment_board"),
                    "icon": "fa-diagram-project",
                    "tone": "warn" if issued.filter(assignment__due_at__lt=now).exists() else "calm",
                }
            )

        if granted["meetings"]:
            pending_minutes = (
                Meeting.objects.filter(school=school, status=Meeting.Status.HELD)
                .filter(Q(organizer=request.user) | Q(minutes__recorder=request.user))
                .exclude(minutes__approval_state=ApprovalState.APPROVED)
                .count()
            )
            cards.append(
                {
                    "key": "minutes",
                    "label": "محاضر تنتظر تحريرك",
                    "value": pending_minutes,
                    "url": reverse("reports:meeting_list"),
                    "icon": "fa-users-rectangle",
                    "tone": "warn" if pending_minutes else "calm",
                }
            )

        if granted["plans"]:
            my_tasks = PlanTask.objects.filter(
                plan__school=school, responsible=request.user
            )
            cards.append(
                {
                    "key": "plan_tasks",
                    "label": "مهام خطة مسندة إليك",
                    "value": my_tasks.count(),
                    "url": reverse("reports:plan_list"),
                    "icon": "fa-compass-drafting",
                    "tone": "calm",
                }
            )

        if granted["achievements"]:
            files = TeacherAchievementFile.objects.filter(
                school=school, teacher_id__in=scoped_teacher_ids
            )
            cards.append(
                {
                    "key": "achievements",
                    "label": "ملفات إنجاز في نطاقك",
                    "value": files.count(),
                    "extra": len(scoped_teacher_ids),
                    "extra_label": "منسوباً في نطاقك",
                    "url": reverse("reports:achievement_school_files"),
                    "icon": "fa-file-lines",
                    "tone": "calm",
                }
            )

        if granted["requests"]:
            open_requests = Ticket.objects.filter(
                school=school,
                is_platform=False,
                department_id__in=supervised,
                status__in=["open", "in_progress"],
            )
            cards.append(
                {
                    "key": "requests",
                    "label": "طلبات مفتوحة في نطاقك",
                    "value": open_requests.count(),
                    "url": reverse("reports:manager_school_tickets"),
                    "icon": "fa-list-check",
                    "tone": "warn" if open_requests.exists() else "calm",
                }
            )

        if granted["documents"]:
            pending_docs = Document.objects.filter(
                school=school, approval_state__in=PENDING_REVIEW_STATES
            ).count()
            cards.append(
                {
                    "key": "documents",
                    "label": "وثائق تنتظر الأرشفة",
                    "value": pending_docs,
                    "url": reverse("reports:document_archive"),
                    "icon": "fa-folder-tree",
                    "tone": "warn" if pending_docs else "calm",
                }
            )
    except Exception:
        logger.exception("Staff dashboard cards failed")
        cards = []

    # ── تغطية التوثيق داخل النطاق ─────────────────────────────────────────
    # هي أنفع ما يُعرض على هذا المستخدم: المدير يتابع مدرسته إجمالاً، والوكيل
    # يتابع قسمين بعينهما — فمعرفة *من* فيهما لم يوثّق هي عملُه اليومي لا خبراً
    # عاماً عنه.
    #
    # **ولا يُضاف لها حارسٌ جديد.** فنصّ ``view_school_dashboard`` هو «يرى
    # مؤشرات المدرسة ضمن نطاقه»، وهذه منها. واختراعُ صلاحيةٍ ثانية لما تشمله
    # الأولى يجعل المدير يمنح صلاحيتين ليحصل على شيءٍ واحد.
    #
    # والنافذة شهرٌ لا سنة: بقيّة بطاقات هذه الشاشة حالاتٌ قائمة الآن، فتغطيةٌ
    # تُقاس على العام كلّه تبدو مطمئنّة بينما القسم صامتٌ منذ أسابيع.
    coverage = None
    if supervised and scoped_teacher_ids:
        try:
            month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            scoped_staff = school_staff_queryset(school, limit_to=scoped_teacher_ids)
            total = scoped_staff.count()
            pending = list(
                pending_documenters(school, since=month_start, limit_to=scoped_teacher_ids)[:6]
            )
            pending_total = pending_documenters(
                school, since=month_start, limit_to=scoped_teacher_ids
            ).count()
            covered = max(0, total - pending_total)
            coverage = {
                "total": total,
                "covered": covered,
                "pending": pending_total,
                "percent": round(covered * 100 / total) if total else 0,
                "pending_preview": pending,
            }
        except Exception:
            logger.exception("Staff dashboard coverage failed")
            coverage = None

    departments = []
    if supervised:
        try:
            departments = list(
                Department.objects.filter(id__in=supervised).order_by("name")
            )
        except Exception:
            departments = []

    return render(
        request,
        "reports/staff_dashboard.html",
        {
            "active": "staff_dashboard",
            "active_school": school,
            "cards": cards,
            "departments": departments,
            "supervised_count": len(supervised),
            "scoped_people": len(scoped_teacher_ids),
            "coverage": coverage,
        },
    )
