from __future__ import annotations

import logging

from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.contrib.auth import get_user_model
from reports.models import (
    AuditLog,
    CustomerComplaint,
    Notification,
    NotificationRecipient,
    Payment,
    Report,
    School,
    SchoolMembership,
    SchoolSubscription,
    Ticket,
)
from reports.cache_utils import invalidate_school, invalidate_user_notifications
from core.observability import report_degraded as _degraded, soft_call, soft_fail
from .middleware import get_current_request

User = get_user_model()
logger = logging.getLogger(__name__)

try:
    from .realtime_notifications import (
        push_delta_to_user,
        push_force_resync,
        push_new_notification_to_teachers,
    )
except Exception:  # pragma: no cover
    push_delta_to_user = None  # type: ignore
    push_force_resync = None  # type: ignore
    push_new_notification_to_teachers = None  # type: ignore


@receiver(post_save, sender=School)
def _telegram_school_registered(sender, instance, created, **kwargs):
    if kwargs.get("raw") or not created:
        return
    try:
        from .telegram_alerts import build_school_registration_alert, queue_telegram_alert

        queue_telegram_alert(build_school_registration_alert(instance))
    except Exception:
        logger.exception("Unable to prepare Telegram school registration alert school=%s", instance.pk)


@receiver(pre_save, sender=Payment)
def _telegram_payment_capture_previous_status(sender, instance, **kwargs):
    if kwargs.get("raw") or not getattr(instance, "pk", None):
        return
    try:
        from .telegram_alerts import telegram_category_enabled

        if telegram_category_enabled("payments"):
            instance._telegram_previous_status = (
                Payment.objects.filter(pk=instance.pk).values_list("status", flat=True).first()
            )
    except Exception:
        instance._telegram_previous_status = None


@receiver(post_save, sender=Payment)
def _telegram_payment_saved(sender, instance, created, **kwargs):
    if kwargs.get("raw"):
        return
    previous_status = getattr(instance, "_telegram_previous_status", None)
    if not created and previous_status == instance.status:
        return
    try:
        from .telegram_alerts import build_payment_alert, queue_telegram_alert

        queue_telegram_alert(build_payment_alert(instance, created=created))
    except Exception:
        logger.exception("Unable to prepare Telegram payment alert payment=%s", instance.pk)


@receiver(pre_save, sender=SchoolSubscription)
def _telegram_subscription_capture_previous_state(sender, instance, **kwargs):
    if kwargs.get("raw") or not getattr(instance, "pk", None):
        return
    try:
        from .telegram_alerts import telegram_category_enabled

        if not telegram_category_enabled("subscriptions"):
            return
        instance._telegram_previous_state = (
            SchoolSubscription.objects.filter(pk=instance.pk)
            .values_list(
                "plan_id",
                "start_date",
                "end_date",
                "is_active",
                "canceled_at",
                "cancel_reason",
            )
            .first()
        )
    except Exception:
        instance._telegram_previous_state = None


@receiver(post_save, sender=SchoolSubscription)
def _telegram_subscription_saved(sender, instance, created, **kwargs):
    if kwargs.get("raw"):
        return
    current_state = (
        instance.plan_id,
        instance.start_date,
        instance.end_date,
        instance.is_active,
        instance.canceled_at,
        instance.cancel_reason,
    )
    previous_state = getattr(instance, "_telegram_previous_state", None)
    if not created and previous_state == current_state:
        return
    try:
        from .telegram_alerts import build_subscription_alert, queue_telegram_alert

        queue_telegram_alert(build_subscription_alert(instance, created=created))
    except Exception:
        logger.exception(
            "Unable to prepare Telegram subscription alert subscription=%s",
            instance.pk,
        )


@receiver(post_save, sender=Ticket)
def _telegram_platform_ticket_created(sender, instance, created, **kwargs):
    if kwargs.get("raw") or not created or not instance.is_platform:
        return
    try:
        from .telegram_alerts import build_support_ticket_alert, queue_telegram_alert

        queue_telegram_alert(build_support_ticket_alert(instance))
    except Exception:
        logger.exception("Unable to prepare Telegram support alert ticket=%s", instance.pk)


