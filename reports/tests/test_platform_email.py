import base64
import hashlib
import hmac
import json
import time
from datetime import timedelta
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from reports.models import (
    AuditLog,
    PlatformEmail,
    PlatformEmailAttachment,
    PlatformEmailConfiguration,
    PlatformEmailEvent,
    School,
    Teacher,
)


WEBHOOK_KEY = b"platform-email-test-secret-32bytes"
WEBHOOK_SECRET = "whsec_" + base64.b64encode(WEBHOOK_KEY).decode()


def _signed_headers(payload: bytes, event_id: str = "evt_test_001") -> dict:
    timestamp = str(int(time.time()))
    signed = b".".join((event_id.encode(), timestamp.encode(), payload))
    signature = base64.b64encode(hmac.new(WEBHOOK_KEY, signed, hashlib.sha256).digest()).decode()
    return {
        "HTTP_SVIX_ID": event_id,
        "HTTP_SVIX_TIMESTAMP": timestamp,
        "HTTP_SVIX_SIGNATURE": f"v1,{signature}",
    }


@override_settings(
    ALLOWED_HOSTS=["testserver"],
    RESEND_API_KEY="re_test_key",
    RESEND_WEBHOOK_SECRET=WEBHOOK_SECRET,
)
class PlatformEmailTests(TestCase):
    def setUp(self):
        self.admin = Teacher.objects.create_superuser(
            phone="599760001",
            name="مدير بريد المنصة",
            password="pass",
        )
        self.user = Teacher.objects.create_user(
            phone="599760002",
            name="مستخدم عادي",
            password="pass",
        )
        self.config = PlatformEmailConfiguration.load()
        self.config.is_sending_enabled = True
        self.config.is_receiving_enabled = True
        self.config.save()

    def _inbound(self, **overrides):
        values = {
            "provider_id": "inbound_001",
            "direction": PlatformEmail.Direction.INBOUND,
            "status": PlatformEmail.Status.RECEIVED,
            "from_email": "customer@example.com",
            "from_name": "عميل المنصة",
            "to_emails": [self.config.inbound_email],
            "subject": "طلب مساعدة",
            "text_body": "أحتاج المساعدة في الاشتراك.",
            "snippet": "أحتاج المساعدة في الاشتراك.",
            "is_read": False,
        }
        values.update(overrides)
        return PlatformEmail.objects.create(**values)

    def test_mailbox_is_restricted_to_superuser(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("reports:platform_email_inbox"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("reports:platform_login"), response.url)

    def test_inbox_has_operational_counts_search_and_folders(self):
        inbound = self._inbound()
        PlatformEmail.objects.create(
            provider_id="outbound_001",
            direction=PlatformEmail.Direction.OUTBOUND,
            status=PlatformEmail.Status.DELIVERED,
            from_email=self.config.sender_email,
            to_emails=["manager@example.com"],
            subject="تأكيد الاشتراك",
            snippet="تم تفعيل الاشتراك.",
            is_read=True,
        )
        self.client.force_login(self.admin)

        response = self.client.get(reverse("reports:platform_email_inbox"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["mail_stats"]["unread"], 1)
        self.assertContains(response, inbound.subject)
        self.assertContains(response, "جاهزية الخدمة")
        sent = self.client.get(reverse("reports:platform_email_inbox"), {"folder": "sent", "q": "manager"})
        self.assertContains(sent, "تأكيد الاشتراك")
        self.assertNotContains(sent, inbound.subject)

    def test_sent_folder_hides_other_project_outbound_messages(self):
        PlatformEmail.objects.create(
            provider_id="tawtheeq_sent_001",
            direction=PlatformEmail.Direction.OUTBOUND,
            status=PlatformEmail.Status.DELIVERED,
            from_email=self.config.sender_email,
            to_emails=["customer@example.com"],
            subject="رسالة توثيق رسمية",
            snippet="رسالة من توثيق.",
            is_read=True,
        )
        PlatformEmail.objects.create(
            provider_id="foreign_sent_001",
            direction=PlatformEmail.Direction.OUTBOUND,
            status=PlatformEmail.Status.DELIVERED,
            from_email="no-reply@school-display.com",
            to_emails=["xmansx1122@gmail.com"],
            subject="اختبار Resend الإنتاج - School Display",
            snippet="رسالة من مشروع آخر.",
            is_read=True,
        )
        self.client.force_login(self.admin)

        response = self.client.get(reverse("reports:platform_email_inbox"), {"folder": "sent"})

        self.assertContains(response, "رسالة توثيق رسمية")
        self.assertNotContains(response, "School Display")
        self.assertEqual(response.context["mail_stats"]["sent"], 1)

    @patch("reports.resend_email._api_request")
    def test_compose_sends_through_resend_and_writes_audit(self, api_request):
        api_request.return_value = {"id": "resend_sent_001"}
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse("reports:platform_email_compose"),
            {
                "to": "first@example.com, second@example.com",
                "cc": "copy@example.com",
                "bcc": "",
                "subject": "رسالة تشغيلية",
                "body": "مرحبًا،\nهذه رسالة من منصة توثيق.",
            },
            REMOTE_ADDR="198.51.100.22",
            HTTP_X_FORWARDED_FOR="203.0.113.15",
        )

        email = PlatformEmail.objects.get(provider_id="resend_sent_001")
        self.assertRedirects(response, reverse("reports:platform_email_detail", args=[email.pk]))
        self.assertEqual(email.status, PlatformEmail.Status.SENT)
        self.assertEqual(email.to_emails, ["first@example.com", "second@example.com"])
        self.assertEqual(email.reply_to_emails, [self.config.reply_to_email])
        audit = AuditLog.objects.get(model_name="PlatformEmail", object_id=email.pk)
        self.assertEqual(audit.ip_address, "198.51.100.22")
        sent_payload = api_request.call_args.kwargs["payload"]
        self.assertEqual(sent_payload["from"], f"{self.config.sender_name} <{self.config.sender_email}>")
        self.assertIn("منصة توثيق", sent_payload["html"])
        self.assertIn("رسالة تشغيلية", sent_payload["html"])
        self.assertIn("مركز الاتصال الرسمي", sent_payload["html"])
        self.assertIn(self.config.reply_to_email, sent_payload["html"])
        self.assertNotIn("<script", sent_payload["html"])

    @patch("reports.resend_email._api_request")
    def test_compose_can_send_to_selected_schools_and_manual_recipients(self, api_request):
        api_request.return_value = {"id": "resend_school_recipients_001"}
        first_school = School.objects.create(
            name="مدرسة الندى",
            code="nada-school",
            email="nada@example.com",
        )
        second_school = School.objects.create(
            name="مدرسة البيان",
            code="bayan-school",
            email="bayan@example.com",
        )
        School.objects.create(
            name="مدرسة بلا بريد",
            code="no-email-school",
            email="",
        )
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse("reports:platform_email_compose"),
            {
                "selected_schools": [str(first_school.pk), str(second_school.pk)],
                "to": "extra@example.com, nada@example.com",
                "cc": "",
                "bcc": "",
                "subject": "تحديث للمدارس",
                "body": "مرحبًا، هذه رسالة موحدة للمدارس المختارة.",
            },
        )

        email = PlatformEmail.objects.get(provider_id="resend_school_recipients_001")
        self.assertRedirects(response, reverse("reports:platform_email_detail", args=[email.pk]))
        self.assertEqual(email.to_emails, ["bayan@example.com", "nada@example.com", "extra@example.com"])
        self.assertEqual(
            api_request.call_args.kwargs["payload"]["to"],
            ["bayan@example.com", "nada@example.com", "extra@example.com"],
        )

        get_response = self.client.get(reverse("reports:platform_email_compose"))
        school_choices = get_response.context["form"].fields["selected_schools"].queryset
        self.assertIn(first_school, school_choices)
        self.assertNotIn(School.objects.get(code="no-email-school"), school_choices)

    def test_registration_manager_email_becomes_school_email_for_mail_center(self):
        response = self.client.post(
            reverse("reports:register_school"),
            {
                "school_name": "مدرسة مركز البريد",
                "stage": School.Stage.PRIMARY,
                "gender": School.Gender.BOYS,
                "city": "الرياض",
                "manager_name": "مدير مركز البريد",
                "manager_phone": "0557778888",
                "manager_email": "mail-center-manager@example.edu.sa",
                "password": "MailCenter#2026",
                "password_confirm": "MailCenter#2026",
                "accept_policies": "on",
            },
        )

        self.assertRedirects(
            response,
            reverse("reports:registration_success"),
            fetch_redirect_response=False,
        )
        school = School.objects.get(name="مدرسة مركز البريد")
        self.assertEqual(school.email, "mail-center-manager@example.edu.sa")

        self.client.force_login(self.admin)
        compose = self.client.get(reverse("reports:platform_email_compose"))
        school_choices = compose.context["form"].fields["selected_schools"].queryset
        self.assertIn(school, school_choices)
        self.assertContains(compose, "مدرسة مركز البريد")
        self.assertContains(compose, "mail-center-manager@example.edu.sa")

    @patch("reports.resend_email._api_request")
    def test_compose_requires_school_or_manual_recipient(self, api_request):
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse("reports:platform_email_compose"),
            {
                "selected_schools": [],
                "to": "",
                "cc": "",
                "bcc": "",
                "subject": "رسالة بدون مستلم",
                "body": "لن ترسل هذه الرسالة دون مستلم.",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "اختر مدرسة لديها بريد إلكتروني أو اكتب بريد مستلم واحدًا على الأقل.")
        api_request.assert_not_called()

    @patch("reports.resend_email._api_request")
    def test_compose_escapes_untrusted_body_inside_branded_template(self, api_request):
        api_request.return_value = {"id": "resend_safe_html_001"}
        self.client.force_login(self.admin)

        self.client.post(
            reverse("reports:platform_email_compose"),
            {
                "to": "recipient@example.com",
                "cc": "",
                "bcc": "",
                "subject": "تنبيه مهم",
                "body": '<script>alert("x")</script>\nتم الاستلام.',
            },
        )

        html = api_request.call_args.kwargs["payload"]["html"]
        self.assertNotIn("<script", html)
        self.assertIn("&lt;script&gt;", html)

    @patch("reports.resend_email._api_request")
    def test_compose_validates_and_sends_attachment(self, api_request):
        api_request.return_value = {"id": "resend_attachment_001"}
        self.client.force_login(self.admin)
        upload = SimpleUploadedFile("guide.pdf", b"%PDF-test", content_type="application/pdf")

        response = self.client.post(
            reverse("reports:platform_email_compose"),
            {
                "to": "recipient@example.com",
                "cc": "",
                "bcc": "",
                "subject": "دليل الاستخدام",
                "body": "الدليل مرفق.",
                "attachments": upload,
            },
        )

        self.assertEqual(response.status_code, 302)
        email = PlatformEmail.objects.get(provider_id="resend_attachment_001")
        self.assertTrue(email.attachments.filter(filename="guide.pdf").exists())
        self.assertEqual(api_request.call_args.kwargs["payload"]["attachments"][0]["filename"], "guide.pdf")

    def test_opening_message_marks_it_read_and_actions_are_audited(self):
        email = self._inbound()
        self.client.force_login(self.admin)

        detail = self.client.get(reverse("reports:platform_email_detail", args=[email.pk]))
        email.refresh_from_db()
        self.assertEqual(detail.status_code, 200)
        self.assertTrue(email.is_read)

        action = self.client.post(
            reverse("reports:platform_email_action", args=[email.pk]),
            {"action": "star"},
        )
        email.refresh_from_db()
        self.assertEqual(action.status_code, 302)
        self.assertTrue(email.is_starred)
        self.assertTrue(AuditLog.objects.filter(model_name="PlatformEmail", object_id=email.pk).exists())

    def test_message_action_rejects_external_next_url(self):
        email = self._inbound()
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse("reports:platform_email_action", args=[email.pk]),
            {"action": "star", "next": "https://attacker.example/redirect"},
        )

        self.assertRedirects(
            response,
            reverse("reports:platform_email_detail", args=[email.pk]),
        )

    @patch("reports.resend_email._api_request")
    def test_reply_keeps_thread_and_reply_headers(self, api_request):
        api_request.return_value = {"id": "resend_reply_001"}
        inbound = self._inbound(message_id="<customer-message@example.com>")
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse("reports:platform_email_detail", args=[inbound.pk]),
            {"action": "reply", "body": "تم استلام طلبكم وسيتم التواصل."},
        )

        reply = PlatformEmail.objects.get(provider_id="resend_reply_001")
        self.assertRedirects(response, reverse("reports:platform_email_detail", args=[reply.pk]))
        self.assertEqual(reply.parent, inbound)
        self.assertEqual(reply.thread_key, inbound.thread_key)
        self.assertEqual(reply.to_emails, [inbound.from_email])
        self.assertEqual(
            api_request.call_args.kwargs["payload"]["headers"]["In-Reply-To"],
            inbound.message_id,
        )

    def test_settings_update_non_secret_identity_only(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("reports:platform_email_settings"),
            {
                "sender_name": "توثيق لخدمات المدارس",
                "sender_email": "notifications@tawtheeq-ksa.com",
                "inbound_email": "support@mail.tawtheeq-ksa.com",
                "reply_to_email": "support@mail.tawtheeq-ksa.com",
                "is_sending_enabled": "on",
                "is_receiving_enabled": "on",
                "retention_days": "730",
            },
        )
        self.assertRedirects(response, reverse("reports:platform_email_settings"))
        self.config.refresh_from_db()
        self.assertEqual(self.config.sender_name, "توثيق لخدمات المدارس")
        self.assertEqual(self.config.retention_days, 730)

    def test_webhook_rejects_invalid_signature(self):
        response = self.client.post(
            reverse("reports:resend_webhook"),
            data=json.dumps({"type": "email.sent"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    @patch("reports.resend_email._api_request")
    def test_signed_received_webhook_fetches_body_and_is_idempotent(self, api_request):
        event = {
            "type": "email.received",
            "created_at": "2026-08-13T10:00:00Z",
            "data": {
                "email_id": "received_001",
                "from": "customer@example.com",
                "to": ["support@mail.tawtheeq-ksa.com"],
                "subject": "استفسار جديد",
            },
        }
        api_request.return_value = {
            **event["data"],
            "id": "received_001",
            "created_at": event["created_at"],
            "text": "هذه هي الرسالة الكاملة.",
            "html": "<p>هذه هي الرسالة الكاملة.</p>",
            "headers": {"from": "عميل جديد <customer@example.com>"},
            "attachments": [
                {
                    "id": "attachment_001",
                    "filename": "invoice.pdf",
                    "content_type": "application/pdf",
                    "size": 1200,
                }
            ],
        }
        payload = json.dumps(event).encode()
        headers = _signed_headers(payload)

        first = self.client.post(
            reverse("reports:resend_webhook"),
            data=payload,
            content_type="application/json",
            **headers,
        )
        second = self.client.post(
            reverse("reports:resend_webhook"),
            data=payload,
            content_type="application/json",
            **headers,
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        email = PlatformEmail.objects.get(provider_id="received_001")
        self.assertEqual(email.from_name, "عميل جديد")
        self.assertEqual(email.text_body, "هذه هي الرسالة الكاملة.")
        self.assertEqual(email.attachments.get().filename, "invoice.pdf")
        self.assertEqual(PlatformEmailEvent.objects.filter(provider_event_id="evt_test_001").count(), 1)
        self.assertEqual(api_request.call_count, 1)

    def test_delivery_webhook_updates_outbound_status(self):
        email = PlatformEmail.objects.create(
            provider_id="sent_for_delivery",
            direction=PlatformEmail.Direction.OUTBOUND,
            status=PlatformEmail.Status.SENT,
            from_email=self.config.sender_email,
            to_emails=["recipient@example.com"],
            subject="حالة التسليم",
        )
        event = {
            "type": "email.delivered",
            "created_at": "2026-08-13T11:00:00Z",
            "data": {"email_id": email.provider_id},
        }
        payload = json.dumps(event).encode()
        response = self.client.post(
            reverse("reports:resend_webhook"),
            data=payload,
            content_type="application/json",
            **_signed_headers(payload, "evt_delivered_001"),
        )
        email.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(email.status, PlatformEmail.Status.DELIVERED)
        self.assertIsNotNone(email.delivered_at)

    def test_webhook_ignores_unknown_outbound_events_from_other_projects(self):
        event = {
            "type": "email.delivered",
            "created_at": "2026-08-13T12:00:00Z",
            "data": {
                "email_id": "foreign_resend_sent_001",
                "from": "School Display <no-reply@school-display.com>",
                "to": ["xmansx1122@gmail.com"],
                "subject": "اختبار Resend الإنتاج - School Display",
            },
        }
        payload = json.dumps(event).encode()

        response = self.client.post(
            reverse("reports:resend_webhook"),
            data=payload,
            content_type="application/json",
            **_signed_headers(payload, "evt_foreign_outbound_001"),
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(PlatformEmail.objects.filter(provider_id="foreign_resend_sent_001").exists())
        event_record = PlatformEmailEvent.objects.get(provider_event_id="evt_foreign_outbound_001")
        self.assertIsNone(event_record.email)

    @patch("reports.resend_email._api_request")
    def test_webhook_ignores_inbound_events_for_other_project_addresses(self, api_request):
        event = {
            "type": "email.received",
            "created_at": "2026-08-13T12:30:00Z",
            "data": {
                "email_id": "foreign_inbound_001",
                "from": "visitor@example.com",
                "to": ["support@xmansx.com"],
                "subject": "رسالة لمشروع آخر",
            },
        }
        payload = json.dumps(event).encode()

        response = self.client.post(
            reverse("reports:resend_webhook"),
            data=payload,
            content_type="application/json",
            **_signed_headers(payload, "evt_foreign_inbound_001"),
        )

        self.assertEqual(response.status_code, 200)
        api_request.assert_not_called()
        self.assertFalse(PlatformEmail.objects.filter(provider_id="foreign_inbound_001").exists())
        event_record = PlatformEmailEvent.objects.get(provider_event_id="evt_foreign_inbound_001")
        self.assertIsNone(event_record.email)

    @patch("reports.resend_email._api_request")
    def test_inbound_refresh_preserves_read_state(self, api_request):
        email = self._inbound(is_read=True)
        api_request.return_value = {
            "id": email.provider_id,
            "from": email.from_email,
            "to": email.to_emails,
            "subject": email.subject,
            "text": "محتوى محدّث من مزود البريد.",
            "headers": {},
            "attachments": [],
        }

        from reports.resend_email import ingest_received_email

        ingest_received_email(email.provider_id)

        email.refresh_from_db()
        self.assertTrue(email.is_read)
        self.assertEqual(email.text_body, "محتوى محدّث من مزود البريد.")

    @patch("reports.tasks._periodic_lock", return_value=True)
    def test_retention_deletes_only_old_archived_mail(self, _lock):
        from reports.tasks import cleanup_platform_email_task

        self.config.retention_days = 30
        self.config.save(update_fields=("retention_days", "updated_at"))
        archived = self._inbound(provider_id="old_archived", is_archived=True)
        active = self._inbound(provider_id="old_active", is_archived=False)
        old = timezone.now() - timedelta(days=31)
        PlatformEmail.objects.filter(pk__in=[archived.pk, active.pk]).update(updated_at=old)

        cleanup_platform_email_task.run()

        self.assertFalse(PlatformEmail.objects.filter(pk=archived.pk).exists())
        self.assertTrue(PlatformEmail.objects.filter(pk=active.pk).exists())

    def test_dashboard_exposes_mail_center_and_unread_count(self):
        self._inbound()
        self.client.force_login(self.admin)
        response = self.client.get(reverse("reports:platform_admin_dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["platform_email_unread"], 1)
        self.assertContains(response, reverse("reports:platform_email_inbox"))
        self.assertContains(response, "بريد المنصة")

    @patch("reports.resend_email._api_request")
    def test_inbound_attachment_download_redirects_to_fresh_signed_url(self, api_request):
        email = self._inbound()
        attachment = PlatformEmailAttachment.objects.create(
            email=email,
            provider_id="att_download_001",
            filename="document.pdf",
        )
        api_request.return_value = {"download_url": "https://inbound-cdn.resend.com/signed-file"}
        self.client.force_login(self.admin)
        response = self.client.get(
            reverse("reports:platform_email_attachment_download", args=[email.pk, attachment.pk])
        )
        self.assertRedirects(response, "https://inbound-cdn.resend.com/signed-file", fetch_redirect_response=False)
