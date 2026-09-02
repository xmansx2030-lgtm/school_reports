from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from reports.models import Teacher

from .collector import sync_inventory_report
from .deployments import DeploymentState
from .models import (
    HealthCheck,
    Incident,
    ManagedProject,
    ManagedServer,
    MobileAccessToken,
    MobileDevice,
    OperationAction,
    OperationsMembership,
    ProjectMetricSnapshot,
    ServerMetricSnapshot,
)
from .services import capture_server_metrics


def deployment_state(**overrides):
    data = {
        "project_id": 1,
        "project_slug": "project",
        "project_name": "Project",
        "repository": "owner/repo",
        "branch": "main",
        "workflow": "ci.yml",
        "configured": True,
        "deployment_enabled": True,
        "source_ready": True,
        "latest_sha": "b" * 40,
        "latest_message": "new release",
        "deployed_sha": "a" * 40,
        "deployed_image": "ghcr.io/owner/repo:" + "a" * 40,
        "up_to_date": False,
        "repository_ahead": True,
        "workflow_status": "completed",
        "workflow_conclusion": "success",
        "workflow_url": "https://github.example/run",
        "workflow_run_id": 123,
        "action_required": "اضغط زر النشر.",
        "generated_note": "",
    }
    data.update(overrides)
    return DeploymentState(**data)


