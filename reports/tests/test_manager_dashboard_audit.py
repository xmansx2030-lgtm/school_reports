"""Audit of the school-manager dashboard and every page it links to.

The dashboard is the manager's entry point: a link that 500s, silently drops a
filter, or renders without its data is a dead end for the only role that can fix
anything in the school. These tests crawl the real surface rather than asserting
on isolated views.
"""

from __future__ import annotations

import json
import re

from django.core.cache import cache
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

DASHBOARD_TEMPLATE = "reports/templates/reports/admin_dashboard.html"


@override_settings(ALLOWED_HOSTS=["testserver"])
class ManagerDashboardAuditTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.school = School.objects.create(
            name="مدرسة فحص اللوحة",
            code="dashboard-audit",
            current_academic_year="1447-1448",
            city="الرياض",
            phone="0500000000",
        )
        plan = SubscriptionPlan.objects.create(
            name="باقة الفحص",
            price=0,
            days_duration=30,
            max_teachers=0,
        )
        SchoolSubscription.objects.create(school=self.school, plan=plan)

        self.manager = Teacher.objects.create_user(
            phone="500110001",
            name="مدير الفحص",
            password="audit-pass",
            is_staff=True,
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )
        self.teacher = Teacher.objects.create_user(
            phone="500110002",
            name="معلم الفحص",
            password="audit-pass",
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.teacher,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        Department.objects.create(school=self.school, name="قسم الفحص")
        ReportType.objects.create(
            school=self.school,
            code="audit-type",
            name="نوع الفحص",
        )

    def _login(self):
        self.client.force_login(self.manager)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()

    def _dashboard_url_names(self) -> list[str]:
        """أسماء الروابط التي تُعكَس بلا وسائط.

        هذا المشي يفتح كل رابطٍ في اللوحة ويتأكّد أنه يعطي ‎200‎. ورابطٌ يأخذ
        مُعرّفاً — مثل ‎{% url 'reports:ticket_detail' oldest_open_id %}‎ — لا
        يُعكَس بلا ذلك المُعرّف، فيسقط المشي كلّه على ‎NoReverseMatch‎ قبل أن
        يفحص شيئاً. وذلك أسوأ من تخطّيه: خطأٌ في الأداة يُقرأ خطأً في اللوحة.

        فتُقتصَر القائمة على الوسم الذي ينتهي بعد اسمه مباشرة. والروابط ذات
        المُعرّفات تُغطّيها اختبارات وجهاتها — لا مشيٌ أعمى لا يملك مُعرّفاً
        صالحاً يمرّره.
        """
        source = open(DASHBOARD_TEMPLATE, encoding="utf-8").read()
        argument_free = re.findall(r"{%\s*url\s*'([^']+)'\s*%}", source)
        return sorted(set(argument_free))

    def test_every_dashboard_link_is_reachable_for_a_manager(self):
        self._login()
        broken = []
        for name in self._dashboard_url_names():
            url = reverse(name)
            response = self.client.get(url, follow=True)
            if response.status_code != 200:
                broken.append((name, url, response.status_code))
        self.assertEqual(broken, [], f"روابط غير قابلة للوصول من لوحة المدير: {broken}")

    def test_focus_links_actually_apply_their_filter(self):
        """A focus chip that drops its filter dumps the manager into a full list."""
        self._login()

        Ticket.objects.create(
            school=self.school,
            creator=self.teacher,
            title="طلب مفتوح",
            body="نص",
            status=Ticket.Status.OPEN,
        )
        Ticket.objects.create(
            school=self.school,
            creator=self.teacher,
            title="طلب مكتمل",
            body="نص",
            status=Ticket.Status.DONE,
        )

        response = self.client.get(
            reverse("reports:manager_school_tickets"), {"status": "attention"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["status"], "attention")
        titles = [ticket.title for ticket in response.context["tickets"]]
        self.assertIn("طلب مفتوح", titles)
        self.assertNotIn("طلب مكتمل", titles)

        TeacherAchievementFile.objects.create(
            teacher=self.teacher,
            school=self.school,
            academic_year="1447-1448",
            status=TeacherAchievementFile.Status.SUBMITTED,
        )
        response = self.client.get(
            reverse("reports:achievement_school_files"), {"status": "submitted"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["status"], "submitted")

    def _seed_school_activity(self):
        """Empty pages hide the template bugs that only appear once rows exist."""
        Report.objects.create(
            school=self.school,
            teacher=self.teacher,
            teacher_name=self.teacher.name,
            title="تقرير الفحص",
            report_date=timezone.localdate(),
            academic_year=self.school.current_academic_year,
            category=ReportType.objects.filter(school=self.school).first(),
        )
        Ticket.objects.create(
            school=self.school,
            creator=self.teacher,
            assignee=self.manager,
            title="طلب داخلي للفحص",
            body="نص الطلب",
            status=Ticket.Status.OPEN,
        )
        TeacherAchievementFile.objects.create(
            teacher=self.teacher,
            school=self.school,
            academic_year="1447-1448",
            status=TeacherAchievementFile.Status.SUBMITTED,
        )

    def test_every_dashboard_link_survives_a_school_that_has_real_data(self):
        self._login()
        self._seed_school_activity()

        broken = []
        for name in self._dashboard_url_names():
            response = self.client.get(reverse(name), follow=True)
            if response.status_code != 200:
                broken.append((name, response.status_code))
        self.assertEqual(broken, [], f"روابط تنكسر مع وجود بيانات فعلية: {broken}")

    def test_no_dashboard_page_crashes_without_an_active_school(self):
        """Managing two schools makes 'no active school' a routine state."""
        School.objects.create(name="مدرسة ثانية", code="second-audit")
        self.client.force_login(self.manager)
        session = self.client.session
        session.pop("active_school_id", None)
        session.save()

        crashed = []
        for name in self._dashboard_url_names():
            response = self.client.get(reverse(name), follow=True)
            if response.status_code >= 500:
                crashed.append((name, response.status_code))
        self.assertEqual(crashed, [], f"صفحات تنهار بلا مدرسة نشطة: {crashed}")

    def test_unknown_dashboard_action_redirects_instead_of_re_rendering(self):
        """A POST answered with HTML lets a refresh re-submit the form."""
        self._login()
        response = self.client.post(
            reverse("reports:admin_dashboard"), {"action": "not_a_real_action"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("reports:admin_dashboard"))

    def test_retired_weekly_summary_toggle_post_is_rejected(self):
        """The toggle is gone; a replayed POST must not resurrect it."""
        self._login()

        response = self.client.post(
            reverse("reports:admin_dashboard"),
            {"action": "toggle_weekly_summary_email", "weekly_summary_email_enabled": "0"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("reports:admin_dashboard"))

    def test_dashboard_renders_and_exposes_its_payload(self):
        self._login()
        response = self.client.get(reverse("reports:admin_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="schoolDashboardPayload"')
        self.assertContains(response, "مركز إدارة المدرسة")
        self.assertEqual(response.context["initial_period"], "all")
        match = re.search(
            r'<script id="schoolDashboardPayload" type="application/json">(.*?)</script>',
            response.content.decode("utf-8"),
            re.S,
        )
        self.assertIsNotNone(match)
        payload = json.loads(match.group(1))
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload["kpis"]["reports_count"], response.context["reports_count"])

    def test_period_switch_keeps_follow_up_counters_stable(self):
        """Follow-up counters are all-time by design; they must not move with the period."""
        self._login()
        Ticket.objects.create(
            school=self.school,
            creator=self.teacher,
            title="طلب قديم مفتوح",
            body="نص",
            status=Ticket.Status.OPEN,
        )

        baseline = self.client.get(
            reverse("reports:api_admin_dashboard_data"), {"period": "all"}
        ).json()
        monthly = self.client.get(
            reverse("reports:api_admin_dashboard_data"), {"period": "month"}
        ).json()

        self.assertEqual(
            baseline["kpis"]["tickets_open"], monthly["kpis"]["tickets_open"]
        )

    def _activate(self, school: School) -> None:
        session = self.client.session
        session["active_school_id"] = school.id
        session.save()

    def test_dashboard_page_rejects_a_school_the_user_does_not_manage(self):
        other = School.objects.create(name="مدرسة أخرى", code="other-audit")
        self.client.force_login(self.manager)
        self._activate(other)

        response = self.client.get(reverse("reports:admin_dashboard"))
        self.assertEqual(response.status_code, 302)

    # The dashboard's own fetch() sends these; the guards must behave the same way.
    _FETCH_HEADERS = {"HTTP_ACCEPT": "application/json", "HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}

    def test_dashboard_api_answers_in_json_instead_of_redirecting(self):
        """A 302 to an HTML page surfaces in fetch as an unexplained parse error."""
        other = School.objects.create(name="مدرسة خارج النطاق", code="other-audit-api")
        self.client.force_login(self.manager)
        self._activate(other)

        api = self.client.get(
            reverse("reports:api_admin_dashboard_data"), **self._FETCH_HEADERS
        )
        self.assertEqual(api.status_code, 403)
        self.assertEqual(api["Content-Type"].split(";")[0], "application/json")

    def test_dashboard_api_refuses_an_anonymous_caller_in_json(self):
        api = self.client.get(reverse("reports:api_admin_dashboard_data"))
        self.assertEqual(api.status_code, 401)
        self.assertEqual(api["Content-Type"].split(";")[0], "application/json")

    def test_dashboard_api_refuses_a_plain_teacher(self):
        self.client.force_login(self.teacher)
        self._activate(self.school)

        api = self.client.get(reverse("reports:api_admin_dashboard_data"))
        self.assertEqual(api.status_code, 403)
        self.assertEqual(api["Content-Type"].split(";")[0], "application/json")

    def test_follow_up_total_equals_the_items_listed_under_it(self):
        """The headline count and the chips below it come from one source."""
        self._login()

        assigned = Ticket.objects.create(
            school=self.school,
            creator=self.teacher,
            title="طلب معيّن للمدير",
            body="نص",
            status=Ticket.Status.OPEN,
            assignee=self.manager,
        )
        self.assertIsNotNone(assigned.pk)
        Ticket.objects.create(
            school=self.school,
            creator=self.teacher,
            title="طلب مفتوح آخر",
            body="نص",
            status=Ticket.Status.OPEN,
        )
        TeacherAchievementFile.objects.create(
            teacher=self.teacher,
            school=self.school,
            academic_year="1447-1448",
            status=TeacherAchievementFile.Status.SUBMITTED,
        )

        response = self.client.get(reverse("reports:admin_dashboard"))
        focus_items = response.context["focus_items"]
        total = response.context["attention_total"]

        # Two open tickets (one of them assigned to the manager) plus one file:
        # the assigned ticket must not be counted twice.
        self.assertEqual(total, 3)
        self.assertEqual(
            total,
            sum(item["count"] for item in focus_items if not item["subset"]),
        )
        subset_items = [item for item in focus_items if item["subset"]]
        self.assertTrue(subset_items, "المهام المعيّنة يجب أن تظهر كتفصيل لا كبند مستقل")

    def _attention_card_count(self) -> int:
        """Rows that claim a decision is waiting.

        The dashboard used to answer "what needs me?" three times — a KPI card,
        a focus list beside it, and an agenda below. They merged into one
        "ما يحتاجك الآن" list, so the marker moved from ``manager-kpi--attention``
        to ``now-row--decision``. The meaning this test guards is unchanged:
        emphasis is spent only on work that is actually pending.
        """
        html = self.client.get(reverse("reports:admin_dashboard")).content.decode("utf-8")
        return len(re.findall(r'class="now-row now-row--decision', html))

    def test_attention_styling_is_reserved_for_real_pending_work(self):
        """A school with nothing pending must not render amber alert cards."""
        self._login()

        self.assertEqual(self._attention_card_count(), 0)
        self.assertContains(
            self.client.get(reverse("reports:admin_dashboard")),
            "لا شيء ينتظرك الآن",
        )

        # TestCase keeps the test inside an outer transaction. Execute the
        # production on_commit invalidation now so this request observes the
        # same cache state that a real committed write would produce.
        with self.captureOnCommitCallbacks(execute=True):
            Ticket.objects.create(
                school=self.school,
                creator=self.teacher,
                title="طلب مفتوح",
                body="نص",
                status=Ticket.Status.OPEN,
            )
        self.assertEqual(self._attention_card_count(), 1)

    def test_dashboard_has_no_hardcoded_light_mode_colours(self):
        """Inline light-mode ink turns invisible on the dark theme."""
        source = open(DASHBOARD_TEMPLATE, encoding="utf-8").read()
        body = source.split("{% block content %}", 1)[1]

        leaked = re.findall(r'style="[^"]*(?:color|background)\s*:\s*#[0-9a-fA-F]{3,6}', body)
        self.assertEqual(
            leaked,
            [],
            f"ألوان ثابتة داخل الوسم تكسر الوضع الليلي: {leaked}",
        )
