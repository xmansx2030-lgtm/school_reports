from __future__ import annotations

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from maintenance.services import collect_reset_summary, execute_school_year_reset
from reports.services_archive import archive_storage_capacity_error
from reports.models import (
    Report,
    Payment,
    PlatformSettings,
    ArchiveStorageOption,
    School,
    SchoolArchiveAddon,
    SchoolMembership,
    Teacher,
    TeacherAchievementFile,
)
from reports.tests.billing_test_helpers import checkout_submission_key, valid_receipt


class SchoolArchiveAddonTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(
            name="Archive School",
            code="archive-school",
            current_academic_year="1447-1448",
        )
        self.teacher = Teacher.objects.create_user(
            phone="0500000001",
            name="Archive Teacher",
            password="pass12345",
        )

    def test_school_year_reset_keeps_reports_and_achievements_when_archive_addon_active(self):
        SchoolArchiveAddon.objects.create(
            school=self.school,
            is_enabled=True,
            start_date=timezone.localdate(),
            paid_amount=100,
        )
        Report.objects.create(
            school=self.school,
            teacher=self.teacher,
            title="Archived Report",
            report_date=timezone.localdate(),
        )
        TeacherAchievementFile.objects.create(
            school=self.school,
            teacher=self.teacher,
            academic_year="1447-1448",
        )

        summary = collect_reset_summary(
            [self.school],
            {"reports": True, "achievements": True, "tickets": False, "notifications": False, "share_links": True},
        )
        self.assertEqual(summary["archive_protected_schools_count"], 1)
        self.assertEqual(summary["reports_count"], 0)
        self.assertEqual(summary["achievements_count"], 0)

        execute_school_year_reset(
            {
                "include_options": {
                    "reports": True,
                    "achievements": True,
                    "tickets": False,
                    "notifications": False,
                    "share_links": True,
                },
                "delete_files": False,
            },
            schools=[self.school],
        )

        self.assertEqual(Report.objects.filter(school=self.school).count(), 1)
        self.assertEqual(TeacherAchievementFile.objects.filter(school=self.school).count(), 1)

    def test_platform_archive_addons_pages_render_for_superuser(self):
        admin = Teacher.objects.create_superuser(
            phone="0500000999",
            name="Admin User",
            password="pass12345",
        )
        self.client.force_login(admin)

        list_response = self.client.get(reverse("reports:platform_archive_addons_list"))
        self.assertEqual(list_response.status_code, 200)

        add_response = self.client.get(reverse("reports:platform_archive_addon_add"))
        self.assertEqual(add_response.status_code, 200)

    def test_school_manager_can_request_archive_addon_and_admin_approval_activates_it(self):
        manager = Teacher.objects.create_user(
            phone="0500000002",
            name="Archive Manager",
            password="pass12345",
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )
        self.client.force_login(manager)

        page_response = self.client.get(reverse("reports:my_subscription"))
        self.assertEqual(page_response.status_code, 200)

        receipt = valid_receipt()
        response = self.client.post(
            reverse("reports:payment_create"),
            {
                "payment_kind": Payment.Purpose.ARCHIVE_ADDON,
                "notes": "تحويل الأرشفة",
                "checkout_submission_key": checkout_submission_key(self.client),
                "receipt_image": receipt,
            },
        )
        self.assertEqual(response.status_code, 302)

        payment = Payment.objects.get(school=self.school, purpose=Payment.Purpose.ARCHIVE_ADDON)
        self.assertEqual(payment.status, Payment.Status.PENDING)
        self.assertEqual(payment.amount, 399)

        admin = Teacher.objects.create_superuser(
            phone="0500000998",
            name="Platform Admin",
            password="pass12345",
        )
        self.client.force_login(admin)
        response = self.client.post(
            reverse("reports:platform_payment_detail", args=[payment.id]),
            {"status": Payment.Status.APPROVED, "notes": payment.notes},
        )
        self.assertEqual(response.status_code, 302)

        payment.refresh_from_db()
        addon = SchoolArchiveAddon.objects.get(school=self.school)
        self.assertEqual(payment.status, Payment.Status.APPROVED)
        self.assertTrue(addon.is_active)
        self.assertEqual(addon.storage_limit_gb, 50)

        self.client.force_login(manager)
        page_response = self.client.get(reverse("reports:my_subscription"))
        self.assertEqual(page_response.status_code, 200)

    def test_archive_payment_uses_platform_pricing_settings(self):
        settings_obj = PlatformSettings.get_solo()
        settings_obj.archive_addon_annual_price = 555
        settings_obj.archive_included_storage_gb = 80
        settings_obj.save()
        storage_option = ArchiveStorageOption.objects.create(
            storage_gb=50,
            price=90,
            sort_order=1,
            is_active=True,
        )

        manager = Teacher.objects.create_user(
            phone="0500000003",
            name="Pricing Manager",
            password="pass12345",
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )
        self.client.force_login(manager)

        receipt = valid_receipt()
        self.client.post(
            reverse("reports:payment_create"),
            {
                "payment_kind": Payment.Purpose.ARCHIVE_ADDON,
                "checkout_submission_key": checkout_submission_key(self.client),
                "receipt_image": receipt,
            },
        )

        payment = Payment.objects.get(school=self.school, purpose=Payment.Purpose.ARCHIVE_ADDON)
        self.assertEqual(payment.amount, 555)

        admin = Teacher.objects.create_superuser(
            phone="0500000997",
            name="Pricing Admin",
            password="pass12345",
        )
        self.client.force_login(admin)
        settings_response = self.client.get(reverse("reports:platform_settings"))
        self.assertEqual(settings_response.status_code, 200)

        self.client.post(
            reverse("reports:platform_payment_detail", args=[payment.id]),
            {"status": Payment.Status.APPROVED, "notes": ""},
        )

        addon = SchoolArchiveAddon.objects.get(school=self.school)
        self.assertEqual(addon.storage_limit_gb, 80)

        self.client.force_login(manager)
        storage_receipt = valid_receipt("storage.png")
        self.client.post(
            reverse("reports:payment_create"),
            {
                "payment_kind": Payment.Purpose.WORK_STORAGE,
                "archive_storage_option_id": str(storage_option.id),
                "checkout_submission_key": checkout_submission_key(self.client),
                "receipt_image": storage_receipt,
            },
        )
        storage_payment = Payment.objects.get(school=self.school, purpose=Payment.Purpose.WORK_STORAGE)
        self.assertEqual(storage_payment.amount, 90)
        self.assertEqual(storage_payment.archive_storage_gb, 50)

        self.client.force_login(admin)
        self.client.post(
            reverse("reports:platform_payment_detail", args=[storage_payment.id]),
            {"status": Payment.Status.APPROVED, "notes": ""},
        )

        # Bought space is credited to the school itself; the yearly-archive
        # add-on no longer carries the storage entitlement.
        self.school.refresh_from_db()
        self.assertEqual(self.school.extra_storage_gb, 50)

    def test_archive_capacity_uses_replacement_delta_not_double_counting(self):
        SchoolArchiveAddon.objects.create(
            school=self.school,
            is_enabled=True,
            start_date=timezone.localdate(),
            storage_limit_gb=1,
        )

        class Sized:
            def __init__(self, size):
                self.size = size
                self.name = "file.bin"

        mb = 1024 * 1024
        School.objects.filter(pk=self.school.pk).update(storage_used_bytes=800 * mb)
        msg = archive_storage_capacity_error(
            self.school,
            [Sized(700 * mb)],
            replacing_files=[Sized(800 * mb)],
        )
        self.assertEqual(msg, "")

        msg = archive_storage_capacity_error(self.school, [Sized(700 * mb)])
        self.assertIn("تم تجاوز حد مساحة عمل المدرسة", msg)
