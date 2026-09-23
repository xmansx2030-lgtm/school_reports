# reports/services_reports.py
# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import date
from typing import Optional

from django.core.cache import cache
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.db.models import Q, QuerySet, Count, Prefetch
from django.shortcuts import get_object_or_404

from .models import Report, ReportEvidence, School


REPORT_EVIDENCE_PREFETCH = Prefetch(
    "evidences",
    queryset=ReportEvidence.objects.only(
        "id", "report_id", "image", "order", "description", "fit_mode", "show_in_print"
    ).order_by("order", "id"),
)

# موديلات مرجعية اختيارية
try:
    from .models import ReportType  # type: ignore
except Exception:  # pragma: no cover
    ReportType = None  # type: ignore

from core.observability import report_degraded as _degraded

from .permissions import allowed_categories_for, restrict_queryset_for_user
from .search_utils import smart_search_q, REPORT_SEARCH_FIELDS


def _model_has_field(model, field_name: str) -> bool:
    """هل للنموذج هذا الحقل؟

    **آخر استعمال مشروع لهذا الفحص.** كان يُستدعى في أربعة عشر موضعاً على
    نماذج معروفة بالاسم (``Report``، ``Department``، ``Ticket``، ``ReportType``)
    — وكلها تملك ``school`` منذ عشرات الترحيلات، فكان الشرط يُقيَّم ``True``
    دائماً ويُخفي القصد. أما هنا فالنموذج **مجهول وقت الكتابة**: يأتي من
    ``qs.model`` أياً كان المتصل، فالسؤال حقيقي.
    """
    try:
        return field_name in {f.name for f in model._meta.get_fields()}
    except Exception:
        return False


def filter_by_school(qs: QuerySet, active_school: Optional[School]) -> QuerySet:
    """Scope tenant-owned rows to one school, failing closed without context."""
    try:
        if _model_has_field(qs.model, "school"):
            if active_school is None:
                return qs.none()
            return qs.filter(school=active_school)
    except Exception:
        _degraded("reports.filter_by_school", model=getattr(qs, "model", None).__name__)
        return qs.none()
    return qs


def paginate(qs: QuerySet, *, per_page: int, page: str | int | None):
    paginator = Paginator(qs, per_page)
    try:
        return paginator.page(page or 1)
    except PageNotAnInteger:
        return paginator.page(1)
    except EmptyPage:
        return paginator.page(paginator.num_pages)


def get_teacher_reports_queryset(*, user, active_school: Optional[School]) -> QuerySet:
    qs = (
        Report.objects.select_related("teacher", "category", "school")
        .prefetch_related(REPORT_EVIDENCE_PREFETCH)
        .only(
            "id",
            "title",
            "report_date",
            "day_name",
            "beneficiaries_count",
            "idea",
            "image1",
            "image2",
            "image3",
            "image4",
            "teacher_id",
            "teacher__id",
            "teacher__name",
            "category_id",
            "category__id",
            "category__name",
            "school_id",
            "school__id",
            "school__name",
        )
        .filter(teacher=user)
        .order_by("-report_date", "-id")
    )
    return filter_by_school(qs, active_school)


def apply_teacher_report_filters(
    qs: QuerySet,
    *,
    start_date,
    end_date,
    q: str,
) -> QuerySet:
    if start_date:
        qs = qs.filter(report_date__gte=start_date)
    if end_date:
        qs = qs.filter(report_date__lte=end_date)
    if q:
        search_q = smart_search_q(q, REPORT_SEARCH_FIELDS)
        if search_q:
            qs = qs.filter(search_q).distinct()
    return qs


def teacher_report_stats(qs: QuerySet) -> dict:
    today = date.today()
    agg = qs.aggregate(
        total=Count("id"),
        this_month=Count(
            "id",
            filter=Q(report_date__month=today.month, report_date__year=today.year),
        ),
    )
    return {
        "total": int(agg.get("total") or 0),
        "this_month": int(agg.get("this_month") or 0),
    }


