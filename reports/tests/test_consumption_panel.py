"""الاستهلاك الثلاثي: التخزين والمقاعد والأرشفة.

الأرقام كانت متفرقة — التخزين في صفحة الأرشيف، والمقاعد في صفحة الاشتراك،
والأرشفة في لوحة المنصة — فلم يكن للمدير مكان واحد يرى فيه أين يقترب من حدّه.
هذه الاختبارات تثبّت الأرقام، وتثبّت أن الجهتين تقرآن المصدر نفسه.
"""

from __future__ import annotations

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from reports.models import (
    School,
    SchoolArchiveAddon,
    SchoolMembership,
    SchoolSubscription,
    SubscriptionPlan,
    Teacher,
)
from reports.services_archive import school_consumption_summary


@override_settings(ALLOWED_HOSTS=["testserver"])
class ConsumptionSummaryTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(name="مدرسة الاستهلاك", code="consumption")
        self.plan = SubscriptionPlan.objects.create(
            name="باقة 25", price=100, days_duration=365, max_teachers=25
        )
        SchoolSubscription.objects.create(school=self.school, plan=self.plan)

        self.manager = Teacher.objects.create_user(
            phone="0500000501", name="مدير المدرسة", password="Passw0rd!123"
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )

    def _add_teachers(self, count: int) -> None:
        for index in range(count):
            teacher = Teacher.objects.create_user(
                phone=f"05006000{index:02d}", name=f"معلم {index}", password="Passw0rd!123"
            )
            SchoolMembership.objects.create(
                school=self.school,
                teacher=teacher,
                role_type=SchoolMembership.RoleType.TEACHER,
            )

    # ---------------------------------------------------------------- المقاعد

    def test_seat_usage_counts_teachers_and_excludes_the_manager(self):
        self._add_teachers(5)

        seats = school_consumption_summary(self.school)["seats"]

        self.assertEqual(seats["used"], 5)
        self.assertEqual(seats["limit"], 25)
        self.assertEqual(seats["remaining"], 20)
        self.assertEqual(seats["usage_percent"], 20)
        self.assertFalse(seats["needs_attention"])

    def test_a_full_seat_allowance_is_flagged(self):
        self._add_teachers(25)

        seats = school_consumption_summary(self.school)["seats"]

        self.assertEqual(seats["used"], 25)
        self.assertEqual(seats["remaining"], 0)
        self.assertEqual(seats["warning_level"], "full")
        self.assertTrue(seats["needs_attention"])

    def test_zero_capacity_reads_as_unlimited_never_as_a_division(self):
        """سعة 0 تعني بلا حدّ في هذا المشروع، فالقسمة عليها خطأ."""
        unlimited_plan = SubscriptionPlan.objects.create(
            name="بلا حدّ", price=0, days_duration=365, max_teachers=0
        )
        SchoolSubscription.objects.filter(school=self.school).update(plan=unlimited_plan)
        self._add_teachers(3)

        # الكائن في الذاكرة يحتفظ بعلاقة الاشتراك القديمة، والعروض تقرأ المدرسة
        # من قاعدة البيانات في كل طلب — فنحاكي ذلك بدل قياس نسخة قديمة.
        seats = school_consumption_summary(School.objects.get(pk=self.school.pk))["seats"]

        self.assertTrue(seats["is_unlimited"])
        self.assertEqual(seats["usage_percent"], 0)
        self.assertEqual(seats["used"], 3)
        self.assertFalse(seats["needs_attention"])

    # -------------------------------------------------------------- الأرشفة

    def test_archive_reads_as_not_subscribed_without_the_addon(self):
        archive = school_consumption_summary(self.school)["archive"]

        self.assertFalse(archive["is_subscribed"])
        self.assertEqual(archive["limit_bytes"], 0)

    def test_archive_allowance_appears_once_the_addon_is_active(self):
        # is_active خاصية محسوبة من is_enabled والتواريخ، لا حقلاً يُكتب.
        SchoolArchiveAddon.objects.create(
            school=self.school, storage_limit_gb=10, is_enabled=True
        )

        archive = school_consumption_summary(self.school)["archive"]

        self.assertTrue(archive["is_subscribed"])
        self.assertEqual(archive["limit_bytes"], 10 * 1024 * 1024 * 1024)
        self.assertEqual(archive["used_bytes"], 0)

    def test_archive_and_work_buckets_stay_independent(self):
        """امتلاء الأرشفة يوقف النسخ السنوية وحدها، ولا يجمّد رفع المعلمين."""
        SchoolArchiveAddon.objects.create(
            school=self.school, storage_limit_gb=1, is_enabled=True
        )
        summary = school_consumption_summary(self.school)

        self.assertNotEqual(
            summary["archive"]["limit_bytes"], summary["storage"]["limit_bytes"]
        )

    # -------------------------------------------------------------- التخزين

    def test_storage_reports_a_limit_and_a_remainder(self):
        storage = school_consumption_summary(self.school)["storage"]

        self.assertGreater(storage["limit_bytes"], 0)
        self.assertFalse(storage["is_unlimited"])
        self.assertEqual(
            storage["remaining_bytes"],
            storage["limit_bytes"] - storage["used_bytes"],
        )
        self.assertTrue(storage["limit_label"])
        self.assertTrue(storage["remaining_label"])

    def test_a_school_without_a_subscription_still_reports_a_summary(self):
        SchoolSubscription.objects.filter(school=self.school).delete()

        summary = school_consumption_summary(School.objects.get(pk=self.school.pk))

        self.assertFalse(summary["has_subscription"])
        self.assertEqual(summary["seats"]["limit"], 0)
        self.assertIn("storage", summary)


