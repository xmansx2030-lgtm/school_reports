from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from django.db import connection
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from reports import capabilities as caps
from reports.models import (
    AuditLog,
    Delegation,
    Department,
    School,
    SchoolMembership,
    SchoolSubscription,
    StaffScope,
    SubscriptionPlan,
    Teacher,
)


ROOT = Path(__file__).resolve().parents[2]
ROLES_TEMPLATE = ROOT / "reports" / "templates" / "reports" / "staff_roles.html"
SCOPE_TEMPLATE = ROOT / "reports" / "templates" / "reports" / "staff_role_scope.html"
STYLESHEET = ROOT / "static" / "css" / "staff-access.css"
SCRIPT = ROOT / "static" / "js" / "staff-access.js"


class StaffAccessSourceTests(SimpleTestCase):
    def test_surfaces_use_shared_v1_structure_and_owned_assets(self):
        roles = ROLES_TEMPLATE.read_text(encoding="utf-8")
        scope = SCOPE_TEMPLATE.read_text(encoding="utf-8")

        for source in (roles, scope):
            self.assertIn("css/staff-access.css", source)
            self.assertIn("js/staff-access.js", source)
            self.assertIn("twq-page", source)
            self.assertIn("twq-page-header", source)
            self.assertIn("twq-section", source)
            self.assertNotIn("<style", source)
            self.assertNotRegex(source, r"\sstyle\s*=")
            self.assertNotRegex(source, r"<script(?![^>]*\bsrc=)")

    def test_security_information_architecture_is_explicit(self):
        roles = ROLES_TEMPLATE.read_text(encoding="utf-8")
        scope = SCOPE_TEMPLATE.read_text(encoding="utf-8")

        self.assertLess(roles.index("الدور الصلاحي"), roles.index("الصلاحيات الأصلية"))
        self.assertLess(roles.index("الصلاحيات الأصلية"), roles.index("نطاق الأقسام"))
        self.assertIn("المسمى الوظيفي", roles)
        self.assertIn("لا يضيف التفويض أي قسم", roles)
        self.assertIn("ترك الأقسام فارغة يعني", scope)
        self.assertIn("fieldset", roles)
        self.assertIn("fieldset", scope)

    def test_styles_use_semantic_tokens_and_logical_properties(self):
        source = STYLESHEET.read_text(encoding="utf-8")

        self.assertIn("var(--twq-", source)
        self.assertNotRegex(source, r"#[0-9a-fA-F]{3,8}\b")
        self.assertNotRegex(source, r"(?i)\brgba?\(")
        self.assertNotRegex(
            source,
            r"(?m)^\s*(?:(?:margin|padding|border|inset)-(?:left|right)|left|right)\s*:",
        )
        self.assertNotIn("transition: all", source)

    def test_script_is_presentation_only_and_has_no_direct_style_writes(self):
        source = SCRIPT.read_text(encoding="utf-8")

        self.assertNotRegex(source, r"\.style(?:\.|\[|\s*=)")
        self.assertNotIn("fetch(", source)
        self.assertNotIn("XMLHttpRequest", source)
        self.assertIn('event.key === "ArrowLeft"', source)
        self.assertIn("data-error-summary", source)


