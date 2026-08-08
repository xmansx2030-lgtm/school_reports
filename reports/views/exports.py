# reports/views/exports.py
# -*- coding: utf-8 -*-
"""تصدير بيانات المدرسة الكاملة (مدير المدرسة)."""
from __future__ import annotations

from django.http import HttpResponse, FileResponse

from ._helpers import *
from ._helpers import _get_active_school, _user_manager_schools

from ..services_export import (
    build_school_export_bytes,
    build_school_export_zip_file,
    export_filename,
    export_zip_filename,
    export_summary_counts,
)
from ..services_archive import school_archive_enabled
from ..generated_exports import async_exports_enabled, enqueue_generated_export
from ..models import GeneratedExportJob
from .export_jobs import generated_export_job_response

_XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _require_manager_school(request: HttpRequest):
    active_school = _get_active_school(request)
    if School.objects.filter(is_active=True).exists():
        if active_school is None:
            messages.error(request, "فضلاً اختر مدرسة أولاً.")
            return active_school, redirect("reports:select_school")
        if (not request.user.is_superuser) and active_school not in _user_manager_schools(request.user):
            messages.error(request, "ليست لديك صلاحية على هذه المدرسة.")
            return active_school, redirect("reports:select_school")
    if active_school is None:
        messages.error(request, "لا توجد مدرسة نشطة للتصدير.")
        return active_school, redirect("reports:admin_dashboard")
    # التصدير الكامل مرتبط باشتراك الأرشفة: غير مفعّل للمدارس غير المشتركة.
    if not school_archive_enabled(active_school):
        messages.error(request, "ميزة التصدير مرتبطة باشتراك الأرشفة وغير مفعّلة لهذه المدرسة. فعّل الاشتراك أولاً.")
        return active_school, redirect("reports:my_subscription")
    return active_school, None


@login_required(login_url="reports:login")
@role_required({"manager"})
@require_http_methods(["GET"])
def school_data_export(request: HttpRequest) -> HttpResponse:
    """صفحة معاينة التصدير: تعرض ملخص ما سيُصدَّر مع زر التنزيل."""
    active_school, redirect_resp = _require_manager_school(request)
    if redirect_resp is not None:
        return redirect_resp

    counts = export_summary_counts(active_school)
    return render(
        request,
        "reports/school_data_export.html",
        {"active_school": active_school, "counts": counts},
    )


@login_required(login_url="reports:login")
@role_required({"manager"})
@ratelimit(key="user", rate="10/h", method="GET", block=True)
@require_http_methods(["GET"])
def school_data_export_download(request: HttpRequest) -> HttpResponse:
    """تنزيل ملف Excel لبيانات المدرسة الكاملة."""
    active_school, redirect_resp = _require_manager_school(request)
    if redirect_resp is not None:
        return redirect_resp

    try:
        content = build_school_export_bytes(active_school)
    except Exception:
        logger.exception("school_data_export_download failed school_id=%s", getattr(active_school, "id", None))
        messages.error(request, "تعذّر إنشاء ملف التصدير. حاول لاحقًا.")
        return redirect("reports:school_data_export")

    response = HttpResponse(content, content_type=_XLSX_CONTENT_TYPE)
    response["Content-Disposition"] = f'attachment; filename="{export_filename(active_school)}"'
    response["Content-Length"] = str(len(content))
    logger.info(
        "School data exported (xlsx) school_id=%s user_id=%s bytes=%s",
        getattr(active_school, "id", None),
        getattr(request.user, "id", None),
        len(content),
    )
    return response


@login_required(login_url="reports:login")
@role_required({"manager"})
@ratelimit(key="user", rate="6/h", method="GET", block=True)
@require_http_methods(["GET"])
def school_data_export_zip(request: HttpRequest) -> HttpResponse:
    """تنزيل أرشيف ZIP يحوي ملفات المدرسة الفعلية + ملف Excel فهرس."""
    job_id = request.GET.get("job")
    if job_id:
        return generated_export_job_response(
            request,
            job_id=job_id,
            fallback_url=reverse("reports:school_data_export"),
        )

    active_school, redirect_resp = _require_manager_school(request)
    if redirect_resp is not None:
        return redirect_resp

    if async_exports_enabled():
        job, created = enqueue_generated_export(
            school=active_school,
            requested_by=request.user,
            kind=GeneratedExportJob.Kind.SCHOOL_ZIP,
        )
        if created:
            messages.info(request, "بدأ تجهيز ملف ZIP في الخلفية.")
        return redirect(f"{reverse('reports:school_data_export_zip')}?job={job.pk}")

    try:
        zip_file = build_school_export_zip_file(active_school, request=request)
    except Exception:
        logger.exception("school_data_export_zip failed school_id=%s", getattr(active_school, "id", None))
        messages.error(request, "تعذّر إنشاء أرشيف التصدير. حاول لاحقًا.")
        return redirect("reports:school_data_export")

    logger.info(
        "School files exported (zip) school_id=%s user_id=%s",
        getattr(active_school, "id", None),
        getattr(request.user, "id", None),
    )
    # FileResponse يبثّ الملف ويغلقه تلقائيًا (يدعم الأرشيفات الكبيرة المسكوبة للقرص)
    return FileResponse(
        zip_file,
        as_attachment=True,
        filename=export_zip_filename(active_school),
        content_type="application/zip",
    )
