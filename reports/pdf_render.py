from __future__ import annotations

from typing import Any

from django.conf import settings


WEASYPRINT_PDF_OPTIONS: dict[str, bool] = {
    # WeasyPrint font subsetting can corrupt Arabic text with a variable webfont.
    # Embedding the complete static system font preserves the shaped glyphs and
    # the PDF text map used by viewers, search, and copy/paste.
    "full_fonts": True,
    "hinting": True,
}


def prefer_reportlab_for_official_arabic() -> bool:
    """Use the deterministic Arabic renderer for official one-record PDFs."""

    renderer = str(
        getattr(settings, "PDF_ARABIC_RENDERER", "reportlab") or "reportlab"
    ).strip().lower()
    return renderer != "weasyprint"


def _weasy_html(*, html: str, base_url: str | None):
    # Kept lazy so management commands still work on hosts without native
    # WeasyPrint libraries, and so the renderer contract can be tested alone.
    from weasyprint import HTML

    return HTML(string=html, base_url=base_url)


def render_html_pdf(
    *, html: str, base_url: str | None, **options: Any
) -> bytes:
    """Render HTML as a validated PDF with Arabic-safe font embedding."""

    pdf_options: dict[str, Any] = {**WEASYPRINT_PDF_OPTIONS, **options}
    payload = _weasy_html(html=html, base_url=base_url).write_pdf(**pdf_options)
    if not payload.startswith(b"%PDF-"):
        raise ValueError("WeasyPrint returned an invalid PDF")
    return payload
