from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from .inventory import PROJECTS, SERVER_DEFAULTS, canonical_project, project_defaults
from .models import ManagedProject, ManagedServer, ManagedService, ProjectMetricSnapshot, ServerMetricSnapshot
from .services import capture_server_metrics


_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_STATUS_PRIORITY = {
    ManagedProject.Status.UNKNOWN: 0,
    ManagedProject.Status.HEALTHY: 1,
    ManagedProject.Status.MAINTENANCE: 2,
    ManagedProject.Status.DEGRADED: 3,
    ManagedProject.Status.DOWN: 4,
}


def _decimal(value, *, maximum: Decimal | None = None) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        parsed = Decimal(str(value)).quantize(Decimal("0.1"))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed.is_finite():
        return None
    parsed = max(Decimal("0"), parsed)
    return min(parsed, maximum) if maximum is not None else parsed


def _is_completed_job(container: dict) -> bool:
    """A one-shot job (e.g. ``migrate``) that finished successfully.

    Compose init jobs run once and exit 0 with ``restart: "no"``; the ``web``
    service even waits for them via ``service_completed_successfully``. Such a
    stopped container is the expected, healthy end state — not a crash — so it
    must not drag the project into "degraded".
    """
    state = str(container.get("state") or "").lower()
    if state != "exited":
        return False
    restart_policy = str(container.get("restart_policy") or "").strip().lower()
    if restart_policy not in {"", "no"}:
        return False
    exit_code = container.get("exit_code")
    try:
        return int(exit_code) == 0
    except (TypeError, ValueError):
        return False


def _status_for_container(container: dict) -> str:
    if _is_completed_job(container):
        return ManagedProject.Status.HEALTHY
    state = str(container.get("state") or "unknown").lower()
    health = str(container.get("health") or "").lower()
    if state == "running" and health != "unhealthy":
        return ManagedProject.Status.HEALTHY
    if state in {"created", "restarting", "paused"}:
        return ManagedProject.Status.DEGRADED
    if state in {"exited", "dead", "removing"} or health == "unhealthy":
        return ManagedProject.Status.DOWN
    return ManagedProject.Status.UNKNOWN


def _service_kind(service_key: str) -> str:
    key = service_key.lower()
    if any(token in key for token in ("postgres", "mysql", "mariadb", "database", "db")):
        return ManagedService.Kind.DATABASE
    if any(token in key for token in ("redis", "cache")):
        return ManagedService.Kind.CACHE
    if any(token in key for token in ("worker", "celery", "queue", "beat")):
        return ManagedService.Kind.WORKER
    if any(token in key for token in ("caddy", "nginx", "traefik", "proxy")):
        return ManagedService.Kind.PROXY
    if any(token in key for token in ("web", "api", "app", "frontend", "backend")):
        return ManagedService.Kind.WEB
    return ManagedService.Kind.OTHER


def _safe_slug(value: str) -> str:
    result = slugify(value.replace("_", "-"))[:80]
    if result:
        return result
    checksum = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"docker-project-{checksum}"


def _repository(value) -> str:
    repository = str(value or "").strip()[:160]
    return repository if _REPOSITORY_RE.fullmatch(repository) else ""


def _release_sha(value) -> str:
    sha = str(value or "").strip().lower()
    return sha if re.fullmatch(r"[0-9a-f]{40}", sha) else ""


def _deployed_image(value) -> str:
    image = str(value or "").strip()[:300]
    if not image or any(character.isspace() or ord(character) < 32 for character in image):
        return ""
    return image


def _sum(containers: list[dict], key: str, *, maximum: Decimal | None = None) -> Decimal | None:
    values = [_decimal(container.get(key)) for container in containers]
    known = [value for value in values if value is not None]
    if not known:
        return None
    total = sum(known, Decimal("0"))
    return min(total, maximum) if maximum is not None else total


def _metric_number(value):
    parsed = _decimal(value)
    return float(parsed) if parsed is not None else None


