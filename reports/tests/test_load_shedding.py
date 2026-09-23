"""Guards on the limits that keep a traffic spike from becoming an outage.

Under ASGI, Django gives every in-flight request its own thread and therefore
its own database connection. Nothing in the stack caps that, so the ceiling
lives in the application and must stay covered by tests.
"""
from __future__ import annotations

import json
import threading
from unittest.mock import patch

from django.conf import settings
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from core.middleware import ConcurrencyLimitMiddleware
from core.logging_filters import SkipExpectedLoadShed


class ConcurrencyLimitMiddlewareTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        ConcurrencyLimitMiddleware._in_flight = 0
        ConcurrencyLimitMiddleware._last_overload_log_at = 0.0

    def tearDown(self):
        ConcurrencyLimitMiddleware._in_flight = 0
        ConcurrencyLimitMiddleware._last_overload_log_at = 0.0

    def _middleware(self, get_response):
        return ConcurrencyLimitMiddleware(get_response)

    @override_settings(MAX_CONCURRENT_REQUESTS=1)
    def test_request_beyond_the_ceiling_is_shed_with_retry_after(self):
        released = threading.Event()
        entered = threading.Event()

        def slow_view(request):
            entered.set()
            released.wait(timeout=5)
            from django.http import HttpResponse

            return HttpResponse("ok")

        middleware = self._middleware(slow_view)
        holder = threading.Thread(
            target=lambda: middleware(self.factory.get("/dashboard/")),
            daemon=True,
        )
        holder.start()
        self.assertTrue(entered.wait(timeout=5))

        shed = middleware(self.factory.get("/dashboard/"))

        released.set()
        holder.join(timeout=5)

        self.assertEqual(shed.status_code, 503)
        self.assertEqual(shed["Retry-After"], "5")
        self.assertEqual(shed["Cache-Control"], "no-store")
        self.assertTrue(shed.expected_load_shed)

    @override_settings(MAX_CONCURRENT_REQUESTS=1)
    def test_json_clients_get_a_json_error(self):
        released = threading.Event()
        entered = threading.Event()

        def slow_view(request):
            entered.set()
            released.wait(timeout=5)
            from django.http import HttpResponse

            return HttpResponse("ok")

        middleware = self._middleware(slow_view)
        holder = threading.Thread(
            target=lambda: middleware(self.factory.get("/dashboard/")),
            daemon=True,
        )
        holder.start()
        self.assertTrue(entered.wait(timeout=5))

        shed = middleware(self.factory.get("/api/v1/anything", HTTP_ACCEPT="application/json"))

        released.set()
        holder.join(timeout=5)

        self.assertEqual(shed.status_code, 503)
        self.assertEqual(shed["Content-Type"], "application/json")
        self.assertEqual(json.loads(shed.content)["detail"], "server_busy")

    @override_settings(MAX_CONCURRENT_REQUESTS=1, OVERLOAD_LOG_INTERVAL_SECONDS=5)
    @patch("core.middleware.logger.warning")
    def test_overload_warning_is_sampled_during_a_spike(self, warning):
        ConcurrencyLimitMiddleware._in_flight = 1
        middleware = self._middleware(lambda request: None)

        for _ in range(25):
            self.assertEqual(middleware(self.factory.get("/dashboard/")).status_code, 503)

        warning.assert_called_once()

    def test_logging_filter_only_suppresses_marked_shed_responses(self):
        import logging

        from django.http import HttpResponse

        filter_ = SkipExpectedLoadShed()
        ordinary = logging.LogRecord("django.request", 40, "", 0, "error", (), None)
        ordinary.response = HttpResponse(status=503)
        expected = logging.LogRecord("django.request", 40, "", 0, "busy", (), None)
        expected.response = HttpResponse(status=503)
        expected.response.expected_load_shed = True
        expected_request = logging.LogRecord(
            "django.request", 40, "", 0, "busy", (), None
        )
        expected_request.request = self.factory.get("/dashboard/")
        expected_request.request.expected_load_shed = True

        self.assertTrue(filter_.filter(ordinary))
        self.assertFalse(filter_.filter(expected))
        self.assertFalse(filter_.filter(expected_request))

    @override_settings(MAX_CONCURRENT_REQUESTS=0)
    def test_zero_disables_shedding(self):
        from django.http import HttpResponse

        middleware = self._middleware(lambda request: HttpResponse("ok"))

        for _ in range(20):
            self.assertEqual(middleware(self.factory.get("/")).status_code, 200)

    @override_settings(MAX_CONCURRENT_REQUESTS=1)
    def test_health_check_is_never_shed(self):
        """A busy container must not be reported as dead and restarted."""
        released = threading.Event()
        entered = threading.Event()

        def slow_view(request):
            from django.http import HttpResponse

            if request.path == "/healthz/":
                return HttpResponse("ok")
            entered.set()
            released.wait(timeout=5)
            return HttpResponse("ok")

        middleware = self._middleware(slow_view)
        holder = threading.Thread(
            target=lambda: middleware(self.factory.get("/dashboard/")),
            daemon=True,
        )
        holder.start()
        self.assertTrue(entered.wait(timeout=5))

        health = middleware(self.factory.get("/healthz/"))

        released.set()
        holder.join(timeout=5)

        self.assertEqual(health.status_code, 200)

    @override_settings(MAX_CONCURRENT_REQUESTS=2)
    def test_counter_is_released_even_when_the_view_raises(self):
        def failing_view(request):
            raise RuntimeError("boom")

        middleware = self._middleware(failing_view)

        for _ in range(5):
            with self.assertRaises(RuntimeError):
                middleware(self.factory.get("/"))

        self.assertEqual(ConcurrencyLimitMiddleware.in_flight(), 0)


