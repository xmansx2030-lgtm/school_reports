# reports/views/achievements.py
# -*- coding: utf-8 -*-
from __future__ import annotations

from ._helpers import *
from ._helpers import (
    _is_staff, _is_manager_in_school, _private_comment_role_label,
    _get_active_school, _school_manager_label,
)
from ..gender_labels import school_gender_labels, school_gender_template_context
from ..services_achievement import ensure_achievement_sections as _ensure_achievement_sections
from ..validators import validate_image_file


_ACHIEVEMENT_SECTION_TOTAL = len(AchievementSection.Code.choices)
_ACHIEVEMENT_STATUS_TONES = {
    TeacherAchievementFile.Status.DRAFT: "draft",
    TeacherAchievementFile.Status.SUBMITTED: "warning",
    TeacherAchievementFile.Status.RETURNED: "danger",
    TeacherAchievementFile.Status.APPROVED: "approved",
}


def _achievement_files_with_progress(queryset):
    """Add presentation-only portfolio counts without per-row queries.

    A documented section has teacher notes, a photographed evidence item, or a
    linked report.  This is deliberately named documentation progress: it does
    not infer approval readiness and never changes workflow state.
    """
    documented_filter = (
        ~Q(sections__teacher_notes="")
        | Q(sections__evidence_images__isnull=False)
        | Q(sections__evidence_reports__isnull=False)
    )
    return queryset.annotate(
        documented_section_count=Count(
            "sections",
            filter=documented_filter,
            distinct=True,
        ),
        image_evidence_count=Count("sections__evidence_images", distinct=True),
        report_evidence_count=Count("sections__evidence_reports", distinct=True),
    )


def _set_achievement_presentation(file, *, total_sections=_ACHIEVEMENT_SECTION_TOTAL):
    documented = int(getattr(file, "documented_section_count", 0) or 0)
    total = max(1, int(total_sections or _ACHIEVEMENT_SECTION_TOTAL))
    file.progress_percent = min(100, round((documented / total) * 100))
    file.total_section_count = total
    file.status_tone = _ACHIEVEMENT_STATUS_TONES.get(file.status, "draft")

    if file.status == TeacherAchievementFile.Status.RETURNED:
        file.next_action_label = "راجع ملاحظات المراجع ثم أعد الإرسال"
    elif file.status == TeacherAchievementFile.Status.SUBMITTED:
        file.next_action_label = "بانتظار قرار المراجع"
    elif file.status == TeacherAchievementFile.Status.APPROVED:
        file.next_action_label = "ملف نهائي للعرض والمشاركة"
    else:
        file.next_action_label = "استكمل المحاور والشواهد ثم أرسل للاعتماد"
    return file


def _notify_achievement_submitted(ach_file, active_school):
    """إشعار مدراء المدرسة عند إرسال ملف إنجاز للاعتماد."""
    try:
        from ..utils import create_system_notification

        school = getattr(ach_file, "school", active_school)
        if school is None:
            return
        manager_ids = list(
            SchoolMembership.objects.filter(
                school=school,
                role_type=SchoolMembership.RoleType.MANAGER,
                is_active=True,
            ).values_list("teacher_id", flat=True)
        )
        if not manager_ids:
            return
        teacher_name = getattr(ach_file.teacher, "name", "") if ach_file.teacher else ""
        sent_verb = "أرسلت" if school_gender_labels(school)["is_girls"] else "أرسل"
        create_system_notification(
            title="📂 ملف إنجاز جديد للاعتماد",
            message=f"{sent_verb} {teacher_name} ملف إنجاز للسنة {ach_file.academic_year} للاعتماد.",
            school=school,
            teacher_ids=manager_ids,
        )
    except Exception:
        logger.exception("Failed to send achievement submitted notification")


def _notify_achievement_decided(ach_file, decision, active_school):
    """إشعار المعلم عند اعتماد أو إرجاع ملف الإنجاز."""
    try:
        from ..utils import create_system_notification

        if not ach_file.teacher_id:
            return
        school = getattr(ach_file, "school", active_school)
        if decision == "approved":
            title = "✅ تم اعتماد ملف الإنجاز"
            message = f"تم اعتماد ملف الإنجاز الخاص بك للسنة {ach_file.academic_year}."
        else:
            title = "🔄 تم إرجاع ملف الإنجاز"
            notes = (getattr(ach_file, "manager_notes", "") or "").strip()
            message = f"تم إرجاع ملف الإنجاز الخاص بك للسنة {ach_file.academic_year} للمراجعة."
            if notes:
                message += f"\nملاحظات: {notes[:200]}"

        create_system_notification(
            title=title,
            message=message,
            school=school,
            teacher_ids=[ach_file.teacher_id],
        )
    except Exception:
        logger.exception("Failed to send achievement decision notification")


def _can_manage_achievement(user, active_school: Optional[School]) -> bool:
    if getattr(user, "is_superuser", False):
        return True
    if active_school is None:
        return False
    try:
        return SchoolMembership.objects.filter(
            teacher=user,
            school=active_school,
            role_type=SchoolMembership.RoleType.MANAGER,
            is_active=True,
        ).exists()
    except Exception:
        return False


def _can_view_achievement(user, active_school: Optional[School]) -> bool:
    """من يحق له الاطلاع على ملفات إنجاز غيره.

    **الاطلاع ليس الاعتماد.** من مُنح ``view_achievements`` يقرأ ولا يقرّر: هذه
    الدالة تفتح العرض، و``_can_manage_achievement`` وحدها تفتح الاعتماد والرفض
    والملاحظات — ولا تُستدعى من هنا.

    وقبل هذا كانت الصلاحية معرَّفة في مرجع الصلاحيات ولا يفحصها سطر واحد في
    المشروع: يمنحها المدير في شاشة الأدوار فلا يحدث شيء.
    """
    if getattr(user, "is_superuser", False):
        return True
    if _can_manage_achievement(user, active_school):
        return True
    if active_school is None:
        return False
    from ..capabilities import VIEW_ACHIEVEMENTS
    from ..permissions import capability_source

    return capability_source(user, VIEW_ACHIEVEMENTS, active_school) is not None


