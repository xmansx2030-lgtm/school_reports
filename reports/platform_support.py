"""Owner-authorized maintenance contexts; the authenticated session stays the owner's."""

import secrets
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.cache import add_never_cache_headers
from django.utils.deprecation import MiddlewareMixin
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from .models import AuditLog, School

SESSION_KEY = "platform_support_context"
CONTEXT_FIELD = "_support_context"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def support_audit(request, event, scope, *, school=None):
    actor = getattr(request, "support_actor", request.user)
    AuditLog.objects.create(
        teacher=actor, actor_name=actor.name, actor_role="مالك المنصة · صيانة",
        school=school, action=AuditLog.Action.UPDATE,
        model_name="PlatformSupportSession", object_id=scope["target_id"],
        object_repr=scope["label"][:255],
        changes={"event": event, "kind": scope["kind"], "context": scope["token"]},
        ip_address=request.META.get("REMOTE_ADDR"),
        user_agent=request.META.get("HTTP_USER_AGENT", "")[:500],
    )


def _restore_school(request, scope):
    previous = scope.get("previous_school_id")
    if previous and School.objects.filter(pk=previous).exists():
        request.session["active_school_id"] = previous
    else:
        request.session.pop("active_school_id", None)


def _start(request, kind, target, label):
    if not request.user.is_active or not request.user.is_superuser:
        raise PermissionDenied
    previous = request.session.get(SESSION_KEY)
    previous_school = request.session.get("active_school_id")
    if previous:
        support_audit(request, "switch", previous, school=getattr(request, "support_school", None))
        previous_school = previous.get("previous_school_id")
    scope = {
        "actor_id": request.user.pk, "kind": kind, "target_id": target.pk,
        "label": label, "token": secrets.token_urlsafe(24),
        "expires_at": (timezone.now() + timedelta(hours=1)).timestamp(),
        "previous_school_id": previous_school,
    }
    support_audit(request, "enter", scope, school=target if kind == "school" else None)
    request.session[SESSION_KEY] = scope
    request.support_scope = scope
    request.support_actor = request.user
    request.support_school = target if kind == "school" else None
    request.support_workspace = target if kind == "personal" else None
    if kind == "school":
        request.session["active_school_id"] = target.pk
    else:
        request.session.pop("active_school_id", None)
    return redirect("reports:admin_dashboard" if kind == "school" else "personal:dashboard")


@never_cache
@login_required(login_url="reports:platform_login")
@require_POST
def enter_school(request, pk):
    if not request.user.is_superuser:
        raise PermissionDenied
    school = get_object_or_404(School, pk=pk)
    return _start(request, "school", school, school.name)


@never_cache
@login_required(login_url="reports:platform_login")
@require_POST
def enter_personal(request, pk):
    if not request.user.is_superuser:
        raise PermissionDenied
    from personal.models import PersonalWorkspace

    workspace = get_object_or_404(PersonalWorkspace.objects.select_related("owner"), pk=pk)
    return _start(request, "personal", workspace, workspace.owner.name)


@never_cache
@login_required(login_url="reports:platform_login")
@require_POST
def exit_support(request):
    if not request.user.is_superuser:
        raise PermissionDenied
    scope = request.session.get(SESSION_KEY)
    if scope:
        support_audit(request, "exit", scope, school=getattr(request, "support_school", None))
        _restore_school(request, scope)
        request.session.pop(SESSION_KEY, None)
        request.support_scope = None
    messages.success(request, "انتهت جلسة الصيانة. عدت إلى لوحة إدارة المنصة.")
    return redirect("reports:platform_admin_dashboard")


