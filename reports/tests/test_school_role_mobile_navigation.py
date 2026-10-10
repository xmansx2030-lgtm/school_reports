"""Role-aware mobile shortcuts must open authorized school workflows."""
from html.parser import HTMLParser

from django.urls import reverse

from reports import capabilities as caps
from reports.lab_kinds import LabKind
from reports.models import SchoolMembership
from reports.tests.test_role_journeys import RoleJourneyTestCase, _user


class _PrimaryShortcut(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.href = None
        self.label = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and "mobile-tabbar__primary" in attrs.get("class", "").split():
            self.href = attrs.get("href")
            self.label = attrs.get("aria-label")


class SchoolRoleMobileShortcutTests(RoleJourneyTestCase):
    def _assert_shortcut(self, user, route, label):
        self._enter(user)
        page = self.client.get(reverse("reports:home"), follow=True)
        self.assertEqual(page.status_code, 200)
        shortcut = _PrimaryShortcut(page.content.decode())
        self.assertEqual(shortcut.href, reverse(route))
        self.assertEqual(shortcut.label, label)
        destination = self.client.get(shortcut.href, follow=True)
        self.assertEqual(destination.status_code, 200)
        return page

    def test_teacher_can_start_report_from_mobile(self):
        self._assert_shortcut(self.teacher, "reports:add_report", "إضافة تقرير")

    def test_scoped_deputy_can_reach_operational_scope(self):
        self._grant(caps.VIEW_SCHOOL_DASHBOARD)
        page = self._assert_shortcut(self.deputy, "reports:staff_dashboard", "متابعة نطاقي")
        self.assertContains(page, 'id="deputyCommandCenter"')

    def test_admin_staff_can_reach_assigned_work_without_extra_capabilities(self):
        staff = _user("موظف إداري", "0500021101")
        SchoolMembership.objects.create(school=self.school, teacher=staff,
            role_type=SchoolMembership.RoleType.ADMIN_STAFF, job_title=SchoolMembership.JobTitle.ADMIN_STAFF)
        self._assert_shortcut(staff, "reports:my_assignments", "متابعة تكليفاتي")

    def test_lab_technician_can_reach_assigned_lab_without_review_authority(self):
        technician = _user("محضر المختبر", "0500021102")
        SchoolMembership.objects.create(school=self.school, teacher=technician,
            role_type=SchoolMembership.RoleType.ADMIN_STAFF, job_title=SchoolMembership.JobTitle.LAB_TECH,
            lab_kind=LabKind.SCIENCE)
        page = self._assert_shortcut(technician, "reports:lab_dashboard", "إدارة المختبر")
        self.assertContains(page, 'class="th-btn th-btn-primary"')
        self.assertNotContains(page, reverse("reports:staff_dashboard"))

    def test_deputy_without_dashboard_capability_keeps_personal_report_shortcut(self):
        self._assert_shortcut(self.deputy, "reports:add_report", "إضافة تقرير")
