from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from reports import capabilities as caps
from reports.models import (
    Delegation,
    Department,
    School,
    SchoolMembership,
    SchoolSubscription,
    StaffScope,
    SubscriptionPlan,
    Teacher,
)


ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "reports" / "templates" / "reports" / "staff_dashboard.html"
STYLESHEET = ROOT / "static" / "css" / "staff-dashboard.css"


class StaffDashboardSourceTests(SimpleTestCase):
    def test_dashboard_uses_the_v1_shell_and_external_owned_css(self):
        source = TEMPLATE.read_text(encoding="utf-8")

        self.assertIn("css/staff-dashboard.css", source)
        self.assertIn('class="twq-page staff-dashboard"', source)
        self.assertIn("twq-page-header", source)
        self.assertIn("twq-section", source)
        self.assertIn("twq-status", source)
        self.assertNotIn("<style", source)
        self.assertNotIn("<script", source)
        self.assertNotRegex(source, r"\sstyle\s*=")

    def test_priority_scope_actions_and_coverage_follow_the_operational_order(self):
        source = TEMPLATE.read_text(encoding="utf-8")

        priority = source.index('id="staffAttentionTitle"')
        actions = source.index('id="staffActionsTitle"')
        coverage = source.index('id="scopeCoverageTitle"')
        self.assertLess(priority, actions)
        self.assertLess(actions, coverage)
        self.assertIn('class="twq-progress__bar"', source)
        self.assertIn('value="{{ coverage.percent }}"', source)

    def test_new_styles_use_semantic_tokens_and_logical_directions(self):
        source = STYLESHEET.read_text(encoding="utf-8")

        self.assertIn("var(--twq-", source)
        self.assertNotRegex(source, r"#[0-9a-fA-F]{3,8}\b")
        self.assertNotRegex(source, r"(?i)\brgba?\(")
        self.assertNotRegex(
            source,
            r"(?m)^\s*(?:(?:margin|padding|border|inset)-(?:left|right)|left|right)\s*:",
        )
        self.assertNotIn("transition: all", source)


@override_settings(ALLOWED_HOSTS=["testserver"])
class StaffDashboardPresentationContextTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.plan = SubscriptionPlan.objects.create(
            name="خطة لوحة النطاق", price=0, days_duration=365, max_teachers=20
        )
        cls.school = School.objects.create(
            name="مدرسة لوحة النطاق",
            code="staff-dashboard-ui",
            current_academic_year="1448-1449",
        )
        SchoolSubscription.objects.create(school=cls.school, plan=cls.plan)
        cls.manager = Teacher.objects.create_user(
            phone="509870001", name="مدير لوحة النطاق", password="x"
        )
        SchoolMembership.objects.create(
            school=cls.school,
            teacher=cls.manager,
            role_type=SchoolMembership.RoleType.MANAGER,
            is_active=True,
        )
        cls.department = Department.objects.create(
            school=cls.school,
            name="قسم لوحة النطاق",
            slug="staff-dashboard-ui-department",
            is_active=True,
        )

    def _enter(self, user):
        self.client.force_login(user)
        session = self.client.session
        session["active_school_id"] = self.school.pk
        session.save()

    def _staff(self, phone, *, capabilities):
        user = Teacher.objects.create_user(phone=phone, name="منسوب النطاق", password="x")
        membership = SchoolMembership.objects.create(
            school=self.school,
            teacher=user,
            role_type=SchoolMembership.RoleType.DEPUTY,
            is_active=True,
        )
        scope = StaffScope.objects.create(
            membership=membership,
            domain=StaffScope.Domain.ACADEMIC,
            capabilities=capabilities,
        )
        scope.departments.set([self.department])
        return user

    def test_permanent_scope_is_exposed_as_presentation_context(self):
        deputy = self._staff(
            "509870002",
            capabilities=[caps.VIEW_SCHOOL_DASHBOARD, caps.REVIEW_REPORTS],
        )
        self._enter(deputy)

        response = self.client.get(reverse("reports:staff_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["dashboard_access_source"], "scope")
        self.assertEqual(response.context["attention_cards"], [])
        self.assertEqual(
            [card["key"] for card in response.context["operational_cards"]],
            ["reports"],
        )
        self.assertContains(response, reverse("reports:approval_inbox"))
        self.assertNotContains(response, reverse("reports:assignment_board"))

    def test_delegated_access_is_labelled_without_widening_department_scope(self):
        deputy = self._staff("509870003", capabilities=[])
        now = timezone.now()
        Delegation.objects.create(
            school=self.school,
            delegator=self.manager,
            delegate=deputy,
            capabilities=[caps.VIEW_SCHOOL_DASHBOARD],
            reason="تغطية مؤقتة",
            starts_at=now - timedelta(hours=1),
            ends_at=now + timedelta(days=1),
        )
        self._enter(deputy)

        response = self.client.get(reverse("reports:staff_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["dashboard_access_source"], "delegation")
        self.assertEqual(response.context["supervised_count"], 1)
        self.assertContains(response, "تفويض مؤقت فعّال")
