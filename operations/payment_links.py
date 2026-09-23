from __future__ import annotations

import hmac
import logging
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urlparse

from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.utils.dateparse import parse_datetime

from core.task_dispatch import enqueue_named_task
from reports.moyasar_gateway import MoyasarGatewayError, fetch_invoice

from .models import OperationsPaymentLink
from .task_names import SEND_PAYMENT_PAID_PUSH_TASK

logger = logging.getLogger(__name__)


class PaymentLinkIntegrityError(Exception):
    """The provider response does not match the locally recorded request."""


def callback_token(public_id) -> str:
    return salted_hmac(
        "operations.payment-link-callback",
        str(public_id),
    ).hexdigest()[:40]


def callback_token_is_valid(public_id, token: str) -> bool:
    return hmac.compare_digest(callback_token(public_id), str(token or ""))


def validate_checkout_url(value: str) -> str:
    checkout_url = str(value or "").strip()
    parsed = urlparse(checkout_url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != "checkout.moyasar.com":
        raise PaymentLinkIntegrityError("رابط الدفع المستلم من ميّسر غير صالح.")
    return checkout_url


def _expected_halalas(link: OperationsPaymentLink) -> int:
    return int((link.amount * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _paid_at(invoice: dict) -> datetime:
    for payment in invoice.get("payments") or []:
        if not isinstance(payment, dict) or str(payment.get("status") or "").lower() not in {
            "paid",
            "captured",
        }:
            continue
        candidate = payment.get("captured_at") or payment.get("updated_at") or payment.get("created_at")
        parsed = parse_datetime(str(candidate or ""))
        if parsed is not None:
            return parsed
    parsed = parse_datetime(str(invoice.get("updated_at") or ""))
    return parsed or timezone.now()


def apply_invoice_state(link: OperationsPaymentLink, invoice: dict) -> OperationsPaymentLink:
    invoice_id = str(invoice.get("id") or "").strip()
    if not invoice_id:
        raise PaymentLinkIntegrityError("استجابة ميّسر لا تحتوي رقم الفاتورة.")
    if link.gateway_invoice_id and invoice_id != link.gateway_invoice_id:
        raise PaymentLinkIntegrityError("رقم فاتورة ميّسر لا يطابق رابط الدفع المحلي.")

    try:
        provider_amount = int(invoice.get("amount"))
    except (TypeError, ValueError) as exc:
        raise PaymentLinkIntegrityError("مبلغ فاتورة ميّسر غير صالح.") from exc
    if provider_amount != _expected_halalas(link):
        raise PaymentLinkIntegrityError("مبلغ فاتورة ميّسر لا يطابق المبلغ المحلي.")
    if str(invoice.get("currency") or "SAR").upper() != link.currency:
        raise PaymentLinkIntegrityError("عملة فاتورة ميّسر لا تطابق العملة المحلية.")

    provider_status = str(invoice.get("status") or "").strip().lower()
    if provider_status not in OperationsPaymentLink.Status.values:
        raise PaymentLinkIntegrityError("حالة فاتورة ميّسر غير معروفة.")

    provider_url = str(invoice.get("url") or link.gateway_url or "").strip()
    if provider_url:
        provider_url = validate_checkout_url(provider_url)

    should_notify_paid = (
        provider_status == OperationsPaymentLink.Status.PAID
        and link.paid_notification_sent_at is None
    )
    link.gateway_invoice_id = invoice_id
    link.gateway_url = provider_url
    link.status = provider_status
    link.provider_error = ""
    link.last_synced_at = timezone.now()
    if provider_status == OperationsPaymentLink.Status.PAID and link.paid_at is None:
        link.paid_at = _paid_at(invoice)
    link.save(
        update_fields=(
            "gateway_invoice_id",
            "gateway_url",
            "status",
            "provider_error",
            "last_synced_at",
            "paid_at",
            "updated_at",
        )
    )
    if should_notify_paid:
        transaction.on_commit(
            lambda link_id=link.pk: enqueue_named_task(
                SEND_PAYMENT_PAID_PUSH_TASK,
                args=(link_id,),
            )
        )
    return link


def sync_payment_link(link: OperationsPaymentLink) -> OperationsPaymentLink:
    if not link.gateway_invoice_id:
        raise PaymentLinkIntegrityError("لم يُربط السجل بفاتورة ميّسر بعد.")
    invoice = fetch_invoice(link.gateway_invoice_id)
    with transaction.atomic():
        locked = OperationsPaymentLink.objects.select_for_update().get(pk=link.pk)
        return apply_invoice_state(locked, invoice)


def reconcile_open_payment_links(*, limit: int = 100) -> dict[str, int]:
    if limit < 1:
        return {"checked": 0, "updated": 0, "failed": 0}
    links = list(
        OperationsPaymentLink.objects.filter(
            status__in=(
                OperationsPaymentLink.Status.INITIATED,
                OperationsPaymentLink.Status.ON_HOLD,
            ),
            gateway_invoice_id__isnull=False,
        ).order_by("last_synced_at", "created_at")[: min(limit, 500)]
    )
    summary = {"checked": 0, "updated": 0, "failed": 0}
    for link in links:
        summary["checked"] += 1
        previous_status = link.status
        try:
            synced = sync_payment_link(link)
        except (MoyasarGatewayError, ImproperlyConfigured, PaymentLinkIntegrityError):
            summary["failed"] += 1
            logger.exception("Operations payment-link reconciliation failed for %s", link.public_id)
            continue
        if synced.status != previous_status:
            summary["updated"] += 1
    return summary
