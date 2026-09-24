# reports/views/billing_school.py
# -*- coding: utf-8 -*-
"""ما يراه العميل: حالة اشتراكه، وسجلّ مدفوعاته، وإنشاء طلب دفع جديد."""
# -*- coding: utf-8 -*-

from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from itertools import pairwise
import json
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse
import uuid

from django.core.exceptions import ImproperlyConfigured
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
from ..billing_invoices import (
    InvoiceUnavailable,
    build_invoice_context,
    decorate_payments_with_invoice_access,
)
from ..pdf_invoice import generate_invoice_pdf
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
from ..moyasar_gateway import (
    MoyasarGatewayError,
    create_invoice as create_moyasar_invoice,
    fetch_invoice as fetch_moyasar_invoice,
    is_enabled as moyasar_is_enabled,
)
from ..tamara_gateway import is_enabled as tamara_is_enabled

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
    _issue_checkout_submission_key,
    _PaymentSelectionError,
    _subscription_quote_from_request,
    _build_unified_payment_items,
    _create_unified_payment,
    _validate_payment_receipt,
    _manager_payment_membership,
)


def subscription_expired(request):
    """صفحة تظهر عند انتهاء الاشتراك.

    نُمرّر معلومات المدرسة + تاريخ انتهاء الاشتراك إن توفّرت لعرضها في الرسالة.
    """
    school = None
    subscription = None
    is_manager = False

    try:
        user = getattr(request, "user", None)
        if user is not None and getattr(user, "is_authenticated", False):
            active_school = _get_active_school(request)
            memberships = (
                SchoolMembership.objects.filter(teacher=user, is_active=True)
                .select_related("school__subscription__plan", "school")
            )
            membership = None
            if active_school:
                membership = memberships.filter(school=active_school).first()
            if membership is None:
                membership = memberships.first()

            if membership is not None:
                school = membership.school
                is_manager = membership.role_type == SchoolMembership.RoleType.MANAGER
                subscription = getattr(school, "subscription", None)

                # لو أصبحت المدرسة النشطة اشتراكها ساري (بعد التبديل مثلاً)،
                # لا معنى لإظهار صفحة الانتهاء.
                with soft_fail(
                    "billing.active_subscription_redirect",
                    school_id=getattr(membership, "school_id", None),
                ):
                    if subscription is not None and not bool(subscription.is_expired):
                        if getattr(request.user, "is_superuser", False):
                            return redirect("reports:platform_admin_dashboard")
                        if _is_staff(request.user):
                            return redirect("reports:admin_dashboard")
                        return redirect("reports:home")
    except Exception:
        # لا نكسر الصفحة لو كانت هناك مشكلة في العضويات
        school = None
        subscription = None
        is_manager = False

    return render(
        request,
        "reports/subscription_expired.html",
        {"school": school, "subscription": subscription, "is_manager": is_manager},
    )


