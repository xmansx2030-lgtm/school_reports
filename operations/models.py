from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone


def _token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _action_request_id() -> str:
    return secrets.token_hex(16)


class ManagedServer(models.Model):
    class Status(models.TextChoices):
        HEALTHY = "healthy", "سليم"
        DEGRADED = "degraded", "متدهور"
        DOWN = "down", "متوقف"
        UNKNOWN = "unknown", "غير معروف"

    name = models.CharField(max_length=120, unique=True)
    slug = models.SlugField(max_length=80, unique=True)
    provider = models.CharField(max_length=40, default="hetzner")
    provider_server_id = models.CharField(max_length=80, blank=True, default="")
    public_ip = models.GenericIPAddressField(null=True, blank=True)
    region = models.CharField(max_length=80, blank=True, default="")
    server_type = models.CharField(max_length=40, blank=True, default="")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.UNKNOWN, db_index=True)
    cpu_percent = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True)
    memory_percent = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True)
    disk_percent = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("name",)

    def __str__(self) -> str:
        return self.name


class ManagedProject(models.Model):
    class Status(models.TextChoices):
        HEALTHY = "healthy", "سليم"
        DEGRADED = "degraded", "متدهور"
        DOWN = "down", "متوقف"
        MAINTENANCE = "maintenance", "صيانة"
        UNKNOWN = "unknown", "غير معروف"

    server = models.ForeignKey(ManagedServer, on_delete=models.PROTECT, related_name="projects")
    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=80, unique=True)
    base_url = models.URLField(max_length=300, blank=True, default="")
    health_path = models.CharField(max_length=160, default="/healthz/")
    compose_project = models.CharField(max_length=120, blank=True, default="", db_index=True)
    expected_status = models.PositiveSmallIntegerField(default=200)
    repository = models.CharField(max_length=160, blank=True, default="")
    deploy_branch = models.CharField(max_length=80, blank=True, default="main")
    ci_workflow = models.CharField(max_length=120, blank=True, default="")
    deploy_repository = models.CharField(max_length=160, blank=True, default="")
    deploy_workflow = models.CharField(max_length=120, blank=True, default="")
    deploy_container = models.CharField(max_length=120, blank=True, default="")
    deployed_sha = models.CharField(max_length=64, blank=True, default="")
    deployed_image = models.CharField(max_length=300, blank=True, default="")
    deployment_enabled = models.BooleanField(default=False)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.UNKNOWN, db_index=True)
    runtime_status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.UNKNOWN,
        db_index=True,
    )
    last_latency_ms = models.PositiveIntegerField(null=True, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    last_runtime_checked_at = models.DateTimeField(null=True, blank=True)
    consecutive_failures = models.PositiveSmallIntegerField(default=0)
    alerts_enabled = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("sort_order", "name")

    @property
    def health_url(self) -> str:
        if not self.base_url:
            return ""
        return f"{self.base_url.rstrip('/')}/{self.health_path.lstrip('/')}"

    @property
    def effective_status(self) -> str:
        """Return the worst of the public health probe and Docker runtime state."""
        priority = {
            self.Status.UNKNOWN: 0,
            self.Status.HEALTHY: 1,
            self.Status.MAINTENANCE: 2,
            self.Status.DEGRADED: 3,
            self.Status.DOWN: 4,
        }
        return max((self.status, self.runtime_status), key=lambda value: priority.get(value, 0))

    def __str__(self) -> str:
        return self.name


class ManagedService(models.Model):
    class Kind(models.TextChoices):
        WEB = "web", "تطبيق ويب"
        DATABASE = "database", "قاعدة بيانات"
        CACHE = "cache", "ذاكرة مؤقتة"
        WORKER = "worker", "عامل خلفي"
        PROXY = "proxy", "وكيل عكسي"
        OTHER = "other", "أخرى"

    project = models.ForeignKey(ManagedProject, on_delete=models.CASCADE, related_name="services")
    name = models.CharField(max_length=120)
    service_key = models.SlugField(max_length=100)
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.OTHER)
    status = models.CharField(
        max_length=16,
        choices=ManagedProject.Status.choices,
        default=ManagedProject.Status.UNKNOWN,
        db_index=True,
    )
    last_checked_at = models.DateTimeField(null=True, blank=True)
    restart_allowed = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("project", "kind", "name")
        constraints = [
            models.UniqueConstraint(fields=("project", "service_key"), name="operations_unique_project_service")
        ]

    def __str__(self) -> str:
        return f"{self.project.name}: {self.name}"


class HealthCheck(models.Model):
    project = models.ForeignKey(ManagedProject, on_delete=models.CASCADE, related_name="health_checks")
    ok = models.BooleanField(db_index=True)
    status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    latency_ms = models.PositiveIntegerField(null=True, blank=True)
    error_code = models.CharField(max_length=60, blank=True, default="")
    response_summary = models.CharField(max_length=300, blank=True, default="")
    checked_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ("-checked_at", "-id")
        indexes = [models.Index(fields=("project", "-checked_at"))]


class ServerMetricSnapshot(models.Model):
    server = models.ForeignKey(ManagedServer, on_delete=models.CASCADE, related_name="metric_snapshots")
    cpu_percent = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True)
    memory_percent = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True)
    disk_percent = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True)
    redis_memory_percent = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True)
    queue_lengths = models.JSONField(default=dict, blank=True)
    captured_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ("-captured_at", "-id")
        indexes = [models.Index(fields=("server", "-captured_at"))]


