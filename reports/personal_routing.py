"""Route the shared PWA shortcuts for teachers with only a personal workspace."""

from django.utils import timezone

from .models import SchoolMembership


def is_personal_only_workspace_user(user) -> bool:
    """A personal owner without any current subscribed school membership."""
    if not getattr(user, "is_authenticated", False):
        return False

    from personal.models import PersonalWorkspace

    if not PersonalWorkspace.objects.filter(owner_id=user.pk).exists():
        return False

    today = timezone.localdate()
    has_subscribed_school = SchoolMembership.objects.filter(
        teacher_id=user.pk,
        is_active=True,
        school__is_active=True,
        school__subscription__is_active=True,
        school__subscription__start_date__lte=today,
        school__subscription__end_date__gte=today,
    ).exists()
    return not has_subscribed_school
