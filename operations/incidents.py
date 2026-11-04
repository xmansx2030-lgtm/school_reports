from django.db import transaction
from django.utils import timezone

from .models import Incident, IncidentEvent


def canonical_incidents():
    return Incident.objects.filter(merged_into__isnull=True)


@transaction.atomic
def observe_incident(key, *, problem, can_resolve=True, **details):
    """Fold observations for one source into its case and emit state changes only."""
    now = timezone.now()
    cases = canonical_incidents().select_for_update()
    if problem:
        incident, created = cases.get_or_create(
            dedupe_key=key, defaults={**details, "last_seen_at": now},
        )
        if created:
            kind = IncidentEvent.Kind.OPENED
        else:
            previous_severity = incident.severity
            incident.occurrence_count += 1
            incident.last_seen_at = now
            for field in ("title", "message", "severity"):
                setattr(incident, field, details[field])
            fields = ["occurrence_count", "last_seen_at", "title", "message", "severity"]
            kind = None
            if incident.status == Incident.Status.RESOLVED:
                incident.status = Incident.Status.OPEN
                incident.resolved_at = incident.acknowledged_at = incident.acknowledged_by = None
                fields.extend(("status", "resolved_at", "acknowledged_at", "acknowledged_by"))
                kind = IncidentEvent.Kind.REOPENED
            elif incident.severity != previous_severity:
                kind = IncidentEvent.Kind.SEVERITY
            incident.save(update_fields=fields)
        if not kind:
            return None
        event = IncidentEvent.objects.create(
            incident=incident, kind=kind, occurred_at=now,
            severity=incident.severity, message=incident.message,
        )
        if kind == IncidentEvent.Kind.SEVERITY and incident.severity != Incident.Severity.CRITICAL:
            return None
        return event
    if not can_resolve:
        return None
    incident = cases.filter(dedupe_key=key).exclude(status=Incident.Status.RESOLVED).first()
    if incident is None:
        return None
    incident.status = Incident.Status.RESOLVED
    incident.resolved_at = now
    incident.save(update_fields=("status", "resolved_at"))
    return IncidentEvent.objects.create(
        incident=incident, kind=IncidentEvent.Kind.RESOLVED, occurred_at=now, severity=incident.severity,
    )
