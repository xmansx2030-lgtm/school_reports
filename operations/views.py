from __future__ import annotations

from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.db import transaction
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.decorators import api_view, authentication_classes, permission_classes, throttle_classes
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle

from reports.models import TeacherTotpDevice
from reports.totp import decrypt_secret, verify_code

from .authentication import OperationsTokenAuthentication, has_operations_access
from .deployments import DeploymentIntegrationError, GitHubDeploymentClient, all_deployment_states
from .models import Incident, ManagedProject, ManagedServer, MobileAccessToken, MobileDevice, OperationAction, OperationsMembership
from .serializers import (
    IncidentSerializer,
    ManagedProjectSerializer,
    ManagedServerSerializer,
    OperationActionSerializer,
    ProjectMetricSerializer,
)
from .services import probe_project


class OperationsLoginThrottle(AnonRateThrottle):
    rate = "10/hour"


ROLE_CAPABILITIES = {
    "owner": ("view", "run_checks", "run_actions", "acknowledge_incidents", "manage_team"),
    OperationsMembership.Role.ADMIN: ("view", "run_checks", "run_actions", "acknowledge_incidents", "manage_team"),
    OperationsMembership.Role.OPERATOR: ("view", "run_checks", "run_actions", "acknowledge_incidents"),
    OperationsMembership.Role.VIEWER: ("view",),
}


def _operations_profile(user) -> tuple[str, str, tuple[str, ...]]:
    membership = OperationsMembership.objects.filter(user=user, is_active=True).first()
    if membership is not None:
        return membership.role, membership.get_role_display(), ROLE_CAPABILITIES[membership.role]
    return "", "غير مخول", ()


def _has_capability(user, capability: str) -> bool:
    return capability in _operations_profile(user)[2]


def _account_payload(user) -> dict:
    role, role_label, capabilities = _operations_profile(user)
    last_token = user.operations_mobile_tokens.order_by("-last_used_at", "-created_at").first()
    return {
        "id": user.pk,
        "name": user.name,
        "phone": user.phone,
        "email": getattr(user, "email", "") or "",
        "is_active": user.is_active,
        "is_staff": user.is_staff,
        "is_superuser": user.is_superuser,
        "date_joined": user.date_joined,
        "last_login": user.last_login,
        "last_seen_at": last_token.last_used_at if last_token else None,
        "active_devices": user.operations_mobile_devices.filter(is_active=True).count(),
        "role": role,
        "role_label": role_label,
        "capabilities": capabilities,
    }


@api_view(["POST"])
@authentication_classes([])
@permission_classes([permissions.AllowAny])
@throttle_classes([OperationsLoginThrottle])
def login(request):
    phone = str(request.data.get("phone") or "").strip()
    password = str(request.data.get("password") or "")
    user = authenticate(request=request, username=phone, password=password)
    if not has_operations_access(user):
        return Response({"detail": "بيانات الدخول غير صحيحة أو الحساب غير مخول."}, status=status.HTTP_401_UNAUTHORIZED)

    totp_device = TeacherTotpDevice.objects.filter(teacher=user, confirmed_at__isnull=False).first()
    if totp_device is not None:
        secret = decrypt_secret(totp_device.secret_encrypted)
        counter = verify_code(secret or "", str(request.data.get("otp") or ""), last_used_counter=totp_device.last_used_counter)
        if counter is None:
            return Response({"detail": "رمز التحقق مطلوب أو غير صحيح.", "otp_required": True}, status=status.HTTP_401_UNAUTHORIZED)
        with transaction.atomic():
            locked = TeacherTotpDevice.objects.select_for_update().get(pk=totp_device.pk)
            counter = verify_code(secret or "", str(request.data.get("otp") or ""), last_used_counter=locked.last_used_counter)
            if counter is None:
                return Response({"detail": "رمز التحقق استُخدم أو انتهت صلاحيته.", "otp_required": True}, status=status.HTTP_401_UNAUTHORIZED)
            locked.last_used_counter = counter
            locked.last_used_at = timezone.now()
            locked.save(update_fields=("last_used_counter", "last_used_at"))

    token, raw = MobileAccessToken.issue(user=user, device_name=str(request.data.get("device_name") or ""))
    return Response({
        "token": raw,
        "expires_at": token.expires_at,
        "user": _account_payload(user),
    })