class ProjectMetricSnapshot(models.Model):
    """A project-only resource sample aggregated from its Docker containers."""

    project = models.ForeignKey(ManagedProject, on_delete=models.CASCADE, related_name="metric_snapshots")
    cpu_percent = models.DecimalField(max_digits=6, decimal_places=1, null=True, blank=True)
    memory_percent = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True)
    memory_used_mb = models.DecimalField(max_digits=12, decimal_places=1, null=True, blank=True)
    memory_limit_mb = models.DecimalField(max_digits=12, decimal_places=1, null=True, blank=True)
    network_rx_mb = models.DecimalField(max_digits=14, decimal_places=1, null=True, blank=True)
    network_tx_mb = models.DecimalField(max_digits=14, decimal_places=1, null=True, blank=True)
    block_read_mb = models.DecimalField(max_digits=14, decimal_places=1, null=True, blank=True)
    block_write_mb = models.DecimalField(max_digits=14, decimal_places=1, null=True, blank=True)
    container_count = models.PositiveSmallIntegerField(default=0)
    running_container_count = models.PositiveSmallIntegerField(default=0)
    container_states = models.JSONField(default=list, blank=True)
    captured_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ("-captured_at", "-id")
        indexes = [models.Index(fields=("project", "-captured_at"))]


class Incident(models.Model):
    class Severity(models.TextChoices):
        INFO = "info", "معلومة"
        WARNING = "warning", "تحذير"
        CRITICAL = "critical", "حرج"

    class Status(models.TextChoices):
        OPEN = "open", "مفتوحة"
        ACKNOWLEDGED = "acknowledged", "تمت رؤيتها"
        RESOLVED = "resolved", "محلولة"

    project = models.ForeignKey(ManagedProject, null=True, blank=True, on_delete=models.CASCADE, related_name="incidents")
    server = models.ForeignKey(ManagedServer, null=True, blank=True, on_delete=models.CASCADE, related_name="incidents")
    dedupe_key = models.CharField(max_length=180, db_index=True)
    title = models.CharField(max_length=160)
    message = models.TextField()
    severity = models.CharField(max_length=12, choices=Severity.choices, default=Severity.WARNING, db_index=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN, db_index=True)
    opened_at = models.DateTimeField(default=timezone.now)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="acknowledged_operations_incidents"
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    last_notified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-opened_at", "-id")
        indexes = [models.Index(fields=("status", "severity", "-opened_at"))]


class OperationAction(models.Model):
    class Action(models.TextChoices):
        CHECK_NOW = "check_now", "فحص الآن"
        RESTART_SERVICE = "restart_service", "إعادة تشغيل خدمة"
        RELOAD_PROXY = "reload_proxy", "إعادة تحميل الوكيل"
        CREATE_BACKUP = "create_backup", "إنشاء نسخة احتياطية"

    class Status(models.TextChoices):
        QUEUED = "queued", "في الانتظار"
        RUNNING = "running", "قيد التنفيذ"
        SUCCEEDED = "succeeded", "نجحت"
        FAILED = "failed", "فشلت"
        REJECTED = "rejected", "مرفوضة"

    project = models.ForeignKey(ManagedProject, on_delete=models.PROTECT, related_name="actions")
    service = models.ForeignKey(ManagedService, null=True, blank=True, on_delete=models.PROTECT, related_name="actions")
    action = models.CharField(max_length=24, choices=Action.choices)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.QUEUED, db_index=True)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="operations_actions")
    request_id = models.CharField(max_length=64, unique=True, default=_action_request_id, editable=False)
    result_summary = models.CharField(max_length=500, blank=True, default="")
    error_code = models.CharField(max_length=80, blank=True, default="")
    requested_at = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-requested_at", "-id")


class OperationsMembership(models.Model):
    class Role(models.TextChoices):
        ADMIN = "admin", "مدير عمليات"
        OPERATOR = "operator", "مشغّل"
        VIEWER = "viewer", "مشاهدة فقط"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="operations_membership",
    )
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.VIEWER, db_index=True)
    is_active = models.BooleanField(default=True, db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_operations_memberships",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("user__name", "user__phone")

    def __str__(self) -> str:
        return f"{self.user} ({self.get_role_display()})"


class MobileAccessToken(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="operations_mobile_tokens")
    public_id = models.CharField(max_length=20, unique=True, db_index=True)
    token_hash = models.CharField(max_length=64, unique=True)
    device_name = models.CharField(max_length=120, blank=True, default="")
    expires_at = models.DateTimeField(db_index=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    @classmethod
    def issue(cls, *, user, device_name: str = "") -> tuple["MobileAccessToken", str]:
        public_id = secrets.token_hex(6)
        raw = f"ops_{public_id}_{secrets.token_urlsafe(40)}"
        lifetime_hours = int(getattr(settings, "OPERATIONS_MOBILE_TOKEN_HOURS", 12) or 12)
        obj = cls.objects.create(
            user=user,
            public_id=public_id,
            token_hash=_token_hash(raw),
            device_name=device_name[:120],
            expires_at=timezone.now() + timedelta(hours=max(1, min(lifetime_hours, 168))),
        )
        return obj, raw

    def is_usable(self) -> bool:
        return self.revoked_at is None and self.expires_at > timezone.now() and self.user.is_active


class MobileDevice(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="operations_mobile_devices")
    device_id = models.CharField(max_length=160)
    name = models.CharField(max_length=120, blank=True, default="")
    platform = models.CharField(max_length=24, default="android")
    fcm_token = models.TextField(blank=True, default="")
    alerts_enabled = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)
    last_seen_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("user", "device_id"), name="operations_unique_user_device")]
        indexes = [models.Index(fields=("is_active", "alerts_enabled"))]
