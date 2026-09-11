# reports/views/platform.py
# -*- coding: utf-8 -*-
from __future__ import annotations

from ._helpers import *
from ._helpers import (
    _parse_date_safe, _set_active_school,
    _get_active_school, _user_manager_schools,
    _clean_query_value, _clean_query_params,
)
from ..services_archive import attach_school_consumption_rows, school_consumption_summary


# =========================
# لوحة إدارة المنصة (مالك النظام وحده)
# =========================


def _require_platform_admin_or_superuser(request: HttpRequest) -> bool:
    return bool(getattr(request.user, "is_superuser", False))


def _require_platform_school_access(request: HttpRequest, school: Optional[School]) -> bool:
    return bool(getattr(request.user, "is_superuser", False) and school is not None)


def _attach_directory_subscription_status(schools: list[School]) -> None:
    if not schools:
        return

    subscription_by_school_id = {
        sub.school_id: sub
        for sub in SchoolSubscription.objects.filter(school_id__in=[school.id for school in schools]).select_related("plan")
    }

    for school in schools:
        subscription = subscription_by_school_id.get(school.id)
        school.directory_subscription = subscription

        if subscription is None:
            school.directory_subscription_state = "none"
            school.directory_subscription_label = "بدون اشتراك"
            continue

        if bool(subscription.is_cancelled):
            school.directory_subscription_state = "cancelled"
            school.directory_subscription_label = "ملغي"
            continue

        if bool(subscription.is_expired):
            school.directory_subscription_state = "expired"
            school.directory_subscription_label = "منتهي"
            continue

        school.directory_subscription_state = "active"
        school.directory_subscription_label = "ساري"


@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def platform_schools_directory(request: HttpRequest) -> HttpResponse:
    if not _require_platform_admin_or_superuser(request):
        messages.error(request, "لا تملك صلاحية الوصول إلى شاشة المدارس.")
        return redirect("reports:home")

    base_qs = School.objects.all().order_by("name")

    q = _clean_query_value(request.GET.get("q"))
    gender = _clean_query_value(request.GET.get("gender")).lower()
    city = _clean_query_value(request.GET.get("city"))

    # قائمة المدن من كامل النطاق (قبل فلترة city) حتى تبقى القائمة مفيدة.
    try:
        cities = (
            base_qs.exclude(city__isnull=True)
            .exclude(city__exact="")
            .values_list("city", flat=True)
            .distinct()
            .order_by("city")
        )
        cities = list(cities)
    except Exception:
        cities = []

    qs = base_qs
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(code__icontains=q) | Q(city__icontains=q))
    if gender in {"boys", "girls"}:
        qs = qs.filter(gender=gender)
    if city:
        qs = qs.filter(city=city)

    page_obj = Paginator(qs.order_by("name"), 25).get_page(request.GET.get("page") or 1)
    school_rows = list(page_obj.object_list)
    _attach_directory_subscription_status(school_rows)
    attach_school_consumption_rows(school_rows)
    page_obj.object_list = school_rows

    ctx = {
        "schools": page_obj,
        "page_obj": page_obj,
        "cities": cities,
        "q": q,
        "gender": gender,
        "city": city,
        "qs": _clean_query_params(request.GET),
    }
    return render(request, "reports/platform_schools_directory.html", ctx)


@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def platform_enter_school(request: HttpRequest, pk: int) -> HttpResponse:
    if not _require_platform_admin_or_superuser(request):
        raise Http404("ليس لديك صلاحية")

    school = get_object_or_404(School, pk=pk)

    _set_active_school(request, school)
    return redirect("reports:platform_school_dashboard")


@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def platform_school_dashboard(request: HttpRequest) -> HttpResponse:
    if not _require_platform_admin_or_superuser(request):
        messages.error(request, "لا تملك صلاحية الوصول إلى لوحة المدرسة.")
        return redirect("reports:home")

    active_school = _get_active_school(request)
    if active_school is None:
        return redirect("reports:platform_schools_directory")

    if not _require_platform_school_access(request, active_school):
        try:
            request.session.pop("active_school_id", None)
        except Exception:
            pass
        messages.error(request, "هذه المدرسة خارج نطاق صلاحياتك.")
        return redirect("reports:platform_schools_directory")

    subscription = (
        SchoolSubscription.objects.filter(school=active_school)
        .select_related("plan")
        .first()
    )

    return render(
        request,
        "reports/platform_school_dashboard.html",
        {
            "school": active_school,
            "subscription": subscription,
            # نفس مصدر أرقام لوحة المدير، فلا يختلف ما يراه الطرفان عن مدرسة واحدة.
            "consumption": school_consumption_summary(active_school),
        },
    )