@api_view(["POST"])
@authentication_classes([OperationsTokenAuthentication])
def logout(request):
    token = request.auth
    token.revoked_at = timezone.now()
    token.save(update_fields=("revoked_at",))
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(["POST"])
@authentication_classes([OperationsTokenAuthentication])
def change_password(request):
    current_password = str(request.data.get("current_password") or "")
    new_password = str(request.data.get("new_password") or "")
    if not request.user.check_password(current_password):
        return Response({"detail": "كلمة المرور الحالية غير صحيحة."}, status=status.HTTP_400_BAD_REQUEST)
    if len(new_password) < 10:
        return Response({"detail": "كلمة المرور الجديدة يجب ألا تقل عن 10 أحرف."}, status=status.HTTP_400_BAD_REQUEST)
    request.user.set_password(new_password)
    request.user.save(update_fields=("password",))
    MobileAccessToken.objects.filter(user=request.user).exclude(pk=request.auth.pk).update(revoked_at=timezone.now())
    return Response({"detail": "تم تغيير كلمة المرور بنجاح."})


@api_view(["GET", "POST"])
@authentication_classes([OperationsTokenAuthentication])
def accounts(request):
    if not _has_capability(request.user, "manage_team"):
        return Response({"detail": "لا تملك صلاحية إدارة فريق العمليات."}, status=status.HTTP_403_FORBIDDEN)
    User = get_user_model()
    if request.method == "GET":
        users = User.objects.filter(operations_membership__isnull=False).distinct().order_by("name", "phone")
        return Response({
            "accounts": [_account_payload(user) for user in users],
            "roles": [
                {"value": value, "label": label}
                for value, label in OperationsMembership.Role.choices
            ],
        })

    name = str(request.data.get("name") or "").strip()
    phone = str(request.data.get("phone") or "").strip()
    password = str(request.data.get("password") or "")
    email = str(request.data.get("email") or "").strip()
    role = str(request.data.get("role") or OperationsMembership.Role.VIEWER)
    if not name or not phone or len(password) < 10:
        return Response({"detail": "الاسم ورقم الجوال وكلمة مرور من 10 أحرف على الأقل مطلوبة."}, status=status.HTTP_400_BAD_REQUEST)
    if User.objects.filter(phone=phone).exists():
        return Response({"detail": "رقم الجوال مستخدم مسبقًا."}, status=status.HTTP_400_BAD_REQUEST)
    if role not in OperationsMembership.Role.values:
        return Response({"detail": "دور فريق العمليات غير صالح."}, status=status.HTTP_400_BAD_REQUEST)
    with transaction.atomic():
        user = User.objects.create_user(phone=phone, name=name, password=password, email=email)
        OperationsMembership.objects.create(user=user, role=role, created_by=request.user)
    return Response(_account_payload(user), status=status.HTTP_201_CREATED)


@api_view(["PATCH"])
@authentication_classes([OperationsTokenAuthentication])
def account_detail(request, user_id: int):
    if not _has_capability(request.user, "manage_team"):
        return Response({"detail": "لا تملك صلاحية إدارة فريق العمليات."}, status=status.HTTP_403_FORBIDDEN)
    User = get_user_model()
    user = User.objects.filter(pk=user_id, operations_membership__isnull=False).first()
    if user is None:
        return Response({"detail": "الحساب غير موجود."}, status=status.HTTP_404_NOT_FOUND)

    updates = []
    if "name" in request.data:
        user.name = str(request.data.get("name") or "").strip()
        updates.append("name")
    if "email" in request.data:
        user.email = str(request.data.get("email") or "").strip()
        updates.append("email")
    if "is_active" in request.data:
        is_active = bool(request.data.get("is_active"))
        if not is_active and user.pk == request.user.pk:
            return Response({"detail": "لا يمكنك تعطيل حسابك الحالي."}, status=status.HTTP_400_BAD_REQUEST)
        if not is_active and OperationsMembership.objects.filter(is_active=True, user__is_active=True).exclude(user=user).count() == 0:
            return Response({"detail": "يجب بقاء حساب عمليات نشط واحد على الأقل."}, status=status.HTTP_400_BAD_REQUEST)
        user.is_active = is_active
        updates.append("is_active")
        OperationsMembership.objects.filter(user=user).update(is_active=is_active)
    if "role" in request.data:
        role = str(request.data.get("role") or "")
        if role not in OperationsMembership.Role.values:
            return Response({"detail": "دور فريق العمليات غير صالح."}, status=status.HTTP_400_BAD_REQUEST)
        OperationsMembership.objects.update_or_create(
            user=user,
            defaults={"role": role, "is_active": user.is_active, "created_by": request.user},
        )
    if request.data.get("password"):
        password = str(request.data.get("password") or "")
        if len(password) < 10:
            return Response({"detail": "كلمة المرور الجديدة يجب ألا تقل عن 10 أحرف."}, status=status.HTTP_400_BAD_REQUEST)
        user.set_password(password)
        updates.append("password")
        MobileAccessToken.objects.filter(user=user).update(revoked_at=timezone.now())
    if not user.name:
        return Response({"detail": "اسم الحساب مطلوب."}, status=status.HTTP_400_BAD_REQUEST)
    if updates:
        user.save(update_fields=tuple(dict.fromkeys(updates)))
    return Response(_account_payload(user))


