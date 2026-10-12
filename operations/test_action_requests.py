"""Authorization, queue lifecycle and lossless bounded log cursor regressions."""

import json
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .action_requests import log_parameters
from .log_cursors import previous_timestamp
from .models import (
    HostAgentHeartbeat, ManagedProject, ManagedServer, ManagedService,
    MobileAccessToken, OperationAction, OperationsMembership,
)


@override_settings(DEBUG=True)
class ActionRequestTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(phone="0500000491", name="Operations test")
        self.membership = OperationsMembership.objects.create(user=self.user, role=OperationsMembership.Role.ADMIN)
        _, token = MobileAccessToken.issue(user=self.user)
        self.headers = {"HTTP_AUTHORIZATION": f"Ops-Token {token}"}
        server = ManagedServer.objects.create(name="Main", slug="main")
        self.project = ManagedProject.objects.create(server=server, name="مواظبة", slug="mowadhabah", compose_project="mowadhabah")
        self.service = ManagedService.objects.create(project=self.project, name="Backend", service_key="backend", kind="web", restart_allowed=True)
        HostAgentHeartbeat.objects.create(name="primary")
        self.url = reverse("operations:create-action", args=(self.project.pk,))

    def post(self, **data):
        return self.client.post(self.url, data, content_type="application/json", **self.headers)

    def complete(self, action, **result):
        action.status = OperationAction.Status.RUNNING
        action.started_at = timezone.now()
        action.save(update_fields=("status", "started_at"))
        payload = {"id": action.pk, "request_id": str(action.request_id), "status": "succeeded", **result}
        with patch("sys.stdin", StringIO(json.dumps(payload))):
            call_command("operations_agent", "complete", stdout=StringIO())
        action.refresh_from_db()

    def test_diagnostics_no_confirmation_and_viewer_cannot_execute(self):
        self.assertEqual(self.post(action="diagnostics").status_code, 202)
        self.membership.role = OperationsMembership.Role.VIEWER
        self.membership.save(update_fields=("role",))
        self.assertEqual(self.post(action="diagnostics").status_code, 403)

    def test_repeated_backup_uses_same_action_even_if_unrelated_service_id_sent(self):
        first = self.post(action="create_backup", confirmation="mowadhabah")
        second = self.post(action="create_backup", confirmation="mowadhabah", service_id=self.service.pk)
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.json()["id"], first.json()["id"])
        self.assertTrue(second.json()["reused"])
        self.assertEqual(OperationAction.objects.count(), 1)
        self.assertIsNone(OperationAction.objects.get().service_id)

    def test_log_repeat_reuses_but_changed_window_is_explicit_conflict(self):
        first = self.post(action="read_logs", service_id=self.service.pk, since_minutes=30)
        repeated = self.post(action="read_logs", service_id=self.service.pk, since_minutes=30)
        changed = self.post(action="read_logs", service_id=self.service.pk, since_minutes=2880)
        self.assertEqual(repeated.json()["id"], first.json()["id"])
        self.assertEqual(changed.status_code, 409)
        self.assertEqual(OperationAction.objects.count(), 1)

    def test_unknown_project_mapping_cannot_create_host_job(self):
        self.project.compose_project = "other-project"
        self.project.save(update_fields=("compose_project",))
        self.assertEqual(self.post(action="diagnostics").status_code, 403)
        self.assertFalse(OperationAction.objects.exists())

    def test_service_must_belong_to_selected_project(self):
        other = ManagedProject.objects.create(server=self.project.server, name="Other", slug="other")
        service = ManagedService.objects.create(project=other, name="Other", service_key="web")
        self.assertEqual(self.post(action="read_logs", service_id=service.pk).status_code, 400)

    def test_cursor_is_utc_bounded_and_nanoseconds_survive(self):
        now = timezone.now().replace(microsecond=0)
        until = now.strftime("%Y-%m-%dT%H:%M:%S") + ".123456789Z"
        params = log_parameters({"until": until, "since_at": (now - timedelta(hours=48)).isoformat(), "tail": 5000})
        self.assertEqual(params["until"], until)
        self.assertEqual(params["tail"], 1000)
        for invalid in ("--details", "2026-01-01T00:00:00", (now + timedelta(hours=1)).isoformat()):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                log_parameters({"until": invalid})

    def test_long_log_page_cursor_follows_displayed_byte_boundary(self):
        action = OperationAction.objects.create(requested_by=self.user, project=self.project, service=self.service, action="read_logs")
        raw = "\n".join(f"2026-10-12T12:00:00.{n:09d}Z [backend] " + "long message " * 150 for n in range(1000))
        self.complete(action, log_content=raw, log_page={"next_until": "2026-10-12T12:00:00.000000000Z", "has_more": False})
        page = action.log_analysis["page"]
        self.assertGreater(action.log_analysis["omitted_lines"], 800)
        first_timestamp = action.log_content.split(" ", 1)[0]
        self.assertEqual(page["next_until"], previous_timestamp(first_timestamp))
        self.assertEqual(page["earliest_at"], first_timestamp)
        self.assertTrue(page["has_more"])
        self.assertEqual(page["returned_lines"], len(action.log_content.splitlines()))

    def test_logs_permission_checked_when_reading_finished_action(self):
        action = OperationAction.objects.create(requested_by=self.user, project=self.project, action="read_logs", status="succeeded", log_content="sensitive")
        self.membership.role = OperationsMembership.Role.OPERATOR
        self.membership.save(update_fields=("role",))
        response = self.client.get(reverse("operations:action-detail", args=(self.project.pk, action.pk)), **self.headers)
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("sensitive", response.content.decode())

    def test_action_history_pages_have_no_duplicates_or_log_body(self):
        rows = [OperationAction.objects.create(requested_by=self.user, project=self.project, action="read_logs", log_content="private content") for _ in range(3)]
        first = self.client.get(self.url, {"limit": 2}, **self.headers).json()
        second = self.client.get(self.url, {"limit": 2, "before_id": first["next_before_id"]}, **self.headers).json()
        self.assertEqual([item["id"] for item in first["results"] + second["results"]], [row.pk for row in reversed(rows)])
        self.assertNotIn("private content", json.dumps(first))

    def test_claim_expires_old_jobs_preserves_running_backup_and_ignores_web_checks(self):
        old = OperationAction.objects.create(requested_by=self.user, project=self.project, action="diagnostics")
        OperationAction.objects.filter(pk=old.pk).update(requested_at=timezone.now() - timedelta(hours=1))
        backup = OperationAction.objects.create(requested_by=self.user, project=self.project, action="create_backup", status="running", started_at=timezone.now() - timedelta(minutes=20))
        check = OperationAction.objects.create(requested_by=self.user, project=self.project, action="check_now")
        fresh = OperationAction.objects.create(requested_by=self.user, project=self.project, action="diagnostics")
        output = StringIO()
        call_command("operations_agent", "claim", stdout=output)
        self.assertEqual(json.loads(output.getvalue())["id"], fresh.pk)
        old.refresh_from_db()
        backup.refresh_from_db()
        check.refresh_from_db()
        self.assertEqual(old.error_code, "queue_expired")
        self.assertEqual(backup.status, "running")
        self.assertEqual(check.status, "queued")

    def test_idle_poll_refreshes_heartbeat_without_system_checks(self):
        from .management.commands.operations_agent import Command

        HostAgentHeartbeat.objects.update(last_seen_at=timezone.now() - timedelta(minutes=5))
        output = StringIO()
        with patch.object(Command, "check", side_effect=AssertionError("Repeated URL checks")):
            Command(stdout=output).execute(operation="poll", force_color=False, no_color=False)
        self.assertEqual(json.loads(output.getvalue()), {})
        self.assertGreater(HostAgentHeartbeat.objects.get(name="primary").last_seen_at,
                           timezone.now() - timedelta(seconds=5))

    def test_poll_claims_once_and_preserves_request_identity(self):
        action = OperationAction.objects.create(requested_by=self.user, project=self.project, action="diagnostics")
        first, second = StringIO(), StringIO()
        call_command("operations_agent", "poll", stdout=first)
        call_command("operations_agent", "poll", stdout=second)
        self.assertEqual(json.loads(first.getvalue())["id"], action.pk)
        self.assertEqual(json.loads(first.getvalue())["request_id"], str(action.request_id))
        self.assertEqual(json.loads(second.getvalue()), {})
        action.refresh_from_db()
        self.assertEqual(action.status, "running")
