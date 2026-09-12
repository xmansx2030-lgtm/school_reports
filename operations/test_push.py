from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings

from reports.models import Teacher

from .models import Incident, ManagedProject, ManagedServer, MobileDevice
from .push import send_incident_push


@override_settings(FCM_PROJECT_ID="isolated-project")
class IncidentPushContractTests(TestCase):
    def setUp(self):
        self.user = Teacher.objects.create_user(
            phone="0500000099",
            name="Push Operator",
            password="strong-test-password",  # noqa: S106 - synthetic test credential.
        )
        server = ManagedServer.objects.create(name="push-server", slug="push-server")
        project = ManagedProject.objects.create(
            server=server,
            name="Push Project",
            slug="push-project",
        )
        self.incident = Incident.objects.create(
            project=project,
            dedupe_key="push-contract",
            title="تنبيه تشغيلي",
            message="نص الحالة التشغيلي الأصلي",
            severity=Incident.Severity.CRITICAL,
        )

    def _device(self, identifier: str, token: str) -> MobileDevice:
        return MobileDevice.objects.create(
            user=self.user,
            device_id=identifier,
            fcm_token=token,
        )

    @patch.dict("os.environ", {}, clear=True)
    def test_configured_project_without_credentials_disables_delivery(self):
        self._device("disabled", "token-disabled")

        self.assertEqual(
            send_incident_push(self.incident),
            {"sent": 0, "failed": 0, "disabled": 1},
        )

    @patch("google.oauth2.service_account.Credentials.from_service_account_file")
    @patch("requests.post")
    @patch.dict(
        "os.environ",
        {"GOOGLE_APPLICATION_CREDENTIALS": "isolated-service-account.json"},
        clear=False,
    )
    def test_invalid_token_is_disabled_without_corrupting_the_next_payload(
        self,
        post: Mock,
        credentials_factory: Mock,
    ):
        invalid = self._device("invalid", "token-invalid")
        self._device("valid", "token-valid")
        credentials = SimpleNamespace(
            token="short-lived-test-token",  # noqa: S106 - synthetic OAuth token.
            refresh=Mock(),
        )
        credentials_factory.return_value = credentials
        post.side_effect = [
            SimpleNamespace(
                ok=False,
                status_code=404,
                text='{"error":{"status":"UNREGISTERED"}}',
            ),
            SimpleNamespace(ok=True, status_code=200, text=""),
        ]

        result = send_incident_push(self.incident)

        self.assertEqual(result, {"sent": 1, "failed": 1, "disabled": 0})
        invalid.refresh_from_db()
        self.assertFalse(invalid.is_active)
        self.assertEqual(invalid.fcm_token, "")
        first_payload = post.call_args_list[0].kwargs["json"]
        second_payload = post.call_args_list[1].kwargs["json"]
        self.assertEqual(
            first_payload["message"]["notification"]["body"],
            "نص الحالة التشغيلي الأصلي",
        )
        self.assertEqual(
            second_payload["message"]["notification"]["body"],
            "نص الحالة التشغيلي الأصلي",
        )
        self.assertEqual(
            second_payload["message"]["android"]["notification"]["channel_id"],
            "operations_alerts",
        )
        credentials.refresh.assert_called_once()
        self.incident.refresh_from_db()
        self.assertIsNotNone(self.incident.last_notified_at)
