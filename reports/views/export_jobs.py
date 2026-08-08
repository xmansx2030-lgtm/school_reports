from __future__ import annotations

import logging

from django.contrib import messages
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from ..models import GeneratedExportJob

logger = logging.getLogger(__name__)


def generated_export_job_response(request, *, job_id, fallback_url: str) -> HttpResponse:
    """Authorised status/download response reused by existing export URLs."""
    try:
        job = GeneratedExportJob.objects.select_related("archive", "school").get(
            pk=int(job_id),
            requested_by=request.user,
        )
    except (GeneratedExportJob.DoesNotExist, TypeError, ValueError):
        raise Http404("مهمة التصدير غير موجودة") from None

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
                return FileResponse(
                    job.artifact_file,
                    as_attachment=True,
                    filename=job.filename or "export.zip",
                    content_type=job.content_type or "application/zip",
                )
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

    return render(
        request,
        "reports/generated_export_status.html",
        {
            "job": job,
            "refresh_url": request.get_full_path(),
            "fallback_url": fallback_url,
        },
        status=202,
    )
