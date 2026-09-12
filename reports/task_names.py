"""Stable Celery task names used by producers outside ``reports.tasks``."""

DELETE_ORPHANED_STORAGE_FILE_TASK = (
    "reports.tasks.delete_orphaned_storage_file_task"
)
BUILD_GENERATED_EXPORT_TASK = "reports.tasks.build_generated_export_task"
