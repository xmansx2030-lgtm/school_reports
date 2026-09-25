from functools import wraps
from io import BytesIO
import json
import logging
from pathlib import Path
import uuid

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.files.uploadedfile import UploadedFile
from django.db import IntegrityError, transaction
from django.db.models import Max, Prefetch, Q, Sum
from django.core.paginator import Paginator
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from django.views.decorators.csrf import csrf_exempt
from django_ratelimit.decorators import ratelimit
from django.utils.http import url_has_allowed_host_and_scheme
from urllib.parse import urlencode

from reports.moyasar_gateway import MoyasarGatewayError, is_enabled as moyasar_is_enabled
from reports.models import Teacher
from reports.report_review import normalise_draft, review_draft

from .assistant_views import personal_assistant_template_context
from .billing import (
    PersonalPaymentError,
    create_personal_checkout,
    generate_personal_invoice_pdf,
    sync_personal_payment,
)
from .forms import (
    PersonalEmailForm,
    PersonalEvidenceForm,
    PersonalGenderForm,
    PersonalInlineEvidenceFormSet,
    PersonalRegistrationForm,
    PersonalReportForm,
    PersonalSchoolParityReportForm,
    PersonalWorkspaceForm,
    personal_inline_evidence_formset,
    personal_report_evidence_formset,
)
from .models import PersonalAcademicYear, PersonalEvidence, PersonalPayment, PersonalPlan, PersonalReport, PersonalWorkspace
from .services import current_school_membership_for, ensure_personal_subscription, ensure_writable_personal_year


logger = logging.getLogger(__name__)


def workspace_required(view):
    @login_required(login_url="reports:login")
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        workspace = PersonalWorkspace.objects.filter(owner=request.user).first()
        if workspace is None:
            return redirect("personal:setup")
        request.personal_workspace = workspace
        request.personal_subscription = ensure_personal_subscription(workspace)
        return view(request, *args, **kwargs)

    return wrapped


@never_cache
@ratelimit(key="ip", rate="5/h", method="POST", block=True)
@require_http_methods(["GET", "POST"])
def register(request):
    if request.user.is_authenticated:
        requested_plan = (request.GET.get("plan") or "").strip()
        if requested_plan.isdigit() and PersonalPlan.objects.filter(
            pk=int(requested_plan), is_active=True, is_published=True, price__gt=0
        ).exists():
            return redirect("personal:checkout_start", plan_id=int(requested_plan))
        return redirect("personal:setup")
    selected_plan = None
    requested_plan = (request.GET.get("plan") or "").strip()
    if requested_plan.isdigit():
        selected_plan = PersonalPlan.objects.filter(
            pk=int(requested_plan), is_active=True, is_published=True
        ).exclude(price__lte=0).first()
    form = PersonalRegistrationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                teacher = Teacher.objects.create_user(
                    phone=form.cleaned_data["phone"],
                    name=form.cleaned_data["name"].strip(),
                    gender=form.cleaned_data["gender"],
                    email=form.cleaned_data["email"],
                    password=form.cleaned_data["password"],
                )
                workspace = PersonalWorkspace.objects.create(
                    owner=teacher,
                    school_name=form.cleaned_data["school_name"].strip(),
                    principal_name=form.cleaned_data["principal_name"].strip(),
                )
                subscription = ensure_personal_subscription(workspace)
        except IntegrityError:
            form.add_error("phone", "تعذر إنشاء حساب جديد بهذا الرقم. يمكن تسجيل الدخول إلى الحساب الموجود.")
        else:
            login(request, teacher)
            if selected_plan:
                messages.success(request, "أُنشئ حسابك. تُفعّل الباقة المدفوعة بعد تأكيد نجاح الدفع.")
                return redirect("personal:checkout_start", plan_id=selected_plan.pk)
            if subscription.is_current:
                messages.success(request, "أُنشئت مساحتك الشخصية وفُعّلت باقتك المجانية. يمكنك الآن توثيق أول عمل.")
            else:
                messages.info(request, "أُنشئت مساحتك الشخصية. يمكنك اختيار باقة متاحة لتفعيل الاشتراك وبدء التوثيق.")
            return redirect("personal:dashboard")
    return render(request, "personal/register.html", {"form": form, "selected_plan": selected_plan})


@never_cache
@login_required(login_url="reports:login")
@require_http_methods(["GET", "POST"])
def setup(request):
    workspace = PersonalWorkspace.objects.filter(owner=request.user).first()
    form = PersonalWorkspaceForm(request.POST or None, instance=workspace)
    next_url = (request.POST.get("next") or request.GET.get("next") or "").strip()
    safe_next = next_url if url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ) else ""
    email_form = PersonalEmailForm(
        request.POST or None, teacher=request.user, require_email=bool(safe_next)
    )
    gender_form = PersonalGenderForm(request.POST or None, teacher=request.user)
    if request.method == "POST" and form.is_valid() and email_form.is_valid() and gender_form.is_valid():
        obj = form.save(commit=False)
        obj.owner = request.user
        obj.save()
        user_update_fields = []
        if request.user.email != email_form.cleaned_data["email"]:
            request.user.email = email_form.cleaned_data["email"]
            user_update_fields.append("email")
        if request.user.gender != gender_form.cleaned_data["gender"]:
            request.user.gender = gender_form.cleaned_data["gender"]
            user_update_fields.append("gender")
        if user_update_fields:
            request.user.save(update_fields=user_update_fields)
        ensure_personal_subscription(obj)
        messages.success(request, "حُفظت بيانات مساحتك وبريد الفواتير والتنبيهات.")
        if safe_next:
            return redirect(safe_next)
        return redirect("personal:dashboard")
    return render(request, "personal/setup.html", {
        "form": form, "email_form": email_form, "gender_form": gender_form,
        "workspace": workspace, "next_url": safe_next,
    })


