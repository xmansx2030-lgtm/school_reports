"""End-to-end audit of the teacher journey, from an empty account onward.

A teacher meets the product on day one with nothing created yet, and every screen
has to hold up in that state before it ever holds real work. These tests walk the
journey in order rather than asserting on isolated views.
"""

from __future__ import annotations

import re

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from reports.models import (
    Department,
    Report,
    ReportType,
    School,
    SchoolMembership,
    SchoolSubscription,
    SubscriptionPlan,
    Teacher,
    TeacherAchievementFile,
    Ticket,
)

HOME_TEMPLATE = "reports/templates/reports/home.html"

# Every screen the teacher journey passes through.
TEACHER_TEMPLATES_ON_DISK = (
    "reports/templates/reports/home.html",
    "reports/templates/reports/my_reports.html",
    "reports/templates/reports/add_report.html",
    "reports/templates/reports/achievement_my_files.html",
    "reports/templates/reports/achievement_file.html",
    "reports/templates/reports/my_requests.html",
    "reports/templates/reports/ticket_detail.html",
    "reports/templates/reports/my_circulars.html",
    "reports/templates/reports/my_notifications.html",
    "reports/templates/reports/my_profile.html",
)

# A white plate behind an official logo is deliberate: the mark is printed on
# white and must not shift with the theme.
DELIBERATE_WHITE_PLATES = frozenset({"af-ministry-logo"})

# Everything a teacher can legitimately open from the navigation or their home.
TEACHER_PAGES = (
    "reports:home",
    "reports:my_reports",
    "reports:add_report",
    "reports:achievement_my_files",
    "reports:my_requests",
    "reports:request_create",
    "reports:my_circulars",
    "reports:my_notifications",
    "reports:assigned_to_me",
    "reports:my_assignments",
    "reports:meeting_list",
    "reports:plan_list",
    "reports:initiative_list",
    "reports:document_archive",
    "reports:my_work_archive",
    "reports:my_activity_log",
    "reports:my_profile",
    "reports:user_guide",
)

# Manager-only surfaces a teacher must never be served.
MANAGER_ONLY_PAGES = (
    "reports:admin_dashboard",
    "reports:admin_reports",
    "reports:manage_teachers",
    "reports:bulk_import_teachers",
    "reports:departments_list",
    "reports:school_settings",
    "reports:manager_school_tickets",
    "reports:notifications_create",
    "reports:circulars_create",
    "reports:my_subscription",
    "reports:school_audit_logs",
    "reports:school_data_export",
)


