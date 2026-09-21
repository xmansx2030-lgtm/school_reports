# reports/views/reporttypes.py
from ._helpers import *
from ._helpers import _get_active_school, _user_manager_schools

from django.db.models import Count, Q as _Q

from ..model_parts.approvals import ApprovalRoute


def _mark_invalid_fields(form) -> None:
    """Connect existing validation errors to their rendered controls."""
    for field_name in form.errors:
        if field_name in form.fields:
            form.fields[field_name].widget.attrs["aria-invalid"] = "true"


@login_required(login_url="reports:login")
@role_required({"manager"})
@require_http_methods(["GET"])
def reporttypes_list(request: HttpRequest) -> HttpResponse:
    if not (ReportType is not None):
        messages.error(request, "إدارة الأنواع تتطلب تفعيل موديل ReportType وتشغيل الهجرات.")
        return render(request, "reports/reporttypes_list.html", {"items": [], "db_backed": False})

    active_school = _get_active_school(request)
    if School.objects.filter(is_active=True).exists():
        if active_school is None:
            messages.error(request, "فضلاً اختر مدرسة أولاً.")
            return redirect("reports:select_school")
        if (not request.user.is_superuser) and active_school not in _user_manager_schools(request.user):
            messages.error(request, "ليست لديك صلاحية على هذه المدرسة.")
            return redirect("reports:select_school")

    qs = ReportType.objects.all().order_by("order", "name")
    if active_school is not None and hasattr(ReportType, "school"):
        qs = qs.filter(school=active_school)

    # Single annotated query instead of N+1 count queries per ReportType
    count_filter = _Q(reports__school=active_school) if (active_school and hasattr(Report, "school")) else _Q()
    qs = qs.annotate(report_count=Count("reports", filter=count_filter)).prefetch_related(
        "departments"
    )

    route_labels = dict(ApprovalRoute.choices)
    items = [
        {
            "obj": rt,
            "code": rt.code,
            "name": rt.name,
            "description": rt.description,
            "is_active": rt.is_active,
            "order": rt.order,
            "count": rt.report_count,
            "approval_route": rt.approval_route,
            "approval_route_label": route_labels.get(
                rt.approval_route, route_labels[ApprovalRoute.DIRECT]
            ),
            "departments_label": "، ".join(
                department.name for department in rt.departments.all()
            ),
        }
        for rt in qs
    ]
    return render(
        request,
        "reports/reporttypes_list.html",
        {"items": items, "db_backed": True, "school": active_school},
    )

@login_required(login_url="reports:login")
@role_required({"manager"})
@require_http_methods(["GET", "POST"])
def reporttype_create(request: HttpRequest) -> HttpResponse:
    if ReportType is None:
        messages.error(request, "إنشاء الأنواع يتطلب تفعيل موديل ReportType.")
        return redirect("reports:reporttypes_list")

    active_school = _get_active_school(request)
    if School.objects.filter(is_active=True).exists():
        if active_school is None:
            messages.error(request, "فضلاً اختر مدرسة أولاً.")
            return redirect("reports:select_school")
        if (not request.user.is_superuser) and active_school not in _user_manager_schools(request.user):
            messages.error(request, "ليست لديك صلاحية على هذه المدرسة.")
            return redirect("reports:select_school")

    try:
        from ..forms import ReportTypeForm  # type: ignore
        FormCls = ReportTypeForm
    except Exception:
        class _RTForm(forms.ModelForm):
            class Meta:
                model = ReportType
                fields = ("name", "code", "description", "order", "is_active")
        FormCls = _RTForm

    form = FormCls(request.POST or None, active_school=active_school)
    if request.method == "POST":
        if form.is_valid():
            obj = form.save(commit=False)
            if hasattr(obj, "school") and active_school is not None:
                obj.school = active_school
            obj.save()
            if hasattr(form, "save_m2m"):
                form.save_m2m()
            messages.success(request, "تمت إضافة نوع التقرير.")
            return redirect("reports:reporttypes_list")
        messages.error(request, "تعذّر الحفظ. تحقّق من الحقول.")
        _mark_invalid_fields(form)
    return render(
        request,
        "reports/reporttype_form.html",
        {"form": form, "mode": "create", "school": active_school},
    )

