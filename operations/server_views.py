"""Authenticated cloud-provider views; only the recorded server can be targeted."""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from rest_framework.decorators import api_view, authentication_classes
from rest_framework.response import Response

from .authentication import OperationsTokenAuthentication
from .hetzner import HetznerClient, HetznerError
from .inventory import SERVER_DEFAULTS
from .models import ManagedServer, OperationsMembership, ProviderAction


def _server(pk: int) -> ManagedServer | None:
    return ManagedServer.objects.filter(
        pk=pk,
        is_active=True,
        provider="hetzner",
        slug=SERVER_DEFAULTS["slug"],
        provider_server_id=SERVER_DEFAULTS["provider_server_id"],
    ).first()


def _admin(user) -> bool:
    return OperationsMembership.objects.filter(
        user=user, is_active=True, role=OperationsMembership.Role.ADMIN
    ).exists()


@api_view(["GET"])
@authentication_classes([OperationsTokenAuthentication])
def provider_overview(request, server_id: int):
    server = _server(server_id)
    if server is None:
        return Response({"detail": "الخادم غير موجود."}, status=404)
    try:
        data = HetznerClient().overview(server.provider_server_id)
    except HetznerError as exc:
        return Response({"configured": exc.code != "not_configured", "detail": str(exc), "error_code": exc.code}, status=200 if exc.code == "not_configured" else 502)
    return Response({"configured": True, "server": data, "can_control": _admin(request.user) and bool(settings.OPERATIONS_HETZNER_WRITE_TOKEN)})


@api_view(["POST"])
@authentication_classes([OperationsTokenAuthentication])
def provider_action(request, server_id: int):
    server = _server(server_id)
    if server is None:
        return Response({"detail": "الخادم غير موجود."}, status=404)
    if not _admin(request.user):
        return Response({"detail": "هذا الإجراء متاح لمدير العمليات فقط."}, status=403)
    action = str(request.data.get("action") or "").strip()
    if action not in {"poweron", "reboot", "shutdown", "snapshot"}:
        return Response({"detail": "إجراء غير مسموح."}, status=400)
    expected = f"{server.slug}:{action}"
    if str(request.data.get("confirmation") or "").strip() != expected:
        return Response({"detail": f"اكتب {expected} لتأكيد الإجراء على جميع المشاريع.", "confirmation_required": expected}, status=409)
    if ProviderAction.objects.filter(
        server=server, action=action, status__in=("requested", "running"),
        requested_at__gte=timezone.now() - timedelta(minutes=10),
    ).exists():
        return Response({"detail": "يوجد إجراء مماثل قيد المتابعة."}, status=409)
    try:
        client = HetznerClient(write=True)
        state = client.server_state(server.provider_server_id)["status"]
        if action == "poweron" and state != "off":
            return Response({"detail": "الخادم ليس متوقفًا."}, status=409)
        if action in {"reboot", "shutdown"} and state != "running":
            return Response({"detail": "الخادم ليس في حالة تشغيل تسمح بهذا الإجراء."}, status=409)
        if action == "snapshot" and state != "running":
            return Response({"detail": "إنشاء Snapshot يتطلب تشغيل الخادم."}, status=409)
        result = client.action(server.provider_server_id, action)
    except HetznerError as exc:
        return Response({"detail": str(exc), "error_code": exc.code}, status=503 if exc.code == "not_configured" else 502)
    record = ProviderAction.objects.create(
        server=server,
        action=action,
        provider_action_id=result.get("id"),
        status=str(result.get("status") or "requested")[:20],
        requested_by=request.user,
    )
    return Response({"id": record.pk, "action": action, "status": record.status, "provider_action_id": record.provider_action_id}, status=202)


@api_view(["GET"])
@authentication_classes([OperationsTokenAuthentication])
def provider_action_status(request, server_id: int, action_id: int):
    server = _server(server_id)
    if server is None:
        return Response({"detail": "الخادم غير موجود."}, status=404)
    record = ProviderAction.objects.filter(pk=action_id, server=server).first()
    if record is None:
        return Response({"detail": "الإجراء غير موجود."}, status=404)
    if record.provider_action_id and record.status not in {"success", "error"}:
        try:
            state = HetznerClient().action_status(record.provider_action_id)
            record.status = str(state.get("status") or record.status)[:20]
            if record.status in {"success", "error"}:
                record.finished_at = timezone.now()
                record.error_code = str((state.get("error") or {}).get("code") or "")[:80]
            record.save(update_fields=("status", "finished_at", "error_code"))
        except HetznerError:
            pass
    return Response({"id": record.pk, "action": record.action, "status": record.status, "error_code": record.error_code, "requested_at": record.requested_at, "finished_at": record.finished_at})
