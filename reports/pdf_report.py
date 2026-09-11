# reports/pdf_report.py
# -*- coding: utf-8 -*-
"""توليد PDF لتقرير واحد بإعادة استخدام قالب الطباعة الرسمي (report_print.html).

يُستخدم في تصدير/أرشفة بيانات المدرسة لإدراج كل تقرير كملف PDF مُنسّق
(ترويسة رسمية + جدول بيانات + الوصف + الصور + التواقيع)، بدل الاكتفاء بالصور.
"""
from __future__ import annotations

import base64
import logging
import mimetypes
from io import BytesIO
from pathlib import Path
from typing import Tuple

from django.conf import settings
from django.contrib.staticfiles import finders
from django.template.loader import render_to_string
from django.templatetags.static import static

from .hijri_utils import hijri_date
from .gender_labels import school_gender_labels, school_gender_template_context
from .models import SchoolMembership
from .pdf_render import prefer_reportlab_for_official_arabic, render_html_pdf
from .utils import _resolve_department_for_category, _build_head_decision


logger = logging.getLogger(__name__)


def _file_data_uri(field) -> str:
    """Return a self-contained URL so private report images render in the PDF."""
    if not getattr(field, "name", ""):
        return ""
    try:
        field.open("rb")
        try:
            data = field.read()
        finally:
            field.close()
    except Exception:
        return ""
    content_type = mimetypes.guess_type(field.name)[0] or "image/jpeg"
    return f"data:{content_type};base64,{base64.b64encode(data).decode('ascii')}"


def _school_principal_name(school) -> str:
    if school is None:
        return getattr(settings, "SCHOOL_PRINCIPAL", "") or ""
    try:
        membership = (
            SchoolMembership.objects.select_related("teacher")
            .filter(school=school, role_type=SchoolMembership.RoleType.MANAGER, is_active=True)
            .order_by("-id")
            .first()
        )
        if membership and membership.teacher:
            return getattr(membership.teacher, "name", "") or ""
    except Exception:
        pass
    return getattr(settings, "SCHOOL_PRINCIPAL", "") or ""


def _moe_logo_url() -> str:
    moe_logo_url = (getattr(settings, "MOE_LOGO_URL", "") or "").strip()
    if not moe_logo_url:
        try:
            path = (getattr(settings, "MOE_LOGO_STATIC", "") or "").strip()
            if path:
                moe_logo_url = static(path)
        except Exception:
            moe_logo_url = ""
    if not moe_logo_url:
        moe_logo_url = static("img/UntiTtled-1.png")
    return moe_logo_url


def build_report_evidence_context(report, *, for_pdf: bool = False) -> dict:
    """يبني قائمة شواهد واحدة للطباعة مع fallback للحقول التاريخية."""
    records = []
    try:
        records = list(report.evidences.filter(show_in_print=True).order_by("order", "id"))
    except Exception:
        records = []

    items = []
    if records:
        for index, evidence in enumerate(records, start=1):
            field = evidence.image
            try:
                src = _file_data_uri(field) if for_pdf else field.url
            except Exception:
                src = ""
            if not src:
                continue
            items.append(
                {
                    "number": index,
                    "field": field,
                    "src": src,
                    "description": evidence.description or f"مرفق توثيقي ({index})",
                    "display_size": evidence.display_size,
                    "fit_mode": evidence.fit_mode,
                    "width_px": evidence.width_px,
                    "height_px": evidence.height_px,
                }
            )
    else:
        for index in range(1, 5):
            field = getattr(report, f"image{index}", None)
            if not getattr(field, "name", ""):
                continue
            try:
                src = _file_data_uri(field) if for_pdf else field.url
            except Exception:
                src = ""
            if src:
                items.append(
                    {
                        "number": index,
                        "field": field,
                        "src": src,
                        "description": f"مرفق توثيقي ({index})",
                        "display_size": "auto",
                        "fit_mode": "contain",
                        "width_px": None,
                        "height_px": None,
                    }
                )

    count = len(items)
    # Evidence is always part of the report sheet.  Older rows may still carry
    # ``evidence_page_mode=separate`` from the retired layout option; ignoring
    # that legacy value here guarantees the new one-page contract for existing
    # reports as well as new ones.
    separate = False
    return {
        "EVIDENCE_ITEMS": items,
        "EVIDENCE_COUNT": count,
        "EVIDENCE_LAYOUT": min(count, 4),
        "EVIDENCE_SEPARATE_PAGE": separate,
    }