@never_cache
@ratelimit(key="user", rate="5/m", method="POST", block=True)
@require_http_methods(["GET", "POST"])
def checkout_start(request, plan_id):
    plan = get_object_or_404(
        PersonalPlan.objects.filter(is_active=True, is_published=True, price__gt=0),
        pk=plan_id,
    )
    if not request.user.is_authenticated:
        return redirect(f"{reverse('personal:register')}?{urlencode({'plan': plan.pk})}")
    school_membership = current_school_membership_for(request.user)
    if school_membership is not None:
        messages.warning(
            request,
            f"هذا الحساب مرتبط بمدرسة {school_membership.school.name} واشتراكها ساري. "
            "المساحة المدرسية متاحة لهذا الحساب، ولا يلزم اشتراك شخصي منفصل.",
        )
        return redirect("reports:home")
    workspace = PersonalWorkspace.objects.filter(owner=request.user).first()
    checkout_path = reverse("personal:checkout_start", args=[plan.pk])
    if workspace is None:
        return redirect(f"{reverse('personal:setup')}?{urlencode({'next': checkout_path})}")
    ensure_personal_subscription(workspace)
    if not request.user.email:
        return redirect(f"{reverse('personal:setup')}?{urlencode({'next': checkout_path})}")
    if request.method == "POST":
        try:
            _payment, checkout_url = create_personal_checkout(
                request=request, workspace=workspace, plan=plan
            )
        except PersonalPaymentError as exc:
            messages.error(request, str(exc))
        else:
            return redirect(checkout_url)
    return render(request, "personal/checkout.html", {
        "plan": plan,
        "workspace": workspace,
        "moyasar_enabled": moyasar_is_enabled(),
    })


@csrf_exempt
@ratelimit(key="ip", rate="120/h", method="POST", block=True)
@require_http_methods(["GET", "POST"])
def moyasar_callback(request, payment_id):
    try:
        payment, status = sync_personal_payment(payment_id)
    except PersonalPaymentError as exc:
        logger.warning("Rejected Moyasar personal callback ref=%s reason=%s", payment_id, exc)
        return JsonResponse({"ok": False, "detail": str(exc)}, status=400)
    except (MoyasarGatewayError, ImproperlyConfigured):
        logger.exception("Could not verify Moyasar personal callback ref=%s", payment_id)
        return JsonResponse({"ok": False, "detail": "Could not verify payment."}, status=502)
    return JsonResponse({"ok": True, "status": status, "activated": bool(payment.activated_at)})


@never_cache
@require_GET
def moyasar_return(request, payment_id):
    try:
        payment, status = sync_personal_payment(payment_id)
    except PersonalPaymentError as exc:
        logger.warning("Rejected Moyasar personal return ref=%s reason=%s", payment_id, exc)
        messages.error(request, "تعذّر مطابقة فاتورة الدفع. تواصل مع الدعم مع الاحتفاظ بإيصال ميسّر.")
    except (MoyasarGatewayError, ImproperlyConfigured):
        logger.exception("Could not verify Moyasar personal return ref=%s", payment_id)
        messages.info(request, "يجري التحقق من الدفع؛ لا تعِد الدفع الآن. ستظهر النتيجة في سجل اشتراكك.")
    else:
        if status == "paid" and payment.activated_at:
            messages.success(request, "تم تأكيد الدفع وتفعيل الباقة الشخصية تلقائيًا. الفاتورة متاحة للتنزيل من سجل الاشتراك.")
        elif status in {"failed", "canceled", "cancelled", "expired", "voided"}:
            messages.error(request, "لم تكتمل عملية الدفع. يمكنك بدء محاولة جديدة من صفحة الاشتراك.")
        else:
            messages.info(request, "الدفع قيد التحقق. ستتحدث صفحة الاشتراك تلقائيًا بعد تأكيد ميسّر.")
    if request.user.is_authenticated and PersonalWorkspace.objects.filter(owner=request.user).exists():
        return redirect("personal:billing")
    return redirect(f"{reverse('reports:login')}?{urlencode({'next': reverse('personal:dashboard')})}")


@workspace_required
@never_cache
@require_GET
def billing(request):
    workspace = request.personal_workspace
    payments = PersonalPayment.objects.filter(workspace=workspace).select_related("plan")[:50]
    return render(request, "personal/billing.html", {
        "workspace": workspace,
        "subscription": request.personal_subscription,
        "payments": payments,
        "report_count": workspace.reports.count(),
        "evidence_count": workspace.evidence.count(),
        "plans": PersonalPlan.objects.filter(is_active=True, is_published=True).order_by("display_order", "price", "id"),
        "paid_plans": PersonalPlan.objects.filter(is_active=True, is_published=True, price__gt=0).order_by("display_order", "price", "id"),
        "moyasar_enabled": moyasar_is_enabled(),
    })


@workspace_required
@never_cache
@require_GET
def payment_invoice(request, payment_id):
    payment = get_object_or_404(
        PersonalPayment.objects.select_related("workspace__owner", "plan"),
        pk=payment_id,
        workspace=request.personal_workspace,
        status=PersonalPayment.Status.PAID,
        activated_at__isnull=False,
    )
    pdf_bytes, filename = generate_personal_invoice_pdf(payment, request=request)
    response = FileResponse(
        BytesIO(pdf_bytes),
        content_type="application/pdf",
        as_attachment=True,
        filename=filename,
    )
    response["Cache-Control"] = "private, no-store"
    response["X-Robots-Tag"] = "noindex, nofollow"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@workspace_required
