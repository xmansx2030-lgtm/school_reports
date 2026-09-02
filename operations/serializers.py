from rest_framework import serializers

from .models import (
    HealthCheck,
    Incident,
    ManagedProject,
    ManagedServer,
    ManagedService,
    OperationAction,
    ProjectMetricSnapshot,
    ServerMetricSnapshot,
)


class ManagedServiceSerializer(serializers.ModelSerializer):
    kind_label = serializers.CharField(source="get_kind_display", read_only=True)

    class Meta:
        model = ManagedService
        fields = ("id", "name", "service_key", "kind", "kind_label", "status", "last_checked_at", "restart_allowed")


class ProjectMetricSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectMetricSnapshot
        fields = (
            "cpu_percent",
            "memory_percent",
            "memory_used_mb",
            "memory_limit_mb",
            "network_rx_mb",
            "network_tx_mb",
            "block_read_mb",
            "block_write_mb",
            "container_count",
            "running_container_count",
            "container_states",
            "captured_at",
        )


class ManagedProjectSerializer(serializers.ModelSerializer):
    services = ManagedServiceSerializer(many=True, read_only=True)
    health_url = serializers.CharField(read_only=True)
    status = serializers.CharField(source="effective_status", read_only=True)
    latest_metric = serializers.SerializerMethodField()

    def get_latest_metric(self, obj):
        metric = obj.metric_snapshots.first()
        return ProjectMetricSerializer(metric).data if metric is not None else None

    class Meta:
        model = ManagedProject
        fields = (
            "id", "name", "slug", "base_url", "health_url", "compose_project", "status",
            "runtime_status", "last_latency_ms", "last_checked_at", "last_runtime_checked_at",
            "consecutive_failures", "alerts_enabled", "latest_metric", "services",
        )


class ManagedServerSerializer(serializers.ModelSerializer):
    projects = ManagedProjectSerializer(many=True, read_only=True)

    class Meta:
        model = ManagedServer
        fields = (
            "id", "name", "slug", "provider", "provider_server_id", "public_ip", "region",
            "server_type", "status", "cpu_percent", "memory_percent", "disk_percent",
            "last_checked_at", "projects",
        )


class IncidentSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source="project.name", default="", read_only=True)

    class Meta:
        model = Incident
        fields = ("id", "title", "message", "severity", "status", "project_name", "opened_at", "acknowledged_at", "resolved_at")


class HealthCheckSerializer(serializers.ModelSerializer):
    class Meta:
        model = HealthCheck
        fields = ("id", "ok", "status_code", "latency_ms", "error_code", "checked_at")


class ServerMetricSerializer(serializers.ModelSerializer):
    class Meta:
        model = ServerMetricSnapshot
        fields = ("cpu_percent", "memory_percent", "disk_percent", "redis_memory_percent", "queue_lengths", "captured_at")


class OperationActionSerializer(serializers.ModelSerializer):
    action_label = serializers.CharField(source="get_action_display", read_only=True)
    requested_by_name = serializers.CharField(source="requested_by.name", read_only=True)

    class Meta:
        model = OperationAction
        fields = (
            "id", "request_id", "action", "action_label", "status", "requested_by_name",
            "result_summary", "error_code", "requested_at", "started_at", "finished_at",
        )
