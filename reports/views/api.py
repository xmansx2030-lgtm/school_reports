# reports/views/api.py
from ._helpers import *
from ._helpers import (
    _is_staff_or_officer, _is_manager_in_school,
    _get_active_school,
)
from .schools import _members_for_department, _resolve_department_by_code_or_pk


@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def api_department_members(request: HttpRequest) -> HttpResponse:
    active_school = _get_active_school(request)
    dept = (request.GET.get("department") or "").strip()
    if not dept:
        return JsonResponse({"results": []})

    is_superuser = bool(getattr(request.user, "is_superuser", False))

    # Allow the owner to specify a school explicitly when needed.
    requested_school_id = (request.GET.get("school") or request.GET.get("target_school") or "").strip()
    selected_school = active_school
    if is_superuser and requested_school_id:
        try:
            selected_school = School.objects.filter(pk=int(requested_school_id), is_active=True).first()
        except (TypeError, ValueError):
            selected_school = None

    # عزل صارم: في وضع تعدد المدارس يجب أن تكون هناك مدرسة نشطة لغير السوبر.
    try:
        has_active_schools = School.objects.filter(is_active=True).exists()
    except Exception:
        has_active_schools = False

    if has_active_schools and selected_school is None and not is_superuser:
        return JsonResponse({"detail": "active_school_required", "results": []}, status=403)

    # تحقق عضوية المستخدم في المدرسة النشطة (حتى لا تُحقن session لمدرسة لا ينتمي لها المستخدم)
    if selected_school is not None and not is_superuser:
        try:
            if not SchoolMembership.objects.filter(
                teacher=request.user,
                school=selected_school,
                is_active=True,
            ).exists():
                return JsonResponse({"detail": "forbidden", "results": []}, status=403)
        except Exception:
            return JsonResponse({"detail": "forbidden", "results": []}, status=403)

    users = _members_for_department(dept, selected_school).values("id", "name")
    return JsonResponse({"results": list(users)})


@login_required(login_url="reports:login")
@access_required(_is_staff_or_officer)
@require_http_methods(["GET"])
def api_school_departments(request: HttpRequest) -> HttpResponse:
    """Return active departments for selected school.

    - Superuser: may request any school via ?school=<id>
    - Non-superuser: school is forced to the active school (session isolation)
    - Always includes global departments (school IS NULL)
    """
    if Department is None:
        return JsonResponse({"results": []})

    active_school = _get_active_school(request)

    is_superuser = bool(getattr(request.user, "is_superuser", False))

    # عزل صارم: في وضع تعدد المدارس يجب أن تكون هناك مدرسة نشطة لغير السوبر.
    try:
        has_active_schools = School.objects.filter(is_active=True).exists()
    except Exception:
        has_active_schools = False

    if has_active_schools and active_school is None and not is_superuser:
        return JsonResponse({"detail": "active_school_required", "results": []}, status=403)

    requested_school_id = (request.GET.get("school") or request.GET.get("target_school") or "").strip()
    selected_school = None

    if is_superuser:
        if requested_school_id:
            try:
                selected_school = School.objects.filter(pk=int(requested_school_id), is_active=True).first()
            except (TypeError, ValueError):
                selected_school = None
    else:
        selected_school = active_school
        # تحقق عضوية المستخدم في المدرسة النشطة
        if selected_school is not None:
            try:
                if not SchoolMembership.objects.filter(
                    teacher=request.user,
                    school=selected_school,
                    is_active=True,
                ).exists():
                    return JsonResponse({"detail": "forbidden", "results": []}, status=403)
            except Exception:
                return JsonResponse({"detail": "forbidden", "results": []}, status=403)

    qs = Department.objects.filter(is_active=True)

    # If no school selected (e.g. superuser scope=all), return all active.
    if selected_school is not None:
        qs = qs.filter(Q(school=selected_school) | Q(school__isnull=True))

    qs = qs.order_by("name")
    return JsonResponse({"results": list(qs.values("id", "name"))})


@login_required(login_url="reports:login")
@access_required(_is_staff_or_officer)
@require_http_methods(["GET"])
def api_notification_teachers(request: HttpRequest) -> HttpResponse:
    """Return teachers list for notification create form, filtered by selected school/department.

    This powers the dynamic recipients UI in the notification create template.
    """
    if NotificationCreateForm is None:
        return JsonResponse({"results": []})

    active_school = _get_active_school(request)

    is_superuser = bool(getattr(request.user, "is_superuser", False))

    # عزل صارم: في وضع تعدد المدارس يجب أن تكون هناك مدرسة نشطة لغير السوبر.
    try:
        has_active_schools = School.objects.filter(is_active=True).exists()
    except Exception:
        has_active_schools = False

    if has_active_schools and active_school is None and not is_superuser:
        return JsonResponse({"detail": "active_school_required", "results": []}, status=403)

    # تحقق عضوية المستخدم في المدرسة النشطة (حتى لا تُحقن session لمدرسة لا ينتمي لها المستخدم)
    if active_school is not None and not is_superuser:
        try:
            if not SchoolMembership.objects.filter(
                teacher=request.user,
                school=active_school,
                is_active=True,
            ).exists():
                return JsonResponse({"detail": "forbidden", "results": []}, status=403)
        except Exception:
            return JsonResponse({"detail": "forbidden", "results": []}, status=403)

    data = request.GET.copy()
    mode = (data.get("mode") or "notification").strip().lower()
    if mode not in {"notification", "circular"}:
        mode = "notification"
    is_circular = mode == "circular"

    if is_circular:
        if not is_superuser:
            if active_school is None or not _is_manager_in_school(request.user, active_school):
                return JsonResponse({"detail": "forbidden", "results": []}, status=403)
    # allow alternate query param names
    if "target_school" not in data and data.get("school"):
        data["target_school"] = data.get("school")
    if "target_department" not in data and data.get("department"):
        data["target_department"] = data.get("department")
    if "audience_scope" not in data and data.get("scope"):
        data["audience_scope"] = data.get("scope")

    # Build base queryset using the same constraints as the form.
    form = NotificationCreateForm(data=data, user=request.user, active_school=active_school, mode=mode)
    teachers_qs = form.fields["teachers"].queryset

    dept_values = [
        str(value).strip()
        for value in data.getlist("target_department")
        if str(value).strip()
    ]
    if dept_values:
        selected_school = active_school
        if getattr(request.user, "is_superuser", False):
            selected_school = None
            school_id = (data.get("target_school") or "").strip()
            if school_id:
                try:
                    selected_school = School.objects.filter(pk=int(school_id)).first()
                except (TypeError, ValueError):
                    selected_school = None

        department_member_ids = set()
        for dept_val in dept_values:
            dept_obj, dept_code, _dept_label = _resolve_department_by_code_or_pk(dept_val, selected_school)
            if dept_obj is None and selected_school is None:
                dept_obj, dept_code, _dept_label = _resolve_department_by_code_or_pk(dept_val, None)

            dept_school = selected_school
            try:
                if dept_obj is not None and hasattr(dept_obj, "school"):
                    dept_school = getattr(dept_obj, "school", None)
            except Exception:
                pass

            department_member_ids.update(
                _members_for_department(dept_code, dept_school).values_list("pk", flat=True)
            )
        teachers_qs = teachers_qs.filter(pk__in=department_member_ids)

    return JsonResponse({"results": list(teachers_qs.values("id", "name"))})
