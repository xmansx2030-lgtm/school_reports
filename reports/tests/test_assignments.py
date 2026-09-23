# -*- coding: utf-8 -*-
"""التكليفات: الإصدار والتنفيذ والتأخر والاعتماد.

الخاصية التي تثبّتها هذه الاختبارات قبل غيرها: **التكليف يرث دورة الاعتماد ولا
يعيد تعريفها**. فقواعد «لا يعتمد أحد عمله» و«المعتمد نهائي» تسري عليه بلا سطر
جديد — وأي اختبار هنا يفشل بينما نظيره في التقارير يمر يعني أن المكوّن انشطر.

ويضاف إليها ما هو خاص بالتكليف: الموعد الذي لم يكن له مقابل في المنصة قبل هذه
المرحلة، وشرط الشواهد الذي يجب أن يُنفَّذ لا أن يبقى توصية.
"""
from __future__ import annotations

from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from reports import capabilities as caps
from reports.forms_assignments import SchoolAssignmentForm
from reports.model_parts.approvals import ApprovalState
from reports.models import (
    Assignment,
    AssignmentTarget,
    Department,
    DepartmentMembership,
    School,
    SchoolMembership,
    SchoolSubscription,
    StaffScope,
    SubscriptionPlan,
    Teacher,
)
from reports.services_approval import ApprovalError, approve, recommend, submit
from reports.services_assignments import (
    accept_target,
    add_evidence,
    overdue_targets_for_school,
    request_clarification,
    update_progress,
)


def _user(name: str, phone: str) -> Teacher:
    return Teacher.objects.create_user(phone=phone, name=name, password="Passw0rd!123")


def _school(name: str, code: str) -> School:
    plan = SubscriptionPlan.objects.create(
        name=f"باقة {code}", price=0, days_duration=365, max_teachers=0
    )
    school = School.objects.create(name=name, code=code)
    SchoolSubscription.objects.create(school=school, plan=plan)
    return school


def _png() -> SimpleUploadedFile:
    # أصغر PNG صالح — يكفي لاختبار مسار الرفع بلا ملف حقيقي.
    data = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
        b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    return SimpleUploadedFile("evidence.png", data, content_type="image/png")


class AssignmentBase(TestCase):
    def setUp(self):
        self.school = _school("مدرسة التكليف", "asg-school")

        self.manager = _user("المدير", "0500030001")
        SchoolMembership.objects.create(
            school=self.school, teacher=self.manager, role_type=SchoolMembership.RoleType.MANAGER
        )
        self.department = Department.objects.create(
            school=self.school, name="الشؤون الإدارية", slug="ops"
        )
        self.staff = _user("الموظف", "0500030002")
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.staff,
            role_type=SchoolMembership.RoleType.ADMIN_STAFF,
        )
        DepartmentMembership.objects.create(department=self.department, teacher=self.staff)

    def _assignment(self, **overrides):
        data = {
            "scope": Assignment.Scope.SCHOOL,
            "school": self.school,
            "issuer": self.manager,
            "title": "جرد المستودع",
            "due_at": timezone.now() + timedelta(days=5),
            "department": self.department,
        }
        data.update(overrides)
        return Assignment.objects.create(**data)

    def _target(self, assignment=None, assignee=None):
        return AssignmentTarget.objects.create(
            assignment=assignment or self._assignment(),
            assignee=assignee or self.staff,
            school=self.school,
        )


