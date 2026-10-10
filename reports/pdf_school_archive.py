from __future__ import annotations

"""Deterministic, Arabic-safe PDFs for school-work archive records.

These PDFs deliberately use ReportLab rather than the HTML renderer.  Archive
creation must remain available on media workers that do not have the native
Pango stack, and a historical package must not become partial merely because a
print-only dependency is unavailable.
"""

from io import BytesIO
from unicodedata import category

from django.utils import timezone

from .pdf_report import _fallback_bold_font_path, _fallback_font_path


class _ArchivePdf:
    def __init__(self, *, school, title: str, subject: str) -> None:
        import arabic_reshaper
        from bidi.algorithm import get_display
        from reportlab.lib.colors import HexColor
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.pdfgen import canvas

        self._reshape = arabic_reshaper.reshape
        self._bidi = get_display
        self._pdfmetrics = pdfmetrics
        self._A4 = A4
        self._green = HexColor("#075c36")
        self._gold = HexColor("#b9975b")
        self._ink = HexColor("#17251f")
        self._muted = HexColor("#66736d")
        self._line = HexColor("#dce5e0")
        self._pale = HexColor("#f6faf8")
        self._white = HexColor("#ffffff")
        self._regular = "TawtheeqArchiveArabic"
        self._bold = "TawtheeqArchiveArabicBold"
        if self._regular not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(self._regular, _fallback_font_path()))
        if self._bold not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(self._bold, _fallback_bold_font_path()))

        self.output = BytesIO()
        self.width, self.height = A4
        self.pdf = canvas.Canvas(
            self.output,
            pagesize=A4,
            pageCompression=1,
            pdfVersion=(1, 4),
        )
        self.pdf.setTitle(title)
        self.pdf.setAuthor("منصة توثيق")
        self.pdf.setSubject(subject)
        self.school = school
        self.title = title
        self.subject = subject
        self.margin = 42
        self.right = self.width - self.margin
        self.content_width = self.width - (2 * self.margin)
        self.bottom = 52
        self.page = 0
        self.y = 0
        self._new_page()

    def rtl(self, value) -> str:
        text = str(value or "—")
        # A wrapped Latin identifier may end in punctuation. Keep its logical
        # order instead of moving that punctuation to the opposite line edge.
        return text if text.isascii() else self._bidi(self._reshape(text), base_dir="R")

    def _draw_right(self, value, x, y, *, size=9, bold=False, color=None) -> None:
        font = self._bold if bold else self._regular
        rendered = self.rtl(value)
        self.pdf.setFont(font, size)
        self.pdf.setFillColor(color or self._ink)
        width = self._pdfmetrics.stringWidth(rendered, font, size)
        self.pdf.drawString(x - width, y, rendered)

    def _wrap(self, value, width, *, size=9, bold=False) -> list[str]:
        font = self._bold if bold else self._regular

        def fits(text: str) -> bool:
            return self._pdfmetrics.stringWidth(self.rtl(text), font, size) <= width

        result: list[str] = []
        for paragraph in str(value or "—").splitlines() or ["—"]:
            words = paragraph.split()
            if not words:
                result.append("")
                continue
            current = ""
            for word in words:
                candidate = f"{current} {word}" if current else word
                if fits(candidate):
                    current = candidate
                    continue
                if current:
                    result.append(current)
                if fits(word):
                    current = word
                    continue

                # Whitespace-only wrapping leaves codes, URLs and unspaced
                # Arabic text outside table borders. Split oversized tokens
                # using the exact rendered font width, retaining their marks.
                clusters: list[str] = []
                for character in word:
                    if clusters and category(character) in {"Mn", "Mc", "Me"}:
                        clusters[-1] += character
                    else:
                        clusters.append(character)
                current = ""
                for cluster in clusters:
                    candidate = current + cluster
                    if current and not fits(candidate):
                        result.append(current)
                        current = cluster
                    else:
                        current = candidate
            result.append(current)
        return result or ["—"]

    def _new_page(self) -> None:
        if self.page:
            self._footer()
            self.pdf.showPage()
        self.page += 1
        self.pdf.setFillColor(self._green)
        self.pdf.rect(0, self.height - 72, self.width, 72, fill=1, stroke=0)
        self.pdf.setFillColor(self._gold)
        self.pdf.rect(0, self.height - 77, self.width, 5, fill=1, stroke=0)
        self._draw_right("منصة توثيق", self.right, self.height - 30, size=16, bold=True, color=self._white)
        self._draw_right(
            getattr(self.school, "name", "") or "المدرسة",
            self.right,
            self.height - 50,
            size=9,
            color=self._white,
        )
        self._draw_right(self.title, self.right, self.height - 104, size=17, bold=True, color=self._green)
        self._draw_right(self.subject, self.right, self.height - 122, size=8, color=self._muted)
        self.y = self.height - 146

    def _footer(self) -> None:
        self.pdf.setStrokeColor(self._line)
        self.pdf.line(self.margin, 36, self.right, 36)
        self._draw_right(
            f"نسخة أرشيفية للقراءة فقط · صفحة {self.page}",
            self.right,
            22,
            size=7.5,
            color=self._muted,
        )
        generated = timezone.localtime().strftime("%Y-%m-%d %H:%M")
        self.pdf.setFont(self._regular, 7.5)
        self.pdf.setFillColor(self._muted)
        self.pdf.drawString(self.margin, 22, generated)

    def _ensure(self, needed: float) -> None:
        if self.y - needed < self.bottom:
            self._new_page()

    def section(self, title: str) -> None:
        self._ensure(32)
        self.pdf.setFillColor(self._green)
        self.pdf.roundRect(self.margin, self.y - 20, self.content_width, 24, 3, fill=1, stroke=0)
        self._draw_right(title, self.right - 8, self.y - 12, size=10, bold=True, color=self._white)
        self.y -= 34

    def field(self, label: str, value) -> None:
        lines = self._wrap(value, self.content_width - 125, size=8.5)
        height = max(24, 10 + len(lines) * 13)
        if height + 5 <= self.height - 146 - self.bottom:
            self._ensure(height + 5)
        offset = 0
        while offset < len(lines):
            self._ensure(29)
            capacity = max(1, int((self.y - self.bottom - 15) / 13))
            page_lines = lines[offset:offset + capacity]
            height = max(24, 10 + len(page_lines) * 13)
            self.pdf.setFillColor(self._pale)
            self.pdf.setStrokeColor(self._line)
            self.pdf.roundRect(self.margin, self.y - height + 5, self.content_width, height, 3, fill=1, stroke=1)
            self._draw_right(label, self.right - 8, self.y - 10, size=8.5, bold=True, color=self._green)
            text_right = self.right - 118
            line_y = self.y - 10
            for line in page_lines:
                self._draw_right(line or " ", text_right, line_y, size=8.5)
                line_y -= 13
            self.y -= height + 6
            offset += len(page_lines)
            if offset < len(lines):
                self._new_page()

    def paragraph(self, value, *, size=9) -> None:
        lines = self._wrap(value, self.content_width - 16, size=size)
        needed = 12 + len(lines) * 14
        # Keep short paragraphs together; a paragraph taller than one page
        # must continue on subsequent pages instead of drawing below the footer.
        if needed <= self.height - 146 - self.bottom:
            self._ensure(needed)
        for line in lines:
            self._ensure(14)
            self._draw_right(line or " ", self.right - 8, self.y, size=size)
            self.y -= 14
        self.y -= 5

    def table(self, headers: list[str], rows: list[list[object]], widths: list[float]) -> None:
        if not rows:
            self.paragraph("لا توجد سجلات.")
            return
        total = sum(widths)
        scaled = [self.content_width * value / total for value in widths]

        wrapped_headers = [
            self._wrap(value, scaled[i] - 10, size=7.2, bold=True)
            for i, value in enumerate(headers)
        ]
        header_height = max(22, 8 + max(len(lines) for lines in wrapped_headers) * 10)

        def draw_fragment(wrapped, *, header=False) -> None:
            row_height = max(22, 8 + max(len(lines) for lines in wrapped) * 10)
            x = self.right
            for index, lines in enumerate(wrapped):
                width = scaled[index]
                self.pdf.setFillColor(self._green if header else self._pale)
                self.pdf.setStrokeColor(self._line)
                self.pdf.rect(x - width, self.y - row_height + 5, width, row_height, fill=1, stroke=1)
                line_y = self.y - 9
                for line in lines:
                    self._draw_right(
                        line or " ",
                        x - 5,
                        line_y,
                        size=7.2,
                        bold=header,
                        color=self._white if header else self._ink,
                    )
                    line_y -= 10
                x -= width
            self.y -= row_height

        def continue_table_page() -> None:
            self._new_page()
            draw_fragment(wrapped_headers, header=True)

        self._ensure(header_height + 24)
        draw_fragment(wrapped_headers, header=True)
        for row in rows:
            wrapped = [self._wrap(value, scaled[i] - 10, size=7.2) for i, value in enumerate(row)]
            line_count = max(len(lines) for lines in wrapped)
            row_height = max(22, 8 + line_count * 10)
            page_row_space = self.height - 146 - header_height - self.bottom
            # Keep ordinary rows together. Oversized rows continue as aligned
            # column fragments with a repeated table header on every new page.
            if row_height + 2 <= page_row_space and self.y - (row_height + 2) < self.bottom:
                continue_table_page()
            offset = 0
            while offset < line_count:
                if self.y - 24 < self.bottom:
                    continue_table_page()
                capacity = max(1, int((self.y - self.bottom - 10) / 10))
                count = min(line_count - offset, capacity)
                draw_fragment([lines[offset:offset + count] for lines in wrapped])
                offset += count
                if offset < line_count:
                    continue_table_page()
        self.y -= 8

    def finish(self) -> bytes:
        self._footer()
        self.pdf.save()
        return self.output.getvalue()