@login_required(login_url="reports:login")
def my_subscription(request):
    """صفحة الاشتراك: لمدير المدرسة، ولمدير المجموعة نيابةً عن إحدى مدارسه."""
    active_school = _get_active_school(request)

    membership = _resolve_payment_actor(request)
    if membership is None:
        messages.error(request, f"عفواً، هذه الصفحة مخصصة لـ{_school_manager_label(active_school)} فقط.")
        return redirect('reports:home')

    # ملاحظة: reverse OneToOne (school.subscription) يرفع DoesNotExist إن لم يوجد سجل
    subscription = (
        SchoolSubscription.objects.filter(school=membership.school)
        .select_related("plan")
        .first()
    )

    archive_addon = SchoolArchiveAddon.objects.filter(school=membership.school).first()
    if archive_addon is not None:
        with soft_fail("billing.sync_school_storage", school_id=membership.school_id):
            sync_school_archive_storage_usage(membership.school)
            archive_addon.refresh_from_db(fields=["storage_used_bytes", "updated_at"])
    pricing = _archive_pricing()
    pending_archive_addon_payment = Payment.objects.filter(
        school=membership.school,
        purpose=Payment.Purpose.ARCHIVE_ADDON,
        status=Payment.Status.PENDING,
    ).order_by("-created_at").first()
    pending_archive_storage_payment = Payment.objects.filter(
        school=membership.school,
        purpose=Payment.Purpose.WORK_STORAGE,
        status=Payment.Status.PENDING,
    ).order_by("-created_at").first()
    pending_archive_space_payment = Payment.objects.filter(
        school=membership.school,
        purpose=Payment.Purpose.ARCHIVE_SPACE,
        status=Payment.Status.PENDING,
    ).order_by("-created_at").first()

    # تظهر آخر 4 عمليات فقط
    payments = list(
        Payment.objects.filter(school=membership.school)
        .select_related("requested_plan", "payer_group", "discount_code")
        .order_by("-created_at")[:4]
    )
    decorate_payments_with_invoice_access(payments)
    renewal_catalog = _renewal_plan_catalog(
        subscription.plan_id if subscription else None
    )
    renewal_plans = [
        option["plan"]
        for group in renewal_catalog
        for option in group["options"]
    ]
    current_plan_ids = {plan.id for plan in renewal_plans}
    default_renewal_plan_id = (
        subscription.plan_id
        if subscription and subscription.plan_id in current_plan_ids
        else (renewal_plans[0].id if renewal_plans else None)
    )
    current_teacher_count = SchoolMembership.seats_used(membership.school)
    current_teacher_limit = int(getattr(subscription, "teacher_limit", 0) or 0)
    recommended_teacher_capacity = normalize_teacher_capacity(
        max(current_teacher_count, current_teacher_limit, 1)
    )
    flexible_catalog = build_flexible_pricing_catalog()
    storage_options = _archive_storage_options(active_only=True)

    context = {
        "subscription": subscription,
        "school": membership.school,
        # الصفحة نفسها تخدم دورين، فتحمل صفة الفاعل معها: اللافتة والحقل
        # المخفي والروابط كلها مشتقّة من هذين المفتاحين لا من تخمين القالب.
        "acting_on_behalf": membership.is_on_behalf,
        "acting_group": membership.group,
        "checkout_submission_key": _issue_checkout_submission_key(
            request, membership
        ),
        "group_payer_payment": _group_payer_badge(membership.school),
        "plans": renewal_plans,
        "renewal_catalog": renewal_catalog,
        "default_renewal_plan_id": default_renewal_plan_id,
        "current_teacher_count": current_teacher_count,
        "current_teacher_limit": current_teacher_limit,
        "recommended_teacher_capacity": recommended_teacher_capacity or 100,
        "flexible_pricing_catalog": flexible_catalog,
        "flexible_pricing_json": serialize_flexible_pricing_catalog(flexible_catalog),
        # Shown next to the calculated price so the manager sees exactly what the
        # subscription covers — and what is sold separately — before paying.
        "subscription_included_features": SUBSCRIPTION_INCLUDED_FEATURES,
        "subscription_addon_notes": SUBSCRIPTION_ADDON_NOTES,
        "payments": payments,
        "archive_addon": archive_addon,
        "archive_addon_price": pricing["addon_price"],
        "archive_included_storage_gb": pricing["included_storage_gb"],
        "archive_storage_block_gb": pricing["storage_block_gb"],
        "archive_storage_block_price": pricing["storage_block_price"],
        # منتجان مستقلان لمساحتين مستقلتين. عرضهما معًا في قائمة واحدة كان
        # يبيع للمدرسة توسعةً لغير المساحة التي امتلأت عندها. قائمة واحدة
        # تُقسَّم في الذاكرة، فلا يتضاعف عدد الاستعلامات على صفحة ثقيلة أصلًا.
        "archive_storage_options": [
            option
            for option in storage_options
            if option.bucket == ArchiveStorageOption.Bucket.WORK
        ],
        "archive_space_options": [
            option
            for option in storage_options
            if option.bucket == ArchiveStorageOption.Bucket.ARCHIVE
        ],
        "storage_overview": school_storage_overview(membership.school),
        # Reported separately: a full archive must never read as "the
        # platform is out of space" for the school's daily work.
        "archive_overview": school_archive_overview(membership.school),
        "pending_archive_addon_payment": pending_archive_addon_payment,
        "pending_archive_storage_payment": pending_archive_storage_payment,
        "pending_archive_space_payment": pending_archive_space_payment,
        "moyasar_enabled": moyasar_is_enabled(),
        "moyasar_environment": str(getattr(settings, "MOYASAR_ENVIRONMENT", "test") or "test"),
        "tamara_enabled": tamara_is_enabled(),
        "tamara_environment": str(getattr(settings, "TAMARA_ENVIRONMENT", "sandbox") or "sandbox"),
        "has_saved_archives": SchoolYearArchive.objects.filter(
            school=membership.school,
            status__in=[
                SchoolYearArchive.Status.READY,
                SchoolYearArchive.Status.PARTIAL,
            ],
        ).exists(),
    }
    return render(request, 'reports/my_subscription.html', context)


