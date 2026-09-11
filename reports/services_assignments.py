# -*- coding: utf-8 -*-
"""دورة تنفيذ التكليف — ما يسبق المراجعة.

الفصل عن ``services_approval`` مقصود: ذاك يحكم **من يقرّر**، وهذا يحكم **متى
يصير العمل جاهزاً للقرار**. فقبول التكليف وتحديث نسبته ورفع شواهده أفعالُ
المكلَّف على عمله هو، ولا تمر بمراجع.

القاعدة الوحيدة التي تربط الاثنين: لا يُرسَل التكليف للمراجعة قبل استيفاء
شواهده. اشتراطُ شاهد ثم قبول إرسال بلا شاهد يجعل الشرط زينة.
"""
from __future__ import annotations

from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.utils import timezone

from .model_parts.approvals import ApprovalState
from .model_parts.assignments import AssignmentEvidence, AssignmentTarget
from .services_approval import ApprovalError

__all__ = [
    "accept_target",
    "request_clarification",
    "update_progress",
    "add_evidence",
    "remove_evidence",
    "ensure_submittable",
    "targets_for_assignee",
    "open_targets_for_assignee",
    "overdue_targets_for_school",
    "assignment_board_rows",
]


def _require_assignee(target: AssignmentTarget, user) -> None:
    if target.assignee_id != getattr(user, "pk", None):
        raise PermissionDenied("هذا التكليف ليس مسنداً إليك.")


def _require_open(target: AssignmentTarget) -> None:
    if getattr(target.assignment, "is_cancelled", False):
        raise ApprovalError("هذا التكليف ملغى.")
    if target.approval_state == ApprovalState.APPROVED:
        raise ApprovalError("اعتُمد تنفيذ هذا التكليف، ولا يقبل تعديلاً.")


def accept_target(target: AssignmentTarget, user) -> AssignmentTarget:
    """قبول التكليف — إقرار المكلَّف بأنه اطّلع وشرع.

    القبول ليس شرطاً للتنفيذ بل إشارةٌ للمكلِّف بأن تكليفه وصل ووُعي. اشتراطه
    قبل العمل يجعل نسيان ضغطة زر يوقف مهمة أُنجزت فعلاً.
    """
    _require_assignee(target, user)
    _require_open(target)
    if target.accepted_at is None:
        target.accepted_at = timezone.now()
        target.save(update_fields=["accepted_at"])
    return target


def request_clarification(target: AssignmentTarget, user, *, note: str) -> AssignmentTarget:
    """طلب توضيح المطلوب قبل الشروع."""
    _require_assignee(target, user)
    _require_open(target)
    text = (note or "").strip()
    if not text:
        raise ApprovalError("اذكر ما تريد توضيحه — طلبٌ بلا سؤال لا يفيد المكلِّف.")

    target.clarification_note = text
    target.save(update_fields=["clarification_note"])
    return target


def update_progress(
    target: AssignmentTarget, user, *, percent: int, note: str = ""
) -> AssignmentTarget:
    """تحديث نسبة الإنجاز.

    النسبة **لا تُقيَّد بالتصاعد**: عملٌ ظُنّ منجزاً ثم تبيّن نقصه يجب أن يعود
    رقمه إلى الحقيقة. ومنعُ النزول يدفع المكلَّف إلى ترك رقم كاذب.
    """
    _require_assignee(target, user)
    _require_open(target)

    try:
        value = int(percent)
    except (TypeError, ValueError) as exc:
        raise ApprovalError("نسبة الإنجاز رقم بين 0 و100.") from exc
    if not 0 <= value <= 100:
        raise ApprovalError("نسبة الإنجاز رقم بين 0 و100.")

    target.progress_percent = value
    fields = ["progress_percent"]
    if note:
        target.progress_note = note.strip()
        fields.append("progress_note")
    # التحديث إقرارٌ بالاطلاع أيضاً، فلا يُطلب من المكلَّف ضغط زرّين لفعل واحد.
    if target.accepted_at is None:
        target.accepted_at = timezone.now()
        fields.append("accepted_at")

    target.save(update_fields=fields)
    return target


def add_evidence(
    target: AssignmentTarget, user, *, file, note: str = ""
) -> AssignmentEvidence:
    _require_assignee(target, user)
    _require_open(target)
    if file is None:
        raise ApprovalError("اختر ملف الشاهد.")

    return AssignmentEvidence.objects.create(
        target=target,
        file=file,
        note=(note or "").strip()[:255],
        uploaded_by=user,
    )


def remove_evidence(evidence: AssignmentEvidence, user) -> None:
    """حذف شاهد — لصاحبه وحده وقبل اعتماد التنفيذ.

    بعد الاعتماد يصير الشاهد جزءاً مما اعتُمد عليه، وحذفه يفرّغ الاعتماد من
    سنده.
    """
    target = evidence.target
    _require_assignee(target, user)
    _require_open(target)
    evidence.delete()