def _achievement_scope_teacher_ids(user, active_school: Optional[School]) -> Optional[set[int]]:
    """المنسوبون الذين يشملهم اطلاع هذا المستخدم — أو ``None`` إن كان يشمل الكل.

    ``None`` تعني «لا حصر» وهي للمدير ومالك النظام. وما دونهما يُحصَر بأقسام
    نطاقه: نصّ الصلاحية «يطّلع على ملفات إنجاز **من يقعون تحت إشرافه**»، ونطاقٌ
    بلا أقسام يعني مجموعة فارغة لا مجموعة كاملة — وهي القاعدة نفسها التي تحكم
    ``supervised_department_ids`` في كل موضع.
    """
    if getattr(user, "is_superuser", False):
        return None
    if _can_manage_achievement(user, active_school):
        return None
    if active_school is None:
        return set()

    from ..permissions import supervised_department_ids

    supervised = supervised_department_ids(user, active_school)
    if not supervised:
        return set()

    if DepartmentMembership is None:
        return set()
    return {
        int(pk)
        for pk in DepartmentMembership.objects.filter(
            department_id__in=supervised
        ).values_list("teacher_id", flat=True)
        if pk
    }


def _may_read_achievement_file(user, active_school: Optional[School], ach_file) -> bool:
    """هل يحق لهذا المستخدم قراءة هذا الملف بعينه (عرضاً أو طباعةً أو PDF)؟

    الفحص على **الملف** لا على الشاشة: ``_can_view_achievement`` تجيب «يطّلع على
    ملفات غيره» ولا تقول على ملفات مَن — فالاكتفاء بها في الطباعة كان يفتح ملفات
    المدرسة كلها لمن نطاقه قسم واحد، بينما الكشف الذي وصل منه مقصورٌ على قسمه.
    """
    if getattr(user, "is_superuser", False):
        return True
    if ach_file.teacher_id == getattr(user, "id", None):
        return True
    if _can_manage_achievement(user, active_school):
        return True
    scoped = _achievement_scope_teacher_ids(user, active_school)
    return bool(scoped is not None and ach_file.teacher_id in scoped)


def _apply_achievement_decision(
    *,
    ach_file: TeacherAchievementFile,
    active_school: Optional[School],
    actor: Teacher,
    target_status: str,
    manager_notes: str,
) -> Optional[TeacherAchievementFile]:
    """Apply one manager decision against the current locked database state.

    Achievement has one decision endpoint, so this helper is the authoritative
    transition gate without introducing a second workflow service.  Locking and
    checking ``submitted`` inside the same transaction rejects stale/repeated
    decisions as well as crafted transitions from draft, returned, or approved.
    """
    if target_status not in {
        TeacherAchievementFile.Status.APPROVED,
        TeacherAchievementFile.Status.RETURNED,
    }:
        return None

    school_id = getattr(active_school, "pk", None) or ach_file.school_id
    with transaction.atomic():
        locked_file = (
            TeacherAchievementFile.objects.select_for_update()
            .filter(pk=ach_file.pk, school_id=school_id)
            .first()
        )
        if locked_file is None or locked_file.status != TeacherAchievementFile.Status.SUBMITTED:
            return None

        locked_file.status = target_status
        locked_file.manager_notes = manager_notes
        locked_file.decided_at = timezone.now()
        locked_file.decided_by = actor
        locked_file.save(
            update_fields=[
                "status",
                "decided_at",
                "decided_by",
                "manager_notes",
                "updated_at",
            ]
        )
        return locked_file


def _save_achievement_evidence_images(*, section: AchievementSection, images: list) -> None:
    """Persist a validated image batch and remove files if a later save fails."""
    stored_files = []
    try:
        with transaction.atomic():
            for uploaded_file in images:
                evidence = AchievementEvidenceImage(section=section, image=uploaded_file)
                try:
                    evidence.save()
                except Exception:
                    field_file = getattr(evidence, "image", None)
                    if (
                        field_file
                        and getattr(field_file, "name", "")
                        and getattr(field_file, "_committed", False)
                    ):
                        field_file.storage.delete(field_file.name)
                    raise
                stored_files.append((evidence.image.storage, evidence.image.name))
    except Exception:
        for storage, name in stored_files:
            try:
                storage.delete(name)
            except Exception:
                logger.exception("Failed to clean achievement evidence after upload rollback")
        raise


@login_required(login_url="reports:login")
@require_http_methods(["GET", "POST"])
def achievement_my_files(request: HttpRequest) -> HttpResponse:
    """قائمة ملفات الإنجاز الخاصة بالمعلّم + إنشاء سنة جديدة."""
    active_school = _get_active_school(request)
    if active_school is None:
        messages.error(request, "فضلاً اختر/حدّد مدرسة أولاً.")
        return redirect("reports:home")

    existing_years = list(
        TeacherAchievementFile.objects.filter(school=active_school)
        .values_list("academic_year", flat=True)
        .distinct()
    )
    
    # سنة المدرسة الحالية هي الخيار الوحيد لإنشاء ملف جديد.
    current_year = (getattr(active_school, "current_academic_year", "") or "").strip()
    allowed = [current_year] if current_year else []

    create_form = AchievementCreateYearForm(
        request.POST or None, 
        year_choices=existing_years,
        allowed_years=allowed
    )
    if request.method == "POST" and (request.POST.get("action") == "create"):
        if create_form.is_valid():
            year = create_form.cleaned_data["academic_year"]
            ach_file, created = TeacherAchievementFile.objects.get_or_create(
                teacher=request.user,
                school=active_school,
                academic_year=year,
                defaults={},
            )
            _ensure_achievement_sections(ach_file)
            if created:
                messages.success(request, "تم إنشاء ملف الإنجاز للسنة.")
            return redirect("reports:achievement_file_detail", pk=ach_file.pk)
        messages.error(request, "تحقق من السنة الدراسية وأعد المحاولة.")

    files = list(_achievement_files_with_progress(
        TeacherAchievementFile.objects.filter(teacher=request.user, school=active_school)
        .order_by("-academic_year", "-id")
    ))
    for achievement_file in files:
        _set_achievement_presentation(achievement_file)
    return render(
        request,
        "reports/achievement_my_files.html",
        {"files": files, "create_form": create_form, "current_school": active_school},
    )


