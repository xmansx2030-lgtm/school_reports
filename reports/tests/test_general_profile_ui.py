from pathlib import Path

from django.conf import settings
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import resolve, reverse

from reports.models import (
    Department,
    DepartmentMembership,
    School,
    SchoolMembership,
    SchoolSubscription,
    SubscriptionPlan,
    Teacher,
)
from reports.views.auth import _profile_membership_contexts


@override_settings(ALLOWED_HOSTS=["testserver"], RATELIMIT_ENABLE=False)
class GeneralProfilePilotTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school_a = School.objects.create(name="مدرسة النور", code="profile-a")
        cls.school_b = School.objects.create(name="مدرسة الأفق", code="profile-b")
        cls.plan = SubscriptionPlan.objects.create(
            name="باقة الملف الشخصي",
            price=0,
            days_duration=30,
            max_teachers=0,
        )
        SchoolSubscription.objects.create(school=cls.school_a, plan=cls.plan)
        SchoolSubscription.objects.create(school=cls.school_b, plan=cls.plan)
        cls.user = Teacher.objects.create_user(
            phone="0558123401",
            name="نورة متعددة المدارس",
            password="Profile-safe-password",
            email="noura@example.com",
            national_id="1099999991",
        )
        cls.other_user = Teacher.objects.create_user(
            phone="0558123402",
            name="مستخدم لا يخص الحساب",
            password="Profile-safe-password",
            email="other.private@example.com",
            national_id="1099999992",
        )
        cls.membership_a = SchoolMembership.objects.create(
            school=cls.school_a,
            teacher=cls.user,
            role_type=SchoolMembership.RoleType.TEACHER,
            job_title=SchoolMembership.JobTitle.TEACHER,
        )
        cls.membership_b = SchoolMembership.objects.create(
            school=cls.school_b,
            teacher=cls.user,
            role_type=SchoolMembership.RoleType.ADMIN_STAFF,
            job_title=SchoolMembership.JobTitle.LAB_TECH,
        )
        cls.inactive_membership = SchoolMembership.objects.create(
            school=cls.school_b,
            teacher=cls.other_user,
            role_type=SchoolMembership.RoleType.TEACHER,
            is_active=False,
        )
        cls.department_a = Department.objects.create(
            school=cls.school_a,
            name="العلوم",
            slug="profile-science",
        )
        cls.department_b = Department.objects.create(
            school=cls.school_b,
            name="المختبر",
            slug="profile-lab",
        )
        DepartmentMembership.objects.create(department=cls.department_a, teacher=cls.user)
        DepartmentMembership.objects.create(department=cls.department_b, teacher=cls.user)

    def setUp(self):
        self.client.force_login(self.user)
        self._activate(self.school_a)

    def _activate(self, school):
        session = self.client.session
        session["active_school_id"] = school.pk
        session.save()

    def test_profile_route_is_self_service_without_a_user_identifier(self):
        match = resolve(reverse("reports:my_profile"))

        self.assertEqual(match.url_name, "my_profile")
        self.assertEqual(match.kwargs, {})
        self.assertRedirects(
            Client().get(reverse("reports:my_profile")),
            f"{reverse('reports:login')}?next={reverse('reports:my_profile')}",
            fetch_redirect_response=False,
        )

    def test_normal_profile_presents_identity_contact_and_account_state(self):
        response = self.client.get(reverse("reports:my_profile"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "حسابي وملفي الشخصي")
        self.assertContains(response, self.user.name)
        self.assertContains(response, self.user.phone)
        self.assertContains(response, self.user.email)
        self.assertContains(response, 'data-status="approved"')
        self.assertContains(response, "حساب نشط")
        self.assertContains(response, "الهوية الوطنية بيان محمي")
        self.assertNotContains(response, self.user.national_id)
        self.assertNotContains(response, self.other_user.email)

    def test_memberships_keep_role_job_title_and_department_separate(self):
        response = self.client.get(reverse("reports:my_profile"))

        self.assertContains(response, self.school_a.name)
        self.assertContains(response, self.school_b.name)
        self.assertContains(response, "الدور الصلاحي", count=2)
        self.assertContains(response, "المسمى الوظيفي", count=2)
        self.assertContains(response, "موظف إداري")
        self.assertContains(response, "محضر مختبر")
        self.assertContains(response, self.department_a.name)
        self.assertContains(response, self.department_b.name)
        self.assertContains(response, "المدرسة الحالية", count=2)
        self.assertEqual(response.context["current_membership_context"]["school"], self.school_a)

    def test_switching_active_school_refreshes_current_role_context(self):
        first = self.client.get(reverse("reports:my_profile"))
        self.assertEqual(first.context["current_membership_context"]["roles"], ["معلم"])

        self._activate(self.school_b)
        second = self.client.get(reverse("reports:my_profile"))

        self.assertEqual(second.context["current_membership_context"]["school"], self.school_b)
        self.assertEqual(second.context["current_membership_context"]["roles"], ["موظف إداري"])
        self.assertContains(second, "محضر مختبر")

    def test_crafted_contact_post_cannot_change_protected_identity_or_membership(self):
        original_membership = (
            self.membership_a.role_type,
            self.membership_a.job_title,
            self.membership_a.school_id,
        )

        response = self.client.post(
            reverse("reports:my_profile"),
            {
                "update_email": "1",
                "email-email": "updated@example.com",
                "name": "اسم مزور",
                "national_id": "1011111111",
                "is_active": "0",
                "role_type": SchoolMembership.RoleType.MANAGER,
                "job_title": SchoolMembership.JobTitle.ADMIN_STAFF,
                "school": self.school_b.pk,
                "school_id": self.school_b.pk,
                "membership": self.membership_b.pk,
                "membership_id": self.membership_b.pk,
                "user_id": self.other_user.pk,
            },
        )

        self.assertRedirects(response, reverse("reports:my_profile"), fetch_redirect_response=False)
        self.user.refresh_from_db()
        self.membership_a.refresh_from_db()
        self.assertEqual(self.user.email, "updated@example.com")
        self.assertEqual(self.user.name, "نورة متعددة المدارس")
        self.assertEqual(self.user.national_id, "1099999991")
        self.assertTrue(self.user.is_active)
        self.assertEqual(
            (
                self.membership_a.role_type,
                self.membership_a.job_title,
                self.membership_a.school_id,
            ),
            original_membership,
        )

    def test_valid_mobile_update_changes_only_the_signed_in_user_mobile(self):
        response = self.client.post(
            reverse("reports:my_profile"),
            {"update_phone": "1", "phone-phone": "0558123499"},
        )

        self.assertRedirects(response, reverse("reports:my_profile"), fetch_redirect_response=False)
        self.user.refresh_from_db()
        self.other_user.refresh_from_db()
        self.assertEqual(self.user.phone, "0558123499")
        self.assertEqual(self.other_user.phone, "0558123402")

    def test_invalid_mobile_update_is_rejected_without_mutation(self):
        response = self.client.post(
            reverse("reports:my_profile"),
            {"update_phone": "1", "phone-phone": "123"},
        )

        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.phone, "0558123401")
        self.assertContains(response, "تعذر تحديث رقم الجوال")
        self.assertContains(response, 'aria-invalid="true"')
        self.assertContains(response, 'id="profilePhoneError"')

    def test_contact_validation_exposes_summary_and_field_semantics(self):
        response = self.client.post(
            reverse("reports:my_profile"),
            {"update_email": "1", "email-email": "not-an-email"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "تعذر تحديث البريد")
        self.assertContains(response, 'aria-invalid="true"')
        self.assertContains(response, 'id="profileEmailError"')
        self.assertContains(response, 'aria-describedby="profileEmailHint profileEmailError"')

    def test_membership_and_department_rendering_is_query_bounded(self):
        memberships = list(
            SchoolMembership.objects.filter(teacher=self.user, is_active=True)
            .select_related("school")
            .order_by("school__name", "id")
        )
        with CaptureQueriesContext(connection) as baseline_queries:
            baseline_contexts = _profile_membership_contexts(
                self.user,
                memberships,
                active_school=self.school_a,
            )
        self.assertEqual(len(baseline_contexts), 2)

        for index in range(4):
            school = School.objects.create(name=f"مدرسة إضافية {index}", code=f"profile-extra-{index}")
            SchoolSubscription.objects.create(school=school, plan=self.plan)
            SchoolMembership.objects.create(
                school=school,
                teacher=self.user,
                role_type=SchoolMembership.RoleType.TEACHER,
            )
            department = Department.objects.create(
                school=school,
                name=f"قسم إضافي {index}",
                slug=f"profile-extra-dept-{index}",
            )
            DepartmentMembership.objects.create(department=department, teacher=self.user)

        expanded_memberships = list(
            SchoolMembership.objects.filter(teacher=self.user, is_active=True)
            .select_related("school")
            .order_by("school__name", "id")
        )
        with CaptureQueriesContext(connection) as expanded_queries:
            expanded_contexts = _profile_membership_contexts(
                self.user,
                expanded_memberships,
                active_school=self.school_a,
            )
        self.assertEqual(len(expanded_contexts), 6)
        self.assertEqual(len(baseline_queries), 1)
        self.assertEqual(len(expanded_queries), 1)

    def test_general_profile_assets_have_clear_ownership(self):
        root = Path(settings.BASE_DIR)
        template = (root / "reports" / "templates" / "reports" / "my_profile.html").read_text(
            encoding="utf-8"
        )
        stylesheet = (root / "static" / "css" / "profile.css").read_text(encoding="utf-8")

        self.assertIn("css/profile.css", template)
        self.assertIn("css/auth-password.css", template)
        self.assertIn("css/auth-security.css", template)
        self.assertNotIn("<style", template)
        self.assertNotRegex(stylesheet, r"#[0-9a-fA-F]{3,8}\b")
        self.assertNotRegex(stylesheet, r"\b(?:rgb|hsl)a?\(")
        self.assertNotIn("transition: all", stylesheet)
        self.assertNotRegex(
            stylesheet,
            r"(?m)^\s*(?:margin|padding|border|inset)-(?:left|right)\s*:",
        )
        self.assertIn("var(--twq-", stylesheet)
        self.assertIn("inline-size", stylesheet)
