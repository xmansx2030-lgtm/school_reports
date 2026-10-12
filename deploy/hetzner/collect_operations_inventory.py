#!/usr/bin/env python3
"""Collect host and per-Compose-project resource usage as a JSON report.

This script runs on the Docker host. It is intentionally dependency-free and
only invokes read-only Docker commands (ps, inspect and stats).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path


_SIZE_RE = re.compile(r"^\s*([0-9.]+)\s*([kmgt]?i?b|b)?\s*$", re.IGNORECASE)
_SIZE_FACTORS = {
    "b": 1,
    "kb": 1000,
    "kib": 1024,
    "mb": 1000**2,
    "mib": 1024**2,
    "gb": 1000**3,
    "gib": 1024**3,
    "tb": 1000**4,
    "tib": 1024**4,
}


def _run(*args: str, timeout: int = 20) -> str:
    result = subprocess.run(  # noqa: S603 - argv only; shell execution is disabled.
        args,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.stdout.strip()


def _size_bytes(value: str) -> float | None:
    match = _SIZE_RE.match(str(value or ""))
    if not match:
        return None
    number = float(match.group(1))
    unit = (match.group(2) or "b").lower()
    return number * _SIZE_FACTORS.get(unit, 1)


def _io_pair(value: str) -> tuple[float | None, float | None]:
    parts = [part.strip() for part in str(value or "").split("/", 1)]
    if len(parts) != 2:
        return None, None
    return _size_bytes(parts[0]), _size_bytes(parts[1])


def _percent(value: str) -> float | None:
    try:
        return max(0.0, float(str(value or "").strip().rstrip("%")))
    except ValueError:
        return None


def _mb(value: float | int | None) -> float | None:
    return round(float(value) / (1024**2), 1) if value is not None else None


def _cpu_sample() -> tuple[int, int, int]:
    fields = [int(value) for value in Path("/proc/stat").read_text(encoding="ascii").splitlines()[0].split()[1:]]
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
    # guest/guest_nice are already included in user/nice ticks.
    return sum(fields[:8]), idle, fields[4] if len(fields) > 4 else 0


def _cpu_metrics() -> tuple[float, float, int, str]:
    """Average the interval since the previous collector run, across one boot."""
    state_path = Path(__file__).resolve().parent / ".operations-cpu-sample.json"
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    current = {"boot_id": boot_id, "monotonic": time.monotonic(), "ticks": _cpu_sample()}
    try:
        previous = json.loads(state_path.read_text(encoding="ascii"))
        elapsed = current["monotonic"] - float(previous["monotonic"])
        valid = previous["boot_id"] == boot_id and 60 <= elapsed <= 1800 and len(previous["ticks"]) == 3
        valid = valid and all(isinstance(value, (int, float)) for value in previous["ticks"])
    except (OSError, ValueError, KeyError, TypeError):
        previous, elapsed, valid = {}, 0, False
    measurement = "interval" if valid else "initial_sample"
    if not valid:
        previous = current
        time.sleep(1)
        current = {"boot_id": boot_id, "monotonic": time.monotonic(), "ticks": _cpu_sample()}
        elapsed = current["monotonic"] - previous["monotonic"]
    total_delta = current["ticks"][0] - previous["ticks"][0]
    idle_delta = current["ticks"][1] - previous["ticks"][1]
    iowait_delta = current["ticks"][2] - previous["ticks"][2]
    if total_delta <= 0 or idle_delta < 0:
        raise ValueError("Invalid CPU counter interval")
    temporary = state_path.with_suffix(".tmp")
    try:
        temporary.write_text(json.dumps(current), encoding="ascii")
        os.chmod(temporary, 0o600)
        os.replace(temporary, state_path)
    except OSError:
        print("operations collector: CPU interval state could not be saved", file=sys.stderr)
    return (
        round(max(0, min(100, (1 - idle_delta / total_delta) * 100)), 1),
        round(max(0, min(100, iowait_delta / total_delta * 100)), 2),
        max(1, round(elapsed)), measurement,
    )


def _pressure() -> dict:
    values = {}
    for resource in ("cpu", "memory", "io"):
        try:
            lines = Path(f"/proc/pressure/{resource}").read_text(encoding="ascii").splitlines()
        except OSError:
            # PSI is optional on older kernels; absence remains visible as no data.
            lines = []
        for line in lines:
            parts = line.split()
            if parts:
                values[f"{resource}_{parts[0]}"] = {
                    key: float(value) for key, value in (part.split("=", 1) for part in parts[1:])
                    if key in {"avg10", "avg60", "avg300"}
                }
    return values


def _host_metrics() -> tuple[dict, float]:
    cpu_percent, iowait_percent, duration, measurement = _cpu_metrics()

    meminfo = {}
    for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
        key, raw = line.split(":", 1)
        meminfo[key] = int(raw.strip().split()[0]) * 1024
    memory_total = float(meminfo.get("MemTotal") or 0)
    memory_available = float(meminfo.get("MemAvailable") or 0)
    memory_percent = round(((memory_total - memory_available) / memory_total) * 100, 1) if memory_total else 0

    disk = shutil.disk_usage("/")
    disk_percent = round((disk.used / disk.total) * 100, 1) if disk.total else 0
    filesystem = os.statvfs("/")
    loads = os.getloadavg()
    return {
        "slug": "school-reports-prod",
        "name": os.uname().nodename,
        "provider": "hetzner",
        "cpu_percent": max(0.0, min(cpu_percent, 100.0)),
        "memory_percent": memory_percent,
        "disk_percent": disk_percent,
        "source": "host",
        "sample_duration_seconds": duration,
        "host_details": {
            "cpu_count": os.cpu_count() or 1,
            "cpu_measurement": measurement,
            "iowait_percent": iowait_percent,
            "load1": round(loads[0], 2), "load5": round(loads[1], 2), "load15": round(loads[2], 2),
            "memory_total_mb": _mb(memory_total), "memory_available_mb": _mb(memory_available),
            "swap_total_mb": _mb(meminfo.get("SwapTotal", 0)),
            "swap_used_mb": _mb(meminfo.get("SwapTotal", 0) - meminfo.get("SwapFree", 0)),
            "disk_total_gb": round(disk.total / 1024**3, 2),
            "disk_used_gb": round(disk.used / 1024**3, 2),
            "disk_free_gb": round(disk.free / 1024**3, 2),
            "disk_inode_percent": round((1 - filesystem.f_ffree / filesystem.f_files) * 100, 2) if filesystem.f_files else None,
            "uptime_seconds": float(Path("/proc/uptime").read_text(encoding="ascii").split()[0]),
            "pressure": _pressure(),
        },
    }, memory_total


def _repository_from_url(url: str) -> str:
    value = str(url or "").strip().removesuffix(".git")
    match = re.search(r"github\.com[/:]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)$", value)
    return match.group(1) if match else ""


def _repository(labels: dict) -> str:
    source = _repository_from_url(labels.get("org.opencontainers.image.source", ""))
    if source:
        return source
    workdir = labels.get("com.docker.compose.project.working_dir", "")
    if not workdir or not Path(workdir).is_dir():
        return ""
    try:
        return _repository_from_url(
            _run("git", "-C", workdir, "remote", "get-url", "origin", timeout=4)
        )
    except (OSError, subprocess.SubprocessError):
        return ""


def _deployed_sha(labels: dict, image: str = "") -> str:
    tag = image.rsplit(":", 1)[-1]
    if re.fullmatch(r"[0-9a-f]{40}", tag):
        return tag
    revision = str(labels.get("org.opencontainers.image.revision") or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{40}", revision):
        return revision
    workdir = labels.get("com.docker.compose.project.working_dir", "")
    if not workdir:
        return ""
    for filename in (".release-sha", ".release-commit"):
        try:
            candidate = (Path(workdir) / filename).read_text(encoding="ascii").strip().lower()
        except OSError:
            continue
        if re.fullmatch(r"[0-9a-f]{40}", candidate):
            return candidate
    return ""


def _image_priority(*, service: str, labels: dict) -> int:
    """Prefer the application image over Postgres/Redis/other dependencies."""
    normalized = service.lower()
    revision_bonus = 10 if labels.get("org.opencontainers.image.revision") else 0
    if normalized in {"web", "backend", "api", "app"}:
        return 5 + revision_bonus
    if "frontend" in normalized:
        return 4 + revision_bonus
    if any(token in normalized for token in ("web", "backend", "worker", "celery", "api")):
        return 3 + revision_bonus
    return 2 if labels.get("org.opencontainers.image.source") else 1



def _container_cpu_usage(item: dict) -> int | None:
    """Read cumulative CPU microseconds from this container's cgroup v2."""
    pid = (item.get("State") or {}).get("Pid")
    if not isinstance(pid, int) or pid <= 0:
        return None
    try:
        lines = Path(f"/proc/{pid}/cgroup").read_text(encoding="ascii").splitlines()
        group = next(line[3:] for line in lines if line.startswith("0::"))
        root = Path("/sys/fs/cgroup").resolve()
        path = (root / group.lstrip("/") / "cpu.stat").resolve()
        if not path.is_relative_to(root):
            return None
        values = dict(line.split() for line in path.read_text(encoding="ascii").splitlines())
        return int(values["usage_usec"])
    except (OSError, ValueError, KeyError, StopIteration):
        # Older cgroup v1 hosts retain the explicitly marked Docker snapshot.
        return None


