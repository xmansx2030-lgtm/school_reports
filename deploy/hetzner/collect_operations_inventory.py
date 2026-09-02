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


def _cpu_sample() -> tuple[int, int]:
    fields = [int(value) for value in Path("/proc/stat").read_text(encoding="ascii").splitlines()[0].split()[1:]]
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
    return sum(fields), idle


def _host_metrics() -> tuple[dict, float]:
    total_before, idle_before = _cpu_sample()
    time.sleep(0.15)
    total_after, idle_after = _cpu_sample()
    total_delta = max(1, total_after - total_before)
    cpu_percent = round((1 - ((idle_after - idle_before) / total_delta)) * 100, 1)

    meminfo = {}
    for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
        key, raw = line.split(":", 1)
        meminfo[key] = int(raw.strip().split()[0]) * 1024
    memory_total = float(meminfo.get("MemTotal") or 0)
    memory_available = float(meminfo.get("MemAvailable") or 0)
    memory_percent = round(((memory_total - memory_available) / memory_total) * 100, 1) if memory_total else 0

    disk = shutil.disk_usage("/")
    disk_percent = round((disk.used / disk.total) * 100, 1) if disk.total else 0
    return {
        "slug": "school-reports-prod",
        "name": os.uname().nodename,
        "provider": "hetzner",
        "cpu_percent": max(0.0, min(cpu_percent, 100.0)),
        "memory_percent": memory_percent,
        "disk_percent": disk_percent,
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


def _deployed_sha(labels: dict) -> str:
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
    try:
        return _repository_from_url(_run("git", "-C", workdir, "remote", "get-url", "origin", timeout=4))
    except (OSError, subprocess.SubprocessError):
        return ""


def _docker_inventory(memory_total: float) -> list[dict]:
    raw_ids = _run("docker", "ps", "-aq")
    container_ids = [value for value in raw_ids.splitlines() if value.strip()]
    if not container_ids:
        return []
    inspections = json.loads(_run("docker", "inspect", *container_ids, timeout=30))

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
    deployed_revisions: dict[str, str] = {}
    for item in inspections:
        config = item.get("Config") or {}
        labels = config.get("Labels") or {}
        compose_project = str(labels.get("com.docker.compose.project") or "").strip()
        if not compose_project:
            # Standalone infrastructure is a service, not an application project.
            continue
        name = str(item.get("Name") or "").lstrip("/")
        state_payload = item.get("State") or {}
        health = (state_payload.get("Health") or {}).get("Status") or ""
        stats = stats_by_name.get(name, {})
        raw_cpu = _percent(stats.get("CPUPerc"))
        mem_used, _ = _io_pair(stats.get("MemUsage", ""))
        network_rx, network_tx = _io_pair(stats.get("NetIO", ""))
        block_read, block_write = _io_pair(stats.get("BlockIO", ""))
        memory_limit = float((item.get("HostConfig") or {}).get("Memory") or 0) or None
        grouped[compose_project].append(
            {
                "name": name,
                "service": str(labels.get("com.docker.compose.service") or name),
                "state": str(state_payload.get("Status") or "unknown"),
                "health": str(health),
                # Docker CPU% is per logical CPU. Normalize it to total host capacity.
                "cpu_percent": round(raw_cpu / cpu_count, 1) if raw_cpu is not None else None,
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
        repositories.setdefault(compose_project, _repository(labels))
        revision = _deployed_sha(labels)
        if revision:
            deployed_revisions[compose_project] = revision

    return [
        {
            "compose_project": project,
            "name": project.replace("_", " ").replace("-", " ").title(),
            "repository": repositories.get(project, ""),
            "deployed_sha": deployed_revisions.get(project, ""),
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
