# -*- coding: utf-8 -*-
"""تطابق عداد وثائق لوحة النطاق مع الرؤية الفعلية في الأرشيف."""
from __future__ import annotations

from datetime import timedelta

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from reports import capabilities as caps
from reports.model_parts.approvals import ApprovalState, PENDING_REVIEW_STATES
from reports.models import (
    Delegation,
    Department,
    Document,
    School,
    SchoolMembership,
    SchoolSubscription,
    StaffScope,
    SubscriptionPlan,
    Teacher,
)
from reports.services_documents import visible_documents


@override_settings(ALLOWED_HOSTS=["testserver"])
class StaffDashboardDocumentScopeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.plan = SubscriptionPlan.objects.create(
            name="خطة اختبار عداد الوثائق",
            price=0,
            days_duration=365,
            max_teachers=20,
        )
        cls.school_a = cls._school("مدرسة النطاق أ", "staff-doc-scope-a")
        cls.school_b = cls._school("مدرسة النطاق ب", "staff-doc-scope-b")

        cls.manager = cls._user("مدير المدرسة", "509880001")
        SchoolMembership.objects.create(
            school=cls.school_a,
            teacher=cls.manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )

        cls.staff = cls._user("وكيل النطاق", "509880002")
        cls.membership_a = SchoolMembership.objects.create(
            school=cls.school_a,
            teacher=cls.staff,
            role_type=SchoolMembership.RoleType.DEPUTY,
        )

        cls.owner = cls._user("رافع الوثائق", "509880003")
        SchoolMembership.objects.create(
            school=cls.school_a,
            teacher=cls.owner,
            role_type=SchoolMembership.RoleType.ADMIN_STAFF,
        )

        cls.department_a = Department.objects.create(
            school=cls.school_a,
            name="القسم أ",
            slug="staff-doc-department-a",
        )
        cls.department_b = Department.objects.create(
            school=cls.school_a,
            name="القسم ب",
            slug="staff-doc-department-b",
        )
        cls.department_c = Department.objects.create(
            school=cls.school_a,
            name="القسم ج",
            slug="staff-doc-department-c",
        )

    @classmethod
    def _school(cls, name: str, code: str) -> School:
        school = School.objects.create(
            name=name,
            code=code,
            current_academic_year="1448-1449",
        )
        SchoolSubscription.objects.create(school=school, plan=cls.plan)
        return school

    @staticmethod
    def _user(name: str, phone: str) -> Teacher:
        return Teacher.objects.create_user(phone=phone, name=name, password="x")

    def _scope(self, *departments: Department, capabilities=None) -> StaffScope:
        StaffScope.objects.filter(membership=self.membership_a).delete()
        scope = StaffScope.objects.create(
            membership=self.membership_a,
            domain=StaffScope.Domain.ACADEMIC,
            capabilities=capabilities
            or [caps.VIEW_SCHOOL_DASHBOARD, caps.ARCHIVE_DOCUMENTS],
        )
        scope.departments.set(departments)
        return scope

    def _document(
        self,
        title: str,
        department: Department,
        *,
        school: School | None = None,
        owner: Teacher | None = None,
    ) -> Document:
        school = school or department.school
        owner = owner or self.owner
        return Document.objects.create(
            school=school,
            owner=owner,
            uploaded_by=owner,
            title=title,
            academic_year="1448-1449",
            department=department,
            kind=Document.Kind.MINUTES,
            file=f"documents/{school.code}/{title}.pdf",
            approval_state=ApprovalState.SUBMITTED,
        )

    def _enter(self, school: School, user: Teacher | None = None) -> None:
        self.client.force_login(user or self.staff)
        session = self.client.session
        session["active_school_id"] = school.pk
        session.save()

    def _dashboard_document_count(self) -> int:
        response = self.client.get(reverse("reports:staff_dashboard"))
        self.assertEqual(response.status_code, 200)
        cards = {card["key"]: card for card in response.context["cards"]}
        return cards["documents"]["value"]

    def _visible_pending_count(self, user: Teacher, school: School) -> int:
        return visible_documents(user, school).filter(
            approval_state__in=PENDING_REVIEW_STATES
        ).count()

    def test_same_school_out_of_scope_document_does_not_change_count(self):
        self._scope(self.department_a)
        self._document("وثيقة ضمن النطاق", self.department_a)
        self._enter(self.school_a)

        self.assertEqual(self._dashboard_document_count(), 1)
        self._document("وثيقة خارج النطاق", self.department_b)

        self.assertEqual(self._visible_pending_count(self.staff, self.school_a), 1)
        self.assertEqual(self._dashboard_document_count(), 1)

    def test_cross_school_document_is_not_counted(self):
        self._scope(self.department_a)
        self._document("وثيقة المدرسة أ", self.department_a)
        department_b_school = Department.objects.create(
            school=self.school_b,
            name="قسم المدرسة ب",
            slug="staff-doc-school-b-department",
        )
        owner_b = self._user("رافع المدرسة ب", "509880004")
        SchoolMembership.objects.create(
            school=self.school_b,
            teacher=owner_b,
            role_type=SchoolMembership.RoleType.ADMIN_STAFF,
        )
        self._document(
            "وثيقة المدرسة ب",
            department_b_school,
            school=self.school_b,
            owner=owner_b,
        )
        self._enter(self.school_a)

        self.assertEqual(self._dashboard_document_count(), 1)
        self.assertEqual(self._visible_pending_count(self.staff, self.school_a), 1)

    def test_empty_department_scope_does_not_fall_back_to_school_wide(self):
        self._scope()
        self._document("وثيقة بلا نطاق مرئي", self.department_a)
        self._enter(self.school_a)

        self.assertEqual(self._visible_pending_count(self.staff, self.school_a), 0)
        self.assertEqual(self._dashboard_document_count(), 0)

    def test_multiple_departments_are_counted_once_within_scope(self):
        self._scope(self.department_a, self.department_c)
        self._document("وثيقة أ", self.department_a)
        self._document("وثيقة ب خارج النطاق", self.department_b)
        self._document("وثيقة ج", self.department_c)
        self._enter(self.school_a)

        self.assertEqual(self._visible_pending_count(self.staff, self.school_a), 2)
        self.assertEqual(self._dashboard_document_count(), 2)

    def test_delegation_grants_capability_without_widening_department_scope(self):
        self._scope(self.department_a, capabilities=[])
        now = timezone.now()
        Delegation.objects.create(
            school=self.school_a,
            delegator=self.manager,
            delegate=self.staff,
            capabilities=[caps.VIEW_SCHOOL_DASHBOARD, caps.ARCHIVE_DOCUMENTS],
            starts_at=now - timedelta(hours=1),
            ends_at=now + timedelta(days=1),
        )
        self._document("وثيقة تفويض مرئية", self.department_a)
        self._document("وثيقة لا يوسعها التفويض", self.department_b)
        self._enter(self.school_a)

        self.assertEqual(self._visible_pending_count(self.staff, self.school_a), 1)
        self.assertEqual(self._dashboard_document_count(), 1)

    def test_active_school_selects_the_matching_scope_and_documents(self):
        department_school_b = Department.objects.create(
            school=self.school_b,
            name="قسم النطاق ب",
            slug="staff-doc-active-school-b",
        )
        membership_b = SchoolMembership.objects.create(
            school=self.school_b,
            teacher=self.staff,
            role_type=SchoolMembership.RoleType.DEPUTY,
        )
        scope_a = self._scope(self.department_a)
        scope_b = StaffScope.objects.create(
            membership=membership_b,
            domain=StaffScope.Domain.ACADEMIC,
            capabilities=[caps.VIEW_SCHOOL_DASHBOARD, caps.ARCHIVE_DOCUMENTS],
        )
        scope_b.departments.add(department_school_b)
        owner_b = self._user("رافع متعدد المدارس", "509880005")
        SchoolMembership.objects.create(
            school=self.school_b,
            teacher=owner_b,
            role_type=SchoolMembership.RoleType.ADMIN_STAFF,
        )

        self._document("وثيقة أ الوحيدة", self.department_a)
        self._document("وثيقة أ خارج النطاق", self.department_b)
        self._document(
            "وثيقة ب الأولى",
            department_school_b,
            school=self.school_b,
            owner=owner_b,
        )
        self._document(
            "وثيقة ب الثانية",
            department_school_b,
            school=self.school_b,
            owner=owner_b,
        )

        self.assertEqual(set(scope_a.departments.values_list("pk", flat=True)), {self.department_a.pk})
        self._enter(self.school_a)
        self.assertEqual(self._dashboard_document_count(), 1)
        self._enter(self.school_b)
        self.assertEqual(self._dashboard_document_count(), 2)
