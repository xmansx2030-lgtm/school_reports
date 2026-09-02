from __future__ import annotations

import json
import logging
import socket
import time
from decimal import InvalidOperation
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import HealthCheck, Incident, ManagedProject, ManagedServer, ManagedService, ServerMetricSnapshot

logger = logging.getLogger(__name__)


def _float_or_none(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError, InvalidOperation):
        return None


def _setting_int(name: str, default: int, *, minimum: int = 1, maximum: int | None = None) -> int:
    try:
        value = int(getattr(settings, name, default) or default)
    except (TypeError, ValueError):
        value = default
    value = max(minimum, value)
    return min(value, maximum) if maximum is not None else value


def _all_at_or_above(snapshots: list[ServerMetricSnapshot], field: str, threshold: int) -> bool:
    values = [_float_or_none(getattr(snapshot, field, None)) for snapshot in snapshots]
    return bool(values) and all(value is not None and value >= threshold for value in values)


def _queue_names_over_threshold(snapshots: list[ServerMetricSnapshot], threshold: int) -> list[str]:
    if not snapshots:
        return []
    queue_names = set()
    for snapshot in snapshots:
        queue_names.update((snapshot.queue_lengths or {}).keys())
    sustained = []
    for queue_name in sorted(queue_names):
        lengths = []
        for snapshot in snapshots:
            try:
                lengths.append(int((snapshot.queue_lengths or {}).get(queue_name) or 0))
            except (TypeError, ValueError):
                lengths.append(0)
        if lengths and all(length >= threshold for length in lengths):
            sustained.append(queue_name)
    return sustained


def _capacity_pressure_messages(server: ManagedServer) -> tuple[list[str], str]:
    sample_count = _setting_int("OPERATIONS_CAPACITY_SUSTAINED_SAMPLES", 3, minimum=2, maximum=12)
    snapshots = list(server.metric_snapshots.order_by("-captured_at")[:sample_count])
    if len(snapshots) < sample_count:
        return [], f"لا يكفي سجل القياسات بعد. المطلوب {sample_count} قياسات متتالية قبل التنبيه."

    cpu_threshold = _setting_int("CPU_ALERT_PERCENT", 85, minimum=1, maximum=100)
    memory_threshold = _setting_int("MEMORY_ALERT_PERCENT", 85, minimum=1, maximum=100)
    disk_threshold = _setting_int("DISK_ALERT_PERCENT", 80, minimum=1, maximum=100)
    redis_threshold = _setting_int("REDIS_MEMORY_ALERT_PERCENT", 80, minimum=1, maximum=100)
    queue_threshold = _setting_int("CELERY_QUEUE_ALERT_LENGTH", 200, minimum=1)
    window_minutes = sample_count * 5

    messages: list[str] = []
    latest = snapshots[0]
    latest_queues = latest.queue_lengths or {}

    if _all_at_or_above(snapshots, "cpu_percent", cpu_threshold):
        messages.append(
            f"CPU مرتفع بشكل مستمر: آخر قراءة {latest.cpu_percent}%، والحد {cpu_threshold}% "
            f"لمدة تقارب {window_minutes} دقيقة. الإجراء المناسب: راجع المهام الثقيلة والـ workers أولًا، "
            "ثم ارفع الخادم إلى خطة CPU أعلى إذا تكرر الضغط أثناء الاستخدام الطبيعي."
        )
    if _all_at_or_above(snapshots, "memory_percent", memory_threshold):
        messages.append(
            f"الذاكرة مرتفعة بشكل مستمر: آخر قراءة {latest.memory_percent}%، والحد {memory_threshold}% "
            f"لمدة تقارب {window_minutes} دقيقة. الإجراء المناسب: افحص أكثر الحاويات استهلاكًا للذاكرة، "
            "ثم ارفع RAM أو افصل قاعدة البيانات/العمال إلى خادم مستقل إذا كان النمو مستمرًا."
        )
    if _all_at_or_above(snapshots, "disk_percent", disk_threshold):
        messages.append(
            f"القرص ممتلئ بشكل متزايد: آخر قراءة {latest.disk_percent}%، والحد {disk_threshold}% "
            f"لمدة تقارب {window_minutes} دقيقة. الإجراء المناسب: نظف صور Docker القديمة والسجلات، "
            "ثم وسع القرص أو انقل الملفات الكبيرة إلى R2 إذا استمر النمو."
        )
    if _all_at_or_above(snapshots, "redis_memory_percent", redis_threshold):
        messages.append(
            f"Redis قريب من حد الذاكرة: آخر قراءة {latest.redis_memory_percent}%، والحد {redis_threshold}% "
            f"لمدة تقارب {window_minutes} دقيقة. الإجراء المناسب: قلل حجم الكاش والمهام المتراكمة، "
            "ثم افصل Redis أو ارفع حد ذاكرته إذا كانت الزيادة طبيعية."
        )

    sustained_queues = _queue_names_over_threshold(snapshots, queue_threshold)
    if sustained_queues:
        details = ", ".join(
            f"{queue}={latest_queues.get(queue, 0)}" for queue in sustained_queues
        )
        messages.append(
            f"طوابير Celery متراكمة بشكل مستمر: {details}، والحد {queue_threshold} مهمة "
            f"لمدة تقارب {window_minutes} دقيقة. الإجراء المناسب: شغّل worker إضافي للطابور المتأخر، "
            "وافحص سبب بطء المهام قبل زيادة الزيارات أو تشغيل أعمال دفعية جديدة."
        )
    return messages, f"تم تقييم آخر {sample_count} قياسات متتالية."


