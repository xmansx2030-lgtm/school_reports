"""Self-service tools for independent teachers, isolated from school tenancy."""

import json
import shutil
from itertools import islice
from tempfile import SpooledTemporaryFile
from zipfile import ZIP_DEFLATED, ZipFile

from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.http import FileResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from django_ratelimit.decorators import ratelimit

from reports.forms import MyPasswordChangeForm, MyProfilePhoneForm
from reports.middleware import clear_force_password_change_flag, is_force_password_change_required
from reports.model_parts.schools import normalize_sa_mobile_identity

from .forms import PersonalAccountForm, PersonalInitiativeForm, PersonalNoticeForm, PersonalYearForm
from .models import (
    PersonalAcademicYear, PersonalInitiative,
    PersonalNotice, PersonalNoticeRecipient, PersonalWorkspace,
)
from .services import ensure_writable_personal_year
from .views import workspace_required


@workspace_required
@never_cache
@require_http_methods(["GET", "POST"])
def years(request):
    ws = request.personal_workspace
    form = PersonalYearForm(request.POST or None, initial={"value": ws.current_academic_year})
    if request.method == "POST":
        if not request.personal_subscription.is_current:
            messages.error(request, "يلزم اشتراك نشط لتعديل السنوات الدراسية.")
            return redirect("personal:years")
        action = (request.POST.get("action") or "").strip()
        if action == "add" and form.is_valid():
            try:
                with transaction.atomic():
                    year = ensure_writable_personal_year(ws, form.cleaned_data["value"])
                    if request.POST.get("make_current") == "on":
                        PersonalWorkspace.objects.filter(pk=ws.pk).update(current_academic_year=year.value)
            except ValidationError as exc:
                form.add_error("value", exc)
            else:
                messages.success(request, "حُفظت السنة الدراسية.")
                return redirect("personal:years")
        if action in {"select", "archive", "reopen"}:
            value = (request.POST.get("year") or "").strip()
            with transaction.atomic():
                locked = PersonalWorkspace.objects.select_for_update().get(pk=ws.pk)
                year = get_object_or_404(PersonalAcademicYear.objects.select_for_update(), workspace=locked, value=value)
                if action == "select":
                    if year.archived_at:
                        messages.error(request, "أعد فتح السنة المؤرشفة قبل جعلها الحالية.")
                    else:
                        locked.current_academic_year = year.value
                        locked.save(update_fields=["current_academic_year", "updated_at"])
                        messages.success(request, "تغيّرت السنة الدراسية الحالية.")
                elif action == "archive":
                    if year.value == locked.current_academic_year:
                        messages.error(request, "اختر سنة حالية أخرى قبل أرشفة هذه السنة.")
                    elif request.POST.get("confirm_year") != year.value:
                        messages.error(request, "تأكيد السنة غير مطابق.")
                    elif not year.archived_at:
                        year.archived_at = timezone.now()
                        year.save(update_fields=["archived_at"])
                        messages.success(request, "أُرشفت السنة؛ بقيت أعمالها متاحة للقراءة والتصدير.")
                elif year.archived_at:
                    year.archived_at = None
                    year.save(update_fields=["archived_at"])
                    messages.success(request, "أُعيد فتح السنة الدراسية.")
            return redirect("personal:years")
    years_list = list(ws.academic_years.all())
    counters = {
        "reports": ws.reports.values("academic_year").annotate(total=Count("id")),
        "evidence": ws.evidence.values("academic_year").annotate(total=Count("id")),
        "initiatives": ws.initiatives.values("academic_year").annotate(total=Count("id")),
        "portfolio": ws.portfolio_sections.filter(
            Q(linked_reports__isnull=False) | Q(linked_evidence__isnull=False) | ~Q(teacher_notes="")
        ).values("academic_year").annotate(total=Count("id", distinct=True)),
    }
    counts = {kind: {row["academic_year"]: row["total"] for row in rows} for kind, rows in counters.items()}
    for year in years_list:
        year.report_count = counts["reports"].get(year.value, 0)
        year.evidence_count = counts["evidence"].get(year.value, 0)
        year.initiative_count = counts["initiatives"].get(year.value, 0)
        year.portfolio_count = counts["portfolio"].get(year.value, 0)
    return render(request, "personal/years.html", {
        "form": form, "years": years_list, "current_year": ws.current_academic_year,
        "can_edit": request.personal_subscription.is_current,
    })


