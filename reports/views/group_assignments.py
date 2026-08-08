# -*- coding: utf-8 -*-
"""تكليفات المدير التنفيذي على مدارس مجموعته.

هذا الملف يستكمل القناة التي كانت مفقودة تماماً: قبله كان المدير التنفيذي يقرأ
إحصاءات محسوبة ولا يملك أي سبيل لطلب عمل من مدارسه ولا لتلقّي ردّها. وأقصى ما
يستطيعه كان تعميماً إعلامياً بلا مسؤول ولا موعد ولا شاهد ولا اعتماد.

**لا يمرّ من هنا سياق مدرسة.** على نهج ``school_groups.py``: هذه شاشات تقرأ
وتكتب عبر مدارس المجموعة مجتمعةً، فلا تلمس ``active_school_id`` في الجلسة ولا
تُعيد ضبط سياق مستخدم يتنقّل بين اللوحة ومدرسة بعينها.

**وحدود الدور محفوظة.** المدير التنفيذي يطلب ويتابع ويعتمد ما طلبه هو. لا يعدّل
بيانات مدرسة، ولا يعتمد عملاً داخلياً لم يطلبه، ولا يملك عضوية في أي منها —
واعتمادُه لردود مدارسه يأتي من كونه المكلِّف لا من صلاحية مدرسية.
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from ..forms_assignments import GroupAssignmentForm
from ..model_parts.approvals import ApprovalState, PENDING_REVIEW_STATES
from ..models import Assignment, AssignmentTarget
from ..permissions import (
    executive_director_groups,
    executive_director_schools_qs,
    is_executive_director,
)
from ..services_approval import (
    ACTION_DISPATCH,
    ApprovalError,
    available_actions,
    transitions_for,
)

__all__ = [
    "group_assignment_board",
    "group_assignment_create",
    "group_assignment_detail",
    "group_assignment_action",
    "group_assignment_cancel",
    "group_report",
    "group_report_xlsx",
    "group_report_pdf",
    "group_practices",
]


def _director_groups(request):
    """مجموعات المستخدم، أو 404 إن لم يكن مديراً تنفيذياً."""
    if not is_executive_director(request.user):
        raise Http404
    groups = list(executive_director_groups(request.user))
    if not groups:
        raise Http404
    return groups


def _selected_group(request, groups):
    """المجموعة المعروضة — مقيَّدة بمجموعات المستخدم.

    معرّفٌ من خارجها لا يوسّع الوصول بل يعود إلى الأولى، كما في لوحة المجموعة.
    """
    requested = (request.GET.get("group") or request.POST.get("group") or "").strip()
    if requested:
        return next((item for item in groups if str(item.pk) == requested), groups[0])
    return groups[0]


@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def group_assignment_board(request):
    """متابعة تنفيذ تكليفات المجموعة، مدرسةً مدرسة."""
    groups = _director_groups(request)
    group = _selected_group(request, groups)

    assignments = list(
        Assignment.objects.filter(group=group, scope=Assignment.Scope.GROUP)
        .select_related("issuer")
        .order_by("-created_at")[:100]
    )

    rows = []
    if assignments:
        ids = [item.pk for item in assignments]
        now = timezone.now()

        stats = {
            row["assignment_id"]: row
            for row in AssignmentTarget.objects.filter(assignment_id__in=ids)
            .values("assignment_id")
            .annotate(
                total=Count("id"),
                done=Count("id", filter=Q(approval_state=ApprovalState.APPROVED)),
                pending=Count("id", filter=Q(approval_state__in=PENDING_REVIEW_STATES)),
            )
        }
        late = {
            row["assignment_id"]: row["late"]
            for row in AssignmentTarget.objects.filter(
                assignment_id__in=ids,
                assignment__due_at__lt=now,
                assignment__cancelled_at__isnull=True,
            )
            .exclude(approval_state=ApprovalState.APPROVED)
            .values("assignment_id")
            .annotate(late=Count("id"))
        }
        for assignment in assignments:
            stat = stats.get(assignment.pk, {}) or {}
            total = int(stat.get("total") or 0)
            done = int(stat.get("done") or 0)
            rows.append(
                {
                    "assignment": assignment,
                    "total": total,
                    "done": done,
                    "pending": int(stat.get("pending") or 0),
                    "overdue": int(late.get(assignment.pk) or 0),
                    "percent": round(done * 100 / total) if total else 0,
                }
            )

    # ما ينتظر قرار المدير التنفيذي — وهو أول ما يفتح عليه الشاشة.
    awaiting = list(
        AssignmentTarget.objects.filter(
            assignment__group=group,
            approval_state__in=PENDING_REVIEW_STATES,
        )
        .select_related("assignment", "assignee", "school")
        .order_by("assignment__due_at", "id")[:50]
    )

    return render(
        request,
        "reports/group_assignment_board.html",
        {
            "active": "group_assignment_board",
            "group": group,
            "groups": groups,
            "rows": rows,
            "awaiting": awaiting,
            "totals": {
                "assignments": len(rows),
                "overdue": sum(row["overdue"] for row in rows),
                "awaiting": len(awaiting),
            },
        },
    )


@login_required(login_url="reports:login")
@require_http_methods(["GET", "POST"])
def group_assignment_create(request):
    """إصدار تكليف على مدارس المجموعة."""
    groups = _director_groups(request)
    group = _selected_group(request, groups)

    allowed = executive_director_schools_qs(request.user).filter(group=group).order_by("name")
    form = GroupAssignmentForm(
        request.POST or None,
        group=group,
        issuer=request.user,
        allowed_schools=allowed,
    )

    if request.method == "POST" and form.is_valid():
        recipients, unreachable = form.resolve_recipients()

        if not recipients:
            messages.error(
                request,
                "لا توجد مدرسة مستقبِلة: المدارس المختارة بلا مدير نشط. "
                "عيّن مديراً لكل مدرسة ثم أعد الإصدار.",
            )
        else:
            with transaction.atomic():
                assignment = form.save(commit=False)
                assignment.issuer = request.user
                assignment.save()

                for manager, school in recipients:
                    AssignmentTarget.objects.create(
                        assignment=assignment,
                        assignee=manager,
                        school=school,
                    )

            messages.success(
                request, f"صدر التكليف إلى {len(recipients)} من مديري المدارس."
            )
            if unreachable:
                # الصمت عن مدرسة لم يصلها التكليف أسوأ من رفض الإصدار كله.
                names = "، ".join(school.name for school in unreachable)
                messages.warning(
                    request,
                    f"لم يصل التكليف إلى: {names} — لا يوجد مدير نشط لها.",
                )
            return redirect("reports:group_assignment_board")

    elif request.method == "POST":
        messages.error(request, "تعذّر إصدار التكليف — تحقّق من الحقول.")

    return render(
        request,
        "reports/group_assignment_create.html",
        {
            "active": "group_assignment_board",
            "group": group,
            "groups": groups,
            "form": form,
            "school_count": allowed.count(),
        },
    )


def _target_in_my_group(request, pk: int, groups) -> AssignmentTarget:
    """المكلَّف ضمن مجموعات هذا المدير التنفيذي — أو 404.

    الفحص على المجموعة لا على المكلِّف: تكليفٌ أصدره مدير تنفيذي سابق على
    المجموعة نفسها يبقى قابلاً للمتابعة من خلفه، وإلا تجمّد عمل المجموعة عند
    كل تغيير قيادي.
    """
    group_ids = [item.pk for item in groups]
    target = get_object_or_404(
        AssignmentTarget.objects.select_related(
            "assignment", "assignment__issuer", "assignee", "school"
        ),
        pk=pk,
        assignment__group_id__in=group_ids,
    )
    return target


@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def group_assignment_detail(request, pk: int):
    """ردّ مدرسة واحدة على تكليف المجموعة."""
    groups = _director_groups(request)
    target = _target_in_my_group(request, pk, groups)

    return render(
        request,
        "reports/group_assignment_detail.html",
        {
            "active": "group_assignment_board",
            "groups": groups,
            "group": target.assignment.group,
            "target": target,
            "assignment": target.assignment,
            "evidence": list(target.evidence.select_related("uploaded_by")),
            "actions": available_actions(target, request.user, school=target.school),
            "timeline": list(transitions_for(target)),
        },
    )


@login_required(login_url="reports:login")
@require_http_methods(["POST"])
def group_assignment_action(request, pk: int):
    """قرار المدير التنفيذي على ردّ مدرسة.

    يمرّ بـ ``ACTION_DISPATCH`` نفسه الذي يخدم التقارير وتكليفات المدرسة، فلا
    قاعدة اعتماد تُكتب هنا مرة ثانية.
    """
    groups = _director_groups(request)
    target = _target_in_my_group(request, pk, groups)

    action = (request.POST.get("approval_action") or "").strip()
    note = (request.POST.get("note") or "").strip()

    handler = ACTION_DISPATCH.get(action)
    if handler is None or action not in available_actions(
        target, request.user, school=target.school
    ):
        messages.error(request, "هذا الإجراء غير متاح على هذا الردّ الآن.")
        return redirect("reports:group_assignment_detail", pk=pk)

    try:
        handler(target, request.user, school=target.school, note=note)
    except PermissionDenied as exc:
        messages.error(request, str(exc) or "لا تملك هذا الإجراء.")
    except (ApprovalError, ValidationError) as exc:
        detail = getattr(exc, "messages", None) or [str(exc)]
        messages.error(request, detail[0])
    else:
        messages.success(
            request,
            {
                "start_review": "بدأت مراجعة ردّ المدرسة.",
                "request_info": "طُلب استكمال من مدير المدرسة.",
                "return": "أُعيد التكليف للمدرسة مع ملاحظتك.",
                "approve": "اعتُمد ردّ المدرسة.",
            }.get(action, "نُفِّذ الإجراء."),
        )

    return redirect("reports:group_assignment_detail", pk=pk)


@login_required(login_url="reports:login")
@require_http_methods(["POST"])
def group_assignment_cancel(request, pk: int):
    """إلغاء تكليف مجموعة — ولا يُحذف."""
    groups = _director_groups(request)
    group_ids = [item.pk for item in groups]
    assignment = get_object_or_404(Assignment, pk=pk, group_id__in=group_ids)

    assignment.cancel(by=request.user, reason=(request.POST.get("reason") or "").strip())
    messages.success(request, "أُلغي التكليف. يبقى ظاهراً في السجل بسبب إلغائه.")
    return redirect("reports:group_assignment_board")


# ─────────────────────────────────────────────────────────────────────────────
# التقرير التنفيذي المجمَّع
# ─────────────────────────────────────────────────────────────────────────────
@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def group_report(request):
    """معاينة التقرير المجمَّع قبل استخراجه.

    المعاينة قبل التنزيل مقصودة: ملفٌ يُنزَّل مباشرةً لا يُعرف محتواه إلا بعد
    فتحه، والمدير التنفيذي يحتاج أن يرى الأرقام ليقرّر أيصدّرها أصلاً.
    """
    from ..services_group_export import build_group_snapshot

    groups = _director_groups(request)
    group = _selected_group(request, groups)

    return render(
        request,
        "reports/group_report.html",
        {
            "active": "group_report",
            "group": group,
            "groups": groups,
            "snapshot": build_group_snapshot(group),
        },
    )


@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def group_report_xlsx(request):
    """تنزيل التقرير المجمَّع بصيغة Excel."""
    from django.http import HttpResponse

    from ..services_group_export import (
        build_group_snapshot,
        build_group_workbook_bytes,
        group_export_filename,
    )

    groups = _director_groups(request)
    group = _selected_group(request, groups)

    payload = build_group_workbook_bytes(build_group_snapshot(group))
    response = HttpResponse(
        payload,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    filename = group_export_filename(group, extension="xlsx")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def group_report_pdf(request):
    """التقرير المجمَّع بصيغة PDF.

    يُصاغ من اللقطة نفسها التي يقرأها Excel، فلا يفترق رقمٌ بين ملفين
    مستخرجين في الدقيقة نفسها.
    """
    from django.http import HttpResponse
    from django.template.loader import render_to_string

    from ..pdf_offload import render_pdf_offloaded
    from ..services_group_export import build_group_snapshot, group_export_filename
    from ..tasks import render_group_report_pdf_task

    groups = _director_groups(request)
    group = _selected_group(request, groups)
    try:
        from ..pdf_report import _generate_report_pdf_weasy

        base_url = request.build_absolute_uri("/")

        def _render_locally():
            snapshot = build_group_snapshot(group)
            html = render_to_string(
                "reports/pdf/group_report_pdf.html",
                {"snapshot": snapshot, "group": group},
                request=request,
            )
            return _generate_report_pdf_weasy(html=html, base_url=base_url)

        payload = render_pdf_offloaded(
            task=render_group_report_pdf_task,
            task_args=[group.pk, base_url],
            render_locally=_render_locally,
            label=f"group-report:{group.pk}",
        )
    except Exception:
        # تعذّر توليد PDF لا يجوز أن يترك المستخدم بصفحة خطأ: نعيده إلى
        # المعاينة مع بيان أن Excel متاح — فالحاجة قائمة والبديل جاهز.
        messages.error(
            request,
            "تعذّر توليد ملف PDF في هذه البيئة. يمكنك استخراج التقرير بصيغة Excel.",
        )
        return redirect("reports:group_report")

    response = HttpResponse(payload, content_type="application/pdf")
    filename = group_export_filename(group, extension="pdf")
    response["Content-Disposition"] = f'inline; filename="{filename}"'
    return response


@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def group_practices(request):
    """الممارسات الناجحة المشتركة بين مدارس المجموعة.

    القناة التي يطلبها التوصيف باسم «مشاركة الممارسات الناجحة بين المدارس».
    وهي **قراءة فقط**: المدير التنفيذي يطّلع ويحيل، ولا يعتمد مبادرةً داخلية
    ولا يشاركها — فاعتمادها ونشرها قرارُ مدرستها.
    """
    from ..services_plans import shared_practices_for_group

    groups = _director_groups(request)
    group = _selected_group(request, groups)
    practices = list(shared_practices_for_group(group)[:100])

    by_school = {}
    for item in practices:
        by_school.setdefault(item.school_id, []).append(item)

    return render(
        request,
        "reports/group_practices.html",
        {
            "active": "group_practices",
            "group": group,
            "groups": groups,
            "practices": practices,
            "school_count": len(by_school),
        },
    )
