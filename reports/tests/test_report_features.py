from django.test import TestCase, override_settings
from django.urls import reverse

from datetime import date
from urllib.parse import unquote

from django.utils import timezone

from reports.models import (
    Report,
    ReportType,
    School,
    SchoolArchiveAddon,
    SchoolMembership,
    SchoolSubscription,
    SubscriptionPlan,
    Teacher,
)
from reports.search_utils import normalize_arabic, smart_search_q, REPORT_SEARCH_FIELDS


@override_settings(ALLOWED_HOSTS=["testserver"])
class _BaseSchoolFixture(TestCase):
    def setUp(self):
        self.school = School.objects.create(name="مدرسة الاختبار", code="test-school")
        self.other_school = School.objects.create(name="مدرسة أخرى", code="other-school")
        plan = SubscriptionPlan.objects.create(name="Plan", price=0, days_duration=30, max_teachers=0)
        SchoolSubscription.objects.create(school=self.school, plan=plan)
        SchoolSubscription.objects.create(school=self.other_school, plan=plan)
        SchoolArchiveAddon.objects.create(
            school=self.school,
            is_enabled=True,
            start_date=timezone.localdate(),
            storage_limit_gb=10,
        )

        self.manager = Teacher.objects.create_user(
            phone="500000001", name="مدير المدرسة", password="pass", is_staff=True
        )
        SchoolMembership.objects.create(
            school=self.school, teacher=self.manager, role_type=SchoolMembership.RoleType.MANAGER
        )

        self.teacher = Teacher.objects.create_user(phone="500000002", name="معلم", password="pass")
        SchoolMembership.objects.create(
            school=self.school, teacher=self.teacher, role_type=SchoolMembership.RoleType.TEACHER
        )

        self.category = ReportType.objects.create(school=self.school, code="radio", name="الإذاعة")

    def _login(self, user):
        self.client.force_login(user)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()


class SchoolDataExportTests(_BaseSchoolFixture):
    def test_export_page_shows_counts(self):
        self._login(self.manager)
        response = self.client.get(reverse("reports:school_data_export"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "أرشفة المدرسة كاملة")
        self.assertContains(response, "جميع السنوات والحالات")
        self.assertContains(response, "النسخة تُحفظ على جهازك")
        self.assertContains(response, reverse("reports:school_archive"))
        self.assertContains(response, "أرشيف السنوات")
        self.assertContains(response, "حفظ محتويات المدرسة")
        self.assertContains(response, "التقارير والإنجاز والأداء القيادي")
        self.assertGreater(response.context["record_total"], 0)
        self.assertContains(response, str(response.context["record_total"]))
        self.assertContains(response, reverse("reports:school_data_export_zip"))

    def test_teacher_cannot_access_export(self):
        self._login(self.teacher)
        response = self.client.get(reverse("reports:school_data_export"))
        self.assertEqual(response.status_code, 302)

    def test_download_returns_xlsx(self):
        self._login(self.manager)
        response = self.client.get(reverse("reports:school_data_export_download"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response["Content-Type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertIn("test-school", response["Content-Disposition"])
        # ملف xlsx صالح يبدأ بتوقيع ZIP
        self.assertEqual(response.content[:2], b"PK")

    def test_zip_export_returns_archive(self):
        import io
        import zipfile

        self._login(self.manager)
        response = self.client.get(reverse("reports:school_data_export_zip"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/zip")
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertIn(f"{self.school.name}.zip", unquote(response["Content-Disposition"]))
        content = b"".join(response.streaming_content)
        zf = zipfile.ZipFile(io.BytesIO(content))
        names = zf.namelist()
        # يحوي ملف الفهرس Excel على الأقل
        self.assertTrue(any(n.endswith(".xlsx") for n in names))


class SmartSearchTests(TestCase):
    def test_normalize_unifies_arabic_variants(self):
        self.assertEqual(normalize_arabic("أَحْمَد"), "احمد")
        self.assertEqual(normalize_arabic("الإذاعةُ"), "الاذاعه")
        self.assertEqual(normalize_arabic("مُصطَفـى"), "مصطفي")

    def test_empty_query_returns_neutral_q(self):
        self.assertEqual(len(smart_search_q("", REPORT_SEARCH_FIELDS)), 0)

    def test_search_matches_across_arabic_variants(self):
        school = School.objects.create(name="مدرسة", code="s1")
        teacher = Teacher.objects.create_user(phone="500111000", name="معلم", password="x")
        cat_radio = ReportType.objects.create(school=school, code="radio", name="الإذاعة")
        cat_line = ReportType.objects.create(school=school, code="line", name="الاصطفاف")
        Report.objects.create(
            school=school, teacher=teacher, title="الإذاعة الصباحية",
            idea="فعالية", category=cat_radio, report_date=date(2025, 1, 1),
        )
        Report.objects.create(
            school=school, teacher=teacher, title="اصطفاف الطابور",
            idea="نشاط", category=cat_line, report_date=date(2025, 1, 2),
        )
        base = Report.objects.filter(school=school)

        # كتابة بلا همزة/تشكيل تطابق العنوان المهموز (عنوان + نوع الإذاعة لنفس الصف)
        q1 = smart_search_q("الاذاعه", REPORT_SEARCH_FIELDS)
        self.assertEqual(base.filter(q1).distinct().count(), 1)
        self.assertEqual(base.filter(q1).first().title, "الإذاعة الصباحية")

        # البحث عبر اسم النوع
        q2 = smart_search_q("اصطفاف", REPORT_SEARCH_FIELDS)
        self.assertEqual(base.filter(q2).distinct().count(), 1)

        # كلمتان (AND) لا تطابقان أي صف واحد
        q3 = smart_search_q("اذاعه طابور", REPORT_SEARCH_FIELDS)
        self.assertEqual(base.filter(q3).count(), 0)

    def test_my_reports_view_smart_search(self):
        school = School.objects.create(name="مدرسة2", code="s2")
        plan = SubscriptionPlan.objects.create(name="P", price=0, days_duration=30, max_teachers=0)
        SchoolSubscription.objects.create(school=school, plan=plan)
        teacher = Teacher.objects.create_user(phone="500111222", name="سعيد", password="x")
        SchoolMembership.objects.create(school=school, teacher=teacher, role_type=SchoolMembership.RoleType.TEACHER)
        Report.objects.create(
            school=school, teacher=teacher, title="مبادرة الإثراء",
            idea="تفاصيل", report_date=date(2025, 3, 3),
        )
        self.client.force_login(teacher)
        session = self.client.session
        session["active_school_id"] = school.id
        session.save()
        from django.urls import reverse as _rev
        resp = self.client.get(_rev("reports:my_reports"), {"q": "الاثراء"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "مبادرة الإثراء")