class AssignmentModelTests(AssignmentBase):
    def test_a_school_assignment_requires_a_school(self):
        assignment = Assignment(
            scope=Assignment.Scope.SCHOOL,
            issuer=self.manager,
            title="بلا مدرسة",
            due_at=timezone.now() + timedelta(days=1),
        )
        with self.assertRaises(ValidationError):
            assignment.full_clean()

    def test_a_group_assignment_requires_a_group(self):
        assignment = Assignment(
            scope=Assignment.Scope.GROUP,
            issuer=self.manager,
            title="بلا مجموعة",
            due_at=timezone.now() + timedelta(days=1),
        )
        with self.assertRaises(ValidationError):
            assignment.full_clean()

    def test_the_issuer_name_is_snapshotted(self):
        assignment = self._assignment()
        self.assertEqual(assignment.issuer_name, "المدير")

    def test_completion_percent_is_computed_not_stored(self):
        assignment = self._assignment()
        other = _user("موظف آخر", "0500030003")
        SchoolMembership.objects.create(
            school=self.school, teacher=other, role_type=SchoolMembership.RoleType.TEACHER
        )
        first = self._target(assignment=assignment)
        self._target(assignment=assignment, assignee=other)

        self.assertEqual(assignment.completion_percent, 0)

        submit(first, self.staff)
        approve(first, self.manager, school=self.school)
        self.assertEqual(assignment.completion_percent, 50)

    def test_cancelling_keeps_the_row(self):
        """ما وصل المكلَّف صار واقعة؛ ومحوه يجعل من نُفّذ عليه إجراءٌ بلا سبب."""
        assignment = self._assignment()
        assignment.cancel(by=self.manager, reason="تغيّرت الخطة")

        assignment.refresh_from_db()
        self.assertTrue(assignment.is_cancelled)
        self.assertEqual(assignment.cancel_reason, "تغيّرت الخطة")
        self.assertTrue(Assignment.objects.filter(pk=assignment.pk).exists())


class OverdueTests(AssignmentBase):
    """التأخر — المفهوم الذي لم يكن له مقابل في المنصة قبل هذه المرحلة."""

    def test_a_target_past_its_due_date_is_overdue(self):
        assignment = self._assignment(due_at=timezone.now() - timedelta(days=1))
        target = self._target(assignment=assignment)
        self.assertTrue(target.is_overdue)

    def test_an_approved_target_is_never_overdue(self):
        """المعتمد سُلّم فعلاً — وإدراجه في المتأخرات تأنيبٌ على ما انتهى."""
        assignment = self._assignment(due_at=timezone.now() + timedelta(seconds=1))
        target = self._target(assignment=assignment)
        submit(target, self.staff)
        approve(target, self.manager, school=self.school)

        Assignment.objects.filter(pk=assignment.pk).update(
            due_at=timezone.now() - timedelta(days=3)
        )
        target.refresh_from_db()
        target.assignment.refresh_from_db()

        self.assertFalse(target.is_overdue)

    def test_a_cancelled_assignment_is_never_overdue(self):
        assignment = self._assignment(due_at=timezone.now() - timedelta(days=2))
        target = self._target(assignment=assignment)
        assignment.cancel(by=self.manager)
        target.assignment.refresh_from_db()

        self.assertFalse(target.is_overdue)

    def test_the_school_overdue_query_finds_it(self):
        assignment = self._assignment(due_at=timezone.now() - timedelta(days=1))
        target = self._target(assignment=assignment)

        found = list(overdue_targets_for_school(self.school))
        self.assertEqual([item.pk for item in found], [target.pk])

    def test_the_overdue_query_excludes_approved_work(self):
        assignment = self._assignment(due_at=timezone.now() + timedelta(seconds=1))
        target = self._target(assignment=assignment)
        submit(target, self.staff)
        approve(target, self.manager, school=self.school)
        Assignment.objects.filter(pk=assignment.pk).update(
            due_at=timezone.now() - timedelta(days=3)
        )

        self.assertEqual(list(overdue_targets_for_school(self.school)), [])


