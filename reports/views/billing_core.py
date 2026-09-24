# reports/views/billing_core.py
# -*- coding: utf-8 -*-
"""نواة الفوترة: ما تشترك فيه شاشات الاشتراك والدفع جميعاً.

**لماذا وحدة مستقلة.** كان كل ما يخصّ الفوترة في ملف واحد من 4337 سطراً —
لوحاتُ مالك المنصة، وبوّابتا الدفع، وشاشات المدرسة، والنواة التي تحتها. وطولُه
لم يكن مسألة ترتيب: ``_apply_payment_effects`` هي الدالة التي تُفعّل الاشتراكات
وتمدّد التواريخ وتمنح السعة، أي أخطر مئتي سطر في المنصة، وكانت مدفونةً في منتصف
ملفٍ لا يُقرأ.

وهنا تُقرأ. وما في هذه الوحدة تعتمد عليه الوحدات الثلاث الأخرى ولا تعتمد هي على
شيء منها — فالاتجاه واحد، والدورةُ مستحيلة بناءً.
"""
# -*- coding: utf-8 -*-

from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
import hashlib
from itertools import pairwise
import json
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse
import uuid

from django.core import signing
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.views.decorators.csrf import csrf_exempt

from core.observability import report_degraded as _degraded, soft_call, soft_fail

from ._helpers import *
from ._helpers import (
    _is_staff, _safe_next_url,
    _school_manager_label, _get_active_school,
    _clean_query_value, _clean_query_params, _parse_date_safe,
)
from ..mansour_knowledge import AUDIENCE_LABELS
from ..permissions import executive_director_schools_qs
from ..utils import create_system_notification
from ..validators import validate_image_file
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
from ..discount_codes import (
    DiscountCodeError,
    find_usable_code,
    release_dead_redemptions,
    reserve_redemption,
)
from ..moyasar_gateway import (
    MoyasarGatewayError,
    create_invoice as create_moyasar_invoice,
    fetch_invoice as fetch_moyasar_invoice,
    is_enabled as moyasar_is_enabled,
)



def _cache_set(key: str, value, ttl: int) -> None:
    """كتابةٌ في الكاش لا تُسقط الصفحة — لكنها لا تمرّ صامتة.

    تعثّرها يعني إعادةَ بناء لوحةٍ ثقيلة في كل طلب: تدهورُ أداءٍ لا يظهر في أي
    شاشة، ويُكتشف من فاتورة الخادم لا من بلاغ.
    """
    soft_call("platform.cache_set", lambda: cache.set(key, value, ttl), default=None, key=key)


ARCHIVE_ADDON_ANNUAL_PRICE = Decimal("399.00")


ARCHIVE_ADDON_INCLUDED_STORAGE_GB = 50


ARCHIVE_STORAGE_BLOCK_GB = 50


ARCHIVE_STORAGE_BLOCK_PRICE = Decimal("149.00")


def _archive_pricing():
    try:
        settings_obj = PlatformSettings.get_solo()
    except Exception:
        settings_obj = None

    return {
        "addon_price": Decimal(getattr(settings_obj, "archive_addon_annual_price", ARCHIVE_ADDON_ANNUAL_PRICE) or ARCHIVE_ADDON_ANNUAL_PRICE),
        "included_storage_gb": int(getattr(settings_obj, "archive_included_storage_gb", ARCHIVE_ADDON_INCLUDED_STORAGE_GB) or ARCHIVE_ADDON_INCLUDED_STORAGE_GB),
        "storage_block_gb": int(getattr(settings_obj, "archive_storage_block_gb", ARCHIVE_STORAGE_BLOCK_GB) or ARCHIVE_STORAGE_BLOCK_GB),
        "storage_block_price": Decimal(getattr(settings_obj, "archive_storage_block_price", ARCHIVE_STORAGE_BLOCK_PRICE) or ARCHIVE_STORAGE_BLOCK_PRICE),
    }


def _ensure_default_archive_storage_option(settings_obj: PlatformSettings) -> None:
    """يزرع خيارًا واحدًا لكل مساحة حتى لا تبقى المنصة بلا منتج قابل للبيع.

    الزرع لكل دلو على حدة: وجود خيار لمساحة العمل لا يعني أن مساحة الأرشفة
    معروضة للبيع، وكان غياب هذا التمييز يترك الأرشفة بلا منتج توسعة إطلاقًا
    رغم أن رسالة امتلائها تطلب من المدير شراء مساحة إضافية.
    """
    block_gb = int(
        getattr(settings_obj, "archive_storage_block_gb", ARCHIVE_STORAGE_BLOCK_GB)
        or ARCHIVE_STORAGE_BLOCK_GB
    )
    block_price = Decimal(
        getattr(settings_obj, "archive_storage_block_price", ARCHIVE_STORAGE_BLOCK_PRICE)
        or ARCHIVE_STORAGE_BLOCK_PRICE
    )
    for bucket in (
        ArchiveStorageOption.Bucket.WORK,
        ArchiveStorageOption.Bucket.ARCHIVE,
    ):
        if ArchiveStorageOption.objects.filter(bucket=bucket).exists():
            continue
        ArchiveStorageOption.objects.create(
            bucket=bucket,
            storage_gb=block_gb,
            price=block_price,
            sort_order=10,
            is_active=True,
        )


def _archive_storage_options(active_only: bool = True, bucket: str | None = None):
    with soft_fail("billing.ensure_default_storage_option"):
        _ensure_default_archive_storage_option(PlatformSettings.get_solo())
    qs = ArchiveStorageOption.objects.all().order_by(
        "bucket", "sort_order", "storage_gb", "id"
    )
    if active_only:
        qs = qs.filter(is_active=True)
    if bucket is not None:
        qs = qs.filter(bucket=bucket)
    return list(qs)


