# -*- coding: utf-8 -*-
"""شاشة «الأدوار والصلاحيات» لمدير المدرسة.

ما تحلّه هذه الشاشة: كان المدير يملك دورين اثنين (مدير/معلّم) ومسمّى وظيفياً
لا أثر له، فلم يكن أمامه سبيل لتمثيل وكيل أو موظف إداري إلا أن يعامل الجميع
معلمين. وهنا يوزّع الأدوار، ويحدّد نطاق كل وكيل، ويفوّض مؤقتاً عند غيابه.

الحدود التي تفرضها الشاشة على المدير نفسه:
- لا يُسند دور «مدير» — نقل الإدارة قرار خارج المدرسة.
- لا يمنح صلاحية غير معرَّفة في مرجع الكود.
- لا يفوّض من ليس منسوباً في مدرسته.
وكلها منفَّذة في النماذج لا في القالب.
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from .. import capabilities as caps
from ..audit_labels import attach_views
from ..forms_staff_roles import DelegationForm, StaffRoleAssignForm, StaffScopeForm
from ..gender_labels import school_gender_labels
from ..models import AuditLog, Delegation, SchoolMembership, StaffScope
from ..permissions import prefetch_memberships_for_school, role_required
from ._helpers import *  # noqa: F401,F403 — نفس مدخلات بقية شاشات المدير
from ._helpers import _get_active_school, _user_manager_schools

__all__ = [
    "staff_roles",
    "staff_role_scope",
    "delegation_revoke",
]


def _require_manager_school(request):
    """المدرسة النشطة التي يديرها الطالب — أو إعادة توجيه."""
    active_school = _get_active_school(request)
    if active_school is None:
        messages.error(request, "فضلاً اختر مدرسة أولاً.")
        return None, redirect("reports:select_school")
    if (not request.user.is_superuser) and active_school not in _user_manager_schools(request.user):
        messages.error(request, "ليست لديك صلاحية كمدير على هذه المدرسة.")
        return None, redirect("reports:select_school")
    return active_school, None


def _display_role(membership, labels) -> tuple[str, str]:
    """(نوع الوسم، مسمّاه) لعضوية بعينها.

    المسمّى الوظيفي يسبق الدور حين يكون أدقّ منه: ``admin_staff`` وسمٌ لصلاحية،
    و«محضر المختبر» اسمُ عمل — والمدير يبحث في كشفه عن الثاني.
    """
    role_type = str(getattr(membership, "role_type", "") or "")
    job_title = str(getattr(membership, "job_title", "") or "")

    if role_type == SchoolMembership.RoleType.DEPUTY:
        return "deputy", str(labels["deputy"])
    if job_title == SchoolMembership.JobTitle.LAB_TECH:
        return "lab_tech", str(labels["lab_tech"])
    if role_type == SchoolMembership.RoleType.ADMIN_STAFF:
        return "admin_staff", str(labels["admin_staff"])
    return "teacher", str(labels["teacher_indefinite"])


def _roster(school) -> list[dict]:
    """منسوبو المدرسة، صفٌّ لكل شخص لا لكل عضوية.

    الشاشة تعرض أشخاصاً لا عضويات: صاحب الدورين سطر واحد يحمل وسمَين، لا
    سطران يوهمان بأنهما رجلان.

    والوسم يُقرأ من الدور **والمسمّى** معاً: محضّر المختبر موظف إداري صلاحيةً،
    فلو عُرض بدوره وحده لظهر «موظف إداري» ولم يجده المدير في كشفه باسمه.
    """
    memberships = (
        SchoolMembership.objects.filter(
            school=school,
            is_active=True,
            role_type__in=SchoolMembership.STAFF_ROLES,
        )
        .select_related("teacher")
        .prefetch_related("scope__departments")
        .order_by("teacher__name", "id")
    )

    by_person: dict[int, dict] = {}
    for membership in memberships:
        row = by_person.setdefault(
            membership.teacher_id,
            {
                "person": membership.teacher,
                "memberships": [],
                "roles": [],
                "primary": None,
                "scope": None,
            },
        )
        row["memberships"].append(membership)
        row["roles"].append(membership.role_type)

    # الدور «الرئيسي» هو الأعلى إشرافاً — فهو ما يُعرض وسماً أولَ وما يُضبط نطاقه.
    precedence = {
        SchoolMembership.RoleType.DEPUTY: 0,
        SchoolMembership.RoleType.ADMIN_STAFF: 1,
        SchoolMembership.RoleType.TEACHER: 2,
    }
    labels = school_gender_labels(school)
    rows = []
    for row in by_person.values():
        primary = min(row["memberships"], key=lambda m: precedence.get(m.role_type, 9))
        row["primary"] = primary
        row["kind"], row["role_label"] = _display_role(primary, labels)
        row["scope"] = getattr(primary, "scope", None)
        row["permission_role_label"] = primary.get_role_type_display()
        row["job_title_label"] = primary.get_job_title_display()
        row["scope_capability_count"] = len(
            getattr(row["scope"], "capabilities", None) or []
        )
        row["can_have_scope"] = primary.role_type in {
            SchoolMembership.RoleType.DEPUTY,
            SchoolMembership.RoleType.ADMIN_STAFF,
        }
        row["also_teaches"] = (
            SchoolMembership.RoleType.TEACHER in row["roles"]
            and primary.role_type != SchoolMembership.RoleType.TEACHER
        )
        rows.append(row)

    rows.sort(
        key=lambda r: (
            precedence.get(r["primary"].role_type, 9),
            (getattr(r["person"], "name", "") or ""),
        )
    )
    return rows


# الفرز والبحث يقعان على الكشف كاملاً، والصفحة تُقتطع منه بعدهما — فالعدّاد
# يقول «١٤ وكيلاً» ولو لم يظهر منهم في هذه الصفحة إلا اثنان.
ROSTER_PAGE_SIZE = 24


def _matches(row, needle: str) -> bool:
    """بحثٌ بالاسم أو الجوال أو المسمّى الوظيفي.

    ثلاثتها لأن المدير يبحث بما يذكره: اسمَ من يعرفه، ورقمَ من أدخله للتوّ،
    و«محضر المختبر» حين ينسى اسمه.
    """
    person = row["person"]
    haystack = " ".join(
        str(part or "")
        for part in (
            getattr(person, "name", ""),
            getattr(person, "phone", ""),
            row.get("role_label", ""),
            getattr(row["primary"], "job_title", ""),
        )
    ).lower()
    return all(word in haystack for word in needle.lower().split())


def _kind_of(row) -> str:
    return str(row.get("kind") or "teacher")


def _roster_counts(rows) -> dict:
    counts = {"all": len(rows), "deputy": 0, "admin_staff": 0, "lab_tech": 0, "teacher": 0}
    for row in rows:
        counts[_kind_of(row)] = counts.get(_kind_of(row), 0) + 1
    counts["needs_scope"] = sum(
        1 for row in rows if row["can_have_scope"] and not row["scope"]
    )
    return counts


def _filter_rows(rows, *, needle: str, kind: str) -> list[dict]:
    result = rows
    if kind == "needs_scope":
        result = [row for row in result if row["can_have_scope"] and not row["scope"]]
    elif kind in {"deputy", "admin_staff", "lab_tech", "teacher"}:
        result = [row for row in result if _kind_of(row) == kind]
    if needle:
        result = [row for row in result if _matches(row, needle)]
    return result


def _delegation_presets() -> list[dict]:
    """توليفات جاهزة للتفويض المؤقت.

    التفويض يُمنح على عجل — قبل سفر أو إجازة — وعشرون خانةً في تلك اللحظة تدفع
    المدير إما إلى تفويض كل شيء أو إلى تأجيل الأمر. فتُعرض ثلاث حالات مفهومة،
    ويبقى التعديل اليدوي فوقها لمن يحتاجه.
    """
    available = [item.code for item in caps.ALL if item.available]
    review = [
        code
        for code in (caps.REVIEW_REPORTS, caps.RECOMMEND_APPROVAL, caps.HANDLE_REQUESTS)
        if code in available
    ]
    operations = [
        code
        for code in (
            caps.HANDLE_REQUESTS,
            caps.DRAFT_CIRCULARS,
            caps.ARCHIVE_DOCUMENTS,
            caps.MANAGE_MEETINGS,
            caps.ASSIGN_TASKS,
        )
        if code in available
    ]
    return [
        {
            "code": "full",
            "label": "تصريف الأعمال كاملاً",
            "hint": "كل ما هو نافذ — للسفر والإجازة الطويلة.",
            "capabilities": available,
        },
        {
            "code": "review",
            "label": "المراجعة والتوصية",
            "hint": "لا يتوقّف سير الأعمال بانتظار عودتك.",
            "capabilities": review,
        },
        {
            "code": "operations",
            "label": "الأعمال اليومية",
            "hint": "الطلبات والتعاميم والاجتماعات والأرشفة.",
            "capabilities": operations,
        },
    ]


def _delegation_capability_groups() -> list[tuple[str, list]]:
    """الصلاحيات النافذة مجمّعة كما تُعرض — مجموعةً مجموعة لا قائمةً واحدة."""
    groups: dict[str, list] = {}
    for item in caps.ALL:
        if item.available:
            groups.setdefault(item.group, []).append(item)
    return list(groups.items())


def _mark_invalid_fields(form) -> None:
    """اربط أخطاء الخادم بحقولها دون نقل التحقق إلى JavaScript."""
    for name in form.errors:
        if name not in form.fields:
            continue
        attrs = form.fields[name].widget.attrs
        attrs["aria-invalid"] = "true"
        attrs["aria-describedby"] = f"{form[name].id_for_label}-errors"


@login_required(login_url="reports:login")
@role_required({"manager"})
@require_http_methods(["GET", "POST"])
def staff_roles(request):
    """الشاشة الرئيسية: كشف الأدوار + الإسناد + التفويضات."""
    active_school, redirect_response = _require_manager_school(request)
    if redirect_response is not None:
        return redirect_response

    assign_form = StaffRoleAssignForm(school=active_school)
    delegation_form = DelegationForm(school=active_school, delegator=request.user)

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()

        if action == "assign_role":
            assign_form = StaffRoleAssignForm(request.POST, school=active_school)
            if assign_form.is_valid():
                with transaction.atomic():
                    membership = assign_form.apply(actor=request.user)
                messages.success(
                    request,
                    f"تم إسناد دور «{assign_form.selected_label}» إلى {membership.teacher.name}.",
                )
                return redirect("reports:staff_roles")
            messages.error(request, "تعذّر إسناد الدور — تحقّق من الحقول.")

        elif action == "grant_delegation":
            delegation_form = DelegationForm(
                request.POST, school=active_school, delegator=request.user
            )
            if delegation_form.is_valid():
                delegation = delegation_form.save(commit=False)
                delegation.school = active_school
                delegation.delegator = request.user
                try:
                    delegation.full_clean()
                except Exception as exc:  # عرض رسائل التحقق النموذجية كما هي
                    messages.error(request, getattr(exc, "messages", ["تعذّر منح التفويض."])[0])
                else:
                    delegation.save()
                    messages.success(
                        request,
                        f"تم تفويض {delegation.delegate.name} حتى "
                        f"{timezone.localtime(delegation.ends_at):%Y-%m-%d %H:%M}.",
                    )
                    return redirect("reports:staff_roles")
            else:
                messages.error(request, "تعذّر منح التفويض — تحقّق من الحقول.")

        else:
            messages.error(request, "إجراء غير معروف.")
            return redirect("reports:staff_roles")

    school_labels = school_gender_labels(active_school)
    _mark_invalid_fields(assign_form)
    _mark_invalid_fields(delegation_form)
    delegations = list(
        Delegation.objects.filter(school=active_school)
        .select_related("delegate", "delegator")
        .order_by("-starts_at", "-id")[:25]
    )
    for delegation in delegations:
        delegation.capability_labels = [
            caps.BY_CODE[code].label
            for code in delegation.capabilities
            if code in caps.BY_CODE
        ]
    live_delegations = [d for d in delegations if d.state in {"active", "scheduled"}]
    past_delegations = [d for d in delegations if d.state not in {"active", "scheduled"}]

    access_audit_logs = list(
        AuditLog.objects.filter(
            school=active_school,
            model_name__in=("SchoolMembership", "StaffScope", "Delegation"),
        )
        .select_related("teacher")
        .order_by("-timestamp")[:8]
    )
    attach_views(access_audit_logs)

    # كشفٌ من مئتَي منسوب لا يُقرأ دفعةً واحدة: يُبحث فيه ويُصفّى ثم يُصفَّح.
    needle = (request.GET.get("q") or "").strip()
    kind = (request.GET.get("role") or "").strip()
    all_rows = _roster(active_school)
    counts = _roster_counts(all_rows)
    matched = _filter_rows(all_rows, needle=needle, kind=kind)

    paginator = Paginator(matched, ROSTER_PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get("page"))
    page_rows = list(page_obj.object_list)
    # التهيئة على صفحةٍ واحدة لا على الكشف كله — وهي أصل التوفير حين يكبر العدد.
    prefetch_memberships_for_school([row["person"] for row in page_rows], active_school)

    return render(
        request,
        "reports/staff_roles.html",
        {
            "active": "staff_roles",
            "active_school": active_school,
            "rows": page_rows,
            "page_obj": page_obj,
            "paginator": paginator,
            "counts": counts,
            "matched_count": len(matched),
            "query": needle,
            "role_filter": kind,
            "is_filtered": bool(needle or kind),
            "assign_form": assign_form,
            "delegation_form": delegation_form,
            "delegation_presets": _delegation_presets(),
            "delegation_groups": _delegation_capability_groups(),
            "delegation_selected": list(delegation_form["capabilities"].value() or []),
            "delegations": delegations,
            "live_delegations": live_delegations,
            "past_delegations": past_delegations,
            "access_audit_logs": access_audit_logs,
            # اللوحة التي تُفتح أولاً: حيث وقع الخطأ إن وقع، وإلا فالكشف.
            "open_tab": (
                "delegate"
                if delegation_form.is_bound and delegation_form.errors
                else "assign"
                if assign_form.is_bound and assign_form.errors
                else "roster"
            ),
            "active_delegation_count": sum(1 for d in delegations if d.state == "active"),
            # الشرح يُبنى قائمةً بمسمّياتها المعروضة، لا قاموساً بمفاتيح الدور:
            # المفتاح كان يُطبع في القالب كما هو (``deputy``) فيقرأ المدير
            # مصطلحاً إنجليزياً لا يعنيه.
            "role_help": [
                {"label": label, "text": text}
                for label, text in (
                    (school_labels["deputy"], "إشراف ومراجعة ضمن نطاق يحدّده المدير. لا يعتمد نهائياً."),
                    (school_labels["admin_staff"], "إعداد وتنفيذ وتوثيق. يرفع عمله للمراجعة."),
                    (
                        school_labels["lab_tech"],
                        "تجهيز المختبر وتوثيق تجاربه وعُهدته. صلاحيته صلاحية الموظف الإداري، ونطاقه يُضبط كنطاقه.",
                    ),
                    (school_labels["teacher_indefinite"], "توثيق أعماله المهنية وملف إنجازه وتنفيذ التكليفات."),
                )
            ],
        },
    )


@login_required(login_url="reports:login")
@role_required({"manager"})
@require_http_methods(["GET", "POST"])
def staff_role_scope(request, pk: int):
    """ضبط نطاق عضوية بعينها."""
    active_school, redirect_response = _require_manager_school(request)
    if redirect_response is not None:
        return redirect_response

    membership = get_object_or_404(
        SchoolMembership.objects.select_related("teacher", "school"),
        pk=pk,
        school=active_school,
        is_active=True,
    )
    if membership.role_type not in {
        SchoolMembership.RoleType.DEPUTY,
        SchoolMembership.RoleType.ADMIN_STAFF,
    }:
        messages.error(request, "النطاق يُضبط للوكيل أو الموظف الإداري فقط.")
        return redirect("reports:staff_roles")

    scope = getattr(membership, "scope", None)
    if scope is None:
        scope = StaffScope(membership=membership)

    if request.method == "POST":
        form = StaffScopeForm(
            request.POST,
            instance=scope,
            school=active_school,
            role_type=membership.role_type,
        )
        template_code = (request.POST.get("template_code") or "").strip()
        apply_template = (request.POST.get("action") or "") == "apply_template"

        if apply_template and template_code:
            # تطبيق القالب لا يحفظ: يملأ الخانات ويترك القرار للمدير، فلا
            # يُفاجأ بحفظٍ لم يطلبه.
            template = caps.TEMPLATES_BY_CODE.get(template_code)
            if template is not None and template.role == membership.role_type:
                form = StaffScopeForm(
                    instance=scope,
                    school=active_school,
                    role_type=membership.role_type,
                    initial={
                        "capabilities": list(template.capabilities),
                        "template_code": template.code,
                        # القالب قرار تنظيمي متكامل: اختيار «شؤون الطلاب» ثم
                        # مطالبة المدير باختيار المجال نفسه ثانيةً احتكاكٌ بلا
                        # معنى، وقد يترك القالب والمجال متعارضين. الأقسام تبقى
                        # قراراً مستقلاً لأنها تختلف من مدرسة لأخرى.
                        "domain": template.domain or scope.domain,
                        "departments": scope.departments.all() if scope.pk else [],
                    },
                )
                messages.info(request, f"طُبِّق قالب «{template.label}». راجعه ثم احفظ.")
            else:
                messages.error(request, "قالب غير معتمد لهذا الدور.")
        elif form.is_valid():
            scope = form.save(commit=False)
            scope.membership = membership
            if scope.granted_by_id is None:
                scope.granted_by = request.user
            scope.save()
            form.save_m2m()
            messages.success(request, f"تم حفظ نطاق {membership.teacher.name}.")
            return redirect("reports:staff_roles")
        else:
            messages.error(request, "تعذّر حفظ النطاق — تحقّق من الحقول.")
    else:
        form = StaffScopeForm(
            instance=scope,
            school=active_school,
            role_type=membership.role_type,
        )

    selected_codes = set(
        form.data.getlist("capabilities")
        if form.is_bound
        else (form.initial.get("capabilities") or [])
    )
    capability_summary = [
        {
            "code": item.code,
            "label": item.label,
            "description": item.description,
            "allowed": item.code in selected_codes,
        }
        for item in caps.capabilities_for_role(membership.role_type)
    ]
    fixed_boundaries = [
        "لا يعتمد الأعمال اعتمادًا نهائيًا؛ القرار يبقى لمدير المدرسة.",
        "لا يضيف المستخدمين ولا يغيّر أدوارهم أو اشتراك المدرسة.",
        "لا يرى بيانات مدرسة أخرى أو أقسامًا خارج نطاقه.",
    ]
    _mark_invalid_fields(form)

    return render(
        request,
        "reports/staff_role_scope.html",
        {
            "active": "staff_roles",
            "active_school": active_school,
            "membership": membership,
            "scope": scope,
            "form": form,
            "templates": caps.templates_for_role(membership.role_type),
            "capability_groups": caps.grouped_for_role(membership.role_type),
            "capability_meta": caps.BY_CODE,
            "capability_summary": capability_summary,
            "fixed_boundaries": fixed_boundaries,
        },
    )


@login_required(login_url="reports:login")
@role_required({"manager"})
@require_http_methods(["POST"])
def delegation_revoke(request, pk: int):
    """سحب تفويض قائم. لا يُحذف الصف — المسحوب واقعة تبقى مقروءة."""
    active_school, redirect_response = _require_manager_school(request)
    if redirect_response is not None:
        return redirect_response

    delegation = get_object_or_404(Delegation, pk=pk, school=active_school)
    if delegation.revoked_at is not None:
        messages.info(request, "هذا التفويض مسحوب أصلاً.")
    else:
        delegation.revoke(by=request.user)
        messages.success(request, f"تم سحب تفويض {delegation.delegate.name}.")
    return redirect("reports:staff_roles")
