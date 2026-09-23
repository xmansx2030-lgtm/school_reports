"""Recipient circular detail presentation and unchanged workflow contracts."""

from datetime import timedelta
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from reports.models import (
    Notification,
    NotificationRecipient,
    School,
    SchoolMembership,
    SchoolSubscription,
    SubscriptionPlan,
    Teacher,
)


@override_settings(ALLOWED_HOSTS=["testserver"])
class MyCircularDetailUiTests(TestCase):
    def setUp(self):
        media_directory = tempfile.TemporaryDirectory()
        self.addCleanup(media_directory.cleanup)
        media_override = override_settings(MEDIA_ROOT=media_directory.name)
        media_override.enable()
        self.addCleanup(media_override.disable)

        self.school = School.objects.create(name="مدرسة الوثائق", code="detail-ui-school")
        plan = SubscriptionPlan.objects.create(
            name="خطة اختبار التفاصيل", price=0, days_duration=30, max_teachers=0
        )
        SchoolSubscription.objects.create(school=self.school, plan=plan)
        self.manager = Teacher.objects.create_user(
            phone="500400200", name="مدير الوثائق", password="pass", is_staff=True
        )
        self.recipient = Teacher.objects.create_user(
            phone="500400201", name="مستلم الوثائق", password="pass"
        )
        self.outsider = Teacher.objects.create_user(
            phone="500400202", name="خارج المدرسة", password="pass"
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.recipient,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        self.client.force_login(self.recipient)
        session = self.client.session
        session["active_school_id"] = self.school.pk
        session.save()

    def _recipient(self, **notification_kwargs):
        notification = Notification.objects.create(
            kind=Notification.Kind.CIRCULAR,
            title="تعميم تشغيل تجريبي",
            message="يرجى قراءة النص والمرفق قبل الإقرار.",
            school=self.school,
            created_by=self.manager,
            **notification_kwargs,
        )
        return NotificationRecipient.objects.create(
            notification=notification, teacher=self.recipient
        )

    def test_read_only_document_marks_read_without_acknowledgement(self):
        recipient = self._recipient()

        response = self.client.get(reverse("reports:my_circular_detail", args=[recipient.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "reports/my_circular_detail.html")
        self.assertContains(response, "css/circular-detail.css")
        self.assertContains(response, "twq-page-header")
        self.assertContains(response, "تعميم تشغيل تجريبي")
        self.assertContains(response, "سُجلت كمقروءة")
        self.assertContains(response, reverse("reports:my_circulars"))
        self.assertContains(response, 'id="printCircular"')
        self.assertNotContains(response, 'id="sigForm"')
        recipient.refresh_from_db()
        self.assertTrue(recipient.is_read)
        self.assertIsNotNone(recipient.read_at)
        self.assertFalse(recipient.is_signed)

    def test_pending_acknowledgement_keeps_post_contract_and_attachment(self):
        recipient = self._recipient(
            requires_signature=True,
            signature_deadline_at=timezone.now() + timedelta(days=1),
            attachment=SimpleUploadedFile(
                "official.pdf", b"%PDF-1.4\n%%EOF\n", content_type="application/pdf"
            ),
        )

        response = self.client.get(reverse("reports:my_circular_detail", args=[recipient.pk]))

        self.assertContains(response, "بانتظار إقرارك")
        self.assertContains(response, "مرفق التعميم")
        self.assertContains(response, recipient.notification.attachment.url)
        self.assertContains(response, 'class="twq-document-item circular-detail__attachment"')
        self.assertContains(response, 'id="sigForm"')
        self.assertContains(response, reverse("reports:circular_sign", args=[recipient.pk]))
        self.assertContains(response, 'name="ack"')
        self.assertContains(response, 'name="signature_data"')
        self.assertContains(response, 'id="sigSubmit"')

    def test_signed_and_expired_states_do_not_offer_signing(self):
        signed = self._recipient(requires_signature=True)
        signed.is_signed = True
        signed.signed_at = timezone.now()
        signed.save(update_fields=["is_signed", "signed_at"])
        signed_response = self.client.get(
            reverse("reports:my_circular_detail", args=[signed.pk])
        )
        self.assertContains(signed_response, "اكتمل إقرارك")
        self.assertContains(signed_response, reverse("reports:circular_receipt", args=[signed.pk]))
        self.assertNotContains(signed_response, 'id="sigForm"')

        expired = self._recipient(
            requires_signature=True,
            signature_deadline_at=timezone.now() - timedelta(days=1),
        )
        expired_response = self.client.get(
            reverse("reports:my_circular_detail", args=[expired.pk])
        )
        self.assertContains(expired_response, "أُغلق الإقرار")
        self.assertNotContains(expired_response, 'id="sigForm"')

    def test_other_user_cannot_see_recipient_document(self):
        recipient = self._recipient()
        self.client.force_login(self.outsider)

        response = self.client.get(reverse("reports:my_circular_detail", args=[recipient.pk]))

        self.assertNotEqual(response.status_code, 200)
        self.assertNotIn("تعميم تشغيل تجريبي", response.content.decode())