def generate_assignment_archive_pdf(assignment, *, school=None) -> bytes:
    target_qs = assignment.targets.select_related("assignee", "school")
    if school is not None:
        from django.db.models import Q

        target_qs = target_qs.filter(
            Q(school=school) | Q(school__isnull=True, assignment__school=school)
        )
    targets = list(
        target_qs
        .prefetch_related("evidence__uploaded_by")
        .order_by("assignee__name", "id")
    )
    document = _ArchivePdf(
        school=school or assignment.school or (targets[0].school if targets else None),
        title=f"تكليف: {assignment.title}",
        subject="سجل التكليف والمكلَّفين والتنفيذ والشواهد",
    )
    document.section("بيانات التكليف")
    document.field("المكلِّف", assignment.issuer_name or getattr(assignment.issuer, "name", ""))
    document.field("المصدر", assignment.get_source_display())
    document.field("الأولوية", assignment.get_priority_display())
    document.field("موعد التسليم", timezone.localtime(assignment.due_at).strftime("%Y-%m-%d %H:%M"))
    document.field("القسم", getattr(assignment.department, "name", "") or "—")
    document.field("المطلوب", assignment.description or assignment.title)
    document.field("اشتراط الشواهد", f"{'نعم' if assignment.requires_evidence else 'لا'} · الحد الأدنى {assignment.min_evidence_count}")
    document.field("حالة السجل", f"ملغى: {'نعم' if assignment.is_cancelled else 'لا'} · {assignment.cancel_reason or '—'}")
    document.section("تنفيذ المكلَّفين")
    document.table(
        ["المكلَّف", "الحالة", "الإنجاز", "ملاحظة التنفيذ", "الشواهد"],
        [
            [
                getattr(target.assignee, "name", "") or "—",
                target.get_approval_state_display(),
                f"{target.progress_percent}%",
                target.progress_note or target.clarification_note or "—",
                str(target.evidence_count),
            ]
            for target in targets
        ],
        [22, 17, 11, 36, 10],
    )
    for target in targets:
        evidence = list(target.evidence.all())
        if not evidence:
            continue
        document.section(f"شواهد {getattr(target.assignee, 'name', '') or 'المكلَّف'}")
        document.table(
            ["الوصف", "اسم الملف", "رفعه", "التاريخ"],
            [
                [
                    item.note or "—",
                    getattr(item.file, "name", "") or "—",
                    getattr(item.uploaded_by, "name", "") or "—",
                    timezone.localtime(item.created_at).strftime("%Y-%m-%d %H:%M"),
                ]
                for item in evidence
            ],
            [30, 35, 20, 20],
        )
    return document.finish()


