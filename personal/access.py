"""Maintenance write permission is separate from the subscriber's billing state."""


def workspace_owner(request):
    """The data owner can differ from the authenticated admin during support."""
    return getattr(request, "personal_owner", request.user)


def workspace_for_request(request):
    from .models import PersonalWorkspace

    selected = getattr(request, "support_workspace", None)
    owner = getattr(request, "personal_owner", None)
    scope = getattr(request, "support_scope", None)
    if (
        selected is not None and owner is not None and owner.pk == selected.owner_id
        and scope and scope["kind"] == "personal" and scope["target_id"] == selected.pk
        and request.user.is_active and request.user.is_superuser and scope["actor_id"] == request.user.pk
    ):
        return selected
    return PersonalWorkspace.objects.filter(owner=request.user).select_related("owner").first()


def support_access_for_workspace(workspace):
    from reports.middleware import get_current_request

    request = get_current_request()
    scope = getattr(request, "support_scope", None)
    actor = getattr(request, "support_actor", None)
    workspace_id = getattr(workspace, "pk", workspace)
    return bool(
        scope and actor and actor.is_active and actor.is_superuser
        and scope["kind"] == "personal" and scope["target_id"] == workspace_id
    )


def subscription_can_write(subscription):
    return subscription.is_current or support_access_for_workspace(subscription.workspace_id)
