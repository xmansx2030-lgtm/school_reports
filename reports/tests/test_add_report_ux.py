"""صفحة إضافة تقرير — ما تغيّر في بنائها، وما يجب ألّا ينكسر معه."""

from __future__ import annotations

import io
from datetime import date
from pathlib import Path

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from PIL import Image

from reports.forms import ReportEvidenceFormSet
from reports.models import (
    Report,
    ReportEvidence,
    ReportType,
    School,
    SchoolMembership,
    SchoolSubscription,
    SubscriptionPlan,
    Teacher,
)


def _png(name: str, *, size=(600, 400)) -> SimpleUploadedFile:
    buffer = io.BytesIO()
    Image.new("RGB", size, (18, 92, 56)).save(buffer, format="PNG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")


def _template(relative: str) -> str:
    return (Path(settings.BASE_DIR) / relative).read_text(encoding="utf-8")


class EvidenceFormsetShapeTests(SimpleTestCase):
    def test_only_one_blank_evidence_card_is_rendered(self):
        """أربع بطاقات فارغة كانت نصفَ الصفحة على الجوال و٢٤ عنصر تحكّم."""
        self.assertEqual(ReportEvidenceFormSet.extra, 1)

    def test_the_card_partial_is_the_single_source_for_both_renders(self):
        formset = _template("reports/templates/reports/partials/report_evidence_formset.html")
        # مرّةً في الحلقة، ومرّةً داخل ``<template>`` للبطاقة المضافة.
        self.assertEqual(formset.count("reports/partials/report_evidence_card.html"), 2)
        self.assertIn("empty_form", formset)
        self.assertIn("data-evidence-template", formset)
        self.assertIn("data-evidence-add", formset)

    def test_print_options_are_folded_away(self):
        card = _template("reports/templates/reports/partials/report_evidence_card.html")
        self.assertIn("<details class=\"ree-advanced\">", card)
        self.assertIn("display_size", card)
        self.assertIn("fit_mode", card)

    def test_delete_is_a_button_not_a_bare_checkbox(self):
        card = _template("reports/templates/reports/partials/report_evidence_card.html")
        self.assertIn("data-evidence-remove", card)
        self.assertIn("data-evidence-delete", card)


class AddReportPageTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(name="مدرسة الواجهة", code="ux-add")
        plan = SubscriptionPlan.objects.create(
            name="خطة الواجهة", price=0, days_duration=30, max_teachers=20
        )
        SchoolSubscription.objects.create(school=self.school, plan=plan)
        self.teacher = Teacher.objects.create_user(
            phone="500009301", name="معلم الواجهة", password="test-pass"
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.teacher,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        self.category = ReportType.objects.create(
            school=self.school, code="activity", name="نشاط مدرسي"
        )
        self.client.force_login(self.teacher)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()

    def _page(self):
        response = self.client.get(reverse("reports:add_report"))
        self.assertEqual(response.status_code, 200)
        return response

    def test_page_renders_the_rail_the_gallery_and_the_mobile_bar(self):
        response = self._page()
        self.assertContains(response, "ملخّص التقرير")
        self.assertContains(response, "data-report-summary")
        self.assertContains(response, "data-evidence-add")
        self.assertContains(response, "data-report-mobile-actions")
        self.assertContains(response, "css/report-rail.css")
        self.assertContains(response, "css/report-mobile-actions.css")
        self.assertContains(response, "js/report-summary-rail.js")

    def test_only_one_blank_evidence_card_reaches_the_page(self):
        response = self._page()
        self.assertEqual(response.content.decode("utf-8").count("data-evidence-card"), 2)

    def test_template_comments_never_reach_the_reader(self):
        """‏``{# … #}`` لا يمتدّ سطرين في Django، فيُطبع كنصٍّ ظاهر للمستخدم.

        وقع ذلك فعلاً في هذه الصفحة: ظهرت تعليقات المطوّر فوق العنوان وبين
        الحقول. والحارس هنا يمنع عودته مهما كثرت التعليقات.
        """
        body = self._page().content.decode("utf-8")
        self.assertNotIn("{#", body)
        self.assertNotIn("#}", body)
        self.assertNotIn("{% comment %}", body)

    def test_required_marks_are_quiet_and_explained_once(self):
        body = self._page().content.decode("utf-8")
        self.assertIn("الحقول المعلَّمة بنجمة مطلوبة", body)
        # كانت تسع شارات حمراء قبل كتابة حرف؛ بقيت ثلاث نجمات هادئة.
        self.assertEqual(body.count('class="ar-required-mark" aria-hidden="true"'), 3)
        self.assertNotIn("مطلوب عند الاختيار", body)

    def test_the_day_and_the_executor_no_longer_take_a_field_each(self):
        body = self._page().content.decode("utf-8")
        # حقل اليوم يبقى في النموذج — الخادم يستقبله — لكنه لم يعد خانةً ظاهرة.
        self.assertIn("ar-visually-hidden", body)
        self.assertIn('id="reportDayEcho"', body)
        self.assertIn("ar-static-value", body)
        self.assertNotIn('id="id_teacher_name"', body)

    def test_marketing_copy_is_gone_from_the_hero(self):
        self.assertNotContains(self._page(), "مساحتك الإبداعية لتوثيق الإنجازات")


class AddReportSubmissionTests(TestCase):
    """أهمّ اختبار في الملف: هل يُحفظ التقرير بعد كل هذا التغيير؟"""

    def setUp(self):
        self.school = School.objects.create(name="مدرسة الحفظ", code="ux-save")
        plan = SubscriptionPlan.objects.create(
            name="خطة الحفظ", price=0, days_duration=30, max_teachers=20
        )
        SchoolSubscription.objects.create(school=self.school, plan=plan)
        self.teacher = Teacher.objects.create_user(
            phone="500009401", name="معلم الحفظ", password="test-pass"
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.teacher,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        self.category = ReportType.objects.create(
            school=self.school, code="activity", name="نشاط مدرسي"
        )
        self.client.force_login(self.teacher)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()

    def _payload(self, *, evidence_forms: int):
        data = {
            "section_selection_enabled": "1",
            "category": "activity",
            "title": "برنامج التوعية بالأمن السيبراني",
            "report_date": date(2026, 9, 1).isoformat(),
            "day_name": "الثلاثاء",
            "show_details": "on",
            "idea": (
                "نُفّذ البرنامج في قاعة النشاط بحضور طلاب الصفوف الثلاثة، وتضمّن "
                "عرضًا تعريفيًا ثم ورشة عملية على أمثلة واقعية من رسائل التصيّد."
            ),
            "show_beneficiaries": "on",
            "beneficiaries_count": "45",
            "evidence_page_mode": Report.EvidencePageMode.AUTO,
            "evidence-TOTAL_FORMS": str(evidence_forms),
            "evidence-INITIAL_FORMS": "0",
            "evidence-MIN_NUM_FORMS": "0",
            "evidence-MAX_NUM_FORMS": "8",
        }
        for index in range(evidence_forms):
            data[f"evidence-{index}-order"] = str(index + 1)
            data[f"evidence-{index}-description"] = ""
            data[f"evidence-{index}-display_size"] = ReportEvidence.DisplaySize.AUTO
            data[f"evidence-{index}-fit_mode"] = ReportEvidence.FitMode.CONTAIN
            data[f"evidence-{index}-show_in_print"] = "on"
        return data

    def test_a_report_saves_with_the_single_rendered_evidence_slot(self):
        data = self._payload(evidence_forms=1)
        data["evidence-0-image"] = _png("one.png")
        response = self.client.post(reverse("reports:add_report"), data=data)
        self.assertIn(response.status_code, (200, 302))

        report = Report.objects.get(title="برنامج التوعية بالأمن السيبراني")
        self.assertEqual(report.school_id, self.school.pk)
        self.assertEqual(ReportEvidence.objects.filter(report=report).count(), 1)

    def test_evidence_cards_added_in_the_browser_are_saved(self):
        """الجافاسكربت يضيف نماذج ويرفع ``TOTAL_FORMS``؛ هذا ما يصل الخادم."""
        data = self._payload(evidence_forms=3)
        data["evidence-0-image"] = _png("one.png")
        data["evidence-1-image"] = _png("two.png")
        data["evidence-2-image"] = _png("three.png")

        response = self.client.post(reverse("reports:add_report"), data=data)
        self.assertIn(response.status_code, (200, 302))

        report = Report.objects.get(title="برنامج التوعية بالأمن السيبراني")
        evidences = ReportEvidence.objects.filter(report=report).order_by("order")
        self.assertEqual(evidences.count(), 3)
        self.assertEqual([item.order for item in evidences], [1, 2, 3])

    def test_a_card_the_user_removed_is_not_saved(self):
        """الإزالة تُعلِّم ``DELETE`` وتُخفي البطاقة، ولا تنتزعها من الصفحة."""
        data = self._payload(evidence_forms=2)
        data["evidence-0-image"] = _png("kept.png")
        data["evidence-1-image"] = _png("dropped.png")
        data["evidence-1-DELETE"] = "on"

        response = self.client.post(reverse("reports:add_report"), data=data)
        self.assertIn(response.status_code, (200, 302))

        report = Report.objects.get(title="برنامج التوعية بالأمن السيبراني")
        self.assertEqual(ReportEvidence.objects.filter(report=report).count(), 1)

    def test_a_report_saves_with_no_evidence_at_all(self):
        response = self.client.post(
            reverse("reports:add_report"), data=self._payload(evidence_forms=1)
        )
        self.assertIn(response.status_code, (200, 302))
        report = Report.objects.get(title="برنامج التوعية بالأمن السيبراني")
        self.assertEqual(ReportEvidence.objects.filter(report=report).count(), 0)
