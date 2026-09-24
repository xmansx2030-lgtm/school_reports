from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver
from django.core.cache import cache

from core.observability import soft_call

from .models import PersonalEvidence, PersonalPlan
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
