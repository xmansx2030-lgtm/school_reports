from datetime import timedelta

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from reports import capabilities as caps
from reports.models import (
    AuditLog,
    Department,
    DepartmentMembership,
    School,
    SchoolMembership,
    SchoolSubscription,
    StaffScope,
    SubscriptionPlan,
    Teacher,
)


def _user(name: str, phone: str, *, superuser: bool = False) -> Teacher:
    return Teacher.objects.create_user(
        name=name,
        phone=phone,
        email=f"{phone}@example.com",
        password="StrongPass123!",
        is_staff=superuser,
        is_superuser=superuser,
    )


def _school(name: str, code: str, plan: SubscriptionPlan) -> School:
    school = School.objects.create(name=name, code=code, is_active=True)
    SchoolSubscription.objects.create(
        school=school,
        plan=plan,
        start_date=timezone.localdate(),
        end_date=timezone.localdate() + timedelta(days=30),
        is_active=True,
    )
    return school


@override_settings(ALLOWED_HOSTS=["testserver"])
class AuditSearchExportTests(TestCase):
    def setUp(self):
        self.plan = SubscriptionPlan.objects.create(
            name="خطة سجل العمليات",
            price=0,
            days_duration=30,
            max_teachers=20,
            is_active=True,
        )
        self.school = _school("مدرسة السجل", "audit-school", self.plan)
        self.other_school = _school("مدرسة أخرى", "audit-other", self.plan)
        self.manager = _user("مدير السجل", "0509100001")
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )
        self.employee = _user("موظف السجل", "0509100002")
        employee_membership = SchoolMembership.objects.create(
            school=self.school,
            teacher=self.employee,
            role_type=SchoolMembership.RoleType.ADMIN_STAFF,
        )
        self.teacher = _user("معلم داخل النطاق", "0509100003")
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.teacher,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        self.outsider = _user("معلم خارج النطاق", "0509100004")
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.outsider,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        self.department = Department.objects.create(
            school=self.school,
            name="قسم السجل",
            slug="audit-department",
        )
        DepartmentMembership.objects.create(
            department=self.department,
            teacher=self.teacher,
            role_type=DepartmentMembership.TEACHER,
        )
        scope = StaffScope.objects.create(
            membership=employee_membership,
            capabilities=[caps.VIEW_AUDIT_LOG, caps.HANDLE_REQUESTS],
            granted_by=self.manager,
        )
        scope.departments.add(self.department)

        self.target_log = AuditLog.objects.create(
            school=self.school,
            teacher=self.teacher,
            action=AuditLog.Action.CREATE,
            model_name="Report",
            object_id=11,
            object_repr="تقرير الجودة المستهدف",
        )
        AuditLog.objects.create(
            school=self.school,
            teacher=self.outsider,
            action=AuditLog.Action.DELETE,
            model_name="Ticket",
            object_id=12,
            object_repr="طلب خارج النطاق",
        )
        other_user = _user("مستخدم مدرسة أخرى", "0509100005")
        SchoolMembership.objects.create(
            school=self.other_school,
            teacher=other_user,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        AuditLog.objects.create(
            school=self.other_school,
            teacher=other_user,
            action=AuditLog.Action.UPDATE,
            model_name="Report",
            object_id=13,
            object_repr="بيانات مدرسة أخرى",
        )

    def _enter(self, user, school=None):
        self.client.force_login(user)
        if school is not None:
            session = self.client.session
            session["active_school_id"] = school.pk
            session.save()

    @staticmethod
    def _stream_text(response) -> str:
        return b"".join(response.streaming_content).decode("utf-8")

    def test_manager_can_search_and_filter_by_model(self):
        self._enter(self.manager, self.school)

        response = self.client.get(
            reverse("reports:school_audit_logs"),
            {"q": "الجودة", "model": "Report"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual([row.pk for row in response.context["logs"]], [self.target_log.pk])
        self.assertContains(response, "بحث")
        self.assertContains(response, "تصدير CSV")

    def test_scoped_employee_export_cannot_escape_their_departments(self):
        self._enter(self.employee, self.school)

        response = self.client.get(
            reverse("reports:school_audit_logs"),
            {"export": "csv"},
        )
        body = self._stream_text(response)

        self.assertEqual(response.status_code, 200)
        self.assertIn("تقرير الجودة المستهدف", body)
        self.assertNotIn("طلب خارج النطاق", body)
        self.assertNotIn("بيانات مدرسة أخرى", body)

    def test_platform_export_includes_school_name_and_respects_search(self):
        admin = _user("مدير المنصة", "0509100006", superuser=True)
        self._enter(admin)

        response = self.client.get(
            reverse("reports:platform_audit_logs"),
            {"q": "مدرسة أخرى", "export": "csv"},
        )
        body = self._stream_text(response)

        self.assertEqual(response.status_code, 200)
        self.assertIn("مدرسة أخرى", body)
        self.assertIn("بيانات مدرسة أخرى", body)
        self.assertNotIn("تقرير الجودة المستهدف", body)

    def test_scope_screen_exposes_plain_can_and_cannot_summary(self):
        membership = SchoolMembership.objects.get(
            school=self.school,
            teacher=self.employee,
            role_type=SchoolMembership.RoleType.ADMIN_STAFF,
        )
        self._enter(self.manager, self.school)

        response = self.client.get(reverse("reports:staff_role_scope", args=[membership.pk]))

        self.assertEqual(response.status_code, 200)
        summary = {item["code"]: item["allowed"] for item in response.context["capability_summary"]}
        self.assertTrue(summary[caps.VIEW_AUDIT_LOG])
        self.assertTrue(summary[caps.HANDLE_REQUESTS])
        self.assertFalse(summary[caps.DRAFT_CIRCULARS])
        self.assertContains(response, "يستطيع")
        self.assertContains(response, "لا يستطيع")
        self.assertContains(response, "لا يعتمد الأعمال اعتمادًا نهائيًا")
        self.assertContains(response, 'id="scopeDeniedList"')
        self.assertContains(response, "hidden")
