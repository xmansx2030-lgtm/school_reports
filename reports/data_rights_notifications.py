from __future__ import annotations

import logging

from django.core import mail
from django.utils.html import format_html

from .email_branding import platform_url, render_branded_email
from .email_identity import format_system_from_email


logger = logging.getLogger(__name__)


def _send(request_obj, *, subject: str, intro: str, body_html, plain: str) -> bool:
    recipient = (getattr(request_obj.teacher, "email", "") or "").strip()
    if not recipient:
        return False
    try:
        html = render_branded_email(
            "message.html",
            email_title=subject,
            email_preheader=intro,
            recipient_name=request_obj.teacher.name,
            email_intro=intro,
            body_html=body_html,
            action_url=platform_url("/profile/my-data/"),
            action_label="متابعة الطلب",
            notice_title="حماية بياناتك",
            notice_text="لن تطلب منك منصة توثيق كلمة المرور أو رمز التحقق لمعالجة هذا الطلب.",
        )
        message = mail.EmailMultiAlternatives(
            subject=subject,
            body=plain,
            from_email=format_system_from_email(),
            to=[recipient],
        )
        message.attach_alternative(html, "text/html")
        message.send(fail_silently=False)
        return True
    except Exception:
        logger.exception("Could not send erasure-request update", extra={"request_id": request_obj.pk})
        return False


def notify_erasure_received(request_obj) -> bool:
    due = request_obj.response_due_at.strftime("%Y-%m-%d")
    body = format_html(
        "تم استلام طلب إتلاف بياناتك برقم <strong>#{}</strong>. "
        "موعد الرد الأصلي هو <strong>{}</strong>. يمكنك متابعة حالته من مركز بياناتي.",
        request_obj.pk,
        due,
    )
    plain = f"تم استلام طلب إتلاف بياناتك رقم #{request_obj.pk}. موعد الرد الأصلي: {due}."
    return _send(
        request_obj,
        subject="استلام طلب إتلاف بياناتك",
        intro="طلبك مسجل ومؤرخ، ويمكنك متابعة حالته في أي وقت.",
        body_html=body,
        plain=plain,
    )


def notify_erasure_updated(request_obj) -> bool:
    due = request_obj.effective_due_at.strftime("%Y-%m-%d")
    details = request_obj.response_note or request_obj.extension_reason or "لا توجد ملاحظة إضافية."
    body = format_html(
        "تحدّث طلبك رقم <strong>#{}</strong> إلى حالة <strong>{}</strong>. "
        "الموعد الحالي للرد: <strong>{}</strong>.<br><br>{}",
        request_obj.pk,
        request_obj.get_status_display(),
        due,
        details,
    )
    plain = (
        f"تحدّث طلب إتلاف بياناتك رقم #{request_obj.pk} إلى: "
        f"{request_obj.get_status_display()}. الموعد الحالي: {due}. {details}"
    )
    return _send(
        request_obj,
        subject="تحديث طلب إتلاف بياناتك",
        intro="يوجد تحديث موثّق على طلبك.",
        body_html=body,
        plain=plain,
    )
