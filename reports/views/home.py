# reports/views/home.py
# -*- coding: utf-8 -*-
from __future__ import annotations

from ._helpers import *
from ..personal_routing import is_personal_only_workspace_user
from ._helpers import (
    _is_staff, _safe_next_url, _filter_by_school,
    _set_active_school, _get_active_school, _user_manager_schools,
    _clean_query_params, _clean_query_value,
)
from ..model_parts.approvals import ApprovalState, PENDING_REVIEW_STATES
from ..model_parts.meetings import Meeting
from ..model_parts.plans import Initiative
from ..services_assignments import open_targets_for_assignee
from ..services_meetings import meetings_for_user
from ..staff_workspace import EMPTY_STAFF_WORKSPACES, build_staff_workspaces

# ما يُعاد إلى صاحبه ليعمل عليه. «أعِد النظر» و«أرفق ما نقص» رسالتان مختلفتان
# في دورة الاعتماد، لكنهما على لوحة صاحب العمل شيء واحد: كرةٌ في ملعبه.
REPORT_STATES_ON_OWNER = (ApprovalState.RETURNED, ApprovalState.NEEDS_INFO)

_EMPTY_INVOLVEMENT = {
    "my_open_targets": [],
    "open_targets_count": 0,
    "overdue_targets_count": 0,
    "my_upcoming_meetings": [],
    "upcoming_meetings_count": 0,
    "pending_minutes_count": 0,
    "my_initiatives": [],
    "draft_initiatives_count": 0,
}

_EMPTY_REPORT_APPROVAL = {
    "report_approval_enabled": False,
    "returned_reports": [],
    "returned_reports_count": 0,
    "awaiting_review_count": 0,
    "draft_reports_count": 0,
}

_EMPTY_ACHIEVEMENT_FOLLOWUP = {
    "returned_achievement_files": [],
    "returned_achievement_count": 0,
}

_EMPTY_TEACHER_SCOPE = {
    "teacher_scope_label": "",
}

_EMPTY_LAB = {
    "lab_summary": None,
    "lab_attention": [],
}


def _achievement_followup(user, school) -> dict:
    """ملفات الإنجاز التي عادت إلى صاحبها وتحتاج إجراءً منه.

    الإشعار قد يُقرأ أو يضيع بين إشعارات أخرى، أما الحالة نفسها فهي مصدر
    الحقيقة. لذلك تبقى في «متابعة اليوم» حتى يستكمل المعلم الملف ويرسله، لا
    حتى يفتح الإشعار فقط.
    """
    if school is None:
        return dict(_EMPTY_ACHIEVEMENT_FOLLOWUP)

    try:
        has_personal_achievement = SchoolMembership.objects.filter(
            Q(
                school=school,
                teacher=user,
                role_type=SchoolMembership.RoleType.TEACHER,
                is_active=True,
            )
            | Q(
                school=school,
                teacher=user,
                role_type=SchoolMembership.RoleType.ADMIN_STAFF,
                job_title=SchoolMembership.JobTitle.LAB_TECH,
                is_active=True,
            )
        ).exists()
        if not has_personal_achievement:
            return dict(_EMPTY_ACHIEVEMENT_FOLLOWUP)

        queryset = TeacherAchievementFile.objects.filter(
            teacher=user,
            school=school,
            status=TeacherAchievementFile.Status.RETURNED,
        ).order_by("-updated_at", "-id")
        return {
            "returned_achievement_files": list(queryset[:3]),
            "returned_achievement_count": queryset.count(),
        }
    except Exception:
        logger.exception("Achievement follow-up panel failed")
        return dict(_EMPTY_ACHIEVEMENT_FOLLOWUP)


def _teacher_scope(user, school) -> dict:
    """وصف قصير للسياق الفعلي: الأقسام التي يعمل فيها صاحب اللوحة."""
    if school is None or DepartmentMembership is None:
        return dict(_EMPTY_TEACHER_SCOPE)

    try:
        names = list(
            DepartmentMembership.objects.filter(
                teacher=user,
                department__school=school,
                department__is_active=True,
            )
            .values_list("department__name", flat=True)
            .distinct()
            .order_by("department__name")[:3]
        )
        return {
            "teacher_scope_label": " • ".join(str(name) for name in names if name),
        }
    except Exception:
        logger.exception("Teacher scope label failed")
        return dict(_EMPTY_TEACHER_SCOPE)


