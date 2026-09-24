from __future__ import annotations

from io import BytesIO
import logging

from django.template.loader import render_to_string

from .pdf_report import _fallback_bold_font_path, _fallback_font_path
from .pdf_render import prefer_reportlab_for_official_arabic, render_html_pdf


logger = logging.getLogger(__name__)


def _generate_invoice_pdf_weasy(*, html: str, base_url: str | None) -> bytes:
    return render_html_pdf(html=html, base_url=base_url)


def _generate_invoice_pdf_fallback(context: dict) -> bytes:
    import arabic_reshaper
    from bidi.algorithm import get_display
    from reportlab.lib.colors import HexColor
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas
    from django.contrib.staticfiles import finders

    regular, bold = "InvoiceArabic", "InvoiceArabicBold"
    if regular not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(regular, _fallback_font_path()))
    if bold not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(bold, _fallback_bold_font_path()))

    def rtl(value):
        return get_display(arabic_reshaper.reshape(str(value or "")), base_dir="R")

    output = BytesIO()
    width, height = A4
    pdf = canvas.Canvas(output, pagesize=A4, pageCompression=1)
    green = HexColor("#075c36")
    gold = HexColor("#b9975b")
    ink = HexColor("#17251f")
    muted = HexColor("#66736d")
    line = HexColor("#dce5e0")
    pale = HexColor("#f6faf8")
    right = width - 18 * mm
    left = 18 * mm

    def draw_right(value, x, y, size=9, font=regular, color=ink):
        rendered = rtl(value)
        pdf.setFont(font, size)
        pdf.setFillColor(color)
        pdf.drawString(x - pdfmetrics.stringWidth(rendered, font, size), y, rendered)

    pdf.setFillColor(green)
    pdf.rect(0, height - 25 * mm, width, 25 * mm, fill=1, stroke=0)
    pdf.setFillColor(gold)
    pdf.rect(0, height - 27 * mm, width, 2 * mm, fill=1, stroke=0)
    draw_right("منصة توثيق", right, height - 13 * mm, 17, bold, HexColor("#ffffff"))
    draw_right("فاتورة اشتراك إلكترونية", right, height - 20 * mm, 9, regular, HexColor("#d8eee4"))
    logo_path = finders.find("img/logo1.png")
    if logo_path:
        try:
            pdf.drawImage(
                ImageReader(logo_path),
                left,
                height - 22 * mm,
                width=17 * mm,
                height=17 * mm,
                preserveAspectRatio=True,
                mask="auto",
            )
        except Exception:
            logger.debug("Could not draw the invoice logo", exc_info=True)

    y = height - 39 * mm
    draw_right("فاتورة اشتراك رسمية", right, y, 18, bold, green)
    pdf.setFillColor(HexColor("#e6f5ed"))
    pdf.roundRect(left, y - 3 * mm, 26 * mm, 9 * mm, 3 * mm, fill=1, stroke=0)
    draw_right(context["status_label"], left + 20 * mm, y, 9, bold, green)
    y -= 14 * mm

    info = (
        ("رقم الفاتورة", context["invoice_number"]),
        ("تاريخ الإصدار", context["issued_at"].strftime("%Y-%m-%d %H:%M")),
        ("مرجع الدفع", context["payment_reference"]),
        ("طريقة الدفع", context["payment_method"]),
    )
    box_width = (width - left - (width - right) - 5 * mm) / 2
    for index, (label, value) in enumerate(info):
        col = index % 2
        row = index // 2
        x = right - col * (box_width + 5 * mm)
        top = y - row * 18 * mm
        pdf.setFillColor(pale)
        pdf.setStrokeColor(line)
        pdf.roundRect(x - box_width, top - 11 * mm, box_width, 14 * mm, 2 * mm, fill=1, stroke=1)
        draw_right(label, x - 4 * mm, top - 2 * mm, 7.5, regular, muted)
        draw_right(value, x - 4 * mm, top - 8 * mm, 9, bold, ink)
    y -= 40 * mm

    business = context["business"]
    customer = context["customer"]
    draw_right("مقدم الخدمة", right, y, 10, bold, green)
    draw_right("مفوتر إلى", width / 2 - 3 * mm, y, 10, bold, green)
    y -= 7 * mm
    seller_lines = [business["legal_name"]]
    if business["commercial_registration"]:
        seller_lines.append(f"السجل التجاري: {business['commercial_registration']}")
    elif business["freelance_document_number"]:
        seller_lines.append(f"وثيقة العمل الحر: {business['freelance_document_number']}")
    if business["address"]:
        seller_lines.append(business["address"])
    customer_lines = [
        customer["name"],
        f"{customer.get('identity_label') or 'رمز المدرسة'}: {customer['code']}",
    ]
    if customer["city"]:
        customer_lines.append(customer["city"])
    for index in range(max(len(seller_lines), len(customer_lines))):
        if index < len(seller_lines):
            draw_right(seller_lines[index], right, y, 8.5, bold if index == 0 else regular)
        if index < len(customer_lines):
            draw_right(customer_lines[index], width / 2 - 3 * mm, y, 8.5, bold if index == 0 else regular)
        y -= 5.5 * mm
    y -= 6 * mm

    pdf.setFillColor(green)
    pdf.rect(left, y - 8 * mm, right - left, 9 * mm, fill=1, stroke=0)
    draw_right("الإجمالي", right - 4 * mm, y - 5 * mm, 8, bold, HexColor("#ffffff"))
    draw_right("الوصف", right - 44 * mm, y - 5 * mm, 8, bold, HexColor("#ffffff"))
    y -= 12 * mm
    for item in context["items"]:
        pdf.setStrokeColor(line)
        pdf.line(left, y - 5 * mm, right, y - 5 * mm)
        draw_right(f"{item['amount']:.2f} SAR", right - 4 * mm, y, 8.5, bold)
        draw_right(item["description"], right - 44 * mm, y, 8.5)
        y -= 10 * mm
    y -= 4 * mm

    totals_x = right
    draw_right("إجمالي البنود", totals_x, y, 8.5, regular, muted)
    draw_right(f"{context['subtotal']:.2f} SAR", totals_x - 70 * mm, y, 9, bold)
    y -= 7 * mm
    discount_total = context.get("discount_total") or 0
    if discount_total:
        codes_label = context.get("discount_codes_label") or ""
        label = f"الخصم (كود {codes_label})" if codes_label else "الخصم"
        draw_right(label, totals_x, y, 8.5, regular, muted)
        draw_right(f"-{discount_total:.2f} SAR", totals_x - 70 * mm, y, 9, bold)
        y -= 7 * mm
    pdf.setFillColor(green)
    pdf.roundRect(left, y - 5 * mm, right - left, 12 * mm, 2 * mm, fill=1, stroke=0)
    draw_right("الإجمالي المدفوع", right - 4 * mm, y, 10, bold, HexColor("#ffffff"))
    draw_right(f"{context['total']:.2f} SAR", right - 70 * mm, y, 11, bold, HexColor("#ffffff"))

    pdf.setStrokeColor(line)
    pdf.line(left, 24 * mm, right, 24 * mm)
    draw_right(
        "فاتورة اشتراك إلكترونية صادرة من منصة توثيق ولا تتطلب توقيعاً. هذا المستند ليس فاتورة ضريبية.",
        right,
        17 * mm,
        8,
        regular,
        muted,
    )
    draw_right(f"رقم الفاتورة: {context['invoice_number']}", right, 11 * mm, 7.5, regular, muted)
    pdf.setFont(regular, 7.5)
    pdf.setFillColor(muted)
    pdf.drawString(left, 11 * mm, rtl("صفحة 1 من 1"))
    pdf.save()
    return output.getvalue()


def generate_invoice_pdf(*, context: dict, request=None) -> tuple[bytes, str]:
    html = render_to_string("reports/billing_invoice_pdf.html", context)
    base_url = request.build_absolute_uri("/") if request is not None else None
    if prefer_reportlab_for_official_arabic():
        pdf_bytes = _generate_invoice_pdf_fallback(context)
    else:
        try:
            pdf_bytes = _generate_invoice_pdf_weasy(html=html, base_url=base_url)
        except Exception:
            pdf_bytes = _generate_invoice_pdf_fallback(context)
    filename = f"tawtheeq-invoice-{context['invoice_number']}.pdf"
    return pdf_bytes, filename