@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def platform_school_reports(request: HttpRequest) -> HttpResponse:
    if not _require_platform_admin_or_superuser(request):
        messages.error(request, "لا تملك صلاحية الوصول.")
        return redirect("reports:home")

    active_school = _get_active_school(request)
    if active_school is None:
        messages.error(request, "فضلاً اختر مدرسة أولاً.")
        return redirect("reports:platform_schools_directory")

    if not _require_platform_school_access(request, active_school):
        messages.error(request, "هذه المدرسة خارج نطاق صلاحياتك.")
        return redirect("reports:platform_schools_directory")

    cats = allowed_categories_for(request.user, active_school)
    qs = get_admin_reports_queryset(user=request.user, active_school=active_school)

    start_date = _parse_date_safe(request.GET.get("start_date"))
    end_date = _parse_date_safe(request.GET.get("end_date"))
    teacher_name = (request.GET.get("teacher_name") or "").strip()
    category = (request.GET.get("category") or "").strip().lower()

    qs = apply_admin_report_filters(
        qs,
        start_date=start_date,
        end_date=end_date,
        teacher_name=teacher_name,
        category=category,
        cats=cats,
    )

    allowed_choices = get_reporttype_choices(active_school=active_school) if (HAS_RTYPE and ReportType is not None) else []
    reports_page = svc_paginate(qs, per_page=20, page=request.GET.get("page", 1))

    context = {
        "reports": reports_page,
        "start_date": request.GET.get("start_date", ""),
        "end_date": request.GET.get("end_date", ""),
        "teacher_name": teacher_name,
        "category": category if (not cats or "all" in cats or category in cats) else "",
        "categories": allowed_choices,
        "can_delete": False,
    }
    return render(request, "reports/admin_reports.html", context)


@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def platform_school_tickets(request: HttpRequest) -> HttpResponse:
    if not _require_platform_admin_or_superuser(request):
        messages.error(request, "لا تملك صلاحية الوصول.")
        return redirect("reports:home")

    active_school = _get_active_school(request)
    if active_school is None:
        messages.error(request, "فضلاً اختر مدرسة أولاً.")
        return redirect("reports:platform_schools_directory")

    if not _require_platform_school_access(request, active_school):
        messages.error(request, "هذه المدرسة خارج نطاق صلاحياتك.")
        return redirect("reports:platform_schools_directory")

    qs = (
        Ticket.objects.select_related("creator", "assignee", "department")
        .prefetch_related("recipients")
        .filter(school=active_school, is_platform=False)
        .order_by("-created_at")
    )

    status = (request.GET.get("status") or "").strip()
    valid_statuses = {value for value, _label in Ticket.Status.choices}
    q = (request.GET.get("q") or "").strip()
    mine = request.GET.get("mine") == "1"

    if status == "attention":
        qs = qs.filter(status__in=[Ticket.Status.OPEN, Ticket.Status.IN_PROGRESS])
    elif status in valid_statuses:
        qs = qs.filter(status=status)
    else:
        status = ""
    if mine:
        qs = qs.filter(Q(assignee=request.user) | Q(recipients=request.user)).distinct()
    if q:
        for kw in q.split():
            qs = qs.filter(Q(title__icontains=kw) | Q(body__icontains=kw))

    ctx = {
        "tickets": Paginator(qs, 25).get_page(request.GET.get("page") or 1),
        "status": status,
        "q": q,
        "mine": mine,
        "status_choices": Ticket.Status.choices,
        "page_title": f"طلبات {active_school.name}",
        "page_heading": f"طلبات المدرسة: {active_school.name}",
        "page_subtitle": "عرض رقابي لطلبات المدرسة الداخلية وحالاتها دون تعديل بيانات المدرسة.",
    }
    return render(request, "reports/tickets_inbox.html", ctx)


