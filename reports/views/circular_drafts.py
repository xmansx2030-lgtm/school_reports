# -*- coding: utf-8 -*-
"""مسودات التعاميم.

يُعدّها الموظف الإداري أو الوكيل بصلاحية ``draft_circulars``، ويعتمدها مدير
المدرسة — **واعتمادُه هو نشرُها**. لا خطوة ثالثة بعده، لأن مسودةً معتمَدة لم
تُنشر تترك الجميع يظنها وصلت.
"""
from __future__ import annotations

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from .. import capabilities as caps
from ..model_parts.approvals import ApprovalState, PENDING_REVIEW_STATES
from ..models import CircularDraft, Department
from ..permissions import capability_source, is_school_manager
from ..services_approval import (
    ACTION_DISPATCH,
    ApprovalError,
    available_actions,
    transitions_for,
)
from ..services_circular_drafts import draft_recipients, publish_draft
from ._helpers import *  # noqa: F401,F403
from ._helpers import active_school_or_redirect as _school_or_redirect

__all__ = ["circular_draft_list", "circular_draft_detail", "circular_draft_action"]


class CircularDraftForm(forms.ModelForm):
    class Meta:
        model = CircularDraft
        fields = ("title", "body", "audience", "department", "requires_signature", "signature_deadline_at")
        widgets = {
            "title": forms.TextInput(attrs={"placeholder": "عنوان التعميم"}),
            "body": forms.Textarea(attrs={"rows": 8, "placeholder": "نص التعميم…"}),
            "signature_deadline_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
        }

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["department"].queryset = (
            Department.objects.filter(school=school, is_active=True).order_by("name")
            if school is not None
            else Department.objects.none()
        )
        self.fields["department"].required = False
        self.fields["signature_deadline_at"].required = False
        self.fields["signature_deadline_at"].help_text = (
            "اختياري — يظهر للمستلمين في صفحة التوقيع ويُستعمل في تقرير الاطّلاع."
        )


def _may_draft(user, school) -> bool:
    if is_school_manager(user, active_school=school):
        return True
    return capability_source(user, caps.DRAFT_CIRCULARS, school) is not None


def _visible_drafts(user, school):
    """المدير يرى الكل، وغيره يرى مسوداته.

    مسودةٌ لم تُعتمد بعدُ ورقةٌ تُتداول بين صاحبها والمعتمِد — وعرضُها على
    زملائه يجعل ما لم يُقرَّر يبدو مقرَّراً.
    """
    base = CircularDraft.objects.filter(school=school).select_related("owner", "department")
    if is_school_manager(user, active_school=school):
        return base
    return base.filter(owner=user)


@login_required(login_url="reports:login")
@require_http_methods(["GET", "POST"])
def circular_draft_list(request):
    school, redirect_response = _school_or_redirect(request)
    if redirect_response is not None:
        return redirect_response

    if not _may_draft(request.user, school):
        messages.error(request, "لا تملك صلاحية إعداد مسودات التعاميم.")
        return redirect("reports:home")

    form = CircularDraftForm(school=school)
    if request.method == "POST":
        form = CircularDraftForm(request.POST, school=school)
        if form.is_valid():
            draft = form.save(commit=False)
            draft.school = school
            draft.owner = request.user
            draft.save()
            messages.success(request, "حُفظت المسودة. أرسلها للاعتماد حين تكتمل.")
            return redirect("reports:circular_draft_detail", pk=draft.pk)
        messages.error(request, "تعذّر حفظ المسودة — تحقّق من الحقول.")

    drafts = list(_visible_drafts(request.user, school)[:100])
    is_manager = is_school_manager(request.user, active_school=school)

    return render(
        request,
        "reports/circular_draft_list.html",
        {
            "active": "circular_draft_list",
            "active_school": school,
            "form": form,
            "drafts": drafts,
            "is_manager": is_manager,
            "awaiting": sum(
                1 for item in drafts if item.approval_state in PENDING_REVIEW_STATES
            ),
            "published": sum(1 for item in drafts if item.is_published),
        },
    )


def _draft_for(request, pk: int, school) -> CircularDraft:
    draft = get_object_or_404(CircularDraft, pk=pk, school=school)
    if not _visible_drafts(request.user, school).filter(pk=pk).exists():
        raise Http404
    return draft


@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def circular_draft_detail(request, pk: int):
    school, redirect_response = _school_or_redirect(request)
    if redirect_response is not None:
        return redirect_response

    draft = _draft_for(request, pk, school)

    return render(
        request,
        "reports/circular_draft_detail.html",
        {
            "active": "circular_draft_list",
            "active_school": school,
            "draft": draft,
            "is_owner": draft.owner_id == request.user.pk,
            "actions": available_actions(draft, request.user, school=school),
            "timeline": list(transitions_for(draft)),
            "recipient_count": len(draft_recipients(draft)),
        },
    )


@login_required(login_url="reports:login")
@require_http_methods(["POST"])
def circular_draft_action(request, pk: int):
    """دورة اعتماد المسودة — والاعتماد يَنشُر."""
    school, redirect_response = _school_or_redirect(request)
    if redirect_response is not None:
        return redirect_response

    draft = _draft_for(request, pk, school)
    action = (request.POST.get("approval_action") or "").strip()
    note = (request.POST.get("note") or "").strip()

    handler = ACTION_DISPATCH.get(action)
    if handler is None or action not in available_actions(draft, request.user, school=school):
        messages.error(request, "هذا الإجراء غير متاح على هذه المسودة الآن.")
        return redirect("reports:circular_draft_detail", pk=pk)

    try:
        handler(draft, request.user, school=school, note=note)
    except PermissionDenied as exc:
        messages.error(request, str(exc) or "لا تملك هذا الإجراء.")
    except (ApprovalError, ValidationError) as exc:
        detail = getattr(exc, "messages", None) or [str(exc)]
        messages.error(request, detail[0])
    else:
        if draft.approval_state == ApprovalState.APPROVED and not draft.is_published:
            notification = publish_draft(draft, request.user)
            messages.success(
                request,
                f"اعتُمد التعميم ونُشر على {notification.recipients.count()} من المنسوبين.",
            )
        else:
            messages.success(
                request,
                {
                    "submit": "أُرسلت المسودة لاعتماد المدير.",
                    "withdraw": "سُحبت المسودة للتعديل.",
                    "start_review": "بدأت مراجعة المسودة.",
                    "request_info": "طُلب استكمال من مُعِدّ المسودة.",
                    "return": "أُعيدت المسودة لمُعِدّها مع ملاحظتك.",
                }.get(action, "نُفِّذ الإجراء."),
            )

    return redirect("reports:circular_draft_detail", pk=pk)