def _safe_error_code(exc: Exception) -> str:
    if isinstance(exc, HTTPError):
        return f"http_{exc.code}"
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(exc, URLError):
        return "connection_error"
    return "probe_error"


def _component_status(payload: dict, service: ManagedService) -> bool | None:
    containers = [payload]
    for key in ("services", "checks", "components", "dependencies"):
        value = payload.get(key)
        if isinstance(value, dict):
            containers.insert(0, value)
    aliases = {service.service_key.lower(), service.kind.lower()}
    if service.kind == ManagedService.Kind.DATABASE:
        aliases.update(("database", "db", "postgres", "postgresql"))
    elif service.kind == ManagedService.Kind.CACHE:
        aliases.update(("cache", "redis"))
    elif service.kind == ManagedService.Kind.WEB:
        aliases.update(("web", "app", "application"))
    for container in containers:
        for key, value in container.items():
            if str(key).lower() not in aliases:
                continue
            if isinstance(value, bool):
                return value
            if isinstance(value, dict):
                value = value.get("ok", value.get("healthy", value.get("status")))
            normalized = str(value).strip().lower()
            if normalized in {"true", "ok", "up", "healthy", "ready", "connected", "pass", "passed"}:
                return True
            if normalized in {"false", "down", "unhealthy", "failed", "error", "disconnected"}:
                return False
    return None


def _update_service_health(project: ManagedProject, payload: dict, *, project_ok: bool, checked_at) -> None:
    for service in project.services.filter(is_active=True):
        result = _component_status(payload, service)
        if result is None and service.kind == ManagedService.Kind.WEB:
            result = project_ok
        if result is None:
            continue
        ManagedService.objects.filter(pk=service.pk).update(
            status=ManagedProject.Status.HEALTHY if result else ManagedProject.Status.DOWN,
            last_checked_at=checked_at,
        )


def probe_project(project: ManagedProject) -> HealthCheck:
    started = time.monotonic()
    status_code = None
    summary = ""
    error_code = ""
    ok = False
    health_payload: dict = {}
    timeout = float(getattr(settings, "OPERATIONS_PROBE_TIMEOUT_SECONDS", 8) or 8)
    try:
        scheme = urlsplit(project.health_url).scheme.lower()
        if scheme not in ({"http", "https"} if settings.DEBUG else {"https"}):
            raise ValueError("unsupported_health_url_scheme")
        request = Request(  # noqa: S310 - scheme allowlisted above
            project.health_url,
            headers={"Accept": "application/json", "User-Agent": "TawtheeqOperations/1.0"},
            method="GET",
        )
        with urlopen(request, timeout=max(1.0, min(timeout, 20.0))) as response:  # noqa: S310 - scheme allowlisted above
            status_code = int(response.status)
            raw = response.read(2048).decode("utf-8", errors="replace")
            summary = " ".join(raw.split())[:300]
            try:
                decoded = json.loads(raw)
                if isinstance(decoded, dict):
                    health_payload = decoded
            except (TypeError, ValueError, json.JSONDecodeError):
                health_payload = {}
            ok = status_code == project.expected_status
    except Exception as exc:
        error_code = _safe_error_code(exc)
        summary = type(exc).__name__
        if isinstance(exc, HTTPError):
            status_code = exc.code
        logger.info("Operations health probe failed project=%s code=%s", project.slug, error_code)

    latency_ms = max(0, int((time.monotonic() - started) * 1000))
    now = timezone.now()
    with transaction.atomic():
        check = HealthCheck.objects.create(
            project=project,
            ok=ok,
            status_code=status_code,
            latency_ms=latency_ms,
            error_code=error_code,
            response_summary=summary,
            checked_at=now,
        )
        locked = ManagedProject.objects.select_for_update().get(pk=project.pk)
        failures = 0 if ok else locked.consecutive_failures + 1
        status = ManagedProject.Status.HEALTHY if ok else (
            ManagedProject.Status.DOWN if failures >= 2 else ManagedProject.Status.DEGRADED
        )
        ManagedProject.objects.filter(pk=project.pk).update(
            status=status,
            last_latency_ms=latency_ms,
            last_checked_at=now,
            consecutive_failures=failures,
        )
        _update_service_health(project, health_payload, project_ok=ok, checked_at=now)

        key = f"project:{project.pk}:health"
        open_incident = Incident.objects.filter(dedupe_key=key, status__in=(Incident.Status.OPEN, Incident.Status.ACKNOWLEDGED)).first()
        new_incident = None
        if not ok and failures >= 2 and open_incident is None:
            new_incident = Incident.objects.create(
                project=project,
                server=project.server,
                dedupe_key=key,
                title=f"تعذر الوصول إلى {project.name}",
                message=f"فشل فحص الصحة مرتين متتاليتين. الرمز: {error_code or status_code or 'unknown'}.",
                severity=Incident.Severity.CRITICAL,
            )
        elif ok and open_incident is not None:
            open_incident.status = Incident.Status.RESOLVED
            open_incident.resolved_at = now
            open_incident.save(update_fields=("status", "resolved_at"))
            new_incident = open_incident
    if new_incident is not None and project.alerts_enabled:
        from .tasks import send_incident_push_task

        send_incident_push_task.delay(new_incident.pk)
    return check


