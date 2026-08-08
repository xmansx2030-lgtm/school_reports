from __future__ import annotations

import threading
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from django.core.cache import cache
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings

from core.middleware import SchoolRateLimitMiddleware
from reports.cache_utils import (
    get_school_dashboard_payload,
    invalidate_school_dashboard,
    school_dashboard_version,
)


class SchoolDashboardCacheTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    @override_settings(SCHOOL_DASHBOARD_CACHE_TTL_SECONDS=45)
    def test_dashboard_is_scoped_by_school_and_period(self):
        calls = []

        def build():
            calls.append(1)
            return {"value": len(calls)}

        first = get_school_dashboard_payload(school_id=11, period="month", builder=build)
        second = get_school_dashboard_payload(school_id=11, period="month", builder=build)
        other_period = get_school_dashboard_payload(school_id=11, period="year", builder=build)
        other_school = get_school_dashboard_payload(school_id=12, period="month", builder=build)

        self.assertEqual(first, second)
        self.assertEqual(len(calls), 3)
        self.assertNotEqual(first, other_period)
        self.assertNotEqual(other_period, other_school)

    def test_version_invalidation_publishes_a_fresh_payload(self):
        calls = []

        def build():
            calls.append(1)
            return {"generation": len(calls)}

        before = get_school_dashboard_payload(school_id=21, period="all", builder=build)
        old_version = school_dashboard_version(21)
        invalidate_school_dashboard(21)
        after = get_school_dashboard_payload(school_id=21, period="all", builder=build)

        self.assertGreater(school_dashboard_version(21), old_version)
        self.assertNotEqual(before, after)

    def test_report_and_ticket_signal_bumps_the_school_version(self):
        from reports.model_parts.signals import invalidate_dashboard_after_school_activity

        old_version = school_dashboard_version(24)
        invalidate_dashboard_after_school_activity(
            sender=object,
            instance=SimpleNamespace(school_id=24),
        )
        self.assertGreater(school_dashboard_version(24), old_version)

    @override_settings(SCHOOL_DASHBOARD_LOCK_WAIT_SECONDS=1.5)
    def test_one_builder_serves_a_cold_concurrent_burst(self):
        entered = threading.Event()
        release = threading.Event()
        calls = []
        results = []

        def build():
            calls.append(1)
            entered.set()
            release.wait(timeout=2)
            return {"ok": True}

        first = threading.Thread(
            target=lambda: results.append(
                get_school_dashboard_payload(school_id=31, period="all", builder=build)
            ),
            daemon=True,
        )
        second = threading.Thread(
            target=lambda: results.append(
                get_school_dashboard_payload(school_id=31, period="all", builder=build)
            ),
            daemon=True,
        )
        first.start()
        self.assertTrue(entered.wait(timeout=1))
        second.start()
        release.set()
        first.join(timeout=2)
        second.join(timeout=2)

        self.assertEqual(calls, [1])
        self.assertEqual(results, [{"ok": True}, {"ok": True}])


class SchoolRateLimitMiddlewareTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.factory = RequestFactory()
        self.middleware = SchoolRateLimitMiddleware(lambda request: HttpResponse("ok"))

    def tearDown(self):
        cache.clear()

    def _request(self, school_id: int, *, authenticated: bool = True):
        request = self.factory.get("/dashboard/")
        request.user = SimpleNamespace(is_authenticated=authenticated)
        request.active_school = SimpleNamespace(pk=school_id) if authenticated else None
        return request

    @override_settings(
        SCHOOL_RATE_LIMIT_ENABLED=True,
        SCHOOL_RATE_LIMIT_REQUESTS=2,
        SCHOOL_RATE_LIMIT_WINDOW_SECONDS=60,
    )
    @patch("core.middleware.time.time", return_value=120)
    def test_third_request_for_same_school_is_rejected(self, _time):
        self.assertEqual(self.middleware(self._request(1)).status_code, 200)
        self.assertEqual(self.middleware(self._request(1)).status_code, 200)
        limited = self.middleware(self._request(1))

        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited["Retry-After"], "60")

    @override_settings(
        SCHOOL_RATE_LIMIT_ENABLED=True,
        SCHOOL_RATE_LIMIT_REQUESTS=1,
        SCHOOL_RATE_LIMIT_WINDOW_SECONDS=60,
    )
    @patch("core.middleware.time.time", return_value=120)
    def test_budgets_are_isolated_per_school(self, _time):
        self.assertEqual(self.middleware(self._request(1)).status_code, 200)
        self.assertEqual(self.middleware(self._request(2)).status_code, 200)

    @override_settings(SCHOOL_RATE_LIMIT_ENABLED=True, SCHOOL_RATE_LIMIT_REQUESTS=1)
    def test_anonymous_traffic_is_left_to_ip_and_cloudflare_limits(self):
        for _ in range(3):
            self.assertEqual(self.middleware(self._request(0, authenticated=False)).status_code, 200)


class GeneratedExportJobTests(TestCase):
    def setUp(self):
        from reports.models import School, Teacher

        self.temp_media = tempfile.TemporaryDirectory()
        self.settings_override = override_settings(MEDIA_ROOT=self.temp_media.name)
        self.settings_override.enable()
        self.school = School.objects.create(name="مدرسة الحمل", code="load-school")
        self.user = Teacher.objects.create_user(
            phone="0500998877",
            name="مدير الاختبار",
            password="Passw0rd!123",
        )

    def tearDown(self):
        self.settings_override.disable()
        self.temp_media.cleanup()

    @patch("reports.tasks.build_generated_export_task.apply_async")
    def test_equivalent_active_exports_are_deduplicated(self, enqueue):
        from reports.generated_exports import enqueue_generated_export
        from reports.models import GeneratedExportJob

        with self.captureOnCommitCallbacks(execute=True):
            first, first_created = enqueue_generated_export(
                school=self.school,
                requested_by=self.user,
                kind=GeneratedExportJob.Kind.SCHOOL_ZIP,
            )
        second, second_created = enqueue_generated_export(
            school=self.school,
            requested_by=self.user,
            kind=GeneratedExportJob.Kind.SCHOOL_ZIP,
        )

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first.pk, second.pk)
        enqueue.assert_called_once_with(args=[first.pk], queue="images")

    @patch("reports.services_export.build_school_export_zip_file")
    def test_zip_is_built_by_the_worker_and_persisted(self, build_zip):
        from reports.models import GeneratedExportJob
        from reports.tasks import build_generated_export_task

        payload = tempfile.SpooledTemporaryFile(max_size=1024)
        payload.write(b"PK\x03\x04background-export")
        payload.seek(0)
        build_zip.return_value = payload
        job = GeneratedExportJob.objects.create(
            school=self.school,
            requested_by=self.user,
            kind=GeneratedExportJob.Kind.SCHOOL_ZIP,
        )

        result = build_generated_export_task.apply(args=[job.pk]).get()
        job.refresh_from_db()

        self.assertTrue(result)
        self.assertEqual(job.status, GeneratedExportJob.Status.READY)
        self.assertTrue(job.artifact_file.name.endswith(".zip"))
        self.assertGreater(job.size_bytes, 0)
        self.assertIsNotNone(job.expires_at)
