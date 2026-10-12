"""Bounded operational trends, with explicit provenance and missing-data coverage."""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone as datetime_timezone

from django.utils import timezone

from .models import HealthCheck, ManagedProject, ProjectMetricSnapshot, ServerMetricSnapshot

WINDOWS = {"1h": (1, 5), "24h": (24, 10), "48h": (48, 15), "7d": (168, 60)}
MAX_SAMPLES = 21000
MAX_CHECKS = 51000
SAMPLE_INTERVAL_SECONDS = 300
STALE_SECONDS = 900
METRIC_FIELDS = (
    "cpu_percent", "memory_percent", "disk_percent", "memory_used_mb", "memory_limit_mb",
    "network_rx_mb", "network_tx_mb", "block_read_mb", "block_write_mb",
)
HOST_SERIES_FIELDS = ("load1", "swap_used_mb", "iowait_percent", "disk_free_gb", "memory_available_mb")


def _number(value):
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _average(values):
    known = [value for value in (_number(item) for item in values) if value is not None]
    return round(sum(known) / len(known), 2) if known else None


def _maximum(values):
    known = [value for value in (_number(item) for item in values) if value is not None]
    return round(max(known), 2) if known else None


def _cpu_average(rows):
    weighted = [(float(row["cpu_percent"]), row["sample_duration_seconds"]) for row in rows
                if row.get("cpu_percent") is not None and row.get("sample_duration_seconds")]
    if weighted:
        return round(sum(value * duration for value, duration in weighted) /
                     sum(duration for _, duration in weighted), 2)
    return _average(row.get("cpu_percent") for row in rows)


def window_bounds(window: str, *, now=None):
    if window not in WINDOWS:
        raise ValueError("اختر فترة 1h أو 24h أو 48h أو 7d.")
    end = now or timezone.now()
    hours, bucket_minutes = WINDOWS[window]
    return end - timedelta(hours=hours), end, bucket_minutes


def _health_summary(checks):
    total = len(checks)
    failed = sum(not row["ok"] for row in checks)
    latencies = sorted(row["latency_ms"] for row in checks if row["latency_ms"] is not None)
    return {
        "health_checks": total,
        "failed_checks": failed,
        "availability_percent": round(100 * (total - failed) / total, 2) if total else None,
        "latency_avg_ms": _average(latencies),
        "latency_p95_ms": latencies[max(0, math.ceil(len(latencies) * 0.95) - 1)] if latencies else None,
        "latest_health_check_at": checks[-1]["checked_at"] if checks else None,
        "health_history_truncated": total >= MAX_CHECKS,
    }


def _checks(queryset, start, end):
    # Fixed seven-day windows + a hard cap prevent an accidentally high-frequency
    # monitor from making a mobile request allocate unbounded memory.
    return list(reversed(list(queryset.filter(checked_at__gte=start, checked_at__lte=end)
                              .order_by("-checked_at", "-id")
                              .values("ok", "latency_ms", "checked_at")[:MAX_CHECKS])))


