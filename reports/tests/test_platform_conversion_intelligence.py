from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from reports.conversion_analytics import build_conversion_rows, safe_ai_snapshot
from reports.models import (
    AiUsageEvent,
    ApprovalState,
    AuditLog,
    Notification,
    PlatformEmail,
    PlatformEmailConfiguration,
    Report,
    ReportType,
    School,
    SchoolConversionOutreach,
    SchoolMembership,
    SchoolSubscription,
    SubscriptionPlan,
    Teacher,
    Ticket,
)


@override_settings(ALLOWED_HOSTS=["testserver"])
class PlatformConversionIntelligenceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = Teacher.objects.create_superuser(
            phone="500990001", name="مالك منصة الاختبار", password="pass"
        )
        cls.regular = Teacher.objects.create_user(
            phone="500990002", name="مستخدم عادي", password="pass"
        )
        cls.manager = Teacher.objects.create_user(
            phone="500990003",
            name="مدير مدرسة النور",
            email="manager@nour.example",
            password="pass",
        )
        cls.other_manager = Teacher.objects.create_user(
            phone="500990004",
            name="مدير مدرسة أخرى",
            email="other@example.test",
            password="pass",
        )
        cls.staff = Teacher.objects.create_user(
            phone="500990005", name="معلم غير مستهدف", email="teacher@example.test", password="pass"
        )
        cls.free_plan = SubscriptionPlan.objects.create(
            name="التجربة المجانية", price=Decimal("0"), days_duration=30, max_teachers=5
        )
        cls.paid_plan = SubscriptionPlan.objects.create(
            name="توثيق السنوية", price=Decimal("1490"), days_duration=365, max_teachers=25
        )
        cls.school = School.objects.create(
            name="مدرسة النور النموذجية", code="conversion-nour", city="الرياض"
        )
        cls.other_school = School.objects.create(
            name="مدرسة الأفق", code="conversion-other", city="جدة"
        )
        cls.subscription = SchoolSubscription.objects.create(
            school=cls.school, plan=cls.free_plan
        )
        cls.other_subscription = SchoolSubscription.objects.create(
            school=cls.other_school, plan=cls.free_plan
        )
        SchoolMembership.objects.create(
            school=cls.school,
            teacher=cls.manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )
        SchoolMembership.objects.create(
            school=cls.school,
            teacher=cls.staff,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        SchoolMembership.objects.create(
            school=cls.other_school,
            teacher=cls.other_manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )
        cls.report_type = ReportType.objects.create(
            name="برامج المدرسة", code="conversion-programs", school=cls.school
        )
        cls.secret_title = "عنوان سري لا يجوز إرساله للذكاء الاصطناعي"
        Report.objects.create(
            school=cls.school,
            teacher=cls.manager,
            category=cls.report_type,
            title=cls.secret_title,
            idea="محتوى خاص جدًا داخل تقرير المدرسة",
            report_date=timezone.localdate(),
            approval_state=ApprovalState.APPROVED,
        )
        Ticket.objects.create(
            school=cls.school,
            creator=cls.manager,
            title="طلب داخلي",
            status=Ticket.Status.DONE,
            is_platform=False,
        )
        AiUsageEvent.objects.create(
            school=cls.school,
            teacher=cls.manager,
            stage=AiUsageEvent.Stage.REPORT_IMPROVE,
            model_name="test-model",
            outcome=AiUsageEvent.Outcome.SUCCESS,
        )

    def setUp(self):
        self.client.force_login(self.admin)

    def _draft(self):
        row = build_conversion_rows([self.school])[0]
        return SchoolConversionOutreach.objects.create(
            school=self.school,
            subscription=self.subscription,
            created_by=self.admin,
            context_snapshot=safe_ai_snapshot(row),
            email_subject="استمرار استفادتكم من توثيق",
            email_body="رسالة بريد مراجعة وآمنة.",
            in_app_title="لنواصل الاستفادة",
            in_app_body="رسالة داخلية مراجعة وآمنة.",
            whatsapp_body="مسودة للنسخ اليدوي فقط.",
        )

    def test_workspace_is_platform_superuser_only(self):
        self.client.force_login(self.regular)
        response = self.client.get(reverse("reports:platform_conversion_dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("reports:platform_login"), response.url)

    def test_scores_cover_multiple_tawtheeq_services_and_are_deterministic(self):
        first = build_conversion_rows([self.school])[0]
        second = build_conversion_rows([self.school])[0]

        self.assertEqual(first["utilization_score"], second["utilization_score"])
        self.assertEqual(first["intent_score"], second["intent_score"])
        self.assertGreaterEqual(first["services_used"], 3)
        self.assertSetEqual(
            {item["key"] for item in first["services"]},
            {"reports", "tickets", "ai"},
        )
        self.assertEqual(sum(item["points"] for item in first["score_factors"]), first["utilization_score"])

    def test_expired_free_subscription_is_segmented_without_ai_guessing(self):
        SchoolSubscription.objects.filter(pk=self.subscription.pk).update(
            end_date=timezone.localdate() - timedelta(days=1)
        )
        self.subscription.refresh_from_db()
        row = build_conversion_rows([self.school])[0]
        self.assertEqual(row["segment"], "trial_expired")
        self.assertEqual(row["segment_label"], "مجانية منتهية")

    def test_safe_snapshot_excludes_report_content_and_personal_identifiers(self):
        row = build_conversion_rows([self.school])[0]
        payload = json.dumps(safe_ai_snapshot(row), ensure_ascii=False)

        self.assertNotIn(self.secret_title, payload)
        self.assertNotIn("محتوى خاص جدًا", payload)
        self.assertNotIn(self.manager.phone, payload)
        self.assertNotIn(self.manager.email, payload)
        self.assertIn("التقارير والتوثيق", payload)

    @override_settings(OPENAI_API_KEY="")
    def test_generation_falls_back_to_reviewable_copy_without_sending(self):
        response = self.client.post(
            reverse("reports:platform_conversion_generate", args=[self.school.pk]),
            {"objective": "convert", "tone": "executive"},
        )
        self.assertEqual(response.status_code, 302)
        outreach = SchoolConversionOutreach.objects.get(school=self.school)
        self.assertFalse(outreach.ai_generated)
        self.assertEqual(outreach.status, SchoolConversionOutreach.Status.DRAFT)
        self.assertTrue(outreach.email_body)
        self.assertEqual(outreach.platform_email_ids, [])
        self.assertTrue(
            AuditLog.objects.filter(
                model_name="SchoolConversionOutreach", object_id=outreach.pk
            ).exists()
        )

    def test_generation_passes_only_the_safe_snapshot_to_drafter(self):
        generated = {
            "analysis_summary": "تحليل آمن",
            "recommended_action": "خطوة آمنة",
            "email_subject": "عنوان",
            "email_body": "نص البريد",
            "in_app_title": "إشعار",
            "in_app_body": "نص الإشعار",
            "whatsapp_body": "نص واتساب",
            "call_script": ["أولًا", "ثانيًا", "ثالثًا"],
            "ai_generated": True,
            "ai_model": "test-model",
        }
        with patch(
            "reports.views.conversions.generate_conversion_draft", return_value=generated
        ) as drafter:
            self.client.post(
                reverse("reports:platform_conversion_generate", args=[self.school.pk]),
                {"objective": "convert", "tone": "supportive"},
            )
        snapshot = drafter.call_args.args[0]
        raw = json.dumps(snapshot, ensure_ascii=False)
        self.assertNotIn(self.secret_title, raw)
        self.assertNotIn(self.manager.phone, raw)
        self.assertEqual(snapshot["school"]["display_name"], self.school.name)

    def test_send_requires_explicit_human_confirmation(self):
        outreach = self._draft()
        response = self.client.post(
            reverse(
                "reports:platform_conversion_send", args=[self.school.pk, outreach.pk]
            ),
            {
                "email_subject": outreach.email_subject,
                "email_body": outreach.email_body,
                "in_app_title": outreach.in_app_title,
                "in_app_body": outreach.in_app_body,
                "whatsapp_body": outreach.whatsapp_body,
                "send_email": "on",
                "send_in_app": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        outreach.refresh_from_db()
        self.assertEqual(outreach.status, SchoolConversionOutreach.Status.DRAFT)

    @override_settings(RESEND_API_KEY="re_test")
    def test_send_targets_manager_only_and_never_sends_whatsapp(self):
        config = PlatformEmailConfiguration.load()
        config.is_sending_enabled = True
        config.save()
        outreach = self._draft()

        def make_notification(title, message, **kwargs):
            return Notification.objects.create(
                title=title, message=message, school=kwargs["school"]
            )

        with (
            patch(
                "reports.views.conversions.send_platform_email",
                return_value=SimpleNamespace(pk=901),
            ) as send_email,
            patch(
                "reports.views.conversions.create_system_notification",
                side_effect=make_notification,
            ) as send_notification,
        ):
            response = self.client.post(
                reverse(
                    "reports:platform_conversion_send", args=[self.school.pk, outreach.pk]
                ),
                {
                    "email_subject": outreach.email_subject,
                    "email_body": outreach.email_body,
                    "in_app_title": outreach.in_app_title,
                    "in_app_body": outreach.in_app_body,
                    "whatsapp_body": outreach.whatsapp_body,
                    "send_email": "on",
                    "send_in_app": "on",
                    "confirm_reviewed": "on",
                },
            )

        self.assertEqual(response.status_code, 302)
        outreach.refresh_from_db()
        self.assertEqual(outreach.status, SchoolConversionOutreach.Status.SENT)
        self.assertEqual(send_email.call_args.kwargs["to"], [self.manager.email])
        self.assertNotIn(self.staff.email, send_email.call_args.kwargs["to"])
        self.assertNotIn(self.other_manager.email, send_email.call_args.kwargs["to"])
        self.assertEqual(send_notification.call_args.kwargs["teacher_ids"], [self.manager.pk])
        self.assertEqual(outreach.whatsapp_body, "مسودة للنسخ اليدوي فقط.")

    @override_settings(RESEND_API_KEY="re_test")
    def test_retry_does_not_repeat_a_channel_that_already_succeeded(self):
        config = PlatformEmailConfiguration.load()
        config.is_sending_enabled = True
        config.save()
        outreach = self._draft()
        outreach.email_status = SchoolConversionOutreach.ChannelStatus.SENT
        outreach.status = SchoolConversionOutreach.Status.PARTIAL
        outreach.platform_email_ids = [800]
        outreach.save()

        with (
            patch("reports.views.conversions.send_platform_email") as send_email,
            patch(
                "reports.views.conversions.create_system_notification",
                return_value=Notification.objects.create(
                    title="نجاح", message="اختبار", school=self.school
                ),
            ) as send_notification,
        ):
            self.client.post(
                reverse(
                    "reports:platform_conversion_send", args=[self.school.pk, outreach.pk]
                ),
                {
                    "email_subject": outreach.email_subject,
                    "email_body": outreach.email_body,
                    "in_app_title": outreach.in_app_title,
                    "in_app_body": outreach.in_app_body,
                    "whatsapp_body": outreach.whatsapp_body,
                    "send_email": "on",
                    "send_in_app": "on",
                    "confirm_reviewed": "on",
                },
            )
        send_email.assert_not_called()
        send_notification.assert_called_once()
        outreach.refresh_from_db()
        self.assertEqual(outreach.status, SchoolConversionOutreach.Status.SENT)
        self.assertEqual(outreach.platform_email_ids, [800])

    def test_pages_have_one_h1_and_responsive_mobile_cards(self):
        dashboard = self.client.get(reverse("reports:platform_conversion_dashboard"))
        detail = self.client.get(
            reverse("reports:platform_conversion_school", args=[self.school.pk])
        )
        self.assertEqual(dashboard.status_code, 200)
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(dashboard.content.count(b"<h1"), 1)
        self.assertEqual(detail.content.count(b"<h1"), 1)
        self.assertContains(dashboard, "conversion-mobile-list")
        self.assertContains(detail, "كيف حُسبت الاستفادة؟")

    def test_latest_draft_is_loaded_into_editable_review_fields(self):
        outreach = self._draft()
        response = self.client.get(
            reverse("reports:platform_conversion_school", args=[self.school.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["outreach"], outreach)
        self.assertEqual(
            response.context["send_form"]["email_subject"].value(),
            outreach.email_subject,
        )
        self.assertContains(response, outreach.email_subject)

    @override_settings(RESEND_API_KEY="")
    def test_enabled_mailbox_stays_selectable_without_local_provider_secret(self):
        config = PlatformEmailConfiguration.load()
        config.is_sending_enabled = True
        config.save()
        PlatformEmail.objects.create(
            direction=PlatformEmail.Direction.OUTBOUND,
            status=PlatformEmail.Status.DELIVERED,
            from_email=config.sender_email,
            to_emails=[self.manager.email],
            subject="دليل تسليم سابق",
            sent_at=timezone.now(),
            delivered_at=timezone.now(),
            last_event_at=timezone.now(),
        )
        self._draft()

        dashboard = self.client.get(reverse("reports:platform_conversion_dashboard"))
        detail = self.client.get(
            reverse("reports:platform_conversion_school", args=[self.school.pk])
        )

        self.assertContains(dashboard, "البريد الرسمي مفعّل")
        self.assertNotContains(dashboard, "قناة البريد غير جاهزة بالكامل")
        self.assertContains(detail, "مفعّل عبر مركز بريد توثيق")
        self.assertContains(detail, "آخر نجاح")
        self.assertFalse(detail.context["send_form"].fields["send_email"].disabled)

    def test_explicitly_disabled_mailbox_disables_email_channel(self):
        config = PlatformEmailConfiguration.load()
        config.is_sending_enabled = False
        config.save()
        self._draft()

        dashboard = self.client.get(reverse("reports:platform_conversion_dashboard"))
        detail = self.client.get(
            reverse("reports:platform_conversion_school", args=[self.school.pk])
        )

        self.assertContains(dashboard, "إرسال البريد متوقف من إعدادات المنصة")
        self.assertTrue(detail.context["send_form"].fields["send_email"].disabled)

    def test_conversion_css_uses_semantic_tokens_without_raw_colors(self):
        css = Path("static/css/conversion-intelligence.css").read_text(encoding="utf-8")
        self.assertNotRegex(css, r"#[0-9a-fA-F]{3,8}\b")
        self.assertNotIn("transition: all", css)
        self.assertIn("var(--twq-primary)", css)
