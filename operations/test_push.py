from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings

from reports.models import Teacher

from .models import (
    Incident,
    ManagedProject,
    ManagedServer,
    MobileDevice,
    OperationsMembership,
    OperationsPaymentLink,
)
from .push import send_incident_push, send_payment_paid_push


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
        OperationsMembership.objects.create(user=self.user, role=OperationsMembership.Role.ADMIN)
        self.payment_link = OperationsPaymentLink.objects.create(
            project=project,
            customer_name="Private Customer",
            customer_phone="+966500000000",
            customer_email="private@example.com",
            amount="250.00",
            description="Private service details",
            gateway_invoice_id="66666666-6666-6666-6666-666666666666",
            gateway_url="https://checkout.moyasar.com/invoices/private-link",
            status=OperationsPaymentLink.Status.PAID,
            created_by=self.user,
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

    @patch("google.oauth2.service_account.Credentials.from_service_account_file")
    @patch("requests.post")
    @patch.dict(
        "os.environ",
        {"GOOGLE_APPLICATION_CREDENTIALS": "isolated-service-account.json"},
        clear=False,
    )
    def test_payment_paid_push_targets_every_admin_and_excludes_private_data(
        self,
        post: Mock,
        credentials_factory: Mock,
    ):
        second_admin = Teacher.objects.create_user(
            phone="0500000098",
            name="Second Manager",
            password="strong-test-password",  # noqa: S106 - synthetic test credential.
        )
        operator = Teacher.objects.create_user(
            phone="0500000097",
            name="Operator",
            password="strong-test-password",  # noqa: S106 - synthetic test credential.
        )
        OperationsMembership.objects.create(user=second_admin, role=OperationsMembership.Role.ADMIN)
        OperationsMembership.objects.create(user=operator, role=OperationsMembership.Role.OPERATOR)
        self._device("admin-one", "token-admin-one")
        MobileDevice.objects.create(
            user=second_admin,
            device_id="admin-two",
            fcm_token="token-admin-two",  # noqa: S106 - synthetic FCM token.
        )
        MobileDevice.objects.create(
            user=operator,
            device_id="operator",
            fcm_token="token-operator",  # noqa: S106 - synthetic FCM token.
        )
        credentials_factory.return_value = SimpleNamespace(
            token="short-lived-test-token",  # noqa: S106 - synthetic OAuth token.
            refresh=Mock(),
        )
        post.return_value = SimpleNamespace(ok=True, status_code=200, text="")

        result = send_payment_paid_push(self.payment_link)

        self.assertEqual(result, {"sent": 2, "failed": 0, "disabled": 0})
        sent_tokens = {call.kwargs["json"]["message"]["token"] for call in post.call_args_list}
        self.assertEqual(sent_tokens, {"token-admin-one", "token-admin-two"})
        serialized_payloads = " ".join(str(call.kwargs["json"]) for call in post.call_args_list)
        self.assertNotIn(self.payment_link.customer_name, serialized_payloads)
        self.assertNotIn(self.payment_link.customer_phone, serialized_payloads)
        self.assertNotIn(self.payment_link.customer_email, serialized_payloads)
        self.assertNotIn(self.payment_link.gateway_url, serialized_payloads)
