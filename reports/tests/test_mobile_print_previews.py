import re
from pathlib import Path

from django.conf import settings
from django.template.loader import get_template
from django.test import SimpleTestCase


class MobilePrintPreviewRegressionTests(SimpleTestCase):
    templates = (
        "reports/report_print.html",
        "reports/meeting_print.html",
        "reports/ticket_print.html",
        "reports/pdf/leadership_portfolio.html",
    )

    @staticmethod
    def _source(relative_path: str) -> str:
        return (Path(settings.BASE_DIR) / relative_path).read_text(encoding="utf-8")

    def test_preview_templates_compile(self):
        for template_name in self.templates:
            with self.subTest(template=template_name):
                self.assertIsNotNone(get_template(template_name))

    def test_every_browser_preview_declares_the_device_viewport(self):
        for template_name in self.templates:
            source = self._source(f"reports/templates/{template_name}")
            with self.subTest(template=template_name):
                self.assertIn('name="viewport"', source)
                self.assertIn("width=device-width", source)
                self.assertIn("viewport-fit=cover", source)

    def test_report_preview_wraps_toolbar_copy_on_small_screens(self):
        source = self._source("reports/templates/reports/report_print.html")
        screen_styles = self._source("static/css/report-print.css")
        responsive_styles = self._source(
            "reports/templates/reports/partials/report_print_official_styles.html"
        )

        self.assertIn("css/report-print.css", source)
        self.assertIn("white-space: normal;", screen_styles)
        self.assertIn("overflow-wrap: anywhere;", screen_styles)
        self.assertIn("@media (max-width: 40rem)", screen_styles)
        self.assertIn("@media screen and (max-width: 820px)", responsive_styles)
        self.assertIn("@media screen and (max-width: 520px)", responsive_styles)
        self.assertIn("@media print", responsive_styles)

    def test_report_preview_keeps_private_comments_outside_the_official_page(self):
        template = self._source("reports/templates/reports/report_print.html")
        styles = self._source(
            "reports/templates/reports/partials/report_print_official_styles.html"
        )
        screen_styles = styles.split("@media print {", 1)[0]

        self.assertRegex(
            template,
            r'</main>\s*\n\s*{% if show_comments %}\s*\n\s*<section class="section report-comments no-print">',
        )
        self.assertIn("display: flex;", screen_styles)
        self.assertIn("flex-direction: column;", screen_styles)
        self.assertIn(".signature-spacer { flex: 1 1 auto;", screen_styles)

    def test_ticket_preview_stacks_document_content_without_affecting_print(self):
        source = self._source("reports/templates/reports/ticket_print.html")

        self.assertIn("@media screen and (max-width: 1024px)", source)
        self.assertIn("@media screen and (max-width: 720px)", source)
        self.assertIn("@media screen and (max-width: 520px)", source)
        self.assertIn(".meta-table tbody", source)
        self.assertIn("min-height: 44px;", source)
        self.assertIn("@media print", source)

    def test_leadership_preview_has_mobile_cards_and_keeps_a4_print_rules(self):
        source = self._source(
            "reports/templates/reports/pdf/leadership_portfolio.html"
        )

        self.assertIn("@media screen and (max-width:1024px)", source)
        self.assertIn("@media screen and (max-width:720px)", source)
        self.assertIn("@media screen and (max-width:420px)", source)
        self.assertIn(".overview,.grid,.report-images{grid-template-columns:1fr}", source)
        self.assertIn(".footer{position:static", source)
        self.assertIn("@media print", source)
        self.assertIn("@page{size:A4", source)


