from io import BytesIO

import openpyxl
from django.core.cache import caches
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from reports.models import (
    School,
    SchoolMembership,
    SchoolSubscription,
    SubscriptionPlan,
    Teacher,
)
from reports.teacher_onboarding import PREVIEW_SESSION_KEY


@override_settings(ALLOWED_HOSTS=["testserver"])
class StaffBulkImportUITests(TestCase):
    def tearDown(self):
        # The onboarding POST is rate-limited per user; TestCase reuses integer
        # IDs across classes while the test cache otherwise survives the class.
        caches["default"].clear()

    def setUp(self):
        self.school = School.objects.create(name="مدرسة واجهة الاستيراد", code="bulk-ui")
        self.plan = SubscriptionPlan.objects.create(
            name="باقة اختبار واجهة الاستيراد",
            price=0,
            days_duration=30,
            max_teachers=20,
        )
        SchoolSubscription.objects.create(school=self.school, plan=self.plan)
        manager = Teacher.objects.create_user(
            phone="0500000081",
            name="مدير الاختبار",
            password="test-only-password",
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )
        self.client.force_login(manager)
        session = self.client.session
        session["active_school_id"] = self.school.pk
        session.save()
        self.url = reverse("reports:bulk_import_teachers")

    def test_quick_page_reuses_design_language_and_preserves_post_fields(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "css/staff-bulk-import.css")
        self.assertContains(response, 'class="twq-page onboard"')
        self.assertContains(response, 'name="action" value="quick_preview"')
        self.assertContains(response, 'name="pasted_rows"')
        self.assertContains(response, 'name="lab_kind"')

    def test_file_page_explains_real_limits_and_keeps_native_upload(self):
        response = self.client.get(self.url, {"mode": "file"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="excel_file"')
        self.assertContains(response, 'name="action" value="file_preview"')
        self.assertContains(response, 'accept=".xlsx,.csv"')
        self.assertContains(response, "10 ميجابايت و2000 صف")
        self.assertContains(response, "الاسم الكامل")
        self.assertContains(response, "رقم الجوال")
        self.assertContains(response, reverse("reports:bulk_import_teachers_template"))

    def test_invalid_preview_has_textual_error_and_no_confirm_action(self):
        response = self.client.post(
            self.url,
            {
                "action": "quick_preview",
                "name": ["صف تجريبي"],
                "phone": ["123"],
                "national_id": [""],
                "job_title": ["teacher"],
                "department": [""],
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.client.session[PREVIEW_SESSION_KEY]["can_confirm"])
        preview = self.client.get(self.url, {"step": "preview"})
        self.assertContains(preview, "صف يحتاج إلى تصحيح")
        self.assertContains(preview, "ob-state--invalid")
        self.assertContains(preview, 'name="action" value="confirm" disabled')
        self.assertContains(preview, reverse("reports:bulk_import_teachers_issues"))

    def test_completed_view_reports_existing_memberships_without_credentials(self):
        existing = Teacher.objects.create_user(
            phone="0552223388",
            name="منسوب قائم",
            password="test-only-existing",
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=existing,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        self.client.post(
            self.url,
            {
                "action": "quick_preview",
                "name": [existing.name],
                "phone": [existing.phone],
                "national_id": [""],
                "job_title": ["teacher"],
                "department": [""],
            },
        )
        token = self.client.session[PREVIEW_SESSION_KEY]["token"]
        self.client.post(self.url, {"action": "confirm", "preview_token": token})
        completed = self.client.get(self.url, {"completed": "1"})
        self.assertContains(completed, "عضوية موجودة مسبقًا")
        self.assertContains(completed, "بدء إضافة جديدة")
        self.assertNotContains(completed, reverse("reports:bulk_import_teachers_result"))

    @staticmethod
    def _upload(headers, *rows):
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(headers)
        for row in rows:
            sheet.append(row)
        payload = BytesIO()
        workbook.save(payload)
        return SimpleUploadedFile(
            "test-staff.xlsx",
            payload.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    def test_file_preview_rejects_missing_column_before_session_preview(self):
        response = self.client.post(
            self.url,
            {
                "action": "file_preview",
                "excel_file": self._upload(["الاسم الكامل"], ["صف ناقص"]),
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "عمودي الاسم الكامل ورقم الجوال")
        self.assertNotIn(PREVIEW_SESSION_KEY, self.client.session)

    def test_file_invalid_and_duplicate_rows_block_all_confirmation(self):
        self.client.post(
            self.url,
            {
                "action": "file_preview",
                "excel_file": self._upload(
                    ["الاسم الكامل", "رقم الجوال"],
                    ["جوال غير صالح", "123"],
                    ["الأول", "0557770011"],
                    ["الثاني", "0557770011"],
                ),
            },
        )
        preview = self.client.session[PREVIEW_SESSION_KEY]
        self.assertEqual(preview["summary"]["invalid"], 2)
        self.assertFalse(preview["can_confirm"])
        self.assertFalse(Teacher.objects.filter(phone="0557770011").exists())

    def test_csv_with_supported_alias_headers_reaches_preview(self):
        upload = SimpleUploadedFile(
            "test-staff.csv",
            "name,mobile\nمنسوب CSV,0557770044\n".encode("utf-8-sig"),
            content_type="text/csv",
        )
        response = self.client.post(
            self.url,
            {"action": "file_preview", "excel_file": upload},
        )
        self.assertEqual(response.status_code, 302)
        preview = self.client.session[PREVIEW_SESSION_KEY]
        self.assertEqual(preview["summary"]["new"], 1)
        self.assertEqual(preview["rows"][0]["phone"], "0557770044")
        self.assertFalse(Teacher.objects.filter(phone="0557770044").exists())

    def test_file_cannot_escalate_to_manager_role_on_confirmation(self):
        self.client.post(
            self.url,
            {
                "action": "file_preview",
                "excel_file": self._upload(
                    ["الاسم الكامل", "رقم الجوال", "المسمى الوظيفي"],
                    ["محاولة دور مدير", "0557770055", "manager"],
                ),
            },
        )
        preview = self.client.session[PREVIEW_SESSION_KEY]
        self.assertEqual(preview["summary"]["invalid"], 1)
        self.assertFalse(preview["can_confirm"])
        page = self.client.get(self.url, {"step": "preview"})
        self.assertContains(page, "التكليف غير مدعوم في الاستيراد الجماعي.")
        self.assertContains(page, 'name="action" value="confirm" disabled')
        self.client.post(
            self.url,
            {"action": "confirm", "preview_token": preview["token"]},
        )
        self.assertFalse(Teacher.objects.filter(phone="0557770055").exists())
        self.assertFalse(
            SchoolMembership.objects.filter(
                school=self.school,
                role_type=SchoolMembership.RoleType.MANAGER,
                teacher__phone="0557770055",
            ).exists()
        )

    def test_file_preview_links_existing_cross_school_account_without_disclosure(self):
        other_school = School.objects.create(name="مدرسة أخرى", code="bulk-ui-other")
        SchoolSubscription.objects.create(school=other_school, plan=self.plan)
        existing = Teacher.objects.create_user(
            phone="0557770022",
            name="الاسم الأصلي",
            password="test-only-existing",
        )
        SchoolMembership.objects.create(
            school=other_school,
            teacher=existing,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        self.client.post(
            self.url,
            {
                "action": "file_preview",
                "excel_file": self._upload(
                    ["الاسم الكامل", "رقم الجوال"],
                    ["اسم مختلف", existing.phone],
                ),
            },
        )
        preview = self.client.session[PREVIEW_SESSION_KEY]
        self.assertEqual(preview["summary"]["link"], 1)
        self.assertTrue(preview["can_confirm"])
        page = self.client.get(self.url, {"step": "preview"})
        self.assertNotContains(page, other_school.name)
        self.client.post(
            self.url,
            {"action": "confirm", "preview_token": preview["token"]},
        )
        self.assertTrue(
            SchoolMembership.objects.filter(school=self.school, teacher=existing).exists()
        )
        existing.refresh_from_db()
        self.assertEqual(existing.name, "الاسم الأصلي")
        self.assertTrue(existing.check_password("test-only-existing"))

    def test_staff_import_remains_manager_and_school_scoped(self):
        employee = Teacher.objects.create_user(
            phone="0500000082",
            name="موظف اختبار",
            password="test-only-employee",
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=employee,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        self.client.force_login(employee)
        denied = self.client.get(self.url)
        self.assertRedirects(denied, reverse("reports:home"))

        other_school = School.objects.create(name="مدرسة خارج النطاق", code="bulk-ui-scope")
        SchoolSubscription.objects.create(school=other_school, plan=self.plan)
        other_manager = Teacher.objects.create_user(
            phone="0500000083",
            name="مدير خارج النطاق",
            password="test-only-manager",
        )
        SchoolMembership.objects.create(
            school=other_school,
            teacher=other_manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )
        self.client.force_login(other_manager)
        session = self.client.session
        session["active_school_id"] = self.school.pk
        session.save()
        denied = self.client.get(self.url)
        self.assertRedirects(denied, reverse("reports:select_school"))

    def test_import_post_requires_csrf_token(self):
        strict_client = Client(enforce_csrf_checks=True)
        manager = Teacher.objects.get(phone="0500000081")
        strict_client.force_login(manager)
        session = strict_client.session
        session["active_school_id"] = self.school.pk
        session.save()
        response = strict_client.post(
            self.url,
            {
                "action": "quick_preview",
                "name": ["صف بلا رمز"],
                "phone": ["0557770033"],
            },
        )
        self.assertEqual(response.status_code, 403)
        self.assertNotIn(PREVIEW_SESSION_KEY, strict_client.session)
