from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from datetime import timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from core.task_dispatch import enqueue_named_task

from .models import Notification, NotificationRecipient, WebPushDelivery, WebPushSubscription
from .task_names import SEND_WEB_PUSH_NOTIFICATION_TASK

logger = logging.getLogger(__name__)

MAX_DELIVERY_ATTEMPTS = 5
TERMINAL_ENDPOINT_STATUSES = {404, 410}
INVALID_SUBSCRIPTION_STATUSES = {400}
TRANSIENT_STATUSES = {408, 425, 429, 500, 502, 503, 504}


class WebPushTransientError(RuntimeError):
    """Raised after the batch when one or more push services should be retried."""


def web_push_is_configured() -> bool:
    return bool(
        getattr(settings, "WEB_PUSH_ENABLED", False)
        and str(getattr(settings, "WEB_PUSH_VAPID_PRIVATE_KEY", "") or "").strip()
        and str(getattr(settings, "WEB_PUSH_VAPID_PUBLIC_KEY", "") or "").strip()
    )


def web_push_public_key() -> str:
    if not web_push_is_configured():
        return ""
    return str(getattr(settings, "WEB_PUSH_VAPID_PUBLIC_KEY", "") or "").strip()


def endpoint_host_allowed(endpoint: str) -> bool:
    """Restrict user-supplied endpoints to known push services (SSRF guard)."""
    try:
        parsed = urlsplit(str(endpoint or "").strip())
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            return False
        hostname = parsed.hostname.lower().rstrip(".")
    except Exception:
        return False

    allowed = tuple(getattr(settings, "WEB_PUSH_ALLOWED_ENDPOINT_HOSTS", ()) or ())
    return any(
        hostname == suffix.lower().lstrip(".")
        or hostname.endswith(f".{suffix.lower().lstrip('.')}")
        for suffix in allowed
        if str(suffix).strip()
    )


def save_browser_subscription(*, teacher, subscription: dict, user_agent: str = "") -> WebPushSubscription:
    endpoint = str(subscription.get("endpoint") or "").strip()
    keys = subscription.get("keys") if isinstance(subscription.get("keys"), dict) else {}
    p256dh = str(keys.get("p256dh") or "").strip()
    auth = str(keys.get("auth") or "").strip()

    if not endpoint_host_allowed(endpoint):
        raise ValueError("push_endpoint_not_allowed")
    if not (40 <= len(p256dh) <= 512 and 8 <= len(auth) <= 256):
        raise ValueError("push_keys_invalid")
    if len(endpoint) > 4096:
        raise ValueError("push_endpoint_too_long")

    saved, _ = WebPushSubscription.objects.update_or_create(
        endpoint=endpoint,
        defaults={
            "teacher": teacher,
            "p256dh": p256dh,
            "auth": auth,
            "user_agent": str(user_agent or "")[:500],
            "is_active": True,
            "failure_count": 0,
        },
    )
    return saved