class ResourceCeilingSettingsTests(SimpleTestCase):
    def test_middleware_is_installed_before_session_and_database_work(self):
        middleware = list(settings.MIDDLEWARE)

        self.assertIn("core.middleware.ConcurrencyLimitMiddleware", middleware)
        self.assertLess(
            middleware.index("core.middleware.ConcurrencyLimitMiddleware"),
            middleware.index("django.contrib.sessions.middleware.SessionMiddleware"),
        )

    def test_database_connections_are_not_held_open_across_requests(self):
        """Persistent connections cannot be reused under ASGI — each request
        arrives on a new thread — so holding them only risks exhausting
        PostgreSQL's max_connections."""
        self.assertEqual(settings.DATABASES["default"].get("CONN_MAX_AGE", 0), 0)

    def test_request_body_buffer_is_bounded(self):
        self.assertLessEqual(settings.DATA_UPLOAD_MAX_MEMORY_SIZE, 10 * 1024 * 1024)


class OverloadResponseTests(TestCase):
    @override_settings(MAX_CONCURRENT_REQUESTS=0, ALLOWED_HOSTS=["testserver"])
    def test_normal_traffic_is_unaffected(self):
        response = self.client.get(reverse("reports:landing"))

        self.assertEqual(response.status_code, 200)


class InfrastructureCapacityMonitorTests(TestCase):
    """Redis eviction and session backlog both degrade silently, so they need
    to be surfaced before users feel them."""

    def _run(self):
        from reports.tasks import monitor_infrastructure_capacity_task

        # These tests exercise the session-backlog contract. Host disk/CPU and
        # external Redis/Celery state belong to the machine running the suite,
        # so letting them leak in makes the result depend on a developer's free
        # disk space or whether a local broker happens to be running.
        with (
            patch("reports.services_capacity._probe_redis_memory"),
            patch("reports.services_capacity._probe_cpu_and_memory"),
            patch("reports.services_capacity._probe_disk"),
            patch("reports.services_capacity._probe_celery_queues"),
            patch("reports.services_capacity._probe_http_metrics"),
            patch("reports.tasks.enqueue_named_task"),
        ):
            return monitor_infrastructure_capacity_task.apply().get()

    def test_monitor_task_contract_is_stable(self):
        from reports.task_names import MONITOR_INFRASTRUCTURE_CAPACITY_TASK
        from reports.tasks import monitor_infrastructure_capacity_task

        self.assertEqual(
            monitor_infrastructure_capacity_task.name,
            MONITOR_INFRASTRUCTURE_CAPACITY_TASK,
        )
        self.assertEqual(
            settings.CELERY_TASK_ROUTES[MONITOR_INFRASTRUCTURE_CAPACITY_TASK]["queue"],
            "periodic",
        )

    @patch("reports.tasks.enqueue_named_task")
    @patch("reports.tasks.collect_infrastructure_capacity_report")
    def test_monitor_task_delegates_collection_and_snapshot(self, collect, enqueue):
        from operations.task_names import STORE_CAPACITY_SNAPSHOT_TASK
        from reports.tasks import monitor_infrastructure_capacity_task

        report = {"alerts": [], "queue_lengths": {"default": 0}}
        collect.return_value = report

        result = monitor_infrastructure_capacity_task.apply().get()

        self.assertEqual(result, report)
        collect.assert_called_once_with()
        enqueue.assert_called_once_with(STORE_CAPACITY_SNAPSHOT_TASK, args=(report,))

    def test_healthy_infrastructure_raises_no_alert(self):
        report = self._run()

        self.assertEqual(report["alerts"], [])
        self.assertEqual(report["expired_sessions"], 0)

    @override_settings(EXPIRED_SESSION_ALERT_THRESHOLD=1000)
    def test_session_backlog_below_threshold_is_quiet(self):
        from datetime import timedelta

        from django.contrib.sessions.backends.db import SessionStore
        from django.contrib.sessions.models import Session
        from django.utils import timezone

        store = SessionStore()
        store["marker"] = "x"
        store.create()
        Session.objects.filter(session_key=store.session_key).update(
            expire_date=timezone.now() - timedelta(days=1)
        )

        report = self._run()

        self.assertEqual(report["expired_sessions"], 1)
        self.assertEqual(report["alerts"], [])

    def test_session_backlog_over_threshold_alerts(self):
        from datetime import timedelta

        from django.contrib.sessions.backends.db import SessionStore
        from django.contrib.sessions.models import Session
        from django.utils import timezone

        store = SessionStore()
        store["marker"] = "x"
        store.create()
        Session.objects.filter(session_key=store.session_key).update(
            expire_date=timezone.now() - timedelta(days=1)
        )

        # settings parsing floors the threshold at 1000, so patch the value the
        # task reads to make one stale row cross it.
        with patch.object(settings, "EXPIRED_SESSION_ALERT_THRESHOLD", 0, create=True):
            report = self._run()

        self.assertEqual(len(report["alerts"]), 1)
        self.assertIn("expired session rows", report["alerts"][0])

    def test_monitor_is_scheduled(self):
        schedule = getattr(settings, "CELERY_BEAT_SCHEDULE", {})

        self.assertIn("monitor-infrastructure-capacity", schedule)
        self.assertEqual(
            schedule["monitor-infrastructure-capacity"]["task"],
            "reports.tasks.monitor_infrastructure_capacity_task",
        )