@require_GET
def dashboard(request):
    ws = request.personal_workspace
    active_reports = ws.reports.filter(trashed_at__isnull=True)
    recent_reports = active_reports[:5]
    recent_evidence = ws.evidence.filter(
        Q(report__isnull=True) | Q(report__trashed_at__isnull=True)
    )[:5]
    stats = {
        "reports": active_reports.count(),
        "complete": active_reports.filter(status=PersonalReport.Status.COMPLETE).count(),
        "evidence": ws.evidence.count(),
        "years": ws.academic_years.count(),
        "initiatives": ws.initiatives.count(),
        "unread_notices": ws.notices.filter(read_at__isnull=True).count(),
    }
    return render(request, "personal/dashboard.html", {
        "workspace": ws, "recent_reports": recent_reports,
        "recent_evidence": recent_evidence, "stats": stats,
        "subscription": request.personal_subscription,
    })


@workspace_required
@require_GET
def report_list(request):
    qs = request.personal_workspace.reports.filter(trashed_at__isnull=True).prefetch_related(Prefetch(
        "evidence", queryset=PersonalEvidence.objects.order_by("order", "id"),
    ))
    query = (request.GET.get("q") or "").strip()[:100]
    year = (request.GET.get("year") or "").strip()[:20]
    status = (request.GET.get("status") or "").strip()
    if query:
        qs = qs.filter(Q(title__icontains=query) | Q(category__icontains=query) | Q(description__icontains=query))
    if year:
        qs = qs.filter(academic_year=year)
    if status in PersonalReport.Status.values:
        qs = qs.filter(status=status)
    years = request.personal_workspace.reports.filter(trashed_at__isnull=True).order_by(
        "-academic_year"
    ).values_list("academic_year", flat=True).distinct()
    page = Paginator(qs, 20).get_page(request.GET.get("page"))
    return render(request, "personal/report_list.html", {
        "reports": page, "query": query, "year": year, "status": status,
        "years": years, "statuses": PersonalReport.Status.choices,
        "subscription_active": request.personal_subscription.is_current,
        "archived_years": set(request.personal_workspace.academic_years.filter(
            archived_at__isnull=False
        ).values_list("value", flat=True)),
    })


def _save_report(form, workspace, owner, *, submission_id=None):
    report = form.save(commit=False)
    report.workspace = workspace
    if not report.pk:
        report.client_submission_id = submission_id
        report.teacher_name = owner.name
        report.school_name = workspace.school_name
        report.principal_name = workspace.principal_name
    report.save()
    return report


def _inline_evidence_forms(request, report=None):
    if request.method == "POST" and "evidence-TOTAL_FORMS" not in request.POST:
        return None  # Existing clients may still post only the report fields.
    formset_class = PersonalInlineEvidenceFormSet if report is None else personal_inline_evidence_formset(
        max(0, 8 - report.evidence.count())
    )
    return formset_class(
        request.POST or None, request.FILES or None, prefix="evidence"
    )


def _save_inline_evidence(formset, workspace, report):
    if formset is None:
        return
    next_order = report.evidence.aggregate(max_order=Max("order"))["max_order"] or 0
    for row in formset.cleaned_data:
        if not row or not (row.get("file") or row.get("source_url")):
            continue
        uploaded = row.get("file")
        next_order += 1
        PersonalEvidence.objects.create(
            workspace=workspace, report=report, title=row["title"],
            academic_year=report.academic_year, file=uploaded,
            source_url=row.get("source_url") or "", file_size=uploaded.size if uploaded else 0,
            order=next_order,
            display_size=row.get("display_size") or PersonalEvidence.DisplaySize.AUTO,
            fit_mode=row.get("fit_mode") or PersonalEvidence.FitMode.CONTAIN,
            show_in_print=row.get("show_in_print", False) if row.get("presentation_enabled") else True,
        )


def _inline_evidence_capacity_error(workspace, subscription, formset, report=None):
    if formset is None:
        return ""
    new_rows = [row for row in formset.cleaned_data if row and (row.get("file") or row.get("source_url"))]
    existing_count = report.evidence.count() if report else 0
    if existing_count + len(new_rows) > 8:
        return "الحد الأعلى لكل تقرير 8 شواهد. أزل شاهدًا قبل إضافة المزيد."
    if workspace.evidence.count() + len(new_rows) > subscription.plan.max_evidence:
        return "وصلت إلى الحد الحالي للشواهد الشخصية."
    used = workspace.evidence.aggregate(total=Sum("file_size"))["total"] or 0
    added = sum(row["file"].size for row in new_rows if row.get("file"))
    if used + added > subscription.plan.storage_limit_mb * 1024 * 1024:
        return "تجاوزت الملفات سعة باقتك الحالية."
    return ""


def _uses_school_report_editor(request):
    return request.method != "POST" or "section_selection_enabled" in request.POST


def _personal_report_image_queryset(report):
    if report is None or not report.pk:
        return PersonalEvidence.objects.none()
    image_suffixes = Q()
    for suffix in (".jpg", ".jpeg", ".png", ".webp"):
        image_suffixes |= Q(file__iendswith=suffix)
    return PersonalEvidence.objects.filter(report=report, workspace=report.workspace).filter(
        image_suffixes
    ).order_by("order", "id")


