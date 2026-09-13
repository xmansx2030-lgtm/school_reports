"""Regression coverage for report tenant boundaries without an active school."""

from __future__ import annotations

from datetime import date

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse

from reports.forms import ReportForm
from reports.models import (
    Report,
    ReportType,
    School,
    SchoolMembership,
    SchoolSubscription,
    SubscriptionPlan,
    Teacher,
)
from reports.services_reports import get_reporttype_choices, get_teacher_reports_queryset


@override_settings(ALLOWED_HOSTS=["testserver"])
class ReportTenantIsolationTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(name="مدرسة النطاق", code="tenant-a")
        self.other_school = School.objects.create(name="مدرسة أخرى", code="tenant-b")
        plan = SubscriptionPlan.objects.create(
            name="خطة اختبار العزل",
            price=0,
            days_duration=30,
            max_teachers=20,
        )
        SchoolSubscription.objects.create(school=self.school, plan=plan)
        SchoolSubscription.objects.create(school=self.other_school, plan=plan)
        self.teacher = Teacher.objects.create_user(
            phone="500991001",
            name="معلم اختبار العزل",
            password="tenant-test-pass",
        )
        self.category = ReportType.objects.create(
            school=self.school,
            code="school-a-category",
            name="تصنيف المدرسة الأولى",
        )
        self.other_category = ReportType.objects.create(
            school=self.other_school,
            code="school-b-category",
            name="تصنيف المدرسة الأخرى",
        )
        self.report = Report.objects.create(
            school=self.school,
            teacher=self.teacher,
            teacher_name=self.teacher.name,
            title="تقرير محمي بحد المدرسة",
            report_date=date(2026, 9, 13),
            category=self.category,
        )
        self.client.force_login(self.teacher)

    def _select_school(self):
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.teacher,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        session = self.client.session
        session["active_school_id"] = self.school.pk
        session.save()

    def test_unassigned_teacher_is_redirected_before_add_form_is_built(self):
        response = self.client.get(reverse("reports:add_report"))

        self.assertRedirects(
            response,
            reverse("reports:select_school"),
            fetch_redirect_response=False,
        )

    def test_unassigned_teacher_cannot_post_a_report(self):
        response = self.client.post(
            reverse("reports:add_report"),
            {
                "title": "محاولة بلا مدرسة",
                "report_date": "2026-09-13",
                "category": self.other_category.code,
            },
        )

        self.assertRedirects(
            response,
            reverse("reports:select_school"),
            fetch_redirect_response=False,
        )
        self.assertFalse(Report.objects.filter(title="محاولة بلا مدرسة").exists())

    def test_report_form_has_no_categories_without_tenant_context(self):
        form = ReportForm()

        self.assertFalse(form.fields["category"].queryset.exists())

    def test_missing_tenant_ignores_a_stale_unscoped_category_cache(self):
        cache.set(
            "reporttype-choices:v1:s0",
            [(self.other_category.code, self.other_category.name)],
            120,
        )

        self.assertEqual(get_reporttype_choices(active_school=None), [])

    def test_active_school_form_does_not_accept_another_schools_category(self):
        self._select_school()
        response = self.client.post(
            reverse("reports:add_report"),
            {
                "section_selection_enabled": "1",
                "title": "محاولة تصنيف عابر للمدارس",
                "report_date": "2026-09-13",
                "category": self.other_category.code,
                "show_details": "on",
                "idea": "تفاصيل مكتملة للاختبار",
                "show_beneficiaries": "on",
                "beneficiaries_count": "1",
                "evidence-TOTAL_FORMS": "0",
                "evidence-INITIAL_FORMS": "0",
                "evidence-MIN_NUM_FORMS": "0",
                "evidence-MAX_NUM_FORMS": "8",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("category", response.context["form"].errors)
        self.assertFalse(
            Report.objects.filter(title="محاولة تصنيف عابر للمدارس").exists()
        )

    def test_model_rejects_a_school_category_on_another_tenants_report(self):
        invalid = Report(
            school=self.school,
            teacher=self.teacher,
            title="رابط مستأجر غير صالح",
            report_date=date(2026, 9, 13),
            category=self.other_category,
        )

        with self.assertRaises(ValidationError):
            invalid.save()

    def test_owned_reports_do_not_cross_a_missing_tenant_boundary(self):
        queryset = get_teacher_reports_queryset(
            user=self.teacher,
            active_school=None,
        )

        self.assertFalse(queryset.exists())

    def test_report_workspaces_require_an_active_school(self):
        for route_name, method in (
            ("reports:my_reports", "get"),
            ("reports:edit_my_report", "get"),
            ("reports:report_print", "get"),
            ("reports:report_share_manage", "get"),
            ("reports:delete_my_report", "post"),
        ):
            with self.subTest(route_name=route_name):
                args = [] if route_name == "reports:my_reports" else [self.report.pk]
                response = getattr(self.client, method)(reverse(route_name, args=args))
                self.assertRedirects(
                    response,
                    reverse("reports:select_school"),
                    fetch_redirect_response=False,
                )

    def test_home_does_not_aggregate_reports_without_an_active_school(self):
        response = self.client.get(reverse("reports:home"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, self.report.title)