def generate_plan_archive_pdf(plan) -> bytes:
    goals = list(plan.goals.all().order_by("order", "id"))
    tasks = list(
        plan.tasks.select_related("goal", "responsible", "department", "assignment")
        .prefetch_related("assignment__targets")
        .order_by("order", "id")
    )
    document = _ArchivePdf(
        school=plan.school,
        title=f"خطة: {plan.title}",
        subject="الخطة وأهدافها ومؤشراتها ومهامها وحالة التنفيذ",
    )
    document.section("بيانات الخطة")
    document.field("مُعِدّ الخطة", plan.owner_name or getattr(plan.owner, "name", ""))
    document.field("السنة الدراسية", plan.academic_year or "—")
    document.field("المرحلة", plan.get_stage_display())
    document.field("الاعتماد", plan.get_approval_state_display())
    document.field("المدة", f"{plan.starts_on or '—'} إلى {plan.ends_on or '—'}")
    document.field("نسبة الإنجاز", f"{plan.progress_percent}%")
    document.field("الوصف", plan.description or "—")
    document.section("الأهداف ومؤشرات القياس")
    document.table(
        ["الهدف", "المؤشر", "المستهدف", "الإنجاز"],
        [[goal.title, goal.indicator or "—", goal.target or "—", f"{goal.progress_percent}%"] for goal in goals],
        [35, 30, 22, 13],
    )
    document.section("مهام الخطة")
    document.table(
        ["المهمة", "الهدف", "المسؤول", "الموعد", "الحالة", "التفصيل"],
        [
            [
                task.title,
                getattr(task.goal, "title", "") or "—",
                getattr(task.responsible, "name", "") or "—",
                timezone.localtime(task.due_at).strftime("%Y-%m-%d %H:%M") if task.due_at else "—",
                {"done": "منجزة", "late": "متأخرة", "running": "قيد التنفيذ", "untracked": "غير محولة لتكليف"}.get(task.state, task.state),
                task.description or "—",
            ]
            for task in tasks
        ],
        [23, 19, 15, 15, 13, 25],
    )
    return document.finish()


