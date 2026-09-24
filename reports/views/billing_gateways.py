# reports/views/billing_gateways.py
# -*- coding: utf-8 -*-
"""بوّابة الدفع (ميسر) والمصالحة الدورية للطلبات المعلّقة.

**القاعدة الحاكمة في هذه الوحدة:** التفعيل يعتمد على التحقق من الفاتورة **لدى
البوّابة**، لا على وصول العميل إلى صفحة النجاح ولا على محتوى الاستدعاء الراجع.
والاستدعاء نفسه قد يصل مرتين، فكل مسار هنا يجب أن يكون خاملاً (idempotent) —
وذلك ما يضمنه ``effects_applied_at`` في ``_apply_payment_effects``.
"""
# -*- coding: utf-8 -*-

from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from itertools import pairwise
import json
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse
import uuid

from django.core import signing
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
from ..tamara_gateway import (
    TamaraGatewayError,
    authorise_order as authorise_tamara_order,
    build_checkout_payload as build_tamara_checkout_payload,
    capture_order as capture_tamara_order,
    create_checkout as create_tamara_checkout,
    get_order as get_tamara_order,
    is_customer_eligible as is_tamara_customer_eligible,
    is_enabled as tamara_is_enabled,
    verify_notification_token as verify_tamara_notification_token,
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
    _CheckoutSubmissionError,
    _lock_checkout_submission,
    _PaymentSelectionError,
    _subscription_quote_from_request,
    _build_unified_payment_items,
    _create_unified_payment,
    _activate_free_discount_order,
    _manager_payment_membership,
)
from ..discount_codes import (
    DiscountCodeError,
    release_dead_redemptions,
    reserve_redemption,
)


_TAMARA_RETURN_STATE_SALT = "reports.tamara.return"
_TAMARA_RETURN_STATE_MAX_AGE_SECONDS = 48 * 60 * 60


def _tamara_return_url(request, result: str, batch_ref: str) -> str:
    """Build a tamper-resistant return URL for one local Tamara order.

    Tamara's redirect does not add its order id to our URL. Carrying our random
    batch reference in a signed value lets the return view pull the authoritative
    order status from Tamara without trusting a browser-supplied payment result.
    """
    state = signing.dumps(batch_ref, salt=_TAMARA_RETURN_STATE_SALT, compress=True)
    path = reverse("reports:tamara_return", args=[result])
    return request.build_absolute_uri(f"{path}?{urlencode({'state': state})}")


def _tamara_batch_from_return_state(state: str) -> str:
    try:
        batch_ref = signing.loads(
            state,
            salt=_TAMARA_RETURN_STATE_SALT,
            max_age=_TAMARA_RETURN_STATE_MAX_AGE_SECONDS,
        )
    except signing.BadSignature as exc:
        raise _ApprovalError("مرجع عودة تمارا غير صالح أو منتهي.") from exc
    if not isinstance(batch_ref, str) or not batch_ref:
        raise _ApprovalError("مرجع عودة تمارا غير صالح.")
    return batch_ref


