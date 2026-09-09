# -*- coding: utf-8 -*-
"""نشر مسودة التعميم بعد اعتمادها.

**الاعتماد هو النشر.** لا خطوة ثالثة بعده: مديرٌ يعتمد مسودةً ثم يُطلب منه
ضغط «نشر» يترك تعاميم معتمَدة لم تصل أحداً — وهي أسوأ حالة يمكن أن تقع فيها
منظومة تعاميم، لأن الجميع يظنها وصلت.

والنشر ينشئ ``Notification`` عادياً بمستلميه، فيسري عليه كل ما بُني للتعاميم
من توقيع ومهلة وإقرار وتقارير اطّلاع بلا تعديل سطر واحد في ذلك المسار.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from .model_parts.circular_drafts import CircularDraft
from .model_parts.notifications import Notification, NotificationRecipient
from .model_parts.schools import SchoolMembership, Teacher

__all__ = ["publish_draft", "draft_recipients"]

DEFAULT_ACK_TEXT = "أقرّ بأنني اطلعت على هذا التعميم وفهمت ما ورد فيه وأتعهد بالالتزام به."


def draft_recipients(draft: CircularDraft) -> list[int]:
    """معرّفات مستلمي المسودة حسب فئتها المستهدفة.

    **المستلمون هم المنسوبون** — ومدير المدرسة خارجهم لأنه مُصدر التعميم لا
    مخاطَبٌ به، وإدراجُه يطالبه بالتوقيع على ما أصدره.

    ومُعِدّ المسودة يبقى ضمن المستلمين ما دام منسوباً: التعميم يُلزمه كما يُلزم
    غيره، وإخراجُه منه يجعل سجل التواقيع ناقصاً بلا سبب.
    """
    people = Teacher.objects.filter(
        is_active=True,
        school_memberships__school_id=draft.school_id,
        school_memberships__is_active=True,
        school_memberships__role_type__in=SchoolMembership.STAFF_ROLES,
    )
    if draft.audience == CircularDraft.Audience.DEPARTMENT and draft.department_id:
        people = people.filter(dept_memberships__department_id=draft.department_id)
    return list(people.values_list("id", flat=True).distinct())


@transaction.atomic
def publish_draft(draft: CircularDraft, publisher) -> Notification:
    """ينشئ التعميم الفعلي من المسودة المعتمَدة.

    يُستدعى مرة واحدة: مسودةٌ تُنشر مرتين تصل مستلميها مرتين، ويصير لكل نسخة
    سجل تواقيع مستقل فلا يُعرف أيهما الحجّة.
    """
    if draft.published_notification_id is not None:
        return draft.published_notification

    notification = Notification.objects.create(
        school_id=draft.school_id,
        title=draft.title,
        message=draft.body,
        kind=Notification.Kind.CIRCULAR,
        requires_signature=bool(draft.requires_signature),
        signature_deadline_at=draft.signature_deadline_at,
        signature_ack_text=DEFAULT_ACK_TEXT if draft.requires_signature else "",
        # المُنشئ هو المعتمِد لا مُعِدّ المسودة: التعميم يصدر باسم من يملك
        # إصداره، وإسنادُه لمُعِدّه يوهم المستلمين بمصدر لا سلطة له.
        created_by=publisher,
    )

    recipient_ids = draft_recipients(draft)
    NotificationRecipient.objects.bulk_create(
        [
            NotificationRecipient(notification=notification, teacher_id=person_id)
            for person_id in recipient_ids
        ],
        ignore_conflicts=True,
    )
    from .realtime_notifications import push_new_notification_to_teachers

    push_new_notification_to_teachers(notification=notification, teacher_ids=recipient_ids)

    draft.published_notification = notification
    draft.published_at = timezone.now()
    draft.save(update_fields=["published_notification", "published_at"])
    return notification
