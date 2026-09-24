# -*- coding: utf-8 -*-
"""خدمة المختبر: العهدة وحركتها والتجارب.

**كل كتابة تمرّ من هنا.** الشاشة تترجم النقرة إلى نداء، والقاعدةُ الوحيدة التي
تُنفَّذ في العرض هي «هل يملك هذا المستخدم الوصول». وما دون ذلك — حساب المتاح،
وقيد الحركة، وتغيير الحالة — يعيش هنا، لأن التحقق الذي يسكن في عرضٍ يُنسى عند
إضافة مسار ثانٍ للشيء نفسه.
"""
from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone

from .capabilities import MANAGE_LAB
from .model_parts.approvals import ApprovalState, PENDING_REVIEW_STATES
from .models import (
    Department,
    DepartmentMembership,
    LabAsset,
    LabAssetHandover,
    LabExperiment,
    SchoolMembership,
)
from .lab_kinds import LAB_KIND_VALUES, infer_lab_kind
from .permissions import (
    capability_source,
    is_lab_technician,
    is_school_manager,
    supervised_department_ids,
)

__all__ = [
    "assets_for_school",
    "experiments_for_school",
    "handovers_for_school",
    "lab_kinds_for_user",
    "lab_resource_in_scope",
    "lab_summary",
    "record_handover",
    "set_asset_condition",
    "outstanding_handovers",
]


# ─────────────────────────────────────────────────────────────────────────────
# استعلامات العرض
# ─────────────────────────────────────────────────────────────────────────────
def lab_kinds_for_user(user, school) -> tuple[str, ...]:
    """Return laboratory kinds visible to the user.

    A capability answers whether access exists; it never supplies operational
    scope.  Both direct and delegated ``MANAGE_LAB`` therefore reuse the
    user's existing ``StaffScope`` departments.  An empty scope grants no lab.
    """
    if user is None or getattr(user, "is_superuser", False) or is_school_manager(user, school):
        return LAB_KIND_VALUES

    source = capability_source(user, MANAGE_LAB, school)
    kinds = set(
        value
        for value in SchoolMembership.objects.filter(
            school=school,
            teacher=user,
            is_active=True,
            job_title=SchoolMembership.JobTitle.LAB_TECH,
        ).values_list("lab_kind", flat=True)
        if value in LAB_KIND_VALUES
    )

    # Backward compatibility: infer old technician/scope assignments once from
    # the former department link. New writes use ``SchoolMembership.lab_kind``.
    department_ids: set[int] = set()
    if is_lab_technician(user, school):
        department_ids.update(
            int(pk)
            for pk in DepartmentMembership.objects.filter(
                teacher=user,
                department__school=school,
                department__is_active=True,
            ).values_list("department_id", flat=True)
        )
    if source in {"scope", "delegation"} or is_lab_technician(user, school):
        department_ids.update(supervised_department_ids(user, school))

    if department_ids:
        for name, slug in Department.objects.filter(
            school=school, is_active=True, pk__in=department_ids
        ).values_list("name", "slug"):
            inferred = infer_lab_kind(name=name, slug=slug)
            if inferred:
                kinds.add(inferred)
    if not kinds and is_lab_technician(user, school):
        legacy_kinds = {
            infer_lab_kind(name=name, slug=slug)
            for name, slug in Department.objects.filter(
                school=school, is_active=True
            ).values_list("name", "slug")
        }
        legacy_kinds.discard("")
        if len(legacy_kinds) == 1:
            kinds.update(legacy_kinds)
    return tuple(value for value in LAB_KIND_VALUES if value in kinds)