def subscription_history(request):
    """عرض سجل العمليات الكامل للاشتراكات"""
    active_school = _get_active_school(request)

    membership = _resolve_payment_actor(request)
    if membership is None:
        messages.error(request, f"عفواً، هذه الصفحة مخصصة لـ{_school_manager_label(active_school)} فقط.")
        return redirect('reports:home')

    # جلب كامل العمليات
    payments = (
        Payment.objects.filter(school=membership.school)
        .select_related("payer_group", "requested_plan", "discount_code")
        .order_by('-created_at')
    )

    paginator = Paginator(payments, 30)
    page_obj = paginator.get_page(request.GET.get("page"))
    page_obj.object_list = list(page_obj.object_list)
    decorate_payments_with_invoice_access(page_obj.object_list)
    
    context = {
        "school": membership.school,
        "payments": page_obj,
        "page_obj": page_obj,
        "acting_on_behalf": membership.is_on_behalf,
        "acting_group": membership.group,
    }
    return render(request, 'reports/subscription_history.html', context)


def _customer_invoice_context(request, payment_id: int) -> tuple[dict, _PaymentActor]:
    actor = _resolve_payment_actor(request)
    if actor is None:
        raise Http404("الفاتورة غير متاحة.")
    payment = get_object_or_404(
        Payment.objects.select_related("school", "requested_plan", "payer_group"),
        pk=payment_id,
        school=actor.school,
    )
    try:
        context = build_invoice_context(payment)
    except InvoiceUnavailable as exc:
        raise Http404(str(exc)) from exc
    context.update(
        {
            "school": actor.school,
            "acting_on_behalf": actor.is_on_behalf,
            "school_query": f"?school={actor.school_id}" if actor.is_on_behalf else "",
        }
    )
    return context, actor


@login_required(login_url="reports:login")
@never_cache
def subscription_invoice(request, payment_id: int):
    context, _actor = _customer_invoice_context(request, payment_id)
    response = render(request, "reports/billing_invoice.html", context)
    response["X-Robots-Tag"] = "noindex, nofollow"
    response["Cache-Control"] = "private, no-store"
    return response


@login_required(login_url="reports:login")
@never_cache
def subscription_invoice_pdf(request, payment_id: int):
    context, _actor = _customer_invoice_context(request, payment_id)
    pdf_bytes, filename = generate_invoice_pdf(context=context, request=request)
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["X-Robots-Tag"] = "noindex, nofollow"
    response["Cache-Control"] = "private, no-store"
    return response