class ExecutionTests(AssignmentBase):
    """أفعال المكلَّف على عمله — قبل أن يصل إلى مراجع."""

    def test_only_the_assignee_may_update_progress(self):
        target = self._target()
        with self.assertRaises(PermissionDenied):
            update_progress(target, self.manager, percent=50)

    def test_progress_is_bounded(self):
        target = self._target()
        for bad in (-1, 101, "كثير"):
            with self.assertRaises(ApprovalError):
                update_progress(target, self.staff, percent=bad)

    def test_progress_may_go_down(self):
        """عملٌ ظُنّ منجزاً ثم تبيّن نقصه يجب أن يعود رقمه إلى الحقيقة."""
        target = self._target()
        update_progress(target, self.staff, percent=90)
        update_progress(target, self.staff, percent=40)
        self.assertEqual(target.progress_percent, 40)

    def test_updating_progress_counts_as_acceptance(self):
        """لا يُطلب من المكلَّف ضغط زرّين لفعل واحد."""
        target = self._target()
        self.assertIsNone(target.accepted_at)
        update_progress(target, self.staff, percent=10)
        self.assertIsNotNone(target.accepted_at)

    def test_accepting_is_idempotent(self):
        target = self._target()
        accept_target(target, self.staff)
        first = target.accepted_at
        accept_target(target, self.staff)
        self.assertEqual(target.accepted_at, first)

    def test_a_clarification_request_needs_a_question(self):
        target = self._target()
        with self.assertRaises(ApprovalError):
            request_clarification(target, self.staff, note="  ")

    def test_nothing_can_be_changed_after_approval(self):
        target = self._target()
        submit(target, self.staff)
        approve(target, self.manager, school=self.school)

        with self.assertRaises(ApprovalError):
            update_progress(target, self.staff, percent=10)

    def test_nothing_can_be_changed_on_a_cancelled_assignment(self):
        assignment = self._assignment()
        target = self._target(assignment=assignment)
        assignment.cancel(by=self.manager)
        target.assignment.refresh_from_db()

        with self.assertRaises(ApprovalError):
            update_progress(target, self.staff, percent=10)


@override_settings(MEDIA_ROOT="/tmp/asg-test-media")
class EvidenceGateTests(AssignmentBase):
    """اشتراطُ شاهد ثم قبول إرسال بلا شاهد يجعل الشرط زينة."""

    def test_submission_is_blocked_until_evidence_is_attached(self):
        assignment = self._assignment(requires_evidence=True, min_evidence_count=2)
        target = self._target(assignment=assignment)

        with self.assertRaises(ApprovalError):
            submit(target, self.staff)

    def test_submission_passes_once_the_requirement_is_met(self):
        assignment = self._assignment(requires_evidence=True, min_evidence_count=1)
        target = self._target(assignment=assignment)
        add_evidence(target, self.staff, file=_png(), note="صورة الجرد")

        submit(target, self.staff)
        self.assertEqual(target.approval_state, ApprovalState.SUBMITTED)

    def test_shortfall_reports_what_is_missing(self):
        assignment = self._assignment(requires_evidence=True, min_evidence_count=3)
        target = self._target(assignment=assignment)
        add_evidence(target, self.staff, file=_png())

        self.assertEqual(target.evidence_shortfall(), 2)

    def test_an_assignment_without_the_requirement_submits_freely(self):
        target = self._target()
        submit(target, self.staff)
        self.assertEqual(target.approval_state, ApprovalState.SUBMITTED)

    def test_only_the_assignee_may_attach_evidence(self):
        target = self._target()
        with self.assertRaises(PermissionDenied):
            add_evidence(target, self.manager, file=_png())


