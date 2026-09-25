"""Creation quotas for a personal subscription; deleting content never refunds them."""

from django.core.exceptions import ValidationError
from django.db import transaction

from .models import PersonalSubscription


def personal_quota_usage(workspace, *, lock=False):
    """Include surviving legacy rows alongside the durable creation counters."""
    subscriptions = PersonalSubscription.objects.select_related("plan")
    if lock:
        subscriptions = subscriptions.select_for_update()
    subscription = subscriptions.get(workspace=workspace)
    reports = max(
        subscription.reports_created,
        workspace.reports.filter(pk__gt=subscription.quota_report_after_id).count(),
    )
    evidence = max(
        subscription.evidence_created,
        workspace.evidence.filter(pk__gt=subscription.quota_evidence_after_id).count(),
    )
    return subscription, reports, evidence


def reserve_personal_quota(workspace, *, reports=0, evidence=0):
    """Reserve creation slots inside the caller's write transaction."""
    if reports < 0 or evidence < 0:
        raise ValueError("Quota reservations cannot be negative.")
    with transaction.atomic():
        workspace.__class__.objects.select_for_update().get(pk=workspace.pk)
        subscription, reports_used, evidence_used = personal_quota_usage(workspace, lock=True)
        if not subscription.is_current:
            raise ValidationError("اشتراك المساحة الشخصية غير نشط حاليًا.")
        if reports and reports_used + reports > subscription.plan.max_reports:
            raise ValidationError("وصلت إلى حد إنشاء التقارير خلال اشتراكك الحالي.")
        if evidence and evidence_used + evidence > subscription.plan.max_evidence:
            raise ValidationError("وصلت إلى حد إنشاء الشواهد خلال اشتراكك الحالي.")
        if (reports or evidence or subscription.reports_created != reports_used
                or subscription.evidence_created != evidence_used):
            subscription.reports_created = reports_used + reports
            subscription.evidence_created = evidence_used + evidence
            subscription.save(update_fields=["reports_created", "evidence_created"])
    return subscription
