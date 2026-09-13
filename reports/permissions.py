# reports/permissions.py
# -*- coding: utf-8 -*-
from __future__ import annotations

from functools import wraps
from typing import Iterable, Set, Any, Optional, List

from django.contrib import messages
from django.db.models import QuerySet, Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect

from core.observability import report_degraded as _degraded

from .models import Department, SchoolGroup, SchoolGroupMembership, SchoolMembership, School

# نحاول الاستيراد المرن لعضويات الأقسام
try:
    from .models import DepartmentMembership  # type: ignore
except Exception:  # pragma: no cover
    DepartmentMembership = None  # type: ignore

__all__ = [
    "get_officer_departments",
    "get_officer_department",
    "get_member_departments",
    "is_officer",
    "is_department_member",
    "get_school_manager_school_ids",
    "is_school_manager",
    "is_school_deputy",
    "is_admin_staff",
    "is_lab_technician",
    "can_record_lab",
    "can_view_lab",
    "is_school_staff",
    "school_roles_for",
    "scope_capabilities",
    "active_delegations",
    "delegated_capabilities",
    "capability_source",
    "has_capability",
    "supervised_department_ids",
    "capability_required",
    "get_executive_director_group_ids",
    "is_executive_director",
    "executive_director_schools_qs",
    "executive_director_groups",
    "can_send_group_notification",
    "can_view_group_batch",
    "effective_user_role_label",
    "can_delete_report",
    "can_edit_report",
    "can_share_report",
    "platform_allowed_schools_qs",
    "role_required",
    "allowed_categories_for",
    "restrict_queryset_for_user",
]


def _coerce_school_id(value) -> Optional[int]:
    """معرّف مدرسة صحيح، أو ``None`` لكل ما ليس كذلك.

    القيمة تصل من الجلسة أو من كائن قد يكون ``None`` — فالتحويل يفشل بـ
    ``TypeError``/``ValueError`` وحدهما، وهما حالتان متوقَّعتان لا أعطال.
    """
    if not value:
        return None
    try:
        return int(value) or None
    except (TypeError, ValueError):
        return None


def _resolved_school_id(
    *,
    active_school: Optional[School] = None,
    active_school_id: Optional[int] = None,
) -> Optional[int]:
    return _coerce_school_id(active_school_id) or _coerce_school_id(
        getattr(active_school, "id", None) if active_school is not None else None
    )


def _school_role_labels(active_school: Optional[School]) -> dict[str, str]:
    from .gender_labels import school_gender_labels

    labels = school_gender_labels(active_school)
    return {
        "manager": str(labels["manager"]),
        "deputy": str(labels["deputy"]),
        "teacher": str(labels["teacher"]),
        "admin_staff": str(labels["admin_staff"]),
        "lab_tech": str(labels["lab_tech"]),
    }


def platform_allowed_schools_qs(user) -> QuerySet[School]:
    """Schools reachable from the platform back office (owner only)."""
    if not getattr(user, "is_superuser", False):
        return School.objects.none()
    return School.objects.filter(is_active=True)


# ==============================
# أدوات داخلية
# ==============================
def _school_membership_cache(user) -> dict:
    cache = getattr(user, "_school_membership_cache", None)
    if not isinstance(cache, dict):
        cache = {}
        user._school_membership_cache = cache
    return cache


def _membership_cache_key(school_id: int, role_types: Iterable[str] = ()) -> tuple:
    """مفتاح ذاكرة العضوية — بالصيغة نفسها التي يبنيها ``_get_school_membership``.

    وجودها في دالة واحدة مقصود: المفتاح المبني هنا يجب أن يطابق المبني هناك
    حرفاً بحرف، وإلا فشلت التهيئة المسبقة **بصمت** — لا خطأ ولا نتيجة خاطئة،
    فقط استعلام لكل صف يعود من حيث لا يُحتسب.
    """
    normalized = tuple(sorted({str(v).strip().lower() for v in (role_types or []) if str(v).strip()}))
    return (int(school_id), normalized)


def prefetch_memberships_for_school(teachers, school) -> None:
    """Batch-load SchoolMembership for *teachers* in one query and pre-populate
    each teacher's ``_school_membership_cache`` so that subsequent calls to
    ``effective_user_role_label`` / ``_get_school_membership`` hit the cache
    instead of issuing N individual queries.

    تُهيَّأ **كل** المفاتيح التي تسألها ``effective_user_role_label``: الفارغ،
    وكل دور منفرداً، ومجموعة المنسوبين. النسخة السابقة كانت تهيّئ مفتاحين
    فقط (الفارغ و``teacher``)، فكان فحص المدير يستعلم لكل صف في الكشف.

    ويُهيَّأ معها كاش **المدير التنفيذي**: منذ أن صارت ``effective_user_role_label``
    تسأل ``is_executive_director`` صار كل صفٍّ في الكشف يستعلم عضويات المجموعة
    لصاحبه. والعضويات هذه ليست في ``SchoolMembership`` فلا يغطّيها الاستعلام
    أعلاه — فتُجلب دفعةً واحدة هنا، وإلا عاد الكشفُ إلى N+1 من حيث لا يُحتسب.
    """
    if not teachers or not school:
        return
    school_id = int(school.id)
    teacher_ids = [t.id for t in teachers]

    director_group_ids: dict = {}
    try:
        rows = SchoolGroupMembership.objects.filter(
            user_id__in=teacher_ids,
            is_active=True,
            role_type=SchoolGroupMembership.RoleType.EXECUTIVE_DIRECTOR,
            group__is_active=True,
        ).values_list("user_id", "group_id")
        for user_id, group_id in rows:
            if group_id:
                director_group_ids.setdefault(user_id, set()).add(int(group_id))
    except Exception:
        director_group_ids = {}

    all_mems = list(
        SchoolMembership.objects
        .select_related("school")
        .filter(teacher_id__in=teacher_ids, school_id=school_id, is_active=True)
        .order_by("teacher_id", "id")
    )
    by_teacher: dict = {}
    for m in all_mems:
        by_teacher.setdefault(m.teacher_id, []).append(m)

    role_values = [str(value).strip().lower() for value in SchoolMembership.RoleType.values]

    for teacher in teachers:
        # الكاش يُملأ بالمفتاح نفسه الذي تقرؤه ``get_executive_director_group_ids``
        # — ومن لا مجموعة له يُخزَّن له الفراغ صراحةً، وإلا ظنّته الدالة «لم
        # يُسأل بعد» فاستعلمت له.
        if getattr(teacher, "_executive_director_group_ids_cache", None) is None:
            teacher._executive_director_group_ids_cache = tuple(
                sorted(director_group_ids.get(teacher.id, ()))
            )

        cache = _school_membership_cache(teacher)
        mems = by_teacher.get(teacher.id, [])
        cache.setdefault(_membership_cache_key(school_id), mems[0] if mems else None)

        for role in role_values:
            match = next((m for m in mems if str(m.role_type).strip().lower() == role), None)
            cache.setdefault(_membership_cache_key(school_id, [role]), match)

        staff_match = next(
            (m for m in mems if str(m.role_type).strip().lower() in set(SchoolMembership.STAFF_ROLES)),
            None,
        )
        cache.setdefault(
            _membership_cache_key(school_id, SchoolMembership.STAFF_ROLES), staff_match
        )


