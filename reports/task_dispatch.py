"""Small Celery dispatch boundary for producers that only know a task name."""

from __future__ import annotations

from celery import current_app


def enqueue_named_task(
    task_name: str,
    *,
    args=None,
    kwargs=None,
    app=None,
    **options,
):
    """Queue a task without importing ``reports.tasks`` from its producer.

    Prefer a registered task so Celery eager mode keeps working in development
    and tests. ``send_task`` remains a safe fallback for web processes where
    the task registry has not been populated yet.
    """
    celery_app = app or current_app
    call_options = dict(options)
    if args is not None:
        call_options["args"] = args
    if kwargs is not None:
        call_options["kwargs"] = kwargs
    task = celery_app.tasks.get(task_name)
    if task is not None:
        return task.apply_async(**call_options)
    return celery_app.send_task(
        task_name,
        **call_options,
    )