def _renewal_plan_catalog(current_plan_id: int | None = None) -> list[dict]:
    """Return customer-facing renewal plans grouped by school capacity.

    Trial and inactive plans must never be offered as renewal choices.  The
    catalogue keeps every published paid duration visible inside its capacity
    group so the school can compare all available choices without opening a
    collapsed control first.
    """
    plans = list(
        SubscriptionPlan.objects.filter(
            is_active=True,
            price__gt=0,
        ).order_by(
            "max_teachers",
            "days_duration",
            "price",
            "id",
        )
    )
    groups: dict[int, dict] = {}

    for plan in plans:
        capacity = int(plan.max_teachers or 0)
        group = groups.setdefault(
            capacity,
            {
                "capacity": capacity,
                "capacity_label": (
                    "عدد مستخدمين غير محدود"
                    if capacity <= 0
                    else f"حتى {capacity} مستخدم"
                ),
                "is_recommended": capacity == 50,
                "options": [],
                "features": [],
            },
        )

        days = int(plan.days_duration or 0)
        if 20 <= days <= 44:
            duration_label = "شهر"
            months = 1
        elif 160 <= days <= 210:
            duration_label = "6 أشهر"
            months = 6
        elif 330 <= days <= 400:
            duration_label = "سنة"
            months = 12
        else:
            duration_label = f"{days} يوم"
            months = None

        features = [
            line.strip().lstrip("-*•▪●‣").strip()
            for line in (plan.description or "").replace("\r", "").split("\n")
            if line.strip()
        ][:3]
        if not group["features"] and features:
            group["features"] = features

        monthly_equivalent = None
        if months:
            monthly_equivalent = (plan.price / Decimal(months)).quantize(
                Decimal("1"),
                rounding=ROUND_HALF_UP,
            )

        group["options"].append(
            {
                "plan": plan,
                "duration_label": duration_label,
                "monthly_equivalent": monthly_equivalent,
                "is_current": plan.id == current_plan_id,
                "annual_savings": None,
            }
        )

    for group in groups.values():
        monthly = next(
            (
                option
                for option in group["options"]
                if 20 <= int(option["plan"].days_duration or 0) <= 44
            ),
            None,
        )
        semiannual = next(
            (
                option
                for option in group["options"]
                if 160 <= int(option["plan"].days_duration or 0) <= 210
            ),
            None,
        )
        annual = next(
            (
                option
                for option in group["options"]
                if 330 <= int(option["plan"].days_duration or 0) <= 400
            ),
            None,
        )
        if monthly and semiannual:
            savings = (monthly["plan"].price * 6) - semiannual["plan"].price
            if savings > 0:
                semiannual["annual_savings"] = savings
        if monthly and annual:
            savings = (monthly["plan"].price * 12) - annual["plan"].price
            if savings > 0:
                annual["annual_savings"] = savings

    return sorted(
        groups.values(),
        key=lambda group: (
            1 if group["capacity"] <= 0 else 0,
            group["capacity"] if group["capacity"] > 0 else 999999,
        ),
    )


def _payment_purpose_label(payment: Payment) -> str:
    try:
        return payment.get_purpose_display()
    except Exception:
        return "اشتراك المدرسة"


def _record_subscription_payment_if_missing(
    *,
    subscription: SchoolSubscription,
    actor,
    note: str,
    force: bool = False,
) -> bool:
    """تسجيل عملية دفع (approved) لاشتراك مدرسة في حال عدم وجود دفعة للفترة الحالية.

    نستخدم ذلك لحالات "الاشتراك أُضيف/فُعِّل يدويًا" حتى يظهر في صفحة المالية.
    """
    try:
        if not bool(getattr(subscription, "is_active", False)):
            return False

        plan = getattr(subscription, "plan", None)
        price = getattr(plan, "price", None)
        if price is None:
            return False
        try:
            if float(price) <= 0:
                return False
        except (TypeError, ValueError):
            # سعرٌ غير رقمي: لا نستنتج منه «مجاني»، بل نُكمل إلى الفحص التالي.
            _degraded("billing.non_numeric_plan_price", plan_id=getattr(plan, "pk", None))

        today = timezone.localdate()
        period_start = getattr(subscription, "start_date", None) or today

        # ✅ تحصين: عند التفعيل/التجديد اليدوي (force=True) لا نريد منع التسجيل
        # بسبب وجود دفعات قديمة، لكن نمنع تكرار نفس العملية في نفس اليوم.
        if force:
            dup_qs = Payment.objects.filter(
                subscription=subscription,
                status__in=[Payment.Status.PENDING, Payment.Status.APPROVED],
                created_at__date=today,
                requested_plan=subscription.plan,
                amount=subscription.plan.price,
            )
            if dup_qs.exists():
                return False
        else:
            existing_qs = Payment.objects.filter(
                subscription=subscription,
                status__in=[Payment.Status.PENDING, Payment.Status.APPROVED],
            )
            # نعتمد created_at بدلاً من payment_date لأن payment_date قد تكون "اليوم" دائماً
            # في التسجيلات اليدوية، مما يمنع تسجيل دفعة جديدة عند التجديد في نفس اليوم.
            existing_qs = existing_qs.filter(created_at__date__gte=period_start)
            if existing_qs.exists():
                return False

        Payment.objects.create(
            school=subscription.school,
            subscription=subscription,
            requested_plan=subscription.plan,
            amount=subscription.plan.price,
            receipt_image=None,
            payment_date=today,
            status=Payment.Status.APPROVED,
            notes=(note or "").strip(),
            created_by=actor,
            payer_kind=Payment.PayerKind.PLATFORM,
        )
        return True
    except Exception:
        logger.exception("Failed to record manual payment for subscription")
        return False


class _ApprovalError(Exception):
    """خطأ يمنع اعتماد عملية دفع (يستوجب التراجع عن المعاملة)."""


# ترتيب تطبيق أثر الاعتماد داخل الطلب الموحّد: الاشتراك أولاً ثم إضافة الأرشفة،
# ثم توسعتا المساحة. ترتيب الأرشفة قبل توسعتها ضروري: شراؤهما معاً في إيصال
# واحد يعني أن الملحق يجب أن يوجد قبل رفع حدّه.
_PURPOSE_APPLY_ORDER = {
    Payment.Purpose.SUBSCRIPTION: 0,
    Payment.Purpose.ARCHIVE_ADDON: 1,
    Payment.Purpose.WORK_STORAGE: 2,
    Payment.Purpose.ARCHIVE_SPACE: 3,
}


