from pathlib import Path
import re

from django.conf import settings
from django.test import TestCase, override_settings
from django.urls import reverse

from reports.models import (
    School,
    SchoolMembership,
    SchoolSubscription,
    SubscriptionPlan,
    Teacher,
)


@override_settings(ALLOWED_HOSTS=["testserver"])
class TeacherHomeDashboardUITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = School.objects.create(
            name="مدرسة المنزل اليومي",
            code="teacher-daily-home",
        )
        plan = SubscriptionPlan.objects.create(
            name="خطة المنزل اليومي",
            price=0,
            days_duration=30,
            max_teachers=0,
        )
        SchoolSubscription.objects.create(school=cls.school, plan=plan)
        cls.teacher = Teacher.objects.create_user(
            phone="500198001",
            name="معلم المنزل اليومي",
            password="teacher-home-pass",
        )
        SchoolMembership.objects.create(
            school=cls.school,
            teacher=cls.teacher,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        cls.admin_staff = Teacher.objects.create_user(
            phone="500198002",
            name="موظف المنزل اليومي",
            password="staff-home-pass",
        )
        SchoolMembership.objects.create(
            school=cls.school,
            teacher=cls.admin_staff,
            role_type=SchoolMembership.RoleType.ADMIN_STAFF,
        )

    def _open_home(self, user):
        self.client.force_login(user)
        session = self.client.session
        session["active_school_id"] = self.school.pk
        session.save()
        return self.client.get(reverse("reports:home"))

    def test_teacher_gets_daily_work_home_before_recent_work(self):
        response = self._open_home(self.teacher)
        html = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertIn('class="twq-page teacher-home teacher-home--daily"', html)
        self.assertContains(response, "ما يحتاج انتباهك الآن")
        self.assertContains(response, "إجراءاتك اليومية")
        self.assertContains(response, "لا توجد عناصر عاجلة الآن")
        self.assertLess(html.index("teacherAttentionTitle"), html.index("teacherActionsTitle"))
        self.assertLess(html.index("teacherActionsTitle"), html.index("recentReportsTitle"))
        self.assertNotContains(response, "متابعة اليوم")
        self.assertNotContains(response, "مساحة عملي")

    def test_teacher_quick_actions_are_real_role_safe_routes(self):
        response = self._open_home(self.teacher)
        html = response.content.decode("utf-8")
        expected = (
            "add_report",
            "request_create",
            "my_circulars",
            "my_assignments",
        )

        for route_name in expected:
            with self.subTest(route_name=route_name):
                destination = reverse(f"reports:{route_name}")
                self.assertIn(destination, html)
                follow = self.client.get(destination)
                self.assertNotEqual(follow.status_code, 403)

        forbidden = (
            "school_settings",
            "manage_teachers",
            "admin_reports",
            "notifications_create",
        )
        for route_name in forbidden:
            with self.subTest(route_name=route_name):
                self.assertNotIn(reverse(f"reports:{route_name}"), html)

    def test_staff_workspace_is_not_recast_as_teacher_daily_home(self):
        response = self._open_home(self.admin_staff)
        html = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("teacher-home--daily", html)
        self.assertNotContains(response, "teacherAttentionTitle")
        self.assertContains(response, "adminStaffCommandCenter")
        self.assertIn('class="th-kpis"', html)

    def test_home_uses_external_owned_styles_without_page_inline_behavior(self):
        template = (
            Path(settings.BASE_DIR)
            / "reports"
            / "templates"
            / "reports"
            / "home.html"
        ).read_text(encoding="utf-8")

        self.assertIn("css/teacher-home.css", template)
        self.assertNotRegex(template, re.compile(r"<style\b", re.IGNORECASE))
        self.assertNotRegex(template, re.compile(r"<script\b", re.IGNORECASE))
        self.assertNotRegex(template, re.compile(r"\sstyle\s*=", re.IGNORECASE))

    def test_teacher_daily_css_uses_v1_tokens_and_logical_direction(self):
        css = (
            Path(settings.BASE_DIR) / "static" / "css" / "teacher-home.css"
        ).read_text(encoding="utf-8")
        daily_css = css.split("/* Teacher Daily Work Home", 1)[1]

        self.assertIn("var(--twq-surface)", daily_css)
        self.assertIn("var(--twq-text)", daily_css)
        self.assertIn("var(--twq-focus)", daily_css)
        self.assertNotRegex(
            daily_css,
            re.compile(r"(?:margin|padding|border)-(?:left|right)\s*:", re.IGNORECASE),
        )
        self.assertNotRegex(daily_css, re.compile(r"(?<![-\w])(?:left|right)\s*:", re.IGNORECASE))
        self.assertNotRegex(daily_css, re.compile(r"#[0-9a-f]{3,8}\b", re.IGNORECASE))
