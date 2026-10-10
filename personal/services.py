from django.db import transaction
from django.core.exceptions import ValidationError
from django.conf import settings
from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone

from core.observability import soft_call
from reports.models import SchoolMembership
from reports.academic_years import approved_academic_year_options, current_academic_year_value, published_academic_years

from .models import PersonalAcademicYear, PersonalPlan, PersonalSubscription, PersonalWorkspace


LANDING_PERSONAL_PLAN_CACHE_KEY = "landing:personal-plans:v2"


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


def personal_year_values(workspace):
    """Published calendar years plus this workspace's preserved historical data."""
    values = set(approved_academic_year_options())
    for relation, field in (
        ("academic_years", "value"), ("reports", "academic_year"),
        ("evidence", "academic_year"), ("initiatives", "academic_year"),
        ("portfolio_sections", "academic_year"), ("courses", "academic_year"),
    ):
        manager = getattr(workspace, relation, None)
        if manager is not None:
            values.update(manager.values_list(field, flat=True).distinct())
    if workspace.current_academic_year:
        values.add(workspace.current_academic_year)
    return sorted(filter(None, values), reverse=True)


def personal_year_choices(workspace):
    return [(value, value) for value in personal_year_values(workspace)]


def selected_personal_year(request):
    """Persist a browsing choice per workspace without changing saved work or quotas."""
    workspace = request.personal_workspace
    values = personal_year_values(workspace)
    key = f"personal_academic_year:{workspace.pk}"
    explicit = ((request.POST.get("year") if request.method == "POST" else request.GET.get("year")) or "").strip()
    if explicit:
        if explicit not in values:
            from django.http import Http404
            raise Http404("السنة الدراسية غير متاحة في هذه المساحة.")
        request.session[key] = explicit
        return explicit
    selected = request.session.get(key)
    if selected in values:
        return selected
    current = current_academic_year_value()
    return current if current in values else (values[0] if values else "")


def personal_year_is_writable(workspace, value):
    return bool(value and value in approved_academic_year_options() and not workspace.academic_years.filter(
        value=value, archived_at__isnull=False,
    ).exists())


def personal_read_only_years(workspace):
    return (set(personal_year_values(workspace)) - set(approved_academic_year_options())) | set(
        workspace.academic_years.filter(archived_at__isnull=False).values_list("value", flat=True)
    )


def personal_evidence_for_year(workspace, value):
    """Hide inconsistent legacy links without altering the preserved source rows."""
    return workspace.evidence.filter(academic_year=value).filter(
        Q(report__isnull=True) | Q(report__workspace=workspace, report__academic_year=value),
    ).filter(
        Q(initiative__isnull=True) | Q(initiative__workspace=workspace, initiative__academic_year=value),
    )


def personal_year_context(request):
    workspace = getattr(request, "personal_workspace", None)
    if workspace is None:
        return {}
    selected = getattr(request, "personal_academic_year", None)
    if selected is None:
        # Error pages also run context processors. Do not re-raise an invalid
        # URL's Http404 while Django is rendering its 404 response.
        return {}
    current = current_academic_year_value()
    approved = set(approved_academic_year_options())
    central = {row.value: row for row in published_academic_years() if row.value in approved}
    owned = {row.value: row for row in workspace.academic_years.all()}
    options = []
    for value in personal_year_values(workspace):
        calendar, record = central.get(value), owned.get(value)
        options.append({
            "value": value, "label": value, "is_current": value == current,
            "is_selected": value == selected, "is_historical": calendar is None,
            "is_legacy": calendar is None, "is_archived": bool(record and record.archived_at),
            "start_date": getattr(calendar, "start_date", None),
            "end_date": getattr(calendar, "end_date", None),
            "source_url": getattr(calendar, "source_url", ""),
        })
    writable = bool(selected in approved and not (owned.get(selected) and owned[selected].archived_at))
    return {
        "personal_selected_year": selected, "selected_year": selected,
        "personal_official_year": current, "central_current_year": current,
        "personal_year_options": options, "academic_year_options": options,
        "selected_year_record": owned.get(selected),
        "personal_year_read_only": not writable,
        "personal_year_can_write": writable,
        "selected_year_start_date": getattr(central.get(selected), "start_date", None),
        "selected_year_end_date": getattr(central.get(selected), "end_date", None),
        "selected_year_source_url": getattr(central.get(selected), "source_url", ""),
    }


def ensure_writable_personal_year(workspace, value):
    """Only platform-published calendar years can receive new or edited work."""
    if value not in approved_academic_year_options():
        raise ValidationError("هذه السنة محفوظة للقراءة فقط أو لم تعتمدها إدارة المنصة بعد.")
    locked = PersonalWorkspace.objects.select_for_update().get(pk=workspace.pk)
    year, _ = PersonalAcademicYear.objects.get_or_create(workspace=locked, value=value)
    if year.archived_at is not None:
        raise ValidationError("هذه السنة مؤرشفة ومتاحة للقراءة والتصدير فقط.")
    return year


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
            "is_basic": plan.code == "personal_free",
            "duration_label": "مستمرة" if plan.duration_days == 0 else f"{plan.duration_days} يومًا",
            "max_reports": plan.max_reports,
            "max_evidence": plan.max_evidence,
            "storage_limit_mb": plan.storage_limit_mb,
            "report_ai_daily_limit": plan.report_ai_daily_limit,
            "voice_report_daily_limit": plan.voice_report_daily_limit,
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
    """Initialize personal billing without granting a paid or unavailable plan."""
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
            workspace=workspace,
            defaults={
                "plan": plan,
                "is_active": plan.is_active and plan.is_published and plan.price == 0,
            },
        )
    return subscription