def lab_resource_in_scope(
    user,
    school,
    *,
    lab_kind: str = "",
    department_id: int | None = None,
) -> bool:
    """Defense-in-depth scope check for sensitive Lab actions.

    Normal lookups are filtered querysets.  Approval services may also receive
    a model instance directly, so they re-check the same central lab scope.
    Legacy unclassified records retain their department-based boundary.
    """
    allowed_kinds = set(lab_kinds_for_user(user, school))
    if not allowed_kinds:
        return False
    if lab_kind:
        return lab_kind in allowed_kinds
    if department_id is not None:
        return int(department_id) in supervised_department_ids(user, school)
    return True


def _assets_in_user_scope(school, user):
    qs = LabAsset.objects.filter(school=school)
    if user is None or getattr(user, "is_superuser", False) or is_school_manager(user, school):
        return qs

    lab_kinds = lab_kinds_for_user(user, school)
    if is_lab_technician(user, school):
        if lab_kinds:
            # السجلات القديمة غير المصنفة لا تُعرض إلا لصاحبها؛ فتظل قابلة
            # للتصنيف من غير أن تصبح ممراً بين مختبري العلوم والحاسب.
            return qs.filter(
                Q(lab_kind__in=lab_kinds)
                | Q(lab_kind="", recorded_by=user)
                | Q(lab_kind="", custodian=user)
            ).distinct()
        return qs.filter(
            Q(lab_kind="", recorded_by=user) | Q(lab_kind="", custodian=user)
        ).distinct()

    source = capability_source(user, MANAGE_LAB, school)
    if source in {"scope", "delegation"} and lab_kinds:
        legacy_department_ids = supervised_department_ids(user, school)
        return qs.filter(
            Q(lab_kind__in=lab_kinds)
            | Q(lab_kind="", department_id__in=legacy_department_ids)
            | Q(lab_kind="", department__isnull=True)
        )
    return qs.none()


def _experiments_in_user_scope(school, user):
    qs = LabExperiment.objects.filter(school=school)
    if user is None or getattr(user, "is_superuser", False) or is_school_manager(user, school):
        return qs

    lab_kinds = lab_kinds_for_user(user, school)
    if is_lab_technician(user, school):
        if lab_kinds:
            return qs.filter(
                Q(lab_kind__in=lab_kinds)
                | Q(lab_kind="", recorder=user)
            )
        return qs.filter(lab_kind="", recorder=user)

    source = capability_source(user, MANAGE_LAB, school)
    if source in {"scope", "delegation"} and lab_kinds:
        legacy_department_ids = supervised_department_ids(user, school)
        return qs.filter(
            Q(lab_kind__in=lab_kinds)
            | Q(lab_kind="", department_id__in=legacy_department_ids)
            | Q(lab_kind="", department__isnull=True)
        )
    return qs.none()


def assets_for_school(school, *, user=None, include_inactive: bool = False):
    """جرد المدرسة، ومعه ما تحتاجه الشاشة في استعلام واحد.

    الكمية الخارجة تُجمَّع في الاستعلام لا في حلقة: قراءة ``out_quantity`` لكل
    صفّ في كشفٍ من مئة صنف مئةُ استعلام — وهو ما لا يظهر في مختبرٍ فيه عشرة
    أصناف ويظهر بعد عام من الاستعمال.
    """
    qs = _assets_in_user_scope(school, user).select_related(
        "department", "custodian"
    )
    if not include_inactive:
        qs = qs.filter(is_active=True)
    return qs.annotate(
        handed_out=Sum(
            "handovers__quantity",
            filter=Q(handovers__direction=LabAssetHandover.Direction.OUT),
        ),
        handed_back=Sum(
            "handovers__quantity",
            filter=Q(handovers__direction=LabAssetHandover.Direction.IN),
        ),
    ).order_by("name", "id")


def experiments_for_school(school, *, user=None):
    return (
        _experiments_in_user_scope(school, user)
        .select_related("department", "recorder", "requested_by", "report")
        .prefetch_related("assets")
        .order_by("-experiment_date", "-id")
    )


