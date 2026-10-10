"""School storage as its own product, independent of yearly archiving.

Storage used to live on SchoolArchiveAddon: a school could not buy space without
buying yearly archiving, and space it had paid for vanished when that add-on
lapsed. These tests pin the separation and the warnings that replace it.
"""
from __future__ import annotations

from datetime import timedelta

from django.core.files.base import ContentFile
from django.test import TestCase
from django.utils import timezone
from reports.academic_years import school_current_academic_year

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
    reclaimable_storage_by_year,
    school_storage_overview,
)

GB = 1024 ** 3
MB = 1024 ** 2


class ReclaimableStorageTests(TestCase):
    """"Delete old files" is only safe advice when a snapshot preserves them."""

    def setUp(self):
        self.school = School.objects.create(
            name="مدرسة التنظيف", code="cleanup-school", current_academic_year="1447"
        )
        self.teacher = Teacher.objects.create_user(
            phone="500033221", name="معلم", password="strong-pass-123"
        )

    def _snapshot(self, year):
        return SchoolYearArchive.objects.create(
            school=self.school,
            academic_year=year,
            version=1,
            status=SchoolYearArchive.Status.READY,
        )

    def _report(self, year, size):
        report = Report.objects.create(
            school=self.school,
            teacher=self.teacher,
            title=f"تقرير {year}",
            report_date=timezone.localdate() - timedelta(days=400),
            academic_year=year,
        )
        # storage_bytes is recomputed from the attached files on save, so a value
        # passed to create() is discarded. Write it without firing the signals.
        Report.objects.filter(pk=report.pk).update(storage_bytes=size)
        return report

    def test_a_year_with_a_snapshot_is_reclaimable(self):
        self._snapshot("1445")
        self._report("1445", 500 * MB)

        rows = reclaimable_storage_by_year(self.school)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["academic_year"], "1445")
        self.assertEqual(rows[0]["bytes"], 500 * MB)

    def test_a_year_without_a_snapshot_is_never_suggested(self):
        self._report("1446", 500 * MB)

        self.assertEqual(reclaimable_storage_by_year(self.school), [])

    def test_the_current_year_is_never_suggested_even_with_a_snapshot(self):
        current_year = school_current_academic_year(self.school)
        self._snapshot(current_year)
        self._report(current_year, 500 * MB)

        self.assertEqual(reclaimable_storage_by_year(self.school), [])

    def test_years_are_listed_newest_first(self):
        self._snapshot("1444")
        self._report("1444", 100 * MB)
        self._snapshot("1445")
        self._report("1445", 800 * MB)

        rows = reclaimable_storage_by_year(self.school)

        self.assertEqual([row["academic_year"] for row in rows], ["1445", "1444"])

    def test_a_snapshotted_year_holding_nothing_is_omitted(self):
        self._snapshot("1445")

        self.assertEqual(reclaimable_storage_by_year(self.school), [])


class StorageWarningLevelTests(TestCase):
    """The manager must learn about the limit before an upload fails."""

    def setUp(self):
        settings_obj = PlatformSettings.get_solo()
        settings_obj.storage_mb_per_teacher = 200
        settings_obj.free_storage_mb = 1024
        settings_obj.save(update_fields=["storage_mb_per_teacher", "free_storage_mb"])

        self.school = School.objects.create(name="مدرسة التنبيه", code="alert-school")
        plan = SubscriptionPlan.objects.create(
            name="سعة 25", price=100, days_duration=365, max_teachers=25
        )
        SchoolSubscription.objects.create(school=self.school, plan=plan)
        self.limit = 25 * 200 * MB

    def _at(self, percent):
        School.objects.filter(pk=self.school.pk).update(
            storage_used_bytes=int(self.limit * percent / 100)
        )
        self.school.refresh_from_db()
        return school_storage_overview(self.school)

    def test_quiet_below_the_warning_threshold(self):
        overview = self._at(70)

        self.assertFalse(overview["needs_attention"])
        self.assertEqual(overview["warning_level"], "ok")

    def test_warns_at_eighty_percent(self):
        overview = self._at(85)

        self.assertTrue(overview["needs_attention"])
        self.assertEqual(overview["warning_level"], "warning")

    def test_escalates_near_the_limit(self):
        overview = self._at(97)

        self.assertEqual(overview["warning_level"], "critical")

    def test_reports_full_when_the_limit_is_reached(self):
        overview = self._at(100)

        self.assertEqual(overview["warning_level"], "full")

    def test_overview_explains_where_the_allowance_comes_from(self):
        overview = self._at(85)

        self.assertEqual(overview["seats"], 25)
        self.assertEqual(overview["base_bytes"], self.limit)
        self.assertEqual(overview["extra_bytes"], 0)


