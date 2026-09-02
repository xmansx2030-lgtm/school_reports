from __future__ import annotations

from typing import Final


SERVER_DEFAULTS: Final = {
    "slug": "school-reports-prod",
    "name": "school-reports-prod",
    "provider": "hetzner",
    "provider_server_id": "155662703",
    "public_ip": "178.104.163.3",
    "server_type": "CPX32",
}


PROJECTS: Final = (
    {
        "slug": "tawtheeq",
        "compose_aliases": ("school_reports", "school-reports", "schoolreports"),
        "name": "منصة توثيق",
        "base_url": "https://tawtheeq-ksa.com",
        "health_path": "/healthz/",
        "repository": "xmansx2030-lgtm/school_reports",
        "ci_workflow": "ci.yml",
        "deploy_workflow": "ci.yml",
        "deploy_container": "school-reports-web-1",
    },
    {
        "slug": "xmansx",
        "compose_aliases": ("tanal", "xmansx", "tanal-barbershop-interface"),
        "name": "منصة TANAL",
        "base_url": "https://xmansx.com",
        "health_path": "/api/health/readiness",
        "repository": "xmansx2030-lgtm/tanal",
        "ci_workflow": "ci.yml",
        "deploy_workflow": "deploy.yml",
        "deploy_container": "tanal-web-1",
    },
    {
        "slug": "school-display",
        "compose_aliases": ("school_display", "school-display", "schooldisplay"),
        "name": "لوحة العرض المدرسية",
        "base_url": "https://school-display.com",
        "health_path": "/",
        "repository": "xmansx2030-lgtm/school_display",
        "ci_workflow": "ci.yml",
        "deploy_workflow": "deploy.yml",
        "deploy_container": "school-display-web-1",
    },
)


def canonical_project(compose_project: str) -> dict | None:
    normalized = compose_project.strip().lower()
    for project in PROJECTS:
        if normalized == project["slug"] or normalized in project["compose_aliases"]:
            return project
    return None


def project_defaults(project: dict, *, server, sort_order: int) -> dict:
    aliases = project["compose_aliases"]
    return {
        "server": server,
        "name": project["name"],
        "base_url": project["base_url"],
        "health_path": project["health_path"],
        "compose_project": aliases[0],
        "repository": project["repository"],
        "deploy_branch": "main",
        "ci_workflow": project["ci_workflow"],
        "deploy_repository": project["repository"],
        "deploy_workflow": project["deploy_workflow"],
        "deploy_container": project["deploy_container"],
        "deployment_enabled": True,
        "sort_order": sort_order,
        "is_active": True,
    }
