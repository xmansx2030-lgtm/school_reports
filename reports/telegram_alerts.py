from __future__ import annotations

import hashlib
import html
import json
import logging
from dataclasses import asdict, dataclass
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlsplit

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from core.task_dispatch import enqueue_named_task

from .task_names import SEND_TELEGRAM_ALERT_TASK

logger = logging.getLogger(__name__)


class TelegramDeliveryError(RuntimeError):
    """A retryable Telegram delivery failure that never includes the bot token."""


@dataclass(frozen=True)
class TelegramAlert:
    event_key: str
    category: str
    text: str
    action_url: str = ""

    def payload(self) -> dict[str, str]:
        return asdict(self)


def _safe(value) -> str:
    return html.escape(str(value or "—"), quote=False)


def _event_time(value=None) -> str:
    moment = value or timezone.now()
    try:
        moment = timezone.localtime(moment)
    except Exception:
        pass
    return moment.strftime("%Y-%m-%d %H:%M")


def _admin_url(view_name: str, *, args: list | None = None, query: str = "") -> str:
    site_url = (getattr(settings, "SITE_URL", "") or "").strip().rstrip("/")
    if not site_url:
        return ""
    path = reverse(view_name, args=args or [])
    url = f"{site_url}{path}"
    if query:
        url = f"{url}?{query.lstrip('?')}"
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return url


def telegram_category_enabled(category: str) -> bool:
    if not bool(getattr(settings, "TELEGRAM_ALERTS_ENABLED", False)):
        return False
    token = (getattr(settings, "TELEGRAM_BOT_TOKEN", "") or "").strip()
    chat_id = (getattr(settings, "TELEGRAM_ALERT_CHAT_ID", "") or "").strip()
    if not token or not chat_id:
        return False
    enabled = getattr(settings, "TELEGRAM_ALERT_CATEGORIES", set()) or set()
    return category in set(enabled)


def build_school_registration_alert(school) -> TelegramAlert:
    text = "\n".join(
        [
            "🟢 <b>تسجيل مدرسة جديدة</b>",
            f"🏫 المدرسة: {_safe(school.name)}",
            f"🆔 رقم المدرسة: <code>{school.pk}</code>",
            f"🎓 التصنيف: {_safe(school.get_stage_display())} · {_safe(school.get_gender_display())}",
            f"🕒 الوقت: {_event_time()}",
            "",
            "#تسجيل",
        ]
    )
    return TelegramAlert(
        event_key=f"registration:school:{school.pk}",
        category="registration",
        text=text,
        action_url=_admin_url("reports:platform_schools_directory"),
    )


def build_support_ticket_alert(ticket) -> TelegramAlert:
    school_name = getattr(getattr(ticket, "school", None), "name", "غير مرتبطة بمدرسة")
    text = "\n".join(
        [
            "🛟 <b>تذكرة دعم فني جديدة</b>",
            f"🏫 المدرسة: {_safe(school_name)}",
            f"🎫 رقم التذكرة: <code>#{ticket.pk}</code>",
            f"📌 الحالة: {_safe(ticket.get_status_display())}",
            f"🕒 الوقت: {_event_time(ticket.created_at)}",
            "",
            "#دعم",
        ]
    )
    return TelegramAlert(
        event_key=f"support:ticket:{ticket.pk}:created",
        category="support",
        text=text,
        action_url=_admin_url(
            "reports:platform_tickets_list",
            query=f"q={ticket.pk}",
        ),
    )


def build_customer_complaint_alert(complaint) -> TelegramAlert:
    text = "\n".join(
        [
            "🔴 <b>شكوى عميل جديدة</b>",
            f"🔖 رقم المتابعة: <code>{_safe(complaint.reference)}</code>",
            f"📌 الحالة: {_safe(complaint.get_status_display())}",
            f"🕒 الوقت: {_event_time(complaint.created_at)}",
            "",
            "#شكاوى",
        ]
    )
    return TelegramAlert(
        event_key=f"complaints:customer:{complaint.pk}:created",
        category="complaints",
        text=text,
        action_url=_admin_url(
            "reports:platform_complaint_detail",
            args=[complaint.pk],
        ),
    )


