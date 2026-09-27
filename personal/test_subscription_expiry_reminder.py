"""Expiry notices for dated personal plans reach their owner only."""

from datetime import timedelta

from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from reports.models import Teacher

from .models import PersonalNoticeRecipient, PersonalPlan, PersonalSubscription, PersonalWorkspace
from .tasks import check_personal_subscription_expiry_task


@override_settings(
    SUBSCRIPTION_EXPIRY_REMINDER_ENABLED=True,
    SUBSCRIPTION_EXPIRY_REMINDER_EMAIL_ENABLED=True,
    SUBSCRIPTION_EXPIRY_REMINDER_DAYS=[14, 7, 3, 1],
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="no-reply@example.com",
)
class PersonalSubscriptionExpiryReminderTests(TestCase):
    def setUp(self):
        cache.clear()
        owner = Teacher.objects.create_user(
            phone="0557111222", name="معلمة فردية", email="teacher@example.com",
            password="pass",  # noqa: S106
        )
        self.workspace = PersonalWorkspace.objects.create(owner=owner, school_name="للتعريف")
        self.plan = PersonalPlan.objects.create(
            code="personal_expiry_test", name="باقة فردية", price=49, duration_days=30
        )
        self.subscription = PersonalSubscription.objects.create(
            workspace=self.workspace, plan=self.plan
        )

    def _expire_in(self, days):
        PersonalSubscription.objects.filter(pk=self.subscription.pk).update(
            end_date=timezone.localdate() + timedelta(days=days)
        )

    def test_owner_gets_in_app_and_email_reminder(self):
        self._expire_in(7)

        summary = check_personal_subscription_expiry_task()

        self.assertEqual(summary["reminders_sent"], 1)
        self.assertEqual(summary["emails_sent"], 1)
        self.assertEqual(PersonalNoticeRecipient.objects.filter(workspace=self.workspace).count(), 1)
        self.assertEqual(mail.outbox[0].to, ["teacher@example.com"])
        self.assertIn("باقة فردية", mail.outbox[0].body)
        self.assertIn("/personal/payments/", mail.outbox[0].body)
        self.client.force_login(self.workspace.owner)
        self.assertContains(self.client.get(reverse("personal:notices")), "اشتراكك الشخصي ينتهي خلال 7 أيام")

    def test_repeated_run_does_not_duplicate_notice_or_email(self):
        self._expire_in(3)
        check_personal_subscription_expiry_task()
        cache.clear()
        mail.outbox.clear()

        summary = check_personal_subscription_expiry_task()

        self.assertEqual(summary["skipped_duplicate"], 1)
        self.assertEqual(PersonalNoticeRecipient.objects.filter(workspace=self.workspace).count(), 1)
        self.assertEqual(mail.outbox, [])

    @override_settings(SUBSCRIPTION_EXPIRY_REMINDER_EMAIL_ENABLED=False)
    def test_in_app_notice_still_arrives_without_email(self):
        self._expire_in(1)

        summary = check_personal_subscription_expiry_task()

        self.assertEqual(summary["reminders_sent"], 1)
        self.assertEqual(summary["emails_sent"], 0)
        self.assertEqual(PersonalNoticeRecipient.objects.filter(workspace=self.workspace).count(), 1)
        self.assertEqual(mail.outbox, [])

    def test_continuous_free_plan_has_no_expiry_reminder(self):
        PersonalSubscription.objects.filter(pk=self.subscription.pk).update(end_date=None)

        summary = check_personal_subscription_expiry_task()

        self.assertEqual(summary["reminders_sent"], 0)
        self.assertFalse(PersonalNoticeRecipient.objects.exists())
