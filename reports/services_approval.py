# -*- coding: utf-8 -*-
"""آلة حالة الاعتماد — القواعد التي لا يجوز أن تعيش في الواجهة.

كل انتقال يمر من هنا، ومن هنا وحده. السبب ليس أناقة التنظيم: الشاشة تُتجاوَز
بطلب مُصاغ يدوياً، والتحقق الذي يعيش في القالب أو في العرض يُنسى عند إضافة
مسار ثانٍ للشيء نفسه. فوضع القاعدة في نقطة واحدة يجعل تجاوزها يتطلب تجاوز
الدالة كلها لا تفويت شرط.

ثلاث قواعد مفروضة هنا لا تُستثنى:

1. **لا يعتمد أحد عمله.** ``decided_by != owner`` دائماً. وهي القاعدة التي
   يكرّرها توصيف الأدوار في أربعة أدوار، وكانت مخروقة فعلياً في ملف الأداء
   القيادي قبل هذه المرحلة.
2. **المعتمد لا يُعدَّل ولا يُحذف.** اعتمادٌ يمكن محوه ليس اعتماداً.
3. **الوكيل يوصي ولا يعتمد** — إلا في مسار ``DEPUTY_FINAL`` الذي يختاره المدير
   صراحةً لنوع تقرير بعينه.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone

from . import capabilities as caps
from .approval_errors import ApprovalError
from .model_parts.approvals import (
    FINAL_STATES,
    ApprovalRoute,
    ApprovalState,
    ApprovalTransition,
)
from .permissions import (
    capability_source,
    is_school_manager,
    supervised_department_ids,
)

__all__ = [
    "ApprovalError",
    "available_actions",
    "submit",
    "issue",
    "start_review",
    "request_info",
    "return_for_changes",
    "recommend",
    "approve",
    "withdraw",
    "transitions_for",
    "route_for",
]


# ─────────────────────────────────────────────────────────────────────────────
# سياق المنفّذ
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Actor:
    """المنفّذ وصفته — بالأصالة أم بالنيابة."""

    user: object
    school: object
    acted_as: str
    on_behalf_of: object | None


def _actor_for(user, school, capability: str) -> Actor:
    """يحدّد صفة التنفيذ من مصدر الصلاحية.

    مدير المدرسة يعمل بالأصالة دائماً — فهو صاحب الصلاحية لا نائب عنها.
    """
    if is_school_manager(user, active_school=school):
        return Actor(user, school, ApprovalTransition.ActedAs.SELF, None)

    source = capability_source(user, capability, school)
    if source == "delegation":
        from .permissions import active_delegations

        delegations = [
            item
            for item in active_delegations(user, school)
            if capability in item.capability_codes()
        ]
        principal = delegations[0].delegator if delegations else None
        return Actor(user, school, ApprovalTransition.ActedAs.DELEGATE, principal)

    return Actor(user, school, ApprovalTransition.ActedAs.SELF, None)


def _owner_id(obj) -> int | None:
    """صاحب العمل — من يُرسل ويُعاد إليه، ولا يعتمد عمله بنفسه.

    ``teacher`` في التقارير، و``assignee`` في التكليفات، و``recorder`` في محاضر
    الاجتماعات، و``manager`` في ملف الأداء القيادي، و``owner`` فيما عداها.
    الترتيب مقصود: الكيان الذي يحمل أكثر من حقل يُقرأ صاحبه من الأخص — فوثيقة
    تحمل ``owner`` و``teacher`` معاً صاحبها الأول لا الثاني.
    """
    for field in ("assignee_id", "recorder_id", "manager_id", "owner_id", "teacher_id"):
        value = getattr(obj, field, None)
        if value:
            return value
    return None


def _can_finalize(user, obj, school) -> bool:
    """من يملك الاعتماد النهائي على هذا السجل.

    مدير المدرسة أصلاً، ثم مسار ``DEPUTY_FINAL`` الذي يفوّضه صراحةً، ثم ما
    يقرّره الكيان بنفسه عبر ``can_finalize_approval`` — كالتكليف الذي يعتمد
    تنفيذَه مَن أصدره.
    """
    if is_school_manager(user, active_school=school):
        return True

    hook = getattr(obj, "can_finalize_approval", None)
    if callable(hook):
        verdict = hook(user, school)
        if verdict is not None:
            return bool(verdict)

    return (
        route_for(obj) == ApprovalRoute.DEPUTY_FINAL
        and capability_source(user, caps.RECOMMEND_APPROVAL, school) is not None
        and _can_review(user, obj, school)
    )


def route_for(obj) -> str:
    """مسار الاعتماد الخاص بهذا السجل.

    يُقرأ من نوع التقرير إن وُجد. غياب النوع لا يعطّل الاعتماد: المسار المباشر
    هو الافتراض الآمن — يصل العمل إلى المدير دائماً، ولا يعلق منتظراً وكيلاً
    قد لا يكون معيَّناً أصلاً.
    """
    category = getattr(obj, "category", None)
    value = (getattr(category, "approval_route", "") or "").strip()
    return value or ApprovalRoute.DIRECT


def _can_review(user, obj, school) -> bool:
    """هل يملك هذا المستخدم مراجعة هذا السجل؟

    المدير يراجع كل شيء في مدرسته. وغيره يحتاج صلاحية ``review_reports``
    **وأن يقع السجل في نطاقه**: وكيلٌ بلا أقسام مُسندة لا يراجع شيئاً — وهو
    السلوك المقصود، فالنطاق الفارغ يعني «لا أقسام» لا «كل الأقسام».

    الكيان يستطيع تجاوز هذه القاعدة عبر ``can_review_approval`` حين تحكم
    مراجعتَه علاقةٌ أخرى — كالتكليف الذي يراجعه من أصدره لا من يشرف على قسمه.
    وإرجاع ``None`` من ذلك الخطّاف يعني «طبّق القاعدة العامة»، فلا يُجبَر كل
    كيان على إعادة تعريف ما لا يخالف فيه.
    """
    if is_school_manager(user, active_school=school):
        return True

    # الخطّاف يُسأل **قبل** بوابة الصلاحية المدرسية لا بعدها: بعضُ المراجعين
    # يستمدّون حقّهم من علاقتهم بالسجل لا من عضوية في المدرسة — كالمدير
    # التنفيذي الذي يتابع ما أصدره ولا يملك عضوية في أي مدرسة أصلاً. ويقع على
    # الخطّاف نفسه أن يشترط الصلاحية لمن لا علاقة تخصّه.
    hook = getattr(obj, "can_review_approval", None)
    if callable(hook):
        verdict = hook(user, school)
        if verdict is not None:
            return bool(verdict)

    if capability_source(user, caps.REVIEW_REPORTS, school) is None:
        return False

    supervised = supervised_department_ids(user, school)
    if not supervised:
        return False

    category = getattr(obj, "category", None)
    if category is None:
        return False

    # نوع التقرير هو عقد مساره. التقرير المعرّف بأنه «مباشرةً إلى المدير» لا
    # ينبغي أن يظهر للوكيل أو يقبل منه إجراءً عبر طلب مصاغ يدويًا. بقية كيانات
    # الاعتماد قد تستخدم خطافات خاصة أعلاه، لذلك نقصر هذا الشرط على التقرير.
    if (
        getattr(getattr(obj, "_meta", None), "model_name", "") == "report"
        and route_for(obj) == ApprovalRoute.DIRECT
    ):
        return False
    try:
        category_departments = set(category.departments.values_list("id", flat=True))
    except Exception:
        return False
    return bool(supervised & category_departments)


def _record(obj, *, actor: Actor, action: str, from_state: str, to_state: str, note: str) -> None:
    from .permissions import effective_user_role_label

    try:
        role = str(effective_user_role_label(actor.user, actor.school) or "")[:64]
    except Exception:
        role = ""

    ApprovalTransition.objects.create(
        content_type=ContentType.objects.get_for_model(type(obj)),
        object_id=obj.pk,
        school=getattr(obj, "school", None),
        actor=actor.user,
        actor_role=role,
        acted_as=actor.acted_as,
        on_behalf_of=actor.on_behalf_of,
        action=action,
        from_state=from_state,
        to_state=to_state,
        note=(note or "").strip(),
    )


def _apply(obj, *, actor: Actor, action: str, to_state: str, note: str, **field_updates) -> None:
    """ينفّذ الانتقال ويكتب الواقعة في معاملة واحدة.

    الاثنان معاً أو لا شيء: حالةٌ تغيّرت بلا واقعة تسجّلها تجعل السجل يكذب،
    وواقعةٌ بلا تغيّر حالة تجعله يبالغ.
    """
    block_reason = getattr(obj, "approval_block_reason", "")
    if block_reason:
        raise ApprovalError(str(block_reason))

    from_state = obj.approval_state
    updates = {"approval_state": to_state, "review_note": (note or "").strip(), **field_updates}

    with transaction.atomic():
        for field, value in updates.items():
            setattr(obj, field, value)
        obj.save(update_fields=list(updates))
        _record(
            obj,
            actor=actor,
            action=action,
            from_state=from_state,
            to_state=to_state,
            note=note,
        )


# ─────────────────────────────────────────────────────────────────────────────
# الانتقالات
# ─────────────────────────────────────────────────────────────────────────────
def submit(obj, user, *, school=None, note: str = ""):
    """إرسال العمل للمراجعة — من صاحبه وحده."""
    school = school or getattr(obj, "school", None)
    if _owner_id(obj) != getattr(user, "pk", None):
        raise ApprovalError("لا يُرسل العمل للمراجعة إلا صاحبه.")
    if obj.approval_state not in {
        ApprovalState.DRAFT,
        ApprovalState.RETURNED,
        ApprovalState.NEEDS_INFO,
    }:
        raise ApprovalError("هذا العمل ليس في حالة تسمح بإرساله.")

    # شرط خاص بالكيان يُفحص قبل الإرسال — كاستيفاء شواهد التكليف. اشتراطُ شاهد
    # ثم قبول إرسال بلا شاهد يجعل الشرط زينة.
    gate = getattr(obj, "assert_ready_for_submission", None)
    if callable(gate):
        gate()

    actor = Actor(user, school, ApprovalTransition.ActedAs.SELF, None)
    _apply(
        obj,
        actor=actor,
        action=ApprovalTransition.Action.SUBMIT,
        to_state=ApprovalState.SUBMITTED,
        note=note,
        submitted_at=timezone.now(),
    )
    return obj


def issue(obj, user, *, school=None, note: str = ""):
    """إصدار وثيقة من صاحب السلطة عليها — لا مراجعتها.

    **الفرق بين الإصدار والاعتماد ليس لفظياً.** الاعتماد حكمٌ على عمل غيرك،
    ولذلك تحرسه قاعدة «لا يعتمد أحد عمله». أما الإصدار فإخراجُ وثيقةٍ أنت
    مصدرها وسلطتها معاً: رئيس المجلس يكتب محضر جلسته ويصدره، ولا يوجد فوقه من
    يراجعه — وطلبُ مراجع لهذا المحضر يعني تعطيله إلى الأبد.

    وحصر هذا الباب صارم: لا يُفتح إلا لمن يجمع صفتين معاً — أنه **صاحب الوثيقة**
    وأنه **صاحب سلطة اعتمادها**، ويقرّ الكيان نفسه بذلك عبر ``allows_issuance``.
    فلا يتسرب منه موظف يعتمد تقريره لأنه كتبه.

    والواقعة تُسجَّل بإجراء ``APPROVE`` موسومةً بأنها إصدار، فيبقى الفرق مقروءاً
    في السجل بدل أن يبدو اعتماداً عادياً.
    """
    school = school or getattr(obj, "school", None)

    gate = getattr(obj, "allows_issuance", None)
    if not callable(gate) or not gate(user, school):
        raise PermissionDenied("هذه الوثيقة تُعتمد بالمراجعة لا بالإصدار.")

    if _owner_id(obj) != getattr(user, "pk", None):
        raise ApprovalError("الإصدار لصاحب الوثيقة نفسه.")
    if not _can_finalize(user, obj, school):
        raise PermissionDenied("لا تملك سلطة إصدار هذه الوثيقة.")
    if obj.approval_state in FINAL_STATES:
        raise ApprovalError("هذه الوثيقة صادرة بالفعل.")

    ready = getattr(obj, "assert_ready_for_submission", None)
    if callable(ready):
        ready()

    actor = Actor(user, school, ApprovalTransition.ActedAs.SELF, None)
    note = (note or "").strip()
    note = (note + "\n" if note else "") + "إصدار من صاحب الوثيقة وسلطتها."
    _apply(
        obj,
        actor=actor,
        action=ApprovalTransition.Action.APPROVE,
        to_state=ApprovalState.APPROVED,
        note=note,
        decided_by=user,
        decided_at=timezone.now(),
    )
    return obj


def withdraw(obj, user, *, school=None, note: str = ""):
    """سحب العمل قبل أن يبدأ أحد مراجعته.

    متاح في ``SUBMITTED`` وحدها: بعد أن يبدأ المراجع عمله يصير السحب سحباً من
    تحت يده، وهو ما يجعل المراجعة عبثاً.
    """
    school = school or getattr(obj, "school", None)
    if _owner_id(obj) != getattr(user, "pk", None):
        raise ApprovalError("لا يسحب العمل إلا صاحبه.")
    if obj.approval_state != ApprovalState.SUBMITTED:
        raise ApprovalError("لا يمكن السحب بعد بدء المراجعة.")

    actor = Actor(user, school, ApprovalTransition.ActedAs.SELF, None)
    _apply(
        obj,
        actor=actor,
        action=ApprovalTransition.Action.WITHDRAW,
        to_state=ApprovalState.DRAFT,
        note=note,
        submitted_at=None,
    )
    return obj


def start_review(obj, user, *, school=None, note: str = ""):
    """بدء المراجعة — يُعلم صاحب العمل أن أحداً أمسك بها فعلاً."""
    school = school or getattr(obj, "school", None)
    if not _can_review(user, obj, school):
        raise PermissionDenied("لا تملك مراجعة هذا العمل.")
    if obj.approval_state != ApprovalState.SUBMITTED:
        raise ApprovalError("المراجعة تبدأ من حالة «مُرسل للمراجعة».")

    actor = _actor_for(user, school, caps.REVIEW_REPORTS)
    _apply(
        obj,
        actor=actor,
        action=ApprovalTransition.Action.START_REVIEW,
        to_state=ApprovalState.UNDER_REVIEW,
        note=note,
        reviewed_by=user,
        reviewed_at=timezone.now(),
    )
    return obj


def request_info(obj, user, *, school=None, note: str = ""):
    """طلب استكمال بيانات أو مرفقات."""
    school = school or getattr(obj, "school", None)
    if not _can_review(user, obj, school):
        raise PermissionDenied("لا تملك مراجعة هذا العمل.")
    if obj.approval_state not in {ApprovalState.SUBMITTED, ApprovalState.UNDER_REVIEW}:
        raise ApprovalError("طلب الاستكمال متاح أثناء المراجعة فقط.")
    if not (note or "").strip():
        raise ApprovalError("اذكر ما ينقص — طلب استكمال بلا بيان لا يفيد صاحبه.")

    actor = _actor_for(user, school, caps.REVIEW_REPORTS)
    _apply(
        obj,
        actor=actor,
        action=ApprovalTransition.Action.REQUEST_INFO,
        to_state=ApprovalState.NEEDS_INFO,
        note=note,
        reviewed_by=user,
        reviewed_at=timezone.now(),
    )
    return obj


def return_for_changes(obj, user, *, school=None, note: str = ""):
    """إعادة العمل لصاحبه بملاحظة."""
    school = school or getattr(obj, "school", None)
    if not _can_review(user, obj, school):
        raise PermissionDenied("لا تملك مراجعة هذا العمل.")
    if obj.approval_state not in {
        ApprovalState.SUBMITTED,
        ApprovalState.UNDER_REVIEW,
        ApprovalState.RECOMMENDED,
    }:
        raise ApprovalError("هذا العمل ليس في حالة تسمح بإعادته.")
    if not (note or "").strip():
        raise ApprovalError("اذكر سبب الإعادة — إعادةٌ بلا ملاحظة تُرجع صاحبها حائراً.")

    actor = _actor_for(user, school, caps.REVIEW_REPORTS)
    _apply(
        obj,
        actor=actor,
        action=ApprovalTransition.Action.RETURN,
        to_state=ApprovalState.RETURNED,
        note=note,
        reviewed_by=user,
        reviewed_at=timezone.now(),
    )
    return obj


def recommend(obj, user, *, school=None, note: str = ""):
    """رفع العمل للمدير موصياً باعتماده — جوهر دور الوكيل."""
    school = school or getattr(obj, "school", None)
    if capability_source(user, caps.RECOMMEND_APPROVAL, school) is None:
        raise PermissionDenied("لا تملك التوصية باعتماد هذا العمل.")
    if not _can_review(user, obj, school):
        raise PermissionDenied("هذا العمل خارج نطاقك.")
    if obj.approval_state not in {ApprovalState.SUBMITTED, ApprovalState.UNDER_REVIEW}:
        raise ApprovalError("التوصية تكون أثناء المراجعة.")
    if _owner_id(obj) == getattr(user, "pk", None):
        raise ApprovalError("لا يوصي أحد باعتماد عمله.")

    actor = _actor_for(user, school, caps.RECOMMEND_APPROVAL)
    _apply(
        obj,
        actor=actor,
        action=ApprovalTransition.Action.RECOMMEND,
        to_state=ApprovalState.RECOMMENDED,
        note=note,
        reviewed_by=user,
        reviewed_at=timezone.now(),
    )
    return obj


def approve(obj, user, *, school=None, note: str = ""):
    """الاعتماد النهائي.

    ثلاثة أبواب إليه لا رابع، يحسمها :func:`_can_finalize`:
    مدير المدرسة أصالةً · مسار ``DEPUTY_FINAL`` الذي يفوّضه المدير لنوع بعينه ·
    ما يقرّره الكيان بنفسه، كالتكليف الذي يعتمد تنفيذَه مَن أصدره.

    وفوق الثلاثة جميعاً تبقى قاعدة «لا يعتمد أحد عمله» قائمة لا تُستثنى.
    """
    school = school or getattr(obj, "school", None)
    route = route_for(obj)

    is_manager = is_school_manager(user, active_school=school)
    if not _can_finalize(user, obj, school):
        raise PermissionDenied("لا تملك الاعتماد النهائي لهذا العمل.")

    if _owner_id(obj) == getattr(user, "pk", None):
        # القاعدة التي يكرّرها التوصيف في أربعة أدوار، وكانت مخروقة فعلياً.
        raise ApprovalError("لا يعتمد أحد عمله بنفسه.")

    if obj.approval_state not in {
        ApprovalState.SUBMITTED,
        ApprovalState.UNDER_REVIEW,
        ApprovalState.RECOMMENDED,
    }:
        raise ApprovalError("هذا العمل ليس في حالة تسمح باعتماده.")

    if route == ApprovalRoute.VIA_DEPUTY and is_manager:
        # المدير يعتمد قبل مرور الوكيل: مسموح لكنه استثناء يُسجَّل، فيبقى
        # تجاوز المسار مقروءاً في السجل بدل أن يمر كأنه المسار المعتاد.
        if obj.approval_state != ApprovalState.RECOMMENDED:
            note = (note or "").strip()
            note = (note + "\n" if note else "") + "اعتماد مباشر دون مرور الوكيل."

    actor = _actor_for(user, school, caps.RECOMMEND_APPROVAL)
    _apply(
        obj,
        actor=actor,
        action=ApprovalTransition.Action.APPROVE,
        to_state=ApprovalState.APPROVED,
        note=note,
        decided_by=user,
        decided_at=timezone.now(),
    )
    return obj


# ─────────────────────────────────────────────────────────────────────────────
# استعلامات العرض
# ─────────────────────────────────────────────────────────────────────────────
def available_actions(obj, user, *, school=None) -> list[str]:
    """الإجراءات المتاحة لهذا المستخدم على هذا السجل الآن.

    مصدر واحد تقرأ منه الشاشة ويُقاس عليه الاختبار، فلا يعرض القالب زراً
    ترفضه الخدمة — وهو أسوأ ما يقابله مستخدم: فعلٌ مرئي ممنوع.
    """
    if getattr(obj, "approval_block_reason", ""):
        return []

    school = school or getattr(obj, "school", None)
    state = obj.approval_state
    is_owner = _owner_id(obj) == getattr(user, "pk", None)
    actions: list[str] = []

    if is_owner:
        gate = getattr(obj, "allows_issuance", None)
        may_issue = callable(gate) and gate(user, school) and _can_finalize(user, obj, school)
        if state in {ApprovalState.DRAFT, ApprovalState.RETURNED, ApprovalState.NEEDS_INFO}:
            # الإصدار يحلّ محل الإرسال لا يزاحمه: عرضُ الاثنين على من لا مراجع
            # فوقه يجعله يختار طريقاً ينتهي بانتظار لا يأتي.
            actions.append("issue" if may_issue else "submit")
        if state == ApprovalState.SUBMITTED:
            # سجلاتٌ أُرسلت قبل إضافة مسار الإصدار لا ينبغي أن تبقى معلقة.
            # نُبقي السحب متاحاً، ونضيف الإصدار لصاحب السلطة وحده.
            if may_issue:
                actions.append("issue")
            actions.append("withdraw")
        return actions

    if not _can_review(user, obj, school):
        return actions

    may_recommend = capability_source(user, caps.RECOMMEND_APPROVAL, school) is not None

    if state == ApprovalState.SUBMITTED:
        actions.append("start_review")
    if state in {ApprovalState.SUBMITTED, ApprovalState.UNDER_REVIEW}:
        actions.append("request_info")
    if state in {ApprovalState.SUBMITTED, ApprovalState.UNDER_REVIEW, ApprovalState.RECOMMENDED}:
        actions.append("return")
    if state in {ApprovalState.SUBMITTED, ApprovalState.UNDER_REVIEW} and may_recommend:
        actions.append("recommend")

    if _can_finalize(user, obj, school) and state in {
        ApprovalState.SUBMITTED,
        ApprovalState.UNDER_REVIEW,
        ApprovalState.RECOMMENDED,
    }:
        actions.append("approve")

    return actions


def transitions_for(obj):
    """تاريخ الاعتماد الكامل لسجل واحد، من الأقدم إلى الأحدث."""
    return (
        ApprovalTransition.objects.filter(
            content_type=ContentType.objects.get_for_model(type(obj)),
            object_id=obj.pk,
        )
        .select_related("actor", "on_behalf_of")
        .order_by("created_at", "id")
    )


ACTION_DISPATCH = {
    "submit": submit,
    "issue": issue,
    "withdraw": withdraw,
    "start_review": start_review,
    "request_info": request_info,
    "return": return_for_changes,
    "recommend": recommend,
    "approve": approve,
}