def _lab_panel(user, school) -> dict:
    """بطاقةُ المختبر على لوحة المحضّر — إضافةً إلى كل ما للمعلّم لا بدلاً منه.

    **يبقى المحضّر على لوحة المعلّم نفسها.** توصيفه يجعل صلاحيته صلاحيةَ الموظف
    الإداري: يكتب تقاريره، ويرفع ملف إنجازه، ويُكلَّف، ويُدعى إلى الاجتماعات.
    فلوحةٌ خاصة تُحلّ محلّ لوحته كانت ستُسقط ذلك كله لأجل بطاقتين. وإنما تُضاف
    البطاقة هنا، ويبقى المختبر شاشاتٍ قائمة بذاتها لمن أراد التفصيل.

    تُعرض لمن المختبر عملُه أو متابعتُه، ولا شيء فيها لغيره.
    """
    if school is None:
        return dict(_EMPTY_LAB)

    from ..permissions import can_view_lab

    try:
        if not can_view_lab(user, school):
            return dict(_EMPTY_LAB)

        from ..models import LabAsset
        from ..services_lab import assets_for_school, lab_summary

        return {
            "lab_summary": lab_summary(school, user=user),
            "lab_attention": list(
                assets_for_school(school, user=user).filter(
                    condition__in=LabAsset.ATTENTION_CONDITIONS
                )[:3]
            ),
        }
    except Exception:
        logger.exception("Lab panel failed")
        return dict(_EMPTY_LAB)


_EMPTY_SUPERVISION = {
    "supervision_rows": [],
    "supervision_total": 0,
    "supervision_scope_missing": False,
}


def _supervision_queue(user, school) -> dict:
    """ما ينتظر قرار هذا المنسوب بحكم ما مُنح — لا ما ينتظره هو من غيره.

    لوحة الهبوط كانت تعرض شيئاً واحداً: عملَ صاحبها. وهو تمامُ الصورة للمعلّم،
    ونصفُها للوكيل: الوكيل يُنتِج عملاً **ويُقرّر في عمل غيره**، والنصف الثاني
    كان غائباً عن لوحته كلياً — فتقريرٌ أُرسل إليه للمراجعة ينتظر في صندوق لا
    يقوده إليه شيء من رئيسيته.

    **البطاقة تُبنى مما مُنح لا مما يوجد.** سطرٌ عن التكليفات لمن لا يكلّف يخبره
    عن عمل لا يملك عليه إجراءً، وبندٌ لا إجراء له يُدرّب صاحبه على تجاهل البطاقة
    كلها — وهي العلّة نفسها التي عُزل من أجلها المحضرُ عن «ما يحتاج متابعتك».

    **نطاقٌ بلا أقسام يُعلَن لا يُخفى.** ``supervision_scope_missing`` تجعل
    اللوحة تقول «مُنحت صلاحيات ولم تُسنَد إليك أقسام» بدل أن تعرض أصفاراً تُقرأ
    «لا عمل عليك» — والفرق بينهما هو الفرق بين مطمئنٍّ ومغفِل.
    """
    if school is None:
        return dict(_EMPTY_SUPERVISION)

    from .. import capabilities as caps
    from ..permissions import (
        capability_source,
        is_lab_technician,
        supervised_department_ids,
    )

    try:
        granted = {
            code: capability_source(user, code, school) is not None
            for code in (
                caps.REVIEW_REPORTS,
                caps.ASSIGN_TASKS,
                caps.HANDLE_REQUESTS,
                caps.ARCHIVE_DOCUMENTS,
            )
        }
        if not any(granted.values()):
            return dict(_EMPTY_SUPERVISION)

        supervised = supervised_department_ids(user, school)
        if is_lab_technician(user, school) and not supervised:
            for code in (
                caps.REVIEW_REPORTS,
                caps.ASSIGN_TASKS,
                caps.HANDLE_REQUESTS,
            ):
                granted[code] = False
            if not any(granted.values()):
                return dict(_EMPTY_SUPERVISION)
        rows: list[dict] = []
        now = timezone.now()

        if granted[caps.REVIEW_REPORTS]:
            count = (
                Report.objects.filter(
                    school=school, approval_state__in=PENDING_REVIEW_STATES
                )
                .filter(category__departments__id__in=supervised)
                .distinct()
                .count()
                if supervised
                else 0
            )
            rows.append(
                {
                    "key": "reports",
                    "label": "تقارير تنتظر مراجعتك",
                    "count": count,
                    "url": reverse("reports:approval_inbox"),
                    "icon": "fa-clipboard-check",
                }
            )

        if granted[caps.ASSIGN_TASKS]:
            from ..model_parts.assignments import AssignmentTarget

            late = AssignmentTarget.objects.filter(
                assignment__school=school,
                assignment__issuer=user,
                assignment__due_at__lt=now,
            ).exclude(approval_state=ApprovalState.APPROVED)
            rows.append(
                {
                    "key": "assignments",
                    "label": "تكليفات أصدرتَها وتأخّرت",
                    "count": late.count(),
                    "url": reverse("reports:assignment_board"),
                    "icon": "fa-diagram-project",
                }
            )

        if granted[caps.HANDLE_REQUESTS]:
            count = (
                Ticket.objects.filter(
                    school=school,
                    is_platform=False,
                    department_id__in=supervised,
                    status__in=["open", "in_progress"],
                ).count()
                if supervised
                else 0
            )
            rows.append(
                {
                    "key": "requests",
                    "label": "طلبات مفتوحة في نطاقك",
                    "count": count,
                    "url": reverse("reports:manager_school_tickets"),
                    "icon": "fa-list-check",
                }
            )

        if granted[caps.ARCHIVE_DOCUMENTS]:
            from ..model_parts.documents import Document

            rows.append(
                {
                    "key": "documents",
                    "label": "وثائق تنتظر الأرشفة",
                    "count": Document.objects.filter(
                        school=school, approval_state__in=PENDING_REVIEW_STATES
                    ).count(),
                    "url": reverse("reports:document_archive"),
                    "icon": "fa-folder-tree",
                }
            )

        return {
            "supervision_rows": rows,
            "supervision_total": sum(int(row["count"]) for row in rows),
            # يحتاج النطاق أقساماً فقط حين تعتمد صلاحيةٌ ممنوحة عليها.
            "supervision_scope_missing": bool(
                not supervised
                and (granted[caps.REVIEW_REPORTS] or granted[caps.HANDLE_REQUESTS])
            ),
        }
    except Exception:
        logger.exception("Supervision queue panel failed")
        return dict(_EMPTY_SUPERVISION)


