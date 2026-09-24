"""Platform-owner controls for independent teacher subscription plans."""

import uuid

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from .forms import PersonalPlanForm
from .models import PersonalPlan


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