def _apply_payment_effects(payment, today, pricing):
    """يطبّق أثر اعتماد عملية دفع واحدة حسب غرضها.

    يعيد (level, message) حيث level ∈ {"success", "warning"}.
    يرمي ``_ApprovalError`` إذا تعذّر تطبيق الأثر (يستوجب التراجع).
    """
    payment = Payment.objects.select_for_update().get(pk=payment.pk)
    if payment.effects_applied_at is not None:
        return ("warning", "سبق تطبيق أثر عملية الدفع هذه؛ لم يُكرّر التفعيل أو زيادة المساحة.")

    def applied(level, message):
        payment.effects_applied_at = timezone.now()
        payment.save(update_fields=["effects_applied_at", "updated_at"])
        try:
            from ..utils import create_system_notification

            manager_ids = list(
                SchoolMembership.objects.filter(
                    school=payment.school,
                    role_type=SchoolMembership.RoleType.MANAGER,
                    is_active=True,
                ).values_list("teacher_id", flat=True)
            )
            if manager_ids:
                # من دفع جزءٌ من الخبر: «تم التجديد» وحدها تترك المدير يظن
                # أن دفعةً من عنده اعتُمدت، فيبحث عن إيصالٍ لم يرسله.
                detail = message
                if payment.payer_kind == Payment.PayerKind.GROUP_DIRECTOR:
                    payer = getattr(payment.payer_group, "name", "") or "مجموعة مدارسك"
                    detail = f"{message}\nهذه الدفعة أنشأتها {payer} نيابةً عن مدرستك."
                create_system_notification(
                    title="تم تفعيل خدمات المدرسة",
                    message=detail,
                    school=payment.school,
                    teacher_ids=manager_ids,
                    is_important=True,
                )
        except Exception:
            logger.exception("Failed to notify school managers after payment approval")
        if purpose == Payment.Purpose.SUBSCRIPTION:
            try:
                from ..tasks import send_subscription_activation_email_task
                from ..utils import run_task_safe

                run_task_safe(send_subscription_activation_email_task, payment.pk)
            except Exception:
                logger.exception("Failed to queue subscription activation email")
        return (level, message)

    purpose = getattr(payment, "purpose", Payment.Purpose.SUBSCRIPTION)

    if purpose == Payment.Purpose.ARCHIVE_ADDON:
        addon, _created = SchoolArchiveAddon.objects.select_for_update().get_or_create(
            school=payment.school,
            defaults={
                "is_enabled": True,
                "start_date": today,
                "end_date": today + timedelta(days=364),
                "storage_limit_gb": pricing["included_storage_gb"],
                "paid_amount": payment.amount,
            },
        )
        if not _created:
            current_end = addon.end_date if addon.end_date and addon.end_date >= today else today
            addon.is_enabled = True
            addon.start_date = addon.start_date or today
            addon.end_date = current_end + timedelta(days=365)
            addon.storage_limit_gb = max(
                int(addon.storage_limit_gb or 0),
                pricing["included_storage_gb"],
            )
            addon.paid_amount = (addon.paid_amount or 0) + payment.amount
            addon.save(
                update_fields=[
                    "is_enabled", "start_date", "end_date",
                    "storage_limit_gb", "paid_amount", "updated_at",
                ]
            )
        return applied("success", "تم تفعيل/تجديد إضافة الأرشفة للمدرسة تلقائياً.")

    if purpose == Payment.Purpose.WORK_STORAGE:
        added_gb = int(payment.archive_storage_gb or 0)
        if added_gb <= 0:
            raise _ApprovalError("طلب زيادة التخزين لا يحتوي على مساحة صالحة.")

        # Storage is its own product. It used to be refused unless the school had
        # first bought yearly archiving, which forced schools that only needed
        # room for report photos to buy a product they did not want.
        school = School.objects.select_for_update().get(pk=payment.school_id)
        school.extra_storage_gb = int(school.extra_storage_gb or 0) + added_gb
        school.save(update_fields=["extra_storage_gb"])
        return applied("success", f"تمت زيادة مساحة عمل المدرسة بمقدار {added_gb}GB.")

    if purpose == Payment.Purpose.ARCHIVE_SPACE:
        added_gb = int(payment.archive_storage_gb or 0)
        if added_gb <= 0:
            raise _ApprovalError("طلب زيادة مساحة الأرشفة لا يحتوي على مساحة صالحة.")

        # مساحة الأرشفة تعيش على الملحق، فرفعُ حدّها يتطلب وجوده. الطلب لا
        # يُقبل أصلاً بلا ملحق، لكن الاعتماد قد يأتي بعد حذفه — فنرفض بوضوح
        # بدل إنشاء ملحق لم تشترِه المدرسة.
        addon = (
            SchoolArchiveAddon.objects.select_for_update()
            .filter(school_id=payment.school_id)
            .first()
        )
        if addon is None:
            raise _ApprovalError(
                "لا يمكن زيادة مساحة الأرشفة قبل تفعيل خدمة الأرشفة السنوية للمدرسة."
            )
        addon.storage_limit_gb = int(addon.storage_limit_gb or 0) + added_gb
        addon.save(update_fields=["storage_limit_gb", "updated_at"])
        return applied(
            "success", f"تمت زيادة مساحة الأرشفة السنوية بمقدار {added_gb}GB."
        )

    # ── الاشتراك ──
    plan_to_apply = payment.requested_plan
    subscription = getattr(payment.school, "subscription", None)
    level, msg = "success", "تم تجديد/تفعيل اشتراك المدرسة تلقائياً."

    if subscription is None:
        if plan_to_apply is None:
            raise _ApprovalError(
                "لا يمكن اعتماد دفع الاشتراك قبل ربطه بباقة؛ لم يُفعّل الاشتراك."
            )
        subscription = SchoolSubscription(
            school=payment.school,
            plan=plan_to_apply,
            teacher_limit_override=(
                int(payment.requested_teacher_limit)
                if payment.requested_teacher_limit
                else None
            ),
            start_date=today,
            end_date=today,
            is_active=True,
        )
        subscription.save()
    else:
        if plan_to_apply is not None:
            subscription.plan = plan_to_apply
        if payment.requested_teacher_limit:
            subscription.teacher_limit_override = int(payment.requested_teacher_limit)
        subscription.is_active = True
        subscription.canceled_at = None
        subscription.cancel_reason = ""
        days = int(getattr(subscription.plan, "days_duration", 0) or 0)
        subscription.start_date = today
        subscription.end_date = today if days <= 0 else today + timedelta(days=days - 1)
        subscription.save(
            update_fields=[
                "plan",
                "teacher_limit_override",
                "start_date",
                "end_date",
                "is_active",
                "canceled_at",
                "cancel_reason",
                "updated_at",
            ]
        )

    if subscription is not None and payment.subscription_id != subscription.id:
        payment.subscription = subscription
        payment.save(update_fields=["subscription"])

    included_archive_gb = int(
        getattr(getattr(subscription, "plan", None), "included_archive_storage_gb", 0) or 0
    )
    if included_archive_gb > 0:
        addon, created = SchoolArchiveAddon.objects.select_for_update().get_or_create(
            school=payment.school,
            defaults={
                "is_enabled": True,
                "start_date": subscription.start_date,
                "end_date": subscription.end_date,
                "storage_limit_gb": included_archive_gb,
                "paid_amount": 0,
                "notes": f"مشمولة تلقائياً ضمن باقة {subscription.plan.name}.",
            },
        )
        if not created:
            addon.is_enabled = True
            addon.start_date = min(addon.start_date or subscription.start_date, subscription.start_date)
            addon.end_date = max(addon.end_date or subscription.end_date, subscription.end_date)
            addon.storage_limit_gb = max(int(addon.storage_limit_gb or 0), included_archive_gb)
            included_note = f"مشمولة تلقائياً ضمن باقة {subscription.plan.name}."
            if included_note not in (addon.notes or ""):
                addon.notes = "\n".join(filter(None, [(addon.notes or "").strip(), included_note]))
            addon.save(
                update_fields=[
                    "is_enabled", "start_date", "end_date", "storage_limit_gb", "notes", "updated_at",
                ]
            )
        msg = f"{msg} كما تم تفعيل الأرشيف المضمّن بسعة {included_archive_gb}GB."

    if payment.requested_teacher_limit:
        msg = f"{msg} سعة المعلمين المعتمدة: {int(payment.requested_teacher_limit)} معلماً."

    return applied(level, msg)


