# reports/views/billing_platform.py
# -*- coding: utf-8 -*-
"""مكتب مالك المنصة: الاشتراكات والباقات والمدفوعات والتسعير والإعدادات.

كل ما في هذه الوحدة محجوز لمالك المنصة وحده. وفصلُها عن شاشات المدرسة ليس
ترتيباً فحسب: خلطُهما في ملف واحد كان يجعل مراجعةَ صلاحيةٍ واحدة تمرّ على كليهما.
"""
# -*- coding: utf-8 -*-

from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from itertools import pairwise
import json
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse
import uuid

from django.core.exceptions import ImproperlyConfigured
from django.db.models import Case, IntegerField, Value, When
from django.views.decorators.csrf import csrf_exempt

from core.observability import report_degraded as _degraded, soft_call, soft_fail

from ._helpers import *
from ._helpers import (
    _is_staff, _safe_next_url,
    _school_manager_label, _get_active_school,
    _clean_query_value, _clean_query_params, _parse_date_safe,
)
from ..mansour_knowledge import AUDIENCE_LABELS
from ..audit_export import audit_csv_response
from ..models import PlatformEmail
from ..permissions import executive_director_schools_qs
from ..utils import create_system_notification
from ..flexible_pricing import (
    ANCHOR_CAPACITIES,
    PERIODS,
    build_flexible_pricing_catalog,
    normalize_teacher_capacity,
    period_key_for_days,
    quote_for_selection,
    serialize_flexible_pricing_catalog,
)
from ..pricing import SUBSCRIPTION_ADDON_NOTES, SUBSCRIPTION_INCLUDED_FEATURES
from ..discount_codes import release_dead_redemptions
from ..moyasar_gateway import (
    MoyasarGatewayError,
    create_invoice as create_moyasar_invoice,
    fetch_invoice as fetch_moyasar_invoice,
    is_enabled as moyasar_is_enabled,
)

from .billing_core import *  # noqa: F401,F403
from .billing_core import (
    _cache_set,
    ARCHIVE_ADDON_ANNUAL_PRICE,
    ARCHIVE_ADDON_INCLUDED_STORAGE_GB,
    ARCHIVE_STORAGE_BLOCK_GB,
    ARCHIVE_STORAGE_BLOCK_PRICE,
    _archive_pricing,
    _ensure_default_archive_storage_option,
    _archive_storage_options,
    _renewal_plan_catalog,
    _payment_purpose_label,
    _record_subscription_payment_if_missing,
    _ApprovalError,
    _PURPOSE_APPLY_ORDER,
    _apply_payment_effects,
    _PaymentActor,
    _requested_school_id,
    _resolve_payment_actor,
    _subscription_redirect,
    _ACTING_SCHOOL_SESSION_KEY,
    _remember_acting_school,
    _subscription_return_redirect,
    _stamp_payer,
    _notify_managers_of_group_payment,
    _group_payer_badge,
    _PaymentSelectionError,
    _subscription_quote_from_request,
    _build_unified_payment_items,
    _create_unified_payment,
    _manager_payment_membership,
)


# ── مسارٌ يُكتب فيه وقت التشغيل، فلا يجوز أن يكون ثابتاً في الكود ───────────
# محتوى معرفة «منصور» يُحرَّر من لوحة مالك المنصة، أي أن هذا المسار **هدفُ
# كتابة** لا مجرّد مصدر قراءة. وكونُه ثابتاً في الوحدة يعني أن أي اختبار ينسى
# ترقيعه يكتب في ملف المستودع الحقيقي — وقد وقع ذلك فعلاً حين نُقل الثابت إلى
# وحدة أخرى وبقي الترقيع مشيراً إلى مكانه القديم: مرّ الاختبار «ناجحاً» بينما
# مسح 800 سطر من ملف المعرفة.
#
# فصار يُقرأ من الإعدادات: بيئةُ الاختبار توجّهه إلى مجلد مؤقّت مرّةً واحدة،
# فيستحيل على أي اختبار — حاليٍّ أو قادم — أن يلمس ملف المستودع.
MANSOUR_KNOWLEDGE_CONTENT_PATH = Path(
    getattr(
        settings,
        "MANSOUR_KNOWLEDGE_CONTENT_PATH",
        Path(__file__).resolve().parents[1] / "mansour_knowledge_content.json",
    )
)


def _validate_mansour_knowledge_payload(payload: dict) -> list[str]:
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["صيغة الملف غير صحيحة: يجب أن يكون JSON Object."]

    required_sections = ("role_guidance", "role_default_slugs", "knowledge_items")
    for section in required_sections:
        if section not in payload:
            errors.append(f"القسم '{section}' مفقود.")

    role_guidance = payload.get("role_guidance")
    if not isinstance(role_guidance, dict):
        errors.append("القسم role_guidance يجب أن يكون Object.")

    role_default_slugs = payload.get("role_default_slugs")
    if not isinstance(role_default_slugs, dict):
        errors.append("القسم role_default_slugs يجب أن يكون Object.")

    knowledge_items = payload.get("knowledge_items")
    if not isinstance(knowledge_items, list) or not knowledge_items:
        errors.append("القسم knowledge_items يجب أن يكون قائمة غير فارغة.")

    if errors:
        return errors

    known_audiences = set(AUDIENCE_LABELS.keys())
    for audience in known_audiences:
        value = role_guidance.get(audience)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"role_guidance.{audience} يجب أن يكون نصًا غير فارغ.")

    slugs: set[str] = set()
    for idx, item in enumerate(knowledge_items, start=1):
        if not isinstance(item, dict):
            errors.append(f"knowledge_items[{idx}] يجب أن يكون Object.")
            continue

        slug = str(item.get("slug") or "").strip()
        title = str(item.get("title") or "").strip()
        text = str(item.get("text") or "").strip()
        url = str(item.get("url") or "").strip()
        topics = item.get("topics")
        audiences = item.get("audiences")

        if not slug:
            errors.append(f"knowledge_items[{idx}].slug مطلوب.")
        elif slug in slugs:
            errors.append(f"slug مكرر: {slug}")
        else:
            slugs.add(slug)

        if not title:
            errors.append(f"knowledge_items[{idx}].title مطلوب.")
        if not text:
            errors.append(f"knowledge_items[{idx}].text مطلوب.")
        if not url:
            errors.append(f"knowledge_items[{idx}].url مطلوب.")
        if not isinstance(topics, list):
            errors.append(f"knowledge_items[{idx}].topics يجب أن تكون قائمة.")

        if audiences is not None and not isinstance(audiences, list):
            errors.append(f"knowledge_items[{idx}].audiences يجب أن تكون قائمة.")
        elif isinstance(audiences, list):
            for audience in audiences:
                audience_value = str(audience).strip()
                if audience_value and audience_value not in known_audiences:
                    errors.append(
                        f"knowledge_items[{idx}].audiences يحتوي قيمة غير معروفة: {audience_value}"
                    )

    if isinstance(role_default_slugs, dict) and slugs:
        for audience, selected_slugs in role_default_slugs.items():
            if audience not in known_audiences:
                errors.append(f"role_default_slugs يحتوي فئة غير معروفة: {audience}")
                continue
            if not isinstance(selected_slugs, list):
                errors.append(f"role_default_slugs.{audience} يجب أن تكون قائمة.")
                continue
            for slug in selected_slugs:
                slug_value = str(slug).strip()
                if slug_value and slug_value not in slugs:
                    errors.append(
                        f"role_default_slugs.{audience} يحتوي slug غير موجود: {slug_value}"
                    )

    return errors


@login_required(login_url="reports:platform_login")
@user_passes_test(lambda u: getattr(u, "is_superuser", False), login_url="reports:platform_login")
@require_http_methods(["GET", "POST"])
def platform_settings(request: HttpRequest) -> HttpResponse:
    """إعدادات المنصة العامة وتسعير الأرشفة."""
    settings_obj = PlatformSettings.get_solo()
    _ensure_default_archive_storage_option(settings_obj)
    form = PlatformSettingsForm(request.POST or None, instance=settings_obj)
    StorageOptionFormSet = forms.modelformset_factory(
        ArchiveStorageOption,
        form=ArchiveStorageOptionForm,
        extra=0,
        can_delete=True,
    )
    storage_options_formset = StorageOptionFormSet(
        request.POST or None,
        queryset=ArchiveStorageOption.objects.all().order_by(
            "bucket", "sort_order", "storage_gb", "id"
        ),
        prefix="storage_options",
    )

    if request.method == "POST":
        if form.is_valid() and storage_options_formset.is_valid():
            has_active_option = False
            for option_form in storage_options_formset.forms:
                if not getattr(option_form, "cleaned_data", None):
                    continue
                if option_form.cleaned_data.get("DELETE"):
                    continue
                if option_form.cleaned_data.get("storage_gb") and option_form.cleaned_data.get("price") and option_form.cleaned_data.get("is_active"):
                    has_active_option = True

            if not has_active_option:
                messages.error(request, "أضف خيار تخزين واحد مفعّل على الأقل.")
                return render(
                    request,
                    "reports/platform_settings.html",
                    {
                        "form": form,
                        "settings_obj": settings_obj,
                        "storage_options_formset": storage_options_formset,
                    },
                )

            saved = form.save(commit=False)
            saved.updated_by = request.user
            saved.save()
            storage_options_formset.save()
            # حالةُ صيانةٍ لا تُبطَل تُبقي المنصة معروضةً كما كانت — تغييرُ
            # مالك المنصة لا يظهر، فيظنّه لم يُحفظ.
            with soft_fail("platform.invalidate_maintenance_state"):
                from django.core.cache import cache

                cache.delete("platform_maintenance_state_v1")
            messages.success(request, "تم حفظ إعدادات المنصة بنجاح.")
            return redirect("reports:platform_settings")
        messages.error(request, "تعذر حفظ الإعدادات. تحقق من القيم المدخلة.")

    # نظرة عامة على التخزين عبر المنصّة (حقل مخزّن رخيص — بلا أي قراءة شبكية)
    storage_overview = School.objects.aggregate(
        used=Sum("storage_used_bytes"),
        schools=Count("id"),
    )
    platform_storage_used_bytes = int(storage_overview.get("used") or 0)
    schools_count = int(storage_overview.get("schools") or 0)

    # Show what the saved rate actually produces: the operator edits megabytes
    # per teacher but thinks in "what does a 50-teacher school get".
    storage_ladder = [
        {"seats": seats, "label": storage_display_for_seats(seats)}
        for seats in (25, 50, 100)
    ]

    return render(
        request,
        "reports/platform_settings.html",
        {
            "form": form,
            "settings_obj": settings_obj,
            "storage_options_formset": storage_options_formset,
            "platform_storage_used_bytes": platform_storage_used_bytes,
            "schools_count": schools_count,
            "storage_ladder": storage_ladder,
        },
    )


