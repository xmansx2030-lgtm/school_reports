from django.db import transaction
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from core.observability import soft_call
from reports.models import SchoolMembership

from .models import PersonalPlan, PersonalSubscription


LANDING_PERSONAL_PLAN_CACHE_KEY = "landing:personal-plans:v1"


def current_school_membership_for(user):
    """Return a staff membership backed by a currently subscribed school."""
    if not getattr(user, "is_authenticated", False):
        return None
    memberships = (
        SchoolMembership.objects.filter(
            teacher=user,
            is_active=True,
            school__is_active=True,
            role_type__in=SchoolMembership.STAFF_ROLES,
            school__subscription__is_active=True,
            school__subscription__end_date__gte=timezone.localdate(),
        )
        .select_related("school", "school__subscription")
        .order_by("id")
    )
    return memberships.first()


def landing_personal_plan_cards():
    """Public personal pricing, independent of the school's pricing catalog."""
    try:
        ttl = max(0, int(getattr(settings, "LANDING_PRICING_CACHE_TTL_SECONDS", 60) or 0))
    except (TypeError, ValueError):
        ttl = 60
    if ttl:
        cached = soft_call(
            "landing.personal_plan_cache_get",
            lambda: cache.get(LANDING_PERSONAL_PLAN_CACHE_KEY),
            default=None,
        )
        if cached is not None:
            return cached

    plans = PersonalPlan.objects.filter(is_active=True, is_published=True).order_by(
        "display_order", "price", "id"
    )
    cards = [
        {
            "id": plan.pk,
            "name": plan.name,
            "description": plan.description,
            "price_display": f"{plan.price:,.2f}".rstrip("0").rstrip("."),
            "is_free": plan.price == 0,
            "duration_label": "مستمرة" if plan.duration_days == 0 else f"{plan.duration_days} يومًا",
            "max_reports": plan.max_reports,
            "max_evidence": plan.max_evidence,
            "storage_limit_mb": plan.storage_limit_mb,
        }
        for plan in plans
    ]
    if ttl:
        soft_call(
            "landing.personal_plan_cache_set",
            lambda: cache.set(LANDING_PERSONAL_PLAN_CACHE_KEY, cards, ttl),
            default=None,
        )
    return cards


def ensure_personal_subscription(workspace):
    """Give a workspace the perpetual free tier; never use a school plan."""
    subscription = PersonalSubscription.objects.select_related("plan").filter(workspace=workspace).first()
    if subscription is not None:
        return subscription
    with transaction.atomic():
        plan, _ = PersonalPlan.objects.get_or_create(
            code="personal_free",
            defaults={
                "name": "المساحة الشخصية الأساسية",
                "price": 0,
                "max_reports": 500,
                "max_evidence": 250,
                "storage_limit_mb": 500,
            },
        )
        subscription, _ = PersonalSubscription.objects.get_or_create(
            workspace=workspace, defaults={"plan": plan}
        )
    return subscription