def build_report_print_context(report) -> dict:
    """يبني نفس سياق report_print.html (لكن بوضع PDF وبدون التعليقات الخاصة)."""
    school = getattr(report, "school", None)

    dept = None
    try:
        dept = _resolve_department_for_category(getattr(report, "category", None), school)
        if dept is not None:
            dept_school = getattr(dept, "school", None)
            if dept_school is not None and dept_school != school:
                dept = None
    except Exception:
        dept = None

    school_stage = ""
    if school is not None:
        try:
            school_stage = getattr(school, "get_stage_display", lambda: "")() or ""
        except Exception:
            school_stage = getattr(school, "stage", "") or ""

    labels = school_gender_labels(school)

    context = {
        "r": report,
        "head_decision": _build_head_decision(dept),
        "SCHOOL_PRINCIPAL": _school_principal_name(school),
        "SCHOOL_NAME": getattr(school, "name", "") if school else getattr(settings, "SCHOOL_NAME", "منصة توثيق"),
        "SCHOOL_STAGE": school_stage,
        "SCHOOL_LOGO_URL": "",
        "MOE_LOGO_URL": _moe_logo_url(),
        **school_gender_template_context(school),
        "executor_label": labels["executor"],
        "show_comments": False,
        "for_pdf": True,
    }
    context.update(build_report_evidence_context(report, for_pdf=True))
    for index in range(1, 5):
        context[f"PDF_IMAGE{index}_URL"] = _file_data_uri(
            getattr(report, f"image{index}", None)
        )
    return context


def _generate_report_pdf_weasy(*, html: str, base_url: str | None) -> bytes:
    return render_html_pdf(html=html, base_url=base_url)


