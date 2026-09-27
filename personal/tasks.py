from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.db.models import Q
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from reports.email_branding import platform_url, render_branded_email

from .billing import generate_personal_invoice_pdf
from .models import PersonalNotice, PersonalNoticeRecipient, PersonalPayment, PersonalSubscription


logger = logging.getLogger(__name__)


@shared_task(ignore_result=True, soft_time_limit=120, time_limit=300)
def check_personal_subscription_expiry_task() -> dict:
    """Remind owners of dated personal subscriptions before they expire."""
    from reports.tasks import _email_delivery_configured, _is_valid_email, _periodic_lock

    enabled = bool(getattr(settings, "SUBSCRIPTION_EXPIRY_REMINDER_ENABLED", True))
    summary = {"enabled": enabled, "subscriptions_checked": 0, "reminders_sent": 0, "emails_sent": 0, "skipped_duplicate": 0}
    if not enabled or not _periodic_lock("check_personal_subscription_expiry", ttl=300):
        return summary

    today = timezone.localdate()
    reminder_days = set(getattr(settings, "SUBSCRIPTION_EXPIRY_REMINDER_DAYS", [14, 7, 3, 1]))
    if not reminder_days:
        return summary
    email_enabled = bool(getattr(settings, "SUBSCRIPTION_EXPIRY_REMINDER_EMAIL_ENABLED", True)) and _email_delivery_configured()
    cutoff = timezone.now() - timedelta(hours=24)
    subscriptions = (
        PersonalSubscription.objects.filter(
            is_active=True,
            start_date__lte=today,
            end_date__gte=today,
            end_date__lte=today + timedelta(days=max(reminder_days)),
            workspace__owner__is_active=True,
        )
        .select_related("workspace__owner", "plan")
    )
    billing_url = platform_url(reverse("personal:billing"))
    for subscription in subscriptions.iterator():
        summary["subscriptions_checked"] += 1
        days_left = (subscription.end_date - today).days
        if days_left not in reminder_days:
            continue

        owner = subscription.workspace.owner
        day_word = "يوم" if days_left == 1 else "أيام"
        title = f"اشتراكك الشخصي ينتهي خلال {days_left} {day_word}"
        if PersonalNoticeRecipient.objects.filter(
            workspace=subscription.workspace,
            notice__title=title,
            notice__created_at__gte=cutoff,
        ).exists():
            summary["skipped_duplicate"] += 1
            continue

        message = (
            f"باقة {subscription.plan.name} الشخصية تنتهي في {subscription.end_date:%Y-%m-%d}. "
            "افتح صفحة الاشتراك لتجديدها قبل توقف إنشاء أعمال جديدة في مساحتك الشخصية."
        )
        with transaction.atomic():
            notice = PersonalNotice.objects.create(title=title, message=message)
            PersonalNoticeRecipient.objects.create(notice=notice, workspace=subscription.workspace)
        summary["reminders_sent"] += 1

        if email_enabled and owner.email and _is_valid_email(owner.email):
            try:
                html_message = render_branded_email(
                    "subscription_expiry.html",
                    recipient_name=owner.name,
                    email_title=title,
                    email_eyebrow=f"اشتراك {owner.personal_teacher_label} الشخصي",
                    email_preheader=message,
                    email_tone="warning",
                    action_url=billing_url,
                    action_label="إدارة الاشتراك والتجديد",
                    meta_items=[
                        {"label": "الباقة", "value": subscription.plan.name},
                        {"label": "تاريخ الانتهاء", "value": subscription.end_date.isoformat()},
                        {"label": "المدة المتبقية", "value": f"{days_left} {day_word}"},
                    ],
                    notice_title="حافظ على استمرارية مساحتك الشخصية",
                    notice_text="أكمل التجديد قبل تاريخ الانتهاء لتواصل إنشاء التقارير والشواهد.",
                )
                email = EmailMultiAlternatives(
                    subject=f"{title} | منصة توثيق",
                    body=f"مرحبًا {owner.name}،\n{message}\n{billing_url}",
                    from_email=(getattr(settings, "DEFAULT_FROM_EMAIL", "") or "no-reply@tawtheeq-ksa.com").strip(),
                    to=[owner.email],
                )
                email.attach_alternative(html_message, "text/html")
                email.send(fail_silently=False)
                summary["emails_sent"] += 1
            except Exception:
                logger.exception("Personal subscription expiry email failed subscription=%s", subscription.pk)

    logger.info("Personal subscription expiry reminder result: %s", summary)
    return summary


