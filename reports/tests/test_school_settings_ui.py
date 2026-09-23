"""Presentation and permission contracts for the school settings pilot."""

from pathlib import Path

from django.conf import settings
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from reports.models import (
    AcademicYear,
    School,
    SchoolMembership,
    SchoolSubscription,
    SubscriptionPlan,
    Teacher,
)


@override_settings(ALLOWED_HOSTS=["testserver"], RATELIMIT_ENABLE=False)
class SchoolSettingsUITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        AcademicYear.objects.update_or_create(
            value="1448-1449", defaults={"is_active": True}
        )
        cls.school = School.objects.create(
            name="مدرسة الإعدادات",
            code="settings-pilot",
            current_academic_year="1448-1449",
            city="الرياض",
        )
        cls.other_school = School.objects.create(
            name="مدرسة أخرى",
            code="settings-other",
            current_academic_year="1448-1449",
        )
        plan = SubscriptionPlan.objects.create(
            name="خطة اختبار الإعدادات", price=0, days_duration=30, max_teachers=0
        )
        SchoolSubscription.objects.create(school=cls.school, plan=plan)
        SchoolSubscription.objects.create(school=cls.other_school, plan=plan)
        cls.manager = Teacher.objects.create_user(
            phone="500910001", name="مدير الإعدادات", password="test-pass", is_staff=True
        )
        SchoolMembership.objects.create(
            school=cls.school,
            teacher=cls.manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )
        cls.employee = Teacher.objects.create_user(
            phone="500910002", name="منسوب المدرسة", password="test-pass"
        )
        SchoolMembership.objects.create(
            school=cls.school,
            teacher=cls.employee,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        cls.owner = Teacher.objects.create_superuser(
            phone="500910003", name="مالك المنصة", password="test-pass"
        )

    def _activate(self, user, school=None, client=None):
        client = client or self.client
        client.force_login(user)
        session = client.session
        session["active_school_id"] = (school or self.school).pk
        session.save()
        return client

    def _payload(self, **changes):
        return {
            "current_academic_year": "1448-1449",
            "email": "school@example.edu.sa",
            "phone": "0551234567",
            "share_link_default_days": "7",
            **changes,
        }

    def test_manager_sees_scoped_sections_and_connected_fields(self):
        self._activate(self.manager)

        response = self.client.get(reverse("reports:school_settings"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "مدرسة الإعدادات")
        self.assertContains(response, "css/school-settings.css")
        self.assertContains(response, "js/school-settings.js")
        self.assertContains(response, 'id="school-approval-title"')
        self.assertContains(response, 'id="id_phone_helptext"')
        self.assertContains(response, 'id="id_phone_error"')
        self.assertContains(
            response,
            'aria-describedby="id_phone_helptext id_phone_error"',
        )
        template_source = (
            Path(settings.BASE_DIR) / "reports" / "templates" / "reports" / "school_settings.html"
        ).read_text(encoding="utf-8")
        self.assertNotIn("<style", template_source)

    def test_save_uses_existing_redirect_and_does_not_accept_protected_fields(self):
        self._activate(self.manager)

        response = self.client.post(
            reverse("reports:school_settings"),
            self._payload(
                report_approval_enabled="on",
                name="اسم مزوّر",
                stage=School.Stage.HIGH,
                gender=School.Gender.GIRLS,
                city="مدينة أخرى",
                code="forged-code",
                is_active="",
            ),
        )
        self.assertRedirects(
            response,
            reverse("reports:admin_dashboard"),
            fetch_redirect_response=False,
        )
        self.school.refresh_from_db()
        self.other_school.refresh_from_db()
        self.assertEqual(self.school.email, "school@example.edu.sa")
        self.assertEqual(self.school.phone, "0551234567")
        self.assertEqual(self.school.share_link_default_days, 7)
        self.assertTrue(self.school.report_approval_enabled)
        self.assertEqual(self.school.name, "مدرسة الإعدادات")
        self.assertEqual(self.school.stage, School.Stage.PRIMARY)
        self.assertEqual(self.school.gender, School.Gender.BOYS)
        self.assertEqual(self.school.city, "الرياض")
        self.assertEqual(self.school.code, "settings-pilot")
        self.assertTrue(self.school.is_active)
        self.assertFalse(self.other_school.report_approval_enabled)

    def test_invalid_values_show_field_errors_and_error_summary(self):
        self._activate(self.manager)

        response = self.client.post(
            reverse("reports:school_settings"),
            self._payload(
                current_academic_year="not-a-year",
                email="invalid-email",
                phone="1234",
                share_link_default_days="-1",
            ),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="schoolSettingsErrors"')
        self.assertContains(response, 'id="id_phone_error"')
        self.assertContains(response, 'aria-invalid="true"')
        for field in (
            "current_academic_year",
            "email",
            "phone",
            "share_link_default_days",
        ):
            self.assertIn(field, response.context["form"].errors)
        self.school.refresh_from_db()
        self.assertEqual(self.school.email, "")
        self.assertFalse(self.school.report_approval_enabled)

    def test_manager_cannot_change_wrong_school_or_employee_settings(self):
        self._activate(self.manager, self.other_school)
        wrong_school = self.client.post(
            reverse("reports:school_settings"),
            self._payload(email="wrong@example.edu.sa"),
        )
        self.assertRedirects(
            wrong_school,
            reverse("reports:admin_dashboard"),
            fetch_redirect_response=False,
        )
        self.other_school.refresh_from_db()
        self.assertEqual(self.other_school.email, "")
        self.school.refresh_from_db()
        own_school_email = self.school.email

        self._activate(self.employee)
        employee = self.client.post(
            reverse("reports:school_settings"),
            self._payload(email="employee@example.edu.sa"),
        )
        self.assertRedirects(
            employee,
            reverse("reports:admin_dashboard"),
            fetch_redirect_response=False,
        )
        self.school.refresh_from_db()
        self.assertEqual(self.school.email, own_school_email)

    def test_platform_owner_can_access_selected_school(self):
        self._activate(self.owner, self.other_school)

        response = self.client.get(reverse("reports:school_settings"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "مدرسة أخرى")

    def test_state_change_requires_csrf(self):
        csrf_client = Client(enforce_csrf_checks=True)
        self._activate(self.manager, client=csrf_client)
        self.assertEqual(
            csrf_client.get(reverse("reports:school_settings")).status_code,
            200,
        )

        response = csrf_client.post(
            reverse("reports:school_settings"),
            self._payload(email="csrf@example.edu.sa"),
        )
        self.assertEqual(response.status_code, 403)
        self.school.refresh_from_db()
        self.assertEqual(self.school.email, "")