def _get_school_membership(
    user,
    *,
    active_school: Optional[School] = None,
    active_school_id: Optional[int] = None,
    role_types: Optional[Iterable[str]] = None,
) -> Optional[SchoolMembership]:
    """Fetch one active school membership and memoize it on the user object."""
    if not getattr(user, "is_authenticated", False):
        return None

    school_id = _resolved_school_id(active_school=active_school, active_school_id=active_school_id)
    if not school_id:
        return None

    # المفتاح يُبنى من الدالة المشتركة نفسها التي تستعملها التهيئة المسبقة،
    # فلا يمكن أن ينحرف أحدهما عن الآخر.
    cache_key = _membership_cache_key(school_id, role_types or [])
    normalized_role_types = cache_key[1]
    cache = _school_membership_cache(user)
    if cache_key in cache:
        return cache[cache_key]

    try:
        qs = (
            SchoolMembership.objects.select_related("school")
            .filter(teacher=user, school_id=school_id, is_active=True)
            .order_by("id")
        )
        if normalized_role_types:
            qs = qs.filter(role_type__in=list(normalized_role_types))
        membership = qs.first()
    except Exception:
        membership = None

    cache[cache_key] = membership
    return membership


def get_school_manager_school_ids(user) -> Set[int]:
    """Returns school ids where the user is an active school manager.

    Source of truth is ``SchoolMembership`` with ``role_type=manager``.
    """
    if not getattr(user, "is_authenticated", False):
        return set()

    cache = getattr(user, "_school_manager_ids_cache", None)
    if cache is not None:
        return set(cache)

    try:
        ids = {
            int(x)
            for x in SchoolMembership.objects.filter(
                teacher=user,
                is_active=True,
                role_type=SchoolMembership.RoleType.MANAGER,
            ).values_list("school_id", flat=True)
            if x
        }
    except Exception:
        ids = set()

    user._school_manager_ids_cache = tuple(sorted(ids))
    return ids


def is_school_manager(
    user,
    active_school: Optional[School] = None,
    *,
    active_school_id: Optional[int] = None,
) -> bool:
    """Canonical manager detection via ``SchoolMembership``."""
    if not getattr(user, "is_authenticated", False):
        return False

    school_id = _resolved_school_id(active_school=active_school, active_school_id=active_school_id)
    if school_id:
        return _get_school_membership(
            user,
            active_school=active_school,
            active_school_id=school_id,
            role_types=[SchoolMembership.RoleType.MANAGER],
        ) is not None

    return bool(get_school_manager_school_ids(user))


# =========================
# الأدوار المدرسية الجديدة: الوكيل والموظف الإداري
# =========================
# كلاهما **عضوية مُنطَقة** لا عَلَم على الحساب. الفرق حاسم: المشروع سبق أن حمل
# دورين وسيطين على شكل أعلام عامة فتعذّر تقييد نطاقهما وانتهيا بالحذف عبر 180
# موضعاً (راجع docs/REMOVE_SUPERVISOR_ROLES.md). فالدرس مطبَّق هنا حرفياً:
# الدور يُسأل عنه دائماً *داخل مدرسة بعينها*.


def _has_school_role(
    user,
    role_type: str,
    *,
    active_school: Optional[School] = None,
    active_school_id: Optional[int] = None,
) -> bool:
    """هل يحمل المستخدم هذا الدور داخل المدرسة المقصودة؟"""
    if not getattr(user, "is_authenticated", False):
        return False

    school_id = _resolved_school_id(active_school=active_school, active_school_id=active_school_id)
    if not school_id:
        # بلا مدرسة لا معنى للسؤال. لا نتوسّع إلى «أي مدرسة» لأن ذلك يحوّل
        # الدور المُنطَق إلى عَلَم عام — وهو الخطأ الذي كلّف المشروع مرة.
        return False

    return _get_school_membership(
        user,
        active_school=active_school,
        active_school_id=school_id,
        role_types=[role_type],
    ) is not None


def is_school_deputy(
    user,
    active_school: Optional[School] = None,
    *,
    active_school_id: Optional[int] = None,
) -> bool:
    """وكيل المدرسة داخل المدرسة النشطة.

    الوكيل ليس مديراً مصغَّراً: هذه الدالة **لا تُستدعى من**
    ``is_school_manager``، ولا تمنح بذاتها أي صلاحية اعتماد نهائي. ما تمنحه
    نطاقُه يأتي لاحقاً من ``DeputyScope`` والتفويض، لا من مجرد حمل الدور.
    """
    return _has_school_role(
        user,
        SchoolMembership.RoleType.DEPUTY,
        active_school=active_school,
        active_school_id=active_school_id,
    )


def is_admin_staff(
    user,
    active_school: Optional[School] = None,
    *,
    active_school_id: Optional[int] = None,
) -> bool:
    """موظف إداري داخل المدرسة النشطة."""
    return _has_school_role(
        user,
        SchoolMembership.RoleType.ADMIN_STAFF,
        active_school=active_school,
        active_school_id=active_school_id,
    )