@api_view(["GET"])
@authentication_classes([OperationsTokenAuthentication])
def dashboard(request):
    servers = ManagedServer.objects.filter(is_active=True).prefetch_related("projects__services")
    projects = list(ManagedProject.objects.filter(is_active=True))
    incidents = Incident.objects.filter(status__in=(Incident.Status.OPEN, Incident.Status.ACKNOWLEDGED)).select_related("project")[:20]
    return Response({
        "generated_at": timezone.now(),
        "summary": {
            "servers": servers.count(),
            "projects": len(projects),
            "healthy_projects": sum(
                project.effective_status == ManagedProject.Status.HEALTHY for project in projects
            ),
            "open_incidents": Incident.objects.filter(status__in=(Incident.Status.OPEN, Incident.Status.ACKNOWLEDGED)).count(),
            "team_members": OperationsMembership.objects.filter(is_active=True, user__is_active=True).count(),
        },
        "current_user": _account_payload(request.user),
        "agent": {
            "ready": bool(getattr(settings, "OPERATIONS_AGENT_ENABLED", False)),
            "label": "متصل" if getattr(settings, "OPERATIONS_AGENT_ENABLED", False) else "غير مفعّل",
        },
        "servers": ManagedServerSerializer(servers, many=True).data,
        "incidents": IncidentSerializer(incidents, many=True).data,
    })


@api_view(["GET"])
@authentication_classes([OperationsTokenAuthentication])
def deployment_status(request):
    states = all_deployment_states()
    return Response({
        "deployments": [state.as_dict() for state in states],
        "repository_ahead_count": sum(1 for state in states if state.repository_ahead),
        "can_deploy_count": sum(1 for state in states if state.as_dict()["can_deploy"]),
    })


@api_view(["POST"])
@authentication_classes([OperationsTokenAuthentication])
def trigger_deployment(request):
    if not _has_capability(request.user, "run_actions"):
        return Response(
            {"detail": "لا تملك صلاحية تشغيل نشر المشاريع."},
            status=status.HTTP_403_FORBIDDEN,
        )
    project_id = request.data.get("project_id")
    project = ManagedProject.objects.filter(pk=project_id, is_active=True).first()
    if project is None:
        return Response({"detail": "المشروع غير موجود."}, status=404)
    client = GitHubDeploymentClient(project)
    state = client.deployment_state()
    confirmation = str(request.data.get("confirmation") or "").strip()
    if not state.repository_ahead:
        return Response({"detail": "الخادم مطابق للمستودع ولا يوجد إصدار أحدث للنشر."}, status=409)
    if state.workflow_status in {"queued", "in_progress", "waiting", "requested", "pending"}:
        return Response({"detail": "يوجد نشر قيد التنفيذ بالفعل. تابع حالته بدل تشغيل نشر جديد."}, status=409)
    if confirmation != state.latest_sha[:12]:
        return Response(
            {
                "detail": f"اكتب رقم الإصدار {state.latest_sha[:12]} لتأكيد النشر.",
                "confirmation_required": state.latest_sha[:12],
            },
            status=409,
        )
    try:
        client.trigger_deploy(source_sha=state.latest_sha)
    except DeploymentIntegrationError as exc:
        return Response({"detail": str(exc)}, status=502)
    return Response({
        "detail": "تم تشغيل مسار النشر في GitHub Actions.",
        "state": client.deployment_state().as_dict(),
    }, status=202)


@api_view(["GET"])
@authentication_classes([OperationsTokenAuthentication])
def project_detail(request, project_id: int):
    project = (
        ManagedProject.objects.select_related("server")
        .prefetch_related("services")
        .filter(pk=project_id, is_active=True)
        .first()
    )
    if project is None:
        return Response({"detail": "المشروع غير موجود."}, status=404)
    checks = project.health_checks.all()[:48]
    actions = project.actions.select_related("requested_by")[:30]
    metrics = project.metric_snapshots.all()[:48]
    payload = ManagedProjectSerializer(project).data
    payload.update({
        "server": ManagedServerSerializer(project.server).data,
        "checks": [{"ok": row.ok, "status_code": row.status_code, "latency_ms": row.latency_ms, "error_code": row.error_code, "checked_at": row.checked_at} for row in checks],
        "metrics": ProjectMetricSerializer(metrics, many=True).data,
        "actions": OperationActionSerializer(actions, many=True).data,
    })
    return Response(payload)


