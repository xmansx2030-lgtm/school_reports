from urllib.parse import urlencode

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods

from .access import subscription_can_write
from .course_forms import PersonalCourseForm
from .models import PersonalAcademicYear, PersonalCourse, PersonalSubscription, PersonalWorkspace
from .services import ensure_writable_personal_year, personal_evidence_for_year, personal_year_is_writable, selected_personal_year
from .views import workspace_required


def _list_url(year=None):
    route = reverse("personal:courses")
    return f"{route}?{urlencode({'year': year})}" if year is not None else route


def _can_edit(request, year):
    return subscription_can_write(request.personal_subscription) and personal_year_is_writable(request.personal_workspace, year)


@workspace_required
@never_cache
@require_GET
def courses(request):
    workspace = request.personal_workspace
    year = selected_personal_year(request)
    queryset = workspace.courses.filter(academic_year=year).select_related("certificate")
    completed = queryset.filter(completion_date__isnull=False)
    total_count = queryset.count()
    completed_count = completed.count()
    completed_hours = completed.aggregate(total=Sum("hours"))["total"] or 0
    query = (request.GET.get("q") or "").strip()[:200]
    if query:
        queryset = queryset.filter(Q(title__icontains=query) | Q(provider__icontains=query))
    page_obj = Paginator(queryset, 12).get_page(request.GET.get("page"))
    return render(request, "personal/courses.html", {
        "year": year, "page_obj": page_obj, "query": query,
        "total_count": total_count, "completed_count": completed_count, "completed_hours": completed_hours,
        "can_edit": _can_edit(request, year),
        "is_archived": PersonalAcademicYear.objects.filter(workspace=workspace, value=year, archived_at__isnull=False).exists(),
    })


def _course_form(request, course=None):
    workspace = request.personal_workspace
    year = course.academic_year if course else selected_personal_year(request)
    request.personal_academic_year = year
    can_edit = _can_edit(request, year)
    list_url = _list_url() if course else _list_url(year)
    form = PersonalCourseForm(request.POST or None, workspace=workspace, year=year, instance=course)
    if request.method == "POST":
        if not can_edit:
            messages.error(request, "يلزم اشتراك نشط وسنة مفتوحة لتعديل سجل الدورات.")
            return redirect(list_url)
        if form.is_valid():
            with transaction.atomic():
                locked = PersonalWorkspace.objects.select_for_update().get(pk=workspace.pk)
                try:
                    subscription = PersonalSubscription.objects.select_for_update().get(workspace=locked)
                    if not subscription_can_write(subscription):
                        raise ValidationError("اشتراك المساحة الشخصية غير نشط حاليًا.")
                    ensure_writable_personal_year(locked, form.cleaned_data["academic_year"])
                    certificate = form.cleaned_data.get("certificate")
                    if certificate:
                        # Certificate updates use the same workspace lock; recheck its current scope.
                        get_object_or_404(personal_evidence_for_year(locked, form.cleaned_data["academic_year"]).select_for_update(), pk=certificate.pk)
                    saved = form.save(commit=False)
                    saved.workspace = locked
                    saved.save()
                except ValidationError as exc:
                    form.add_error(None, exc.messages)
                else:
                    messages.success(request, "حُفظت الدورة في سجل السنة الدراسية.")
                    return redirect(_list_url() if course else _list_url(saved.academic_year))
    return render(request, "personal/course_form.html", {
        "form": form, "course": course, "editing": bool(course), "year": year,
        "can_edit": can_edit, "list_url": list_url,
    })


@workspace_required
@never_cache
@require_http_methods(["GET", "POST"])
def course_create(request):
    return _course_form(request)


@workspace_required
@never_cache
@require_http_methods(["GET", "POST"])
def course_edit(request, pk):
    course = get_object_or_404(PersonalCourse, pk=pk, workspace=request.personal_workspace)
    return _course_form(request, course)


@workspace_required
@never_cache
@require_http_methods(["GET", "POST"])
def course_delete(request, pk):
    course = get_object_or_404(PersonalCourse, pk=pk, workspace=request.personal_workspace)
    year = course.academic_year
    request.personal_academic_year = year
    can_edit = _can_edit(request, year)
    confirmation_error = ""
    if request.method == "POST":
        if not can_edit:
            messages.error(request, "يلزم اشتراك نشط وسنة مفتوحة لحذف الدورة.")
            return redirect(_list_url())
        if request.POST.get("confirm_delete") != "yes":
            confirmation_error = "أكّد حذف سجل الدورة للمتابعة."
        else:
            with transaction.atomic():
                locked = PersonalWorkspace.objects.select_for_update().get(pk=request.personal_workspace.pk)
                try:
                    subscription = PersonalSubscription.objects.select_for_update().get(workspace=locked)
                    if not subscription_can_write(subscription):
                        raise ValidationError("اشتراك المساحة الشخصية غير نشط حاليًا.")
                    ensure_writable_personal_year(locked, year)
                except ValidationError as exc:
                    messages.error(request, " ".join(exc.messages))
                else:
                    current = get_object_or_404(locked.courses.select_for_update(), pk=pk)
                    had_certificate = bool(current.certificate_id)
                    current.delete()
                    messages.success(request, (
                        "حُذفت الدورة. بقيت شهادتها محفوظة في مكتبة الشواهد." if had_certificate
                        else "حُذفت الدورة من سجل السنة الدراسية."
                    ))
            return redirect(_list_url())
    return render(request, "personal/course_confirm_delete.html", {
        "course": course, "year": year, "can_edit": can_edit,
        "confirmation_error": confirmation_error, "list_url": _list_url(),
    })