def _achievement_locked_reason(ach_file, school, *, action: str) -> str:
    """سبب منع إجراءٍ على ملف إنجاز خرج من يد صاحبه — بصيغة تقول ما العمل.

    رسالةٌ واحدة لكل المواضع، و``action`` مصدرٌ صريح («حذف ملف الإنجاز»)
    لا فعلٌ يُصرَّف في النص. و«ممنوع» بلا مخرج تدفع المستخدم إلى الدعم، وما
    يحتاجه أن يعرف مَن يفتح له الباب.
    """
    if ach_file.is_final:
        return f"تعذّر {action}: الملف معتمَد، والمعتمَد سجلٌّ لا يُغيَّر."
    return (
        f"تعذّر {action}: الملف مُرسل وينتظر القرار. اطلب من "
        f"{_school_manager_label(school)} إعادته إليك أولاً."
    )


@login_required(login_url="reports:login")
@require_http_methods(["POST"])
def achievement_file_delete(request: HttpRequest, pk: int) -> HttpResponse:
    """حذف ملف إنجاز (للمالك فقط، وما دام في يده)."""
    active_school = _get_active_school(request)
    file = get_object_or_404(TeacherAchievementFile, pk=pk, teacher=request.user)

    # عزل حسب المدرسة النشطة
    if active_school is not None and getattr(file, "school_id", None) != active_school.id:
        messages.error(request, "لا تملك صلاحية حذف ملف إنجاز من مدرسة أخرى.")
        return redirect("reports:achievement_my_files")
    
    # المعتمَد لا يُحذف، والمُرسَل لا يُسحب من تحت من يراجعه. وحرية التصحيح
    # مكفولة في المسودة والمُعاد، وهما حالتا الملكية.
    if not file.is_editable_by_owner:
        messages.error(
            request,
            _achievement_locked_reason(file, active_school, action="حذف ملف الإنجاز"),
        )
        return redirect("reports:achievement_file_detail", pk=file.pk)

    file_school = getattr(file, "school", active_school)
    file.delete()
    sync_school_archive_storage_usage(file_school)
    messages.success(request, "تم حذف ملف الإنجاز.")
    return redirect("reports:achievement_my_files")


@login_required(login_url="reports:login")
@require_http_methods(["POST"])
def achievement_file_update_year(request: HttpRequest, pk: int) -> HttpResponse:
    """تصحيح السنة الدراسية لملف الإنجاز (للمالك فقط)."""
    file = get_object_or_404(TeacherAchievementFile, pk=pk, teacher=request.user)
    active_school = _get_active_school(request)

    # عزل حسب المدرسة النشطة
    if active_school is not None and getattr(file, "school_id", None) != active_school.id:
        messages.error(request, "لا تملك صلاحية تعديل ملف إنجاز من مدرسة أخرى.")
        return redirect("reports:achievement_my_files")

    # السنة هويّة الملف لا حقلاً فيه: تغييرها بعد الاعتماد ينقل شواهد سنةٍ
    # إلى سجلّ سنةٍ أخرى، فيصير المعتمَد شاهداً على ما لم يُعتمد.
    if not file.is_editable_by_owner:
        messages.error(
            request,
            _achievement_locked_reason(file, active_school, action="تعديل السنة الدراسية"),
        )
        return redirect("reports:achievement_file_detail", pk=file.pk)

    current_year = (getattr(file.school, "current_academic_year", "") or "").strip()
    form = AchievementCreateYearForm(
        request.POST,
        allowed_years=[current_year] if current_year else [],
    )
    
    if form.is_valid():
        new_year = form.cleaned_data["academic_year"]
        
        # 1. التحقق من عدم التكرار
        duplicate = TeacherAchievementFile.objects.filter(
            teacher=request.user, 
            school=file.school, 
            academic_year=new_year
        ).exclude(pk=file.pk).exists()

        if duplicate:
            messages.error(request, f" لا يمكن التعديل: لديك ملف آخر بالفعل للسنة {new_year}")
        else:
            file.academic_year = new_year
            file.save(update_fields=["academic_year", "updated_at"])
            messages.success(request, f"تم تعديل السنة الدراسية إلى {new_year}.")

    else:
        # استخراج أول خطأ
        err = next(iter(form.errors.values()))[0] if form.errors else "بيانات غير صالحة"
        messages.error(request, f"تعذر تحديث السنة: {err}")

    return redirect("reports:achievement_my_files")


