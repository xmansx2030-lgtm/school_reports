from datetime import timedelta
from unittest.mock import patch

from django.test import SimpleTestCase
from django.utils import timezone

from .models import ManagedProject, ManagedServer
from .serializers import ManagedProjectSerializer, ManagedServerSerializer


class MonitoringFreshnessTests(SimpleTestCase):
    def setUp(self):
        self.now = timezone.now()
        self.clock = patch("operations.models.timezone.now", return_value=self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def project(self, **overrides):
        values = {
            "name": "Hosted project", "slug": "tawtheeq", "compose_project": "school-reports",
            "base_url": "https://example.com", "status": "healthy", "runtime_status": "healthy",
            "last_checked_at": self.now, "last_runtime_checked_at": self.now,
        }
        values.update(overrides)
        return ManagedProject(**values)

    def test_missing_or_expired_runtime_cannot_count_as_healthy(self):
        for captured_at in (None, self.now - timedelta(minutes=16)):
            with self.subTest(captured_at=captured_at):
                project = self.project(last_runtime_checked_at=captured_at)
                self.assertEqual(project.effective_status, "unknown")
                self.assertTrue(ManagedProjectSerializer().get_stale(project))

    def test_public_probe_must_be_fresh_even_when_runtime_is_current(self):
        for captured_at in (None, self.now - timedelta(minutes=16)):
            with self.subTest(captured_at=captured_at):
                project = self.project(last_checked_at=captured_at)
                self.assertEqual(project.effective_status, "unknown")
                self.assertTrue(project.is_stale)

    def test_current_unknown_required_source_cannot_be_overruled_by_healthy(self):
        for source in ("status", "runtime_status"):
            with self.subTest(source=source):
                self.assertEqual(self.project(**{source: "unknown"}).effective_status, "unknown")

    def test_recorded_failure_and_maintenance_survive_staleness(self):
        for source in ("status", "runtime_status"):
            for status in ("down", "degraded", "maintenance"):
                with self.subTest(source=source, status=status):
                    project = self.project(**{source: status}, last_checked_at=None, last_runtime_checked_at=None)
                    self.assertEqual(project.effective_status, status)

    def test_manual_public_project_does_not_require_unconfigured_runtime(self):
        project = self.project(compose_project="", runtime_status="unknown", last_runtime_checked_at=None)
        self.assertEqual(project.effective_status, "healthy")
        self.assertFalse(project.is_stale)

    def test_runtime_only_project_does_not_require_unconfigured_public_probe(self):
        project = self.project(base_url="", status="unknown", last_checked_at=None)
        self.assertEqual(project.effective_status, "healthy")
        self.assertFalse(project.is_stale)

    def test_exact_fifteen_minute_boundary_is_current(self):
        project = self.project(last_runtime_checked_at=self.now - timedelta(minutes=15))
        self.assertEqual(project.effective_status, "healthy")
        project.last_runtime_checked_at -= timedelta(microseconds=1)
        self.assertEqual(project.effective_status, "unknown")

    def test_server_serializer_never_reports_stale_success_as_healthy(self):
        serializer = ManagedServerSerializer()
        status_field = serializer.fields["status"]
        stale_field = serializer.fields["stale"]
        server = ManagedServer(name="Host", slug="host", status="healthy", last_checked_at=None)
        self.assertEqual(status_field.get_attribute(server), "unknown")
        self.assertTrue(stale_field.get_attribute(server))
        server.last_checked_at = self.now
        self.assertEqual(status_field.get_attribute(server), "healthy")
        self.assertFalse(stale_field.get_attribute(server))
        server.last_checked_at = None
        server.status = "down"
        self.assertEqual(status_field.get_attribute(server), "down")
