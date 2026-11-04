from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import time
import uuid
from datetime import datetime
from email.utils import parseaddr
from html import escape
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.html import strip_tags

from .email_branding import render_branded_email
from .models import (
    PlatformEmail,
    PlatformEmailAttachment,
    PlatformEmailConfiguration,
    PlatformEmailEvent,
)

logger = logging.getLogger(__name__)


class ResendError(RuntimeError):
    pass


def resend_is_configured() -> bool:
    return bool(getattr(settings, "RESEND_API_KEY", ""))


def webhook_is_configured() -> bool:
    return bool(getattr(settings, "RESEND_WEBHOOK_SECRET", ""))


def _api_request(path: str, *, method: str = "GET", payload: dict | None = None, idempotency_key: str = "") -> dict:
    api_key = (getattr(settings, "RESEND_API_KEY", "") or "").strip()
    if not api_key:
        raise ResendError("مفتاح Resend API غير مضبوط على الخادم.")

    base_url = (getattr(settings, "RESEND_API_BASE_URL", "https://api.resend.com") or "").rstrip("/")
    parsed_base = urlparse(base_url)
    if parsed_base.scheme != "https" or not parsed_base.netloc:
        raise ResendError("عنوان Resend API يجب أن يكون HTTPS صالحًا.")
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "User-Agent": "Tawtheeq-Platform/1.0",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key[:256]
    request = Request(  # noqa: S310 - URL is restricted to an absolute HTTPS provider endpoint above.
        f"{base_url}{path}", data=body, headers=headers, method=method
    )
    try:
        with urlopen(  # noqa: S310 - fixed provider URL, override exists for isolated tests only.
            request,
            timeout=int(getattr(settings, "RESEND_TIMEOUT", 15)),
        ) as response:
            return json.loads(response.read().decode("utf-8") or "{}")
    except HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8")[:1000]
        except Exception:
            detail = ""
        logger.warning("Resend API rejected %s %s: %s", method, path, detail or exc.code)
        raise ResendError("رفض مزود البريد العملية. راجع الإعدادات وعنوان المرسل.") from exc
    except (URLError, TimeoutError, ValueError) as exc:
        logger.warning("Resend API unavailable for %s %s: %s", method, path, exc)
        raise ResendError("تعذر الاتصال بمزود البريد حاليًا.") from exc


def verify_webhook_signature(payload: bytes, headers: dict[str, str]) -> bool:
    """Verify the Svix signature used by Resend, including replay protection."""

    secret = (getattr(settings, "RESEND_WEBHOOK_SECRET", "") or "").strip()
    message_id = (headers.get("svix-id") or "").strip()
    timestamp = (headers.get("svix-timestamp") or "").strip()
    signatures = (headers.get("svix-signature") or "").strip()
    if not secret or not message_id or not timestamp or not signatures:
        return False
    try:
        timestamp_value = int(timestamp)
    except (TypeError, ValueError):
        return False
    tolerance = int(getattr(settings, "RESEND_WEBHOOK_TOLERANCE", 300))
    if abs(int(time.time()) - timestamp_value) > tolerance:
        return False

    encoded_secret = secret[6:] if secret.startswith("whsec_") else secret
    try:
        padding = "=" * (-len(encoded_secret) % 4)
        signing_key = base64.b64decode(encoded_secret + padding)
    except (ValueError, TypeError):
        return False
    signed = b".".join((message_id.encode(), timestamp.encode(), payload))
    expected = base64.b64encode(hmac.new(signing_key, signed, hashlib.sha256).digest()).decode()
    for token in signatures.split():
        candidate = token.split(",", 1)[1] if token.startswith("v1,") else ""
        if candidate and hmac.compare_digest(candidate, expected):
            return True
    return False


def _parse_datetime(value) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = parse_datetime(str(value or ""))
    return parsed or timezone.now()