def _school_image_evidence_forms(request, report=None):
    instance = report if report is not None else PersonalReport()
    image_queryset = _personal_report_image_queryset(report)
    image_count = image_queryset.count() if report is not None else 0
    document_count = (report.evidence.count() - image_count) if report is not None else 0
    image_limit = max(image_count, 8 - document_count)
    formset_class = personal_report_evidence_formset(image_limit)
    data = request.POST if request.method == "POST" else None
    if data is not None and "evidence-TOTAL_FORMS" not in data:
        data = data.copy()
        data["evidence-TOTAL_FORMS"] = "0"
        data["evidence-INITIAL_FORMS"] = "0"
        data["evidence-MIN_NUM_FORMS"] = "0"
        data["evidence-MAX_NUM_FORMS"] = str(image_limit)
    return formset_class(
        data, request.FILES if request.method == "POST" else None,
        instance=instance, queryset=image_queryset, prefix="evidence",
    )


def _school_image_capacity_error(workspace, subscription, formset, report=None):
    current_report_count = report.evidence.count() if report else 0
    new_count = 0
    removed_from_report = 0
    physically_deleted = 0
    added_bytes = 0
    released_bytes = 0
    for form in formset.forms:
        row = getattr(form, "cleaned_data", None)
        if not row:
            continue
        old = form.instance
        image = row.get("image")
        if row.get("DELETE"):
            if old.pk:
                removed_from_report += 1
                if not old.portfolio_links.exists() and not old.initiative_id:
                    physically_deleted += 1
                    released_bytes += old.file_size
        elif old.pk:
            if image:
                added_bytes += image.size
                released_bytes += old.file_size
        elif image:
            new_count += 1
            added_bytes += image.size
    if current_report_count + new_count - removed_from_report > 8:
        return "الحد الأعلى لكل تقرير 8 شواهد. أزل شاهدًا قبل إضافة المزيد."
    if workspace.evidence.count() + new_count - physically_deleted > subscription.plan.max_evidence:
        return "وصلت إلى الحد الحالي للشواهد الشخصية."
    used = workspace.evidence.aggregate(total=Sum("file_size"))["total"] or 0
    if used + added_bytes - released_bytes > subscription.plan.storage_limit_mb * 1024 * 1024:
        return "تجاوزت الملفات سعة باقتك الحالية."
    return ""


def _save_school_image_evidence(formset, workspace, report):
    highest_order = report.evidence.aggregate(max_order=Max("order"))["max_order"] or 0
    active = []
    for form in formset.forms:
        row = getattr(form, "cleaned_data", None)
        if not row:
            continue
        if row.get("DELETE"):
            if form.instance.pk:
                witness = get_object_or_404(
                    PersonalEvidence.objects.select_for_update(),
                    pk=form.instance.pk, workspace=workspace, report=report,
                )
                # A portfolio or initiative may still need the owned library item.
                if witness.portfolio_links.exists() or witness.initiative_id:
                    witness.report = None
                    witness.save(update_fields=["report"])
                else:
                    witness.delete()
            continue
        if form.instance.pk or row.get("image"):
            active.append(form)
    active.sort(key=lambda form: (form.cleaned_data.get("order") or 999, form.prefix))
    existing_ids = [form.instance.pk for form in active if form.instance.pk]
    existing_slots = list(report.evidence.filter(pk__in=existing_ids).values_list("order", flat=True))
    new_slots = [highest_order + index for index in range(1, len(active) - len(existing_ids) + 1)]
    # Reuse image positions in submitted order, including when a new image is
    # dragged ahead of an existing one. Keep PDF/link positions untouched.
    image_slots = sorted(existing_slots + new_slots)
    for form, order in zip(active, image_slots, strict=True):
        old_file_name = ""
        old_storage = None
        if form.instance.pk:
            current = get_object_or_404(
                PersonalEvidence.objects.select_for_update(),
                pk=form.instance.pk, workspace=workspace, report=report,
            )
            if form.cleaned_data.get("image") and current.file:
                old_file_name = current.file.name
                old_storage = current.file.storage
            form.instance = current
        witness = form.save(commit=False)
        witness.workspace = workspace
        witness.report = report
        witness.academic_year = report.academic_year
        witness.order = order
        witness.save()
        if old_file_name and old_storage and old_file_name != witness.file.name:
            transaction.on_commit(
                lambda name=old_file_name, storage=old_storage: storage.delete(name),
                robust=True,
            )


@workspace_required
@never_cache
@ratelimit(key="user", rate="20/m", method="POST", block=True)
@require_POST
def review_report_readiness(request):
    """Run the shared free structural review for an unsaved personal draft."""
    if not request.personal_subscription.is_current:
        return JsonResponse({"ok": False, "message": "اشتراك المساحة الشخصية غير نشط حاليًا."}, status=403)
    if request.content_type != "application/json":
        return JsonResponse({"ok": False, "message": "صيغة الطلب غير صحيحة."}, status=415)
    if len(request.body) > 40000:
        return JsonResponse({"ok": False, "message": "نص التقرير أطول من الحد المسموح."}, status=413)
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = None
    if not isinstance(payload, dict):
        return JsonResponse({"ok": False, "message": "تعذر قراءة بيانات التقرير."}, status=400)
    draft = normalise_draft(payload, beneficiaries_label="المستفيدين")
    result = review_draft(draft, semantic=False)
    personal_hints = {
        "title": "اذكر اسم النشاط أو البرنامج بوضوح.",
        "category": "اختر النوع الذي يصف هذا العمل ليسهل تنظيم تقاريرك.",
        "evidence": "أرفق صورة للشاهد إن كانت متاحة لديك.",
    }
    for issue in result["issues"]:
        if issue["field"] in personal_hints:
            issue["hint"] = personal_hints[issue["field"]]
    result["headline"] = {
        "ready": "التقرير مكتمل بنيويًا" if not result["issues"] else "التقرير مكتمل بنيويًا، وفيه ما يمكن تحسينه",
        "almost": "قريب من الاكتمال",
        "needs_work": "يحتاج استكمالًا قبل الحفظ",
    }[result["level"]]
    result.update({"remaining": 0, "daily_limit": 0, "reason": "structural_only"})
    response = JsonResponse({"ok": True, **result}, json_dumps_params={"ensure_ascii": False})
    response["Cache-Control"] = "no-store"
    return response


