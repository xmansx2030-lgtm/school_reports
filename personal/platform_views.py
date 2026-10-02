"""Platform-owner views for independent teacher plans and subscriptions."""

import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods
from django.utils import timezone

from reports.models import AiUsageEvent

from .assistant_views import personal_assistant_template_context
from .forms import PersonalPlanForm
from .models import PersonalPlan, PersonalSubscription
from .quotas import personal_quota_usage


def _current_subscription_query():
    today = timezone.localdate()
    return Q(is_active=True, start_date__lte=today) & (Q(end_date__isnull=True) | Q(end_date__gte=today))


@never_cache
@login_required(login_url="reports:platform_login")
@user_passes_test(lambda user: user.is_superuser, login_url="reports:platform_login")
@require_http_methods(["GET"])
def subscriber_list(request):
    subscriptions = PersonalSubscription.objects.select_related("workspace__owner", "plan")
    stats = subscriptions.aggregate(total=Count("pk"), active=Count("pk", filter=_current_subscription_query()))
    q = (request.GET.get("q") or "").strip()[:150]
    status = request.GET.get("status", "")
    plan_id = request.GET.get("plan", "")
    if q:
        subscriptions = subscriptions.filter(
            Q(workspace__owner__name__icontains=q) | Q(workspace__owner__phone__icontains=q)
            | Q(workspace__owner__email__icontains=q) | Q(workspace__school_name__icontains=q)
        )
    if status == "active":
        subscriptions = subscriptions.filter(_current_subscription_query())
    elif status == "inactive":
        subscriptions = subscriptions.exclude(_current_subscription_query())
    else:
        status = ""
    if plan_id.isascii() and plan_id.isdigit() and len(plan_id) < 10:
        subscriptions = subscriptions.filter(plan_id=int(plan_id))
    else:
        plan_id = ""
    page = Paginator(subscriptions.order_by("-workspace__created_at", "-pk"), 30).get_page(request.GET.get("page"))
    return render(request, "personal/platform_subscribers.html", {
        "subscriptions": page, "page_obj": page, "stats": stats, "q": q,
        "status": status, "plan_id": plan_id,
        "today": timezone.localdate(),
        "plans": PersonalPlan.objects.order_by("display_order", "name"),
    })


@never_cache
@login_required(login_url="reports:platform_login")
@user_passes_test(lambda user: user.is_superuser, login_url="reports:platform_login")
@require_http_methods(["GET"])
def subscriber_detail(request, pk):
    subscription = get_object_or_404(
        PersonalSubscription.objects.select_related("workspace__owner", "plan"), workspace_id=pk,
    )
    workspace = subscription.workspace
    _current, report_count, evidence_count = personal_quota_usage(workspace)
    since = timezone.now() - timedelta(days=30)
    # Report calls recorded without a school for this account. Keep calls
    # attributed to a school and public Mansour conversations separate.
    events = AiUsageEvent.objects.filter(
        teacher_id=workspace.owner_id, school__isnull=True, created_at__gte=since,
        stage__in=[AiUsageEvent.Stage.REPORT_IMPROVE, AiUsageEvent.Stage.TRANSCRIPTION, AiUsageEvent.Stage.VOICE_POLISH],
    )
    ai_summary = events.aggregate(
        calls=Count("pk"), failed=Count("pk", filter=Q(outcome=AiUsageEvent.Outcome.FAILED)),
        input_tokens=Sum("input_tokens"), output_tokens=Sum("output_tokens"),
        cost=Sum("estimated_cost"), unpriced=Count("pk", filter=Q(estimated_cost__isnull=True)),
    )
    return render(request, "personal/platform_subscriber_detail.html", {
        "workspace": workspace, "subscription": subscription,
        "report_count": report_count, "evidence_count": evidence_count,
        "storage_used_mb": (workspace.evidence.aggregate(total=Sum("file_size"))["total"] or 0) / (1024 * 1024),
        "payments": workspace.payments.all()[:30], "ai_summary": ai_summary,
        **personal_assistant_template_context(workspace.owner, subscription),
    })


@never_cache
@login_required(login_url="reports:platform_login")
@user_passes_test(lambda user: user.is_superuser, login_url="reports:platform_login")
@require_http_methods(["GET"])
def plan_list(request):
    plans = list(
        PersonalPlan.objects.annotate(subscriber_count=Count("personalsubscription"))
        .order_by("display_order", "price", "id")
    )
    return render(request, "personal/platform_plans.html", {
        "plans": plans,
        "published_count": sum(plan.is_active and plan.is_published for plan in plans),
        "subscriber_count": sum(plan.subscriber_count for plan in plans),
        "moyasar_enabled": bool(getattr(settings, "MOYASAR_ENABLED", False)),
        "activation_email_enabled": bool(getattr(settings, "SUBSCRIPTION_ACTIVATION_EMAIL_ENABLED", True)),
    })


@never_cache
@login_required(login_url="reports:platform_login")
@user_passes_test(lambda user: user.is_superuser, login_url="reports:platform_login")
@require_http_methods(["GET", "POST"])
def plan_form(request, pk=None):
    plan = get_object_or_404(PersonalPlan, pk=pk) if pk is not None else None
    form = PersonalPlanForm(request.POST or None, instance=plan)
    if request.method == "POST":
        if form.is_valid():
            saved = form.save(commit=False)
            if saved.pk is None:
                saved.code = f"teacher-{uuid.uuid4().hex}"
            saved.save()
            messages.success(request, "حُفظت باقة المعلمين والمعلمات، وحُدّث عرضها في صفحة الهبوط.")
            return redirect("reports:platform_personal_plans")
        for name in form.errors:
            if name in form.fields:
                attrs = form.fields[name].widget.attrs
                attrs["aria-invalid"] = "true"
                attrs["aria-describedby"] = " ".join(filter(None, [
                    attrs.get("aria-describedby"), f"id_{name}_errors",
                ]))
    return render(request, "personal/platform_plan_form.html", {"form": form, "plan": plan})
