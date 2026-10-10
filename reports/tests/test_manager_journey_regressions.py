# -*- coding: utf-8 -*-
"""أعطالٌ ظهرت في مسحٍ كامل لرحلة مدير المدرسة.

أربعة أعطال يجمعها طبعٌ واحد: **لا شيء منها يفشل في السجلّ**. النموذج يُرسَل
والصفحة تُعاد، والاختبار الذي يُرسل بيانات صحيحة يمرّ — والمدير وحده هو من يرى
حقلاً فارغاً، أو صفحة خطأ، أو شاشة دخولٍ وهو داخلٌ أصلاً. فحراستها هنا لا في
مراجعة العين.

1. ``datetime-local`` لا يقرأ إلا ``2026-09-03T15:18``. صيغة جانغو العامة
   بمسافة يرفضها المتصفح **صامتاً** فيعرض الحقل فارغاً — والموعد الافتراضي
   الذي وضعه الكود لا يصل صاحبه.
2. سنةٌ دراسيةٌ إلزاميةٌ بلا خيار واحد تجعل أرشيف الوثائق معطّلاً في كل مدرسة
   لم يمرّ مديرها على «بيانات المدرسة».
3. مُعرِّفٌ غير رقمي يُمرَّر خاماً إلى الاستعلام ينهار بـ 500 قبل أن يصل السطر
   الذي كُتبت فيه الرسالة المقصودة.
4. حارسُ صلاحيةٍ يردّ المسجَّلَ بالفعل إلى شاشة الدخول: مشكلتُه صلاحيةٌ لا
   هوية.
"""
from __future__ import annotations

import re
from datetime import timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from reports.academic_years import hijri_academic_year_options, current_academic_year_value
from reports.form_widgets import DATETIME_LOCAL_FORMAT, DateTimeLocalInput
from reports.forms_documents import DocumentUploadForm
from reports.models import (
    AcademicYear,
    Department,
    School,
    SchoolMembership,
    SchoolSubscription,
    SubscriptionPlan,
    Teacher,
)
from reports.views._helpers import coerce_pk

# ما يقبله المتصفح في قيمة ``datetime-local`` بحسب مواصفة HTML.
DATETIME_LOCAL_VALUE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$")


def _user(name: str, phone: str) -> Teacher:
    return Teacher.objects.create_user(phone=phone, name=name, password="Passw0rd!123")


