"""Authenticated, read-only server and project analytics."""

from rest_framework.decorators import api_view, authentication_classes
from rest_framework.response import Response

from .analytics import project_analytics, server_analytics
from .authentication import OperationsTokenAuthentication
from .models import ManagedProject, ManagedServer


@api_view(["GET"])
@authentication_classes([OperationsTokenAuthentication])
def server_history(request, server_id: int):
    server = ManagedServer.objects.filter(pk=server_id, is_active=True).first()
    if server is None:
        return Response({"detail": "الخادم غير موجود."}, status=404)
    try:
        payload = server_analytics(server, str(request.query_params.get("window", "24h")))
    except ValueError as exc:
        return Response({"detail": str(exc)}, status=400)
    return Response(payload)


@api_view(["GET"])
@authentication_classes([OperationsTokenAuthentication])
def project_history(request, project_id: int):
    project = ManagedProject.objects.filter(pk=project_id, is_active=True).first()
    if project is None:
        return Response({"detail": "المشروع غير موجود."}, status=404)
    try:
        payload = project_analytics(project, str(request.query_params.get("window", "24h")))
    except ValueError as exc:
        return Response({"detail": str(exc)}, status=400)
    return Response(payload)