class ReportSignaturesAtPageBottomTests(SimpleTestCase):
    """التواقيع في نهاية الورقة لا في نهاية النص.

    التقرير القصير كان ينتهي في منتصف الصفحة والتواقيع ملتصقة بآخر سطر فتبدو
    الوثيقة مبتورة. والعلاج كان مكتوباً أصلاً — فاصلٌ مرن داخل صفحة مرنة — لكنه
    كان معطَّلاً بثلاث قواعد في ورقة الأنماط «الرسمية» التي تُحمَّل بعده:
    ``display:block`` على ‎.page‎، و``min-height:auto`` عند الطباعة، وتصفير
    الفاصل بـ``height:0``.

    قِيس بعد الإصلاح في متصفّح حقيقي: التقرير القصير صفحة واحدة والتواقيع
    ملاصقة لأسفلها، والتقرير الممتد لثلاث صفحات يتبع فيه التوقيعُ النصَّ بلا
    صفحة فارغة.
    """

    OFFICIAL_STYLES = "reports/templates/reports/partials/report_print_official_styles.html"

    @staticmethod
    def _source(relative_path: str) -> str:
        return (Path(settings.BASE_DIR) / relative_path).read_text(encoding="utf-8")

    def setUp(self):
        source = self._source(self.OFFICIAL_STYLES)
        # كتلة الطباعة وحدها؛ قواعد الشاشة لا شأن لها بموضع التواقيع.
        self.print_block = source.split("@media print {", 1)[1]

    def test_the_printed_page_is_a_flex_column(self):
        """بغير هذا لا يعمل الفاصل المرن مهما ضُبط."""
        self.assertIn("display: flex;", self.print_block)
        self.assertIn("flex-direction: column;", self.print_block)

    def test_the_spacer_can_still_grow(self):
        self.assertIn(".signature-spacer { flex: 1 1 auto;", self.print_block)
        self.assertNotIn(".signature-spacer { min-height: 0; height: 0; }", self.print_block)
        self.assertIn("min-height: 2mm;", self.print_block)

    def test_the_page_height_matches_the_declared_page_margins(self):
        """ارتفاع الصفحة مشتق من ``@page``؛ فإن تغيّر الهامش ولم يتبعه الارتفاع
        ظهرت صفحة ثانية فارغة أو بقيت التواقيع مرتفعة عن أسفل الورقة."""
        source = self._source(self.OFFICIAL_STYLES)
        margin = re.search(r"@page \{[^}]*?margin:\s*([\d.]+)mm\s+[\d.]+mm\s+([\d.]+)mm", source, re.S)
        self.assertIsNotNone(margin, "تعذّر قراءة هوامش @page")
        top, bottom = margin.group(1), margin.group(2)

        height = re.search(
            r"min-height:\s*calc\(297mm\s*-\s*([\d.]+)mm\s*-\s*([\d.]+)mm\s*-\s*[\d.]+mm\)",
            self.print_block,
        )
        self.assertIsNotNone(height, "ارتفاع صفحة الطباعة غير مشتق من مقاس A4")
        self.assertEqual(
            (height.group(1), height.group(2)),
            (top, bottom),
            "ارتفاع الصفحة لا يطابق هوامش @page المعلنة",
        )


class CircularSignaturesPrintPaginationTests(SimpleTestCase):
    """تقارير المستلمين الطويلة يجب أن تتمدد على أكثر من ورقة في Chrome."""

    @staticmethod
    def _source(relative_path: str) -> str:
        return (Path(settings.BASE_DIR) / relative_path).read_text(encoding="utf-8")

    def setUp(self):
        self.template = self._source(
            "reports/templates/reports/notification_signatures_print.html"
        )
        styles = self._source("static/css/circulars-official.css")
        self.print_block = styles.split("@media print {", 1)[1]

    def test_the_print_document_is_not_limited_to_the_screen_height(self):
        self.assertIn('class="cir-print-root"', self.template)
        self.assertIn("html.cir-print-root, body.cir-print-body", self.print_block)
        self.assertIn("height: auto !important;", self.print_block)
        self.assertIn("overflow: visible !important;", self.print_block)

    def test_the_recipient_table_can_cross_pages_without_splitting_rows(self):
        self.assertIn(
            ".cir-report-table { break-inside: auto; page-break-inside: auto; }",
            self.print_block,
        )
        self.assertIn(
            ".cir-report-table thead { display: table-header-group; }",
            self.print_block,
        )
        self.assertIn(
            ".cir-report-table tr { break-inside: avoid; page-break-inside: avoid; }",
            self.print_block,
        )