def is_lab_technician(
    user,
    active_school: Optional[School] = None,
    *,
    active_school_id: Optional[int] = None,
) -> bool:
    """محضّر المختبر في المدرسة النشطة.

    **يُحسم بالمسمّى الوظيفي لا بالدور**، وهو الموضع الوحيد في المشروع الذي
    يفعل ذلك — ولذلك تفسيرٌ: ``role_type`` وصفُ صلاحية، والمحضّر صلاحيتُه
    صلاحيةُ الموظف الإداري بنصّ التوصيف. أما ``job_title`` فهو **عملُه**: من
    يُسأل عن عهدة المختبر وتجاربه هو من عملُه المختبر، لا كل موظف إداري.

    والعضوية شرطٌ في الحالتين، فالمسمّى محفوظ على العضوية لا على الحساب — ومن
    كان محضّراً في مدرسة لا يصير محضّراً في أخرى بتبديل المدرسة النشطة.
    """
    if not getattr(user, "is_authenticated", False):
        return False

    school_id = _resolved_school_id(active_school=active_school, active_school_id=active_school_id)
    if not school_id:
        return False

    membership = _get_school_membership(
        user,
        active_school=active_school,
        active_school_id=school_id,
        role_types=[SchoolMembership.RoleType.ADMIN_STAFF],
    )
    if membership is None:
        # دَينٌ معروف: من أُضيف قبل توحيد بابَي الإسناد يحمل
        # ``job_title=lab_tech`` مع ``role_type=teacher``. فلا يُحجب عن مختبره
        # لأجل صفٍّ قديم — والمسمّى هو الفاصل هنا لا الدور.
        membership = _get_school_membership(
            user,
            active_school=active_school,
            active_school_id=school_id,
            role_types=[SchoolMembership.RoleType.TEACHER],
        )
    if membership is None:
        return False

    return (
        str(getattr(membership, "job_title", "") or "").strip()
        == SchoolMembership.JobTitle.LAB_TECH
    )


def can_record_lab(
    user,
    active_school: Optional[School] = None,
    *,
    active_school_id: Optional[int] = None,
) -> bool:
    """من يسجّل في المختبر: يضيف صنفاً، ويوثّق تجربة، ويقيّد حركة عهدة.

    المحضّر بحكم عمله، ومدير المدرسة بحكم دوره، ومالك النظام دائماً.
    **ولا يدخل هنا حاملُ ``manage_lab``**: تلك صلاحية *متابعة* — يقرأ ويراجع
    ويوصي. ومن يشرف على عمل غيره لا يكتبه نيابةً عنه، وإلا صار الجردُ الذي
    يُراجعه جردَ نفسه.
    """
    if not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    if is_school_manager(user, active_school=active_school, active_school_id=active_school_id):
        return True
    return is_lab_technician(
        user, active_school, active_school_id=active_school_id
    )


def can_view_lab(
    user,
    active_school: Optional[School] = None,
    *,
    active_school_id: Optional[int] = None,
) -> bool:
    """من يرى شاشات المختبر: من يسجّل فيه، ومن مُنح متابعته."""
    if can_record_lab(user, active_school, active_school_id=active_school_id):
        return True
    from .capabilities import MANAGE_LAB

    return capability_source(
        user, MANAGE_LAB, active_school, active_school_id=active_school_id
    ) is not None


def is_school_staff(
    user,
    active_school: Optional[School] = None,
    *,
    active_school_id: Optional[int] = None,
) -> bool:
    """منسوب في المدرسة النشطة — أي عضو فيها من غير مديرها."""
    if not getattr(user, "is_authenticated", False):
        return False

    school_id = _resolved_school_id(active_school=active_school, active_school_id=active_school_id)
    if not school_id:
        return False

    return _get_school_membership(
        user,
        active_school=active_school,
        active_school_id=school_id,
        role_types=list(SchoolMembership.STAFF_ROLES),
    ) is not None


def school_roles_for(
    user,
    active_school: Optional[School] = None,
    *,
    active_school_id: Optional[int] = None,
) -> Set[str]:
    """كل الأدوار التي يحملها المستخدم في المدرسة المقصودة.

    مجموعة لا قيمة واحدة، لأن الدور الواحد ليس القاعدة: الوكيل ذو النصاب
    التدريسي يحمل ``deputy`` و``teacher`` معاً، وأي دالة تفترض دوراً واحداً
    ستخفي أحدهما.
    """
    if not getattr(user, "is_authenticated", False):
        return set()

    school_id = _resolved_school_id(active_school=active_school, active_school_id=active_school_id)
    if not school_id:
        return set()

    return set(
        SchoolMembership.objects.filter(
            teacher=user,
            school_id=school_id,
            is_active=True,
        ).values_list("role_type", flat=True)
    )


# =========================
# الصلاحيات المُنطَقة والتفويض المؤقت
# =========================
# مصدران اثنان للصلاحية داخل المدرسة، ولا ثالث لهما:
#
#   1. **النطاق** (``StaffScope``) — صلاحية دائمة يمنحها المدير مع الدور.
#   2. **التفويض** (``Delegation``) — صلاحية مؤقتة تنتهي بذاتها زمنياً.
#
# والفرق بينهما ليس في القوة بل في **المسؤولية**: ما يُمارَس بالتفويض يُنسب
# للمفوِّض في سجل الإجراءات، وما يُمارَس بالنطاق يُنسب لصاحبه. ولذلك تُميّز
# ``capability_source`` بينهما بدل أن تُرجع نعم/لا مسطّحة.


def _staff_scope_for(user, school_id: int):
    """نطاق المستخدم في مدرسة، مخزَّن على الكائن لطلب واحد."""
    cache = getattr(user, "_staff_scope_cache", None)
    if not isinstance(cache, dict):
        cache = {}
        user._staff_scope_cache = cache
    if school_id in cache:
        return cache[school_id]

    from .models import StaffScope

    try:
        scope = (
            StaffScope.objects.filter(
                membership__teacher=user,
                membership__school_id=school_id,
                membership__is_active=True,
            )
            .select_related("membership")
            .first()
        )
    except Exception:
        scope = None

    cache[school_id] = scope
    return scope


def scope_capabilities(
    user,
    active_school: Optional[School] = None,
    *,
    active_school_id: Optional[int] = None,
) -> Set[str]:
    """الصلاحيات الدائمة الممنوحة بالنطاق."""
    if not getattr(user, "is_authenticated", False):
        return set()
    school_id = _resolved_school_id(active_school=active_school, active_school_id=active_school_id)
    if not school_id:
        return set()

    scope = _staff_scope_for(user, int(school_id))
    return set(scope.capability_codes()) if scope is not None else set()


def active_delegations(
    user,
    active_school: Optional[School] = None,
    *,
    active_school_id: Optional[int] = None,
) -> list:
    """التفويضات السارية الآن — محسوبة زمنياً لا مقروءة من عَلَم مخزَّن."""
    if not getattr(user, "is_authenticated", False):
        return []
    school_id = _resolved_school_id(active_school=active_school, active_school_id=active_school_id)
    if not school_id:
        return []

    from django.utils import timezone

    from .models import Delegation

    now = timezone.now()
    try:
        rows = list(
            Delegation.objects.filter(
                school_id=school_id,
                delegate=user,
                revoked_at__isnull=True,
                starts_at__lte=now,
                ends_at__gte=now,
            ).select_related("delegator")
        )
    except Exception:
        rows = []
    return rows


