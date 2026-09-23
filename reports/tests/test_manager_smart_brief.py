from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from reports.manager_brief import build_manager_brief, safe_ai_snapshot
from reports.models import (
    ReportType,
    School,
    SchoolMembership,
    SchoolSubscription,
    SubscriptionPlan,
    Teacher,
)


ROOT = Path(__file__).resolve().parents[2]


def _payload(**overrides):
    payload = {
        "period": "month",
        "period_label": "هذا الشهر",
        "generated_at": "2026-09-23 12:00",
        "kpis": {
            "reports_count": 12,
            "teachers_count": 10,
            "tickets_total": 6,
            "tickets_open": 3,
            "tickets_done": 3,
        },
        "coverage": {"covered": 7, "total": 10, "pending": 3, "percent": 70, "has_staff": True},
        "trends": {"reports_count": {"direction": "down", "previous": 15, "delta": -3}},
        "responsiveness": {},
    }
    payload.update(overrides)
    return payload


class ManagerBriefServiceTests(SimpleTestCase):
    def test_the_database_snapshot_owns_priorities_actions_and_numbers(self):
        brief = build_manager_brief(_payload())

        self.assertFalse(brief["ai_generated"])
        self.assertEqual(brief["period_label"], "هذا الشهر")
        self.assertEqual(brief["signals"][0]["value"], "70%")
        self.assertEqual(brief["priorities"][0]["key"], "coverage")
        self.assertEqual(
            brief["priorities"][0]["url"],
            reverse("reports:notifications_create") + "?remind=coverage",
        )

    def test_a_school_without_staff_is_not_congratulated_for_zero_of_zero(self):
        payload = _payload(
            kpis={"reports_count": 0, "teachers_count": 0, "tickets_total": 0, "tickets_open": 0, "tickets_done": 0},
            coverage={"covered": 0, "total": 0, "pending": 0, "percent": 0, "has_staff": False},
        )

        brief = build_manager_brief(payload)

        self.assertEqual(brief["status"], "setup")
        self.assertEqual(brief["priorities"][0]["key"], "team")
        self.assertNotIn("اكتملت التغطية", brief["summary"])

    def test_ai_snapshot_contains_aggregates_not_school_people_or_record_content(self):
        brief = build_manager_brief(_payload())
        snapshot = safe_ai_snapshot(_payload(), brief)
        encoded = json.dumps(snapshot, ensure_ascii=False)

        self.assertIn("documentation_coverage_percent", encoded)
        self.assertNotIn("school", snapshot)
        self.assertNotIn("url", encoded)
        self.assertNotIn("teacher", encoded)