@override_settings(ALLOWED_HOSTS=["testserver"])
class TeacherJourneyAuditTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(
            name="مدرسة رحلة المعلم",
            code="teacher-journey",
            current_academic_year="1447-1448",
            city="جدة",
            phone="0500000001",
        )
        plan = SubscriptionPlan.objects.create(
            name="باقة الرحلة",
            price=0,
            days_duration=30,
            max_teachers=0,
        )
        SchoolSubscription.objects.create(school=self.school, plan=plan)

        self.manager = Teacher.objects.create_user(
            phone="500220001",
            name="مدير الرحلة",
            password="journey-pass",
            is_staff=True,
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )
        self.teacher = Teacher.objects.create_user(
            phone="500220002",
            name="معلم الرحلة",
            password="journey-pass",
        )
        self.department = Department.objects.create(
            school=self.school, name="قسم رحلة المعلم"
        )
        self.membership = SchoolMembership.objects.create(
            school=self.school,
            teacher=self.teacher,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        self.report_type = ReportType.objects.create(
            school=self.school,
            code="journey-type",
            name="نوع رحلة المعلم",
        )

    def _login_teacher(self):
        self.client.force_login(self.teacher)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()

    # ---------------------------------------------------------------- day one

    def test_a_brand_new_teacher_can_open_every_page_they_are_offered(self):
        """Day one has no reports, no requests, no files: nothing may break."""
        self._login_teacher()
        broken = []
        for name in TEACHER_PAGES:
            response = self.client.get(reverse(name), follow=True)
            if response.status_code != 200:
                broken.append((name, response.status_code))
        self.assertEqual(broken, [], f"صفحات لا تفتح لمعلم جديد: {broken}")

    def _page_body(self, url_name: str) -> str:
        response = self.client.get(reverse(url_name))
        self.assertEqual(response.status_code, 200, url_name)
        return response.content.decode("utf-8").split("</head>", 1)[-1]

    def test_empty_pages_tell_the_teacher_what_to_do_next(self):
        """An empty list with no next step is a dead end on the very first visit."""
        self._login_teacher()

        # Each page needs its own next step, not a generic one.
        expectations = {
            "reports:home": reverse("reports:add_report"),
            "reports:my_reports": reverse("reports:add_report"),
            "reports:achievement_my_files": "إنشاء",
            "reports:my_requests": reverse("reports:request_create"),
            "reports:my_assignments": "لا تكليفات مفتوحة",
            "reports:plan_list": "لا توجد خطط معتمدة لك بعد",
        }
        missing = [
            name
            for name, marker in expectations.items()
            if marker not in self._page_body(name)
        ]
        self.assertEqual(missing, [], f"صفحات بلا إجراء تالٍ للمعلم الجديد: {missing}")

    def test_a_teacher_with_no_report_type_available_is_told_why(self):
        """Day one often means the manager has not finished setup yet.

        Landing on an empty form with no explanation reads as a broken product.
        """
        self._login_teacher()
        ReportType.objects.filter(school=self.school).delete()

        response = self.client.get(reverse("reports:add_report"), follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["has_report_types"])
        # Server-rendered, so it holds without JavaScript.
        self.assertContains(response, "لا توجد أنواع تقارير متاحة لك بعد")

    def test_the_setup_notice_stays_hidden_once_report_types_exist(self):
        self._login_teacher()
        response = self.client.get(reverse("reports:add_report"))

        self.assertTrue(response.context["has_report_types"])
        self.assertNotContains(response, "لا توجد أنواع تقارير متاحة لك بعد")

    # -------------------------------------------------------- the actual work

    def _create_report(self, title: str = "تقرير الرحلة") -> Report:
        return Report.objects.create(
            school=self.school,
            teacher=self.teacher,
            teacher_name=self.teacher.name,
            title=title,
            report_date=timezone.localdate(),
            academic_year=self.school.current_academic_year,
            category=self.report_type,
        )

    def test_teacher_can_walk_the_full_report_lifecycle(self):
        self._login_teacher()
        report = self._create_report()

        for name in ("reports:my_reports", "reports:home"):
            self.assertContains(self.client.get(reverse(name)), report.title)

        for name in ("reports:report_print", "reports:edit_my_report", "reports:report_share_manage"):
            response = self.client.get(reverse(name, args=[report.pk]))
            self.assertEqual(response.status_code, 200, f"{name} غير متاح لصاحب التقرير")

    def test_a_teacher_cannot_touch_another_teachers_report(self):
        other = Teacher.objects.create_user(
            phone="500220003", name="معلم آخر", password="journey-pass"
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=other,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        foreign = Report.objects.create(
            school=self.school,
            teacher=other,
            teacher_name=other.name,
            title="تقرير زميل",
            report_date=timezone.localdate(),
            academic_year=self.school.current_academic_year,
            category=self.report_type,
        )

        self._login_teacher()
        leaked = []
        for name in ("reports:edit_my_report", "reports:report_share_manage"):
            response = self.client.get(reverse(name, args=[foreign.pk]))
            if response.status_code == 200:
                leaked.append(name)
        self.assertEqual(leaked, [], f"المعلم وصل لتقرير زميله عبر: {leaked}")

    def test_teacher_can_open_their_achievement_file_and_requests(self):
        self._login_teacher()
        achievement = TeacherAchievementFile.objects.create(
            teacher=self.teacher,
            school=self.school,
            academic_year=self.school.current_academic_year,
        )
        ticket = Ticket.objects.create(
            school=self.school,
            creator=self.teacher,
            title="طلب المعلم",
            body="نص الطلب",
            status=Ticket.Status.OPEN,
        )

        self.assertEqual(
            self.client.get(
                reverse("reports:achievement_file_detail", args=[achievement.pk])
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(reverse("reports:ticket_detail", args=[ticket.pk])).status_code,
            200,
        )
        self.assertContains(self.client.get(reverse("reports:my_requests")), ticket.title)

    # ------------------------------------------------------------- boundaries

    def test_manager_only_pages_are_closed_to_a_teacher(self):
        self._login_teacher()
        leaked = []
        for name in MANAGER_ONLY_PAGES:
            response = self.client.get(reverse(name))
            if response.status_code == 200:
                leaked.append(name)
        self.assertEqual(leaked, [], f"صفحات إدارية مفتوحة للمعلم: {leaked}")

    def test_a_teacher_without_an_active_school_is_not_stranded(self):
        self.client.force_login(self.teacher)
        session = self.client.session
        session.pop("active_school_id", None)
        session.save()

        crashed = []
        for name in TEACHER_PAGES:
            response = self.client.get(reverse(name), follow=True)
            if response.status_code >= 500:
                crashed.append((name, response.status_code))
        self.assertEqual(crashed, [], f"صفحات تنهار بلا مدرسة نشطة: {crashed}")

    # ------------------------------------------------------------ consistency

    def test_every_light_surface_on_the_journey_has_a_dark_counterpart(self):
        """A pinned light background under themed text renders light-on-light.

        This is how the achievement empty state reached 1.04:1 and the requests
        CTA reached 1.31:1 on the dark theme.
        """
        dark_css = open("static/css/dark-mode.css", encoding="utf-8").read()
        rule_re = re.compile(r"([^{}]+)\{([^{}]*)\}")
        background_re = re.compile(r"background(?:-color)?\s*:\s*(#[0-9a-fA-F]{3,8})")

        def luminance(colour: str) -> float:
            colour = colour.lstrip("#")
            if len(colour) == 3:
                colour = "".join(c * 2 for c in colour)
            channels = [int(colour[i : i + 2], 16) / 255 for i in (0, 2, 4)]
            channels = [
                c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
                for c in channels
            ]
            return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]

        uncovered = []
        for path in TEACHER_TEMPLATES_ON_DISK:
            text = open(path, encoding="utf-8").read()
            styles = "\n".join(re.findall(r"<style>(.*?)</style>", text, re.S))
            dark_rules = "\n".join(
                line for line in text.splitlines() if 'data-theme="dark"' in line
            )
            for selector, block in rule_re.findall(styles):
                selector = " ".join(selector.split())
                if "data-theme" in selector or selector.startswith("@"):
                    continue
                match = background_re.search(block)
                if not match:
                    continue
                colour = match.group(1)
                if len(colour.lstrip("#")) not in (3, 6) or luminance(colour) < 0.55:
                    continue
                classes = re.findall(r"\.([a-zA-Z0-9_-]+)", selector)
                if not classes:
                    continue
                if any(name in DELIBERATE_WHITE_PLATES for name in classes):
                    continue
                covered = any(
                    f".{name}" in dark_rules or f".{name}" in dark_css for name in classes
                )
                if not covered:
                    uncovered.append(f"{path.rsplit('/', 1)[-1]}: {selector[:48]} ({colour})")

        self.assertEqual(
            uncovered,
            [],
            "أسطح فاتحة بلا مقابل في الوضع الليلي:\n" + "\n".join(uncovered),
        )

    def test_report_dates_are_shown_in_hijri_everywhere(self):
        """The platform stores Gregorian and displays Hijri.

        A screen that slips back to |date: shows the teacher a different date for
        the same report than the one they see in تقاريري.
        """
        import glob

        offenders = []
        for path in glob.glob("reports/templates/reports/*.html"):
            text = open(path, encoding="utf-8").read()
            for line_no, line in enumerate(text.splitlines(), start=1):
                visible_markup = re.sub(r'\sdatetime="[^"]*"', "", line)
                if re.search(r"report_date\s*\|\s*date:", visible_markup):
                    offenders.append(f"{path.rsplit('/', 1)[-1]}:{line_no}")
        self.assertEqual(
            offenders,
            [],
            "تواريخ تقارير معروضة ميلاديًا بدل الهجري: " + ", ".join(offenders),
        )

    def test_teacher_sees_the_same_report_date_on_home_and_in_my_reports(self):
        self._login_teacher()
        report = self._create_report()

        home = self.client.get(reverse("reports:home")).content.decode("utf-8")
        listing = self.client.get(reverse("reports:my_reports")).content.decode("utf-8")

        from reports.templatetags.hijri_tags import hijri

        expected = hijri(report.report_date)
        self.assertIn(expected, home, "الرئيسية لا تعرض التاريخ الهجري")
        self.assertIn(expected, listing, "تقاريري لا تعرض التاريخ الهجري")

    def test_teacher_home_has_no_hardcoded_light_mode_colours(self):
        source = open(HOME_TEMPLATE, encoding="utf-8").read()
        body = source.split("{% block content %}", 1)[-1].split("{% block scripts %}", 1)[0]
        leaked = re.findall(
            r'style="[^"]*(?:^|;)\s*(?:color|background(?:-color)?)\s*:\s*#[0-9a-fA-F]{3,8}',
            body,
        )
        self.assertEqual(leaked, [], f"ألوان ثابتة تكسر الوضع الليلي: {leaked}")
