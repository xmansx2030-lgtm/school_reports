from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from reports.guidance import school_readiness
from reports.models import School, SchoolSubscription


@override_settings(
    ALLOWED_HOSTS=["testserver"],
    RATELIMIT_ENABLE=False,
    TRIAL_PLAN_NAME="التجربة المجانية",
)
class ManagerFirstRunExperienceTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def _registration_payload(self) -> dict[str, str]:
        return {
            "school_name": "مدرسة رحلة اليوم الأول",
            "stage": School.Stage.PRIMARY,
            "gender": School.Gender.BOYS,
            "city": "الرياض",
            "manager_name": "مدير اليوم الأول",
            "manager_phone": "+966 55 333 2244",
            "manager_email": "first-day@example.edu.sa",
            "password": "FirstDay#2026",
            "password_confirm": "FirstDay#2026",
            "accept_policies": "on",
        }

    def _register(self) -> School:
        response = self.client.post(
            reverse("reports:register_school"),
            self._registration_payload(),
        )
        self.assertRedirects(
            response,
            reverse("reports:registration_success"),
            fetch_redirect_response=False,
        )
        return School.objects.get(name="مدرسة رحلة اليوم الأول")

    def _open_dashboard(self):
        self.client.get(reverse("reports:registration_success"))
        return self.client.get(reverse("reports:admin_dashboard"))

    def test_new_manager_lands_in_a_guided_setup_mode(self):
        self._register()

        response = self._open_dashboard()
        html = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["manager_onboarding"])
        self.assertEqual(response.context["setup_completed"], 1)
        self.assertEqual(response.context["setup_total"], 6)
        self.assertEqual(response.context["setup_percent"], 17)
        self.assertEqual(response.context["setup_next_step"]["key"], "profile")
        self.assertFalse(response.context["has_dashboard_activity"])

        self.assertContains(response, 'data-manager-mode="setup"')
        self.assertContains(response, "ابدأ بـ بيانات المدرسة والسنة الحالية")
        self.assertContains(response, "إعداد المدرسة خطوة بخطوة")
        self.assertLess(html.index('id="managerSetup"'), html.index('id="managerToday"'))
        self.assertEqual(html.count('role="progressbar"'), 2)
        self.assertContains(response, "لا توجد مهام تشغيلية معلّقة بعد")
        self.assertNotContains(response, "لا شيء ينتظرك الآن")
        self.assertContains(response, "لا توجد بيانات أداء بعد — وهذا طبيعي")
        self.assertNotContains(response, 'id="ticketCompletionRate"')

    def test_trial_is_welcoming_on_day_one_but_warns_near_expiry(self):
        school = self._register()

        first_day = self._open_dashboard()
        self.assertTrue(first_day.context["is_trial_subscription"])
        self.assertEqual(first_day.context["subscription_days_inclusive"], 30)
        self.assertIsNone(first_day.context["subscription_warning"])
        self.assertContains(first_day, "التجربة مفعّلة")
        self.assertNotContains(first_day, "موعد التجديد يقترب")

        SchoolSubscription.objects.filter(school=school).update(
            end_date=timezone.localdate() + timedelta(days=6)
        )
        near_expiry = self.client.get(reverse("reports:admin_dashboard"))
        self.assertEqual(near_expiry.context["subscription_warning"], "critical")
        self.assertContains(near_expiry, "متبقي 6 أيام على انتهاء الاشتراك")

    def test_non_applicable_staff_scopes_do_not_inflate_readiness(self):
        self._register()

        response = self._open_dashboard()
        scope_step = next(
            step
            for step in response.context["setup_steps"]
            if step["key"] == "staff_scopes"
        )

        self.assertFalse(scope_step["applicable"])
        self.assertContains(response, "غير مطلوب حاليًا؛ يظهر عند إضافة وكيل أو موظف إداري.")

    def test_optional_locked_city_does_not_trap_the_manager_in_profile_setup(self):
        payload = self._registration_payload()
        payload["city"] = ""
        response = self.client.post(reverse("reports:register_school"), payload)
        self.assertRedirects(
            response,
            reverse("reports:registration_success"),
            fetch_redirect_response=False,
        )
        school = School.objects.get(name="مدرسة رحلة اليوم الأول")
        school.current_academic_year = "1448-1449"
        school.save(update_fields=["current_academic_year"])

        profile_step = next(
            step for step in school_readiness(school)["steps"] if step["key"] == "profile"
        )

        self.assertTrue(profile_step["complete"])
        self.assertNotIn("المدينة", profile_step["description"])

    def test_school_settings_puts_the_first_editable_blocker_first(self):
        self._register()

        response = self.client.get(reverse("reports:school_settings"))
        html = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ابدأ بالسنة الدراسية الحالية")
        self.assertLess(
            html.index('id="id_current_academic_year"'),
            html.index('for="id_email"'),
        )
        self.assertLess(html.index('id="id_current_academic_year"'), html.index('id="submitBtn"'))
        self.assertLess(html.index('id="submitBtn"'), html.index("هوية المدرسة المسجلة"))

        for field_id in (
            "id_current_academic_year",
            "id_email",
            "id_phone",
            "id_share_link_default_days",
            "id_report_approval_enabled",
        ):
            tag = re.search(
                rf'<(?:input|select)\b[^>]*\bid="{field_id}"[^>]*>',
                html,
            )
            self.assertIsNotNone(tag, field_id)
            described_by = re.search(r'aria-describedby="([^"]+)"', tag.group(0))
            self.assertIsNotNone(described_by, field_id)
            for token in described_by.group(1).split():
                self.assertIn(f'id="{token}"', html, (field_id, token))

    def test_school_settings_normalizes_supported_phone_formats_and_rejects_invalid_ones(self):
        school = self._register()
        payload = {
            "current_academic_year": "1448-1449",
            "email": school.email,
            "phone": "+966 55 333 2244",
            "share_link_default_days": "7",
        }

        saved = self.client.post(reverse("reports:school_settings"), payload)
        self.assertRedirects(
            saved,
            reverse("reports:admin_dashboard"),
            fetch_redirect_response=False,
        )
        school.refresh_from_db()
        self.assertEqual(school.phone, "0553332244")

        payload["phone"] = "1234"
        invalid = self.client.post(reverse("reports:school_settings"), payload)
        html = invalid.content.decode("utf-8")

        self.assertEqual(invalid.status_code, 200)
        self.assertContains(invalid, "أدخل رقم جوال سعوديًا صحيحًا")
        phone_tag = re.search(r'<input\b[^>]*\bid="id_phone"[^>]*>', html)
        self.assertIsNotNone(phone_tag)
        described_by = phone_tag.group(0)
        self.assertIn("id_phone_error", described_by)
        self.assertIn('id="id_phone_error"', html)
        school.refresh_from_db()
        self.assertEqual(school.phone, "0553332244")

    def test_dashboard_css_and_live_controls_keep_accessible_state(self):
        project_root = Path(settings.BASE_DIR)
        css = (project_root / "static" / "css" / "design-system.css").read_text(
            encoding="utf-8"
        )
        template = (
            project_root
            / "reports"
            / "templates"
            / "reports"
            / "admin_dashboard.html"
        ).read_text(encoding="utf-8")
        setup_partial = (
            project_root
            / "reports"
            / "templates"
            / "reports"
            / "partials"
            / "manager_dashboard_setup.html"
        ).read_text(encoding="utf-8")

        hero_rule = css.split("body.page .manager-hero h1", 1)[1].split("}", 1)[0]
        self.assertIn("-webkit-text-fill-color: currentColor", hero_rule)
        self.assertIn('labelledBar.setAttribute("aria-label"', template)
        self.assertIn('link.setAttribute("aria-current", "location")', template)
        self.assertIn("var(--header-bottom, var(--header-h, 72px))", template)
        self.assertEqual((template + setup_partial).count('role="progressbar"'), 2)
        self.assertEqual((template + setup_partial).count('aria-valuenow="{{ setup_percent }}"'), 2)
        for period_label_id in (
            "managerPrintPeriodLabel",
            "managerHeroPeriodLabel",
            "managerCoveragePeriodLabel",
            "managerReportsScope",
        ):
            self.assertIn(f'id="{period_label_id}"', template)
            self.assertIn(f'setText("{period_label_id}", periodLabel)', template)
        self.assertNotIn('setText("managerPeriodLabel"', template)
        self.assertIn(
            'aria-describedby="reportsChartSummary"',
            template,
        )
        self.assertIn(
            'aria-describedby="categoriesChartSummary"',
            template,
        )
        for table_id, body_id in (
            ("reportsChartTable", "reportsChartTableBody"),
            ("categoriesChartTable", "categoriesChartTableBody"),
        ):
            self.assertIn(f'id="{table_id}"', template)
            self.assertIn(f'id="{body_id}"', template)
        self.assertIn(
            'paintChartAlternatives(charts, nextPayload.period_label || "الكل")',
            template,
        )
        for period_label_id in (
            "reportsChartPeriodLabel",
            "reportsChartTablePeriodLabel",
            "categoriesChartPeriodLabel",
            "categoriesChartTablePeriodLabel",
        ):
            self.assertIn(f'id="{period_label_id}"', template)
            self.assertIn(f'setText("{period_label_id}", periodLabel)', template)
        self.assertNotIn(
            'class="manager-print-head" aria-hidden="true"',
            template,
        )

        settings_template = (
            project_root
            / "reports"
            / "templates"
            / "reports"
            / "school_settings.html"
        ).read_text(encoding="utf-8")
        settings_css = (project_root / "static" / "css" / "school-settings.css").read_text(
            encoding="utf-8"
        )
        settings_script = (project_root / "static" / "js" / "school-settings.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("css/school-settings.css", settings_template)
        self.assertIn("js/school-settings.js", settings_template)
        self.assertIn(":focus-visible", settings_css)
        focus_rule = settings_css.split(":focus-visible", 1)[1].split("}", 1)[0]
        self.assertIn("outline: 3px solid", focus_rule)
        self.assertIn('window.addEventListener("pageshow"', settings_script)
        self.assertIn("if (event.persisted) restoreSubmitButton();", settings_script)
        self.assertIn('input[name="phone"]', settings_css)
        self.assertIn("direction: ltr;", settings_css)
        self.assertIn('aria-busy="false" aria-describedby="saveStatus"', settings_template)
        self.assertIn('id="saveStatus" role="status" aria-live="polite"', settings_template)
        self.assertIn('form.setAttribute("aria-busy", "true")', settings_script)
        self.assertIn('form.setAttribute("aria-busy", "false")', settings_script)
        self.assertIn('if (saveStatus) saveStatus.textContent = "";', settings_script)
