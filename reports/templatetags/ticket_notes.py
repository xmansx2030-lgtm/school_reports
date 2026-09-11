from __future__ import annotations

from typing import Any

from django import template

from reports.models import Ticket

register = template.Library()


_STATUS_MAP = dict(getattr(Ticket.Status, "choices", []))


def _status_label(raw: str) -> str:
    raw = (raw or "").strip()
    return _STATUS_MAP.get(raw, raw)


@register.filter(name="ticket_note_ar")
def ticket_note_ar(value: Any) -> str:
    """Convert status-change note bodies to Arabic labels.

    Handles legacy bodies that stored raw status codes, e.g.:
      - "تغيير الحالة: open → in_progress"
      - "تغيير الحالة تلقائيًا بسبب ملاحظة المرسل: done → open"

    If the text doesn't match a known pattern, returns it unchanged.
    """

    text = "" if value is None else str(value)
    if "→" not in text or "تغيير الحالة" not in text:
        return text

    # Split once in case the note contains multiple arrows.
    left, right = text.split("→", 1)
    if ":" not in left:
        return text

    prefix, old_raw = left.rsplit(":", 1)
    new_raw = right

    old_label = _status_label(old_raw)
    new_label = _status_label(new_raw)

    # Preserve original prefix wording (manual vs auto).
    return f"{prefix.strip()}: {old_label} → {new_label}"