@login_required(login_url="reports:platform_login")
@user_passes_test(lambda u: getattr(u, "is_superuser", False), login_url="reports:platform_login")
@require_http_methods(["GET", "POST"])
def platform_mansour_content(request: HttpRequest) -> HttpResponse:
    """Edit Mansour assistant knowledge content JSON from platform admin dashboard."""

    class MansourContentForm(forms.Form):
        content = forms.CharField(
            label="محتوى قاعدة معرفة منصور (JSON)",
            widget=forms.Textarea(
                attrs={
                    "rows": 30,
                    "dir": "ltr",
                    "spellcheck": "false",
                    "class": "form-control",
                }
            ),
        )

    def _read_content() -> str:
        try:
            return MANSOUR_KNOWLEDGE_CONTENT_PATH.read_text(encoding="utf-8")
        except Exception:
            return "{}"

    if request.method == "POST":
        form = MansourContentForm(request.POST)
        if form.is_valid():
            raw = form.cleaned_data["content"]
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                form.add_error("content", f"صيغة JSON غير صحيحة عند السطر {exc.lineno}.")
            else:
                validation_errors = _validate_mansour_knowledge_payload(payload)
                if validation_errors:
                    form.add_error("content", "\n".join(validation_errors))
                else:
                    pretty = json.dumps(payload, ensure_ascii=False, indent=2)
                    temp_path = MANSOUR_KNOWLEDGE_CONTENT_PATH.with_suffix(".json.tmp")
                    temp_path.write_text(pretty + "\n", encoding="utf-8")
                    temp_path.replace(MANSOUR_KNOWLEDGE_CONTENT_PATH)
                    try:
                        from ..mansour_assistant import reload_mansour_knowledge_runtime

                        reload_mansour_knowledge_runtime()
                    except Exception:
                        messages.warning(
                            request,
                            "تم حفظ المحتوى، لكن لم يتم تحديث جلسة المساعد تلقائيًا. قد تحتاج لإعادة تحميل الخدمة.",
                        )
                    messages.success(request, "تم تحديث محتوى منصور بنجاح.")
                    return redirect("reports:platform_mansour_content")
        messages.error(request, "تعذر حفظ المحتوى. تحقق من صيغة JSON.")
    else:
        form = MansourContentForm(initial={"content": _read_content()})

    stats = {
        "characters": len(form["content"].value() or ""),
    }
    return render(
        request,
        "reports/platform_mansour_content.html",
        {
            "form": form,
            "content_path": str(MANSOUR_KNOWLEDGE_CONTENT_PATH),
            "stats": stats,
        },
    )


