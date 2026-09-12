"""المدير التنفيذي لمجموعة المدارس المتكاملة.

التنظيم الذي تثبّته هذه الاختبارات: المدير التنفيذي يقود مجموعة مدارس إشرافاً
ومتابعةً، وكل مدرسة تحتفظ بمديرها وبكامل صلاحياته. فالخاصية الحرجة ليست ما
يستطيع المدير التنفيذي رؤيته، بل ما لا يستطيع فعله — وما لا يراه غيره.
"""

from __future__ import annotations

from django.test import TestCase, override_settings
from django.urls import reverse

from reports.models import (
    School,
    SchoolGroup,
    SchoolGroupMembership,
    SchoolMembership,
    SchoolSubscription,
    SubscriptionPlan,
    Teacher,
)
from reports.permissions import (
    executive_director_schools_qs,
    is_executive_director,
    is_school_manager,
)


def _user(name: str, phone: str) -> Teacher:
    return Teacher.objects.create_user(phone=phone, name=name, password="Passw0rd!123")


@override_settings(ALLOWED_HOSTS=["testserver"])
class ExecutiveDirectorTests(TestCase):
    def setUp(self):
        self.group = SchoolGroup.objects.create(name="مجمع النور", code="al-noor")
        self.other_group = SchoolGroup.objects.create(name="مجمع الفجر", code="al-fajr")

        self.schools = [
            School.objects.create(name=f"مدرسة {index}", code=f"school-{index}", group=self.group)
            for index in range(1, 4)
        ]
        # المدرسة الثالثة تُترك بلا اشتراك عمداً: حالة مشروعة داخل المجموعة،
        # وعليها يقوم اختبار عرضها بدل إخفائها.
        # باقة سنوية: باقة الثلاثين يوماً تقع فوراً في نطاق «يقارب الانتهاء»،
        # فلا تصلح لتمييز الحالة الفعّالة عن المنبِّهة.
        plan = SubscriptionPlan.objects.create(
            name="باقة المجموعة", price=0, days_duration=365, max_teachers=0
        )
        for school in self.schools[:2]:
            SchoolSubscription.objects.create(school=school, plan=plan)
        self.unsubscribed_school = self.schools[2]
        self.outside_school = School.objects.create(name="مدرسة خارج المجموعة", code="outside")
        self.other_group_school = School.objects.create(
            name="مدرسة مجموعة أخرى", code="other-group-school", group=self.other_group
        )

        self.director = _user("مدير تنفيذي", "0500000001")
        SchoolGroupMembership.objects.create(group=self.group, user=self.director)

        self.principal = _user("مدير مدرسة", "0500000002")
        SchoolMembership.objects.create(
            school=self.schools[0],
            teacher=self.principal,
            role_type=SchoolMembership.RoleType.MANAGER,
        )

        self.teacher = _user("معلم", "0500000003")
        SchoolMembership.objects.create(
            school=self.schools[0],
            teacher=self.teacher,
            role_type=SchoolMembership.RoleType.TEACHER,
        )

    # ------------------------------------------------------------ الصلاحيات

    def test_director_never_counts_as_a_school_manager(self):
        """أهم خاصية: الإشراف لا يمنح الإدارة اليومية في أي مدرسة."""
        self.assertTrue(is_executive_director(self.director))
        self.assertFalse(is_school_manager(self.director))
        for school in self.schools:
            self.assertFalse(
                is_school_manager(self.director, active_school=school),
                f"المدير التنفيذي اجتاز فحص إدارة {school.name}",
            )

    def test_principal_keeps_full_authority_in_their_own_school(self):
        self.assertTrue(is_school_manager(self.principal, active_school=self.schools[0]))
        self.assertFalse(is_executive_director(self.principal))

    def test_director_sees_only_their_own_group_schools(self):
        visible = set(executive_director_schools_qs(self.director).values_list("code", flat=True))

        self.assertEqual(visible, {school.code for school in self.schools})
        self.assertNotIn(self.outside_school.code, visible)
        self.assertNotIn(self.other_group_school.code, visible)

    def test_directorship_is_scoped_to_the_named_group(self):
        self.assertTrue(is_executive_director(self.director, self.group))
        self.assertFalse(is_executive_director(self.director, self.other_group))

    def test_a_school_leaving_the_group_drops_out_of_view_at_once(self):
        leaving = self.schools[0]
        leaving.group = None
        leaving.save(update_fields=["group"])

        self.director.refresh_from_db()
        visible = set(executive_director_schools_qs(self.director).values_list("code", flat=True))
        self.assertNotIn(leaving.code, visible)

    def test_deactivating_the_membership_revokes_access(self):
        SchoolGroupMembership.objects.filter(user=self.director).update(is_active=False)
        fresh = Teacher.objects.get(pk=self.director.pk)

        self.assertFalse(is_executive_director(fresh))
        self.assertFalse(executive_director_schools_qs(fresh).exists())

    def test_teachers_and_principals_are_not_directors(self):
        for user in (self.teacher, self.principal):
            self.assertFalse(is_executive_director(user))
            self.assertFalse(executive_director_schools_qs(user).exists())

    def test_a_group_cannot_have_two_active_directors(self):
        from django.db.utils import IntegrityError

        rival = _user("مدير تنفيذي ثانٍ", "0500000004")
        with self.assertRaises(IntegrityError):
            SchoolGroupMembership.objects.create(group=self.group, user=rival)

    # ---------------------------------------------------------------- اللوحة

    def test_dashboard_aggregates_the_group_and_hides_outside_schools(self):
        self.client.force_login(self.director)
        response = self.client.get(reverse("reports:executive_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["totals"]["schools"], len(self.schools))
        self.assertContains(response, self.group.name)
        self.assertNotContains(response, self.outside_school.name)
        self.assertNotContains(response, self.other_group_school.name)

    def test_dashboard_exposes_the_executive_decision_surface(self):
        self.client.force_login(self.director)
        response = self.client.get(reverse("reports:executive_dashboard"))

        self.assertContains(response, "ما يحتاج قرارك")
        self.assertContains(response, "صحة مدارس المجموعة")
        self.assertContains(response, "data-school-search")
        for row in response.context["rows"]:
            self.assertIn("risk_score", row)
            self.assertIn("risk_label", row)
            self.assertIn("reports_delta", row)

    def test_activity_window_is_selectable_but_bounded(self):
        self.client.force_login(self.director)

        for days in (7, 30, 90):
            with self.subTest(days=days):
                response = self.client.get(
                    reverse("reports:executive_dashboard"), {"window": days}
                )
                self.assertEqual(response.context["activity_window_days"], days)

        response = self.client.get(
            reverse("reports:executive_dashboard"), {"window": 9999}
        )
        self.assertEqual(response.context["activity_window_days"], 30)

    def test_only_the_director_is_offered_the_group_link(self):
        self.client.force_login(self.director)
        self.assertContains(self.client.get(reverse("reports:executive_dashboard")), "لوحة المجموعة")

        for user in (self.teacher, self.principal):
            self.client.force_login(user)
            # ``home`` يحوّل هذين الدورين إلى وجهتيهما، فنتبع التحويل لنفحص
            # الصفحة التي يرونها فعلاً لا صفحة التحويل.
            landing = self.client.get(reverse("reports:home"), follow=True)
            self.assertNotContains(landing, "لوحة المجموعة")

    def test_dashboard_is_invisible_to_everyone_else(self):
        for user in (self.teacher, self.principal):
            self.client.force_login(user)
            response = self.client.get(reverse("reports:executive_dashboard"))
            self.assertEqual(response.status_code, 404, f"{user.name} وصل إلى لوحة المجموعة")

    def test_group_query_parameter_cannot_reach_another_group(self):
        """معرّف مجموعة لا يقودها المستخدم يجب ألا يوسّع وصوله."""
        self.client.force_login(self.director)
        response = self.client.get(
            reverse("reports:executive_dashboard"), {"group": self.other_group.pk}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["group"].pk, self.group.pk)
        self.assertNotContains(response, self.other_group_school.name)

    def test_a_school_without_a_subscription_is_shown_not_hidden(self):
        self.client.force_login(self.director)
        response = self.client.get(reverse("reports:executive_dashboard"))

        rows = {row["school"].code: row for row in response.context["rows"]}
        self.assertEqual(len(rows), len(self.schools))
        self.assertEqual(rows[self.unsubscribed_school.code]["subscription"]["tone"], "none")
        self.assertEqual(rows[self.schools[0].code]["subscription"]["tone"], "active")
        self.assertContains(response, "بلا اشتراك")

    def test_dashboard_shows_per_school_consumption(self):
        """مدرسة تقارب امتلاء مساحتها يتوقف فيها الرفع، فهو أول من يجب أن يعرف."""
        self.client.force_login(self.director)
        response = self.client.get(reverse("reports:executive_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "الاستهلاك")
        self.assertContains(response, 'class="dir-usage"')
        for row in response.context["rows"]:
            self.assertIn("storage", row["consumption"])
            self.assertIn("seats", row["consumption"])
            self.assertIn("archive", row["consumption"])

    def test_a_school_near_its_storage_limit_needs_attention(self):
        """الامتلاء يوقف الرفع كما يوقفه انتهاء الاشتراك، فيستحق نفس التنبيه."""
        # سعة 0 في باقة التهيئة تعني تخزيناً بلا حدّ، فلا نسبة تُقاس عليها.
        sized_plan = SubscriptionPlan.objects.create(
            name="باقة بسعة", price=100, days_duration=365, max_teachers=25
        )
        crowded = self.schools[0]
        SchoolSubscription.objects.filter(school=crowded).update(plan=sized_plan)
        School.objects.filter(pk=crowded.pk).update(storage_used_bytes=10 ** 15)

        self.client.force_login(self.director)
        response = self.client.get(reverse("reports:executive_dashboard"))

        rows = {row["school"].pk: row for row in response.context["rows"]}
        self.assertFalse(rows[crowded.pk]["consumption"]["storage"]["is_unlimited"])
        self.assertEqual(rows[crowded.pk]["consumption"]["storage"]["percent"], 100)

        flagged = {row["school"].pk for row in response.context["needs_attention"]}
        self.assertIn(crowded.pk, flagged)

    def test_consumption_queries_do_not_grow_with_the_group_size(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self.client.force_login(self.director)
        # طلب تسخين: الطلب الأول يحمل تهيئة الجلسة وذاكرات الصلاحيات.
        self.client.get(reverse("reports:executive_dashboard"))
        with CaptureQueriesContext(connection) as few:
            self.client.get(reverse("reports:executive_dashboard"))

        plan = SubscriptionPlan.objects.first()
        for index in range(6):
            extra = School.objects.create(
                name=f"مدرسة إضافية {index}", code=f"extra-{index}", group=self.group
            )
            SchoolSubscription.objects.create(school=extra, plan=plan)

        with CaptureQueriesContext(connection) as many:
            self.client.get(reverse("reports:executive_dashboard"))

        self.assertLessEqual(
            len(many.captured_queries),
            len(few.captured_queries),
            f"الاستعلامات نمت من {len(few.captured_queries)} إلى {len(many.captured_queries)}",
        )

    def test_dashboard_never_enters_a_single_school_context(self):
        """اللوحة تقرأ عبر المدارس مجتمعةً، فلا يصح أن تدخل المستخدم في مدرسة."""
        self.client.force_login(self.director)

        response = self.client.get(reverse("reports:executive_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.client.session.get("active_school_id"))