def delegated_capabilities(
    user,
    active_school: Optional[School] = None,
    *,
    active_school_id: Optional[int] = None,
) -> Set[str]:
    """الصلاحيات المؤقتة السارية الآن بالتفويض."""
    codes: Set[str] = set()
    for delegation in active_delegations(
        user, active_school, active_school_id=active_school_id
    ):
        codes |= delegation.capability_codes()
    return codes


def capability_source(
    user,
    capability: str,
    active_school: Optional[School] = None,
    *,
    active_school_id: Optional[int] = None,
) -> Optional[str]:
    """مصدر الصلاحية: ``"scope"`` أو ``"delegation"`` أو ``None``.

    النطاق يسبق التفويض في الفحص لأن الأصالة تسبق النيابة: من يملك الصلاحية
    أصلاً لا يُسجَّل عمله كأنه نيابة عن غيره.
    """
    if not getattr(user, "is_authenticated", False):
        return None
    code = str(capability or "").strip()
    if not code:
        return None

    if code in scope_capabilities(user, active_school, active_school_id=active_school_id):
        return "scope"
    if code in delegated_capabilities(user, active_school, active_school_id=active_school_id):
        return "delegation"
    return None


def has_capability(
    user,
    capability: str,
    active_school: Optional[School] = None,
    *,
    active_school_id: Optional[int] = None,
) -> bool:
    """هل يملك المستخدم هذه الصلاحية في المدرسة المقصودة؟

    مدير المدرسة يملك كل شيء داخل مدرسته بحكم دوره، فلا يُسأل عن نطاق.
    ومالك النظام يمر دائماً.
    """
    if not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    if is_school_manager(user, active_school=active_school, active_school_id=active_school_id):
        return True
    return capability_source(
        user, capability, active_school, active_school_id=active_school_id
    ) is not None


def supervised_department_ids(
    user,
    active_school: Optional[School] = None,
    *,
    active_school_id: Optional[int] = None,
) -> Set[int]:
    """الأقسام التي يشرف عليها المستخدم ضمن نطاقه.

    مجموعة فارغة تعني «لا أقسام مُسندة» لا «كل الأقسام». التأويل المعاكس يحوّل
    نطاقاً لم يُضبط بعد إلى صلاحية على المدرسة كلها.
    """
    if not getattr(user, "is_authenticated", False):
        return set()
    school_id = _resolved_school_id(active_school=active_school, active_school_id=active_school_id)
    if not school_id:
        return set()

    scope = _staff_scope_for(user, int(school_id))
    if scope is None:
        return set()
    try:
        return {int(pk) for pk in scope.departments.values_list("id", flat=True)}
    except Exception:
        return set()


def capability_required(capability: str):
    """حصر الوصول على من يملك صلاحية بعينها في المدرسة النشطة."""

    def decorator(view_func):
        @wraps(view_func)
        def _wrapped(request: HttpRequest, *args, **kwargs) -> HttpResponse:
            user = request.user
            if not getattr(user, "is_authenticated", False):
                return redirect("reports:login")

            try:
                school_id = int(request.session.get("active_school_id") or 0) or None
            except Exception:
                school_id = None

            if not school_id and not getattr(user, "is_superuser", False):
                messages.error(request, "فضلاً اختر مدرسة أولاً.")
                return redirect("reports:select_school")

            if has_capability(user, capability, active_school_id=school_id):
                return view_func(request, *args, **kwargs)

            messages.error(request, "لا تملك صلاحية الوصول إلى هذه الصفحة.")
            return redirect("reports:home")

        return _wrapped

    return decorator


# =========================
# المدير التنفيذي لمجموعة المدارس المتكاملة
# =========================
# التنظيم الذي تنفّذه هذه الدوال: المدير التنفيذي يقود مجموعة مدارس إشرافاً
# ومتابعةً، ولا يتولى الإدارة اليومية لأي مدرسة، ولا ينتقص من صلاحيات مديرها.
#
# ولذلك لا تمنح أيٌّ من هذه الدوال صلاحية كتابة، ولا تُستدعى من ``is_school_manager``
# ولا من دوال ``can_edit_report`` وأخواتها. كون المدير التنفيذي لا يجتاز فحص
# الإدارة المدرسية ليس إغفالاً بل هو الشرط الذي يحفظ التنظيم، ويحرسه اختبار صريح.


def get_executive_director_group_ids(user) -> Set[int]:
    """Group ids where the user is an active executive director."""
    if not getattr(user, "is_authenticated", False):
        return set()

    cache = getattr(user, "_executive_director_group_ids_cache", None)
    if cache is not None:
        return set(cache)

    try:
        ids = {
            int(x)
            for x in SchoolGroupMembership.objects.filter(
                user=user,
                is_active=True,
                role_type=SchoolGroupMembership.RoleType.EXECUTIVE_DIRECTOR,
                group__is_active=True,
            ).values_list("group_id", flat=True)
            if x
        }
    except Exception:
        ids = set()

    user._executive_director_group_ids_cache = tuple(sorted(ids))
    return ids


def is_executive_director(user, group: Optional[SchoolGroup] = None, *, group_id: Optional[int] = None) -> bool:
    """Canonical executive-director detection via ``SchoolGroupMembership``.

    Scoped to a group when one is given; otherwise any active directorship counts.
    """
    if not getattr(user, "is_authenticated", False):
        return False

    resolved = group_id
    if resolved is None and group is not None:
        resolved = getattr(group, "pk", None)

    ids = get_executive_director_group_ids(user)
    if resolved:
        return int(resolved) in ids
    return bool(ids)


def executive_director_groups(user) -> QuerySet[SchoolGroup]:
    """The active groups this user leads."""
    ids = get_executive_director_group_ids(user)
    if not ids:
        return SchoolGroup.objects.none()
    return SchoolGroup.objects.filter(id__in=ids, is_active=True).order_by("name")


def executive_director_schools_qs(user) -> QuerySet[School]:
    """Schools visible to an executive director — their groups' schools only.

    Deliberately narrow: a user with no directorship gets an empty queryset rather
    than a broad one, so a missing membership can never widen access.
    """
    ids = get_executive_director_group_ids(user)
    if not ids:
        return School.objects.none()
    return School.objects.filter(group_id__in=ids, is_active=True).order_by("name")