def _staff_involvement(user, school) -> dict:
    """ما المنسوب طرفٌ فيه: تكليفاته، اجتماعاته، مبادراته.

    الشاشات الثلاث موجودة ويصلها من القائمة، لكن لوحته كانت تعرض تقاريره
    وطلباته وحدهما — فما كُلّف به أو دُعي إليه لا يُرى إلا إن عرف أين يبحث عنه.
    هذه الدالة تجمعه في مكان واحد.

    **لمن؟** لكل من تُعرض له هذه اللوحة لا للمعلّم وحده: المدير له لوحته،
    ومن سواه — وكيلاً كان أو موظفاً إدارياً أو محضّر مختبر — يهبط هنا. ولذلك
    لا شيء في هذه الدالة مربوط بدور بعينه.

    **العدّ في القاعدة لا في الذاكرة.** جلبُ كل ما يخصّ المستخدم ثم ترشيحه
    بـ ``for`` يجعل كلفة اللوحة تابعةً لتاريخ صاحبها الكامل، وهو ما يظهر بعد
    عام من الاستعمال لا في يومه الأول.

    تُرجع سياقاً فارغاً ومتّسقاً عند أي خطأ: اللوحة صفحةُ هبوط، وسقوطها
    لأجل بطاقة جانبية أسوأ من غياب البطاقة.
    """
    if school is None:
        return dict(_EMPTY_INVOLVEMENT)

    try:
        now = timezone.now()

        open_targets = open_targets_for_assignee(user, school)
        # موعدٌ فات ولم يُعتمد = متأخر، وهو نصّ ``is_overdue`` نفسه بعد أن
        # رشّحت الخدمةُ الملغى والمعتمد.
        overdue_targets_count = open_targets.filter(
            assignment__due_at__lt=now
        ).count()

        my_meetings = meetings_for_user(user, school=school)
        # الحاضرون لا تعرضهم اللوحة، فلا داعي لاستعلام يجلبهم.
        # والأقرب أولاً: ترتيب القائمة الأصلي ترتيب أرشيف لا ترقّب.
        upcoming = (
            my_meetings.prefetch_related(None)
            .filter(status=Meeting.Status.SCHEDULED, scheduled_at__gte=now)
            .order_by("scheduled_at", "id")
        )

        # **المحضر عمل من يحرّره لا من يحضره.** توصيف الأدوار يجعل كتابته من
        # مهام الموظف الإداري واعتماده من مهام المدير، فوضعُه في «ما يحتاج
        # متابعتك» لكل حاضرٍ يضع عليه بنداً لا يملك عليه إجراءً — وبندٌ لا
        # إجراء له يُدرّب صاحبه على تجاهل البطاقة كلها.
        pending_minutes_count = (
            my_meetings.filter(status=Meeting.Status.HELD)
            .filter(Q(organizer=user) | Q(minutes__recorder=user))
            .exclude(minutes__approval_state=ApprovalState.APPROVED)
            .count()
        )

        initiatives = Initiative.objects.filter(school=school, teacher=user)

        return {
            "my_open_targets": list(open_targets[:4]),
            "open_targets_count": open_targets.count(),
            "overdue_targets_count": overdue_targets_count,
            "my_upcoming_meetings": list(upcoming[:3]),
            "upcoming_meetings_count": upcoming.count(),
            "pending_minutes_count": pending_minutes_count,
            # البطاقة تعرض العنوان والحالة، فلا ضمَّ لخطةٍ لا تُذكر.
            "my_initiatives": list(initiatives.order_by("-created_at", "-id")[:3]),
            "draft_initiatives_count": initiatives.filter(
                approval_state=ApprovalState.DRAFT
            ).count(),
        }
    except Exception:
        logger.exception("Staff involvement panel failed")
        return dict(_EMPTY_INVOLVEMENT)