class StorageThresholdAlertTaskTests(TestCase):
    def setUp(self):
        settings_obj = PlatformSettings.get_solo()
        settings_obj.storage_mb_per_teacher = 200
        settings_obj.save(update_fields=["storage_mb_per_teacher"])

        self.school = School.objects.create(name="مدرسة الإنذار", code="threshold-school")
        plan = SubscriptionPlan.objects.create(
            name="سعة 25", price=100, days_duration=365, max_teachers=25
        )
        SchoolSubscription.objects.create(school=self.school, plan=plan)
        self.manager = Teacher.objects.create_user(
            phone="500022110", name="مدير التنبيه", password="strong-pass-123"
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )
        self.limit = 25 * 200 * MB

    def _use_percent(self, percent):
        # ``storage_used_bytes`` هو الإجمالي على القرص، ومساحة العمل = الإجمالي
        # ناقص النسخ السنوية. فلضبط استهلاك العمل عند نسبة ما، تُضاف النسخ
        # المحفوظة إلى المطلوب — وإلا صار العمل صفراً كلما وُجدت نسخة كبيرة.
        from django.db.models import Sum

        snapshots = int(
            SchoolYearArchive.objects.filter(school=self.school)
            .aggregate(total=Sum("storage_bytes"))
            .get("total")
            or 0
        )
        School.objects.filter(pk=self.school.pk).update(
            storage_used_bytes=int(self.limit * percent / 100) + snapshots
        )

    def _run(self):
        from django.core.cache import cache

        from reports.tasks import check_storage_thresholds_task

        # Release the concurrency lock so each call runs; the de-duplication
        # under test is the notification one, not the lock.
        cache.delete("periodic_lock:check_storage_thresholds")
        return check_storage_thresholds_task.apply().get()

    def _messages(self):
        return list(
            Notification.objects.filter(
                school=self.school, recipients__teacher=self.manager
            )
            .distinct()
            .values_list("message", flat=True)
        )

    def test_no_alert_while_there_is_room(self):
        self._use_percent(50)

        self.assertEqual(self._run()["warnings_sent"], 0)

    def test_manager_is_alerted_near_the_limit(self):
        self._use_percent(85)

        self.assertEqual(self._run()["warnings_sent"], 1)
        self.assertEqual(len(self._messages()), 1)

    def test_alert_names_both_ways_out(self):
        self._use_percent(85)

        self._run()

        message = self._messages()[0]
        self.assertIn("رفع حد مساحة العمل", message)
        self.assertIn("نسخة", message)

    def test_alert_points_at_a_reclaimable_year_when_one_exists(self):
        SchoolYearArchive.objects.create(
            school=self.school,
            academic_year="1445",
            version=1,
            status=SchoolYearArchive.Status.READY,
        )
        old_report = Report.objects.create(
            school=self.school,
            teacher=self.manager,
            title="تقرير قديم",
            report_date=timezone.localdate() - timedelta(days=500),
            academic_year="1445",
        )
        Report.objects.filter(pk=old_report.pk).update(storage_bytes=500 * MB)
        self._use_percent(85)

        self._run()

        self.assertIn("1445", self._messages()[0])

    def test_a_full_school_is_told_uploads_are_stopped(self):
        self._use_percent(100)

        self._run()

        self.assertIn("متوقف", self._messages()[0])

    def test_the_same_level_is_not_repeated(self):
        self._use_percent(85)

        self._run()
        second = self._run()

        self.assertEqual(second["skipped_duplicate"], 1)
        self.assertEqual(len(self._messages()), 1)

    def test_crossing_into_a_worse_level_alerts_again(self):
        self._use_percent(85)
        self._run()

        self._use_percent(97)
        second = self._run()

        self.assertEqual(second["warnings_sent"], 1)
        self.assertEqual(len(self._messages()), 2)

    def test_the_task_is_scheduled(self):
        from django.conf import settings as django_settings

        schedule = getattr(django_settings, "CELERY_BEAT_SCHEDULE", {})

        self.assertIn("check-storage-thresholds-daily", schedule)
        self.assertEqual(
            schedule["check-storage-thresholds-daily"]["task"],
            "reports.tasks.check_storage_thresholds_task",
        )

    # ------------------------------------------------ المساحة الثانية تُنذر أيضاً

    def _archive_addon(self, *, limit_gb=10):
        return SchoolArchiveAddon.objects.create(
            school=self.school,
            is_enabled=True,
            start_date=timezone.localdate() - timedelta(days=1),
            end_date=timezone.localdate() + timedelta(days=90),
            storage_limit_gb=limit_gb,
        )

    def _fill_archive(self, percent, *, limit_gb=10):
        archive = SchoolYearArchive(
            school=self.school,
            academic_year="1446-1447",
            version=1,
            status=SchoolYearArchive.Status.READY,
        )
        archive.archive_file.save("snap.zip", ContentFile(b"x"), save=False)
        archive.save()
        SchoolYearArchive.objects.filter(pk=archive.pk).update(
            storage_bytes=int(limit_gb * 1024 * MB * percent / 100)
        )
        return archive

    def test_the_archive_space_warns_before_it_blocks_archiving(self):
        """كانت تمتلئ صامتةً حتى تفشل أرشفة سنةٍ كاملة — وهي عملية سنوية،
        فاكتشاف العطل عندها يعني تأجيلها لا إصلاحها."""
        self._archive_addon(limit_gb=10)
        self._fill_archive(85)

        self.assertEqual(self._run()["warnings_sent"], 1)
        message = self._messages()[0]
        self.assertIn("مساحة الأرشفة السنوية", message)
        self.assertIn("لا يؤثر ذلك على عمل المعلمين", message)

    def test_a_full_archive_says_only_archiving_stopped(self):
        self._archive_addon(limit_gb=10)
        self._fill_archive(100)

        self._run()

        message = self._messages()[0]
        self.assertIn("حفظ أي نسخة سنوية جديدة متوقف", message)

    def test_each_space_gets_its_own_alert(self):
        self._archive_addon(limit_gb=10)
        self._fill_archive(97)
        self._use_percent(85)

        summary = self._run()

        self.assertEqual(summary["warnings_sent"], 2)
        joined = "\n".join(self._messages())
        self.assertIn("مساحة عمل", joined)
        self.assertIn("مساحة الأرشفة السنوية", joined)

    def test_a_full_archive_never_alerts_about_the_work_space(self):
        self._archive_addon(limit_gb=10)
        self._fill_archive(100)
        self._use_percent(20)

        self._run()

        self.assertNotIn("رفع أي ملف جديد متوقف", "\n".join(self._messages()))

    def test_a_school_without_the_archive_service_is_never_nagged(self):
        self._use_percent(20)

        self.assertEqual(self._run()["warnings_sent"], 0)