@workspace_required
@ratelimit(key="user", rate="30/h", method="POST", block=True)
@require_http_methods(["GET", "POST"])
def report_create(request):
    ws = request.personal_workspace
    subscription = request.personal_subscription
    if not subscription.is_current:
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"ok": False, "message": "اشتراك المساحة الشخصية غير نشط حاليًا."}, status=403)
        messages.error(request, "اشتراك المساحة الشخصية غير نشط حاليًا. أعمالك المحفوظة متاحة للقراءة.")
        return redirect("personal:dashboard")
    if ws.reports.count() >= subscription.plan.max_reports:
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"ok": False, "message": "وصلت إلى حد التقارير الشخصية في باقتك."}, status=422)
        messages.error(request, "وصلت إلى الحد الحالي للتقارير الشخصية. تواصل مع الدعم لزيادة السعة.")
        return redirect("personal:reports")
    school_editor = _uses_school_report_editor(request)
    if school_editor:
        form = PersonalSchoolParityReportForm(
            request.POST or None, workspace=ws,
            initial={
                "report_date": timezone.localdate(), "show_goal": False,
                "show_details": False, "show_implementation": False,
                "show_results": False, "show_recommendations": False,
                "show_beneficiaries": False,
            },
        )
        evidence_formset = _school_image_evidence_forms(request)
    else:
        form = PersonalReportForm(request.POST or None, initial={
            "report_date": timezone.localdate(), "academic_year": ws.current_academic_year,
            "show_details": True,
            "show_goals": False, "show_implementation": False,
            "show_results": False, "show_recommendations": False,
            "client_submission_id": uuid.uuid4(),
        })
        evidence_formset = _inline_evidence_forms(request)
    if request.method == "POST" and form.is_valid() and (evidence_formset is None or evidence_formset.is_valid()):
        submission_id = form.cleaned_data.get("client_submission_id")
        try:
            with transaction.atomic():
                locked = PersonalWorkspace.objects.select_for_update().get(pk=ws.pk)
                existing = locked.reports.filter(client_submission_id=submission_id).first() if submission_id else None
                if existing:
                    messages.info(request, "حُفظ هذا التقرير مسبقًا.")
                    return redirect("personal:report_trash") if existing.trashed_at else redirect(
                        "personal:report_detail", pk=existing.pk,
                    )
                if locked.reports.count() >= subscription.plan.max_reports:
                    raise ValidationError("وصلت إلى الحد الحالي للتقارير الشخصية.")
                ensure_writable_personal_year(locked, form.cleaned_data["academic_year"])
                capacity_error = (
                    _school_image_capacity_error(locked, subscription, evidence_formset)
                    if school_editor else
                    _inline_evidence_capacity_error(locked, subscription, evidence_formset)
                )
                if capacity_error:
                    raise ValidationError(capacity_error)
                report = _save_report(form, locked, request.user, submission_id=submission_id)
                if school_editor:
                    _save_school_image_evidence(evidence_formset, locked, report)
                else:
                    _save_inline_evidence(evidence_formset, locked, report)
        except ValidationError as exc:
            form.add_error(None, exc)
        except IntegrityError:
            existing = ws.reports.filter(client_submission_id=submission_id).first() if submission_id else None
            if existing:
                messages.info(request, "حُفظ هذا التقرير مسبقًا.")
                return redirect("personal:report_trash") if existing.trashed_at else redirect(
                    "personal:report_detail", pk=existing.pk,
                )
            raise
        else:
            messages.success(request, "حُفظ التقرير وشواهده في مساحتك الشخصية.")
            return redirect("personal:report_detail", pk=report.pk)
    return render(request, "personal/report_form.html", {
        "form": form, "evidence_formset": evidence_formset, "editing": False,
        "personal_report_review_enabled": True,
        **personal_assistant_template_context(request.user, subscription),
    }, status=422 if request.method == "POST" and request.headers.get("X-Requested-With") == "XMLHttpRequest" else 200)


