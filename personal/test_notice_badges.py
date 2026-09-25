from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from reports.models import Teacher

from .models import PersonalNotice, PersonalNoticeRecipient, PersonalWorkspace
from .templatetags.personal_notices import personal_unread_notices


@override_settings(ALLOWED_HOSTS=["testserver"], RATELIMIT_ENABLE=False)
class PersonalNoticeBadgeTests(TestCase):
    def setUp(self):
        self.owner = Teacher.objects.create_user(
            phone="0557990120", name="معلمة مستقلة", password="Personal#2026"  # noqa: S106
        )
        self.workspace = PersonalWorkspace.objects.create(owner=self.owner)
        other = Teacher.objects.create_user(
            phone="0557990121", name="معلم آخر", password="Personal#2026"  # noqa: S106
        )
        self.other_workspace = PersonalWorkspace.objects.create(owner=other)
        self.receipts = []
        for index in range(3):
            notice = PersonalNotice.objects.create(title=f"تنبيه {index}", message="رسالة", created_by=self.owner)
            self.receipts.append(PersonalNoticeRecipient.objects.create(
                notice=notice, workspace=self.workspace,
                read_at=timezone.now() if index == 2 else None,
            ))
        other_notice = PersonalNotice.objects.create(title="تنبيه آخر", message="رسالة", created_by=self.owner)
        PersonalNoticeRecipient.objects.create(notice=other_notice, workspace=self.other_workspace)

    def test_header_and_mobile_bells_show_owned_unread_count_after_read(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("personal:dashboard"))
        self.assertContains(response, 'aria-label="إشعارات المنصة، 2 غير مقروء"')
        self.assertContains(response, 'aria-label="الإشعارات، 2 غير مقروء"')
        self.assertContains(response, '<span class="dot" aria-hidden="true">2</span>')
        self.assertContains(response, '<b aria-hidden="true">2</b>')

        response = self.client.get(reverse("personal:notice_detail", args=[self.receipts[0].pk]))
        self.assertContains(response, 'aria-label="إشعارات المنصة، 1 غير مقروء"')
        self.assertContains(response, 'aria-label="الإشعارات، 1 غير مقروء"')

    def test_count_is_cached_per_request_and_requires_owned_workspace(self):
        request = RequestFactory().get("/")
        request.user = self.owner
        request.personal_workspace = self.workspace
        with self.assertNumQueries(1):
            self.assertEqual(personal_unread_notices({"request": request}), 2)
            self.assertEqual(personal_unread_notices({"request": request}), 2)

        request = RequestFactory().get("/")
        request.user = self.owner
        request.personal_workspace = self.other_workspace
        with self.assertNumQueries(0):
            self.assertEqual(personal_unread_notices({"request": request}), 0)

        request = RequestFactory().get("/")
        request.user = AnonymousUser()
        with self.assertNumQueries(0):
            self.assertEqual(personal_unread_notices({"request": request}), 0)