@override_settings(ALLOWED_HOSTS=["testserver"])
class ConsumptionPanelSurfaceTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(name="مدرسة اللوحة", code="panel-school")
        plan = SubscriptionPlan.objects.create(
            name="باقة", price=100, days_duration=365, max_teachers=25
        )
        SchoolSubscription.objects.create(school=self.school, plan=plan)

        self.manager = Teacher.objects.create_user(
            phone="0500000601", name="مدير", password="Passw0rd!123"
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )

    def _use_school(self, user):
        self.client.force_login(user)
        session = self.client.session
        session["active_school_id"] = self.school.pk
        session.save()

    def test_the_manager_dashboard_shows_all_three_buckets(self):
        self._use_school(self.manager)

        response = self.client.get(reverse("reports:admin_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "الاستهلاك والمتبقي")
        self.assertContains(response, "مساحة العمل")
        self.assertContains(response, "مقاعد المعلمين")
        self.assertContains(response, "مساحة الأرشفة")

    def test_the_platform_school_page_shows_the_same_panel(self):
        owner = Teacher.objects.create_superuser(
            phone="0500000602", name="مالك النظام", password="Passw0rd!123"
        )
        self._use_school(owner)

        response = self.client.get(reverse("reports:platform_school_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "الاستهلاك والمتبقي")
        self.assertContains(response, "مقاعد المعلمين")

    def test_the_manager_sees_where_the_space_went(self):
        """نسبة 80% بلا تفصيل تخبر المدير أنه قارب الحدّ ولا تخبره ما يحرّره."""
        self._use_school(self.manager)

        response = self.client.get(reverse("reports:admin_dashboard"))

        self.assertContains(response, "أين ذهبت مساحة العمل؟")
        self.assertIn("breakdown", response.context["consumption"]["storage"])

    def test_the_schools_directory_shows_usage_without_entering_each_school(self):
        owner = Teacher.objects.create_superuser(
            phone="0500000604", name="مالك الدليل", password="Passw0rd!123"
        )
        self.client.force_login(owner)

        response = self.client.get(reverse("reports:platform_schools_directory"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="dir-usage"')
        row = next(s for s in response.context["schools"] if s.pk == self.school.pk)
        self.assertEqual(row.consumption_row["seats"]["used"], 0)
        self.assertFalse(row.consumption_row["seats"]["is_unlimited"])
        self.assertFalse(row.consumption_row["archive"]["is_subscribed"])
        self.assertIn("storage", row.consumption_row)

    def test_directory_queries_do_not_grow_with_the_number_of_schools(self):
        """استدعاء الملخّص الكامل لكل صف كان سيعني عشرات الاستعلامات في صفحة واحدة.

        القياس مقارنةٌ بين حجمين لا رقمٌ ثابت: الرقم الثابت يسقط عند أي تعديل
        مشروع في الصفحة، أما النمو مع عدد المدارس فهو الخلل الفعلي.
        """
        plan = SubscriptionPlan.objects.first()
        owner = Teacher.objects.create_superuser(
            phone="0500000605", name="مالك", password="Passw0rd!123"
        )
        self.client.force_login(owner)

        def _add(prefix: str, count: int) -> None:
            for index in range(count):
                school = School.objects.create(
                    name=f"مدرسة {prefix}{index}", code=f"dir-{prefix}-{index}"
                )
                SchoolSubscription.objects.create(school=school, plan=plan)

        _add("a", 3)
        # طلب تسخين: الطلب الأول يحمل تهيئة الجلسة وذاكرات الصلاحيات، فقياسه
        # يقارن شيئين مختلفين.
        self.client.get(reverse("reports:platform_schools_directory"))
        with CaptureQueriesContext(connection) as few:
            self.client.get(reverse("reports:platform_schools_directory"))

        _add("b", 9)
        with CaptureQueriesContext(connection) as many:
            self.client.get(reverse("reports:platform_schools_directory"))

        if len(many.captured_queries) > len(few.captured_queries):
            # الرقم وحده لا يُشخِّص. عرض الاستعلام الذي ظهر يحوّل «10 != 11»
            # إلى سطر SQL يُقرأ ويُصلَح — وهذا الاختبار سقط مرة في تشغيل كامل
            # ولم يُعَد إنتاجه منفرداً، فبقاؤه بلا تشخيص يعني تكرار البحث
            # من الصفر عند كل عودة.
            def _shapes(captured):
                import re

                return [
                    re.sub(r"\d+", "?", entry["sql"])[:180] for entry in captured
                ]

            before, after = _shapes(few.captured_queries), _shapes(many.captured_queries)
            extra = [item for item in after if item not in before] or [
                "(لا فرق في شكل الاستعلامات — تكرارٌ لاستعلام قائم)"
            ]
            details = "\n".join(extra)
            self.fail(
                f"عدد الاستعلامات نما من {len(before)} إلى {len(after)} بزيادة المدارس.\n"
                f"الاستعلامات التي ظهرت:\n{details}"
            )

    def test_both_surfaces_read_the_same_numbers(self):
        """رقمان مختلفان لنفس المدرسة يصنعان تذكرة دعم."""
        owner = Teacher.objects.create_superuser(
            phone="0500000603", name="مالك", password="Passw0rd!123"
        )

        self._use_school(self.manager)
        manager_view = self.client.get(reverse("reports:admin_dashboard"))
        self._use_school(owner)
        platform_view = self.client.get(reverse("reports:platform_school_dashboard"))

        self.assertEqual(
            manager_view.context["consumption"]["seats"]["limit"],
            platform_view.context["consumption"]["seats"]["limit"],
        )
        self.assertEqual(
            manager_view.context["consumption"]["storage"]["limit_bytes"],
            platform_view.context["consumption"]["storage"]["limit_bytes"],
        )
