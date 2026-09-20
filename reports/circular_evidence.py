"""Canonical document and acknowledgement evidence for issued circulars."""

from __future__ import annotations

import hashlib
import json


def evidence_digest(value: dict) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def document_snapshot(notification, *, basis: str) -> dict:
    attachment = notification.attachment
    attachment_digest = ""
    attachment_size = 0
    if attachment:
        hasher = hashlib.sha256()
        with attachment.storage.open(attachment.name, "rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                attachment_size += len(chunk)
                hasher.update(chunk)
        attachment_digest = hasher.hexdigest()

    return {
        "version": 1,
        "basis": basis,
        "notification_id": notification.pk,
        "kind": notification.kind,
        "school_id": notification.school_id,
        "created_by_id": notification.created_by_id,
        "issued_at": notification.created_at.isoformat(),
        "title": notification.title,
        "message": notification.message,
        "requires_signature": notification.requires_signature,
        "ack_text": notification.signature_ack_text if notification.requires_signature else "",
        "deadline_at": (
            notification.signature_deadline_at.isoformat()
            if notification.signature_deadline_at else ""
        ),
        "attachment_name": attachment.name if attachment else "",
        "attachment_size": attachment_size,
        "attachment_sha256": attachment_digest,
    }


def document_matches_snapshot(notification) -> bool:
    snapshot = notification.issued_snapshot or {}
    if not snapshot or not notification.issued_digest:
        return False
    return (
        evidence_digest(snapshot) == notification.issued_digest
        and evidence_digest(document_snapshot(
            notification, basis=snapshot.get("basis", "at_issue")
        )) == notification.issued_digest
    )


def acknowledgement_digest(recipient, signed_at) -> str:
    evidence = {
        "version": 1,
        "recipient_id": recipient.pk,
        "teacher_id": recipient.teacher_id,
        "notification_id": recipient.notification_id,
        "document_sha256": recipient.signed_document_digest,
        "ack_text": recipient.signed_ack_text,
        "method": recipient.signature_method,
        "signed_at": signed_at.isoformat(),
    }
    # Old phone acknowledgements must retain their original digest format.
    if recipient.signature_image_sha256:
        evidence["signature_image_sha256"] = recipient.signature_image_sha256
    return evidence_digest(evidence)