def _report_approval(my_reports_qs, school) -> dict:
    """حال تقارير صاحبها في دورة الاعتماد.

    التقرير صار يمرّ بمراجعة واعتماد، ولوحةٌ تعرض «أحدث تقاريري» بلا حالاتها
    تُخفي أهمّ ما استجدّ: تقريرٌ أُعيد بملاحظة يبدو فيها منجزاً، فينتظر صاحبه
    اعتماداً لا يأتي لأن الكرة في ملعبه هو.

    **المفتاح يكتم الوسم لا الخبر.** الاعتماد اختيار لكل مدرسة، ومدرسةٌ لم
    تفعّله تُحفظ تقاريرها معتمدةً فوراً — فوسم «معتمد» على كل صفّ فيها ضجيجٌ
    يخبر عن لا شيء، ولذلك يحكم المفتاحُ الوسمَ وحده. أما ما أُعيد إلى صاحبه
    فيُعرض متى وُجد: مدرسةٌ أوقفت الدورة بعد أن أعادت تقريراً تترك صاحبه أمام
    عملٍ عالقٍ لا يعلم به، وإخفاؤه ليس صمتاً بل حجب.
    """
    empty = dict(_EMPTY_REPORT_APPROVAL)
    if school is None:
        return empty

    approval_enabled = bool(getattr(school, "report_approval_enabled", False))
    empty["report_approval_enabled"] = approval_enabled

    try:
        counts = my_reports_qs.aggregate(
            returned=Count("id", filter=Q(approval_state__in=REPORT_STATES_ON_OWNER)),
            awaiting=Count("id", filter=Q(approval_state__in=PENDING_REVIEW_STATES)),
            drafts=Count("id", filter=Q(approval_state=ApprovalState.DRAFT)),
        )
        returned_count = int(counts.get("returned") or 0)
        returned = (
            list(
                my_reports_qs.filter(
                    approval_state__in=REPORT_STATES_ON_OWNER
                ).order_by("-report_date", "-id")[:3]
            )
            if returned_count
            else []
        )
        return {
            "report_approval_enabled": approval_enabled,
            "returned_reports": returned,
            "returned_reports_count": returned_count,
            "awaiting_review_count": int(counts.get("awaiting") or 0),
            "draft_reports_count": int(counts.get("drafts") or 0),
        }
    except Exception:
        logger.exception("Report approval panel failed")
        return empty