def get_admin_reports_queryset(
    *, user, active_school: Optional[School], include_approval_state: bool = False
) -> QuerySet:
    qs = (
        Report.objects.select_related("teacher", "category", "school")
        .prefetch_related(REPORT_EVIDENCE_PREFETCH)
        .only(
            "id",
            "title",
            "report_date",
            "teacher_name",
            "day_name",
            "beneficiaries_count",
            "idea",
            "image1",
            "image2",
            "image3",
            "image4",
            "teacher_id",
            "teacher__id",
            "teacher__name",
            "category_id",
            "category__id",
            "category__name",
            "category__code",
            "school_id",
            "school__id",
            "school__name",
            *(("approval_state",) if include_approval_state else ()),
        )
        .order_by("-report_date", "-id")
    )
    qs = restrict_queryset_for_user(qs, user, active_school)
    return filter_by_school(qs, active_school)


def apply_admin_report_filters(
    qs: QuerySet,
    *,
    start_date,
    end_date,
    teacher_name: str,
    category: str,
    cats,
) -> QuerySet:
    if start_date:
        qs = qs.filter(report_date__gte=start_date)
    if end_date:
        qs = qs.filter(report_date__lte=end_date)

    if teacher_name:
        for token in [t for t in teacher_name.split() if t]:
            qs = qs.filter(teacher_name__icontains=token)

    if category:
        category = (category or "").strip().lower()
        if cats and "all" not in cats:
            if category in cats:
                qs = qs.filter(category__code=category)
        else:
            qs = qs.filter(category__code=category)

    return qs


def get_reporttype_choices(*, active_school: Optional[School]) -> list[tuple[str, str]]:
    if ReportType is None or active_school is None:
        # Return before the cache lookup as well: an old ``school_id=0`` entry
        # from the former fail-open behaviour must not survive a deployment.
        return []

    school_id = int(getattr(active_school, "id", 0) or 0)
    cache_key = f"reporttype-choices:v1:s{school_id}"
    try:
        cached = cache.get(cache_key)
        if cached is not None:
            return list(cached)
    except Exception:
        pass

    qs = ReportType.objects.filter(
        is_active=True,
        school=active_school,
    ).order_by("order", "name")

    result = [(rt.code, rt.name) for rt in qs]
    try:
        cache.set(cache_key, result, 120)
    except Exception:
        pass
    return result


def get_report_for_user_or_404(*, user, pk: int, active_school: Optional[School]):
    """جلب تقرير واحد مع احترام عزل المدارس وصلاحيات الرؤية.

    **مالك النظام وحده يتجاوز حدّ المدرسة.** كان الشرط ``user.is_staff``، و
    ``is_staff`` عَلَمٌ إداري في Django لا دورٌ مستأجِر: يُضبط تلقائياً للسوبر،
    لكنه يُمنح يدوياً من لوحة الإدارة لمن ليس مالكاً للنظام. وباجتماعه مع غياب
    المدرسة النشطة كان يُعيد أي تقرير في المنصة برقمه.

    ومن لا مدرسة نشطة له لا يرى إلا تقاريره: توسيع النطاق عند غياب السياق هو
    عين الخطأ الذي يجب أن يفشل مغلقاً.
    """
    qs = Report.objects.select_related("teacher", "category", "school")

    if getattr(user, "is_superuser", False):
        if active_school is not None:
            qs = qs.filter(school=active_school)
        return get_object_or_404(qs, pk=pk)

    # غير المالك: غياب المدرسة النشطة لا يوسّع النطاق حتى إلى تقاريره؛
    # التقرير سجل تابع لمدرسة وليس أرشيفاً شخصياً عابراً للمستأجرين.
    if active_school is None:
        return get_object_or_404(qs.none(), pk=pk)

    qs = qs.filter(school=active_school)

    try:
        cats = allowed_categories_for(user, active_school) or set()
    except Exception:
        cats = set()

    if "all" in cats:
        return get_object_or_404(qs, pk=pk)

    if cats:
        return get_object_or_404(qs.filter(Q(teacher=user) | Q(category__code__in=list(cats))), pk=pk)

    return get_object_or_404(qs, pk=pk, teacher=user)