def _host_details(payload) -> dict:
    if not isinstance(payload, dict):
        return {}
    details = {key: _metric_number(payload.get(key)) for key in (
        "cpu_count", "load1", "load5", "load15", "memory_total_mb", "memory_available_mb",
        "swap_used_mb", "swap_total_mb", "disk_used_gb", "disk_total_gb", "disk_free_gb",
        "disk_inode_percent", "iowait_percent", "uptime_seconds",
    )}
    pressure = payload.get("pressure") if isinstance(payload.get("pressure"), dict) else {}
    details["pressure"] = {
        key: {metric: _metric_number(values.get(metric)) for metric in ("avg10", "avg60", "avg300")}
        for key, values in pressure.items()
        if key in {"cpu_some", "memory_some", "memory_full", "io_some", "io_full"} and isinstance(values, dict)
    }
    details["cpu_measurement"] = str(payload.get("cpu_measurement") or "sample")[:40]
    return details


def _runtime_status(containers: list[dict]) -> str:
    if not containers:
        return ManagedProject.Status.UNKNOWN
    statuses = [_status_for_container(container) for container in containers]
    worst = max(statuses, key=lambda value: _STATUS_PRIORITY[value])
    if worst == ManagedProject.Status.DOWN and any(
        status == ManagedProject.Status.HEALTHY for status in statuses
    ):
        return ManagedProject.Status.DEGRADED
    return worst


def _sync_services(project: ManagedProject, containers: list[dict], *, captured_at) -> None:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for container in containers:
        service_key = _safe_slug(str(container.get("service") or container.get("name") or "service"))[:100]
        grouped[service_key].append(container)

    project.services.update(is_active=False)
    for service_key, service_containers in grouped.items():
        status = max(
            (_status_for_container(container) for container in service_containers),
            key=lambda value: _STATUS_PRIORITY[value],
        )
        raw_name = str(service_containers[0].get("service") or service_key).strip()
        ManagedService.objects.update_or_create(
            project=project,
            service_key=service_key,
            defaults={
                "name": raw_name[:120] or service_key,
                "kind": _service_kind(service_key),
                "status": status,
                "last_checked_at": captured_at,
                "restart_allowed": (
                    canonical_project(project.compose_project) is not None
                    and _service_kind(service_key) in {ManagedService.Kind.WEB, ManagedService.Kind.WORKER}
                    and "beat" not in service_key
                    and "migrate" not in service_key
                ),
                "is_active": True,
            },
        )