def can_send_group_notification(user, school_ids: Iterable[int]) -> bool:
    """هل يجوز لهذا المستخدم تعميمٌ على هذه المدارس بالضبط؟

    الشرط أن تكون **كل** مدرسة مطلوبة ضمن مدارس مجموعاته. ولا يُقبل تقاطع جزئي:
    طلبٌ فيه مدرسة واحدة خارج النطاق يُرفض كاملاً بدل أن يُنفَّذ منقوصاً، فتنفيذُ
    بعضِه يُوهم المرسِل أن التعميم وصل حيث لم يصل.
    """
    requested = {int(x) for x in school_ids if x}
    if not requested:
        return False
    if not is_executive_director(user):
        return False

    allowed = set(executive_director_schools_qs(user).values_list("id", flat=True))
    return requested.issubset(allowed)


def can_view_group_batch(user, batch) -> bool:
    """تقرير الدفعة لمرسِلها وحده، وما دام مديراً تنفيذياً لمجموعتها.

    ربط الصلاحية بالمنصب لا بالإرسال وحده مقصود: من يترك موقعه يفقد الوصول إلى
    تقارير مدارسَ لم تعد تحت إشرافه.
    """
    if batch is None or not getattr(user, "is_authenticated", False):
        return False
    if getattr(batch, "sender_id", None) != getattr(user, "id", None):
        return False
    return is_executive_director(user, group_id=getattr(batch, "group_id", None))


def effective_user_role_label(
    user,
    active_school: Optional[School] = None,
    *,
    active_school_id: Optional[int] = None,
) -> str:
    """Single source of truth for the role label shown in the UI."""
    if user is None:
        return "مستخدم"

    school_id = _resolved_school_id(active_school=active_school, active_school_id=active_school_id)
    school = active_school
    if school is None and school_id:
        try:
            school = School.objects.filter(pk=school_id, is_active=True).only("id", "gender").first()
        except Exception:
            school = None

    cache = getattr(user, "_effective_role_label_cache", None)
    if not isinstance(cache, dict):
        cache = {}
        user._effective_role_label_cache = cache

    cache_key = int(school_id or 0)
    if cache_key in cache:
        return str(cache[cache_key] or "مستخدم")

    labels = _school_role_labels(school)
    label = "مستخدم"
    try:
        if getattr(user, "is_superuser", False):
            label = "مدير النظام"
        elif is_school_manager(
            user,
            active_school=school,
            active_school_id=school_id,
        ):
            label = labels["manager"]
        elif is_executive_director(user):
            # يُسأل عنه قبل ``is_staff`` وقبل الأدوار المدرسية: المدير التنفيذي
            # لا يملك عضوية في أي مدرسة بحكم تصميمه، فكانت ترويسته تناديه
            # «مستخدم» — وهو أدنى ما يُقال لمن يقود مجموعة مدارس.
            #
            # ومسمّاه لا يُشتق من جنس مدرسة: مجموعته قد تجمع بنين وبنات، ولا
            # مدرسة نشطة له أصلاً — فتسميته ثابتة عن قصد، خلافاً لبقية
            # المسمّيات التي تقرأ من ``school_gender_labels``.
            label = "مدير تنفيذي"
        elif getattr(user, "is_staff", False):
            label = "مدير النظام"
        elif is_school_deputy(user, active_school=school, active_school_id=school_id):
            # الوكيل أعلى من المنسوب وأدنى من المدير، فتسبق تسميتُه أي مسمّى
            # وظيفي آخر يحمله — ووكيلٌ له نصاب تدريسي يظل وكيلاً في الترويسة.
            label = labels["deputy"]
        else:
            # عضوية الموظف الإداري تُطلب أولاً: من يجمع بين وظيفته ونصاب تدريسي
            # يحمل عضويتين، وأيّهما تعود من استعلام «المنسوبين» رهنٌ بترتيب
            # المعرّفات — فكان المحضّر يظهر «معلّماً» لأن عضويته التدريسية أسبق.
            staff_membership = _get_school_membership(
                user,
                active_school=school,
                active_school_id=school_id,
                role_types=[SchoolMembership.RoleType.ADMIN_STAFF],
            ) or _get_school_membership(
                user,
                active_school=school,
                active_school_id=school_id,
                role_types=list(SchoolMembership.STAFF_ROLES),
            )
            if staff_membership is not None:
                job_title = (getattr(staff_membership, "job_title", "") or "").strip()
                role_type = (getattr(staff_membership, "role_type", "") or "").strip()
                # المسمّى يسبق الدور: ``admin_staff`` وصف صلاحية، و«محضر
                # المختبر» اسم عمله — والترويسة تُظهر ما يعرف به صاحبها.
                if job_title == SchoolMembership.JobTitle.LAB_TECH:
                    label = labels["lab_tech"]
                elif role_type == SchoolMembership.RoleType.ADMIN_STAFF:
                    label = labels["admin_staff"]
                elif job_title == SchoolMembership.JobTitle.ADMIN_STAFF:
                    label = labels["admin_staff"]
                else:
                    label = labels["teacher"]
            else:
                label = "مستخدم"
    except Exception:
        label = "مستخدم"

    cache[cache_key] = label
    return label


# ==============================
# اكتشاف “مسؤول قسم” (متعدد الأقسام)
# ==============================
def get_officer_departments(user, *, active_school: Optional[School] = None) -> List[Department]:
    """
        يعيد قائمة الأقسام التي المستخدم مسؤول عنها عبر DepartmentMembership.role_type = OFFICER.

        ملاحظة: لا نعتمد على user.role/Role.slug لأن الأقسام الآن مخصصة لكل مدرسة ويمكن أن تتكرر slugs.
    تُعاد قائمة بدون تكرار ومحافظة على الترتيب.
    """
    if not getattr(user, "is_authenticated", False):
        return []

    seen = set()
    results: List[Department] = []

    # عبر العضويات
    if DepartmentMembership is not None:
        try:
            memb_qs = (
                DepartmentMembership.objects.select_related("department")
                .filter(teacher=user, role_type=getattr(DepartmentMembership, "OFFICER", "officer"),
                        department__is_active=True)
            )
            if active_school is not None:
                memb_qs = memb_qs.filter(department__school=active_school)
            for m in memb_qs:
                d = m.department
                if d and d.pk not in seen:
                    seen.add(d.pk)
                    results.append(d)
        except Exception:
            # قائمةٌ فارغة هنا تعني «لا يرأس قسماً» — فيفقد رئيسُ القسم
            # صلاحيات قسمه كاملةً. لا يجوز أن يقع ذلك بلا أثر.
            _degraded("perm.officer_departments", user_id=getattr(user, "pk", None))

    return results


def get_officer_department(user) -> Optional[Department]:
    """توافق خلفي: أول قسم من get_officer_departments أو None."""
    depts = get_officer_departments(user)
    return depts[0] if depts else None