class _PaymentActor:
    """من يقف خلف طلب الدفع، وبأي صفة.

    شاشة الاشتراك كانت تسأل سؤالاً واحداً: «هل المستخدم مديرُ هذه المدرسة؟».
    وهو سؤالٌ يكفي ما دام الدافع واحداً. ولمّا صار للمدير التنفيذي أن يدفع
    نيابةً عن إحدى مدارس مجموعته، صار السؤال سؤالين: **أي مدرسة** و**بأي
    صفة** — والثاني هو ما يُكتب في ``Payment.payer_kind`` فيراه مدير المدرسة
    في سجلّه بدل دفعةٍ بلا مصدر.

    يكشف ``school`` و``school_id`` بالاسمين نفسيهما اللذين كانت ``SchoolMembership``
    تكشفهما، فبقيّة مسار الدفع لا تعلم بالفرق ولم تتغيّر.
    """

    __slots__ = ("school", "school_id", "payer_kind", "group")

    def __init__(self, school, payer_kind, group=None):
        self.school = school
        self.school_id = school.pk
        self.payer_kind = payer_kind
        self.group = group

    @property
    def is_on_behalf(self) -> bool:
        return self.payer_kind == Payment.PayerKind.GROUP_DIRECTOR


def _requested_school_id(request) -> str:
    """المدرسة المقصودة: من الجسم في الإرسال، ومن المسار في العرض.

    ``POST`` لا يحمل معاملات المسار عبر ``formaction``، ولذلك يحمل النموذج
    حقلاً مخفياً باسم ``on_behalf_school``.
    """
    for source in (request.POST, request.GET):
        value = (source.get("on_behalf_school") or source.get("school") or "").strip()
        if value:
            return value
    return ""


def _resolve_payment_actor(request):
    """يحلّ الفاعل: مديرُ المدرسة لمدرسته، أو المدير التنفيذي لإحدى مدارسه.

    الترتيب مقصود: عضوية الإدارة تُسأل أولاً، فمن يدير مدرسةً ويقود مجموعتها
    معاً يدفع بصفته مديرها لا نيابةً عنها. ومعرّف مدرسةٍ خارج نطاق المستخدم
    لا يُسقِط الطلب إلى مدرسةٍ أخرى — يُرفض الطلب، وإلا صار تمريرُ رقمٍ عشوائي
    طريقاً لتحصيل دفعةٍ على مدرسةٍ لم يقصدها الدافع.
    """
    requested_id = _requested_school_id(request)
    active_school = _get_active_school(request)
    manager_qs = SchoolMembership.objects.filter(
        teacher=request.user,
        role_type=SchoolMembership.RoleType.MANAGER,
        is_active=True,
    ).select_related("school")

    if requested_id:
        if not requested_id.isdigit():
            return None

        # School-facing billing is anchored to the active school. A manager's
        # membership in another school is not authority to switch billing
        # context through a crafted query string or hidden POST field.
        if active_school is not None and int(requested_id) == int(active_school.pk):
            membership = manager_qs.filter(school=active_school).first()
            if membership is not None:
                return _PaymentActor(membership.school, Payment.PayerKind.SCHOOL)

        # The one intentional exception is the established group-director
        # workflow. Its scope comes from the group relationship, not from a
        # school-manager membership or the active-school session.
        school = (
            executive_director_schools_qs(request.user)
            .select_related("group")
            .filter(pk=requested_id)
            .first()
        )
        if school is not None:
            return _PaymentActor(
                school, Payment.PayerKind.GROUP_DIRECTOR, getattr(school, "group", None)
            )
        return None

    if active_school:
        membership = manager_qs.filter(school=active_school).first()
        if membership is not None:
            return _PaymentActor(membership.school, Payment.PayerKind.SCHOOL)
    return None


def _subscription_redirect(actor):
    """العودة إلى الصفحة نفسها التي انطلق منها الطلب.

    الرجوع المجرّد إلى ``my_subscription`` كان يُخرج المدير التنفيذي من سياق
    المدرسة التي يدفع عنها إلى صفحةٍ تردّه، فتضيع رسالة النجاح.
    """
    url = reverse("reports:my_subscription")
    if actor is not None and actor.is_on_behalf:
        return redirect(f"{url}?school={actor.school_id}")
    return redirect(url)


_ACTING_SCHOOL_SESSION_KEY = "subscription_acting_school_id"


def _remember_acting_school(request, actor) -> None:
    """يحفظ المدرسة المقصودة قبل مغادرة الموقع إلى بوابة الدفع.

    صفحات العودة من البوابة (``*_return`` و``moyasar_callback``) لا تحمل معها
    شيئاً عن سياق الطلب، فكان المدير التنفيذي يعود من ميّسر إلى صفحةٍ تردّه
    إلى الرئيسية — ورسالةُ نجاح الدفع تضيع معه.
    """
    if actor is not None and actor.is_on_behalf:
        request.session[_ACTING_SCHOOL_SESSION_KEY] = int(actor.school_id)
    else:
        request.session.pop(_ACTING_SCHOOL_SESSION_KEY, None)


def _subscription_return_redirect(request):
    """العودة بعد البوابة إلى صفحة المدرسة التي انطلق منها الطلب.

    الصلاحية تُعاد قراءتها من المصدر لا من الجلسة: مفتاح الجلسة يقول «أين
    كان» لا «ماذا يملك»، فلو سُحبت قيادته للمجموعة بين الذهاب والعودة عاد
    إلى صفحته هو.
    """
    school_id = request.session.get(_ACTING_SCHOOL_SESSION_KEY)
    if school_id and getattr(request.user, "is_authenticated", False):
        allowed = executive_director_schools_qs(request.user).filter(pk=school_id).exists()
        if allowed:
            return redirect(f"{reverse('reports:my_subscription')}?school={int(school_id)}")
    return redirect("reports:my_subscription")