@workspace_required
@never_cache
@require_GET
def year_export(request, value):
    ws = request.personal_workspace
    year_record = get_object_or_404(PersonalAcademicYear, workspace=ws, value=value)
    reports = list(ws.reports.filter(academic_year=value).order_by("id"))
    initiatives = list(ws.initiatives.filter(academic_year=value).order_by("id"))
    evidence = list(ws.evidence.filter(academic_year=value).order_by("id"))
    sections = list(ws.portfolio_sections.filter(academic_year=value).prefetch_related(
        "linked_reports", "linked_evidence"
    ))
    manifest = {
        "academic_year": value,
        "owner": ws.owner.name,
        "portfolio_profile": {
            "qualifications": year_record.qualifications,
            "professional_experience": year_record.professional_experience,
            "specialization": year_record.specialization,
            "teaching_load": year_record.teaching_load,
            "subjects_taught": year_record.subjects_taught,
            "contact_info": year_record.contact_info,
        },
        "reports": [{
            "id": row.pk, "title": row.title, "category": row.category,
            "date": row.report_date.isoformat(), "description": row.description,
            "goals": row.goals, "implementation": row.implementation,
            "results": row.results, "recommendations": row.recommendations,
            "show_goals": row.show_goals, "show_implementation": row.show_implementation,
            "show_details": row.show_details,
            "show_results": row.show_results, "show_recommendations": row.show_recommendations,
            "show_beneficiaries": row.show_beneficiaries,
            "beneficiaries_count": row.beneficiaries_count,
            "teacher_name": row.teacher_name, "school_name": row.school_name,
            "principal_name": row.principal_name,
            "status": row.status,
        } for row in reports],
        "initiatives": [{
            "id": row.pk, "title": row.title, "summary": row.summary,
            "impact": row.impact, "status": row.status,
            "is_best_practice": row.is_best_practice,
        } for row in initiatives],
        "evidence": [],
        "portfolio_sections": [{
            "code": section.code, "title": section.get_code_display(),
            "teacher_notes": section.teacher_notes,
            "report_ids": [link.report_id for link in section.linked_reports.all()],
            "evidence_ids": [link.evidence_id for link in section.linked_evidence.all()],
        } for section in sections],
    }
    archive = SpooledTemporaryFile(max_size=8 * 1024 * 1024)
    with ZipFile(archive, "w", compression=ZIP_DEFLATED) as bundle:
        for item in evidence:
            entry = {
                "id": item.pk, "title": item.title, "description": item.description,
                "report_id": item.report_id, "initiative_id": item.initiative_id,
                "source_url": item.source_url,
            }
            if item.file:
                suffix = item.file.name.rsplit(".", 1)[-1].lower()
                suffix = suffix if suffix in {"pdf", "jpg", "jpeg", "png"} else "bin"
                filename = f"evidence/{item.pk}.{suffix}"
                try:
                    with item.file.open("rb") as source, bundle.open(filename, "w") as target:
                        shutil.copyfileobj(source, target)
                except (OSError, FileNotFoundError):
                    entry["file_missing"] = True
                else:
                    entry["file"] = filename
            manifest["evidence"].append(entry)
        bundle.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    archive.seek(0)
    response = FileResponse(archive, as_attachment=True, filename=f"personal-work-{value}.zip")
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@workspace_required
@never_cache
@require_http_methods(["GET", "POST"])
def initiatives(request, pk=None):
    ws = request.personal_workspace
    initiative = get_object_or_404(PersonalInitiative, pk=pk, workspace=ws) if pk else None
    original_academic_year = initiative.academic_year if initiative else None
    initial = {"academic_year": ws.current_academic_year} if not initiative else None
    form = PersonalInitiativeForm(request.POST or None, instance=initiative, initial=initial)
    if request.method == "POST":
        if not request.personal_subscription.is_current:
            messages.error(request, "يلزم اشتراك نشط لتعديل المبادرات.")
            return redirect("personal:initiatives")
        if form.is_valid():
            with transaction.atomic():
                locked = PersonalWorkspace.objects.select_for_update().get(pk=ws.pk)
                original_archived = initiative and PersonalAcademicYear.objects.filter(
                    workspace=locked, value=original_academic_year, archived_at__isnull=False
                ).exists()
                if original_archived:
                    form.add_error("academic_year", "السنة الأصلية مؤرشفة؛ أعد فتحها قبل تعديل المبادرة.")
                else:
                    try:
                        ensure_writable_personal_year(locked, form.cleaned_data["academic_year"])
                    except ValidationError as exc:
                        form.add_error("academic_year", exc)
                    else:
                        saved = form.save(commit=False)
                        saved.workspace = locked
                        saved.save()
                        messages.success(request, "حُفظت المبادرة في مساحتك الشخصية.")
                        return redirect("personal:initiatives")
    query = ws.initiatives.all()
    year = (request.GET.get("year") or "").strip()[:20]
    if year:
        query = query.filter(academic_year=year)
    initiatives_list = list(query)
    archived_years = set(ws.academic_years.filter(archived_at__isnull=False).values_list("value", flat=True))
    for item in initiatives_list:
        item.can_edit_personal = request.personal_subscription.is_current and item.academic_year not in archived_years
        item.status_tone = {
            PersonalInitiative.Status.COMPLETE: "completed",
            PersonalInitiative.Status.ARCHIVED: "info",
        }.get(item.status, "draft")
    return render(request, "personal/initiatives.html", {
        "form": form, "initiative": initiative, "initiatives": initiatives_list,
        "years": ws.academic_years.all(), "year": year,
        "can_edit": request.personal_subscription.is_current and not (
            initiative and PersonalAcademicYear.objects.filter(
                workspace=ws, value=initiative.academic_year, archived_at__isnull=False
            ).exists()
        ),
    })