def build_payment_alert(payment, *, created: bool) -> TelegramAlert:
    batch_ref = (getattr(payment, "batch_ref", "") or "").strip()
    operation_ref = f"دفعة موحدة {batch_ref}" if batch_ref else f"#{payment.pk}"
    heading = "طلب دفع جديد" if created else "تحديث حالة دفع"
    event_root = f"batch:{batch_ref}" if batch_ref else f"payment:{payment.pk}"
    event_suffix = "created" if created else f"status:{payment.status}"
    text = "\n".join(
        [
            f"💳 <b>{heading}</b>",
            f"🏫 المدرسة: {_safe(payment.school.name)}",
            f"🧾 العملية: <code>{_safe(operation_ref)}</code>",
            f"📦 الغرض: {_safe(payment.get_purpose_display())}",
            f"📌 الحالة: {_safe(payment.get_status_display())}",
            f"🕒 الوقت: {_event_time(payment.updated_at or payment.created_at)}",
            "",
            "#دفع",
        ]
    )
    return TelegramAlert(
        event_key=f"payment:{event_root}:{event_suffix}",
        category="payments",
        text=text,
        action_url=_admin_url(
            "reports:platform_payment_detail",
            args=[payment.pk],
        ),
    )


def build_payment_recovery_alert(payment) -> TelegramAlert:
    """Announce a payment the reconciliation sweep had to rescue.

    Reaching this point means the gateway's callback never landed and the
    customer never returned to the site: the school had paid and was sitting
    unactivated until the sweep caught it. The recovery is automatic, but the
    upstream failure is not something to leave unseen — a rising count of these
    points at a webhook or availability problem worth investigating.
    """
    batch_ref = (getattr(payment, "batch_ref", "") or "").strip()
    operation_ref = f"دفعة موحدة {batch_ref}" if batch_ref else f"#{payment.pk}"
    text = "\n".join(
        [
            "🛟 <b>تم إنقاذ عملية دفع لم يصلنا تأكيدها</b>",
            f"🏫 المدرسة: {_safe(payment.school.name)}",
            f"🧾 العملية: <code>{_safe(operation_ref)}</code>",
            f"💳 الوسيلة: {_safe(payment.get_payment_method_display())}",
            f"💰 المبلغ: {_safe(payment.amount)} ريال",
            f"🕒 الوقت: {_event_time()}",
            "",
            "فُعّلت الخدمة تلقائياً بعد التحقق من المزوّد. تكرار هذا التنبيه يعني "
            "خللاً في وصول إشعارات بوابة الدفع يستحق الفحص.",
            "",
            "#دفع #تسوية",
        ]
    )
    return TelegramAlert(
        # Bucketed per payment so one rescue alerts once, not on every sweep.
        event_key=f"payment-recovery:{payment.pk}",
        category="payments",
        text=text,
        action_url=_admin_url("reports:platform_payment_detail", args=[payment.pk]),
    )


def build_subscription_alert(subscription, *, created: bool) -> TelegramAlert:
    if bool(getattr(subscription, "is_cancelled", False)):
        status_label = "ملغي"
    elif bool(subscription.is_active) and not bool(subscription.is_expired):
        status_label = "نشط"
    elif bool(subscription.is_active):
        status_label = "منتهي"
    else:
        status_label = "متوقف"

    heading = "اشتراك مدرسة جديد" if created else "تحديث اشتراك مدرسة"
    state = "|".join(
        [
            str(subscription.plan_id),
            str(subscription.start_date),
            str(subscription.end_date),
            str(bool(subscription.is_active)),
            str(bool(getattr(subscription, "is_cancelled", False))),
        ]
    )
    fingerprint = hashlib.sha256(state.encode("utf-8")).hexdigest()[:16]
    event_suffix = "created" if created else f"state:{fingerprint}"
    text = "\n".join(
        [
            f"🟠 <b>{heading}</b>",
            f"🏫 المدرسة: {_safe(subscription.school.name)}",
            f"📦 الباقة: {_safe(subscription.plan.name)}",
            f"📌 الحالة: {_safe(status_label)}",
            f"📅 الانتهاء: {_safe(subscription.end_date)}",
            f"🕒 الوقت: {_event_time(subscription.updated_at or subscription.created_at)}",
            "",
            "#اشتراك",
        ]
    )
    return TelegramAlert(
        event_key=f"subscription:{subscription.pk}:{event_suffix}",
        category="subscriptions",
        text=text,
        action_url=_admin_url(
            "reports:platform_subscription_detail",
            args=[subscription.pk],
        ),
    )


