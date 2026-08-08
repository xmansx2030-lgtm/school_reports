from __future__ import annotations

import base64

from django.contrib.staticfiles import finders
from django.templatetags.static import static
from django.template.loader import render_to_string
from django.utils.safestring import mark_safe


def generate_user_guide_pdf(*, base_url: str) -> bytes:
    guide_html = render_to_string(
        "reports/partials/user_guide_content.html",
        {"pdf_mode": True},
    )
    logo_src = None
    fpath = finders.find("img/logo1.png")
    if fpath:
        try:
            with open(fpath, "rb") as logo_file:
                logo_src = "data:image/png;base64," + base64.b64encode(logo_file.read()).decode("ascii")
        except Exception:
            logo_src = None
    if not logo_src:
        logo_src = base_url.rstrip("/") + static("img/logo1.png")

    html = render_to_string(
        "reports/user_guide_pdf.html",
        {
            "title": "دليل استخدام منصة توثيق",
            "logo_url": logo_src,
            "guide_html": mark_safe(guide_html),  # noqa: S308 - repository-owned template
        },
    )
    from weasyprint import HTML

    return HTML(string=html, base_url=base_url).write_pdf()