def get_member_departments(user, *, active_school: Optional[School] = None) -> List[Department]:
    """
        يعيد قائمة الأقسام التي المستخدم عضو فيها (TEACHER) عبر DepartmentMembership.
        
        هؤلاء الأعضاء يحصلون على صلاحيات قراءة فقط (عرض + طباعة) لتقارير قسمهم،
        دون صلاحيات المشاركة أو الحذف (على عكس رؤساء الأقسام OFFICER).
    """
    if not getattr(user, "is_authenticated", False):
        return []

    seen = set()
    results: List[Department] = []

    if DepartmentMembership is not None:
        try:
            memb_qs = (
                DepartmentMembership.objects.select_related("department")
                .filter(teacher=user, role_type=getattr(DepartmentMembership, "TEACHER", "teacher"),
                        department__is_active=True)
            )
            if active_school is not None:
                memb_qs = memb_qs.filter(department__school=active_school)
            for m in memb_qs:
                d = m.department
                if d and d.pk not in seen:
                    seen.add(d.pk)
                    results.append(d)
        except Exception:
            _degraded("perm.member_departments", user_id=getattr(user, "pk", None))

    return results


def is_officer(user) -> bool:
    """هل المستخدم مسؤول قسم؟ (عضويات أو مطابقة الدور)"""
    return bool(get_officer_departments(user))


def is_department_member(user, *, active_school: Optional[School] = None) -> bool:
    """هل المستخدم عضو في أي قسم (TEACHER)؟"""
    return bool(get_member_departments(user, active_school=active_school))


def _scope_school_id(*, active_school: Optional[School] = None, report_school: Optional[School] = None) -> Optional[int]:
    return _coerce_school_id(
        getattr(active_school, "id", None) if active_school is not None else None
    ) or _coerce_school_id(
        getattr(report_school, "id", None) if report_school is not None else None
    )


def _build_report_permission_scope(user, *, school_id: Optional[int]) -> dict:
    """Build a lightweight permission scope once per user/school context.

    This avoids repeating manager/officer DB lookups for every report row.
    """
    scope = {
        "is_authenticated": bool(getattr(user, "is_authenticated", False)),
        "is_superuser": bool(getattr(user, "is_superuser", False)),
        "manager_school_ids": set(),
        "officer_reporttype_ids": set(),
    }

    if not scope["is_authenticated"] or scope["is_superuser"]:
        return scope

    manager_ids = get_school_manager_school_ids(user)
    if school_id is not None:
        manager_ids = {sid for sid in manager_ids if sid == school_id}
    scope["manager_school_ids"] = set(manager_ids)

    try:
        if DepartmentMembership is None:
            return scope
        officer_value = getattr(DepartmentMembership, "OFFICER", "officer")
        depts_qs = Department.objects.filter(
            is_active=True,
            memberships__teacher=user,
            memberships__role_type=officer_value,
        )
        if school_id is not None:
            depts_qs = depts_qs.filter(school_id=school_id)
        rt_ids = set(depts_qs.values_list("reporttypes__id", flat=True))
        scope["officer_reporttype_ids"] = {int(x) for x in rt_ids if x}
    except Exception:
        scope["officer_reporttype_ids"] = set()

    return scope


def _get_report_permission_scope(user, *, active_school: Optional[School] = None, report_school: Optional[School] = None) -> dict:
    sid = _scope_school_id(active_school=active_school, report_school=report_school)
    cache_obj = getattr(user, "_report_perm_scope_cache", None)
    if not isinstance(cache_obj, dict):
        cache_obj = {}
        user._report_perm_scope_cache = cache_obj

    key = sid if sid is not None else "all"
    if key not in cache_obj:
        cache_obj[key] = _build_report_permission_scope(user, school_id=sid)
    return cache_obj[key]


def _can_lift_report_seal(user, report, *, active_school: Optional[School] = None) -> bool:
    """هل يملك هذا المستخدم فكّ ختم الاعتماد عن هذا التقرير؟

    **المعتمِد وحده.** قيدُ الحالة يحمي الختم، فمن يملك الختم لا يقيّده —
    وإلا مُنع المدير من تقريره هو بعد أن اعتمده، بينما يتصرّف في تقارير من
    دونه المعتمَدة بلا مانع. أما رئيسُ القسم فيراجع ولا يعتمد في المسارات
    التي ينتهي فيها القرار إلى المدير، فتعديلُه تقريرَه المعتمَد تغييرٌ لما
    ختمه **غيرُه** — وهي الثغرة نفسها لا استثناءً منها.
    """
    if not getattr(user, "is_authenticated", False):
        return False

    report_school = getattr(report, "school", None)
    scope = _get_report_permission_scope(user, active_school=active_school, report_school=report_school)
    if scope.get("is_superuser"):
        return True
    return getattr(report, "school_id", None) in scope.get("manager_school_ids", set())


def _has_authority_over_report(user, report, *, active_school: Optional[School] = None) -> bool:
    """سلطةٌ على التقرير من غير طريق ملكيته: سوبر، أو مديرُ مدرسته، أو رئيسُ قسمه."""
    if _can_lift_report_seal(user, report, active_school=active_school):
        return True
    report_school = getattr(report, "school", None)
    scope = _get_report_permission_scope(user, active_school=active_school, report_school=report_school)
    return getattr(report, "category_id", None) in scope.get("officer_reporttype_ids", set())


def _can_manage_report(user, report, *, active_school: Optional[School] = None) -> bool:
    if not getattr(user, "is_authenticated", False):
        return False

    if getattr(report, "teacher_id", None) == getattr(user, "id", None):
        return True

    return _has_authority_over_report(user, report, active_school=active_school)


def _owner_locked_by_approval(user, report, *, active_school: Optional[School] = None) -> bool:
    """هل خرج التقرير من يد صاحبه لأن دورة الاعتماد بلغت به مبلغاً؟

    **على مُعِدّه وحده.** ما أرسله لا يُسحب من تحت مراجعه، وما اعتُمد لا
    يُبدَّل بعد ختمه — واعتمادٌ يُعدَّل بعده النصُّ ليس اعتماداً. وحالات
    الملكية (مسودة، مُعاد، بانتظار استكمال) تبقى مفتوحة، وهي كل ما يحتاجه
    للتصحيح. ومن يملك فكّ الختم لا يقيّده الختم — راجع ``_can_lift_report_seal``
    لحدّ هذا الاستثناء ولماذا لا يبلغ رئيس القسم.

    **ومدرسةٌ لم تفعّل الدورة لا يمسّها هذا.** تقاريرها تُحفظ ``approved``
    فوراً بلا مراجعٍ ولا ختم، فقيدُها به يجمّد كل تقرير في المنصة لحظة
    حفظه — وهو عكس المقصود تماماً.
    """
    if getattr(report, "teacher_id", None) != getattr(user, "id", None):
        return False

    if not getattr(getattr(report, "school", None), "report_approval_enabled", False):
        return False

    # ``is_editable_by_owner`` من ``ApprovalMixin``. غيابه يعني سجلاً خارج
    # الدورة، ولا يُقيَّد بها.
    if getattr(report, "is_editable_by_owner", None) is not False:
        return False

    return not _can_lift_report_seal(user, report, active_school=active_school)