@override_settings(DEBUG=True)
class OperationsApiTests(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = Teacher.objects.create_superuser(phone="0500000001", name="Ops Admin", password="strong-test-password")
        self.regular = Teacher.objects.create_user(phone="0500000002", name="Regular", password="strong-test-password")
        OperationsMembership.objects.create(user=self.admin, role=OperationsMembership.Role.ADMIN)
        self.server = ManagedServer.objects.create(name="main", slug="main", public_ip="127.0.0.1")
        self.project = ManagedProject.objects.create(
            server=self.server,
            name="Project",
            slug="project",
            base_url="https://example.com",
            health_path="/healthz/",
        )

    def _login(self):
        response = self.client.post(
            reverse("operations:login"),
            {"phone": self.admin.phone, "password": "strong-test-password", "device_name": "test"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        return response.json()["token"]

    def test_login_is_restricted_to_operations_members(self):
        response = self.client.post(
            reverse("operations:login"),
            {"phone": self.regular.phone, "password": "strong-test-password"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)
        self.assertFalse(MobileAccessToken.objects.filter(user=self.regular).exists())

    def test_unrelated_superuser_cannot_access_operations(self):
        unrelated = Teacher.objects.create_superuser(
            phone="0500000003",
            name="Platform Admin",
            password="strong-test-password",
        )

        response = self.client.post(
            reverse("operations:login"),
            {"phone": unrelated.phone, "password": "strong-test-password"},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 401)
        self.assertFalse(MobileAccessToken.objects.filter(user=unrelated).exists())

    def test_dashboard_requires_ops_token_and_returns_inventory(self):
        self.assertEqual(self.client.get(reverse("operations:dashboard")).status_code, 401)
        _, token = MobileAccessToken.issue(user=self.admin, device_name="test")
        response = self.client.get(reverse("operations:dashboard"), HTTP_AUTHORIZATION=f"Ops-Token {token}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["summary"]["projects"], 1)

    def test_dashboard_returns_project_specific_latest_usage(self):
        ProjectMetricSnapshot.objects.create(
            project=self.project,
            cpu_percent=12.5,
            memory_percent=7.5,
            memory_used_mb=384,
            container_count=3,
            running_container_count=3,
        )
        _, token = MobileAccessToken.issue(user=self.admin, device_name="test")

        response = self.client.get(
            reverse("operations:dashboard"),
            HTTP_AUTHORIZATION=f"Ops-Token {token}",
        )

        metric = response.json()["servers"][0]["projects"][0]["latest_metric"]
        self.assertEqual(metric["cpu_percent"], "12.5")
        self.assertEqual(metric["memory_used_mb"], "384.0")
        self.assertEqual(metric["running_container_count"], 3)

    def test_project_detail_does_not_present_server_usage_as_project_usage(self):
        ServerMetricSnapshot.objects.create(
            server=self.server,
            cpu_percent=91,
            memory_percent=82,
            disk_percent=73,
        )
        ProjectMetricSnapshot.objects.create(
            project=self.project,
            cpu_percent=11,
            memory_percent=12,
            memory_used_mb=256,
            container_count=2,
            running_container_count=2,
        )
        token = self._login()

        response = self.client.get(
            reverse("operations:project-detail", args=[self.project.pk]),
            HTTP_AUTHORIZATION=f"Ops-Token {token}",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["metrics"][0]["cpu_percent"], "11.0")
        self.assertNotIn("disk_percent", response.json()["metrics"][0])

    def test_inventory_report_updates_known_repositories_and_discovers_projects(self):
        report = {
            "server": {
                "slug": self.server.slug,
                "name": self.server.name,
                "cpu_percent": 40,
                "memory_percent": 50,
                "disk_percent": 60,
            },
            "projects": [
                {
                    "compose_project": "tanal",
                    "deployed_sha": "c" * 40,
                    "containers": [
                        {
                            "name": "tanal-web-1",
                            "service": "web",
                            "state": "running",
                            "health": "healthy",
                            "cpu_percent": 4.5,
                            "memory_host_percent": 3.2,
                            "memory_used_mb": 160,
                        }
                    ],
                },
                {
                    "compose_project": "new_portal",
                    "repository": "owner/new-portal",
                    "containers": [
                        {
                            "name": "new-portal-api-1",
                            "service": "api",
                            "state": "running",
                            "cpu_percent": 2,
                            "memory_host_percent": 1,
                        }
                    ],
                },
            ],
        }

        result = sync_inventory_report(report)

        tanal = ManagedProject.objects.get(slug="xmansx")
        discovered = ManagedProject.objects.get(slug="new-portal")
        self.assertEqual(tanal.repository, "xmansx2030-lgtm/tanal")
        self.assertEqual(tanal.deploy_repository, "xmansx2030-lgtm/tanal")
        self.assertEqual(tanal.deployed_sha, "c" * 40)
        self.assertEqual(tanal.runtime_status, ManagedProject.Status.HEALTHY)
        self.assertEqual(discovered.repository, "owner/new-portal")
        self.assertEqual(discovered.base_url, "")
        self.assertEqual(discovered.metric_snapshots.get().memory_percent, 1)
        self.assertEqual(result["projects"], 2)

    def test_completed_migrate_job_does_not_degrade_project(self):
        report = {
            "server": {"slug": self.server.slug, "name": self.server.name},
            "projects": [
                {
                    "compose_project": "tanal",
                    "containers": [
                        {
                            "name": "tanal-web-1",
                            "service": "web",
                            "state": "running",
                            "health": "healthy",
                        },
                        {
                            "name": "tanal-migrate-1",
                            "service": "migrate",
                            "state": "exited",
                            "exit_code": 0,
                            "restart_policy": "no",
                        },
                    ],
                }
            ],
        }

        sync_inventory_report(report)

        tanal = ManagedProject.objects.get(slug="xmansx")
        # A finished one-shot job is the expected end state, not a crash.
        self.assertEqual(tanal.runtime_status, ManagedProject.Status.HEALTHY)
        metric = tanal.metric_snapshots.latest("captured_at")
        self.assertEqual(metric.container_count, 1)
        self.assertEqual(metric.running_container_count, 1)

    def test_failed_migrate_job_still_marks_project_down(self):
        report = {
            "server": {"slug": self.server.slug, "name": self.server.name},
            "projects": [
                {
                    "compose_project": "tanal",
                    "containers": [
                        {
                            "name": "tanal-web-1",
                            "service": "web",
                            "state": "running",
                            "health": "healthy",
                        },
                        {
                            "name": "tanal-migrate-1",
                            "service": "migrate",
                            "state": "exited",
                            "exit_code": 1,
                            "restart_policy": "no",
                        },
                    ],
                }
            ],
        }

        sync_inventory_report(report)

        tanal = ManagedProject.objects.get(slug="xmansx")
        # A non-zero exit is a real failure and must surface as degraded/down.
        self.assertEqual(tanal.runtime_status, ManagedProject.Status.DEGRADED)
        metric = tanal.metric_snapshots.latest("captured_at")
        self.assertEqual(metric.container_count, 2)
        self.assertEqual(metric.running_container_count, 1)

    def test_device_registration_never_exposes_other_devices(self):
        token = self._login()
        response = self.client.post(
            reverse("operations:device-registration"),
            {"device_id": "android-test", "name": "Tablet", "fcm_token": "secret-fcm-token"},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Ops-Token {token}",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(MobileDevice.objects.get().user, self.admin)
        self.assertNotIn("fcm_token", response.json())

    @patch("operations.views.probe_project")
    def test_check_now_is_audited(self, probe):
        probe.return_value = HealthCheck(project=self.project, ok=True, latency_ms=12, checked_at=timezone.now())
        token = self._login()
        response = self.client.post(
            reverse("operations:create-action", args=[self.project.pk]),
            {"action": "check_now"},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Ops-Token {token}",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(OperationAction.objects.get().status, OperationAction.Status.SUCCEEDED)

    def test_destructive_action_requires_exact_project_confirmation(self):
        token = self._login()
        response = self.client.post(
            reverse("operations:create-action", args=[self.project.pk]),
            {"action": "create_backup", "confirmation": "wrong"},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Ops-Token {token}",
        )
        self.assertEqual(response.status_code, 409)
        self.assertFalse(OperationAction.objects.exists())

    def test_acknowledge_incident_records_actor_and_time(self):
        incident = Incident.objects.create(project=self.project, server=self.server, dedupe_key="x", title="Down", message="Unavailable")
        token = self._login()
        response = self.client.post(
            reverse("operations:acknowledge-incident", args=[incident.pk]),
            HTTP_AUTHORIZATION=f"Ops-Token {token}",
        )
        self.assertEqual(response.status_code, 200)
        incident.refresh_from_db()
        self.assertEqual(incident.status, Incident.Status.ACKNOWLEDGED)
        self.assertEqual(incident.acknowledged_by, self.admin)

    @override_settings(OPERATIONS_CAPACITY_SUSTAINED_SAMPLES=3, CPU_ALERT_PERCENT=80)
    @patch("operations.tasks.send_incident_push_task.delay")
    def test_capacity_alert_requires_sustained_pressure(self, push_delay):
        for cpu_percent in (95, 20, 95):
            capture_server_metrics(
                self.server,
                {
                    "cpu_percent": cpu_percent,
                    "memory_percent": 40,
                    "disk_percent": 30,
                    "redis_used_percent": 10,
                    "queue_lengths": {"default": 0},
                },
            )

        self.assertFalse(Incident.objects.filter(dedupe_key=f"server:{self.server.pk}:capacity").exists())
        push_delay.assert_not_called()

    @override_settings(
        OPERATIONS_CAPACITY_SUSTAINED_SAMPLES=3,
        CPU_ALERT_PERCENT=80,
        CELERY_QUEUE_ALERT_LENGTH=10,
    )
    @patch("operations.tasks.send_incident_push_task.delay")
    def test_capacity_alert_includes_recommended_action(self, push_delay):
        for _ in range(3):
            capture_server_metrics(
                self.server,
                {
                    "cpu_percent": 91,
                    "memory_percent": 40,
                    "disk_percent": 30,
                    "redis_used_percent": 10,
                    "queue_lengths": {"images": 12},
                },
            )

        incident = Incident.objects.get(dedupe_key=f"server:{self.server.pk}:capacity")
        self.assertIn("CPU مرتفع بشكل مستمر", incident.message)
        self.assertIn("الإجراء المناسب", incident.message)
        self.assertIn("worker إضافي", incident.message)
        push_delay.assert_called_once_with(incident.pk)

    @patch("operations.views.all_deployment_states")
    def test_deployment_status_reports_repository_drift(self, all_states):
        all_states.return_value = [deployment_state(project_id=self.project.pk)]
        token = self._login()
        response = self.client.get(reverse("operations:deployment-status"), HTTP_AUTHORIZATION=f"Ops-Token {token}")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["repository_ahead_count"], 1)
        self.assertEqual(payload["can_deploy_count"], 1)
        self.assertTrue(payload["deployments"][0]["repository_ahead"])
        self.assertEqual(payload["deployments"][0]["latest_short_sha"], "b" * 12)

    @patch("operations.views.GitHubDeploymentClient")
    def test_trigger_deployment_requires_latest_sha_confirmation(self, client_cls):
        state = deployment_state(project_id=self.project.pk, workflow_url="", workflow_run_id=None)
        client = client_cls.return_value
        client.deployment_state.return_value = state
        token = self._login()

        rejected = self.client.post(
            reverse("operations:trigger-deployment"),
            {"project_id": self.project.pk, "confirmation": "wrong"},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Ops-Token {token}",
        )
        self.assertEqual(rejected.status_code, 409)
        client.trigger_deploy.assert_not_called()

        accepted = self.client.post(
            reverse("operations:trigger-deployment"),
            {"project_id": self.project.pk, "confirmation": "b" * 12},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Ops-Token {token}",
        )
        self.assertEqual(accepted.status_code, 202)
        client.trigger_deploy.assert_called_once_with(source_sha="b" * 40)

    @patch("operations.views.GitHubDeploymentClient")
    def test_trigger_deployment_requires_run_actions_capability(self, client_cls):
        OperationsMembership.objects.create(
            user=self.regular,
            role=OperationsMembership.Role.VIEWER,
            created_by=self.admin,
        )
        _, token = MobileAccessToken.issue(user=self.regular, device_name="test")

        response = self.client.post(
            reverse("operations:trigger-deployment"),
            {"project_id": self.project.pk, "confirmation": "b" * 12},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Ops-Token {token}",
        )

        self.assertEqual(response.status_code, 403)
        client_cls.assert_not_called()