def _addresses(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        value = [value]
    output = []
    for item in value:
        _name, address = parseaddr(str(item or ""))
        address = address.strip().lower()
        if address and address not in output:
            output.append(address)
    return output


def _platform_email_domains() -> set[str]:
    configured = getattr(settings, "PLATFORM_EMAIL_ALLOWED_DOMAINS", None)
    if configured:
        raw_domains = configured
        if isinstance(raw_domains, str):
            raw_domains = raw_domains.split(",")
        return {
            str(domain or "").strip().lower()
            for domain in raw_domains
            if str(domain or "").strip()
        }

    config = PlatformEmailConfiguration.load()
    addresses = {
        config.sender_email,
        config.inbound_email,
        config.reply_to_email,
        getattr(settings, "DEFAULT_FROM_EMAIL", ""),
    }
    domains = {"tawtheeq-ksa.com", "mail.tawtheeq-ksa.com"}
    for address in addresses:
        _name, parsed = parseaddr(str(address or ""))
        if "@" in parsed:
            domains.add(parsed.rsplit("@", 1)[-1].strip().lower())
    return {domain for domain in domains if domain}


def platform_email_domains() -> set[str]:
    """Return email domains that belong to this Tawtheeq installation."""

    return _platform_email_domains()


def _address_is_platform_owned(address: str) -> bool:
    _name, parsed = parseaddr(str(address or ""))
    if "@" not in parsed:
        return False
    domain = parsed.rsplit("@", 1)[-1].strip().lower()
    return domain in _platform_email_domains()


def _event_is_for_platform_mail(event_type: str, data: dict) -> bool:
    if event_type == "email.received":
        recipients = [
            *_addresses(data.get("to")),
            *_addresses(data.get("cc")),
            *_addresses(data.get("bcc")),
        ]
        return any(_address_is_platform_owned(address) for address in recipients)

    _name, sender_email = parseaddr(str(data.get("from") or ""))
    return _address_is_platform_owned(sender_email)


def _snippet(text: str, html: str = "") -> str:
    source = text or strip_tags(html or "")
    return re.sub(r"\s+", " ", source).strip()[:320]


def _email_html(body: str, *, subject: str = "", support_email: str = "") -> str:
    safe_body = "<br>".join(escape(body).splitlines())
    title = subject.strip() or "رسالة من منصة توثيق"
    return render_branded_email(
        "message.html",
        email_title=title,
        email_preheader=_snippet(body),
        email_intro="رسالة موجهة إليك من مركز الاتصال الرسمي في منصة توثيق.",
        body_html=safe_body,
        support_email=support_email,
        is_automated=False,
    )


def send_platform_email(
    *,
    created_by,
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    parent: PlatformEmail | None = None,
    attachments: list[dict] | None = None,
) -> PlatformEmail:
    config = PlatformEmailConfiguration.load()
    if not config.is_sending_enabled:
        raise ResendError("الإرسال متوقف من إعدادات بريد المنصة.")
    if not resend_is_configured():
        raise ResendError("أضف مفتاح Resend API إلى بيئة الخادم أولًا.")

    now = timezone.now()
    idempotency_key = f"platform-mail-{uuid.uuid4()}"
    email = PlatformEmail.objects.create(
        direction=PlatformEmail.Direction.OUTBOUND,
        status=PlatformEmail.Status.QUEUED,
        thread_key=parent.thread_key if parent else uuid.uuid4(),
        parent=parent,
        from_email=config.sender_email,
        from_name=config.sender_name,
        to_emails=to,
        cc_emails=cc or [],
        bcc_emails=bcc or [],
        reply_to_emails=[config.reply_to_email],
        subject=subject.strip() or "(بدون موضوع)",
        text_body=body,
        html_body=_email_html(
            body,
            subject=subject,
            support_email=config.reply_to_email,
        ),
        snippet=_snippet(body),
        created_by=created_by,
        last_event_at=now,
        provider_payload={"idempotency_key": idempotency_key},
        is_read=True,
    )
    payload = {
        "from": f"{config.sender_name} <{config.sender_email}>",
        "to": to,
        "subject": email.subject,
        "text": body,
        "html": email.html_body,
        "reply_to": config.reply_to_email,
        "tags": [
            {"name": "source", "value": "platform_mailbox"},
            {"name": "local_id", "value": str(email.pk)},
        ],
    }
    if cc:
        payload["cc"] = cc
    if bcc:
        payload["bcc"] = bcc
    if parent and parent.message_id:
        payload["headers"] = {
            "In-Reply-To": parent.message_id,
            "References": parent.message_id,
        }
    if attachments:
        payload["attachments"] = [
            {"filename": item["filename"], "content": item["content"]}
            for item in attachments
        ]
    try:
        response = _api_request("/emails", method="POST", payload=payload, idempotency_key=idempotency_key)
        provider_id = str(response.get("id") or "").strip()
        with transaction.atomic():
            email = PlatformEmail.objects.select_for_update().get(pk=email.pk)
            email.provider_id = provider_id or email.provider_id
            if email.status == PlatformEmail.Status.QUEUED:
                email.status = PlatformEmail.Status.SENT
                email.last_event_at = now
            email.sent_at = now
            email.provider_payload = {"idempotency_key": idempotency_key, "send_response": response}
            email.save(
                update_fields=(
                    "provider_id", "status", "sent_at", "last_event_at", "provider_payload", "updated_at",
                )
            )
            for item in attachments or []:
                PlatformEmailAttachment.objects.create(
                    email=email,
                    filename=item["filename"],
                    content_type=item.get("content_type", ""),
                    size=item.get("size", 0),
                )
        return email
    except ResendError as exc:
        email.status = PlatformEmail.Status.FAILED
        email.failure_reason = str(exc)
        email.last_event_at = timezone.now()
        email.save(update_fields=("status", "failure_reason", "last_event_at", "updated_at"))
        raise


def ingest_received_email(provider_id: str, metadata: dict | None = None) -> PlatformEmail:
    details = _api_request(f"/emails/receiving/{quote(provider_id, safe='')}")
    data = {**(metadata or {}), **details}
    headers = data.get("headers") if isinstance(data.get("headers"), dict) else {}
    sender_name, sender_email = parseaddr(str(headers.get("from") or data.get("from") or ""))
    message_id = str(data.get("message_id") or "").strip()
    in_reply_to = str(headers.get("in-reply-to") or headers.get("In-Reply-To") or "").strip()
    parent = PlatformEmail.objects.filter(message_id=in_reply_to).first() if in_reply_to else None
    received_at = _parse_datetime(data.get("created_at"))
    defaults = {
        "direction": PlatformEmail.Direction.INBOUND,
        "status": PlatformEmail.Status.RECEIVED,
        "message_id": message_id,
        "thread_key": parent.thread_key if parent else uuid.uuid4(),
        "parent": parent,
        "from_email": sender_email or str(data.get("from") or "unknown@example.invalid"),
        "from_name": sender_name,
        "to_emails": _addresses(data.get("to")),
        "cc_emails": _addresses(data.get("cc")),
        "bcc_emails": _addresses(data.get("bcc")),
        "reply_to_emails": _addresses(data.get("reply_to")),
        "subject": str(data.get("subject") or "(بدون موضوع)")[:500],
        "text_body": str(data.get("text") or ""),
        "html_body": str(data.get("html") or ""),
        "snippet": _snippet(str(data.get("text") or ""), str(data.get("html") or "")),
        "raw_headers": headers,
        "provider_payload": {key: value for key, value in data.items() if key not in {"html", "text", "raw"}},
        "is_read": False,
        "received_at": received_at,
        "last_event_at": received_at,
    }
    with transaction.atomic():
        was_read = PlatformEmail.objects.filter(provider_id=provider_id, is_read=True).exists()
        email, created = PlatformEmail.objects.update_or_create(provider_id=provider_id, defaults=defaults)
        if not created and was_read:
            email.is_read = True
            email.save(update_fields=("is_read", "updated_at"))
        for attachment in data.get("attachments") or []:
            attachment_id = str(attachment.get("id") or "")
            values = {
                "filename": str(attachment.get("filename") or "مرفق")[:255],
                "content_type": str(attachment.get("content_type") or "")[:160],
                "content_disposition": str(attachment.get("content_disposition") or "")[:40],
                "content_id": str(attachment.get("content_id") or "")[:255],
                "size": max(0, int(attachment.get("size") or 0)),
            }
            if attachment_id:
                PlatformEmailAttachment.objects.update_or_create(
                    email=email,
                    provider_id=attachment_id,
                    defaults=values,
                )
            else:
                PlatformEmailAttachment.objects.create(email=email, **values)
    return email


EVENT_STATUS_MAP = {
    "email.sent": PlatformEmail.Status.SENT,
    "email.delivered": PlatformEmail.Status.DELIVERED,
    "email.delivery_delayed": PlatformEmail.Status.DELAYED,
    "email.bounced": PlatformEmail.Status.BOUNCED,
    "email.complained": PlatformEmail.Status.COMPLAINED,
    "email.failed": PlatformEmail.Status.FAILED,
    "email.suppressed": PlatformEmail.Status.SUPPRESSED,
}


@transaction.atomic
def process_webhook_event(event: dict, provider_event_id: str) -> PlatformEmailEvent:
    event_type = str(event.get("type") or "")
    data = event.get("data")
    if not isinstance(data, dict):
        data = {}
    occurred_at = _parse_datetime(event.get("created_at"))
    recorded, created = PlatformEmailEvent.objects.get_or_create(
        provider_event_id=provider_event_id,
        defaults={"event_type": event_type or "unknown", "occurred_at": occurred_at, "payload": event},
    )
    if not created:
        return recorded

    email = None
    provider_id = str(data.get("email_id") or data.get("id") or "").strip()
    if event_type == "email.received" and provider_id:
        if _event_is_for_platform_mail(event_type, data):
            email = ingest_received_email(provider_id, data)
        else:
            logger.info(
                "Ignored Resend inbound webhook outside Tawtheeq mail scope event_type=%s provider_id=%s",
                event_type,
                provider_id,
            )
    elif provider_id:
        messages = PlatformEmail.objects.select_for_update()
        email = messages.filter(provider_id=provider_id).first()
        if email is None:
            if not _event_is_for_platform_mail(event_type, data):
                logger.info(
                    "Ignored Resend outbound webhook outside Tawtheeq mail scope event_type=%s provider_id=%s",
                    event_type,
                    provider_id,
                )
            else:
                sender_name, sender_email = parseaddr(str(data.get("from") or ""))
                tags = data.get("tags")
                if isinstance(tags, dict) and tags.get("source") == "platform_mailbox":
                    local_id = str(tags.get("local_id") or "")
                    if re.fullmatch(r"[1-9][0-9]{0,17}", local_id):
                        email = messages.filter(
                            pk=int(local_id), direction=PlatformEmail.Direction.OUTBOUND, from_email__iexact=sender_email,
                        ).first()
                        if email and email.provider_id is None:
                            email.provider_id = provider_id
                            email.save(update_fields=("provider_id", "updated_at"))
                        elif email and email.provider_id != provider_id:
                            email = None
                if email is None:
                    email, _ = messages.get_or_create(
                        provider_id=provider_id,
                        defaults={
                            "direction": PlatformEmail.Direction.OUTBOUND,
                            "status": EVENT_STATUS_MAP.get(event_type, PlatformEmail.Status.SENT),
                            "from_email": sender_email or "unknown@example.invalid",
                            "from_name": sender_name,
                            "to_emails": _addresses(data.get("to")),
                            "cc_emails": _addresses(data.get("cc")),
                            "bcc_emails": _addresses(data.get("bcc")),
                            "subject": str(data.get("subject") or "(رسالة مرسلة من المنصة)")[:500],
                            "snippet": "سُجلت الرسالة من أحداث مزود البريد.",
                            "is_read": True,
                            "sent_at": occurred_at,
                            "last_event_at": occurred_at,
                            "provider_payload": data,
                        },
                    )

    if email is not None:
        update_fields = ["last_event_at", "updated_at"]
        email.last_event_at = occurred_at
        # Resend can deliver the initial acceptance event after a delivery outcome.
        if event_type in EVENT_STATUS_MAP and (
            event_type != "email.sent" or email.status in (PlatformEmail.Status.QUEUED, PlatformEmail.Status.SENT)
        ):
            email.status = EVENT_STATUS_MAP[event_type]
            update_fields.append("status")
        if event_type == "email.delivered":
            email.delivered_at = occurred_at
            update_fields.append("delivered_at")
        elif event_type == "email.opened":
            email.opened_at = email.opened_at or occurred_at
            email.opened_count += 1
            update_fields.extend(("opened_at", "opened_count"))
        elif event_type == "email.clicked":
            email.clicked_at = email.clicked_at or occurred_at
            email.clicked_count += 1
            update_fields.extend(("clicked_at", "clicked_count"))
        elif event_type in {"email.failed", "email.bounced"}:
            bounce = data.get("bounce") if isinstance(data.get("bounce"), dict) else {}
            email.failure_reason = str(
                data.get("reason") or data.get("error") or bounce.get("message") or ""
            )[:2000]
            update_fields.append("failure_reason")
        email.save(update_fields=tuple(dict.fromkeys(update_fields)))

    if email is not None:
        recorded.email = email
        recorded.save(update_fields=("email",))
    return recorded


def sync_recent_received_emails(limit: int = 50) -> tuple[int, int]:
    response = _api_request(f"/emails/receiving?limit={max(1, min(limit, 100))}")
    rows = response.get("data") if isinstance(response.get("data"), list) else []
    created = 0
    failed = 0
    for row in rows:
        provider_id = str(row.get("id") or "").strip()
        if not provider_id or PlatformEmail.objects.filter(provider_id=provider_id).exists():
            continue
        try:
            ingest_received_email(provider_id, row)
            created += 1
        except ResendError:
            failed += 1
    return created, failed


def attachment_download_url(email: PlatformEmail, attachment: PlatformEmailAttachment) -> str:
    if email.direction != PlatformEmail.Direction.INBOUND or not email.provider_id or not attachment.provider_id:
        raise ResendError("هذا المرفق لا يملك رابط تنزيل من مزود البريد.")
    payload = _api_request(
        "/emails/receiving/{}/attachments/{}".format(
            quote(email.provider_id, safe=""),
            quote(attachment.provider_id, safe=""),
        )
    )
    url = str(payload.get("download_url") or "")
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (
        hostname == "resend.com"
        or hostname.endswith(".resend.com")
        or hostname.endswith(".cloudfront.net")
    ):
        raise ResendError("لم يُرجع مزود البريد رابط تنزيل آمنًا.")
    return url