@login_required(login_url="reports:login")
@require_http_methods(["GET", "POST"])
def achievement_school_files(request: HttpRequest) -> HttpResponse:
    """قائمة ملفات الإنجاز للمدرسة (مدير المدرسة).

    - تعرض جميع المعلمين في المدرسة النشطة.
    - بجانب كل معلم: فتح الملف + طباعة/حفظ PDF.
    - الاعتماد/الرفض يكون داخل صفحة الملف نفسها.
    """
    active_school = _get_active_school(request)
    if active_school is None:
        messages.error(request, "فضلاً اختر/حدّد مدرسة أولاً.")
        return redirect("reports:home")

    if not _can_view_achievement(request.user, active_school):
        messages.error(request, "لا تملك صلاحية الاطلاع على ملفات الإنجاز.")
        return redirect("reports:home")

    # اختيار سنة (اختياري): إن لم تُحدد، نأخذ آخر سنة موجودة في المدرسة
    year = (request.GET.get("year") or request.POST.get("year") or "").strip()
    try:
        year = year.replace("–", "-").replace("—", "-")
    except Exception:
        pass

    existing_years = list(
        TeacherAchievementFile.objects.filter(school=active_school)
        .values_list("academic_year", flat=True)
        .distinct()
    )
    current_year = (getattr(active_school, "current_academic_year", "") or "").strip()
    year_choices = sorted(set(existing_years) | ({current_year} if current_year else set()))

    if not year and year_choices:
        year = current_year if current_year in year_choices else year_choices[-1]
    if year and year_choices and year not in year_choices:
        year = current_year if current_year in year_choices else year_choices[-1]

    base_url = reverse("reports:achievement_school_files")

    def _redirect_with_year(year_value: str) -> HttpResponse:
        year_value = (year_value or "").strip()
        if not year_value:
            return redirect(base_url)
        return redirect(f"{base_url}?{urlencode({'year': year_value})}")

    # إنشاء ملف إنجاز من صفحة المدرسة غير مسموح: المعلّم هو من ينشئ ملفه من (ملف الإنجاز)
    if request.method == "POST" and (request.POST.get("action") == "create"):
        teacher_label = school_gender_labels(active_school)["teacher"]
        messages.error(request, f"إنشاء ملف الإنجاز متاح لـ{teacher_label} فقط.")
        return _redirect_with_year(year)

    # Search Logic
    q = request.GET.get("q", "").strip()
    status = (request.GET.get("status") or "").strip()
    valid_statuses = {value for value, _label in TeacherAchievementFile.Status.choices}
    if status not in valid_statuses | {"missing"}:
        status = ""

    teachers = (
        Teacher.objects.filter(
            is_active=True,
            school_memberships__school=active_school,
            school_memberships__is_active=True,
            school_memberships__role_type__in=SchoolMembership.STAFF_ROLES,
        )
        .distinct()
        .only("id", "name", "phone", "national_id")
        .order_by("name")
    )

    # النطاق قبل المرشّح: التصفية الأمنية على الاستعلام الأساس، ثم يُبنى فوقها
    # بحثُ المستخدم ومرشّحاته. والترتيب المعاكس يجعل كل مرشّح جديد ثغرةً محتملة.
    scoped_teacher_ids = _achievement_scope_teacher_ids(request.user, active_school)
    if scoped_teacher_ids is not None:
        teachers = teachers.filter(id__in=scoped_teacher_ids)


    if q:
        from ..search_utils import smart_search_q, ACHIEVEMENT_TEACHER_SEARCH_FIELDS

        search_q = smart_search_q(q, ACHIEVEMENT_TEACHER_SEARCH_FIELDS)
        if search_q:
            teachers = teachers.filter(search_q).distinct()

    files_by_teacher_id = {}
    if year:
        files = _achievement_files_with_progress(
            TeacherAchievementFile.objects.filter(school=active_school, academic_year=year)
            .select_related("teacher")
            .only("id", "teacher_id", "status", "academic_year")
        )
        if q:
            # تصفية الملفات أيضاً لتحسين الأداء
            files = files.filter(teacher__in=teachers)

        teacher_ids_with_files = files.values_list("teacher_id", flat=True)
        if status == "missing":
            teachers = teachers.exclude(id__in=teacher_ids_with_files)
            files = files.none()
        elif status:
            matching_teacher_ids = files.filter(status=status).values_list(
                "teacher_id",
                flat=True,
            )
            teachers = teachers.filter(id__in=matching_teacher_ids)
            files = files.filter(status=status)

        files_by_teacher_id = {}
        for achievement_file in files:
            _set_achievement_presentation(achievement_file)
            files_by_teacher_id[achievement_file.teacher_id] = achievement_file

    rows = [{"teacher": t, "file": files_by_teacher_id.get(t.id)} for t in teachers]
    review_priority = {
        TeacherAchievementFile.Status.SUBMITTED: 0,
        TeacherAchievementFile.Status.RETURNED: 1,
        TeacherAchievementFile.Status.DRAFT: 2,
        TeacherAchievementFile.Status.APPROVED: 4,
    }
    rows.sort(
        key=lambda row: (
            review_priority.get(getattr(row["file"], "status", None), 3),
            (getattr(row["teacher"], "name", "") or "").casefold(),
        )
    )

    return render(
        request,
        "reports/achievement_school_files.html",
        {
            "rows": rows,
            "year": year,
            "year_choices": year_choices,
            "current_school": active_school,
            "is_manager": _can_manage_achievement(request.user, active_school),
            "q": q,
            "status": status,
            "status_choices": TeacherAchievementFile.Status.choices,
        },
    )


@login_required(login_url="reports:login")
@require_http_methods(["GET", "POST"])
def achievement_school_teachers(request: HttpRequest) -> HttpResponse:
    """Alias قديم: توجيه إلى صفحة المدرسة الموحدة."""
    params = {}
    year = (request.GET.get("year") or request.POST.get("year") or "").strip()
    if year:
        params["year"] = year
    url = reverse("reports:achievement_school_files")
    if params:
        return redirect(f"{url}?{urlencode(params)}")
    return redirect("reports:achievement_school_files")