class AssignmentApprovalTests(AssignmentBase):
    """المكوّن المشترك يعمل على التكليف بلا سطر جديد."""

    def test_the_issuer_reviews_and_approves(self):
        target = self._target()
        submit(target, self.staff)
        approve(target, self.manager, school=self.school)

        self.assertEqual(target.approval_state, ApprovalState.APPROVED)
        self.assertEqual(target.decided_by_id, self.manager.pk)

    def test_an_assignee_cannot_approve_their_own_execution(self):
        """القاعدة نفسها التي تحكم التقارير — تسري هنا بلا إعادة تعريف."""
        target = self._target(assignee=self.manager)
        submit(target, self.manager)

        with self.assertRaises(ApprovalError):
            approve(target, self.manager, school=self.school)

    def test_an_unrelated_colleague_cannot_review(self):
        stranger = _user("غريب", "0500030010")
        SchoolMembership.objects.create(
            school=self.school, teacher=stranger, role_type=SchoolMembership.RoleType.TEACHER
        )
        target = self._target()
        submit(target, self.staff)

        with self.assertRaises(PermissionDenied):
            recommend(target, stranger, school=self.school)

    def test_a_deputy_supervising_the_department_may_recommend(self):
        deputy = _user("الوكيل", "0500030011")
        membership = SchoolMembership.objects.create(
            school=self.school, teacher=deputy, role_type=SchoolMembership.RoleType.DEPUTY
        )
        scope = StaffScope.objects.create(
            membership=membership,
            capabilities=[caps.REVIEW_REPORTS, caps.RECOMMEND_APPROVAL],
        )
        scope.departments.add(self.department)

        target = self._target()
        submit(target, self.staff)
        recommend(target, deputy, school=self.school, note="أُنجز")

        self.assertEqual(target.approval_state, ApprovalState.RECOMMENDED)

    def test_a_deputy_outside_the_department_cannot_review(self):
        other_department = Department.objects.create(
            school=self.school, name="قسم آخر", slug="other-dept"
        )
        deputy = _user("وكيل بعيد", "0500030012")
        membership = SchoolMembership.objects.create(
            school=self.school, teacher=deputy, role_type=SchoolMembership.RoleType.DEPUTY
        )
        scope = StaffScope.objects.create(
            membership=membership, capabilities=[caps.REVIEW_REPORTS]
        )
        scope.departments.add(other_department)

        target = self._target()
        submit(target, self.staff)

        with self.assertRaises(PermissionDenied):
            recommend(target, deputy, school=self.school)


