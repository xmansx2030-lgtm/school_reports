"""Teacher-owned achievement portfolio, with no school approval workflow."""

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods
from django_ratelimit.decorators import ratelimit

from reports.model_parts.achievements import AchievementSection

from .forms import PersonalEvidenceForm, clean_academic_year
from .portfolio_forms import PersonalPortfolioProfileForm
from .models import (
    PersonalAcademicYear, PersonalEvidence, PersonalPortfolioEvidence,
    PersonalPortfolioReport, PersonalPortfolioSection, PersonalReport,
    PersonalWorkspace,
)
from .services import ensure_writable_personal_year
from .views import workspace_required


def _posted_id(request, name):
    raw = request.POST.get(name) or ""
    if not raw.isdecimal():
        raise Http404
    return int(raw)


def _portfolio_years(workspace):
    values = (
        set(workspace.academic_years.values_list("value", flat=True))
        | set(workspace.reports.values_list("academic_year", flat=True))
        | set(workspace.evidence.values_list("academic_year", flat=True))
        | set(workspace.initiatives.values_list("academic_year", flat=True))
        | set(workspace.portfolio_sections.values_list("academic_year", flat=True))
    )
    if workspace.current_academic_year:
        values.add(workspace.current_academic_year)
    return sorted(filter(None, values), reverse=True)


def _sections(workspace, year):
    """Show all school portfolio axes without writing rows on a GET."""
    rows = PersonalPortfolioSection.objects.filter(
        workspace=workspace, academic_year=year
    ).prefetch_related("linked_reports__report", "linked_evidence__evidence")
    by_code = {row.code: row for row in rows}
    sections = []
    for code, title in AchievementSection.Code.choices:
        row = by_code.get(code)
        reports = list(row.linked_reports.all()) if row else []
        evidence = list(row.linked_evidence.all()) if row else []
        sections.append({
            "code": code, "title": title, "notes": row.teacher_notes if row else "",
            "reports": reports, "evidence": evidence,
            "documented": bool(row and (row.teacher_notes.strip() or reports or evidence)),
        })
    return sections


def _context(workspace, year):
    sections = _sections(workspace, year) if year else []
    documented = sum(1 for section in sections if section["documented"])
    year_record = PersonalAcademicYear.objects.filter(workspace=workspace, value=year).first() if year else None
    return {
        "workspace": workspace, "years": _portfolio_years(workspace), "year": year,
        "year_record": year_record, "general_form": PersonalPortfolioProfileForm(instance=year_record),
        "sections": sections, "documented_count": documented,
        "progress_percent": round(documented * 100 / len(AchievementSection.Code.choices)),
        "reports": workspace.reports.filter(academic_year=year) if year else workspace.reports.none(),
        "evidence": workspace.evidence.filter(academic_year=year) if year else workspace.evidence.none(),
        "initiatives": workspace.initiatives.filter(academic_year=year) if year else workspace.initiatives.none(),
        "is_archived": bool(year_record and year_record.archived_at),
    }


