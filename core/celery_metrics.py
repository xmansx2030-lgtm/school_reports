from __future__ import annotations

import logging
import time

from celery.signals import task_failure, task_postrun, task_prerun, task_retry
from django.core.cache import cache

from core import opmetrics


logger = logging.getLogger(__name__)

_CACHE_PREFIX = "celery:task:start:"


def _start_key(task_id: str | None) -> str | None:
    if not task_id:
        return None
    return f"{_CACHE_PREFIX}{task_id}"


@task_prerun.connect
def record_task_start(sender=None, task_id=None, task=None, **kwargs):
    key = _start_key(task_id)
    if not key:
        return
    try:
        cache.set(key, time.monotonic(), timeout=7200)
    except Exception:
        # Metrics are best effort and must never alter task execution.
        logger.debug("Unable to record Celery task start", exc_info=True)


@task_postrun.connect
def record_task_finish(sender=None, task_id=None, task=None, state=None, **kwargs):
    key = _start_key(task_id)
    started = None
    try:
        if key:
            started = cache.get(key)
            cache.delete(key)
    except Exception:
        logger.debug("Unable to finish Celery timing metric", exc_info=True)

    task_name = getattr(sender, "name", None) or getattr(task, "name", None) or "unknown"
    opmetrics.increment(f"celery.task.postrun.{state or 'unknown'}")
    if started is None:
        return
    try:
        duration_ms = round((time.monotonic() - float(started)) * 1000, 1)
    except Exception:
        return
    opmetrics.timing(f"celery.task.duration.{task_name}", duration_ms)
    logger.info(
        "Celery task finished task=%s task_id=%s state=%s duration_ms=%s",
        task_name,
        task_id,
        state,
        duration_ms,
    )


@task_failure.connect
def record_task_failure(task_id=None, sender=None, exception=None, traceback=None, einfo=None, **kwargs):
    task_name = getattr(sender, "name", None) or "unknown"
    opmetrics.increment(f"celery.task.failure_signal.{task_name}")
    logger.warning(
        "Celery task failed task=%s task_id=%s error=%s",
        task_name,
        task_id,
        exception,
    )


@task_retry.connect
def record_task_retry(request=None, reason=None, einfo=None, **kwargs):
    task_name = getattr(request, "task", None) or "unknown"
    opmetrics.increment(f"celery.task.retry.{task_name}")
    logger.info(
        "Celery task retry task=%s task_id=%s retries=%s reason=%s",
        task_name,
        getattr(request, "id", None),
        getattr(request, "retries", None),
        reason,
    )