def can_delete_report(user, report, *, active_school: Optional[School] = None) -> bool:
    """
    يحدد هل المستخدم يستطيع حذف التقرير.

    الصلاحيات:
    - السوبر: نعم
    - مدير المدرسة: نعم
    - رئيس القسم (OFFICER): نعم (للتقارير المرتبطة بقسمه)
    - عضو القسم (TEACHER): لا (عرض فقط)
    - صاحب التقرير: نعم — ما دام التقرير في يده (راجع ``_owner_locked_by_approval``)
    """
    if not _can_manage_report(user, report, active_school=active_school):
        return False
    return not _owner_locked_by_approval(user, report, active_school=active_school)


def can_share_report(user, report, *, active_school: Optional[School] = None) -> bool:
    """
    يحدد هل المستخدم يستطيع مشاركة التقرير (إنشاء رابط عام).
    
    الصلاحيات:
    - السوبر: نعم
    - مدير المدرسة: نعم
    - رئيس القسم (OFFICER): نعم (للتقارير المرتبطة بقسمه)
    - عضو القسم (TEACHER): لا (عرض فقط)
    - صاحب التقرير: نعم
    """
    return _can_manage_report(user, report, active_school=active_school)


def can_edit_report(user, report, *, active_school: Optional[School] = None) -> bool:
    """
    يحدد هل المستخدم يستطيع تعديل التقرير.

    الصلاحيات:
    - السوبر: نعم
    - مدير المدرسة: نعم
    - رئيس القسم (OFFICER): نعم (للتقارير المرتبطة بقسمه)
    - عضو القسم (TEACHER): لا (عرض فقط)
    - صاحب التقرير: نعم — ما دام التقرير في يده (راجع ``_owner_locked_by_approval``)
    """
    if not _can_manage_report(user, report, active_school=active_school):
        return False
    return not _owner_locked_by_approval(user, report, active_school=active_school)


# ==============================
# ديكوريتر حصر الوصول حسب الدور (بالـ slug)
# ==============================
def role_required(allowed_roles: Iterable[str]):
    """حصر الوصول على أدوار مدرسية محددة.

    مثال:
        @login_required(login_url="reports:login")
        @role_required({"manager", "deputy"})
        def some_view(...): ...

    قواعد ثابتة:
    - مالك النظام يمر دائماً.
    - كل دور يُحسم عبر ``SchoolMembership`` **داخل المدرسة النشطة** — لا عبر
      عَلَم على الحساب. فمن كان وكيلاً في مدرسة لا يصير وكيلاً في أخرى بتبديل
      المدرسة النشطة.
    - أي اسم دور غير معروف يُرفَض. النسخة السابقة كانت تعرف ``manager`` وحده
      وتتجاهل ما عداه **بصمت**: ``@role_required({"deputy"})`` كان يمنع الجميع
      بلا سبب ظاهر، وهو أسوأ من الخطأ الصريح لأنه يبدو كأنه يعمل.
    """
    allowed = {str(role).strip().lower() for role in (allowed_roles or []) if str(role).strip()}

    # خريطة الأدوار المدعومة → الدالة التي تحسمها.
    _CHECKS = {
        "manager": is_school_manager,
        "deputy": is_school_deputy,
        "admin_staff": is_admin_staff,
        "staff": is_school_staff,
    }

    unknown = allowed - set(_CHECKS)
    if unknown:
        raise ValueError(
            f"role_required: أدوار غير معروفة {sorted(unknown)}. "
            f"المدعوم: {sorted(_CHECKS)}."
        )

    def _has_active_schools() -> bool:
        try:
            return School.objects.filter(is_active=True).exists()
        except Exception:
            return False

    def _get_active_school_id(request: HttpRequest) -> int | None:
        try:
            sid = request.session.get("active_school_id")
            return int(sid) if sid else None
        except Exception:
            return None

    def _holds_any_role_somewhere(user) -> bool:
        """هل يحمل المستخدم أحد الأدوار المطلوبة في أي مدرسة؟

        يُستعمل وحده لتقرير **وجهة إعادة التوجيه** حين لا توجد مدرسة نشطة:
        من يحمل الدور يُرسَل لاختيار مدرسة، ومن لا يحمله يُرسَل للرئيسية بدل
        أن يدور في صفحة اختيار لا تنفعه. وهو لا يمنح وصولاً بذاته.
        """
        if "manager" in allowed and is_school_manager(user):
            return True
        return SchoolMembership.objects.filter(
            teacher=user,
            is_active=True,
            role_type__in=[role for role in allowed if role != "staff"]
            or list(SchoolMembership.STAFF_ROLES),
        ).exists()

    def decorator(view_func):
        @wraps(view_func)
        def _wrapped(request: HttpRequest, *args, **kwargs) -> HttpResponse:
            user = request.user
            if not getattr(user, "is_authenticated", False):
                return redirect("reports:login")

            # السوبر دومًا مسموح
            if getattr(user, "is_superuser", False):
                return view_func(request, *args, **kwargs)

            active_school_id = _get_active_school_id(request)

            # النظام متعدد المدارس: الصلاحية المدرسية بلا مدرسة نشطة لا تُحسم.
            if _has_active_schools() and not active_school_id:
                if _holds_any_role_somewhere(user):
                    messages.error(request, "فضلاً اختر مدرسة أولاً.")
                    return redirect("reports:select_school")
                messages.error(request, "لا تملك صلاحية الوصول إلى هذه الصفحة.")
                return redirect("reports:home")

            for role in allowed:
                if _CHECKS[role](user, active_school_id=active_school_id):
                    return view_func(request, *args, **kwargs)

            messages.error(request, "لا تملك صلاحية الوصول إلى هذه الصفحة.")
            return redirect("reports:home")

        return _wrapped

    return decorator


