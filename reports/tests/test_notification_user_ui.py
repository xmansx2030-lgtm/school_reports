from __future__ import annotations

import re
import tempfile
from pathlib import Path

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse

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
class NotificationUserExperienceTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls._media = tempfile.TemporaryDirectory()
        cls._media_override = override_settings(MEDIA_ROOT=cls._media.name)
        cls._media_override.enable()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls._media_override.disable()
        cls._media.cleanup()

    def setUp(self):
        plan = SubscriptionPlan.objects.create(
            name="باقة الإشعارات", price=0, days_duration=365, max_teachers=0
        )
        self.school = School.objects.create(name="مدرسة الإشعارات", code="notification-ui")
        self.other_school = School.objects.create(
            name="مدرسة أخرى", code="notification-ui-other"
        )
        SchoolSubscription.objects.create(school=self.school, plan=plan)
        SchoolSubscription.objects.create(school=self.other_school, plan=plan)
        self.user = Teacher.objects.create_user(
            phone="0500081001", name="مستلم الإشعار", password="Passw0rd!123"
        )
        self.other_user = Teacher.objects.create_user(
            phone="0500081002", name="مستلم آخر", password="Passw0rd!123"
        )
        self.sender = Teacher.objects.create_user(
            phone="0500081003", name="مرسل الإشعار", password="Passw0rd!123"
        )
        for user in (self.user, self.other_user, self.sender):
            SchoolMembership.objects.create(
                school=self.school,
                teacher=user,
                role_type=SchoolMembership.RoleType.TEACHER,
            )
        SchoolMembership.objects.create(
            school=self.other_school,
            teacher=self.user,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        self.notification = Notification.objects.create(
            school=self.school,
            created_by=self.sender,
            title="اجتماع الفريق التعليمي",
            message="يرجى الاطلاع على موعد الاجتماع والتعليمات المرفقة.",
            is_important=True,
        )
        self.recipient = NotificationRecipient.objects.create(
            notification=self.notification,
            teacher=self.user,
        )
        self.client.force_login(self.user)
        session = self.client.session
        session["active_school_id"] = self.school.pk
        session.save()

    def test_inbox_uses_v1_structure_without_marking_items_read(self):
        response = self.client.get(reverse("reports:my_notifications"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="twq-page notifications-page"')
        self.assertContains(response, 'class="twq-page-header notifications-header"')
        self.assertContains(response, "بحث في الصفحة الحالية")
        self.assertContains(response, "غير مقروء")
        self.assertContains(response, "static/css/notifications-user.css")
        self.assertContains(response, "static/js/notifications-user.js")
        self.recipient.refresh_from_db()
        self.assertFalse(self.recipient.is_read)

    def test_inbox_is_scoped_to_active_school_and_keeps_pagination(self):
        foreign = Notification.objects.create(
            school=self.other_school,
            created_by=self.sender,
            title="رسالة المدرسة الأخرى",
            message="لا تظهر في المدرسة النشطة.",
        )
        NotificationRecipient.objects.create(notification=foreign, teacher=self.user)
        for index in range(13):
            item = Notification.objects.create(
                school=self.school,
                created_by=self.sender,
                title=f"رسالة {index}",
                message="محتوى الاختبار",
            )
            NotificationRecipient.objects.create(notification=item, teacher=self.user)

        response = self.client.get(reverse("reports:my_notifications"))

        self.assertNotContains(response, foreign.title)
        self.assertEqual(response.context["page_obj"].paginator.per_page, 12)
        self.assertContains(response, "صفحة 1 من 2")

    def test_detail_marks_read_and_presents_sender_and_attachment(self):
        self.notification.attachment = SimpleUploadedFile(
            "guide.pdf", b"%PDF-1.4 notification guide", content_type="application/pdf"
        )
        self.notification.save(update_fields=["attachment"])

        response = self.client.get(
            reverse("reports:my_notification_detail", args=[self.recipient.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "مرسل الإشعار")
        self.assertContains(response, "مرفق الإشعار")
        self.assertContains(response, self.notification.attachment.url)
        self.assertContains(response, 'rel="noopener"')
        self.recipient.refresh_from_db()
        self.assertTrue(self.recipient.is_read)
        self.assertIsNotNone(self.recipient.read_at)

    def test_other_user_cannot_open_or_mark_recipient_read(self):
        client = Client()
        client.force_login(self.other_user)
        session = client.session
        session["active_school_id"] = self.school.pk
        session.save()

        detail = client.get(
            reverse("reports:my_notification_detail", args=[self.recipient.pk])
        )
        mark_read = client.post(
            reverse("reports:notification_mark_read", args=[self.recipient.pk])
        )

        self.assertEqual(detail.status_code, 302)
        self.assertEqual(mark_read.status_code, 404)
        self.assertNotIn(self.notification.title, detail.content.decode("utf-8"))
        self.recipient.refresh_from_db()
        self.assertFalse(self.recipient.is_read)

    def test_mark_read_requires_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        session = client.session
        session["active_school_id"] = self.school.pk
        session.save()

        response = client.post(
            reverse("reports:notification_mark_read", args=[self.recipient.pk])
        )

        self.assertEqual(response.status_code, 403)
        self.recipient.refresh_from_db()
        self.assertFalse(self.recipient.is_read)

    def test_user_templates_own_no_embedded_css_or_inline_javascript(self):
        templates = Path(__file__).resolve().parents[1] / "templates" / "reports"
        for name in ("my_notifications.html", "my_notification_detail.html"):
            source = (templates / name).read_text(encoding="utf-8")
            self.assertNotIn("<style", source)
            self.assertNotRegex(source, re.compile(r"\sstyle\s*=", re.IGNORECASE))
            self.assertNotRegex(
                source,
                re.compile(r"<script(?![^>]*\bsrc\s*=)[^>]*>", re.IGNORECASE),
            )

    def test_notification_assets_use_tokens_and_no_direct_style_writes(self):
        root = Path(__file__).resolve().parents[2]
        css = (root / "static" / "css" / "notifications-user.css").read_text(
            encoding="utf-8"
        )
        js = (root / "static" / "js" / "notifications-user.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("var(--twq-", css)
        self.assertNotRegex(css, re.compile(r"#[0-9a-fA-F]{3,8}\b"))
        self.assertNotRegex(css, re.compile(r"\b(?:rgb|hsl)a?\("))
        self.assertNotIn(".style.", js)
