from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from .deployments import all_deployment_states
from .incidents import observe_incident
from .models import (
    HealthCheck,
    Incident,
    IncidentEvent,
    ManagedProject,
    ManagedServer,
    OperationAction,
    OperationsPaymentLink,
    ProjectMetricSnapshot,
    ServerMetricSnapshot,
)
from .push import send_incident_push, send_payment_paid_push
from .payment_links import reconcile_open_payment_links
from .services import capture_server_metrics, probe_all_projects
from .task_names import SEND_INCIDENT_PUSH_TASK, SEND_PAYMENT_PAID_PUSH_TASK, STORE_CAPACITY_SNAPSHOT_TASK


@shared_task(ignore_result=True)
def run_operations_monitor_task() -> dict[str, int]:
    checks = probe_all_projects()
    return {"checked": len(checks), "failed": sum(1 for check in checks if not check.ok)}


@shared_task(name=STORE_CAPACITY_SNAPSHOT_TASK, ignore_result=True)
def store_capacity_snapshot_task(report: dict) -> None:
    server = ManagedServer.objects.filter(is_active=True).order_by("id").first()
    if server is not None:
        capture_server_metrics(server, report)


@shared_task(ignore_result=True)
def sync_deployed_revisions_task() -> dict[str, int]:
    release_sha = str(getattr(settings, "RELEASE_SHA", "") or "").strip()
    release_image = str(getattr(settings, "RELEASE_IMAGE", "") or "").strip()
    if not release_sha or release_sha == "unknown":
        return {"updated": 0}
    values = {"deployed_sha": release_sha[:64]}
    if release_image:
        values["deployed_image"] = release_image[:300]
    updated = ManagedProject.objects.filter(slug="tawtheeq", is_active=True).update(**values)
    return {"updated": updated}


@shared_task(ignore_result=True)
def reconcile_payment_links_task() -> dict[str, int | bool]:
    if not getattr(settings, "MOYASAR_ENABLED", False):
        return {"enabled": False, "checked": 0, "updated": 0, "failed": 0, "notifications_queued": 0}
    summary = reconcile_open_payment_links(limit=100)
    due_ids = list(
        OperationsPaymentLink.objects.filter(
            status=OperationsPaymentLink.Status.PAID,
            paid_notification_sent_at__isnull=True,
        ).values_list("pk", flat=True)[:100]
    )
    for link_id in due_ids:
        send_payment_paid_push_task.delay(link_id)
    return {"enabled": True, **summary, "notifications_queued": len(due_ids)}


@shared_task(ignore_result=True)
def monitor_deployment_state_task() -> dict[str, object]:
    if not getattr(settings, "OPERATIONS_DEPLOY_MONITOR_ENABLED", True):
        return {"enabled": False}
    states = all_deployment_states()
    projects = ManagedProject.objects.in_bulk(state.project_id for state in states)
    ahead = [state for state in states if state.repository_ahead]
    for state in states:
        if not state.latest_sha or not state.deployed_sha or state.deployed_sha == "unknown":
            continue
        project = projects.get(state.project_id)
        if project is None:
            continue
        event = observe_incident(
            f"deployment:{project.pk}:repository-ahead",
            problem=state.repository_ahead,
            project=project, server=project.server,
            title=f"يوجد إصدار أحدث لـ {project.name}",
            message="راجع بطاقة النشر للتحقق من الإصدار قبل تشغيل النشر.",
            severity=Incident.Severity.INFO,
        )
        if event is not None and project.alerts_enabled:
            send_incident_push_task.delay(event.incident_id, event.pk)
    return {
        "enabled": True,
        "repository_ahead": bool(ahead),
        "ahead_count": len(ahead),
    }


@shared_task(
    name=SEND_INCIDENT_PUSH_TASK,
    ignore_result=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
)
def send_incident_push_task(incident_id: int, event_id: int | None = None) -> dict[str, int]:
    incident = Incident.objects.filter(pk=incident_id, merged_into__isnull=True).first()
    event = None
    if incident is not None and event_id is not None:
        event = IncidentEvent.objects.filter(pk=event_id, incident=incident).first()
        if event is None:
            return {"sent": 0, "failed": 0, "disabled": 0}
    return send_incident_push(incident, event=event) if incident is not None else {"sent": 0, "failed": 0, "disabled": 0}


@shared_task(
    name=SEND_PAYMENT_PAID_PUSH_TASK,
    ignore_result=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
)
def send_payment_paid_push_task(payment_link_id: int) -> dict[str, int]:
    with transaction.atomic():
        link = (
            OperationsPaymentLink.objects.select_for_update()
            .select_related("project")
            .filter(pk=payment_link_id)
            .first()
        )
        if link is None or link.status != OperationsPaymentLink.Status.PAID:
            return {"sent": 0, "failed": 0, "disabled": 0, "skipped": 1}
        if link.paid_notification_sent_at is not None:
            return {"sent": 0, "failed": 0, "disabled": 0, "skipped": 1}

        result = send_payment_paid_push(link)
        if result["failed"] and not result["sent"]:
            raise RuntimeError("FCM rejected every payment-paid notification.")
        link.paid_notification_sent_at = timezone.now()
        link.save(update_fields=("paid_notification_sent_at", "updated_at"))
        return {**result, "skipped": 0}


@shared_task(ignore_result=True)
def cleanup_operations_history_task() -> dict[str, int]:
    retention_days = max(7, int(getattr(settings, "OPERATIONS_HISTORY_RETENTION_DAYS", 30) or 30))
    cutoff = timezone.now() - timedelta(days=retention_days)
    checks, _ = HealthCheck.objects.filter(checked_at__lt=cutoff).delete()
    metrics, _ = ServerMetricSnapshot.objects.filter(captured_at__lt=cutoff).delete()
    project_metrics, _ = ProjectMetricSnapshot.objects.filter(captured_at__lt=cutoff).delete()
    actions, _ = OperationAction.objects.filter(requested_at__lt=cutoff).delete()
    redacted_logs = OperationAction.objects.filter(
        action=OperationAction.Action.READ_LOGS,
        requested_at__lt=timezone.now() - timedelta(hours=24),
    ).exclude(log_content="").update(log_content="", log_analysis={})
    return {
        "health_checks": checks,
        "server_metrics": metrics,
        "project_metrics": project_metrics,
        "actions": actions,
        "redacted_logs": redacted_logs,
    }
