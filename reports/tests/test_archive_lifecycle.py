"""The manager's archive journey: capacity, consumption, and what expiry does.

Storage is not an archive-only concern. The same limit gates every upload in the
platform, so the archive add-on's lifecycle decides whether teachers can attach a
photo to a report at all.
"""
from __future__ import annotations

from datetime import timedelta

from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.utils import timezone

from reports.models import (
    Notification,
    PlatformSettings,
    Report,
    School,
    SchoolArchiveAddon,
    SchoolMembership,
    SchoolSubscription,
    SchoolYearArchive,
    SubscriptionPlan,
    Teacher,
)
from reports.services_archive import (
    archive_storage_capacity_error,
    school_storage_allowance,
    school_storage_limit_bytes,
    school_storage_overview,
)

GB = 1024 ** 3
MB = 1024 ** 2


class _Upload:
    def __init__(self, size):
        self.size = size
        self.name = "upload.bin"


@override_settings(ALLOWED_HOSTS=["testserver"])
class StorageAllowanceTests(TestCase):
    """Storage is sized from the purchased teacher capacity and is deliberately
    independent of the yearly-archive add-on."""

    def setUp(self):
        settings_obj = PlatformSettings.get_solo()
        settings_obj.free_storage_mb = 1024
        settings_obj.storage_mb_per_teacher = 200
        settings_obj.save(
            update_fields=["free_storage_mb", "storage_mb_per_teacher"]
        )
        self.school = School.objects.create(name="مدرسة الأرشيف", code="archive-school")

    def _subscribe(self, *, seats, days=365, active=True):
        plan = SubscriptionPlan.objects.create(
            name=f"سعة {seats}", price=100, days_duration=days, max_teachers=seats
        )
        subscription = SchoolSubscription.objects.create(school=self.school, plan=plan)
        if not active:
            # Write the past end date without going through save(), which owns
            # the date logic, then re-fetch the school: refresh_from_db() does
            # not reliably drop the cached reverse one-to-one, so the allowance
            # would still see the original subscription.
            SchoolSubscription.objects.filter(pk=subscription.pk).update(
                end_date=timezone.localdate() - timedelta(days=1)
            )
            subscription.refresh_from_db()
        self.school = School.objects.get(pk=self.school.pk)
        return subscription

    def _use(self, num_bytes):
        School.objects.filter(pk=self.school.pk).update(storage_used_bytes=num_bytes)
        self.school.refresh_from_db()

    def _archive_addon(self, *, active=True):
        return SchoolArchiveAddon.objects.create(
            school=self.school,
            is_enabled=True,
            start_date=timezone.localdate() - timedelta(days=30),
            end_date=(
                timezone.localdate() + timedelta(days=30)
                if active
                else timezone.localdate() - timedelta(days=1)
            ),
            storage_limit_gb=50,
        )

    def test_base_storage_scales_with_purchased_capacity(self):
        self._subscribe(seats=50)

        # 200 MB per seat x 50 seats
        self.assertEqual(school_storage_limit_bytes(self.school), 50 * 200 * MB)

    def test_a_bigger_capacity_gets_more_room(self):
        self._subscribe(seats=100)

        self.assertEqual(school_storage_limit_bytes(self.school), 100 * 200 * MB)

    def test_purchased_extra_storage_adds_on_top(self):
        self._subscribe(seats=25)
        School.objects.filter(pk=self.school.pk).update(extra_storage_gb=10)
        self.school.refresh_from_db()

        allowance = school_storage_allowance(self.school)

        self.assertEqual(allowance["base_bytes"], 25 * 200 * MB)
        self.assertEqual(allowance["extra_bytes"], 10 * GB)
        self.assertEqual(allowance["total_bytes"], 25 * 200 * MB + 10 * GB)

    def test_storage_does_not_depend_on_the_archive_addon(self):
        """The separation this whole change exists for."""
        self._subscribe(seats=50)
        expected = school_storage_limit_bytes(self.school)

        self._archive_addon(active=True)
        self.assertEqual(school_storage_limit_bytes(self.school), expected)

        SchoolArchiveAddon.objects.all().delete()
        self._archive_addon(active=False)
        self.assertEqual(school_storage_limit_bytes(self.school), expected)

    def test_an_expired_archive_addon_no_longer_freezes_uploads(self):
        """Previously the limit collapsed to the free tier and every upload in
        the platform was rejected."""
        self._subscribe(seats=50)
        self._archive_addon(active=False)
        self._use(5 * GB)

        self.assertEqual(archive_storage_capacity_error(self.school, [_Upload(2 * MB)]), "")

    def test_a_school_without_an_active_subscription_falls_back_to_the_floor(self):
        self._subscribe(seats=50, active=False)

        self.assertEqual(school_storage_limit_bytes(self.school), 1024 * MB)

    def test_over_limit_message_offers_raising_the_limit(self):
        self._subscribe(seats=25)
        self._use(5 * GB)

        error = archive_storage_capacity_error(self.school, [_Upload(2 * MB)])

        self.assertIn("زيادة مساحة العمل", error)

    def test_over_limit_message_offers_clearing_an_archived_year(self):
        self._subscribe(seats=25)
        SchoolYearArchive.objects.create(
            school=self.school,
            academic_year="1445",
            version=1,
            status=SchoolYearArchive.Status.READY,
        )
        old_report = Report.objects.create(
            school=self.school,
            teacher=Teacher.objects.create_user(
                phone="500044332", name="معلم", password="strong-pass-123"
            ),
            title="تقرير قديم",
            report_date=timezone.localdate() - timedelta(days=500),
            academic_year="1445",
        )
        Report.objects.filter(pk=old_report.pk).update(storage_bytes=3 * GB)
        self._use(5 * GB)

        error = archive_storage_capacity_error(self.school, [_Upload(2 * MB)])

        self.assertIn("1445", error)
        self.assertIn("نسخة سنوية محفوظة", error)

    def test_uploads_within_the_limit_pass(self):
        self._subscribe(seats=50)
        self._use(1 * GB)

        self.assertEqual(archive_storage_capacity_error(self.school, [_Upload(5 * MB)]), "")

    def test_replacing_a_file_frees_its_space_for_the_check(self):
        self._subscribe(seats=25)
        self._use(5 * GB)

        class _Existing:
            name = "old.bin"
            size = 200 * MB

        error = archive_storage_capacity_error(
            self.school, [_Upload(50 * MB)], replacing_files=[_Existing()]
        )

        self.assertEqual(error, "")


