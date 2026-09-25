"""DB boundary for the host-only, fixed-action executor."""

from __future__ import annotations

import json
import sys
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from operations.log_analysis import sanitize_and_analyze
from operations.models import HostAgentHeartbeat, OperationAction


class Command(BaseCommand):
    help = "Heartbeat, claim, or complete one host operation. For the host runner only."

    def add_arguments(self, parser):
        parser.add_argument("operation", choices=("heartbeat", "claim", "complete"))

    def handle(self, *args, **options):
        operation = options["operation"]
        if operation == "heartbeat":
            HostAgentHeartbeat.objects.update_or_create(
                name="primary", defaults={"last_seen_at": timezone.now()}
            )
            self.stdout.write('{"ready":true}')
            return
        if operation == "claim":
            with transaction.atomic():
                # A crashed runner must not leave an action advertised as in progress forever.
                cutoff = timezone.now() - timedelta(minutes=90)
                OperationAction.objects.filter(
                    status=OperationAction.Status.RUNNING, started_at__lt=cutoff
                ).update(
                    status=OperationAction.Status.FAILED,
                    error_code="runner_timeout",
                    result_summary="انتهت مهلة وكيل الخادم قبل تأكيد النتيجة.",
                    finished_at=timezone.now(),
                )
                action = (
                    OperationAction.objects.select_for_update()
                    .select_related("project", "service")
                    .filter(status=OperationAction.Status.QUEUED)
                    .order_by("requested_at", "pk")
                    .first()
                )
                if action is None:
                    self.stdout.write("{}")
                    return
                action.status = OperationAction.Status.RUNNING
                action.started_at = timezone.now()
                action.result_summary = "قيد التنفيذ على الخادم."
                action.save(update_fields=("status", "started_at", "result_summary"))
                payload = {
                    "id": action.pk,
                    "request_id": action.request_id,
                    "action": action.action,
                    "project_slug": action.project.slug,
                    "compose_project": action.project.compose_project,
                    "service_key": action.service.service_key if action.service else "",
                    "service_kind": action.service.kind if action.service else "",
                    "parameters": action.parameters,
                }
            self.stdout.write(json.dumps(payload, separators=(",", ":")))
            return
        try:
            payload = json.load(sys.stdin)
            action_id = int(payload["id"])
            request_id = str(payload["request_id"])
            outcome = str(payload["status"])
            if outcome not in {"succeeded", "failed"}:
                raise ValueError("Invalid outcome")
            with transaction.atomic():
                action = OperationAction.objects.select_for_update().get(
                    pk=action_id, request_id=request_id, status=OperationAction.Status.RUNNING
                )
                action.status = outcome
                action.error_code = str(payload.get("error_code") or "")[:80]
                action.result_summary = str(payload.get("summary") or "")[:500]
                action.finished_at = timezone.now()
                fields = ["status", "error_code", "result_summary", "finished_at"]
                if action.action == OperationAction.Action.READ_LOGS:
                    action.log_content, action.log_analysis = sanitize_and_analyze(
                        str(payload.get("log_content") or "")[:100000]
                    )
                    fields.extend(("log_content", "log_analysis"))
                action.save(update_fields=fields)
        except (KeyError, TypeError, ValueError, OperationAction.DoesNotExist) as exc:
            raise CommandError("Invalid or stale host action result") from exc
        self.stdout.write('{"completed":true}')
