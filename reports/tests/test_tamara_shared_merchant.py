import json

from django.test import TestCase, override_settings
from django.urls import reverse

from reports.models import Payment
from reports.tests.test_tamara_payments import _notification_token


@override_settings(
    ALLOWED_HOSTS=["testserver"],
    RATELIMIT_ENABLE=False,
    TAMARA_ENABLED=True,
    TAMARA_NOTIFICATION_TOKEN="notification-secret",
)
class SharedMerchantWebhookTests(TestCase):
    def test_other_application_event_is_acknowledged_without_payment(self):
        token = _notification_token("notification-secret")
        payload = {
            "order_id": "11111111-1111-1111-1111-111111111111",
            "order_reference_id": "TM-other-application",
            "event_type": "order_approved",
        }
        response = self.client.post(
            reverse("reports:tamara_webhook"),
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["ignored"], "other_platform")
        self.assertFalse(Payment.objects.exists())

    def test_unknown_own_order_still_returns_not_found(self):
        token = _notification_token("notification-secret")
        payload = {
            "order_id": "22222222-2222-2222-2222-222222222222",
            "order_reference_id": "TWQ-MISSING-ORDER",
            "event_type": "order_approved",
        }
        response = self.client.post(
            reverse("reports:tamara_webhook"),
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 404)
