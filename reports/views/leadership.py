# reports/views/leadership.py
from __future__ import annotations

from ._helpers import *
from ._helpers import _get_active_school
from ..gender_labels import school_gender_template_context


def _ensure_leadership_sections(portfolio: SchoolLeadershipPortfolio) -> None:
    existing = set(portfolio.sections.values_list("code", flat=True))
    LeadershipPortfolioSection.objects.bulk_create(
        LeadershipPortfolioSection(portfolio=portfolio, code=code)
        for code, _label in LeadershipPortfolioSection.Code.choices
        if code not in existing
    )


def _leadership_context(
    portfolio: SchoolLeadershipPortfolio, *, report_owner: Teacher | None = None
) -> dict:
    sections = portfolio.sections.prefetch_related(
        "evidence_images",
        "evidence_reports__report__category",
    ).order_by("code", "id")
    completed = sections.filter(is_completed=True).count()
    total = len(LeadershipPortfolioSection.Code.choices)
    return {
        "portfolio": portfolio,
        "sections": sections,
        "overview_form": LeadershipPortfolioForm(instance=portfolio),
        "current_school": portfolio.school,
        "completed_sections": completed,
        "total_sections": total,
        "completion_percent": int((completed / total) * 100),
        "evidence_count": LeadershipEvidenceImage.objects.filter(section__portfolio=portfolio).count(),
        "report_evidence_count": LeadershipEvidenceReport.objects.filter(
            section__portfolio=portfolio
        ).count(),
        "available_reports": (
            Report.objects.filter(
                school=portfolio.school,
                teacher=report_owner,
                academic_year=portfolio.academic_year,
            )
            .select_related("category")
            .order_by("-report_date", "-id")[:100]
            if report_owner is not None
            else Report.objects.none()
        ),
        **school_gender_template_context(portfolio.school),
        "manager_label": school_gender_template_context(portfolio.school)["SCHOOL_MANAGER_LABEL"],
    }


def _manager_portfolio_or_404(request: HttpRequest, pk: int) -> SchoolLeadershipPortfolio:
    active_school = _get_active_school(request)
    portfolio = get_object_or_404(
        SchoolLeadershipPortfolio.objects.select_related("school", "manager"), pk=pk
    )
    if (
        active_school is None
        or portfolio.school_id != active_school.id
        or not is_school_manager(request.user, active_school=active_school)
    ):
        raise Http404
    return portfolio


@login_required(login_url="reports:login")
@require_http_methods(["GET", "POST"])
def leadership_portfolio_list(request: HttpRequest) -> HttpResponse:
    active_school = _get_active_school(request)
    if active_school is None:
        messages.error(request, "فضلاً اختر مدرسة أولاً.")
        return redirect("reports:home")
    if not is_school_manager(request.user, active_school=active_school):
        return HttpResponse(status=403)

    if request.method == "POST":
        year = (active_school.current_academic_year or "").strip()
        if not year:
            messages.error(request, "حدد السنة الدراسية الحالية من إعدادات المدرسة أولاً.")
            return redirect("reports:leadership_portfolio_list")
        portfolio, created = SchoolLeadershipPortfolio.objects.get_or_create(
            school=active_school,
            academic_year=year,
            defaults={"manager": request.user},
        )
        _ensure_leadership_sections(portfolio)
        if created:
            messages.success(request, "تم إنشاء ملف الأداء القيادي.")
        return redirect("reports:leadership_portfolio_detail", pk=portfolio.pk)

    portfolios = SchoolLeadershipPortfolio.objects.filter(school=active_school).annotate(
        completed_count=Count(
            "sections",
            filter=Q(sections__is_completed=True),
            distinct=True,
        ),
        evidence_count=Count("sections__evidence_images", distinct=True),
        report_evidence_count=Count("sections__evidence_reports", distinct=True),
    )
    return render(
        request,
        "reports/leadership_portfolio_list.html",
        {"portfolios": portfolios, "current_school": active_school},
    )


