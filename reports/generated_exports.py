from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .cache_utils import redis_cache_lock
from .models import GeneratedExportJob

logger = logging.getLogger(__name__)


def async_exports_enabled() -> bool:
    return bool(getattr(settings, "HEAVY_EXPORT_ASYNC_ENABLED", True))


def _fingerprint(*, school_id: int, user_id: int | None, kind: str, parameters: dict) -> str:
    payload = json.dumps(
        {
            "school": int(school_id),
            "user": int(user_id or 0),
            "kind": str(kind),
            "parameters": parameters,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def enqueue_generated_export(*, school, requested_by, kind: str, parameters: dict | None = None):
    """Create at most one active equivalent export and queue it after commit."""
    params = dict(parameters or {})
    fingerprint = _fingerprint(
        school_id=school.pk,
        user_id=getattr(requested_by, "pk", None),
        kind=kind,
        parameters=params,
    )
    params["fingerprint"] = fingerprint
    lock_key = f"generated-export:enqueue:{fingerprint}"

    with redis_cache_lock(lock_key, timeout=10) as acquired:
        active = GeneratedExportJob.objects.filter(
            school=school,
            requested_by=requested_by,
            kind=kind,
            status__in=[GeneratedExportJob.Status.QUEUED, GeneratedExportJob.Status.RUNNING],
            parameters__fingerprint=fingerprint,
            created_at__gte=timezone.now() - timedelta(minutes=30),
        ).first()
        if active is not None:
            return active, False

        # If Redis is momentarily unavailable the database lookup above still
        # provides best-effort de-duplication; correctness does not depend on the
        # lock, only avoiding duplicate expensive work does.
        job = GeneratedExportJob.objects.create(
            school=school,
            requested_by=requested_by,
            kind=kind,
            parameters=params,
        )

        def _enqueue():
            try:
                from .tasks import build_generated_export_task

                build_generated_export_task.apply_async(args=[job.pk], queue="images")
            except Exception as exc:
                logger.exception("Unable to queue generated export job=%s", job.pk)
                GeneratedExportJob.objects.filter(pk=job.pk).update(
                    status=GeneratedExportJob.Status.FAILED,
                    error_message=str(exc)[:500],
                    completed_at=timezone.now(),
                )

        transaction.on_commit(_enqueue)
        return job, True


def wait_for_job_visibility(job_id: int, *, seconds: float = 0.5):
    """Tiny polling helper used only after a duplicate enqueue race."""
    deadline = time.monotonic() + max(0.0, seconds)
    while time.monotonic() < deadline:
        job = GeneratedExportJob.objects.filter(pk=job_id).first()
        if job is not None:
            return job
        time.sleep(0.05)
    return None
