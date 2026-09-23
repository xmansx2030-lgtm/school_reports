# -*- coding: utf-8 -*-
"""شاشات المختبر: العهدة وحركتها، والتجارب ودورة اعتمادها.

**من يسجّل ومن يتابع مفصولان.** ``can_record_lab`` للمحضّر ومدير المدرسة —
هؤلاء يكتبون. و``can_view_lab`` تضيف إليهم مَن مُنح ``manage_lab`` — وهو يقرأ
ويراجع ولا يكتب. وخلطُهما كان سيجعل المتابِعَ يسجّل جرداً ثم يراجع جردَ نفسه.

وكل انتقال في اعتماد التجربة يمرّ من ``services_approval``، وكل كتابة في العهدة
من ``services_lab`` — فلا قاعدة عملٍ واحدة في هذا الملف.
"""
from __future__ import annotations

from django.core.exceptions import PermissionDenied

from ._helpers import *  # noqa: F401,F403
from ._helpers import _get_active_school, _clean_query_params
from ..forms_lab import LabAssetForm, LabExperimentForm, LabHandoverForm
from ..model_parts.approvals import ApprovalState
from ..models import LabAsset, LabAssetHandover, LabExperiment
from ..permissions import can_record_lab, can_view_lab, is_lab_technician
from ..services_approval import (
    ACTION_DISPATCH,
    ApprovalError,
    available_actions,
    transitions_for,
)
from ..services_lab import (
    assets_for_school,
    experiments_for_school,
    handovers_for_school,
    lab_summary,
    outstanding_handovers,
    record_handover,
    set_asset_condition,
)

__all__ = [
    "lab_dashboard",
    "lab_assets",
    "lab_asset_detail",
    "lab_asset_action",
    "lab_assets_print",
    "lab_experiments",
    "lab_experiment_detail",
    "lab_experiment_action",
    "lab_experiment_print",
]

PAGE_SIZE = 25

_ACTION_MESSAGES = {
    "submit": "أُرسلت التجربة للاعتماد.",
    "withdraw": "سُحبت التجربة للتعديل.",
    "start_review": "بدأت مراجعة التجربة.",
    "request_info": "طُلب استكمال بيانات التجربة.",
    "return": "أُعيدت التجربة لمحضّرها مع ملاحظتك.",
    "recommend": "رُفعت التجربة موصىً باعتمادها.",
    "approve": "اعتُمدت التجربة.",
}


# ─────────────────────────────────────────────────────────────────────────────
# بوابات مشتركة
# ─────────────────────────────────────────────────────────────────────────────
def _lab_context(request):
    """المدرسة النشطة مع حقّ الوصول — أو وجهةُ تحويل.

    تُرجع ``(school, can_record, None)`` عند السماح، و``(None, False, response)``
    عند المنع. وجمعُهما في دالة واحدة مقصود: ثمانِ شاشات تسأل السؤالين نفسيهما،
    وتكرارُهما فيها يجعل إضافة شاشة تاسعة تنساهما.
    """
    school = _get_active_school(request)
    if school is None:
        messages.error(request, "فضلاً اختر مدرسة أولاً.")
        return None, False, redirect("reports:select_school")

    if not can_view_lab(request.user, school):
        messages.error(request, "لا تملك صلاحية الوصول إلى شاشات المختبر.")
        return None, False, redirect("reports:home")

    return school, can_record_lab(request.user, school), None


def _asset_for(request, pk: int, school) -> LabAsset:
    """الصنف في مدرسة المستخدم — وما خرج عنها يُعامَل كغير موجود.

    ``404`` لا ``403``: تمييز «ممنوع» عن «غير موجود» يكشف وجود عهدةٍ في مدرسة
    لا يحق للمستخدم معرفة أنها موجودة.
    """
    return get_object_or_404(
        assets_for_school(
            school, user=request.user, include_inactive=True
        ).select_related("school", "department", "custodian"),
        pk=pk,
    )


def _experiment_for(request, pk: int, school) -> LabExperiment:
    return get_object_or_404(
        experiments_for_school(school, user=request.user).select_related(
            "school", "department", "recorder", "requested_by", "report"
        ),
        pk=pk,
    )


