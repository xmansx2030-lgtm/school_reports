"""Presentation and contract checks for the existing staff edit route."""

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


class StaffEditUiTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(name="مدرسة التجربة", code="staff-edit-a")
        self.other_school = School.objects.create(name="مدرسة أخرى", code="staff-edit-b")
        plan = SubscriptionPlan.objects.create(
            name="باقة تجريبية للمنسوبين", price=0, days_duration=365, max_teachers=20
        )
        SchoolSubscription.objects.create(school=self.school, plan=plan)
        SchoolSubscription.objects.create(school=self.other_school, plan=plan)
        self.manager = Teacher.objects.create_user(
            phone="0509000001", name="مدير التجربة", password="Passw0rd!123"
        )
        self.member = Teacher.objects.create_user(
            phone="0509000002", name="منسوب التجربة", password="Passw0rd!123"
        )
        self.outsider = Teacher.objects.create_user(
            phone="0509000003", name="منسوب آخر", password="Passw0rd!123"
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )
        self.membership = SchoolMembership.objects.create(
            school=self.school,
            teacher=self.member,
            role_type=SchoolMembership.RoleType.TEACHER,
            job_title=SchoolMembership.JobTitle.TEACHER,
        )
        SchoolMembership.objects.create(
            school=self.other_school,
            teacher=self.outsider,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        self.client.force_login(self.manager)
        session = self.client.session
        session["active_school_id"] = self.school.pk
        session.save()
        self.url = reverse("reports:edit_teacher", args=[self.member.pk])

    def test_edit_is_separate_from_role_and_department_assignment(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "reports/edit_teacher.html")
        self.assertContains(response, "css/staff-edit.css")
        self.assertIsNotNone(finders.find("css/staff-edit.css"))
        self.assertContains(response, 'id="teacherEditForm"')
        self.assertContains(response, 'name="job_title"')
        self.assertContains(response, 'value="0509000002"')
        self.assertContains(response, "المسمى الوظيفي وصف تنظيمي")
        self.assertContains(response, "تغيير الدور أو ارتباط القسم لا يحدث عند حفظ هذا النموذج")
        self.assertNotContains(response, 'name="role_type"')
        self.assertNotContains(response, 'name="department"')
        self.assertContains(response, 'aria-describedby="staff-phone-help staff-phone-error"')
        self.assertContains(response, 'name="next"', count=0)

    def test_next_navigation_is_internal_only(self):
        response = self.client.get(self.url + "?next=https://outside.example/account")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "outside.example")
        response = self.client.get(self.url + "?next=/staff/teachers/?q=sample")
        self.assertContains(response, 'name="next" value="/staff/teachers/?q=sample"')

    def test_invalid_values_show_field_errors_without_saving(self):
        response = self.client.post(
            self.url,
            {
                "name": "",
                "phone": "123",
                "national_id": "bad",
                "job_title": SchoolMembership.JobTitle.TEACHER,
                "is_active": "on",
                "password": "",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "تعذّر حفظ التعديلات")
        self.assertContains(response, 'id="staff-phone-error"')
        self.assertContains(response, 'aria-invalid="true"')
        self.member.refresh_from_db()
        self.assertEqual(self.member.name, "منسوب التجربة")

    def test_save_preserves_permission_role_and_password(self):
        old_password = self.member.password
        response = self.client.post(
            self.url,
            {
                "name": "اسم محدّث",
                "phone": self.member.phone,
                "national_id": "",
                "job_title": SchoolMembership.JobTitle.ADMIN_STAFF,
                "is_active": "on",
                "password": "",
                "next": "/staff/teachers/?q=sample",
            },
        )
        self.assertRedirects(
            response, "/staff/teachers/?q=sample", fetch_redirect_response=False
        )
        self.member.refresh_from_db()
        self.membership.refresh_from_db()
        self.assertEqual(self.member.name, "اسم محدّث")
        self.assertEqual(self.member.password, old_password)
        self.assertEqual(self.membership.role_type, SchoolMembership.RoleType.TEACHER)
        self.assertEqual(
            self.membership.job_title, SchoolMembership.JobTitle.ADMIN_STAFF
        )

    def test_duplicate_phone_and_invalid_job_title_are_rejected(self):
        response = self.client.post(
            self.url,
            {
                "name": self.member.name,
                "phone": self.outsider.phone,
                "national_id": "",
                "job_title": "manager",
                "is_active": "on",
                "password": "",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="staff-phone-error"')
        self.assertContains(response, 'id="staff-job_title-error"')
        self.member.refresh_from_db()
        self.membership.refresh_from_db()
        self.assertEqual(self.member.phone, "0509000002")
        self.assertEqual(self.membership.role_type, SchoolMembership.RoleType.TEACHER)

    def test_duplicate_national_id_is_rejected(self):
        self.outsider.national_id = "1234567890"
        self.outsider.save(update_fields=["national_id"])
        response = self.client.post(
            self.url,
            {
                "name": self.member.name,
                "phone": self.member.phone,
                "national_id": self.outsider.national_id,
                "job_title": SchoolMembership.JobTitle.TEACHER,
                "is_active": "on",
                "password": "",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="staff-national_id-error"')
        self.member.refresh_from_db()
        self.assertIsNone(self.member.national_id)

    def test_account_deactivation_does_not_remove_school_membership(self):
        response = self.client.post(
            self.url,
            {
                "name": self.member.name,
                "phone": self.member.phone,
                "national_id": "",
                "job_title": SchoolMembership.JobTitle.TEACHER,
                "password": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_active)
        self.assertTrue(
            SchoolMembership.objects.filter(
                school=self.school, teacher=self.member
            ).exists()
        )

    def test_wrong_school_and_ordinary_employee_cannot_edit(self):
        wrong_school_url = reverse("reports:edit_teacher", args=[self.outsider.pk])
        response = self.client.get(wrong_school_url)
        self.assertNotEqual(response.status_code, 200)

        self.client.force_login(self.member)
        response = self.client.get(self.url)
        self.assertNotEqual(response.status_code, 200)

    def test_platform_owner_retains_existing_cross_school_access(self):
        self.manager.is_superuser = True
        self.manager.save(update_fields=["is_superuser", "is_staff"])
        response = self.client.get(
            reverse("reports:edit_teacher", args=[self.outsider.pk])
        )
        self.assertEqual(response.status_code, 200)

    def test_post_requires_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.manager)
        session = client.session
        session["active_school_id"] = self.school.pk
        session.save()
        response = client.post(self.url, {"name": "تغيير"})
        self.assertEqual(response.status_code, 403)
