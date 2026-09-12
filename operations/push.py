from __future__ import annotations

import logging
import os

from django.conf import settings
from django.utils import timezone

from .models import Incident, MobileDevice

logger = logging.getLogger(__name__)

FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"


def fcm_is_configured() -> bool:
    return bool(
        str(getattr(settings, "FCM_PROJECT_ID", "") or "").strip()
        and str(os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or "").strip()
    )


def send_incident_push(incident: Incident) -> dict[str, int]:
    result = {"sent": 0, "failed": 0, "disabled": 0}
    devices = list(MobileDevice.objects.filter(is_active=True, alerts_enabled=True).exclude(fcm_token=""))
    if not devices:
        return result
    if not fcm_is_configured():
        result["disabled"] = len(devices)
        return result

    from google.auth.transport.requests import Request
    from google.oauth2 import service_account
    import requests

    credentials = service_account.Credentials.from_service_account_file(
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"],
        scopes=[FCM_SCOPE],
    )
    credentials.refresh(Request())
    project_id = str(settings.FCM_PROJECT_ID).strip()
    endpoint = f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
    headers = {"Authorization": f"Bearer {credentials.token}", "Content-Type": "application/json; charset=UTF-8"}
    resolved = incident.status == Incident.Status.RESOLVED
    title = (
        f"عادت {incident.project.name} للعمل"
        if resolved and incident.project_id
        else f"زالت حالة التحذير عن {incident.server.name}"
        if resolved and incident.server_id
        else incident.title
    )
    body = "نجحت الفحوصات وعادت الحالة إلى المستوى الطبيعي." if resolved else incident.message[:220]
    for device in devices:
        payload = {
            "message": {
                "token": device.fcm_token,
                "notification": {"title": title, "body": body},
                "data": {
                    "type": "operations_incident",
                    "incident_id": str(incident.pk),
                    "severity": incident.severity,
                    "project_id": str(incident.project_id or ""),
                },
                "android": {
                    "priority": "high",
                    "notification": {"channel_id": "operations_alerts", "sound": "default"},
                },
            }
        }
        try:
            response = requests.post(endpoint, headers=headers, json=payload, timeout=(4, 8))
            if response.ok:
                result["sent"] += 1
            else:
                result["failed"] += 1
                failure_body = response.text[:500]
                if response.status_code in (400, 404) and (
                    "UNREGISTERED" in failure_body
                    or "registration-token-not-registered" in failure_body
                ):
                    MobileDevice.objects.filter(pk=device.pk).update(is_active=False, fcm_token="")
                logger.warning("FCM incident delivery failed device=%s status=%s", device.pk, response.status_code)
        except requests.RequestException:
            result["failed"] += 1
            logger.exception("FCM incident delivery error device=%s", device.pk)
    Incident.objects.filter(pk=incident.pk).update(last_notified_at=timezone.now())
    return result
