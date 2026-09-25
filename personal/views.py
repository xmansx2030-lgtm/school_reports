from functools import wraps
from io import BytesIO
import logging
from pathlib import Path

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Q, Sum
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
    PersonalWorkspaceForm,
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
    payments = PersonalPayment.objects.filter(workspace=request.personal_workspace).select_related("plan")[:50]
    return render(request, "personal/billing.html", {
        "workspace": request.personal_workspace,
        "subscription": request.personal_subscription,
        "payments": payments,
        "plans": PersonalPlan.objects.filter(is_active=True, is_published=True).order_by("display_order", "price", "id"),
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
    recent_reports = ws.reports.all()[:5]
    recent_evidence = ws.evidence.all()[:5]
    stats = {
        "reports": ws.reports.count(),
        "complete": ws.reports.filter(status=PersonalReport.Status.COMPLETE).count(),
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
    qs = request.personal_workspace.reports.all()
    query = (request.GET.get("q") or "").strip()[:100]
    year = (request.GET.get("year") or "").strip()[:20]
    status = (request.GET.get("status") or "").strip()
    if query:
        qs = qs.filter(Q(title__icontains=query) | Q(category__icontains=query) | Q(description__icontains=query))
    if year:
        qs = qs.filter(academic_year=year)
    if status in PersonalReport.Status.values:
        qs = qs.filter(status=status)
    years = request.personal_workspace.reports.order_by("-academic_year").values_list("academic_year", flat=True).distinct()
    page = Paginator(qs, 20).get_page(request.GET.get("page"))
    return render(request, "personal/report_list.html", {
        "reports": page, "query": query, "year": year, "status": status,
        "years": years, "statuses": PersonalReport.Status.choices,
    })


def _save_report(form, workspace, owner):
    report = form.save(commit=False)
    report.workspace = workspace
    if not report.pk:
        report.teacher_name = owner.name
        report.school_name = workspace.school_name
        report.principal_name = workspace.principal_name
    report.save()
    return report


def _inline_evidence_forms(request):
    if request.method == "POST" and "evidence-TOTAL_FORMS" not in request.POST:
        return None  # Existing clients may still post only the report fields.
    return PersonalInlineEvidenceFormSet(
        request.POST or None, request.FILES or None, prefix="evidence"
    )


def _save_inline_evidence(formset, workspace, report):
    if formset is None:
        return
    for row in formset.cleaned_data:
        if not row or not (row.get("file") or row.get("source_url")):
            continue
        uploaded = row.get("file")
        PersonalEvidence.objects.create(
            workspace=workspace, report=report, title=row["title"],
            academic_year=report.academic_year, file=uploaded,
            source_url=row.get("source_url") or "", file_size=uploaded.size if uploaded else 0,
        )


def _inline_evidence_capacity_error(workspace, subscription, formset):
    if formset is None:
        return ""
    new_rows = [row for row in formset.cleaned_data if row and (row.get("file") or row.get("source_url"))]
    if workspace.evidence.count() + len(new_rows) > subscription.plan.max_evidence:
        return "وصلت إلى الحد الحالي للشواهد الشخصية."
    used = workspace.evidence.aggregate(total=Sum("file_size"))["total"] or 0
    added = sum(row["file"].size for row in new_rows if row.get("file"))
    if used + added > subscription.plan.storage_limit_mb * 1024 * 1024:
        return "تجاوزت الملفات سعة باقتك الحالية."
    return ""


@workspace_required
@ratelimit(key="user", rate="30/h", method="POST", block=True)
@require_http_methods(["GET", "POST"])
def report_create(request):
    ws = request.personal_workspace
    subscription = request.personal_subscription
    if not subscription.is_current:
        messages.error(request, "اشتراك المساحة الشخصية غير نشط حاليًا. أعمالك المحفوظة متاحة للقراءة.")
        return redirect("personal:dashboard")
    if ws.reports.count() >= subscription.plan.max_reports:
        messages.error(request, "وصلت إلى الحد الحالي للتقارير الشخصية. تواصل مع الدعم لزيادة السعة.")
        return redirect("personal:reports")
    form = PersonalReportForm(request.POST or None, initial={
        "report_date": timezone.localdate(), "academic_year": ws.current_academic_year,
        "show_goals": False, "show_implementation": False,
        "show_results": False, "show_recommendations": False,
    })
    evidence_formset = _inline_evidence_forms(request)
    if request.method == "POST" and form.is_valid() and (evidence_formset is None or evidence_formset.is_valid()):
        try:
            with transaction.atomic():
                locked = PersonalWorkspace.objects.select_for_update().get(pk=ws.pk)
                if locked.reports.count() >= subscription.plan.max_reports:
                    raise ValidationError("وصلت إلى الحد الحالي للتقارير الشخصية.")
                ensure_writable_personal_year(locked, form.cleaned_data["academic_year"])
                capacity_error = _inline_evidence_capacity_error(locked, subscription, evidence_formset)
                if capacity_error:
                    raise ValidationError(capacity_error)
                report = _save_report(form, locked, request.user)
                _save_inline_evidence(evidence_formset, locked, report)
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            messages.success(request, "حُفظ التقرير وشواهده في مساحتك الشخصية.")
            return redirect("personal:report_detail", pk=report.pk)
    return render(request, "personal/report_form.html", {"form": form, "evidence_formset": evidence_formset, "editing": False})


@workspace_required
@require_http_methods(["GET", "POST"])
def report_edit(request, pk):
    report = get_object_or_404(PersonalReport, pk=pk, workspace=request.personal_workspace)
    if request.method == "POST" and PersonalAcademicYear.objects.filter(
        workspace=request.personal_workspace, value=report.academic_year, archived_at__isnull=False
    ).exists():
        messages.error(request, "السنة الأصلية مؤرشفة. أعد فتحها قبل تعديل التقرير.")
        return redirect("personal:report_detail", pk=pk)
    if request.method == "POST" and not request.personal_subscription.is_current:
        messages.error(request, "اشتراك المساحة الشخصية غير نشط حاليًا.")
        return redirect("personal:report_detail", pk=pk)
    form = PersonalReportForm(request.POST or None, instance=report)
    evidence_formset = _inline_evidence_forms(request)
    if request.method == "POST" and form.is_valid() and (evidence_formset is None or evidence_formset.is_valid()):
        try:
            with transaction.atomic():
                locked = PersonalWorkspace.objects.select_for_update().get(pk=request.personal_workspace.pk)
                ensure_writable_personal_year(locked, form.cleaned_data["academic_year"])
                capacity_error = _inline_evidence_capacity_error(locked, request.personal_subscription, evidence_formset)
                if capacity_error:
                    raise ValidationError(capacity_error)
                report = _save_report(form, locked, request.user)
                _save_inline_evidence(evidence_formset, locked, report)
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            messages.success(request, "حُفظت التعديلات والشواهد.")
            return redirect("personal:report_detail", pk=report.pk)
    return render(request, "personal/report_form.html", {
        "form": form, "evidence_formset": evidence_formset, "editing": True, "report": report,
        "existing_evidence": report.evidence.all(),
    })


@workspace_required
@require_GET
def report_detail(request, pk):
    report = get_object_or_404(PersonalReport, pk=pk, workspace=request.personal_workspace)
    return render(request, "personal/report_detail.html", {"report": report, "evidence": report.evidence.all()})


@workspace_required
@require_POST
def report_delete(request, pk):
    report = get_object_or_404(PersonalReport, pk=pk, workspace=request.personal_workspace)
    if not request.personal_subscription.is_current or PersonalAcademicYear.objects.filter(
        workspace=request.personal_workspace, value=report.academic_year, archived_at__isnull=False
    ).exists():
        messages.error(request, "لا يمكن حذف عمل من سنة مؤرشفة أو اشتراك غير نشط.")
        return redirect("personal:report_detail", pk=pk)
    report.delete()
    messages.success(request, "حُذف التقرير. بقيت الشواهد في مكتبتك الشخصية.")
    return redirect("personal:reports")


@workspace_required
@require_GET
def report_print(request, pk):
    report = get_object_or_404(PersonalReport, pk=pk, workspace=request.personal_workspace)
    response = render(request, "personal/print.html", {
        "document_title": report.title, "report": report, "evidence": report.evidence.all(),
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
    return render(request, "personal/evidence_list.html", {"evidence": page, "year": year, "years": years})


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
        report = PersonalReport.objects.filter(pk=report_id, workspace=ws).first()
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
            elif used + new_size > subscription.plan.storage_limit_mb * 1024 * 1024:
                form.add_error("file", f"تجاوزت الملفات سعة باقتك الحالية ({subscription.plan.storage_limit_mb} ميجابايت).")
            else:
                obj = form.save(commit=False)
                obj.workspace = locked
                obj.file_size = new_size
                obj.save()
                messages.success(request, "أُضيف الشاهد إلى مكتبتك الشخصية.")
                return redirect("personal:evidence")
    return render(request, "personal/evidence_form.html", {"form": form})


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
    if suffix not in {".jpg", ".jpeg", ".png"}:
        raise Http404
    try:
        evidence.file.open("rb")
    except (OSError, FileNotFoundError):
        raise Http404 from None
    response = FileResponse(
        evidence.file, as_attachment=False,
        content_type="image/png" if suffix == ".png" else "image/jpeg",
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


@workspace_required
@require_GET
def portfolio(request):
    ws = request.personal_workspace
    years = sorted(set(ws.reports.values_list("academic_year", flat=True)) |
                   set(ws.evidence.values_list("academic_year", flat=True)) |
                   set(ws.initiatives.values_list("academic_year", flat=True)) |
                   set(ws.academic_years.values_list("value", flat=True)), reverse=True)
    year = (request.GET.get("year") or "").strip()[:20]
    if year not in years:
        year = years[0] if years else ""
    reports = ws.reports.filter(academic_year=year) if year else ws.reports.none()
    evidence = ws.evidence.filter(academic_year=year) if year else ws.evidence.none()
    initiatives = ws.initiatives.filter(academic_year=year) if year else ws.initiatives.none()
    categories = reports.values("category").annotate(total=Count("id")).order_by("-total", "category")
    school_names = list(reports.order_by("school_name").values_list("school_name", flat=True).distinct()) if year else []
    principal_names = list(reports.exclude(principal_name="").order_by("principal_name").values_list("principal_name", flat=True).distinct()) if year else []
    return render(request, "personal/portfolio.html", {
        "workspace": ws, "years": years, "year": year,
        "reports": reports, "evidence": evidence, "initiatives": initiatives, "categories": categories,
        "school_names": school_names or [ws.school_name],
        "principal_names": principal_names or ([ws.principal_name] if ws.principal_name else []),
    })


@workspace_required
@require_GET
def portfolio_print(request):
    ws = request.personal_workspace
    year = (request.GET.get("year") or "").strip()[:20]
    if not year or not (ws.reports.filter(academic_year=year).exists() or ws.evidence.filter(academic_year=year).exists() or ws.initiatives.filter(academic_year=year).exists()):
        raise Http404
    response = render(request, "personal/print.html", {
        "document_title": f"ملف الإنجاز {year}", "workspace": ws, "year": year,
        "reports": ws.reports.filter(academic_year=year),
        "evidence": ws.evidence.filter(academic_year=year),
        "initiatives": ws.initiatives.filter(academic_year=year),
        "school_names": list(ws.reports.filter(academic_year=year).order_by("school_name").values_list("school_name", flat=True).distinct()) or [ws.school_name],
        "principal_names": list(ws.reports.filter(academic_year=year).exclude(principal_name="").order_by("principal_name").values_list("principal_name", flat=True).distinct()) or ([ws.principal_name] if ws.principal_name else []),
    })
    response["Cache-Control"] = "no-store"
    return response