@workspace_required
@require_http_methods(["GET", "POST"])
def report_edit(request, pk):
    report = get_object_or_404(
        PersonalReport, pk=pk, workspace=request.personal_workspace, trashed_at__isnull=True,
    )
    if PersonalAcademicYear.objects.filter(
        workspace=request.personal_workspace, value=report.academic_year, archived_at__isnull=False
    ).exists():
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"ok": False, "message": "السنة الأصلية مؤرشفة. أعد فتحها قبل تعديل التقرير."}, status=403)
        messages.error(request, "السنة الأصلية مؤرشفة. أعد فتحها قبل تعديل التقرير.")
        return redirect("personal:report_detail", pk=pk)
    if not request.personal_subscription.is_current:
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"ok": False, "message": "اشتراك المساحة الشخصية غير نشط حاليًا."}, status=403)
        messages.error(request, "اشتراك المساحة الشخصية غير نشط حاليًا.")
        return redirect("personal:report_detail", pk=pk)
    school_editor = _uses_school_report_editor(request)
    if school_editor:
        form = PersonalSchoolParityReportForm(request.POST or None, instance=report, workspace=request.personal_workspace)
        evidence_formset = _school_image_evidence_forms(request, report)
    else:
        form = PersonalReportForm(request.POST or None, instance=report)
        evidence_formset = _inline_evidence_forms(request, report)
    if request.method == "POST" and form.is_valid() and (evidence_formset is None or evidence_formset.is_valid()):
        try:
            with transaction.atomic():
                locked = PersonalWorkspace.objects.select_for_update().get(pk=request.personal_workspace.pk)
                get_object_or_404(PersonalReport, pk=report.pk, workspace=locked, trashed_at__isnull=True)
                ensure_writable_personal_year(locked, form.cleaned_data["academic_year"])
                capacity_error = (
                    _school_image_capacity_error(
                        locked, request.personal_subscription, evidence_formset, report=report,
                    ) if school_editor else _inline_evidence_capacity_error(
                        locked, request.personal_subscription, evidence_formset, report=report,
                    )
                )
                if capacity_error:
                    raise ValidationError(capacity_error)
                report = _save_report(form, locked, request.user)
                if school_editor:
                    _save_school_image_evidence(evidence_formset, locked, report)
                else:
                    _save_inline_evidence(evidence_formset, locked, report)
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            messages.success(request, "حُفظت التعديلات والشواهد.")
            return redirect("personal:report_detail", pk=report.pk)
    return render(request, "personal/report_form.html", {
        "form": form, "evidence_formset": evidence_formset, "editing": True, "report": report,
        "existing_evidence": report.evidence.order_by("order", "id"),
        "existing_documents": report.evidence.exclude(pk__in=_personal_report_image_queryset(report).values("pk")).order_by("order", "id"),
        "personal_report_review_enabled": True,
        "legacy_long_description": len(report.description or "") > 600,
        **personal_assistant_template_context(request.user, request.personal_subscription),
    }, status=422 if request.method == "POST" and request.headers.get("X-Requested-With") == "XMLHttpRequest" else 200)


@workspace_required
@require_GET
def report_detail(request, pk):
    report = get_object_or_404(
        PersonalReport, pk=pk, workspace=request.personal_workspace, trashed_at__isnull=True,
    )
    can_modify = request.personal_subscription.is_current and not PersonalAcademicYear.objects.filter(
        workspace=request.personal_workspace, value=report.academic_year, archived_at__isnull=False
    ).exists()
    return render(request, "personal/report_detail.html", {
        "report": report, "evidence": report.evidence.order_by("order", "id"), "can_modify": can_modify,
    })


@workspace_required
@require_POST
def report_mark_complete(request, pk):
    with transaction.atomic():
        workspace = PersonalWorkspace.objects.select_for_update().get(pk=request.personal_workspace.pk)
        report = get_object_or_404(
            PersonalReport.objects.select_for_update(),
            pk=pk, workspace=workspace, trashed_at__isnull=True,
        )
        if not request.personal_subscription.is_current or PersonalAcademicYear.objects.filter(
            workspace=workspace, value=report.academic_year, archived_at__isnull=False,
        ).exists():
            messages.error(request, "لا يمكن إكمال تقرير من سنة مؤرشفة أو اشتراك غير نشط.")
        elif report.status == PersonalReport.Status.DRAFT:
            report.status = PersonalReport.Status.COMPLETE
            report.save(update_fields=["status", "updated_at"])
            messages.success(request, "اكتمل التقرير في مساحتك الشخصية.")
    return redirect("personal:report_detail", pk=pk)


@workspace_required
@require_POST
def report_delete(request, pk):
    with transaction.atomic():
        locked = PersonalWorkspace.objects.select_for_update().get(pk=request.personal_workspace.pk)
        report = get_object_or_404(PersonalReport, pk=pk, workspace=locked, trashed_at__isnull=True)
        if not request.personal_subscription.is_current or PersonalAcademicYear.objects.filter(
            workspace=locked, value=report.academic_year, archived_at__isnull=False
        ).exists():
            messages.error(request, "لا يمكن نقل تقرير من سنة مؤرشفة أو اشتراك غير نشط.")
            return redirect("personal:report_detail", pk=pk)
        report.move_to_trash(by=request.user)
    messages.success(request, "نُقل التقرير إلى السلة. بقيت شواهده وروابط ملف الإنجاز محفوظة للاستعادة.")
    return redirect("personal:reports")


@workspace_required
@require_GET
def report_trash(request):
    reports = request.personal_workspace.reports.filter(trashed_at__isnull=False).select_related(
        "trashed_by"
    ).order_by("-trashed_at", "-id")
    page = Paginator(reports, 20).get_page(request.GET.get("page"))
    archived_years = set(request.personal_workspace.academic_years.filter(
        archived_at__isnull=False
    ).values_list("value", flat=True))
    return render(request, "personal/report_trash.html", {
        "reports": page, "subscription_active": request.personal_subscription.is_current,
        "archived_years": archived_years,
    })


