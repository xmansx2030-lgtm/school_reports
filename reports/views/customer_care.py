from __future__ import annotations

from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from core.client_ip import client_ip

from ..customer_care_forms import CustomerComplaintUpdateForm
from ..models import AuditLog, CustomerComplaint
from ..view_access import platform_superuser_required


@platform_superuser_required
@require_http_methods(["GET"])
def platform_complaints_list(request: HttpRequest) -> HttpResponse:
    query = (request.GET.get("q") or "").strip()
    status_filter = (request.GET.get("status") or "all").strip()
    allowed_statuses = {value for value, _label in CustomerComplaint.Status.choices}
    if status_filter not in allowed_statuses | {"all"}:
        status_filter = "all"

    scope = CustomerComplaint.objects.all()
    if query:
        scope = scope.filter(
            Q(name__icontains=query)
            | Q(email__icontains=query)
            | Q(phone__icontains=query)
            | Q(order_reference__icontains=query)
            | Q(subject__icontains=query)
            | Q(message__icontains=query)
        )

    counts = scope.aggregate(
        all=Count("id"),
        new=Count("id", filter=Q(status=CustomerComplaint.Status.NEW)),
        in_progress=Count(
            "id", filter=Q(status=CustomerComplaint.Status.IN_PROGRESS)
        ),
        resolved=Count("id", filter=Q(status=CustomerComplaint.Status.RESOLVED)),
        closed=Count("id", filter=Q(status=CustomerComplaint.Status.CLOSED)),
    )

    complaints = scope
    if status_filter != "all":
        complaints = complaints.filter(status=status_filter)

    page_obj = Paginator(complaints.order_by("-created_at", "-id"), 30).get_page(
        request.GET.get("page")
    )
    return render(
        request,
        "reports/platform_complaints.html",
        {
            "complaints": page_obj,
            "page_obj": page_obj,
            "search_query": query,
            "current_status": status_filter,
            "tab_counts": counts,
        },
    )


@platform_superuser_required
@require_http_methods(["GET", "POST"])
def platform_complaint_detail(request: HttpRequest, pk: int) -> HttpResponse:
    complaint = get_object_or_404(CustomerComplaint, pk=pk)

    if request.method == "POST":
        with transaction.atomic():
            complaint = get_object_or_404(
                CustomerComplaint.objects.select_for_update(), pk=pk
            )
            previous_status = complaint.status
            previous_notes = complaint.internal_notes
            form = CustomerComplaintUpdateForm(request.POST, instance=complaint)
            if form.is_valid():
                updated = form.save(commit=False)
                if updated.status == CustomerComplaint.Status.RESOLVED:
                    updated.resolved_at = updated.resolved_at or timezone.now()
                elif updated.status in {
                    CustomerComplaint.Status.NEW,
                    CustomerComplaint.Status.IN_PROGRESS,
                }:
                    updated.resolved_at = None
                elif (
                    updated.status == CustomerComplaint.Status.CLOSED
                    and updated.resolved_at is None
                ):
                    updated.resolved_at = timezone.now()
                updated.save(
                    update_fields=(
                        "status",
                        "internal_notes",
                        "resolved_at",
                        "updated_at",
                    )
                )

                changes = {
                    "status": {
                        "from": previous_status,
                        "to": updated.status,
                    },
                    "internal_notes_updated": previous_notes != updated.internal_notes,
                }
                AuditLog.objects.create(
                    teacher=request.user,
                    action=AuditLog.Action.UPDATE,
                    model_name="CustomerComplaint",
                    object_id=updated.pk,
                    object_repr=f"{updated.reference} — {updated.subject}"[:255],
                    changes=changes,
                    ip_address=client_ip(request),
                    user_agent=(request.META.get("HTTP_USER_AGENT") or "")[:500],
                )
                messages.success(
                    request,
                    f"تم تحديث الشكوى {updated.reference} وتسجيل الإجراء.",
                )
                return redirect("reports:platform_complaint_detail", pk=updated.pk)
    else:
        form = CustomerComplaintUpdateForm(instance=complaint)

    audit_rows = (
        AuditLog.objects.filter(
            model_name="CustomerComplaint",
            object_id=complaint.pk,
        )
        .select_related("teacher")
        .order_by("-timestamp")[:50]
    )
    status_labels = dict(CustomerComplaint.Status.choices)
    activity = []
    for row in audit_rows:
        changes = row.changes or {}
        status_change = changes.get("status") or {}
        activity.append(
            {
                "timestamp": row.timestamp,
                "actor": getattr(row.teacher, "name", "") or str(row.teacher or "النظام"),
                "from_status": status_labels.get(
                    status_change.get("from"), status_change.get("from") or "—"
                ),
                "to_status": status_labels.get(
                    status_change.get("to"), status_change.get("to") or "—"
                ),
                "notes_updated": bool(changes.get("internal_notes_updated")),
            }
        )
    return render(
        request,
        "reports/platform_complaint_detail.html",
        {
            "complaint": complaint,
            "form": form,
            "activity": activity,
        },
    )