def probe_all_projects() -> list[HealthCheck]:
    projects = ManagedProject.objects.filter(
        is_active=True,
    ).exclude(
        base_url="",
    ).select_related("server")
    return [probe_project(project) for project in projects]


def capture_server_metrics(server: ManagedServer, report: dict) -> ServerMetricSnapshot:
    now = timezone.now()
    snapshot = ServerMetricSnapshot.objects.create(
        server=server,
        cpu_percent=report.get("cpu_percent"),
        memory_percent=report.get("memory_percent"),
        disk_percent=report.get("disk_percent"),
        redis_memory_percent=report.get("redis_used_percent"),
        queue_lengths=report.get("queue_lengths") or {},
        captured_at=now,
    )
    values = [report.get("cpu_percent"), report.get("memory_percent"), report.get("disk_percent")]
    thresholds = [
        int(getattr(settings, "CPU_ALERT_PERCENT", 85)),
        int(getattr(settings, "MEMORY_ALERT_PERCENT", 85)),
        int(getattr(settings, "DISK_ALERT_PERCENT", 80)),
    ]
    known = [(float(value), threshold) for value, threshold in zip(values, thresholds, strict=True) if value is not None]
    status = ManagedServer.Status.DEGRADED if any(value >= threshold for value, threshold in known) else ManagedServer.Status.HEALTHY
    ManagedServer.objects.filter(pk=server.pk).update(
        status=status,
        cpu_percent=report.get("cpu_percent"),
        memory_percent=report.get("memory_percent"),
        disk_percent=report.get("disk_percent"),
        last_checked_at=now,
    )
    key = f"server:{server.pk}:capacity"
    open_incident = Incident.objects.filter(
        dedupe_key=key,
        status__in=(Incident.Status.OPEN, Incident.Status.ACKNOWLEDGED),
    ).first()
    notify_incident = None
    alerts, evaluation_summary = _capacity_pressure_messages(server)
    severity = Incident.Severity.CRITICAL if any("القرص" in alert for alert in alerts) else Incident.Severity.WARNING
    if alerts and open_incident is None:
        notify_incident = Incident.objects.create(
            server=server,
            dedupe_key=key,
            title=f"ضغط مرتفع على {server.name}",
            message=("\n\n".join(alerts) + f"\n\n{evaluation_summary}")[:2000],
            severity=severity,
        )
    elif alerts and open_incident is not None:
        open_incident.title = f"ضغط مرتفع على {server.name}"
        open_incident.message = ("\n\n".join(alerts) + f"\n\n{evaluation_summary}")[:2000]
        open_incident.severity = severity
        open_incident.save(update_fields=("title", "message", "severity"))
    elif not alerts and open_incident is not None:
        open_incident.status = Incident.Status.RESOLVED
        open_incident.resolved_at = now
        open_incident.save(update_fields=("status", "resolved_at"))
        notify_incident = open_incident
    if notify_incident is not None:
        from .tasks import send_incident_push_task

        send_incident_push_task.delay(notify_incident.pk)
    return snapshot
