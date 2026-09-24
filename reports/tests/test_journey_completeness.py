# -*- coding: utf-8 -*-
"""ما تبقّى من ثقوب الرحلات بعد إغلاق الأعطال الكبرى.

أربعةٌ من نوعين مختلفين:

- **ثقبٌ في الطريق**: عملٌ يملكه صاحبه ولا شاشة تجمعه له — كشفُ تقارير الوكيل،
  وصندوقُ اعتماد المدير التنفيذي، ورابطُ ملف إنجاز عضو القسم.
- **ثقبٌ في العزل**: شاشةٌ تعرض أكثر مما يحقّ لصاحبها — كشفُ الخطط كان يُظهر
  خطط المدرسة كلها لكل منسوب.

والثاني أخطر: الأول يُشتكى منه فيُصلَح، والثاني لا يشتكي منه أحد.
"""
from __future__ import annotations

from datetime import date, timedelta

from django.core.cache import cache
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from reports import capabilities as caps
from reports.model_parts.approvals import ApprovalState
from reports.models import (
    Assignment,
    AssignmentTarget,
    Department,
    DepartmentMembership,
    Meeting,
    MeetingMinutes,
    Plan,
    PlanTask,
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
from reports.services_plans import plans_visible_to

PASSWORD = "Passw0rd!123"


def _user(name: str, phone: str) -> Teacher:
    return Teacher.objects.create_user(phone=phone, name=name, password=PASSWORD)


def _school(name: str, code: str, **kwargs) -> School:
    plan = SubscriptionPlan.objects.create(
        name=f"باقة {code}", price=0, days_duration=365, max_teachers=0
    )
    school = School.objects.create(name=name, code=code, **kwargs)
    SchoolSubscription.objects.create(school=school, plan=plan)
    return school


class CompletenessTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.school = _school("ثانوية الإكمال", "completeness")

        self.manager = _user("مدير الإكمال", "0500041001")
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )

        self.department = Department.objects.create(
            school=self.school, name="قسم العلوم", slug="comp-science"
        )
        self.other_department = Department.objects.create(
            school=self.school, name="قسم اللغات", slug="comp-languages"
        )

        self.deputy = _user("وكيل الإكمال", "0500041002")
        self.deputy_membership = SchoolMembership.objects.create(
            school=self.school,
            teacher=self.deputy,
            role_type=SchoolMembership.RoleType.DEPUTY,
        )

        self.teacher = _user("معلم العلوم", "0500041003")
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.teacher,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        DepartmentMembership.objects.create(
            department=self.department,
            teacher=self.teacher,
            role_type=DepartmentMembership.TEACHER,
        )

        self.outsider = _user("معلم اللغات", "0500041004")
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.outsider,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        DepartmentMembership.objects.create(
            department=self.other_department,
            teacher=self.outsider,
            role_type=DepartmentMembership.TEACHER,
        )

    # ------------------------------------------------------------------
    def _grant(self, *capabilities, departments=None):
        scope, _ = StaffScope.objects.get_or_create(membership=self.deputy_membership)
        scope.capabilities = list(capabilities)
        scope.save()
        scope.departments.set(
            departments if departments is not None else [self.department]
        )
        cache.clear()
        return scope

    def _enter(self, user):
        self.client.force_login(user)
        session = self.client.session
        session["active_school_id"] = self.school.pk
        session.save()

    def _page(self, user, url_name, *args) -> str:
        self._enter(user)
        return self.client.get(reverse(url_name, args=args)).content.decode()

    def _report(self, teacher, department, title):
        # اسم النوع محيَّد عن عنوان التقرير: قائمة الأنواع تُعرض في مرشّح
        # الصفحة، فاسمٌ يحتوي العنوان يجعل التوكيد يُطابق خيار المرشّح لا صفّ
        # التقرير — فيمرّ الاختبار وإن سقط العزل، أو يسقط وإن صحّ.
        report_type = ReportType.objects.create(
            school=self.school,
            code=f"rt-{department.slug}-{abs(hash(title)) % 10000}",
            name=f"نوع {department.slug}",
        )
        report_type.departments.add(department)
        return Report.objects.create(
            school=self.school,
            teacher=teacher,
            category=report_type,
            title=title,
            report_date=date(2026, 8, 1),
        )


