# -*- coding: utf-8 -*-
"""Active-school tenant boundary for school Assignment objects."""
from __future__ import annotations

from datetime import timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from reports import capabilities as caps
from reports.model_parts.approvals import ApprovalState
from reports.models import (
    Assignment,
    AssignmentTarget,
    Department,
    School,
    SchoolMembership,
    SchoolSubscription,
    StaffScope,
    SubscriptionPlan,
    Teacher,
)
from reports.services_approval import submit


def _user(name: str, phone: str) -> Teacher:
    return Teacher.objects.create_user(
        phone=phone,
        name=name,
        password="Passw0rd!123",
    )


def _school(name: str, code: str) -> School:
    plan = SubscriptionPlan.objects.create(
        name=f"باقة {code}",
        price=0,
        days_duration=365,
        max_teachers=0,
    )
    school = School.objects.create(name=name, code=code)
    SchoolSubscription.objects.create(school=school, plan=plan)
    return school


@override_settings(ALLOWED_HOSTS=["testserver"])
class AssignmentActiveSchoolScopeTests(TestCase):
    """Ownership grants authority only inside the active school context."""

    def setUp(self):
        self.school_a = _school("مدرسة أ", "assignment-scope-a")
        self.school_b = _school("مدرسة ب", "assignment-scope-b")
        self.department_b = Department.objects.create(
            school=self.school_b,
            name="قسم مدرسة ب",
            slug="assignment-scope-b-department",
        )

        self.issuer = _user("مُصدر التكليف", "0500091001")
        self.assignee = _user("المكلّف", "0500091002")
        self.manager = _user("مدير المدرستين", "0500091003")
        self.reviewer = _user("مراجع القسم", "0500091004")

        for user in (self.issuer, self.assignee, self.manager, self.reviewer):
            SchoolMembership.objects.create(
                school=self.school_a,
                teacher=user,
                role_type=(
                    SchoolMembership.RoleType.MANAGER
                    if user == self.manager
                    else SchoolMembership.RoleType.TEACHER
                ),
            )
            membership_b = SchoolMembership.objects.create(
                school=self.school_b,
                teacher=user,
                role_type=(
                    SchoolMembership.RoleType.MANAGER
                    if user == self.manager
                    else SchoolMembership.RoleType.DEPUTY
                    if user == self.reviewer
                    else SchoolMembership.RoleType.TEACHER
                ),
            )
            if user == self.reviewer:
                scope = StaffScope.objects.create(
                    membership=membership_b,
                    capabilities=[caps.REVIEW_REPORTS, caps.RECOMMEND_APPROVAL],
                )
                scope.departments.add(self.department_b)

        self.assignment = Assignment.objects.create(
            scope=Assignment.Scope.SCHOOL,
            school=self.school_b,
            issuer=self.issuer,
            title="تكليف مدرسة ب السري",
            department=self.department_b,
            due_at=timezone.now() + timedelta(days=5),
        )
        self.target = AssignmentTarget.objects.create(
            assignment=self.assignment,
            assignee=self.assignee,
            school=self.school_b,
        )

    def _enter(self, user: Teacher, school: School) -> None:
        self.client.force_login(user)
        session = self.client.session
        session["active_school_id"] = school.pk
        session.save()

    def _get(self, route: str, *, user: Teacher, school: School):
        self._enter(user, school)
        return self.client.get(reverse(f"reports:{route}", args=[self._route_pk(route)]))

    def _route_pk(self, route: str) -> int:
        if route in {"assignment_view", "assignment_print"}:
            return self.assignment.pk
        return self.target.pk

    def test_issuer_cannot_open_overview_or_print_outside_active_school(self):
        for route in ("assignment_view", "assignment_print"):
            with self.subTest(route=route):
                response = self._get(route, user=self.issuer, school=self.school_a)
                self.assertEqual(response.status_code, 404)
                self.assertNotContains(
                    response,
                    self.assignment.title,
                    status_code=404,
                )

    def test_assignee_cannot_open_overview_or_target_outside_active_school(self):
        for route in ("assignment_view", "assignment_detail"):
            with self.subTest(route=route):
                response = self._get(route, user=self.assignee, school=self.school_a)
                self.assertEqual(response.status_code, 404)

    def test_target_school_field_cannot_override_the_assignment_school(self):
        AssignmentTarget.objects.filter(pk=self.target.pk).update(school=self.school_a)

        response = self._get(
            "assignment_detail",
            user=self.assignee,
            school=self.school_a,
        )

        self.assertEqual(response.status_code, 404)

    def test_manager_cannot_open_school_b_objects_while_school_a_is_active(self):
        for route in ("assignment_view", "assignment_print", "assignment_detail"):
            with self.subTest(route=route):
                response = self._get(route, user=self.manager, school=self.school_a)
                self.assertEqual(response.status_code, 404)

    def test_progress_and_execution_actions_cannot_mutate_outside_active_school(self):
        self._enter(self.assignee, self.school_a)
        original = {
            "accepted_at": self.target.accepted_at,
            "clarification_note": self.target.clarification_note,
            "progress_percent": self.target.progress_percent,
            "progress_note": self.target.progress_note,
            "evidence_count": self.target.evidence.count(),
        }
        posts = (
            {"target_action": "accept"},
            {"target_action": "clarify", "note": "أحتاج توضيحًا"},
            {"target_action": "progress", "percent": 75, "note": "تم التنفيذ"},
            {
                "target_action": "add_evidence",
                "file": SimpleUploadedFile("proof.txt", b"proof", content_type="text/plain"),
            },
            {"target_action": "remove_evidence", "evidence_id": 999999},
        )

        for payload in posts:
            with self.subTest(action=payload["target_action"]):
                response = self.client.post(
                    reverse("reports:assignment_target_action", args=[self.target.pk]),
                    payload,
                )
                self.assertEqual(response.status_code, 404)

        self.target.refresh_from_db()
        self.assertEqual(self.target.accepted_at, original["accepted_at"])
        self.assertEqual(self.target.clarification_note, original["clarification_note"])
        self.assertEqual(self.target.progress_percent, original["progress_percent"])
        self.assertEqual(self.target.progress_note, original["progress_note"])
        self.assertEqual(self.target.evidence.count(), original["evidence_count"])

    def test_submit_cannot_transition_outside_active_school(self):
        self._enter(self.assignee, self.school_a)

        response = self.client.post(
            reverse("reports:assignment_approval_action", args=[self.target.pk]),
            {"approval_action": "submit", "approval_state": ApprovalState.APPROVED},
        )

        self.assertEqual(response.status_code, 404)
        self.target.refresh_from_db()
        self.assertEqual(self.target.approval_state, ApprovalState.DRAFT)

    def test_issuer_cannot_approve_outside_active_school(self):
        submit(self.target, self.assignee)
        self._enter(self.issuer, self.school_a)

        response = self.client.post(
            reverse("reports:assignment_approval_action", args=[self.target.pk]),
            {"approval_action": "approve"},
        )

        self.assertEqual(response.status_code, 404)
        self.target.refresh_from_db()
        self.assertEqual(self.target.approval_state, ApprovalState.SUBMITTED)

    def test_reviewer_cannot_request_info_or_recommend_outside_active_school(self):
        submit(self.target, self.assignee)
        self._enter(self.reviewer, self.school_a)

        for action in ("request_info", "recommend"):
            with self.subTest(action=action):
                response = self.client.post(
                    reverse("reports:assignment_approval_action", args=[self.target.pk]),
                    {"approval_action": action, "note": "ملاحظة مراجعة"},
                )
                self.assertEqual(response.status_code, 404)

        self.target.refresh_from_db()
        self.assertEqual(self.target.approval_state, ApprovalState.SUBMITTED)

    def test_switching_to_school_b_enables_the_same_objects_for_issuer(self):
        self._enter(self.issuer, self.school_a)
        denied = self.client.get(
            reverse("reports:assignment_view", args=[self.assignment.pk])
        )

        self._enter(self.issuer, self.school_b)
        allowed = self.client.get(
            reverse("reports:assignment_view", args=[self.assignment.pk])
        )

        self.assertEqual(denied.status_code, 404)
        self.assertEqual(allowed.status_code, 200)
        self.assertContains(allowed, self.assignment.title)

    def test_switching_to_school_b_enables_detail_and_progress_for_assignee(self):
        self._enter(self.assignee, self.school_a)
        denied = self.client.get(
            reverse("reports:assignment_detail", args=[self.target.pk])
        )

        self._enter(self.assignee, self.school_b)
        allowed = self.client.get(
            reverse("reports:assignment_detail", args=[self.target.pk])
        )
        mutation = self.client.post(
            reverse("reports:assignment_target_action", args=[self.target.pk]),
            {"target_action": "progress", "percent": 40, "note": "ضمن مدرسة ب"},
        )

        self.assertEqual(denied.status_code, 404)
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(mutation.status_code, 302)
        self.target.refresh_from_db()
        self.assertEqual(self.target.progress_percent, 40)

    def test_switching_to_school_b_enables_manager_approval(self):
        submit(self.target, self.assignee)
        self._enter(self.manager, self.school_a)
        denied = self.client.post(
            reverse("reports:assignment_approval_action", args=[self.target.pk]),
            {"approval_action": "approve"},
        )

        self._enter(self.manager, self.school_b)
        allowed = self.client.post(
            reverse("reports:assignment_approval_action", args=[self.target.pk]),
            {"approval_action": "approve"},
        )

        self.assertEqual(denied.status_code, 404)
        self.assertEqual(allowed.status_code, 302)
        self.target.refresh_from_db()
        self.assertEqual(self.target.approval_state, ApprovalState.APPROVED)

    def test_department_reviewer_rules_still_apply_inside_school_b(self):
        submit(self.target, self.assignee)
        self._enter(self.reviewer, self.school_b)

        response = self.client.post(
            reverse("reports:assignment_approval_action", args=[self.target.pk]),
            {"approval_action": "recommend", "note": "مستوفٍ"},
        )

        self.assertEqual(response.status_code, 302)
        self.target.refresh_from_db()
        self.assertEqual(self.target.approval_state, ApprovalState.RECOMMENDED)