def queue_telegram_alert(alert: TelegramAlert) -> bool:
    """Queue only after the surrounding DB transaction commits successfully."""
    if not telegram_category_enabled(alert.category):
        return False

    payload = alert.payload()

    def _enqueue() -> None:
        try:
            enqueue_named_task(
                SEND_TELEGRAM_ALERT_TASK,
                args=(payload,),
                queue="notifications",
            )
        except Exception:
            logger.exception(
                "Unable to enqueue Telegram alert event=%s category=%s",
                alert.event_key,
                alert.category,
            )

    transaction.on_commit(_enqueue)
    return True


def _dedupe_cache_key(prefix: str, event_key: str) -> str:
    digest = hashlib.sha256(event_key.encode("utf-8")).hexdigest()
    return f"telegram-alert:{prefix}:{digest}"


def deliver_telegram_alert(payload: dict[str, str]) -> str:
    """Send one Telegram alert with cache-backed locking and de-duplication."""
    category = str(payload.get("category") or "").strip()
    if not telegram_category_enabled(category):
        return "disabled"

    event_key = str(payload.get("event_key") or "").strip()
    text = str(payload.get("text") or "").strip()
    if not event_key or not text:
        raise TelegramDeliveryError("Telegram alert payload is incomplete.")

    sent_key = _dedupe_cache_key("sent", event_key)
    lock_key = _dedupe_cache_key("lock", event_key)
    if cache.get(sent_key):
        return "duplicate"
    if not cache.add(lock_key, "1", timeout=90):
        return "in_progress"

    token = (getattr(settings, "TELEGRAM_BOT_TOKEN", "") or "").strip()
    chat_id = (getattr(settings, "TELEGRAM_ALERT_CHAT_ID", "") or "").strip()
    timeout = float(getattr(settings, "TELEGRAM_ALERT_TIMEOUT_SECONDS", 10) or 10)
    ttl = int(getattr(settings, "TELEGRAM_ALERT_DEDUP_TTL_SECONDS", 2_592_000) or 2_592_000)

    body: dict[str, object] = {
        "chat_id": chat_id,
        "text": text[:3900],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    action_url = str(payload.get("action_url") or "").strip()
    if action_url:
        parsed = urlsplit(action_url)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            body["reply_markup"] = {
                "inline_keyboard": [
                    [{"text": "فتح في لوحة الإدارة", "url": action_url}]
                ]
            }

    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
    request = urlrequest.Request(
        endpoint,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urlrequest.urlopen(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200) or 200)
            raw = response.read(16_384)
        if status < 200 or status >= 300:
            raise TelegramDeliveryError(f"Telegram API returned HTTP {status}.")
        try:
            result = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise TelegramDeliveryError("Telegram API returned invalid JSON.") from exc
        if not bool(result.get("ok")):
            raise TelegramDeliveryError("Telegram API rejected the alert.")
    except TelegramDeliveryError:
        cache.delete(lock_key)
        raise
    except urlerror.HTTPError as exc:
        cache.delete(lock_key)
        raise TelegramDeliveryError(f"Telegram API returned HTTP {exc.code}.") from None
    except (urlerror.URLError, TimeoutError):
        cache.delete(lock_key)
        raise TelegramDeliveryError("Telegram API is temporarily unreachable.") from None
    except Exception:
        cache.delete(lock_key)
        raise TelegramDeliveryError("Telegram alert delivery failed.") from None

    cache.set(sent_key, "1", timeout=max(ttl, 300))
    cache.delete(lock_key)
    logger.info(
        "Telegram alert delivered event=%s category=%s",
        event_key,
        category,
    )
    return "sent"