@receiver(post_save, sender=CustomerComplaint)
def _telegram_customer_complaint_created(sender, instance, created, **kwargs):
    if kwargs.get("raw") or not created:
        return
    try:
        from .telegram_alerts import (
            build_customer_complaint_alert,
            queue_telegram_alert,
        )

        queue_telegram_alert(build_customer_complaint_alert(instance))
    except Exception:
        logger.exception(
            "Unable to prepare Telegram complaint alert complaint=%s",
            instance.pk,
        )


def _audit_role_snapshot(user, school) -> str:
    """دور المستخدم لحظة الحدث، لقطةً لا اشتقاقاً متأخراً.

    مطابقة لما يفعله ``model_parts.signals._audit_actor_snapshot``: من دخل
    معلّماً ثم صار مديراً لا يصح أن يظهر دخوله القديم بصفة مدير.
    """
    try:
        from .permissions import effective_user_role_label

        return str(effective_user_role_label(user, school) or "")[:64]
    except Exception:
        return ""


def _infer_school_for_audit(request, user) -> School | None:
    """Best-effort school inference for audit events.

    - Prefer the active school cached on the request by middleware.
    - Otherwise, prefer the active school in session.
    - Otherwise, if the user has exactly one active membership, use it.
    - Otherwise, return None.
    """
    if request is None or user is None:
        return None

    # ── Fast path: reuse middleware-cached school ──
    cached = getattr(request, "active_school", None)
    if cached is not None:
        return cached

    try:
        sid = request.session.get("active_school_id")
    except Exception:
        sid = None

    if sid:
        school = soft_call(
            "audit.school_from_session",
            lambda: School.objects.filter(pk=sid, is_active=True).first(),
            default=None,
            school_id=sid,
        )
        if school is not None:
            return school

    def _sole_membership_school():
        schools = (
            School.objects.filter(memberships__teacher=user, memberships__is_active=True, is_active=True)
            .distinct()
            .only("id", "name")
        )
        return schools.first() if schools.count() == 1 else None

    return soft_call(
        "audit.school_from_sole_membership",
        _sole_membership_school,
        default=None,
        user_id=getattr(user, "pk", None),
    )


@receiver(user_logged_in)
def _single_session_on_login(sender, request, user, **kwargs):
    """Ensure a user can only have one active session.

    On login:
    - Ensure the current session has a key.
    - Delete the previously recorded session (if any).
    - Persist the new session key on the user.

    Notes:
    - This relies on the DB-backed session engine (default). If a different
      session backend is used, deleting the old session may be a no-op.
    """
    try:
        if request.session.session_key is None:
            request.session.save()
        new_key = request.session.session_key or ""
    except Exception:
        return

    try:
        old_key = getattr(user, "current_session_key", "") or ""
    except Exception:
        old_key = ""

    if old_key and new_key and old_key != new_key:
        # الجلسةُ القديمة التي لا تُحذف تعني بقاء الجهاز السابق مسجَّلاً —
        # أي **إبطال قاعدة الجلسة الواحدة** بلا أن يعلم أحد.
        with soft_fail("auth.delete_previous_session", user_id=getattr(user, "pk", None)):
            # Support both DB-backed and cache-backed session engines.
            from importlib import import_module
            from django.conf import settings as _settings

            engine = import_module(_settings.SESSION_ENGINE)
            store = engine.SessionStore(old_key)
            store.delete()

    with soft_fail("auth.persist_session_key", user_id=getattr(user, "pk", None)):
        if getattr(user, "current_session_key", "") != new_key:
            user.current_session_key = new_key
            user.save(update_fields=["current_session_key"])

    # Audit: login
    try:
        _login_school = _infer_school_for_audit(request, user)
        AuditLog.objects.create(
            school=_login_school,
            teacher=user,
            actor_role=_audit_role_snapshot(user, _login_school),
            action=AuditLog.Action.LOGIN,
            model_name="Auth",
            object_id=getattr(user, "pk", None),
            object_repr=f"Login: {str(user)[:200]}",
            ip_address=(request.META.get("REMOTE_ADDR") if request else None),
            user_agent=(request.META.get("HTTP_USER_AGENT", "")[:500] if request else ""),
        )
    except Exception:
        # لا نكسر تسجيل الدخول بسبب فشل سجل العمليات — لكن سجلّ تدقيقٍ ينقصه
        # دخولٌ ليس سجلاً، وغيابه يُكتشف بعد الحادثة لا قبلها.
        _degraded("audit.login_entry", user_id=getattr(user, "pk", None))