@login_required(login_url="reports:login")
@require_http_methods(["GET", "POST"])
def leadership_portfolio_detail(request: HttpRequest, pk: int) -> HttpResponse:
    portfolio = _manager_portfolio_or_404(request, pk)
    _ensure_leadership_sections(portfolio)

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
        if action == "save_overview":
            form = LeadershipPortfolioForm(request.POST, instance=portfolio)
            if form.is_valid():
                form.save()
                messages.success(request, "تم حفظ الملخص القيادي.")
            else:
                messages.error(request, "تحقق من البيانات وأعد المحاولة.")
        elif action == "save_section":
            section = get_object_or_404(
                LeadershipPortfolioSection,
                pk=request.POST.get("section_id"),
                portfolio=portfolio,
            )
            form = LeadershipPortfolioSectionForm(request.POST, instance=section)
            if form.is_valid():
                form.save()
                messages.success(request, "تم حفظ المحور.")
            else:
                messages.error(request, "تعذر حفظ المحور.")
        elif action == "upload_evidence":
            section = get_object_or_404(
                LeadershipPortfolioSection,
                pk=request.POST.get("section_id"),
                portfolio=portfolio,
            )
            images = request.FILES.getlist("images")
            remaining = max(0, 8 - section.evidence_images.count())
            if not images:
                messages.error(request, "اختر صورة واحدة على الأقل.")
            elif remaining == 0:
                messages.error(request, "اكتمل الحد الأعلى: 8 شواهد لهذا المحور.")
            else:
                images = images[:remaining]
                capacity_error = archive_storage_capacity_error(portfolio.school, images)
                if capacity_error:
                    messages.error(request, capacity_error)
                else:
                    caption = (request.POST.get("caption") or "").strip()[:180]
                    candidates = [
                        LeadershipEvidenceImage(section=section, image=image, caption=caption)
                        for image in images
                    ]
                    try:
                        for candidate in candidates:
                            candidate.full_clean(exclude=["storage_bytes"])
                        with transaction.atomic():
                            for candidate in candidates:
                                candidate.save()
                    except ValidationError as exc:
                        messages.error(request, "; ".join(exc.messages))
                    else:
                        sync_school_archive_storage_usage(portfolio.school)
                        messages.success(request, "تمت إضافة الشواهد.")
        elif action == "delete_evidence":
            evidence = get_object_or_404(
                LeadershipEvidenceImage,
                pk=request.POST.get("evidence_id"),
                section__portfolio=portfolio,
            )
            evidence.delete()
            sync_school_archive_storage_usage(portfolio.school)
            messages.success(request, "تم حذف الشاهد.")
        elif action == "add_report_evidence":
            section = get_object_or_404(
                LeadershipPortfolioSection,
                pk=request.POST.get("section_id"),
                portfolio=portfolio,
            )
            report = get_object_or_404(
                Report,
                pk=request.POST.get("report_id"),
                school=portfolio.school,
                teacher=request.user,
                academic_year=portfolio.academic_year,
            )
            _evidence, created = LeadershipEvidenceReport.objects.get_or_create(
                section=section,
                report=report,
            )
            if created:
                messages.success(request, "تمت إضافة التقرير كشاهد في المحور.")
            else:
                messages.info(request, "التقرير مضاف إلى هذا المحور مسبقًا.")
        elif action == "remove_report_evidence":
            evidence = get_object_or_404(
                LeadershipEvidenceReport,
                pk=request.POST.get("evidence_id"),
                section__portfolio=portfolio,
                report__teacher=request.user,
            )
            evidence.delete()
            messages.success(request, "تمت إزالة التقرير من المحور دون حذف التقرير.")
        elif action == "set_status":
            value = request.POST.get("status")
            if value in SchoolLeadershipPortfolio.Status.values:
                portfolio.status = value
                portfolio.save(update_fields=["status", "updated_at"])
                messages.success(request, "تم تحديث حالة الملف.")
        return redirect("reports:leadership_portfolio_detail", pk=portfolio.pk)

    return render(
        request,
        "reports/leadership_portfolio_detail.html",
        _leadership_context(portfolio, report_owner=request.user),
    )


@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def leadership_portfolio_print(request: HttpRequest, pk: int) -> HttpResponse:
    portfolio = _manager_portfolio_or_404(request, pk)
    _ensure_leadership_sections(portfolio)
    return render(request, "reports/pdf/leadership_portfolio.html", _leadership_context(portfolio))


@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def leadership_portfolio_pdf(request: HttpRequest, pk: int) -> HttpResponse:
    portfolio = _manager_portfolio_or_404(request, pk)
    _ensure_leadership_sections(portfolio)
    try:
        from ..pdf_leadership import generate_leadership_portfolio_pdf
        from ..pdf_offload import render_pdf_offloaded
        from ..tasks import render_leadership_pdf_task

        base_url = request.build_absolute_uri("/")
        pdf = render_pdf_offloaded(
            task=render_leadership_pdf_task,
            task_args=[portfolio.pk, base_url],
            render_locally=lambda: generate_leadership_portfolio_pdf(
                portfolio,
                request=request,
                base_url=base_url,
            ),
            label=f"leadership:{portfolio.pk}",
        )
    except Exception:
        logger.exception("Leadership portfolio PDF generation failed")
        return HttpResponse("تعذر توليد ملف PDF حاليًا.", status=503)
    response = HttpResponse(pdf, content_type="application/pdf")
    response["Content-Disposition"] = (
        f'attachment; filename="leadership_{portfolio.school_id}_{portfolio.academic_year}.pdf"'
    )
    return response


__all__ = [name for name in globals() if not name.startswith("__")]