def _coverage(rows, start, end):
    expected = max(1, math.ceil((end - start).total_seconds() / SAMPLE_INTERVAL_SECONDS))
    last = rows[-1]["captured_at"] if rows else None
    durations = [row.get("sample_duration_seconds") for row in rows]
    # Count occupied five-minute slots, so duplicate observations never claim
    # complete coverage of an otherwise empty monitoring window.
    slots = {int((row["captured_at"] - start).total_seconds() // SAMPLE_INTERVAL_SECONDS) for row in rows}
    return {
        "sample_count": len(rows),
        "expected_samples": expected,
        "coverage_percent": round(min(100, len(slots) * 100 / expected), 1),
        "first_captured_at": rows[0]["captured_at"] if rows else None,
        "last_captured_at": last,
        "stale": last is None or (end - last).total_seconds() > STALE_SECONDS,
        "sample_duration_seconds": _average(durations),
        "expected_interval_seconds": SAMPLE_INTERVAL_SECONDS,
        "history_truncated": len(rows) >= MAX_SAMPLES,
    }


def _summary(rows, checks):
    result = {
        "cpu_avg": _cpu_average(rows),
        "cpu_max": _maximum(row.get("cpu_percent") for row in rows),
        "memory_avg": _average(row.get("memory_percent") for row in rows),
        "memory_max": _maximum(row.get("memory_percent") for row in rows),
        "memory_used_avg_mb": _average(row.get("memory_used_mb") for row in rows),
        "memory_used_max_mb": _maximum(row.get("memory_used_mb") for row in rows),
        "disk_latest": _number(rows[-1].get("disk_percent")) if rows else None,
    }
    # Host CPU observations measure unequal intervals (e.g. cron delay); weight
    # them by their measured duration rather than treating a short sample as 5m.
    result.update(_health_summary(checks))
    return result


def _series(rows, bucket_minutes):
    grouped = defaultdict(list)
    bucket_seconds = bucket_minutes * 60
    for row in rows:
        timestamp = int(row["captured_at"].timestamp())
        grouped[timestamp - timestamp % bucket_seconds].append(row)
    result = []
    for timestamp, samples in sorted(grouped.items()):
        point = {"captured_at": datetime.fromtimestamp(timestamp, tz=datetime_timezone.utc), "sample_count": len(samples)}
        point.update({field: _average(row.get(field) for row in samples) for field in METRIC_FIELDS})
        point["cpu_percent"] = _cpu_average(samples)
        point.update({field: _average((row.get("host_details") or {}).get(field) for row in samples)
                      for field in HOST_SERIES_FIELDS})
        result.append(point)
    return result


def _payload(rows, checks, *, window, start, end, bucket_minutes, source, include_details=True):
    labels = {
        "host": "قياسات المضيف؛ CPU متوسط الفترة المسجلة",
        "legacy_sampled": "عينات تاريخية سابقة مختلطة المصادر؛ لا تثبت ضغطًا مستمرًا",
        "docker": "عينات Docker؛ CPU كنسبة من إجمالي أنوية الخادم",
        "unavailable": "لا توجد قياسات متاحة للفترة",
    }
    latest = dict(rows[-1]) if rows else None
    if latest:
        for field in METRIC_FIELDS:
            if field in latest:
                latest[field] = _number(latest[field])
    return {
        "window": window, "start": start, "end": end, "bucket_minutes": bucket_minutes,
        "source": source, "source_label": labels[source],
        "coverage": _coverage(rows, start, end), "summary": _summary(rows, checks),
        "series": _series(rows, bucket_minutes) if include_details else [], "latest": latest,
        "notes": [
            "نسبة التوفر محسوبة من الفحوص المسجلة وليست مدة انقطاع مؤكدة.",
            "الشبكة وقراءة/كتابة القرص عدادات تراكمية منذ بدء الحاويات، وليست معدل نقل.",
            "الفجوات تعني غياب القياس؛ لا تعني استهلاكًا صفريًا.",
        ],
    }


def project_analytics(project, window="24h", *, now=None, include_details=True):
    start, end, bucket_minutes = window_bounds(window, now=now)
    fields = [field for field in METRIC_FIELDS if field != "disk_percent"]
    rows = list(reversed(list(ProjectMetricSnapshot.objects.filter(
        project=project, captured_at__gte=start, captured_at__lte=end,
    ).order_by("-captured_at", "-id").values(
        *fields, "id", "captured_at", "container_count", "running_container_count",
    )[:MAX_SAMPLES])))
    checks = _checks(project.health_checks, start, end)
    payload = _payload(rows, checks, window=window, start=start, end=end,
                       bucket_minutes=bucket_minutes, source="docker" if rows else "unavailable",
                       include_details=include_details)
    if payload["latest"]:
        # Full container JSON is useful only for the latest snapshot. Fetching
        # it for thousands of historic rows made a 7d server comparison expensive.
        latest_id = payload["latest"].pop("id")
        if include_details:
            payload["latest"]["container_states"] = ProjectMetricSnapshot.objects.filter(
                pk=latest_id,
            ).values_list("container_states", flat=True).first() or []
    payload.update({"id": project.pk, "name": project.name, "slug": project.slug})
    return payload


def server_analytics(server, window="24h", *, now=None):
    start, end, bucket_minutes = window_bounds(window, now=now)
    queryset = ServerMetricSnapshot.objects.filter(server=server, captured_at__gte=start, captured_at__lte=end)
    # Never combine container-capacity, old unknown-provenance, and host metrics.
    # Once real host samples exist, explicitly show their shorter coverage.
    if queryset.filter(source=ServerMetricSnapshot.Source.HOST).exists():
        queryset = queryset.filter(source=ServerMetricSnapshot.Source.HOST)
        source = "host"
    else:
        queryset = queryset.filter(source=ServerMetricSnapshot.Source.LEGACY)
        source = "legacy_sampled"
    rows = list(reversed(list(queryset.order_by("-captured_at", "-id").values(
        "cpu_percent", "memory_percent", "disk_percent", "captured_at", "source",
        "sample_duration_seconds", "host_details",
    )[:MAX_SAMPLES])))
    projects = list(ManagedProject.objects.filter(server=server, is_active=True))
    checks = _checks(HealthCheck.objects.filter(project__in=projects), start, end)
    payload = _payload(rows, checks, window=window, start=start, end=end,
                       bucket_minutes=bucket_minutes, source=source if rows else "unavailable")
    payload.update({"id": server.pk, "name": server.name, "slug": server.slug})
    payload["projects"] = []
    for project in projects:
        detail = project_analytics(project, window, now=end, include_details=False)
        payload["projects"].append({key: detail[key] for key in ("id", "name", "slug", "summary", "coverage")})
    return payload
