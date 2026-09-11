"""Small, shared access guards for independent Django view modules."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import Http404

from .permissions import executive_director_groups, is_executive_director


def platform_superuser_required(view: Callable[..., Any]) -> Callable[..., Any]:
    """Preserve the platform-login redirect for anonymous and denied users."""
    return login_required(login_url="reports:platform_login")(
        user_passes_test(
            lambda user: getattr(user, "is_superuser", False),
            login_url="reports:platform_login",
        )(view)
    )


def executive_director_groups_or_404(request) -> list:
    """Return the director's groups without ever widening an empty scope."""
    user = request.user
    if not is_executive_director(user):
        raise Http404
    groups = list(executive_director_groups(user))
    if not groups:
        raise Http404
    return groups
