# -*- coding: utf-8 -*-
"""أرشيف الوثائق: الرفع والتصنيف والبحث واعتماد النقل.

شاشة واحدة تجمع الأربعة. تفريقُها كان سيجعل رفع وثيقة واحدة رحلةً بين صفحات،
وهي عمل الموظف الإداري اليومي لا مشروعاً يُخطَّط له.

**الأرشفة قرار لا فعل رفع**: الوثيقة تُرفع مسودةً، ثم تُرسَل، ثم يُعتمد نقلها.
وسير الاعتماد يمر بالمكوّن المشترك — فما اعتُمد لا يُعدَّل ولا يُحذف بلا سطر
جديد يفرض ذلك.
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from .. import capabilities as caps
from ..forms_documents import DocumentUploadForm
from ..model_parts.approvals import ApprovalState, PENDING_REVIEW_STATES
from ..models import Department, Document
from ..permissions import capability_source, is_school_manager
from ..services_approval import (
    ACTION_DISPATCH,
    ApprovalError,
    available_actions,
    transitions_for,
)
from ..services_archive import archive_storage_capacity_error
from ..services_documents import (
    apply_document_filters,
    document_facets,
    visible_documents,
)
from ._helpers import *  # noqa: F401,F403
from ._helpers import _clean_query_params, active_school_or_redirect as _school_or_redirect

__all__ = ["document_archive", "document_detail", "document_action"]

PAGE_SIZE = 24


@login_required(login_url="reports:login")
@require_http_methods(["GET", "POST"])
def document_archive(request):
    """أرشيف الوثائق — الرفع والبحث في شاشة واحدة."""
    school, redirect_response = _school_or_redirect(request)
    if redirect_response is not None:
        return redirect_response

    form = DocumentUploadForm(school=school, uploader=request.user)

    if request.method == "POST":
        form = DocumentUploadForm(
            request.POST, request.FILES, school=school, uploader=request.user
        )
        if form.is_valid():
            capacity_error = archive_storage_capacity_error(
                school, [form.cleaned_data.get("file")]
            )
            if capacity_error:
                # سعةٌ ممتلئة تُقال قبل الرفع لا بعده: رفعٌ يفشل صامتاً يجعل
                # الموظف يظن وثيقته محفوظة.
                messages.error(request, capacity_error)
            else:
                document = form.save(commit=False)
                document.school = school
                document.owner = request.user
                document.uploaded_by = request.user
                document.save()
                messages.success(
                    request, "رُفعت الوثيقة كمسودة. أرسلها للأرشفة حين تكتمل بياناتها."
                )
                return redirect("reports:document_detail", pk=document.pk)
        else:
            messages.error(request, "تعذّر رفع الوثيقة — تحقّق من الحقول.")

    scoped = visible_documents(request.user, school)
    facets = document_facets(scoped)

    year = (request.GET.get("year") or "").strip()
    kind = (request.GET.get("kind") or "").strip()
    department = (request.GET.get("department") or "").strip()
    term = (request.GET.get("q") or "").strip()

    results = apply_document_filters(
        scoped, year=year, kind=kind, department=department, term=term
    )
    page = Paginator(results, PAGE_SIZE).get_page(request.GET.get("page"))

    may_review = is_school_manager(request.user, active_school=school) or (
        capability_source(request.user, caps.ARCHIVE_DOCUMENTS, school) is not None
    )
    pending = (
        list(scoped.filter(approval_state__in=PENDING_REVIEW_STATES)[:20])
        if may_review
        else []
    )

    return render(
        request,
        "reports/document_archive.html",
        {
            "active": "document_archive",
            "active_school": school,
            "form": form,
            "page_obj": page,
            "pending": pending,
            "may_review": may_review,
            "kinds": Document.Kind.choices,
            "years": facets["years"],
            "departments": Department.objects.filter(school=school, is_active=True).order_by("name"),
            "q_year": year,
            "q_kind": kind,
            "q_department": department,
            "q_term": term,
            "has_filters": bool(year or kind or department or term),
            "qs": _clean_query_params(request.GET),
            "archived_count": scoped.filter(approval_state=ApprovalState.APPROVED).count(),
        },
    )


def _document_for(request, pk: int, school) -> Document:
    """الوثيقة إن كانت ضمن ما يراه هذا المستخدم — وإلا فغير موجودة."""
    document = get_object_or_404(
        Document.objects.select_related("owner", "department", "school"), pk=pk
    )
    if document.school_id != getattr(school, "pk", None):
        raise Http404
    if not visible_documents(request.user, school).filter(pk=pk).exists():
        raise Http404
    return document


@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def document_detail(request, pk: int):
    school, redirect_response = _school_or_redirect(request)
    if redirect_response is not None:
        return redirect_response

    document = _document_for(request, pk, school)

    return render(
        request,
        "reports/document_detail.html",
        {
            "active": "document_archive",
            "active_school": school,
            "document": document,
            "is_owner": document.owner_id == request.user.pk,
            "actions": available_actions(document, request.user, school=school),
            "timeline": list(transitions_for(document)),
        },
    )


@login_required(login_url="reports:login")
@require_http_methods(["POST"])
def document_action(request, pk: int):
    """دورة اعتماد نقل الوثيقة إلى الأرشيف."""
    school, redirect_response = _school_or_redirect(request)
    if redirect_response is not None:
        return redirect_response

    document = _document_for(request, pk, school)
    action = (request.POST.get("approval_action") or "").strip()
    note = (request.POST.get("note") or "").strip()

    handler = ACTION_DISPATCH.get(action)
    if handler is None or action not in available_actions(
        document, request.user, school=school
    ):
        messages.error(request, "هذا الإجراء غير متاح على هذه الوثيقة الآن.")
        return redirect("reports:document_detail", pk=pk)

    try:
        handler(document, request.user, school=school, note=note)
    except PermissionDenied as exc:
        messages.error(request, str(exc) or "لا تملك هذا الإجراء.")
    except (ApprovalError, ValidationError) as exc:
        detail = getattr(exc, "messages", None) or [str(exc)]
        messages.error(request, detail[0])
    else:
        messages.success(
            request,
            {
                "submit": "أُرسلت الوثيقة لاعتماد أرشفتها.",
                "issue": "أُصدرت الوثيقة ونُقلت إلى الأرشيف المعتمَد.",
                "withdraw": "سُحبت الوثيقة للتعديل.",
                "start_review": "بدأت مراجعة الوثيقة.",
                "request_info": "طُلب استكمال بيانات الوثيقة.",
                "return": "أُعيدت الوثيقة لرافعها مع ملاحظتك.",
                "approve": "اعتُمد نقل الوثيقة إلى الأرشيف.",
            }.get(action, "نُفِّذ الإجراء."),
        )

    return redirect("reports:document_detail", pk=pk)
