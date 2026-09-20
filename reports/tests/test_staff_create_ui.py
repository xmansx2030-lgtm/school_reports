"""Individual staff creation UI contracts; business rules remain in onboarding tests."""

from django.contrib.staticfiles import finders
from django.test import Client, TestCase
from django.urls import reverse

from reports.models import (
    School,
    SchoolMembership,
    SchoolSubscription,
    SubscriptionPlan,
    Teacher,
)


class StaffCreateUiTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(name="مدرسة الإنشاء", code="staff-create-a")
        self.other_school = School.objects.create(name="مدرسة ثانية", code="staff-create-b")
        plan = SubscriptionPlan.objects.create(
            name="باقة إنشاء تجريبية", price=0, days_duration=365, max_teachers=20
        )
        SchoolSubscription.objects.create(school=self.school, plan=plan)
        SchoolSubscription.objects.create(school=self.other_school, plan=plan)
        self.manager = Teacher.objects.create_user(
            phone="0509200001", name="مدير الإنشاء", password="SafePass123!"
        )
        self.employee = Teacher.objects.create_user(
            phone="0509200002", name="موظف", password="SafePass123!"
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.employee,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        self.client.force_login(self.manager)
        session = self.client.session
        session["active_school_id"] = self.school.pk
        session.save()
        self.url = reverse("reports:add_teacher")

    def test_form_uses_real_fields_and_school_context(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "reports/add_teacher.html")
        self.assertContains(response, "css/staff-create.css")
        self.assertIsNotNone(finders.find("css/staff-create.css"))
        self.assertContains(response, "مدرسة الإنشاء")
        self.assertContains(response, 'id="teacherCreateForm"')
        self.assertContains(response, 'name="csrfmiddlewaretoken"')
        self.assertContains(response, 'type="radio" name="job_title"', count=4)
        self.assertContains(response, 'class="role-option"', count=4)
        for field in ("name", "phone", "national_id", "is_active", "keep_teaching_role", "lab_kind"):
            self.assertContains(response, f'name="{field}"')
        self.assertNotContains(response, 'name="password"')
        self.assertNotContains(response, 'name="department"')
        self.assertNotContains(response, 'name="email"')

    def test_accessible_errors_and_internal_next(self):
        response = self.client.post(
            self.url,
            {
                "name": "",
                "phone": "123",
                "national_id": "bad",
                "job_title": "manager",
                "is_active": "on",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="staff-create-errors"')
        self.assertContains(response, 'aria-invalid="true"')
        self.assertContains(response, 'aria-describedby="staff-create-phone-help staff-create-phone-error"')
        self.assertContains(response, 'id="staff-role-error"')
        self.assertFalse(Teacher.objects.filter(phone="123").exists())
        self.assertNotContains(self.client.get(self.url + "?next=https://bad.example/"), "bad.example")
        self.assertContains(
            self.client.get(self.url + "?next=/staff/teachers/?q=sample"),
            'name="next" value="/staff/teachers/?q=sample"',
        )

    def test_new_account_redirects_and_uses_existing_password_contract(self):
        response = self.client.post(
            self.url,
            {
                "name": "منسوب جديد",
                "phone": "0559200003",
                "national_id": "",
                "job_title": SchoolMembership.RoleType.TEACHER,
                "is_active": "on",
            },
        )
        self.assertRedirects(
            response, reverse("reports:manage_teachers"), fetch_redirect_response=False
        )
        member = Teacher.objects.get(phone="0559200003")
        self.assertTrue(member.check_password(member.phone))
        self.assertTrue(
            SchoolMembership.objects.filter(
                school=self.school, teacher=member, role_type=SchoolMembership.RoleType.TEACHER
            ).exists()
        )

    def test_existing_cross_school_account_is_linked_without_password_change(self):
        member = Teacher.objects.create_user(
            phone="0559200004", name="حساب سابق", password="KeepOldPass123!"
        )
        SchoolMembership.objects.create(
            school=self.other_school,
            teacher=member,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        original_password = member.password
        response = self.client.post(
            self.url,
            {
                "name": "اسم مختلف لا يكتب",
                "phone": member.phone,
                "national_id": "",
                "job_title": SchoolMembership.RoleType.TEACHER,
                "is_active": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        member.refresh_from_db()
        self.assertEqual(member.name, "حساب سابق")
        self.assertEqual(member.password, original_password)
        self.assertEqual(
            SchoolMembership.objects.filter(school=self.school, teacher=member).count(), 1
        )

    def test_existing_member_has_no_duplicate_membership(self):
        payload = {
            "name": self.employee.name,
            "phone": self.employee.phone,
            "national_id": "",
            "job_title": SchoolMembership.RoleType.TEACHER,
            "is_active": "on",
        }
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            SchoolMembership.objects.filter(school=self.school, teacher=self.employee).count(), 1
        )

    def test_employee_cannot_access_create(self):
        self.client.force_login(self.employee)
        session = self.client.session
        session["active_school_id"] = self.school.pk
        session.save()
        response = self.client.get(self.url)
        self.assertNotEqual(response.status_code, 200)

    def test_manager_cannot_switch_to_another_school_or_post_a_school_id(self):
        session = self.client.session
        session["active_school_id"] = self.other_school.pk
        session.save()
        self.assertNotEqual(self.client.get(self.url).status_code, 200)
        session = self.client.session
        session["active_school_id"] = self.school.pk
        session.save()
        response = self.client.post(
            self.url,
            {
                "name": "منسوب محدود النطاق",
                "phone": "0559200006",
                "national_id": "",
                "job_title": SchoolMembership.RoleType.TEACHER,
                "is_active": "on",
                "school": str(self.other_school.pk),
            },
        )
        self.assertEqual(response.status_code, 302)
        member = Teacher.objects.get(phone="0559200006")
        self.assertTrue(SchoolMembership.objects.filter(school=self.school, teacher=member).exists())
        self.assertFalse(
            SchoolMembership.objects.filter(school=self.other_school, teacher=member).exists()
        )

    def test_platform_owner_can_open_the_same_scoped_form(self):
        owner = Teacher.objects.create_superuser(
            phone="0509200007", name="مالك المنصة", password="SafePass123!"
        )
        self.client.force_login(owner)
        session = self.client.session
        session["active_school_id"] = self.school.pk
        session.save()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "مدرسة الإنشاء")

    def test_csrf_is_required(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.manager)
        session = client.session
        session["active_school_id"] = self.school.pk
        session.save()
        response = client.post(
            self.url,
            {"name": "غير معتمد", "phone": "0559200005", "job_title": "teacher"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Teacher.objects.filter(phone="0559200005").exists())