def handovers_for_school(school, *, user=None, limit: int | None = None):
    asset_ids = _assets_in_user_scope(school, user).values("pk")
    qs = (
        LabAssetHandover.objects.filter(school=school, asset_id__in=asset_ids)
        .select_related("asset", "asset__department", "person", "recorded_by")
        .order_by("-happened_at", "-id")
    )
    return qs[:limit] if limit else qs


def outstanding_handovers(school, *, user=None):
    """ما خرج من المختبر ولم يُرجَع — مجموعاً بالصنف والمستلم.

    السؤال العملي عند الجرد ليس «كم حركةً وقعت؟» بل «ماذا لا يزال خارج
    المختبر وعند من؟»، وهو فرقٌ لا يجيبه سردُ الحركات بترتيب زمني.
    """
    # الحركات القديمة كانت تسمح بإرجاعٍ بلا اسم. التجميع بـ ``person_id``
    # يجعل هذا الإرجاع في مجموعة مستقلة سالبة، فيبقى المستلم ظاهراً في كشف
    # المتأخرات رغم عودة الصنف فعلياً. نبني رصيداً زمنياً (FIFO) لكل صنف:
    # الإرجاع المسمّى يسوّي عهدة الشخص أولاً، وغير المسمّى يسوّي أقدم عهدة.
    asset_ids = _assets_in_user_scope(school, user).values("pk")
    movements = (
        LabAssetHandover.objects.filter(school=school, asset_id__in=asset_ids)
        .values(
            "asset_id",
            "asset__name",
            "person_id",
            "person_name",
            "direction",
            "quantity",
        )
        .order_by("asset_id", "happened_at", "id")
    )
    lots_by_asset: dict[int, list[dict]] = {}
    asset_names: dict[int, str] = {}

    for movement in movements:
        asset_id = movement["asset_id"]
        asset_names[asset_id] = movement["asset__name"]
        lots = lots_by_asset.setdefault(asset_id, [])
        quantity = int(movement.get("quantity") or 0)

        if movement["direction"] == LabAssetHandover.Direction.OUT:
            lots.append(
                {
                    "person_id": movement["person_id"],
                    "person_name": movement["person_name"] or "—",
                    "quantity": quantity,
                }
            )
            continue

        # المسمّى يطابق صاحبه أولاً. ويأتي بقية الرصيد بعده لإبقاء الإجمالي
        # متسقاً حتى مع بيانات تاريخية غير مكتملة أو حساب حُذف لاحقاً.
        person_id = movement["person_id"]
        person_name = movement["person_name"] or ""

        def same_person(lot):
            if person_id is not None:
                return lot["person_id"] == person_id
            return bool(person_name) and lot["person_name"] == person_name

        candidates = (
            [lot for lot in lots if same_person(lot)]
            + [lot for lot in lots if not same_person(lot)]
            if person_id is not None or person_name
            else list(lots)
        )
        remaining = quantity
        for lot in candidates:
            if remaining <= 0:
                break
            consumed = min(remaining, lot["quantity"])
            lot["quantity"] -= consumed
            remaining -= consumed

    result = []
    for asset_id, lots in lots_by_asset.items():
        grouped: dict[tuple[int | None, str], int] = {}
        for lot in lots:
            if lot["quantity"] <= 0:
                continue
            key = (lot["person_id"], lot["person_name"])
            grouped[key] = grouped.get(key, 0) + lot["quantity"]
        for (person_id, person_name), quantity in grouped.items():
            result.append(
                {
                    "asset_id": asset_id,
                    "asset_name": asset_names[asset_id],
                    "person_id": person_id,
                    "person_name": person_name,
                    "quantity": quantity,
                }
            )
    return sorted(result, key=lambda row: (row["asset_name"], row["person_name"]))


