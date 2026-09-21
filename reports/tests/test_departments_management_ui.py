"""UI, CRUD, and tenant contracts for the departments management pilot."""

from pathlib import Path

from django.conf import settings
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from reports.models import (
    Department,
    DepartmentMembership,
    LabAsset,
    ReportType,
    School,
    SchoolMembership,
    SchoolSubscription,
    SubscriptionPlan,
    Teacher,
    Ticket,
)


@override_settings(ALLOWED_HOSTS=["testserver"], RATELIMIT_ENABLE=False)
class DepartmentsManagementUITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = School.objects.create(name="مدرسة الأقسام", code="departments-ui")
        cls.other_school = School.objects.create(name="مدرسة أخرى", code="departments-other")
        plan = SubscriptionPlan.objects.create(
            name="خطة اختبار الأقسام", price=0, days_duration=30, max_teachers=0
        )
        SchoolSubscription.objects.create(school=cls.school, plan=plan)
        SchoolSubscription.objects.create(school=cls.other_school, plan=plan)

        cls.manager = Teacher.objects.create_user(
            phone="500920001", name="مدير الأقسام", password="test-pass", is_staff=True
        )
        SchoolMembership.objects.create(
            school=cls.school,
            teacher=cls.manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )
        cls.employee = Teacher.objects.create_user(
            phone="500920002", name="منسوب الأقسام", password="test-pass"
        )
        SchoolMembership.objects.create(
            school=cls.school,
            teacher=cls.employee,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        cls.owner = Teacher.objects.create_superuser(
            phone="500920003", name="مالك المنصة", password="test-pass"
        )
        cls.report_type = ReportType.objects.create(
            school=cls.school, code="department-ui-report", name="تقارير الأقسام"
        )
        cls.other_report_type = ReportType.objects.create(
            school=cls.other_school, code="other-department-report", name="تقارير أخرى"
        )

    def _activate(self, user, school=None, client=None):
        client = client or self.client
        client.force_login(user)
        session = client.session
        session["active_school_id"] = (school or self.school).pk
        session.save()
        return client

    def _payload(self, name="قسم العلوم", slug="", **changes):
        return {
            "name": name,
            "slug": slug,
            "is_active": "on",
            "reporttypes": [str(self.report_type.pk)],
            **changes,
        }

    def test_list_uses_v1_workspace_and_school_scoped_counts(self):
        department = Department.objects.create(
            school=self.school, name="قسم العلوم", slug="science", is_active=True
        )
        Department.objects.create(
            school=self.other_school, name="قسم سري", slug="private", is_active=True
        )
        DepartmentMembership.objects.create(department=department, teacher=self.employee)
        Ticket.objects.create(
            school=self.school,
            creator=self.employee,
            department=department,
            title="طلب القسم",
        )
        self._activate(self.manager)

        response = self.client.get(reverse("reports:departments_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "مدرسة الأقسام")
        self.assertContains(response, "قسم العلوم")
        self.assertNotContains(response, "قسم سري")
        self.assertContains(response, "css/departments-management.css")
        self.assertContains(response, 'class="twq-table departments-table"')
        self.assertContains(response, 'data-status="active"')
        self.assertContains(response, "عضو")
        template = (
            Path(settings.BASE_DIR)
            / "reports"
            / "templates"
            / "reports"
            / "departments_list.html"
        ).read_text(encoding="utf-8")
        self.assertNotIn("<style", template)

    def test_create_and_duplicate_validation_preserve_contract(self):
        self._activate(self.manager)
        missing_name = self.client.post(
            reverse("reports:department_create"), self._payload(name=""), follow=False
        )
        self.assertEqual(missing_name.status_code, 200)
        self.assertIn("name", missing_name.context["form"].errors)
        self.assertFalse(Department.objects.filter(school=self.school).exists())

        created = self.client.post(
            reverse("reports:department_create"), self._payload(), follow=False
        )
        self.assertRedirects(
            created, reverse("reports:departments_list"), fetch_redirect_response=False
        )
        department = Department.objects.get(school=self.school, name="قسم العلوم")
        self.assertTrue(department.slug)
        self.assertEqual(department.name, "قسم العلوم")
        self.assertTrue(department.is_active)
        self.assertEqual(list(department.reporttypes.all()), [self.report_type])

        duplicate = self.client.post(
            reverse("reports:department_create"), self._payload(), follow=False
        )
        self.assertEqual(duplicate.status_code, 200)
        self.assertIn("slug", duplicate.context["form"].errors)
        self.assertContains(duplicate, 'id="departmentFormErrors"')
        self.assertContains(duplicate, 'aria-invalid="true"')
        self.assertEqual(Department.objects.filter(school=self.school).count(), 1)

    def test_edit_preserves_slug_and_can_deactivate(self):
        department = Department.objects.create(
            school=self.school, name="قسم قديم", slug="stable-code", is_active=True
        )
        membership = DepartmentMembership.objects.create(
            department=department, teacher=self.employee
        )
        self._activate(self.manager)

        response = self.client.post(
            reverse("reports:department_edit", args=[department.slug]),
            {
                "name": "قسم مطوّر",
                "slug": department.slug,
                "reporttypes": [str(self.report_type.pk)],
            },
        )

        self.assertRedirects(
            response, reverse("reports:departments_list"), fetch_redirect_response=False
        )
        department.refresh_from_db()
        self.assertEqual(department.name, "قسم مطوّر")
        self.assertEqual(department.slug, "stable-code")
        self.assertFalse(department.is_active)
        self.assertTrue(DepartmentMembership.objects.filter(pk=membership.pk).exists())

    def test_crafted_cross_school_edit_delete_and_report_type_are_rejected(self):
        other = Department.objects.create(
            school=self.other_school, name="قسم المدرسة الأخرى", slug="other-only"
        )
        self._activate(self.manager)

        edit = self.client.post(
            reverse("reports:department_edit", args=[other.slug]),
            self._payload(name="اسم مزور", slug=other.slug),
        )
        delete = self.client.post(reverse("reports:department_delete", args=[other.slug]))
        crafted_type = self.client.post(
            reverse("reports:department_create"),
            self._payload(
                name="قسم بنوع خارجي",
                slug="external-type",
                reporttypes=[str(self.other_report_type.pk)],
            ),
        )

        self.assertRedirects(
            edit, reverse("reports:departments_list"), fetch_redirect_response=False
        )
        self.assertRedirects(
            delete, reverse("reports:departments_list"), fetch_redirect_response=False
        )
        other.refresh_from_db()
        self.assertEqual(other.name, "قسم المدرسة الأخرى")
        self.assertEqual(crafted_type.status_code, 200)
        self.assertIn("reporttypes", crafted_type.context["form"].errors)
        self.assertFalse(
            Department.objects.filter(school=self.school, slug="external-type").exists()
        )

    def test_delete_cascades_membership_and_detaches_ticket(self):
        department = Department.objects.create(
            school=self.school, name="قسم مؤقت", slug="temporary"
        )
        membership = DepartmentMembership.objects.create(
            department=department, teacher=self.employee
        )
        ticket = Ticket.objects.create(
            school=self.school,
            creator=self.employee,
            department=department,
            title="طلب مرتبط",
        )
        self._activate(self.manager)

        response = self.client.post(
            reverse("reports:department_delete", args=[department.slug])
        )

        self.assertRedirects(
            response, reverse("reports:departments_list"), fetch_redirect_response=False
        )
        self.assertFalse(Department.objects.filter(pk=department.pk).exists())
        self.assertFalse(DepartmentMembership.objects.filter(pk=membership.pk).exists())
        ticket.refresh_from_db()
        self.assertIsNone(ticket.department_id)

    def test_protected_lab_dependency_blocks_delete(self):
        department = Department.objects.create(
            school=self.school, name="مختبر العلوم", slug="science-lab"
        )
        LabAsset.objects.create(
            school=self.school, department=department, name="مجهر تجريبي"
        )
        self._activate(self.manager)

        response = self.client.post(
            reverse("reports:department_delete", args=[department.slug]), follow=True
        )

        self.assertTrue(Department.objects.filter(pk=department.pk).exists())
        self.assertContains(response, "لا يمكن حذف")
        self.assertContains(response, "لوجود سجلات مرتبطة")

    def test_manager_department_is_protected_in_ui_and_backend(self):
        department = Department.objects.create(
            school=self.school, name="الإدارة", slug="manager"
        )
        self._activate(self.manager)

        listing = self.client.get(reverse("reports:departments_list"))
        deletion = self.client.post(
            reverse("reports:department_delete", args=[department.slug]), follow=True
        )

        self.assertContains(listing, "قسم محمي")
        self.assertNotContains(
            listing,
            reverse("reports:department_delete", args=[department.slug]),
        )
        self.assertTrue(Department.objects.filter(pk=department.pk).exists())
        self.assertContains(deletion, "تعذّر حذف القسم")

    def test_employee_is_denied_and_platform_owner_uses_selected_school(self):
        own_department = Department.objects.create(
            school=self.school, name="قسم المدرسة", slug="own-department"
        )
        other_department = Department.objects.create(
            school=self.other_school, name="قسم المالك", slug="owner-department"
        )
        self._activate(self.employee)
        denied = self.client.get(reverse("reports:departments_list"))
        self.assertNotEqual(denied.status_code, 200)

        self._activate(self.owner, self.other_school)
        owner = self.client.get(reverse("reports:departments_list"))
        self.assertEqual(owner.status_code, 200)
        self.assertContains(owner, other_department.name)
        self.assertNotContains(owner, own_department.name)

    def test_state_changes_require_csrf(self):
        department = Department.objects.create(
            school=self.school, name="قسم CSRF", slug="csrf-department"
        )
        client = Client(enforce_csrf_checks=True)
        self._activate(self.manager, client=client)

        create = client.post(reverse("reports:department_create"), self._payload())
        delete = client.post(reverse("reports:department_delete", args=[department.slug]))

        self.assertEqual(create.status_code, 403)
        self.assertEqual(delete.status_code, 403)
        self.assertTrue(Department.objects.filter(pk=department.pk).exists())