@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def manager_school_tickets(request: HttpRequest) -> HttpResponse:
    """طلبات المدرسة — للمدير كاملةً، ولمن مُنح «متابعة الطلبات» في نطاقه.

    شاشةٌ واحدة لا شاشتان: الفرق بين المدير والوكيل هنا **مدى** ما يراه لا شكل
    ما يراه، والمدى يُحسم في الاستعلام أدناه. وشاشتان تعرضان القائمة نفسها
    بنطاقين مختلفين تتباعدان عند أول تعديل في المرشّحات.
    """
    from ..capabilities import HANDLE_REQUESTS
    from ..permissions import capability_source, supervised_department_ids

    active_school = _get_active_school(request)
    is_manager = bool(
        getattr(request.user, "is_superuser", False)
        or (active_school is not None and active_school in _user_manager_schools(request.user))
    )
    may_handle = bool(
        active_school is not None
        and capability_source(request.user, HANDLE_REQUESTS, active_school) is not None
    )

    if School.objects.filter(is_active=True).exists():
        if active_school is None:
            messages.error(request, "فضلاً اختر مدرسة أولاً.")
            return redirect("reports:select_school")
        if not (is_manager or may_handle):
            messages.error(request, "لا تملك صلاحية الوصول إلى هذه الصفحة.")
            return redirect("reports:home")

    qs = (
        Ticket.objects.select_related("creator", "assignee", "department")
        .prefetch_related("recipients")
        .filter(school=active_school, is_platform=False)
        .order_by("-created_at")
    )

    # النطاق قبل المرشّح: يُضيَّق الاستعلام الأساس أولاً، ثم تُبنى فوقه مرشّحات
    # المستخدم. وأقسامٌ فارغة تعني كشفاً فارغاً — لا كشف المدرسة كاملاً.
    if not is_manager:
        supervised = supervised_department_ids(request.user, active_school)
        qs = qs.filter(department_id__in=supervised) if supervised else qs.none()

    status = (request.GET.get("status") or "").strip()
    valid_statuses = {value for value, _label in Ticket.Status.choices}
    q = (request.GET.get("q") or "").strip()
    mine = request.GET.get("mine") == "1"

    if status == "attention":
        qs = qs.filter(status__in=[Ticket.Status.OPEN, Ticket.Status.IN_PROGRESS])
    elif status in valid_statuses:
        qs = qs.filter(status=status)
    else:
        status = ""
    if mine:
        qs = qs.filter(Q(assignee=request.user) | Q(recipients=request.user)).distinct()
    if q:
        for kw in q.split():
            qs = qs.filter(Q(title__icontains=kw) | Q(body__icontains=kw))

    ctx = {
        "tickets": Paginator(qs, 25).get_page(request.GET.get("page") or 1),
        "status": status,
        "q": q,
        "mine": mine,
        "status_choices": Ticket.Status.choices,
        "page_title": "طلبات المدرسة الداخلية",
        "page_heading": "طلبات المدرسة الداخلية"
        if is_manager
        else "طلبات نطاق إشرافي",
        # العنوان يقول للوكيل إن ما يراه بعضٌ لا كل: كشفٌ مُنطَق يُقرأ كاملاً
        # يجعله يظن أن مدرسته لا طلبات فيها غير هذه.
        "page_subtitle": "طلبات العمل داخل المدرسة؛ راجع المسؤول والحالة والملاحظات من مكان واحد."
        if is_manager
        else "طلبات الأقسام التي تشرف عليها وحدها — وما خرج عن نطاقك يبقى عند مدير المدرسة.",
    }
    return render(request, "reports/tickets_inbox.html", ctx)


@login_required(login_url="reports:login")
@require_http_methods(["GET", "POST"])
def platform_school_notify(request: HttpRequest) -> HttpResponse:
    if not _require_platform_admin_or_superuser(request):
        messages.error(request, "لا تملك صلاحية الوصول.")
        return redirect("reports:home")

    active_school = _get_active_school(request)
    if active_school is not None and not _require_platform_school_access(request, active_school):
        messages.error(request, "هذه المدرسة خارج نطاق صلاحياتك.")
        return redirect("reports:platform_schools_directory")

    form = PlatformSchoolNotificationForm(
        request.POST or None,
        user=request.user,
        active_school=active_school,
    )
    if request.method == "POST" and form.is_valid():
        title = (form.cleaned_data.get("title") or "").strip()
        message_text = form.cleaned_data["message"]
        is_important = bool(form.cleaned_data.get("is_important"))
        target_schools = list(form.target_schools())

        try:
            created_count = 0
            recipient_count = 0
            with transaction.atomic():
                for school in target_schools:
                    n = Notification.objects.create(
                        title=title,
                        message=message_text,
                        is_important=is_important,
                        school=school,
                        created_by=request.user,
                    )
                    created_count += 1
                    teacher_ids = list(
                        SchoolMembership.objects.filter(
                            school=school,
                            is_active=True,
                            teacher__is_active=True,
                        )
                        .values_list("teacher_id", flat=True)
                        .distinct()
                    )
                    recipients = [NotificationRecipient(notification=n, teacher_id=tid) for tid in teacher_ids]
                    NotificationRecipient.objects.bulk_create(recipients, ignore_conflicts=True)
                    recipient_count += len(teacher_ids)

                    try:
                        from ..cache_utils import invalidate_user_notifications

                        for tid in teacher_ids:
                            invalidate_user_notifications(int(tid))
                    except Exception:
                        pass

                    # Push WS delta (bulk_create doesn't trigger signals)
                    try:
                        from ..realtime_notifications import push_new_notification_to_teachers

                        push_new_notification_to_teachers(notification=n, teacher_ids=teacher_ids)
                    except Exception:
                        pass
            messages.success(request, f"تم إرسال الإشعار إلى {recipient_count} مستخدم ضمن {created_count} مدرسة.")
            return redirect("reports:notifications_sent")
        except Exception:
            logger.exception("Failed to send school notification")
            messages.error(request, "تعذّر إرسال الإشعار. حاول مرة أخرى.")

    return render(
        request,
        "reports/platform_school_notify.html",
        {
            "form": form,
            "school": active_school,
            "allowed_schools_count": form.fields["selected_schools"].queryset.count(),
        },
    )