@workspace_required
@never_cache
@require_GET
def notice_list(request):
    items = request.personal_workspace.notices.select_related("notice").order_by("-notice__created_at", "-id")
    page_obj = Paginator(items, 12).get_page(request.GET.get("page"))
    return render(request, "personal/notices.html", {
        "page_obj": page_obj,
        "unread_count": request.personal_workspace.notices.filter(read_at__isnull=True).count(),
    })


@workspace_required
@never_cache
@require_GET
def notice_detail(request, pk):
    receipt = get_object_or_404(
        PersonalNoticeRecipient.objects.select_related("notice"), pk=pk, workspace=request.personal_workspace
    )
    if receipt.read_at is None:
        receipt.read_at = timezone.now()
        receipt.save(update_fields=["read_at"])
    return render(request, "personal/notice_detail.html", {"receipt": receipt})


@workspace_required
@require_POST
def notice_mark_read(request, pk):
    receipt = get_object_or_404(PersonalNoticeRecipient, pk=pk, workspace=request.personal_workspace)
    if receipt.read_at is None:
        receipt.read_at = timezone.now()
        receipt.save(update_fields=["read_at"])
    return redirect("personal:notice_detail", pk=pk)


@workspace_required
@never_cache
@ratelimit(key="user", rate="10/h", method="POST", block=True)
@require_http_methods(["GET", "POST"])
def account(request):
    user = request.user
    force_password_change = is_force_password_change_required(request)
    action = request.POST.get("action") if request.method == "POST" else ""
    profile_form = PersonalAccountForm(
        request.POST if action == "profile" else None, instance=user, prefix="profile"
    )
    phone_form = MyProfilePhoneForm(
        request.POST if action == "phone" else None, instance=user, prefix="phone"
    )
    password_form = MyPasswordChangeForm(
        user, request.POST if action == "password" else None,
        prefix="password", require_email=force_password_change,
    )
    if action == "profile" and profile_form.is_valid():
        profile_form.save()
        messages.success(request, "حُفظت بيانات الحساب.")
        return redirect("personal:account")
    if action == "phone":
        if force_password_change:
            messages.error(request, "غيّر كلمة المرور المطلوبة أولًا قبل تعديل رقم الجوال.")
        elif phone_form.is_valid():
            try:
                phone_form.save()
            except IntegrityError:
                phone_form.add_error("phone", "رقم الجوال مستخدم في حساب آخر.")
            else:
                messages.success(request, "تغيّر رقم الجوال.")
                return redirect("personal:account")
    if action == "password" and password_form.is_valid():
        changed_user = password_form.save()
        update_session_auth_hash(request, changed_user)
        session_key = request.session.session_key or ""
        if session_key and changed_user.current_session_key != session_key:
            changed_user.current_session_key = session_key
            changed_user.save(update_fields=["current_session_key"])
        clear_force_password_change_flag(request)
        messages.success(request, "تغيّرت كلمة المرور.")
        return redirect("personal:account")
    return render(request, "personal/account.html", {
        "profile_form": profile_form, "phone_form": phone_form,
        "password_form": password_form, "force_password_change": force_password_change,
    })


@login_required(login_url="reports:platform_login")
@user_passes_test(lambda user: user.is_superuser, login_url="reports:platform_login")
@never_cache
@require_http_methods(["GET", "POST"])
def platform_notice_compose(request):
    form = PersonalNoticeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        if form.cleaned_data["audience"] == "all":
            targets = PersonalWorkspace.objects.all()
        else:
            phone = normalize_sa_mobile_identity(form.cleaned_data["recipient_phone"])
            targets = PersonalWorkspace.objects.filter(owner__phone=phone)
        if not targets.exists():
            form.add_error("recipient_phone", "لم يُعثر على مساحة شخصية بهذا الرقم.")
        else:
            with transaction.atomic():
                notice, created = PersonalNotice.objects.get_or_create(
                    submission_key=form.cleaned_data["submission_key"],
                    defaults={
                        "title": form.cleaned_data["title"],
                        "message": form.cleaned_data["message"],
                        "created_by": request.user,
                    },
                )
                if created:
                    ids = targets.values_list("pk", flat=True).iterator(chunk_size=500)
                    while batch := list(islice(ids, 500)):
                        PersonalNoticeRecipient.objects.bulk_create([
                            PersonalNoticeRecipient(notice=notice, workspace_id=pk) for pk in batch
                        ], batch_size=500)
            messages.success(request, "أُرسل الإشعار إلى المساحات الشخصية المحددة.")
            return redirect("personal:platform_notice_compose")
    recent = PersonalNotice.objects.select_related("created_by").order_by("-created_at")[:20]
    return render(request, "personal/platform_notice_compose.html", {"form": form, "recent": recent})
