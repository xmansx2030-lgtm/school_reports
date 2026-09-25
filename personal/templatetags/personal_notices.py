"""Personal workspace notification indicators for the shared page shell."""

from django import template

from personal.models import PersonalNoticeRecipient


register = template.Library()


@register.simple_tag(takes_context=True)
def personal_unread_notices(context):
    request = context.get("request")
    user = getattr(request, "user", None)
    if not getattr(user, "is_authenticated", False):
        return 0

    if hasattr(request, "_personal_unread_notices_count"):
        return request._personal_unread_notices_count

    workspace = getattr(request, "personal_workspace", None) or context.get("workspace")
    if not workspace or workspace.owner_id != user.pk:
        request._personal_unread_notices_count = 0
        return 0

    count = PersonalNoticeRecipient.objects.filter(
        workspace_id=workspace.pk, read_at__isnull=True,
    ).count()
    request._personal_unread_notices_count = count
    return count
