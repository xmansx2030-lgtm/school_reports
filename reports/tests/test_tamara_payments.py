import base64
import hashlib
import hmac
import json
import time
from decimal import Decimal
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse

from reports.models import (
    Payment,
    School,
    SchoolMembership,
    SchoolSubscription,
    SubscriptionPlan,
    Teacher,
)


def _notification_token(secret: str) -> str:
    def encoded(value):
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    header = encoded({"typ": "JWT", "alg": "HS256"})
    payload = encoded({"iss": "Tamara", "exp": int(time.time()) + 300})
    signature = hmac.new(
        secret.encode("utf-8"),
        f"{header}.{payload}".encode("ascii"),
        hashlib.sha256,
    ).digest()
    encoded_signature = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
    return f"{header}.{payload}.{encoded_signature}"


@override_settings(ALLOWED_HOSTS=["testserver"], RATELIMIT_ENABLE=False)
class TamaraPaymentTests(TestCase):
    def setUp(self):
        self.plan = SubscriptionPlan.objects.create(
            name="باقة سنوية",
            price=Decimal("1200.00"),
            days_duration=365,
            max_teachers=50,
        )
        self.school = School.objects.create(
            name="مدرسة تمارا",
            code="tamara-school",
            city="الرياض",
        )
        self.subscription = SchoolSubscription.objects.create(
            school=self.school,
            plan=self.plan,
        )
        self.manager = Teacher.objects.create_user(
            phone="0550111222",
            name="مدير المدرسة",
            password="pass",
            email="manager@example.com",
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )
        self.client.force_login(self.manager)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()
        response = self.client.get(reverse("reports:my_subscription"))
        self.submission_key = response.context["checkout_submission_key"]

    def _checkout_payload(self):
        return {
            "include_subscription": "1",
            "plan_id": str(self.plan.id),
            "tamara_city": "الرياض",
            "tamara_address": "حي الياسمين",
            "checkout_submission_key": self.submission_key,
        }

    @override_settings(TAMARA_ENABLED=False)
    def test_tamara_option_is_hidden_when_disabled(self):
        response = self.client.get(reverse("reports:my_subscription"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'id="tamaraSubmit"')

    @override_settings(
        TAMARA_ENABLED=True,
        TAMARA_ENVIRONMENT="sandbox",
        TAMARA_API_TOKEN="sandbox-api-token",
        TAMARA_NOTIFICATION_TOKEN="sandbox-notification-token",
    )
    def test_tamara_option_is_clear_and_marked_as_sandbox(self):
        response = self.client.get(reverse("reports:my_subscription"))
        self.assertContains(response, 'id="tamaraSubmit"')
        self.assertContains(response, 'id="tamaraInstallmentAmount"')
        self.assertContains(response, 'data-payment-choice="tamara"')
        self.assertContains(response, "img/payment/tamara-wordmark-ar.png")
        self.assertContains(response, "إضافة عنوان فوترة")
        self.assertContains(response, "بيئة اختبار")

    @override_settings(TAMARA_ENABLED=True, TAMARA_API_TOKEN="sandbox-api-token")
    @patch("reports.views.billing_gateways.is_tamara_customer_eligible", return_value=True)
    @patch("reports.views.billing_gateways.create_tamara_checkout")
    def test_checkout_uses_server_price_and_creates_pending_order(
        self, create_checkout_mock, eligibility_mock
    ):
        create_checkout_mock.return_value = {
            "order_id": "11111111-1111-1111-1111-111111111111",
            "checkout_id": "22222222-2222-2222-2222-222222222222",
            "status": "new",
            "checkout_url": "https://checkout.tamara.co/checkout/example",
        }
        response = self.client.post(
            reverse("reports:tamara_checkout_create"), self._checkout_payload()
        )
        self.assertRedirects(
            response,
            "https://checkout.tamara.co/checkout/example",
            fetch_redirect_response=False,
        )
        payment = Payment.objects.get(school=self.school)
        self.assertEqual(payment.payment_method, Payment.Method.TAMARA)
        self.assertEqual(payment.amount, self.plan.price)
        self.assertEqual(payment.gateway_status, "new")
        self.assertTrue(payment.batch_ref)
        sent = create_checkout_mock.call_args.args[0]
        self.assertEqual(sent["total_amount"]["amount"], 1200.0)
        self.assertEqual(sent["billing_address"]["city"], "الرياض")
        self.assertNotIn("shipping_address", sent)
        self.assertTrue(parse_qs(urlparse(sent["merchant_url"]["success"]).query)["state"][0])
        eligibility_mock.assert_called_once_with(
            amount=Decimal("1200.00"),
            phone=self.manager.phone,
            email=self.manager.email,
        )

    @override_settings(TAMARA_ENABLED=True, TAMARA_API_TOKEN="sandbox-api-token")
    @patch("reports.views.billing_gateways.is_tamara_customer_eligible", return_value=True)
    @patch("reports.views.billing_gateways.create_tamara_checkout")
    def test_unsafe_checkout_url_creates_no_local_payment(
        self, create_checkout_mock, _eligibility_mock
    ):
        create_checkout_mock.return_value = {
            "order_id": "11111111-1111-1111-1111-111111111111",
            "checkout_url": "https://eviltamara.co/fake",
        }
        response = self.client.post(
            reverse("reports:tamara_checkout_create"), self._checkout_payload()
        )
        self.assertRedirects(
            response,
            reverse("reports:my_subscription"),
            fetch_redirect_response=False,
        )
        self.assertFalse(Payment.objects.exists())

    @override_settings(TAMARA_ENABLED=True, TAMARA_API_TOKEN="sandbox-api-token")
    @patch("reports.views.billing_gateways.is_tamara_customer_eligible", return_value=True)
    @patch("reports.views.billing_gateways.create_tamara_checkout")
    def test_duplicate_checkout_key_creates_one_tamara_order(
        self, create_checkout_mock, _eligibility_mock
    ):
        create_checkout_mock.return_value = {
            "order_id": "14141414-1414-1414-1414-141414141414",
            "checkout_id": "15151515-1515-1515-1515-151515151515",
            "status": "new",
            "checkout_url": "https://checkout.tamara.co/checkout/idempotent",
        }
        payload = self._checkout_payload()

        self.client.post(reverse("reports:tamara_checkout_create"), payload)
        self.client.post(reverse("reports:tamara_checkout_create"), payload)

        self.assertEqual(Payment.objects.filter(school=self.school).count(), 1)
        self.assertEqual(create_checkout_mock.call_count, 1)

    @override_settings(
        TAMARA_ENABLED=True,
        TAMARA_NOTIFICATION_TOKEN="notification-secret",
    )
    def test_webhook_rejects_invalid_notification_token(self):
        response = self.client.post(
            reverse("reports:tamara_webhook"),
            data=json.dumps({"order_id": "unknown", "event_type": "order_approved"}),
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer invalid",
        )
        self.assertEqual(response.status_code, 401)

    def _pending_payment(self, *, order_id="33333333-3333-3333-3333-333333333333"):
        return Payment.objects.create(
            school=self.school,
            subscription=self.subscription,
            requested_plan=self.plan,
            purpose=Payment.Purpose.SUBSCRIPTION,
            amount=self.plan.price,
            payment_method=Payment.Method.TAMARA,
            gateway_order_id=order_id,
            gateway_status="authorised",
            batch_ref="tamara-paid",
            created_by=self.manager,
        )

    @override_settings(TAMARA_ENABLED=True, TAMARA_API_TOKEN="sandbox-api-token")
    @patch("reports.views.billing_gateways.capture_tamara_order")
    @patch("reports.views.billing_gateways.authorise_tamara_order")
    @patch("reports.views.billing_gateways.get_tamara_order")
    @patch("reports.views.billing_gateways.is_tamara_customer_eligible", return_value=True)
    @patch("reports.views.billing_gateways.create_tamara_checkout")
    def test_success_return_verifies_capture_and_activates_immediately(
        self,
        create_checkout_mock,
        _eligibility_mock,
        get_order_mock,
        authorise_mock,
        capture_mock,
    ):
        order_id = "44444444-4444-4444-4444-444444444444"
        create_checkout_mock.return_value = {
            "order_id": order_id,
            "checkout_id": "55555555-5555-5555-5555-555555555555",
            "status": "new",
            "checkout_url": "https://checkout.tamara.co/checkout/return-test",
        }
        checkout_response = self.client.post(
            reverse("reports:tamara_checkout_create"), self._checkout_payload()
        )
        self.assertEqual(checkout_response.status_code, 302)
        success_url = create_checkout_mock.call_args.args[0]["merchant_url"]["success"]
        payment = Payment.objects.get(gateway_order_id=order_id)
        expected_reference = f"TWQ-{payment.batch_ref.upper()}"
        get_order_mock.return_value = {
            "order_id": order_id,
            "order_reference_id": expected_reference,
            "status": "approved",
            "total_amount": {"amount": 1200, "currency": "SAR"},
        }
        authorise_mock.return_value = {"status": "authorised"}
        capture_mock.return_value = {
            "status": "fully_captured",
            "capture_id": "capture-return",
            "captured_amount": {"amount": 1200, "currency": "SAR"},
        }

        response = self.client.get(success_url, follow=True)

        self.assertContains(response, "تم تحصيل الدفعة عبر تمارا وتفعيل الاشتراك بنجاح")
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.APPROVED)
        self.assertEqual(payment.gateway_capture_id, "capture-return")
        self.assertIsNotNone(payment.effects_applied_at)

    @override_settings(TAMARA_ENABLED=True)
    @patch("reports.views.billing_gateways.get_tamara_order")
    def test_success_return_does_not_activate_an_unpaid_order(self, get_order_mock):
        payment = self._pending_payment(order_id="66666666-6666-6666-6666-666666666666")
        from reports.views.billing_gateways import _tamara_return_url

        request = type("Request", (), {"build_absolute_uri": lambda _self, path: f"http://testserver{path}"})()
        success_url = _tamara_return_url(request, "success", payment.batch_ref)
        get_order_mock.return_value = {
            "order_id": payment.gateway_order_id,
            "order_reference_id": "TWQ-TAMARA-PAID",
            "status": "new",
            "total_amount": {"amount": 1200, "currency": "SAR"},
        }

        response = self.client.get(success_url, follow=True)

        self.assertContains(response, "يجري التحقق من التحصيل")
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.PENDING)
        self.assertIsNone(payment.effects_applied_at)

    @override_settings(TAMARA_ENABLED=True)
    @patch("reports.views.billing_gateways.get_tamara_order")
    def test_return_rejects_gateway_order_reference_mismatch(self, get_order_mock):
        payment = self._pending_payment(order_id="77777777-7777-7777-7777-777777777777")
        from reports.views.billing_gateways import _tamara_return_url

        request = type("Request", (), {"build_absolute_uri": lambda _self, path: f"http://testserver{path}"})()
        success_url = _tamara_return_url(request, "success", payment.batch_ref)
        get_order_mock.return_value = {
            "order_id": payment.gateway_order_id,
            "order_reference_id": "TWQ-SOMEONE-ELSES-ORDER",
            "status": "fully_captured",
            "total_amount": {"amount": 1200, "currency": "SAR"},
        }

        self.client.get(success_url)

        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.PENDING)
        self.assertIsNone(payment.effects_applied_at)

    @override_settings(
        TAMARA_ENABLED=True,
        TAMARA_NOTIFICATION_TOKEN="notification-secret",
    )
    def test_captured_webhook_fulfils_order_once(self):
        payment = self._pending_payment()
        payload = {
            "order_id": payment.gateway_order_id,
            "order_reference_id": "TWQ-TAMARA-PAID",
            "event_type": "order_captured",
            "data": {
                "capture_id": "capture-1",
                "captured_amount": {"amount": 1200, "currency": "SAR"},
            },
        }
        token = _notification_token("notification-secret")
        url = reverse("reports:tamara_webhook")
        first = self.client.post(
            url,
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        first_end_date = SchoolSubscription.objects.get(pk=self.subscription.pk).end_date
        second = self.client.post(
            url,
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        payment.refresh_from_db()
        self.subscription.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.APPROVED)
        self.assertEqual(payment.gateway_status, "fully_captured")
        self.assertEqual(payment.gateway_capture_id, "capture-1")
        self.assertIsNotNone(payment.effects_applied_at)
        self.assertEqual(self.subscription.end_date, first_end_date)

    @override_settings(
        TAMARA_ENABLED=True,
        TAMARA_NOTIFICATION_TOKEN="notification-secret",
    )
    def test_capture_amount_mismatch_does_not_activate(self):
        payment = self._pending_payment()
        payload = {
            "order_id": payment.gateway_order_id,
            "order_reference_id": "TWQ-TAMARA-PAID",
            "event_type": "order_captured",
            "data": {
                "capture_id": "capture-short",
                "captured_amount": {"amount": 100, "currency": "SAR"},
            },
        }
        response = self.client.post(
            reverse("reports:tamara_webhook"),
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {_notification_token('notification-secret')}",
        )
        self.assertEqual(response.status_code, 502)
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.PENDING)
        self.assertIsNone(payment.effects_applied_at)

    @override_settings(TAMARA_ENABLED=True)
    def test_platform_admin_cannot_approve_uncaptured_tamara_payment(self):
        payment = self._pending_payment()
        admin = Teacher.objects.create_superuser(
            phone="0550999888", name="مدير المنصة", password="pass"
        )
        self.client.force_login(admin)
        response = self.client.post(
            reverse("reports:platform_payment_detail", args=[payment.id]),
            {"status": Payment.Status.APPROVED},
        )
        self.assertEqual(response.status_code, 302)
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.PENDING)
        self.assertIsNone(payment.effects_applied_at)
