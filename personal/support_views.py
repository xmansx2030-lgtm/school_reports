"""Back-office maintenance screens, always authorized by the real platform owner."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from reports.models import AuditLog
from .models import PersonalSubscription, PersonalWorkspace
from .services import ensure_personal_subscription
from .support_forms import PersonalMaintenanceSubscriptionForm


@never_cache
@login_required(login_url="reports:platform_login")
@require_http_methods(["GET", "POST"])
def subscription_settings(request, pk):
    if not request.user.is_active or not request.user.is_superuser:
        raise PermissionDenied
    workspace = get_object_or_404(PersonalWorkspace.objects.select_related("owner"), pk=pk)
    subscription = ensure_personal_subscription(workspace)
    if request.method == "POST":
        with transaction.atomic():
            subscription = PersonalSubscription.objects.select_for_update().get(pk=subscription.pk)
            before = {"plan_id": subscription.plan_id, "is_active": subscription.is_active, "end_date": str(subscription.end_date)}
            form = PersonalMaintenanceSubscriptionForm(request.POST, instance=subscription)
            if form.is_valid():
                result = form.save()
                AuditLog.objects.create(
                    teacher=request.user, actor_name=request.user.name, actor_role="مالك المنصة",
                    action=AuditLog.Action.UPDATE, model_name="PersonalSubscription", object_id=result.pk,
                    object_repr=workspace.owner.name,
                    changes={"workspace_id": workspace.pk, "before": before, "after": {
                        "plan_id": result.plan_id, "is_active": result.is_active, "end_date": str(result.end_date),
                    }},
                    ip_address=request.META.get("REMOTE_ADDR"),
                    user_agent=request.META.get("HTTP_USER_AGENT", "")[:500],
                )
                messages.success(request, "حُفظت إعدادات الاشتراك وسُجّل التعديل الإداري.")
                return redirect("personal:platform_subscriber_detail", pk=workspace.pk)
    else:
        form = PersonalMaintenanceSubscriptionForm(instance=subscription)
    return render(request, "personal/platform_subscription_settings.html", {
        "workspace": workspace, "subscription": subscription, "form": form,
    })