@override_settings(ALLOWED_HOSTS=["testserver"])
class ManagerSmartBriefJourneyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = School.objects.create(
            name="مدرسة الموجز",
            code="smart-brief-school",
            current_academic_year="1448-1449",
        )
        cls.other_school = School.objects.create(
            name="مدرسة أخرى سرية",
            code="smart-brief-other",
            current_academic_year="1448-1449",
        )
        plan = SubscriptionPlan.objects.create(
            name="خطة الموجز",
            price=0,
            days_duration=365,
            max_teachers=50,
        )
        SchoolSubscription.objects.create(school=cls.school, plan=plan)
        SchoolSubscription.objects.create(school=cls.other_school, plan=plan)
        ReportType.objects.create(
            school=cls.school,
            code="brief",
            name="تقرير الموجز",
        )
        cls.manager = Teacher.objects.create_user(
            phone="500833001",
            name="مدير الموجز",
            password="probe-pass",
            is_staff=True,
        )
        cls.teacher = Teacher.objects.create_user(
            phone="500833002",
            name="معلم عادي",
            password="probe-pass",
        )
        SchoolMembership.objects.create(
            school=cls.school,
            teacher=cls.manager,
            role_type=SchoolMembership.RoleType.MANAGER,
            is_active=True,
        )
        SchoolMembership.objects.create(
            school=cls.school,
            teacher=cls.teacher,
            role_type=SchoolMembership.RoleType.TEACHER,
            is_active=True,
        )

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def _login(self, user=None, school=None):
        self.client.force_login(user or self.manager)
        session = self.client.session
        session["active_school_id"] = (school or self.school).pk
        session.save()

    @override_settings(REPORT_AI_ENABLED=False, OPENAI_API_KEY="")
    def test_manager_dashboard_renders_a_complete_fallback_brief(self):
        self._login()

        response = self.client.get(reverse("reports:admin_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="managerBrief"')
        self.assertContains(response, "موجز المدير الذكي")
        self.assertContains(response, "تحليل مباشر من بيانات النظام")
        self.assertContains(response, reverse("reports:manager_smart_brief"))
        self.assertNotContains(response, self.other_school.name)

    @override_settings(REPORT_AI_ENABLED=False, OPENAI_API_KEY="")
    def test_endpoint_returns_the_safe_brief_even_without_an_ai_provider(self):
        self._login()

        response = self.client.post(
            reverse("reports:manager_smart_brief"),
            data=json.dumps({"period": "month"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store")
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["brief"]["ai_generated"])
        self.assertEqual(payload["brief"]["period"], "month")
        self.assertEqual(len(payload["brief"]["signals"]), 3)

    @override_settings(
        REPORT_AI_ENABLED=True,
        OPENAI_API_KEY="test-key",
        REPORT_AI_MODEL="test-model",
    )
    @patch("reports.manager_brief.responses_create")
    def test_ai_can_polish_copy_but_cannot_replace_server_actions(self, responses_create):
        responses_create.return_value = {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": json.dumps(
                                {
                                    "headline": "ركّز على بناء الإيقاع التشغيلي",
                                    "summary": "ابدأ بالأولوية المعروضة ثم راقب أثرها في القراءة التالية.",
                                },
                                ensure_ascii=False,
                            ),
                        }
                    ],
                }
            ],
        }
        self._login()

        response = self.client.post(
            reverse("reports:manager_smart_brief"),
            data=json.dumps({"period": "month"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        brief = response.json()["brief"]
        self.assertTrue(brief["ai_generated"])
        self.assertEqual(brief["headline"], "ركّز على بناء الإيقاع التشغيلي")
        self.assertTrue(all(item["url"].startswith(("/", "#")) for item in brief["priorities"]))
        sent_snapshot = json.loads(responses_create.call_args.args[0]["input"])
        self.assertNotIn("school", sent_snapshot)
        self.assertNotIn(self.manager.name, json.dumps(sent_snapshot, ensure_ascii=False))

    def test_teacher_and_anonymous_callers_cannot_read_the_school_brief(self):
        anonymous = self.client.post(
            reverse("reports:manager_smart_brief"),
            data="{}",
            content_type="application/json",
        )
        self.assertEqual(anonymous.status_code, 401)

        self._login(self.teacher)
        teacher = self.client.post(
            reverse("reports:manager_smart_brief"),
            data="{}",
            content_type="application/json",
        )
        self.assertEqual(teacher.status_code, 403)

    def test_get_is_not_a_generation_backdoor(self):
        self._login()
        response = self.client.get(reverse("reports:manager_smart_brief"))
        self.assertEqual(response.status_code, 405)


class ManagerSmartBriefAssetTests(SimpleTestCase):
    def test_assets_keep_csp_safe_external_javascript_and_semantic_tokens(self):
        template = (ROOT / "reports/templates/reports/admin_dashboard.html").read_text(encoding="utf-8")
        partial = (ROOT / "reports/templates/reports/partials/manager_smart_brief.html").read_text(encoding="utf-8")
        css = (ROOT / "static/css/manager-smart-brief.css").read_text(encoding="utf-8")
        javascript = (ROOT / "static/js/manager-smart-brief.js").read_text(encoding="utf-8")

        self.assertIn("manager-smart-brief.js", template)
        self.assertIn('type="button"', partial)
        self.assertIn('role="status"', partial)
        self.assertIn("--twq-", css)
        self.assertNotIn("transition: all", css)
        self.assertNotRegex(css, r"#[0-9a-fA-F]{3,8}")
        self.assertIn('headers: {', javascript)
        self.assertIn('"X-CSRFToken": csrfToken()', javascript)
        self.assertIn("textContent", javascript)