@login_required(login_url="reports:login")
@require_http_methods(["GET", "POST"])
def achievement_file_detail(request: HttpRequest, pk: int) -> HttpResponse:
    active_school = _get_active_school(request)
    labels = school_gender_labels(active_school)
    ach_file = get_object_or_404(TeacherAchievementFile, pk=pk)
    user = request.user

    if not getattr(user, "is_superuser", False):
        if active_school is None or ach_file.school_id != getattr(active_school, "id", None):
            raise Http404

    is_manager = _can_manage_achievement(user, active_school)
    is_owner = (ach_file.teacher_id == getattr(user, "id", None))
    # المتابع المُنح ``view_achievements`` يقرأ ملفات نطاقه. و``is_manager``
    # يبقى كما هو، فهو وحده مقياس الاعتماد والملاحظات أدناه.
    scoped_teacher_ids = _achievement_scope_teacher_ids(user, active_school)
    is_scoped_viewer = bool(
        scoped_teacher_ids is not None and ach_file.teacher_id in scoped_teacher_ids
    )

    if not (
        getattr(user, "is_superuser", False) or is_manager or is_owner or is_scoped_viewer
    ):
        return HttpResponse(status=403)

    _ensure_achievement_sections(ach_file)
    try:
        from django.db.models import Prefetch

        ev_reports_qs = AchievementEvidenceReport.objects.select_related(
            "report",
            "report__category",
        ).order_by("id")
        sections = list(
            AchievementSection.objects.filter(file=ach_file)
            .prefetch_related("evidence_images", Prefetch("evidence_reports", queryset=ev_reports_qs))
            .order_by("code", "id")
        )
    except Exception:
        sections = list(
            AchievementSection.objects.filter(file=ach_file)
            .prefetch_related("evidence_images", "evidence_reports")
            .order_by("code", "id")
        )

    documented_sections = 0
    image_evidence_count = 0
    report_evidence_count = 0
    for section in sections:
        images = list(section.evidence_images.all())
        reports = list(section.evidence_reports.all())
        section.image_evidence_count = len(images)
        section.report_evidence_count = len(reports)
        section.is_documented = bool((section.teacher_notes or "").strip() or images or reports)
        documented_sections += int(section.is_documented)
        image_evidence_count += len(images)
        report_evidence_count += len(reports)

    ach_file.documented_section_count = documented_sections
    ach_file.image_evidence_count = image_evidence_count
    ach_file.report_evidence_count = report_evidence_count
    _set_achievement_presentation(ach_file, total_sections=len(sections))

    can_edit_teacher = bool(is_owner and ach_file.is_editable_by_owner)
    can_post = bool(can_edit_teacher or is_manager)

    general_form = TeacherAchievementFileForm(request.POST or None, instance=ach_file)
    manager_notes_form = AchievementManagerNotesForm(request.POST or None, instance=ach_file)
    year_form = AchievementCreateYearForm()
    upload_form = AchievementEvidenceUploadForm()

    # تعليقات خاصة (يراها المعلم + أصحاب الصلاحية داخل المدرسة/المنصة)
    is_staff_user = _is_staff(user)
    can_add_private_comment = bool(is_manager or is_staff_user or getattr(user, "is_superuser", False))
    show_private_comments = bool(is_owner or can_add_private_comment)
    private_comments = (
        TeacherPrivateComment.objects.select_related("created_by")
        .filter(achievement_file=ach_file, teacher=ach_file.teacher)
        .order_by("-created_at", "-id")
        if show_private_comments
        else TeacherPrivateComment.objects.none()
    )
    private_comment_form = PrivateCommentForm(request.POST or None) if can_add_private_comment else None

    # الرجوع ثابت حسب الدور: المعلّم -> ملفاتي، غير ذلك -> ملفات المدرسة
    if is_owner:
        back_url = reverse("reports:achievement_my_files")
    else:
        url = reverse("reports:achievement_school_files")
        back_url = f"{url}?{urlencode({'year': ach_file.academic_year})}"

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
        section_id = request.POST.get("section_id")

        # ===== تعليقات خاصة (لا تظهر في الطباعة أو المشاركة) =====
        # توافق خلفي: platform_comment
        if action in {"platform_comment", "private_comment_create", "private_comment_update", "private_comment_delete"}:
            if not can_add_private_comment:
                return HttpResponse(status=403)

            # create
            if action in {"platform_comment", "private_comment_create"}:
                if private_comment_form is not None and private_comment_form.is_valid():
                    body = private_comment_form.cleaned_data["body"]
                    try:
                        with transaction.atomic():
                            TeacherPrivateComment.objects.create(
                                teacher=ach_file.teacher,
                                created_by=user,
                                school=active_school,
                                achievement_file=ach_file,
                                body=body,
                            )
                            n = Notification.objects.create(
                                title="تعليق خاص على ملف الإنجاز",
                                message=body,
                                is_important=True,
                                school=active_school,
                                created_by=user,
                            )
                            NotificationRecipient.objects.create(notification=n, teacher=ach_file.teacher)
                        messages.success(request, f"تم إرسال التعليق الخاص لـ{labels['teacher']}.")
                        return redirect("reports:achievement_file_detail", pk=ach_file.pk)
                    except Exception:
                        logger.exception("Failed to create private achievement comment")
                        messages.error(request, "تعذر حفظ التعليق. حاول مرة أخرى.")
                else:
                    messages.error(request, "تحقق من نص التعليق وأعد المحاولة.")
                return redirect("reports:achievement_file_detail", pk=ach_file.pk)

            # update/delete (only comment owner, or superuser)
            comment_id = request.POST.get("comment_id")
            try:
                comment_id_int = int(comment_id) if comment_id else None
            except (TypeError, ValueError):
                comment_id_int = None

            if not comment_id_int:
                messages.error(request, "تعذر تحديد التعليق.")
                return redirect("reports:achievement_file_detail", pk=ach_file.pk)

            comment = TeacherPrivateComment.objects.filter(
                pk=comment_id_int,
                achievement_file=ach_file,
                teacher=ach_file.teacher,
            ).first()
            if comment is None:
                messages.error(request, "التعليق غير موجود.")
                return redirect("reports:achievement_file_detail", pk=ach_file.pk)

            is_owner_of_comment = getattr(comment, "created_by_id", None) == getattr(user, "id", None)

            if action == "private_comment_update":
                # تعديل: لصاحب التعليق فقط
                if not is_owner_of_comment:
                    return HttpResponse(status=403)
                body = (request.POST.get("body") or "").strip()
                if not body:
                    messages.error(request, "نص التعليق مطلوب.")
                    return redirect("reports:achievement_file_detail", pk=ach_file.pk)
                try:
                    TeacherPrivateComment.objects.filter(pk=comment.pk).update(body=body)
                    messages.success(request, "تم تعديل التعليق.")
                except Exception:
                    messages.error(request, "تعذر تعديل التعليق.")
                return redirect("reports:achievement_file_detail", pk=ach_file.pk)

            if action == "private_comment_delete":
                # حذف: لصاحب التعليق فقط، والسوبر يمكنه حذف أي تعليق
                if not (is_owner_of_comment or getattr(user, "is_superuser", False)):
                    return HttpResponse(status=403)
                try:
                    comment.delete()
                    messages.success(request, "تم حذف التعليق.")
                except Exception:
                    messages.error(request, "تعذر حذف التعليق.")
                return redirect("reports:achievement_file_detail", pk=ach_file.pk)

        if not can_post:
            return HttpResponse(status=403)

        if action == "save_general" and can_edit_teacher:
            if general_form.is_valid():
                general_form.save()
                messages.success(request, "تم حفظ البيانات العامة.")
                return redirect("reports:achievement_file_detail", pk=ach_file.pk)
            messages.error(request, "تحقق من الحقول وأعد المحاولة.")

        elif action == "save_section" and can_edit_teacher and section_id:
            sec = get_object_or_404(AchievementSection, pk=int(section_id), file=ach_file)
            sec_form = AchievementSectionNotesForm(request.POST, instance=sec)
            if sec_form.is_valid():
                sec_form.save()
                messages.success(request, "تم حفظ ملاحظات المحور.")
                return redirect("reports:achievement_file_detail", pk=ach_file.pk)
            messages.error(request, "تحقق من ملاحظات المحور وأعد المحاولة.")

        elif action == "upload_evidence" and can_edit_teacher and section_id:
            sec = get_object_or_404(AchievementSection, pk=int(section_id), file=ach_file)
            imgs = request.FILES.getlist("images")
            if not imgs:
                messages.error(request, "اختر صورًا للرفع.")
                return redirect("reports:achievement_file_detail", pk=ach_file.pk)
            existing_count = AchievementEvidenceImage.objects.filter(section=sec).count()
            remaining = max(0, 8 - existing_count)
            if remaining <= 0:
                messages.error(request, "لا يمكن إضافة أكثر من 8 صور لهذا المحور.")
                return redirect("reports:achievement_file_detail", pk=ach_file.pk)
            imgs = imgs[:remaining]

            # Validate the complete accepted batch before the first storage/DB
            # mutation. This keeps invalid multi-upload requests all-or-nothing.
            try:
                for image in imgs:
                    validate_image_file(image)
            except ValidationError as exc:
                messages.error(request, " ".join(exc.messages))
                return redirect("reports:achievement_file_detail", pk=ach_file.pk)

            capacity_error = archive_storage_capacity_error(active_school, imgs)
            if capacity_error:
                messages.error(request, capacity_error)
                return redirect("reports:achievement_file_detail", pk=ach_file.pk)
            try:
                _save_achievement_evidence_images(section=sec, images=imgs)
            except Exception:
                logger.exception("Failed to persist achievement evidence batch")
                messages.error(request, "تعذر رفع الشواهد. لم يتم حفظ أي ملف.")
                return redirect("reports:achievement_file_detail", pk=ach_file.pk)
            sync_school_archive_storage_usage(active_school)
            messages.success(request, "تم رفع الشواهد.")
            return redirect("reports:achievement_file_detail", pk=ach_file.pk)

        elif action == "delete_evidence" and can_edit_teacher:
            img_id = request.POST.get("image_id")
            if img_id:
                img = get_object_or_404(AchievementEvidenceImage, pk=int(img_id), section__file=ach_file)
                try:
                    img.delete()
                except Exception:
                    pass
                sync_school_archive_storage_usage(active_school)
                messages.success(request, "تم حذف الصورة.")
            return redirect("reports:achievement_file_detail", pk=ach_file.pk)

        elif action == "add_report_evidence" and can_edit_teacher and section_id:
            sec = get_object_or_404(AchievementSection, pk=int(section_id), file=ach_file)
            report_id = request.POST.get("report_id")
            try:
                report_id_int = int(report_id) if report_id else None
            except (TypeError, ValueError):
                report_id_int = None
            if not report_id_int:
                messages.error(request, "تعذر تحديد التقرير.")
                return redirect("reports:achievement_file_detail", pk=ach_file.pk)

            rep_qs = Report.objects.select_related("category").filter(teacher=request.user)
            try:
                if active_school is not None:
                    rep_qs = rep_qs.filter(school=active_school)
            except Exception:
                pass
            r = get_object_or_404(rep_qs, pk=report_id_int)

            try:
                add_report_evidence(section=sec, report=r)
                messages.success(request, "تمت إضافة التقرير كشاهد.")
            except Exception:
                messages.error(request, "تعذر إضافة التقرير. ربما تمت إضافته مسبقاً.")
            return redirect("reports:achievement_file_detail", pk=ach_file.pk)

        elif action == "delete_report_evidence" and can_edit_teacher and section_id:
            sec = get_object_or_404(AchievementSection, pk=int(section_id), file=ach_file)
            evidence_id = request.POST.get("evidence_id")
            try:
                evidence_id_int = int(evidence_id) if evidence_id else None
            except (TypeError, ValueError):
                evidence_id_int = None
            if not evidence_id_int:
                messages.error(request, "تعذر تحديد الشاهد.")
                return redirect("reports:achievement_file_detail", pk=ach_file.pk)

            try:
                ok = remove_report_evidence(section=sec, evidence_id=evidence_id_int)
                if ok:
                    sync_school_archive_storage_usage(active_school)
                    messages.success(request, "تمت إزالة التقرير من الشواهد.")
                else:
                    messages.error(request, "الشاهد غير موجود.")
            except Exception:
                messages.error(request, "تعذر إزالة الشاهد.")
            return redirect("reports:achievement_file_detail", pk=ach_file.pk)

        elif action == "import_prev" and can_edit_teacher:
            prev_year = (request.POST.get("prev_year") or "").strip()
            if prev_year:
                prev = TeacherAchievementFile.objects.filter(
                    teacher=ach_file.teacher,
                    school=ach_file.school,
                    academic_year=prev_year,
                ).first()
            else:
                prev = (
                    TeacherAchievementFile.objects.filter(
                        teacher=ach_file.teacher, school=ach_file.school
                    )
                    .exclude(pk=ach_file.pk)
                    .order_by("-academic_year", "-id")
                    .first()
                )
            if not prev:
                messages.error(request, "لا يوجد ملف سابق للاستيراد.")
                return redirect("reports:achievement_file_detail", pk=ach_file.pk)

            # استيراد الحقول الثابتة فقط
            ach_file.qualifications = prev.qualifications
            ach_file.professional_experience = prev.professional_experience
            ach_file.specialization = prev.specialization
            ach_file.teaching_load = prev.teaching_load
            ach_file.subjects_taught = prev.subjects_taught
            ach_file.contact_info = prev.contact_info
            ach_file.save(update_fields=[
                "qualifications",
                "professional_experience",
                "specialization",
                "teaching_load",
                "subjects_taught",
                "contact_info",
                "updated_at",
            ])
            messages.success(request, "تم استيراد البيانات الثابتة من ملف سابق.")
            return redirect("reports:achievement_file_detail", pk=ach_file.pk)

        elif action == "submit" and can_edit_teacher:
            now = timezone.now()
            try:
                with transaction.atomic():
                    ach_file.status = TeacherAchievementFile.Status.SUBMITTED
                    ach_file.submitted_at = now
                    ach_file.save(update_fields=["status", "submitted_at", "updated_at"])

                    frozen = freeze_achievement_report_evidences(ach_file=ach_file)
                    sync_school_archive_storage_usage(active_school)

                # إشعار مدير المدرسة بملف إنجاز جديد
                _notify_achievement_submitted(ach_file, active_school)

                if frozen:
                    messages.success(request, f"تم إرسال الملف للاعتماد وتجميد {frozen} تقرير/تقارير كشواهد.")
                else:
                    messages.success(request, "تم إرسال الملف للاعتماد.")
            except Exception:
                # حتى لو فشل التجميد لأي سبب، لا نكسر تجربة المستخدم
                messages.success(request, "تم إرسال الملف للاعتماد.")
            return redirect("reports:achievement_file_detail", pk=ach_file.pk)

        elif action == "approve" and is_manager:
            if not manager_notes_form.is_valid():
                messages.error(request, "تعذر حفظ ملاحظات الاعتماد. راجع النص وحاول مرة أخرى.")
                return redirect("reports:achievement_file_detail", pk=ach_file.pk)
            decided_file = _apply_achievement_decision(
                ach_file=ach_file,
                active_school=active_school,
                actor=request.user,
                target_status=TeacherAchievementFile.Status.APPROVED,
                manager_notes=manager_notes_form.cleaned_data["manager_notes"],
            )
            if decided_file is None:
                messages.error(request, "تعذر تنفيذ القرار لأن الملف لم يعد بانتظار الاعتماد.")
                return redirect("reports:achievement_file_detail", pk=ach_file.pk)

            # إشعار المعلم باعتماد ملف الإنجاز
            _notify_achievement_decided(decided_file, "approved", active_school)

            messages.success(request, "تم اعتماد ملف الإنجاز.")
            return redirect("reports:achievement_file_detail", pk=ach_file.pk)

        elif action == "return" and is_manager:
            if not manager_notes_form.is_valid():
                messages.error(request, "تعذر حفظ ملاحظات الإرجاع. راجع النص وحاول مرة أخرى.")
                return redirect("reports:achievement_file_detail", pk=ach_file.pk)
            decided_file = _apply_achievement_decision(
                ach_file=ach_file,
                active_school=active_school,
                actor=request.user,
                target_status=TeacherAchievementFile.Status.RETURNED,
                manager_notes=manager_notes_form.cleaned_data["manager_notes"],
            )
            if decided_file is None:
                messages.error(request, "تعذر تنفيذ القرار لأن الملف لم يعد بانتظار الاعتماد.")
                return redirect("reports:achievement_file_detail", pk=ach_file.pk)

            # إشعار المعلم بإرجاع ملف الإنجاز
            _notify_achievement_decided(decided_file, "returned", active_school)

            messages.success(request, f"تم إرجاع الملف لـ{labels['teacher']} مع الملاحظات.")
            return redirect("reports:achievement_file_detail", pk=ach_file.pk)

        messages.error(request, "تعذر تنفيذ العملية.")

    try:
        if show_private_comments and private_comments is not None:
            for c in private_comments:
                try:
                    c.created_by_role_label = _private_comment_role_label(getattr(c, "created_by", None), active_school)
                except Exception:
                    c.created_by_role_label = ""
    except Exception:
        pass

    return render(
        request,
        "reports/achievement_file.html",
        {
            "file": ach_file,
            "sections": sections,
            "general_form": general_form,
            "upload_form": upload_form,
            "manager_notes_form": manager_notes_form,
            "can_edit_teacher": can_edit_teacher,
            "is_manager": is_manager,
            "is_owner": is_owner,
            "show_private_comments": show_private_comments,
            "private_comments": private_comments,
            "private_comment_form": private_comment_form,
            "can_add_private_comment": can_add_private_comment,
            "current_user_id": getattr(user, "id", None),
            "is_superuser": bool(getattr(user, "is_superuser", False)),
            "year_form": year_form,
            "current_school": active_school,
            "back_url": back_url,
        },
    )