class ArchiveStorageOverviewTests(TestCase):
    def setUp(self):
        settings_obj = PlatformSettings.get_solo()
        settings_obj.free_storage_mb = 1024
        settings_obj.save(update_fields=["free_storage_mb"])
        self.school = School.objects.create(name="مدرسة العرض", code="overview-school")

    def test_overview_reports_usage_against_the_effective_limit(self):
        School.objects.filter(pk=self.school.pk).update(storage_used_bytes=512 * MB)

        overview = school_storage_overview(self.school)

        self.assertEqual(overview["limit_bytes"], 1024 * MB)
        self.assertEqual(overview["usage_percent"], 50.0)
        self.assertFalse(overview["is_unlimited"])

    def test_year_snapshots_are_counted_in_their_own_bucket(self):
        """A snapshot must never show up as work usage.

        storage_bytes is derived from the attached file by the tracking signals,
        so the snapshot has to carry a real one.
        """
        payload = b"x" * (2 * MB)
        archive = SchoolYearArchive(
            school=self.school,
            academic_year="1447",
            version=1,
            status=SchoolYearArchive.Status.READY,
        )
        archive.archive_file.save("snapshot.zip", ContentFile(payload), save=False)
        archive.save()

        overview = school_storage_overview(self.school)

        # The work card charges nothing for it...
        self.assertEqual(overview["used_bytes"], 0)
        self.assertNotIn("snapshots", overview["breakdown"])
        # ...but names it, so the space is not unaccounted for either.
        self.assertEqual(overview["snapshot_bytes"], len(payload))
        self.assertEqual(overview["total_held_bytes"], len(payload))

    def test_creating_a_snapshot_raises_the_school_total(self):
        archive = SchoolYearArchive(
            school=self.school,
            academic_year="1447",
            version=1,
            status=SchoolYearArchive.Status.READY,
        )
        archive.archive_file.save("snapshot.zip", ContentFile(b"y" * (3 * MB)), save=False)
        archive.save()

        self.school.refresh_from_db()
        self.assertEqual(self.school.storage_used_bytes, 3 * MB)

    def test_deleting_a_snapshot_returns_its_space(self):
        archive = SchoolYearArchive(
            school=self.school,
            academic_year="1447",
            version=1,
            status=SchoolYearArchive.Status.READY,
        )
        archive.archive_file.save("snapshot.zip", ContentFile(b"z" * (3 * MB)), save=False)
        archive.save()

        archive.delete()

        self.school.refresh_from_db()
        self.assertEqual(self.school.storage_used_bytes, 0)

    def test_unlimited_when_the_platform_sets_no_free_cap_and_no_addon(self):
        settings_obj = PlatformSettings.get_solo()
        settings_obj.free_storage_mb = 0
        settings_obj.save(update_fields=["free_storage_mb"])

        overview = school_storage_overview(self.school)

        self.assertTrue(overview["is_unlimited"])
        self.assertEqual(overview["usage_percent"], 0)