def _complete_moyasar_invoice(batch_ref: str, invoice: dict) -> None:
    invoice_id = str(invoice.get("id") or "").strip()
    invoice_status = str(invoice.get("status") or "").strip().lower()
    currency = str(invoice.get("currency") or "").strip().upper()
    metadata = invoice.get("metadata") if isinstance(invoice.get("metadata"), dict) else {}
    if invoice_status != "paid":
        raise _ApprovalError("فاتورة ميّسر لم تصل إلى حالة مدفوعة.")
    if currency != "SAR":
        raise _ApprovalError("عملة فاتورة ميّسر لا تطابق عملة الطلب.")
    if str(metadata.get("batch_ref") or "") != batch_ref:
        raise _ApprovalError("مرجع فاتورة ميّسر لا يطابق الطلب المحلي.")

    payment_attempts = invoice.get("payments") if isinstance(invoice.get("payments"), list) else []
    paid_attempt = next(
        (
            attempt
            for attempt in payment_attempts
            if isinstance(attempt, dict)
            and str(attempt.get("status") or "").lower() in {"paid", "captured"}
        ),
        {},
    )
    gateway_payment_id = str(paid_attempt.get("id") or "")[:160]

    pricing = _archive_pricing()
    today = timezone.localdate()
    with transaction.atomic():
        payments = list(
            Payment.objects.select_for_update()
            .filter(payment_method=Payment.Method.MOYASAR, batch_ref=batch_ref)
            .order_by("id")
        )
        if not payments or not invoice_id:
            raise _ApprovalError("طلب ميّسر غير معروف.")
        if any(payment.gateway_order_id != invoice_id for payment in payments):
            raise _ApprovalError("رقم فاتورة ميّسر لا يطابق الطلب المحلي.")

        expected_halalas = int(
            (
                sum((payment.amount for payment in payments), Decimal("0"))
                * Decimal("100")
            ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
        try:
            invoice_amount = int(invoice.get("amount"))
        except (TypeError, ValueError) as exc:
            raise _ApprovalError("مبلغ فاتورة ميّسر غير صالح.") from exc
        if invoice_amount != expected_halalas:
            raise _ApprovalError("مبلغ فاتورة ميّسر لا يطابق مبلغ الطلب.")

        payments.sort(key=lambda payment: _PURPOSE_APPLY_ORDER.get(payment.purpose, 99))
        for payment in payments:
            if payment.status == Payment.Status.APPROVED and payment.effects_applied_at:
                continue
            payment.status = Payment.Status.APPROVED
            payment.gateway_status = "paid"
            payment.gateway_capture_id = gateway_payment_id
            payment.gateway_completed_at = payment.gateway_completed_at or timezone.now()
            payment.save(
                update_fields=[
                    "status",
                    "gateway_status",
                    "gateway_capture_id",
                    "gateway_completed_at",
                    "updated_at",
                ]
            )
            _apply_payment_effects(payment, today, pricing)


def _sync_moyasar_batch(batch_ref: str) -> str:
    payment = (
        Payment.objects.filter(
            payment_method=Payment.Method.MOYASAR,
            batch_ref=batch_ref,
        )
        .order_by("id")
        .first()
    )
    if not payment or not payment.gateway_order_id:
        raise _ApprovalError("طلب ميّسر غير معروف.")
    invoice = fetch_moyasar_invoice(payment.gateway_order_id)
    invoice_status = str(invoice.get("status") or "").strip().lower()
    if invoice_status == "paid":
        _complete_moyasar_invoice(batch_ref, invoice)
    elif invoice_status in {"failed", "canceled", "expired", "voided"}:
        local_status = (
            Payment.Status.REJECTED
            if invoice_status == "failed"
            else Payment.Status.CANCELLED
        )
        Payment.objects.filter(
            payment_method=Payment.Method.MOYASAR,
            batch_ref=batch_ref,
            status=Payment.Status.PENDING,
        ).update(status=local_status, gateway_status=invoice_status)
        release_dead_redemptions(batch_ref=batch_ref)
    else:
        Payment.objects.filter(
            payment_method=Payment.Method.MOYASAR,
            batch_ref=batch_ref,
            status=Payment.Status.PENDING,
        ).update(gateway_status=invoice_status[:32])
    return invoice_status


@login_required(login_url="reports:login")
@ratelimit(key="user", rate="5/m", method="POST", block=True)
@require_http_methods(["POST"])
def moyasar_checkout_create(request):
    membership = _manager_payment_membership(request)
    if not moyasar_is_enabled():
        messages.error(request, "الدفع الإلكتروني غير متاح حاليًا.")
        return _subscription_redirect(membership)

    if not membership:
        messages.error(request, "هذه الخدمة مخصصة لإدارة المدرسة.")
        return redirect("reports:home")

    subscription = (
        SchoolSubscription.objects.filter(school=membership.school)
        .select_related("plan")
        .first()
    )
    try:
        items, warnings = _build_unified_payment_items(request, membership, subscription)
    except _PaymentSelectionError as exc:
        messages.error(request, str(exc))
        return _subscription_redirect(membership)

    total = sum((Decimal(str(item["amount"])) for item in items), Decimal("0"))

    # كود خصم غطّى الطلب كاملاً: لا فاتورة لدى البوابة لمبلغ صفري — تفعيل مباشر.
    if total <= 0:
        return _activate_free_discount_order(
            request,
            membership,
            subscription,
            items,
            payment_method=Payment.Method.MOYASAR,
        )

    _remember_acting_school(request, membership)
    labels = "، ".join(item["label"] for item in items)
    duplicate = False
    checkout_url = ""
    try:
        with transaction.atomic():
            batch_ref, existing = _lock_checkout_submission(
                request,
                membership,
                items,
                Payment.Method.MOYASAR,
            )
            if existing:
                duplicate = True
            else:
                callback_url = request.build_absolute_uri(
                    reverse("reports:moyasar_callback", args=[batch_ref])
                )
                success_url = request.build_absolute_uri(
                    reverse("reports:moyasar_return", args=[batch_ref])
                )
                back_url = request.build_absolute_uri(
                    _subscription_redirect(membership).url
                )
                invoice = create_moyasar_invoice(
                    amount=total,
                    description=f"خدمات منصة توثيق: {labels}",
                    callback_url=callback_url,
                    success_url=success_url,
                    back_url=back_url,
                    metadata={
                        "batch_ref": batch_ref,
                        "school_id": str(membership.school_id),
                    },
                )

                checkout_url = str(invoice.get("url") or "").strip()
                parsed_checkout_url = urlparse(checkout_url)
                checkout_host = (parsed_checkout_url.hostname or "").lower()
                if (
                    parsed_checkout_url.scheme != "https"
                    or checkout_host != "checkout.moyasar.com"
                ):
                    logger.error("Moyasar returned an unsafe checkout URL")
                    raise _ApprovalError("تعذّر التحقق من رابط الدفع الإلكتروني.")

                checkout_query = dict(
                    parse_qsl(parsed_checkout_url.query, keep_blank_values=True)
                )
                checkout_query["lang"] = "ar"
                checkout_url = parsed_checkout_url._replace(
                    query=urlencode(checkout_query)
                ).geturl()

                invoice_id = str(invoice.get("id") or "").strip()
                gateway_status = str(invoice.get("status") or "initiated")[:32]
                note = (
                    f"[فاتورة دفع إلكتروني {batch_ref.upper()}] {labels} "
                    f"— الإجمالي {total} ريال."
                )
                for item in items:
                    payment = Payment.objects.create(
                        **_stamp_payer(
                            {
                                "school": membership.school,
                                "subscription": subscription,
                                "requested_plan": item.get("requested_plan"),
                                "requested_teacher_limit": item.get(
                                    "requested_teacher_limit"
                                ),
                                "purpose": item["purpose"],
                                "amount": item["amount"],
                                "discount_code": item.get("discount_code"),
                                "discount_amount": item.get("discount_amount", 0),
                                "archive_storage_gb": item.get(
                                    "archive_storage_gb", 0
                                ),
                                "notes": note,
                                "batch_ref": batch_ref,
                                "payment_method": Payment.Method.MOYASAR,
                                "gateway_order_id": invoice_id,
                                "gateway_checkout_id": invoice_id,
                                "gateway_status": gateway_status,
                                "created_by": request.user,
                            },
                            membership,
                        )
                    )
                    if item.get("discount_code") is not None:
                        reserve_redemption(
                            item["discount_code"],
                            membership.school,
                            payment=payment,
                            batch_ref=batch_ref,
                            amount=item.get("discount_amount", Decimal("0.00")),
                        )
    except _CheckoutSubmissionError as exc:
        messages.error(request, str(exc))
        return _subscription_redirect(membership)
    except (MoyasarGatewayError, ImproperlyConfigured):
        logger.exception("Moyasar invoice creation failed")
        messages.error(
            request,
            "تعذّر بدء الدفع الإلكتروني. حاول مجددًا أو استخدم طريقة أخرى.",
        )
        return _subscription_redirect(membership)
    except _ApprovalError as exc:
        messages.error(request, str(exc))
        return _subscription_redirect(membership)
    except DiscountCodeError as exc:
        # نُفد الكود بين التحقق والحجز؛ فاتورة البوابة اليتيمة تنتهي صلاحيتها
        # وحدها ولا يملك أحد رابط دفعها.
        messages.error(request, str(exc))
        return _subscription_redirect(membership)

    if duplicate:
        messages.info(
            request,
            "تمت معالجة طلب الدفع هذا مسبقًا، ولم تُنشأ فاتورة إلكترونية مكررة.",
        )
        return _subscription_redirect(membership)

    if warnings:
        messages.warning(request, "لم تُضف بعض العناصر: " + " ، ".join(warnings))
    _notify_managers_of_group_payment(
        membership,
        total=total,
        labels=labels,
        payment_method=Payment.Method.MOYASAR,
    )
    return redirect(checkout_url)


@require_http_methods(["GET"])
def moyasar_return(request, batch_ref: str):
    if not moyasar_is_enabled():
        messages.error(request, "الدفع الإلكتروني غير متاح حاليًا.")
        return _subscription_return_redirect(request)
    try:
        invoice_status = _sync_moyasar_batch(batch_ref)
    except (MoyasarGatewayError, ImproperlyConfigured, _ApprovalError):
        logger.exception("Moyasar return verification failed for batch %s", batch_ref)
        messages.error(request, "تعذّر التحقق من نتيجة الدفع الإلكتروني. سيُعاد التحقق تلقائيًا.")
    else:
        if invoice_status == "paid":
            messages.success(
                request,
                "تم تأكيد نجاح الدفع الإلكتروني وتفعيل الباقة والخدمات المختارة تلقائيًا.",
            )
        elif invoice_status in {"failed", "canceled", "expired", "voided"}:
            messages.error(request, "لم تكتمل عملية الدفع الإلكتروني. يمكنك إنشاء طلب جديد.")
        else:
            messages.info(request, "عملية الدفع الإلكتروني ما زالت بانتظار الإكمال.")
    return _subscription_return_redirect(request)


@login_required(login_url="reports:login")
@ratelimit(key="user", rate="10/m", method="POST", block=True)
@require_http_methods(["POST"])
def moyasar_checkout_cancel(request, payment_id: int):
    """Let a manager drop an electronic order they never paid.

    A customer who closes the Moyasar tab cannot get back to it — the checkout
    URL is single-use and is not kept — so without this the order sits pending
    forever and the school is stuck looking at a payment it cannot finish.

    Cancelling only touches our own records. If the customer somehow does pay
    the old link afterwards, the invoice becomes paid at Moyasar and the
    webhook activates the services anyway: _complete_moyasar_invoice keys off
    the invoice status, not off the local row being pending.
    """
    membership = _manager_payment_membership(request)
    payment = Payment.objects.filter(
        pk=payment_id,
        school=getattr(membership, "school", None),
        payment_method=Payment.Method.MOYASAR,
        status=Payment.Status.PENDING,
    ).first()
    if not membership or not payment or not payment.batch_ref:
        messages.error(request, "طلب الدفع الإلكتروني غير متاح للإلغاء.")
        return _subscription_redirect(membership)

    # Never cancel on our word alone — ask Moyasar first. A paid invoice whose
    # callback has not landed yet must be completed, not thrown away.
    try:
        invoice_status = _sync_moyasar_batch(payment.batch_ref)
    except (MoyasarGatewayError, ImproperlyConfigured, _ApprovalError):
        logger.exception("Moyasar cancel verification failed for batch %s", payment.batch_ref)
        messages.error(request, "تعذّر التحقق من حالة الطلب لدى مزود الدفع. حاول مجددًا.")
        return _subscription_redirect(membership)

    if invoice_status == "paid":
        messages.success(request, "الدفع مكتمل بالفعل، وتم تفعيل الخدمات المختارة.")
        return _subscription_redirect(membership)

    cancelled = Payment.objects.filter(
        payment_method=Payment.Method.MOYASAR,
        batch_ref=payment.batch_ref,
        status=Payment.Status.PENDING,
    ).update(status=Payment.Status.CANCELLED, gateway_status="customer_cancelled")
    release_dead_redemptions(batch_ref=payment.batch_ref)
    if cancelled:
        messages.success(request, "أُلغي الطلب غير المدفوع. يمكنك إنشاء طلب جديد متى شئت.")
    else:
        messages.info(request, "لم يعد الطلب معلّقًا.")
    return _subscription_redirect(membership)


@csrf_exempt
# Unauthenticated by design — Moyasar calls it — and safe because the invoice is
# re-fetched from Moyasar rather than trusted from the request body. The limit
# only stops an anonymous client from replaying it to generate database lookups
# and outbound gateway calls.
@ratelimit(key="ip", rate="60/m", method="POST", block=True)
@require_http_methods(["POST"])
def moyasar_callback(request, batch_ref: str):
    if not moyasar_is_enabled():
        return JsonResponse({"detail": "Moyasar is disabled."}, status=404)
    try:
        invoice_status = _sync_moyasar_batch(batch_ref)
    except (MoyasarGatewayError, ImproperlyConfigured, _ApprovalError):
        logger.exception("Moyasar callback verification failed for batch %s", batch_ref)
        return JsonResponse({"detail": "Could not verify invoice."}, status=502)
    return JsonResponse({"ok": True, "status": invoice_status})


def _tamara_risk_assessment(school, items):
    approved_rows = Payment.objects.filter(
        school=school,
        status=Payment.Status.APPROVED,
        amount__gt=0,
    ).values_list("batch_ref", "id", "payment_date")
    successful_orders = {}
    for batch_ref, payment_id, payment_date in approved_rows:
        successful_orders.setdefault(batch_ref or f"payment-{payment_id}", payment_date)

    paid_dates = sorted(successful_orders.values())
    today = timezone.localdate()
    duration_days = max(
        (
            getattr(item.get("requested_plan"), "days_duration", 0) or 0
            for item in items
        ),
        default=0,
    ) or 365

    def format_date(value):
        return value.strftime("%d-%m-%Y")

    return {
        "account_creation_date": format_date(school.created_at.date()),
        "total_order_count": len(successful_orders),
        "is_premium_customer": False,
        "date_first_paid": format_date(paid_dates[0]) if paid_dates else None,
        "date_last_paid": format_date(paid_dates[-1]) if paid_dates else None,
        "education": {
            "education_type": "School reporting platform subscription",
            "start_date": format_date(today),
            "end_date": format_date(today + timedelta(days=duration_days - 1)),
            "event_location": "Online",
            "purchase_type": "Subscription",
        },
    }


@login_required(login_url="reports:login")
@ratelimit(key="user", rate="5/m", method="POST", block=True)
@require_http_methods(["POST"])
def tamara_checkout_create(request):
    membership = _manager_payment_membership(request)
    if not tamara_is_enabled():
        messages.error(request, "الدفع عبر تمارا غير متاح حاليًا.")
        return _subscription_redirect(membership)
    if not membership:
        messages.error(request, "هذه الخدمة مخصصة لإدارة المدرسة.")
        return redirect("reports:home")

    subscription = (
        SchoolSubscription.objects.filter(school=membership.school)
        .select_related("plan")
        .first()
    )
    try:
        items, warnings = _build_unified_payment_items(request, membership, subscription)
    except _PaymentSelectionError as exc:
        messages.error(request, str(exc))
        return _subscription_redirect(membership)

    total = sum((Decimal(str(item["amount"])) for item in items), Decimal("0"))
    if total <= 0:
        return _activate_free_discount_order(
            request,
            membership,
            subscription,
            items,
            payment_method=Payment.Method.TAMARA,
        )

    city = (request.POST.get("tamara_city") or membership.school.city or "").strip()
    address = (request.POST.get("tamara_address") or "").strip()
    _remember_acting_school(request, membership)
    labels = "، ".join(item["label"] for item in items)
    user_agent = (request.headers.get("User-Agent") or "").lower()

    if not is_tamara_customer_eligible(
        amount=total,
        phone=request.user.phone,
        email=request.user.email,
    ):
        messages.warning(
            request,
            "تمارا غير متاحة لهذا الطلب حاليًا. يمكنك استخدام طريقة دفع أخرى.",
        )
        return _subscription_redirect(membership)

    duplicate = False
    checkout_url = ""
    try:
        with transaction.atomic():
            batch_ref, existing = _lock_checkout_submission(
                request,
                membership,
                items,
                Payment.Method.TAMARA,
            )
            if existing:
                duplicate = True
            else:
                order_reference = f"TWQ-{batch_ref.upper()}"
                payload = build_tamara_checkout_payload(
                    order_reference=order_reference,
                    items=items,
                    customer_name=request.user.name,
                    customer_phone=request.user.phone,
                    customer_email=request.user.email,
                    city=city,
                    address=address,
                    success_url=_tamara_return_url(request, "success", batch_ref),
                    failure_url=_tamara_return_url(request, "failure", batch_ref),
                    cancel_url=_tamara_return_url(request, "cancel", batch_ref),
                    risk_assessment=_tamara_risk_assessment(
                        membership.school, items
                    ),
                    is_mobile=any(
                        marker in user_agent
                        for marker in ("android", "iphone", "ipad", "mobile")
                    ),
                )
                checkout = create_tamara_checkout(payload)

                checkout_url = str(checkout.get("checkout_url") or "").strip()
                parsed_checkout_url = urlparse(checkout_url)
                checkout_host = (parsed_checkout_url.hostname or "").lower()
                if (
                    parsed_checkout_url.scheme != "https"
                    or checkout_host != "tamara.co"
                    and not checkout_host.endswith(".tamara.co")
                ):
                    logger.error("Tamara returned an unsafe checkout URL")
                    raise _ApprovalError("تعذّر التحقق من رابط الدفع عبر تمارا.")

                order_id = str(checkout["order_id"])
                checkout_id = str(checkout.get("checkout_id") or "")
                gateway_status = str(checkout.get("status") or "new").lower()[:32]
                note = (
                    f"[طلب تمارا {order_reference}] {labels} "
                    f"— الإجمالي {total} ريال."
                )
                for item in items:
                    payment = Payment.objects.create(
                        **_stamp_payer(
                            {
                                "school": membership.school,
                                "subscription": subscription,
                                "requested_plan": item.get("requested_plan"),
                                "requested_teacher_limit": item.get(
                                    "requested_teacher_limit"
                                ),
                                "purpose": item["purpose"],
                                "amount": item["amount"],
                                "discount_code": item.get("discount_code"),
                                "discount_amount": item.get("discount_amount", 0),
                                "archive_storage_gb": item.get(
                                    "archive_storage_gb", 0
                                ),
                                "notes": note,
                                "batch_ref": batch_ref,
                                "payment_method": Payment.Method.TAMARA,
                                "gateway_order_id": order_id,
                                "gateway_checkout_id": checkout_id,
                                "gateway_status": gateway_status,
                                "created_by": request.user,
                            },
                            membership,
                        )
                    )
                    if item.get("discount_code") is not None:
                        reserve_redemption(
                            item["discount_code"],
                            membership.school,
                            payment=payment,
                            batch_ref=batch_ref,
                            amount=item.get("discount_amount", Decimal("0.00")),
                        )
    except _CheckoutSubmissionError as exc:
        messages.error(request, str(exc))
        return _subscription_redirect(membership)
    except (TamaraGatewayError, ImproperlyConfigured):
        logger.exception("Tamara checkout creation failed")
        messages.error(
            request,
            "تعذّر بدء الدفع عبر تمارا. حاول مجددًا أو استخدم طريقة أخرى.",
        )
        return _subscription_redirect(membership)
    except _ApprovalError as exc:
        messages.error(request, str(exc))
        return _subscription_redirect(membership)
    except DiscountCodeError as exc:
        # The hosted checkout exists but its URL is never disclosed, so it will
        # expire without creating a locally payable order.
        messages.error(request, str(exc))
        return _subscription_redirect(membership)

    if duplicate:
        messages.info(
            request,
            "تمت معالجة طلب الدفع هذا مسبقًا، ولم يُنشأ طلب تمارا مكرر.",
        )
        return _subscription_redirect(membership)

    if warnings:
        messages.warning(request, "لم تُضف بعض العناصر: " + " ، ".join(warnings))
    _notify_managers_of_group_payment(
        membership,
        total=total,
        labels=labels,
        payment_method=Payment.Method.TAMARA,
    )
    return redirect(checkout_url)


@require_http_methods(["GET"])
def tamara_return(request, result: str):
    gateway_status = ""
    state = (request.GET.get("state") or "").strip()
    if state:
        try:
            batch_ref = _tamara_batch_from_return_state(state)
            payment = (
                Payment.objects.filter(
                    payment_method=Payment.Method.TAMARA,
                    batch_ref=batch_ref,
                    amount__gt=0,
                )
                .exclude(gateway_order_id="")
                .first()
            )
            if payment is None:
                raise _ApprovalError("طلب تمارا غير معروف.")
            gateway_status = _sync_tamara_order(payment.gateway_order_id)
        except (TamaraGatewayError, ImproperlyConfigured, _ApprovalError):
            # A webhook or the periodic reconciliation pass can still complete
            # the payment. Never turn a temporary verification failure into a
            # false failure message or, more importantly, browser-trusted success.
            logger.exception("Tamara return verification failed")

    if gateway_status == "fully_captured":
        messages.success(request, "تم تحصيل الدفعة عبر تمارا وتفعيل الاشتراك بنجاح.")
    elif gateway_status in {"declined", "expired", "canceled", "cancelled"}:
        messages.error(request, "لم تكتمل عملية الدفع عبر تمارا ولم يتم تفعيل أي خدمة.")
    elif result == "success":
        messages.info(
            request,
            "اكتملت خطوات الدفع لدى تمارا، ويجري التحقق من التحصيل. سيُفعّل الاشتراك تلقائيًا فور التأكيد.",
        )
    elif result == "cancel":
        messages.warning(request, "أُلغيت عملية الدفع عبر تمارا ولم يتم تفعيل أي خدمة.")
    else:
        messages.error(request, "لم تكتمل عملية الدفع عبر تمارا. يمكنك المحاولة مجددًا.")
    return _subscription_return_redirect(request)


@login_required(login_url="reports:login")
@ratelimit(key="user", rate="10/m", method="POST", block=True)
@require_http_methods(["POST"])
def tamara_checkout_cancel(request, payment_id: int):
    membership = _manager_payment_membership(request)
    payment = Payment.objects.filter(
        pk=payment_id,
        school=getattr(membership, "school", None),
        payment_method=Payment.Method.TAMARA,
        status=Payment.Status.PENDING,
    ).first()
    if not membership or not payment or not payment.gateway_order_id:
        messages.error(request, "طلب تمارا غير متاح للإلغاء.")
        return _subscription_redirect(membership)

    order_payments = Payment.objects.filter(
        school=membership.school,
        payment_method=Payment.Method.TAMARA,
        gateway_order_id=payment.gateway_order_id,
    )
    if order_payments.filter(
        Q(status=Payment.Status.APPROVED) | Q(effects_applied_at__isnull=False)
    ).exists():
        messages.error(request, "لا يمكن إلغاء طلب تم تحصيله أو تفعيله.")
        return _subscription_redirect(membership)

    try:
        gateway_status = str(
            get_tamara_order(payment.gateway_order_id).get("status") or ""
        ).lower()
    except (TamaraGatewayError, ImproperlyConfigured):
        logger.exception("Tamara cancel verification failed order=%s", payment.gateway_order_id)
        messages.error(request, "تعذّر التحقق من حالة الطلب لدى تمارا. حاول مجددًا.")
        return _subscription_redirect(membership)

    if gateway_status not in {"new", "canceled", "cancelled", "expired", "declined"}:
        messages.warning(
            request,
            "بدأت معالجة الدفع لدى تمارا، لذلك لا يمكن إلغاء الطلب من المنصة.",
        )
        return _subscription_redirect(membership)

    local_status = (
        Payment.Status.REJECTED
        if gateway_status == "declined"
        else Payment.Status.CANCELLED
    )
    order_payments.filter(status=Payment.Status.PENDING).update(
        status=local_status,
        gateway_status=(
            "customer_cancelled" if gateway_status == "new" else gateway_status
        ),
    )
    release_dead_redemptions(batch_ref=payment.batch_ref)
    messages.success(request, "أُلغي الطلب غير المدفوع. يمكنك إنشاء طلب جديد متى شئت.")
    return _subscription_redirect(membership)


def _complete_tamara_order(
    order_id: str,
    *,
    gateway_status: str,
    capture_id: str,
    captured_amount,
) -> None:
    if gateway_status != "fully_captured":
        raise _ApprovalError("طلب تمارا لم يصل إلى حالة التحصيل الكامل.")
    payments = list(
        Payment.objects.filter(
            payment_method=Payment.Method.TAMARA,
            gateway_order_id=order_id,
            amount__gt=0,
        ).order_by("id")
    )
    if not payments:
        raise _ApprovalError("طلب تمارا غير معروف.")

    expected_total = sum((payment.amount for payment in payments), Decimal("0"))
    try:
        actual_total = Decimal(str(captured_amount)).quantize(Decimal("0.01"))
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise _ApprovalError("مبلغ تحصيل تمارا غير صالح.") from exc
    if actual_total != expected_total.quantize(Decimal("0.01")):
        raise _ApprovalError("مبلغ تحصيل تمارا لا يطابق مبلغ الطلب.")

    pricing = _archive_pricing()
    with transaction.atomic():
        locked = list(
            Payment.objects.select_for_update()
            .filter(
                payment_method=Payment.Method.TAMARA,
                gateway_order_id=order_id,
                amount__gt=0,
            )
            .order_by("id")
        )
        locked.sort(key=lambda payment: _PURPOSE_APPLY_ORDER.get(payment.purpose, 99))
        for payment in locked:
            payment.status = Payment.Status.APPROVED
            payment.gateway_status = gateway_status[:32]
            payment.gateway_capture_id = capture_id[:160]
            payment.gateway_completed_at = payment.gateway_completed_at or timezone.now()
            payment.save(
                update_fields=[
                    "status",
                    "gateway_status",
                    "gateway_capture_id",
                    "gateway_completed_at",
                    "updated_at",
                ]
            )
            _apply_payment_effects(payment, timezone.localdate(), pricing)


def _record_tamara_refund(
    order_id: str, *, refund_id: str, refunded_amount
) -> None:
    try:
        amount = Decimal(str(refunded_amount)).quantize(Decimal("0.01"))
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise _ApprovalError("بيانات استرجاع تمارا غير صالحة.") from exc
    if amount <= 0 or not refund_id:
        raise _ApprovalError("بيانات استرجاع تمارا غير صالحة.")

    with transaction.atomic():
        originals = list(
            Payment.objects.select_for_update()
            .filter(
                payment_method=Payment.Method.TAMARA,
                gateway_order_id=order_id,
                amount__gt=0,
            )
            .order_by("id")
        )
        if not originals:
            raise _ApprovalError("طلب تمارا غير معروف.")
        if Payment.objects.filter(
            payment_method=Payment.Method.TAMARA,
            gateway_order_id=order_id,
            gateway_capture_id=refund_id,
            amount__lt=0,
        ).exists():
            return

        captured_total = sum((payment.amount for payment in originals), Decimal("0"))
        refunded_total = -(
            Payment.objects.filter(
                payment_method=Payment.Method.TAMARA,
                gateway_order_id=order_id,
                amount__lt=0,
            ).aggregate(total=Sum("amount"))["total"]
            or Decimal("0")
        )
        if refunded_total + amount > captured_total:
            raise _ApprovalError("إجمالي استرجاع تمارا يتجاوز مبلغ الطلب.")

        original = originals[0]
        status = (
            "fully_refunded"
            if refunded_total + amount == captured_total
            else "partially_refunded"
        )
        Payment.objects.filter(pk__in=[payment.pk for payment in originals]).update(
            gateway_status=status
        )
        Payment.objects.create(
            school=original.school,
            subscription=original.subscription,
            requested_plan=original.requested_plan,
            requested_teacher_limit=original.requested_teacher_limit,
            purpose=original.purpose,
            amount=-amount,
            payment_method=Payment.Method.TAMARA,
            gateway_order_id=order_id,
            gateway_capture_id=refund_id[:160],
            gateway_status=status,
            gateway_completed_at=timezone.now(),
            batch_ref=original.batch_ref,
            status=Payment.Status.APPROVED,
            notes=f"استرجاع عبر تمارا للطلب {order_id}.",
            created_by=None,
            payer_kind=Payment.PayerKind.PLATFORM,
        )


def _tamara_captured_amount(response, fallback):
    for key in ("captured_amount", "total_amount"):
        value = response.get(key)
        if isinstance(value, dict) and value.get("amount") is not None:
            currency = str(value.get("currency") or "").strip().upper()
            if currency and currency != "SAR":
                raise _ApprovalError("عملة تحصيل تمارا لا تطابق عملة الطلب.")
            return value["amount"]
    return fallback


def _sync_tamara_order(order_id: str) -> str:
    payments = Payment.objects.filter(
        payment_method=Payment.Method.TAMARA,
        gateway_order_id=order_id,
        amount__gt=0,
    )
    if not payments.exists():
        raise _ApprovalError("طلب تمارا غير معروف.")
    total = payments.aggregate(total=Sum("amount"))["total"] or Decimal("0")
    response = get_tamara_order(order_id)
    status = str(response.get("status") or "").lower()
    capture_id = str(response.get("capture_id") or "")

    returned_order_id = str(response.get("order_id") or "").strip()
    if returned_order_id and returned_order_id != order_id:
        raise _ApprovalError("رقم طلب تمارا لا يطابق الطلب المحلي.")
    expected_reference = f"TWQ-{payments.first().batch_ref.upper()}"
    returned_reference = str(response.get("order_reference_id") or "").strip()
    if returned_reference and returned_reference != expected_reference:
        raise _ApprovalError("مرجع طلب تمارا لا يطابق الطلب المحلي.")
    _tamara_captured_amount(response, total)

    if status == "approved":
        response = authorise_tamara_order(order_id)
        status = str(response.get("status") or "authorised").lower()
    if status in {"authorised", "authorized"}:
        response = capture_tamara_order(order_id, total)
        status = str(response.get("status") or "").lower()
        capture_id = str(response.get("capture_id") or capture_id)
        # Some successful command responses contain no order status. A pull
        # after the command is the authority and also heals a lost webhook.
        if not status:
            response = get_tamara_order(order_id)
            status = str(response.get("status") or "").lower()
            capture_id = str(response.get("capture_id") or capture_id)
    if status == "fully_captured":
        _complete_tamara_order(
            order_id,
            gateway_status=status,
            capture_id=capture_id,
            captured_amount=_tamara_captured_amount(response, total),
        )
    elif status in {"declined", "expired", "canceled", "cancelled"}:
        local_status = (
            Payment.Status.REJECTED
            if status == "declined"
            else Payment.Status.CANCELLED
        )
        payments.filter(status=Payment.Status.PENDING).update(
            status=local_status,
            gateway_status=status,
        )
        first = payments.first()
        release_dead_redemptions(batch_ref=first.batch_ref if first else "")
    else:
        payments.filter(status=Payment.Status.PENDING).update(gateway_status=status[:32])
    return status


@csrf_exempt
@ratelimit(key="ip", rate="60/m", method="POST", block=True)
@require_http_methods(["POST"])
def tamara_webhook(request):
    if not tamara_is_enabled():
        return JsonResponse({"detail": "Tamara is disabled."}, status=404)

    header = request.headers.get("Authorization", "")
    header_token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    query_token = (request.GET.get("tamaraToken") or "").strip()
    if header_token and query_token and header_token != query_token:
        return JsonResponse({"detail": "Conflicting notification tokens."}, status=401)
    try:
        verify_tamara_notification_token(header_token or query_token)
        payload = json.loads(request.body.decode("utf-8"))
    except (
        TamaraGatewayError,
        ImproperlyConfigured,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ):
        return JsonResponse({"detail": "Invalid notification."}, status=401)

    order_id = str(payload.get("order_id") or "").strip()
    event_type = str(payload.get("event_type") or "").strip().lower()
    payments = Payment.objects.filter(
        payment_method=Payment.Method.TAMARA,
        gateway_order_id=order_id,
        amount__gt=0,
    )
    first_payment = payments.first()
    if not order_id or first_payment is None:
        return JsonResponse({"detail": "Unknown order."}, status=404)
    expected_reference = f"TWQ-{first_payment.batch_ref.upper()}"
    if str(payload.get("order_reference_id") or "") != expected_reference:
        return JsonResponse({"detail": "Order reference mismatch."}, status=409)

    terminal_statuses = {
        "order_declined": Payment.Status.REJECTED,
        "order_expired": Payment.Status.CANCELLED,
        "order_canceled": Payment.Status.CANCELLED,
        "order_cancelled": Payment.Status.CANCELLED,
    }
    if event_type in terminal_statuses:
        payments.filter(status=Payment.Status.PENDING).update(
            status=terminal_statuses[event_type],
            gateway_status=event_type.removeprefix("order_")[:32],
        )
        release_dead_redemptions(batch_ref=first_payment.batch_ref)
        return JsonResponse({"ok": True})

    if event_type == "order_refunded":
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        refunded = (
            data.get("refunded_amount")
            if isinstance(data.get("refunded_amount"), dict)
            else {}
        )
        try:
            _record_tamara_refund(
                order_id,
                refund_id=str(data.get("refund_id") or ""),
                refunded_amount=refunded.get("amount"),
            )
        except _ApprovalError:
            logger.exception("Tamara refund webhook processing failed order=%s", order_id)
            return JsonResponse({"detail": "Could not process refund."}, status=502)
        return JsonResponse({"ok": True})

    total = payments.aggregate(total=Sum("amount"))["total"] or Decimal("0")
    capture_id = ""
    captured_amount = total
    try:
        if event_type == "order_approved":
            response = authorise_tamara_order(order_id)
            gateway_status = str(response.get("status") or "authorised").lower()
            if gateway_status != "fully_captured":
                response = capture_tamara_order(order_id, total)
                gateway_status = str(response.get("status") or "").lower()
            capture_id = str(response.get("capture_id") or "")
            captured_amount = _tamara_captured_amount(response, total)
        elif event_type in {"order_authorised", "order_authorized"}:
            response = capture_tamara_order(order_id, total)
            gateway_status = str(response.get("status") or "").lower()
            capture_id = str(response.get("capture_id") or "")
            captured_amount = _tamara_captured_amount(response, total)
        elif event_type == "order_captured":
            gateway_status = "fully_captured"
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            capture_id = str(data.get("capture_id") or "")
            captured_amount = _tamara_captured_amount(data, total)
        else:
            return JsonResponse({"ok": True, "ignored": True})

        if gateway_status != "fully_captured":
            raise TamaraGatewayError("Tamara order was not fully captured.")
        _complete_tamara_order(
            order_id,
            gateway_status=gateway_status,
            capture_id=capture_id,
            captured_amount=captured_amount,
        )
    except (TamaraGatewayError, _ApprovalError):
        logger.exception("Tamara webhook processing failed order=%s", order_id)
        return JsonResponse({"detail": "Could not process order."}, status=502)
    return JsonResponse({"ok": True})


def _abandon_stale_gateway_batch(payment, *, cutoff) -> bool:
    """Cancel an electronic order the customer walked away from.

    The gateway has just told us the invoice is still unpaid. A hosted checkout
    URL is single-use and is not stored, so once the customer closes that tab
    there is no way back to it — the order can never be completed and leaving it
    pending only shows the school a payment it cannot finish, while blocking the
    add-on purchases that refuse to queue behind a pending request.

    Only the local rows are touched. Should the customer somehow still pay,
    the invoice turns paid at the gateway and the webhook activates the
    services regardless of what the local row says.
    """
    if payment.created_at > cutoff:
        return False

    updated = Payment.objects.filter(
        payment_method=payment.payment_method,
        batch_ref=payment.batch_ref,
        status=Payment.Status.PENDING,
    ).update(status=Payment.Status.CANCELLED, gateway_status="abandoned")
    release_dead_redemptions(batch_ref=payment.batch_ref)
    if updated:
        logger.info(
            "Cancelled abandoned %s order batch=%s rows=%s",
            payment.payment_method,
            payment.batch_ref,
            updated,
        )
    return bool(updated)


def reconcile_pending_gateway_payments(
    *,
    max_age_days: int = 7,
    limit: int = 200,
    abandon_after_minutes: int | None = None,
) -> dict:
    """Re-check gateway payments still sitting as PENDING and finish them.

    Activation depended entirely on the customer's browser returning or on the
    gateway's callback reaching us. Both can fail —
    a closed tab, a dropped webhook, a deploy restarting the container mid-call —
    and nothing retried. The school had paid, the money was captured, and the
    subscription silently never activated until someone complained.

    This walks recent pending gateway payments and re-runs the same verified
    completion path the callback uses: the amount is still re-checked against the
    gateway and effects are still applied once (``effects_applied_at``), so
    reconciling is safe to repeat.

    Payments older than ``max_age_days`` are left alone for manual review rather
    than retried forever.
    """
    summary = {
        "checked": 0,
        "activated": 0,
        "still_pending": 0,
        "abandoned": 0,
        "failed": 0,
        # Payment rows that had to be rescued, so the caller can raise an alert
        # per rescue rather than per sweep.
        "recovered_payment_ids": [],
    }
    cutoff = timezone.now() - timedelta(days=max(1, int(max_age_days)))

    if abandon_after_minutes is None:
        abandon_after_minutes = int(
            getattr(settings, "PAYMENT_ABANDON_AFTER_MINUTES", 60) or 0
        )
    # Zero disables abandonment, leaving the sweep purely a rescue pass.
    abandon_cutoff = (
        timezone.now() - timedelta(minutes=abandon_after_minutes)
        if abandon_after_minutes > 0
        else None
    )

    pending = (
        Payment.objects.filter(
            status=Payment.Status.PENDING,
            payment_method__in=[Payment.Method.MOYASAR, Payment.Method.TAMARA],
            created_at__gte=cutoff,
        )
        .exclude(gateway_order_id="")
        .order_by("created_at")
    )

    # One attempt per gateway order, not per payment row in the batch.
    seen: set[tuple[str, str]] = set()
    for payment in pending[: max(1, int(limit))]:
        key = (payment.payment_method, payment.batch_ref or payment.gateway_order_id)
        if key in seen:
            continue
        seen.add(key)
        summary["checked"] += 1

        try:
            if not payment.batch_ref:
                continue
            # A disabled gateway may still have historical pending rows.
            # Without credentials those rows cannot be reconciled, and
            # retrying every row every 20 minutes only floods production
            # logs with the same configuration exception. Keep the rows
            # pending for an operator to resolve once credentials exist.
            if payment.payment_method == Payment.Method.MOYASAR:
                credential = str(
                    getattr(settings, "MOYASAR_SECRET_KEY", "") or ""
                ).strip()
                if not credential:
                    summary["still_pending"] += 1
                    continue
                status = _sync_moyasar_batch(payment.batch_ref)
                settled = status == "paid"
            else:
                credential = str(
                    getattr(settings, "TAMARA_API_TOKEN", "") or ""
                ).strip()
                if not credential:
                    summary["still_pending"] += 1
                    continue
                status = _sync_tamara_order(payment.gateway_order_id)
                settled = status == "fully_captured"
            if settled:
                summary["activated"] += 1
                summary["recovered_payment_ids"].append(payment.pk)
            elif abandon_cutoff is not None and _abandon_stale_gateway_batch(
                payment, cutoff=abandon_cutoff
            ):
                summary["abandoned"] += 1
            else:
                summary["still_pending"] += 1
        except Exception:
            summary["failed"] += 1
            logger.exception(
                "Gateway reconciliation failed method=%s order=%s batch=%s",
                payment.payment_method,
                payment.gateway_order_id,
                payment.batch_ref,
            )

    return summary