def generate_initiative_archive_pdf(initiative) -> bytes:
    document = _ArchivePdf(
        school=initiative.school,
        title=f"مبادرة: {initiative.title}",
        subject="سجل المبادرة وفكرتها وأثرها وقرار اعتمادها",
    )
    document.section("بيانات المبادرة")
    document.field("مقدم المبادرة", initiative.teacher_name or getattr(initiative.teacher, "name", ""))
    document.field("الخطة المرتبطة", getattr(initiative.plan, "title", "") or "—")
    document.field("حالة الاعتماد", initiative.get_approval_state_display())
    document.field("ممارسة ناجحة", "نعم" if initiative.is_best_practice else "لا")
    document.field("مشاركة مع المجموعة", "نعم" if initiative.is_shared else "لا")
    document.field("تاريخ المشاركة", timezone.localtime(initiative.shared_at).strftime("%Y-%m-%d %H:%M") if initiative.shared_at else "—")
    document.field("الفكرة والأثر", initiative.summary or "—")
    return document.finish()


def generate_lab_inventory_archive_pdf(*, school, assets, handovers) -> bytes:
    document = _ArchivePdf(
        school=school,
        title="كشف عهدة المختبر وحركاتها",
        subject="لقطة كاملة للأصول والكميات والحالة والتسليم والإرجاع",
    )
    document.section("أصول وعهد المختبر")
    document.table(
        ["الصنف", "الرقم", "المختبر", "النوع", "الكمية", "المتاح", "الحالة", "المسؤول"],
        [
            [
                asset.name,
                asset.code or "—",
                asset.get_lab_kind_display() if asset.lab_kind else getattr(asset.department, "name", "") or "—",
                asset.get_category_display(),
                f"{asset.quantity} {asset.unit or ''}".strip(),
                str(asset.available_quantity),
                asset.get_condition_display(),
                getattr(asset.custodian, "name", "") or "—",
            ]
            for asset in assets
        ],
        [20, 12, 15, 14, 10, 9, 12, 16],
    )
    document.section("سجل حركات العهدة")
    document.table(
        ["الصنف", "الحركة", "المستلم", "الكمية", "التاريخ", "سجلها", "الملاحظة"],
        [
            [
                getattr(handover.asset, "name", "") or "—",
                handover.get_direction_display(),
                handover.person_name or getattr(handover.person, "name", "") or "—",
                str(handover.quantity),
                timezone.localtime(handover.happened_at).strftime("%Y-%m-%d %H:%M"),
                getattr(handover.recorded_by, "name", "") or "—",
                handover.note or "—",
            ]
            for handover in handovers
        ],
        [20, 11, 17, 9, 16, 14, 23],
    )
    return document.finish()


def generate_lab_experiment_archive_pdf(experiment) -> bytes:
    assets = list(experiment.assets.filter(school=experiment.school))
    document = _ArchivePdf(
        school=experiment.school,
        title=f"تجربة مختبر: {experiment.title or 'بلا عنوان'}",
        subject="سجل التجربة وأهدافها وخطواتها وموادها وشواهدها",
    )
    document.section("بيانات التجربة")
    document.field("المختبر", experiment.get_lab_kind_display() if experiment.lab_kind else getattr(experiment.department, "name", "") or "—")
    document.field("محضر المختبر", getattr(experiment.recorder, "name", "") or "—")
    document.field("المعلم الطالب", getattr(experiment.requested_by, "name", "") or "—")
    document.field("تاريخ التنفيذ", experiment.experiment_date or "—")
    document.field("المادة والصف", f"{experiment.subject or '—'} · {experiment.class_name or '—'}")
    document.field("عدد الطلاب", experiment.students_count)
    document.field("حالة الاعتماد", experiment.get_approval_state_display())
    document.field("الأهداف", experiment.objectives or "—")
    document.field("خطوات التنفيذ", experiment.procedure or "—")
    document.field("المواد والأدوات", experiment.materials_note or "—")
    document.field("إجراءات السلامة", experiment.safety_notes or "—")
    document.field("أصناف العهدة المستخدمة", "، ".join(asset.name for asset in assets) or "—")
    document.field("التقرير المرتبط", f"#{experiment.report_id}" if experiment.report_id else "—")
    return document.finish()
