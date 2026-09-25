"""Expiring public links scoped to one personal report or one annual portfolio.

School ShareLink records and school authorization are deliberately not consulted.
The same visible-evidence scope builds the HTML and gates every file response.
"""

from datetime import timedelta
from pathlib import Path
import secrets

from django.contrib import messages
from django.db import IntegrityError, transaction
from django.db.models import F, Prefetch, Q
from django.http import FileResponse, Http404, HttpResponseBadRequest, HttpResponseNotFound
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods
from django_ratelimit.decorators import ratelimit
from urllib.parse import urlencode

from reports.model_parts.achievements import AchievementSection

from .models import (
    PersonalAcademicYear,
    PersonalEvidence,
    PersonalPortfolioEvidence,
    PersonalPortfolioReport,
    PersonalPortfolioSection,
    PersonalReport,
    PersonalShareLink,
    PersonalSubscription,
    PersonalWorkspace,
)
from .portfolio_views import _portfolio_years
from .views import workspace_required


EXPIRY_CHOICES = (1, 7, 14, 30, 90)


def _share_filter(workspace, kind, *, report=None, year=None):
    links = PersonalShareLink.objects.filter(workspace=workspace, kind=kind, report=report)
    return links.filter(academic_year=year) if kind == "portfolio" else links


def _manage_url(kind, *, report=None, year=None):
    if kind == "report":
        return reverse("personal:report_share_manage", args=[report.pk])
    return f'{reverse("personal:portfolio_share_manage")}?{urlencode({"year": year})}'


def _manage(request, *, kind, report=None, year=None):
    workspace = request.personal_workspace
    subject_year = year if kind == "portfolio" else report.academic_year
    target_url = _manage_url(kind, report=report, year=year)
    links = _share_filter(workspace, kind, report=report, year=year)

    if request.method == "POST":
        action = request.POST.get("action")
        if action not in {"enable", "disable"}:
            return HttpResponseBadRequest("إجراء غير صالح")
        if action == "enable":
            raw_days = request.POST.get("expiry_days", "")
            if raw_days not in {str(days) for days in EXPIRY_CHOICES}:
                return HttpResponseBadRequest("مدة صلاحية غير صالحة")
            if not request.personal_subscription.is_current:
                messages.error(request, "يلزم اشتراك شخصي نشط لإنشاء رابط مشاركة.")
                return redirect(target_url)
            days = int(raw_days)
        with transaction.atomic():
            # Serialize owner actions, including concurrent token rotations.
            locked = PersonalWorkspace.objects.select_for_update().get(pk=workspace.pk)
            if action == "enable":
                subscription = PersonalSubscription.objects.select_for_update().filter(workspace=locked).first()
                if subscription is None or not subscription.is_current:
                    messages.error(request, "يلزم اشتراك شخصي نشط لإنشاء رابط مشاركة.")
                    return redirect(target_url)
            if kind == "report":
                get_object_or_404(
                    PersonalReport, pk=report.pk, workspace=locked, trashed_at__isnull=True,
                )
            elif subject_year not in _portfolio_years(locked):
                raise Http404
            links.filter(is_active=True).update(is_active=False)
            if action == "enable":
                expires_at = timezone.now() + timedelta(days=days)
                for _ in range(5):
                    try:
                        with transaction.atomic():
                            PersonalShareLink.objects.create(
                                workspace=locked,
                                kind=kind,
                                report=report,
                                academic_year=subject_year,
                                token=secrets.token_urlsafe(32),
                                is_active=True,
                                expires_at=expires_at,
                            )
                    except IntegrityError:
                        continue
                    break
                else:
                    raise RuntimeError("Unable to generate a unique personal share token")
        messages.success(
            request,
            "أُنشئ رابط مشاركة مؤقت جديد." if action == "enable" else "أُوقف رابط المشاركة.",
        )
        return redirect(target_url)

    active_links = links.filter(is_active=True, expires_at__gt=timezone.now())
    if kind == "report":
        active_links = active_links.filter(academic_year=report.academic_year)
    active_link = active_links.order_by("-created_at", "-pk").first()
    public_url = ""
    if active_link:
        route = "share_public_report" if kind == "report" else "share_public_portfolio"
        public_url = request.build_absolute_uri(reverse(f"personal:{route}", args=[active_link.token]))
    return render(request, "personal/share_manage.html", {
        "kind": kind,
        "report": report,
        "year": subject_year,
        "subject_title": report.title if report else f"ملف الإنجاز {subject_year}",
        "return_url": reverse("personal:report_detail", args=[report.pk]) if report else f'{reverse("personal:portfolio")}?{urlencode({"year": subject_year})}',
        "active_link": active_link,
        "public_url": public_url,
        "expiry_choices": EXPIRY_CHOICES,
        "can_create": request.personal_subscription.is_current,
    })


