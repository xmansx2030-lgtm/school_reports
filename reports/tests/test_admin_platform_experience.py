from django.contrib import admin
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from reports.admin import TeacherAdmin, TeacherCreationForm
from reports.models import School, SchoolMembership, Teacher
from reports.templatetags.admin_navigation import platform_admin_navigation


@override_settings(ALLOWED_HOSTS=["testserver"])
class PlatformAdminExperienceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.platform_admin = Teacher.objects.create_superuser(
            phone="500880001",
            name="مشرف المنصة",
            password="Admin-Safe-Password-2026!",
        )
        cls.user = Teacher.objects.create_user(
            phone="500880002",
            name="مستخدم تجريبي",
            password="User-Safe-Password-2026!",
        )
        cls.school = School.objects.create(
            name="مدرسة اختبار الإدارة",
            code="admin-experience-school",
        )
        cls.membership = SchoolMembership.objects.create(
            school=cls.school,
            teacher=cls.user,
            role_type=SchoolMembership.RoleType.MANAGER,
            is_active=True,
        )

    def setUp(self):
        self.client.force_login(self.platform_admin)

    def test_user_change_page_never_exposes_writable_password_field(self):
        url = reverse("admin:reports_teacher_change", args=(self.user.pk,))
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="password"')
        self.assertContains(response, "تعيين كلمة مرور جديدة")
        self.assertContains(response, "مدرسة اختبار الإدارة")
        self.assertContains(response, "الصلاحيات المتقدمة")

        request = RequestFactory().get(url)
        request.user = self.platform_admin
        form_class = TeacherAdmin(Teacher, admin.site).get_form(request, self.user)
        self.assertNotIn("password", form_class.base_fields)

    def test_user_password_route_hashes_the_new_password(self):
        password_url = reverse("admin:auth_user_password_change", args=(self.user.pk,))
        new_password = "New-Safe-Password-2026!"

        response = self.client.post(
            password_url,
            {
                "usable_password": "true",
                "password1": new_password,
                "password2": new_password,
                "set-password": "1",
            },
        )

        self.assertRedirects(
            response,
            reverse("admin:reports_teacher_change", args=(self.user.pk,)),
            fetch_redirect_response=False,
        )
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(new_password))
        self.assertNotEqual(self.user.password, new_password)

    def test_user_creation_form_applies_password_validation(self):
        weak_form = TeacherCreationForm(
            data={
                "phone": "500880003",
                "name": "حساب جديد",
                "national_id": "",
                "is_active": "on",
                "password1": "123",
                "password2": "123",
            }
        )

        self.assertFalse(weak_form.is_valid())
        self.assertIn("password2", weak_form.errors)
        self.assertEqual(
            weak_form.fields["password1"].widget.attrs["autocomplete"],
            "new-password",
        )

    def test_admin_home_groups_daily_tasks_and_collapses_technical_tools(self):
        response = self.client.get(reverse("admin:index"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ماذا تريد أن تدير اليوم؟")
        self.assertContains(response, "المدارس والمستخدمون")
        self.assertContains(response, "الاشتراكات والإيرادات")
        self.assertContains(response, "الدعم والتواصل")
        self.assertContains(response, "الأدوات المتقدمة والسجلات")
        self.assertContains(response, "data-admin-advanced")

        app_list = response.context["app_list"]
        navigation = platform_admin_navigation(app_list)
        available_models = {
            id(model)
            for app in app_list
            for model in app.get("models", ())
        }
        grouped_models = {
            id(model)
            for section in navigation["primary"] + navigation["advanced"]
            for model in section["models"]
        }
        self.assertEqual(grouped_models, available_models)

    def test_sidebar_uses_business_sections_and_keeps_advanced_models_reachable(self):
        response = self.client.get(
            reverse("admin:reports_teacher_change", args=(self.user.pk,))
        )

        self.assertContains(response, "إدارة المنصة")
        self.assertContains(response, "ابحث في أدوات الإدارة")
        self.assertContains(response, "الأدوات المتقدمة")
        self.assertContains(response, reverse("admin:operations_managedserver_changelist"))