@login_required(login_url="reports:platform_login")
@user_passes_test(lambda u: getattr(u, "is_superuser", False), login_url="reports:platform_login")
def platform_admin_dashboard(request: HttpRequest) -> HttpResponse:
    """لوحة تحكم خاصة بمالك النظام لإدارة المنصة بالكامل - تحديث 2026."""
    from django.core.cache import cache
    from django.http import JsonResponse
    from django.db.models.functions import TruncMonth
    import json
    
    now = timezone.now()
    force_refresh = (request.GET.get("refresh") or "").strip() == "1"

    period_labels = {
        "all": "الكل",
        "year": "هذا العام",
        "quarter": "هذا الربع",
        "month": "هذا الشهر",
    }

    def _normalize_period(raw: str | None) -> str:
        value = (raw or "all").strip().lower()
        return value if value in period_labels else "all"

    def _period_start(period: str):
        if period == "year":
            return now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        if period == "quarter":
            quarter_month = ((now.month - 1) // 3) * 3 + 1
            return now.replace(month=quarter_month, day=1, hour=0, minute=0, second=0, microsecond=0)
        if period == "month":
            return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return None
    
    # البيانات الحرجة (بدون كاش أو كاش قصير جداً)
    # ملاحظة: عدّاد التذاكر المفتوحة يأتي من payload الفترة (kpis.tickets_open) أدناه،
    # لذا لا نحسبه هنا تفاديًا لاستعلام مهدور.
    pending_payments = Payment.objects.filter(status=Payment.Status.PENDING).count()
    pending_school_addition_requests = SchoolAdditionRequest.objects.filter(
        status=SchoolAdditionRequest.Status.PENDING
    ).count()
    complaints_pending = CustomerComplaint.objects.filter(
        status__in=(
            CustomerComplaint.Status.NEW,
            CustomerComplaint.Status.IN_PROGRESS,
        )
    ).count()
    platform_email_unread = PlatformEmail.objects.filter(
        direction=PlatformEmail.Direction.INBOUND,
        is_read=False,
        is_archived=False,
    ).count()

    # البيانات الإحصائية (كاش 5 دقائق)
    stats_cache_key = "platform_stats_v4"
    stats = None if force_refresh else cache.get(stats_cache_key)
    
    if not stats:
        # ملاحظة: عدّادات التقارير والتذاكر (إجمالي/منجز/مرفوض) تُعرض حسب الفترة المختارة،
        # وتأتي من payload الفترة، لذلك لا نحسب نسخة "كل الوقت" هنا (كانت تُحسب ثم تُتجاوز).
        teachers_count = Teacher.objects.count()

        # تحسين الاستعلامات باستخدام aggregate
        school_stats = School.objects.aggregate(
            total=Count('id'),
            active=Count('id', filter=Q(is_active=True)),
            storage_used=Sum("storage_used_bytes"),
        )

        # ملخص تشغيلي للتخزين دون أي اتصالات شبكية مع R2.
        # الحد الفعلي يأتي من إضافة الأرشيف النشطة، وإلا من الحد المجاني العام.
        storage_near_limit_count = 0
        # Storage no longer depends on the yearly-archive add-on; it comes from
        # the purchased teacher capacity plus any separately bought space.
        storage_schools = School.objects.select_related("subscription__plan").only(
            "id",
            "storage_used_bytes",
            "extra_storage_gb",
            "subscription__end_date",
            "subscription__is_active",
            "subscription__canceled_at",
            "subscription__teacher_limit_override",
            "subscription__plan__max_teachers",
            "subscription__plan__days_duration",
        )
        # النسخ السنوية لها حدّها المستقل، فإدراجها هنا كان يعدّ المدارس التي
        # أرشفت سنواتها «قاربت الامتلاء» وهي لم تمسّ مساحة عملها. استعلام واحد
        # مجمّع للجميع كي لا يتحوّل العدّاد إلى استعلام لكل مدرسة.
        snapshot_bytes_by_school = {
            row["school"]: int(row["total"] or 0)
            for row in SchoolYearArchive.objects.values("school").annotate(
                total=Sum("storage_bytes")
            )
        }
        for school in storage_schools.iterator():
            limit_bytes = school_storage_limit_bytes(school)
            work_used = max(
                0,
                int(school.storage_used_bytes or 0)
                - snapshot_bytes_by_school.get(school.pk, 0),
            )
            if limit_bytes > 0 and work_used >= int(limit_bytes * 0.8):
                storage_near_limit_count += 1
        
        platform_managers_count = (
            Teacher.objects.filter(
                school_memberships__role_type=SchoolMembership.RoleType.MANAGER,
                school_memberships__is_active=True,
            )
            .distinct()
            .count()
        )

        has_reporttype = False
        reporttypes_count = 0
        with soft_fail("platform.reporttypes_count"):
            has_reporttype = True
            reporttypes_count = ReportType.objects.filter(is_active=True).count()

        stats = {
            "teachers_count": teachers_count,
            "platform_schools_total": school_stats['total'],
            "platform_schools_active": school_stats['active'],
            "platform_storage_used_bytes": int(school_stats["storage_used"] or 0),
            "storage_near_limit_count": storage_near_limit_count,
            "platform_managers_count": platform_managers_count,
            "has_reporttype": has_reporttype,
            "reporttypes_count": reporttypes_count,
        }
        
        _cache_set(stats_cache_key, stats, 300)  # 5 دقائق
    
    # بيانات الاشتراكات والمالية (كاش 3 دقائق)
    # ملاحظة: إجمالي الإيرادات يُعرض حسب الفترة المختارة (kpis.total_revenue)، لذا لا نحسب
    # نسخة "كل الوقت" هنا (كانت تُحسب ثم تُتجاوز).
    financial_cache_key = "platform_financial_v3"
    financial = None if force_refresh else cache.get(financial_cache_key)

    if not financial:
        subscriptions_active = SchoolSubscription.objects.filter(is_active=True, end_date__gte=now.date()).count()
        subscriptions_expired = SchoolSubscription.objects.filter(Q(is_active=False) | Q(end_date__lt=now.date())).count()
        subscriptions_expiring_soon = SchoolSubscription.objects.filter(
            is_active=True,
            end_date__gte=now.date(),
            end_date__lte=now.date() + timedelta(days=30)
        ).count()

        # قائمة الاشتراكات المنتهية قريباً (للجدول)
        subscriptions_expiring_list = SchoolSubscription.objects.filter(
            is_active=True,
            end_date__gte=now.date(),
            end_date__lte=now.date() + timedelta(days=30)
        ).select_related('school', 'plan').order_by('end_date')[:10]

        financial = {
            "subscriptions_active": subscriptions_active,
            "subscriptions_expired": subscriptions_expired,
            "subscriptions_expiring_soon": subscriptions_expiring_soon,
            "subscriptions_expiring_list": list(subscriptions_expiring_list),
        }
        
        _cache_set(financial_cache_key, financial, 180)  # 3 دقائق

    # ملاحظة: SchoolSubscription.days_remaining خاصية محسوبة (read-only)،
    # والقالب يقرأها مباشرة، فلا حاجة لإسنادها هنا (الإسناد كان يسبب AttributeError).

    # بيانات الرسوم الثابتة (غير مرتبطة بفترة الفلتر) - كاش 10 دقائق
    charts_cache_key = "platform_charts_v2"
    charts = None if force_refresh else cache.get(charts_cache_key)
    
    if not charts:
        # توزيع المدارس حسب المرحلة
        schools_by_stage = School.objects.values('stage').annotate(
            count=Count('id')
        ).order_by('stage')
        
        stage_labels = []
        stage_data = []
        stage_colors = []
        # المفاتيح يجب أن تطابق School.Stage القيم الفعلية: kg / primary / middle / high
        color_map = {
            'kg': '#8b5cf6',       # بنفسجي — رياض أطفال
            'primary': '#3b82f6',  # أزرق — ابتدائي
            'middle': '#10b981',   # أخضر — متوسط
            'high': '#f59e0b',     # كهرماني — ثانوي
        }
        
        for item in schools_by_stage:
            stage_name = dict(School.Stage.choices).get(item['stage'], item['stage'])
            stage_labels.append(stage_name)
            stage_data.append(item['count'])
            stage_colors.append(color_map.get(item['stage'], '#6b7280'))
        
        charts = {
            "stage_labels": json.dumps(stage_labels),
            "stage_data": json.dumps(stage_data),
            "stage_colors": json.dumps(stage_colors),
        }
        
        _cache_set(charts_cache_key, charts, 600)  # 10 دقائق

    def _build_period_payload(period: str, *, force: bool = False) -> dict:
        cache_key = f"platform_dashboard_period_payload_v2:{period}"
        cached_payload = None if force else cache.get(cache_key)
        if cached_payload:
            return cached_payload

        start_at = _period_start(period)

        payments_qs = Payment.objects.filter(status=Payment.Status.APPROVED)
        reports_qs = Report.objects.all()
        tickets_qs = Ticket.objects.filter(is_platform=True)
        schools_qs = School.objects.all()

        if start_at is not None:
            payments_qs = payments_qs.filter(payment_date__gte=start_at.date())
            reports_qs = reports_qs.filter(created_at__gte=start_at)
            tickets_qs = tickets_qs.filter(created_at__gte=start_at)
            schools_qs = schools_qs.filter(created_at__gte=start_at)

        total_revenue_period = payments_qs.aggregate(total=Sum("amount"))["total"] or 0
        reports_count_period = reports_qs.count()
        schools_count_period = schools_qs.count()

        tickets_agg = tickets_qs.aggregate(
            total=Count("id"),
            open=Count("id", filter=Q(status__in=["open", "in_progress"])),
            done=Count("id", filter=Q(status="done")),
            rejected=Count("id", filter=Q(status="rejected")),
        )

        revenue_rows = (
            payments_qs
            .annotate(month=TruncMonth("payment_date"))
            .values("month")
            .annotate(total=Sum("amount"))
            .order_by("month")
        )
        reports_rows = (
            reports_qs
            .annotate(month=TruncMonth("created_at"))
            .values("month")
            .annotate(count=Count("id"))
            .order_by("month")
        )

        revenue_labels: list[str] = []
        revenue_data: list[float] = []
        for row in revenue_rows:
            month_value = row.get("month")
            if month_value is None:
                continue
            revenue_labels.append(month_value.strftime("%Y-%m"))
            revenue_data.append(float(row.get("total") or 0))

        reports_labels: list[str] = []
        reports_data: list[int] = []
        for row in reports_rows:
            month_value = row.get("month")
            if month_value is None:
                continue
            reports_labels.append(month_value.strftime("%Y-%m"))
            reports_data.append(int(row.get("count") or 0))

        payload = {
            "period": period,
            "period_label": period_labels.get(period, "الكل"),
            "generated_at": timezone.localtime(now).strftime("%Y-%m-%d %H:%M"),
            "kpis": {
                "schools_total": int(stats.get("platform_schools_total", 0)),
                "schools_active": int(stats.get("platform_schools_active", 0)),
                "schools_created_in_period": int(schools_count_period),
                "subscriptions_active": int(financial.get("subscriptions_active", 0)),
                "storage_used_bytes": int(stats.get("platform_storage_used_bytes", 0)),
                "storage_near_limit": int(stats.get("storage_near_limit_count", 0)),
                "total_revenue": float(total_revenue_period),
                "reports_count": int(reports_count_period),
                "tickets_total": int(tickets_agg.get("total") or 0),
                "tickets_open": int(tickets_agg.get("open") or 0),
                "tickets_done": int(tickets_agg.get("done") or 0),
                "tickets_rejected": int(tickets_agg.get("rejected") or 0),
            },
            "operations": {
                "pending_payments": int(pending_payments),
                "subscriptions_expiring_soon": int(financial.get("subscriptions_expiring_soon", 0)),
            },
            "charts": {
                "revenue": {
                    "labels": revenue_labels,
                    "data": revenue_data,
                },
                "reports": {
                    "labels": reports_labels,
                    "data": reports_data,
                },
            },
        }

        _cache_set(cache_key, payload, 120)

        return payload
    
    # آخر الأنشطة (بدون كاش)
    recent_activities = []
    try:
        recent_payments = Payment.objects.filter(
            status=Payment.Status.APPROVED
        ).select_related('school').order_by('-updated_at')[:5]
        
        for payment in recent_payments:
            recent_activities.append({
                'type': 'payment',
                'icon': 'fa-check-circle',
                'color': 'emerald',
                'title': 'تمت الموافقة على دفعة',
                'description': f"{payment.school.name if payment.school else 'مدرسة'} - {payment.amount} ر.س",
                'time': payment.updated_at,
            })
        
        recent_subscriptions = SchoolSubscription.objects.filter(
            is_active=True
        ).select_related('school', 'plan').order_by('-created_at')[:3]
        
        for sub in recent_subscriptions:
            recent_activities.append({
                'type': 'subscription',
                'icon': 'fa-star',
                'color': 'indigo',
                'title': 'اشتراك جديد',
                'description': f"{sub.school.name} - {sub.plan.name}",
                'time': sub.created_at,
            })
        
        # ترتيب حسب الوقت
        recent_activities.sort(key=lambda x: x['time'], reverse=True)
        recent_activities = recent_activities[:8]
    except Exception:
        _degraded("platform.recent_activities")
    
    selected_period = _normalize_period(request.GET.get("period"))
    period_payload = _build_period_payload(selected_period, force=force_refresh)
    period_payload.setdefault("operations", {})["complaints_pending"] = int(
        complaints_pending
    )

    wants_json = (
        request.GET.get("format") == "json"
        or request.headers.get("x-requested-with") == "XMLHttpRequest"
        or "application/json" in (request.headers.get("accept") or "")
    )
    if wants_json:
        return JsonResponse(period_payload, json_dumps_params={"ensure_ascii": False})

    # دمج جميع البيانات
    ctx = {
        **stats,
        **financial,
        **charts,
        "pending_payments": pending_payments,
        "pending_school_addition_requests": pending_school_addition_requests,
        "complaints_pending": complaints_pending,
        "platform_email_unread": platform_email_unread,
        "tickets_open": int(period_payload["kpis"]["tickets_open"]),
        "recent_activities": recent_activities,
        "initial_period": selected_period,
        "dashboard_period_payload": json.dumps(period_payload, ensure_ascii=False),
        "total_revenue": period_payload["kpis"]["total_revenue"],
        "reports_count": period_payload["kpis"]["reports_count"],
        "tickets_total": period_payload["kpis"]["tickets_total"],
        "tickets_done": period_payload["kpis"]["tickets_done"],
        "tickets_rejected": period_payload["kpis"]["tickets_rejected"],
        "revenue_labels": json.dumps(period_payload["charts"]["revenue"]["labels"], ensure_ascii=False),
        "revenue_data": json.dumps(period_payload["charts"]["revenue"]["data"]),
        "reports_labels": json.dumps(period_payload["charts"]["reports"]["labels"], ensure_ascii=False),
        "reports_data": json.dumps(period_payload["charts"]["reports"]["data"]),
    }

    return render(request, "reports/platform_admin_dashboard.html", ctx)


@login_required(login_url="reports:platform_login")
@user_passes_test(lambda u: getattr(u, "is_superuser", False), login_url="reports:platform_login")
@require_http_methods(["GET"])
def platform_admin_dashboard_data(request: HttpRequest) -> HttpResponse:
    """JSON data endpoint for the platform dashboard."""
    query = request.GET.copy()
    query["format"] = "json"
    request.GET = query
    request.META["HTTP_ACCEPT"] = "application/json"
    request.META["HTTP_X_REQUESTED_WITH"] = "XMLHttpRequest"
    return platform_admin_dashboard(request)


@login_required(login_url="reports:platform_login")
@user_passes_test(lambda u: getattr(u, "is_superuser", False), login_url="reports:platform_login")
@require_http_methods(["GET"])
def platform_admin_dashboard_search(request: HttpRequest) -> HttpResponse:
    """Lightweight global search for the platform admin dashboard."""
    query = (request.GET.get("q") or "").strip()
    results: list[dict[str, str]] = []

    if len(query) < 2:
        return JsonResponse({"results": results}, json_dumps_params={"ensure_ascii": False})
    query_params = urlencode({"q": query})

    school_qs = (
        School.objects.filter(
            Q(name__icontains=query)
            | Q(code__icontains=query)
            | Q(city__icontains=query)
            | Q(phone__icontains=query)
        )
        .only("id", "name", "code", "city")
        .order_by("name")[:5]
    )
    for school in school_qs:
        subtitle_bits = [bit for bit in (school.code, school.city) if bit]
        results.append({
            "title": school.name,
            "subtitle": " · ".join(subtitle_bits) or "مدرسة",
            "type": "مدرسة",
            "icon": "fa-school",
            "href": f"{reverse('reports:schools_admin_list')}?{query_params}",
        })

    # نبحث في مدراء المدارس فقط لأنهم الفئة التي تديرها المنصة وتظهر فعلاً
    # في صفحة الوجهة (school_managers_list). هذا يضمن أن نتيجة البحث قابلة للوصول.
    manager_qs = (
        Teacher.objects.filter(
            Q(name__icontains=query)
            | Q(phone__icontains=query)
            | Q(email__icontains=query)
            | Q(national_id__icontains=query),
            school_memberships__role_type=SchoolMembership.RoleType.MANAGER,
            school_memberships__is_active=True,
        )
        .distinct()
        .only("id", "name", "phone")
        .order_by("name")[:5]
    )
    for teacher in manager_qs:
        results.append({
            "title": teacher.name,
            "subtitle": teacher.phone or "مدير مدرسة",
            "type": "مدير مدرسة",
            "icon": "fa-user-tie",
            "href": f"{reverse('reports:school_managers_list')}?{query_params}",
        })

    ticket_qs = (
        Ticket.objects.filter(
            Q(title__icontains=query)
            | Q(body__icontains=query)
            | Q(school__name__icontains=query),
            is_platform=True,
        )
        .select_related("school")
        .only("id", "title", "status", "school__name")
        .order_by("-updated_at")[:5]
    )
    for ticket in ticket_qs:
        results.append({
            "title": f"#{ticket.pk} - {ticket.title}",
            "subtitle": getattr(ticket.school, "name", "") or ticket.get_status_display(),
            "type": "تذكرة",
            "icon": "fa-headset",
            "href": reverse("reports:ticket_detail", kwargs={"pk": ticket.pk}),
        })

    return JsonResponse({"results": results[:12]}, json_dumps_params={"ensure_ascii": False})


@login_required(login_url="reports:login")
@user_passes_test(lambda u: getattr(u, "is_superuser", False), login_url="reports:login")
def platform_audit_logs(request: HttpRequest) -> HttpResponse:
    """عرض سجل العمليات للنظام بالكامل (لمالك النظام)."""
    
    logs_qs = AuditLog.objects.all().select_related("teacher", "school").order_by("-timestamp")

    teacher_id = _clean_query_value(request.GET.get("teacher"))
    action = _clean_query_value(request.GET.get("action"))
    model_name = _clean_query_value(request.GET.get("model"))
    query = _clean_query_value(request.GET.get("q"))[:120]
    start_date = _parse_date_safe(request.GET.get("start_date"))
    end_date = _parse_date_safe(request.GET.get("end_date"))
    allowed_actions = {value for value, _label in AuditLog.Action.choices}

    if teacher_id.isdigit():
        logs_qs = logs_qs.filter(teacher_id=teacher_id)
    else:
        teacher_id = ""
    if action in allowed_actions:
        logs_qs = logs_qs.filter(action=action)
    else:
        action = ""
    available_models = list(
        logs_qs.order_by("model_name").values_list("model_name", flat=True).distinct()
    )
    if model_name in available_models:
        logs_qs = logs_qs.filter(model_name=model_name)
    else:
        model_name = ""
    if query:
        logs_qs = logs_qs.filter(
            Q(actor_name__icontains=query)
            | Q(teacher__name__icontains=query)
            | Q(teacher__phone__icontains=query)
            | Q(object_repr__icontains=query)
            | Q(model_name__icontains=query)
            | Q(school__name__icontains=query)
        )
    if start_date is not None:
        logs_qs = logs_qs.filter(timestamp__date__gte=start_date)
    if end_date is not None:
        logs_qs = logs_qs.filter(timestamp__date__lte=end_date)

    if request.GET.get("export") == "csv":
        return audit_csv_response(
            logs_qs.select_related("school", "teacher"),
            filename="platform-audit.csv",
        )

    paginator = Paginator(logs_qs, 50)
    page = request.GET.get("page")
    logs = paginator.get_page(page)

    from ..audit_labels import attach_views as _attach_audit_views, model_filter_choices

    _attach_audit_views(logs)

    params = request.GET.copy()
    if "page" in params:
        params.pop("page")
    if "export" in params:
        params.pop("export")
    for key in list(params.keys()):
        cleaned = _clean_query_value(params.get(key))
        if cleaned:
            params[key] = cleaned
        else:
            params.pop(key)

    teachers = (
        Teacher.objects.filter(
            id__in=AuditLog.objects.values("teacher_id").distinct()
        )
        .only("id", "name", "phone")
        .order_by("name", "id")
    )

    ctx = {
        "logs": logs,
        "teachers": teachers,
        "actions": AuditLog.Action.choices,
        "is_platform": True,
        "q_teacher": teacher_id,
        "q_action": action,
        "q_model": model_name,
        "q": query,
        "models": model_filter_choices(available_models),
        "q_start": start_date.isoformat() if start_date else "",
        "q_end": end_date.isoformat() if end_date else "",
        "qs": params.urlencode(),
    }
    return render(request, "reports/audit_logs.html", ctx)


@login_required(login_url="reports:login")
@user_passes_test(lambda u: getattr(u, "is_superuser", False), login_url="reports:login")
def platform_subscriptions_list(request: HttpRequest) -> HttpResponse:
    today = timezone.localdate()
    status = (request.GET.get("status") or "all").strip().lower()
    plan_id = (request.GET.get("plan") or "").strip()
    q = (request.GET.get("q") or "").strip()
    soon_days = 30
    urgent_days = 7

    base_qs = SchoolSubscription.objects.select_related("school", "plan")

    stats = base_qs.aggregate(
        total=Count("id"),
        active=Count("id", filter=Q(is_active=True, end_date__gte=today)),
        cancelled=Count("id", filter=Q(is_active=False, canceled_at__isnull=False)),
        expired=Count(
            "id",
            filter=Q(
                Q(end_date__lt=today, canceled_at__isnull=True)
                | Q(is_active=False, canceled_at__isnull=True)
            ),
        ),
    )
    money_stats = base_qs.aggregate(
        active_value=Sum("plan__price", filter=Q(is_active=True, end_date__gte=today)),
        expiring_value=Sum(
            "plan__price",
            filter=Q(
                is_active=True,
                end_date__gte=today,
                end_date__lte=today + timedelta(days=soon_days),
            ),
        ),
    )

    payments_period_qs = Payment.objects.filter(
        status=Payment.Status.APPROVED,
        amount__gt=0,
        payment_date__gte=today.replace(day=1),
    )
    payment_stats = payments_period_qs.aggregate(
        collected_this_month=Sum("amount"),
    )

    expiring_soon_count = base_qs.filter(
        is_active=True,
        end_date__gte=today,
        end_date__lte=today + timedelta(days=soon_days),
    ).count()
    urgent_renewals_count = base_qs.filter(
        is_active=True,
        end_date__gte=today,
        end_date__lte=today + timedelta(days=urgent_days),
    ).count()

    subscriptions = base_qs
    if status == "active":
        subscriptions = subscriptions.filter(is_active=True, end_date__gte=today)
    elif status == "cancelled":
        subscriptions = subscriptions.filter(is_active=False, canceled_at__isnull=False)
    elif status == "expired":
        subscriptions = subscriptions.filter(
            Q(end_date__lt=today, canceled_at__isnull=True)
            | Q(is_active=False, canceled_at__isnull=True)
        )

    if plan_id:
        subscriptions = subscriptions.filter(plan_id=plan_id)

    if q:
        subscriptions = subscriptions.filter(school__name__icontains=q)

    subscriptions = subscriptions.order_by("-start_date")

    # ✅ لتفادي N+1: نجلب المدفوعات المرتبطة بكل اشتراك
    subscriptions = subscriptions.prefetch_related(
        Prefetch(
            "payments",
            queryset=Payment.objects.filter(
                status__in=[Payment.Status.PENDING, Payment.Status.APPROVED]
            ).only("id", "subscription_id", "payment_date"),
            to_attr="_prefetched_active_payments",
        )
    )

    # ✅ استرجاعات (refunds): مدفوعات approved بمبالغ سالبة
    subscriptions = subscriptions.prefetch_related(
        Prefetch(
            "payments",
            queryset=Payment.objects.filter(
                status=Payment.Status.APPROVED,
                amount__lt=0,
            ).only("id", "subscription_id", "payment_date", "amount"),
            to_attr="_prefetched_refunds",
        )
    )

    # ✅ حساب بسيط: هل يوجد دفع ضمن فترة الاشتراك الحالية؟
    # نستخدم payment_date >= start_date لتحديد أنه يخص نفس الفترة.
    from decimal import Decimal

    plans = SubscriptionPlan.objects.all().order_by("price", "name")

    paginator = Paginator(subscriptions, 30)
    page_obj = paginator.get_page(request.GET.get("page"))

    # تزيين كائنات الصفحة الحالية فقط (بدل كل النتائج)
    collection_gap_count = 0
    for sub in page_obj:
        try:
            pref = getattr(sub, "_prefetched_active_payments", []) or []
            sub.has_payment_for_period = any(
                (getattr(p, "payment_date", None) is not None and p.payment_date >= sub.start_date)
                for p in pref
            )
        except Exception:
            sub.has_payment_for_period = False
        if bool(getattr(sub, "is_active", False)) and not bool(getattr(sub, "is_expired", False)) and not sub.has_payment_for_period and getattr(sub.plan, "price", 0):
            collection_gap_count += 1

        try:
            remaining = int(getattr(sub, "days_remaining", 0))
        except Exception:
            remaining = 0
        sub.commercial_priority = "normal"
        if bool(getattr(sub, "is_cancelled", False)) or bool(getattr(sub, "is_expired", False)):
            sub.commercial_priority = "lost"
        elif not sub.has_payment_for_period and getattr(sub.plan, "price", 0):
            sub.commercial_priority = "collect"
        elif remaining <= urgent_days:
            sub.commercial_priority = "urgent"
        elif remaining <= soon_days:
            sub.commercial_priority = "renew"

        # مبلغ الاسترجاع لهذه الفترة (مجموع القيم السالبة كقيمة موجبة)
        try:
            refunds = getattr(sub, "_prefetched_refunds", []) or []
            total = Decimal("0")
            for p in refunds:
                if getattr(p, "payment_date", None) is not None and p.payment_date >= sub.start_date:
                    amt = getattr(p, "amount", None)
                    if amt is not None:
                        total += (-amt)
            sub.refund_amount_for_period = total
        except Exception:
            sub.refund_amount_for_period = Decimal("0")

    ctx = {
        "subscriptions": page_obj,
        "page_obj": page_obj,
        "status": status,
        "plans": plans,
        "plan_id": plan_id,
        "q": q,
        "stats_total": stats.get("total") or 0,
        "stats_active": stats.get("active") or 0,
        "stats_cancelled": stats.get("cancelled") or 0,
        "stats_expired": stats.get("expired") or 0,
        "results_count": paginator.count,
        "active_value": money_stats.get("active_value") or 0,
        "expiring_value": money_stats.get("expiring_value") or 0,
        "collected_this_month": payment_stats.get("collected_this_month") or 0,
        "expiring_soon_count": expiring_soon_count,
        "urgent_renewals_count": urgent_renewals_count,
        "collection_gap_count": collection_gap_count,
        "soon_days": soon_days,
        "urgent_days": urgent_days,
    }

    return render(request, "reports/platform_subscriptions.html", ctx)


@login_required(login_url="reports:login")
@user_passes_test(lambda u: getattr(u, "is_superuser", False), login_url="reports:login")
def platform_subscription_detail(request: HttpRequest, pk: int) -> HttpResponse:
    """تفاصيل اشتراك مدرسة مع سجل العمليات المالية المرتبط به."""
    subscription = get_object_or_404(
        SchoolSubscription.objects.select_related("school", "plan"),
        pk=pk,
    )

    payments_qs = (
        Payment.objects.filter(subscription=subscription)
        .select_related("requested_plan", "created_by")
        .order_by("-created_at")
    )

    period_start = getattr(subscription, "start_date", None)
    has_payment_for_period = False
    if period_start is not None:
        has_payment_for_period = payments_qs.filter(
            status__in=[Payment.Status.PENDING, Payment.Status.APPROVED],
            payment_date__gte=period_start,
        ).exists()

    next_url = _safe_next_url(request.GET.get("next"))

    payments_list = list(payments_qs[:41])
    has_more = len(payments_list) > 40
    if has_more:
        payments_list = payments_list[:40]
    payments_count = payments_qs.count() if has_more else len(payments_list)
    approved_total = payments_qs.filter(status=Payment.Status.APPROVED, amount__gt=0).aggregate(total=Sum("amount")).get("total") or 0
    refund_total = payments_qs.filter(status=Payment.Status.APPROVED, amount__lt=0).aggregate(total=Sum("amount")).get("total") or 0
    pending_total = payments_qs.filter(status=Payment.Status.PENDING).aggregate(total=Sum("amount")).get("total") or 0

    plan_price = getattr(getattr(subscription, "plan", None), "price", 0) or 0
    try:
        outstanding_amount = max(plan_price - approved_total, 0)
    except Exception:
        outstanding_amount = 0

    ctx = {
        "subscription": subscription,
        "payments": payments_list,
        "payments_count": payments_count,
        "has_payment_for_period": has_payment_for_period,
        "next_url": next_url,
        "approved_total": approved_total,
        "refund_total": -refund_total,
        "pending_total": pending_total,
        "outstanding_amount": outstanding_amount,
    }
    return render(request, "reports/platform_subscription_detail.html", ctx)


@login_required(login_url="reports:login")
@user_passes_test(lambda u: getattr(u, "is_superuser", False), login_url="reports:login")
@require_http_methods(["POST"])
def platform_subscription_delete(request: HttpRequest, pk: int) -> HttpResponse:
    subscription = get_object_or_404(SchoolSubscription.objects.select_related("school", "plan"), pk=pk)

    reason = (request.POST.get("reason") or "").strip()
    refund_raw = (request.POST.get("refund_amount") or "").strip()
    if not reason:
        messages.error(request, "سبب الإلغاء مطلوب لإلغاء الاشتراك.")
        next_url = _safe_next_url(request.POST.get("next") or request.GET.get("next"))
        return redirect(next_url or "reports:platform_subscriptions_list")

    try:
        today = timezone.localdate()
        school_name = subscription.school.name

        with transaction.atomic():
            subscription.is_active = False
            subscription.end_date = today
            if getattr(subscription, "canceled_at", None) is None:
                subscription.canceled_at = timezone.now()
            subscription.cancel_reason = reason

            subscription.save(update_fields=["is_active", "end_date", "canceled_at", "cancel_reason", "updated_at"])

            # ✅ سجل مالي/سجل عمليات المدرسة:
            # نُسجل حدث الإلغاء نفسه كعملية (cancelled) حتى يظهر في:
            # - صفحة المالية (ضمن تبويب cancelled)
            # - صفحة "سجل العمليات السابقة" للمدرسة
            # ولا يؤثر على إجمالي الإيرادات (لأنه مبلغ 0 وبحالة cancelled).
            try:
                exists_cancel_event = Payment.objects.filter(
                    subscription=subscription,
                    status=Payment.Status.CANCELLED,
                    payment_date=today,
                    amount=0,
                ).exists()
                if not exists_cancel_event:
                    Payment.objects.create(
                        school=subscription.school,
                        subscription=subscription,
                        requested_plan=subscription.plan,
                        amount=0,
                        receipt_image=None,
                        payment_date=today,
                        status=Payment.Status.CANCELLED,
                        notes=(
                            "تم إلغاء الاشتراك بواسطة إدارة المنصة.\n"
                            f"سبب الإلغاء: {reason}"
                        ),
                        created_by=request.user,
                    )
            except Exception:
                logger.exception("Failed to record subscription cancellation event")

            # ✅ المالية:
            # - عند الإلغاء: نُلغي فقط المدفوعات المعلّقة لهذه الفترة حتى لا يتم اعتمادها لاحقاً بالخطأ.
            # - خيار إضافي: "استرجاع مبلغ" (كامل/جزئي) عبر تسجيل عملية مالية سالبة (approved)
            #   بحيث يظهر الاسترجاع ويخصم من إجمالي المالية.
            try:
                period_start = getattr(subscription, "start_date", None)

                # 1) إلغاء المعلّق فقط
                pending_qs = Payment.objects.filter(
                    subscription=subscription,
                    status=Payment.Status.PENDING,
                )
                if period_start:
                    pending_qs = pending_qs.filter(payment_date__gte=period_start)

                cancel_note = f"تم إلغاء الاشتراك: {reason}"
                for p in pending_qs.only("id", "status", "notes"):
                    p.status = Payment.Status.CANCELLED
                    p.notes = (f"{p.notes}\n" if (p.notes or "").strip() else "") + cancel_note
                    p.save(update_fields=["status", "notes", "updated_at"])

                # 2) استرجاع مبلغ (اختياري)
                if refund_raw:
                    from decimal import Decimal, InvalidOperation
                    from django.db.models import Sum
                    from django.db.models.functions import Coalesce

                    raw = refund_raw.strip().lower()

                    approved_qs = Payment.objects.filter(
                        subscription=subscription,
                        status=Payment.Status.APPROVED,
                    )
                    if period_start:
                        approved_qs = approved_qs.filter(payment_date__gte=period_start)

                    net_paid = approved_qs.aggregate(total=Coalesce(Sum("amount"), Decimal("0"))).get("total")
                    try:
                        net_paid = Decimal(str(net_paid or "0"))
                    except Exception:
                        net_paid = Decimal("0")

                    max_refund = net_paid if net_paid > 0 else Decimal("0")
                    refund_amount = Decimal("0")

                    if raw in {"full", "كامل", "كاملًا", "كاملا", "استرجاع كامل", "استرجاع كاملًا"}:
                        refund_amount = max_refund
                    else:
                        # السماح بأرقام مثل 100 أو 100.50 أو 100,50
                        try:
                            normalized = raw.replace(",", ".")
                            refund_amount = Decimal(normalized)
                        except (InvalidOperation, ValueError):
                            refund_amount = Decimal("0")

                    if refund_amount < 0:
                        refund_amount = Decimal("0")
                    if refund_amount > max_refund:
                        refund_amount = max_refund

                    # منع الاسترجاع المكرر لنفس اليوم/المبلغ (تحصين بسيط)
                    if refund_amount > 0:
                        exists_refund = Payment.objects.filter(
                            subscription=subscription,
                            status=Payment.Status.APPROVED,
                            amount=-refund_amount,
                            payment_date=today,
                        ).exists()

                        if not exists_refund:
                            Payment.objects.create(
                                school=subscription.school,
                                subscription=subscription,
                                requested_plan=subscription.plan,
                                amount=-refund_amount,
                                receipt_image=None,
                                payment_date=today,
                                status=Payment.Status.APPROVED,
                                notes=(
                                    f"استرجاع مبلغ: {refund_amount} ريال.\n"
                                    f"سبب الإلغاء: {reason}"
                                ),
                                created_by=request.user,
                            )
            except Exception:
                logger.exception("Failed to cancel payments for cancelled subscription")

        messages.success(request, f"تم إلغاء اشتراك مدرسة {school_name}.")
    except Exception:
        logger.exception("platform_subscription_delete failed")
        messages.error(request, "حدث خطأ غير متوقع أثناء إلغاء الاشتراك.")

    next_url = _safe_next_url(request.POST.get("next") or request.GET.get("next"))
    return redirect(next_url or "reports:platform_subscriptions_list")


@login_required(login_url="reports:login")
@user_passes_test(lambda u: getattr(u, "is_superuser", False), login_url="reports:login")
def platform_plans_list(request: HttpRequest) -> HttpResponse:
    plans = list(
        SubscriptionPlan.objects.all().order_by(
            "-is_active",
            "max_teachers",
            "days_duration",
            "price",
            "id",
        )
    )
    paired_plans = {}

    for plan in plans:
        if plan.price <= 0:
            plan.period_key = "trial"
            plan.period_label = "تجربة مجانية"
            months = None
        elif 20 <= plan.days_duration <= 44:
            plan.period_key = "monthly"
            plan.period_label = "شهر"
            months = 1
        elif plan.days_duration >= 300:
            plan.period_key = "annual"
            plan.period_label = "سنة"
            months = 12
        elif plan.days_duration >= 45:
            plan.period_key = "semiannual"
            plan.period_label = "6 أشهر"
            months = 6
        else:
            plan.period_key = "custom"
            plan.period_label = f"{plan.days_duration} يوم"
            months = None

        plan.monthly_equivalent = None
        if months:
            plan.monthly_equivalent = (plan.price / Decimal(months)).quantize(
                Decimal("1"),
                rounding=ROUND_HALF_UP,
            )

        plan.feature_lines = [
            line.strip().lstrip("-*•▪●‣").strip()
            for line in (plan.description or "").replace("\r", "").split("\n")
            if line.strip()
        ][:4]
        plan.is_recommended = bool(plan.price > 0 and plan.max_teachers == 50)
        plan.annual_savings = None
        plan.annual_discount_percent = None
        paired_plans[(plan.max_teachers, plan.period_key)] = plan

    renewal_catalog = _renewal_plan_catalog()
    catalog_plan_ids = {
        option["plan"].id
        for group in renewal_catalog
        for option in group["options"]
    }
    other_plans = [plan for plan in plans if plan.id not in catalog_plan_ids]

    for plan in plans:
        if plan.period_key != "annual":
            continue
        monthly = paired_plans.get((plan.max_teachers, "monthly"))
        if monthly is None:
            continue
        comparison_price = monthly.price * 12
        savings = max(Decimal("0"), comparison_price - plan.price)
        plan.annual_savings = savings
        if comparison_price > 0:
            plan.annual_discount_percent = int(
                ((savings / comparison_price) * 100).quantize(
                    Decimal("1"),
                    rounding=ROUND_HALF_UP,
                )
            )

    active_plans = [plan for plan in plans if plan.is_active]
    paid_plans = [plan for plan in active_plans if plan.price > 0]
    annual_discounts = [
        plan.annual_discount_percent
        for plan in active_plans
        if plan.annual_discount_percent is not None
    ]
    capacities = {
        plan.max_teachers
        for plan in paid_plans
        if plan.max_teachers > 0
    }
    stats = {
        "active_count": len(active_plans),
        "paid_count": len(paid_plans),
        "monthly_count": sum(plan.period_key == "monthly" for plan in paid_plans),
        "semiannual_count": sum(plan.period_key == "semiannual" for plan in paid_plans),
        "annual_count": sum(plan.period_key == "annual" for plan in paid_plans),
        "capacity_count": len(capacities),
        "annual_discount_max": max(annual_discounts, default=0),
    }

    flexible_catalog = build_flexible_pricing_catalog(plans=active_plans)

    return render(
        request,
        "reports/platform_plans.html",
        {
            "plans": plans,
            "renewal_catalog": renewal_catalog,
            "other_plans": other_plans,
            "stats": stats,
            "flexible_pricing_catalog": flexible_catalog,
            "flexible_pricing_json": serialize_flexible_pricing_catalog(flexible_catalog),
            "pricing_warnings": _anchor_pricing_warnings(active_plans),
        },
    )


def _anchor_pricing_warnings(active_plans) -> list[str]:
    """Flag anchor edits that would break the interpolated pricing model.

    Prices between the anchors are interpolated, so the model only holds if the
    price rises with capacity and every paid anchor grants the same
    entitlements. An admin editing one anchor here can silently create a band
    where a school pays more for less — this surfaces that before schools hit it.
    """
    paid = [
        plan
        for plan in active_plans
        if Decimal(getattr(plan, "price", 0) or 0) > 0
        and int(getattr(plan, "max_teachers", 0) or 0) > 0
        and period_key_for_days(getattr(plan, "days_duration", 0))
    ]
    if not paid:
        return []

    warnings: list[str] = []

    entitlements = {
        "مستوى الدعم": {(getattr(plan, "support_level", "") or "") for plan in paid},
        "جلسات الإعداد": {int(getattr(plan, "onboarding_sessions", 0) or 0) for plan in paid},
        "الأرشيف المشمول": {
            int(getattr(plan, "included_archive_storage_gb", 0) or 0) for plan in paid
        },
    }
    for label, values in entitlements.items():
        if len(values) > 1:
            warnings.append(
                f"«{label}» غير متطابق بين الباقات المرجعية ({', '.join(str(v) for v in sorted(values, key=str))}). "
                "الأسعار بين المراجع محسوبة بالاستيفاء، فاختلاف المزايا يخلق سعة يدفع فيها العميل أكثر ويحصل على أقل."
            )

    by_period: dict[str, list] = {}
    for plan in paid:
        by_period.setdefault(period_key_for_days(plan.days_duration), []).append(plan)
    for _period_key, plans_in_period in by_period.items():
        ordered = sorted(plans_in_period, key=lambda p: int(p.max_teachers or 0))
        for lower, upper in pairwise(ordered):
            if Decimal(upper.price) <= Decimal(lower.price):
                warnings.append(
                    f"سعر «{upper.name}» ({upper.price}) ليس أعلى من «{lower.name}» ({lower.price}) "
                    "رغم أن سعته أكبر؛ سيؤدي ذلك إلى منحنى أسعار غير منطقي في الصفحة الرئيسية."
                )

    return warnings


@login_required(login_url="reports:login")
@user_passes_test(lambda u: getattr(u, "is_superuser", False), login_url="reports:login")
def platform_archive_addons_list(request: HttpRequest) -> HttpResponse:
    """إدارة ملحق الأرشفة المدفوع كإضافة مستقلة عن الاشتراك."""
    today = timezone.localdate()
    status = (request.GET.get("status") or "all").strip().lower()
    q = _clean_query_value(request.GET.get("q"))

    addons = SchoolArchiveAddon.objects.select_related("school").order_by("school__name", "id")
    if q:
        addons = addons.filter(Q(school__name__icontains=q) | Q(school__code__icontains=q))

    if status == "active":
        addons = addons.filter(is_enabled=True).filter(Q(end_date__isnull=True) | Q(end_date__gte=today), start_date__lte=today)
    elif status == "disabled":
        addons = addons.filter(is_enabled=False)
    elif status == "expired":
        addons = addons.filter(is_enabled=True, end_date__lt=today)

    stats_base = SchoolArchiveAddon.objects.all()
    stats = {
        "total": stats_base.count(),
        "active": stats_base.filter(is_enabled=True).filter(
            Q(end_date__isnull=True) | Q(end_date__gte=today),
            start_date__lte=today,
        ).count(),
        "expired": stats_base.filter(is_enabled=True, end_date__lt=today).count(),
        "disabled": stats_base.filter(is_enabled=False).count(),
        "schools_without_addon": School.objects.filter(archive_addon__isnull=True).count(),
    }

    page_obj = svc_paginate(addons, per_page=30, page=request.GET.get("page", 1))
    for addon in page_obj:
        # رقمُ استهلاكٍ قديم يُعرض بثقة أخطرُ من غيابه: القرار يُبنى عليه.
        with soft_fail("billing.sync_addon_storage", school_id=addon.school_id):
            sync_school_archive_storage_usage(addon.school)
            addon.refresh_from_db(fields=["storage_used_bytes", "updated_at"])
    return render(
        request,
        "reports/platform_archive_addons.html",
        {
            "addons": page_obj,
            "page_obj": page_obj,
            "stats": stats,
            "status": status,
            "q": q,
            "today": today,
            "results_count": addons.count(),
            "qs": _clean_query_params(request.GET),
        },
    )


@login_required(login_url="reports:login")
@user_passes_test(lambda u: getattr(u, "is_superuser", False), login_url="reports:login")
@require_http_methods(["GET", "POST"])
def platform_archive_addon_form(request: HttpRequest, pk: Optional[int] = None) -> HttpResponse:
    addon = get_object_or_404(SchoolArchiveAddon.objects.select_related("school"), pk=pk) if pk else None
    form = SchoolArchiveAddonForm(request.POST or None, instance=addon)

    if request.method == "POST":
        if form.is_valid():
            form.save()
            messages.success(request, "تم حفظ ملحق الأرشيف بنجاح.")
            return redirect("reports:platform_archive_addons_list")
        messages.error(request, "تعذّر الحفظ. تحقق من الحقول.")

    return render(
        request,
        "reports/platform_archive_addon_form.html",
        {
            "form": form,
            "addon": addon,
        },
    )


@login_required(login_url="reports:login")
@user_passes_test(lambda u: getattr(u, "is_superuser", False), login_url="reports:login")
@require_http_methods(["POST"])
def platform_archive_addon_toggle(request: HttpRequest, pk: int) -> HttpResponse:
    addon = get_object_or_404(SchoolArchiveAddon, pk=pk)
    addon.is_enabled = not bool(addon.is_enabled)
    addon.save(update_fields=["is_enabled", "updated_at"])
    messages.success(request, "تم تفعيل ملحق الأرشيف." if addon.is_enabled else "تم إيقاف ملحق الأرشيف.")
    next_url = _safe_next_url(request.POST.get("next") or request.GET.get("next"))
    return redirect(next_url or "reports:platform_archive_addons_list")


@login_required(login_url="reports:login")
@user_passes_test(lambda u: getattr(u, "is_superuser", False), login_url="reports:login")
def platform_payments_list(request: HttpRequest) -> HttpResponse:
    status = (request.GET.get("status") or "active").strip().lower()
    if status not in {"active", "pending", "refunds", "cancelled", "all"}:
        status = "active"
    q = _clean_query_value(request.GET.get("q"))
    start_date = _parse_date_safe(request.GET.get("start_date"))
    end_date = _parse_date_safe(request.GET.get("end_date"))

    base_qs = Payment.objects.select_related(
        "school", "requested_plan", "subscription", "created_by"
    ).order_by("-created_at")

    # نطاق ثابت يحترم البحث والتاريخ فقط (لا يتأثر بتبويب الحالة) — تُحسب عليه
    # المؤشرات المالية وعدّادات التبويبات حتى تبقى ثابتة عند التنقل بين التبويبات.
    scope_qs = base_qs
    if q:
        query_filter = Q(school__name__icontains=q) | Q(school__code__icontains=q) | Q(notes__icontains=q)
        scope_qs = scope_qs.filter(query_filter)
    if start_date is not None:
        scope_qs = scope_qs.filter(payment_date__gte=start_date)
    if end_date is not None:
        scope_qs = scope_qs.filter(payment_date__lte=end_date)

    # جدول العمليات = النطاق + تبويب الحالة.
    # ملاحظة: الاسترجاعات = عمليات مقبولة بمبلغ سالب.
    if status == "pending":
        payments = scope_qs.filter(status=Payment.Status.PENDING).order_by(
            Case(
                When(payment_method=Payment.Method.BANK_TRANSFER, then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            ),
            "created_at",
        )
    elif status == "refunds":
        payments = scope_qs.filter(status=Payment.Status.APPROVED, amount__lt=0)
    elif status == "cancelled":
        payments = scope_qs.filter(status=Payment.Status.CANCELLED)
    elif status == "all":
        payments = scope_qs
    else:
        payments = scope_qs.exclude(status=Payment.Status.CANCELLED)

    # المؤشرات المالية (مستقلة عن تبويب الحالة، ضمن نطاق البحث/التاريخ)
    stats = scope_qs.aggregate(
        total=Count("id"),
        pending=Count("id", filter=Q(status=Payment.Status.PENDING)),
        approved=Count("id", filter=Q(status=Payment.Status.APPROVED, amount__gt=0)),
        rejected=Count("id", filter=Q(status=Payment.Status.REJECTED)),
        cancelled=Count("id", filter=Q(status=Payment.Status.CANCELLED)),
        refunds=Count("id", filter=Q(status=Payment.Status.APPROVED, amount__lt=0)),
        gross_revenue=Sum("amount", filter=Q(status=Payment.Status.APPROVED, amount__gt=0)),
        refunds_value=Sum("amount", filter=Q(status=Payment.Status.APPROVED, amount__lt=0)),
        pending_value=Sum("amount", filter=Q(status=Payment.Status.PENDING, amount__gt=0)),
    )
    net_revenue = (stats.get("gross_revenue") or 0) + (stats.get("refunds_value") or 0)

    # عدّادات التبويبات (مشتقة من نفس النطاق دون استعلامات إضافية)
    total_count = stats.get("total") or 0
    cancelled_count = stats.get("cancelled") or 0
    tab_counts = {
        "active": total_count - cancelled_count,
        "pending": stats.get("pending") or 0,
        "refunds": stats.get("refunds") or 0,
        "cancelled": cancelled_count,
        "all": total_count,
    }

    paginator = Paginator(payments, 50)
    page_obj = paginator.get_page(request.GET.get("page"))

    params = request.GET.copy()
    if "page" in params:
        params.pop("page")
    # معاملات الفلترة بدون status — لتمرير البحث/التاريخ مع روابط التبويبات
    filter_params = request.GET.copy()
    for key in ("page", "status"):
        filter_params.pop(key, None)

    ctx = {
        "payments": page_obj,
        "page_obj": page_obj,
        "status": status,
        "q": q,
        "start_date": start_date.isoformat() if start_date else "",
        "end_date": end_date.isoformat() if end_date else "",
        "qs": params.urlencode(),
        "filter_qs": filter_params.urlencode(),
        "table_count": payments.count(),
        "tab_counts": tab_counts,
        "payments_total": total_count,
        "payments_pending": stats["pending"] or 0,
        "payments_approved": stats["approved"] or 0,
        "payments_rejected": stats["rejected"] or 0,
        "payments_cancelled": cancelled_count,
        "payments_refunds": stats["refunds"] or 0,
        "gross_revenue": stats.get("gross_revenue") or 0,
        "refunds_value": -(stats.get("refunds_value") or 0),
        "pending_value": stats.get("pending_value") or 0,
        "net_revenue": net_revenue,
    }
    return render(request, "reports/platform_payments.html", ctx)


@login_required(login_url="reports:login")
@user_passes_test(lambda u: getattr(u, "is_superuser", False), login_url="reports:login")
def platform_payment_detail(request: HttpRequest, pk: int) -> HttpResponse:
    payment = get_object_or_404(
        Payment.objects.select_related(
            "school", "subscription", "requested_plan", "discount_code"
        ),
        pk=pk,
    )

    # ── بنود الطلب الموحّد للمدرسة نفسها (نفس batch_ref) ──
    batch_payments = []
    if payment.batch_ref:
        batch_payments = list(
            Payment.objects.filter(school=payment.school, batch_ref=payment.batch_ref)
            .select_related("school", "requested_plan")
            .order_by("id")
        )

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
        today = timezone.localdate()
        pricing = _archive_pricing()

        settled_gateway_statuses = {
            Payment.Method.MOYASAR: {"paid"},
            Payment.Method.TAMARA: {"fully_captured"},
        }
        gateway_unsettled = (
            payment.payment_method in settled_gateway_statuses
            and payment.gateway_status
            not in settled_gateway_statuses[payment.payment_method]
        )
        if gateway_unsettled:
            requested_status = (request.POST.get("status") or "").strip()
            if action == "approve_batch" or requested_status == Payment.Status.APPROVED:
                messages.error(request, "لا يمكن اعتماد دفعة إلكترونية يدويًا قبل تأكيد التحصيل من البوابة.")
                return redirect("reports:platform_payment_detail", pk=pk)

        # ===== (أ) اعتماد الطلب الموحّد كاملاً بضغطة واحدة =====
        if action == "approve_batch" and payment.batch_ref:
            pending = [p for p in batch_payments if p.status == Payment.Status.PENDING]
            if not pending:
                messages.info(request, "لا توجد عناصر قيد المراجعة في هذا الطلب.")
                return redirect("reports:platform_payment_detail", pk=pk)

            pending.sort(key=lambda p: _PURPOSE_APPLY_ORDER.get(p.purpose, 99))
            try:
                with transaction.atomic():
                    for p in pending:
                        p.status = Payment.Status.APPROVED
                        p.save(update_fields=["status", "updated_at"])
                        _apply_payment_effects(p, today, pricing)
            except _ApprovalError as exc:
                messages.error(request, f"تعذّر اعتماد الطلب: {exc}")
                return redirect("reports:platform_payment_detail", pk=pk)

            messages.success(request, f"تم اعتماد الطلب الموحّد كاملاً ({len(pending)} عنصر) وتفعيل بنوده تلقائياً.")
            return redirect("reports:platform_payment_detail", pk=pk)

        # ===== (ب) تحديث حالة عملية واحدة =====
        prev_status = payment.status
        new_status = request.POST.get("status")
        notes = request.POST.get("notes")

        if new_status in Payment.Status.values:
            payment.status = new_status
        if notes is not None:
            payment.notes = notes

        try:
            with transaction.atomic():
                payment.save()
                if prev_status != Payment.Status.APPROVED and payment.status == Payment.Status.APPROVED:
                    level, msg = _apply_payment_effects(payment, today, pricing)
                    getattr(messages, level)(request, msg)
                if payment.status in {Payment.Status.REJECTED, Payment.Status.CANCELLED}:
                    # رفضُ الطلب يحرّر حجز كود الخصم ليعود استخدامه إلى الرصيد.
                    released = release_dead_redemptions(payment_id=payment.pk)
                    if released:
                        messages.info(
                            request,
                            "أُعيد استخدام كود الخصم المرتبط بهذه الدفعة إلى رصيد الكود.",
                        )
        except _ApprovalError as exc:
            messages.error(request, str(exc))
            return redirect("reports:platform_payment_detail", pk=pk)

        messages.success(request, "تم تحديث حالة الدفع بنجاح.")
        return redirect("reports:platform_payment_detail", pk=pk)

    batch_total = sum((p.amount for p in batch_payments), Decimal("0")) if batch_payments else None
    batch_has_pending = any(p.status == Payment.Status.PENDING for p in batch_payments)

    return render(
        request,
        "reports/platform_payment_detail.html",
        {
            "payment": payment,
            "batch_payments": batch_payments,
            "batch_total": batch_total,
            "batch_has_pending": batch_has_pending,
        },
    )


@login_required(login_url="reports:login")
@user_passes_test(lambda u: getattr(u, "is_superuser", False), login_url="reports:login")
def platform_tickets_list(request: HttpRequest) -> HttpResponse:
    query = request.GET.get("q", "").strip()
    status_filter = request.GET.get("status", "").strip()

    # تذاكر الدعم الفني فقط (platform tickets) — نطاق ثابت يحترم البحث فقط
    scope = (
        Ticket.objects.filter(is_platform=True)
        .select_related("creator", "school")
        .order_by("-created_at")
    )
    if query:
        scope = scope.filter(
            Q(school__name__icontains=query) |
            Q(school__code__icontains=query) |
            Q(title__icontains=query) |
            Q(id__icontains=query)
        )

    # عدّادات الحالات (ضمن نطاق البحث، مستقلة عن التبويب المختار)
    tab_counts = scope.aggregate(
        all=Count("id"),
        open=Count("id", filter=Q(status=Ticket.Status.OPEN)),
        in_progress=Count("id", filter=Q(status=Ticket.Status.IN_PROGRESS)),
        done=Count("id", filter=Q(status=Ticket.Status.DONE)),
        rejected=Count("id", filter=Q(status=Ticket.Status.REJECTED)),
    )

    tickets = scope
    if status_filter and status_filter != "all":
        tickets = tickets.filter(status=status_filter)

    paginator = Paginator(tickets, 30)
    page_obj = paginator.get_page(request.GET.get("page"))

    return render(request, "reports/platform_tickets.html", {
        "tickets": page_obj,
        "page_obj": page_obj,
        "search_query": query,
        "current_status": status_filter,
        "tab_counts": tab_counts,
    })


@login_required(login_url="reports:login")
@user_passes_test(lambda u: getattr(u, "is_superuser", False), login_url="reports:login")
def platform_plan_form(request: HttpRequest, pk: Optional[int] = None) -> HttpResponse:
    """إضافة أو تعديل خطة اشتراك"""
    plan = None
    if pk:
        plan = get_object_or_404(SubscriptionPlan, pk=pk)
    
    if request.method == "POST":
        form = SubscriptionPlanForm(request.POST, instance=plan)
        if form.is_valid():
            form.save()
            messages.success(request, "تم حفظ الخطة بنجاح.")
            return redirect("reports:platform_plans_list")
        else:
            messages.error(request, "يرجى تصحيح الأخطاء أدناه.")
    else:
        form = SubscriptionPlanForm(instance=plan)
    
    return render(request, "reports/platform_plan_form.html", {"form": form, "plan": plan})


@login_required(login_url="reports:login")
@user_passes_test(lambda u: getattr(u, "is_superuser", False), login_url="reports:login")
@require_http_methods(["POST"])
def platform_plan_delete(request: HttpRequest, pk: int) -> HttpResponse:
    plan = get_object_or_404(SubscriptionPlan, pk=pk)

    try:
        plan_name = plan.name
        plan.delete()
        messages.success(request, f"تم حذف الخطة: {plan_name}.")
    except ProtectedError:
        messages.error(request, "لا يمكن حذف هذه الخطة لأنها مرتبطة باشتراكات مدارس حالياً.")
    except Exception:
        logger.exception("platform_plan_delete failed")
        messages.error(request, "حدث خطأ غير متوقع أثناء حذف الخطة.")

    next_url = _safe_next_url(request.POST.get("next") or request.GET.get("next"))
    return redirect(next_url or "reports:platform_plans_list")


@login_required(login_url="reports:login")
@user_passes_test(lambda u: getattr(u, "is_superuser", False), login_url="reports:login")
def platform_subscription_form(request: HttpRequest, pk: Optional[int] = None) -> HttpResponse:
    """إضافة اشتراك مدرسة (تم إلغاء تعديل الباقة/الاشتراك نهائياً)."""
    subscription = None
    # ✅ تم إلغاء التعديل نهائياً: أي محاولة لفتح رابط قديم للتعديل تُرفض.
    if pk is not None:
        raise Http404
    
    if request.method == "POST":
        # ✅ إذا كانت المدرسة لديها اشتراك سابق (ملغي/منتهي) فلا ننشئ سجل جديد (OneToOne)
        # بل نجدد/نفعّل الاشتراك الموجود لتفادي خطأ "المدرسة موجودة مسبقاً".
        school_id_raw = (request.POST.get("school") or "").strip()
        try:
            school_id = int(school_id_raw)
        except Exception:
            school_id = None

        if school_id is not None:
            existing = (
                SchoolSubscription.objects.filter(school_id=school_id)
                .select_related("school", "plan")
                .first()
            )
            if existing is not None:
                # إن كان الاشتراك ملغي/منتهي: نجدد/نفعّل نفس السجل (OneToOne)
                # لكن نسمح بتغيير الباقة حسب اختيار الإدارة (إن لزم).
                if bool(getattr(existing, "is_cancelled", False)) or bool(getattr(existing, "is_expired", False)):
                    from datetime import timedelta

                    today = timezone.localdate()
                    prev_plan_id = getattr(existing, "plan_id", None)
                    form = SchoolSubscriptionForm(request.POST, instance=existing, allow_plan_change=True)
                    if form.is_valid():
                        subscription_obj = form.save(commit=False)

                        # عند التجديد: فعّل وامسح بيانات الإلغاء
                        subscription_obj.is_active = True
                        if getattr(subscription_obj, "canceled_at", None) is not None:
                            subscription_obj.canceled_at = None
                        if (getattr(subscription_obj, "cancel_reason", "") or "").strip():
                            subscription_obj.cancel_reason = ""

                        # إذا لم تتغير الباقة، فاعتبرها تجديداً أيضاً واضبط التواريخ لليوم
                        # (لأن منطق model.save يعيد الحساب فقط عند تغيير plan).
                        if getattr(subscription_obj, "plan_id", None) == prev_plan_id:
                            days = int(getattr(getattr(subscription_obj, "plan", None), "days_duration", 0) or 0)
                            subscription_obj.start_date = today
                            subscription_obj.end_date = today if days <= 0 else today + timedelta(days=days - 1)

                        subscription_obj.save()

                        # تحصين مالي: أي دفعات pending قديمة لا يجب أن تبقى عالقة بعد التجديد.
                        # وتعثّرُ التحصين نفسه تحصينٌ لم يقع — يجب أن يُرى.
                        with soft_fail(
                            "billing.cancel_stale_pending_payments",
                            subscription_id=getattr(subscription_obj, "pk", None),
                        ):
                            Payment.objects.filter(
                                subscription=subscription_obj,
                                status=Payment.Status.PENDING,
                                created_at__date__lt=subscription_obj.start_date,
                            ).update(
                                status=Payment.Status.CANCELLED,
                                notes="تم إلغاء هذه العملية تلقائياً بسبب تجديد/تغيير الاشتراك.",
                            )

                        _record_subscription_payment_if_missing(
                            subscription=subscription_obj,
                            actor=request.user,
                            note="تم تجديد الاشتراك (مع تحديث الباقة عند الحاجة) وتسجيل الدفعة بواسطة إدارة المنصة.",
                            force=True,
                        )

                        messages.success(
                            request,
                            f"تم تفعيل/تجديد اشتراك مدرسة {subscription_obj.school.name} حتى {subscription_obj.end_date:%Y-%m-%d}.",
                        )
                        return redirect("reports:platform_subscriptions_list")
                    else:
                        messages.error(request, "يرجى تصحيح الأخطاء أدناه.")
                        return render(request, "reports/platform_subscription_add.html", {"form": form})

                messages.info(
                    request,
                    "هذه المدرسة لديها اشتراك قائم بالفعل. استخدم زر (تجديد) من قائمة الاشتراكات.",
                )
                return redirect("reports:platform_subscriptions_list")

        was_existing = bool(subscription and getattr(subscription, "pk", None))
        prev_is_active = bool(getattr(subscription, "is_active", False)) if subscription else False
        form = SchoolSubscriptionForm(request.POST, instance=subscription)
        if form.is_valid():
            subscription_obj = form.save()

            # ✅ المالية:
            # - عند إنشاء اشتراك جديد من لوحة المنصة: نسجّل دفعة (approved) لتظهر في المالية.
            # - عند تعديل اشتراك موجود: لا نسجّل دفعة إلا إذا كان غير نشط ثم تم تفعيله.
            created_payment = False
            try:
                became_active = (not prev_is_active) and bool(getattr(subscription_obj, "is_active", False))
                if (not was_existing) or became_active:
                    created_payment = _record_subscription_payment_if_missing(
                        subscription=subscription_obj,
                        actor=request.user,
                        note="تم تسجيل الدفعة يدويًا بواسطة إدارة المنصة.",
                        force=False,
                    )
            except Exception:
                created_payment = False

            if created_payment:
                messages.success(request, "تم حفظ الاشتراك وتسجيل عملية الدفع بنجاح.")
            else:
                messages.success(request, "تم حفظ الاشتراك بنجاح.")
            return redirect("reports:platform_subscriptions_list")
        else:
            messages.error(request, "يرجى تصحيح الأخطاء أدناه.")
    else:
        form = SchoolSubscriptionForm(instance=subscription)

    return render(request, "reports/platform_subscription_add.html", {"form": form})


@login_required(login_url="reports:login")
@user_passes_test(lambda u: getattr(u, "is_superuser", False), login_url="reports:login")
@require_http_methods(["POST"])
def platform_subscription_renew(request: HttpRequest, pk: int) -> HttpResponse:
    """تجديد اشتراك مدرسة مباشرةً من اليوم (ميلادي).

    - يضبط start_date = اليوم
    - يضبط end_date = اليوم + (plan.days_duration - 1)
    - يفعّل is_active=True

    هذا المسار مخصص لمالك النظام فقط لتسهيل التجديد من صفحة الاشتراكات.
    """
    subscription = get_object_or_404(SchoolSubscription.objects.select_related("plan", "school"), pk=pk)

    from datetime import timedelta

    today = timezone.localdate()
    subscription.start_date = today
    days = int(getattr(subscription.plan, "days_duration", 0) or 0)
    if days <= 0:
        subscription.end_date = today
    else:
        subscription.end_date = today + timedelta(days=days - 1)

    subscription.is_active = True
    # عند التجديد: امسح بيانات الإلغاء
    if getattr(subscription, "canceled_at", None) is not None:
        subscription.canceled_at = None
    if getattr(subscription, "cancel_reason", ""):
        subscription.cancel_reason = ""
    subscription.save()

    created_payment = _record_subscription_payment_if_missing(
        subscription=subscription,
        actor=request.user,
        note="تم تجديد الاشتراك وتسجيل الدفعة يدويًا بواسطة إدارة المنصة.",
        force=True,
    )
    if created_payment:
        messages.success(
            request,
            f"تم تجديد اشتراك مدرسة {subscription.school.name} حتى {subscription.end_date:%Y-%m-%d}، وتم تسجيل عملية الدفع.",
        )
    else:
        messages.success(request, f"تم تجديد اشتراك مدرسة {subscription.school.name} حتى {subscription.end_date:%Y-%m-%d}.")

    next_url = _safe_next_url(request.POST.get("next") or request.GET.get("next"))
    return redirect(next_url or "reports:platform_subscriptions_list")


@login_required(login_url="reports:login")
@user_passes_test(lambda u: getattr(u, "is_superuser", False), login_url="reports:login")
@require_http_methods(["POST"])
def platform_subscription_record_payment(request: HttpRequest, pk: int) -> HttpResponse:
    """تسجيل دفعة يدوية لاشتراك موجود بدون تغيير تواريخه."""
    subscription = get_object_or_404(SchoolSubscription.objects.select_related("plan", "school"), pk=pk)

    ok = _record_subscription_payment_if_missing(
        subscription=subscription,
        actor=request.user,
        note="تم تسجيل الدفعة يدويًا بواسطة إدارة المنصة.",
    )
    if ok:
        messages.success(request, "تم تسجيل عملية الدفع بنجاح.")
    else:
        messages.info(request, "لا يمكن تسجيل دفعة جديدة (يوجد دفع بالفعل أو الاشتراك غير نشط/مجاني).")

    next_url = _safe_next_url(request.POST.get("next") or request.GET.get("next"))
    return redirect(next_url or "reports:platform_subscriptions_list")


# =====================================================================
# إدارة السنوات الدراسية (مدير النظام) — مصدر مركزي لخيارات المدارس
# =====================================================================
@login_required(login_url="reports:login")
@user_passes_test(lambda u: getattr(u, "is_superuser", False), login_url="reports:login")
@require_http_methods(["GET", "POST"])
def platform_academic_years(request: HttpRequest) -> HttpResponse:
    """صفحة تحكم مدير النظام في السنوات الدراسية المتاحة للمدارس."""
    import re
    from ..models import AcademicYear, School

    def _norm(v: str) -> str:
        return (v or "").strip().replace("–", "-").replace("—", "-")

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()

        if action == "add":
            value = _norm(request.POST.get("value"))
            if not re.match(r"^\d{4}-\d{4}$", value):
                messages.error(request, "صيغة السنة يجب أن تكون مثل 1447-1448")
            else:
                start, end = value.split("-", 1)
                if int(end) != int(start) + 1:
                    messages.error(request, "السنة يجب أن تكون بفارق سنة واحدة، مثل 1447-1448")
                else:
                    _, created = AcademicYear.objects.get_or_create(
                        value=value, defaults={"is_active": True, "order": int(start)}
                    )
                    messages.success(request, "تمت إضافة السنة." if created else "السنة موجودة مسبقًا.")
            return redirect("reports:platform_academic_years")

        if action == "generate":
            # توليد السنوات الثلاث القادمة تلقائيًا اعتمادًا على آخر سنة مسجّلة
            existing = list(AcademicYear.objects.values_list("value", flat=True))
            anchor = 0
            for v in existing:
                try:
                    anchor = max(anchor, int(str(v)[:4]))
                except (TypeError, ValueError):
                    # قيمةُ سنةٍ مشوّهة في القاعدة: تُتخطّى ولا تُوقف التوليد.
                    _degraded("platform.malformed_academic_year", value=str(v)[:16])
            if anchor == 0:
                today = timezone.localdate()
                g = today.year + (today.month - 1) / 12.0
                anchor = int(round((g - 622) * 33.0 / 32.0))
            added = 0
            for st in range(anchor, anchor + 4):
                _, created = AcademicYear.objects.get_or_create(
                    value=f"{st}-{st + 1}", defaults={"is_active": True, "order": st}
                )
                added += 1 if created else 0
            messages.success(request, f"تم توليد {added} سنة جديدة." if added else "لا توجد سنوات جديدة لإضافتها.")
            return redirect("reports:platform_academic_years")

        if action == "toggle":
            obj = AcademicYear.objects.filter(pk=request.POST.get("id")).first()
            if obj:
                obj.is_active = not obj.is_active
                obj.save(update_fields=["is_active"])
                messages.success(request, "تم تحديث حالة السنة.")
            return redirect("reports:platform_academic_years")

        if action == "delete":
            obj = AcademicYear.objects.filter(pk=request.POST.get("id")).first()
            if obj:
                used = School.objects.filter(current_academic_year=obj.value).count()
                if used:
                    messages.error(request, f"لا يمكن حذف «{obj.value}» لأنها السنة الحالية لـ {used} مدرسة. عطّلها بدلًا من الحذف.")
                else:
                    obj.delete()
                    messages.success(request, "تم حذف السنة.")
            return redirect("reports:platform_academic_years")

    years = list(AcademicYear.objects.all().order_by("-value"))
    # عدد المدارس التي تعتمد كل سنة كسنة حالية
    usage = {
        row["current_academic_year"]: row["c"]
        for row in School.objects.exclude(current_academic_year="")
        .values("current_academic_year")
        .annotate(c=Count("id"))
    }
    items = [{"obj": y, "schools": usage.get(y.value, 0)} for y in years]
    active_count = sum(1 for y in years if y.is_active)

    return render(
        request,
        "reports/platform_academic_years.html",
        {"items": items, "total": len(years), "active_count": active_count},
    )


PRICING_MATRIX_PERIOD_DAYS = {"1m": 30, "6m": 180, "1y": 365}


def _anchor_plan(capacity: int, period_key: str):
    """Return the stored plan for a capacity/period pair, if it exists."""
    return (
        SubscriptionPlan.objects.filter(
            max_teachers=capacity,
            days_duration=PRICING_MATRIX_PERIOD_DAYS[period_key],
        )
        .order_by("id")
        .first()
    )


def _pricing_matrix_initial(capacities) -> dict:
    initial = {}
    for capacity in capacities:
        for period_key in PRICING_MATRIX_PERIOD_DAYS:
            plan = _anchor_plan(capacity, period_key)
            if plan is not None:
                initial[PricingMatrixForm.field_name(capacity, period_key)] = plan.price
    return initial


def _default_anchor_name(capacity: int, period_key: str) -> str:
    label = {"1m": "شهري", "6m": "6 أشهر", "1y": "سنوي"}[period_key]
    return f"سعة {capacity} معلماً | {label}"


def _default_anchor_description(capacity: int) -> str:
    return "\n".join(
        [
            f"تشغيل كامل للمدرسة حتى {capacity} معلماً",
            "التقارير والإنجاز والطلبات والتعاميم وPDF",
            "دعم بأولوية وجميع مزايا المنصة دون تجزئة",
        ]
    )


@login_required(login_url="reports:login")
@user_passes_test(lambda u: getattr(u, "is_superuser", False), login_url="reports:login")
@require_http_methods(["GET", "POST"])
def platform_pricing_matrix(request: HttpRequest) -> HttpResponse:
    """Maintain the nine anchor prices that drive every published price.

    Editing them one plan at a time made the relationships between them
    invisible, which is how a capacity ended up cheaper than the one below it.
    Here they are validated together and saved in one transaction.
    """
    capacities = list(ANCHOR_CAPACITIES)

    if request.method == "POST":
        form = PricingMatrixForm(request.POST, capacities=capacities)
        if form.is_valid():
            created = 0
            updated = 0
            with transaction.atomic():
                for capacity in capacities:
                    for period_key, days in PRICING_MATRIX_PERIOD_DAYS.items():
                        price = form.price_for(capacity, period_key)
                        plan = _anchor_plan(capacity, period_key)
                        if plan is None:
                            SubscriptionPlan.objects.create(
                                name=_default_anchor_name(capacity, period_key),
                                description=_default_anchor_description(capacity),
                                price=price,
                                days_duration=days,
                                max_teachers=capacity,
                                # Uniform by design — see the invariant in
                                # reports/pricing.py.
                                support_level="priority",
                                onboarding_sessions=0,
                                included_archive_storage_gb=0,
                                is_active=True,
                            )
                            created += 1
                        elif plan.price != price or not plan.is_active:
                            plan.price = price
                            plan.is_active = True
                            plan.save(update_fields=["price", "is_active"])
                            updated += 1

            messages.success(
                request,
                f"تم حفظ مصفوفة الأسعار (أُضيفت {created} وحُدّثت {updated}). "
                "الأسعار البينية أُعيد احتسابها تلقائياً في صفحة الهبوط وصفحة التجديد.",
            )
            return redirect("reports:platform_pricing_matrix")

        messages.error(request, "راجع الأسعار المُعلّمة بالأحمر؛ لم يُحفظ أي تغيير.")
    else:
        form = PricingMatrixForm(
            initial=_pricing_matrix_initial(capacities),
            capacities=capacities,
        )

    active_plans = list(SubscriptionPlan.objects.filter(is_active=True))
    return render(
        request,
        "reports/platform_pricing_matrix.html",
        {
            "form": form,
            "period_labels": [PERIODS[key]["label"] for key in PricingMatrixForm.PERIOD_ORDER],
            "anchor_capacities": capacities,
            "flexible_pricing_catalog": build_flexible_pricing_catalog(plans=active_plans),
            "pricing_warnings": _anchor_pricing_warnings(active_plans),
            "included_features": SUBSCRIPTION_INCLUDED_FEATURES,
            "addon_notes": SUBSCRIPTION_ADDON_NOTES,
        },
    )

# =========================
# أكواد الخصم (Platform Admin)
# =========================

@login_required(login_url="reports:login")
@user_passes_test(lambda u: getattr(u, "is_superuser", False), login_url="reports:login")
def platform_discount_codes_list(request: HttpRequest) -> HttpResponse:
    """قائمة أكواد الخصم مع مؤشراتها: الاستخدامات والرصيد وقيمة الخصومات الممنوحة."""
    today = timezone.localdate()
    status = (request.GET.get("status") or "all").strip().lower()
    q = _clean_query_value(request.GET.get("q"))

    codes = (
        DiscountCode.objects.annotate(
            uses=Count("redemptions", distinct=True),
            granted=Sum("redemptions__amount_discounted"),
        )
        .order_by("-created_at", "-id")
    )
    if q:
        codes = codes.filter(code__icontains=q.upper())

    if status == "usable":
        codes = codes.filter(is_active=True, uses__lt=F("max_uses")).filter(
            Q(valid_from__isnull=True) | Q(valid_from__lte=today),
            Q(valid_until__isnull=True) | Q(valid_until__gte=today),
        )
    elif status == "exhausted":
        codes = codes.filter(uses__gte=F("max_uses"))
    elif status == "expired":
        codes = codes.filter(valid_until__lt=today)
    elif status == "disabled":
        codes = codes.filter(is_active=False)

    stats_base = DiscountCode.objects.annotate(uses=Count("redemptions", distinct=True))
    stats = {
        "total": stats_base.count(),
        "usable": stats_base.filter(is_active=True, uses__lt=F("max_uses"))
        .filter(
            Q(valid_from__isnull=True) | Q(valid_from__lte=today),
            Q(valid_until__isnull=True) | Q(valid_until__gte=today),
        )
        .count(),
        "exhausted": stats_base.filter(uses__gte=F("max_uses")).count(),
        "redemptions": DiscountRedemption.objects.count(),
        "granted_total": DiscountRedemption.objects.aggregate(
            total=Sum("amount_discounted")
        )["total"] or Decimal("0.00"),
    }

    page_obj = svc_paginate(codes, per_page=30, page=request.GET.get("page", 1))
    return render(
        request,
        "reports/platform_discount_codes.html",
        {
            "codes": page_obj,
            "page_obj": page_obj,
            "stats": stats,
            "status": status,
            "q": q,
            "today": today,
            "results_count": codes.count(),
            "qs": _clean_query_params(request.GET),
        },
    )


@login_required(login_url="reports:login")
@user_passes_test(lambda u: getattr(u, "is_superuser", False), login_url="reports:login")
@require_http_methods(["GET", "POST"])
def platform_discount_code_form(request: HttpRequest, pk: Optional[int] = None) -> HttpResponse:
    """إضافة أو تعديل كود خصم."""
    code = get_object_or_404(DiscountCode, pk=pk) if pk else None
    form = DiscountCodeForm(request.POST or None, instance=code)

    if request.method == "POST":
        if form.is_valid():
            obj = form.save(commit=False)
            if obj.pk is None:
                obj.created_by = request.user
            obj.save()
            messages.success(request, f"تم حفظ كود الخصم {obj.code} بنجاح.")
            return redirect("reports:platform_discount_codes_list")
        messages.error(request, "تعذّر الحفظ. تحقق من الحقول.")

    return render(
        request,
        "reports/platform_discount_code_form.html",
        {"form": form, "code": code},
    )


@login_required(login_url="reports:login")
@user_passes_test(lambda u: getattr(u, "is_superuser", False), login_url="reports:login")
def platform_discount_code_detail(request: HttpRequest, pk: int) -> HttpResponse:
    """تفاصيل الكود: من استخدمه، متى، وعلى أي دفعة."""
    code = get_object_or_404(DiscountCode, pk=pk)
    redemptions = list(
        code.redemptions.select_related("school", "payment").order_by("-created_at")
    )
    return render(
        request,
        "reports/platform_discount_code_detail.html",
        {
            "code": code,
            "redemptions": redemptions,
            "granted_total": sum(
                (r.amount_discounted or Decimal("0.00") for r in redemptions),
                Decimal("0.00"),
            ),
        },
    )


@login_required(login_url="reports:login")
@user_passes_test(lambda u: getattr(u, "is_superuser", False), login_url="reports:login")
@require_http_methods(["POST"])
def platform_discount_code_toggle(request: HttpRequest, pk: int) -> HttpResponse:
    code = get_object_or_404(DiscountCode, pk=pk)
    code.is_active = not bool(code.is_active)
    code.save(update_fields=["is_active", "updated_at"])
    messages.success(
        request,
        f"تم تفعيل الكود {code.code}." if code.is_active else f"تم إيقاف الكود {code.code}.",
    )
    next_url = _safe_next_url(request.POST.get("next") or request.GET.get("next"))
    return redirect(next_url or "reports:platform_discount_codes_list")


@login_required(login_url="reports:login")
@user_passes_test(lambda u: getattr(u, "is_superuser", False), login_url="reports:login")
@require_http_methods(["POST"])
def platform_discount_code_delete(request: HttpRequest, pk: int) -> HttpResponse:
    code = get_object_or_404(DiscountCode, pk=pk)
    if code.redemptions.exists():
        # حذف كود مستخدَم يمحو أثره من السجل المالي؛ الإيقاف يمنع استخدامه الجديد.
        messages.error(
            request,
            "لا يمكن حذف كود استُخدم من قبل — أوقفه بدلاً من ذلك ليبقى سجل استخداماته.",
        )
        return redirect("reports:platform_discount_codes_list")
    code_label = code.code
    code.delete()
    messages.success(request, f"تم حذف كود الخصم {code_label}.")
    next_url = _safe_next_url(request.POST.get("next") or request.GET.get("next"))
    return redirect(next_url or "reports:platform_discount_codes_list")