class PlatformSupportMiddleware(MiddlewareMixin):
    """Validate with the real owner on every request, then scope only personal views."""

    def process_request(self, request):
        scope = request.session.get(SESSION_KEY)
        if not scope:
            return None
        actor = request.user
        valid = (
            isinstance(scope, dict) and actor.is_authenticated and actor.is_active
            and actor.is_superuser and scope.get("actor_id") == actor.pk
            and scope.get("kind") in {"school", "personal"}
            and isinstance(scope.get("target_id"), int)
            and isinstance(scope.get("label"), str)
            and isinstance(scope.get("expires_at"), (int, float))
            and isinstance(scope.get("token"), str) and bool(scope["token"])
        )
        if not valid:
            request.session.pop(SESSION_KEY, None)
            request.session.pop("active_school_id", None)
            return None
        request.support_actor = actor
        request.support_scope = scope
        request.support_expires_at = timezone.datetime.fromtimestamp(scope["expires_at"], tz=timezone.get_current_timezone())
        if scope["kind"] == "school":
            request.support_school = School.objects.filter(pk=scope["target_id"]).first()
            target = request.support_school
        else:
            from personal.models import PersonalWorkspace

            request.support_workspace = PersonalWorkspace.objects.select_related("owner").filter(pk=scope["target_id"]).first()
            target = request.support_workspace
        if scope["expires_at"] <= timezone.now().timestamp() or target is None:
            support_audit(request, "expire" if target else "target_unavailable", scope, school=getattr(request, "support_school", None))
            _restore_school(request, scope)
            request.session.pop(SESSION_KEY, None)
            request.support_scope = None
            if request.method not in SAFE_METHODS:
                return self._conflict(request)
            messages.info(request, "انتهت جلسة الصيانة. افتح المساحة مجددًا لمتابعة العمل.")
            return redirect("reports:platform_admin_dashboard")
        if scope["kind"] == "school":
            request.session["active_school_id"] = target.pk
        else:
            request.session.pop("active_school_id", None)
        return None

    @staticmethod
    def _conflict(request):
        payload = {
            "detail": "support_context_changed",
            "message": "تغيّرت مساحة الصيانة أو انتهت الجلسة. أعد تحميل الصفحة قبل الحفظ.",
        }
        if (
            "application/json" in request.headers.get("Accept", "")
            or request.headers.get("X-Requested-With") == "XMLHttpRequest"
            or request.headers.get("X-Platform-Support-Context")
            or request.content_type == "application/json"
        ):
            return JsonResponse(payload, status=409)
        scope = getattr(request, "support_scope", None)
        destination = "reports:platform_admin_dashboard"
        if scope:
            destination = "personal:dashboard" if scope["kind"] == "personal" else "reports:admin_dashboard"
        return render(request, "reports/platform_support_conflict.html", {"support_destination": destination}, status=409)

    def process_view(self, request, view_func, view_args, view_kwargs):
        scope = getattr(request, "support_scope", None)
        match = request.resolver_match
        # A form left in another tab must never write into the newly selected space.
        token = request.headers.get("X-Platform-Support-Context") or request.POST.get(CONTEXT_FIELD)
        if request.method not in SAFE_METHODS and (scope or token):
            if not scope or not token or not secrets.compare_digest(str(token).encode(), scope["token"].encode()):
                return self._conflict(request)
        if not scope or not match:
            return None
        if scope["kind"] == "personal":
            shared_entries = {
                "reports:home": "personal:dashboard", "reports:my_profile": "personal:account",
                "reports:my_notifications": "personal:notices", "reports:role_guidance": "personal:dashboard",
            }
            if match.view_name in shared_entries and request.method in SAFE_METHODS:
                return redirect(shared_entries[match.view_name])
            if match.namespace == "personal" and not match.url_name.startswith("platform_"):
                owner = request.support_workspace.owner
                request.user = owner
                request._cached_user = owner
                request._acached_user = owner
        if scope["kind"] == "school" and match.view_name == "reports:switch_school":
            return self._conflict(request)
        return None

    def process_response(self, request, response):
        if getattr(request, "support_actor", None):
            scope = getattr(request, "support_scope", None)
            if scope and request.method not in SAFE_METHODS and response.status_code < 400:
                AuditLog.objects.create(
                    teacher=request.support_actor, actor_name=request.support_actor.name,
                    actor_role="مالك المنصة · صيانة", school=getattr(request, "support_school", None),
                    action=AuditLog.Action.UPDATE, model_name="PlatformSupportAction", object_id=scope["target_id"],
                    object_repr=scope["label"][:255], changes={
                        "event": "request", "kind": scope["kind"], "context": scope["token"],
                        "method": request.method, "path": request.path, "status": response.status_code,
                    },
                    ip_address=request.META.get("REMOTE_ADDR"),
                    user_agent=request.META.get("HTTP_USER_AGENT", "")[:500],
                )
            add_never_cache_headers(response)
            response["X-Platform-Support"] = "active" if getattr(request, "support_scope", None) else "ended"
        return response