@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def achievement_report_picker(request: HttpRequest, pk: int) -> HttpResponse:
    """Return a partial HTML list to pick teacher reports as evidence for a section."""

    active_school = _get_active_school(request)
    ach_file = get_object_or_404(TeacherAchievementFile, pk=pk)
    user = request.user

    is_owner = (ach_file.teacher_id == getattr(user, "id", None))
    if not is_owner:
        return HttpResponse(status=403)
    if not getattr(user, "is_superuser", False):
        if active_school is None or ach_file.school_id != getattr(active_school, "id", None):
            raise Http404

    if ach_file.status not in {
        TeacherAchievementFile.Status.DRAFT,
        TeacherAchievementFile.Status.RETURNED,
    }:
        return HttpResponse(status=403)

    section_id = request.GET.get("section_id")
    try:
        section_id_int = int(section_id) if section_id else None
    except (TypeError, ValueError):
        section_id_int = None
    if not section_id_int:
        return HttpResponse(status=400)

    section = get_object_or_404(AchievementSection, pk=section_id_int, file=ach_file)
    q = (request.GET.get("q") or "").strip()

    qs = achievement_picker_reports_qs(teacher=user, active_school=active_school, q=q).select_related("category")
    reports = list(qs[:50])
    already_ids = set(
        AchievementEvidenceReport.objects.filter(section=section, report__isnull=False).values_list(
            "report_id", flat=True
        )
    )

    return render(
        request,
        "reports/partials/achievement_report_picker_list.html",
        {
            "file": ach_file,
            "section": section,
            "reports": reports,
            "q": q,
            "already_ids": already_ids,
        },
    )


