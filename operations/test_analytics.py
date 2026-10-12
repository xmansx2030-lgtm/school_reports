from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIRequestFactory

from reports.models import Teacher

from .analytics import project_analytics, server_analytics
from .analytics_views import project_history, server_history
from .collector import sync_inventory_report
from .inventory import PROJECTS, canonical_project
from .models import (
    HealthCheck, Incident, ManagedProject, ManagedServer, MobileAccessToken,
    OperationsMembership, ProjectMetricSnapshot, ServerMetricSnapshot,
)
from .services import capture_server_metrics


@override_settings(DEBUG=True)
class AnalyticsTests(TestCase):
    def setUp(self):
        cache.clear()
        self.now = timezone.now()
        self.server = ManagedServer.objects.create(name="analytics", slug="analytics")
        self.project = ManagedProject.objects.create(name="Project", slug="analytics-project", server=self.server)

    def test_server_history_uses_host_only_and_discloses_short_coverage(self):
        for source, cpu, minutes in (("legacy", 99, 50), ("capacity", 88, 4), ("host", 20, 2)):
            ServerMetricSnapshot.objects.create(
                server=self.server, source=source, cpu_percent=cpu,
                sample_duration_seconds=300 if source == "host" else None,
                captured_at=self.now - timedelta(minutes=minutes),
            )
        result = server_analytics(self.server, "1h", now=self.now)
        self.assertEqual(result["source"], "host")
        self.assertEqual(result["summary"]["cpu_avg"], 20)
        self.assertEqual(result["coverage"]["sample_count"], 1)
        self.assertEqual(result["coverage"]["expected_samples"], 12)
        self.assertLess(result["coverage"]["coverage_percent"], 10)
        self.assertIsNone(result["summary"]["memory_avg"])

    def test_legacy_history_is_explicit_and_never_reclassified_as_host(self):
        ServerMetricSnapshot.objects.create(server=self.server, cpu_percent=12, captured_at=self.now)
        result = server_analytics(self.server, "48h", now=self.now)
        self.assertEqual(result["source"], "legacy_sampled")
        self.assertIsNone(result["coverage"]["sample_duration_seconds"])
        self.assertIn("مختلطة", result["source_label"])

    def test_capacity_only_does_not_become_host_history(self):
        ServerMetricSnapshot.objects.create(server=self.server, source="capacity", cpu_percent=90)
        result = server_analytics(self.server, "24h")
        self.assertEqual(result["source"], "unavailable")
        self.assertTrue(result["coverage"]["stale"])
        self.assertIsNone(result["summary"]["cpu_avg"])

    def test_seven_day_series_is_bounded_and_excludes_older_samples(self):
        ProjectMetricSnapshot.objects.bulk_create([
            ProjectMetricSnapshot(project=self.project, cpu_percent=10,
                                  captured_at=self.now - timedelta(minutes=5 * index))
            for index in range(2020)
        ])
        result = project_analytics(self.project, "7d", now=self.now)
        self.assertEqual(result["coverage"]["sample_count"], 2017)
        self.assertLessEqual(len(result["series"]), 169)
        self.assertEqual(result["summary"]["cpu_avg"], 10)
        self.assertEqual(result["source"], "docker")

    def test_health_availability_latency_p95_and_empty_buckets(self):
        for index in range(20):
            HealthCheck.objects.create(project=self.project, ok=index < 19, latency_ms=index * 10,
                                       checked_at=self.now - timedelta(minutes=index))
        HealthCheck.objects.create(project=self.project, ok=False, latency_ms=9999,
                                   checked_at=self.now - timedelta(days=2))
        result = project_analytics(self.project, "1h", now=self.now)
        self.assertEqual(result["summary"]["availability_percent"], 95)
        self.assertEqual(result["summary"]["failed_checks"], 1)
        self.assertEqual(result["summary"]["latency_p95_ms"], 180)
        self.assertEqual(result["series"], [])
        self.assertIsNone(result["latest"])

    def test_duplicate_samples_do_not_inflate_coverage(self):
        ProjectMetricSnapshot.objects.bulk_create([
            ProjectMetricSnapshot(project=self.project, captured_at=self.now - timedelta(seconds=1))
            for _ in range(12)
        ])
        result = project_analytics(self.project, "1h", now=self.now)
        self.assertEqual(result["coverage"]["sample_count"], 12)
        self.assertLess(result["coverage"]["coverage_percent"], 10)

    def test_read_endpoints_require_operations_token_and_validate_window(self):
        factory = APIRequestFactory()
        response = server_history(factory.get("/"), server_id=self.server.pk)
        self.assertEqual(response.status_code, 401)
        user = Teacher.objects.create_user(phone="0599998001", name="Analytics reader")
        OperationsMembership.objects.create(user=user, role=OperationsMembership.Role.VIEWER)
        _, token = MobileAccessToken.issue(user=user)
        headers = {"HTTP_AUTHORIZATION": f"Ops-Token {token}"}
        response = project_history(factory.get("/", {"window": "48h"}, **headers), project_id=self.project.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["window"], "48h")
        response = server_history(factory.get("/", {"window": "999d"}, **headers), server_id=self.server.pk)
        self.assertEqual(response.status_code, 400)
        self.project.is_active = False
        self.project.save(update_fields=("is_active",))
        response = project_history(factory.get("/", **headers), project_id=self.project.pk)
        self.assertEqual(response.status_code, 404)

    def test_host_cpu_mean_is_weighted_by_measured_interval(self):
        for cpu, duration, minutes in ((10, 60, 5), (40, 300, 0)):
            ServerMetricSnapshot.objects.create(server=self.server, source="host", cpu_percent=cpu,
                                               sample_duration_seconds=duration,
                                               captured_at=self.now - timedelta(minutes=minutes))
        self.assertEqual(server_analytics(self.server, "1h", now=self.now)["summary"]["cpu_avg"], 35)

    def test_host_series_weights_short_initial_samples_like_summary(self):
        start = self.now.replace(minute=0, second=0, microsecond=0)
        for cpu, duration, minutes in ((99, 1, 1), (10, 300, 2)):
            ServerMetricSnapshot.objects.create(server=self.server, source="host", cpu_percent=cpu,
                                               sample_duration_seconds=duration,
                                               captured_at=start + timedelta(minutes=minutes))
        result = server_analytics(self.server, "24h", now=start + timedelta(minutes=6))
        self.assertEqual(len(result["series"]), 1)
        self.assertEqual(result["summary"]["cpu_avg"], 10.3)
        self.assertEqual(result["series"][0]["cpu_percent"], 10.3)

    def test_only_project_detail_loads_latest_container_json(self):
        ProjectMetricSnapshot.objects.create(project=self.project, captured_at=self.now,
                                             container_states=[{"name": "latest-web"}])
        ProjectMetricSnapshot.objects.create(project=self.project, captured_at=self.now - timedelta(minutes=5),
                                             container_states=[{"name": "old-web"}])
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as queries:
            result = server_analytics(self.server, "24h", now=self.now)
        self.assertEqual(result["projects"][0]["coverage"]["sample_count"], 2)
        self.assertFalse(any("container_states" in item["sql"] for item in queries))
        result = project_analytics(self.project, "24h", now=self.now)
        self.assertEqual(result["latest"]["container_states"], [{"name": "latest-web"}])

    @patch("operations.services.enqueue_named_task")
    def test_capacity_does_not_overwrite_host_and_missing_data_does_not_resolve_alert(self, enqueue):
        capture_server_metrics(self.server, {"source": "host", "cpu_percent": 10, "memory_percent": 40,
                                             "disk_percent": 30, "sample_duration_seconds": 300})
        capture_server_metrics(self.server, {"cpu_percent": 99, "memory_percent": 99, "disk_percent": 99})
        self.server.refresh_from_db()
        self.assertEqual(float(self.server.cpu_percent), 10)
        incident = Incident.objects.create(server=self.server, dedupe_key=f"server:{self.server.pk}:host-capacity",
                                           title="CPU", message="CPU high")
        capture_server_metrics(self.server, {"source": "host"})
        incident.refresh_from_db()
        self.assertEqual(incident.status, Incident.Status.OPEN)
        enqueue.assert_not_called()

    @override_settings(CPU_ALERT_PERCENT=80)
    @patch("operations.services.enqueue_named_task")
    def test_rapid_or_short_cpu_samples_do_not_establish_sustained_pressure(self, enqueue):
        for _ in range(3):
            capture_server_metrics(self.server, {"source": "host", "cpu_percent": 99,
                                                 "memory_percent": 20, "disk_percent": 30,
                                                 "sample_duration_seconds": 300})
        self.assertFalse(Incident.objects.filter(server=self.server).exists())
        ServerMetricSnapshot.objects.filter(server=self.server).delete()
        for index in range(3):
            with patch("operations.services.timezone.now", return_value=self.now - timedelta(minutes=(2 - index) * 5)):
                capture_server_metrics(self.server, {"source": "host", "cpu_percent": 99,
                                                     "memory_percent": 20, "disk_percent": 30,
                                                     "sample_duration_seconds": 1})
        self.assertFalse(Incident.objects.filter(server=self.server).exists())
        enqueue.assert_not_called()

    def test_fifth_inventory_retains_tanal_identity_and_container_details(self):
        self.assertEqual(len(PROJECTS), 5)
        self.assertEqual(canonical_project("tanal")["slug"], "xmansx")
        sync_inventory_report({
            "server": {"cpu_percent": 10, "sample_duration_seconds": 300,
                       "host_details": {"swap_used_mb": 22, "unknown_secret": "never-store"}},
            "projects": [{"compose_project": "mowadhabah", "containers": [{
                "name": "mowadhabah-backend-1", "service": "backend", "state": "running",
                "health": "healthy", "cpu_percent": 2, "memory_used_mb": 321,
                "restart_count": 1, "oom_killed": False,
            }]}],
        })
        project = ManagedProject.objects.get(slug="mowadhabah")
        self.assertEqual(project.health_url, "https://mowadhabah.com/api/v1/health/")
        self.assertFalse(project.deployment_enabled)
        self.assertTrue(project.services.get(service_key="backend").restart_allowed)
        state = project.metric_snapshots.get().container_states[0]
        self.assertEqual(state["memory_used_mb"], 321)
        self.assertEqual(state["cpu_percent"], 2)
        snapshot = project.server.metric_snapshots.get()
        self.assertEqual(snapshot.source, "host")
        self.assertEqual(snapshot.sample_duration_seconds, 300)
        self.assertNotIn("unknown_secret", snapshot.host_details)

