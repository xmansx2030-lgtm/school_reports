import datetime
import tempfile
from io import StringIO
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import transaction
from django.test import TransactionTestCase

from reports.file_cleanup import _model_file_fields, delete_file_if_unreferenced
from reports.models import (
    AchievementEvidenceImage,
    AchievementEvidenceReport,
    LeadershipEvidenceImage,
    Notification,
    Payment,
    Report,
    ReportEvidence,
    RequestTicket,
    School,
    SchoolYearArchive,
    Teacher,
    TeacherAchievementFile,
    Ticket,
    TicketImage,
)


def _png():
    return bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000a49444154789c6360000002000154a24f6f0000000049454e44ae426082"
    )


class StorageObjectCleanupTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.storage = FileSystemStorage(location=self.temp_dir.name)
        self.image_field = Report._meta.get_field("image1")
        self.original_storage = self.image_field.storage
        self.image_field.storage = self.storage
        self.addCleanup(self._restore_storage)

        self.school = School.objects.create(name="مدرسة الحذف", code="cleanup-school")
        self.teacher = Teacher.objects.create_user(
            phone="0500555000",
            name="مستخدم الحذف",
            password="safe-password",
        )

    def _restore_storage(self):
        self.image_field.storage = self.original_storage

    def _report(self, filename="evidence.png"):
        with patch("reports.utils.run_task_safe"):
            return Report.objects.create(
                school=self.school,
                teacher=self.teacher,
                title="تقرير",
                report_date=datetime.date(2026, 1, 1),
                image1=SimpleUploadedFile(
                    filename,
                    _png(),
                    content_type="image/png",
                ),
            )

    def test_deleting_record_deletes_physical_storage_object_after_commit(self):
        report = self._report()
        name = report.image1.name
        self.assertTrue(self.storage.exists(name))

        report.delete()

        self.assertFalse(self.storage.exists(name))

    def test_replacing_file_deletes_old_object_and_keeps_new_object(self):
        report = self._report("old.png")
        old_name = report.image1.name
        report.image1 = SimpleUploadedFile(
            "new.png",
            _png() + b"new",
            content_type="image/png",
        )
        with patch("reports.utils.run_task_safe"):
            report.save(update_fields=["image1"])
        new_name = report.image1.name

        self.assertNotEqual(old_name, new_name)
        self.assertFalse(self.storage.exists(old_name))
        self.assertTrue(self.storage.exists(new_name))

    def test_clearing_file_field_deletes_old_object(self):
        report = self._report()
        old_name = report.image1.name
        report.image1 = None
        with patch("reports.utils.run_task_safe"):
            report.save(update_fields=["image1"])

        self.assertFalse(self.storage.exists(old_name))

    def test_rollback_keeps_database_row_and_storage_object(self):
        report = self._report()
        report_id = report.pk
        name = report.image1.name

        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                report.delete()
                self.assertTrue(self.storage.exists(name))
                raise RuntimeError("rollback")

        self.assertTrue(Report.objects.filter(pk=report_id).exists())
        self.assertTrue(self.storage.exists(name))

    def test_shared_object_is_not_deleted_until_last_reference_is_removed(self):
        first = self._report()
        name = first.image1.name
        second = Report.objects.create(
            school=self.school,
            teacher=self.teacher,
            title="مرجع ثانٍ",
            report_date=datetime.date(2026, 1, 2),
        )
        Report.objects.filter(pk=second.pk).update(image1=name)

        first.delete()
        self.assertTrue(self.storage.exists(name))

        second.refresh_from_db()
        second.delete()
        self.assertFalse(self.storage.exists(name))

    def test_update_fields_does_not_delete_unpersisted_in_memory_change(self):
        report = self._report()
        name = report.image1.name
        report.image1 = None
        report.title = "تعديل نصي"
        with patch("reports.utils.run_task_safe"):
            report.save(update_fields=["title"])

        report.refresh_from_db()
        self.assertEqual(report.image1.name, name)
        self.assertTrue(self.storage.exists(name))

    def test_transient_storage_error_schedules_retry_task(self):
        report = self._report()
        name = report.image1.name
        with (
            patch.object(self.storage, "delete", side_effect=OSError("R2 unavailable")),
            patch("reports.file_cleanup.run_named_task_safe") as run_task,
        ):
            report.delete()

        run_task.assert_called_once()
        args = run_task.call_args.args
        self.assertEqual(
            args,
            (
                "reports.tasks.delete_orphaned_storage_file_task",
                delete_file_if_unreferenced,
                "reports.Report",
                "image1",
                name,
            ),
        )
        self.assertTrue(self.storage.exists(name))

    def test_cleanup_retry_task_name_remains_backward_compatible(self):
        from reports.tasks import delete_orphaned_storage_file_task

        self.assertEqual(
            delete_orphaned_storage_file_task.name,
            "reports.tasks.delete_orphaned_storage_file_task",
        )

    def test_cascade_delete_removes_ticket_attachment_and_images(self):
        attachment_field = Ticket._meta.get_field("attachment")
        ticket_image_field = TicketImage._meta.get_field("image")
        original_attachment_storage = attachment_field.storage
        original_image_storage = ticket_image_field.storage
        attachment_field.storage = self.storage
        ticket_image_field.storage = self.storage
        try:
            ticket = Ticket.objects.create(
                school=self.school,
                creator=self.teacher,
                title="طلب بمرفقات",
                attachment=SimpleUploadedFile(
                    "request.pdf",
                    b"%PDF-request",
                    content_type="application/pdf",
                ),
            )
            with patch("reports.tasks.process_ticket_image.apply_async"):
                ticket_image = TicketImage.objects.create(
                    ticket=ticket,
                    image=SimpleUploadedFile(
                        "request.png",
                        _png(),
                        content_type="image/png",
                    ),
                )
            attachment_name = ticket.attachment.name
            image_name = ticket_image.image.name
            self.assertTrue(self.storage.exists(attachment_name))
            self.assertTrue(self.storage.exists(image_name))

            ticket.delete()

            self.assertFalse(self.storage.exists(attachment_name))
            self.assertFalse(self.storage.exists(image_name))
        finally:
            attachment_field.storage = original_attachment_storage
            ticket_image_field.storage = original_image_storage

    def test_all_project_file_models_are_discovered_automatically(self):
        expected = {
            Report: {"image1", "image2", "image3", "image4"},
            ReportEvidence: {"image"},
            TeacherAchievementFile: {"pdf_file"},
            AchievementEvidenceImage: {"image"},
            AchievementEvidenceReport: {
                "archived_image1",
                "archived_image2",
                "archived_image3",
                "archived_image4",
            },
            LeadershipEvidenceImage: {"image"},
            Ticket: {"attachment"},
            TicketImage: {"image"},
            RequestTicket: {"attachment"},
            Notification: {"attachment"},
            Payment: {"receipt_image"},
            SchoolYearArchive: {"archive_file"},
        }
        for model, field_names in expected.items():
            self.assertEqual(
                {field.name for field in _model_file_fields(model)},
                field_names,
            )

    def test_legacy_orphan_cleanup_command_is_dry_run_by_default(self):
        orphan_name = self.storage.save(
            "reports/legacy-orphan.png",
            ContentFile(_png()),
        )
        stdout = StringIO()
        with patch(
            "reports.management.commands.cleanup_orphaned_files._all_file_fields",
            return_value=[(Report, self.image_field)],
        ):
            call_command(
                "cleanup_orphaned_files",
                "--prefix=reports",
                stdout=stdout,
            )
        self.assertTrue(self.storage.exists(orphan_name))
        self.assertIn("معاينة فقط", stdout.getvalue())

        stdout = StringIO()
        with patch(
            "reports.management.commands.cleanup_orphaned_files._all_file_fields",
            return_value=[(Report, self.image_field)],
        ):
            call_command(
                "cleanup_orphaned_files",
                "--prefix=reports",
                "--delete",
                stdout=stdout,
            )
        self.assertFalse(self.storage.exists(orphan_name))
        self.assertIn("حذف فعلي", stdout.getvalue())