def _clean_text(value: str, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _payload_for(notification: Notification, recipient_id: int) -> dict:
    kind = str(getattr(notification, "kind", "") or "notification")
    is_circular = kind == Notification.Kind.CIRCULAR
    is_newsletter = kind == Notification.Kind.NEWSLETTER
    fallback_title = "تعميم جديد" if is_circular else ("نشرة جديدة" if is_newsletter else "إشعار جديد")
    title = _clean_text(notification.title, 100) or fallback_title
    body = _clean_text(notification.message, 220) or "لديك تنبيه جديد في منصة توثيق."
    route_name = "reports:my_circular_detail" if is_circular else "reports:my_notification_detail"
    return {
        "title": title,
        "body": body,
        "url": reverse(route_name, args=[recipient_id]),
        "tag": f"tawtheeq-notification-{notification.pk}",
        "notificationId": notification.pk,
        "requireInteraction": bool(notification.is_important or notification.requires_signature),
        "icon": "/static/img/pwa/icon-192.png",
        # Android keeps only the badge's alpha and tints it, so a full-colour
        # icon arrives in the status bar as a grey square. ``badge-96`` is the
        # glyph alone, white on transparent.
        "badge": "/static/img/pwa/badge-96.png",
    }


def _response_status(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    try:
        return int(getattr(response, "status_code", 0) or 0) or None
    except (TypeError, ValueError):
        return None


def deliver_notification_web_push(notification_id: int, teacher_ids: Iterable[int]) -> dict[str, int]:
    """Deliver one in-app notification to every active subscribed device.

    Successful notification/device pairs are recorded, making repeated Celery
    tasks idempotent. Expired endpoints are deleted automatically.
    """
    stats = {"sent": 0, "failed": 0, "expired": 0, "skipped": 0}
    if not web_push_is_configured():
        return stats

    ids = sorted({int(value) for value in teacher_ids if int(value) > 0})
    if not ids:
        return stats

    try:
        notification = Notification.objects.get(pk=int(notification_id))
    except Notification.DoesNotExist:
        return stats
    if notification.expires_at and notification.expires_at <= timezone.now():
        return stats

    recipients = {
        row.teacher_id: row.id
        for row in NotificationRecipient.objects.filter(
            notification_id=notification.pk,
            teacher_id__in=ids,
        ).only("id", "teacher_id")
    }
    if not recipients:
        return stats

    subscriptions = WebPushSubscription.objects.filter(
        teacher_id__in=recipients,
        is_active=True,
    ).order_by("id")

    from pywebpush import WebPushException, webpush

    private_key = str(settings.WEB_PUSH_VAPID_PRIVATE_KEY).strip()
    subject = str(getattr(settings, "WEB_PUSH_SUBJECT", "") or "").strip()
    timeout = float(getattr(settings, "WEB_PUSH_TIMEOUT_SECONDS", 10) or 10)
    had_transient_failure = False

    for subscription in subscriptions.iterator(chunk_size=200):
        delivery, created = WebPushDelivery.objects.get_or_create(
            subscription=subscription,
            notification=notification,
        )
        if delivery.status == WebPushDelivery.Status.SENT:
            stats["skipped"] += 1
            continue
        if (
            not created
            and delivery.status == WebPushDelivery.Status.PENDING
            and delivery.last_attempt_at
            and delivery.last_attempt_at >= timezone.now() - timedelta(minutes=2)
        ):
            stats["skipped"] += 1
            continue
        if delivery.attempts >= MAX_DELIVERY_ATTEMPTS:
            stats["skipped"] += 1
            continue

        delivery.status = WebPushDelivery.Status.PENDING
        delivery.attempts += 1
        delivery.last_attempt_at = timezone.now()
        delivery.last_error = ""
        delivery.save(update_fields=["status", "attempts", "last_attempt_at", "last_error"])

        try:
            webpush(
                subscription_info={
                    "endpoint": subscription.endpoint,
                    "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
                },
                data=json.dumps(
                    _payload_for(notification, recipients[subscription.teacher_id]),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                vapid_private_key=private_key,
                vapid_claims={"sub": subject},
                ttl=24 * 60 * 60,
                headers={"Urgency": "high" if notification.is_important else "normal"},
                timeout=timeout,
            )
        except WebPushException as exc:
            status = _response_status(exc)
            error = _clean_text(str(exc), 500)
            if status in TERMINAL_ENDPOINT_STATUSES:
                subscription.delete()
                stats["expired"] += 1
                continue
            if status in INVALID_SUBSCRIPTION_STATUSES:
                subscription.is_active = False
                subscription.failure_count += 1
                subscription.save(update_fields=["is_active", "failure_count", "updated_at"])
            else:
                subscription.failure_count += 1
                subscription.save(update_fields=["failure_count", "updated_at"])
            delivery.status = WebPushDelivery.Status.FAILED
            delivery.last_error = error
            delivery.save(update_fields=["status", "last_error"])
            stats["failed"] += 1
            had_transient_failure = had_transient_failure or status is None or status in TRANSIENT_STATUSES
            logger.warning(
                "Web Push delivery failed notification=%s subscription=%s status=%s",
                notification.pk,
                subscription.pk,
                status,
            )
        except Exception as exc:
            delivery.status = WebPushDelivery.Status.FAILED
            delivery.last_error = _clean_text(str(exc), 500)
            delivery.save(update_fields=["status", "last_error"])
            subscription.failure_count += 1
            subscription.save(update_fields=["failure_count", "updated_at"])
            stats["failed"] += 1
            had_transient_failure = True
            logger.exception(
                "Unexpected Web Push failure notification=%s subscription=%s",
                notification.pk,
                subscription.pk,
            )
        else:
            now = timezone.now()
            delivery.status = WebPushDelivery.Status.SENT
            delivery.sent_at = now
            delivery.last_error = ""
            delivery.save(update_fields=["status", "sent_at", "last_error"])
            subscription.failure_count = 0
            subscription.last_success_at = now
            subscription.save(update_fields=["failure_count", "last_success_at", "updated_at"])
            stats["sent"] += 1

    if had_transient_failure:
        raise WebPushTransientError(f"Transient Web Push failures: {stats}")
    return stats


def queue_notification_web_push(*, notification, teacher_ids: Iterable[int]) -> None:
    """Queue delivery after the surrounding DB transaction commits."""
    if not web_push_is_configured():
        return
    notification_id = int(getattr(notification, "pk", notification) or 0)
    ids = sorted({int(value) for value in teacher_ids if int(value) > 0})
    if notification_id <= 0 or not ids:
        return

    def _dispatch() -> None:
        broker = str(getattr(settings, "CELERY_BROKER_URL", "") or "").strip()
        if broker:
            try:
                enqueue_named_task(
                    SEND_WEB_PUSH_NOTIFICATION_TASK,
                    args=(notification_id, ids),
                    queue="notifications",
                )
                return
            except Exception:
                logger.exception("Could not enqueue Web Push; using direct fallback")
        try:
            deliver_notification_web_push(notification_id, ids)
        except WebPushTransientError:
            logger.exception("Direct Web Push delivery had transient failures")

    transaction.on_commit(_dispatch)