@login_required(login_url="reports:login")
@role_required({"manager"})
@require_http_methods(["GET", "POST"])
def reporttype_update(request: HttpRequest, pk: int) -> HttpResponse:
    if ReportType is None:
        messages.error(request, "تعديل الأنواع يتطلب تفعيل موديل ReportType.")
        return redirect("reports:reporttypes_list")

    active_school = _get_active_school(request)
    if School.objects.filter(is_active=True).exists():
        if active_school is None:
            messages.error(request, "فضلاً اختر مدرسة أولاً.")
            return redirect("reports:select_school")
        if (not request.user.is_superuser) and active_school not in _user_manager_schools(request.user):
            messages.error(request, "ليست لديك صلاحية على هذه المدرسة.")
            return redirect("reports:select_school")

    obj = get_object_or_404(ReportType, pk=pk, school=active_school)

    try:
        from ..forms import ReportTypeForm  # type: ignore
        FormCls = ReportTypeForm
    except Exception:
        class _RTForm(forms.ModelForm):
            class Meta:
                model = ReportType
                fields = ("name", "code", "description", "order", "is_active")
        FormCls = _RTForm

    form = FormCls(request.POST or None, instance=obj, active_school=active_school)
    if request.method == "POST":
        if form.is_valid():
            form.save()
            messages.success(request, "تم تعديل نوع التقرير.")
            return redirect("reports:reporttypes_list")
        messages.error(request, "تعذّر الحفظ. تحقّق من الحقول.")
        _mark_invalid_fields(form)
    return render(
        request,
        "reports/reporttype_form.html",
        {"form": form, "mode": "edit", "obj": obj, "school": active_school},
    )

@login_required(login_url="reports:login")
@role_required({"manager"})
@require_http_methods(["POST"])
def reporttype_delete(request: HttpRequest, pk: int) -> HttpResponse:
    if ReportType is None:
        messages.error(request, "حذف الأنواع يتطلب تفعيل موديل ReportType.")
        return redirect("reports:reporttypes_list")

    active_school = _get_active_school(request)
    if School.objects.filter(is_active=True).exists():
        if active_school is None:
            messages.error(request, "فضلاً اختر مدرسة أولاً.")
            return redirect("reports:select_school")
        if (not request.user.is_superuser) and active_school not in _user_manager_schools(request.user):
            messages.error(request, "ليست لديك صلاحية على هذه المدرسة.")
            return redirect("reports:select_school")

    # عزل المدرسة: لا نسمح بحذف نوع تقرير يخص مدرسة أخرى.
    # الأنواع العامة (school is NULL) يسمح بها للسوبر فقط.
    if getattr(request.user, "is_superuser", False):
        obj = get_object_or_404(ReportType, pk=pk)
    else:
        obj = get_object_or_404(ReportType, pk=pk, school=active_school)

    # حساب الاستخدام وفق نطاق المدرسة.
    try:
        if getattr(obj, "school_id", None) is not None:
            used = Report.objects.filter(category__code=obj.code, school_id=obj.school_id).count()
        else:
            used = Report.objects.filter(category__code=obj.code).count()
    except Exception:
        used = Report.objects.filter(category__code=obj.code).count()
    if used > 0:
        messages.error(request, f"لا يمكن حذف «{obj.name}» لوجود {used} تقرير مرتبط. يمكنك تعطيله بدلًا من الحذف.")
        return redirect("reports:reporttypes_list")

    try:
        obj.delete()
        messages.success(request, f"تم حذف «{obj.name}».")
    except Exception:
        logger.exception("reporttype_delete failed")
        messages.error(request, "تعذّر حذف نوع التقرير.")

    return redirect("reports:reporttypes_list")
