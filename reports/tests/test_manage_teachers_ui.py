"""Presentation contracts for the school people directory pilot."""

from __future__ import annotations

from html import unescape
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from reports.models import School, SchoolMembership, SchoolSubscription, SubscriptionPlan, Teacher


class ManageTeachersTemplateTests(SimpleTestCase):
    def test_list_owns_external_css_and_keeps_post_confirmation_hook(self):
        source = (
            Path(settings.BASE_DIR) / "reports/templates/reports/manage_teachers.html"
        ).read_text(encoding="utf-8")
        self.assertIn("css/users-list.css", source)
        self.assertNotIn("<style", source)
        self.assertIn('class="teacher-delete-form users-list-delete-form"', source)
        self.assertIn('data-confirm-type="danger"', source)
        self.assertIn("{% csrf_token %}", source)


class ManageTeachersListUITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = School.objects.create(name="مدرسة الدليل", code="users-list-a")
        cls.other_school = School.objects.create(name="مدرسة أخرى", code="users-list-b")
        plan = SubscriptionPlan.objects.create(
            name="باقة اختبار الدليل", price=0, days_duration=365, max_teachers=40
        )
        SchoolSubscription.objects.create(school=cls.school, plan=plan)
        SchoolSubscription.objects.create(school=cls.other_school, plan=plan)
        cls.manager = Teacher.objects.create_user(
            phone="0500091000", name="مدير الدليل", password="TestPass123!"
        )
        SchoolMembership.objects.create(
            school=cls.school,
            teacher=cls.manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )
        cls.employee = Teacher.objects.create_user(
            phone="0500091001", name="منسوب ظاهر", email="staff@example.test",
            password="TestPass123!"
        )
        SchoolMembership.objects.create(
            school=cls.school,
            teacher=cls.employee,
            role_type=SchoolMembership.RoleType.TEACHER,
            job_title=SchoolMembership.JobTitle.TEACHER,
        )
        cls.outsider = Teacher.objects.create_user(
            phone="0500091002", name="منسوب خارج المدرسة", password="TestPass123!"
        )
        SchoolMembership.objects.create(
            school=cls.other_school,
            teacher=cls.outsider,
            role_type=SchoolMembership.RoleType.TEACHER,
        )

    def setUp(self):
        self.client.force_login(self.manager)
        session = self.client.session
        session["active_school_id"] = self.school.pk
        session.save()

    def test_manager_sees_scoped_identity_status_and_existing_actions(self):
        response = self.client.get(reverse("reports:manage_teachers"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "منسوب ظاهر")
        self.assertContains(response, "staff@example.test")
        self.assertNotContains(response, "منسوب خارج المدرسة")
        self.assertContains(response, "مدرسة الدليل")
        self.assertContains(response, 'data-status="active"')
        self.assertContains(response, 'aria-label="تعديل بيانات: منسوب ظاهر"')
        self.assertContains(response, 'aria-label="إزالة من المدرسة: منسوب ظاهر"')
        self.assertContains(response, 'name="status"')
        self.assertContains(response, 'name="job_title"')
        self.assertContains(response, 'name="department"')
        self.assertContains(response, 'name="q"')
        self.assertContains(response, 'class="twq-table users-list-table"')

    def test_search_and_status_filter_have_visible_active_context(self):
        response = self.client.get(
            reverse("reports:manage_teachers"), {"q": "منسوب ظاهر", "status": "active"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "الفلاتر النشطة")
        self.assertContains(response, "نتيجة مطابقة")
        self.assertContains(response, "منسوب ظاهر")
        self.assertNotContains(response, "منسوب خارج المدرسة")

    def test_no_matches_is_distinct_from_an_empty_directory(self):
        response = self.client.get(reverse("reports:manage_teachers"), {"q": "لا-يطابق"})
        self.assertContains(response, "لا توجد نتائج مطابقة")
        self.assertNotContains(response, "لا يوجد منسوبون في الدليل بعد")

    def test_inactive_school_membership_has_no_unusable_management_actions(self):
        inactive_member = Teacher.objects.create_user(
            phone="0500091003", name="عضوية غير نشطة", password="TestPass123!"
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=inactive_member,
            role_type=SchoolMembership.RoleType.TEACHER,
            is_active=False,
        )
        response = self.client.get(
            reverse("reports:manage_teachers"), {"q": "عضوية غير نشطة"}
        )
        self.assertContains(response, "عضوية غير نشطة")
        self.assertContains(response, "لا إجراء متاح")
        self.assertNotContains(response, reverse("reports:edit_teacher", args=[inactive_member.pk]))
        self.assertNotContains(response, reverse("reports:delete_teacher", args=[inactive_member.pk]))

    def test_pagination_preserves_filter_parameters(self):
        for index in range(21):
            teacher = Teacher.objects.create_user(
                phone=f"05{index + 10000000:08d}",
                name=f"منسوب صفحة {index}",
                password="TestPass123!",
            )
            SchoolMembership.objects.create(
                school=self.school,
                teacher=teacher,
                role_type=SchoolMembership.RoleType.TEACHER,
                job_title=SchoolMembership.JobTitle.TEACHER,
            )
        response = self.client.get(
            reverse("reports:manage_teachers"),
            {"q": "منسوب", "status": "active", "job_title": "teacher"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["teachers_page"].paginator.num_pages, 2)
        html = unescape(response.content.decode("utf-8"))
        self.assertIn("?page=2&q=", html)
        self.assertIn("&status=active&job_title=teacher", html)

    def test_ordinary_employee_cannot_open_manager_list(self):
        self.client.force_login(self.employee)
        response = self.client.get(reverse("reports:manage_teachers"))
        self.assertNotEqual(response.status_code, 200)