@receiver(user_logged_out)
def _single_session_on_logout(sender, request, user, **kwargs):
    """Clear recorded session key when the active session logs out."""
    if not user:
        return

    try:
        sk = request.session.session_key or ""
    except Exception:
        sk = ""

    with soft_fail("auth.clear_session_key", user_id=getattr(user, "pk", None)):
        if sk and getattr(user, "current_session_key", "") == sk:
            user.current_session_key = ""
            user.save(update_fields=["current_session_key"])

    # Audit: logout
    try:
        _logout_school = _infer_school_for_audit(request, user)
        AuditLog.objects.create(
            school=_logout_school,
            teacher=user,
            actor_role=_audit_role_snapshot(user, _logout_school),
            action=AuditLog.Action.LOGOUT,
            model_name="Auth",
            object_id=getattr(user, "pk", None),
            object_repr=f"Logout: {str(user)[:200]}",
            ip_address=(request.META.get("REMOTE_ADDR") if request else None),
            user_agent=(request.META.get("HTTP_USER_AGENT", "")[:500] if request else ""),
        )
    except Exception:
        _degraded("audit.logout_entry", user_id=getattr(user, "pk", None))


# =========================
# Realtime Notifications (Channels / WebSocket)
# =========================


@receiver(pre_save, sender=NotificationRecipient)
def _notif_recipient_pre_save(sender, instance: NotificationRecipient, **kwargs):
    """Capture previous values for delta calculation.

    Important: queryset.update()/bulk_update won't trigger this.
    """
    if kwargs.get("raw"):
        return

    try:
        if not getattr(instance, "pk", None):
            return
    except Exception:
        return

    try:
        old = (
            NotificationRecipient.objects.select_related("notification")
            .only(
                "id",
                "is_read",
                "is_signed",
                "notification__kind",
                "notification__requires_signature",
                "notification__school_id",
            )
            .filter(pk=instance.pk)
            .first()
        )
        if old is None:
            return
        instance._sr_old_is_read = bool(getattr(old, "is_read", False))
        instance._sr_old_is_signed = bool(getattr(old, "is_signed", False))
        n = getattr(old, "notification", None)
        instance._sr_old_notification_kind = getattr(n, "kind", "notification") if n else "notification"
        instance._sr_old_requires_signature = bool(getattr(n, "requires_signature", False)) if n else False
        instance._sr_old_notification_school_id = getattr(n, "school_id", None) if n else None
    except Exception:
        return


