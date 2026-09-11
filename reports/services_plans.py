# -*- coding: utf-8 -*-
"""دورة حياة الخطة والمبادرة.

جسران يخرجان من هذا الملف:

- :func:`convert_task_to_assignment` — من مهمة في خطة إلى عمل يُتابَع. وهو
  الجسر نفسه الذي بُني للقرارات، بمصدر مختلف: ``Assignment.Source.PLAN``.
- :func:`share_initiative` — من ممارسة معتمَدة في مدرسة إلى ممارسة معروضة على
  المجموعة. وهي القناة التي يطلبها التوصيف باسم «مشاركة الممارسات الناجحة».

وما عدا ذلك يعمل بالمكوّنات القائمة: الخطة والمبادرة يرثان دورة الاعتماد،
فتُراجَعان وتُعادان وتُعتمدان بلا منطق جديد.
"""
from __future__ import annotations

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Prefetch, Q
from django.utils import timezone

from .model_parts.approvals import ApprovalState
from .model_parts.assignments import Assignment, AssignmentTarget
from .model_parts.plans import Initiative, Plan, PlanTask

__all__ = [
    "PlanError",
    "convert_task_to_assignment",
    "share_initiative",
    "plans_for_school",
    "plans_visible_to",
    "initiatives_visible_to",
    "plan_board_rows",
    "shared_practices_for_group",
]


class PlanError(ValidationError):
    """إجراء غير مسموح على الخطة أو المبادرة."""


def _may_edit_plan(plan: Plan, user) -> bool:
    if plan.owner_id == getattr(user, "pk", None):
        return True
    from .permissions import is_school_manager

    return bool(plan.school_id) and is_school_manager(user, active_school=plan.school)


@transaction.atomic
def convert_task_to_assignment(task: PlanTask, user) -> Assignment:
    """تحويل مهمة خطة إلى تكليف متابَع.

    الشرطان — مسؤول وموعد — إلزاميان هنا لا عند كتابة المهمة: خطةٌ تُصاغ
    أهدافها ومهامها قبل أن يُسمّى منفّذوها حالةٌ مشروعة في التخطيط، ومنعُها
    يدفع المُعِدّ إلى إسناد وهمي ليُكمل النموذج.
    """
    plan = task.plan
    if task.assignment_id is not None:
        raise PlanError("هذه المهمة محوَّلة إلى تكليف بالفعل.")
    if not _may_edit_plan(plan, user):
        raise PermissionDenied("تحويل مهام الخطة لمُعِدّها أو لمدير المدرسة.")
    if task.responsible_id is None:
        raise PlanError("حدّد المسؤول عن المهمة أولاً.")
    if task.due_at is None:
        raise PlanError("حدّد موعد التنفيذ أولاً — مهمةٌ بلا موعد لا تُتابَع.")

    target_school = plan.school
    if target_school is None:
        # خطة مشتركة: المسؤول مدير مدرسة، فتُنسب حصته لمدرسته.
        from .model_parts.schools import SchoolMembership

        membership = (
            SchoolMembership.objects.filter(
                teacher_id=task.responsible_id,
                role_type=SchoolMembership.RoleType.MANAGER,
                is_active=True,
            )
            .select_related("school")
            .first()
        )
        target_school = getattr(membership, "school", None)

    assignment = Assignment.objects.create(
        scope=(
            Assignment.Scope.GROUP
            if plan.scope == Plan.Scope.GROUP
            else Assignment.Scope.SCHOOL
        ),
        school=plan.school,
        group=plan.group,
        department=task.department,
        issuer=user,
        source=Assignment.Source.PLAN,
        title=task.title[:200],
        description=task.description,
        due_at=task.due_at,
    )
    AssignmentTarget.objects.create(
        assignment=assignment,
        assignee_id=task.responsible_id,
        school=target_school,
    )

    task.assignment = assignment
    task.save(update_fields=["assignment"])

    # أول مهمة تُحوَّل تنقل الخطة من الإعداد إلى التنفيذ: مرحلةٌ تُدار يدوياً
    # تبقى على «قيد الإعداد» بعد أشهر من العمل الفعلي.
    if plan.stage == Plan.Stage.PREPARING:
        plan.stage = Plan.Stage.RUNNING
        plan.save(update_fields=["stage"])

    return assignment


