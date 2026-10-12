"""DB boundary for the host-only, fixed-action executor."""

from __future__ import annotations

import json
import re
import sys
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from operations.log_analysis import sanitize_and_analyze
from operations.log_cursors import previous_timestamp
from operations.models import HostAgentHeartbeat, OperationAction


class Command(BaseCommand):
    help = "Heartbeat, claim, or complete one host operation. For the host runner only."
    # CI and deployment run the full checks. Loading every URL/view/PDF module
    # for each minute's DB-only poll is expensive and provides no new safety.
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("operation", choices=("heartbeat", "claim", "poll", "complete"))

    def handle(self, *args, **options):
        operation = options["operation"]
        if operation in {"heartbeat", "poll"}:
            HostAgentHeartbeat.objects.update_or_create(
                name="primary", defaults={"last_seen_at": timezone.now()}
            )
            if operation == "heartbeat":
                self.stdout.write('{"ready":true}')
                return
        if operation in {"claim", "poll"}:
            with transaction.atomic():
                now = timezone.now()
                OperationAction.objects.filter(
                    status=OperationAction.Status.QUEUED,
                    requested_at__lt=now - timedelta(minutes=30),
                ).update(
                    status=OperationAction.Status.FAILED, error_code="queue_expired",
                    result_summary="انتهت صلاحية الطلب قبل بدء التنفيذ؛ أعد طلب الإجراء إذا بقي مطلوبًا.",
                    finished_at=now,
                )
                # A crashed runner must not leave an action advertised as in progress forever.
                cutoff = now - timedelta(minutes=90)
                OperationAction.objects.filter(
                    status=OperationAction.Status.RUNNING, started_at__lt=cutoff
                ).update(
                    status=OperationAction.Status.FAILED,
                    error_code="runner_timeout",
                    result_summary="انتهت مهلة وكيل الخادم قبل تأكيد النتيجة.",
                    finished_at=timezone.now(),
                )
                OperationAction.objects.filter(
                    status=OperationAction.Status.RUNNING,
                    started_at__lt=now - timedelta(minutes=10),
                ).exclude(action=OperationAction.Action.CREATE_BACKUP).update(
                    status=OperationAction.Status.FAILED, error_code="runner_timeout",
                    result_summary="انتهت مهلة الإجراء قبل تأكيد النتيجة؛ تحقق من الحالة قبل تكراره.",
                    finished_at=now,
                )
                action = (
                    OperationAction.objects.select_for_update()
                    # service is nullable. Joining it here makes PostgreSQL reject
                    # SELECT FOR UPDATE on the nullable side of the outer join.
                    .select_related("project")
                    .filter(status=OperationAction.Status.QUEUED)
                    .exclude(action=OperationAction.Action.CHECK_NOW)
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
                        str(payload.get("log_content") or "")
                    )
                    page = payload.get("log_page")
                    if isinstance(page, dict):
                        action.log_analysis["page"] = {
                            key: value for key, value in page.items()
                            if key in {"since_at", "until", "since_minutes", "requested_tail", "returned_lines",
                                       "earliest_at", "latest_at", "has_more", "truncated", "next_until", "retention_note",
                                       "boundary_collision", "omitted_container_count"}
                            and isinstance(value, (str, int, bool))
                        }
                        action.log_analysis["page"]["truncated"] = bool(page.get("truncated")) or action.log_analysis["truncated"]
                        action.log_analysis["page"]["returned_lines"] = action.log_analysis["lines"]
                        if action.log_analysis.get("omitted_lines"):
                            stamps = re.findall(r"(?m)^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z)\s", action.log_content)
                            # The next page must start at the displayed boundary,
                            # not at lines removed by the storage byte limit.
                            action.log_analysis["page"].update({
                                "has_more": bool(stamps), "next_until": previous_timestamp(stamps[0]) if stamps else "",
                                "earliest_at": stamps[0] if stamps else "",
                            })
                    fields.extend(("log_content", "log_analysis"))
                elif action.action == OperationAction.Action.DIAGNOSTICS:
                    diagnostic = payload.get("diagnostics")
                    action.log_analysis = {"diagnostics": diagnostic if isinstance(diagnostic, dict) else {}}
                    fields.append("log_analysis")
                action.save(update_fields=fields)
        except (KeyError, TypeError, ValueError, OperationAction.DoesNotExist) as exc:
            raise CommandError("Invalid or stale host action result") from exc
        self.stdout.write('{"completed":true}')
