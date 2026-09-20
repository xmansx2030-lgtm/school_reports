import base64
import tempfile
from datetime import timedelta
from io import BytesIO

from PIL import Image, ImageDraw

from django.core.exceptions import ValidationError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from reports.circular_evidence import acknowledgement_digest
from reports.models import (
    Notification,
    NotificationRecipient,
    School,
    SchoolMembership,
    SchoolSubscription,
    SubscriptionPlan,
    Teacher,
)


def drawn_signature_data() -> str:
    image = Image.new("RGBA", (900, 300), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.line([(80, 210), (190, 70), (270, 195), (375, 88), (530, 170)], fill=(7, 67, 44, 255), width=6)
    output = BytesIO()
    image.save(output, "PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")


@override_settings(ALLOWED_HOSTS=["testserver"])
class CircularSignatureDeadlineTests(TestCase):
    def setUp(self):
        self._media_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._media_directory.cleanup)
        self._media_override = override_settings(MEDIA_ROOT=self._media_directory.name)
        self._media_override.enable()
        self.addCleanup(self._media_override.disable)
        self.school = School.objects.create(name="مدرسة", code="deadline-school")
        plan = SubscriptionPlan.objects.create(
            name="خطة اختبار التعاميم",
            price=0,
            days_duration=30,
            max_teachers=0,
        )
        SchoolSubscription.objects.create(school=self.school, plan=plan)
        self.manager = Teacher.objects.create_user(
            phone="500700800", name="مدير", password="pass", is_staff=True
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )
        self.user = Teacher.objects.create_user(
            phone="0512345678", name="معلم", password="pass"
        )
        SchoolMembership.objects.create(
            school=self.school, teacher=self.user,
            role_type=SchoolMembership.RoleType.TEACHER,
        )

    def _make_circular(self, deadline):
        n = Notification.objects.create(
            title="تعميم",
            message="نص",
            requires_signature=True,
            signature_deadline_at=deadline,
            school=self.school,
            created_by=self.manager,
        )
        rec = NotificationRecipient.objects.create(notification=n, teacher=self.user)
        return n, rec

    def _sign(self, rec):
        self.client.force_login(self.user)
        return self.client.post(
            reverse("reports:circular_sign", args=[rec.pk]),
            {"signature_data": drawn_signature_data(), "ack": "1"},
        )

    def test_signing_blocked_after_deadline(self):
        _, rec = self._make_circular(timezone.now() - timedelta(days=1))
        self._sign(rec)
        rec.refresh_from_db()
        self.assertFalse(rec.is_signed)  # لم يُسمح بالتوقيع بعد انتهاء الموعد

    def test_signing_allowed_before_deadline(self):
        _, rec = self._make_circular(timezone.now() + timedelta(days=1))
        self._sign(rec)
        rec.refresh_from_db()
        self.assertTrue(rec.is_signed)  # التوقيع مسموح قبل الموعد

    def test_signing_allowed_without_deadline(self):
        _, rec = self._make_circular(None)
        self._sign(rec)
        rec.refresh_from_db()
        self.assertTrue(rec.is_signed)

    def test_receipt_requires_acknowledgement_and_duplicate_signing_keeps_original_evidence(self):
        _, rec = self._make_circular(None)
        self.client.force_login(self.user)
        receipt_url = reverse("reports:circular_receipt", args=[rec.pk])
        self.assertEqual(self.client.get(receipt_url).status_code, 404)

        self._sign(rec)
        rec.refresh_from_db()
        original = (
            rec.signed_at,
            rec.signature_image.name,
            rec.signature_image_sha256,
            rec.signature_evidence_digest,
        )
        self._sign(rec)
        rec.refresh_from_db()
        self.assertEqual(
            (
                rec.signed_at,
                rec.signature_image.name,
                rec.signature_image_sha256,
                rec.signature_evidence_digest,
            ),
            original,
        )
        self.assertEqual(self.client.get(receipt_url).status_code, 200)

    def test_status_filters_pagination_and_school_scope(self):
        future = timezone.now() + timedelta(days=1)
        past = timezone.now() - timedelta(days=1)
        pending, _ = self._make_circular(future)
        closed, _ = self._make_circular(past)
        signed, signed_rec = self._make_circular(None)
        signed_rec.is_signed = True
        signed_rec.signed_at = timezone.now()
        signed_rec.save(update_fields=["is_signed", "signed_at"])
        reading = Notification.objects.create(
            kind=Notification.Kind.CIRCULAR,
            title="للاطلاع فقط",
            message="نص",
            requires_signature=False,
            school=self.school,
            created_by=self.manager,
        )
        NotificationRecipient.objects.create(notification=reading, teacher=self.user)
        foreign_school = School.objects.create(name="مدرسة خارج النطاق", code="foreign-circular-filter")
        foreign = Notification.objects.create(
            kind=Notification.Kind.CIRCULAR,
            title="خارج النطاق",
            message="نص",
            requires_signature=False,
            school=foreign_school,
            created_by=self.manager,
        )
        NotificationRecipient.objects.create(notification=foreign, teacher=self.user)
        self.client.force_login(self.user)
        session = self.client.session
        session["active_school_id"] = self.school.pk
        session.save()

        for value, expected in (
            ("pending", pending.pk),
            ("closed", closed.pk),
            ("signed", signed.pk),
            ("reading", reading.pk),
        ):
            response = self.client.get(reverse("reports:my_circulars"), {"status": value})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.context["status_filter"], value)
            self.assertEqual(
                [item.notification_id for item in response.context["page_obj"].object_list],
                [expected],
            )
        self.assertEqual(
            self.client.get(reverse("reports:my_circulars"), {"status": "invalid"}).context["status_filter"],
            "all",
        )
        for index in range(13):
            extra = Notification.objects.create(
                kind=Notification.Kind.CIRCULAR,
                title=f"للاطلاع {index}",
                message="نص",
                requires_signature=False,
                school=self.school,
                created_by=self.manager,
            )
            NotificationRecipient.objects.create(notification=extra, teacher=self.user)
        page_two = self.client.get(reverse("reports:my_circulars"), {"status": "reading", "page": 2})
        self.assertEqual(page_two.context["page_obj"].paginator.count, 14)
        self.assertEqual(page_two.context["page_obj"].number, 2)

    def test_opening_detail_records_read_without_acknowledgement(self):
        _, rec = self._make_circular(None)
        self.client.force_login(self.user)
        response = self.client.get(reverse("reports:my_circular_detail", args=[rec.pk]))
        self.assertEqual(response.status_code, 200)
        rec.refresh_from_db()
        self.assertTrue(rec.is_read)
        self.assertIsNotNone(rec.read_at)
        self.assertFalse(rec.is_signed)

    def test_wrong_recipient_and_missing_csrf_cannot_sign(self):
        _, rec = self._make_circular(None)
        other = Teacher.objects.create_user(phone="0555000001", name="غير مستلم", password="pass")
        self.client.force_login(other)
        response = self.client.post(
            reverse("reports:circular_sign", args=[rec.pk]),
            {"signature_data": drawn_signature_data(), "ack": "1"},
        )
        self.assertEqual(response.status_code, 404)

        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)
        response = csrf_client.post(
            reverse("reports:circular_sign", args=[rec.pk]),
            {"signature_data": drawn_signature_data(), "ack": "1"},
        )
        self.assertEqual(response.status_code, 403)
        rec.refresh_from_db()
        self.assertFalse(rec.is_signed)

    def test_expired_document_cannot_be_signed_via_direct_post(self):
        notification, rec = self._make_circular(None)
        notification.expires_at = timezone.now() - timedelta(minutes=1)
        notification.save(update_fields=["expires_at"])
        self._sign(rec)
        rec.refresh_from_db()
        self.assertFalse(rec.is_signed)

    def test_recipient_detail_renders_official_document_and_print_action(self):
        notification, rec = self._make_circular(timezone.now() + timedelta(days=1))
        self.client.force_login(self.user)

        response = self.client.get(reverse("reports:my_circular_detail", args=[rec.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "reports/my_circular_detail.html")
        self.assertContains(response, notification.title)
        self.assertContains(response, "وثيقة إدارية رسمية")
        self.assertContains(response, "طباعة التعميم")
        self.assertContains(response, 'name="signature_data"')
        self.assertContains(response, 'data-handwritten-signature')
        self.assertContains(response, "نص التعميم")
        self.assertContains(response, self.user.name)
        self.assertContains(response, 'class="cir-ack__box"')
        self.assertContains(response, "window.rcConfirm({")
        self.assertNotContains(response, "window.confirm(")
        self.assertContains(response, "CIR-")

    def test_issue_snapshot_and_private_receipt_match_signed_document(self):
        notification, rec = self._make_circular(timezone.now() + timedelta(days=1))
        self.assertEqual(notification.issued_snapshot["basis"], "at_issue")
        self.assertEqual(len(notification.issued_digest), 64)

        self._sign(rec)
        rec.refresh_from_db()
        self.assertEqual(rec.signed_document_digest, notification.issued_digest)
        self.assertEqual(rec.signed_ack_text, notification.signature_ack_text)
        self.assertEqual(rec.signature_method, "drawn_ack")
        self.assertEqual(len(rec.signature_image_sha256), 64)
        self.assertTrue(rec.signature_image.name.startswith("circular_signatures/"))
        self.assertEqual(len(rec.signature_evidence_digest), 64)

        receipt = self.client.get(reverse("reports:circular_receipt", args=[rec.pk]))
        self.assertEqual(receipt.status_code, 200)
        self.assertTrue(receipt.context["evidence_valid"])
        self.assertContains(receipt, rec.signature_evidence_digest)
        self.assertContains(receipt, reverse("reports:circular_signature_image", args=[rec.pk]))
        image_response = self.client.get(reverse("reports:circular_signature_image", args=[rec.pk]))
        self.assertEqual(image_response.status_code, 200)
        self.assertEqual(image_response["Content-Type"], "image/png")

        Notification.objects.filter(pk=notification.pk).update(message="نص مختلف")
        changed_receipt = self.client.get(reverse("reports:circular_receipt", args=[rec.pk]))
        self.assertFalse(changed_receipt.context["evidence_valid"])

        other = Teacher.objects.create_user(phone="0533333333", name="آخر", password="pass")
        self.client.force_login(other)
        self.assertEqual(self.client.get(reverse("reports:circular_receipt", args=[rec.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("reports:circular_signature_image", args=[rec.pk])).status_code, 404)

    def test_issued_document_and_signed_evidence_cannot_be_changed_through_model(self):
        notification, rec = self._make_circular(None)
        notification.message = "نص آخر"
        with self.assertRaises(ValidationError):
            notification.save()
        notification.refresh_from_db()

        self._sign(rec)
        rec.refresh_from_db()
        rec.signed_at = timezone.now() + timedelta(hours=1)
        with self.assertRaises(ValidationError):
            rec.save()

    def test_old_document_is_marked_as_captured_at_first_sign(self):
        notification, rec = self._make_circular(None)
        Notification.objects.filter(pk=notification.pk).update(issued_snapshot=None, issued_digest="")
        self._sign(rec)
        notification.refresh_from_db()
        self.assertEqual(notification.issued_snapshot["basis"], "at_first_sign")
        receipt = self.client.get(reverse("reports:circular_receipt", args=[rec.pk]))
        self.assertTrue(receipt.context["evidence_valid"])
        self.assertContains(receipt, "ولا يثبت هذا السجل محتواها وقت الإرسال")

    def test_changed_document_is_rejected_even_if_a_bulk_update_bypasses_model_guard(self):
        notification, rec = self._make_circular(None)
        Notification.objects.filter(pk=notification.pk).update(message="نص متغير")
        self._sign(rec)
        rec.refresh_from_db()
        self.assertFalse(rec.is_signed)

    def test_invalid_drawings_do_not_block_a_later_valid_signature(self):
        _, rec = self._make_circular(None)
        self.client.force_login(self.user)
        url = reverse("reports:circular_sign", args=[rec.pk])
        for _ in range(5):
            self.client.post(url, {"signature_data": "invalid", "ack": "1"})
        self.client.post(url, {"signature_data": drawn_signature_data(), "ack": "1"})
        rec.refresh_from_db()
        self.assertTrue(rec.is_signed)
        self.assertEqual(rec.signature_attempt_count, 6)

    def test_empty_drawing_or_phone_only_cannot_sign(self):
        _, rec = self._make_circular(None)
        self.client.force_login(self.user)
        url = reverse("reports:circular_sign", args=[rec.pk])
        self.client.post(url, {"ack": "1", "phone": self.user.phone})
        rec.refresh_from_db()
        self.assertFalse(rec.is_signed)
        self.assertFalse(rec.signature_image)

        empty_image = Image.new("RGBA", (900, 300), (0, 0, 0, 0))
        output = BytesIO()
        empty_image.save(output, "PNG")
        blank_data = "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")
        self.client.post(url, {"ack": "1", "signature_data": blank_data})
        rec.refresh_from_db()
        self.assertFalse(rec.is_signed)

    def test_signature_image_tampering_invalidates_receipt_and_image_response(self):
        _, rec = self._make_circular(None)
        self._sign(rec)
        rec.refresh_from_db()
        with rec.signature_image.storage.open(rec.signature_image.name, "wb") as stored:
            stored.write(b"changed")
        receipt = self.client.get(reverse("reports:circular_receipt", args=[rec.pk]))
        self.assertFalse(receipt.context["evidence_valid"])
        self.assertEqual(self.client.get(reverse("reports:circular_signature_image", args=[rec.pk])).status_code, 404)

    def test_previous_phone_acknowledgement_keeps_its_evidence_and_has_no_drawing(self):
        notification, rec = self._make_circular(None)
        rec.is_signed = True
        rec.signed_at = timezone.now()
        rec.signed_document_digest = notification.issued_digest
        rec.signed_ack_text = notification.signature_ack_text
        rec.signature_method = "account_phone_ack"
        rec.signature_evidence_digest = acknowledgement_digest(rec, rec.signed_at)
        rec.save()
        self.client.force_login(self.user)
        receipt = self.client.get(reverse("reports:circular_receipt", args=[rec.pk]))
        self.assertTrue(receipt.context["evidence_valid"])
        self.assertContains(receipt, "مطابقة رقم الجوال المسجل")
        self.assertNotContains(receipt, "التوقيع المرسوم المحفوظ")

    def test_print_report_contains_drawn_image_and_legacy_row_label(self):
        notification, rec = self._make_circular(None)
        self._sign(rec)
        self.client.force_login(self.manager)
        session = self.client.session
        session["active_school_id"] = self.school.pk
        session.save()
        report = self.client.get(reverse("reports:notification_signatures_print", args=[notification.pk]))
        self.assertContains(report, reverse("reports:circular_signature_image", args=[rec.pk]))
        self.assertEqual(self.client.get(reverse("reports:circular_signature_image", args=[rec.pk])).status_code, 200)

        other_school = School.objects.create(name="مدرسة ثانية", code="another-signature-school")
        SchoolSubscription.objects.create(
            school=other_school,
            plan=SchoolSubscription.objects.get(school=self.school).plan,
        )
        other_manager = Teacher.objects.create_user(
            phone="0555998877", name="مدير آخر", password="pass", is_staff=True,
        )
        SchoolMembership.objects.create(
            school=other_school, teacher=other_manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )
        self.client.force_login(other_manager)
        other_session = self.client.session
        other_session["active_school_id"] = other_school.pk
        other_session.save()
        self.assertEqual(self.client.get(reverse("reports:circular_signature_image", args=[rec.pk])).status_code, 404)

    def test_reading_only_circular_is_not_presented_as_pending_signature(self):
        notification = Notification.objects.create(
            title="تعميم للاطلاع", message="نص", kind=Notification.Kind.CIRCULAR,
            requires_signature=False, school=self.school, created_by=self.manager,
        )
        NotificationRecipient.objects.create(notification=notification, teacher=self.user)
        self.client.force_login(self.user)
        session = self.client.session
        session["active_school_id"] = self.school.pk
        session.save()
        page = self.client.get(reverse("reports:my_circulars"))
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context["page_obj"].paginator.count, 1)
        self.assertFalse(page.context["page_obj"].object_list[0].notification.requires_signature)

        self.client.force_login(self.manager)
        session = self.client.session
        session["active_school_id"] = self.school.pk
        session.save()
        detail = self.client.get(reverse("reports:notification_detail", args=[notification.pk]))
        self.assertEqual(detail.status_code, 200)
        self.assertFalse(detail.context["n"].requires_signature)

    def test_manager_detail_and_print_report_use_real_signature_percentage(self):
        notification, signed_recipient = self._make_circular(timezone.now() + timedelta(days=1))
        signed_recipient.is_read = True
        signed_recipient.is_signed = True
        signed_recipient.read_at = timezone.now()
        signed_recipient.signed_at = timezone.now()
        signed_recipient.save(update_fields=["is_read", "is_signed", "read_at", "signed_at"])
        second_user = Teacher.objects.create_user(
            phone="0523456789", name="معلم ثان", password="pass"
        )
        NotificationRecipient.objects.create(notification=notification, teacher=second_user)

        self.client.force_login(self.manager)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()

        detail_response = self.client.get(
            reverse("reports:notification_detail", args=[notification.pk])
        )
        print_response = self.client.get(
            reverse("reports:notification_signatures_print", args=[notification.pk])
        )

        self.assertEqual(detail_response.status_code, 200)
        self.assertEqual(detail_response.context["signature_stats"]["signed_percentage"], 50)
        self.assertContains(detail_response, "نسبة اكتمال التوقيع")
        self.assertEqual(print_response.status_code, 200)
        self.assertEqual(print_response.context["stats"]["signed_percentage"], 50)
        self.assertContains(print_response, "تقرير الاطلاع والتوقيع")
        self.assertContains(print_response, "50%")
        self.assertNotContains(print_response, "صفحة 1 من 1")
        self.assertNotContains(print_response, "cdnjs.cloudflare.com")
