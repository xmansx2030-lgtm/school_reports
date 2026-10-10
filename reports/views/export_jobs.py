from __future__ import annotations

import logging

from django.conf import settings
from django.contrib import messages
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django_ratelimit.core import is_ratelimited

from ..models import GeneratedExportJob

logger = logging.getLogger(__name__)


def export_creation_is_limited(request, *, group: str, rate: str) -> bool:
    """Rate-limit new builds while allowing lightweight status polling."""
    return bool(
        is_ratelimited(
            request,
            group=group,
            key="user",
            rate=rate,
            method="GET",
            increment=True,
        )
    )


def generated_export_job_response(
    request, *, job_id, fallback_url: str, expected_school=None, expected_kind=None,
) -> HttpResponse:
    """Authorised status/download response reused by existing export URLs."""
    try:
        job = GeneratedExportJob.objects.select_related("archive", "school").get(
            pk=int(job_id),
            requested_by=request.user,
        )
    except (GeneratedExportJob.DoesNotExist, TypeError, ValueError):
        raise Http404("مهمة التصدير غير موجودة") from None

    if (
        (expected_school is not None and job.school_id != expected_school.pk)
        or (expected_kind is not None and job.kind != expected_kind)
    ):
        raise Http404("مهمة التصدير غير موجودة")

    if job.kind == GeneratedExportJob.Kind.SCHOOL_ZIP:
        from ..permissions import is_school_manager
        from ._helpers import _get_active_school

        active_school = expected_school or _get_active_school(request)
        if active_school is None or active_school.pk != job.school_id or not (
            request.user.is_superuser
            or is_school_manager(request.user, active_school=job.school)
        ):
            raise Http404("مهمة التصدير غير موجودة")

    if (
        job.status == GeneratedExportJob.Status.READY
        and job.expires_at
        and job.expires_at <= timezone.now()
    ):
        try:
            if getattr(job.artifact_file, "name", ""):
                job.artifact_file.delete(save=False)
        finally:
            GeneratedExportJob.objects.filter(pk=job.pk).update(
                artifact_file="",
                status=GeneratedExportJob.Status.EXPIRED,
            )
            job.status = GeneratedExportJob.Status.EXPIRED

    if job.status == GeneratedExportJob.Status.READY:
        if job.archive_id:
            messages.success(request, "اكتمل إنشاء نسخة الأرشيف وحُفظت بصورة دائمة.")
            return redirect(
                f"{reverse('reports:school_archive')}?year={job.archive.academic_year}&snapshot={job.archive_id}"
            )
        if getattr(job.artifact_file, "name", ""):
            try:
                job.artifact_file.open("rb")
                response = FileResponse(
                    job.artifact_file,
                    as_attachment=True,
                    filename=job.filename or "export.zip",
                    content_type=job.content_type or "application/zip",
                )
                response["Cache-Control"] = "private, no-store"
                return response
            except Exception:
                logger.exception("Generated export download failed job=%s", job.pk)
                messages.error(request, "تعذر فتح الملف الجاهز. أعد إنشاء التصدير.")
                return redirect(fallback_url)

    if job.status in {GeneratedExportJob.Status.FAILED, GeneratedExportJob.Status.EXPIRED}:
        message = (
            "انتهت صلاحية ملف التصدير؛ أنشئ نسخة جديدة."
            if job.status == GeneratedExportJob.Status.EXPIRED
            else "تعذر إنشاء ملف التصدير. حاول مرة أخرى."
        )
        messages.error(request, message)
        return redirect(fallback_url)

    now = timezone.now()
    waiting_seconds = max(0, int((now - job.created_at).total_seconds()))
    stale_after = max(
        30,
        int(getattr(settings, "GENERATED_EXPORT_QUEUE_STALE_SECONDS", 120) or 120),
    )
    recovery_enqueued = bool(
        (job.parameters or {}).get("core_recovery_enqueued_at")
    )

    return render(
        request,
        "reports/generated_export_status.html",
        {
            "job": job,
            "refresh_url": request.get_full_path(),
            "fallback_url": fallback_url,
            "waiting_seconds": waiting_seconds,
            "is_delayed": (
                job.status == GeneratedExportJob.Status.QUEUED
                and waiting_seconds >= stale_after
            ),
            "recovery_enqueued": recovery_enqueued,
        },
        status=202,
    )