def _submission_missing(experiment: LabExperiment) -> list[dict[str, str]]:
    """بيانات الإرسال الناقصة بصياغة تصلح لقائمة تحقق في الشاشة."""
    missing = []
    if not (experiment.title or "").strip():
        missing.append({"field": "title", "label": "عنوان التجربة"})
    if experiment.experiment_date is None:
        missing.append({"field": "experiment_date", "label": "تاريخ التنفيذ"})
    if not (experiment.procedure or "").strip():
        missing.append({"field": "procedure", "label": "خطوات التنفيذ"})
    return missing


# ─────────────────────────────────────────────────────────────────────────────
# لوحة المختبر
# ─────────────────────────────────────────────────────────────────────────────
@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def lab_dashboard(request: HttpRequest) -> HttpResponse:
    """هُبوط المحضّر على مختبره: العهدة والتجارب وما يحتاج انتباهاً."""
    school, can_record, redirect_response = _lab_context(request)
    if redirect_response is not None:
        return redirect_response

    outstanding = outstanding_handovers(school, user=request.user)
    summary = lab_summary(
        school, user=request.user, outstanding_rows=outstanding
    )
    attention_assets = list(
        assets_for_school(school, user=request.user).filter(
            condition__in=LabAsset.ATTENTION_CONDITIONS
        )[:6]
    )
    recent_experiments = list(experiments_for_school(school, user=request.user)[:5])
    my_pending = (
        experiments_for_school(school, user=request.user)
        .filter(recorder=request.user, approval_state=ApprovalState.DRAFT)
        .count()
    )

    return render(
        request,
        "reports/lab_dashboard.html",
        {
            "active": "lab_dashboard",
            "active_school": school,
            "summary": summary,
            "attention_assets": attention_assets,
            "recent_experiments": recent_experiments,
            "outstanding": outstanding[:8],
            "recent_handovers": list(
                handovers_for_school(school, user=request.user, limit=6)
            ),
            "my_draft_count": my_pending,
            "can_record": can_record,
            "is_lab_tech": is_lab_technician(request.user, school),
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# العهدة
# ─────────────────────────────────────────────────────────────────────────────
@login_required(login_url="reports:login")
@require_http_methods(["GET", "POST"])
def lab_assets(request: HttpRequest) -> HttpResponse:
    """جرد المختبر — الكشف والإضافة في شاشة واحدة."""
    school, can_record, redirect_response = _lab_context(request)
    if redirect_response is not None:
        return redirect_response

    form = LabAssetForm(school=school, user=request.user)
    if request.method == "POST":
        if not can_record:
            messages.error(request, "متابعةُ المختبر لا تُجيز التسجيل فيه.")
            return redirect("reports:lab_assets")

        form = LabAssetForm(request.POST, school=school, user=request.user)
        if form.is_valid():
            asset = form.save(commit=False)
            asset.school = school
            asset.recorded_by = request.user
            if asset.custodian_id is None and is_lab_technician(request.user, school):
                # المحضّر هو صاحب العهدة افتراضاً، فلا يُطلب منه تسمية نفسه في
                # كل صنف — وعهدةٌ بلا مسؤول تُسأل عنها المدرسة كلها.
                asset.custodian = request.user
            asset.save()
            messages.success(request, "أُضيف الصنف إلى جرد المختبر.")
            return redirect("reports:lab_asset_detail", pk=asset.pk)
        messages.error(request, "تعذّر حفظ الصنف — تحقّق من الحقول.")

    # النطاق قبل المرشّح: يُبنى الاستعلام الآمن أولاً ثم تُركَّب عليه المرشّحات.
    rows = assets_for_school(school, user=request.user)

    term = (request.GET.get("q") or "").strip()
    category = (request.GET.get("category") or "").strip()
    condition = (request.GET.get("condition") or "").strip()

    if term:
        rows = rows.filter(
            Q(name__icontains=term)
            | Q(code__icontains=term)
            | Q(location__icontains=term)
        )
    if category in {value for value, _label in LabAsset.Category.choices}:
        rows = rows.filter(category=category)
    else:
        category = ""
    if condition in {value for value, _label in LabAsset.Condition.choices}:
        rows = rows.filter(condition=condition)
    else:
        condition = ""

    page = Paginator(rows, PAGE_SIZE).get_page(request.GET.get("page") or 1)
    outstanding = outstanding_handovers(school, user=request.user)
    holders_by_asset: dict[int, list[dict]] = {}
    for row in outstanding:
        holders_by_asset.setdefault(row["asset_id"], []).append(row)
    for asset in page.object_list:
        asset.current_holders = holders_by_asset.get(asset.pk, [])

    summary = lab_summary(
        school, user=request.user, outstanding_rows=outstanding
    )

    return render(
        request,
        "reports/lab_assets.html",
        {
            "active": "lab_assets",
            "active_school": school,
            "form": form,
            "page_obj": page,
            "summary": summary,
            "categories": LabAsset.Category.choices,
            "conditions": LabAsset.Condition.choices,
            "q_term": term,
            "q_category": category,
            "q_condition": condition,
            "has_filters": bool(term or category or condition),
            "qs": _clean_query_params(request.GET),
            "can_record": can_record,
        },
    )


@login_required(login_url="reports:login")
@require_http_methods(["GET", "POST"])
def lab_asset_detail(request: HttpRequest, pk: int) -> HttpResponse:
    """صنفٌ بعينه: تعديل بياناته وسجلّ حركته."""
    school, can_record, redirect_response = _lab_context(request)
    if redirect_response is not None:
        return redirect_response

    asset = _asset_for(request, pk, school)
    form = LabAssetForm(instance=asset, school=school, user=request.user)
    handover_form = LabHandoverForm(school=school)

    if request.method == "POST":
        if not can_record:
            messages.error(request, "متابعةُ المختبر لا تُجيز التسجيل فيه.")
            return redirect("reports:lab_asset_detail", pk=pk)

        form = LabAssetForm(
            request.POST, instance=asset, school=school, user=request.user
        )
        if form.is_valid():
            form.save()
            messages.success(request, "حُدِّثت بيانات الصنف.")
            return redirect("reports:lab_asset_detail", pk=pk)
        messages.error(request, "تعذّر الحفظ — تحقّق من الحقول.")

    return render(
        request,
        "reports/lab_asset_detail.html",
        {
            "active": "lab_assets",
            "active_school": school,
            "asset": asset,
            "form": form,
            "handover_form": handover_form,
            "handovers": list(
                asset.handovers.select_related("person", "recorded_by").all()[:50]
            ),
            "out_quantity": asset.out_quantity,
            "available_quantity": asset.available_quantity,
            "conditions": LabAsset.Condition.choices,
            "can_record": can_record,
        },
    )


@login_required(login_url="reports:login")
@require_http_methods(["POST"])
def lab_asset_action(request: HttpRequest, pk: int) -> HttpResponse:
    """حركة عهدة، أو تغيير حالة، أو إخراج من الجرد."""
    school, can_record, redirect_response = _lab_context(request)
    if redirect_response is not None:
        return redirect_response

    if not can_record:
        messages.error(request, "متابعةُ المختبر لا تُجيز التسجيل فيه.")
        return redirect("reports:lab_asset_detail", pk=pk)

    asset = _asset_for(request, pk, school)
    action = (request.POST.get("lab_action") or "").strip()

    if action == "handover":
        form = LabHandoverForm(request.POST, school=school)
        if not form.is_valid():
            first = next(iter(form.errors.values()))[0]
            messages.error(request, first)
            return redirect("reports:lab_asset_detail", pk=pk)
        try:
            record_handover(
                asset,
                direction=form.cleaned_data["direction"],
                person=form.cleaned_data.get("person"),
                quantity=form.cleaned_data["quantity"],
                actor=request.user,
                note=form.cleaned_data.get("note") or "",
            )
        except ValidationError as exc:
            detail = getattr(exc, "messages", None) or [str(exc)]
            messages.error(request, detail[0])
        else:
            messages.success(
                request,
                "سُجِّل التسليم."
                if form.cleaned_data["direction"] == LabAssetHandover.Direction.OUT
                else "سُجِّل الإرجاع.",
            )

    elif action == "condition":
        try:
            set_asset_condition(
                asset,
                condition=(request.POST.get("condition") or "").strip(),
                actor=request.user,
            )
        except ValidationError as exc:
            detail = getattr(exc, "messages", None) or [str(exc)]
            messages.error(request, detail[0])
        else:
            messages.success(request, "حُدِّثت حالة الصنف.")

    elif action == "retire":
        # الإخراج من الجرد لا حذف: صنفٌ حُذف يمحو معه سجلّ حركته، فيسقط أثر من
        # تسلّمه ومتى — وهو أول ما يُسأل عنه عند فقد عهدة.
        asset.is_active = False
        asset.save(update_fields=["is_active", "updated_at"])
        messages.success(request, "أُخرج الصنف من الجرد مع بقاء سجلّ حركته.")
        return redirect("reports:lab_assets")

    elif action == "restore":
        asset.is_active = True
        asset.save(update_fields=["is_active", "updated_at"])
        messages.success(request, "أُعيد الصنف إلى الجرد.")

    else:
        messages.error(request, "إجراء غير معروف.")

    return redirect("reports:lab_asset_detail", pk=pk)


@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def lab_assets_print(request: HttpRequest) -> HttpResponse:
    """كشف العهدة للطباعة — الجرد الذي يُوقَّع عليه."""
    school, can_record, redirect_response = _lab_context(request)
    if redirect_response is not None:
        return redirect_response

    outstanding = outstanding_handovers(school, user=request.user)
    return render(
        request,
        "reports/lab_assets_print.html",
        {
            "active_school": school,
            "assets": list(assets_for_school(school, user=request.user)),
            "outstanding": outstanding,
            "summary": lab_summary(
                school, user=request.user, outstanding_rows=outstanding
            ),
            "printed_at": timezone.now(),
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# التجارب
# ─────────────────────────────────────────────────────────────────────────────
@login_required(login_url="reports:login")
@require_http_methods(["GET", "POST"])
def lab_experiments(request: HttpRequest) -> HttpResponse:
    """سجل التجارب — الكشف والتوثيق في شاشة واحدة."""
    school, can_record, redirect_response = _lab_context(request)
    if redirect_response is not None:
        return redirect_response

    form = LabExperimentForm(school=school, user=request.user)
    if request.method == "POST":
        if not can_record:
            messages.error(request, "متابعةُ المختبر لا تُجيز التسجيل فيه.")
            return redirect("reports:lab_experiments")

        form = LabExperimentForm(request.POST, school=school, user=request.user)
        if form.is_valid():
            experiment = form.save(commit=False)
            experiment.school = school
            experiment.recorder = request.user
            experiment.save()
            form.save_m2m()
            messages.success(request, "حُفظت المسودة. أكملها ثم أرسلها للاعتماد.")
            return redirect("reports:lab_experiment_detail", pk=experiment.pk)
        messages.error(request, "تعذّر حفظ التجربة — تحقّق من الحقول.")

    rows = experiments_for_school(school, user=request.user)

    term = (request.GET.get("q") or "").strip()
    state = (request.GET.get("state") or "").strip()
    if term:
        rows = rows.filter(
            Q(title__icontains=term)
            | Q(subject__icontains=term)
            | Q(class_name__icontains=term)
        )
    if state in {value for value, _label in ApprovalState.choices}:
        rows = rows.filter(approval_state=state)
    else:
        state = ""

    page = Paginator(rows, PAGE_SIZE).get_page(request.GET.get("page") or 1)
    summary = lab_summary(school, user=request.user)

    return render(
        request,
        "reports/lab_experiments.html",
        {
            "active": "lab_experiments",
            "active_school": school,
            "form": form,
            "page_obj": page,
            "summary": summary,
            "states": ApprovalState.choices,
            "q_term": term,
            "q_state": state,
            "has_filters": bool(term or state),
            "qs": _clean_query_params(request.GET),
            "can_record": can_record,
            "has_lab_assets": summary["assets_total"] > 0,
        },
    )


@login_required(login_url="reports:login")
@require_http_methods(["GET", "POST"])
def lab_experiment_detail(request: HttpRequest, pk: int) -> HttpResponse:
    school, can_record, redirect_response = _lab_context(request)
    if redirect_response is not None:
        return redirect_response

    experiment = _experiment_for(request, pk, school)
    is_owner = experiment.recorder_id == request.user.pk
    editable = bool(
        can_record and (is_owner or not experiment.recorder_id) and experiment.is_editable_by_owner
    )

    form = LabExperimentForm(instance=experiment, school=school, user=request.user)
    if request.method == "POST":
        if not editable:
            # المعتمد نهائي، وما ليس لك لا تعدّله. الرسالة تفرّق بين السببين
            # فلا يظن صاحبها أن الشاشة معطّلة.
            messages.error(
                request,
                "التجربة معتمدة فلا تُعدَّل."
                if experiment.is_final
                else "لا تملك تعديل هذه التجربة.",
            )
            return redirect("reports:lab_experiment_detail", pk=pk)

        form = LabExperimentForm(
            request.POST, instance=experiment, school=school, user=request.user
        )
        if form.is_valid():
            form.save()
            messages.success(request, "حُدِّثت بيانات التجربة.")
            return redirect("reports:lab_experiment_detail", pk=pk)
        messages.error(request, "تعذّر الحفظ — تحقّق من الحقول.")

    return render(
        request,
        "reports/lab_experiment_detail.html",
        {
            "active": "lab_experiments",
            "active_school": school,
            "experiment": experiment,
            "form": form,
            "editable": editable,
            "is_owner": is_owner,
            # مصدر الحقيقة الوحيد للأزرار. لا يُعرض زرٌّ لا تذكره هذه القائمة.
            "actions": available_actions(experiment, request.user, school=school),
            "timeline": list(transitions_for(experiment)),
            "can_record": can_record,
            "submission_missing": _submission_missing(experiment),
            "has_lab_assets": form.fields["assets"].queryset.exists(),
        },
    )


@login_required(login_url="reports:login")
@require_http_methods(["POST"])
def lab_experiment_action(request: HttpRequest, pk: int) -> HttpResponse:
    """دورة اعتماد التجربة — كل انتقال من ``services_approval`` وحده."""
    school, _can_record, redirect_response = _lab_context(request)
    if redirect_response is not None:
        return redirect_response

    experiment = _experiment_for(request, pk, school)
    action = (request.POST.get("approval_action") or "").strip()
    note = (request.POST.get("note") or "").strip()

    handler = ACTION_DISPATCH.get(action)
    if handler is None or action not in available_actions(
        experiment, request.user, school=school
    ):
        messages.error(request, "هذا الإجراء غير متاح على هذه التجربة الآن.")
        return redirect("reports:lab_experiment_detail", pk=pk)

    try:
        handler(experiment, request.user, school=school, note=note)
    except PermissionDenied as exc:
        messages.error(request, str(exc) or "لا تملك هذا الإجراء.")
    except (ApprovalError, ValidationError) as exc:
        detail = getattr(exc, "messages", None) or [str(exc)]
        messages.error(request, detail[0])
    else:
        messages.success(request, _ACTION_MESSAGES.get(action, "نُفِّذ الإجراء."))

    return redirect("reports:lab_experiment_detail", pk=pk)


@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def lab_experiment_print(request: HttpRequest, pk: int) -> HttpResponse:
    school, _can_record, redirect_response = _lab_context(request)
    if redirect_response is not None:
        return redirect_response

    experiment = _experiment_for(request, pk, school)
    return render(
        request,
        "reports/lab_experiment_print.html",
        {
            "active_school": school,
            "experiment": experiment,
            "assets": list(experiment.assets.all()),
            "printed_at": timezone.now(),
        },
    )