def lab_summary(school, *, user=None, outstanding_rows=None) -> dict:
    """مؤشرات المختبر — في استعلامين لا استعلامٍ لكل رقم."""
    assets = _assets_in_user_scope(school, user).filter(is_active=True).aggregate(
        total=Count("id"),
        attention=Count(
            "id", filter=Q(condition__in=LabAsset.ATTENTION_CONDITIONS)
        ),
        missing=Count("id", filter=Q(condition=LabAsset.Condition.MISSING)),
        damaged=Count("id", filter=Q(condition=LabAsset.Condition.DAMAGED)),
    )
    experiments = _experiments_in_user_scope(school, user).aggregate(
        total=Count("id"),
        pending=Count("id", filter=Q(approval_state__in=PENDING_REVIEW_STATES)),
        approved=Count("id", filter=Q(approval_state=ApprovalState.APPROVED)),
        drafts=Count("id", filter=Q(approval_state=ApprovalState.DRAFT)),
    )
    if outstanding_rows is None:
        outstanding_rows = outstanding_handovers(school, user=user)

    return {
        "assets_total": int(assets.get("total") or 0),
        "assets_attention": int(assets.get("attention") or 0),
        "assets_missing": int(assets.get("missing") or 0),
        "assets_damaged": int(assets.get("damaged") or 0),
        "experiments_total": int(experiments.get("total") or 0),
        "experiments_pending": int(experiments.get("pending") or 0),
        "experiments_approved": int(experiments.get("approved") or 0),
        "experiments_drafts": int(experiments.get("drafts") or 0),
        "outstanding": len(outstanding_rows),
    }


# ─────────────────────────────────────────────────────────────────────────────
# الكتابة
# ─────────────────────────────────────────────────────────────────────────────
@transaction.atomic
def record_handover(
    asset: LabAsset,
    *,
    direction: str,
    person,
    quantity: int,
    actor,
    happened_at=None,
    note: str = "",
) -> LabAssetHandover:
    """قيدُ حركةِ عهدة، بعد التحقق من أنها ممكنة.

    ``full_clean`` تُستدعى صراحةً: قواعد «لا يُسلَّم أكثر من المتاح» و«لا يُرجَع
    أكثر من الخارج» تعيش في ``clean`` النموذج، و``save`` وحدها لا تستدعيها —
    فمسارٌ يتخطاها يكتب جرداً يقول إن خارج المختبر خمساً من أربع.
    """
    if person is None:
        if direction == LabAssetHandover.Direction.OUT:
            raise ValidationError("حدّد من تسلَّم الصنف.")
        if direction == LabAssetHandover.Direction.IN:
            raise ValidationError("حدّد من أعاد الصنف لتسوية عهدته بدقة.")

    handover = LabAssetHandover(
        school=asset.school,
        asset=asset,
        direction=direction,
        person=person,
        quantity=int(quantity or 0),
        happened_at=happened_at or timezone.now(),
        recorded_by=actor,
        note=(note or "").strip()[:255],
    )
    handover.full_clean(exclude=["person_name", "school"])
    handover.save()
    return handover


@transaction.atomic
def set_asset_condition(asset: LabAsset, *, condition: str, actor=None) -> LabAsset:
    """تغيير حالة صنف — تالفاً أو مفقوداً أو سليماً بعد الصيانة."""
    valid = {value for value, _label in LabAsset.Condition.choices}
    if condition not in valid:
        raise ValidationError("حالة غير معروفة.")

    # صنفٌ لا يزال بعضُه خارج المختبر لا يُوسَم مفقوداً: الفقد حكمٌ على ما في
    # المختبر، وما هو في يد معلّمٍ معروفٍ ليس مفقوداً — هو مُسلَّم.
    if condition == LabAsset.Condition.MISSING and asset.out_quantity > 0:
        raise ValidationError(
            "لا يُوسَم الصنف مفقوداً وبعضه مُسلَّم — سجّل الإرجاع أولاً أو راجع سجل الحركة."
        )

    asset.condition = condition
    asset.save(update_fields=["condition", "updated_at"])
    return asset