def ensure_submittable(target: AssignmentTarget) -> None:
    """يتحقق من استيفاء شرط الشواهد قبل الإرسال للمراجعة."""
    shortfall = target.evidence_shortfall()
    if shortfall:
        raise ApprovalError(
            f"هذا التكليف يتطلب شواهد — ينقصك {shortfall} شاهد على الأقل قبل الإرسال."
        )


# ─────────────────────────────────────────────────────────────────────────────
# استعلامات العرض
# ─────────────────────────────────────────────────────────────────────────────
def _in_school(qs, school):
    """قصر التكليفات على مدرسة — قاعدةٌ واحدة لا نسختان.

    تكليفات المجموعة تصل مديرَ المدرسة بمدرسته، فتُدرَج في سياقها. وكتابةُ هذا
    الشرط مرتين تجعل شاشةً تعرض تكليف المجموعة وأخرى تُسقطه.
    """
    if school is None:
        return qs
    return qs.filter(Q(school=school) | Q(assignment__school=school))


def targets_for_assignee(user, school=None):
    """تكليفات المستخدم، مع كل ما تحتاجه الشاشة في استعلام واحد."""
    qs = (
        AssignmentTarget.objects.filter(assignee=user)
        .select_related("assignment", "assignment__issuer", "assignment__department", "school")
        .annotate(evidence_total=Count("evidence"))
        .order_by("assignment__due_at", "id")
    )
    return _in_school(qs, school)


def open_targets_for_assignee(user, school=None):
    """ما لم يُغلق من تكليفات المستخدم — أقربها موعداً أولاً.

    نسخة خفيفة من :func:`targets_for_assignee` للوحات التي تعرض العنوان
    والموعد وحدهما: عدّ الشواهد ضمٌّ و``GROUP BY`` لا تحتاجهما بطاقةٌ لا
    تعرضهما، وثمنهما يُدفع على صفحة الهبوط لكل منسوب.

    والمعنى هنا مطابق لـ ``AssignmentTarget.is_overdue`` عمداً: الملغى خارج
    الحساب، والمعتمد ليس مفتوحاً — فيتّفق ما تعدّه القاعدةُ وما يقوله الصفّ.
    """
    qs = (
        AssignmentTarget.objects.filter(assignee=user)
        .filter(assignment__cancelled_at__isnull=True)
        .exclude(approval_state=ApprovalState.APPROVED)
        .select_related("assignment")
        .order_by("assignment__due_at", "id")
    )
    return _in_school(qs, school)


def overdue_targets_for_school(school):
    """المتأخرات في مدرسة — الاستعلام الذي لم يكن له مقابل قبل هذه المرحلة."""
    now = timezone.now()
    return (
        AssignmentTarget.objects.filter(
            Q(school=school) | Q(assignment__school=school),
            assignment__due_at__lt=now,
            assignment__cancelled_at__isnull=True,
        )
        .exclude(approval_state=ApprovalState.APPROVED)
        .select_related("assignment", "assignee")
        .order_by("assignment__due_at", "id")
    )


def assignment_board_rows(assignments):
    """صف لكل تكليف بمؤشرات توزيعه — باستعلامين لا باستعلام لكل صف."""
    assignments = list(assignments)
    if not assignments:
        return []

    ids = [item.pk for item in assignments]
    now = timezone.now()
    stats = {
        row["assignment_id"]: row
        for row in AssignmentTarget.objects.filter(assignment_id__in=ids)
        .values("assignment_id")
        .annotate(
            total=Count("id"),
            done=Count("id", filter=Q(approval_state=ApprovalState.APPROVED)),
            pending=Count(
                "id",
                filter=Q(
                    approval_state__in=[
                        ApprovalState.SUBMITTED,
                        ApprovalState.UNDER_REVIEW,
                        ApprovalState.RECOMMENDED,
                    ]
                ),
            ),
        )
    }
    overdue = {
        row["assignment_id"]: row["late"]
        for row in AssignmentTarget.objects.filter(
            assignment_id__in=ids,
            assignment__due_at__lt=now,
            assignment__cancelled_at__isnull=True,
        )
        .exclude(approval_state=ApprovalState.APPROVED)
        .values("assignment_id")
        .annotate(late=Count("id"))
    }

    rows = []
    for assignment in assignments:
        row = stats.get(assignment.pk, {}) or {}
        total = int(row.get("total") or 0)
        done = int(row.get("done") or 0)
        rows.append(
            {
                "assignment": assignment,
                "total": total,
                "done": done,
                "pending": int(row.get("pending") or 0),
                "overdue": int(overdue.get(assignment.pk) or 0),
                "percent": round(done * 100 / total) if total else 0,
            }
        )
    return rows
