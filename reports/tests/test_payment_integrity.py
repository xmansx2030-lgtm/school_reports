"""Money paths: the amount is the server's to decide, and paying must activate.

Two properties are load-bearing for a paid product:
  1. Nothing a browser sends can change what a school is charged.
  2. A captured payment activates the school even when the gateway never
     manages to tell us.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from reports.models import (
    ArchiveStorageOption,
    Payment,
    PlatformSettings,
    School,
    SchoolMembership,
    SchoolSubscription,
    SubscriptionPlan,
    Teacher,
)
from reports.pricing import DEFAULT_SUBSCRIPTION_PLANS
from reports.tests.billing_test_helpers import (
    checkout_submission_key,
    valid_receipt,
)


def _receipt():
    return valid_receipt()


@override_settings(ALLOWED_HOSTS=["testserver"], RATELIMIT_ENABLE=False)
class PaymentAmountIntegrityTests(TestCase):
    """The browser picks *what* to buy; the server decides what it costs."""

    def setUp(self):
        for spec in DEFAULT_SUBSCRIPTION_PLANS:
            SubscriptionPlan.objects.create(is_active=True, **spec)

        self.school = School.objects.create(name="مدرسة الدفع", code="pay-school")
        self.plan = SubscriptionPlan.objects.get(days_duration=365, max_teachers=25)
        SchoolSubscription.objects.create(school=self.school, plan=self.plan)
        self.manager = Teacher.objects.create_user(
            phone="500011990", name="مدير الدفع", password="strong-pass-123"
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
        self.submission_key = checkout_submission_key(self.client)

    def _post(self, **extra):
        payload = {
            "unified": "1",
            "include_subscription": "1",
            "plan_id": str(self.plan.id),
            "teacher_capacity": "25",
            "checkout_submission_key": self.submission_key,
            "receipt_image": _receipt(),
        }
        payload.update(extra)
        return self.client.post(reverse("reports:payment_create"), payload)

    def _subscription_payment(self):
        return Payment.objects.get(
            school=self.school, purpose=Payment.Purpose.SUBSCRIPTION
        )

    def test_amount_comes_from_the_catalog_not_the_request(self):
        self._post(amount="1", price="1", total="1")

        self.assertEqual(self._subscription_payment().amount, Decimal("1290.00"))

    def test_a_larger_capacity_is_charged_its_own_price(self):
        """Selecting the 25-seat plan id with 100 seats must cost the 100 price."""
        self._post(teacher_capacity="100")

        payment = self._subscription_payment()
        self.assertEqual(payment.amount, Decimal("2990"))
        self.assertEqual(payment.requested_teacher_limit, 100)

    def test_capacity_cannot_be_pushed_past_the_published_range(self):
        response = self._post(teacher_capacity="5000")

        self.assertEqual(response.status_code, 302)
        self.assertFalse(Payment.objects.filter(school=self.school).exists())

    def test_an_inactive_plan_cannot_be_bought(self):
        hidden = SubscriptionPlan.objects.create(
            name="باقة مخفية", price=1, days_duration=365, max_teachers=25, is_active=False
        )

        self._post(plan_id=str(hidden.id))

        self.assertFalse(Payment.objects.filter(school=self.school).exists())

    def test_a_free_plan_cannot_be_used_to_pay_nothing(self):
        trial = SubscriptionPlan.objects.get(price=0)

        self._post(plan_id=str(trial.id))

        self.assertFalse(Payment.objects.filter(school=self.school).exists())

    def test_capacity_below_the_current_teacher_count_is_refused(self):
        subscription = SchoolSubscription.objects.get(school=self.school)
        subscription.teacher_limit_override = 100
        subscription.save(update_fields=["teacher_limit_override"])
        # The seat check reads school.subscription, which is cached on the
        # instance; re-fetch so it sees the raised limit.
        self.school = School.objects.get(pk=self.school.pk)

        for index in range(30):
            teacher = Teacher.objects.create_user(
                phone=f"5001100{index:02d}", name=f"معلم {index}", password="strong-pass-123"
            )
            SchoolMembership.objects.create(
                school=self.school,
                teacher=teacher,
                role_type=SchoolMembership.RoleType.TEACHER,
            )

        self._post(teacher_capacity="25")

        self.assertFalse(Payment.objects.filter(school=self.school).exists())

    def test_storage_price_comes_from_the_published_option(self):
        option = ArchiveStorageOption.objects.create(
            storage_gb=50, price=Decimal("149.00"), is_active=True
        )

        self.client.post(
            reverse("reports:payment_create"),
            {
                "unified": "1",
                "include_archive_storage": "1",
                "archive_storage_option_id": str(option.id),
                "amount": "1",
                "checkout_submission_key": self.submission_key,
                "receipt_image": _receipt(),
            },
        )

        payment = Payment.objects.get(
            school=self.school, purpose=Payment.Purpose.WORK_STORAGE
        )
        self.assertEqual(payment.amount, Decimal("149.00"))
        self.assertEqual(payment.archive_storage_gb, 50)

    def test_an_inactive_storage_option_is_refused(self):
        option = ArchiveStorageOption.objects.create(
            storage_gb=50, price=Decimal("149.00"), is_active=False
        )

        self.client.post(
            reverse("reports:payment_create"),
            {
                "unified": "1",
                "include_archive_storage": "1",
                "archive_storage_option_id": str(option.id),
                "checkout_submission_key": self.submission_key,
                "receipt_image": _receipt(),
            },
        )

        self.assertFalse(Payment.objects.filter(school=self.school).exists())

    def test_buying_storage_no_longer_requires_the_archive_addon(self):
        option = ArchiveStorageOption.objects.create(
            storage_gb=50, price=Decimal("149.00"), is_active=True
        )

        self.client.post(
            reverse("reports:payment_create"),
            {
                "payment_kind": Payment.Purpose.WORK_STORAGE,
                "archive_storage_option_id": str(option.id),
                "receipt_image": _receipt(),
            },
        )

        self.assertTrue(
            Payment.objects.filter(
                school=self.school, purpose=Payment.Purpose.WORK_STORAGE
            ).exists()
        )


@override_settings(
    ALLOWED_HOSTS=["testserver"],
    RATELIMIT_ENABLE=False,
    MOYASAR_ENABLED=True,
    MOYASAR_ENVIRONMENT="test",
    MOYASAR_SECRET_KEY="sk_test_dummy",
)
class MoyasarActivationTests(TestCase):
    """Paying electronically must activate the subscription — and only for the
    amount actually captured."""

    def setUp(self):
        PlatformSettings.get_solo()
        self.school = School.objects.create(name="مدرسة ميّسر", code="moyasar-school")
        self.plan = SubscriptionPlan.objects.create(
            name="سعة 25 | سنوي", price=Decimal("1290"), days_duration=365, max_teachers=25
        )
        self.manager = Teacher.objects.create_user(
            phone="500012340", name="مدير ميّسر", password="strong-pass-123"
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )
        self.payment = Payment.objects.create(
            school=self.school,
            requested_plan=self.plan,
            requested_teacher_limit=25,
            purpose=Payment.Purpose.SUBSCRIPTION,
            amount=Decimal("1290"),
            payment_method=Payment.Method.MOYASAR,
            batch_ref="batch123",
            gateway_order_id="inv_1",
            gateway_checkout_id="inv_1",
            status=Payment.Status.PENDING,
            created_by=self.manager,
        )

    def _invoice(self, *, status="paid", amount=129000):
        return {
            "id": "inv_1",
            "status": status,
            "currency": "SAR",
            "amount": amount,
            "metadata": {"batch_ref": "batch123", "school_id": str(self.school.id)},
            "payments": [{"id": "pay_1", "status": "paid"}],
        }

    def _callback(self):
        return self.client.post(
            reverse("reports:moyasar_callback", args=["batch123"]),
            data="{}",
            content_type="application/json",
        )

    def test_a_paid_invoice_activates_the_subscription(self):
        with patch("reports.views.billing_gateways.fetch_moyasar_invoice", return_value=self._invoice()):
            response = self._callback()

        self.assertEqual(response.status_code, 200)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.APPROVED)

        subscription = SchoolSubscription.objects.get(school=self.school)
        self.assertTrue(subscription.is_active)
        self.assertFalse(subscription.is_expired)
        self.assertEqual(subscription.teacher_limit, 25)

    def test_a_paid_invoice_emails_the_manager_after_activation(self):
        self.manager.email = "principal@example.com"
        self.manager.save(update_fields=["email"])
        mail.outbox.clear()

        with patch("reports.views.billing_gateways.fetch_moyasar_invoice", return_value=self._invoice()), self.captureOnCommitCallbacks(execute=True):
            response = self._callback()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual(sent.to, ["principal@example.com"])
        self.assertIn("تم تفعيل اشتراك", sent.subject)
        self.assertIn(self.school.name, sent.subject)
        self.assertIn(self.plan.name, sent.body)
        self.assertIn("1290.00 SAR", sent.body)
        self.assertIn(reverse("reports:my_subscription"), sent.body)
        self.assertEqual(len(sent.alternatives), 1)
        html, content_type = sent.alternatives[0]
        self.assertEqual(content_type, "text/html")
        self.assertIn("تم تفعيل الاشتراك", html)
        self.assertIn("إدارة الاشتراك وعرض الفاتورة", html)
        self.assertIn(reverse("reports:subscription_invoice", args=[self.payment.pk]), html)

    def test_an_underpaid_invoice_activates_nothing(self):
        """The gateway amount is re-checked server-side, so a tampered or partial
        capture cannot buy a subscription."""
        with patch(
            "reports.views.billing_gateways.fetch_moyasar_invoice",
            return_value=self._invoice(amount=100),
        ):
            response = self._callback()

        self.assertEqual(response.status_code, 502)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.PENDING)
        self.assertFalse(SchoolSubscription.objects.filter(school=self.school).exists())

    def test_a_failed_invoice_does_not_activate(self):
        with patch(
            "reports.views.billing_gateways.fetch_moyasar_invoice",
            return_value=self._invoice(status="failed"),
        ):
            self._callback()

        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.REJECTED)
        self.assertFalse(SchoolSubscription.objects.filter(school=self.school).exists())

    def test_a_replayed_callback_does_not_extend_the_subscription_twice(self):
        with patch("reports.views.billing_gateways.fetch_moyasar_invoice", return_value=self._invoice()):
            self._callback()
            first_end = SchoolSubscription.objects.get(school=self.school).end_date
            self._callback()

        self.assertEqual(SchoolSubscription.objects.get(school=self.school).end_date, first_end)

    def test_an_unknown_batch_never_reaches_the_gateway(self):
        with patch("reports.views.billing_gateways.fetch_moyasar_invoice") as fetch:
            response = self.client.post(
                reverse("reports:moyasar_callback", args=["nope"]),
                data="{}",
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 502)
        fetch.assert_not_called()


@override_settings(
    ALLOWED_HOSTS=["testserver"],
    RATELIMIT_ENABLE=False,
    MOYASAR_ENABLED=True,
    MOYASAR_ENVIRONMENT="test",
    MOYASAR_SECRET_KEY="sk_test_dummy",
)
class PaymentReconciliationTests(TestCase):
    """A captured payment must activate even if the callback never arrives and
    the customer closes the tab."""

    def setUp(self):
        self.school = School.objects.create(name="مدرسة التسوية", code="reconcile-school")
        self.plan = SubscriptionPlan.objects.create(
            name="سعة 25 | سنوي", price=Decimal("1290"), days_duration=365, max_teachers=25
        )
        self.manager = Teacher.objects.create_user(
            phone="500098760", name="مدير التسوية", password="strong-pass-123"
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )
        self.payment = Payment.objects.create(
            school=self.school,
            requested_plan=self.plan,
            requested_teacher_limit=25,
            purpose=Payment.Purpose.SUBSCRIPTION,
            amount=Decimal("1290"),
            payment_method=Payment.Method.MOYASAR,
            batch_ref="lostbatch",
            gateway_order_id="inv_lost",
            status=Payment.Status.PENDING,
            created_by=self.manager,
        )

    def _run(self):
        from django.core.cache import cache

        from reports.tasks import reconcile_pending_gateway_payments_task

        cache.delete("periodic_lock:reconcile_gateway_payments")
        return reconcile_pending_gateway_payments_task.apply().get()

    def _invoice(self, status="paid"):
        return {
            "id": "inv_lost",
            "status": status,
            "currency": "SAR",
            "amount": 129000,
            "metadata": {"batch_ref": "lostbatch", "school_id": str(self.school.id)},
            "payments": [{"id": "pay_lost", "status": "paid"}],
        }

    def test_a_lost_callback_is_recovered(self):
        with patch("reports.views.billing_gateways.fetch_moyasar_invoice", return_value=self._invoice()):
            summary = self._run()

        self.assertEqual(summary["activated"], 1)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.APPROVED)
        self.assertTrue(SchoolSubscription.objects.get(school=self.school).is_active)

    def test_an_unpaid_invoice_stays_pending(self):
        with patch(
            "reports.views.billing_gateways.fetch_moyasar_invoice",
            return_value=self._invoice(status="initiated"),
        ):
            summary = self._run()

        self.assertEqual(summary["activated"], 0)
        self.assertEqual(summary["still_pending"], 1)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.PENDING)

    def test_reconciling_twice_activates_once(self):
        with patch("reports.views.billing_gateways.fetch_moyasar_invoice", return_value=self._invoice()):
            self._run()
            end_date = SchoolSubscription.objects.get(school=self.school).end_date
            self._run()

        self.assertEqual(SchoolSubscription.objects.get(school=self.school).end_date, end_date)

    def test_stale_payments_are_left_for_manual_review(self):
        Payment.objects.filter(pk=self.payment.pk).update(
            created_at=timezone.now() - timedelta(days=30)
        )

        with patch("reports.views.billing_gateways.fetch_moyasar_invoice") as fetch:
            summary = self._run()

        self.assertEqual(summary["checked"], 0)
        fetch.assert_not_called()

    def test_a_gateway_error_is_counted_and_does_not_crash_the_sweep(self):
        with patch(
            "reports.views.billing_gateways.fetch_moyasar_invoice",
            side_effect=RuntimeError("gateway down"),
        ):
            summary = self._run()

        self.assertEqual(summary["failed"], 1)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.PENDING)

    @staticmethod
    def _recovery_alerts(queue):
        return [
            call.args[0]
            for call in queue.call_args_list
            if str(getattr(call.args[0], "event_key", "")).startswith("payment-recovery:")
        ]

    def test_a_rescued_payment_alerts_the_team(self):
        """The rescue is automatic, but the upstream failure must not be silent."""
        with patch("reports.views.billing_gateways.fetch_moyasar_invoice", return_value=self._invoice()),                 patch("reports.telegram_alerts.queue_telegram_alert") as queue:
            self._run()

        alerts = self._recovery_alerts(queue)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].category, "payments")
        self.assertIn("إنقاذ عملية دفع", alerts[0].text)
        self.assertIn(self.school.name, alerts[0].text)

    def test_no_alert_when_nothing_needed_rescuing(self):
        with patch(
            "reports.views.billing_gateways.fetch_moyasar_invoice",
            return_value=self._invoice(status="initiated"),
        ), patch("reports.telegram_alerts.queue_telegram_alert") as queue:
            self._run()

        self.assertEqual(self._recovery_alerts(queue), [])

    def test_a_rescue_is_announced_once_not_on_every_sweep(self):
        with patch("reports.views.billing_gateways.fetch_moyasar_invoice", return_value=self._invoice()),                 patch("reports.telegram_alerts.queue_telegram_alert") as queue:
            self._run()
            self._run()

        self.assertEqual(len(self._recovery_alerts(queue)), 1)

    def test_a_broken_alert_channel_does_not_undo_the_recovery(self):
        with patch("reports.views.billing_gateways.fetch_moyasar_invoice", return_value=self._invoice()),                 patch(
                    "reports.telegram_alerts.queue_telegram_alert",
                    side_effect=RuntimeError("telegram down"),
                ):
            summary = self._run()

        self.assertEqual(summary["activated"], 1)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.APPROVED)

    def test_the_sweep_is_scheduled(self):
        from django.conf import settings as django_settings

        schedule = getattr(django_settings, "CELERY_BEAT_SCHEDULE", {})

        self.assertIn("reconcile-pending-gateway-payments", schedule)
        self.assertEqual(
            schedule["reconcile-pending-gateway-payments"]["task"],
            "reports.tasks.reconcile_pending_gateway_payments_task",
        )