@workspace_required
@never_cache
@ratelimit(key="user", rate="20/h", method="POST", block=True)
@require_http_methods(["GET", "POST"])
def report_share_manage(request, pk):
    report = get_object_or_404(
        PersonalReport, pk=pk, workspace=request.personal_workspace, trashed_at__isnull=True,
    )
    return _manage(request, kind="report", report=report)


@workspace_required
@never_cache
@ratelimit(key="user", rate="20/h", method="POST", block=True)
@require_http_methods(["GET", "POST"])
def portfolio_share_manage(request):
    year = (request.GET.get("year") if request.method == "GET" else request.POST.get("year")) or ""
    year = year.strip()
    if not year or year not in _portfolio_years(request.personal_workspace):
        raise Http404
    return _manage(request, kind="portfolio", year=year)


def _valid_public_link(token, kind=None):
    lookup = {"token": token, "is_active": True, "expires_at__gt": timezone.now()}
    if kind is not None:
        lookup["kind"] = kind
    link = get_object_or_404(
        PersonalShareLink.objects.select_related("workspace", "workspace__owner", "report"),
        **lookup,
    )
    if link.workspace.owner_id is None:
        raise Http404
    if link.kind == "report":
        if (
            link.report_id is None
            or link.report.workspace_id != link.workspace_id
            or link.report.academic_year != link.academic_year
            or link.report.trashed_at is not None
        ):
            raise Http404
    elif link.kind == "portfolio":
        if link.report_id is not None or not link.academic_year:
            raise Http404
    else:
        raise Http404
    return link


def _published_evidence_queryset(link):
    """The single file-visibility rule for public HTML and the witness proxy."""
    workspace, year = link.workspace, link.academic_year
    evidence = PersonalEvidence.objects.filter(
        workspace=workspace, academic_year=year, show_in_print=True,
    )
    if link.kind == "report":
        return evidence.filter(report=link.report)
    active_report = Q(
        report__workspace=workspace,
        report__academic_year=year,
        report__trashed_at__isnull=True,
    )
    linked_report = Q(
        report__portfolio_links__section__workspace=workspace,
        report__portfolio_links__section__academic_year=year,
        report__portfolio_links__section__code__in=[code for code, _ in AchievementSection.Code.choices],
    )
    linked_axis = Q(
        portfolio_links__section__workspace=workspace,
        portfolio_links__section__academic_year=year,
        portfolio_links__section__code__in=[code for code, _ in AchievementSection.Code.choices],
    )
    return evidence.filter((active_report & linked_report) | linked_axis).filter(
        Q(report__isnull=True) | active_report,
    ).distinct()


