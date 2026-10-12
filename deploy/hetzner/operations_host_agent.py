#!/usr/bin/env python3
"""Run one approved operations action on the Docker host, with no public listener."""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEPLOY_PATH = Path(os.environ.get("DEPLOY_PATH", "/opt/school_reports")).resolve()
COMPOSE = ("docker", "compose", "-f", "compose.hetzner.yaml", "-f", "compose.caddy.yaml")
ALLOWED_PROJECTS = {
    "tawtheeq": {"school_reports", "school-reports", "schoolreports"},
    "xmansx": {"tanal", "xmansx", "tanal-barbershop-interface"},
    "mizaan-beta": {"mizaan-beta", "mizaan_beta"},
    "school-display": {"school_display", "school-display", "schooldisplay"},
    "mowadhabah": {"mowadhabah"},
}
BACKUP_UNITS = {
    "tawtheeq": ("school-reports-postgres-backup.service", "school-reports-media-backup.service"),
    "school-display": ("school-display-offsite-backup.service",),
    "mizaan-beta": ("mizaan-beta-offsite-backup.service",),
    "xmansx": ("tanal-offsite-backup.service",),
    "mowadhabah": ("mowadhabah-files-backup.service",),
}
LOG_TIMESTAMP = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z)\s(.*)$")
MAX_LOG_CAPTURE_BYTES = 1024 * 1024


class LogOutputLimitExceeded(RuntimeError):
    """An entire log request fails rather than returning a secret cut mid-value."""


def timestamp_key(stamp: str) -> str:
    base, _, fraction = stamp.removesuffix("Z").partition(".")
    return base + "." + fraction.ljust(9, "0") if stamp else ""


def previous_timestamp(stamp: str) -> str:
    """Keep in sync with operations.log_cursors; this runner is standalone."""
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?Z", stamp)
    if match is None:
        raise ValueError("Invalid log cursor timestamp")
    second = datetime.fromisoformat(match.group(1)).replace(tzinfo=timezone.utc)
    nanoseconds = int((match.group(2) or "").ljust(9, "0"))
    if nanoseconds:
        nanoseconds -= 1
    else:
        second -= timedelta(seconds=1)
        nanoseconds = 999999999
    base = second.isoformat(timespec="seconds").removesuffix("+00:00")
    return f"{base}.{nanoseconds:09d}Z"


def _bounded_output(args: tuple[str, ...], *, timeout: int, maximum: int) -> str:
    output = bytearray()
    exceeded = threading.Event()
    read_errors = []
    with subprocess.Popen(  # noqa: S603 - fixed Docker argv, no shell.
        args, cwd=DEPLOY_PATH, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    ) as process:
        def collect():
            try:
                while chunk := process.stdout.read(65536):
                    if len(output) + len(chunk) > maximum:
                        exceeded.set()
                        process.kill()
                        return
                    output.extend(chunk)
            except OSError:
                read_errors.append("capture_failed")

        reader = threading.Thread(target=collect, daemon=True)
        reader.start()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
            raise
        finally:
            reader.join(timeout=5)
        if exceeded.is_set():
            raise LogOutputLimitExceeded("Log output exceeds the bounded capture size")
        if reader.is_alive() or read_errors:
            raise RuntimeError("Log output could not be captured completely")
        if process.returncode:
            # No raw output is attached to an exception or persisted in an action.
            raise subprocess.CalledProcessError(process.returncode, args)
    return output.decode("utf-8", errors="replace").strip()


def run(*args: str, input_text: str | None = None, timeout: int = 30, include_stderr: bool = False,
        max_output_bytes: int | None = None) -> str:
    if max_output_bytes is not None:
        if input_text is not None or not include_stderr:
            raise ValueError("Bounded log capture requires merged output and no input")
        return _bounded_output(args, timeout=timeout, maximum=max_output_bytes)
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell.
        args, cwd=DEPLOY_PATH, input=input_text, text=True, capture_output=True,
        timeout=timeout, check=True,
    )
    return (result.stdout + ("\n" + result.stderr if include_stderr else "")).strip()


def django(operation: str, payload: dict | None = None) -> dict:
    output = run(
        *COMPOSE, "exec", "-T", "web", "python", "manage.py",
        "operations_agent", operation,
        input_text=json.dumps(payload, separators=(",", ":")) if payload else None,
        timeout=45,
    )
    return json.loads(output)


