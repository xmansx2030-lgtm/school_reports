from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.test import TestCase, override_settings

from reports.models import (
    CustomerComplaint,
    Payment,
    School,
    SchoolSubscription,
    SubscriptionPlan,
    Teacher,
    Ticket,
)
from reports.telegram_alerts import (
    build_customer_complaint_alert,
    build_payment_alert,
    build_school_registration_alert,
    build_support_ticket_alert,
    deliver_telegram_alert,
)


TELEGRAM_SETTINGS = {
    "TELEGRAM_ALERTS_ENABLED": True,
    "TELEGRAM_BOT_TOKEN": "test-token",
    "TELEGRAM_ALERT_CHAT_ID": "-1001234567890",
    "TELEGRAM_ALERT_CATEGORIES": {
        "support",
        "subscriptions",
        "registration",
        "payments",
        "complaints",
    },
    "SITE_URL": "https://tawtheeq-ksa.com",
}


@override_settings(**TELEGRAM_SETTINGS)
class TelegramAlertTests(TestCase):
    def setUp(self):
        cache.clear()
        self.school = School.objects.create(
            name="مدرسة الاختبار",
            code="telegram-test-school",
            phone="0501234567",
        )
        self.plan = SubscriptionPlan.objects.create(
            name="الباقة المهنية",
            price=500,
            days_duration=365,
            max_teachers=100,
        )
        self.subscription = SchoolSubscription.objects.create(
            school=self.school,
            plan=self.plan,
        )
        self.manager = Teacher.objects.create_user(
            phone="0509876543",
            name="مدير المدرسة",
            password="secret-password",
        )

    def test_telegram_task_name_is_stable(self):
        from reports.task_names import SEND_TELEGRAM_ALERT_TASK
        from reports.tasks import send_telegram_alert_task

        self.assertEqual(send_telegram_alert_task.name, SEND_TELEGRAM_ALERT_TASK)

    def test_alert_builders_never_include_phone_password_body_or_receipt(self):
        registration = build_school_registration_alert(self.school)
        ticket = Ticket.objects.create(
            school=self.school,
            creator=self.manager,
            title="عنوان قد يحتوي بيانات حساسة",
            body="تفاصيل شديدة الحساسية",
            is_platform=True,
        )
        support = build_support_ticket_alert(ticket)
        payment = Payment.objects.create(
            school=self.school,
            subscription=self.subscription,
            requested_plan=self.plan,
            amount=500,
            purpose=Payment.Purpose.SUBSCRIPTION,
            created_by=self.manager,
            notes="ملاحظة مالية خاصة",
        )
        payment_alert = build_payment_alert(payment, created=True)
        combined = "\n".join([registration.text, support.text, payment_alert.text])

        self.assertNotIn("0501234567", combined)
        self.assertNotIn("0509876543", combined)
        self.assertNotIn("secret-password", combined)
        self.assertNotIn("تفاصيل شديدة الحساسية", combined)
        self.assertNotIn("عنوان قد يحتوي بيانات حساسة", combined)
        self.assertNotIn("ملاحظة مالية خاصة", combined)
        self.assertNotIn("500", payment_alert.text)

    def test_school_creation_is_queued_only_after_commit(self):
        with patch("reports.telegram_alerts.enqueue_named_task") as mocked:
            with self.captureOnCommitCallbacks(execute=True):
                school = School.objects.create(
                    name="مدرسة تسجيل جديدة",
                    code="new-telegram-school",
                )

        mocked.assert_called_once()
        self.assertEqual(
            mocked.call_args.args,
            ("reports.tasks.send_telegram_alert_task",),
        )
        self.assertEqual(mocked.call_args.kwargs["queue"], "notifications")
        payload = mocked.call_args.kwargs["args"][0]
        self.assertEqual(payload["category"], "registration")
        self.assertIn(str(school.pk), payload["event_key"])
        self.assertNotIn("phone", json.dumps(payload))

    def test_customer_complaint_is_queued_without_personal_details(self):
        with patch("reports.telegram_alerts.enqueue_named_task") as mocked:
            with self.captureOnCommitCallbacks(execute=True):
                complaint = CustomerComplaint.objects.create(
                    name="اسم خاص",
                    email="private@example.com",
                    phone="0501112233",
                    subject="موضوع خاص",
                    message="تفاصيل خاصة لا ترسل إلى تيليجرام",
                )

        mocked.assert_called_once()
        payload = mocked.call_args.kwargs["args"][0]
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertEqual(payload["category"], "complaints")
        self.assertIn(complaint.reference, payload["text"])
        self.assertIn(
            f"/platform/complaints/{complaint.pk}/",
            payload["action_url"],
        )
        self.assertNotIn("اسم خاص", serialized)
        self.assertNotIn("private@example.com", serialized)
        self.assertNotIn("0501112233", serialized)
        self.assertNotIn("موضوع خاص", serialized)
        self.assertNotIn("تفاصيل خاصة", serialized)

    def test_customer_complaint_builder_uses_tracking_data_only(self):
        complaint = CustomerComplaint.objects.create(
            name="بيانات حساسة",
            email="sensitive@example.com",
            subject="عنوان حساس",
            message="نص حساس",
        )
        alert = build_customer_complaint_alert(complaint)

        self.assertEqual(alert.category, "complaints")
        self.assertIn(complaint.reference, alert.text)
        self.assertNotIn(complaint.name, alert.text)
        self.assertNotIn(complaint.email, alert.text)
        self.assertNotIn(complaint.subject, alert.text)
        self.assertNotIn(complaint.message, alert.text)

    def test_payment_status_change_queues_one_status_alert(self):
        payment = Payment.objects.create(
            school=self.school,
            subscription=self.subscription,
            requested_plan=self.plan,
            amount=500,
            purpose=Payment.Purpose.SUBSCRIPTION,
            created_by=self.manager,
        )

        with patch("reports.telegram_alerts.enqueue_named_task") as mocked:
            with self.captureOnCommitCallbacks(execute=True):
                payment.status = Payment.Status.APPROVED
                payment.save(update_fields=["status", "updated_at"])

        mocked.assert_called_once()
        payload = mocked.call_args.kwargs["args"][0]
        self.assertEqual(payload["category"], "payments")
        self.assertIn("status:approved", payload["event_key"])

    @patch("reports.telegram_alerts.urlrequest.urlopen")
    def test_delivery_is_deduplicated_and_uses_admin_button(self, mocked_urlopen):
        response = MagicMock()
        response.status = 200
        response.read.return_value = b'{"ok": true}'
        mocked_urlopen.return_value.__enter__.return_value = response
        payload = {
            "event_key": "registration:school:999",
            "category": "registration",
            "text": "🟢 <b>اختبار</b>",
            "action_url": "https://tawtheeq-ksa.com/platform/schools/",
        }

        self.assertEqual(deliver_telegram_alert(payload), "sent")
        self.assertEqual(deliver_telegram_alert(payload), "duplicate")
        self.assertEqual(mocked_urlopen.call_count, 1)

        request_obj = mocked_urlopen.call_args.args[0]
        body = json.loads(request_obj.data.decode("utf-8"))
        self.assertEqual(body["chat_id"], "-1001234567890")
        self.assertEqual(
            body["reply_markup"]["inline_keyboard"][0][0]["text"],
            "فتح في لوحة الإدارة",
        )