def _fallback_font_path() -> str:
    configured = (getattr(settings, "PDF_FALLBACK_FONT_PATH", "") or "").strip()
    candidates = [
        configured,
        "/usr/share/fonts/opentype/noto/NotoSansArabic-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf",
        "/usr/share/fonts/opentype/noto/NotoKufiArabic-Regular.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/tahoma.ttf",
        "/usr/share/fonts/opentype/noto/NotoNaskhArabic-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise OSError("No Arabic-capable fallback PDF font is installed.")


def _fallback_bold_font_path() -> str:
    configured = (getattr(settings, "PDF_FALLBACK_BOLD_FONT_PATH", "") or "").strip()
    candidates = [
        configured,
        "/usr/share/fonts/opentype/noto/NotoSansArabic-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansArabic-Bold.ttf",
        "/usr/share/fonts/opentype/noto/NotoKufiArabic-Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/tahomabd.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return _fallback_font_path()


def _fallback_logo_path() -> str:
    configured = (getattr(settings, "MOE_LOGO_STATIC", "") or "").strip()
    for static_path in (configured, "img/UntiTtled-1.png"):
        if not static_path:
            continue
        try:
            resolved = finders.find(static_path)
        except Exception:
            resolved = None
        if isinstance(resolved, (list, tuple)):
            resolved = resolved[0] if resolved else None
        if resolved and Path(resolved).is_file():
            return str(resolved)
    return ""


def _fallback_image_bytes(field) -> bytes:
    if not getattr(field, "name", ""):
        return b""
    field.open("rb")
    try:
        return field.read()
    finally:
        field.close()


def _generate_report_pdf_fallback(report, *, context: dict | None = None) -> bytes:
    """Generate the same official report experience without native HTML libraries."""
    import arabic_reshaper
    from bidi.algorithm import get_display
    from PIL import Image, ImageOps
    from reportlab.lib.colors import HexColor, white
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas

    context = context or build_report_print_context(report)
    regular_font = "TawtheeqReportArabic"
    bold_font = "TawtheeqReportArabicBold"
    if regular_font not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(regular_font, _fallback_font_path()))
    if bold_font not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(bold_font, _fallback_bold_font_path()))

    def rtl(value) -> str:
        text = str(value or "-")
        return get_display(arabic_reshaper.reshape(text), base_dir="R")

    def wrap(value, max_width: float, font_size: float, font_name: str = regular_font) -> list[str]:
        logical_lines: list[str] = []
        for paragraph in str(value or "-").splitlines() or ["-"]:
            words = paragraph.split()
            if not words:
                logical_lines.append("")
                continue
            current = words[0]
            for word in words[1:]:
                candidate = f"{current} {word}"
                if pdfmetrics.stringWidth(rtl(candidate), font_name, font_size) <= max_width:
                    current = candidate
                else:
                    logical_lines.append(current)
                    current = word
            logical_lines.append(current)
        return logical_lines

    green = HexColor("#006C35")
    green_dark = HexColor("#073D2B")
    green_soft = HexColor("#EDF6F1")
    gold = HexColor("#B9975B")
    ink = HexColor("#17251F")
    muted = HexColor("#64736C")
    line_color = HexColor("#D6E0DA")

    output = BytesIO()
    page_width, page_height = A4
    pdf = canvas.Canvas(output, pagesize=A4, pageCompression=1, pdfVersion=(1, 4))
    title = getattr(report, "title", "") or "تقرير مدرسي"
    pdf.setTitle(title)
    pdf.setAuthor("منصة توثيق")
    pdf.setSubject("تقرير مدرسي رسمي")

    margin = 42
    bottom_margin = 48
    content_width = page_width - (margin * 2)
    y = page_height - margin
    page_number = 1

    school = getattr(report, "school", None)
    school_name = getattr(school, "name", "") or getattr(
        settings, "SCHOOL_NAME", "المدرسة"
    )

    def draw_right(value, right_x, baseline_y, size=11, *, bold=False, color=ink):
        rendered = rtl(value)
        font_name = bold_font if bold else regular_font
        pdf.setFont(font_name, size)
        pdf.setFillColor(color)
        width = pdfmetrics.stringWidth(rendered, font_name, size)
        pdf.drawString(right_x - width, baseline_y, rendered)

    def draw_center(value, center_x, baseline_y, size=11, *, bold=False, color=ink):
        rendered = rtl(value)
        font_name = bold_font if bold else regular_font
        pdf.setFont(font_name, size)
        pdf.setFillColor(color)
        width = pdfmetrics.stringWidth(rendered, font_name, size)
        pdf.drawString(center_x - (width / 2), baseline_y, rendered)

    def draw_page_footer():
        pdf.saveState()
        pdf.setStrokeColor(line_color)
        pdf.setLineWidth(0.6)
        pdf.line(margin, 32, page_width - margin, 32)
        draw_center(
            f"منصة توثيق  |  صفحة {page_number}",
            page_width / 2,
            20,
            7.3,
            color=muted,
        )
        pdf.restoreState()

    document_date = hijri_date(getattr(report, "report_date", None))
    document_day = getattr(report, "day_name", "") or ""
    document_ref = f"REP-{getattr(report, 'id', 'x')}"
    academic_year = getattr(report, "academic_year", "") or ""
    logo_path = _fallback_logo_path()

    def page_header():
        nonlocal y
        band_y = page_height - 42
        pdf.setFillColor(green)
        pdf.rect(margin, band_y, content_width * 0.72, 5, stroke=0, fill=1)
        pdf.setFillColor(gold)
        pdf.rect(margin + content_width * 0.72, band_y, content_width * 0.28, 5, stroke=0, fill=1)

        draw_right("المملكة العربية السعودية", page_width - margin, page_height - 59, 9.5, bold=True, color=green_dark)
        draw_right("وزارة التعليم", page_width - margin, page_height - 74, 9)
        draw_right(school_name, page_width - margin, page_height - 89, 9, bold=True)

        if logo_path:
            try:
                pdf.drawImage(
                    logo_path,
                    (page_width - 92) / 2,
                    page_height - 103,
                    width=92,
                    height=55,
                    preserveAspectRatio=True,
                    anchor="c",
                    mask="auto",
                )
            except Exception:
                pass

        if academic_year:
            left_right = margin + 142
            draw_right("العام الدراسي", left_right, page_height - 68, 8.2, bold=True, color=green_dark)
            draw_right(f"{academic_year} هـ", left_right, page_height - 86, 10.2, bold=True)

        pdf.setStrokeColor(line_color)
        pdf.setLineWidth(0.7)
        pdf.line(margin, page_height - 112, page_width - margin, page_height - 112)
        y = page_height - 128

    def new_page():
        nonlocal page_number
        draw_page_footer()
        pdf.showPage()
        page_number += 1
        page_header()

    def ensure_space(required):
        if y - required < bottom_margin:
            new_page()

    page_header()

    title_lines = wrap(title, content_width - 34, 16, bold_font)
    identity_height = 35 + (len(title_lines) * 21)
    ensure_space(identity_height + 10)
    pdf.setFillColor(HexColor("#FBFDFC"))
    pdf.setStrokeColor(line_color)
    pdf.roundRect(margin, y - identity_height, content_width, identity_height, 7, stroke=1, fill=1)
    pdf.setFillColor(green)
    pdf.rect(page_width - margin - 5, y - identity_height, 5, identity_height, stroke=0, fill=1)
    title_y = y - 22
    for title_line in title_lines:
        draw_right(title_line, page_width - margin - 16, title_y, 16, bold=True, color=green_dark)
        title_y -= 21
    execution_date = f"{document_day} - {document_date} هـ" if document_day else f"{document_date} هـ"
    tags = f"رقم التقرير: {document_ref}    |    تاريخ التنفيذ: {execution_date}"
    draw_right(tags, page_width - margin - 16, y - identity_height + 12, 7.8, color=muted)
    y -= identity_height + 12

    category = getattr(getattr(report, "category", None), "name", "") or "عام"
    executor = (
        getattr(report, "teacher_display_name", "")
        or getattr(report, "teacher_name", "")
        or getattr(getattr(report, "teacher", None), "name", "")
        or "-"
    )
    beneficiaries = getattr(report, "beneficiaries_count", None)
    beneficiaries = beneficiaries if beneficiaries is not None else "-"
    row_height = 27
    half_width = content_width / 2
    label_width = 72

    def draw_metadata_cell(left, width, label, value):
        pdf.setFillColor(white)
        pdf.rect(left, y - row_height, width, row_height, stroke=1, fill=1)
        pdf.setFillColor(green_soft)
        pdf.rect(left + width - label_width, y - row_height, label_width, row_height, stroke=1, fill=1)
        draw_right(label, left + width - 7, y - 18, 8.2, bold=True, color=green_dark)
        draw_right(value, left + width - label_width - 7, y - 18, 8.5, bold=True)

    pdf.setStrokeColor(line_color)
    ensure_space(row_height)
    if getattr(report, "show_beneficiaries", True):
        draw_metadata_cell(margin + half_width, half_width, "التصنيف", category)
        beneficiaries_label = context.get("SCHOOL_BENEFICIARIES_OBJ_LABEL") or "المستفيدين"
        draw_metadata_cell(margin, half_width, f"عدد {beneficiaries_label}", beneficiaries)
    else:
        draw_metadata_cell(margin, content_width, "التصنيف", category)
    y -= row_height
    y -= 14

    def draw_section_heading(number, heading, *, continued=False):
        nonlocal y
        ensure_space(30)
        pdf.setFillColor(green)
        pdf.circle(page_width - margin - 11, y - 8, 10, stroke=0, fill=1)
        draw_center(number, page_width - margin - 11, y - 11, 6.8, bold=True, color=white)
        suffix = " - تابع" if continued else ""
        draw_right(f"{heading}{suffix}", page_width - margin - 29, y - 12, 10.5, bold=True, color=green_dark)
        y -= 29

    def draw_text_section(number, heading, value, *, reserve_after=0):
        nonlocal y
        lines = wrap(value or "-", content_width - 24, 10.2)
        ensure_space(96 + reserve_after)
        draw_section_heading(number, heading)
        first_chunk = True
        while lines:
            available_lines = max(0, int((y - bottom_margin - 26) // 16.5))
            if available_lines < 2:
                new_page()
                draw_section_heading(number, heading, continued=not first_chunk)
                available_lines = max(2, int((y - bottom_margin - 26) // 16.5))
            chunk = lines[:available_lines]
            lines = lines[available_lines:]
            box_height = max(54, 20 + (len(chunk) * 16.5))
            pdf.setFillColor(white)
            pdf.setStrokeColor(line_color)
            pdf.roundRect(margin, y - box_height, content_width, box_height, 6, stroke=1, fill=1)
            line_y = y - 20
            for logical_line in chunk:
                draw_right(logical_line, page_width - margin - 12, line_y, 10.2)
                line_y -= 16.5
            y -= box_height + 13
            first_chunk = False
            if lines:
                new_page()
                draw_section_heading(number, heading, continued=True)

    text_sections = []
    if getattr(report, "show_goal", False):
        text_sections.append(("الهدف", getattr(report, "goal", "")))
    if getattr(report, "show_details", True):
        text_sections.append(("تفاصيل التقرير", getattr(report, "idea", "")))
    if getattr(report, "show_implementation", False):
        text_sections.append(("آلية التنفيذ", getattr(report, "implementation_method", "")))
    if getattr(report, "show_results", False):
        text_sections.append(("النتائج", getattr(report, "results", "")))
    if getattr(report, "show_recommendations", False):
        text_sections.append(("التوصيات", getattr(report, "recommendations", "")))

    section_number = 0
    has_attached_images = bool(context.get("EVIDENCE_ITEMS"))
    for index, (heading, value) in enumerate(text_sections):
        section_number += 1
        is_last_text_section = index == len(text_sections) - 1
        signature_reserve = 105 if is_last_text_section and not has_attached_images else 0
        draw_text_section(
            f"{section_number:02}",
            heading,
            value,
            reserve_after=signature_reserve,
        )

    images = []
    for item in context.get("EVIDENCE_ITEMS", []):
        field = item.get("field")
        try:
            data = _fallback_image_bytes(field)
        except Exception:
            data = b""
        if data:
            images.append((item.get("number"), data, item.get("description") or "مرفق توثيقي"))

    def prepared_image_reader(data):
        original = BytesIO(data)
        source = Image.open(original)
        orientation = source.getexif().get(274, 1)
        if orientation in (None, 1):
            original.seek(0)
            return ImageReader(original)

        # Correct camera orientation only when needed. PNG keeps the decoded pixels
        # lossless instead of applying another lossy JPEG compression pass.
        source = ImageOps.exif_transpose(source)
        corrected = BytesIO()
        source.save(corrected, format="PNG", optimize=True)
        corrected.seek(0)
        return ImageReader(corrected)

    if images:
        section_number += 1
        evidence_section_number = f"{section_number:02}"
        gap = 10
        rows = (len(images) + 1) // 2
        box_height = 88 if rows >= 2 else 126
        row_height = box_height + 9
        signature_reserve = 105
        first_row_reserve = signature_reserve if rows == 1 else 0
        if y - (29 + row_height + first_row_reserve) < bottom_margin:
            new_page()
        draw_section_heading(evidence_section_number, "المرفقات والشواهد")
        for offset in range(0, len(images), 2):
            row = images[offset : offset + 2]
            if y - row_height - (signature_reserve if offset + 2 >= len(images) else 0) < bottom_margin:
                new_page()
                draw_section_heading(evidence_section_number, "المرفقات والشواهد", continued=True)
            if len(row) == 1:
                box_width = content_width * 0.64
                row_positions = [margin + ((content_width - box_width) / 2)]
            else:
                box_width = (content_width - gap) / 2
                row_positions = [page_width - margin - box_width, margin]
            for column, (_index, data, description) in enumerate(row):
                left = row_positions[column]
                pdf.setFillColor(white)
                pdf.setStrokeColor(line_color)
                pdf.roundRect(left, y - box_height, box_width, box_height, 6, stroke=1, fill=1)
                try:
                    reader = prepared_image_reader(data)
                    img_width, img_height = reader.getSize()
                    scale = min(
                        (box_width - 16) / max(img_width, 1),
                        (box_height - 32) / max(img_height, 1),
                    )
                    draw_width = img_width * scale
                    draw_height = img_height * scale
                    pdf.drawImage(
                        reader,
                        left + ((box_width - draw_width) / 2),
                        y - box_height + 24 + ((box_height - 30 - draw_height) / 2),
                        width=draw_width,
                        height=draw_height,
                        preserveAspectRatio=True,
                        mask="auto",
                    )
                except Exception:
                    draw_center("تعذرت قراءة الصورة", left + (box_width / 2), y - 58, 9, color=muted)
                draw_right(description, left + box_width - 8, y - box_height + 9, 7.5, bold=True, color=muted)
            y -= row_height
        y -= 5

    ensure_space(105)
    # Keep approvals in the document's closing zone when the fallback renderer
    # is used.  ``y`` decreases as content is drawn, so taking the smaller value
    # moves a short report's signatures down without ever overlapping longer
    # content.
    signature_anchor_y = bottom_margin + 112
    y = min(y, signature_anchor_y)
    pdf.setStrokeColor(line_color)
    pdf.line(margin, y, page_width - margin, y)
    draw_right("الاعتمادات والتوقيعات", page_width - margin, y - 16, 10.5, bold=True, color=green_dark)
    y -= 42

    principal = context.get("SCHOOL_PRINCIPAL") or "........................"
    roles = [(context.get("executor_label") or "المنفّذ", executor)]
    head_decision = context.get("head_decision") or {}
    if head_decision and not head_decision.get("no_render"):
        head_name = "........................"
        if head_decision.get("single"):
            head_name = head_decision.get("name") or head_name
        elif head_decision.get("multi_dept"):
            head_name = head_decision.get("dept_name") or head_name
        roles.append((context.get("SCHOOL_HEAD_OF_DEPARTMENT_LABEL") or "رئيس القسم", head_name))
    roles.append((context.get("SCHOOL_MANAGER_LABEL") or "مدير المدرسة", principal))

    column_width = content_width / len(roles)
    for index, (role_label, person_name) in enumerate(roles):
        center_x = page_width - margin - (column_width * index) - (column_width / 2)
        draw_center(role_label, center_x, y, 9.2, bold=True, color=green_dark)
        line_y = y - 44
        pdf.setStrokeColor(HexColor("#738079"))
        pdf.setLineWidth(0.7)
        pdf.line(center_x - (column_width * 0.34), line_y, center_x + (column_width * 0.34), line_y)
        draw_center(f"الاسم: {person_name}", center_x, line_y - 13, 7.7, color=ink)

    draw_page_footer()
    pdf.save()
    return output.getvalue()


def generate_report_pdf(*, request, report) -> Tuple[bytes, str]:
    """يولّد PDF لتقرير ويعيد (bytes, filename).

    - يعيد استخدام قالب الطباعة الرسمي لضمان تطابق الشكل مع طباعة المتصفح.
    - WeasyPrint يطبّق أنماط ``@media print`` فيخفي شريط الأدوات والتعليقات تلقائيًا.
    """
    context = build_report_print_context(report)
    html = render_to_string("reports/report_print.html", context)

    base_url = None
    try:
        base_url = request.build_absolute_uri("/")
    except Exception:
        base_url = None

    if prefer_reportlab_for_official_arabic():
        pdf_bytes = _generate_report_pdf_fallback(report, context=context)
    else:
        try:
            pdf_bytes = _generate_report_pdf_weasy(html=html, base_url=base_url)
        except Exception as exc:
            logger.warning(
                "WeasyPrint report rendering failed; using official ReportLab fallback report_id=%s error=%s",
                getattr(report, "id", None),
                type(exc).__name__,
            )
            pdf_bytes = _generate_report_pdf_fallback(report, context=context)
    filename = f"report_{getattr(report, 'id', 'x')}.pdf"
    return pdf_bytes, filename
