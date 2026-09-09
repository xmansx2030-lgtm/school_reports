from __future__ import annotations

from django.conf import settings
from django.template.loader import render_to_string
from django.utils import timezone

from .pdf_render import render_html_pdf


def _render_pdf(context: dict, *, request=None) -> bytes:
    html = render_to_string("reports/archive_record_pdf.html", context)
    if request is not None:
        base_url = request.build_absolute_uri("/")
    else:
        base_url = str(settings.BASE_DIR)
    return render_html_pdf(html=html, base_url=base_url)


def generate_ticket_archive_pdf(ticket, *, request=None) -> bytes:
    recipients = list(ticket.recipients.all().order_by("name", "id"))
    notes = list(ticket.notes.select_related("author").order_by("created_at", "id"))
    return _render_pdf(
        {
            "record_kind": "ticket",
            "record": ticket,
            "school": ticket.school,
            "recipients": recipients,
            "notes": notes,
            "generated_at": timezone.localtime(),
        },
        request=request,
    )


def generate_notification_archive_pdf(notification, *, request=None) -> bytes:
    recipient_rows = list(
        notification.recipients.select_related("teacher").order_by("teacher__name", "id")
    )
    signed_count = sum(1 for row in recipient_rows if row.is_signed)
    read_count = sum(1 for row in recipient_rows if row.is_read)
    return _render_pdf(
        {
            "record_kind": (
                "circular"
                if getattr(notification, "kind", "notification") == "circular"
                else ("newsletter" if getattr(notification, "kind", "notification") == "newsletter" else "notification")
            ),
            "record": notification,
            "school": notification.school,
            "recipient_rows": recipient_rows,
            "signed_count": signed_count,
            "read_count": read_count,
            "generated_at": timezone.localtime(),
        },
        request=request,
    )
