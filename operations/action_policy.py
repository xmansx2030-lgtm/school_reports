"""Fixed, reviewed operations capabilities for the hosted projects."""

from __future__ import annotations


BACKUP_UNITS = {
    "tawtheeq": ("school-reports-postgres-backup.service", "school-reports-media-backup.service"),
    "school-display": ("school-display-offsite-backup.service",),
    "mizaan-beta": ("mizaan-beta-offsite-backup.service",),
    "xmansx": ("tanal-offsite-backup.service",),
    "mowadhabah": ("mowadhabah-files-backup.service",),
}

MAX_LOG_MINUTES = 7 * 24 * 60
MAX_LOG_LINES = 1000


def project_actions(project) -> list[str]:
    from .inventory import canonical_project  # noqa: PLC0415

    actions = ["check_now"] if project.base_url else []
    known = canonical_project(project.compose_project)
    if known is None or known["slug"] != project.slug:
        return actions
    actions.extend(("read_logs", "diagnostics"))
    if project.services.filter(is_active=True, restart_allowed=True).exists():
        actions.append("restart_service")
    if project.slug in BACKUP_UNITS:
        actions.append("create_backup")
    if project.slug == "tawtheeq":
        actions.append("reload_proxy")
    return actions


def backup_description(slug: str) -> str:
    if slug == "mowadhabah":
        return "نسخة ملفات مواظبة الخارجية؛ نسخة قاعدة البيانات تديرها مهمة مواظبة الدورية."
    if slug in BACKUP_UNITS:
        return "نسخة خارجية عبر خدمات النسخ المعتمدة لهذا المشروع."
    return ""