def _stamp_payer(payment_kwargs: dict, actor) -> dict:
    """يبصم سجل الدفع بصفة الدافع قبل حفظه."""
    payment_kwargs["payer_kind"] = actor.payer_kind
    payment_kwargs["payer_group"] = actor.group if actor.is_on_behalf else None
    return payment_kwargs


def _notify_managers_of_group_payment(actor, *, total, labels, payment_method) -> None:
    """يُعلم مديري المدرسة فور إنشاء المجموعة طلباً نيابةً عنهم.

    الإشعار عند الاعتماد وحده لا يكفي: بين الطلب واعتماده قد يشتري المدير
    نفس البند مرة ثانية، فيدفع عن شيءٍ اشترته مجموعته قبل ساعة.
    """
    if not actor.is_on_behalf:
        return
    try:
        manager_ids = list(
            SchoolMembership.objects.filter(
                school=actor.school,
                role_type=SchoolMembership.RoleType.MANAGER,
                is_active=True,
            ).values_list("teacher_id", flat=True)
        )
        if not manager_ids:
            return
        group_name = getattr(actor.group, "name", "") or "مجموعة المدارس"
        if payment_method in {Payment.Method.MOYASAR, Payment.Method.TAMARA}:
            activation_detail = "ستُفعّل تلقائيًا فور تأكيد نجاح الدفع من البوابة."
        else:
            activation_detail = "ستُراجع صورة التحويل وتُفعّل خلال يوم عمل واحد كحد أقصى."
        create_system_notification(
            title="طلب دفع أنشأته مجموعتك لمدرستك",
            message=(
                f"أنشأ المدير التنفيذي لـ{group_name} طلب دفع لمدرستك: {labels} "
                f"— الإجمالي {total} ريال. لا حاجة لدفع هذه البنود مرة أخرى؛ "
                f"{activation_detail}"
            ),
            school=actor.school,
            teacher_ids=manager_ids,
            is_important=True,
        )
    except Exception:
        logger.exception("Failed to notify school managers about a group-paid order")


def _group_payer_badge(school):
    """آخر دفعة أنشأتها المجموعة لهذه المدرسة، إن وُجدت.

    وجودها هو ما يُظهر الشارة الدائمة في صفحة المدير: مدرسةٌ لها مجموعة لم
    تدفع عنها قطّ لا يصحّ أن تُخبَر أن «مجموعتك تدير اشتراكك».
    """
    return (
        Payment.objects.filter(
            school=school,
            payer_kind=Payment.PayerKind.GROUP_DIRECTOR,
        )
        .select_related("payer_group", "created_by")
        .order_by("-created_at")
        .first()
    )


class _PaymentSelectionError(Exception):
    pass


class _CheckoutSubmissionError(_PaymentSelectionError):
    pass


_CHECKOUT_SUBMISSION_SALT = "reports.school-billing.checkout"
_CHECKOUT_SUBMISSION_MAX_AGE_SECONDS = 24 * 60 * 60


def _issue_checkout_submission_key(request, actor) -> str:
    """Issue a signed, one-page checkout key scoped to actor and billing school."""
    return signing.dumps(
        {
            "user_id": int(request.user.pk),
            "school_id": int(actor.school_id),
            "payer_kind": str(actor.payer_kind),
            "nonce": uuid.uuid4().hex,
        },
        salt=_CHECKOUT_SUBMISSION_SALT,
        compress=True,
    )