# ═══════════════════════════════════════════════════════════════════════
# أ) كشف تقارير الوكيل
# ═══════════════════════════════════════════════════════════════════════
@override_settings(ALLOWED_HOSTS=["testserver"])
class ScopedSchoolReportsTests(CompletenessTestCase):
    """الوكيل كان يراجع فرداً فرداً ولا يملك كشفاً يسأل منه «ما وثّقه قسمي؟»."""

    def test_approval_states_match_the_report_in_desktop_and_mobile(self):
        self.school.report_approval_enabled = True
        self.school.save(update_fields=["report_approval_enabled"])
        report_type = ReportType.objects.create(
            school=self.school, code="approval-list", name="تقارير الاعتماد"
        )
        report_type.departments.add(self.department)
        expected = []
        for state in ApprovalState:
            report = Report.objects.create(
                school=self.school,
                teacher=self.teacher,
                category=report_type,
                title=f"تقرير الاعتماد {state.value}",
                report_date=date(2026, 8, 1),
                approval_state=state,
            )
            expected.append(report)

        self._enter(self.manager)
        response = self.client.get(reverse("reports:admin_reports"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        desktop, mobile = html.split('<div class="repdash-mobile"', 1)
        for report in expected:
            label = f"<span>{report.get_approval_state_display()}</span>"
            self.assertIn(label, desktop)
            self.assertIn(label, mobile)
            self.assertIn(f'data-status="{report.approval_tone}"', desktop)
            self.assertIn(f'data-status="{report.approval_tone}"', mobile)

        # ApprovalTransition is history, not the current state source. A report
        # without a transition still has its real persisted draft state.
        draft = next(report for report in expected if report.approval_state == ApprovalState.DRAFT)
        self.assertEqual(draft.get_approval_state_display(), "مسودة")

    def test_approval_state_does_not_bypass_school_isolation(self):
        own = self._report(self.teacher, self.department, "تقرير المدرسة الحالية")
        own.approval_state = ApprovalState.SUBMITTED
        own.save(update_fields=["approval_state"])
        other_school = _school("مدرسة أخرى", "approval-other-school")
        other_teacher = _user("معلم مدرسة أخرى", "0500041999")
        other = Report.objects.create(
            school=other_school,
            teacher=other_teacher,
            title="تقرير مدرسة أخرى خاص",
            report_date=date(2026, 8, 1),
            approval_state=ApprovalState.APPROVED,
        )

        self._enter(self.manager)
        response = self.client.get(reverse("reports:admin_reports"))
        report_ids = {report.pk for report in response.context["reports"]}
        self.assertIn(own.pk, report_ids)
        self.assertNotIn(other.pk, report_ids)
        self.assertNotContains(response, other.title)

    def test_approval_state_does_not_add_per_report_queries(self):
        first = self._report(self.teacher, self.department, "تقرير قياس الاستعلام")
        self._enter(self.manager)
        cache.clear()
        with CaptureQueriesContext(connection) as one_report_queries:
            one_report = self.client.get(reverse("reports:admin_reports"))
        self.assertEqual(one_report.status_code, 200)

        for index in range(6):
            Report.objects.create(
                school=self.school,
                teacher=self.teacher,
                category=first.category,
                title=f"تقرير قياس إضافي {index}",
                report_date=date(2026, 8, 1),
                approval_state=ApprovalState.SUBMITTED,
            )
        cache.clear()
        with CaptureQueriesContext(connection) as many_report_queries:
            many_reports = self.client.get(reverse("reports:admin_reports"))
        self.assertEqual(many_reports.status_code, 200)
        # The second request may reuse warmed caches, but adding six rows must
        # not increase the query count by one query per row.
        self.assertLessEqual(len(many_report_queries), len(one_report_queries))
        with self.assertNumQueries(0):
            states = [
                (report.get_approval_state_display(), report.approval_tone)
                for report in many_reports.context["reports"]
            ]
        self.assertEqual(len(states), 7)

    def test_the_reviewer_reaches_the_school_reports_list(self):
        self._grant(caps.REVIEW_REPORTS)
        self._enter(self.deputy)
        response = self.client.get(reverse("reports:admin_reports"))
        self.assertEqual(response.status_code, 200)

    def test_the_list_carries_their_scope_and_nothing_beyond_it(self):
        """التوكيد على **الاستعلام** لا على نصّ الصفحة.

        الصفحة تحمل مرشّحات ومسمّيات ونصوصاً مشتركة، فبحثٌ نصّي فيها يخلط ما
        عُرض في صفٍّ بما ذُكر في خيار مرشّح — فيمرّ الاختبار وإن سقط العزل.
        """
        self._grant(caps.REVIEW_REPORTS)
        self._report(self.teacher, self.department, "تقرير علمي")
        self._report(self.outsider, self.other_department, "تقرير لغوي")
        self._enter(self.deputy)
        response = self.client.get(reverse("reports:admin_reports"))
        titles = {report.title for report in response.context["reports"]}
        self.assertEqual(titles, {"تقرير علمي"})

    def test_the_manager_still_sees_every_report(self):
        self._report(self.teacher, self.department, "تقرير علمي")
        self._report(self.outsider, self.other_department, "تقرير لغوي")
        page = self._page(self.manager, "reports:admin_reports")
        self.assertIn("تقرير علمي", page)
        self.assertIn("تقرير لغوي", page)

    def test_the_reviewer_owns_no_row_actions(self):
        """صلاحيته «مراجعة … دون اعتماد نهائي» — فالحذف والتعديل للمدير."""
        self._grant(caps.REVIEW_REPORTS)
        self._report(self.teacher, self.department, "تقرير علمي")
        self._enter(self.deputy)
        response = self.client.get(reverse("reports:admin_reports"))
        self.assertFalse(response.context["can_delete"])
        self.assertNotContains(response, f'href="{reverse("reports:report_trash")}"')
        for report in response.context["reports"]:
            self.assertFalse(report.user_can_delete)
            self.assertFalse(report.user_can_edit)
            self.assertFalse(report.user_can_share)

    def test_the_manager_keeps_their_row_actions(self):
        self._report(self.teacher, self.department, "تقرير علمي")
        self._enter(self.manager)
        response = self.client.get(reverse("reports:admin_reports"))
        self.assertTrue(response.context["can_delete"])

    def test_a_plain_teacher_is_still_refused(self):
        """عقدٌ محروس في ``MANAGER_ONLY_PAGES`` — لا يجوز أن يفتحه التوسيع."""
        self._enter(self.teacher)
        response = self.client.get(reverse("reports:admin_reports"))
        self.assertNotEqual(response.status_code, 200)

    def test_an_empty_scope_yields_an_empty_list(self):
        self._grant(caps.REVIEW_REPORTS, departments=[])
        self._report(self.teacher, self.department, "تقرير علمي")
        page = self._page(self.deputy, "reports:admin_reports")
        self.assertNotIn("تقرير علمي", page)

    def test_the_reviewer_is_offered_the_list_in_the_nav(self):
        self._grant(caps.REVIEW_REPORTS)
        page = self._page(self.deputy, "reports:home")
        self.assertIn(f'href="{reverse("reports:admin_reports")}"', page)


# ═══════════════════════════════════════════════════════════════════════
# ب) صندوق اعتماد المجموعة
# ═══════════════════════════════════════════════════════════════════════
@override_settings(ALLOWED_HOSTS=["testserver"])
class GroupApprovalInboxTests(TestCase):
    """المدير التنفيذي كان يفتح كل تكليف وكل جلسة ليعرف هل ينتظره شيء."""

    def setUp(self):
        cache.clear()
        self.group = SchoolGroup.objects.create(name="مجموعة الإكمال", code="comp-group")
        self.school = _school("مدرسة المجموعة", "comp-group-1", group=self.group)
        self.director = _user("المدير التنفيذي", "0500042001")
        SchoolGroupMembership.objects.create(
            group=self.group,
            user=self.director,
            role_type=SchoolGroupMembership.RoleType.EXECUTIVE_DIRECTOR,
        )
        self.principal = _user("مدير المدرسة", "0500042002")
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.principal,
            role_type=SchoolMembership.RoleType.MANAGER,
        )

    def _group_assignment(self, title="تكليف المجموعة", state=ApprovalState.SUBMITTED):
        assignment = Assignment.objects.create(
            scope=Assignment.Scope.GROUP,
            group=self.group,
            issuer=self.director,
            title=title,
            due_at=timezone.now() + timedelta(days=7),
        )
        AssignmentTarget.objects.create(
            assignment=assignment,
            assignee=self.principal,
            school=self.school,
            approval_state=state,
        )
        return assignment

    def test_the_inbox_opens_for_the_director(self):
        self.client.force_login(self.director)
        response = self.client.get(reverse("reports:group_approval_inbox"))
        self.assertEqual(response.status_code, 200)

    def test_it_is_invisible_to_everyone_else(self):
        for user in (self.principal, _user("غريب", "0500042009")):
            with self.subTest(user=user.name):
                self.client.force_login(user)
                response = self.client.get(reverse("reports:group_approval_inbox"))
                self.assertEqual(response.status_code, 404)

    def test_a_submitted_reply_reaches_the_inbox(self):
        self._group_assignment("ردٌّ ينتظر")
        self.client.force_login(self.director)
        response = self.client.get(reverse("reports:group_approval_inbox"))
        self.assertEqual(len(response.context["assignment_rows"]), 1)
        self.assertContains(response, "ردٌّ ينتظر")

    def test_replies_are_grouped_by_their_assignment(self):
        """تكليفٌ على عشر مدارس بندٌ واحد فيه عشرة ردود لا عشرة بنود."""
        assignment = self._group_assignment("تكليف واسع")
        other_school = _school("مدرسة ثانية", "comp-group-2", group=self.group)
        other_principal = _user("مدير ثانٍ", "0500042003")
        SchoolMembership.objects.create(
            school=other_school,
            teacher=other_principal,
            role_type=SchoolMembership.RoleType.MANAGER,
        )
        AssignmentTarget.objects.create(
            assignment=assignment,
            assignee=other_principal,
            school=other_school,
            approval_state=ApprovalState.SUBMITTED,
        )
        self.client.force_login(self.director)
        response = self.client.get(reverse("reports:group_approval_inbox"))
        rows = response.context["assignment_rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]["targets"]), 2)

    def test_an_approved_reply_leaves_the_inbox(self):
        self._group_assignment(state=ApprovalState.APPROVED)
        self.client.force_login(self.director)
        response = self.client.get(reverse("reports:group_approval_inbox"))
        self.assertEqual(response.context["assignment_rows"], [])

    def test_another_directors_assignment_stays_out(self):
        other_group = SchoolGroup.objects.create(name="مجموعة أخرى", code="comp-other")
        other_director = _user("مدير تنفيذي آخر", "0500042004")
        SchoolGroupMembership.objects.create(
            group=other_group,
            user=other_director,
            role_type=SchoolGroupMembership.RoleType.EXECUTIVE_DIRECTOR,
        )
        stranger = Assignment.objects.create(
            scope=Assignment.Scope.GROUP,
            group=other_group,
            issuer=other_director,
            title="تكليف غريب",
            due_at=timezone.now() + timedelta(days=5),
        )
        AssignmentTarget.objects.create(
            assignment=stranger,
            assignee=self.principal,
            school=self.school,
            approval_state=ApprovalState.SUBMITTED,
        )
        self.client.force_login(self.director)
        response = self.client.get(reverse("reports:group_approval_inbox"))
        self.assertEqual(response.context["assignment_rows"], [])

    def test_a_held_council_without_minutes_is_flagged(self):
        Meeting.objects.create(
            scope=Meeting.Scope.GROUP,
            group=self.group,
            organizer=self.director,
            title="جلسة بلا محضر",
            status=Meeting.Status.HELD,
            scheduled_at=timezone.now() - timedelta(days=1),
        )
        self.client.force_login(self.director)
        response = self.client.get(reverse("reports:group_approval_inbox"))
        self.assertEqual(len(response.context["minutes_missing"]), 1)
        self.assertContains(response, "جلسة بلا محضر")

    def test_a_submitted_council_minute_reaches_the_inbox(self):
        meeting = Meeting.objects.create(
            scope=Meeting.Scope.GROUP,
            group=self.group,
            organizer=self.director,
            title="جلسة بمحضر",
            status=Meeting.Status.HELD,
            scheduled_at=timezone.now() - timedelta(days=1),
        )
        MeetingMinutes.objects.create(
            meeting=meeting,
            recorder=self.director,
            body="ما جرى في الجلسة.",
            approval_state=ApprovalState.SUBMITTED,
        )
        self.client.force_login(self.director)
        response = self.client.get(reverse("reports:group_approval_inbox"))
        self.assertEqual(len(response.context["minutes_rows"]), 1)

    def test_an_empty_inbox_says_so(self):
        self.client.force_login(self.director)
        response = self.client.get(reverse("reports:group_approval_inbox"))
        self.assertContains(response, "لا شيء ينتظر قرارك")

    def test_the_director_is_offered_the_inbox_in_the_nav(self):
        self.client.force_login(self.director)
        page = self.client.get(reverse("reports:executive_dashboard")).content.decode()
        self.assertIn(f'href="{reverse("reports:group_approval_inbox")}"', page)


# ═══════════════════════════════════════════════════════════════════════
# ج) ملف الإنجاز لعضو القسم
# ═══════════════════════════════════════════════════════════════════════
@override_settings(ALLOWED_HOSTS=["testserver"])
class OwnAchievementFileStaysReachableTests(CompletenessTestCase):
    """ملفٌ يُقيَّم عليه صاحبه سنوياً لا يُحجب عنه لأنه عضو قسم."""

    def test_a_department_member_still_finds_their_own_file(self):
        page = self._page(self.teacher, "reports:home")
        self.assertIn(f'href="{reverse("reports:achievement_my_files")}"', page)

    def test_an_officer_still_finds_their_own_file(self):
        DepartmentMembership.objects.filter(
            department=self.department, teacher=self.teacher
        ).update(role_type=DepartmentMembership.OFFICER)
        cache.clear()
        page = self._page(self.teacher, "reports:home")
        self.assertIn(f'href="{reverse("reports:achievement_my_files")}"', page)

    def test_a_teacher_outside_any_department_finds_it_too(self):
        loner = _user("معلم بلا قسم", "0500041010")
        SchoolMembership.objects.create(
            school=self.school,
            teacher=loner,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        cache.clear()
        page = self._page(loner, "reports:home")
        self.assertIn(f'href="{reverse("reports:achievement_my_files")}"', page)


# ═══════════════════════════════════════════════════════════════════════
# د) عزل كشف الخطط
# ═══════════════════════════════════════════════════════════════════════
@override_settings(ALLOWED_HOSTS=["testserver"])
class PlanListIsScopedTests(CompletenessTestCase):
    """الكشف كان يعرض خطط المدرسة كلها لكل منسوب، والتفصيل يمنعها عليه.

    وعنوانُ الخطة وحده خبر: «معالجة تدنّي نتائج الصف الثالث» تُقرأ من الكشف.
    """

    def _plan(self, owner, title):
        return Plan.objects.create(
            school=self.school,
            owner=owner,
            title=title,
            scope=Plan.Scope.SCHOOL,
        )

    def test_a_teacher_sees_no_plan_they_are_not_party_to(self):
        self._plan(self.manager, "خطة الإدارة")
        self.assertEqual(list(plans_visible_to(self.teacher, self.school)), [])

    def test_the_plan_list_hides_it_on_screen_too(self):
        self._plan(self.manager, "خطة الإدارة")
        page = self._page(self.teacher, "reports:plan_list")
        self.assertNotIn("خطة الإدارة", page)

    def test_the_manager_sees_every_plan(self):
        self._plan(self.manager, "خطة الإدارة")
        self.assertEqual(len(plans_visible_to(self.manager, self.school)), 1)

    def test_the_owner_sees_their_own_plan(self):
        self._plan(self.teacher, "خطتي")
        self.assertEqual(len(plans_visible_to(self.teacher, self.school)), 1)

    def test_whoever_holds_a_task_sees_its_plan(self):
        """من يُنفّذ جزءاً من خطة يحق له أن يرى موقعه منها."""
        plan = self._plan(self.manager, "خطة فيها مهمتي")
        PlanTask.objects.create(plan=plan, title="مهمة", responsible=self.teacher)
        self.assertEqual(len(plans_visible_to(self.teacher, self.school)), 1)

    def test_two_tasks_in_one_plan_do_not_duplicate_it(self):
        plan = self._plan(self.manager, "خطة بمهمتين")
        PlanTask.objects.create(plan=plan, title="أولى", responsible=self.teacher)
        PlanTask.objects.create(plan=plan, title="ثانية", responsible=self.teacher)
        self.assertEqual(len(plans_visible_to(self.teacher, self.school)), 1)

    def test_track_plans_opens_only_the_scoped_department_list(self):
        plan = self._plan(self.manager, "خطة الإدارة")
        PlanTask.objects.create(plan=plan, title="مهمة القسم", department=self.department)
        self._grant(caps.TRACK_PLANS)
        self.assertEqual(len(plans_visible_to(self.deputy, self.school)), 1)

    def test_a_bare_deputy_sees_nothing(self):
        self._plan(self.manager, "خطة الإدارة")
        self.assertEqual(list(plans_visible_to(self.deputy, self.school)), [])

    def test_the_owner_still_opens_their_plan_detail(self):
        """الكشف ضاق ولم يُغلق ما كان مفتوحاً."""
        plan = self._plan(self.teacher, "خطتي")
        self._enter(self.teacher)
        response = self.client.get(reverse("reports:plan_detail", args=[plan.pk]))
        self.assertEqual(response.status_code, 200)


# ═══════════════════════════════════════════════════════════════════════
# هـ) شريط المدير التنفيذي
# ═══════════════════════════════════════════════════════════════════════
@override_settings(ALLOWED_HOSTS=["testserver"])
class ExecutiveHeaderNavigationTests(TestCase):
    """شريطه كان تسعة مداخل مسطّحة — العلّة نفسها التي جُمّع من أجلها شريط المدير.

    وتُحرَس هنا الخصائص الثلاث نفسها: أن الشريط بقي قصيراً، وأن شيئاً من وجهاته
    لم يسقط في أثناء التجميع، وأن المجموعات تُشغَّل بلوحة المفاتيح وقارئ الشاشة.
    """

    def setUp(self):
        cache.clear()
        self.group = SchoolGroup.objects.create(name="مجموعة الشريط", code="bar-group")
        _school("مدرسة الشريط", "bar-school-1", group=self.group)
        self.director = _user("مدير الشريط التنفيذي", "0500043001")
        SchoolGroupMembership.objects.create(
            group=self.group,
            user=self.director,
            role_type=SchoolGroupMembership.RoleType.EXECUTIVE_DIRECTOR,
        )

    def _header(self) -> str:
        """الشريط وحده — لا بقية الصفحة، فلا تُخلط روابط المحتوى بروابطه."""
        import re

        self.client.force_login(self.director)
        page = self.client.get(reverse("reports:executive_dashboard")).content.decode()
        nav = re.search(r'<nav class="hdr-nav".*?</nav>', page, re.S)
        self.assertIsNotNone(nav, "شريط التنقّل غير موجود في الصفحة")
        return nav.group(0)

    def test_the_bar_stays_short(self):
        import re

        header = self._header()
        direct_tabs = len(re.findall(r'<a class="tab ', header))
        groups = header.count("data-nav-group")
        self.assertLessEqual(
            direct_tabs + groups, 5, "شريط المدير التنفيذي عاد إلى الازدحام"
        )

    def test_no_destination_was_lost_in_the_grouping(self):
        """التجميع إخفاءٌ للضجيج لا للوجهات."""
        header = self._header()
        for name in (
            "reports:executive_dashboard",
            "reports:group_approval_inbox",
            "reports:group_assignment_board",
            "reports:council_list",
            "reports:group_practices",
            "reports:group_report",
            "reports:group_audit_log",
            "reports:group_archive",
            "reports:group_notifications_sent",
        ):
            with self.subTest(destination=name):
                self.assertIn(f'href="{reverse(name)}"', header)

    def test_approval_stays_a_single_click(self):
        """عملُه اليومي لا يُدفَن تحت نقرة ثانية — معيارُ المدير نفسه."""
        import re

        header = self._header()
        inbox = reverse("reports:group_approval_inbox")
        self.assertRegex(header, rf'class="tab [^"]*"\s+href="{re.escape(inbox)}"')

    def test_every_group_is_operable_by_keyboard_and_screen_reader(self):
        header = self._header()
        self.assertEqual(
            header.count("data-nav-group"), header.count('aria-haspopup="true"')
        )
        self.assertEqual(
            header.count("data-nav-group"), header.count('aria-expanded="false"')
        )
        self.assertEqual(header.count('class="nav-pop"'), header.count('role="menu"'))

    def test_the_grouped_shell_is_marked_for_the_director_too(self):
        """الوسم هو ما يؤخّر انهيار الشريط، فيُمنح لكل شريط مجمَّع."""
        import re

        self.client.force_login(self.director)
        page = self.client.get(reverse("reports:executive_dashboard")).content.decode()
        shell = re.search(r"<header[^>]*>", page)
        self.assertIsNotNone(shell)
        self.assertIn("hdr-grouped", shell.group(0))

    def test_personal_work_screens_are_not_offered_to_him(self):
        """ثلاث شاشات تسأل عن مدرسة نشطة وتردّه — فلا تُعرض له.

        وهي القاعدة نفسها المطبَّقة في كل الشريط: لا زرّ يقود إلى منع.
        """
        header = self._header()
        for name in (
            "reports:my_reports",
            "reports:my_requests",
            "reports:achievement_my_files",
        ):
            with self.subTest(destination=name):
                self.assertNotIn(f'href="{reverse(name)}"', header)

    def test_they_really_would_have_refused_him(self):
        """إثبات أن الإخفاء ليس تجميلاً: الشاشة تردّه فعلاً."""
        self.client.force_login(self.director)
        response = self.client.get(reverse("reports:achievement_my_files"))
        self.assertNotEqual(response.status_code, 200)

    def test_a_director_who_also_teaches_keeps_them(self):
        """من جمع الصفتين له عملٌ شخصي فعلاً، فلا يُحجب عنه."""
        school = School.objects.get(code="bar-school-1")
        SchoolMembership.objects.create(
            school=school,
            teacher=self.director,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        cache.clear()
        header = self._header()
        self.assertIn(f'href="{reverse("reports:my_reports")}"', header)
