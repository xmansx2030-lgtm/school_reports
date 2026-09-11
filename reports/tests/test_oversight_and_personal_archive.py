# -*- coding: utf-8 -*-
"""البنود المتبقية: السجل المُنطَق، وإشراف المجموعة، وأرشيف الأعمال الشخصي.

الخاصية المشتركة بين هذه الشاشات كلها: **النطاق يضيق بضيق الصلاحية**. سجل
الوكيل يقف عند أقسامه، وشاشات المدير التنفيذي تقف عند مجموعته، وأرشيف الأعمال
يقف عند صاحبه — وكلها مفروضة في الاستعلام لا في القالب.
"""
from __future__ import annotations

from datetime import date

from django.test import TestCase, override_settings
from django.urls import reverse

from reports import capabilities as caps
from reports.models import (
    AuditLog,
    Department,
    DepartmentMembership,
    Report,
    ReportType,
    School,
    SchoolGroup,
    SchoolGroupMembership,
    SchoolMembership,
    SchoolSubscription,
    StaffScope,
    SubscriptionPlan,
    Teacher,
)


def _user(name: str, phone: str) -> Teacher:
    return Teacher.objects.create_user(phone=phone, name=name, password="Passw0rd!123")


def _plan():
    return SubscriptionPlan.objects.create(
        name="باقة", price=0, days_duration=365, max_teachers=0
    )


@override_settings(ALLOWED_HOSTS=["testserver"])
class ScopedAuditLogTests(TestCase):
    """سجل الإجراءات في نطاق الوكيل — بند صريح في توصيفه."""

    def setUp(self):
        self.school = School.objects.create(name="مدرسة السجل", code="au-school")
        SchoolSubscription.objects.create(school=self.school, plan=_plan())

        self.manager = _user("المدير", "0500100001")
        SchoolMembership.objects.create(
            school=self.school, teacher=self.manager, role_type=SchoolMembership.RoleType.MANAGER
        )
        self.mine = Department.objects.create(school=self.school, name="قسمي", slug="au-mine")
        self.other = Department.objects.create(school=self.school, name="قسم آخر", slug="au-other")

        self.inside = _user("داخل النطاق", "0500100002")
        SchoolMembership.objects.create(
            school=self.school, teacher=self.inside, role_type=SchoolMembership.RoleType.TEACHER
        )
        DepartmentMembership.objects.create(department=self.mine, teacher=self.inside)

        self.outside = _user("خارج النطاق", "0500100003")
        SchoolMembership.objects.create(
            school=self.school, teacher=self.outside, role_type=SchoolMembership.RoleType.TEACHER
        )
        DepartmentMembership.objects.create(department=self.other, teacher=self.outside)

        self.deputy = _user("الوكيل", "0500100004")
        membership = SchoolMembership.objects.create(
            school=self.school, teacher=self.deputy, role_type=SchoolMembership.RoleType.DEPUTY
        )
        scope = StaffScope.objects.create(
            membership=membership, capabilities=[caps.VIEW_AUDIT_LOG]
        )
        scope.departments.add(self.mine)

        for actor, label in ((self.inside, "أثر داخل النطاق"), (self.outside, "أثر خارج النطاق")):
            AuditLog.objects.create(
                school=self.school,
                teacher=actor,
                actor_name=actor.name,
                action=AuditLog.Action.CREATE,
                model_name="Report",
                object_repr=label,
            )

        self.url = reverse("reports:school_audit_logs")

    def _enter(self, user):
        self.client.force_login(user)
        session = self.client.session
        session["active_school_id"] = self.school.pk
        session.save()

    def test_the_manager_sees_everything(self):
        self._enter(self.manager)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "أثر داخل النطاق")
        self.assertContains(response, "أثر خارج النطاق")

    def test_the_deputy_sees_only_their_departments(self):
        self._enter(self.deputy)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "أثر داخل النطاق")
        self.assertNotContains(response, "أثر خارج النطاق")

    def test_a_deputy_without_the_capability_is_refused(self):
        bare = _user("وكيل بلا صلاحية", "0500100010")
        SchoolMembership.objects.create(
            school=self.school, teacher=bare, role_type=SchoolMembership.RoleType.DEPUTY
        )
        self._enter(bare)

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)

    def test_a_plain_teacher_is_refused(self):
        self._enter(self.inside)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)

    def test_an_empty_scope_shows_nothing_not_everything(self):
        """نطاقٌ بلا أقسام يعني سجلاً فارغاً لا سجلاً كاملاً."""
        deputy = _user("وكيل بلا أقسام", "0500100011")
        membership = SchoolMembership.objects.create(
            school=self.school, teacher=deputy, role_type=SchoolMembership.RoleType.DEPUTY
        )
        StaffScope.objects.create(membership=membership, capabilities=[caps.VIEW_AUDIT_LOG])
        self._enter(deputy)

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "أثر داخل النطاق")
        self.assertNotContains(response, "أثر خارج النطاق")

    def test_a_query_filter_cannot_widen_the_scope(self):
        """المرشّح يُبنى فوق النطاق لا بدلاً منه."""
        self._enter(self.deputy)
        response = self.client.get(self.url, {"teacher": self.outside.pk})

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "أثر خارج النطاق")