@workspace_required
@require_POST
def report_restore(request, pk):
    with transaction.atomic():
        locked = PersonalWorkspace.objects.select_for_update().get(pk=request.personal_workspace.pk)
        report = get_object_or_404(PersonalReport, pk=pk, workspace=locked, trashed_at__isnull=False)
        if not request.personal_subscription.is_current:
            messages.error(request, "يلزم اشتراك نشط لاستعادة التقرير.")
            return redirect("personal:report_trash")
        if PersonalAcademicYear.objects.filter(
            workspace=locked, value=report.academic_year, archived_at__isnull=False,
        ).exists():
            messages.error(request, "السنة الدراسية مؤرشفة. أعد فتحها قبل استعادة التقرير.")
            return redirect("personal:report_trash")
        report.restore_from_trash()
    messages.success(request, "استُعيد التقرير وشواهده وروابط ملف الإنجاز.")
    return redirect("personal:report_detail", pk=pk)


@workspace_required
@require_GET
def report_print(request, pk):
    report = get_object_or_404(
        PersonalReport, pk=pk, workspace=request.personal_workspace, trashed_at__isnull=True,
    )
    evidence = list(report.evidence.filter(show_in_print=True).order_by("order", "id"))
    response = render(request, "personal/report_print.html", {
        "document_title": report.title, "report": report,
        "evidence": evidence,
        "print_image_count": sum(item.is_image for item in evidence),
        "workspace": request.personal_workspace,
    })
    response["Cache-Control"] = "no-store"
    return response


@workspace_required
@require_GET
def evidence_list(request):
    year = (request.GET.get("year") or "").strip()[:20]
    qs = request.personal_workspace.evidence.select_related("report")
    if year:
        qs = qs.filter(academic_year=year)
    years = request.personal_workspace.evidence.order_by("-academic_year").values_list("academic_year", flat=True).distinct()
    page = Paginator(qs, 20).get_page(request.GET.get("page"))
    return render(request, "personal/evidence_list.html", {
        "evidence": page, "year": year, "years": years,
        "subscription_active": request.personal_subscription.is_current,
        "archived_years": set(request.personal_workspace.academic_years.filter(
            archived_at__isnull=False
        ).values_list("value", flat=True)),
    })


@workspace_required
@ratelimit(key="user", rate="30/h", method="POST", block=True)
@require_http_methods(["GET", "POST"])
def evidence_create(request):
    ws = request.personal_workspace
    subscription = request.personal_subscription
    if not subscription.is_current:
        messages.error(request, "اشتراك المساحة الشخصية غير نشط حاليًا. شواهدك المحفوظة متاحة للقراءة.")
        return redirect("personal:dashboard")
    if ws.evidence.count() >= subscription.plan.max_evidence:
        messages.error(request, "وصلت إلى الحد الحالي للشواهد الشخصية. تواصل مع الدعم لزيادة السعة.")
        return redirect("personal:evidence")
    initial = {}
    report_id = request.GET.get("report")
    initiative_id = request.GET.get("initiative")
    if report_id:
        report = PersonalReport.objects.filter(pk=report_id, workspace=ws, trashed_at__isnull=True).first()
        if report:
            initial = {"report": report, "academic_year": report.academic_year}
    elif initiative_id:
        initiative = ws.initiatives.filter(pk=initiative_id).first()
        if initiative:
            initial = {"initiative": initiative, "academic_year": initiative.academic_year}
    else:
        initial = {"academic_year": ws.current_academic_year}
    form = PersonalEvidenceForm(
        request.POST or None, request.FILES or None, workspace=ws, initial=initial
    )
    if request.method == "POST" and form.is_valid():
        uploaded = form.cleaned_data.get("file")
        new_size = uploaded.size if uploaded else 0
        with transaction.atomic():
            locked = PersonalWorkspace.objects.select_for_update().get(pk=ws.pk)
            try:
                ensure_writable_personal_year(locked, form.cleaned_data["academic_year"])
            except ValidationError as exc:
                form.add_error("academic_year", exc)
            used = locked.evidence.aggregate(total=Sum("file_size"))["total"] or 0
            if form.errors:
                pass
            elif locked.evidence.count() >= subscription.plan.max_evidence:
                form.add_error(None, "وصلت إلى الحد الحالي للشواهد الشخصية.")
            elif form.cleaned_data.get("report") and locked.evidence.filter(
                report=form.cleaned_data["report"]
            ).count() >= 8:
                form.add_error("report", "الحد الأعلى للتقرير 8 شواهد.")
            elif used + new_size > subscription.plan.storage_limit_mb * 1024 * 1024:
                form.add_error("file", f"تجاوزت الملفات سعة باقتك الحالية ({subscription.plan.storage_limit_mb} ميجابايت).")
            else:
                obj = form.save(commit=False)
                obj.workspace = locked
                obj.file_size = new_size
                if obj.report_id:
                    obj.order = (locked.evidence.filter(report_id=obj.report_id).aggregate(
                        max_order=Max("order")
                    )["max_order"] or 0) + 1
                obj.save()
                messages.success(request, "أُضيف الشاهد إلى مكتبتك الشخصية.")
                return redirect("personal:evidence")
    return render(request, "personal/evidence_form.html", {"form": form})


