"""Stable Celery task names used by producers outside ``reports.tasks``."""

DELETE_ORPHANED_STORAGE_FILE_TASK = (
    "reports.tasks.delete_orphaned_storage_file_task"
)
BUILD_GENERATED_EXPORT_TASK = "reports.tasks.build_generated_export_task"
MONITOR_INFRASTRUCTURE_CAPACITY_TASK = (
    "reports.tasks.monitor_infrastructure_capacity_task"
)
SEND_NOTIFICATION_TASK = "reports.tasks.send_notification_task"
SEND_TELEGRAM_ALERT_TASK = "reports.tasks.send_telegram_alert_task"
SEND_WEB_PUSH_NOTIFICATION_TASK = (
    "reports.tasks.send_web_push_notification_task"
)
