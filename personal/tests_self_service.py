"""Focused checks for the personal recipient journey shared with Tawtheeq UI."""

from django.test import TestCase, override_settings
from django.urls import reverse

from reports.models import Teacher

from .models import PersonalNotice, PersonalNoticeRecipient, PersonalWorkspace


@override_settings(ALLOWED_HOSTS=["testserver"], RATELIMIT_ENABLE=False)
class PersonalNoticeInboxTests(TestCase):
    def setUp(self):
        self.owner = Teacher.objects.create_user(
            phone="0557333401", name="صاحب المساحة", password="Personal#2026"  # noqa: S106
        )
        self.other = Teacher.objects.create_user(
            phone="0557333402", name="معلم آخر", password="Personal#2026"  # noqa: S106
        )
        self.workspace = PersonalWorkspace.objects.create(owner=self.owner, school_name="اسم تعريفي")
        self.other_workspace = PersonalWorkspace.objects.create(owner=self.other, school_name="اسم آخر")
        self.client.force_login(self.owner)

    def test_inbox_is_paginated_and_read_only_after_opening_owned_detail(self):
        for index in range(13):
            notice = PersonalNotice.objects.create(title=f"رسالة {index}", message=f"محتوى {index}")
            PersonalNoticeRecipient.objects.create(notice=notice, workspace=self.workspace)

        first_page = self.client.get(reverse("personal:notices"))
        self.assertEqual(first_page.status_code, 200)
        self.assertEqual(len(first_page.context["page_obj"]), 12)
        self.assertContains(first_page, "13 رسالة")
        self.assertContains(first_page, "غير المقروء")
        self.assertEqual(PersonalNoticeRecipient.objects.filter(workspace=self.workspace, read_at__isnull=True).count(), 13)

        receipt = PersonalNoticeRecipient.objects.filter(workspace=self.workspace).first()
        detail = self.client.get(reverse("personal:notice_detail", args=[receipt.pk]))
        self.assertEqual(detail.status_code, 200)
        receipt.refresh_from_db()
        self.assertIsNotNone(receipt.read_at)

        foreign = PersonalNoticeRecipient.objects.create(
            notice=PersonalNotice.objects.create(title="خاصة بمعلم آخر", message="محتوى خاص"),
            workspace=self.other_workspace,
        )
        self.assertEqual(self.client.get(reverse("personal:notice_detail", args=[foreign.pk])).status_code, 404)
        self.assertIsNone(PersonalNoticeRecipient.objects.get(pk=foreign.pk).read_at)