@receiver(post_save, sender=NotificationRecipient)
def _notif_recipient_post_save(sender, instance: NotificationRecipient, created: bool, **kwargs):
    """Push counter updates to the recipient over WebSocket."""
    if kwargs.get("raw"):
        return

    if push_delta_to_user is None:
        return

    try:
        teacher_id = int(getattr(instance, "teacher_id", 0) or 0)
        if teacher_id <= 0:
            return
    except Exception:
        return

    try:
        req = get_current_request()
        trace_id = getattr(req, "trace_id", None) if req is not None else None
    except Exception:
        trace_id = None

    try:
        n = getattr(instance, "notification", None)
        notification_kind = getattr(n, "kind", "notification") if n else "notification"
        requires_signature = bool(getattr(n, "requires_signature", False)) if n else False
        notif_school_id = getattr(n, "school_id", None) if n else None
    except Exception:
        notification_kind = "notification"
        requires_signature = False
        notif_school_id = None

    if created:
        # New recipient row == new attention item.
        try:
            if notification_kind == "circular" and requires_signature:
                push_delta_to_user(
                    teacher_id=teacher_id,
                    notification_school_id=notif_school_id,
                    delta_signatures_pending=1,
                    delta_count=1,
                    trace_id=trace_id,
                )
            else:
                push_delta_to_user(
                    teacher_id=teacher_id,
                    notification_school_id=notif_school_id,
                    delta_unread=1,
                    delta_count=1,
                    trace_id=trace_id,
                )
        except Exception:
            _degraded("realtime.delta_new_recipient", user_id=teacher_id)
        return

    # Updates: read/signature state change
    try:
        old_is_read = getattr(instance, "_sr_old_is_read", None)
        old_is_signed = getattr(instance, "_sr_old_is_signed", None)
        old_kind = getattr(instance, "_sr_old_notification_kind", notification_kind)
        old_requires_signature = getattr(instance, "_sr_old_requires_signature", requires_signature)
        old_school_id = getattr(instance, "_sr_old_notification_school_id", notif_school_id)
    except Exception:
        _degraded("realtime.read_previous_state", user_id=teacher_id)
        old_is_read = None
        old_is_signed = None
        old_kind = notification_kind
        old_requires_signature = requires_signature
        old_school_id = notif_school_id

    # If schema/instance didn't have old values, do a safe resync.
    if old_is_read is None and old_is_signed is None:
        if push_force_resync is not None:
            with soft_fail("realtime.force_resync", user_id=teacher_id):
                push_force_resync(teacher_id=teacher_id, trace_id=trace_id)
        return

    # Compute both badge states explicitly. A signed newsletter belongs to the
    # notification bell and stays there after opening until its signature is done.
    try:
        new_is_read = bool(getattr(instance, "is_read", False))
        new_is_signed = bool(getattr(instance, "is_signed", False))
        old_notification_attention = old_kind in {"notification", "newsletter"} and (
            not bool(old_is_read)
            or (
                old_kind == "newsletter"
                and bool(old_requires_signature)
                and not bool(old_is_signed)
            )
        )
        new_notification_attention = notification_kind in {"notification", "newsletter"} and (
            not new_is_read
            or (
                notification_kind == "newsletter"
                and requires_signature
                and not new_is_signed
            )
        )
        old_circular_pending = (
            old_kind == "circular"
            and bool(old_requires_signature)
            and not bool(old_is_signed)
        )
        new_circular_pending = (
            notification_kind == "circular"
            and requires_signature
            and not new_is_signed
        )
        delta_unread = int(new_notification_attention) - int(old_notification_attention)
        delta_signatures = int(new_circular_pending) - int(old_circular_pending)
        if delta_unread or delta_signatures:
            push_delta_to_user(
                teacher_id=teacher_id,
                notification_school_id=old_school_id,
                delta_unread=delta_unread,
                delta_signatures_pending=delta_signatures,
                delta_count=delta_unread + delta_signatures,
                trace_id=trace_id,
            )
    except Exception:
        _degraded("realtime.delta_attention_change", user_id=teacher_id)

# =========================
# System Notifications Logic (Added for System Manager)
# =========================