@login_required(login_url="reports:login")
@ratelimit(key="user", rate="5/m", method="POST", block=True)
def payment_create(request):
    """صفحة رفع إيصال الدفع"""
    active_school = _get_active_school(request)

    membership = _resolve_payment_actor(request)

    if not membership:
        messages.error(request, f"عفواً، هذه الصفحة مخصصة لـ{_school_manager_label(active_school)} فقط.")
        return redirect('reports:home')

    subscription = (
        SchoolSubscription.objects.filter(school=membership.school)
        .select_related("plan")
        .first()
    )

    if request.method == 'POST':
        # ✅ مسار الدفع الموحّد: تجميع كل العناصر المختارة في إيصال واحد
        if (request.POST.get("unified") or "").strip() == "1":
            return _create_unified_payment(request, membership, subscription)

        receipt = request.FILES.get('receipt_image')
        notes = (request.POST.get('notes') or "").strip()
        payment_kind = (request.POST.get("payment_kind") or Payment.Purpose.SUBSCRIPTION).strip()
        plan_id = request.POST.get('plan_id')
        requested_plan = None
        pricing = _archive_pricing()

        try:
            _validate_payment_receipt(receipt)
        except _PaymentSelectionError as exc:
            messages.error(request, str(exc))
            return _subscription_redirect(membership)

        if payment_kind == Payment.Purpose.ARCHIVE_ADDON:
            if Payment.objects.filter(
                school=membership.school,
                purpose=Payment.Purpose.ARCHIVE_ADDON,
                status=Payment.Status.PENDING,
            ).exists():
                messages.warning(request, "لديك طلب تفعيل أرشفة قيد المراجعة بالفعل.")
                return _subscription_redirect(membership)

            amount = pricing["addon_price"]
            request_notes = "طلب تفعيل/تجديد إضافة الأرشفة السنوية."
            if notes:
                request_notes = f"{request_notes}\n{notes}"

            Payment.objects.create(**_stamp_payer({
                "school": membership.school,
                "subscription": subscription,
                "requested_plan": None,
                "purpose": Payment.Purpose.ARCHIVE_ADDON,
                "amount": amount,
                "receipt_image": receipt,
                "notes": request_notes,
                "created_by": request.user,
            }, membership))
            messages.success(request, "تم رفع طلب تفعيل الأرشفة، وسيظهر الأرشيف فور اعتماد مدير النظام.")
            return _subscription_redirect(membership)

        if payment_kind in (
            Payment.Purpose.WORK_STORAGE,
            Payment.Purpose.ARCHIVE_SPACE,
        ):
            is_archive_space = payment_kind == Payment.Purpose.ARCHIVE_SPACE
            bucket = (
                ArchiveStorageOption.Bucket.ARCHIVE
                if is_archive_space
                else ArchiveStorageOption.Bucket.WORK
            )
            space_label = "مساحة الأرشفة السنوية" if is_archive_space else "مساحة عمل المدرسة"

            if Payment.objects.filter(
                school=membership.school,
                purpose=payment_kind,
                status=Payment.Status.PENDING,
            ).exists():
                messages.warning(
                    request, f"لديك طلب زيادة {space_label} قيد المراجعة بالفعل."
                )
                return _subscription_redirect(membership)

            if is_archive_space and not school_archive_enabled(membership.school):
                # حدّ الأرشفة يعيش على الملحق، فبيع توسعة بلا ملحق يأخذ مالاً
                # مقابل مساحة لا مكان لها.
                messages.error(
                    request,
                    "فعّل خدمة الأرشفة السنوية أولاً، ثم يمكنك زيادة مساحتها.",
                )
                return _subscription_redirect(membership)

            option_field = (
                "archive_space_option_id" if is_archive_space else "archive_storage_option_id"
            )
            option_id = request.POST.get(option_field)
            storage_option = None
            if option_id:
                try:
                    storage_option = ArchiveStorageOption.objects.get(
                        pk=option_id, is_active=True, bucket=bucket
                    )
                except (ArchiveStorageOption.DoesNotExist, ValueError, TypeError):
                    storage_option = None

            if storage_option is None:
                messages.error(request, f"اختر خيار زيادة {space_label} صالحاً.")
                return _subscription_redirect(membership)

            storage_gb = int(storage_option.storage_gb or 0)
            amount = storage_option.price

            request_notes = f"طلب زيادة {space_label} بمقدار {storage_gb}GB."
            if notes:
                request_notes = f"{request_notes}\n{notes}"

            Payment.objects.create(**_stamp_payer({
                "school": membership.school,
                "subscription": subscription,
                "requested_plan": None,
                "purpose": payment_kind,
                "archive_storage_gb": storage_gb,
                "amount": amount,
                "receipt_image": receipt,
                "notes": request_notes,
                "created_by": request.user,
            }, membership))
            messages.success(
                request,
                f"تم رفع طلب زيادة {space_label}، وسيتم تحديث الحد فور الاعتماد.",
            )
            return _subscription_redirect(membership)

        # 1. محاولة أخذ الباقة من اختيار المستخدم
        if plan_id:
            requested_plan = SubscriptionPlan.objects.filter(
                pk=plan_id,
                is_active=True,
                price__gt=0,
            ).first()
            if requested_plan is None:
                messages.error(request, "الباقة المختارة غير متاحة للتجديد.")
                return _subscription_redirect(membership)
        
        # 2. إذا لم يختر، نأخذ الباقة الحالية
        if (
            not requested_plan
            and subscription
            and subscription.plan.is_active
            and subscription.plan.price > 0
        ):
            requested_plan = subscription.plan

        # التحقق النهائي
        if not requested_plan:
            messages.error(request, "يرجى اختيار باقة للاشتراك/التجديد.")
            return _subscription_redirect(membership)

        try:
            quote = _subscription_quote_from_request(request, membership.school, requested_plan)
        except _PaymentSelectionError as exc:
            messages.error(request, str(exc))
            return _subscription_redirect(membership)
        requested_plan = quote["plan"]
        requested_teacher_limit = int(quote["capacity"] or 0)
        amount = quote["price"]
        try:
            if amount is None or float(amount) <= 0:
                messages.error(request, "لا يمكن إنشاء طلب دفع لأن الباقة المختارة مجانية/غير صالحة.")
                return _subscription_redirect(membership)
        except (TypeError, ValueError):
            # مبلغٌ غير رقمي لا يجوز أن يمضي إلى إنشاء الدفعة: الفحص كان
            # يُبتلع فيُنشأ طلبٌ بقيمةٍ لا يعرفها أحد.
            _degraded("billing.non_numeric_amount", school_id=membership.school_id)
            messages.error(request, "تعذّر تحديد قيمة الطلب. أعد المحاولة أو راجع الدعم.")
            return _subscription_redirect(membership)

        Payment.objects.create(**_stamp_payer({
            "school": membership.school,
            "subscription": subscription,
            "requested_plan": requested_plan,
            "requested_teacher_limit": requested_teacher_limit,
            "purpose": Payment.Purpose.SUBSCRIPTION,
            "amount": amount,
            "receipt_image": receipt,
            "notes": notes,
            "created_by": request.user,
        }, membership))
        
        msg = format_html("""
        <div style="text-align: center; line-height: 1.6;">
            <p style="margin-bottom: 0.5rem; font-weight: 700; font-size: 1.1rem;">تم استلام طلبك بنجاح ✅</p>
            <p style="margin-bottom: 0.5rem;">جاري مراجعة الإيصال والتحقق منه، وسيتم تفعيل الباقة التالية فور الاعتماد:</p>
            <div style="background: rgba(255,255,255,0.2); border: 1px solid rgba(255,255,255,0.3); padding: 0.75rem 1rem; border-radius: 12px; display: inline-block; margin-top: 0.5rem; color: #fff;">
                <div style="font-weight: 800; font-size: 1.1rem; margin-bottom: 0.25rem;">{}</div>
                <div style="font-size: 0.9rem;">
                    السعر: {} ريال &bull; السعة: {} معلماً &bull; المدة: {} يوم
                </div>
            </div>
        </div>
        """, requested_plan.name, amount, requested_teacher_limit, requested_plan.days_duration)
        messages.success(request, msg)
        return _subscription_redirect(membership)
            
    return _subscription_redirect(membership)


