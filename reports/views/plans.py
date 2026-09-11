# -*- coding: utf-8 -*-
"""شاشات الخطط والمبادرات.

الخطة تُعدّ ثم تُعتمد ثم تُنفَّذ. والتنفيذ لا يعيش هنا: مهامها تتحوّل إلى
تكليفات فتُتابَع بالمواعيد والشواهد التي بُنيت لها — ولا تُبنى لها متابعة ثانية
موازية تتباعد عنها.

والمبادرة تُقترح من أيٍّ كان وتُعتمد من المدير، ثم يقرّر مشاركتها مع المجموعة.
والقراران منفصلان عمداً: مشاركةُ غير المعتمد نقلٌ لما لم يُتحقّق منه.
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from .. import capabilities as caps
from ..forms_plans import InitiativeForm, PlanForm, PlanGoalForm, PlanTaskForm
from ..models import Initiative, Plan, PlanGoal, PlanTask
from ..permissions import capability_source, is_school_manager
from ..services_approval import (
    ACTION_DISPATCH,
    ApprovalError,
    available_actions,
    issue,
    transitions_for,
)
from ..services_plans import (
    PlanError,
    convert_task_to_assignment,
    initiatives_visible_to,
    plan_board_rows,
    plans_visible_to,
    share_initiative,
)
from ._helpers import *  # noqa: F401,F403
from ._helpers import active_school_or_redirect as _school_or_redirect

__all__ = [
    "plan_list",
    "plan_create",
    "plan_detail",
    "plan_edit",
    "plan_delete",
    "plan_print",
    "plan_action",
    "plan_approval_action",
    # الاقتراح يقع في ``initiative_list`` نفسها: نموذج الاقتراح بجوار قائمة
    # المبادرات، فلا تُفتح صفحة لحقلين.
    "initiative_list",
    "initiative_action",
]


def _may_plan(user, school) -> bool:
    if is_school_manager(user, active_school=school):
        return True
    return capability_source(user, caps.TRACK_PLANS, school) is not None


def _may_manage(user, plan, school) -> bool:
    """من يملك الخطة: مُعِدُّها أو مدير مدرستها. وهو مقياس التعديل والحذف معاً."""
    return plan.owner_id == user.pk or is_school_manager(user, active_school=school)


def _plan_for(request, pk: int, school) -> Plan:
    """الخطة التي يحق لهذا المستخدم رؤيتها.

    مُعِدُّها، أو مدير مدرستها، أو من أُسندت إليه مهمة فيها — فمن يُنفّذ جزءاً
    من خطة يحق له أن يرى موقعه منها.
    """
    plan = get_object_or_404(
        Plan.objects.select_related("owner", "school", "group"), pk=pk
    )
    if plan.owner_id == request.user.pk:
        return plan
    if plan.school_id == getattr(school, "pk", None):
        if is_school_manager(request.user, active_school=school):
            return plan
        if plan.tasks.filter(responsible=request.user).exists():
            return plan
        if capability_source(request.user, caps.TRACK_PLANS, school) is not None:
            return plan
    raise Http404


# ─────────────────────────────────────────────────────────────────────────────
# الخطط
# ─────────────────────────────────────────────────────────────────────────────
@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def plan_list(request):
    school, redirect_response = _school_or_redirect(request)
    if redirect_response is not None:
        return redirect_response

    rows = plan_board_rows(plans_visible_to(request.user, school)[:100])
    my_tasks = list(
        PlanTask.objects.filter(plan__school=school, responsible=request.user)
        .select_related("plan", "assignment")
        .order_by("due_at", "id")[:50]
    )
    tasks_total = sum(row["total"] for row in rows)
    done = sum(row["done"] for row in rows)

    return render(
        request,
        "reports/plan_list.html",
        {
            "active": "plan_list",
            "active_school": school,
            "rows": rows,
            "my_tasks": my_tasks,
            "can_plan": _may_plan(request.user, school),
            "totals": {
                "plans": len(rows),
                "late": sum(row["late"] for row in rows),
                "tasks": sum(row["total"] for row in rows),
                "done": done,
                # نسبة المدرسة كلها = المهام المنجَزة ÷ مهام خططها. متوسّطُ نسبِ
                # الخطط يساوي بين خطة من مهمتين وأخرى من أربعين.
                "percent": round(done * 100 / tasks_total) if tasks_total else 0,
            },
        },
    )


@login_required(login_url="reports:login")
@require_http_methods(["GET", "POST"])
def plan_create(request):
    school, redirect_response = _school_or_redirect(request)
    if redirect_response is not None:
        return redirect_response

    if not _may_plan(request.user, school):
        messages.error(request, "لا تملك صلاحية إعداد الخطط.")
        return redirect("reports:home")

    form = PlanForm(request.POST or None, school=school, owner=request.user)
    if request.method == "POST" and form.is_valid():
        plan = form.save(commit=False)
        plan.owner = request.user
        plan.save()
        messages.success(request, "أُنشئت الخطة. أضف أهدافها ومهامها.")
        return redirect("reports:plan_detail", pk=plan.pk)

    if request.method == "POST":
        messages.error(request, "تعذّر إنشاء الخطة — تحقّق من الحقول.")

    return render(
        request,
        "reports/plan_create.html",
        {"active": "plan_list", "active_school": school, "form": form},
    )


@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def plan_detail(request, pk: int):
    school, redirect_response = _school_or_redirect(request)
    if redirect_response is not None:
        return redirect_response

    plan = _plan_for(request, pk, school)
    may_manage = _may_manage(request.user, plan, school)

    tasks = list(
        plan.tasks.select_related("goal", "responsible", "assignment")
        .prefetch_related("assignment__targets")
        .order_by("order", "id")
    )

    return render(
        request,
        "reports/plan_detail.html",
        {
            "active": "plan_list",
            "active_school": school,
            "plan": plan,
            # ``may_edit`` تحكم بنود الخطة: تُضاف الأهداف والمهام ما دامت الخطة
            # في مرحلة تسمح بذلك. و``may_manage`` تحكم ملكيتها: الطباعة والحذف
            # والوصول إلى شاشة التحرير.
            "may_edit": may_manage and plan.is_editable_by_owner,
            "may_manage": may_manage,
            "can_delete": may_manage and _delete_block(plan) is None,
            "delete_block": _delete_block(plan),
            "goals": list(plan.goals.all()),
            "tasks": tasks,
            "summary": plan.task_summary,
            "percent": plan.progress_percent,
            "goal_form": PlanGoalForm(),
            "task_form": PlanTaskForm(plan=plan),
            "actions": available_actions(plan, request.user, school=school),
            "timeline": list(transitions_for(plan)),
            "share_url": request.build_absolute_uri(
                reverse("reports:plan_detail", args=[plan.pk])
            ),
        },
    )


def _delete_block(plan: Plan) -> str | None:
    """ما يمنع حذف الخطة، أو ``None`` إن كانت تُحذف.

    الخطة تُحذف ما دامت مسودّةً لم يترتّب عليها شيء. فإذا اعتُمدت صارت وثيقة
    صادرة، وإذا تحوّلت مهمةٌ منها إلى تكليف صار على منسوبٍ عملٌ قائم — وحذفها
    حينها يترك تكليفاً بلا سند يُعرف منه لماذا صدر. وفي الحالتين تُغلق ولا
    تُحذف، فيبقى السجل مقروءاً.
    """
    if plan.is_final:
        return "الخطة معتمدة — وثيقةٌ صادرة تُغلق ولا تُحذف."
    if plan.tasks.filter(assignment__isnull=False).exists():
        return "تحوّلت مهام من هذه الخطة إلى تكليفات قائمة — أغلقها بدل حذفها."
    return None


@login_required(login_url="reports:login")
@require_http_methods(["GET", "POST"])
def plan_edit(request, pk: int):
    """تعديل بيانات الخطة — عنوانها ووصفها ومداها الزمني.

    وأهدافها ومهامها تُعدَّل من صفحتها نفسها، فالتحرير هنا للوثيقة لا لبنودها.
    """
    school, redirect_response = _school_or_redirect(request)
    if redirect_response is not None:
        return redirect_response

    plan = _plan_for(request, pk, school)
    if not _may_manage(request.user, plan, school):
        messages.error(request, "تعديل الخطة لمُعِدّها أو لمدير المدرسة.")
        return redirect("reports:plan_detail", pk=pk)
    if not plan.is_editable_by_owner:
        messages.error(
            request,
            "الخطة في مرحلة لا تُعدَّل فيها — اسحبها للتعديل أولاً إن كانت تنتظر قراراً.",
        )
        return redirect("reports:plan_detail", pk=pk)

    form = PlanForm(
        request.POST or None,
        instance=plan,
        school=plan.school,
        group=plan.group,
        owner=plan.owner,
    )
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "حُفظت تعديلات الخطة.")
        return redirect("reports:plan_detail", pk=plan.pk)

    if request.method == "POST":
        messages.error(request, "تعذّر حفظ التعديلات — تحقّق من الحقول.")

    return render(
        request,
        "reports/plan_edit.html",
        {"active": "plan_list", "active_school": school, "plan": plan, "form": form},
    )


@login_required(login_url="reports:login")
@require_http_methods(["POST"])
def plan_delete(request, pk: int):
    """حذف خطة لم يترتّب عليها شيء بعد."""
    school, redirect_response = _school_or_redirect(request)
    if redirect_response is not None:
        return redirect_response

    plan = _plan_for(request, pk, school)
    if not _may_manage(request.user, plan, school):
        messages.error(request, "حذف الخطة لمُعِدّها أو لمدير المدرسة.")
        return redirect("reports:plan_detail", pk=pk)

    block = _delete_block(plan)
    if block is not None:
        messages.error(request, block)
        return redirect("reports:plan_detail", pk=pk)

    title = plan.title
    plan.delete()
    messages.success(request, f"حُذفت خطة «{title}».")
    return redirect("reports:plan_list")


@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def plan_print(request, pk: int):
    """نسخة A4 رسمية من الخطة: أهدافها بمؤشراتها، ومهامها بمسؤوليها ومواعيدها."""
    school, redirect_response = _school_or_redirect(request)
    if redirect_response is not None:
        return redirect_response

    plan = _plan_for(request, pk, school)

    tasks = list(
        plan.tasks.select_related("goal", "responsible", "department", "assignment")
        .prefetch_related("assignment__targets")
        .order_by("order", "id")
    )
    goals = list(plan.goals.all())
    # المهام تحت أهدافها كما تُقرأ الخطة على الورق — ومهامٌ بلا هدف تُجمع أخيراً
    # ولا تُخفى، فإخفاؤها يجعل مجموع الجدول أقل من مجموع الخطة.
    grouped = [{"goal": goal, "tasks": [t for t in tasks if t.goal_id == goal.pk]} for goal in goals]
    loose = [task for task in tasks if task.goal_id is None]

    moe_logo_url = (getattr(settings, "MOE_LOGO_URL", "") or "").strip()
    if not moe_logo_url:
        moe_logo_url = static("img/UntiTtled-1.png")

    return render(
        request,
        "reports/plan_print.html",
        {
            "active_school": school,
            "plan": plan,
            "goals": goals,
            "tasks": tasks,
            "grouped": grouped,
            "loose": loose,
            "summary": plan.task_summary,
            "percent": plan.progress_percent,
            "now": timezone.localtime(timezone.now()),
            "MOE_LOGO_URL": moe_logo_url,
        },
    )


@login_required(login_url="reports:login")
@require_http_methods(["POST"])
def plan_action(request, pk: int):
    """أهداف الخطة ومهامها وتحويلها إلى تكليفات."""
    school, redirect_response = _school_or_redirect(request)
    if redirect_response is not None:
        return redirect_response

    plan = _plan_for(request, pk, school)
    action = (request.POST.get("plan_action") or "").strip()

    may_edit = plan.owner_id == request.user.pk or is_school_manager(
        request.user, active_school=school
    )

    try:
        if action in {"add_goal", "add_task", "remove_goal", "remove_task"} and not may_edit:
            raise PermissionDenied("تعديل الخطة لمُعِدّها أو لمدير المدرسة.")
        if action in {"add_goal", "add_task"} and not plan.is_editable_by_owner:
            raise PlanError("الخطة ليست في حالة تسمح بتعديلها.")

        if action == "add_goal":
            form = PlanGoalForm(request.POST)
            if not form.is_valid():
                messages.error(request, "اكتب عنوان الهدف.")
            else:
                goal = form.save(commit=False)
                goal.plan = plan
                goal.order = (plan.goals.aggregate(top=Max("order"))["top"] or 0) + 1
                goal.save()
                messages.success(request, "أُضيف الهدف.")

        elif action == "remove_goal":
            get_object_or_404(PlanGoal, pk=request.POST.get("goal_id"), plan=plan).delete()
            messages.success(request, "حُذف الهدف.")

        elif action == "add_task":
            form = PlanTaskForm(request.POST, plan=plan)
            if not form.is_valid():
                messages.error(request, "تعذّر إضافة المهمة — تحقّق من الحقول.")
            else:
                task = form.save(commit=False)
                task.plan = plan
                task.order = (plan.tasks.aggregate(top=Max("order"))["top"] or 0) + 1
                task.save()
                messages.success(request, "أُضيفت المهمة.")

        elif action == "remove_task":
            task = get_object_or_404(PlanTask, pk=request.POST.get("task_id"), plan=plan)
            if task.is_tracked:
                raise PlanError("لا تُحذف مهمة صار لها تكليف — ألغِ تكليفها أولاً.")
            task.delete()
            messages.success(request, "حُذفت المهمة.")

        elif action == "track_task":
            task = get_object_or_404(PlanTask, pk=request.POST.get("task_id"), plan=plan)
            convert_task_to_assignment(task, request.user)
            messages.success(request, "حُوِّلت المهمة إلى تكليف — تُتابَع الآن بموعدها.")

        elif action == "close":
            if not may_edit:
                raise PermissionDenied("إغلاق الخطة لمُعِدّها أو لمدير المدرسة.")
            plan.stage = Plan.Stage.CLOSED
            plan.save(update_fields=["stage"])
            messages.success(request, "أُغلقت الخطة.")

        else:
            messages.error(request, "إجراء غير معروف.")

    except PermissionDenied as exc:
        messages.error(request, str(exc) or "لا تملك هذا الإجراء.")
    except (PlanError, ApprovalError, ValidationError) as exc:
        detail = getattr(exc, "messages", None) or [str(exc)]
        messages.error(request, detail[0])

    return redirect("reports:plan_detail", pk=pk)


@login_required(login_url="reports:login")
@require_http_methods(["POST"])
def plan_approval_action(request, pk: int):
    """دورة اعتماد الخطة — بالمكوّن المشترك."""
    school, redirect_response = _school_or_redirect(request)
    if redirect_response is not None:
        return redirect_response

    plan = _plan_for(request, pk, school)
    action = (request.POST.get("approval_action") or "").strip()
    note = (request.POST.get("note") or "").strip()

    handler = ACTION_DISPATCH.get(action)
    if handler is None or action not in available_actions(plan, request.user, school=school):
        messages.error(request, "هذا الإجراء غير متاح على الخطة الآن.")
        return redirect("reports:plan_detail", pk=pk)

    try:
        handler(plan, request.user, school=school, note=note)
    except PermissionDenied as exc:
        messages.error(request, str(exc) or "لا تملك هذا الإجراء.")
    except (ApprovalError, ValidationError) as exc:
        detail = getattr(exc, "messages", None) or [str(exc)]
        messages.error(request, detail[0])
    else:
        messages.success(
            request,
            {
                "issue": "صدرت الخطة واعتُمدت.",
                "submit": "أُرسلت الخطة للاعتماد.",
                "withdraw": "سُحبت الخطة للتعديل.",
                "return": "أُعيدت الخطة لمُعِدّها مع ملاحظتك.",
                "approve": "اعتُمدت الخطة.",
            }.get(action, "نُفِّذ الإجراء."),
        )

    return redirect("reports:plan_detail", pk=pk)


# ─────────────────────────────────────────────────────────────────────────────
# المبادرات
# ─────────────────────────────────────────────────────────────────────────────
@login_required(login_url="reports:login")
@require_http_methods(["GET", "POST"])
def initiative_list(request):
    """المبادرات: اقتراح جديد + متابعة ما اقتُرح."""
    school, redirect_response = _school_or_redirect(request)
    if redirect_response is not None:
        return redirect_response

    is_manager = is_school_manager(request.user, active_school=school)
    form = InitiativeForm(school=school)

    if request.method == "POST":
        form = InitiativeForm(request.POST, school=school)
        if form.is_valid():
            try:
                with transaction.atomic():
                    initiative = form.save(commit=False)
                    initiative.school = school
                    initiative.teacher = request.user
                    initiative.save()
                    if is_manager:
                        issue(initiative, request.user, school=school)
            except (ApprovalError, ValidationError) as exc:
                detail = getattr(exc, "messages", None) or [str(exc)]
                form.add_error("summary", detail[0])
                messages.error(request, "لا يمكن إصدار المبادرة قبل اكتمال فكرتها وأثرها.")
            else:
                if is_manager:
                    messages.success(request, "صدرت المبادرة واعتُمدت، وأصبحت ظاهرة لمنسوبي المدرسة.")
                else:
                    messages.success(request, "سُجِّلت المبادرة كمسودة. أرسلها للاعتماد حين تكتمل.")
                return redirect("reports:initiative_list")
        else:
            messages.error(request, "تعذّر تسجيل المبادرة — تحقّق من الحقول.")

    visible = list(initiatives_visible_to(request.user, school)[:100])

    rows = []
    for item in visible:
        rows.append(
            {
                "initiative": item,
                "actions": available_actions(item, request.user, school=school),
                "can_share": is_manager and item.can_share(),
                "mine": item.teacher_id == request.user.pk,
            }
        )

    return render(
        request,
        "reports/initiative_list.html",
        {
            "active": "initiative_list",
            "active_school": school,
            "form": form,
            "rows": rows,
            "is_manager": is_manager,
        },
    )


@login_required(login_url="reports:login")
@require_http_methods(["POST"])
def initiative_action(request, pk: int):
    """إجراءات المبادرة: دورة الاعتماد + المشاركة مع المجموعة."""
    school, redirect_response = _school_or_redirect(request)
    if redirect_response is not None:
        return redirect_response

    initiative = get_object_or_404(Initiative, pk=pk, school=school)
    action = (request.POST.get("initiative_action") or "").strip()
    note = (request.POST.get("note") or "").strip()

    try:
        if action == "share":
            share_initiative(initiative, request.user)
            messages.success(request, "شُوركت الممارسة مع مدارس المجموعة.")
        else:
            handler = ACTION_DISPATCH.get(action)
            if handler is None or action not in available_actions(
                initiative, request.user, school=school
            ):
                messages.error(request, "هذا الإجراء غير متاح على المبادرة الآن.")
            else:
                handler(initiative, request.user, school=school, note=note)
                messages.success(
                    request,
                    {
                        "issue": "صدرت المبادرة واعتُمدت.",
                        "submit": "أُرسلت المبادرة للاعتماد.",
                        "withdraw": "سُحبت المبادرة للتعديل.",
                        "return": "أُعيدت المبادرة لمقدّمها مع ملاحظتك.",
                        "approve": "اعتُمدت المبادرة.",
                    }.get(action, "نُفِّذ الإجراء."),
                )

    except PermissionDenied as exc:
        messages.error(request, str(exc) or "لا تملك هذا الإجراء.")
    except (PlanError, ApprovalError, ValidationError) as exc:
        detail = getattr(exc, "messages", None) or [str(exc)]
        messages.error(request, detail[0])

    return redirect("reports:initiative_list")