class ArchiveAddonExpiryReminderTests(TestCase):
    """Subscriptions were reminded before expiry; the add-on was not — even
    though its expiry is what stops uploads platform-wide."""

    def setUp(self):
        settings_obj = PlatformSettings.get_solo()
        settings_obj.free_storage_mb = 1024
        settings_obj.save(update_fields=["free_storage_mb"])

        self.school = School.objects.create(name="مدرسة التذكير", code="reminder-school")
        self.manager = Teacher.objects.create_user(
            phone="500055443", name="مدير الأرشيف", password="strong-pass-123"
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )

    def _addon(self, days_left, limit_gb=50):
        return SchoolArchiveAddon.objects.create(
            school=self.school,
            is_enabled=True,
            start_date=timezone.localdate() - timedelta(days=300),
            end_date=timezone.localdate() + timedelta(days=days_left),
            storage_limit_gb=limit_gb,
        )

    def _run(self):
        from django.core.cache import cache

        from reports.tasks import check_archive_addon_expiry_task

        # The task holds a 5-minute cache lock so two beat workers cannot run it
        # at once. Release it between calls so each invocation actually executes
        # and the 24-hour notification de-duplication is what is under test.
        cache.delete("periodic_lock:check_archive_addon_expiry")
        return check_archive_addon_expiry_task.apply().get()

    def _manager_notifications(self):
        return Notification.objects.filter(
            school=self.school, recipients__teacher=self.manager
        ).distinct()

    def test_manager_is_warned_on_the_reminder_days(self):
        self._addon(days_left=7)

        summary = self._run()

        self.assertEqual(summary["reminders_sent"], 1)
        self.assertEqual(self._manager_notifications().count(), 1)

    def test_no_warning_on_a_day_that_is_not_a_reminder_day(self):
        self._addon(days_left=9)

        summary = self._run()

        self.assertEqual(summary["reminders_sent"], 0)
        self.assertEqual(self._manager_notifications().count(), 0)

    def test_the_reminder_states_only_snapshot_creation_stops(self):
        self._addon(days_left=3)

        self._run()

        message = self._manager_notifications().first().message
        self.assertIn("إنشاء نسخة سنوية جديدة", message)

    def test_the_reminder_promises_storage_is_unaffected(self):
        """Storage is a separate product now, so lapsing must not be implied to
        threaten it — that would push managers into a renewal they do not need."""
        self._addon(days_left=3)
        School.objects.filter(pk=self.school.pk).update(storage_used_bytes=20 * GB)

        self._run()

        message = self._manager_notifications().first().message
        self.assertIn("مساحة التخزين لا تتأثر", message)
        self.assertNotIn("سيتوقف", message)

    def test_saved_snapshots_are_promised_to_remain_downloadable(self):
        self._addon(days_left=1)

        self._run()

        self.assertIn("قابلة للتنزيل", self._manager_notifications().first().message)

    def test_the_same_warning_is_not_repeated_within_a_day(self):
        self._addon(days_left=7)

        self._run()
        second = self._run()

        self.assertEqual(second["skipped_duplicate"], 1)
        self.assertEqual(self._manager_notifications().count(), 1)

    def test_an_open_ended_addon_is_never_warned_about(self):
        SchoolArchiveAddon.objects.create(
            school=self.school,
            is_enabled=True,
            start_date=timezone.localdate() - timedelta(days=10),
            end_date=None,
        )

        self.assertEqual(self._run()["reminders_sent"], 0)

    def test_an_already_expired_addon_is_not_re_warned(self):
        self._addon(days_left=-5)

        self.assertEqual(self._run()["reminders_sent"], 0)

    def test_the_reminder_is_scheduled(self):
        from django.conf import settings as django_settings

        schedule = getattr(django_settings, "CELERY_BEAT_SCHEDULE", {})
        self.assertIn("check-archive-addon-expiry-daily", schedule)
        self.assertEqual(
            schedule["check-archive-addon-expiry-daily"]["task"],
            "reports.tasks.check_archive_addon_expiry_task",
        )
