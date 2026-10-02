"""Entitlement visibility and platform operations for independent accounts."""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from reports.models import AiUsageEvent, School, Teacher

from .assistant_quota import reserve_daily_slot
from .assistant_views import personal_assistant_template_context
from .models import PersonalPayment, PersonalPlan, PersonalSubscription, PersonalWorkspace
from .services import ensure_personal_subscription


@override_settings(
    ALLOWED_HOSTS=["testserver"], RATELIMIT_ENABLE=False,
    OPENAI_API_KEY="test-key", REPORT_AI_ENABLED=True, VOICE_REPORT_ENABLED=True,
    VOICE_REPORT_PWA_ONLY=True, LANDING_PRICING_CACHE_TTL_SECONDS=0,
)
class IndependentTeacherJourneyTests(TestCase):
    def setUp(self):
        cache.clear()
        self.teacher = Teacher.objects.create_user(phone="0557900811", name="معلمة مستقلة")
        self.workspace = PersonalWorkspace.objects.create(owner=self.teacher, school_name="مدرسة غير مشتركة")
        self.subscription = ensure_personal_subscription(self.workspace)
        self.plan = PersonalPlan.objects.create(
            code="independent-premium", name="باقة التوثيق الفردي", price="99.00", duration_days=365,
            report_ai_daily_limit=2, voice_report_daily_limit=1,
        )
        self.admin = Teacher.objects.create_superuser(phone="0557900812", name="مدير النظام", password="LocalTest#2026")  # noqa: S106
        self.client.force_login(self.teacher)

    def activate_paid(self):
        self.subscription.plan = self.plan
        self.subscription.save()

    def test_entitlements_are_visible_across_discovery_checkout_and_current_workspace(self):
        landing = Client().get(reverse("reports:landing"))
        card = next(p for p in landing.context["personal_plan_cards"] if p["id"] == self.plan.pk)
        self.assertEqual((card["report_ai_daily_limit"], card["voice_report_daily_limit"]), (2, 1))
        self.teacher.email = "teacher@example.test"
        self.teacher.save(update_fields=["email"])
        checkout = self.client.get(reverse("personal:checkout_start", args=[self.plan.pk]))
        self.assertContains(checkout, "تحسين الصياغة: 2 يوميًا")
        self.activate_paid()
        for route in ("personal:dashboard", "personal:billing", "personal:report_create"):
            with self.subTest(route=route):
                page = self.client.get(reverse(route))
                self.assertEqual(page.status_code, 200)
                tools = page.context["personal_assistant_tools"]
                self.assertEqual([t["state"] for t in tools], ["available", "available"])
                self.assertEqual([t["remaining"] for t in tools], [2, 1])
                self.assertContains(page, "الذكاء الاصطناعي في باقتك")
        self.assertFalse(School.objects.exists())

    def test_free_expired_disabled_and_exhausted_states_are_distinct(self):
        tools = personal_assistant_template_context(self.teacher, self.subscription)["personal_assistant_tools"]
        self.assertEqual([t["state"] for t in tools], ["excluded", "excluded"])
        self.activate_paid()
        reserve_daily_slot("improvement", self.teacher.pk, 2)
        reserve_daily_slot("improvement", self.teacher.pk, 2)
        tools = personal_assistant_template_context(self.teacher, self.subscription)["personal_assistant_tools"]
        self.assertEqual([t["state"] for t in tools], ["exhausted", "available"])
        with override_settings(REPORT_AI_ENABLED=False):
            tools = personal_assistant_template_context(self.teacher, self.subscription)["personal_assistant_tools"]
            self.assertEqual(tools[0]["state"], "unavailable")
            self.assertEqual(tools[0]["included"], 2)
        self.subscription.end_date = timezone.localdate() - timedelta(days=1)
        self.subscription.save(update_fields=["end_date"])
        tools = personal_assistant_template_context(self.teacher, self.subscription)["personal_assistant_tools"]
        self.assertEqual([t["state"] for t in tools], ["inactive", "inactive"])

    def test_cache_outage_does_not_claim_user_exhausted_the_quota(self):
        self.activate_paid()
        with patch("personal.assistant_quota.cache.get", side_effect=ConnectionError("test cache outage")):
            context = personal_assistant_template_context(self.teacher, self.subscription)
        self.assertEqual([t["state"] for t in context["personal_assistant_tools"]], ["quota_unavailable", "quota_unavailable"])
        self.assertFalse(context["report_ai_enabled"])
        self.assertFalse(context["voice_report_enabled"])

    def test_getting_started_reflects_content_and_stays_read_only(self):
        page = self.client.get(reverse("personal:dashboard"))
        self.assertEqual(page.context["getting_started_done"], 0)
        self.assertFalse(self.workspace.reports.exists())
        self.teacher.email = "teacher@example.test"
        self.teacher.save(update_fields=["email"])
        page = self.client.get(reverse("personal:dashboard"))
        self.assertEqual(page.context["getting_started_done"], 1)
        self.assertContains(page, "المهام والتعاميم والاعتماد المدرسي تتطلب عضوية مدرسة مشتركة")

    def test_admin_routes_reject_teacher_anonymous_and_post_requests(self):
        urls = [reverse("personal:platform_subscribers"), reverse("personal:platform_subscriber_detail", args=[self.workspace.pk])]
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 302)
        self.client.logout()
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 302)
        self.client.force_login(self.admin)
        for url in urls:
            self.assertEqual(self.client.post(url).status_code, 405)
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertIn("no-cache", response["Cache-Control"])

    def test_admin_filters_use_personal_plan_and_exclude_future_or_expired_subscriptions(self):
        self.activate_paid()
        other = Teacher.objects.create_user(phone="0557900813", name="حساب مستقبلي")
        other_workspace = PersonalWorkspace.objects.create(owner=other)
        future = ensure_personal_subscription(other_workspace)
        PersonalSubscription.objects.filter(pk=future.pk).update(start_date=timezone.localdate() + timedelta(days=5))
        self.client.force_login(self.admin)
        response = self.client.get(reverse("personal:platform_subscribers"), {"q": "مستقلة", "status": "active", "plan": self.plan.pk})
        self.assertEqual([s.workspace_id for s in response.context["subscriptions"]], [self.workspace.pk])
        self.assertEqual(response.context["stats"]["active"], 1)
        response = self.client.get(reverse("personal:platform_subscribers"), {"status": "inactive"})
        self.assertEqual([s.workspace_id for s in response.context["subscriptions"]], [other_workspace.pk])
        self.assertEqual(self.client.get(reverse("personal:platform_subscribers"), {"plan": "9" * 100}).status_code, 200)

    def test_admin_ai_summary_keeps_school_and_mansour_usage_separate_and_cost_partial(self):
        self.activate_paid()
        school = School.objects.create(name="مدرسة أخرى", code="independent-ai-scope", stage="primary", gender="boys")
        for stage, tenant, cost, tokens in (
            (AiUsageEvent.Stage.REPORT_IMPROVE, None, Decimal("0.003"), 10),
            (AiUsageEvent.Stage.TRANSCRIPTION, None, None, 0),
            (AiUsageEvent.Stage.REPORT_IMPROVE, school, Decimal("10"), 900),
            (AiUsageEvent.Stage.MANSOUR, None, Decimal("20"), 800),
        ):
            AiUsageEvent.objects.create(teacher=self.teacher, school=tenant, stage=stage, input_tokens=tokens, estimated_cost=cost)
        self.client.force_login(self.admin)
        response = self.client.get(reverse("personal:platform_subscriber_detail", args=[self.workspace.pk]))
        summary = response.context["ai_summary"]
        self.assertEqual((summary["calls"], summary["input_tokens"], summary["unpriced"]), (2, 10, 1))
        self.assertEqual(summary["cost"], Decimal("0.003"))
        self.assertContains(response, "النداءات المسعّرة فقط")

    def test_pending_payment_is_visible_to_admin_without_activating_subscription(self):
        PersonalPayment.objects.create(
            workspace=self.workspace, plan=self.plan, plan_name=self.plan.name, duration_days=365,
            amount=self.plan.price, customer_name=self.teacher.name, customer_email="teacher@example.test",
        )
        self.client.force_login(self.admin)
        response = self.client.get(reverse("personal:platform_subscriber_detail", args=[self.workspace.pk]))
        self.assertContains(response, "بانتظار الدفع")
        self.assertContains(response, "لم تُفعّل")
        self.subscription.refresh_from_db()
        self.assertNotEqual(self.subscription.plan_id, self.plan.pk)

    def test_ajax_save_retry_and_edit_confirm_owned_destination_without_duplicate_creation(self):
        import uuid
        payload = {
            "client_submission_id": str(uuid.uuid4()), "title": "تقرير مستقل",
            "category": "نشاط", "report_date": timezone.localdate().isoformat(),
            "academic_year": "1448-1449", "description": "نشاط القراءة وتوثيق نتائجه",
            "show_details": "on", "status": "complete",
        }
        for _ in range(2):
            response = self.client.post(reverse("personal:report_create"), payload, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
            self.assertEqual(response.status_code, 200)
            report = self.workspace.reports.get()
            self.assertEqual(response.json(), {"ok": True, "redirect_url": reverse("personal:report_detail", args=[report.pk])})
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.reports_created, 1)
        edited = self.client.post(reverse("personal:report_edit", args=[report.pk]), {
            **payload, "title": "تقرير مستقل محدث",
        }, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(edited.status_code, 200)
        self.assertTrue(edited.json()["ok"])
        report.refresh_from_db()
        self.assertEqual(report.title, "تقرير مستقل محدث")
