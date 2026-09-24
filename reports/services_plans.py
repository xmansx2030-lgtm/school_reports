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
    "plan_department_ids_for_user",
    "plan_resource_in_scope",
    "plan_items_are_mutable",
    "plan_allows_task_conversion",
    "plans_for_school",
    "plans_visible_to",
    "initiatives_visible_to",
    "plan_board_rows",
    "shared_practices_for_group",
]


class PlanError(ValidationError):
    """إجراء غير مسموح على الخطة أو المبادرة."""


def plan_department_ids_for_user(user, school) -> tuple[int, ...]:
    """Return the supervised departments available to ``TRACK_PLANS``.

    The capability answers whether plan tracking authority exists; it never
    manufactures operational scope.  Direct and delegated capability sources
    therefore use the same ``StaffScope`` departments, and an empty scope
    remains empty.
    """
    if user is None or school is None:
        return ()

    from .capabilities import TRACK_PLANS
    from .permissions import capability_source, supervised_department_ids

    if capability_source(user, TRACK_PLANS, school) not in {"scope", "delegation"}:
        return ()
    return tuple(sorted(supervised_department_ids(user, school)))


def plan_resource_in_scope(user, school, plan: Plan) -> bool:
    """Central school/relationship/department scope check for school Plans."""
    if user is None or school is None:
        return False
    if plan.scope != Plan.Scope.SCHOOL or plan.school_id != getattr(school, "pk", None):
        return False
    if getattr(user, "is_superuser", False):
        return True

    from .permissions import is_school_manager

    if is_school_manager(user, active_school=school):
        return True
    if plan.owner_id == getattr(user, "pk", None):
        return True
    if plan.tasks.filter(responsible=user).exists():
        return True

    department_ids = plan_department_ids_for_user(user, school)
    return bool(department_ids) and plan.tasks.filter(
        department_id__in=department_ids
    ).exists()


def plan_items_are_mutable(plan: Plan) -> bool:
    """Whether Goal/Task structure may change under the existing contracts."""
    return plan.stage != Plan.Stage.CLOSED and plan.is_editable_by_owner


def plan_allows_task_conversion(plan: Plan) -> bool:
    """Whether execution may start from a task.

    Approval freezes the approved document but intentionally does not prevent
    starting its execution.  Closing the operational plan does.
    """
    return plan.stage != Plan.Stage.CLOSED


def _may_edit_plan(plan: Plan, user) -> bool:
    if plan.owner_id == getattr(user, "pk", None):
        return True
    from .permissions import is_school_manager

    return bool(plan.school_id) and is_school_manager(user, active_school=plan.school)


@transaction.atomic
def convert_task_to_assignment(task: PlanTask, user, *, school=None) -> Assignment:
    """تحويل مهمة خطة إلى تكليف متابَع.

    الشرطان — مسؤول وموعد — إلزاميان هنا لا عند كتابة المهمة: خطةٌ تُصاغ
    أهدافها ومهامها قبل أن يُسمّى منفّذوها حالةٌ مشروعة في التخطيط، ومنعُها
    يدفع المُعِدّ إلى إسناد وهمي ليُكمل النموذج.
    """
    try:
        current_task = (
            PlanTask.objects.select_for_update()
            .select_related("plan", "responsible", "department", "assignment")
            .get(pk=task.pk)
        )
    except PlanTask.DoesNotExist as exc:
        raise PlanError("مهمة الخطة غير موجودة.") from exc

    plan = Plan.objects.select_for_update().get(pk=current_task.plan_id)
    current_task.plan = plan

    if school is not None and (
        plan.scope != Plan.Scope.SCHOOL
        or plan.school_id != getattr(school, "pk", None)
    ):
        raise PermissionDenied("هذه المهمة خارج المدرسة النشطة.")
    if current_task.assignment_id is not None:
        raise PlanError("هذه المهمة محوَّلة إلى تكليف بالفعل.")
    if not _may_edit_plan(plan, user):
        raise PermissionDenied("تحويل مهام الخطة لمُعِدّها أو لمدير المدرسة.")
    if not plan_allows_task_conversion(plan):
        raise PlanError("الخطة مغلقة — لا يمكن إنشاء تكليفات جديدة منها.")
    if current_task.responsible_id is None:
        raise PlanError("حدّد المسؤول عن المهمة أولاً.")
    if current_task.due_at is None:
        raise PlanError("حدّد موعد التنفيذ أولاً — مهمةٌ بلا موعد لا تُتابَع.")
    if current_task.due_at <= timezone.now():
        raise PlanError("موعد التنفيذ يجب أن يكون في المستقبل.")

    from .model_parts.schools import SchoolMembership

    if plan.scope == Plan.Scope.SCHOOL and not SchoolMembership.objects.filter(
        school_id=plan.school_id,
        teacher_id=current_task.responsible_id,
        is_active=True,
        role_type__in=SchoolMembership.STAFF_ROLES,
    ).exists():
        raise PlanError("المسؤول يجب أن يكون من منسوبي المدرسة النشطين.")
    if (
        plan.scope == Plan.Scope.SCHOOL
        and current_task.department_id is not None
        and current_task.department.school_id != plan.school_id
    ):
        raise PlanError("القسم المختار لا يتبع مدرسة الخطة.")

    target_school = plan.school
    if target_school is None:
        # خطة مشتركة: المسؤول مدير مدرسة، فتُنسب حصته لمدرسته.
        membership = (
            SchoolMembership.objects.filter(
                teacher_id=current_task.responsible_id,
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
        department=current_task.department,
        issuer=user,
        source=Assignment.Source.PLAN,
        title=current_task.title[:200],
        description=current_task.description,
        due_at=current_task.due_at,
    )
    AssignmentTarget.objects.create(
        assignment=assignment,
        assignee_id=current_task.responsible_id,
        school=target_school,
    )

    current_task.assignment = assignment
    current_task.save(update_fields=["assignment"])
    # Preserve the established caller contract: the passed instance exposes
    # the created relation immediately, while authorization used the locked row.
    task.assignment = assignment

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
        .prefetch_related(
            Prefetch(
                "tasks",
                queryset=PlanTask.objects.select_related("assignment").prefetch_related(
                    "assignment__targets"
                ),
            )
        )
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
    from .permissions import is_school_manager

    base = plans_for_school(school)

    if getattr(user, "is_superuser", False):
        return base
    if is_school_manager(user, active_school=school):
        return base

    # مُعِدُّها أو منفّذُ مهمةٍ فيها يراها داخل المدرسة النشطة. أما
    # ``TRACK_PLANS`` فيضيف خطط الأقسام المشمولة فقط، ولا يحوّل النطاق الفارغ
    # أو التفويض إلى وصول مدرسي شامل.
    visible = Q(owner=user) | Q(tasks__responsible=user)
    department_ids = plan_department_ids_for_user(user, school)
    if department_ids:
        visible |= Q(tasks__department_id__in=department_ids)
    return base.filter(visible).distinct()


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
        percent = round(summary["done"] * 100 / summary["total"]) if summary["total"] else 0
        rows.append(
            {
                "plan": plan,
                "total": summary["total"],
                "done": summary["done"],
                "late": summary["late"],
                "tracked": summary["tracked"],
                "percent": percent,
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