@login_required(login_url="reports:login")
@access_required(_is_staff)
@require_http_methods(["GET", "POST"])
def select_school(request: HttpRequest) -> HttpResponse:
    """شاشة اختيار المدرسة للآدمن ومديري المدارس.

    - المستخدم السوبر يوزر يشاهد جميع المدارس.
    - مدير المدرسة يشاهد فقط المدارس التي هو مدير لها.
    """

    if request.user.is_superuser:
        schools_qs = School.objects.filter(is_active=True)
    else:
        manager_schools = _user_manager_schools(request.user)
        schools_qs = School.objects.filter(id__in=[s.id for s in manager_schools], is_active=True)

    # إن لم يكن للمستخدم أي مدارس مرتبطة به نسمح له برؤية لا شيء

    if request.method == "POST":
        sid = request.POST.get("school_id")
        try:
            school = schools_qs.get(pk=sid)
            _set_active_school(request, school)
            messages.success(request, f"تم اختيار المدرسة: {school.name}")
            return redirect("reports:admin_dashboard")
        except (School.DoesNotExist, ValueError, TypeError):
            messages.error(request, "تعذّر اختيار المدرسة. فضلاً اختر مدرسة صحيحة.")

    search_query = _clean_query_value(request.GET.get("q"))
    if search_query:
        schools_qs = schools_qs.filter(
            Q(name__icontains=search_query)
            | Q(code__icontains=search_query)
            | Q(city__icontains=search_query)
            | Q(stage__icontains=search_query)
            | Q(gender__icontains=search_query)
        )

    schools_qs = schools_qs.order_by("name", "id").only(
        "id",
        "name",
        "code",
        "city",
        "stage",
        "gender",
    )
    page_obj = Paginator(schools_qs, 24).get_page(request.GET.get("page") or 1)

    context = {
        "schools": page_obj,
        "page_obj": page_obj,
        "current_school": _get_active_school(request),
        "search_query": search_query,
        "total_schools_count": page_obj.paginator.count,
        "query_params_without_page": _clean_query_params(request.GET),
    }
    return render(request, "reports/select_school.html", context)


@login_required(login_url="reports:login")
@require_http_methods(["POST"])
def switch_school(request: HttpRequest) -> HttpResponse:
    """تبديل المدرسة النشطة بسرعة من الهيدر/القائمة.

    **POST وحده.** كان يقبل ``GET`` أيضاً ويقرأ ``school_id`` منه، وDjango لا
    يفرض رمز CSRF على ``GET`` — فوسمُ ``<img src="…/switch-school/?school_id=N">``
    في أي صفحة يبدّل المدرسة النشطة لمن يديرون أكثر من مدرسة، بصمت ومن موقع
    آخر. الضرر ليس ترقية صلاحية — المدرسة تبقى من مدارسه — بل **الالتباس**:
    التعميم التالي يذهب إلى مدرسة لم يقصدها، والفعل يُنسب إليه.

    وتبديل المدرسة تغييرٌ في الحالة، وتغييرُ الحالة عبر GET خطأ في ذاته.
    """
    sid = request.POST.get("school_id")
    next_raw = request.POST.get("next")

    current_school = _get_active_school(request)
    current_is_manager = bool(
        current_school is not None
        and is_school_manager(request.user, active_school=current_school)
    )
    default_next = (
        "reports:admin_dashboard"
        if getattr(request.user, "is_superuser", False) or current_is_manager
        else "reports:home"
    )
    safe_next = _safe_next_url(next_raw)
    next_url = safe_next or default_next

    if not sid:
        return redirect(next_url)

    if request.user.is_superuser:
        schools_qs = School.objects.filter(is_active=True)
    else:
        # الدور مقيد بالمدرسة لا بالحساب: من يدير مدرسة ويدرّس في أخرى يجب أن
        # يجد المدرستين معاً، ثم تتشكل الواجهة حسب دوره في المدرسة المختارة.
        schools_qs = (
            School.objects.filter(
                is_active=True,
                memberships__teacher=request.user,
                memberships__is_active=True,
            )
            .distinct()
        )

    try:
        school = schools_qs.get(pk=sid)
        _set_active_school(request, school)
        if safe_next is None:
            next_url = (
                "reports:admin_dashboard"
                if getattr(request.user, "is_superuser", False)
                or is_school_manager(request.user, active_school=school)
                else "reports:home"
            )
        messages.success(request, f"تم اختيار المدرسة: {school.name}")
    except (School.DoesNotExist, ValueError, TypeError):
        messages.error(request, "تعذّر تبديل المدرسة. فضلاً اختر مدرسة صحيحة.")

    return redirect(next_url)

