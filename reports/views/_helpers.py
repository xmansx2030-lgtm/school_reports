# reports/views/_helpers.py
# -*- coding: utf-8 -*-
"""Shared imports, helpers and constants for all view modules."""
from __future__ import annotations
from datetime import date, timedelta
from functools import wraps
import logging
import os
import traceback
from typing import Optional, Tuple
from urllib.parse import quote, urlencode, urlparse

import openpyxl

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.hashers import make_password
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import (
    Count,
    Exists,
    F,
    Prefetch,
    Q,
    ManyToManyField,
    ForeignKey,
    OuterRef,
    Subquery,
    Sum,
)
from django.db.models.functions import TruncWeek, TruncMonth
from django.core.exceptions import ValidationError
from django.http import FileResponse, Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.views.decorators.http import require_http_methods
from django.views.decorators.cache import cache_control, cache_page, never_cache
from django.db.models.deletion import ProtectedError

from django.templatetags.static import static
from django.contrib.staticfiles import finders

from django_ratelimit.decorators import ratelimit


def _user_guide_md_path() -> str:
    # Prefer the curated precise guide if present, fallback to legacy guide.
    preferred = os.path.join(settings.BASE_DIR, "docs", "system_user_guide_precise_ar.md")
    if os.path.exists(preferred):
        return preferred
    return os.path.join(settings.BASE_DIR, "docs", "user_guide_complete_ar.md")


@require_http_methods(["GET"])
def user_guide(request: HttpRequest) -> HttpResponse:
    """Render the public, task-focused Arabic user guide."""
    ctx = {
        "download_url": reverse("reports:user_guide_download"),
        "download_pdf_url": reverse("reports:user_guide_download_pdf"),
    }
    return render(request, "reports/user_guide.html", ctx)


@require_http_methods(["GET"])
def user_guide_download(request: HttpRequest) -> HttpResponse:
    """Download the raw Markdown file for the user guide."""

    md_path = _user_guide_md_path()
    if not os.path.exists(md_path):
        raise Http404("User guide not found")

    return FileResponse(
        open(md_path, "rb"),
        as_attachment=True,
        filename="user_guide_complete_ar.md",
        content_type="text/markdown; charset=utf-8",
    )


@cache_page(60 * 60)
@ratelimit(key="ip", rate="20/h", method="GET", block=True)
@require_http_methods(["GET"])
def user_guide_download_pdf(request: HttpRequest) -> HttpResponse:
    """Download the user guide as a PDF (includes platform logo)."""
    try:
        from ..pdf_offload import render_pdf_offloaded
        from ..pdf_user_guide import generate_user_guide_pdf
        from ..tasks import render_user_guide_pdf_task

        base_url = request.build_absolute_uri("/")
        pdf_bytes = render_pdf_offloaded(
            task=render_user_guide_pdf_task,
            task_args=[base_url],
            render_locally=lambda: generate_user_guide_pdf(base_url=base_url),
            label="user-guide",
        )
    except Exception:
        logging.getLogger(__name__).exception("Failed to render user guide PDF")
        return HttpResponse(
            "تعذر توليد ملف PDF حاليًا.",
            status=503,
            content_type="text/plain; charset=utf-8",
        )
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = 'attachment; filename="user_guide.pdf"'
    return response

# ===== فورمات =====
from ..forms import (
    ReportForm,
    ReportEvidenceFormSet,
    TeacherCreateForm,
    TeacherEditForm,
    MyProfileEmailForm,
    MyProfilePhoneForm,
    MyPasswordChangeForm,
    TicketActionForm,
    TicketCreateForm,
    DepartmentForm,  # إن لم تكن موجودة في مشروعك سيتم استخدام بديل داخلي
    ManagerCreateForm,
    ArchiveStorageOptionForm,
    PlatformSettingsForm,
    PricingMatrixForm,
    SubscriptionPlanForm,
    SchoolSubscriptionForm,
    SchoolArchiveAddonForm,
    DiscountCodeForm,
    AchievementCreateYearForm,
    TeacherAchievementFileForm,
    AchievementSectionNotesForm,
    AchievementEvidenceUploadForm,
    AchievementManagerNotesForm,
    LeadershipPortfolioForm,
    LeadershipPortfolioSectionForm,
    PlatformSchoolNotificationForm,
    PrivateCommentForm,
    TicketNoteEditForm,
)

