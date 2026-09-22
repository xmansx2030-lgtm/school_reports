import tempfile
from pathlib import Path
from uuid import uuid4

from django.core.files.uploadedfile import SimpleUploadedFile
from django.template.loader import get_template
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from reports.models import (
    Department,
    DepartmentMembership,
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
class NotificationAdminUiTests(TestCase):
    def setUp(self):
        self._media_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._media_directory.cleanup)
        self._media_override = override_settings(MEDIA_ROOT=self._media_directory.name)
        self._media_override.enable()
        self.addCleanup(self._media_override.disable)

        plan = SubscriptionPlan.objects.create(
            name="خطة الإشعارات", price=0, days_duration=30, max_teachers=20
        )
        self.school = School.objects.create(name="مدرسة الإشعارات", code="notify-admin")
        self.other_school = School.objects.create(name="مدرسة أخرى", code="notify-other")
        SchoolSubscription.objects.create(school=self.school, plan=plan)
        SchoolSubscription.objects.create(school=self.other_school, plan=plan)
        self.manager = Teacher.objects.create_user(
            phone="500912001", name="مدير الإشعارات", password="pass", is_staff=True
        )
        self.teacher = Teacher.objects.create_user(
            phone="500912002", name="مستلم أول", password="pass"
        )
        self.second_teacher = Teacher.objects.create_user(
            phone="500912003", name="مستلم ثان", password="pass"
        )
        self.outsider = Teacher.objects.create_user(
            phone="500912004", name="مستلم خارجي", password="pass"
        )
        for teacher, school, role in (
            (self.manager, self.school, SchoolMembership.RoleType.MANAGER),
            (self.teacher, self.school, SchoolMembership.RoleType.TEACHER),
            (self.second_teacher, self.school, SchoolMembership.RoleType.TEACHER),
            (self.outsider, self.other_school, SchoolMembership.RoleType.TEACHER),
        ):
            SchoolMembership.objects.create(school=school, teacher=teacher, role_type=role)
        self.department = Department.objects.create(
            school=self.school, name="قسم الاختبار", slug="notify-dept", is_active=True
        )
        self.other_department = Department.objects.create(
            school=self.other_school, name="قسم خارجي", slug="notify-other-dept", is_active=True
        )
        DepartmentMembership.objects.create(department=self.department, teacher=self.teacher)
        DepartmentMembership.objects.create(department=self.department, teacher=self.second_teacher)
        DepartmentMembership.objects.create(department=self.other_department, teacher=self.outsider)
        self._login(self.manager)

    def _login(self, user, school=None):
        self.client.force_login(user)
        session = self.client.session
        session["active_school_id"] = (school or self.school).pk
        session.save()

    def _send(self, **overrides):
        payload = {
            "submission_key": str(uuid4()),
            "communication_type": "notification",
            "title": "إشعار الاختبار",
            "message": "رسالة واضحة للمستلمين.",
            "teachers": [str(self.teacher.pk)],
        }
        payload.update(overrides)
        return self.client.post(reverse("reports:notifications_create"), payload)

    def test_sender_templates_use_owned_assets_without_embedded_presentation(self):
        for template_name in (
            "reports/send_notification.html",
            "reports/notifications_sent.html",
            "reports/notification_detail.html",
        ):
            source = get_template(template_name).template.source
            self.assertNotIn("<style", source)
            self.assertNotIn("style=", source)
        self.assertNotIn("<script nonce=", get_template("reports/send_notification.html").template.source)
        self.assertContains(self.client.get(reverse("reports:notifications_create")), "css/notifications-admin.css")
        self.assertContains(self.client.get(reverse("reports:notifications_create")), "js/notifications-composer.js")

    def test_composer_keeps_real_fields_and_accessible_sections(self):
        response = self.client.get(reverse("reports:notifications_create"))
        self.assertEqual(response.status_code, 200)
        for field in (
            "communication_type", "title", "message", "is_important", "expires_at",
            "attachment", "requires_signature", "signature_deadline_at",
            "signature_ack_text", "target_department", "teachers",
        ):
            self.assertContains(response, f'name="{field}"')
        self.assertContains(response, 'id="notificationComposerTitle"')
        self.assertContains(response, "العدد النهائي يُحتسب")
        self.assertContains(response, "الإرسال فوري")

    def test_simple_send_integrates_sent_list_and_recipient_inbox(self):
        response = self._send(is_important="on")
        self.assertRedirects(response, reverse("reports:notifications_sent"))
        notification = Notification.objects.get(title="إشعار الاختبار")
        self.assertEqual(notification.created_by, self.manager)
        self.assertEqual(notification.school, self.school)
        self.assertTrue(notification.is_important)
        recipient = NotificationRecipient.objects.get(notification=notification)
        self.assertEqual(recipient.teacher, self.teacher)
        self.assertContains(self.client.get(reverse("reports:notifications_sent")), notification.title)
        self._login(self.teacher)
        self.assertContains(self.client.get(reverse("reports:my_notifications")), notification.title)

    def test_department_and_direct_targeting_deduplicates_recipient(self):
        response = self._send(
            target_department=[str(self.department.pk)],
            teachers=[str(self.teacher.pk)],
        )
        self.assertEqual(response.status_code, 302)
        notification = Notification.objects.get(title="إشعار الاختبار")
        self.assertEqual(
            set(notification.recipients.values_list("teacher_id", flat=True)),
            {self.teacher.pk, self.second_teacher.pk},
        )
        self.assertEqual(notification.recipients.filter(teacher=self.teacher).count(), 1)

    def test_empty_audience_and_cross_school_targets_are_rejected(self):
        empty = self._send(teachers=[])
        self.assertEqual(empty.status_code, 200)
        self.assertContains(empty, "يرجى تحديد المستلمين")
        self.assertContains(empty, 'id="notificationErrorSummary"')
        self.assertContains(empty, 'role="alert"')
        self.assertContains(empty, 'tabindex="-1"')
        self.assertFalse(Notification.objects.filter(title="إشعار الاختبار").exists())

        foreign_teacher = self._send(teachers=[str(self.outsider.pk)])
        self.assertEqual(foreign_teacher.status_code, 200)
        self.assertFalse(Notification.objects.filter(title="إشعار الاختبار").exists())

        foreign_department = self._send(
            teachers=[], target_department=[str(self.other_department.pk)]
        )
        self.assertEqual(foreign_department.status_code, 200)
        self.assertFalse(Notification.objects.filter(title="إشعار الاختبار").exists())

    def test_sender_and_school_fields_are_backend_authoritative(self):
        response = self._send(
            sender=str(self.outsider.pk),
            created_by=str(self.outsider.pk),
            school=str(self.other_school.pk),
        )
        self.assertEqual(response.status_code, 302)
        notification = Notification.objects.get(title="إشعار الاختبار")
        self.assertEqual(notification.created_by, self.manager)
        self.assertEqual(notification.school, self.school)

    def test_newsletter_attachment_and_important_flag_are_preserved(self):
        response = self.client.post(
            reverse("reports:notifications_create"),
            {
                "submission_key": str(uuid4()),
                "communication_type": "newsletter",
                "title": "نشرة الاختبار",
                "message": "مقدمة النشرة.",
                "is_important": "on",
                "teachers": [str(self.teacher.pk)],
                "attachment": SimpleUploadedFile(
                    "newsletter.pdf", b"%PDF-1.4\n% notification admin test\n",
                    content_type="application/pdf",
                ),
            },
        )
        self.assertRedirects(
            response, f"{reverse('reports:notifications_sent')}?kind=newsletter"
        )
        newsletter = Notification.objects.get(title="نشرة الاختبار")
        self.assertEqual(newsletter.kind, Notification.Kind.NEWSLETTER)
        self.assertTrue(newsletter.is_important)
        self.assertTrue(newsletter.attachment)

    def test_sent_filters_pagination_and_real_read_statistics(self):
        notification = Notification.objects.create(
            school=self.school, created_by=self.manager,
            title="إحصاء القراءة", message="النص",
        )
        NotificationRecipient.objects.create(
            notification=notification, teacher=self.teacher, is_read=True
        )
        NotificationRecipient.objects.create(
            notification=notification, teacher=self.second_teacher, is_read=False
        )
        response = self.client.get(reverse("reports:notifications_sent"))
        self.assertContains(response, "إحصاء القراءة")
        self.assertContains(response, "50%")
        self.assertContains(response, "1 / 2")
        self.assertEqual(response.context["stats"][notification.pk]["read"], 1)

        Notification.objects.create(
            school=self.school, created_by=self.manager,
            kind=Notification.Kind.NEWSLETTER, title="نشرة منفصلة", message="النص",
        )
        filtered = self.client.get(reverse("reports:notifications_sent"), {"kind": "newsletter"})
        self.assertContains(filtered, "نشرة منفصلة")
        self.assertNotContains(filtered, "إحصاء القراءة")

        for number in range(21):
            Notification.objects.create(
                school=self.school, created_by=self.manager,
                title=f"إشعار صفحة {number}", message="النص",
            )
        self.assertContains(self.client.get(reverse("reports:notifications_sent")), "page=2")

    def test_sender_detail_shows_message_attachment_and_recipient_states(self):
        notification = Notification.objects.create(
            school=self.school, created_by=self.manager,
            kind=Notification.Kind.NEWSLETTER, title="تفاصيل النشرة",
            message="محتوى النشرة", requires_signature=True,
            attachment=SimpleUploadedFile(
                "detail.pdf", b"%PDF-1.4\n% detail\n", content_type="application/pdf"
            ),
        )
        NotificationRecipient.objects.create(
            notification=notification, teacher=self.teacher,
            is_read=True, is_signed=True,
        )
        NotificationRecipient.objects.create(
            notification=notification, teacher=self.second_teacher,
        )
        response = self.client.get(reverse("reports:notification_detail", args=[notification.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "محتوى النشرة")
        self.assertContains(response, "مرفق النشرة")
        self.assertContains(response, notification.attachment.url)
        self.assertContains(response, "مقروء")
        self.assertContains(response, "غير مقروء")
        self.assertContains(response, "موقّع")
        self.assertContains(response, "غير موقّع")
        self.assertEqual(
            response.context["recipient_stats"],
            {"total": 2, "read": 1, "unread": 1, "read_percentage": 50},
        )
        self.assertContains(response, "نسبة القراءة")
        self.assertContains(response, "50%")
        self.assertEqual(response.context["signature_stats"]["signed_percentage"], 50)

    def test_sender_detail_read_percentage_uses_sent_list_rounding(self):
        third_teacher = Teacher.objects.create_user(
            phone="500912005", name="مستلم ثالث", password="pass"
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=third_teacher,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        cases = (
            ("لا مستلمين", (), 0),
            ("غير مقروء", (False,), 0),
            ("مقروء", (True,), 100),
            ("قراءة جزئية", (True, True, False), 67),
        )
        recipients = (self.teacher, self.second_teacher, third_teacher)
        for title, read_states, expected in cases:
            with self.subTest(title=title):
                notification = Notification.objects.create(
                    school=self.school,
                    created_by=self.manager,
                    title=title,
                    message="النص",
                )
                for teacher, is_read in zip(recipients, read_states, strict=False):
                    NotificationRecipient.objects.create(
                        notification=notification,
                        teacher=teacher,
                        is_read=is_read,
                    )
                detail = self.client.get(
                    reverse("reports:notification_detail", args=[notification.pk])
                )
                self.assertEqual(
                    detail.context["recipient_stats"]["read_percentage"], expected
                )
                self.assertContains(detail, f"{expected}%")

                sent = self.client.get(reverse("reports:notifications_sent"))
                sent_notification = next(
                    item
                    for item in sent.context["page_obj"].object_list
                    if item.pk == notification.pk
                )
                self.assertEqual(sent_notification.stat_rate, expected)

    def test_composer_script_focuses_only_rendered_server_errors(self):
        source = Path("static/js/notifications-composer.js").read_text(encoding="utf-8")
        self.assertIn('byId("notificationErrorSummary")', source)
        self.assertIn("form.querySelector('[aria-invalid=\"true\"]')", source)
        self.assertIn("focusServerErrors();", source)
        self.assertNotIn("autofocus", source.lower())

        invalid = self._send(communication_type="newsletter", title="")
        self.assertContains(invalid, 'href="#id_title"')
        self.assertContains(invalid, 'aria-invalid="true"')

    def test_sent_detail_is_private_to_sender_or_school_manager(self):
        notification = Notification.objects.create(
            school=self.school, created_by=self.manager,
            title="تفاصيل خاصة", message="النص",
        )
        self._login(self.teacher)
        denied = self.client.get(reverse("reports:notification_detail", args=[notification.pk]))
        self.assertNotEqual(denied.status_code, 200)

        self._login(self.outsider, self.other_school)
        cross_school = self.client.get(reverse("reports:notification_detail", args=[notification.pk]))
        self.assertNotEqual(cross_school.status_code, 200)

    def test_delete_requires_csrf_and_preserves_ownership(self):
        notification = Notification.objects.create(
            school=self.school, created_by=self.manager,
            title="إشعار للحذف", message="النص",
        )
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.manager)
        session = csrf_client.session
        session["active_school_id"] = self.school.pk
        session.save()
        denied = csrf_client.post(reverse("reports:notification_delete", args=[notification.pk]))
        self.assertEqual(denied.status_code, 403)
        self.assertTrue(Notification.objects.filter(pk=notification.pk).exists())

        self._login(self.teacher)
        self.client.post(reverse("reports:notification_delete", args=[notification.pk]))
        self.assertTrue(Notification.objects.filter(pk=notification.pk).exists())

    def test_user_open_updates_sender_read_statistics(self):
        notification = Notification.objects.create(
            school=self.school, created_by=self.manager,
            title="إشعار قراءة", message="النص",
        )
        recipient = NotificationRecipient.objects.create(
            notification=notification, teacher=self.teacher
        )
        self._login(self.teacher)
        self.client.get(reverse("reports:my_notification_detail", args=[recipient.pk]))
        recipient.refresh_from_db()
        self.assertTrue(recipient.is_read)
        self._login(self.manager)
        sent = self.client.get(reverse("reports:notifications_sent"))
        self.assertContains(sent, "1 / 1")

    def test_no_direct_colors_in_admin_stylesheet(self):
        source = Path("static/css/notifications-admin.css").read_text(encoding="utf-8")
        for marker in ("#fff", "#000", "rgb(", "rgba(", "hsl(", "hsla("):
            self.assertNotIn(marker, source.lower())
