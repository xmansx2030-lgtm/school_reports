from __future__ import annotations

import logging
import os
from functools import partial
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone

from .models import Incident, IncidentEvent, MobileDevice, OperationsMembership, OperationsPaymentLink

logger = logging.getLogger(__name__)

FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
FCM_TIMEOUT = (4, 8)


class FCMConnectionError(RuntimeError):
    """No notification could be submitted because connections failed."""


def fcm_is_configured() -> bool:
    return bool(
        str(getattr(settings, "FCM_PROJECT_ID", "") or "").strip()
        and str(os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or "").strip()
    )


def _send_push(
    devices: list[MobileDevice],
    *,
    title: str,
    body: str,
    data: dict[str, str],
    log_context: str,
) -> dict[str, int]:
    result = {"sent": 0, "failed": 0, "disabled": 0}
    if not devices:
        return result
    if not fcm_is_configured():
        result["disabled"] = len(devices)
        return result

    from google.auth.transport.requests import Request
    from google.oauth2 import service_account
    import requests
    from urllib3.exceptions import ConnectTimeoutError, MaxRetryError
    from urllib3.util import Retry

    credentials = service_account.Credentials.from_service_account_file(
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"],
        scopes=[FCM_SCOPE],
    )
    connection_failures = 0
    with requests.Session() as session:
        # Retry only before a request reaches FCM, when no duplicate push is possible.
        retries = Retry(total=2, connect=2, read=0, status=0, other=0, backoff_factor=0.25)
        session.mount("https://", requests.adapters.HTTPAdapter(max_retries=retries))
        credentials.refresh(partial(Request(session=session), timeout=FCM_TIMEOUT))
        project_id = str(settings.FCM_PROJECT_ID).strip()
        endpoint = f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
        headers = {"Authorization": f"Bearer {credentials.token}", "Content-Type": "application/json; charset=UTF-8"}
        for device in devices:
            payload = {
                "message": {
                    "token": device.fcm_token,
                    "notification": {"title": title, "body": body},
                    "data": data,
                    "android": {
                        "priority": "high",
                        "notification": {
                            "channel_id": "operations_alerts",
                            "sound": "default",
                            **({"tag": f"incident-{data['incident_id']}"} if "incident_id" in data else {}),
                        },
                    },
                }
            }
            try:
                response = session.post(endpoint, headers=headers, json=payload, timeout=FCM_TIMEOUT)
                if response.ok:
                    result["sent"] += 1
                else:
                    result["failed"] += 1
                    failure_body = response.text[:500]
                    if response.status_code in (400, 404) and (
                        "UNREGISTERED" in failure_body
                        or "registration-token-not-registered" in failure_body
                    ):
                        MobileDevice.objects.filter(pk=device.pk, fcm_token=device.fcm_token).update(
                            is_active=False, fcm_token="",
                        )
                    logger.warning(
                        "FCM %s delivery failed device=%s status=%s",
                        log_context,
                        device.pk,
                        response.status_code,
                    )
            except requests.RequestException as exc:
                result["failed"] += 1
                retry_error = exc.args[0] if exc.args else None
                if isinstance(exc, requests.ConnectTimeout) or (
                    isinstance(retry_error, MaxRetryError) and isinstance(retry_error.reason, ConnectTimeoutError)
                ):
                    connection_failures += 1
                logger.warning("FCM %s delivery error device=%s type=%s", log_context, device.pk, type(exc).__name__)
    if connection_failures == len(devices):
        raise FCMConnectionError("Could not connect to Firebase to send notifications.")
    return result


def _in_quiet_hours(membership: OperationsMembership, minute: int) -> bool:
    start, end = membership.quiet_start_minute, membership.quiet_end_minute
    if start is None or end is None or start == end:
        return False
    return start <= minute < end if start < end else minute >= start or minute < end


def send_incident_push(incident: Incident, *, event: IncidentEvent | None = None) -> dict[str, int]:
    devices = MobileDevice.objects.filter(
        is_active=True,
        alerts_enabled=True,
        user__is_active=True,
        user__operations_membership__is_active=True,
    ).exclude(fcm_token="").select_related("user__operations_membership")
    severity = event.severity if event else incident.severity
    recipients = list(devices)
    if severity == Incident.Severity.INFO:
        local = (event.occurred_at if event else timezone.now()).astimezone(ZoneInfo("Asia/Riyadh"))
        minute = local.hour * 60 + local.minute
        recipients = [device for device in recipients if not _in_quiet_hours(device.user.operations_membership, minute)]
    resolved = event.kind == IncidentEvent.Kind.RESOLVED if event else incident.status == Incident.Status.RESOLVED
    title = (
        "تمت تسوية حالة الإصدارات"
        if resolved and incident.dedupe_key.startswith("deployment:")
        else f"عادت {incident.project.name} للعمل"
        if resolved and incident.project_id
        else f"زالت حالة التحذير عن {incident.server.name}"
        if resolved and incident.server_id
        else incident.title
    )
    body = "نجحت الفحوصات وعادت الحالة إلى المستوى الطبيعي." if resolved else (event.message if event else incident.message)[:220]
    result = _send_push(
        recipients,
        title=title,
        body=body,
        data={
            "type": "operations_incident",
            "incident_id": str(incident.pk),
            "severity": severity,
            "project_id": str(incident.project_id or ""),
        },
        log_context="incident",
    )
    if result["sent"]:
        Incident.objects.filter(pk=incident.pk).update(last_notified_at=timezone.now())
    return result


def send_payment_paid_push(link: OperationsPaymentLink) -> dict[str, int]:
    """Notify every active Operations admin device without exposing customer data."""
    devices = list(
        MobileDevice.objects.filter(
            is_active=True,
            alerts_enabled=True,
            user__is_active=True,
            user__operations_membership__is_active=True,
            user__operations_membership__role=OperationsMembership.Role.ADMIN,
        )
        .exclude(fcm_token="")
        .order_by("pk")
    )
    return _send_push(
        devices,
        title=f"تم استلام دفعة لمشروع {link.project.name}",
        body=f"المبلغ: {link.amount} {link.currency}",
        data={
            "type": "operations_payment_paid",
            "payment_link_id": str(link.public_id),
            "project_id": str(link.project_id),
            "status": OperationsPaymentLink.Status.PAID,
        },
        log_context="payment-paid",
    )
