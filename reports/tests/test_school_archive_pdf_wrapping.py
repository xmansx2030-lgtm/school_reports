from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen.canvas import Canvas

from reports.pdf_school_archive import _ArchivePdf, generate_lab_inventory_archive_pdf


class SchoolArchivePdfWrappingTests(SimpleTestCase):
    """Real font measurements and canvas positions; no database or fake PDF."""

    def make_document(self):
        return _ArchivePdf(
            school=SimpleNamespace(name="مدرسة اختبار الأرشيف"),
            title="اختبار القراءة داخل الأرشيف",
            subject="الرموز والنصوص الطويلة",
        )

    def test_long_codes_urls_and_arabic_tokens_fit_actual_inventory_cell(self):
        document = self.make_document()
        width = document.content_width * 12 / sum([20, 12, 15, 14, 10, 9, 12, 16]) - 10
        for value in (
            "LOCAL-QA-LAB-01",
            "ASSET-" + "LONGIDENTIFIER0123456789" * 6,
            "https://school.example.test/archive/" + "document123" * 12,
            "الكهرومغناطيسية" * 12,
            "مُخْتَبَر" * 16,
        ):
            for bold in (False, True):
                with self.subTest(value=value, bold=bold):
                    lines = document._wrap(value, width, size=7.2, bold=bold)
                    self.assertGreater(len(lines), 1)
                    self.assertEqual("".join(lines), value)
                    font = document._bold if bold else document._regular
                    for line in lines:
                        self.assertLessEqual(
                            pdfmetrics.stringWidth(document.rtl(line), font, 7.2),
                            width + 0.001,
                        )

    def test_latin_code_fragments_keep_punctuation_in_reading_order(self):
        document = self.make_document()
        for fragment in ("LOCAL-QA-", "-LAB-01", "https://school.example/"):
            self.assertEqual(document.rtl(fragment), fragment)

    def test_real_inventory_pdf_draws_entire_long_code_inside_its_column(self):
        code = "LOCAL-QA-LAB-01-" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ-" * 5 + "TAIL"
        asset = SimpleNamespace(
            name="المجهر", code=code, lab_kind="science", department=None,
            quantity=2, unit="جهاز", available_quantity=2,
            custodian=SimpleNamespace(name="محضر المختبر"),
            get_lab_kind_display=lambda: "مختبر العلوم",
            get_category_display=lambda: "جهاز",
            get_condition_display=lambda: "سليم",
        )
        content_width = A4[0] - 84
        code_right = A4[0] - 42 - content_width * 20 / 108 - 5
        code_left = A4[0] - 42 - content_width * 32 / 108 + 5
        original_draw = Canvas.drawString
        code_positions = []

        def record_draw(canvas, x, y, text, *args, **kwargs):
            text_width = pdfmetrics.stringWidth(text, canvas._fontname, canvas._fontsize)
            if text.isascii() and abs(x + text_width - code_right) < 0.001:
                code_positions.append((x, x + text_width, y, text))
            return original_draw(canvas, x, y, text, *args, **kwargs)

        with patch.object(Canvas, "drawString", new=record_draw):
            payload = generate_lab_inventory_archive_pdf(
                school=SimpleNamespace(name="مدرسة الاختبار"), assets=[asset], handovers=[],
            )
        self.assertTrue(payload.startswith(b"%PDF-"))
        self.assertGreater(len(code_positions), 1)
        self.assertEqual("".join(row[3] for row in code_positions), code)
        for left, right, _y, _text in code_positions:
            self.assertGreaterEqual(left, code_left - 0.001)
            self.assertLessEqual(right, code_right + 0.001)

    def test_long_multiline_paragraph_preserves_every_line_on_safe_pages(self):
        document = self.make_document()
        expected = [f"ARCHIVE-LINE-{number:04d} حفظ النص الكامل في الأرشيف" for number in range(180)]
        original_draw = document._draw_right
        observed = []

        def record_draw(value, x, y, **kwargs):
            if str(value).startswith("ARCHIVE-LINE-"):
                observed.append((document.page, y, value))
            return original_draw(value, x, y, **kwargs)

        with patch.object(document, "_draw_right", side_effect=record_draw):
            document.paragraph("\n".join(expected))
        self.assertEqual([row[2] for row in observed], expected)
        self.assertGreaterEqual(document.page, 4)
        for _page, y, _text in observed:
            self.assertGreaterEqual(y, document.bottom)
            self.assertLessEqual(y, document.height - 146)
        self.assertTrue(document.finish().startswith(b"%PDF-"))

    def test_experiment_long_field_preserves_lines_and_repeats_label_on_safe_pages(self):
        document = self.make_document()
        expected = [f"FIELD-LINE-{number:04d} خطوة تجربة محفوظة كاملة" for number in range(180)]
        original_draw = document._draw_right
        observed = []
        label_pages = []

        def record_draw(value, x, y, **kwargs):
            if str(value).startswith("FIELD-LINE-"):
                observed.append((document.page, y, value))
            elif value == "خطوات التنفيذ":
                label_pages.append(document.page)
            return original_draw(value, x, y, **kwargs)

        with patch.object(document, "_draw_right", side_effect=record_draw):
            document.field("خطوات التنفيذ", "\n".join(expected))
        self.assertEqual([row[2] for row in observed], expected)
        self.assertGreaterEqual(document.page, 4)
        self.assertEqual(label_pages, sorted({row[0] for row in observed}))
        for _page, y, _text in observed:
            self.assertGreaterEqual(y, document.bottom)
            self.assertLessEqual(y, document.height - 146)
        self.assertTrue(document.finish().startswith(b"%PDF-"))

    def test_assignment_long_table_row_preserves_both_columns_and_repeats_header(self):
        document = self.make_document()
        notes = [f"NOTE-LINE-{number:04d} حفظ ملاحظة التنفيذ" for number in range(180)]
        people = [f"PERSON-LINE-{number:04d}" for number in range(75)]
        widths = [22, 17, 11, 36, 10]
        note_right = document.right - document.content_width * 50 / sum(widths) - 5
        note_left = document.right - document.content_width * 86 / sum(widths) + 5
        original_draw = document._draw_right
        observed_notes = []
        observed_people = []
        header_pages = []

        def record_draw(value, x, y, **kwargs):
            if str(value).startswith("NOTE-LINE-"):
                observed_notes.append((document.page, x, y, value))
            elif str(value).startswith("PERSON-LINE-"):
                observed_people.append(value)
            elif value == "ملاحظة التنفيذ":
                header_pages.append(document.page)
            return original_draw(value, x, y, **kwargs)

        with patch.object(document, "_draw_right", side_effect=record_draw):
            document.table(
                ["المكلَّف", "الحالة", "الإنجاز", "ملاحظة التنفيذ", "الشواهد"],
                [["\n".join(people), "معتمد", "42%", "\n".join(notes), "3"]],
                widths,
            )
        self.assertEqual([row[3] for row in observed_notes], notes)
        self.assertEqual(observed_people, people)
        self.assertGreaterEqual(document.page, 3)
        self.assertEqual(header_pages, sorted({row[0] for row in observed_notes}))
        for _page, x, y, value in observed_notes:
            self.assertAlmostEqual(x, note_right)
            self.assertGreaterEqual(y, document.bottom)
            self.assertLessEqual(y, document.height - 146)
            rendered_width = pdfmetrics.stringWidth(document.rtl(value), document._regular, 7.2)
            self.assertGreaterEqual(x - rendered_width, note_left - 0.001)
        self.assertTrue(document.finish().startswith(b"%PDF-"))