def _visible_scope(link):
    """Build page rows from the same visibility rule used by public downloads."""
    workspace, year = link.workspace, link.academic_year
    published = _published_evidence_queryset(link)
    if link.kind == "report":
        evidence = list(published.order_by("order", "pk"))
        return {
            "workspace": workspace, "report": link.report, "evidence": evidence,
        }

    section_rows = PersonalPortfolioSection.objects.filter(
        workspace=workspace, academic_year=year,
    ).prefetch_related(
        Prefetch("linked_reports", queryset=PersonalPortfolioReport.objects.filter(
            report__workspace=workspace,
            report__academic_year=year,
            report__trashed_at__isnull=True,
        ).select_related("report")),
        Prefetch("linked_evidence", queryset=PersonalPortfolioEvidence.objects.filter(
            evidence__in=published,
        ).select_related("evidence")),
    )
    by_code = {row.code: row for row in section_rows}
    sections = []
    visible_evidence = []
    for code, title in AchievementSection.Code.choices:
        row = by_code.get(code)
        reports = list(row.linked_reports.all()) if row else []
        evidence_links = list(row.linked_evidence.all()) if row else []
        visible_evidence.extend(item.evidence for item in evidence_links)
        notes = row.teacher_notes if row else ""
        sections.append({
            "code": code, "title": title, "notes": notes,
            "reports": reports, "evidence": evidence_links,
            "documented": bool(notes.strip() or reports or evidence_links),
        })

    reports = list(PersonalReport.objects.filter(
        workspace=workspace, academic_year=year, trashed_at__isnull=True,
        portfolio_links__section__workspace=workspace,
        portfolio_links__section__academic_year=year,
        portfolio_links__section__code__in=[code for code, _ in AchievementSection.Code.choices],
    ).distinct().order_by("-report_date", "-pk"))
    report_evidence = published.filter(report__in=reports).order_by("report_id", "order", "pk")
    by_report = {report.pk: [] for report in reports}
    for item in report_evidence:
        by_report[item.report_id].append(item)
        visible_evidence.append(item)
    report_entries = [{"report": report, "evidence": by_report[report.pk]} for report in reports]
    year_record = PersonalAcademicYear.objects.filter(
        workspace=workspace, value=year,
    ).values(
        "qualifications", "professional_experience", "specialization",
        "teaching_load", "subjects_taught",
    ).first()
    context = {
        "workspace": workspace, "year": year, "year_record": year_record,
        "sections": sections,
        "documented_count": sum(section["documented"] for section in sections),
        "report_entries": report_entries,
        "visible_evidence_count": len({item.pk for item in visible_evidence}),
    }
    return context


def _public_response(response):
    response["Cache-Control"] = "no-store"
    response["X-Robots-Tag"] = "noindex, nofollow"
    response["Referrer-Policy"] = "no-referrer"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def _not_found():
    return _public_response(HttpResponseNotFound())


def _count_open(link):
    PersonalShareLink.objects.filter(pk=link.pk, is_active=True).update(
        access_count=F("access_count") + 1,
        last_accessed_at=timezone.now(),
    )


@ratelimit(key="ip", rate="120/h", method="GET", block=True)
@require_GET
def public_report(request, token):
    try:
        link = _valid_public_link(token, "report")
        context = _visible_scope(link)
    except Http404:
        return _not_found()
    _count_open(link)
    context["token"] = token
    return _public_response(render(request, "personal/share_public_report.html", context))


@ratelimit(key="ip", rate="120/h", method="GET", block=True)
@require_GET
def public_portfolio(request, token):
    try:
        link = _valid_public_link(token, "portfolio")
        context = _visible_scope(link)
    except Http404:
        return _not_found()
    _count_open(link)
    context["token"] = token
    return _public_response(render(request, "personal/share_public_portfolio.html", context))


@ratelimit(key="ip", rate="2400/h", method="GET", block=True)
@require_GET
def public_evidence(request, token, evidence_id, mode):
    if mode not in {"preview", "download"}:
        return _not_found()
    try:
        link = _valid_public_link(token)
        evidence = get_object_or_404(_published_evidence_queryset(link), pk=evidence_id)
    except Http404:
        return _not_found()
    suffix = Path(evidence.file.name).suffix.lower() if evidence.file else ""
    if suffix not in {".png", ".jpg", ".jpeg", ".pdf"}:
        return _not_found()
    if mode == "preview" and suffix not in {".png", ".jpg", ".jpeg"}:
        return _not_found()
    try:
        evidence.file.open("rb")
    except Exception:
        return _not_found()
    content_type = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".pdf": "application/pdf"}[suffix]
    response = FileResponse(
        evidence.file, as_attachment=mode == "download",
        filename=Path(evidence.file.name).name if mode == "download" else None,
        content_type=content_type,
    )
    return _public_response(response)