@workspace_required
@ratelimit(key="user", rate="30/h", method="POST", block=True)
@require_http_methods(["GET", "POST"])
def evidence_edit(request, pk):
    ws = request.personal_workspace
    evidence = get_object_or_404(PersonalEvidence, pk=pk, workspace=ws)
    if not request.personal_subscription.is_current or PersonalAcademicYear.objects.filter(
        workspace=ws, value=evidence.academic_year, archived_at__isnull=False
    ).exists():
        messages.error(request, "لا يمكن تعديل شاهد من سنة مؤرشفة أو اشتراك غير نشط.")
        return redirect("personal:evidence")
    form = PersonalEvidenceForm(
        request.POST or None, request.FILES or None, workspace=ws, instance=evidence
    )
    if request.method == "POST" and form.is_valid():
        uploaded = form.cleaned_data.get("file")
        new_size = (
            uploaded.size if isinstance(uploaded, UploadedFile)
            else evidence.file_size if uploaded else 0
        )
        with transaction.atomic():
            locked = PersonalWorkspace.objects.select_for_update().get(pk=ws.pk)
            current = get_object_or_404(PersonalEvidence.objects.select_for_update(), pk=pk, workspace=locked)
            try:
                ensure_writable_personal_year(locked, current.academic_year)
                ensure_writable_personal_year(locked, form.cleaned_data["academic_year"])
            except ValidationError as exc:
                form.add_error("academic_year", exc)
            if not form.errors and current.academic_year != form.cleaned_data["academic_year"]:
                if current.portfolio_links.exists():
                    form.add_error("academic_year", "لا يمكن تغيير سنة شاهد مرتبط بمحور ملف الإنجاز.")
            used = locked.evidence.aggregate(total=Sum("file_size"))["total"] or 0
            if not form.errors and used - current.file_size + new_size > request.personal_subscription.plan.storage_limit_mb * 1024 * 1024:
                form.add_error("file", "تجاوزت الملفات سعة باقتك الحالية.")
            target_report = form.cleaned_data.get("report")
            if not form.errors and target_report and locked.evidence.filter(
                report=target_report
            ).exclude(pk=current.pk).count() >= 8:
                form.add_error("report", "الحد الأعلى للتقرير 8 شواهد.")
            if not form.errors:
                obj = form.save(commit=False)
                obj.workspace = locked
                obj.file_size = new_size
                if obj.report_id != current.report_id:
                    obj.order = (locked.evidence.filter(report_id=obj.report_id).aggregate(
                        max_order=Max("order")
                    )["max_order"] or 0) + 1 if obj.report_id else 1
                obj.save()
                messages.success(request, "حُفظت تعديلات الشاهد.")
                if obj.report_id and not obj.report.trashed_at:
                    return redirect("personal:report_detail", pk=obj.report_id)
                return redirect("personal:evidence")
    return render(request, "personal/evidence_form.html", {
        "form": form, "editing": True, "evidence_item": evidence,
    })


@workspace_required
@require_POST
def report_evidence_move(request, pk):
    report = get_object_or_404(
        PersonalReport, pk=pk, workspace=request.personal_workspace, trashed_at__isnull=True,
    )
    if not request.personal_subscription.is_current or PersonalAcademicYear.objects.filter(
        workspace=request.personal_workspace, value=report.academic_year, archived_at__isnull=False
    ).exists():
        messages.error(request, "لا يمكن ترتيب شواهد سنة مؤرشفة أو اشتراك غير نشط.")
        return redirect("personal:report_detail", pk=pk)
    direction = request.POST.get("direction")
    if direction not in {"up", "down"}:
        raise Http404
    with transaction.atomic():
        rows = list(PersonalEvidence.objects.select_for_update().filter(
            workspace=request.personal_workspace, report=report
        ).order_by("order", "id"))
        try:
            position = next(index for index, item in enumerate(rows) if item.pk == int(request.POST.get("evidence_id", "")))
        except (ValueError, StopIteration):
            raise Http404 from None
        target = position - 1 if direction == "up" else position + 1
        if 0 <= target < len(rows):
            rows[position], rows[target] = rows[target], rows[position]
            for index, item in enumerate(rows, start=1):
                item.order = index
            PersonalEvidence.objects.bulk_update(rows, ["order"])
    return redirect("personal:report_detail", pk=pk)


@workspace_required
@require_GET
def evidence_download(request, pk):
    evidence = get_object_or_404(PersonalEvidence, pk=pk, workspace=request.personal_workspace)
    if not evidence.file:
        raise Http404
    try:
        evidence.file.open("rb")
    except (OSError, FileNotFoundError):
        raise Http404 from None
    response = FileResponse(evidence.file, as_attachment=True, filename=Path(evidence.file.name).name)
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@workspace_required
@require_GET
def evidence_preview(request, pk):
    evidence = get_object_or_404(PersonalEvidence, pk=pk, workspace=request.personal_workspace)
    suffix = Path(evidence.file.name).suffix.lower() if evidence.file else ""
    if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
        raise Http404
    try:
        evidence.file.open("rb")
    except (OSError, FileNotFoundError):
        raise Http404 from None
    response = FileResponse(
        evidence.file, as_attachment=False,
        content_type={".png": "image/png", ".webp": "image/webp"}.get(suffix, "image/jpeg"),
    )
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@workspace_required
@require_POST
def evidence_delete(request, pk):
    evidence = get_object_or_404(PersonalEvidence, pk=pk, workspace=request.personal_workspace)
    if not request.personal_subscription.is_current or PersonalAcademicYear.objects.filter(
        workspace=request.personal_workspace, value=evidence.academic_year, archived_at__isnull=False
    ).exists():
        messages.error(request, "لا يمكن حذف شاهد من سنة مؤرشفة أو اشتراك غير نشط.")
        return redirect("personal:evidence")
    evidence.delete()
    messages.success(request, "حُذف الشاهد.")
    return redirect("personal:evidence")
