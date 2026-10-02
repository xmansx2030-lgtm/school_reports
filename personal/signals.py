from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver
from django.core.cache import cache

from core.observability import soft_call

from .models import (
    PersonalAcademicYear, PersonalEvidence, PersonalInitiative, PersonalPlan,
    PersonalPortfolioSection, PersonalPortfolioReport, PersonalPortfolioEvidence,
    PersonalReport, PersonalShareLink, PersonalSubscription, PersonalWorkspace,
)
from .services import LANDING_PERSONAL_PLAN_CACHE_KEY


@receiver(post_save, sender=PersonalPlan)
@receiver(post_delete, sender=PersonalPlan)
def refresh_public_personal_plans(sender, **kwargs):
    transaction.on_commit(lambda: soft_call(
        "landing.personal_plan_cache_delete",
        lambda: cache.delete(LANDING_PERSONAL_PLAN_CACHE_KEY),
        default=None,
    ))


@receiver(post_delete, sender=PersonalEvidence)
def remove_personal_evidence_file(sender, instance, **kwargs):
    if instance.file:
        name = instance.file.name
        storage = instance.file.storage
        transaction.on_commit(lambda: storage.delete(name))


def audit_personal_maintenance(sender, instance, **kwargs):
    """Record the true administrator, without retaining private field contents."""
    from reports.middleware import get_current_request
    from reports.models import AuditLog

    request = get_current_request()
    scope = getattr(request, "support_scope", None)
    if not scope or scope["kind"] != "personal":
        return
    workspace_id = instance.pk if sender is PersonalWorkspace else getattr(instance, "workspace_id", None)
    if workspace_id is None:
        section = getattr(instance, "section", None)
        if section is not None:
            workspace_id = section.workspace_id
    if workspace_id != scope["target_id"]:
        return
    actor = request.support_actor
    action = AuditLog.Action.DELETE if kwargs.get("signal") is post_delete else (
        AuditLog.Action.CREATE if kwargs.get("created") else AuditLog.Action.UPDATE
    )
    AuditLog.objects.create(
        teacher=actor, actor_name=actor.name, actor_role="مالك المنصة · صيانة",
        action=action, model_name=sender.__name__, object_id=instance.pk,
        object_repr=scope["label"][:255],
        changes={"support": {"kind": "personal", "target_id": workspace_id, "context": scope["token"]}},
        ip_address=request.META.get("REMOTE_ADDR"),
        user_agent=request.META.get("HTTP_USER_AGENT", "")[:500],
    )


for _model in (
    PersonalWorkspace, PersonalSubscription, PersonalReport, PersonalEvidence,
    PersonalAcademicYear, PersonalInitiative, PersonalPortfolioSection, PersonalPortfolioReport,
    PersonalPortfolioEvidence, PersonalShareLink,
):
    post_save.connect(audit_personal_maintenance, sender=_model, dispatch_uid=f"personal_support_save:{_model.__name__}")
    post_delete.connect(audit_personal_maintenance, sender=_model, dispatch_uid=f"personal_support_delete:{_model.__name__}")
