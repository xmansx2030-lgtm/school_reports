from datetime import timedelta
from decimal import Decimal
from pathlib import Path
import re

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from reports.models import Payment, School, SchoolSubscription, SubscriptionPlan, Teacher


class PlatformDashboardAssetContractTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        base_dir = Path(settings.BASE_DIR)
        cls.template = (
            base_dir / "reports" / "templates" / "reports" / "platform_admin_dashboard.html"
        ).read_text(encoding="utf-8")
        cls.css = (base_dir / "static" / "css" / "platform-dashboard.css").read_text(
            encoding="utf-8"
        )
        cls.js = (base_dir / "static" / "js" / "platform-dashboard.js").read_text(
            encoding="utf-8"
        )

    def test_dashboard_owns_external_css_and_behavior_js(self):
        self.assertIn("css/platform-dashboard.css", self.template)
        self.assertIn("js/platform-dashboard.js", self.template)
        self.assertNotIn("<style", self.template.lower())
        self.assertNotRegex(self.template, r"<script(?![^>]+\bsrc=)[^>]*>\s*(?!\{\{)")
        self.assertNotRegex(self.template, r"\sstyle\s*=")
        self.assertNotIn(".style", self.js)

    def test_module_css_uses_semantic_tokens_and_logical_properties(self):
        self.assertNotRegex(self.css, r"#[0-9a-fA-F]{3,8}\b")
        self.assertNotRegex(self.css, r"\b(?:rgb|rgba|hsl|hsla)\(")
        self.assertNotRegex(
            self.css,
            r"\b(?:margin|padding|border|inset)-(?:left|right)\s*:",
        )
        self.assertNotRegex(self.css, r"(?:^|[;{])\s*(?:left|right)\s*:")
        self.assertIn("var(--twq-", self.css)

    def test_charts_have_text_equivalents_and_zero_state(self):
        for canvas_id, rows_id in (
            ("revenueChart", "revenueChartRows"),
            ("reportsChart", "reportsChartRows"),
            ("schoolsChart", "schoolsChartRows"),
        ):
            self.assertIn(f'id="{canvas_id}"', self.template)
            self.assertIn(f'id="{rows_id}"', self.template)
            self.assertIn(f'data-chart-empty="{canvas_id}"', self.template)
        self.assertIn("role=\"img\"", self.template)
        self.assertIn("<details", self.template)


@override_settings(
    ALLOWED_HOSTS=["testserver"],
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "platform-dashboard-ui-tests",
        }
    },
)
class PlatformDashboardUiTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = Teacher.objects.create_superuser(
            phone="599755500", name="مالك المنصة", password="pass"
        )

    def tearDown(self):
        cache.clear()

    def test_owner_gets_operational_dashboard_with_one_h1(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("reports:platform_admin_dashboard"))

        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")
        self.assertEqual(len(re.findall(r"<h1\b", html, flags=re.IGNORECASE)), 1)
        self.assertContains(response, "مركز التشغيل")
        self.assertContains(response, "يحتاج انتباهك")
        self.assertContains(response, "نطاق عالمي")
        self.assertContains(response, 'aria-controls="searchResults"')
        self.assertContains(response, "عرض البيانات الرقمية للإيرادات")
        self.assertNotIn("NaN", html)
        self.assertNotIn("Infinity", html)

    def test_ordinary_school_user_cannot_open_platform_dashboard(self):
        manager = Teacher.objects.create_user(
            phone="599755501", name="مدير مدرسة", password="pass", is_staff=True
        )
        self.client.force_login(manager)

        response = self.client.get(reverse("reports:platform_admin_dashboard"))

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("reports:platform_login"), response.url)

    def test_empty_platform_returns_zero_safe_chart_contract(self):
        self.client.force_login(self.owner)
        response = self.client.get(
            reverse("reports:api_platform_dashboard_data"),
            {"period": "month", "refresh": "1"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["kpis"]["total_revenue"], 0.0)
        self.assertEqual(payload["charts"]["revenue"]["data"], [])
        self.assertEqual(payload["charts"]["reports"]["data"], [])

    def test_primary_shortcuts_resolve_in_committed_dashboard_contract(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("reports:platform_admin_dashboard"))

        for route_name in (
            "schools_admin_list",
            "platform_subscriptions_list",
            "platform_payments_list",
            "platform_tickets_list",
            "platform_complaints_list",
            "platform_email_inbox",
            "platform_settings",
            "platform_audit_logs",
        ):
            self.assertContains(response, reverse(f"reports:{route_name}"))


@override_settings(
    ALLOWED_HOSTS=["testserver"],
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "platform-dashboard-query-tests",
        }
    },
)
class PlatformDashboardQueryTests(TestCase):
    def test_roster_size_does_not_create_query_growth(self):
        owner = Teacher.objects.create_superuser(
            phone="599755599", name="مالك المنصة", password="pass"
        )
        plan = SubscriptionPlan.objects.create(
            name="تشغيلية",
            price=Decimal("1200.00"),
            days_duration=365,
            max_teachers=80,
        )
        today = timezone.localdate()
        for index in range(24):
            school = School.objects.create(
                name=f"مدرسة {index:02d}",
                code=f"platform-dashboard-{index:02d}",
                storage_used_bytes=index * 1024,
            )
            SchoolSubscription.objects.create(
                school=school,
                plan=plan,
                start_date=today,
                end_date=today + timedelta(days=20 + index),
                is_active=True,
            )
            if index < 6:
                Payment.objects.create(
                    school=school,
                    amount=Decimal("250.00") + index,
                    payment_date=today,
                    status=Payment.Status.APPROVED,
                    created_by=owner,
                )

        self.client.force_login(owner)
        endpoint = reverse("reports:platform_admin_dashboard")
        cache.clear()
        with CaptureQueriesContext(connection) as cold_queries:
            cold_response = self.client.get(endpoint)
        with CaptureQueriesContext(connection) as warm_queries:
            warm_response = self.client.get(endpoint)

        self.assertEqual(cold_response.status_code, 200)
        self.assertEqual(warm_response.status_code, 200)
        self.assertLessEqual(len(cold_queries), 66)
        self.assertLessEqual(len(warm_queries), 9)