def heartbeat_loop(stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            django("heartbeat")
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        stop.wait(45)


def containers(compose_project: str, service_key: str) -> list[dict]:
    ids = run(
        "docker", "ps", "-aq", "--filter",
        f"label=com.docker.compose.project={compose_project}", timeout=20,
    ).splitlines()
    if not ids:
        return []
    rows = json.loads(run("docker", "inspect", *ids, timeout=30))
    matches = []
    for row in rows:
        labels = (row.get("Config") or {}).get("Labels") or {}
        service = str(labels.get("com.docker.compose.service") or "")
        normalized = re.sub(r"[^a-z0-9-]+", "-", service.lower().replace("_", "-")).strip("-")
        if labels.get("com.docker.compose.project") == compose_project and normalized == service_key:
            matches.append({
                "id": row["Id"],
                "name": str(row.get("Name") or "").lstrip("/"),
                "service": service,
                "running": bool((row.get("State") or {}).get("Running")),
                "health": ((row.get("State") or {}).get("Health") or {}).get("Status", ""),
            })
    return matches


def read_logs(selected: list[dict], params: dict) -> dict:
    minutes = max(5, min(int(params.get("since_minutes") or 30), 10080))
    tail = max(50, min(int(params.get("tail") or 200), 1000))
    now = datetime.now(timezone.utc)
    until = str(params.get("until") or now.isoformat().replace("+00:00", "Z"))
    since = str(params.get("since_at") or (now - timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z"))
    # A job is a DB message, not authority to pass arbitrary CLI options.
    for value in (since, until):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z", value):
            raise ValueError("Invalid log time boundary")
    records = []
    for item in selected[:4]:
        output = run(
            "docker", "logs", "--timestamps", "--since", since,
            "--until", until, "--tail", str(tail + 1), item["id"],
            timeout=30, include_stderr=True, max_output_bytes=MAX_LOG_CAPTURE_BYTES,
        )
        for line in output.splitlines():
            match = LOG_TIMESTAMP.match(line)
            stamp, message = match.groups() if match else ("", line)
            records.append((stamp, f"{stamp} [{item['name']}] {message}".strip()))
    records.sort(key=lambda record: timestamp_key(record[0]))
    has_more = len(records) > tail
    chosen = records[-tail:]
    timestamps = [stamp for stamp, _ in chosen if stamp]
    oldest = timestamps[0] if timestamps else ""
    boundary_collision = bool(oldest) and any(
        timestamp_key(stamp) == timestamp_key(oldest) for stamp, _ in records[:-tail]
    )
    omitted_containers = max(0, len(selected) - 4)
    retention_note = "المتاح هو السجل الذي ما زال Docker يحتفظ به؛ قد تغيب سجلات الحاويات المستبدلة."
    if boundary_collision:
        retention_note += " تكرر وقت حد الصفحة بين عدة أسطر؛ قد لا يشمل التصفح جميع الأسطر التي تحمل الوقت نفسه."
    if omitted_containers:
        retention_note += f" شملت القراءة أول أربع حاويات فقط؛ لم تشمل {omitted_containers} حاوية إضافية."
    # Bounded pages describe only Docker's retained history, not complete retention.
    return {
        "summary": f"اكتملت قراءة صفحة سجلات {len(selected[:4])} حاوية.",
        "log_content": "\n".join(line for _, line in chosen),
        "log_page": {
            "since_at": since, "until": until, "since_minutes": minutes,
            "requested_tail": tail, "returned_lines": len(chosen),
            "earliest_at": oldest, "latest_at": timestamps[-1] if timestamps else "",
            "has_more": has_more and bool(oldest), "truncated": has_more or bool(omitted_containers),
            # Docker --until includes its boundary; step back exactly 1ns so
            # the displayed boundary record cannot repeat on the older page.
            "next_until": previous_timestamp(oldest) if has_more and oldest else "",
            "boundary_collision": boundary_collision,
            "omitted_container_count": omitted_containers,
            "retention_note": retention_note,
        },
    }


def diagnostics(compose_project: str, slug: str) -> dict:
    ids = run("docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={compose_project}").splitlines()
    if not ids:
        raise ValueError("No project containers")
    inspected = json.loads(run("docker", "inspect", *ids, timeout=30))
    stats = {}
    running = [row["Id"] for row in inspected if row.get("State", {}).get("Running")]
    if running:
        for line in run("docker", "stats", "--no-stream", "--format", "{{json .}}", *running, timeout=45).splitlines():
            row = json.loads(line)
            stats[row.get("Name", "")] = {key: row.get(key) for key in ("CPUPerc", "MemUsage", "MemPerc", "NetIO", "BlockIO", "PIDs")}
    services = []
    for row in inspected:
        state = row.get("State") or {}
        config = row.get("Config") or {}
        host = row.get("HostConfig") or {}
        name = str(row.get("Name") or "").lstrip("/")
        services.append({
            "name": name, "service": (config.get("Labels") or {}).get("com.docker.compose.service", ""),
            "state": state.get("Status"), "health": (state.get("Health") or {}).get("Status", ""),
            "restart_count": row.get("RestartCount", 0), "oom_killed": bool(state.get("OOMKilled")),
            "exit_code": state.get("ExitCode"), "started_at": state.get("StartedAt"),
            "memory_limit_bytes": host.get("Memory", 0), "cpu_limit": host.get("NanoCpus", 0) / 1e9,
            "log_driver": (host.get("LogConfig") or {}).get("Type", ""),
            "log_rotation": (host.get("LogConfig") or {}).get("Config", {}).get("max-size", "default"),
            "resources": stats.get(name, {}),
        })
    backups = []
    for unit in BACKUP_UNITS.get(slug, ()):
        raw = run("systemctl", "show", unit, "--property=LoadState,ActiveState,Result,ExecMainStatus,ExecMainExitTimestamp", timeout=15)
        backups.append({"unit": unit, **dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)})
    return {
        "summary": f"اكتمل تشخيص {len(services)} حاوية ومراجعة حالة النسخ الاحتياطية.",
        "diagnostics": {
            "captured_at": datetime.now(timezone.utc).isoformat(), "compose_project": compose_project,
            "services": services, "backups": backups,
            "running": sum(service["state"] == "running" for service in services),
            "unhealthy": sum(service["health"] == "unhealthy" for service in services),
            "oom_count": sum(service["oom_killed"] for service in services),
            "restart_count": sum(service["restart_count"] for service in services),
        },
    }


def execute(job: dict) -> dict:
    action = str(job.get("action") or "")
    slug = str(job.get("project_slug") or "")
    compose_project = str(job.get("compose_project") or "")
    if compose_project not in ALLOWED_PROJECTS.get(slug, set()):
        raise ValueError("Project is outside the approved host inventory")
    service_key = str(job.get("service_key") or "")
    if action == "diagnostics":
        return diagnostics(compose_project, slug)
    if action in {"read_logs", "restart_service"}:
        if not service_key:
            raise ValueError("A service is required")
        selected = containers(compose_project, service_key)
        if not selected:
            raise ValueError("No matching Docker service is present")
        if action == "read_logs":
            return read_logs(selected, job.get("parameters") or {})
        kind = str(job.get("service_kind") or "")
        if kind not in {"web", "worker"} or any(word in service_key for word in ("beat", "migrate")):
            raise ValueError("Restart is not approved for this service")
        for item in selected:
            if not re.search(r"web|api|app|backend|frontend|worker|celery|queue", item["service"], re.I):
                raise ValueError("Docker service label is not restartable")
        for item in selected:
            run("docker", "restart", "--time", "30", item["id"], timeout=90)
        deadline = time.monotonic() + 90
        while True:
            refreshed = containers(compose_project, service_key)
            if len(refreshed) == len(selected) and all(
                item["running"] and item.get("health", "") in {"", "healthy"} for item in refreshed
            ):
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("Service did not return to healthy running state")
            time.sleep(3)
        return {"summary": f"أُعيد تشغيل {len(selected)} حاوية، وجميعها تعمل."}
    if action == "create_backup" and slug in BACKUP_UNITS:
        for unit in BACKUP_UNITS[slug]:
            run("systemctl", "start", unit, timeout=3600)
            if run("systemctl", "show", unit, "--property=Result", "--value", timeout=15) != "success":
                raise RuntimeError("Backup unit did not report success")
        scope = "ملفات مواظبة الخارجية" if slug == "mowadhabah" else "المشروع الخارجية"
        return {"summary": f"اكتملت نسخة {scope} عبر خدمات النسخ المعتمدة."}
    if action == "reload_proxy" and slug == "tawtheeq":
        run(*COMPOSE, "exec", "-T", "caddy", "caddy", "validate", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile", timeout=30)
        run(*COMPOSE, "exec", "-T", "caddy", "caddy", "reload", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile", timeout=30)
        return {"summary": "تم التحقق من إعداد Caddy وإعادة تحميل الوكيل المشترك."}
    raise ValueError("Host action is not supported")


def main() -> None:
    if sys.argv[1:] == ["--heartbeat-only"]:
        django("heartbeat")
        return
    if len(sys.argv) > 1:
        raise ValueError("Unknown host agent option")
    DEPLOY_PATH.joinpath("deploy", "hetzner").mkdir(parents=True, exist_ok=True)
    with DEPLOY_PATH.joinpath("deploy", "hetzner", ".operations-agent.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        django("heartbeat")
        stop = threading.Event()
        thread = threading.Thread(target=heartbeat_loop, args=(stop,), daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 50
            for _ in range(5):
                job = django("claim")
                if not job:
                    break
                outcome = {"id": job["id"], "request_id": job["request_id"], "status": "succeeded"}
                try:
                    outcome.update(execute(job))
                except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as exc:
                    # Never send raw subprocess output or exception text; it may contain secrets.
                    outcome.update({
                        "status": "failed", "error_code": type(exc).__name__[:80],
                        "summary": (
                            "تجاوز حجم السجل حد القراءة الآمن؛ قلل عدد الأسطر أو الفترة ثم أعد الطلب. لم يُحفظ محتوى جزئي."
                            if isinstance(exc, LogOutputLimitExceeded)
                            else "فشل تنفيذ الإجراء على المضيف. راجع سجل وكيل العمليات."
                        ),
                    })
                    print(f"Host action {job['id']} failed: {type(exc).__name__}", file=sys.stderr)
                django("complete", outcome)
                if time.monotonic() >= deadline:
                    break
        finally:
            stop.set()
            thread.join(timeout=2)


if __name__ == "__main__":
    main()
