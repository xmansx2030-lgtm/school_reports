from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
import logging
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.templatetags.static import static

from reports.moyasar_gateway import (
    MoyasarGatewayError,
    create_invoice as create_moyasar_invoice,
    fetch_invoice as fetch_moyasar_invoice,
    is_enabled as moyasar_is_enabled,
)

from .models import PersonalPayment, PersonalSubscription


logger = logging.getLogger(__name__)


class PersonalPaymentError(Exception):
    pass


def create_personal_checkout(*, request, workspace, plan) -> tuple[PersonalPayment, str]:
    if not moyasar_is_enabled():
        raise PersonalPaymentError("الدفع الإلكتروني غير متاح حاليًا.")
    email = (workspace.owner.email or "").strip().lower()
    if not email:
        raise PersonalPaymentError("يلزم إضافة البريد الإلكتروني قبل متابعة الدفع.")
    if plan.price <= 0 or plan.duration_days <= 0:
        raise PersonalPaymentError("هذه الباقة لا تتطلب دفعًا إلكترونيًا.")

    payment = PersonalPayment.objects.create(
        workspace=workspace,
        plan=plan,
        plan_name=plan.name,
        duration_days=plan.duration_days,
        amount=Decimal(plan.price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        customer_name=(workspace.owner.name or "المعلم").strip(),
        customer_email=email,
        school_name=workspace.school_name,
    )
    payment_ref = str(payment.pk)
    callback_url = request.build_absolute_uri(
        reverse("personal:moyasar_callback", args=[payment.pk])
    )
    success_url = request.build_absolute_uri(
        reverse("personal:moyasar_return", args=[payment.pk])
    )
    back_url = request.build_absolute_uri(reverse("personal:billing"))
    try:
        invoice = create_moyasar_invoice(
            amount=payment.amount,
            description=f"اشتراك مساحة {workspace.owner.personal_teacher_label} الشخصية: {payment.plan_name}",
            callback_url=callback_url,
            success_url=success_url,
            back_url=back_url,
            metadata={
                "personal_payment_ref": payment_ref,
                "personal_workspace_id": str(workspace.pk),
            },
        )
    except (MoyasarGatewayError, ImproperlyConfigured):
        payment.status = PersonalPayment.Status.FAILED
        payment.gateway_status = "invoice_creation_failed"
        payment.save(update_fields=["status", "gateway_status", "updated_at"])
        logger.exception("Moyasar personal invoice creation failed payment=%s", payment.pk)
        raise PersonalPaymentError("تعذّر بدء الدفع الإلكتروني. حاول مرة أخرى.") from None

    checkout_url = str(invoice.get("url") or "").strip()
    parsed = urlparse(checkout_url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != "checkout.moyasar.com":
        payment.status = PersonalPayment.Status.FAILED
        payment.gateway_status = "unsafe_checkout_url"
        payment.save(update_fields=["status", "gateway_status", "updated_at"])
        logger.error("Moyasar returned an unsafe personal checkout URL payment=%s", payment.pk)
        raise PersonalPaymentError("تعذّر التحقق من رابط الدفع. حاول مرة أخرى.")

    invoice_id = str(invoice.get("id") or "").strip()
    if not invoice_id:
        payment.status = PersonalPayment.Status.FAILED
        payment.gateway_status = "invoice_missing_id"
        payment.save(update_fields=["status", "gateway_status", "updated_at"])
        raise PersonalPaymentError("لم تُنشأ فاتورة دفع صالحة. حاول مرة أخرى.")

    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["lang"] = "ar"
    checkout_url = urlunparse(parsed._replace(query=urlencode(query)))
    payment.gateway_invoice_id = invoice_id
    payment.gateway_status = str(invoice.get("status") or "initiated")[:32]
    payment.save(update_fields=["gateway_invoice_id", "gateway_status", "updated_at"])
    return payment, checkout_url


def _verified_paid_invoice(payment: PersonalPayment, invoice: dict) -> str:
    if str(invoice.get("status") or "").strip().lower() != "paid":
        raise PersonalPaymentError("فاتورة ميسّر لم تصل إلى حالة مدفوعة.")
    if str(invoice.get("currency") or "").strip().upper() != "SAR":
        raise PersonalPaymentError("عملة فاتورة ميسّر لا تطابق مبلغ الطلب.")
    if str(invoice.get("id") or "").strip() != payment.gateway_invoice_id:
        raise PersonalPaymentError("رقم فاتورة ميسّر لا يطابق الطلب المحلي.")
    metadata = invoice.get("metadata") if isinstance(invoice.get("metadata"), dict) else {}
    if str(metadata.get("personal_payment_ref") or "") != str(payment.pk):
        raise PersonalPaymentError("مرجع فاتورة ميسّر لا يطابق طلب المعلم.")
    if str(metadata.get("personal_workspace_id") or "") != str(payment.workspace_id):
        raise PersonalPaymentError("فاتورة ميسّر لا تطابق المساحة الشخصية.")
    try:
        invoice_amount = int(invoice.get("amount"))
    except (TypeError, ValueError) as exc:
        raise PersonalPaymentError("مبلغ فاتورة ميسّر غير صالح.") from exc
    expected = int((payment.amount * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if invoice_amount != expected:
        raise PersonalPaymentError("مبلغ فاتورة ميسّر لا يطابق مبلغ الباقة.")
    attempts = invoice.get("payments") if isinstance(invoice.get("payments"), list) else []
    paid_attempt = next(
        (
            item for item in attempts
            if isinstance(item, dict)
            and str(item.get("status") or "").lower() in {"paid", "captured"}
        ),
        {},
    )
    return str(paid_attempt.get("id") or "")[:160]


def apply_paid_personal_invoice(payment_id, invoice: dict) -> PersonalPayment:
    snapshot = PersonalPayment.objects.select_related("plan", "workspace").filter(pk=payment_id).first()
    if snapshot is None:
        raise PersonalPaymentError("طلب الدفع الشخصي غير معروف.")
    capture_id = _verified_paid_invoice(snapshot, invoice)
    should_queue_email = False
    with transaction.atomic():
        payment = (
            PersonalPayment.objects.select_for_update()
            .select_related("plan", "workspace")
            .get(pk=payment_id)
        )
        if payment.status != PersonalPayment.Status.PAID:
            subscription = PersonalSubscription.objects.select_for_update().get(
                workspace_id=payment.workspace_id
            )
            today = timezone.localdate()
            start_date = today
            if subscription.is_current and subscription.end_date and subscription.end_date >= today:
                # Keep the current entitlement usable while adding the purchased days.
                # A future start_date would make is_current false immediately after renewal.
                end_date = subscription.end_date + timedelta(days=payment.duration_days)
                if subscription.plan_id == payment.plan_id:
                    start_date = subscription.start_date
            else:
                end_date = today + timedelta(days=payment.duration_days - 1)
            PersonalSubscription.objects.filter(pk=subscription.pk).update(
                plan_id=payment.plan_id,
                start_date=start_date,
                end_date=end_date,
                is_active=True,
            )
            payment.status = PersonalPayment.Status.PAID
            payment.gateway_status = "paid"
            payment.gateway_payment_id = capture_id
            payment.paid_at = timezone.now()
            payment.activated_at = payment.paid_at
            payment.save(update_fields=[
                "status", "gateway_status", "gateway_payment_id", "paid_at", "activated_at", "updated_at",
            ])
        should_queue_email = payment.email_sent_at is None
        if should_queue_email:
            from reports.utils import run_task_safe
            from .tasks import send_personal_payment_receipt_task

            transaction.on_commit(
                lambda: run_task_safe(send_personal_payment_receipt_task, str(payment.pk))
            )
    payment.refresh_from_db()
    return payment


def sync_personal_payment(payment_id) -> tuple[PersonalPayment, str]:
    payment = PersonalPayment.objects.filter(pk=payment_id).first()
    if payment is None or not payment.gateway_invoice_id:
        raise PersonalPaymentError("طلب الدفع الشخصي غير معروف.")
    invoice = fetch_moyasar_invoice(payment.gateway_invoice_id)
    status = str(invoice.get("status") or "").strip().lower()
    if status == "paid":
        payment = apply_paid_personal_invoice(payment.pk, invoice)
    elif status in {"failed", "canceled", "cancelled", "expired", "voided"}:
        local_status = (
            PersonalPayment.Status.FAILED if status == "failed"
            else PersonalPayment.Status.CANCELLED
        )
        PersonalPayment.objects.filter(
            pk=payment.pk, status=PersonalPayment.Status.PENDING
        ).update(status=local_status, gateway_status=status, updated_at=timezone.now())
        payment.refresh_from_db()
    else:
        PersonalPayment.objects.filter(
            pk=payment.pk, status=PersonalPayment.Status.PENDING
        ).update(gateway_status=status[:32], updated_at=timezone.now())
        payment.refresh_from_db()
    return payment, status


def build_personal_invoice_context(payment: PersonalPayment) -> dict:
    from reports.billing_invoices import _business_identity

    issued_at = payment.paid_at or timezone.now()
    local_issued_at = timezone.localtime(issued_at)
    reference = payment.gateway_payment_id or payment.gateway_invoice_id or str(payment.pk)
    amount = Decimal(payment.amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {
        "invoice_number": f"TQP-{local_issued_at.year}-{str(payment.pk).replace('-', '')[:12].upper()}",
        "issued_at": local_issued_at,
        "paid_at": local_issued_at,
        "status_label": "مدفوعة",
        "currency": "SAR",
        "business": _business_identity(),
        "customer": {
            "name": payment.customer_name,
            "code": "حساب شخصي",
            "identity_label": "نوع الحساب",
            "city": payment.school_name,
            "phone": payment.workspace.owner.phone,
            "email": payment.customer_email,
        },
        "items": [{
            "description": f"اشتراك {payment.workspace.owner.personal_teacher_label} الشخصي · {payment.plan_name} · {payment.duration_days} يومًا",
            "quantity": 1,
            "unit_price": amount,
            "amount": amount,
        }],
        "subtotal": amount,
        "discount_total": Decimal("0.00"),
        "discount_codes_label": "",
        "tax_amount": Decimal("0.00"),
        "total": amount,
        "payment_method": "دفع إلكتروني عبر ميسّر",
        "payment_reference": reference,
        "payer_name": payment.customer_name,
        "anchor_payment_id": payment.pk,
        "payment_ids": [payment.pk],
        "logo_url": static("img/logo1.png"),
    }


def generate_personal_invoice_pdf(payment: PersonalPayment, request=None) -> tuple[bytes, str]:
    from reports.pdf_invoice import generate_invoice_pdf

    context = build_personal_invoice_context(payment)
    pdf_bytes, _filename = generate_invoice_pdf(context=context, request=request)
    return pdf_bytes, f"tawtheeq-personal-invoice-{context['invoice_number']}.pdf"
