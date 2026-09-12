"""Business service for building and persisting generated export artifacts."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.core.files import File
from django.db import transaction
from django.utils import timezone

from core import opmetrics
from core.observability import report_degraded as _degraded, soft_fail

from .cache_utils import redis_cache_lock
from .models import GeneratedExportJob, School, SchoolYearArchive, Teacher

logger = logging.getLogger(__name__)


def locked_generated_export_job(job_id: int) -> GeneratedExportJob | None:
    """Lock only the job row; nullable joins make PostgreSQL reject FOR UPDATE."""
    return GeneratedExportJob.objects.select_for_update().filter(pk=job_id).first()


def build_generated_export_job(job_id: int) -> bool:
    """Build a ZIP and persist its durable export-job state."""
    from .services_archive import (
        archive_snapshot_capacity_error,
        sync_school_archive_storage_usage,
    )
    from .services_export import (
        archive_zip_filename,
        build_school_export_zip_file,
        export_zip_filename,
    )

    with redis_cache_lock(
        f"generated-export:build:{int(job_id)}",
        timeout=31 * 60,
    ) as acquired:
        if not acquired:
            logger.info(
                "Generated export already owned by another worker job=%s",
                job_id,
            )
            return True

        with transaction.atomic():
            job = locked_generated_export_job(job_id)
            if job is None:
                return False
            if job.status == GeneratedExportJob.Status.READY:
                return True
            job.status = GeneratedExportJob.Status.RUNNING
            job.started_at = timezone.now()
            job.error_message = ""
            job.save(update_fields=["status", "started_at", "error_message"])

        zip_file: Any | None = None
        archive: SchoolYearArchive | None = None
        metadata: dict[str, Any] | None = None
        try:
            school = School.objects.get(pk=job.school_id)
            requested_by = Teacher.objects.filter(pk=job.requested_by_id).first()
            params = dict(job.parameters or {})

            if job.kind == GeneratedExportJob.Kind.SCHOOL_ZIP:
                zip_file = build_school_export_zip_file(school, request=None)
                filename = export_zip_filename(school)
                metadata = None
            else:
                academic_year = str(params.get("academic_year") or "").strip()
                if not academic_year:
                    raise ValueError("السنة الدراسية مطلوبة لإنشاء الأرشيف.")
                school_wide = bool(params.get("school_wide", True))
                zip_file, metadata = build_school_export_zip_file(
                    school,
                    academic_year=academic_year,
                    teacher=requested_by,
                    school_wide=school_wide,
                    request=None,
                    return_metadata=True,
                )
                filename = archive_zip_filename(school, academic_year)

            try:
                zip_file.seek(0, 2)
                size_bytes = int(zip_file.tell())
                zip_file.seek(0)
            except Exception:
                size_bytes = int((metadata or {}).get("archive_size_bytes") or 0)

            if job.kind == GeneratedExportJob.Kind.ARCHIVE_SNAPSHOT:
                if metadata is None:
                    raise RuntimeError("Archive export metadata is missing.")
                capacity_error = archive_snapshot_capacity_error(school, size_bytes)
                if capacity_error:
                    raise ValueError(capacity_error)

                academic_year = str(params.get("academic_year") or "").strip()
                with transaction.atomic():
                    School.objects.select_for_update().get(pk=school.pk)
                    locked_job = GeneratedExportJob.objects.select_for_update().get(
                        pk=job.pk
                    )
                    if locked_job.status == GeneratedExportJob.Status.READY:
                        return True
                    latest_version = (
                        SchoolYearArchive.objects.filter(
                            school=school,
                            academic_year=academic_year,
                        )
                        .order_by("-version")
                        .values_list("version", flat=True)
                        .first()
                        or 0
                    )
                    archive = SchoolYearArchive(
                        school=school,
                        academic_year=academic_year,
                        version=int(latest_version) + 1,
                        status=(
                            SchoolYearArchive.Status.PARTIAL
                            if metadata["is_partial"]
                            else SchoolYearArchive.Status.READY
                        ),
                        archive_sha256=metadata["archive_sha256"],
                        file_count=metadata["file_count"],
                        missing_file_count=metadata["missing_file_count"],
                        failed_pdf_count=metadata["failed_pdf_count"],
                        report_count=metadata["report_count"],
                        achievement_count=metadata["achievement_count"],
                        leadership_count=metadata["leadership_count"],
                        ticket_count=metadata["ticket_count"],
                        circular_count=metadata["circular_count"],
                        notification_count=metadata["notification_count"],
                        assignment_count=int(
                            metadata.get("assignment_count") or 0
                        ),
                        plan_count=int(metadata.get("plan_count") or 0),
                        initiative_count=int(metadata.get("initiative_count") or 0),
                        lab_asset_count=int(metadata.get("lab_asset_count") or 0),
                        lab_handover_count=int(
                            metadata.get("lab_handover_count") or 0
                        ),
                        lab_experiment_count=int(
                            metadata.get("lab_experiment_count") or 0
                        ),
                        notes=metadata["notes"],
                        created_by=requested_by,
                    )
                    archive.archive_file.save(filename, File(zip_file), save=False)
                    archive.save()
                    locked_job.status = GeneratedExportJob.Status.READY
                    locked_job.archive = archive
                    locked_job.filename = filename
                    locked_job.size_bytes = size_bytes
                    locked_job.completed_at = timezone.now()
                    locked_job.expires_at = None
                    locked_job.save(
                        update_fields=[
                            "status",
                            "archive",
                            "filename",
                            "size_bytes",
                            "completed_at",
                            "expires_at",
                        ]
                    )
                sync_school_archive_storage_usage(school)
            else:
                job.artifact_file.save(filename, File(zip_file), save=False)
                job.status = GeneratedExportJob.Status.READY
                job.filename = filename
                job.content_type = "application/zip"
                job.size_bytes = size_bytes
                job.completed_at = timezone.now()
                job.expires_at = timezone.now() + timedelta(
                    hours=max(
                        1,
                        int(
                            getattr(
                                settings,
                                "GENERATED_EXPORT_RETENTION_HOURS",
                                6,
                            )
                            or 6
                        ),
                    )
                )
                job.save(
                    update_fields=[
                        "artifact_file",
                        "status",
                        "filename",
                        "content_type",
                        "size_bytes",
                        "completed_at",
                        "expires_at",
                    ]
                )

            opmetrics.increment(f"generated_export.success.{job.kind}")
            logger.info(
                "Generated export ready job=%s kind=%s school=%s bytes=%s",
                job.pk,
                job.kind,
                job.school_id,
                size_bytes,
            )
            return True
        except Exception as exc:
            logger.exception("Generated export failed job=%s", job_id)
            GeneratedExportJob.objects.filter(pk=job_id).update(
                status=GeneratedExportJob.Status.FAILED,
                error_message=str(exc)[:500],
                completed_at=timezone.now(),
            )
            opmetrics.increment("generated_export.failed")
            if archive is not None and archive.archive_file.name:
                try:
                    persisted = bool(
                        archive.pk
                        and SchoolYearArchive.objects.filter(pk=archive.pk).exists()
                    )
                    if not persisted:
                        archive.archive_file.delete(save=False)
                except Exception:
                    # ملفٌ يتيم في R2 يُحتسب على حصة المدرسة إلى الأبد.
                    _degraded(
                        "storage.orphan_archive_cleanup",
                        archive_id=getattr(archive, "pk", None),
                    )
            if (
                job.kind != GeneratedExportJob.Kind.ARCHIVE_SNAPSHOT
                and getattr(job.artifact_file, "name", "")
            ):
                with soft_fail("storage.orphan_export_cleanup", job_id=job.pk):
                    job.artifact_file.delete(save=False)
            raise
        finally:
            if zip_file is not None:
                with soft_fail("export.close_zip_handle"):
                    zip_file.close()