def _container_cpu_metrics(inspections: list[dict]) -> dict[str, dict]:
    """Average container CPU over the collector interval, excluding stale boots."""
    state_path = Path(__file__).resolve().parent / ".operations-container-cpu.json"
    try:
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError:
        return {}
    now = time.monotonic()
    current = {}
    for item in inspections:
        usage = _container_cpu_usage(item)
        if usage is not None and item.get("Id"):
            current[item["Id"]] = {"usage": usage, "started_at": (item.get("State") or {}).get("StartedAt")}
    try:
        previous = json.loads(state_path.read_text(encoding="ascii"))
        elapsed = now - float(previous["monotonic"])
        valid = previous["boot_id"] == boot_id and 60 <= elapsed <= 1800
        old_containers = previous["containers"] if valid else {}
        if not isinstance(old_containers, dict):
            old_containers = {}
    except (OSError, ValueError, KeyError, TypeError):
        elapsed, old_containers = 0, {}
    result = {}
    for container_id, sample in current.items():
        before = old_containers.get(container_id)
        if not isinstance(before, dict) or sample["started_at"] != before.get("started_at"):
            continue
        usage_before = before.get("usage")
        if not isinstance(usage_before, (int, float)) or usage_before < 0 or sample["usage"] < usage_before:
            continue
        percent = (sample["usage"] - usage_before) / 1_000_000 / elapsed / max(1, os.cpu_count() or 1) * 100
        result[container_id] = {
            "cpu_percent": round(max(0, min(100, percent)), 2),
            "cpu_measurement": "interval",
            "sample_duration_seconds": round(elapsed),
        }
    temporary = state_path.with_suffix(".tmp")
    try:
        temporary.write_text(json.dumps({"boot_id": boot_id, "monotonic": now, "containers": current}), encoding="ascii")
        os.chmod(temporary, 0o600)
        os.replace(temporary, state_path)
    except OSError:
        print("operations collector: container CPU interval state could not be saved", file=sys.stderr)
    return result