# ── لماذا لم تعد هذه الاستيرادات «اختيارية» ────────────────────────────────
# كانت كل واحدة منها ملفوفة بـ ``try/except`` تُسند ``None`` عند الفشل، ومعها
# في كل موضع استعمال شرطُ ``if X is not None``. والشرط لا يحمي من شيء: النماذج
# والنماذج الاستمارية جزءٌ من التطبيق نفسه، و117 ترحيلاً تُثبت وجودها. أما
# الثمن فحقيقي — فشلُ استيرادٍ حقيقي (خطأ نحوي، دورةُ استيراد، حقل محذوف) كان
# يُقرأ «الميزة غير متوفرة» فتختفي شاشات كاملة بلا خطأ واحد، ويُشخَّص ذلك
# كعطلٍ في الصلاحيات لا كخطأ استيراد.
#
# فالاستيراد الآن مباشر: يفشل التطبيق عند الإقلاع بأثرٍ يقول أين — وهو أرخص
# ألف مرة من ميزةٍ تختفي صامتة في الإنتاج.
from ..forms import NotificationCreateForm

# ===== موديلات =====
from ..models import (
    Report,
    PlatformSettings,
    ShareLink,
    get_share_link_default_days,
    Teacher,
    Ticket,
    TicketNote,
    TicketImage,
    School,
    SchoolAdditionRequest,
    SchoolMembership,
    MANAGER_SLUG,
    SubscriptionPlan,
    SchoolSubscription,
    SchoolArchiveAddon,
    SchoolYearArchive,
    SchoolYearArchiveDownload,
    ArchiveStorageOption,
    Payment,
    DiscountCode,
    DiscountRedemption,
    AuditLog,
    CustomerComplaint,
    school_has_archive_addon,
    TeacherAchievementFile,
    AchievementSection,
    AchievementEvidenceImage,
    AchievementEvidenceReport,
    SchoolLeadershipPortfolio,
    LeadershipPortfolioSection,
    LeadershipEvidenceImage,
    LeadershipEvidenceReport,
    TeacherPrivateComment,
)

from ..services_archive import (
    UNCLASSIFIED_YEAR,
    archive_available_years,
    archive_payload,
    archive_snapshot_capacity_error,
    archive_storage_capacity_error,
    archive_year_label,
    reclaimable_storage_by_year,
    school_administrative_archive_payload,
    school_administrative_archive_stats,
    _human_size,
    school_archive_enabled,
    school_archive_overview,
    school_consumption_summary,
    school_snapshot_used_bytes,
    school_storage_allowance,
    school_storage_pressure,
    storage_display_for_seats,
    school_storage_limit_bytes,
    school_storage_overview,
    sync_school_archive_storage_usage,
)

from ..services_achievement import (
    achievement_picker_reports_qs,
    add_report_evidence,
    ensure_achievement_sections as _ensure_achievement_sections,
    freeze_achievement_report_evidences,
    remove_report_evidence,
)

from ..models import (
    Department,
    DepartmentMembership,
    Notification,
    NotificationRecipient,
    ReportType,
)

# ===== صلاحيات =====
from ..permissions import (
    allowed_categories_for,
    role_required,
    restrict_queryset_for_user,
    effective_user_role_label,
    get_school_manager_school_ids,
    is_admin_staff,
    is_executive_director,
    is_school_deputy,
    is_school_manager,
    platform_allowed_schools_qs,
)
from ..permissions import is_officer

# ===== خدمات التقارير (تنظيم منطق العرض/التصفية) =====
from ..services_reports import (
    apply_admin_report_filters,
    apply_teacher_report_filters,
    get_admin_reports_queryset,
    get_report_for_user_or_404 as svc_get_report_for_user_or_404,
    get_reporttype_choices,
    get_teacher_reports_queryset,
    paginate as svc_paginate,
    teacher_report_stats,
)