@override_settings(ALLOWED_HOSTS=["testserver"], RATELIMIT_ENABLE=False)
class StaffAccessPilotTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.plan = SubscriptionPlan.objects.create(
            name="خطة إدارة الوصول", price=0, days_duration=365, max_teachers=0
        )
        cls.school_a = School.objects.create(name="مدرسة الوصول أ", code="access-a")
        cls.school_b = School.objects.create(name="مدرسة الوصول ب", code="access-b")
        SchoolSubscription.objects.create(school=cls.school_a, plan=cls.plan)
        SchoolSubscription.objects.create(school=cls.school_b, plan=cls.plan)
        cls.manager_a = Teacher.objects.create_user(
            phone="0557000001", name="مدير الوصول أ", password="Passw0rd!123"
        )
        cls.manager_b = Teacher.objects.create_user(
            phone="0557000002", name="مدير الوصول ب", password="Passw0rd!123"
        )
        SchoolMembership.objects.create(
            school=cls.school_a,
            teacher=cls.manager_a,
            role_type=SchoolMembership.RoleType.MANAGER,
        )
        SchoolMembership.objects.create(
            school=cls.school_b,
            teacher=cls.manager_b,
            role_type=SchoolMembership.RoleType.MANAGER,
        )
        cls.staff = Teacher.objects.create_user(
            phone="0557000003", name="منسوب متعدد المدارس", password="Passw0rd!123"
        )
        cls.membership_a = SchoolMembership.objects.create(
            school=cls.school_a,
            teacher=cls.staff,
            role_type=SchoolMembership.RoleType.DEPUTY,
            job_title=SchoolMembership.JobTitle.TEACHER,
        )
        cls.membership_b = SchoolMembership.objects.create(
            school=cls.school_b,
            teacher=cls.staff,
            role_type=SchoolMembership.RoleType.ADMIN_STAFF,
            job_title=SchoolMembership.JobTitle.LAB_TECH,
        )
        cls.department_a = Department.objects.create(
            school=cls.school_a, name="قسم المدرسة أ", slug="access-a-dept"
        )
        cls.department_b = Department.objects.create(
            school=cls.school_b, name="قسم المدرسة ب", slug="access-b-dept"
        )

    def _enter(self, user, school):
        self.client.force_login(user)
        session = self.client.session
        session["active_school_id"] = school.pk
        session.save()

    def test_manager_sees_role_job_scope_and_audit_as_separate_facts(self):
        scope = StaffScope.objects.create(
            membership=self.membership_a,
            capabilities=[caps.VIEW_SCHOOL_DASHBOARD, caps.REVIEW_REPORTS],
        )
        scope.departments.add(self.department_a)
        AuditLog.objects.create(
            school=self.school_a,
            teacher=self.manager_a,
            actor_name=self.manager_a.name,
            actor_role="مدير مدرسة",
            action=AuditLog.Action.UPDATE,
            model_name="StaffScope",
            object_id=scope.pk,
            object_repr=f"نطاق {self.staff.name}",
        )
        self._enter(self.manager_a, self.school_a)

        response = self.client.get(reverse("reports:staff_roles"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "إدارة وصول المنسوبين والتفويضات")
        self.assertContains(response, "الدور الصلاحي")
        self.assertContains(response, "المسمى الوظيفي")
        self.assertContains(response, "الصلاحيات الأصلية")
        self.assertContains(response, self.department_a.name)
        self.assertContains(response, self.manager_a.name)
        self.assertNotContains(response, self.department_b.name)

    def test_multi_school_context_never_mixes_membership_facts(self):
        self._enter(self.manager_a, self.school_a)
        in_a = self.client.get(reverse("reports:staff_roles"))
        row_a = next(row for row in in_a.context["rows"] if row["person"] == self.staff)
        self.assertEqual(row_a["primary"].role_type, SchoolMembership.RoleType.DEPUTY)
        self.assertEqual(row_a["job_title_label"], "معلم")

        self._enter(self.manager_b, self.school_b)
        in_b = self.client.get(reverse("reports:staff_roles"))
        row_b = next(row for row in in_b.context["rows"] if row["person"] == self.staff)
        self.assertEqual(row_b["primary"].role_type, SchoolMembership.RoleType.ADMIN_STAFF)
        self.assertEqual(row_b["job_title_label"], "محضر مختبر")

    def test_foreign_department_is_rejected_without_scope_creation(self):
        self._enter(self.manager_a, self.school_a)

        response = self.client.post(
            reverse("reports:staff_role_scope", args=[self.membership_a.pk]),
            {
                "action": "save_scope",
                "domain": StaffScope.Domain.ACADEMIC,
                "departments": [self.department_b.pk],
                "capabilities": [caps.REVIEW_REPORTS],
                "template_code": "",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(StaffScope.objects.filter(membership=self.membership_a).exists())
        self.assertContains(response, 'aria-invalid="true"')
        self.assertContains(response, "تعذّر حفظ النطاق")

    def test_delegation_ignores_forged_school_and_delegator_fields(self):
        self._enter(self.manager_a, self.school_a)
        starts = timezone.localtime()
        ends = starts + timedelta(days=2)

        response = self.client.post(
            reverse("reports:staff_roles"),
            {
                "action": "grant_delegation",
                "delegate": self.staff.pk,
                "capabilities": [caps.VIEW_AUDIT_LOG],
                "reason": "تغطية مؤقتة",
                "starts_at": starts.strftime("%Y-%m-%dT%H:%M"),
                "ends_at": ends.strftime("%Y-%m-%dT%H:%M"),
                "school": self.school_b.pk,
                "delegator": self.manager_b.pk,
            },
        )

        self.assertEqual(response.status_code, 302)
        delegation = Delegation.objects.get(delegate=self.staff)
        self.assertEqual(delegation.school_id, self.school_a.pk)
        self.assertEqual(delegation.delegator_id, self.manager_a.pk)

    def test_delegated_staff_cannot_chain_or_open_access_management(self):
        Delegation.objects.create(
            school=self.school_a,
            delegator=self.manager_a,
            delegate=self.staff,
            capabilities=[caps.VIEW_AUDIT_LOG],
            starts_at=timezone.now() - timedelta(hours=1),
            ends_at=timezone.now() + timedelta(days=1),
        )
        self._enter(self.staff, self.school_a)

        response = self.client.get(reverse("reports:staff_roles"))

        self.assertEqual(response.status_code, 302)
        self.assertFalse(
            Delegation.objects.filter(delegator=self.staff, school=self.school_a).exists()
        )

    def test_expiry_tampering_does_not_create_a_delegation(self):
        self._enter(self.manager_a, self.school_a)
        starts = timezone.localtime()

        response = self.client.post(
            reverse("reports:staff_roles"),
            {
                "action": "grant_delegation",
                "delegate": self.staff.pk,
                "capabilities": [caps.VIEW_AUDIT_LOG],
                "starts_at": starts.strftime("%Y-%m-%dT%H:%M"),
                "ends_at": (starts - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M"),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Delegation.objects.filter(delegate=self.staff).exists())
        self.assertContains(response, "نهاية التفويض يجب أن تكون بعد بدايته")

    def test_post_actions_require_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.manager_a)
        session = client.session
        session["active_school_id"] = self.school_a.pk
        session.save()

        response = client.post(
            reverse("reports:staff_roles"),
            {
                "action": "assign_role",
                "member": self.staff.pk,
                "role_type": SchoolMembership.RoleType.TEACHER,
            },
        )

        self.assertEqual(response.status_code, 403)
        self.membership_a.refresh_from_db()
        self.assertEqual(self.membership_a.role_type, SchoolMembership.RoleType.DEPUTY)

    def test_roster_query_count_is_bounded_with_many_choices(self):
        for index in range(30):
            person = Teacher.objects.create_user(
                phone=f"0568{index:06d}", name=f"منسوب قياس {index}", password="x"
            )
            SchoolMembership.objects.create(
                school=self.school_a,
                teacher=person,
                role_type=SchoolMembership.RoleType.TEACHER,
            )
        self._enter(self.manager_a, self.school_a)
        self.client.get(reverse("reports:staff_roles"))

        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(reverse("reports:staff_roles"))

        self.assertEqual(response.status_code, 200)
        self.assertLess(len(captured), 60)
