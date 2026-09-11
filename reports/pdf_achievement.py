# reports/pdf_achievement.py
# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Tuple
import base64

from django.template.loader import render_to_string
from django.utils import timezone
from django.contrib.staticfiles import finders

from .gender_labels import school_gender_template_context
from .models import TeacherAchievementFile, AchievementEvidenceReport, AchievementSection
from .pdf_render import render_html_pdf


def _static_png_as_data_uri(path: str) -> str | None:
    try:
        fpath = finders.find(path)
        if not fpath:
            return None
        with open(fpath, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        return f"data:image/png;base64,{b64}"
    except Exception:
        return None


def achievement_pdf_filename(ach_file: TeacherAchievementFile) -> str:
    """اسم الملف المقترح — مفصولٌ عن التوليد كي يُعرف بلا تصيير.

    المستدعي الذي يفرّغ التوليد إلى العامل يحتاج الاسم في الطلب نفسه ليضعه في
    ``Content-Disposition``، ولا يصحّ أن يشتري اسماً بتصييرِ ملفٍ كامل.
    """
    safe_teacher = (ach_file.teacher_name or "teacher").replace("/", "-")
    year = (ach_file.academic_year or "").strip() or "year"
    return f"achievement_{safe_teacher}_{year}.pdf"


def generate_achievement_pdf(
    *,
    ach_file: TeacherAchievementFile,
    request=None,
    base_url: str | None = None,
) -> Tuple[bytes, str]:
    """Generate an achievement file PDF.

    Returns: (pdf_bytes, suggested_filename)

    Notes:
    - Uses WeasyPrint (system deps installed in Dockerfile).
    - PDF is generated on-demand; caller decides whether to persist it.

    ``request`` صار اختيارياً و``base_url`` بديلاً عنه: هذه الدالة تُنفَّذ الآن
    في عامل الوسائط أيضاً، وهناك لا طلبَ أصلاً. ولم يكن الطلب يُستعمل إلا في
    اشتقاق ``base_url`` — ``render_to_string`` هنا تُستدعى بلا ``request``، فلا
    معالجات سياق تعتمد عليه — فصار الوسيط صريحاً بدل أن يُشتقّ من كائنٍ ضخم
    لا يلزم منه إلا سطر واحد.
    """

    from django.db.models import Prefetch

    ev_reports_qs = AchievementEvidenceReport.objects.select_related(
        "report",
        "report__category",
    ).order_by("id")
    sections = (
        AchievementSection.objects.filter(file=ach_file)
        .prefetch_related("evidence_images", Prefetch("evidence_reports", queryset=ev_reports_qs))
        .order_by("code", "id")
    )

    school = ach_file.school
    primary = (getattr(school, "print_primary_color", None) or "").strip() or "#2563eb"
    ctx = {
        "file": ach_file,
        "school": school,
        "sections": sections,
        "has_evidence_reports": AchievementEvidenceReport.objects.filter(section__file=ach_file).exists(),
        "theme": {"brand": primary},
        "now": timezone.localtime(timezone.now()),
        "for_pdf": True,
        "ministry_logo_src": _static_png_as_data_uri("img/UntiTtled-1.png"),
        **school_gender_template_context(school),
    }

    html = render_to_string("reports/pdf/achievement_file.html", ctx)

    if base_url is None and request is not None:
        try:
            base_url = request.build_absolute_uri("/")
        except Exception:
            base_url = None

    pdf_bytes = render_html_pdf(html=html, base_url=base_url)

    return pdf_bytes, achievement_pdf_filename(ach_file)