from ..permissions import (
    can_delete_report,
    can_edit_report,
    can_share_report,
)

# ===== إعدادات محلية =====
# ``HAS_RTYPE`` بقي لأن قوالب وعروضاً كثيرة تقرؤه؛ وقيمته صارت ثابتةً صادقة
# بعد أن صار الاستيراد مباشراً.
HAS_RTYPE: bool = True
DM_TEACHER = getattr(DepartmentMembership, "TEACHER", "teacher")
DM_OFFICER = getattr(DepartmentMembership, "OFFICER", "officer")

# إيقاف/تشغيل الرجوع التلقائي للحالة عند ملاحظة المرسل (افتراضي معطّل)
AUTO_REOPEN_ON_SENDER_NOTE: bool = getattr(settings, "TICKETS_AUTO_REOPEN_ON_SENDER_NOTE", False)

logger = logging.getLogger(__name__)

# =========================
# أدوات مساعدة عامة
# =========================
def _is_staff(user) -> bool:
    # ✅ دعم مدير المدرسة (School Manager)
    if not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_staff", False):
        return True
    return is_school_manager(user)


def _is_staff_or_officer(user) -> bool:
    """يسمح للموظّفين (is_staff) أو لمسؤولي الأقسام (Officer)."""
    return bool(
        getattr(user, "is_authenticated", False)
        and (_is_staff(user) or is_officer(user))
    )


def coerce_pk(value) -> Optional[int]:
    """مُعرِّفٌ صحيحٌ موجب من مُدخَل خام، أو ``None`` لكل ما ليس كذلك.

    **لماذا لا يُمرَّر الخام إلى الاستعلام؟** لأن ``filter(pk="abc")`` يرفع
    ``ValueError`` من داخل جانغو، فينهار الطلب بـ 500 **قبل** أن يصل السطر
    الذي يعرض الرسالة المقصودة تحته مباشرة. والنتيجة أن الشاشة التي كُتبت لها
    رسالةٌ مهذَّبة تُظهر صفحة خطأٍ عامة بدلاً منها.
    """
    if value in (None, ""):
        return None
    try:
        pk = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return pk if pk > 0 else None


def access_required(test_func, *, message: str = "لا تملك صلاحية الوصول إلى هذه الصفحة."):
    """حارس صلاحية لا يردّ الداخلَ إلى شاشة الدخول.

    ``user_passes_test`` يوجّه **كل** من يرسب في الاختبار إلى ``login_url``،
    فمن كان مسجَّلاً بالفعل يُرمى إلى شاشة دخولٍ لا معنى لها: هو داخلٌ فعلاً،
    ومشكلته صلاحيةٌ لا هوية. الحارس هنا يفرّق بين الحالتين:

    - غير المسجَّل → شاشة الدخول مع ``next`` كما هو متوقَّع.
    - المسجَّل بلا صلاحية → الرئيسية مع سبب مكتوب.
    - طلبات JSON → 403 بجسمٍ يقرأه المتصفّح، لا إعادة توجيه تُفسَّر نجاحاً.

    الاختبار نفسه لا يتغيّر، فمن كان يمرّ يظلّ يمرّ.
    """

    def decorator(view_func):
        @wraps(view_func)
        def _wrapped(request: HttpRequest, *args, **kwargs) -> HttpResponse:
            if test_func(request.user):
                return view_func(request, *args, **kwargs)

            if not getattr(request.user, "is_authenticated", False):
                login_url = reverse("reports:login")
                return redirect(f"{login_url}?{urlencode({'next': request.get_full_path()})}")

            wants_json = (
                request.headers.get("x-requested-with") == "XMLHttpRequest"
                or "application/json" in (request.headers.get("accept") or "")
            )
            if wants_json:
                return JsonResponse({"detail": message}, status=403)

            messages.error(request, message)
            return redirect("reports:home")

        return _wrapped

    return decorator


def _safe_next_url(next_url: str | None) -> str | None:
    if not next_url:
        return None
    next_url = (next_url or "").strip()
    if not next_url:
        return None
    # حماية من قيم template الشائعة عند وجود None
    if next_url.lower() in {"none", "null", "undefined"}:
        return None

    # نسمح فقط بمسارات داخلية تبدأ بـ / (ونمنع //)
    if not next_url.startswith("/") or next_url.startswith("//"):
        return None

    parsed = urlparse(next_url)
    if parsed.scheme == "" and parsed.netloc == "":
        return next_url
    return None