@shared_task(
    bind=True,
    ignore_result=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=5,
)
def send_personal_payment_receipt_task(self, payment_id: str) -> dict:
    from reports.tasks import _email_delivery_configured

    now = timezone.now()
    with transaction.atomic():
        payment = (
            PersonalPayment.objects.select_for_update()
            .select_related("workspace__owner", "plan")
            .filter(pk=payment_id, status=PersonalPayment.Status.PAID)
            .first()
        )
        if payment is None or payment.email_sent_at:
            return {"sent": 0, "skipped": 1}
        if payment.email_sending_at and payment.email_sending_at > now - timedelta(minutes=15):
            return {"sent": 0, "skipped": 1}
        payment.email_sending_at = now
        payment.save(update_fields=["email_sending_at", "updated_at"])

    if not bool(getattr(settings, "SUBSCRIPTION_ACTIVATION_EMAIL_ENABLED", True)) or not _email_delivery_configured():
        PersonalPayment.objects.filter(pk=payment_id).update(email_sending_at=None)
        logger.warning("Personal payment receipt skipped because email delivery is disabled or unconfigured payment=%s", payment_id)
        return {"sent": 0, "skipped": 1}

    try:
        from django.urls import reverse

        payment.refresh_from_db()
        pdf_bytes, filename = generate_personal_invoice_pdf(payment)
        invoice_url = platform_url(reverse("personal:payment_invoice", args=[payment.pk]))
        subscription = payment.workspace.subscription
        start_date = subscription.start_date.isoformat() if subscription.start_date else ""
        end_date = subscription.end_date.isoformat() if subscription.end_date else ""
        email_context = {
            "recipient_name": payment.customer_name,
            "plan_name": payment.plan_name,
            "amount": f"{payment.amount:.2f}",
            "currency": "SAR",
            "invoice_number": f"TQP-{timezone.localtime(payment.paid_at or now).year}-{str(payment.pk).replace('-', '')[:12].upper()}",
            "invoice_url": invoice_url,
            "start_date": start_date,
            "end_date": end_date,
            "school_name": payment.school_name,
        }
        plain_message = render_to_string(
            "reports/emails/personal_subscription_activated.txt", email_context
        ).strip()
        html_message = render_branded_email(
            "personal_subscription_activated.html",
            recipient_name=payment.customer_name,
            email_title="تم تفعيل باقتك الشخصية",
            email_eyebrow=f"اشتراك {payment.workspace.owner.personal_teacher_label} الشخصي",
            email_preheader="تم تأكيد الدفع وتفعيل مساحتك الشخصية، والفاتورة مرفقة.",
            email_tone="success",
            plan_name=payment.plan_name,
            action_url=invoice_url,
            action_label="عرض الفاتورة في حسابك",
            meta_items=[
                {"label": "الباقة", "value": payment.plan_name},
                {"label": "المبلغ المدفوع", "value": f"{payment.amount:.2f} SAR"},
                {"label": "بداية الاشتراك", "value": start_date},
                {"label": "نهاية الاشتراك", "value": end_date},
                {"label": "المدرسة للتعريف", "value": payment.school_name or "—"},
            ],
            notice_title="الفاتورة الإلكترونية مرفقة",
            notice_text=f"رقم الفاتورة: {email_context['invoice_number']}",
        )
        message = EmailMultiAlternatives(
            subject=f"تم تفعيل باقتك الشخصية | منصة توثيق",
            body=plain_message,
            from_email=(getattr(settings, "DEFAULT_FROM_EMAIL", "") or "no-reply@tawtheeq-ksa.com").strip(),
            to=[payment.customer_email],
        )
        message.attach_alternative(html_message, "text/html")
        message.attach(filename, pdf_bytes, "application/pdf")
        message.send(fail_silently=False)
    except Exception:
        PersonalPayment.objects.filter(pk=payment_id).update(email_sending_at=None)
        logger.exception("Personal payment receipt email failed payment=%s", payment_id)
        raise

    PersonalPayment.objects.filter(pk=payment_id).update(
        email_sent_at=timezone.now(), email_sending_at=None
    )
    logger.info("Personal payment receipt sent payment=%s", payment_id)
    return {"sent": 1, "skipped": 0}


@shared_task(bind=True, ignore_result=True)
def reconcile_personal_payments_task(self) -> dict:
    from django.conf import settings
    from django.core.exceptions import ImproperlyConfigured

    from reports.moyasar_gateway import MoyasarGatewayError, is_enabled

    from .billing import PersonalPaymentError, sync_personal_payment

    if not bool(getattr(settings, "PAYMENT_RECONCILIATION_ENABLED", True)):
        return {"checked": 0, "recovered": 0, "errors": 0}
    now = timezone.now()
    cutoff = now - timedelta(days=5)
    summary = {"checked": 0, "recovered": 0, "errors": 0}
    pending_ids = []
    if is_enabled():
        pending_ids = list(
            PersonalPayment.objects.filter(
                status=PersonalPayment.Status.PENDING,
                gateway_invoice_id__gt="",
                created_at__gte=cutoff,
            ).order_by("created_at").values_list("pk", flat=True)[:100]
        )
    for payment_id in pending_ids:
        summary["checked"] += 1
        try:
            payment, status = sync_personal_payment(payment_id)
            if status == "paid" and payment.activated_at:
                summary["recovered"] += 1
        except (MoyasarGatewayError, ImproperlyConfigured, PersonalPaymentError):
            summary["errors"] += 1
            logger.exception("Personal gateway payment reconciliation failed payment=%s", payment_id)

    unsent_ids = list(
        PersonalPayment.objects.filter(
            status=PersonalPayment.Status.PAID,
            email_sent_at__isnull=True,
        ).filter(
            Q(email_sending_at__isnull=True) | Q(email_sending_at__lt=now - timedelta(minutes=15))
        ).values_list("pk", flat=True)[:100]
    )
    from reports.utils import run_task_safe

    for payment_id in unsent_ids:
        run_task_safe(send_personal_payment_receipt_task, str(payment_id))
    summary["receipts_queued"] = len(unsent_ids)
    return summary