@transaction.atomic
def sync_inventory_report(report: dict) -> dict[str, int]:
    """Synchronize Docker inventory and per-project usage from a host report."""
    if not isinstance(report, dict) or not isinstance(report.get("projects"), list):
        raise ValueError("The operations inventory report must contain a projects list.")

    server_payload = report.get("server") if isinstance(report.get("server"), dict) else {}
    server, _ = ManagedServer.objects.update_or_create(
        slug=str(server_payload.get("slug") or SERVER_DEFAULTS["slug"])[:80],
        defaults={
            "name": str(server_payload.get("name") or SERVER_DEFAULTS["name"])[:120],
            "provider": str(server_payload.get("provider") or SERVER_DEFAULTS["provider"])[:40],
            "provider_server_id": str(
                server_payload.get("provider_server_id") or SERVER_DEFAULTS["provider_server_id"]
            )[:80],
            "public_ip": server_payload.get("public_ip") or SERVER_DEFAULTS["public_ip"],
            "server_type": str(server_payload.get("server_type") or SERVER_DEFAULTS["server_type"])[:40],
            "is_active": True,
        },
    )
    captured_at = timezone.now()
    capture_server_metrics(
        server,
        {
            "source": ServerMetricSnapshot.Source.HOST,
            "sample_duration_seconds": server_payload.get("sample_duration_seconds"),
            "host_details": _host_details(server_payload.get("host_details")),
            "cpu_percent": _decimal(server_payload.get("cpu_percent"), maximum=Decimal("100")),
            "memory_percent": _decimal(server_payload.get("memory_percent"), maximum=Decimal("100")),
            "disk_percent": _decimal(server_payload.get("disk_percent"), maximum=Decimal("100")),
            "redis_used_percent": _decimal(server_payload.get("redis_used_percent"), maximum=Decimal("100")),
            "queue_lengths": server_payload.get("queue_lengths") or {},
        },
    )

    created = 0
    updated = 0
    for order, payload in enumerate(report["projects"], start=1):
        if not isinstance(payload, dict):
            continue
        compose_project = str(payload.get("compose_project") or payload.get("name") or "").strip()[:120]
        if not compose_project:
            continue
        known = canonical_project(compose_project)
        slug = known["slug"] if known else _safe_slug(compose_project)
        if known:
            defaults = project_defaults(known, server=server, sort_order=PROJECTS.index(known) + 1)
            defaults["compose_project"] = compose_project
            discovered_repository = _repository(payload.get("repository"))
            if discovered_repository and not defaults["repository"]:
                defaults["repository"] = discovered_repository
        else:
            defaults = {
                "server": server,
                "name": str(payload.get("name") or compose_project)[:120],
                "base_url": "",
                "health_path": "",
                "compose_project": compose_project,
                "repository": _repository(payload.get("repository")),
                "deploy_branch": "main",
                "sort_order": order,
                "is_active": True,
            }
        project, was_created = ManagedProject.objects.update_or_create(slug=slug, defaults=defaults)
        created += int(was_created)
        updated += int(not was_created)

        containers = [item for item in (payload.get("containers") or []) if isinstance(item, dict)]
        runtime_status = _runtime_status(containers)
        project.runtime_status = runtime_status
        project.last_runtime_checked_at = captured_at
        update_fields = ["runtime_status", "last_runtime_checked_at", "updated_at"]
        deployed_sha = _release_sha(payload.get("deployed_sha"))
        if deployed_sha:
            project.deployed_sha = deployed_sha
            update_fields.append("deployed_sha")
        deployed_image = _deployed_image(payload.get("deployed_image"))
        if deployed_image:
            project.deployed_image = deployed_image
            update_fields.append("deployed_image")
        project.save(update_fields=update_fields)
        _sync_services(project, containers, captured_at=captured_at)

        # One-shot jobs that finished successfully are not long-running services,
        # so keep them out of the "X/Y running" ratio — otherwise a healthy stack
        # reads as 8/9 the moment its migrate job exits.
        service_containers = [item for item in containers if not _is_completed_job(item)]
        running_count = sum(
            str(item.get("state") or "").lower() == "running" for item in service_containers
        )
        ProjectMetricSnapshot.objects.create(
            project=project,
            cpu_percent=_sum(containers, "cpu_percent", maximum=Decimal("100")),
            memory_percent=_sum(containers, "memory_host_percent", maximum=Decimal("100")),
            memory_used_mb=_sum(containers, "memory_used_mb"),
            memory_limit_mb=_sum(containers, "memory_limit_mb"),
            network_rx_mb=_sum(containers, "network_rx_mb"),
            network_tx_mb=_sum(containers, "network_tx_mb"),
            block_read_mb=_sum(containers, "block_read_mb"),
            block_write_mb=_sum(containers, "block_write_mb"),
            container_count=len(service_containers),
            running_container_count=running_count,
            container_states=[
                {
                    "name": str(item.get("name") or "")[:120],
                    "service": str(item.get("service") or "")[:120],
                    "state": str(item.get("state") or "unknown")[:24],
                    "health": str(item.get("health") or "")[:24],
                    "completed_job": _is_completed_job(item),
                    "cpu_percent": _metric_number(item.get("cpu_percent")),
                    "memory_used_mb": _metric_number(item.get("memory_used_mb")),
                    "memory_limit_mb": _metric_number(item.get("memory_limit_mb")),
                    "network_rx_mb": _metric_number(item.get("network_rx_mb")),
                    "network_tx_mb": _metric_number(item.get("network_tx_mb")),
                    "block_read_mb": _metric_number(item.get("block_read_mb")),
                    "block_write_mb": _metric_number(item.get("block_write_mb")),
                    "restart_count": _metric_number(item.get("restart_count")),
                    "oom_killed": bool(item.get("oom_killed", False)),
                    "started_at": str(item.get("started_at") or "")[:40],
                    "image": _deployed_image(item.get("image")),
                }
                for item in containers
            ],
            captured_at=captured_at,
        )

    return {"created": created, "updated": updated, "projects": created + updated}
