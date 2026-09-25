from decimal import Decimal
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


@override_settings(ALLOWED_HOSTS=["testserver"], RATELIMIT_ENABLE=False)
class MoyasarPaymentTests(TestCase):
    def setUp(self):
        self.plan = SubscriptionPlan.objects.create(
            name="باقة سنوية",
            price=Decimal("1200.00"),
            days_duration=365,
            max_teachers=50,
        )
        self.school = School.objects.create(
            name="مدرسة ميّسر",
            code="moyasar-school",
            city="الرياض",
        )
        self.subscription = SchoolSubscription.objects.create(
            school=self.school,
            plan=self.plan,
        )
        self.manager = Teacher.objects.create_user(
            phone="0550222333",
            name="مدير المدرسة",
            password="pass",
            email="moyasar-manager@example.com",
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
            "checkout_submission_key": self.submission_key,
        }

    @override_settings(MOYASAR_ENABLED=False)
    def test_moyasar_option_is_hidden_when_disabled(self):
        response = self.client.get(reverse("reports:my_subscription"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'id="moyasarSubmit"')

    @override_settings(
        MOYASAR_ENABLED=True,
        MOYASAR_ENVIRONMENT="test",
        MOYASAR_SECRET_KEY="sk_test_example",
        TAMARA_ENABLED=False,
    )
    def test_moyasar_option_is_marked_as_a_test_environment(self):
        response = self.client.get(reverse("reports:my_subscription"))

        self.assertContains(response, 'id="moyasarSubmit"')
        self.assertContains(response, 'data-payment-choice="moyasar"')
        self.assertContains(response, "الدفع الإلكتروني")
        self.assertNotContains(response, "Apple Pay")
        self.assertNotContains(response, "Samsung Pay")
        self.assertNotContains(response, "الدفع عبر ميّسر")
        self.assertContains(response, "بيئة اختبار")
        for asset in ("img/payment/mada.svg", "img/payment/visa.svg", "img/payment/mastercard.svg"):
            self.assertContains(response, asset)
        self.assertNotContains(response, "img/payment/tamara-wordmark-ar.png")

    @override_settings(
        MOYASAR_ENABLED=True,
        MOYASAR_ENVIRONMENT="live",
        MOYASAR_SECRET_KEY="sk_live_example",
    )
    def test_checkout_exposes_mobile_summary_legal_terms_and_accessible_fields(self):
        Payment.objects.create(
            school=self.school,
            subscription=self.subscription,
            requested_plan=self.plan,
            purpose=Payment.Purpose.SUBSCRIPTION,
            amount=self.plan.price,
            payment_method=Payment.Method.MOYASAR,
            created_by=self.manager,
        )
        response = self.client.get(reverse("reports:my_subscription"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="mobileCheckoutBar"')
        self.assertContains(response, 'id="mobileSelectedCount"')
        self.assertContains(response, 'id="mobileOrderTotal"')
        self.assertContains(response, 'id="mobileCheckoutContinue"')
        self.assertContains(response, 'for="bankTransferNotes"')
        self.assertContains(response, 'id="bankTransferNotes"')
        self.assertContains(response, 'data-label="التاريخ"')
        self.assertContains(response, 'data-label="العملية"')
        self.assertContains(response, 'data-label="المبلغ"')
        self.assertContains(response, 'data-label="الحالة"')
        self.assertContains(response, 'data-label="ملاحظات"')
        self.assertContains(response, "لا توجد ضريبة قيمة مضافة أو رسوم خفية")
        self.assertContains(response, reverse("reports:terms_conditions"))
        self.assertContains(response, reverse("reports:refund_policy"))
        self.assertContains(response, "سيتم تفعيل الباقة تلقائيًا بعد نجاح عملية الدفع")
        self.assertContains(response, "دون مراجعة يدوية")
        self.assertContains(response, "خلال يوم عمل واحد كحد أقصى")
        self.assertNotContains(response, "فريق الدعم يراجع كل دفعة قبل التفعيل")
        for asset in (
            "img/payment/mada.svg",
            "img/payment/visa.svg",
            "img/payment/mastercard.svg",
        ):
            self.assertContains(response, asset)
        self.assertNotContains(response, "img/payment/apple-pay.svg")
        self.assertNotContains(response, "img/payment/samsung-pay.svg")

    @override_settings(
        MOYASAR_ENABLED=True,
        MOYASAR_ENVIRONMENT="test",
        MOYASAR_SECRET_KEY="sk_test_example",
    )
    @patch("reports.views.billing_gateways.create_moyasar_invoice")
    def test_checkout_uses_server_price_and_creates_pending_invoice_payment(
        self, create_invoice_mock
    ):
        create_invoice_mock.return_value = {
            "id": "11111111-1111-1111-1111-111111111111",
            "status": "initiated",
            "url": "https://checkout.moyasar.com/invoices/example",
        }

        response = self.client.post(
            reverse("reports:moyasar_checkout_create"),
            self._checkout_payload(),
        )

        self.assertRedirects(
            response,
            "https://checkout.moyasar.com/invoices/example?lang=ar",
            fetch_redirect_response=False,
        )
        payment = Payment.objects.get(school=self.school)
        self.assertEqual(payment.payment_method, Payment.Method.MOYASAR)
        self.assertEqual(payment.status, Payment.Status.PENDING)
        self.assertEqual(payment.amount, self.plan.price)
        self.assertEqual(payment.gateway_status, "initiated")
        self.assertTrue(payment.batch_ref)
        self.assertFalse(payment.receipt_image)
        sent = create_invoice_mock.call_args.kwargs
        self.assertEqual(sent["amount"], Decimal("1200.00"))
        self.assertEqual(sent["metadata"]["batch_ref"], payment.batch_ref)
        self.assertEqual(sent["metadata"]["school_id"], str(self.school.id))

    @override_settings(
        MOYASAR_ENABLED=True,
        MOYASAR_ENVIRONMENT="test",
        MOYASAR_SECRET_KEY="sk_test_example",
    )
    @patch("reports.views.billing_gateways.create_moyasar_invoice")
    def test_unsafe_checkout_url_creates_no_local_payment(self, create_invoice_mock):
        create_invoice_mock.return_value = {
            "id": "11111111-1111-1111-1111-111111111111",
            "status": "initiated",
            "url": "https://evil-moyasar.example/invoices/example",
        }

        response = self.client.post(
            reverse("reports:moyasar_checkout_create"),
            self._checkout_payload(),
        )

        self.assertRedirects(
            response,
            reverse("reports:my_subscription"),
            fetch_redirect_response=False,
        )
        self.assertFalse(Payment.objects.exists())

    @override_settings(
        MOYASAR_ENABLED=True,
        MOYASAR_ENVIRONMENT="test",
        MOYASAR_SECRET_KEY="sk_test_example",
    )
    @patch("reports.views.billing_gateways.create_moyasar_invoice")
    def test_duplicate_checkout_key_creates_one_gateway_invoice(
        self, create_invoice_mock
    ):
        create_invoice_mock.return_value = {
            "id": "12121212-1212-1212-1212-121212121212",
            "status": "initiated",
            "url": "https://checkout.moyasar.com/invoices/idempotent",
        }
        payload = self._checkout_payload()

        self.client.post(reverse("reports:moyasar_checkout_create"), payload)
        self.client.post(reverse("reports:moyasar_checkout_create"), payload)

        self.assertEqual(Payment.objects.filter(school=self.school).count(), 1)
        self.assertEqual(create_invoice_mock.call_count, 1)

    @override_settings(
        MOYASAR_ENABLED=True,
        MOYASAR_ENVIRONMENT="test",
        MOYASAR_SECRET_KEY="sk_test_example",
    )
    @patch("reports.views.billing_gateways.create_moyasar_invoice")
    def test_same_checkout_key_rejects_changed_server_quote(self, create_invoice_mock):
        create_invoice_mock.return_value = {
            "id": "13131313-1313-1313-1313-131313131313",
            "status": "initiated",
            "url": "https://checkout.moyasar.com/invoices/fingerprint",
        }
        other_plan = SubscriptionPlan.objects.create(
            name="باقة نصف سنوية",
            price=Decimal("700.00"),
            days_duration=180,
            max_teachers=50,
        )
        self.client.post(
            reverse("reports:moyasar_checkout_create"), self._checkout_payload()
        )
        changed = self._checkout_payload()
        changed["plan_id"] = str(other_plan.pk)

        self.client.post(reverse("reports:moyasar_checkout_create"), changed)

        self.assertEqual(Payment.objects.filter(school=self.school).count(), 1)
        self.assertEqual(create_invoice_mock.call_count, 1)

    @override_settings(
        MOYASAR_ENABLED=True,
        MOYASAR_ENVIRONMENT="test",
        MOYASAR_SECRET_KEY="sk_test_example",
    )
    @patch("reports.views.billing_gateways.fetch_moyasar_invoice")
    def test_paid_callback_fulfils_invoice_once(self, fetch_invoice_mock):
        invoice_id = "22222222-2222-2222-2222-222222222222"
        batch_ref = "moyasar-paid"
        payment = Payment.objects.create(
            school=self.school,
            subscription=self.subscription,
            requested_plan=self.plan,
            purpose=Payment.Purpose.SUBSCRIPTION,
            amount=self.plan.price,
            payment_method=Payment.Method.MOYASAR,
            gateway_order_id=invoice_id,
            gateway_checkout_id=invoice_id,
            gateway_status="initiated",
            batch_ref=batch_ref,
            created_by=self.manager,
        )
        fetch_invoice_mock.return_value = {
            "id": invoice_id,
            "status": "paid",
            "amount": 120000,
            "currency": "SAR",
            "metadata": {"batch_ref": batch_ref},
            "payments": [{"id": "payment-attempt-1", "status": "paid"}],
        }
        self.client.logout()
        url = reverse("reports:moyasar_callback", args=[batch_ref])

        first = self.client.post(url, data={})
        first_end_date = SchoolSubscription.objects.get(pk=self.subscription.pk).end_date
        second = self.client.post(url, data={})

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        payment.refresh_from_db()
        self.subscription.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.APPROVED)
        self.assertEqual(payment.gateway_status, "paid")
        self.assertEqual(payment.gateway_capture_id, "payment-attempt-1")
        self.assertIsNotNone(payment.effects_applied_at)
        self.assertEqual(self.subscription.end_date, first_end_date)

    @override_settings(
        MOYASAR_ENABLED=True,
        MOYASAR_ENVIRONMENT="test",
        MOYASAR_SECRET_KEY="sk_test_example",
    )
    @patch("reports.views.billing_gateways.fetch_moyasar_invoice")
    def test_callback_rejects_amount_mismatch(self, fetch_invoice_mock):
        invoice_id = "33333333-3333-3333-3333-333333333333"
        batch_ref = "moyasar-mismatch"
        payment = Payment.objects.create(
            school=self.school,
            subscription=self.subscription,
            requested_plan=self.plan,
            purpose=Payment.Purpose.SUBSCRIPTION,
            amount=self.plan.price,
            payment_method=Payment.Method.MOYASAR,
            gateway_order_id=invoice_id,
            gateway_status="initiated",
            batch_ref=batch_ref,
            created_by=self.manager,
        )
        fetch_invoice_mock.return_value = {
            "id": invoice_id,
            "status": "paid",
            "amount": 100,
            "currency": "SAR",
            "metadata": {"batch_ref": batch_ref},
            "payments": [{"id": "payment-attempt-2", "status": "paid"}],
        }

        response = self.client.post(
            reverse("reports:moyasar_callback", args=[batch_ref]),
            data={},
        )

        self.assertEqual(response.status_code, 502)
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.PENDING)
        self.assertIsNone(payment.effects_applied_at)

    @override_settings(MOYASAR_ENABLED=True)
    def test_platform_admin_cannot_approve_unpaid_moyasar_payment(self):
        payment = Payment.objects.create(
            school=self.school,
            subscription=self.subscription,
            requested_plan=self.plan,
            purpose=Payment.Purpose.SUBSCRIPTION,
            amount=self.plan.price,
            payment_method=Payment.Method.MOYASAR,
            gateway_order_id="44444444-4444-4444-4444-444444444444",
            gateway_status="initiated",
            batch_ref="moyasar-manual-block",
            created_by=self.manager,
        )
        admin = Teacher.objects.create_superuser(
            phone="0550999777",
            name="مدير المنصة",
            password="pass",
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
