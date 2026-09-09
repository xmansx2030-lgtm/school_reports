import tempfile
from unittest.mock import patch

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from reports.forms import NotificationCreateForm
from reports.models import (
    Notification,
    NotificationRecipient,
    School,
    SchoolMembership,
    SchoolSubscription,
    SubscriptionPlan,
    Teacher,
)


@override_settings(
    ALLOWED_HOSTS=["testserver"],
    CELERY_BROKER_URL="",
    NOTIFICATIONS_LOCAL_FALLBACK_ENABLED=False,
)
class NewsletterExperienceTests(TestCase):
    def setUp(self):
        self._media_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._media_directory.cleanup)
        self._media_override = override_settings(MEDIA_ROOT=self._media_directory.name)
        self._media_override.enable()
        self.addCleanup(self._media_override.disable)

        self.school = School.objects.create(name="مدرسة النشرات", code="newsletter-school")
        plan = SubscriptionPlan.objects.create(
            name="باقة النشرات",
            price=0,
            days_duration=30,
            max_teachers=10,
        )
        SchoolSubscription.objects.create(school=self.school, plan=plan)
        self.manager = Teacher.objects.create_user(
            phone="500880001",
            name="مدير النشرات",
            password="pass",
            is_staff=True,
        )
        self.teacher = Teacher.objects.create_user(
            phone="0500880002",
            name="مستلم النشرة",
            password="pass",
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.teacher,
            role_type=SchoolMembership.RoleType.TEACHER,
        )

    def _newsletter_form(self, *, requires_signature: bool):
        data = {
            "communication_type": "newsletter",
            "title": "نشرة الأسبوع",
            "message": "أبرز أخبار المدرسة لهذا الأسبوع.",
            "teachers": [str(self.teacher.pk)],
        }
        if requires_signature:
            data["requires_signature"] = "on"
            data["signature_ack_text"] = "أقر بالاطلاع على النشرة."
        return NotificationCreateForm(
            data=data,
            files={
                "attachment": SimpleUploadedFile(
                    "newsletter.pdf",
                    b"%PDF-1.4\n% newsletter test\n",
                    content_type="application/pdf",
                )
            },
            user=self.manager,
            active_school=self.school,
            mode="notification",
        )

    def _login(self, user):
        self.client.force_login(user)
        session = self.client.session
        session["active_school_id"] = self.school.pk
        session.save()

    def test_newsletter_can_be_sent_with_or_without_signature(self):
        unsigned_form = self._newsletter_form(requires_signature=False)
        self.assertTrue(unsigned_form.is_valid(), unsigned_form.errors.as_data())
        unsigned = unsigned_form.save(creator=self.manager, default_school=self.school)

        signed_form = self._newsletter_form(requires_signature=True)
        self.assertTrue(signed_form.is_valid(), signed_form.errors.as_data())
        signed = signed_form.save(creator=self.manager, default_school=self.school)

        self.assertEqual(unsigned.kind, Notification.Kind.NEWSLETTER)
        self.assertFalse(unsigned.requires_signature)
        self.assertTrue(bool(unsigned.attachment))
        self.assertEqual(signed.kind, Notification.Kind.NEWSLETTER)
        self.assertTrue(signed.requires_signature)
        self.assertEqual(signed.signature_ack_text, "أقر بالاطلاع على النشرة.")
        self.assertEqual(
            set(signed.recipients.values_list("teacher_id", flat=True)),
            {self.teacher.pk},
        )

    def test_create_screen_has_clear_notification_newsletter_switch(self):
        self._login(self.manager)

        response = self.client.get(reverse("reports:notifications_create"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ماذا تريد أن ترسل؟")
        self.assertContains(response, "نشرة")
        self.assertContains(response, 'id="newsletterOptions"', html=False)
        self.assertContains(response, "طلب توقيع المستلمين")
        self.assertContains(response, "إرسال النشرة")

    def test_signed_newsletter_stays_out_of_circular_lists(self):
        newsletter = Notification.objects.create(
            school=self.school,
            created_by=self.manager,
            kind=Notification.Kind.NEWSLETTER,
            title="نشرة موقعة",
            message="نص النشرة",
            requires_signature=True,
        )
        circular = Notification.objects.create(
            school=self.school,
            created_by=self.manager,
            kind=Notification.Kind.CIRCULAR,
            title="تعميم رسمي",
            message="نص التعميم",
            requires_signature=True,
        )
        NotificationRecipient.objects.create(notification=newsletter, teacher=self.teacher)
        NotificationRecipient.objects.create(notification=circular, teacher=self.teacher)
        self._login(self.manager)

        newsletter_sent = self.client.get(
            f"{reverse('reports:notifications_sent')}?kind=newsletter"
        )
        circular_sent = self.client.get(reverse("reports:circulars_sent"))

        self.assertContains(newsletter_sent, newsletter.title)
        self.assertNotContains(newsletter_sent, circular.title)
        self.assertContains(circular_sent, circular.title)
        self.assertNotContains(circular_sent, newsletter.title)

    def test_signed_newsletter_has_recipient_signature_and_print_report(self):
        newsletter = Notification.objects.create(
            school=self.school,
            created_by=self.manager,
            kind=Notification.Kind.NEWSLETTER,
            title="نشرة تتطلب التوقيع",
            message="مقدمة النشرة",
            requires_signature=True,
            signature_ack_text="أقر بالاطلاع.",
        )
        recipient = NotificationRecipient.objects.create(
            notification=newsletter,
            teacher=self.teacher,
        )

        self._login(self.teacher)
        detail = self.client.get(
            reverse("reports:my_notification_detail", args=[recipient.pk])
        )
        self.assertEqual(detail.status_code, 200)
        self.assertTemplateUsed(detail, "reports/my_circular_detail.html")
        self.assertContains(detail, "الاطلاع على النشرة")
        self.assertContains(detail, "اعتماد التوقيع نهائيًا")
        self.assertContains(detail, "NWS-")

        self._login(self.manager)
        report = self.client.get(
            reverse("reports:notification_signatures_print", args=[newsletter.pk])
        )
        self.assertEqual(report.status_code, 200)
        self.assertContains(report, "سجل تواقيع النشرة")
        self.assertContains(report, "NWS-")

    def test_unsigned_newsletter_has_read_receipt_but_no_signature_report(self):
        newsletter = Notification.objects.create(
            school=self.school,
            created_by=self.manager,
            kind=Notification.Kind.NEWSLETTER,
            title="نشرة للاطلاع",
            message="مقدمة النشرة",
            requires_signature=False,
        )
        recipient = NotificationRecipient.objects.create(
            notification=newsletter,
            teacher=self.teacher,
        )

        self._login(self.teacher)
        detail = self.client.get(
            reverse("reports:my_notification_detail", args=[recipient.pk])
        )
        recipient.refresh_from_db()
        self.assertTrue(recipient.is_read)
        self.assertContains(detail, "هذه النشرة لا تتطلب توقيعًا")
        self.assertNotContains(detail, "اعتماد التوقيع نهائيًا")

        self._login(self.manager)
        report = self.client.get(
            reverse("reports:notification_signatures_print", args=[newsletter.pk])
        )
        self.assertEqual(report.status_code, 302)

    def test_signed_newsletter_uses_notification_badge_not_circular_badge(self):
        newsletter = Notification.objects.create(
            school=self.school,
            created_by=self.manager,
            kind=Notification.Kind.NEWSLETTER,
            title="نشرة معلقة",
            message="تحتاج توقيعًا",
            requires_signature=True,
        )
        newsletter_recipient = NotificationRecipient.objects.create(
            notification=newsletter,
            teacher=self.teacher,
            is_read=True,
        )
        circular = Notification.objects.create(
            school=self.school,
            created_by=self.manager,
            kind=Notification.Kind.CIRCULAR,
            title="تعميم معلق",
            message="يحتاج توقيعًا",
            requires_signature=True,
        )
        NotificationRecipient.objects.create(
            notification=circular,
            teacher=self.teacher,
            is_read=True,
        )
        self._login(self.teacher)

        response = self.client.get(reverse("reports:unread_notifications_count"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["unread"], 1)
        self.assertEqual(response.json()["signatures_pending"], 1)
        self.assertEqual(response.json()["count"], 2)

        newsletter_recipient.is_signed = True
        newsletter_recipient.save(update_fields=["is_signed"])
        cache.clear()
        response = self.client.get(reverse("reports:unread_notifications_count"))
        self.assertEqual(response.json()["unread"], 0)
        self.assertEqual(response.json()["signatures_pending"], 1)

    @patch("reports.signals.push_delta_to_user")
    def test_signed_newsletter_realtime_delta_stays_on_notification_badge(self, push_delta):
        newsletter = Notification.objects.create(
            school=self.school,
            created_by=self.manager,
            kind=Notification.Kind.NEWSLETTER,
            title="نشرة لحظية",
            message="تحتاج توقيعًا",
            requires_signature=True,
        )

        recipient = NotificationRecipient.objects.create(
            notification=newsletter,
            teacher=self.teacher,
        )

        push_delta.assert_called_once()
        self.assertEqual(push_delta.call_args.kwargs["delta_unread"], 1)
        self.assertNotIn("delta_signatures_pending", push_delta.call_args.kwargs)

        push_delta.reset_mock()
        recipient.is_read = True
        recipient.save(update_fields=["is_read"])
        push_delta.assert_not_called()

        recipient.is_signed = True
        recipient.save(update_fields=["is_signed"])
        push_delta.assert_called_once()
        self.assertEqual(push_delta.call_args.kwargs["delta_unread"], -1)
        self.assertEqual(push_delta.call_args.kwargs["delta_signatures_pending"], 0)