@workspace_required
@never_cache
@ratelimit(key="user", rate="60/h", method="POST", block=True)
@require_http_methods(["GET", "POST"])
def portfolio(request):
    workspace = request.personal_workspace
    years = _portfolio_years(workspace)
    selected_year = ((request.POST.get("year") if request.method == "POST" else request.GET.get("year")) or "").strip()
    year = selected_year if selected_year in years else (workspace.current_academic_year or (years[0] if years else ""))
    if request.method == "POST":
        if selected_year not in years:
            raise Http404
        if not request.personal_subscription.is_current:
            messages.error(request, "يلزم اشتراك نشط لتعديل ملف الإنجاز.")
            return redirect("personal:portfolio")
        if PersonalAcademicYear.objects.filter(
            workspace=workspace, value=year, archived_at__isnull=False
        ).exists():
            messages.error(request, "هذه السنة مؤرشفة؛ أعد فتحها قبل تعديل ملف الإنجاز.")
            return redirect("personal:portfolio")
        try:
            clean_academic_year(year)
        except ValidationError:
            raise Http404 from None
        action = request.POST.get("action") or ""
        if action == "save_general":
            with transaction.atomic():
                locked = PersonalWorkspace.objects.select_for_update().get(pk=workspace.pk)
                try:
                    year_record = ensure_writable_personal_year(locked, year)
                except ValidationError:
                    messages.error(request, "هذه السنة مؤرشفة؛ أعد فتحها قبل التعديل.")
                    return redirect("personal:portfolio")
                general_form = PersonalPortfolioProfileForm(request.POST, instance=year_record)
                if general_form.is_valid():
                    general_form.save()
                    messages.success(request, "حُفظت البيانات المهنية لملف السنة.")
                    return redirect(f"{request.path}?year={year}#personalIdentity")
            context = _context(workspace, year)
            context["general_form"] = general_form
            context["can_edit"] = True
            return render(request, "personal/portfolio.html", context, status=400)
        try:
            code = int(request.POST.get("section_code", ""))
        except (TypeError, ValueError):
            raise Http404 from None
        if code not in AchievementSection.Code.values:
            raise Http404
        if action not in {"save_notes", "link_report", "unlink_report", "link_evidence", "unlink_evidence", "upload_evidence"}:
            raise Http404
        target = redirect(f"{request.path}?year={year}#portfolio-section-{code}")
        with transaction.atomic():
            locked = PersonalWorkspace.objects.select_for_update().get(pk=workspace.pk)
            try:
                ensure_writable_personal_year(locked, year)
            except ValidationError:
                messages.error(request, "هذه السنة مؤرشفة؛ أعد فتحها قبل التعديل.")
                return target
            section, _ = PersonalPortfolioSection.objects.get_or_create(
                workspace=locked, academic_year=year, code=code
            )
            if action == "save_notes":
                notes = (request.POST.get("teacher_notes") or "").strip()
                if len(notes) > 10000:
                    messages.error(request, "وصف المحور طويل جدًا؛ الحد 10000 حرف.")
                else:
                    section.teacher_notes = notes
                    section.save(update_fields=["teacher_notes", "updated_at"])
                    messages.success(request, "حُفظ وصف المحور.")
            elif action == "link_report":
                report = get_object_or_404(PersonalReport, pk=_posted_id(request, "report_id"), workspace=locked, academic_year=year)
                _, created = PersonalPortfolioReport.objects.get_or_create(section=section, report=report)
                messages.success(request, "رُبط التقرير بالمحور." if created else "التقرير مرتبط بهذا المحور بالفعل.")
            elif action == "unlink_report":
                link = get_object_or_404(PersonalPortfolioReport, pk=_posted_id(request, "link_id"), section=section)
                link.delete()
                messages.success(request, "أُزيل ربط التقرير من المحور.")
            elif action == "link_evidence":
                evidence = get_object_or_404(PersonalEvidence, pk=_posted_id(request, "evidence_id"), workspace=locked, academic_year=year)
                if section.linked_evidence.count() >= 8:
                    messages.error(request, "الحد الأعلى لهذا المحور 8 شواهد.")
                else:
                    _, created = PersonalPortfolioEvidence.objects.get_or_create(section=section, evidence=evidence)
                    messages.success(request, "رُبط الشاهد بالمحور." if created else "الشاهد مرتبط بهذا المحور بالفعل.")
            elif action == "unlink_evidence":
                link = get_object_or_404(PersonalPortfolioEvidence, pk=_posted_id(request, "link_id"), section=section)
                link.delete()
                messages.success(request, "أُزيل ربط الشاهد؛ بقي الأصل في مكتبة شواهدك.")
            else:
                form = PersonalEvidenceForm(request.POST, request.FILES, workspace=locked)
                if not form.is_valid() or form.cleaned_data.get("academic_year") != year:
                    messages.error(request, "تعذر إضافة الشاهد؛ تحقق من العنوان والملف أو الرابط والسنة الدراسية.")
                    return target
                uploaded = form.cleaned_data.get("file")
                file_size = uploaded.size if uploaded else 0
                plan = request.personal_subscription.plan
                used = locked.evidence.aggregate(total=Sum("file_size"))["total"] or 0
                if section.linked_evidence.count() >= 8 or locked.evidence.count() >= plan.max_evidence:
                    messages.error(request, "بلغت الحد الأعلى للشواهد في المحور أو الباقة.")
                    return target
                if used + file_size > plan.storage_limit_mb * 1024 * 1024:
                    messages.error(request, "تجاوز الملف سعة باقتك الحالية.")
                    return target
                evidence = form.save(commit=False)
                evidence.workspace = locked
                evidence.file_size = file_size
                evidence.save()
                PersonalPortfolioEvidence.objects.create(section=section, evidence=evidence)
                messages.success(request, "أُضيف الشاهد إلى المحور ومكتبتك الشخصية.")
        return target

    context = _context(workspace, year)
    context["can_edit"] = bool(year and request.personal_subscription.is_current and not context["is_archived"])
    return render(request, "personal/portfolio.html", context)


@workspace_required
@never_cache
@require_GET
def portfolio_print(request):
    workspace = request.personal_workspace
    year = (request.GET.get("year") or "").strip()
    if year not in _portfolio_years(workspace):
        raise Http404
    context = _context(workspace, year)
    context["document_title"] = f"ملف الإنجاز {year}"
    context["school_names"] = list(context["reports"].order_by("school_name").values_list("school_name", flat=True).distinct()) or [workspace.school_name]
    context["principal_names"] = list(context["reports"].exclude(principal_name="").order_by("principal_name").values_list("principal_name", flat=True).distinct()) or ([workspace.principal_name] if workspace.principal_name else [])
    response = render(request, "personal/portfolio_print.html", context)
    response["Cache-Control"] = "private, no-store"
    return response
