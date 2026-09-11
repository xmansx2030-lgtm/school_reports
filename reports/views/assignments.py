# -*- coding: utf-8 -*-
"""شاشات التكليفات.

ثلاث شاشات لا أكثر، وكلٌّ تجيب عن سؤال واحد:

- ``my_assignments`` — «ما المطلوب مني؟» للمكلَّف.
- ``assignment_board`` — «أين وصل ما كلّفتُ به؟» للمكلِّف، ومعه المتأخرات.
- ``assignment_view`` — التكليف الواحد كوثيقة: نصّه وموعده وجدول المكلَّفين،
  ومنه الطباعة والمشاركة. اللوحة تعرض الأرقام، وهذه الشاشة تعرض التكليف نفسه.
- ``assignment_detail`` — لوحة العمل الواحد: القبول والنسبة والشواهد والإرسال.

وسير الاعتماد على التكليف لا يُكتب هنا: ``AssignmentTarget`` يرث
``ApprovalMixin``، فيراجَع ويُعتمد من صندوق المراجعة نفسه الذي بُني للتقارير.
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError as DjangoValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from .. import capabilities as caps
from ..forms_assignments import (
    EvidenceUploadForm,
    ProgressUpdateForm,
    SchoolAssignmentForm,
)
from ..model_parts.approvals import ApprovalState
from ..models import Assignment, AssignmentEvidence, AssignmentTarget
from ..permissions import capability_source, is_school_manager
from ..services_approval import (
    ACTION_DISPATCH,
    ApprovalError,
    available_actions,
    transitions_for,
)
from ..services_assignments import (
    accept_target,
    add_evidence,
    assignment_board_rows,
    overdue_targets_for_school,
    remove_evidence,
    request_clarification,
    targets_for_assignee,
    update_progress,
)
from ._helpers import *  # noqa: F401,F403
from ._helpers import _get_active_school
from ._helpers import active_school_or_redirect as _school_or_redirect

__all__ = [
    "my_assignments",
    "assignment_board",
    "assignment_create",
    "assignment_view",
    "assignment_print",
    "assignment_detail",
    "assignment_target_action",
    "assignment_approval_action",
    "assignment_cancel",
]


def _may_issue(user, school) -> bool:
    """من يملك إصدار تكليف: المدير، أو من مُنح ``assign_tasks``."""
    if is_school_manager(user, active_school=school):
        return True
    return capability_source(user, caps.ASSIGN_TASKS, school) is not None


# ─────────────────────────────────────────────────────────────────────────────
# المكلَّف
# ─────────────────────────────────────────────────────────────────────────────
@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def my_assignments(request):
    """«ما المطلوب مني؟»"""
    school, redirect_response = _school_or_redirect(request)
    if redirect_response is not None:
        return redirect_response

    targets = list(targets_for_assignee(request.user, school))
    now = timezone.now()

    # الإلغاء حالة أرشيفية مستقلة: لا هو «قيد العمل» ولا «منجز». إبقاؤه في
    # المفتوح كان يجعل القائمة تناقض عدّاد الرئيسية، ويعرض للمكلَّف أزرار عمل
    # على تكليف تمنعه الخدمات من تعديله.
    cancelled_targets = [t for t in targets if t.assignment.is_cancelled]
    active_targets = [t for t in targets if not t.assignment.is_cancelled]
    open_targets = [
        t for t in active_targets if t.approval_state != ApprovalState.APPROVED
    ]
    done_targets = [
        t for t in active_targets if t.approval_state == ApprovalState.APPROVED
    ]
    overdue = [t for t in open_targets if t.is_overdue]
    # «قريب» = أقل من ثلاثة أيام. المدى القصير هو ما يستحق تنبيهاً؛ ما بعده
    # يظهر في القائمة ولا يصرخ.
    due_soon = [
        t
        for t in open_targets
        if not t.is_overdue and t.due_at and (t.due_at - now).days <= 3
    ]

    return render(
        request,
        "reports/my_assignments.html",
        {
            "active": "my_assignments",
            "active_school": school,
            "open_targets": open_targets,
            "done_targets": done_targets,
            "cancelled_targets": cancelled_targets,
            "overdue_count": len(overdue),
            "due_soon_count": len(due_soon),
            "total_count": len(targets),
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# المكلِّف
# ─────────────────────────────────────────────────────────────────────────────
@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def assignment_board(request):
    """«أين وصل ما كلّفتُ به؟» — ومعه متأخرات المدرسة."""
    school, redirect_response = _school_or_redirect(request)
    if redirect_response is not None:
        return redirect_response

    if not _may_issue(request.user, school):
        messages.error(request, "لا تملك صلاحية الوصول إلى هذه الصفحة.")
        return redirect("reports:home")

    is_manager = is_school_manager(request.user, active_school=school)
    assignments = Assignment.objects.filter(school=school).select_related(
        "issuer", "department"
    )
    if not is_manager:
        # الوكيل يتابع ما أصدره هو. متابعة ما أصدره المدير ليست من نطاقه.
        assignments = assignments.filter(issuer=request.user)

    rows = assignment_board_rows(assignments[:100])
    overdue = list(overdue_targets_for_school(school)[:50]) if is_manager else []

    return render(
        request,
        "reports/assignment_board.html",
        {
            "active": "assignment_board",
            "active_school": school,
            "rows": rows,
            "overdue": overdue,
            "is_manager": is_manager,
            "totals": {
                "assignments": len(rows),
                "overdue": sum(row["overdue"] for row in rows),
                "pending": sum(row["pending"] for row in rows),
            },
        },
    )


@login_required(login_url="reports:login")
@require_http_methods(["GET", "POST"])
def assignment_create(request):
    """إصدار تكليف جديد."""
    school, redirect_response = _school_or_redirect(request)
    if redirect_response is not None:
        return redirect_response

    if not _may_issue(request.user, school):
        messages.error(request, "لا تملك صلاحية إصدار التكليفات.")
        return redirect("reports:home")

    form = SchoolAssignmentForm(
        request.POST or None, school=school, issuer=request.user
    )

    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            # النطاق والمدرسة والمكلِّف مثبّتة على النسخة من داخل النموذج، فلا
            # تُقرأ من الطلب ولا يمكن تزويرها بحقل مخفي.
            assignment = form.save(commit=False)
            assignment.issuer = request.user
            assignment.save()

            for assignee in form.cleaned_data["assignees"]:
                AssignmentTarget.objects.create(
                    assignment=assignment,
                    assignee=assignee,
                    school=school,
                )

        messages.success(
            request,
            f"صدر التكليف إلى {len(form.cleaned_data['assignees'])} من المنسوبين.",
        )
        return redirect("reports:assignment_board")

    if request.method == "POST":
        messages.error(request, "تعذّر إصدار التكليف — تحقّق من الحقول.")

    return render(
        request,
        "reports/assignment_create.html",
        {"active": "assignment_board", "active_school": school, "form": form},
    )


# ─────────────────────────────────────────────────────────────────────────────
# التكليف الواحد: عرضاً وطباعةً ومشاركة
# ─────────────────────────────────────────────────────────────────────────────
def _assignment_for(request, pk: int, school) -> Assignment:
    """التكليف مع فحص من يحق له عرضه.

    الحقّ في العرض أوسع من الحقّ في الإجراء: المكلِّف، ومدير المدرسة التي وقع
    فيها، والمكلَّف نفسه، ومن يراجع تنفيذه. ومن لا يملك واحدة منها لا يعلم أن
    التكليف موجود أصلاً — ولذلك ``Http404`` لا رسالة منع.
    """
    assignment = get_object_or_404(
        Assignment.objects.select_related("issuer", "department", "school", "group"),
        pk=pk,
    )

    if assignment.issuer_id == request.user.pk:
        return assignment
    if assignment.targets.filter(assignee=request.user).exists():
        return assignment
    if school is not None and is_school_manager(request.user, active_school=school):
        # تكليف المجموعة لا مدرسةَ له، فيُعرف بمدرسة مكلَّفيه.
        if assignment.school_id == school.pk or assignment.targets.filter(school=school).exists():
            return assignment
    if any(
        target.can_review_approval(request.user, school)
        for target in assignment.targets.select_related("assignment", "school")
    ):
        return assignment

    raise Http404


def _assignment_context(request, assignment, school) -> dict:
    """ما تعرضه شاشتا العرض والطباعة معاً — بلا ازدواج يفترق عند أول تعديل."""
    targets = list(
        assignment.targets.select_related("assignee", "school").order_by(
            "assignee__name", "id"
        )
    )
    done = sum(1 for t in targets if t.approval_state == ApprovalState.APPROVED)
    overdue = sum(1 for t in targets if t.is_overdue)
    pending = sum(
        1
        for t in targets
        if t.approval_state
        in (ApprovalState.SUBMITTED, ApprovalState.UNDER_REVIEW, ApprovalState.RECOMMENDED)
    )

    return {
        "active_school": school,
        "assignment": assignment,
        "targets": targets,
        "totals": {
            "total": len(targets),
            "done": done,
            "pending": pending,
            "overdue": overdue,
            "percent": round(done * 100 / len(targets)) if targets else 0,
        },
    }


@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def assignment_view(request, pk: int):
    """التكليف كوثيقة واحدة: نصّه وشروطه وجدول من صدر إليهم."""
    school = _get_active_school(request)
    assignment = _assignment_for(request, pk, school)

    context = _assignment_context(request, assignment, school)
    context.update(
        {
            "active": "assignment_board",
            "can_manage": (
                assignment.issuer_id == request.user.pk
                or (school is not None and is_school_manager(request.user, active_school=school))
            ),
            "share_url": request.build_absolute_uri(
                reverse("reports:assignment_view", args=[assignment.pk])
            ),
        }
    )
    return render(request, "reports/assignment_view.html", context)


@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def assignment_print(request, pk: int):
    """نسخة A4 من التكليف — ما يُوقَّع ويُحفَظ في الملف الورقي."""
    school = _get_active_school(request)
    assignment = _assignment_for(request, pk, school)

    moe_logo_url = (getattr(settings, "MOE_LOGO_URL", "") or "").strip()
    if not moe_logo_url:
        moe_logo_url = static("img/UntiTtled-1.png")

    context = _assignment_context(request, assignment, school)
    context.update(
        {
            "now": timezone.localtime(timezone.now()),
            "MOE_LOGO_URL": moe_logo_url,
        }
    )
    return render(request, "reports/assignment_print.html", context)


@login_required(login_url="reports:login")
@require_http_methods(["POST"])
def assignment_cancel(request, pk: int):
    """إلغاء تكليف — ولا يُحذف، فما وصل المكلَّف صار واقعة."""
    school, redirect_response = _school_or_redirect(request)
    if redirect_response is not None:
        return redirect_response

    assignment = get_object_or_404(Assignment, pk=pk, school=school)
    if not (
        is_school_manager(request.user, active_school=school)
        or assignment.issuer_id == request.user.pk
    ):
        messages.error(request, "لا تملك إلغاء هذا التكليف.")
        return redirect("reports:assignment_board")

    assignment.cancel(by=request.user, reason=(request.POST.get("reason") or "").strip())
    messages.success(request, "أُلغي التكليف. يبقى ظاهراً في السجل بسبب إلغائه.")
    return redirect("reports:assignment_board")


# ─────────────────────────────────────────────────────────────────────────────
# التفصيل
# ─────────────────────────────────────────────────────────────────────────────
def _target_for(request, pk: int, school) -> AssignmentTarget:
    """المكلَّف مع فحص أن الطالب صاحبه أو من يحق له متابعته."""
    target = get_object_or_404(
        AssignmentTarget.objects.select_related(
            "assignment", "assignment__issuer", "assignee", "school"
        ),
        pk=pk,
    )
    if target.assignee_id == request.user.pk:
        return target
    if is_school_manager(request.user, active_school=school):
        if target.school_id == school.pk or target.assignment.school_id == school.pk:
            return target
    if target.assignment.issuer_id == request.user.pk:
        return target
    if target.can_review_approval(request.user, school):
        return target
    raise Http404


@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def assignment_detail(request, pk: int):
    """لوحة العمل على تكليف واحد."""
    school, redirect_response = _school_or_redirect(request)
    if redirect_response is not None:
        return redirect_response

    target = _target_for(request, pk, school)
    is_assignee = target.assignee_id == request.user.pk

    return render(
        request,
        "reports/assignment_detail.html",
        {
            "active": "my_assignments" if is_assignee else "assignment_board",
            "active_school": school,
            "target": target,
            "assignment": target.assignment,
            "is_assignee": is_assignee,
            "evidence": list(target.evidence.select_related("uploaded_by")),
            "shortfall": target.evidence_shortfall(),
            "actions": available_actions(target, request.user, school=school),
            "timeline": list(transitions_for(target)),
            "progress_form": ProgressUpdateForm(
                initial={"percent": target.progress_percent, "note": target.progress_note}
            ),
            "evidence_form": EvidenceUploadForm(),
        },
    )


@login_required(login_url="reports:login")
@require_http_methods(["POST"])
def assignment_target_action(request, pk: int):
    """إجراءات المكلَّف على تكليفه: القبول والتوضيح والنسبة والشواهد."""
    school, redirect_response = _school_or_redirect(request)
    if redirect_response is not None:
        return redirect_response

    target = _target_for(request, pk, school)
    action = (request.POST.get("target_action") or "").strip()

    try:
        if action == "accept":
            accept_target(target, request.user)
            messages.success(request, "تم قبول التكليف.")

        elif action == "clarify":
            request_clarification(target, request.user, note=request.POST.get("note", ""))
            messages.success(request, "أُرسل طلب التوضيح إلى المكلِّف.")

        elif action == "progress":
            form = ProgressUpdateForm(request.POST)
            if not form.is_valid():
                messages.error(request, "نسبة الإنجاز رقم بين 0 و100.")
            else:
                update_progress(
                    target,
                    request.user,
                    percent=form.cleaned_data["percent"],
                    note=form.cleaned_data.get("note", ""),
                )
                messages.success(request, "حُدِّثت نسبة الإنجاز.")

        elif action == "add_evidence":
            form = EvidenceUploadForm(request.POST, request.FILES)
            if not form.is_valid():
                messages.error(request, "اختر ملف الشاهد.")
            else:
                add_evidence(
                    target,
                    request.user,
                    file=form.cleaned_data["file"],
                    note=form.cleaned_data.get("note", ""),
                )
                messages.success(request, "رُفع الشاهد.")

        elif action == "remove_evidence":
            evidence = get_object_or_404(
                AssignmentEvidence, pk=request.POST.get("evidence_id"), target=target
            )
            remove_evidence(evidence, request.user)
            messages.success(request, "حُذف الشاهد.")

        else:
            messages.error(request, "إجراء غير معروف.")

    except PermissionDenied as exc:
        messages.error(request, str(exc) or "لا تملك هذا الإجراء.")
    except ApprovalError as exc:
        detail = getattr(exc, "messages", None) or [str(exc)]
        messages.error(request, detail[0])

    return redirect("reports:assignment_detail", pk=pk)


@login_required(login_url="reports:login")
@require_http_methods(["POST"])
def assignment_approval_action(request, pk: int):
    """إجراءات دورة الاعتماد على تنفيذ التكليف.

    منفصل عن ``assignment_target_action`` لأن الأول أفعال المكلَّف على عمله،
    وهذا قرارات المراجعة والاعتماد. ويستدعي ``ACTION_DISPATCH`` نفسه الذي يخدم
    التقارير، فقواعد «لا يعتمد أحد عمله» و«المعتمد نهائي» تسري هنا بلا سطر
    جديد — وهو العائد المقصود من بناء المكوّن قبل هذه المرحلة.
    """
    school, redirect_response = _school_or_redirect(request)
    if redirect_response is not None:
        return redirect_response

    target = _target_for(request, pk, school)
    action = (request.POST.get("approval_action") or "").strip()
    note = (request.POST.get("note") or "").strip()

    handler = ACTION_DISPATCH.get(action)
    if handler is None or action not in available_actions(target, request.user, school=school):
        messages.error(request, "هذا الإجراء غير متاح على هذا التكليف الآن.")
        return redirect("reports:assignment_detail", pk=pk)

    try:
        handler(target, request.user, school=school, note=note)
    except PermissionDenied as exc:
        messages.error(request, str(exc) or "لا تملك هذا الإجراء.")
    except (ApprovalError, DjangoValidationError) as exc:
        detail = getattr(exc, "messages", None) or [str(exc)]
        messages.error(request, detail[0])
    else:
        messages.success(
            request,
            {
                "submit": "أُرسل تنفيذك للمراجعة.",
                "withdraw": "سُحب التنفيذ للتعديل.",
                "start_review": "بدأت مراجعة التنفيذ.",
                "request_info": "طُلب استكمال من المكلَّف.",
                "return": "أُعيد التكليف للمكلَّف مع ملاحظتك.",
                "recommend": "رُفع التنفيذ للمدير موصىً باعتماده.",
                "approve": "اعتُمد تنفيذ التكليف.",
            }.get(action, "نُفِّذ الإجراء."),
        )

    return redirect("reports:assignment_detail", pk=pk)
