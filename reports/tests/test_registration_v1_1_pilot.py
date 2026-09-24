from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from reports.models import (
    School,
    SchoolArchiveAddon,
    SchoolMembership,
    SchoolSubscription,
    SubscriptionPlan,
    Teacher,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@override_settings(ALLOWED_HOSTS=["testserver"], RATELIMIT_ENABLE=False)
class RegistrationV11PilotTests(TestCase):
    def payload(self, **overrides):
        data = {
            "school_name": "مدرسة رحلة التسجيل",
            "stage": School.Stage.PRIMARY,
            "gender": School.Gender.BOYS,
            "city": "الرياض",
            "manager_name": "مدير رحلة التسجيل",
            "manager_phone": "0557654321",
            "manager_email": "registration-pilot@example.edu.sa",
            "password": "Registration#2026",
            "password_confirm": "Registration#2026",
            "accept_policies": "on",
        }
        data.update(overrides)
        return data

    def test_core_templates_use_external_assets_without_embedded_presentation(self):
        templates = [
            PROJECT_ROOT / "reports/templates/reports/register_school.html",
            PROJECT_ROOT / "reports/templates/reports/registration_success.html",
        ]
        for template_path in templates:
            source = template_path.read_text(encoding="utf-8")
            self.assertNotIn("<style", source, template_path.name)
            self.assertNotRegex(source, r"<script(?![^>]+\bsrc=)[^>]*>")
            self.assertNotRegex(source, r"\sstyle\s*=")
            self.assertIn("css/registration.css", source)
            self.assertIn("js/registration.js", source)

        css = (PROJECT_ROOT / "static/css/registration.css").read_text(encoding="utf-8")
        javascript = (PROJECT_ROOT / "static/js/registration.js").read_text(encoding="utf-8")
        self.assertNotRegex(css, r"#[0-9a-fA-F]{3,8}\b")
        self.assertNotRegex(css, r"\b(?:rgb|rgba|hsl|hsla)\(")
        self.assertNotRegex(css, r"(?:margin|padding|border)-(?:left|right)\b")
        self.assertNotRegex(css, r"\b(?:left|right)\s*:")
        self.assertNotIn("transition: all", css)
        self.assertNotIn("!important", css)
        self.assertNotRegex(javascript, r"\.style(?:\.|\[)")
        self.assertNotIn('setAttribute("style"', javascript)

    def test_invalid_post_has_linked_error_summary_and_does_not_echo_password(self):
        response = self.client.post(
            reverse("reports:register_school"),
            self.payload(manager_phone="12345", manager_email="invalid", password="Secret#123", password_confirm="different"),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "راجع البيانات قبل إنشاء المدرسة")
        self.assertContains(response, 'data-registration-error-summary')
        self.assertContains(response, 'href="#id_manager_phone"')
        self.assertContains(response, 'aria-invalid="true"')
        self.assertContains(response, "id_manager_phone_help id_manager_phone_error")
        self.assertNotContains(response, "Secret#123")
        self.assertFalse(School.objects.exists())

    def test_provisioning_rolls_back_every_record_when_archive_step_fails(self):
        counts_before = {
            "schools": School.objects.count(),
            "teachers": Teacher.objects.count(),
            "memberships": SchoolMembership.objects.count(),
            "plans": SubscriptionPlan.objects.count(),
            "subscriptions": SchoolSubscription.objects.count(),
            "archive": SchoolArchiveAddon.objects.count(),
        }

        with patch(
            "reports.views.onboarding.SchoolArchiveAddon.objects.create",
            side_effect=RuntimeError("forced archive failure"),
        ):
            response = self.client.post(reverse("reports:register_school"), self.payload())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "لم تُحفظ أي بيانات")
        self.assertEqual(School.objects.count(), counts_before["schools"])
        self.assertEqual(Teacher.objects.count(), counts_before["teachers"])
        self.assertEqual(SchoolMembership.objects.count(), counts_before["memberships"])
        self.assertEqual(SubscriptionPlan.objects.count(), counts_before["plans"])
        self.assertEqual(SchoolSubscription.objects.count(), counts_before["subscriptions"])
        self.assertEqual(SchoolArchiveAddon.objects.count(), counts_before["archive"])

    def test_failure_log_does_not_include_raw_phone_or_password(self):
        phone = "0557654321"
        password = "Registration#2026"
        with patch(
            "reports.views.onboarding.SchoolArchiveAddon.objects.create",
            side_effect=RuntimeError("forced archive failure"),
        ), self.assertLogs("reports.views.onboarding", level="ERROR") as captured:
            response = self.client.post(
                reverse("reports:register_school"),
                self.payload(manager_phone=phone, password=password, password_confirm=password),
            )

        self.assertEqual(response.status_code, 200)
        logs = "\n".join(captured.output)
        self.assertNotIn(phone, logs)
        self.assertNotIn(password, logs)
        self.assertIn("trace_id=", logs)

    def test_public_fields_cannot_escalate_role_or_forge_trial(self):
        response = self.client.post(
            reverse("reports:register_school"),
            self.payload(
                role_type=SchoolMembership.RoleType.MANAGER,
                is_superuser="1",
                is_staff="1",
                is_active="0",
                plan="enterprise",
                days_duration="9999",
                storage_limit_gb="9999",
                school_status="approved",
                next="https://attacker.example/",
            ),
        )

        self.assertRedirects(response, reverse("reports:registration_success"), fetch_redirect_response=False)
        manager = Teacher.objects.get(phone="0557654321")
        school = School.objects.get(name="مدرسة رحلة التسجيل")
        membership = SchoolMembership.objects.get(teacher=manager, school=school)
        subscription = SchoolSubscription.objects.select_related("plan").get(school=school)
        archive = SchoolArchiveAddon.objects.get(school=school)
        self.assertFalse(manager.is_superuser)
        self.assertFalse(manager.is_staff)
        self.assertEqual(membership.role_type, SchoolMembership.RoleType.MANAGER)
        self.assertTrue(membership.is_active)
        self.assertTrue(school.is_active)
        self.assertEqual(subscription.plan.price, 0)
        self.assertEqual(subscription.plan.days_duration, 30)
        self.assertEqual(subscription.plan.max_teachers, 5)
        self.assertEqual(archive.storage_limit_gb, 1)

    def test_receipt_is_session_bound_consumed_once_and_not_url_addressable(self):
        response = self.client.post(reverse("reports:register_school"), self.payload())
        self.assertEqual(response.status_code, 302)

        unrelated_client = Client()
        direct = unrelated_client.get(reverse("reports:registration_success"))
        self.assertEqual(direct.status_code, 302)
        self.assertIn(reverse("reports:login"), direct.url)

        success = self.client.get(reverse("reports:registration_success"))
        self.assertEqual(success.status_code, 200)
        self.assertContains(success, "Registration#2026")
        self.assertNotIn("Registration#2026", success.request["PATH_INFO"])
        self.assertIn("no-store", success.headers["Cache-Control"])

        refresh = self.client.get(reverse("reports:registration_success"))
        self.assertRedirects(refresh, reverse("reports:admin_dashboard"), fetch_redirect_response=False)

    def test_registration_logs_user_in_and_hands_off_to_existing_authentication(self):
        response = self.client.post(reverse("reports:register_school"), self.payload())
        school = School.objects.get(name="مدرسة رحلة التسجيل")
        manager = Teacher.objects.get(phone="0557654321")
        session = self.client.session
        self.assertEqual(int(session["_auth_user_id"]), manager.pk)
        self.assertEqual(session["active_school_id"], school.pk)
        self.assertRedirects(response, reverse("reports:registration_success"), fetch_redirect_response=False)

        success = self.client.get(reverse("reports:registration_success"))
        self.assertContains(success, f'href="{reverse("reports:login")}"')
        self.assertContains(success, f'href="{reverse("reports:admin_dashboard")}"')

    def test_supported_saudi_mobile_formats_use_the_existing_canonical_identity(self):
        cases = ("0557654321", "557654321", "+966557654321")
        for index, phone in enumerate(cases):
            with self.subTest(phone=phone):
                response = self.client.post(
                    reverse("reports:register_school"),
                    self.payload(
                        school_name=f"مدرسة صيغة الجوال {index}",
                        manager_phone=phone,
                        manager_email=f"mobile-{index}@example.edu.sa",
                    ),
                )
                self.assertEqual(response.status_code, 302)
                self.assertTrue(Teacher.objects.filter(phone="0557654321").exists())
                self.client.logout()
                if index < len(cases) - 1:
                    teacher = Teacher.objects.get(phone="0557654321")
                    School.objects.filter(memberships__teacher=teacher).delete()
                    teacher.delete()
                    SubscriptionPlan.objects.all().delete()

    def test_forged_post_marketing_fields_do_not_override_server_attribution(self):
        self.client.get(reverse("reports:register_school"), {"utm_source": "trusted-campaign"})
        response = self.client.post(
            reverse("reports:register_school"),
            self.payload(marketing_source="forged", marketing_referrer="attacker.example"),
        )
        self.assertEqual(response.status_code, 302)
        school = School.objects.get(name="مدرسة رحلة التسجيل")
        self.assertEqual(school.marketing_source, "trusted-campaign")
        self.assertNotEqual(school.marketing_referrer, "attacker.example")


@override_settings(ALLOWED_HOSTS=["testserver"], RATELIMIT_ENABLE=False)
class RegistrationCsrfTests(TestCase):
    def test_registration_post_requires_csrf(self):
        client = Client(enforce_csrf_checks=True)
        response = client.post(reverse("reports:register_school"), {"school_name": "مدرسة"})
        self.assertEqual(response.status_code, 403)


@override_settings(ALLOWED_HOSTS=["testserver"], RATELIMIT_ENABLE=True)
class RegistrationRateLimitTests(TestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_public_registration_post_is_limited_to_five_attempts_per_hour(self):
        client = Client()
        statuses = []
        for _ in range(6):
            response = client.post(
                reverse("reports:register_school"),
                {"school_name": ""},
                REMOTE_ADDR="203.0.113.25",
            )
            statuses.append(response.status_code)
        self.assertEqual(statuses[:5], [200, 200, 200, 200, 200])
        self.assertEqual(statuses[5], 403)
