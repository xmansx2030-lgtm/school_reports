"""Database-backed coordination for notification send submissions."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import Any

from django.db import IntegrityError, transaction

from .models import Notification, NotificationSendSubmission, School, Teacher


class NotificationSubmissionConflict(Exception):
    """The same sender/key pair was reused for a different logical payload."""


def _model_ids(values: Iterable[Any] | None) -> list[int]:
    if not values:
        return []
    return sorted({int(getattr(value, "pk", value)) for value in values})


def _canonical_datetime(value: datetime | date | None) -> str:
    return value.isoformat() if value is not None else ""


def _attachment_identity(attachment: Any | None) -> dict[str, Any] | None:
    if attachment is None:
        return None

    digest = hashlib.sha256()
    try:
        attachment.seek(0)
        for chunk in attachment.chunks():
            digest.update(chunk)
    finally:
        # Django storage expects the uploaded stream to remain readable from
        # its beginning after validation/fingerprinting.
        attachment.seek(0)

    return {
        "sha256": digest.hexdigest(),
        "size": int(getattr(attachment, "size", 0) or 0),
        "content_type": str(getattr(attachment, "content_type", "") or "").lower(),
        "name": str(getattr(attachment, "name", "") or ""),
    }


def notification_submission_scope(
    *,
    cleaned_data: Mapping[str, Any],
    sender: Teacher,
    default_school: School | None,
) -> tuple[str, School | None]:
    """Return the canonical audience scope and school used by form.save()."""

    if bool(getattr(sender, "is_superuser", False)):
        scope = str(cleaned_data.get("audience_scope") or "school").strip()
        if scope == "all":
            return "all", None
        return "school", cleaned_data.get("target_school") or None
    return "school", default_school


def notification_submission_fingerprint(
    *,
    cleaned_data: Mapping[str, Any],
    sender: Teacher,
    default_school: School | None,
    mode: str,
) -> tuple[str, School | None]:
    """Hash normalized, validated input without persisting its sensitive text."""

    scope, school = notification_submission_scope(
        cleaned_data=cleaned_data,
        sender=sender,
        default_school=default_school,
    )
    normalized_mode = (mode or "notification").strip().lower()
    kind = (
        Notification.Kind.CIRCULAR
        if normalized_mode == "circular"
        else str(cleaned_data.get("communication_type") or Notification.Kind.NOTIFICATION)
    )
    attachment = cleaned_data.get("attachment")
    payload = {
        "version": 1,
        "sender_id": int(sender.pk),
        "scope": scope,
        "school_id": int(school.pk) if school is not None else None,
        "kind": kind,
        "title": str(cleaned_data.get("title") or ""),
        "message": str(cleaned_data.get("message") or ""),
        "is_important": bool(cleaned_data.get("is_important")),
        "expires_at": _canonical_datetime(cleaned_data.get("expires_at")),
        "requires_signature": bool(cleaned_data.get("requires_signature")),
        "signature_deadline_at": _canonical_datetime(
            cleaned_data.get("signature_deadline_at")
        ),
        "signature_ack_text": str(cleaned_data.get("signature_ack_text") or ""),
        "teacher_ids": _model_ids(cleaned_data.get("teachers")),
        "department_ids": _model_ids(cleaned_data.get("target_department")),
        "attachment": _attachment_identity(attachment),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), school


def reserve_notification_submission(
    *,
    sender: Teacher,
    submission_key,
    school: School | None,
    payload_fingerprint: str,
) -> tuple[NotificationSendSubmission, bool]:
    """Reserve ``sender + key`` using the database uniqueness constraint.

    The nested atomic block creates a savepoint.  A uniqueness race rolls back
    only that insertion, leaving the caller's outer transaction usable.  Any
    other integrity failure is re-raised unless the exact reservation exists.
    """

    try:
        with transaction.atomic():
            submission = NotificationSendSubmission.objects.create(
                sender=sender,
                submission_key=submission_key,
                school=school,
                payload_fingerprint=payload_fingerprint,
            )
        return submission, True
    except IntegrityError:
        submission = (
            NotificationSendSubmission.objects.select_related("notification")
            .filter(sender=sender, submission_key=submission_key)
            .first()
        )
        if submission is None:
            raise
        return submission, False


def validate_submission_fingerprint(
    submission: NotificationSendSubmission,
    payload_fingerprint: str,
) -> None:
    if not hmac.compare_digest(submission.payload_fingerprint, payload_fingerprint):
        raise NotificationSubmissionConflict


__all__ = (
    "NotificationSubmissionConflict",
    "notification_submission_fingerprint",
    "notification_submission_scope",
    "reserve_notification_submission",
    "validate_submission_fingerprint",
)