@override_settings(ALLOWED_HOSTS=["testserver"])
class AssignmentScreenTests(AssignmentBase):
    def _enter(self, user):
        self.client.force_login(user)
        session = self.client.session
        session["active_school_id"] = self.school.pk
        session.save()

    def test_the_assignee_sees_their_assignment(self):
        self._target()
        self._enter(self.staff)

        response = self.client.get(reverse("reports:my_assignments"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "جرد المستودع")

    def test_a_colleague_does_not_see_it(self):
        self._target()
        other = _user("زميل", "0500030020")
        SchoolMembership.objects.create(
            school=self.school, teacher=other, role_type=SchoolMembership.RoleType.TEACHER
        )
        self._enter(other)

        response = self.client.get(reverse("reports:my_assignments"))
        self.assertNotContains(response, "جرد المستودع")

    def test_a_plain_staff_member_cannot_open_the_board(self):
        self._enter(self.staff)
        response = self.client.get(reverse("reports:assignment_board"))
        self.assertEqual(response.status_code, 302)

    def test_the_manager_opens_the_board(self):
        self._target()
        self._enter(self.manager)

        response = self.client.get(reverse("reports:assignment_board"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "جرد المستودع")

    def test_manager_without_staff_is_sent_to_team_setup_and_submit_is_disabled(self):
        SchoolMembership.objects.filter(
            school=self.school,
            teacher=self.staff,
        ).update(is_active=False)
        self._enter(self.manager)

        response = self.client.get(reverse("reports:assignment_create"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-has-assignees="false"')
        self.assertContains(response, "إضافة فريق المدرسة الآن")
        self.assertContains(response, f'href="{reverse("reports:bulk_import_teachers")}"')
        self.assertContains(response, 'id="assignmentSubmit"')
        self.assertContains(
            response,
            'disabled aria-disabled="true" aria-describedby="assignmentTeamSetupNotice"',
        )

    def test_issuing_an_assignment_creates_one_target_per_person(self):
        second = _user("موظف ثانٍ", "0500030021")
        SchoolMembership.objects.create(
            school=self.school, teacher=second, role_type=SchoolMembership.RoleType.TEACHER
        )
        self._enter(self.manager)

        due = timezone.localtime() + timedelta(days=4)
        response = self.client.post(
            reverse("reports:assignment_create"),
            {
                "title": "تحديث بيانات المنسوبين",
                "description": "راجع الملف المرفق",
                "department": self.department.pk,
                "priority": Assignment.Priority.NORMAL,
                "due_at": due.strftime("%Y-%m-%dT%H:%M"),
                "min_evidence_count": 1,
                "assignees": [self.staff.pk, second.pk],
            },
        )
        self.assertEqual(response.status_code, 302)

        assignment = Assignment.objects.get(title="تحديث بيانات المنسوبين")
        self.assertEqual(assignment.targets.count(), 2)

    def test_a_due_date_in_the_past_is_refused(self):
        self._enter(self.manager)
        past = timezone.localtime() - timedelta(days=1)

        self.client.post(
            reverse("reports:assignment_create"),
            {
                "title": "تكليف بأثر رجعي",
                "department": self.department.pk,
                "priority": Assignment.Priority.NORMAL,
                "due_at": past.strftime("%Y-%m-%dT%H:%M"),
                "min_evidence_count": 1,
                "assignees": [self.staff.pk],
            },
        )
        self.assertFalse(Assignment.objects.filter(title="تكليف بأثر رجعي").exists())

    def test_a_crafted_create_post_cannot_override_school_issuer_or_target_state(self):
        elsewhere = _school("مدرسة الحقول المصاغة", "asg-crafted")
        stranger = _user("مستهدف من مدرسة أخرى", "0500030040")
        SchoolMembership.objects.create(
            school=elsewhere,
            teacher=stranger,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        self._enter(self.manager)
        due = timezone.localtime() + timedelta(days=3)

        rejected = self.client.post(
            reverse("reports:assignment_create"),
            {
                "title": "مستهدف غير صالح",
                "priority": Assignment.Priority.NORMAL,
                "due_at": due.strftime("%Y-%m-%dT%H:%M"),
                "assignees": [stranger.pk],
                "school": elsewhere.pk,
                "issuer": stranger.pk,
                "approval_state": ApprovalState.APPROVED,
            },
        )

        self.assertEqual(rejected.status_code, 200)
        self.assertFalse(Assignment.objects.filter(title="مستهدف غير صالح").exists())

        accepted = self.client.post(
            reverse("reports:assignment_create"),
            {
                "title": "سياق يثبته الخادم",
                "priority": Assignment.Priority.NORMAL,
                "due_at": due.strftime("%Y-%m-%dT%H:%M"),
                "assignees": [self.staff.pk],
                "school": elsewhere.pk,
                "issuer": stranger.pk,
                "approval_state": ApprovalState.APPROVED,
            },
        )

        self.assertEqual(accepted.status_code, 302)
        assignment = Assignment.objects.get(title="سياق يثبته الخادم")
        target = assignment.targets.get()
        self.assertEqual(assignment.school, self.school)
        self.assertEqual(assignment.issuer, self.manager)
        self.assertEqual(target.school, self.school)
        self.assertEqual(target.approval_state, ApprovalState.DRAFT)

    def test_assignment_create_post_requires_csrf(self):
        csrf_client = self.client_class(enforce_csrf_checks=True)
        csrf_client.force_login(self.manager)
        session = csrf_client.session
        session["active_school_id"] = self.school.pk
        session.save()

        response = csrf_client.post(
            reverse("reports:assignment_create"),
            {
                "title": "طلب بلا CSRF",
                "priority": Assignment.Priority.NORMAL,
                "due_at": (timezone.localtime() + timedelta(days=3)).strftime(
                    "%Y-%m-%dT%H:%M"
                ),
                "assignees": [self.staff.pk],
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(Assignment.objects.filter(title="طلب بلا CSRF").exists())

    def test_an_assignment_target_of_another_school_reads_as_missing(self):
        elsewhere = _school("مدرسة بعيدة", "asg-far")
        stranger = _user("بعيد", "0500030022")
        SchoolMembership.objects.create(
            school=elsewhere, teacher=stranger, role_type=SchoolMembership.RoleType.TEACHER
        )
        far_assignment = Assignment.objects.create(
            scope=Assignment.Scope.SCHOOL,
            school=elsewhere,
            issuer=stranger,
            title="تكليف بعيد",
            due_at=timezone.now() + timedelta(days=3),
        )
        far_target = AssignmentTarget.objects.create(
            assignment=far_assignment, assignee=stranger, school=elsewhere
        )
        self._enter(self.manager)

        response = self.client.get(
            reverse("reports:assignment_detail", args=[far_target.pk])
        )
        self.assertEqual(response.status_code, 404)

    def test_the_board_links_to_the_assignment_itself(self):
        target = self._target()
        self._enter(self.manager)

        response = self.client.get(reverse("reports:assignment_board"))
        self.assertContains(
            response, reverse("reports:assignment_view", args=[target.assignment.pk])
        )
        self.assertContains(
            response, reverse("reports:assignment_print", args=[target.assignment.pk])
        )

    def test_the_issuer_opens_the_assignment_and_its_print(self):
        target = self._target()
        self._enter(self.manager)

        page = self.client.get(
            reverse("reports:assignment_view", args=[target.assignment.pk])
        )
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "جرد المستودع")
        self.assertContains(page, self.staff.name)
        # الرابط المعروض للمشاركة هو الصفحة نفسها مطلقاً.
        self.assertContains(
            page,
            f"http://testserver{reverse('reports:assignment_view', args=[target.assignment.pk])}",
        )

        printed = self.client.get(
            reverse("reports:assignment_print", args=[target.assignment.pk])
        )
        self.assertEqual(printed.status_code, 200)
        self.assertContains(printed, "جرد المستودع")
        self.assertContains(printed, self.staff.name)

    def test_the_assignee_opens_the_assignment_they_were_given(self):
        target = self._target()
        self._enter(self.staff)

        response = self.client.get(
            reverse("reports:assignment_view", args=[target.assignment.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'href="{reverse("reports:my_assignments")}"')
        self.assertNotContains(
            response, f'href="{reverse("reports:assignment_board")}"'
        )

    def test_a_stranger_cannot_open_the_assignment_or_print_it(self):
        target = self._target()
        other = _user("زميل بلا علاقة", "0500030030")
        SchoolMembership.objects.create(
            school=self.school, teacher=other, role_type=SchoolMembership.RoleType.TEACHER
        )
        self._enter(other)

        self.assertEqual(
            self.client.get(
                reverse("reports:assignment_view", args=[target.assignment.pk])
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                reverse("reports:assignment_print", args=[target.assignment.pk])
            ).status_code,
            404,
        )

    def test_an_assignment_of_another_school_reads_as_missing(self):
        elsewhere = _school("مدرسة أخرى", "asg-other")
        stranger = _user("غريب", "0500030031")
        far_assignment = Assignment.objects.create(
            scope=Assignment.Scope.SCHOOL,
            school=elsewhere,
            issuer=stranger,
            title="تكليف مدرسة أخرى",
            due_at=timezone.now() + timedelta(days=3),
        )
        AssignmentTarget.objects.create(
            assignment=far_assignment, assignee=stranger, school=elsewhere
        )
        self._enter(self.manager)

        response = self.client.get(
            reverse("reports:assignment_view", args=[far_assignment.pk])
        )
        self.assertEqual(response.status_code, 404)

    def test_the_assignee_updates_progress_through_the_screen(self):
        target = self._target()
        self._enter(self.staff)

        self.client.post(
            reverse("reports:assignment_target_action", args=[target.pk]),
            {"target_action": "progress", "percent": 60, "note": "أُنجز الجرد الجزئي"},
        )
        target.refresh_from_db()
        self.assertEqual(target.progress_percent, 60)

    def test_the_manager_approves_through_the_screen(self):
        target = self._target()
        submit(target, self.staff)
        self._enter(self.manager)

        self.client.post(
            reverse("reports:assignment_approval_action", args=[target.pk]),
            {"approval_action": "approve"},
        )
        target.refresh_from_db()
        self.assertEqual(target.approval_state, ApprovalState.APPROVED)

    def test_cancelling_from_the_board_marks_it_cancelled(self):
        assignment = self._assignment()
        self._target(assignment=assignment)
        self._enter(self.manager)

        self.client.post(
            reverse("reports:assignment_cancel", args=[assignment.pk]),
            {"reason": "تغيّرت الأولويات"},
        )
        assignment.refresh_from_db()
        self.assertTrue(assignment.is_cancelled)

    def test_a_cancelled_assignment_moves_to_the_assignees_archive(self):
        target = self._target()
        target.assignment.cancel(by=self.manager, reason="تغيّرت الأولويات")
        self._enter(self.staff)

        response = self.client.get(reverse("reports:my_assignments"))

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(target, response.context["open_targets"])
        self.assertIn(target, response.context["cancelled_targets"])
        self.assertContains(response, "الملغاة")
        self.assertContains(response, "تغيّرت الأولويات")

    def test_a_cancelled_assignment_detail_has_no_execution_actions(self):
        target = self._target()
        target.assignment.cancel(by=self.manager, reason="لم تعد المهمة مطلوبة")
        self._enter(self.staff)

        response = self.client.get(
            reverse("reports:assignment_detail", args=[target.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["actions"], [])
        self.assertContains(response, "هذا التكليف ملغى")
        self.assertContains(response, "لم تعد المهمة مطلوبة")
        self.assertNotContains(response, "رفع الشاهد")

    def test_a_cancelled_assignment_rejects_a_forged_approval_post(self):
        target = self._target()
        target.assignment.cancel(by=self.manager, reason="انتهت الحاجة")
        self._enter(self.staff)

        response = self.client.post(
            reverse("reports:assignment_approval_action", args=[target.pk]),
            {"approval_action": "submit"},
            follow=True,
        )

        target.refresh_from_db()
        self.assertEqual(target.approval_state, ApprovalState.DRAFT)
        self.assertContains(response, "هذا الإجراء غير متاح على هذا التكليف الآن")


@override_settings(ALLOWED_HOSTS=["testserver"])
class AssigneeScopeTests(AssignmentBase):
    """من يجوز تكليفه.

    نطاق التكليف داخل المدرسة هو المدرسة لا القسم: يُكلَّف المعلم والوكيل
    والموظف الإداري سواء. وكان النطاق محصوراً بأقسام المكلِّف، فترتّب عليه منعان
    بلا مقابل يثبّتهما هذان الاختباران الأولان صراحةً.
    """

    def _deputy(self, *, departments=()):
        deputy = _user("الوكيل المُصدِر", "0500030030")
        membership = SchoolMembership.objects.create(
            school=self.school, teacher=deputy, role_type=SchoolMembership.RoleType.DEPUTY
        )
        scope = StaffScope.objects.create(
            membership=membership, capabilities=[caps.ASSIGN_TASKS]
        )
        for department in departments:
            scope.departments.add(department)
        return deputy

    def _offered(self, issuer):
        form = SchoolAssignmentForm(school=self.school, issuer=issuer)
        return list(form.fields["assignees"].queryset)

    def test_a_deputy_without_departments_still_has_someone_to_assign(self):
        """نطاقٌ لم تُسنَد إليه أقسام بعد كان لا يكلّف أحداً على الإطلاق."""
        self.assertIn(self.staff, self._offered(self._deputy()))

    def test_an_employee_outside_every_department_is_offered(self):
        """الموظف الإداري بلا قسم لم يكن قابلاً للتكليف أصلاً."""
        loner = _user("إداري بلا قسم", "0500030031")
        SchoolMembership.objects.create(
            school=self.school,
            teacher=loner,
            role_type=SchoolMembership.RoleType.ADMIN_STAFF,
        )
        self.assertIn(loner, self._offered(self._deputy()))

    def test_every_staff_role_is_offered(self):
        teacher = _user("معلم", "0500030032")
        SchoolMembership.objects.create(
            school=self.school, teacher=teacher, role_type=SchoolMembership.RoleType.TEACHER
        )
        deputy = self._deputy()

        self.assertEqual(
            {person.pk for person in self._offered(self.manager)},
            {teacher.pk, deputy.pk, self.staff.pk},
        )

    def test_the_issuer_is_never_offered_to_themselves(self):
        """المكلِّف يعتمد التنفيذ، فتكليفه لنفسه عملٌ لا مخرج له."""
        deputy = self._deputy()
        self.assertNotIn(deputy, self._offered(deputy))

    def test_a_member_of_another_school_is_not_offered(self):
        elsewhere = _school("مدرسة أخرى", "asg-other")
        stranger = _user("غريب عن المدرسة", "0500030033")
        SchoolMembership.objects.create(
            school=elsewhere, teacher=stranger, role_type=SchoolMembership.RoleType.TEACHER
        )
        self.assertNotIn(stranger, self._offered(self.manager))

    def test_each_option_carries_its_role_and_departments(self):
        form = SchoolAssignmentForm(school=self.school, issuer=self.manager)
        option = next(
            item for item in form.assignee_options() if item["value"] == self.staff.pk
        )

        self.assertEqual(option["role"], SchoolMembership.RoleType.ADMIN_STAFF)
        self.assertEqual(option["departments"], ["الشؤون الإدارية"])

    def test_a_deputy_who_also_teaches_reads_as_a_deputy(self):
        """الرجل الواحد يحمل الدورين عمداً، ويُعرَّف بالأخصّ منهما."""
        deputy = self._deputy()
        SchoolMembership.objects.create(
            school=self.school, teacher=deputy, role_type=SchoolMembership.RoleType.TEACHER
        )

        form = SchoolAssignmentForm(school=self.school, issuer=self.manager)
        option = next(
            item for item in form.assignee_options() if item["value"] == deputy.pk
        )
        self.assertEqual(option["role"], SchoolMembership.RoleType.DEPUTY)

    def test_a_deputy_may_issue_beyond_their_departments(self):
        far_department = Department.objects.create(
            school=self.school, name="قسم بعيد", slug="far-dept"
        )
        deputy = self._deputy(departments=[far_department])
        teacher = _user("معلم خارج نطاقه", "0500030034")
        SchoolMembership.objects.create(
            school=self.school, teacher=teacher, role_type=SchoolMembership.RoleType.TEACHER
        )

        self.client.force_login(deputy)
        session = self.client.session
        session["active_school_id"] = self.school.pk
        session.save()

        due = timezone.localtime() + timedelta(days=3)
        response = self.client.post(
            reverse("reports:assignment_create"),
            {
                "title": "حصر العهد",
                "priority": Assignment.Priority.NORMAL,
                "due_at": due.strftime("%Y-%m-%dT%H:%M"),
                "min_evidence_count": 1,
                "assignees": [teacher.pk],
            },
        )

        self.assertEqual(response.status_code, 302)
        assignment = Assignment.objects.get(title="حصر العهد")
        self.assertEqual(
            [target.assignee_id for target in assignment.targets.all()], [teacher.pk]
        )

    def test_a_department_outside_the_scope_is_not_even_offered(self):
        """عرضُ خيارٍ يُرفض بعد ملء النموذج كله عيبٌ في العرض لا حماية."""
        mine = Department.objects.create(school=self.school, name="قسمي", slug="mine")
        deputy = self._deputy(departments=[mine])

        form = SchoolAssignmentForm(school=self.school, issuer=deputy)
        self.assertEqual(
            [department.pk for department in form.fields["department"].queryset],
            [mine.pk],
        )

    def test_a_crafted_department_outside_scope_is_rejected(self):
        mine = Department.objects.create(
            school=self.school, name="قسم الوكيل", slug="deputy-scope"
        )
        outside = Department.objects.create(
            school=self.school, name="قسم خارج النطاق", slug="outside-scope"
        )
        deputy = self._deputy(departments=[mine])
        self.client.force_login(deputy)
        session = self.client.session
        session["active_school_id"] = self.school.pk
        session.save()

        response = self.client.post(
            reverse("reports:assignment_create"),
            {
                "title": "قسم مصاغ يدويًا",
                "department": outside.pk,
                "priority": Assignment.Priority.NORMAL,
                "due_at": (timezone.localtime() + timedelta(days=3)).strftime(
                    "%Y-%m-%dT%H:%M"
                ),
                "assignees": [self.staff.pk],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Assignment.objects.filter(title="قسم مصاغ يدويًا").exists())