def share_initiative(initiative: Initiative, user) -> Initiative:
    """مشاركة ممارسة ناجحة مع مدارس المجموعة.

    بيد مدير المدرسة وحده: المبادرة عملُ منسوبيه وسمعةُ مدرسته، ونشرُها خارجها
    قرارٌ يخصّه. ولا تُشارَك إلا معتمَدة — ومشاركةُ غير المعتمد نقلٌ إلى مدارس
    أخرى لما لم تتحقق منه مدرستها بعد.
    """
    from .permissions import is_school_manager

    if not is_school_manager(user, active_school=initiative.school):
        raise PermissionDenied("مشاركة الممارسات بيد مدير المدرسة.")
    if initiative.approval_state != ApprovalState.APPROVED:
        raise PlanError("لا تُشارَك مبادرة قبل اعتمادها.")
    if initiative.is_shared:
        return initiative

    initiative.shared_at = timezone.now()
    initiative.shared_by = user
    if not initiative.is_best_practice:
        initiative.is_best_practice = True
    initiative.save(update_fields=["shared_at", "shared_by", "is_best_practice"])
    return initiative


# ─────────────────────────────────────────────────────────────────────────────
# استعلامات العرض
# ─────────────────────────────────────────────────────────────────────────────
def plans_for_school(school):
    return (
        Plan.objects.filter(school=school, scope=Plan.Scope.SCHOOL)
        .select_related("owner")
        .prefetch_related(Prefetch("tasks", queryset=PlanTask.objects.select_related("assignment")))
        .order_by("-created_at", "-id")
    )


def plans_visible_to(user, school):
    """الخطط التي يحق لهذا المستخدم رؤيتها في الكشف.

    **الكشف يوافق التفصيل.** ``plan_for`` في العرض يسمح بأربعة: مُعِدّ الخطة،
    ومدير المدرسة، ومن أُسندت إليه مهمة فيها، ومن مُنح ``track_plans``. وكان
    الكشف يعرض **خطط المدرسة كلها لكل منسوب** — فيرى المعلّم عناوين خطط لا يملك
    فتحها، ويقرأ من العنوان ونسبة الإنجاز ما لم يُقصد أن يقرأه.

    والفرق ليس تجميلاً: خطة «معالجة تدنّي نتائج الصف الثالث» عنوانُها وحده خبر.

    ومن لا خطة له يرى كشفاً فارغاً — وهو الصحيح: الخطط ليست وثيقة عامة في
    المدرسة، ومن يحتاج الاطلاع عليها يُمنح ``track_plans``.
    """
    from .permissions import capability_source, is_school_manager

    base = plans_for_school(school)

    if getattr(user, "is_superuser", False):
        return base
    if is_school_manager(user, active_school=school):
        return base

    from .capabilities import TRACK_PLANS

    if capability_source(user, TRACK_PLANS, school) is not None:
        return base

    # مُعِدُّها أو منفّذُ مهمةٍ فيها. و``distinct`` لازمة: خطةٌ فيها مهمتان
    # لصاحبها نفسه تعود صفّين من الضمّ.
    return base.filter(Q(owner=user) | Q(tasks__responsible=user)).distinct()


def initiatives_visible_to(user, school):
    """مبادرات المدرسة بحسب دور المشاهد.

    المدير يرى السجل الكامل ليراجع ويعتمد ويشارك. والمنسوب يرى
    مقترحاته بكل حالاتها، ومبادرات مدرسته المعتمدة فقط. وبذلك لا
    يتسرّب مقترح زميل قبل قرار المدير، ولا تختفي المبادرة بعد اعتمادها.
    """
    from .permissions import is_school_manager

    base = (
        Initiative.objects.filter(school=school)
        .select_related("teacher", "plan")
        .order_by("-created_at", "-id")
    )
    if is_school_manager(user, active_school=school):
        return base
    return base.filter(Q(teacher=user) | Q(approval_state=ApprovalState.APPROVED)).distinct()


def plan_board_rows(plans) -> list[dict]:
    """صف لكل خطة بمؤشراتها — بلا استعلام لكل صف."""
    rows = []
    for plan in plans:
        summary = plan.task_summary
        rows.append(
            {
                "plan": plan,
                "total": summary["total"],
                "done": summary["done"],
                "late": summary["late"],
                "tracked": summary["tracked"],
                "percent": plan.progress_percent,
            }
        )
    return rows


def shared_practices_for_group(group):
    """الممارسات الناجحة المشتركة داخل المجموعة.

    مقصورة على المعتمَد والمشارَك: ما لم تعتمده مدرسته لا يُعرض على غيرها، وما
    لم يُشارَك يبقى داخلياً — والشرطان معاً لا أحدهما.
    """
    return (
        Initiative.objects.filter(
            school__group=group,
            approval_state=ApprovalState.APPROVED,
            shared_at__isnull=False,
        )
        .select_related("school", "teacher", "shared_by")
        .order_by("-shared_at", "-id")
    )
