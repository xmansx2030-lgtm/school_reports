"""Stable Celery task names used by operations producers and workers."""

SEND_INCIDENT_PUSH_TASK = "operations.tasks.send_incident_push_task"
STORE_CAPACITY_SNAPSHOT_TASK = "operations.tasks.store_capacity_snapshot_task"

__all__ = ("SEND_INCIDENT_PUSH_TASK", "STORE_CAPACITY_SNAPSHOT_TASK")
