"""Maintenance write permission is separate from the subscriber's billing state."""


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