# ==============================
# صلاحيات أنواع التقارير (بالاعتماد على الدور + أقسام المسؤول)
# ==============================
def allowed_categories_for(user, active_school: Optional[School] = None) -> Set[str]:
    """
        يعيد مجموعة أكواد ReportType المسموحة للمستخدم داخل مدرسة محددة:
            - {"all"} للسوبر.
            - مدير المدرسة (SchoolMembership.role_type=manager) داخل active_school: {"all"}.
            - رئيس قسم (OFFICER): أكواد reporttypes للأقسام التي هو مسؤول عنها.
            - عضو قسم (TEACHER): أكواد reporttypes للأقسام التي هو عضو فيها.
            - من مُنح ``review_reports``: أكواد reporttypes لأقسام **نطاقه**.

        ملاحظة: لا نستخدم Role.allowed_reporttypes هنا لأن Role عالمي وقد يخلط بين المدارس.

        **ولماذا يدخل النطاق هنا؟** لأن هذه الدالة هي ما تقرأ منه
        ``restrict_queryset_for_user``، وهي بدورها أساسُ كشف تقارير المدرسة.
        وبلا النطاق كان الوكيل الذي مُنح المراجعة يُحصَر في **تقاريره هو** —
        فيفتح كشف المدرسة فيراه فارغاً، والصلاحية ممنوحة والشاشة مفتوحة.

        وموضع الإصلاح هنا لا في العرض: العرض الواحد يُصلَح مرة، وهذه الدالة
        تُقرأ من كل موضع يسأل «ما يحق له الاطلاع عليه من أنواع التقارير».
    """
    try:
        # سوبر: يرى الكل (لكن عزل المدارس يُطبّق في الـ views)
        if getattr(user, "is_superuser", False):
            return {"all"}
        if active_school is not None and SchoolMembership is not None:
            if is_school_manager(user, active_school=active_school):
                return {"all"}

        if active_school is None:
            # بدون مدرسة نشطة لا نسمح بتوسيع الوصول
            return set()

        allowed_codes: Set[str] = set()

        # ✅ أقسام النطاق لمن مُنح مراجعة التقارير
        try:
            from .capabilities import REVIEW_REPORTS

            if capability_source(user, REVIEW_REPORTS, active_school) is not None:
                supervised = supervised_department_ids(user, active_school)
                if supervised:
                    allowed_codes |= {
                        code
                        for code in Department.objects.filter(
                            id__in=supervised
                        ).values_list("reporttypes__code", flat=True)
                        if code
                    }
        except Exception:
            _degraded("perm.allowed_codes_supervised", user_id=getattr(user, "pk", None))

        # ✅ رؤساء الأقسام (OFFICER)
        try:
            officer_depts = get_officer_departments(user, active_school=active_school)
            for d in officer_depts:
                allowed_codes |= set(c for c in d.reporttypes.values_list("code", flat=True) if c)
        except Exception:
            _degraded("perm.allowed_codes_officer", user_id=getattr(user, "pk", None))

        # ✅ أعضاء الأقسام (TEACHER) - عرض فقط
        try:
            member_depts = get_member_departments(user, active_school=active_school)
            for d in member_depts:
                allowed_codes |= set(c for c in d.reporttypes.values_list("code", flat=True) if c)
        except Exception:
            _degraded("perm.allowed_codes_member", user_id=getattr(user, "pk", None))

        return allowed_codes
    except Exception:
        # مجموعةٌ فارغة = «لا يرى أي تصنيف». آمنٌ بحقّ، ومُعطِّلٌ تماماً — فيجب
        # أن يصل التنبيه لا أن يُقرأ كأنه نتيجة صحيحة.
        _degraded("perm.allowed_categories", user_id=getattr(user, "pk", None))
        return set()


# ==============================
# تقييد QuerySet بحسب المستخدم
# ==============================
def _scope_to_school(qs: QuerySet[Any], active_school: Optional[School]) -> QuerySet[Any]:
    """حصر الاستعلام على المدرسة النشطة متى كان للموديل حقل ``school``."""
    try:
        if "school" in {f.name for f in qs.model._meta.get_fields()}:
            if active_school is None:
                return qs.none()
            return qs.filter(school=active_school)
    except Exception:
        return qs.none()
    return qs


def restrict_queryset_for_user(qs: QuerySet[Any], user, active_school: Optional[School] = None) -> QuerySet[Any]:
    """
    يقيّد QuerySet للتقارير بحسب صلاحيات المستخدم:
      - السوبر/المدير/الدور الذي يرى الكل: يرى الجميع.
      - غير ذلك: يرى تقاريره + أي تقرير يقع ضمن الأنواع المسموح بها له (من الدور/الأقسام).

    **حدّ المدرسة يُفرض هنا لا عند المستدعي.** الفلترة بـ ``category__code``
    أدناه ليست معزولة بذاتها: أكواد أنواع التقارير مخصَّصة لكل مدرسة **وقد
    تتكرر** بينها (وهو ما توثّقه ``allowed_categories_for`` صراحةً)، فقسمٌ اسمه
    ``activities`` في مدرستين يعطي الكود نفسه. وكانت سلامةُ الدالة قائمة على أن
    كل مستدعٍ يُتبعها بـ ``filter_by_school`` — عُرفٌ صحيحٌ اليوم في كل المواضع،
    لكن لا شيء يفرضه على الموضع القادم. فطيُّ الفلتر إلى الداخل يجعل الدالة
    آمنة وحدها، ولا يغيّر نتيجة أي مستدعٍ حالي لأن الفلتر نفسه يُطبَّق مرتين.
    """
    # مالك النظام وحده يستطيع العمل بلا مدرسة نشطة في الشاشات المنصية.
    # متى اختار مدرسة يبقى حدّها مطبقاً مثل بقية المستخدمين.
    if getattr(user, "is_superuser", False):
        return _scope_to_school(qs, active_school) if active_school is not None else qs

    qs = _scope_to_school(qs, active_school)

    # ✅ مدير المدرسة داخل active_school يرى كل تقارير مدرسته
    if active_school is not None and SchoolMembership is not None:
        try:
            if SchoolMembership.objects.filter(
                teacher=user,
                school=active_school,
                role_type__in=[
                    SchoolMembership.RoleType.MANAGER,
                ],
                is_active=True,
            ).exists():
                return qs
        except Exception:
            _degraded(
                "perm.restrict_queryset_manager_check",
                user_id=getattr(user, "pk", None),
                school_id=getattr(active_school, "pk", None),
            )

    allowed_codes = allowed_categories_for(user, active_school)
    if "all" in allowed_codes:
        return qs

    conditions = Q(teacher=user)  # دائمًا يرى تقاريره
    if allowed_codes:
        conditions |= Q(category__code__in=list(allowed_codes))

    return qs.filter(conditions).distinct()