def _role_display_map(active_school: Optional[School] = None) -> dict:
    """خريطة عرض عربية للأدوار/الأقسام.

    ملاحظة مهمة للتوسع (Multi-tenant): قد تتكرر slugs للأقسام بين المدارس،
    لذا عندما تتوفر مدرسة نشطة نُقيّد القراءة عليها (مع السماح بالأقسام العامة school=NULL).
    """
    from ..gender_labels import school_gender_labels

    labels = school_gender_labels(active_school)
    base = {"teacher": labels["teacher"], "manager": labels["manager_short"], "officer": "مسؤول قسم"}
    if Department is not None:
        try:
            qs = Department.objects.filter(is_active=True).only("slug", "role_label", "name")
            if active_school is not None:
                qs = qs.filter(Q(school=active_school) | Q(school__isnull=True))
            for d in qs:
                base[d.slug] = d.role_label or d.name or d.slug
        except Exception:
            pass
    return base


def _is_manager_in_school(user, active_school: Optional[School]) -> bool:
    """هل المستخدم مدير داخل المدرسة النشطة؟

    - السوبر: نعم
    - role.slug == manager: نعم (توافق خلفي)
    - أو SchoolMembership(RoleType.MANAGER) داخل active_school
    """
    if getattr(user, "is_superuser", False):
        return True
    return is_school_manager(user, active_school=active_school)


def _safe_redirect(request: HttpRequest, fallback_name: str) -> HttpResponse:
    nxt = _safe_next_url(request.POST.get("next") or request.GET.get("next"))
    if nxt:
        return redirect(nxt)
    return redirect(fallback_name)

def _parse_date_safe(value: str | None) -> date | None:
    value = _clean_query_value(value)
    if not value:
        return None
    return parse_date(value)


def _clean_query_value(value: str | None) -> str:
    value = (value or "").strip()
    if value.lower() in {"none", "null", "undefined"}:
        return ""
    return value


def _clean_query_params(query_dict, *, drop_keys: tuple[str, ...] = ("page",)) -> str:
    params = query_dict.copy()
    for key in drop_keys:
        params.pop(key, None)

    for key in list(params.keys()):
        cleaned_values = [_clean_query_value(v) for v in params.getlist(key)]
        cleaned_values = [v for v in cleaned_values if v]
        if cleaned_values:
            params.setlist(key, cleaned_values)
        else:
            params.pop(key, None)

    return params.urlencode()


def _filter_by_school(qs, school: Optional[School]):
    """Apply a tenant boundary; missing/invalid context returns no tenant rows."""
    try:
        if "school" in [f.name for f in qs.model._meta.get_fields()]:
            if school is None:
                return qs.none()
            return qs.filter(school=school)
    except Exception:
        return qs.none()
    return qs


def _private_comment_role_label(author, school: Optional[School]) -> str:
    return _canonical_role_label(author, school)


def _school_manager_label(school: Optional[School]) -> str:
    """مسمى مدير/مديرة المدرسة حسب نوع المدرسة."""
    from ..gender_labels import school_gender_labels

    return str(school_gender_labels(school)["manager"])


def _school_teachers_obj_label(school: Optional[School]) -> str:
    """صيغة جمع منصوبة/مجرورة (المعلمين/المعلمات) حسب نوع المدرسة."""
    from ..gender_labels import school_gender_labels

    return str(school_gender_labels(school)["teachers_object"])


def _canonical_role_label(user, school: Optional[School]) -> str:
    return "" if user is None else effective_user_role_label(user, active_school=school)


def _canonical_sender_name(user) -> str:
    if user is None:
        return "الإدارة"
    return (
        (getattr(user, "name", None) or "").strip()
        or (getattr(user, "phone", None) or "").strip()
        or (getattr(user, "username", None) or "").strip()
        or "الإدارة"
    )