# =========================
# الرئيسية (لوحة المعلم)
# =========================
@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def home(request: HttpRequest) -> HttpResponse:
    active_school = _get_active_school(request)

    # مدير المدرسة له رئيسية واحدة واضحة: لوحة إدارة المدرسة.
    # يحتفظ المدير بإمكانية الوصول لصفحاته الشخصية من قائمة الحساب، لكن لا
    # ينبغي أن تقوده كلمة "الرئيسية" إلى لوحة المعلم وتخلق مسارين متنافسين.
    if active_school is not None and is_school_manager(
        request.user,
        active_school=active_school,
    ):
        return redirect("reports:admin_dashboard")

    # بلا سياق مدرسي لا يمكن حسم دور متعدد المدارس. المدير يُقاد إلى مسار
    # الإدارة/اختيار المدرسة، أما وجود مدرسة نشطة أعلاه فيجعل دورها هو الحاكم.
    if active_school is None and is_school_manager(request.user):
        return redirect("reports:admin_dashboard")

    # والمدير التنفيذي كذلك: رئيسيته لوحة مجموعته.
    #
    # **بشرط ألا يكون منسوباً في مدرسة.** من جمع الصفتين — مديراً تنفيذياً
    # وله نصاب في إحدى مدارس مجموعته — يفقد بالتحويل لوحته الشخصية ولا يجد
    # طريقاً إليها، لأن «الرئيسية» هي طريقها الوحيد. فالتحويل مقصور على
    # الحالة التي صُمِّم لها الدور: عضوية على المجموعة وحدها.
    if is_executive_director(request.user) and not SchoolMembership.objects.filter(
        teacher=request.user, is_active=True
    ).exists():
        return redirect("reports:executive_dashboard")

    # The shared PWA starts at /home/. Route teachers whose only current
    # workspace is personal to its dashboard, including stale school sessions.
    if is_personal_only_workspace_user(request.user):
        return redirect("personal:dashboard")

    stats = {"today_count": 0, "total_count": 0, "last_title": "—"}
    req_stats = {"open": 0, "in_progress": 0, "done": 0, "rejected": 0, "total": 0}

    # بطاقة هادئة لأحدث إشعار غير مقروء. لا تُسجّل القراءة إلا عند فتح التفاصيل
    # أو باختيار إجراء القراءة صراحةً.
    home_notification = None
    home_notification_recipient_id: int | None = None
    try:
        if NotificationRecipient is not None and Notification is not None:
            now = timezone.now()
            nqs = (
                NotificationRecipient.objects.select_related("notification", "notification__created_by")
                .filter(teacher=request.user)
            )

            # غير مقروء فقط
            try:
                if hasattr(NotificationRecipient, "is_read"):
                    nqs = nqs.filter(is_read=False)
                elif hasattr(NotificationRecipient, "read_at"):
                    nqs = nqs.filter(read_at__isnull=True)
            except Exception:
                pass

            # عزل حسب المدرسة النشطة (مع السماح بإشعارات عامة school=NULL)
            try:
                if hasattr(Notification, "school"):
                    if active_school is not None:
                        nqs = nqs.filter(Q(notification__school=active_school) | Q(notification__school__isnull=True))
                    else:
                        nqs = nqs.filter(notification__school__isnull=True)
            except Exception:
                pass

            # استبعاد المنتهي
            try:
                if hasattr(Notification, "expires_at"):
                    nqs = nqs.filter(Q(notification__expires_at__gt=now) | Q(notification__expires_at__isnull=True))
            except Exception:
                pass

            rec = nqs.order_by("-created_at", "-id").first()
            if rec is not None:
                home_notification = getattr(rec, "notification", None)
                try:
                    home_notification_recipient_id = int(rec.pk)
                except Exception:
                    home_notification_recipient_id = None
    except Exception:
        home_notification = None
        home_notification_recipient_id = None

    try:
        my_qs = _filter_by_school(
            Report.objects.filter(teacher=request.user).only(
                "id",
                "title",
                "report_date",
                "day_name",
                "beneficiaries_count",
                # حالة الاعتماد وملاحظتها تُقرآن على اللوحة، وتأجيلهما هنا
                # يعني استعلاماً لكل صفّ عند أول عرضٍ لهما في القالب.
                "approval_state",
                "review_note",
            ),
            active_school,
        )
        today = timezone.localdate()
        stats["total_count"] = my_qs.count()
        stats["today_count"] = my_qs.filter(report_date=today).count()
        last_report = my_qs.order_by("-report_date", "-id").first()
        stats["last_title"] = (last_report.title if last_report else "—")

        report_approval = _report_approval(my_qs, active_school)
        # ما عُرض في «أُعيدت إليك» لا يُعاد في «آخر ما وثّقت»: صفٌّ واحد مرتين
        # في بطاقة واحدة يجعل القارئ يحسبهما تقريرين.
        recent_qs = my_qs.order_by("-report_date", "-id")
        shown_ids = [r.pk for r in report_approval["returned_reports"]]
        if shown_ids:
            recent_qs = recent_qs.exclude(pk__in=shown_ids)
        recent_reports = list(recent_qs[:4])

        my_tickets_qs = _filter_by_school(
            Ticket.objects.filter(creator=request.user)
            .select_related("assignee", "department")
            .only("id", "title", "status", "department", "created_at", "assignee__name")
            .order_by("-created_at", "-id"),
            active_school,
        )
        agg = my_tickets_qs.aggregate(
            open=Count("id", filter=Q(status="open")),
            in_progress=Count("id", filter=Q(status="in_progress")),
            done=Count("id", filter=Q(status="done")),
            rejected=Count("id", filter=Q(status="rejected")),
            total=Count("id"),
        )
        for k in req_stats.keys():
            req_stats[k] = int(agg.get(k) or 0)
        recent_tickets = list(my_tickets_qs[:5])

        return render(
            request,
            "reports/home.html",
            {
                "stats": stats,
                "recent_reports": recent_reports,
                "req_stats": req_stats,
                "recent_tickets": recent_tickets[:3],
                "active_requests_count": req_stats["open"] + req_stats["in_progress"],
                "home_notification": home_notification,
                "home_notification_recipient_id": home_notification_recipient_id,
                **_staff_involvement(request.user, active_school),
                **report_approval,
                **_achievement_followup(request.user, active_school),
                **_teacher_scope(request.user, active_school),
                **build_staff_workspaces(request.user, active_school),
                **_supervision_queue(request.user, active_school),
                **_lab_panel(request.user, active_school),
            },
        )
    except Exception:
        logger.exception("Home view failed")
        if settings.DEBUG or os.getenv("SHOW_ERRORS") == "1":
            html = "<h2>Home exception</h2><pre>{}</pre>".format(traceback.format_exc())
            return HttpResponse(html, status=500)
        # Never redirect back to this same view: if the failure is persistent
        # the browser ends up in an endless redirect loop instead of showing a
        # readable error.
        try:
            return render(request, "reports/home.html", {
                "stats": stats,
                "recent_reports": [],
                "req_stats": req_stats,
                "recent_tickets": [],
                "active_requests_count": 0,
                "home_notification": None,
                "home_notification_recipient_id": None,
                "home_load_failed": True,
                # القالب يقرأ هذه المفاتيح، وغيابها يجعل صفحة العطل تعتمد على
                # صمت ``TEMPLATE_STRING_IF_INVALID`` بدل أن تعتمد على قيمة.
                **_EMPTY_INVOLVEMENT,
                **_EMPTY_REPORT_APPROVAL,
                **_EMPTY_ACHIEVEMENT_FOLLOWUP,
                **_EMPTY_TEACHER_SCOPE,
                **EMPTY_STAFF_WORKSPACES,
                **_EMPTY_SUPERVISION,
                **_EMPTY_LAB,
            }, status=500)
        except Exception:
            # The template itself may be the failing part; fall back to plain text.
            logger.exception("Home view fallback rendering failed")
            return HttpResponse(
                "تعذّر تحميل الصفحة الرئيسية حالياً. حاول مجدداً بعد قليل.",
                status=500,
                content_type="text/plain; charset=utf-8",
            )
