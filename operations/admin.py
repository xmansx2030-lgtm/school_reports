from django.contrib import admin

from .models import (
    HealthCheck,
    Incident,
    ManagedProject,
    ManagedServer,
    ManagedService,
    MobileAccessToken,
    MobileDevice,
    OperationAction,
    OperationsMembership,
    ProjectMetricSnapshot,
    ServerMetricSnapshot,
)


@admin.register(ManagedServer)
class ManagedServerAdmin(admin.ModelAdmin):
    list_display = ("name", "provider", "public_ip", "status", "last_checked_at", "is_active")
    list_filter = ("provider", "status", "is_active")
    search_fields = ("name", "slug", "public_ip")


@admin.register(ManagedProject)
class ManagedProjectAdmin(admin.ModelAdmin):
    list_display = (
        "name", "server", "compose_project", "base_url", "status", "runtime_status",
        "last_latency_ms", "last_runtime_checked_at", "is_active",
    )
    list_filter = ("status", "runtime_status", "alerts_enabled", "is_active", "server")
    search_fields = ("name", "slug", "compose_project", "base_url", "repository")


@admin.register(ManagedService)
class ManagedServiceAdmin(admin.ModelAdmin):
    list_display = ("name", "project", "kind", "status", "restart_allowed", "is_active")
    list_filter = ("kind", "status", "restart_allowed", "is_active")


@admin.register(Incident)
class IncidentAdmin(admin.ModelAdmin):
    list_display = ("title", "severity", "status", "project", "opened_at", "resolved_at")
    list_filter = ("severity", "status")
    readonly_fields = ("dedupe_key", "opened_at", "acknowledged_at", "resolved_at")


admin.site.register(HealthCheck)
admin.site.register(ServerMetricSnapshot)
admin.site.register(ProjectMetricSnapshot)
admin.site.register(OperationAction)
admin.site.register(MobileDevice)


@admin.register(OperationsMembership)
class OperationsMembershipAdmin(admin.ModelAdmin):
    list_display = ("user", "role", "is_active", "created_by", "updated_at")
    list_filter = ("role", "is_active")
    search_fields = ("user__name", "user__phone", "user__email")


@admin.register(MobileAccessToken)
class MobileAccessTokenAdmin(admin.ModelAdmin):
    list_display = ("user", "device_name", "created_at", "expires_at", "last_used_at", "revoked_at")
    readonly_fields = ("public_id", "token_hash", "created_at", "last_used_at")