@receiver(post_save, sender=SchoolSubscription)
def notify_admin_on_subscription(sender, instance, created, **kwargs):
    """
    إشعار مدير النظام عند إنشاء اشتراك جديد أو تجديده.
    """
    if kwargs.get("raw"):
        return

    try:
        school_name = getattr(instance.school, "name", "مدرسة")
        plan_name = getattr(instance.plan, "name", "باقة")
        end_date = instance.end_date
        
        if created:
            title = "🔔 طلب اشتراك جديد"
            msg = f"تم تسجيل اشتراك جديد للمدرسة: {school_name}\nالباقة: {plan_name}\nينتهي في: {end_date}"
        else:
            # هنا نفترض الحفظ قد يكون تجديداً أو تعديلاً
            title = "🔔 تحديث اشتراك"
            msg = f"تم تحديث اشتراك المدرسة: {school_name}\nالباقة الحالية: {plan_name}\nتاريخ الانتهاء الجديد: {end_date}"

        # إنشاء الإشعار
        notification = Notification.objects.create(
            title=title,
            message=msg,
            is_important=True,
            # school=None لجعلها عامة نوعاً ما أو نربطها بمستخدمين محددين أدناه
        )
        
        # إرسال لكل من لديه is_superuser=True
        # نفترض أن مدير النظام هو Superuser
        admins = User.objects.filter(is_superuser=True)
        admin_ids = [a.pk for a in admins]
        # ✅ استعلام واحد بدل N استعلامات exists() في حلقة
        existing = set(
            NotificationRecipient.objects.filter(
                notification=notification, teacher_id__in=admin_ids
            ).values_list("teacher_id", flat=True)
        )
        recipients = []
        new_ids = []
        for admin in admins:
            if admin.pk not in existing:
                recipients.append(NotificationRecipient(
                    notification=notification,
                    teacher=admin
                ))
                new_ids.append(admin.pk)
        
        if recipients:
            NotificationRecipient.objects.bulk_create(recipients, ignore_conflicts=True)
            if push_new_notification_to_teachers is not None:
                push_new_notification_to_teachers(
                    notification=notification,
                    teacher_ids=[i for i in new_ids if i],
                )
    except Exception:
        _degraded("notifications.fanout_recipients", notification_id=getattr(notification, "pk", None))
        # تجنب كسر العملية الأساسية في حال خطأ في الإشعارات
        pass


@receiver(post_save, sender=Ticket)
def notify_admin_on_platform_ticket(sender, instance, created, **kwargs):
    """
    إشعار مدير النظام عند فتح تذكرة دعم فني (is_platform=True).

    ملاحظة: تم إلغاء التنفيذ لتجنب التكرار مع trigger_ticket_notifications
    الموجود في models.py الذي يتعامل مع نفس الحدث.
    """
    pass

# ── Cache invalidation signals ──────────────────────────────────────
@receiver(post_save, sender=Report)
def _invalidate_school_on_report(sender, instance, **kwargs):
    """Bust school stats cache when a report is created/updated."""
    if kwargs.get("raw"):
        return

    # كاشٌ لا يُبطَل يعني لوحةً تعرض أرقاماً قديمة بثقة — وهو أسوأ من لوحة
    # بطيئة، لأن لا شيء فيها يدلّ على أنها قديمة.
    with soft_fail("cache.invalidate_school_on_report", report_id=instance.pk):
        sid = getattr(instance, "school_id", None)
        if sid:
            invalidate_school(sid)


@receiver(post_save, sender=Ticket)
def _invalidate_school_on_ticket(sender, instance, **kwargs):
    """Bust school stats cache when a ticket is created/updated."""
    if kwargs.get("raw"):
        return

    with soft_fail("cache.invalidate_school_on_ticket", ticket_id=instance.pk):
        sid = getattr(instance, "school_id", None)
        if sid:
            invalidate_school(sid)


@receiver(post_save, sender=NotificationRecipient)
def _invalidate_user_notif_cache(sender, instance, **kwargs):
    """Bust unread notification count for the recipient."""
    if kwargs.get("raw"):
        return

    # عدّادٌ لا يُبطَل يبقى مرتفعاً بعد القراءة — أكثر ما يُبلَّغ عنه.
    with soft_fail("cache.invalidate_user_notifications", recipient_id=instance.pk):
        tid = getattr(instance.teacher, "id", None)
        if tid:
            invalidate_user_notifications(tid)


@receiver(post_save, sender=NotificationRecipient)
def _web_push_new_notification_recipient(sender, instance, created: bool, **kwargs):
    """Individual creates do not pass through the bulk realtime helper."""
    if kwargs.get("raw") or not created:
        return
    try:
        from .web_push import queue_notification_web_push

        queue_notification_web_push(
            notification=instance.notification,
            teacher_ids=[instance.teacher_id],
        )
    except Exception:
        logger.exception(
            "Unable to schedule Web Push notification=%s teacher=%s",
            getattr(instance, "notification_id", None),
            getattr(instance, "teacher_id", None),
        )