def _docker_inventory(memory_total: float) -> list[dict]:
    raw_ids = _run("docker", "ps", "-aq")
    container_ids = [value for value in raw_ids.splitlines() if value.strip()]
    if not container_ids:
        return []
    inspections = json.loads(_run("docker", "inspect", *container_ids, timeout=30))
    cpu_metrics = _container_cpu_metrics(inspections)

    running_names = [
        str(item.get("Name") or "").lstrip("/")
        for item in inspections
        if (item.get("State") or {}).get("Running")
    ]
    stats_by_name = {}
    if running_names:
        try:
            raw_stats = _run(
                "docker",
                "stats",
                "--no-stream",
                "--format",
                "{{ json . }}",
                *running_names,
                timeout=45,
            )
            for line in raw_stats.splitlines():
                row = json.loads(line)
                stats_by_name[str(row.get("Name") or row.get("Container") or "")] = row
        except (json.JSONDecodeError, subprocess.SubprocessError):
            stats_by_name = {}

    cpu_count = max(1, os.cpu_count() or 1)
    grouped: dict[str, list[dict]] = defaultdict(list)
    repositories: dict[str, str] = {}
    repository_checks: set[str] = set()
    deployed_revisions: dict[str, str] = {}
    deployed_images: dict[str, tuple[int, str]] = {}
    for item in inspections:
        config = item.get("Config") or {}
        labels = config.get("Labels") or {}
        compose_project = str(labels.get("com.docker.compose.project") or "").strip()
        if not compose_project:
            # Standalone infrastructure is a service, not an application project.
            continue
        name = str(item.get("Name") or "").lstrip("/")
        service = str(labels.get("com.docker.compose.service") or name)
        state_payload = item.get("State") or {}
        health = (state_payload.get("Health") or {}).get("Status") or ""
        restart_policy = ((item.get("HostConfig") or {}).get("RestartPolicy") or {}).get("Name") or ""
        stats = stats_by_name.get(name, {})
        raw_cpu = _percent(stats.get("CPUPerc"))
        cpu_reading = cpu_metrics.get(item.get("Id"), {
            "cpu_percent": round(raw_cpu / cpu_count, 2) if raw_cpu is not None else None,
            "cpu_measurement": "snapshot" if raw_cpu is not None else "unavailable",
            "sample_duration_seconds": None,
        })
        mem_used, _ = _io_pair(stats.get("MemUsage", ""))
        network_rx, network_tx = _io_pair(stats.get("NetIO", ""))
        block_read, block_write = _io_pair(stats.get("BlockIO", ""))
        memory_limit = float((item.get("HostConfig") or {}).get("Memory") or 0) or None
        grouped[compose_project].append(
            {
                "name": name,
                "service": service,
                "state": str(state_payload.get("Status") or "unknown"),
                "health": str(health),
                # A one-shot job (e.g. migrate) exits 0 and is not restarted; its
                # exit code and restart policy let the collector tell "finished its
                # work" apart from "a service crashed".
                "exit_code": state_payload.get("ExitCode"),
                "restart_policy": str(restart_policy),
                "restart_count": item.get("RestartCount", 0),
                "oom_killed": bool(state_payload.get("OOMKilled")),
                "started_at": str(state_payload.get("StartedAt") or ""),
                "image": str(config.get("Image") or ""),
                # Docker CPU% is per logical CPU. Normalize it to total host capacity.
                **cpu_reading,
                "memory_host_percent": (
                    round((mem_used / memory_total) * 100, 1)
                    if mem_used is not None and memory_total
                    else None
                ),
                "memory_used_mb": _mb(mem_used),
                "memory_limit_mb": _mb(memory_limit),
                "network_rx_mb": _mb(network_rx),
                "network_tx_mb": _mb(network_tx),
                "block_read_mb": _mb(block_read),
                "block_write_mb": _mb(block_write),
            }
        )
        image_repository = _repository_from_url(
            labels.get("org.opencontainers.image.source", "")
        )
        if image_repository:
            repositories[compose_project] = image_repository
        elif compose_project not in repository_checks:
            repository = _repository(labels)
            if repository:
                repositories[compose_project] = repository
            repository_checks.add(compose_project)
        image = str(config.get("Image") or "").strip()
        if image:
            candidate = (_image_priority(service=service, labels=labels), image)
            if candidate[0] > deployed_images.get(compose_project, (0, ""))[0]:
                deployed_images[compose_project] = candidate
                deployed_revisions[compose_project] = _deployed_sha(labels, image)

    return [
        {
            "compose_project": project,
            "name": project.replace("_", " ").replace("-", " ").title(),
            "repository": repositories.get(project, ""),
            "deployed_sha": deployed_revisions.get(project, ""),
            "deployed_image": deployed_images.get(project, (0, ""))[1],
            "containers": containers,
        }
        for project, containers in sorted(grouped.items())
    ]


def main() -> None:
    server, memory_total = _host_metrics()
    print(
        json.dumps(
            {"version": 1, "server": server, "projects": _docker_inventory(memory_total)},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
