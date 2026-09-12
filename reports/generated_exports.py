from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import timedelta

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .cache_utils import redis_cache_lock
from .models import GeneratedExportJob
from .services_generated_exports import build_generated_export_job
from .task_dispatch import enqueue_named_task
from .task_names import BUILD_GENERATED_EXPORT_TASK

logger = logging.getLogger(__name__)
CORE_RECOVERY_MODE = "inline-v1"


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

    with redis_cache_lock(lock_key, timeout=10):
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
                enqueue_named_task(
                    BUILD_GENERATED_EXPORT_TASK,
                    args=[job.pk],
                    queue="images",
                )
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


def recover_stale_generated_exports(*, limit: int = 1) -> int:
    """Build exports abandoned by the media queue in the core worker once.

    The primary route remains the isolated ``images`` queue.  If that worker is
    unavailable, a durable job used to remain ``queued`` forever.  Beat runs this
    recovery *inside* the core worker, so execute one fallback build there instead
    of publishing another message that can itself remain stranded.  The build
    lock keeps a late media delivery and this fallback idempotent.
    """
    queued_after = max(
        30,
        int(getattr(settings, "GENERATED_EXPORT_QUEUE_STALE_SECONDS", 120) or 120),
    )
    running_after = max(
        300,
        int(
            getattr(settings, "GENERATED_EXPORT_RUNNING_STALE_SECONDS", 35 * 60)
            or 35 * 60
        ),
    )
    retry_after = max(
        60,
        int(getattr(settings, "GENERATED_EXPORT_RECOVERY_RETRY_SECONDS", 600) or 600),
    )
    max_attempts = max(
        1,
        int(getattr(settings, "GENERATED_EXPORT_RECOVERY_MAX_ATTEMPTS", 3) or 3),
    )
    now = timezone.now()
    candidates = list(
        GeneratedExportJob.objects.filter(
            status__in=[GeneratedExportJob.Status.QUEUED, GeneratedExportJob.Status.RUNNING]
        )
        .filter(
            models.Q(
                status=GeneratedExportJob.Status.QUEUED,
                created_at__lte=now - timedelta(seconds=queued_after),
            )
            | models.Q(
                status=GeneratedExportJob.Status.RUNNING,
                started_at__lte=now - timedelta(seconds=running_after),
            )
        )
        .order_by("created_at", "id")[: max(1, int(limit or 1))]
    )

    recovered = 0
    for candidate in candidates:
        with transaction.atomic():
            job = GeneratedExportJob.objects.select_for_update().get(pk=candidate.pk)
            parameters = dict(job.parameters or {})
            attempts = int(parameters.get("core_recovery_attempts") or 0)
            recovery_mode = str(parameters.get("core_recovery_mode") or "")
            last_recovery = parse_datetime(
                str(parameters.get("core_recovery_enqueued_at") or "")
            )
            if (
                recovery_mode == CORE_RECOVERY_MODE
                and last_recovery
                and (now - last_recovery).total_seconds() < retry_after
            ):
                continue
            if attempts >= max_attempts:
                job.status = GeneratedExportJob.Status.FAILED
                job.error_message = (
                    "تعذر تشغيل عاملَي إنشاء الأرشيف بعد محاولات الاسترداد التلقائي."
                )
                job.completed_at = now
                job.save(update_fields=["status", "error_message", "completed_at"])
                logger.error("Generated export recovery exhausted job=%s", job.pk)
                continue
            parameters["core_recovery_enqueued_at"] = now.isoformat()
            parameters["core_recovery_mode"] = CORE_RECOVERY_MODE
            parameters["core_recovery_from_status"] = job.status
            parameters["core_recovery_attempts"] = attempts + 1
            job.parameters = parameters
            job.status = GeneratedExportJob.Status.QUEUED
            job.started_at = None
            job.error_message = ""
            job.save(
                update_fields=["parameters", "status", "started_at", "error_message"]
            )

        try:
            build_generated_export_job(job.pk)
        except Exception as exc:
            logger.exception("Unable to build recovered generated export job=%s", job.pk)
            GeneratedExportJob.objects.filter(pk=job.pk).update(
                status=GeneratedExportJob.Status.FAILED,
                error_message=str(exc)[:500],
                completed_at=timezone.now(),
            )
        else:
            recovered += 1
            logger.warning(
                "Built stale generated export in core worker job=%s previous_status=%s",
                job.pk,
                parameters["core_recovery_from_status"],
            )
    return recovered
