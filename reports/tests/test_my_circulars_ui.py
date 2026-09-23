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
class MyCircularsListUiTests(TestCase):
    def setUp(self):
        self._media_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._media_directory.cleanup)
        self._media_override = override_settings(MEDIA_ROOT=self._media_directory.name)
        self._media_override.enable()
        self.addCleanup(self._media_override.disable)
        self.school = School.objects.create(name="مدرسة التعاميم", code="circular-ui-school")
        plan = SubscriptionPlan.objects.create(
            name="خطة اختبار القائمة", price=0, days_duration=30, max_teachers=0
        )
        SchoolSubscription.objects.create(school=self.school, plan=plan)
        self.manager = Teacher.objects.create_user(
            phone="500400100", name="مدير المدرسة", password="pass", is_staff=True
        )
        self.recipient = Teacher.objects.create_user(
            phone="500400101", name="مستلم التعميم", password="pass"
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

    def _circular(self, *, title, requires_signature=False, is_read=False, deadline=None, attachment=""):
        document = Notification.objects.create(
            kind=Notification.Kind.CIRCULAR,
            title=title,
            message="نص التعميم الرسمي",
            school=self.school,
            created_by=self.manager,
            requires_signature=requires_signature,
            signature_deadline_at=deadline,
            attachment=attachment,
        )
        return NotificationRecipient.objects.create(
            notification=document,
            teacher=self.recipient,
            is_read=is_read,
        )

    def test_list_separates_read_state_from_acknowledgement_and_keeps_actions(self):
        unread = self._circular(
            title="تعميم ينتظر الإقرار",
            requires_signature=True,
            deadline=timezone.now() + timedelta(days=1),
            attachment=SimpleUploadedFile(
                "test-document.pdf", b"%PDF-1.4\n%%EOF\n", content_type="application/pdf"
            ),
        )
        self._circular(title="تعميم للاطلاع", is_read=True)

        response = self.client.get(reverse("reports:my_circulars"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "css/circulars-list.css")
        self.assertContains(response, "غير مقروء")
        self.assertContains(response, "مقروء")
        self.assertContains(response, "بانتظار إقرارك")
        self.assertContains(response, "للاطلاع فقط")
        self.assertContains(response, "يحتوي مرفقًا")
        self.assertContains(response, reverse("reports:my_circular_detail", args=[unread.pk]))
        self.assertContains(response, reverse("reports:circulars_mark_all_read"))
        unread.refresh_from_db()
        self.assertFalse(unread.is_read)

    def test_existing_status_filter_and_pagination_remain_connected(self):
        for index in range(13):
            self._circular(title=f"للاطلاع {index}")

        response = self.client.get(reverse("reports:my_circulars"), {"status": "reading"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'href="?status=reading" aria-current="page"')
        self.assertContains(response, "المحدد حاليًا")
        self.assertContains(response, "?status=reading&amp;page=2")
        self.assertEqual(response.context["page_obj"].paginator.num_pages, 2)

    def test_active_school_does_not_show_another_schools_circular(self):
        other_school = School.objects.create(name="مدرسة أخرى", code="circular-ui-other")
        visible = self._circular(title="تعميم مدرستي")
        foreign = Notification.objects.create(
            kind=Notification.Kind.CIRCULAR,
            title="تعميم مدرسة أخرى",
            message="لا يجب عرضه هنا",
            school=other_school,
            created_by=self.manager,
        )
        NotificationRecipient.objects.create(notification=foreign, teacher=self.recipient)

        response = self.client.get(reverse("reports:my_circulars"))

        self.assertContains(response, reverse("reports:my_circular_detail", args=[visible.pk]))
        self.assertNotContains(response, "تعميم مدرسة أخرى")