@api_view(["POST"])
@authentication_classes([OperationsTokenAuthentication])
def create_action(request, project_id: int):
    project = ManagedProject.objects.filter(pk=project_id, is_active=True).first()
    if project is None:
        return Response({"detail": "المشروع غير موجود."}, status=404)
    action_name = str(request.data.get("action") or "")
    allowed = {choice for choice, _ in OperationAction.Action.choices}
    if action_name not in allowed:
        return Response({"detail": "الإجراء غير مسموح."}, status=400)
    capability = "run_checks" if action_name == OperationAction.Action.CHECK_NOW else "run_actions"
    if not _has_capability(request.user, capability):
        return Response({"detail": "لا تملك صلاحية تنفيذ هذا الإجراء."}, status=status.HTTP_403_FORBIDDEN)
    destructive = action_name != OperationAction.Action.CHECK_NOW
    if destructive and str(request.data.get("confirmation") or "") != project.slug:
        return Response({"detail": f"اكتب {project.slug} لتأكيد الإجراء.", "confirmation_required": project.slug}, status=409)

    service = None
    if request.data.get("service_id"):
        service = project.services.filter(pk=request.data.get("service_id"), is_active=True).first()
        if service is None:
            return Response({"detail": "الخدمة غير موجودة ضمن المشروع."}, status=400)
        if action_name == OperationAction.Action.RESTART_SERVICE and not service.restart_allowed:
            return Response({"detail": "إعادة تشغيل هذه الخدمة غير مفعلة."}, status=403)

    action = OperationAction.objects.create(project=project, service=service, action=action_name, requested_by=request.user)
    if action_name == OperationAction.Action.CHECK_NOW:
        action.status = OperationAction.Status.RUNNING
        action.started_at = timezone.now()
        action.save(update_fields=("status", "started_at"))
        check = probe_project(project)
        action.status = OperationAction.Status.SUCCEEDED if check.ok else OperationAction.Status.FAILED
        action.result_summary = "اكتمل الفحص بنجاح." if check.ok else f"فشل الفحص: {check.error_code or check.status_code}."
        action.finished_at = timezone.now()
        action.save(update_fields=("status", "result_summary", "finished_at"))
    elif not bool(getattr(settings, "OPERATIONS_AGENT_ENABLED", False)):
        action.status = OperationAction.Status.REJECTED
        action.error_code = "agent_not_configured"
        action.result_summary = "يلزم تفعيل وكيل العمليات الآمن على الخادم قبل تنفيذ هذا الإجراء."
        action.finished_at = timezone.now()
        action.save(update_fields=("status", "error_code", "result_summary", "finished_at"))
    else:
        action.status = OperationAction.Status.QUEUED
        action.result_summary = "تم إرسال الإجراء إلى وكيل العمليات."
        action.save(update_fields=("status", "result_summary"))
    return Response(OperationActionSerializer(action).data, status=201)


@api_view(["POST", "DELETE"])
@authentication_classes([OperationsTokenAuthentication])
def device_registration(request):
    device_id = str(request.data.get("device_id") or "").strip()
    if not device_id:
        return Response({"detail": "معرف الجهاز مطلوب."}, status=400)
    if request.method == "DELETE":
        MobileDevice.objects.filter(user=request.user, device_id=device_id).update(is_active=False, fcm_token="")
        return Response(status=204)
    device, _ = MobileDevice.objects.update_or_create(
        user=request.user,
        device_id=device_id[:160],
        defaults={
            "name": str(request.data.get("name") or "")[:120],
            "platform": str(request.data.get("platform") or "android")[:24],
            "fcm_token": str(request.data.get("fcm_token") or ""),
            "alerts_enabled": bool(request.data.get("alerts_enabled", True)),
            "is_active": True,
            "last_seen_at": timezone.now(),
        },
    )
    return Response({"id": device.pk, "alerts_enabled": device.alerts_enabled})


@api_view(["POST"])
@authentication_classes([OperationsTokenAuthentication])
def acknowledge_incident(request, incident_id: int):
    if not _has_capability(request.user, "acknowledge_incidents"):
        return Response({"detail": "لا تملك صلاحية التعامل مع التنبيهات."}, status=status.HTTP_403_FORBIDDEN)
    incident = Incident.objects.filter(pk=incident_id, status=Incident.Status.OPEN).first()
    if incident is None:
        return Response({"detail": "التنبيه غير موجود أو تمت معالجته."}, status=404)
    incident.status = Incident.Status.ACKNOWLEDGED
    incident.acknowledged_at = timezone.now()
    incident.acknowledged_by = request.user
    incident.save(update_fields=("status", "acknowledged_at", "acknowledged_by"))
    return Response(IncidentSerializer(incident).data)