def faq(request: HttpRequest) -> HttpResponse:
    """صفحة الأسئلة الشائعة"""
    return render(request, "reports/faq.html")


def privacy_policy(request: HttpRequest) -> HttpResponse:
    """صفحة سياسة الخصوصية"""
    return render(request, "reports/privacy_policy.html")

@login_required(login_url="reports:login")
@ratelimit(key="user", rate="15/m", method="POST", block=True)
@require_http_methods(["POST"])
def discount_code_check(request):
    """تحقق فوري من كود الخصم لعرض قيمته في ملخص الطلب.

    نتيجة هذا المسار للعرض فقط؛ التحقق الملزم يُعاد بالكامل عند إنشاء الطلب،
    والحجز يجري هناك تحت قفل الصف — فالمتصفح لا يُصدّق على شيء.
    """
    from ..discount_codes import DiscountCodeError, find_usable_code

    actor = _resolve_payment_actor(request)
    if actor is None:
        return JsonResponse(
            {"ok": False, "message": "هذه الخدمة مخصصة لإدارة المدرسة."}, status=403
        )

    try:
        code = find_usable_code(request.POST.get("discount_code") or "", actor.school)
    except DiscountCodeError as exc:
        return JsonResponse({"ok": False, "message": str(exc)})

    payload = {
        "ok": True,
        "code": code.code,
        "discount_type": code.discount_type,
        "value": str(code.value),
        "display_value": code.display_value,
        "message": f"تم قبول الكود {code.code} — خصم {code.display_value} على بند الاشتراك.",
    }

    # لو أرسل العميل سياق الباقة الحالية نعيد المبلغ المخصوم بحساب الخادم نفسه.
    plan_id = (request.POST.get("plan_id") or "").strip()
    if plan_id.isdigit():
        plan = SubscriptionPlan.objects.filter(
            pk=plan_id, is_active=True, price__gt=0
        ).first()
        if plan is not None:
            try:
                quote = _subscription_quote_from_request(request, actor.school, plan)
            except _PaymentSelectionError:
                quote = None
            if quote is not None:
                original = Decimal(str(quote["price"]))
                discount = code.discount_for(original)
                payload.update(
                    {
                        "original_amount": str(original),
                        "discount_amount": str(discount),
                        "amount_after": str(original - discount),
                    }
                )
    return JsonResponse(payload)