@override_settings(ALLOWED_HOSTS=["testserver"])
class GroupOversightTests(TestCase):
    """شاشات إشراف المدير التنفيذي — قراءة، ومقيَّدة بمجموعته."""

    def setUp(self):
        self.group = SchoolGroup.objects.create(name="مجمع", code="ov-group")
        self.other_group = SchoolGroup.objects.create(name="مجمع آخر", code="ov-other")

        self.director = _user("المدير التنفيذي", "0500101001")
        SchoolGroupMembership.objects.create(group=self.group, user=self.director)

        self.school = School.objects.create(
            name="مدرسة المجموعة", code="ov-school", group=self.group
        )
        SchoolSubscription.objects.create(school=self.school, plan=_plan())
        self.manager = _user("مدير المدرسة", "0500101002")
        SchoolMembership.objects.create(
            school=self.school, teacher=self.manager, role_type=SchoolMembership.RoleType.MANAGER
        )

        self.outsider_school = School.objects.create(
            name="مدرسة خارجية", code="ov-far", group=self.other_group
        )
        SchoolSubscription.objects.create(school=self.outsider_school, plan=_plan())

        AuditLog.objects.create(
            school=self.school,
            teacher=self.manager,
            actor_name=self.manager.name,
            action=AuditLog.Action.CREATE,
            model_name="Report",
            object_repr="أثر في مدرستي",
        )
        AuditLog.objects.create(
            school=self.outsider_school,
            actor_name="غريب",
            action=AuditLog.Action.CREATE,
            model_name="Report",
            object_repr="أثر خارج المجموعة",
        )

    # ── تفاصيل المدرسة ────────────────────────────────────────────────
    def test_the_director_opens_a_school_detail(self):
        self.client.force_login(self.director)
        response = self.client.get(
            reverse("reports:group_school_detail", args=[self.school.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "مدرسة المجموعة")

    def test_a_school_outside_the_group_reads_as_missing(self):
        self.client.force_login(self.director)
        response = self.client.get(
            reverse("reports:group_school_detail", args=[self.outsider_school.pk])
        )
        self.assertEqual(response.status_code, 404)

    def test_a_school_manager_cannot_open_the_oversight_screen(self):
        self.client.force_login(self.manager)
        response = self.client.get(
            reverse("reports:group_school_detail", args=[self.school.pk])
        )
        self.assertEqual(response.status_code, 404)

    def test_the_detail_screen_does_not_expose_report_contents(self):
        """يتابع أداء المدرسة ولا يفتّش أعمال منسوبيها."""
        teacher = _user("معلم", "0500101010")
        SchoolMembership.objects.create(
            school=self.school, teacher=teacher, role_type=SchoolMembership.RoleType.TEACHER
        )
        category = ReportType.objects.create(school=self.school, code="ov", name="نوع")
        Report.objects.create(
            school=self.school,
            teacher=teacher,
            title="عنوان تقرير خاص",
            report_date=date(2026, 8, 1),
            category=category,
        )
        self.client.force_login(self.director)

        response = self.client.get(
            reverse("reports:group_school_detail", args=[self.school.pk])
        )
        self.assertNotContains(response, "عنوان تقرير خاص")

    # ── سجل المجموعة ──────────────────────────────────────────────────
    def test_the_group_audit_log_covers_group_schools_only(self):
        self.client.force_login(self.director)
        response = self.client.get(reverse("reports:group_audit_log"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "أثر في مدرستي")
        self.assertNotContains(response, "أثر خارج المجموعة")

    def test_a_school_manager_cannot_open_the_group_audit_log(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse("reports:group_audit_log"))
        self.assertEqual(response.status_code, 404)

    def test_a_school_filter_outside_the_group_is_ignored(self):
        self.client.force_login(self.director)
        response = self.client.get(
            reverse("reports:group_audit_log"), {"school": self.outsider_school.pk}
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "أثر خارج المجموعة")

    # ── أرشيف المجموعة ────────────────────────────────────────────────
    def test_the_group_archive_lists_schools_without_archives(self):
        """المدرسة بلا أرشيف هي ما يستحق التنبيه."""
        self.client.force_login(self.director)
        response = self.client.get(reverse("reports:group_archive"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "مدرسة المجموعة")
        self.assertContains(response, "بلا أرشيف")

    def test_a_school_manager_cannot_open_the_group_archive(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse("reports:group_archive"))
        self.assertEqual(response.status_code, 404)


@override_settings(ALLOWED_HOSTS=["testserver"])
class PersonalWorkArchiveTests(TestCase):
    """أرشيف الأعمال الشخصي — ما أنتجتَه لا ما فعلتَه."""

    def setUp(self):
        self.school = School.objects.create(
            name="مدرسة الأعمال", code="wa-school", current_academic_year="1447-1448"
        )
        SchoolSubscription.objects.create(school=self.school, plan=_plan())

        self.manager = _user("المدير", "0500102001")
        SchoolMembership.objects.create(
            school=self.school, teacher=self.manager, role_type=SchoolMembership.RoleType.MANAGER
        )
        self.mine = _user("صاحب الأعمال", "0500102002")
        SchoolMembership.objects.create(
            school=self.school, teacher=self.mine, role_type=SchoolMembership.RoleType.TEACHER
        )
        self.other = _user("زميل", "0500102003")
        SchoolMembership.objects.create(
            school=self.school, teacher=self.other, role_type=SchoolMembership.RoleType.TEACHER
        )

        category = ReportType.objects.create(school=self.school, code="wa", name="نوع")
        Report.objects.create(
            school=self.school,
            teacher=self.mine,
            title="تقريري الخاص",
            report_date=date(2026, 8, 1),
            academic_year="1447-1448",
            category=category,
        )
        Report.objects.create(
            school=self.school,
            teacher=self.other,
            title="تقرير الزميل",
            report_date=date(2026, 8, 1),
            academic_year="1447-1448",
            category=category,
        )
        self.url = reverse("reports:my_work_archive")

    def _enter(self, user):
        self.client.force_login(user)
        session = self.client.session
        session["active_school_id"] = self.school.pk
        session.save()

    def test_it_shows_only_the_viewers_own_work(self):
        self._enter(self.mine)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "تقريري الخاص")
        self.assertNotContains(response, "تقرير الزميل")

    def test_the_year_filter_narrows_results(self):
        """التصفية تعمل بين سنتين للمستخدم نفسه."""
        category = ReportType.objects.get(school=self.school, code="wa")
        Report.objects.create(
            school=self.school,
            teacher=self.mine,
            title="تقرير السنة الماضية",
            report_date=date(2025, 9, 1),
            academic_year="1446-1447",
            category=category,
        )
        self._enter(self.mine)

        response = self.client.get(self.url, {"year": "1446-1447"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "تقرير السنة الماضية")
        self.assertNotContains(response, "تقريري الخاص")

    def test_a_year_the_user_has_no_work_in_is_ignored(self):
        """قائمةٌ تعرض سنواتٍ لا عمل له فيها تجعله يبحث في فراغ — فتُتجاهَل."""
        self._enter(self.mine)
        response = self.client.get(self.url, {"year": "1440-1441"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "تقريري الخاص")

    def test_an_unknown_year_is_ignored_rather_than_erroring(self):
        self._enter(self.mine)
        response = self.client.get(self.url, {"year": "'; DROP--"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "تقريري الخاص")

    def test_a_newcomer_sees_the_empty_state(self):
        newcomer = _user("جديد", "0500102010")
        SchoolMembership.objects.create(
            school=self.school, teacher=newcomer, role_type=SchoolMembership.RoleType.TEACHER
        )
        self._enter(newcomer)

        response = self.client.get(self.url)
        self.assertContains(response, "أرشيفك فارغ")

    def test_an_anonymous_visitor_is_redirected(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
