from __future__ import annotations

import re
import tempfile
from datetime import date
from pathlib import Path

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.template.loader import render_to_string
from django.test import TestCase, override_settings

from reports.models import Report, School, SchoolMembership, Teacher
from reports.pdf_report import _generate_report_pdf_fallback, build_report_print_context


ONE_PIXEL_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000154a24f6f0000000049454e44ae426082"
)


class OfficialReportPrintDesignTests(TestCase):
    def setUp(self):
        self.media_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.media_dir.cleanup)
        self.media_override = override_settings(MEDIA_ROOT=self.media_dir.name)
        self.media_override.enable()
        self.addCleanup(self.media_override.disable)

        self.school = School.objects.create(
            name="مدرسة الوثيقة الرسمية",
            code="official-report-print",
            gender=School.Gender.GIRLS,
            stage=School.Stage.PRIMARY,
            current_academic_year="1447-1448",
        )
        self.manager = Teacher.objects.create_user(
            phone="0500999101",
            name="مديرة المدرسة",
            password="safe-manager-password",
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )
        self.teacher = Teacher.objects.create_user(
            phone="0500999102",
            name="منفذة التقرير",
            password="safe-teacher-password",
        )
        self.report = Report.objects.create(
            school=self.school,
            teacher=self.teacher,
            title="برنامج جودة الممارسات التعليمية",
            report_date=date(2026, 5, 14),
            academic_year="1447-1448",
            beneficiaries_count=35,
            idea="وصف تنفيذي واضح ومختصر للتقرير المدرسي.",
        )

    @staticmethod
    def _source(relative_path: str) -> str:
        return (Path(settings.BASE_DIR) / relative_path).read_text(encoding="utf-8")

    @staticmethod
    def _pdf_page_count(pdf_bytes: bytes) -> int:
        return len(re.findall(rb"/Type\s*/Page\b", pdf_bytes))

    def test_html_print_template_is_self_contained_and_official(self):
        template = self._source("reports/templates/reports/report_print.html")
        styles = self._source(
            "reports/templates/reports/partials/report_print_official_styles.html"
        )

        fallback_pdf_source = self._source("reports/pdf_report.py")

        self.assertNotIn("وثيقة تقرير مدرسي", template)
        self.assertNotIn("وثيقة تقرير مدرسي", fallback_pdf_source)
        self.assertNotIn("مرجع الوثيقة", template)
        self.assertNotIn("مرجع الوثيقة", fallback_pdf_source)
        self.assertNotIn("يعتمد التقرير بعد مراجعة محتواه وشواهده", template)
        self.assertNotIn(
            "يعتمد التقرير بعد مراجعة محتواه وشواهده", fallback_pdf_source
        )
        self.assertNotIn("SCHOOL_STAGE", template)
        self.assertIn("REP-{{ r.id }}", template)
        self.assertIn('tags = f"رقم التقرير: {document_ref}', fallback_pdf_source)
        self.assertEqual(template.count("رقم التقرير:"), 1)
        self.assertEqual(template.count("REP-{{ r.id }}"), 1)
        self.assertEqual(template.count("تاريخ التنفيذ:"), 1)
        self.assertNotIn("<th>المنفذ</th>", template)
        self.assertEqual(fallback_pdf_source.count('f"رقم التقرير:'), 1)
        self.assertEqual(fallback_pdf_source.count("تاريخ التنفيذ:"), 1)
        self.assertNotIn("draw_metadata_cell(margin + half_width, half_width, executor_label", fallback_pdf_source)
        self.assertIn("تفاصيل التقرير", template)
        self.assertIn("r.show_goal", template)
        self.assertIn("r.show_implementation", template)
        self.assertIn("r.show_results", template)
        self.assertIn("r.show_recommendations", template)
        self.assertIn("counter(report-section, decimal-leading-zero)", styles)
        self.assertIn("الاعتمادات والتوقيعات", template)
        self.assertIn('class="approval-block"', template)
        self.assertIn("page--dense-evidence", template)
        self.assertIn("REP-{{ r.id }}", template)
        self.assertNotIn("css/app.css", template)
        self.assertNotIn("css/royal-theme.css", template)
        self.assertNotIn("MIN_FIT_SCALE", template)
        self.assertIn('content: "منصة توثيق', styles)
        self.assertIn("counter(page)", styles)
        self.assertIn("counter(pages)", styles)
        self.assertIn("break-inside: avoid", styles)
        self.assertIn(".page--dense-evidence .img-box img", styles)
        self.assertIn("object-fit: contain", styles)
        self.assertIn("signature_anchor_y = bottom_margin + 112", fallback_pdf_source)

    def test_print_pilot_keeps_pdf_css_self_contained_and_screen_controls_separate(self):
        template = self._source("reports/templates/reports/report_print.html")
        document_css = self._source(
            "reports/templates/reports/partials/report_print_official_styles.html"
        )
        screen_css = self._source("static/css/report-print.css")

        self.assertEqual(template.count('<style nonce="{{ CSP_NONCE }}">'), 1)
        self.assertIn('include "reports/partials/report_print_official_styles.html"', template)
        self.assertIn('{% if not for_pdf %}<link rel="stylesheet" href="{% static \'css/report-print.css\' %}', template)
        self.assertIn('class="toolbar twq-print-actions no-print"', template)
        self.assertIn('<main class="page', template)
        self.assertIn('id="btnPrint"', template)
        self.assertIn('btn-print--secondary{% endif %}', template)
        self.assertIn('report_approval_enabled and is_report_owner and r.is_editable_by_owner', template)
        self.assertIn('id="btnBack"', template)
        self.assertIn('window.print()', template)
        self.assertIn('class="section report-comments no-print"', template)
        self.assertIn('@page {', document_css)
        self.assertIn('size: A4;', document_css)
        self.assertRegex(document_css, r'@page\s*\{[^}]*background:\s*#fff;')
        self.assertIn('html[data-theme="dark"] body { color-scheme: light !important;', document_css)
        self.assertIn('break-inside: avoid', document_css)
        self.assertNotIn('print-color-adjust: exact', document_css)
        self.assertIn('@media screen', screen_css)
        self.assertNotIn('@media print', screen_css)

    def test_print_pilot_preserves_fields_evidence_dates_and_signatures(self):
        template = self._source("reports/templates/reports/report_print.html")
        evidence = self._source("reports/templates/reports/partials/report_evidence_print.html")
        for field in (
            'r.title', 'SCHOOL_NAME', 'MOE_LOGO_URL', 'r.academic_year',
            'r.report_date|hijri', 'r.category.name', 'r.show_beneficiaries',
            'r.show_goal', 'r.show_details', 'r.show_implementation',
            'r.show_results', 'r.show_recommendations', 'head_decision',
            'SCHOOL_PRINCIPAL', 'executor_name', 'EVIDENCE_SEPARATE_PAGE',
            'reports/partials/report_evidence_print.html',
        ):
            with self.subTest(field=field):
                self.assertIn(field, template)
        self.assertIn('src="{{ evidence.src }}"', evidence)
        self.assertIn('alt="{{ evidence.description }}"', evidence)
        self.assertIn('class="img-fit--{{ evidence.fit_mode }}"', evidence)
        self.assertIn('name="action" value="private_comment_create"', template)
        self.assertIn('name="action" value="private_comment_update"', template)
        self.assertIn('name="action" value="private_comment_delete"', template)
        self.assertIn('{% csrf_token %}', template)
        self.assertIn('<h1 id="reportTitle">', template)
        self.assertIn('<h2 class="section-h">', template)

    def test_pdf_context_uses_gendered_labels_and_counts_evidence(self):
        for index in range(1, 5):
            setattr(
                self.report,
                f"image{index}",
                SimpleUploadedFile(
                    f"evidence-{index}.png",
                    ONE_PIXEL_PNG,
                    content_type="image/png",
                ),
            )
        self.report.save(update_fields=[f"image{index}" for index in range(1, 5)])

        context = build_report_print_context(self.report)

        self.assertEqual(context["EVIDENCE_COUNT"], 4)
        self.assertEqual(context["executor_label"], "المنفّذة")
        self.assertEqual(context["SCHOOL_MANAGER_LABEL"], "مديرة المدرسة")
        self.assertEqual(context["SCHOOL_HEAD_OF_DEPARTMENT_LABEL"], "رئيسة القسم")
        self.assertTrue(context["PDF_IMAGE1_URL"].startswith("data:image/png;base64,"))

    def test_chrome_print_pagination_selects_block_flow_only_when_needed(self):
        template_name = "reports/report_print.html"

        short_html = render_to_string(
            template_name, build_report_print_context(self.report)
        )
        self.assertIn('<main class="page">', short_html)

        self.report.goal = "تقرير تفصيلي " * 80
        long_html = render_to_string(
            template_name, build_report_print_context(self.report)
        )
        self.assertIn('page--natural-print-flow', long_html)

        self.report.goal = ""
        for index in range(1, 5):
            setattr(
                self.report,
                f"image{index}",
                SimpleUploadedFile(
                    f"pagination-evidence-{index}.png",
                    ONE_PIXEL_PNG,
                    content_type="image/png",
                ),
            )
        self.report.save(update_fields=["goal", *[f"image{i}" for i in range(1, 5)]])
        evidence_html = render_to_string(
            template_name, build_report_print_context(self.report)
        )
        self.assertIn('page--dense-evidence page--natural-print-flow', evidence_html)
        self.assertEqual(evidence_html.count('class="img-box '), 4)
        self.assertIn('class="approval-block"', evidence_html)

    def test_print_breaks_gallery_row_not_the_whole_evidence_section(self):
        styles = self._source(
            "reports/templates/reports/partials/report_print_official_styles.html"
        )
        self.assertRegex(styles, r"\.evidence-section\s*\{\s*break-inside:\s*auto;")
        self.assertRegex(styles, r"\.img-box\s*\{[^}]*break-inside:\s*avoid;")
        self.assertRegex(
            styles,
            r"\.images-grid--4 \.img-box:nth-child\(3\)\s*\{[^}]*break-before:\s*page;",
        )
        self.assertRegex(
            styles, r"\.page\.page--natural-print-flow\s*\{\s*display:\s*block;"
        )
        self.assertRegex(
            styles, r"\.images-grid--1 \.img-box img\s*\{\s*height:\s*auto;"
        )
        self.assertRegex(styles, r"\.approval-block\s*\{[^}]*break-inside:\s*avoid;")

    def test_fallback_pdf_is_one_page_for_a_short_report_without_images(self):
        context = build_report_print_context(self.report)

        pdf_bytes = _generate_report_pdf_fallback(self.report, context=context)

        self.assertTrue(pdf_bytes.startswith(b"%PDF-"))
        self.assertEqual(self._pdf_page_count(pdf_bytes), 1)
        self.assertGreater(len(pdf_bytes), 10_000)

    def test_fallback_pdf_keeps_four_images_and_signatures_on_one_page(self):
        for index in range(1, 5):
            setattr(
                self.report,
                f"image{index}",
                SimpleUploadedFile(
                    f"official-evidence-{index}.png",
                    ONE_PIXEL_PNG,
                    content_type="image/png",
                ),
            )
        self.report.save(update_fields=[f"image{index}" for index in range(1, 5)])
        context = build_report_print_context(self.report)

        pdf_bytes = _generate_report_pdf_fallback(self.report, context=context)

        self.assertTrue(pdf_bytes.startswith(b"%PDF-"))
        self.assertEqual(self._pdf_page_count(pdf_bytes), 1)
        self.assertGreater(len(pdf_bytes), 10_000)