def _model_has_field(model, field_name: str) -> bool:
    """تحقق آمن: هل الموديل يحتوي على حقل باسم معين؟"""
    if model is None:
        return False
    try:
        return field_name in {f.name for f in model._meta.get_fields()}
    except Exception:
        return False


def _get_active_school(request: HttpRequest) -> Optional[School]:
    """إرجاع المدرسة المختارة حالياً من الجلسة (إن وُجدت).

    تحسين احترافي:
    - إذا لم تُحدَّد مدرسة في الجلسة، وكان للمستخدم مدرسة واحدة فقط → نعتبرها المدرسة النشطة تلقائياً.
    - لمالك النظام: إن لم يكن لديه عضويات ومدرسة واحدة فقط مفعّلة في النظام → نختارها تلقائياً.
    """
    sid = request.session.get("active_school_id")
    try:
        if sid:
            # ── إعادة استخدام ما حمّله الـ middleware إن توفر ──
            cached = getattr(request, "active_school", None)
            if cached is not None and getattr(cached, "pk", None) == int(sid):
                return cached
            school = School.objects.filter(pk=sid, is_active=True).first()
            if school is not None:
                request.active_school = school
            return school

        user = getattr(request, "user", None)
        # مستخدم عادي: مدرسة واحدة فقط ضمن عضوياته
        if user is not None and getattr(user, "is_authenticated", False):
            schools = _user_schools(user)
            if len(schools) == 1:
                school = schools[0]
                _set_active_school(request, school)
                return school

            # مالك النظام مع مدرسة واحدة فقط في النظام
            if getattr(user, "is_superuser", False):
                qs = School.objects.filter(is_active=True)
                if qs.count() == 1:
                    school = qs.first()
                    if school is not None:
                        _set_active_school(request, school)
                        return school
    except Exception:
        return None
    return None


def active_school_or_redirect(request: HttpRequest) -> tuple[Optional[School], Optional[HttpResponse]]:
    """Resolve the active school or return the standard school-picker redirect."""
    school = _get_active_school(request)
    if school is None:
        messages.error(request, "فضلاً اختر مدرسة أولاً.")
        return None, redirect("reports:select_school")
    return school, None


def _set_active_school(request: HttpRequest, school: Optional[School]) -> None:
    """تحديث المدرسة المختارة في الجلسة للمستخدم الحالي."""
    if school is None:
        request.session.pop("active_school_id", None)
    else:
        request.session["active_school_id"] = school.pk


def _user_schools(user) -> list[School]:
    """إرجاع المدارس المرتبطة بالمستخدم عبر عضويات SchoolMembership."""
    if not getattr(user, "is_authenticated", False):
        return []
    try:
        qs = (
            School.objects.filter(memberships__teacher=user, memberships__is_active=True)
            .distinct()
            .order_by("name")
        )
        return list(qs)
    except Exception:
        return []


def _user_manager_schools(user) -> list[School]:
    """المدارس التي يكون فيها المستخدم مدير مدرسة."""
    if not getattr(user, "is_authenticated", False):
        return []

    try:
        school_ids = get_school_manager_school_ids(user)
        if not school_ids:
            return []
        qs = School.objects.filter(id__in=list(school_ids), is_active=True).order_by("name")
        return list(qs)
    except Exception:
        return []


def _user_department_codes(user, active_school: Optional[School] = None) -> list[str]:
    codes = set()

    # في وضع تعدد المدارس، يجب تحديد المدرسة النشطة لتجنب تداخل slugs بين المدارس
    try:
        if active_school is None and School.objects.filter(is_active=True).count() > 1:
            return []
    except Exception:
        # fail-closed إذا تعذر تحديد عدد المدارس
        if active_school is None:
            return []

    if DepartmentMembership is not None:
        try:
            mem_qs = DepartmentMembership.objects.filter(teacher=user)
            if active_school is not None:
                mem_qs = mem_qs.filter(department__school=active_school)
            mem_codes = mem_qs.values_list("department__slug", flat=True)
            for c in mem_codes:
                if c:
                    codes.add(c)
        except Exception:
            logger.exception("Failed to fetch user department codes")

    return list(codes)
