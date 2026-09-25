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
from pathlib import Path

DEPLOY_PATH = Path(os.environ.get("DEPLOY_PATH", "/opt/school_reports")).resolve()
COMPOSE = ("docker", "compose", "-f", "compose.hetzner.yaml", "-f", "compose.caddy.yaml")
ALLOWED_PROJECTS = {
    "tawtheeq": {"school_reports", "school-reports", "schoolreports"},
    "xmansx": {"tanal", "xmansx", "tanal-barbershop-interface"},
    "mizaan-beta": {"mizaan-beta", "mizaan_beta"},
    "school-display": {"school_display", "school-display", "schooldisplay"},
}


def run(*args: str, input_text: str | None = None, timeout: int = 30, include_stderr: bool = False) -> str:
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
            })
    return matches


def execute(job: dict) -> dict:
    action = str(job.get("action") or "")
    slug = str(job.get("project_slug") or "")
    compose_project = str(job.get("compose_project") or "")
    if compose_project not in ALLOWED_PROJECTS.get(slug, set()):
        raise ValueError("Project is outside the approved host inventory")
    service_key = str(job.get("service_key") or "")
    if action in {"read_logs", "restart_service"}:
        if not service_key:
            raise ValueError("A service is required")
        selected = containers(compose_project, service_key)
        if not selected:
            raise ValueError("No matching Docker service is present")
        if action == "read_logs":
            params = job.get("parameters") or {}
            since = max(5, min(int(params.get("since_minutes") or 30), 180))
            tail = max(50, min(int(params.get("tail") or 200), 250))
            chunks = []
            for item in selected[:4]:
                output = run(
                    "docker", "logs", "--timestamps", "--since", f"{since}m",
                    "--tail", str(tail), item["id"], timeout=30, include_stderr=True,
                )
                chunks.append(f"[{item['name']}]\n{output}")
            return {
                "summary": f"اكتملت قراءة سجلات {len(chunks)} حاوية.",
                "log_content": "\n".join(chunks)[-100000:],
            }
        kind = str(job.get("service_kind") or "")
        if kind not in {"web", "worker"} or any(word in service_key for word in ("beat", "migrate")):
            raise ValueError("Restart is not approved for this service")
        for item in selected:
            if not re.search(r"web|api|app|frontend|worker|celery|queue", item["service"], re.I):
                raise ValueError("Docker service label is not restartable")
        for item in selected:
            run("docker", "restart", "--time", "30", item["id"], timeout=90)
        refreshed = containers(compose_project, service_key)
        if len(refreshed) != len(selected) or not all(item["running"] for item in refreshed):
            raise RuntimeError("Service did not return to running state")
        return {"summary": f"أُعيد تشغيل {len(selected)} حاوية، وجميعها تعمل."}
    if action == "create_backup" and slug == "tawtheeq":
        run("systemctl", "start", "school-reports-postgres-backup.service", timeout=3600)
        run("systemctl", "start", "school-reports-media-backup.service", timeout=3600)
        return {"summary": "اكتملت نسختا قاعدة البيانات والوسائط عبر خدمات النسخ المعتمدة."}
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
            job = django("claim")
            if not job:
                return
            outcome = {
                "id": job["id"],
                "request_id": job["request_id"],
                "status": "succeeded",
            }
            try:
                outcome.update(execute(job))
            except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as exc:
                # Never send raw subprocess output or exception text; it may contain secrets.
                outcome.update({
                    "status": "failed",
                    "error_code": type(exc).__name__[:80],
                    "summary": "فشل تنفيذ الإجراء على المضيف. راجع سجل وكيل العمليات.",
                })
                print(f"Host action {job['id']} failed: {type(exc).__name__}", file=sys.stderr)
            django("complete", outcome)
        finally:
            stop.set()
            thread.join(timeout=2)


if __name__ == "__main__":
    main()
