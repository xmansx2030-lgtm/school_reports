"""Infrastructure-capacity probes used by periodic task orchestration."""

from __future__ import annotations

import logging
import shutil
import time
from typing import Any

from django.apps import apps
from django.conf import settings
from django.utils import timezone

from core import opmetrics

logger = logging.getLogger(__name__)

CapacityReport = dict[str, Any]


def _threshold(name: str, default: int) -> int:
    """Read a threshold while preserving an intentional configured zero."""
    value = getattr(settings, name, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _probe_redis_memory(report: CapacityReport) -> None:
    try:
        from django_redis import get_redis_connection

        info = get_redis_connection("default").info(section="memory")
        used = int(info.get("used_memory") or 0)
        limit = int(info.get("maxmemory") or 0)
        if limit <= 0 or used <= 0:
            return
        percent = round((used / limit) * 100, 1)
        report["redis_used_percent"] = percent
        if percent < _threshold("REDIS_MEMORY_ALERT_PERCENT", 80):
            return
        message = (
            f"Redis memory at {percent}% of its limit "
            f"({round(used / (1024 * 1024), 1)} MB of {round(limit / (1024 * 1024), 1)} MB)."
        )
        report["alerts"].append(message)
        logger.error("Infrastructure capacity warning: %s", message)
        opmetrics.increment("infra.redis.memory_high")
    except Exception:
        # Redis is an optional probe in local/development environments. The
        # remaining probes still provide a useful report.
        logger.debug("Redis memory probe unavailable", exc_info=True)


def _probe_expired_sessions(report: CapacityReport) -> None:
    try:
        session_model = apps.get_model("sessions", "Session")
        expired = session_model.objects.filter(expire_date__lt=timezone.now()).count()
        report["expired_sessions"] = expired
        if expired <= _threshold("EXPIRED_SESSION_ALERT_THRESHOLD", 100_000):
            return
        message = f"{expired} expired session rows are still pending cleanup."
        report["alerts"].append(message)
        logger.error("Infrastructure capacity warning: %s", message)
        opmetrics.increment("infra.sessions.backlog_high")
    except Exception:
        logger.debug("Session backlog probe failed", exc_info=True)


def _cpu_sample() -> tuple[int, int]:
    with open("/proc/stat", encoding="ascii") as proc_stat:
        fields = [int(value) for value in proc_stat.readline().split()[1:]]
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
    return sum(fields), idle


def _probe_cpu_and_memory(report: CapacityReport) -> None:
    try:
        total_before, idle_before = _cpu_sample()
        time.sleep(0.15)
        total_after, idle_after = _cpu_sample()
        total_delta = max(1, total_after - total_before)
        cpu_percent = round(
            max(0.0, min(100.0, (1 - ((idle_after - idle_before) / total_delta)) * 100)),
            1,
        )

        meminfo: dict[str, int] = {}
        with open("/proc/meminfo", encoding="ascii") as proc_mem:
            for line in proc_mem:
                key, raw = line.split(":", 1)
                meminfo[key] = int(raw.strip().split()[0])
        total_memory = int(meminfo.get("MemTotal") or 0)
        available_memory = int(meminfo.get("MemAvailable") or 0)
        memory_percent = (
            round(((total_memory - available_memory) / total_memory) * 100, 1)
            if total_memory
            else 0.0
        )
        report["cpu_percent"] = cpu_percent
        report["memory_percent"] = memory_percent
        if cpu_percent >= _threshold("CPU_ALERT_PERCENT", 85):
            report["alerts"].append(f"CPU usage at {cpu_percent}%.")
            opmetrics.increment("infra.cpu.high")
        if memory_percent >= _threshold("MEMORY_ALERT_PERCENT", 85):
            report["alerts"].append(f"Memory usage at {memory_percent}%.")
            opmetrics.increment("infra.memory.high")
    except Exception:
        # /proc is Linux-specific; Windows development still runs all other
        # probes without treating the absent host interface as task failure.
        logger.debug("CPU/memory capacity probe failed", exc_info=True)


def _probe_disk(report: CapacityReport) -> None:
    try:
        disk = shutil.disk_usage("/")
        disk_percent = round((disk.used / disk.total) * 100, 1) if disk.total else 0.0
        report["disk_percent"] = disk_percent
        if disk_percent < _threshold("DISK_ALERT_PERCENT", 80):
            return
        report["alerts"].append(
            f"Disk usage at {disk_percent}% ({round(disk.free / (1024**3), 1)} GB free)."
        )
        opmetrics.increment("infra.disk.high")
    except Exception:
        logger.debug("Disk capacity probe failed", exc_info=True)


def _probe_celery_queues(report: CapacityReport) -> None:
    try:
        import redis as redis_client

        broker_url = str(getattr(settings, "CELERY_BROKER_URL", "") or "")
        broker = redis_client.from_url(
            broker_url,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        queue_limit = _threshold("CELERY_QUEUE_ALERT_LENGTH", 200)
        for queue_name in ("default", "notifications", "images", "periodic"):
            length = int(broker.llen(queue_name) or 0)
            report["queue_lengths"][queue_name] = length
            if length < queue_limit:
                continue
            report["alerts"].append(
                f"Celery queue '{queue_name}' contains {length} pending tasks."
            )
            opmetrics.increment(f"infra.queue.high.{queue_name}")
    except Exception:
        logger.debug("Celery queue probe failed", exc_info=True)


def _probe_http_metrics(report: CapacityReport) -> None:
    try:
        metric_snapshot = opmetrics.snapshot()
        request_count = int(metric_snapshot.get("http.requests.total") or 0)
        error_count = int(metric_snapshot.get("http.responses.5xx") or 0)
        timing_count = int(metric_snapshot.get("http.response.duration.count") or 0)
        timing_sum = int(metric_snapshot.get("http.response.duration.sum_ms") or 0)
        min_samples = _threshold("HTTP_ALERT_MIN_SAMPLES", 20)
        if request_count:
            error_percent = round(error_count * 100 / request_count, 2)
            report["http_5xx_percent"] = error_percent
            error_limit = float(getattr(settings, "HTTP_5XX_ALERT_PERCENT", 2.0) or 2.0)
            if request_count >= min_samples and error_percent >= error_limit:
                report["alerts"].append(
                    f"HTTP 5xx rate at {error_percent}% ({error_count}/{request_count})."
                )
                opmetrics.increment("infra.http.5xx_high")
        if timing_count:
            average_ms = round(timing_sum / timing_count, 1)
            report["http_average_ms"] = average_ms
            if timing_count >= min_samples and average_ms >= _threshold(
                "HTTP_LATENCY_ALERT_MS",
                2000,
            ):
                report["alerts"].append(
                    f"Average HTTP response time at {average_ms} ms ({timing_count} samples)."
                )
                opmetrics.increment("infra.http.latency_high")
    except Exception:
        logger.debug("HTTP operational metrics probe failed", exc_info=True)


def collect_infrastructure_capacity_report() -> CapacityReport:
    """Collect independent best-effort probes into one serializable report."""
    report: CapacityReport = {
        "redis_used_percent": None,
        "expired_sessions": None,
        "cpu_percent": None,
        "memory_percent": None,
        "disk_percent": None,
        "queue_lengths": {},
        "http_5xx_percent": None,
        "http_average_ms": None,
        "alerts": [],
    }
    _probe_redis_memory(report)
    _probe_expired_sessions(report)
    _probe_cpu_and_memory(report)
    _probe_disk(report)
    _probe_celery_queues(report)
    _probe_http_metrics(report)
    return report


__all__ = ("CapacityReport", "collect_infrastructure_capacity_report")