class ManagerSchoolBase(TestCase):
    def setUp(self):
        plan = SubscriptionPlan.objects.create(
            name="باقة", price=0, days_duration=365, max_teachers=0
        )
        self.school = School.objects.create(name="مدرسة الرحلة", code="journey-school")
        SchoolSubscription.objects.create(school=self.school, plan=plan)

        self.manager = _user("المدير", "0500090001")
        SchoolMembership.objects.create(
            school=self.school, teacher=self.manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )
        self.teacher = _user("المعلم", "0500090002")
        SchoolMembership.objects.create(
            school=self.school, teacher=self.teacher,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        self.department = Department.objects.create(
            school=self.school, name="الشؤون الأكاديمية", slug="journey-dept"
        )
        self.client.force_login(self.manager)
        session = self.client.session
        session["active_school_id"] = self.school.pk
        session.save()

    def _dept_code(self) -> str:
        return self.department.slug


# ══════════════════════════════════════════════════════════════════════════
# 1) الموعد الافتراضي يجب أن يصل المتصفح بالصيغة التي يقرأها
# ══════════════════════════════════════════════════════════════════════════
class DateTimeLocalRenderingTests(TestCase):
    def test_widget_renders_the_iso_form_the_browser_accepts(self):
        moment = timezone.localtime() + timedelta(days=7)
        rendered = DateTimeLocalInput().render("due_at", moment)
        value = re.search(r'value="([^"]*)"', rendered).group(1)
        self.assertRegex(value, DATETIME_LOCAL_VALUE)
        self.assertNotIn(" ", value, "المسافة بدل T ترفضها المواصفة فيُعرض الحقل فارغاً")

    def test_format_constant_matches_the_html_grammar(self):
        self.assertEqual(DATETIME_LOCAL_FORMAT, "%Y-%m-%dT%H:%M")

    def test_no_form_declares_a_bare_datetime_local_widget(self):
        """الصيغة تُورَث من الحقل المشترك، فلا تُكتب بيدٍ فتُنسى في نموذج."""
        from django import forms as django_forms

        from reports import (
            forms as forms_root,
            forms_assignments,
            forms_group_notifications,
            forms_meetings,
            forms_plans,
            forms_staff_roles,
        )

        modules = [
            forms_root, forms_assignments, forms_group_notifications,
            forms_meetings, forms_plans, forms_staff_roles,
        ]
        offenders = []
        for module in modules:
            for form_name in dir(module):
                form_cls = getattr(module, form_name)
                if not (isinstance(form_cls, type)
                        and issubclass(form_cls, django_forms.BaseForm)):
                    continue
                for field_name, field in getattr(form_cls, "base_fields", {}).items():
                    widget = field.widget
                    if widget.attrs.get("type") != "datetime-local":
                        continue
                    if getattr(widget, "format", None) != DATETIME_LOCAL_FORMAT:
                        offenders.append(f"{module.__name__}.{form_name}.{field_name}")
        self.assertEqual(offenders, [], "حقول تطبع صيغةً لا يقرأها المتصفح")


class ManagerSeesTheSuggestedDeadlineTests(ManagerSchoolBase):
    def test_assignment_screen_prefills_a_usable_due_date(self):
        response = self.client.get(reverse("reports:assignment_create"))
        self.assertEqual(response.status_code, 200)
        self._assert_prefilled(response, "due_at")

    def test_meeting_screen_prefills_a_usable_datetime(self):
        response = self.client.get(reverse("reports:meeting_create"))
        self.assertEqual(response.status_code, 200)
        self._assert_prefilled(response, "scheduled_at")

    def _assert_prefilled(self, response, field_name):
        html = response.content.decode("utf-8")
        tag = re.search(r'<input[^>]*name="%s"[^>]*>' % field_name, html)
        self.assertIsNotNone(tag, f"حقل {field_name} غير معروض")
        value = re.search(r'value="([^"]*)"', tag.group(0))
        self.assertIsNotNone(value, f"حقل {field_name} بلا موعد مقترح")
        self.assertRegex(
            value.group(1), DATETIME_LOCAL_VALUE,
            f"الموعد المقترح في {field_name} بصيغة يرفضها المتصفح فيعرضه فارغاً",
        )


# ══════════════════════════════════════════════════════════════════════════
# 2) أرشيف الوثائق يعمل في مدرسةٍ لم تُضبط سنتها بعد
# ══════════════════════════════════════════════════════════════════════════
class AcademicYearOptionsTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(name="مدرسة جديدة", code="fresh-school")

    def test_a_brand_new_school_still_gets_years_to_choose_from(self):
        self.assertEqual(self.school.current_academic_year, current_academic_year_value())
        self.assertEqual(list(self.school.allowed_academic_years or []), [])
        self.assertTrue(hijri_academic_year_options(self.school))

    def test_central_years_are_offered_when_the_owner_defines_them(self):
        AcademicYear.objects.update_or_create(value="1447-1448", defaults={"is_active": True})
        AcademicYear.objects.update_or_create(value="1448-1449", defaults={"is_active": True})
        AcademicYear.objects.update_or_create(value="1400-1401", defaults={"is_active": False})
        options = hijri_academic_year_options(self.school)
        self.assertIn("1447-1448", options)
        self.assertIn("1448-1449", options)
        self.assertNotIn("1400-1401", options, "سنة معطّلة لا تُعرض")

    def test_a_year_the_school_already_uses_never_disappears(self):
        AcademicYear.objects.update_or_create(value="1448-1449", defaults={"is_active": True})
        self.school.current_academic_year = "1440-1441"
        self.school.save(update_fields=["current_academic_year"])
        self.assertIn("1440-1441", hijri_academic_year_options(self.school))


@override_settings(MEDIA_ROOT="/tmp/journey-test-media")
class DocumentArchiveIsUsableFromDayOneTests(ManagerSchoolBase):
    def test_upload_form_offers_a_year_without_any_school_setup(self):
        form = DocumentUploadForm(school=self.school)
        choices = [value for value, _ in form.fields["academic_year"].choices if value]
        self.assertTrue(
            choices,
            "حقل السنة إلزامي؛ خلوّه من الخيارات يجعل رفع أي وثيقة مستحيلاً",
        )

    def test_manager_can_archive_a_document_before_touching_settings(self):
        AcademicYear.objects.update_or_create(value="1447-1448", defaults={"is_active": True})
        response = self.client.post(
            reverse("reports:document_archive"),
            {
                "title": "محضر لجنة",
                "description": "وصف",
                "academic_year": "1447-1448",
                "kind": "minutes",
                "file": SimpleUploadedFile(
                    "doc.pdf", b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n",
                    content_type="application/pdf",
                ),
            },
        )
        self.assertIn(response.status_code, (302, 303))
        from reports.models import Document

        self.assertEqual(Document.objects.filter(school=self.school).count(), 1)


# ══════════════════════════════════════════════════════════════════════════
# 3) مُعرِّفٌ مشوَّه يُقابَل برسالة لا بصفحة خطأ
# ══════════════════════════════════════════════════════════════════════════
class CoercePkTests(TestCase):
    def test_only_positive_whole_numbers_survive(self):
        self.assertEqual(coerce_pk("12"), 12)
        self.assertEqual(coerce_pk(" 12 "), 12)
        self.assertEqual(coerce_pk(12), 12)
        for bad in ("", None, "abc", "1.5", "-3", "0", "١٢٣ن", [], {}):
            self.assertIsNone(coerce_pk(bad), f"{bad!r} ليس مُعرِّفاً")


class MalformedIdsDoNotCrashManagerScreensTests(ManagerSchoolBase):
    def _post(self, url, data):
        response = self.client.post(url, data)
        self.assertLess(
            response.status_code, 500,
            "مُعرِّفٌ مشوَّه أسقط الشاشة بدل أن يُقابَل بالرسالة المكتوبة له",
        )
        return response

    def test_department_members_survives_every_action(self):
        url = reverse("reports:department_members", kwargs={"code": self._dept_code()})
        for action in ("add", "set_officer", "unset_officer", "remove"):
            for teacher_id in ("abc", "", "-1", "9" * 40):
                self._post(url, {"action": action, "teacher_id": teacher_id})

    def test_api_key_creation_survives_a_malformed_identity(self):
        url = reverse("reports:api_key_create")
        for acting_as in ("abc", "", "-1"):
            response = self._post(url, {"name": "مفتاح", "acting_as": acting_as, "scope": "read"})
            self.assertIn(response.status_code, (302, 303))
        from reports.models import SchoolApiKey

        self.assertEqual(
            SchoolApiKey.objects.filter(school=self.school).count(), 0,
            "لا يُصدَر مفتاح لهوية لم تُحسم",
        )

    def test_a_valid_identity_still_issues_a_key(self):
        response = self.client.post(
            reverse("reports:api_key_create"),
            {"name": "مفتاح", "acting_as": str(self.teacher.pk), "scope": "read"},
        )
        self.assertIn(response.status_code, (302, 303))
        from reports.models import SchoolApiKey

        self.assertEqual(SchoolApiKey.objects.filter(school=self.school).count(), 1)


# ══════════════════════════════════════════════════════════════════════════
# 4) من كان داخلاً لا يُردّ إلى شاشة الدخول
# ══════════════════════════════════════════════════════════════════════════
class LoggedInUsersAreNotBouncedToLoginTests(ManagerSchoolBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.teacher)
        session = self.client.session
        session["active_school_id"] = self.school.pk
        session.save()

    def test_manager_dashboard_sends_a_teacher_home_not_to_login(self):
        response = self.client.get(reverse("reports:admin_dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertNotIn(
            "/login/", response.headers["Location"],
            "المستخدم مسجَّلٌ بالفعل؛ مشكلته صلاحيةٌ لا هوية",
        )
        self.assertEqual(response.headers["Location"], reverse("reports:home"))

    def test_the_reason_is_stated_not_left_to_guessing(self):
        response = self.client.get(reverse("reports:admin_dashboard"), follow=True)
        self.assertEqual(response.status_code, 200)
        texts = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("صلاحية" in t for t in texts), texts)

    def test_the_manager_still_reaches_the_dashboard(self):
        self.client.force_login(self.manager)
        session = self.client.session
        session["active_school_id"] = self.school.pk
        session.save()
        self.assertEqual(
            self.client.get(reverse("reports:admin_dashboard")).status_code, 200
        )

    def test_an_anonymous_visitor_is_still_sent_to_login(self):
        self.client.logout()
        response = self.client.get(reverse("reports:admin_dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.headers["Location"])

    def test_a_json_caller_gets_a_status_not_a_redirect(self):
        response = self.client.get(
            reverse("reports:api_school_departments"),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertIn(response.status_code, (200, 403))
        if response.status_code == 403:
            self.assertEqual(response["Content-Type"], "application/json")