@login_required(login_url="reports:login")
@ratelimit(key="user", rate="20/h", method="GET", block=True)
@require_http_methods(["GET"])
def achievement_file_pdf(request: HttpRequest, pk: int) -> HttpResponse:
    active_school = _get_active_school(request)
    ach_file = get_object_or_404(TeacherAchievementFile, pk=pk)
    if not getattr(request.user, "is_superuser", False):
        if active_school is None or ach_file.school_id != getattr(active_school, "id", None):
            raise Http404
    if not _may_read_achievement_file(request.user, active_school, ach_file):
        return HttpResponse(status=403)

    # توليد PDF عند الطلب — في عامل الوسائط متى أمكن.
    #
    # المحتوى واللحظة والاسم كما كانت تماماً؛ المتغيّر أن حرق المعالج انتقل من
    # حاوية الويب إلى العامل المخصَّص للعمل الثقيل. وأي تعثّر يرتدّ إلى التوليد
    # المحلي داخل ``render_pdf_offloaded`` نفسه، فلا مسار جديد للفشل هنا.
    try:
        from ..pdf_achievement import achievement_pdf_filename, generate_achievement_pdf
        from ..pdf_offload import render_pdf_offloaded
        from ..tasks import render_achievement_pdf_task

        base_url = request.build_absolute_uri("/")
        filename = achievement_pdf_filename(ach_file)
        pdf_bytes = render_pdf_offloaded(
            task=render_achievement_pdf_task,
            task_args=[ach_file.pk, base_url],
            render_locally=lambda: generate_achievement_pdf(
                ach_file=ach_file, base_url=base_url
            )[0],
            label=f"achievement:{ach_file.pk}",
        )
    except OSError as ex:
        # WeasyPrint on Windows يحتاج مكتبات نظام (GTK/Pango/Cairo) مثل libgobject.
        msg = str(ex) or ""
        if "libgobject" in msg or "gobject-2.0" in msg:
            # أفضل UX: لا نعرض صفحة خطأ/نص؛ نرجع لنفس صفحة الملف برسالة واضحة.
            messages.error(
                request,
                "تعذر توليد PDF محليًا لأن مكتبات الطباعة غير مثبتة على هذا الجهاز. "
                "أفضل حل: شغّل المشروع على Render/Docker/WSL (Linux) أو ثبّت GTK runtime على Windows.",
            )
            logger.warning("WeasyPrint native deps missing: %s", msg)
            return redirect("reports:achievement_file_detail", pk=ach_file.pk)

        if settings.DEBUG:
            raise
        messages.error(request, "تعذر توليد ملف PDF حاليًا.")
        return redirect("reports:achievement_file_detail", pk=ach_file.pk)
    except Exception:
        if settings.DEBUG:
            raise
        messages.error(request, "تعذر توليد ملف PDF حاليًا.")
        return redirect("reports:achievement_file_detail", pk=ach_file.pk)

    resp = HttpResponse(pdf_bytes, content_type="application/pdf")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def achievement_file_print(request: HttpRequest, pk: int) -> HttpResponse:
    """صفحة طباعة ملف الإنجاز (مثل طباعة التقارير).

    تعتمد على الطباعة من المتصفح (Save as PDF) لتجنّب مشاكل WeasyPrint على Windows.
    """

    active_school = _get_active_school(request)
    ach_file = get_object_or_404(TeacherAchievementFile, pk=pk)

    if not getattr(request.user, "is_superuser", False):
        if active_school is None or ach_file.school_id != getattr(active_school, "id", None):
            raise Http404

    if not _may_read_achievement_file(request.user, active_school, ach_file):
        return HttpResponse(status=403)

    _ensure_achievement_sections(ach_file)
    try:
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
        has_evidence_reports = AchievementEvidenceReport.objects.filter(section__file=ach_file).exists()
    except Exception:
        sections = (
            AchievementSection.objects.filter(file=ach_file)
            .prefetch_related("evidence_images", "evidence_reports")
            .order_by("code", "id")
        )
        has_evidence_reports = False

    school = ach_file.school
    primary = (getattr(school, "print_primary_color", None) or "").strip() or "#2563eb"

    # تم حذف شعارات المدارس (logo_file/logo_url) نهائيًا من النظام
    school_logo_url = ""

    try:
        from ..pdf_achievement import _static_png_as_data_uri

        ministry_logo_src = _static_png_as_data_uri("img/UntiTtled-1.png")
    except Exception:
        ministry_logo_src = None

    # تحديد URL الرجوع الذكي حسب دور المستخدم
    back_url = "reports:achievement_my_files"  # الافتراضي للمعلم
    is_manager = _is_manager_in_school(request.user, active_school)
    is_staff_user = _is_staff(request.user)
    is_superuser_val = bool(getattr(request.user, "is_superuser", False))
    
    if is_superuser_val or is_manager or is_staff_user:
        back_url = "reports:achievement_school_files"
    
    return render(
        request,
        "reports/pdf/achievement_file.html",
        {
            "file": ach_file,
            "school": school,
            "sections": sections,
            "has_evidence_reports": has_evidence_reports,
            "theme": {"brand": primary},
            **school_gender_template_context(school),
            "now": timezone.localtime(timezone.now()),
            "school_logo_url": school_logo_url,
            "ministry_logo_src": ministry_logo_src,
            "back_url": back_url,
        },
    )