def _checkout_payload_fingerprint(items, payment_method: str) -> str:
    """Fingerprint only server-authoritative quote values, never browser totals."""
    normalized_items = []
    for item in items:
        plan = item.get("requested_plan")
        normalized_items.append(
            {
                "purpose": str(item["purpose"]),
                "plan_id": getattr(plan, "pk", None),
                "plan_days": int(getattr(plan, "days_duration", 0) or 0),
                "teacher_limit": int(item.get("requested_teacher_limit") or 0),
                "amount": format(
                    Decimal(str(item["amount"])).quantize(Decimal("0.01")),
                    "f",
                ),
                "discount_code_id": getattr(item.get("discount_code"), "pk", None),
                "discount_amount": format(
                    Decimal(str(item.get("discount_amount", 0))).quantize(
                        Decimal("0.01")
                    ),
                    "f",
                ),
                "archive_storage_gb": int(item.get("archive_storage_gb") or 0),
            }
        )
    normalized_items.sort(
        key=lambda value: (
            value["purpose"],
            value["plan_id"] or 0,
            value["archive_storage_gb"],
        )
    )
    encoded = json.dumps(
        {"payment_method": str(payment_method), "items": normalized_items},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _checkout_submission_identity(request, actor, items, payment_method: str):
    """Return a durable 32-char batch reference and its per-key prefix."""
    raw_key = (request.POST.get("checkout_submission_key") or "").strip()
    if not raw_key:
        raise _CheckoutSubmissionError(
            "انتهت جلسة طلب الدفع. حدّث الصفحة ثم أعد المحاولة."
        )
    try:
        key_data = signing.loads(
            raw_key,
            salt=_CHECKOUT_SUBMISSION_SALT,
            max_age=_CHECKOUT_SUBMISSION_MAX_AGE_SECONDS,
        )
    except signing.BadSignature as exc:
        raise _CheckoutSubmissionError(
            "مرجع طلب الدفع غير صالح أو منتهي. حدّث الصفحة ثم أعد المحاولة."
        ) from exc

    expected = (
        int(request.user.pk),
        int(actor.school_id),
        str(actor.payer_kind),
    )
    try:
        actual = (
            int(key_data["user_id"]),
            int(key_data["school_id"]),
            str(key_data["payer_kind"]),
        )
        nonce = str(key_data["nonce"])
    except (KeyError, TypeError, ValueError) as exc:
        raise _CheckoutSubmissionError("مرجع طلب الدفع غير صالح.") from exc
    if actual != expected or len(nonce) < 16:
        raise _CheckoutSubmissionError("مرجع طلب الدفع لا يخص هذا المستخدم أو المدرسة.")

    key_material = f"{expected[0]}:{expected[1]}:{expected[2]}:{nonce}".encode(
        "utf-8"
    )
    key_prefix = "I" + hashlib.sha256(key_material).hexdigest()[:15]
    fingerprint = _checkout_payload_fingerprint(items, payment_method)
    return f"{key_prefix}{fingerprint}", key_prefix


def _lock_checkout_submission(request, actor, items, payment_method: str):
    """Serialize one school's checkout key and detect replay or key misuse.

    Callers must invoke this inside ``transaction.atomic()``. Locking the
    existing School row gives us durable race safety without a new table or
    migration; ``Payment.batch_ref`` persists the key digest and quote digest.
    """
    batch_ref, key_prefix = _checkout_submission_identity(
        request, actor, items, payment_method
    )
    School.objects.select_for_update().get(pk=actor.school_id)
    existing = list(
        Payment.objects.filter(
            school_id=actor.school_id,
            created_by=request.user,
            batch_ref__startswith=key_prefix,
        ).order_by("pk")
    )
    if not existing:
        return batch_ref, []
    if any(payment.batch_ref != batch_ref for payment in existing):
        raise _CheckoutSubmissionError(
            "استُخدم مرجع طلب الدفع نفسه مع خيارات مختلفة. حدّث الصفحة لإنشاء طلب جديد."
        )
    return batch_ref, existing


def _validate_payment_receipt(receipt) -> None:
    """Run the canonical image validator before any Payment or storage write."""
    if receipt is None:
        raise _PaymentSelectionError("يرجى إرفاق صورة الإيصال.")
    try:
        validate_image_file(receipt)
    except ValidationError as exc:
        detail = " ".join(exc.messages) if exc.messages else str(exc)
        raise _PaymentSelectionError(detail) from exc


def _save_payment_receipt(payment: Payment, receipt):
    """Save one validated receipt and remove it if model persistence fails."""
    payment.receipt_image = receipt
    try:
        payment.save()
    except Exception:
        field_file = payment.receipt_image
        if (
            getattr(field_file, "name", "")
            and getattr(field_file, "_committed", False)
        ):
            field_file.storage.delete(field_file.name)
        raise
    return payment.receipt_image.storage, payment.receipt_image.name


def _subscription_quote_from_request(request, school, requested_plan):
    raw_capacity = (request.POST.get("teacher_capacity") or "").strip()
    if not raw_capacity:
        return {
            "plan": requested_plan,
            "capacity": int(getattr(requested_plan, "max_teachers", 0) or 0),
            "price": Decimal(getattr(requested_plan, "price", 0) or 0),
        }
    try:
        requested_capacity = int(raw_capacity)
    except (TypeError, ValueError) as exc:
        raise _PaymentSelectionError("سعة المعلمين المختارة غير صالحة.") from exc

    capacity = normalize_teacher_capacity(requested_capacity)
    if capacity is None:
        raise _PaymentSelectionError("السعات المنشورة متاحة حتى 100 معلم. تواصل مع الدعم لسعة أكبر.")

    current_teacher_count = SchoolMembership.seats_used(school)
    if capacity < current_teacher_count:
        raise _PaymentSelectionError(
            f"لا يمكن اختيار سعة {capacity} مع وجود {current_teacher_count} معلماً في المدرسة."
        )

    quote = quote_for_selection(requested_plan.pk, capacity)
    if quote is None:
        raise _PaymentSelectionError("تعذّر احتساب سعر السعة المختارة من الباقات المنشورة.")
    return quote


def _build_unified_payment_items(request, membership, subscription):
    school = membership.school
    pricing = _archive_pricing()
    include_sub = (request.POST.get("include_subscription") or "") == "1"
    include_addon = (request.POST.get("include_archive_addon") or "") == "1"
    include_storage = (request.POST.get("include_archive_storage") or "") == "1"
    include_archive_space = (request.POST.get("include_archive_space") or "") == "1"

    if not (include_sub or include_addon or include_storage or include_archive_space):
        raise _PaymentSelectionError("اختر عنصرًا واحدًا على الأقل للدفع.")

    archive_addon = SchoolArchiveAddon.objects.filter(school=school).first()
    addon_active = bool(archive_addon and archive_addon.is_active)
    items = []
    warnings = []

    if include_sub:
        requested_plan = None
        plan_id = request.POST.get("plan_id")
        if plan_id:
            requested_plan = SubscriptionPlan.objects.filter(
                pk=plan_id,
                is_active=True,
                price__gt=0,
            ).first()
            if requested_plan is None:
                raise _PaymentSelectionError("الباقة المختارة غير متاحة للتجديد.")
        if (
            not requested_plan
            and subscription
            and subscription.plan.is_active
            and subscription.plan.price > 0
        ):
            requested_plan = subscription.plan
        if not requested_plan:
            raise _PaymentSelectionError("يرجى اختيار باقة للاشتراك/التجديد.")
        quote = _subscription_quote_from_request(request, school, requested_plan)
        requested_plan = quote["plan"]
        amount = quote["price"]
        requested_teacher_limit = int(quote["capacity"] or 0)
        try:
            if float(amount) <= 0:
                raise _PaymentSelectionError("لا يمكن إنشاء طلب دفع لباقة مجانية/غير صالحة.")
        except (TypeError, ValueError) as exc:
            raise _PaymentSelectionError("سعر الباقة غير صالح.") from exc
        items.append({
            "purpose": Payment.Purpose.SUBSCRIPTION,
            "requested_plan": requested_plan,
            "requested_teacher_limit": requested_teacher_limit,
            "amount": amount,
            "label": f"اشتراك: {requested_plan.name} · سعة {requested_teacher_limit} معلماً",
        })

    if include_addon:
        if Payment.objects.filter(
            school=school,
            purpose=Payment.Purpose.ARCHIVE_ADDON,
            status=Payment.Status.PENDING,
        ).exists():
            warnings.append("إضافة الأرشفة (يوجد طلب قيد المراجعة)")
        else:
            items.append({
                "purpose": Payment.Purpose.ARCHIVE_ADDON,
                "requested_plan": None,
                "amount": pricing["addon_price"],
                "label": "إضافة الأرشفة السنوية",
            })

    if include_storage:
        # Work storage is its own product — no yearly-archive add-on required.
        if Payment.objects.filter(
            school=school,
            purpose=Payment.Purpose.WORK_STORAGE,
            status=Payment.Status.PENDING,
        ).exists():
            warnings.append("زيادة مساحة العمل (يوجد طلب قيد المراجعة)")
        else:
            option = None
            option_id = request.POST.get("archive_storage_option_id")
            if option_id:
                option = ArchiveStorageOption.objects.filter(
                    pk=option_id,
                    is_active=True,
                    bucket=ArchiveStorageOption.Bucket.WORK,
                ).first()
            if option is None:
                raise _PaymentSelectionError("اختر خيار زيادة مساحة عمل صالح.")
            items.append({
                "purpose": Payment.Purpose.WORK_STORAGE,
                "requested_plan": None,
                "amount": option.price,
                "archive_storage_gb": int(option.storage_gb or 0),
                "label": f"زيادة مساحة عمل المدرسة {option.storage_gb}GB",
            })

    if include_archive_space:
        # عكس مساحة العمل: هذه توسعةٌ لحدٍّ يعيش على ملحق الأرشفة، فلا معنى
        # لبيعها لمدرسة بلا ملحق — إلا إذا كانت تشتري الملحق في الطلب نفسه.
        if Payment.objects.filter(
            school=school,
            purpose=Payment.Purpose.ARCHIVE_SPACE,
            status=Payment.Status.PENDING,
        ).exists():
            warnings.append("زيادة مساحة الأرشفة (يوجد طلب قيد المراجعة)")
        elif not (addon_active or include_addon):
            warnings.append(
                "زيادة مساحة الأرشفة (تتطلب تفعيل خدمة الأرشفة السنوية أولاً)"
            )
        else:
            option = None
            option_id = request.POST.get("archive_space_option_id")
            if option_id:
                option = ArchiveStorageOption.objects.filter(
                    pk=option_id,
                    is_active=True,
                    bucket=ArchiveStorageOption.Bucket.ARCHIVE,
                ).first()
            if option is None:
                raise _PaymentSelectionError("اختر خيار زيادة مساحة أرشفة صالح.")
            items.append({
                "purpose": Payment.Purpose.ARCHIVE_SPACE,
                "requested_plan": None,
                "amount": option.price,
                "archive_storage_gb": int(option.storage_gb or 0),
                "label": f"زيادة مساحة الأرشفة السنوية {option.storage_gb}GB",
            })

    if not items:
        detail = " ، ".join(warnings) if warnings else "لا توجد عناصر صالحة للدفع."
        raise _PaymentSelectionError(f"تعذّر إنشاء الطلب: {detail}")

    _apply_discount_code_to_items(request, school, items)
    return items, warnings


def _apply_discount_code_to_items(request, school, items) -> None:
    """يطبّق كود الخصم (إن أُرسل) على بند الاشتراك وحده.

    الخصم يسري على الاشتراك فقط بقرار منتج صريح — لا على الأرشفة والمساحات.
    التحقق هنا بلا حجز؛ الحجز الفعلي يجري داخل معاملة إنشاء الدفع حيث يُعاد
    الفحص تحت قفل الصف.
    """
    raw_code = (request.POST.get("discount_code") or "").strip()
    if not raw_code:
        return

    subscription_item = next(
        (it for it in items if it["purpose"] == Payment.Purpose.SUBSCRIPTION),
        None,
    )
    if subscription_item is None:
        raise _PaymentSelectionError(
            "كود الخصم يسري على بند الاشتراك فقط؛ أضف تجديد الاشتراك إلى الطلب لاستخدامه."
        )

    try:
        code = find_usable_code(raw_code, school)
    except DiscountCodeError as exc:
        raise _PaymentSelectionError(str(exc)) from exc

    original = Decimal(str(subscription_item["amount"]))
    discount = code.discount_for(original)
    subscription_item["amount"] = original - discount
    subscription_item["discount_code"] = code
    subscription_item["discount_amount"] = discount
    subscription_item["label"] = (
        f"{subscription_item['label']} · كود خصم {code.code} (-{discount} ريال)"
    )


def _create_unified_payment(request, membership, subscription):
    """ينشئ طلب دفع موحّد: يجمع الاشتراك + إضافة الأرشفة + زيادة المساحة في إيصال واحد.

    لكل عنصر مختار يُنشأ سجل Payment مستقل بنفس صورة الإيصال (ملف واحد مشترك)،
    حتى يبقى منطق الاعتماد الحالي (لكل غرض على حدة) سليمًا دون تغيير.

    زيادة مساحة التخزين مستقلة تمامًا عن إضافة الأرشفة السنوية، فيمكن طلبها وحدها
    أو ضمن نفس الطلب دون أي شرط مسبق.
    """
    school = membership.school
    receipt = request.FILES.get("receipt_image")
    notes = (request.POST.get("notes") or "").strip()

    try:
        items, warnings = _build_unified_payment_items(request, membership, subscription)
    except _PaymentSelectionError as exc:
        messages.error(request, str(exc))
        return _subscription_redirect(membership)

    total = sum((Decimal(str(it["amount"])) for it in items), Decimal("0"))

    # خصم 100% يجعل الطلب صفرياً: لا تحويل بنكي ولا إيصال — تفعيل مباشر.
    if total <= 0:
        return _activate_free_discount_order(
            request,
            membership,
            subscription,
            items,
            payment_method=Payment.Method.BANK_TRANSFER,
        )

    try:
        _validate_payment_receipt(receipt)
    except _PaymentSelectionError as exc:
        messages.error(request, str(exc))
        return _subscription_redirect(membership)

    labels = "، ".join(it["label"] for it in items)
    duplicate = False
    stored_receipt = None
    try:
        with transaction.atomic():
            batch_ref, existing = _lock_checkout_submission(
                request,
                membership,
                items,
                Payment.Method.BANK_TRANSFER,
            )
            if existing:
                duplicate = True
            else:
                base_note = (
                    f"[طلب موحّد {batch_ref.upper()}] {labels} "
                    f"— الإجمالي {total} ريال."
                )
                if membership.is_on_behalf:
                    group_name = (
                        getattr(membership.group, "name", "") or "مجموعة المدارس"
                    )
                    base_note = f"{base_note}\nدفعته {group_name} نيابةً عن المدرسة."
                if notes:
                    base_note = f"{base_note}\nملاحظة المدير: {notes}"

                shared_name = None
                for it in items:
                    payment = Payment(
                        **_stamp_payer(
                            {
                                "school": school,
                                "subscription": subscription,
                                "requested_plan": it.get("requested_plan"),
                                "requested_teacher_limit": it.get(
                                    "requested_teacher_limit"
                                ),
                                "purpose": it["purpose"],
                                "amount": it["amount"],
                                "discount_code": it.get("discount_code"),
                                "discount_amount": it.get("discount_amount", 0),
                                "archive_storage_gb": it.get(
                                    "archive_storage_gb", 0
                                ),
                                "notes": base_note,
                                "batch_ref": batch_ref,
                                "payment_method": Payment.Method.BANK_TRANSFER,
                                "created_by": request.user,
                            },
                            membership,
                        )
                    )
                    if shared_name is None:
                        # نحفظ الملف مرة واحدة ثم نعيد استخدام اسمه لبقية السجلات
                        stored_receipt = _save_payment_receipt(payment, receipt)
                        shared_name = payment.receipt_image.name
                    else:
                        payment.receipt_image.name = shared_name
                        payment.save()
                    if it.get("discount_code") is not None:
                        reserve_redemption(
                            it["discount_code"],
                            school,
                            payment=payment,
                            batch_ref=batch_ref,
                            amount=it.get("discount_amount", Decimal("0.00")),
                        )
    except _CheckoutSubmissionError as exc:
        messages.error(request, str(exc))
        return _subscription_redirect(membership)
    except DiscountCodeError as exc:
        # سبقتنا مدرسة أخرى إلى آخر استخدام بين التحقق والحجز — الطلب كله تراجع.
        if stored_receipt is not None:
            stored_receipt[0].delete(stored_receipt[1])
        messages.error(request, str(exc))
        return _subscription_redirect(membership)
    except Exception:
        if stored_receipt is not None:
            try:
                stored_receipt[0].delete(stored_receipt[1])
            except Exception:
                logger.exception("Failed to clean payment receipt after rollback")
        raise

    if duplicate:
        messages.info(
            request,
            "تمت معالجة طلب الدفع هذا مسبقًا، ولم يُنشأ طلب مكرر.",
        )
        return _subscription_redirect(membership)

    msg = format_html(
        """
        <div style="text-align:center; line-height:1.7;">
            <p style="margin:0 0 .4rem; font-weight:800; font-size:1.1rem;">تم استلام طلب التحويل البنكي بنجاح</p>
            <p style="margin:0 0 .5rem;">عدد العناصر: {} &bull; الإجمالي: {} ريال</p>
            <p style="margin:0; font-size:.9rem; opacity:.85;">الطلب قيد المراجعة، وسيتم تفعيل الباقة والخدمات المختارة خلال يوم عمل واحد كحد أقصى بعد التحقق من الإيصال.</p>
        </div>
        """,
        len(items),
        total,
    )
    messages.success(request, msg)
    if warnings:
        messages.warning(request, "لم تُضف بعض العناصر: " + " ، ".join(warnings))
    _notify_managers_of_group_payment(
        membership,
        total=total,
        labels=labels,
        payment_method=Payment.Method.BANK_TRANSFER,
    )
    return _subscription_redirect(membership)


def _activate_free_discount_order(
    request,
    membership,
    subscription,
    items,
    *,
    payment_method,
):
    """يفعّل طلباً صار مجموعه صفراً بكود خصم 100% — بلا إيصال وبلا بوابة.

    مجموعٌ صفري لا يقع إلا حين يكون الطلب بندَ اشتراكٍ وحيداً خُصم بالكامل:
    بقية البنود أسعارها موجبة دائماً، وبند الاشتراك يُرفض أصلاً لو كانت باقته
    مجانية قبل الخصم. الدفعة تُنشأ معتمدةً ويُطبَّق أثرها في نفس المعاملة،
    فلا تمرّ على مراجعة يدوية لمبلغٍ لا يوجد.
    """
    school = membership.school
    subscription_items = [
        it for it in items if it["purpose"] == Payment.Purpose.SUBSCRIPTION
    ]
    if (
        len(items) != 1
        or not subscription_items
        or subscription_items[0].get("discount_code") is None
    ):
        messages.error(request, "تعذّر إنشاء الطلب: مجموع الطلب غير صالح.")
        return _subscription_redirect(membership)

    item = subscription_items[0]
    code = item["discount_code"]
    note = (
        f"تفعيل مجاني بكود خصم {code.code} "
        f"(خصم {item.get('discount_amount', 0)} ريال من كامل قيمة الاشتراك)."
    )
    extra_note = (request.POST.get("notes") or "").strip()
    if extra_note:
        note = f"{note}\nملاحظة المدير: {extra_note}"

    today = timezone.localdate()
    pricing = _archive_pricing()
    duplicate = False
    try:
        with transaction.atomic():
            batch_ref, existing = _lock_checkout_submission(
                request,
                membership,
                items,
                payment_method,
            )
            if existing:
                duplicate = True
            else:
                payment = Payment.objects.create(
                    **_stamp_payer(
                        {
                            "school": school,
                            "subscription": subscription,
                            "requested_plan": item.get("requested_plan"),
                            "requested_teacher_limit": item.get(
                                "requested_teacher_limit"
                            ),
                            "purpose": Payment.Purpose.SUBSCRIPTION,
                            "amount": item["amount"],
                            "discount_code": code,
                            "discount_amount": item.get("discount_amount", 0),
                            "notes": note,
                            "batch_ref": batch_ref,
                            "payment_method": payment_method,
                            "status": Payment.Status.APPROVED,
                            "created_by": request.user,
                        },
                        membership,
                    )
                )
                reserve_redemption(
                    code,
                    school,
                    payment=payment,
                    batch_ref=batch_ref,
                    amount=item.get("discount_amount", Decimal("0.00")),
                )
                _apply_payment_effects(payment, today, pricing)
    except _CheckoutSubmissionError as exc:
        messages.error(request, str(exc))
        return _subscription_redirect(membership)
    except DiscountCodeError as exc:
        messages.error(request, str(exc))
        return _subscription_redirect(membership)
    except _ApprovalError as exc:
        messages.error(request, f"تعذّر تفعيل الاشتراك: {exc}")
        return _subscription_redirect(membership)

    if duplicate:
        messages.info(
            request,
            "تمت معالجة طلب الدفع هذا مسبقًا، ولم يُنشأ طلب مكرر.",
        )
        return _subscription_redirect(membership)

    messages.success(
        request,
        format_html(
            """
            <div style="text-align:center; line-height:1.7;">
                <p style="margin:0 0 .4rem; font-weight:800; font-size:1.1rem;">تم تفعيل الاشتراك مجاناً</p>
                <p style="margin:0;">غطّى كود الخصم <strong dir="ltr">{}</strong> كامل قيمة الاشتراك، وتم التفعيل فوراً دون الحاجة لأي دفع.</p>
            </div>
            """,
            code.code,
        ),
    )
    _notify_managers_of_group_payment(
        membership,
        total=Decimal("0.00"),
        labels=item["label"],
        payment_method=Payment.Method.BANK_TRANSFER,
    )
    return _subscription_redirect(membership)


def _manager_payment_membership(request):
    """اسمٌ تاريخي لِما صار «الفاعل»: مدير المدرسة أو المدير التنفيذي عنها."""
    return _resolve_payment_actor(request)
