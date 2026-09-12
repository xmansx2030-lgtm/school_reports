"""Application service for materializing notification recipients."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from django.apps import apps

from core.observability import report_degraded

logger = logging.getLogger(__name__)


def dispatch_notification_recipients(
    notification_id: int,
    teacher_ids: list[int] | None = None,
    *,
    trace_id: str | None = None,
) -> bool:
    """Create recipient rows idempotently and emit best-effort realtime updates."""
    Notification = apps.get_model("reports", "Notification")
    NotificationRecipient = apps.get_model("reports", "NotificationRecipient")
    Teacher = apps.get_model("reports", "Teacher")

    try:
        notification = Notification.objects.get(pk=notification_id)
    except Notification.DoesNotExist:
        logger.error("Notification %s not found.", notification_id)
        return False

    if teacher_ids:
        teachers = Teacher.objects.filter(pk__in=teacher_ids, is_active=True).only("id")
    else:
        teachers = (
            Teacher.objects.filter(
                is_active=True,
                school_memberships__school__is_active=True,
            )
            .distinct()
            .only("id")
        )
        role_types = ["teacher"]
        if notification.school_id:
            teachers = teachers.filter(
                school_memberships__school=notification.school,
                school_memberships__is_active=True,
                school_memberships__role_type__in=role_types,
            ).distinct()
        else:
            teachers = teachers.filter(
                school_memberships__is_active=True,
                school_memberships__role_type__in=role_types,
            ).distinct()

    try:
        from .realtime_notifications import push_new_notification_to_teachers
    except Exception:
        report_degraded("notifications.realtime_import")
        push_new_notification_to_teachers = None

    batch_size = 500
    batch_ids: list[int] = []
    teacher_id_qs = teachers.values_list("id", flat=True)
    for teacher_id in teacher_id_qs.iterator(chunk_size=batch_size):
        batch_ids.append(teacher_id)
        if len(batch_ids) >= batch_size:
            _persist_recipient_batch(
                notification,
                batch_ids,
                NotificationRecipient,
                push_new_notification_to_teachers,
                trace_id,
                metric="realtime.push_batch",
            )
            batch_ids = []

    if batch_ids:
        _persist_recipient_batch(
            notification,
            batch_ids,
            NotificationRecipient,
            push_new_notification_to_teachers,
            trace_id,
            metric="realtime.push_batch_tail",
        )
    return True


def _persist_recipient_batch(
    notification: Any,
    teacher_ids: list[int],
    recipient_model: Any,
    realtime_dispatch: Callable[..., object] | None,
    trace_id: str | None,
    *,
    metric: str,
) -> None:
    # These model classes come from Django's dynamic app registry. Keeping Any
    # at this single adapter boundary avoids pretending their runtime type is a
    # concrete imported model and reintroducing the dependency cycle.
    recipient_model.objects.bulk_create(
        [recipient_model(notification=notification, teacher_id=teacher_id) for teacher_id in teacher_ids],
        ignore_conflicts=True,
    )
    if realtime_dispatch is None:
        return
    try:
        realtime_dispatch(
            notification=notification,
            teacher_ids=teacher_ids,
            trace_id=trace_id,
        )
    except Exception:
        # Recipient rows are durable; realtime delivery is an optional channel.
        report_degraded(metric, count=len(teacher_ids))


__all__ = ("dispatch_notification_recipients",)
