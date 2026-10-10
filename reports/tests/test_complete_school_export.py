from __future__ import annotations

import hashlib
import json
import tempfile
import zipfile
from contextlib import ExitStack
from datetime import timedelta
from io import BytesIO
from unittest.mock import patch
from urllib.parse import unquote

from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from reports.models import (
    Assignment, AssignmentEvidence, AssignmentTarget, AuditLog, Document, GeneratedExportJob,
    Notification, NotificationRecipient, Payment, Report, ReportEvidence, School,
    SchoolArchiveAddon, SchoolMembership, SchoolSubscription, SchoolYearArchive,
    ShareLink, SubscriptionPlan, Teacher, TeacherAchievementFile,
)
from reports.services_export import build_school_export_zip_file, export_zip_filename
from reports.school_export_snapshot import school_original_file_fields, school_record_querysets


@override_settings(ALLOWED_HOSTS=["testserver"])
class CompleteSchoolExportTests(TestCase):
    def setUp(self):
        # Download response.close() and eager Celery cleanup emit lifecycle
        # signals inside TestCase's outer transaction. File-backed SQLite
        # otherwise closes that transaction's connection (in-memory SQLite
        # silently ignores close), masking later tests with closed-DB errors.
        connection_cleanup = patch("django.db.backends.base.base.BaseDatabaseWrapper.close_if_unusable_or_obsolete")
        connection_cleanup.start()
        self.addCleanup(connection_cleanup.stop)
        media = tempfile.TemporaryDirectory()
        self.addCleanup(media.cleanup)
        settings = override_settings(MEDIA_ROOT=media.name)
        settings.enable()
        self.addCleanup(settings.disable)
        self.school = School.objects.create(name="مدرسة الأمانة الرسمية", code="complete-school")
        self.other = School.objects.create(name="مدرسة أخرى", code="complete-other")
        self.manager = Teacher.objects.create_user(phone="0500710101", name="مدير المدرسة", password="manager-pass")
        self.teacher = Teacher.objects.create_user(phone="0500710102", name="معلم المدرسة", password="teacher-pass")
        self.other_teacher = Teacher.objects.create_user(phone="0500710103", name="معلم المدرسة الأخرى", password="other-pass")
        plan = SubscriptionPlan.objects.create(name="خطة التصدير", price=100, days_duration=365, max_teachers=20)
        SchoolSubscription.objects.create(school=self.school, plan=plan)
        SchoolSubscription.objects.create(school=self.other, plan=plan)
        SchoolMembership.objects.create(school=self.school, teacher=self.manager, role_type="manager")
        SchoolMembership.objects.create(school=self.school, teacher=self.teacher, role_type="teacher")
        SchoolMembership.objects.create(school=self.other, teacher=self.other_teacher, role_type="manager")
        for school in (self.school, self.other):
            SchoolArchiveAddon.objects.create(school=school, is_enabled=True, start_date=timezone.localdate(), end_date=timezone.localdate() + timedelta(days=30))
        self.report = Report.objects.create(school=self.school, teacher=self.teacher, title="تقرير السنة الأولى", academic_year="1446-1447", report_date=timezone.localdate())

    def _login(self, user=None, school=None):
        self.client.force_login(user or self.manager)
        session = self.client.session
        session["active_school_id"] = (school or self.school).pk
        session.save()

    def _build(self, **kwargs):
        with ExitStack() as stack:
            for target, value in (
                ("reports.pdf_report.generate_report_pdf", (b"%PDF-report", "report.pdf")),
                ("reports.pdf_achievement.generate_achievement_pdf", (b"%PDF-achievement", "achievement.pdf")),
                ("reports.pdf_archive_records.generate_notification_archive_pdf", b"%PDF-notification"),
            ):
                stack.enter_context(patch(target, return_value=value))
            stream, metadata = build_school_export_zip_file(self.school, return_metadata=True, **kwargs)
        self.addCleanup(stream.close)
        package = zipfile.ZipFile(stream)
        self.addCleanup(package.close)
        return package, metadata, json.loads(package.read("تقرير-الاكتمال.json"))

    def test_all_years_states_original_files_and_security_exclusions(self):
        self.teacher.national_id = "1234567890"
        self.teacher.save(update_fields=["national_id"])
        old_image = b"legacy-report-image"
        self.report.image1.save("legacy.png", ContentFile(old_image))
        ReportEvidence.objects.create(report=self.report, image=SimpleUploadedFile("new.png", b"new-evidence"))
        trashed = Report.objects.create(school=self.school, teacher=self.teacher, title="تقرير محفوظ في السلة", academic_year="1447-1448", report_date=timezone.localdate(), trashed_at=timezone.now())
        trashed.image1.save("trash.png", ContentFile(b"trash-original"))
        circular = Notification.objects.create(school=self.school, created_by=self.manager, title="تعميم رسمي", message="النص الرسمي", requires_signature=True)
        recipient = NotificationRecipient(notification=circular, teacher=self.teacher)
        recipient.signature_image.save("signature.png", ContentFile(b"signature-original"), save=False)
        recipient.save()
        payment = Payment(school=self.school, amount=100, created_by=self.manager, gateway_checkout_id="SECRET-CHECKOUT", notes="ملاحظات الإيصال المحفوظة")
        payment.receipt_image.save("receipt.png", ContentFile(b"receipt-original"), save=False)
        payment.save()
        saved = SchoolYearArchive(school=self.school, academic_year="1446-1447", version=1)
        saved.archive_file.save("annual.zip", ContentFile(b"saved-annual-zip"), save=False)
        saved.save()
        ShareLink.objects.create(school=self.school, report=self.report, kind="report", token="SECRET-SHARE", expires_at=timezone.now() + timedelta(days=3))
        AuditLog.objects.create(
            school=self.school, teacher=self.manager, action="update", model_name="SchoolMembership",
            changes={"role_type": {"before": "teacher", "after": "deputy"}, "password": "SECRET-PASSWORD", "support": {"kind": "school", "context": "SECRET-SUPPORT"}},
        )
        Report.objects.create(school=self.other, teacher=self.other_teacher, title="OTHER-SCHOOL-PRIVATE", academic_year="1448-1449", report_date=timezone.localdate())

        package, metadata, manifest = self._build()

        reports = json.loads(package.read("البيانات/Report.json"))
        self.assertEqual({row["id"] for row in reports}, {self.report.pk, trashed.pk})
        self.assertEqual(manifest["scope"], "all_school_years")
        self.assertEqual(manifest["records"]["Report"], 2)
        self.assertFalse(metadata["is_partial"])
        originals = {package.read(item["path"]) for item in manifest["original_files"]}
        self.assertTrue({old_image, b"new-evidence", b"trash-original", b"signature-original", b"receipt-original", b"saved-annual-zip"}.issubset(originals))
        users = json.loads(package.read("البيانات/Teacher.json"))
        self.assertEqual({row["id"] for row in users}, {self.manager.pk, self.teacher.pk})
        for row in users:
            self.assertNotIn("password", row)
            self.assertNotIn("current_session_key", row)
        teacher_row = next(row for row in users if row["id"] == self.teacher.pk)
        self.assertEqual(teacher_row["national_id"], self.teacher.national_id)
        all_metadata = b"\n".join(package.read(name) for name in package.namelist() if name.endswith(".json"))
        for secret in (b"SECRET-SHARE", b"SECRET-CHECKOUT", b"SECRET-PASSWORD", b"SECRET-SUPPORT", b"OTHER-SCHOOL-PRIVATE"):
            self.assertNotIn(secret, all_metadata)
        payment_rows = json.loads(package.read("البيانات/Payment.json"))
        self.assertEqual(payment_rows[0]["notes"], payment.notes)
        audit_rows = json.loads(package.read("البيانات/AuditLog.json"))
        changes = next(row["changes"] for row in audit_rows if row["changes"] and "role_type" in row["changes"])
        self.assertEqual(changes["role_type"], {"before": "teacher", "after": "deputy"})
        self.assertEqual(changes["support"]["kind"], "school")
        self.assertGreater(len(manifest["exclusions"]), 0)

    def test_registry_tenant_boundaries_cover_every_queryset(self):
        for school in (self.school, self.other):
            TeacherAchievementFile.objects.create(school=school, teacher=self.teacher if school == self.school else self.other_teacher, academic_year="1447-1448")
            Document.objects.create(school=school, owner=self.teacher if school == self.school else self.other_teacher, title=f"وثيقة {school.code}", file=SimpleUploadedFile(f"{school.code}.pdf", school.code.encode()))
        registry = dict(school_record_querysets(self.school))
        self.assertEqual(set(registry["School"].values_list("id", flat=True)), {self.school.pk})
        self.assertEqual(set(registry["Teacher"].values_list("id", flat=True)), {self.manager.pk, self.teacher.pk})
        for name in ("Report", "Document", "TeacherAchievementFile", "Payment", "SchoolYearArchive"):
            self.assertFalse(registry[name].exclude(school=self.school).exists(), name)
        original_names = [field.name for _, field in school_original_file_fields(self.school)]
        self.assertTrue(original_names)
        self.assertFalse(any(self.other.storage_key in name for name in original_names))

    def test_group_assignment_exports_only_current_school_targets_and_files(self):
        assignment = Assignment.objects.create(school=self.school, issuer=self.manager, title="تكليف متعدد المدارس", description="تفاصيل التنفيذ", due_at=timezone.now() + timedelta(days=3))
        mine = AssignmentTarget.objects.create(assignment=assignment, school=self.school, assignee=self.teacher)
        theirs = AssignmentTarget.objects.create(assignment=assignment, school=self.other, assignee=self.other_teacher)
        AssignmentEvidence.objects.create(target=mine, uploaded_by=self.teacher, file=SimpleUploadedFile("mine.pdf", b"MINE-ONLY"))
        AssignmentEvidence.objects.create(target=theirs, uploaded_by=self.other_teacher, file=SimpleUploadedFile("theirs.pdf", b"SIBLING-PRIVATE"))
        package, _metadata, manifest = self._build()
        targets = json.loads(package.read("البيانات/AssignmentTarget.json"))
        self.assertEqual({row["id"] for row in targets}, {mine.pk})
        self.assertEqual(manifest["records"]["AssignmentEvidence"], 1)
        self.assertNotIn(b"SIBLING-PRIVATE", b"\n".join(package.read(path) for path in package.namelist()))

    def test_missing_stored_file_is_reported_once_with_record_identity(self):
        missing = Document.objects.create(school=self.school, owner=self.teacher, title="وثيقة مفقودة", file="missing/not-on-disk.pdf")
        package, metadata, manifest = self._build()
        self.assertTrue(metadata["is_partial"])
        self.assertEqual(metadata["missing_file_count"], 1)
        self.assertEqual(len(manifest["missing_files"]), 1)
        issue = manifest["missing_files"][0]
        self.assertEqual((issue["model"], issue["record_id"], issue["field"]), ("reports.Document", missing.pk, "file"))
        self.assertIn("ملفات أصلية تعذرت قراءتها: 1", package.read("الفهرس-والتحقق.txt").decode())

    def test_manifest_hashes_match_actual_bytes_and_no_duplicate_zip_paths(self):
        # Identical display names formerly created the same achievement PDF path.
        self.manager.name = self.teacher.name
        self.manager.save(update_fields=["name"])
        for teacher in (self.manager, self.teacher):
            TeacherAchievementFile.objects.create(school=self.school, teacher=teacher, academic_year="1447-1448")
        package, _metadata, manifest = self._build()
        self.assertEqual(len(package.namelist()), len(set(package.namelist())))
        for item in manifest["files"]:
            data = package.read(item["path"])
            self.assertEqual(item["size_bytes"], len(data))
            self.assertEqual(item["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(sum(path.endswith(".pdf") and path.startswith("ملفات-الإنجاز/") for path in package.namelist()), 2)

    def test_annual_scope_does_not_include_other_years_or_full_school_registry(self):
        package, metadata, manifest = self._build(academic_year="1446-1447")
        self.assertEqual(metadata["report_count"], 1)
        self.assertNotIn("البيانات/Report.json", package.namelist())
        self.assertNotEqual(manifest["scope"], "all_school_years")
        self.assertNotIn("ملخص-البيانات.xlsx", package.namelist())

    def test_arabic_school_name_is_the_download_filename_and_header_is_safe(self):
        self._login()
        with patch("reports.views.exports.build_school_export_zip_file", return_value=(BytesIO(b"PK-test"), {"is_partial": False})):
            response = self.client.get(reverse("reports:school_data_export_zip"))
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, 200)
        self.assertIn("مدرسة الأمانة الرسمية.zip", unquote(response["Content-Disposition"]))
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.school.name = "../ مدرسة\r\nX-Evil: value"
        filename = export_zip_filename(self.school)
        self.assertNotIn("/", filename)
        self.assertNotIn("\r", filename)
        self.assertNotIn("\n", filename)

    def test_all_staff_roles_are_denied_complete_school_download(self):
        roles = (("teacher", "teacher"), ("deputy", ""), ("admin_staff", "admin_staff"), ("admin_staff", "lab_tech"))
        membership = SchoolMembership.objects.get(school=self.school, teacher=self.teacher)
        for role, job_title in roles:
            with self.subTest(role=role, job_title=job_title):
                membership.role_type = role
                membership.job_title = job_title
                membership.save()
                self._login(self.teacher)
                with patch("reports.views.exports.build_school_export_zip_file") as build:
                    response = self.client.get(reverse("reports:school_data_export_zip"))
                self.assertNotEqual(response.status_code, 200)
                build.assert_not_called()

    def test_ready_background_artifact_rechecks_manager_and_current_school(self):
        artifact = GeneratedExportJob(school=self.school, requested_by=self.manager, kind="school_zip", status="ready", filename="school.zip")
        artifact.artifact_file.save("school.zip", ContentFile(b"PRIVATE-ARCHIVE"), save=False)
        artifact.save()
        self._login()
        response = self.client.get(reverse("reports:school_data_export_zip"), {"job": artifact.pk})
        self.assertEqual(response.status_code, 200)
        response.close()
        SchoolMembership.objects.filter(school=self.other, teacher=self.other_teacher).update(is_active=False)
        SchoolMembership.objects.create(school=self.other, teacher=self.manager, role_type="manager")
        self._login(school=self.other)
        self.assertEqual(self.client.get(reverse("reports:school_data_export_zip"), {"job": artifact.pk}).status_code, 404)
        SchoolMembership.objects.filter(school=self.school, teacher=self.manager).update(role_type="teacher")
        self._login(school=self.school)
        self.assertNotEqual(self.client.get(reverse("reports:school_data_export_zip"), {"job": artifact.pk}).status_code, 200)
        # The annual compatibility URL cannot bypass full-school job checks.
        self.assertEqual(self.client.get(reverse("reports:school_archive_export"), {"job": artifact.pk}).status_code, 404)

    def test_background_worker_does_not_build_after_manager_access_is_revoked(self):
        from reports.services_generated_exports import build_generated_export_job

        job = GeneratedExportJob.objects.create(school=self.school, requested_by=self.manager, kind="school_zip")
        SchoolMembership.objects.filter(school=self.school, teacher=self.manager).update(role_type="teacher")
        with patch("reports.services_export.build_school_export_zip_file") as build:
            with self.assertRaisesMessage(ValueError, "لم تعد صلاحية تصدير المدرسة"):
                build_generated_export_job(job.pk)
        build.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.status, "failed")
        self.assertFalse(job.artifact_file.name)

    def test_background_worker_rechecks_access_after_long_build(self):
        from reports.services_generated_exports import build_generated_export_job

        job = GeneratedExportJob.objects.create(school=self.school, requested_by=self.manager, kind="school_zip")

        def revoked_during_build(*_args, **_kwargs):
            SchoolMembership.objects.filter(school=self.school, teacher=self.manager).update(role_type="teacher")
            return BytesIO(b"PK-private-data")

        with patch("reports.services_export.build_school_export_zip_file", side_effect=revoked_during_build):
            with self.assertRaisesMessage(ValueError, "لم تعد صلاحية تصدير المدرسة"):
                build_generated_export_job(job.pk)
        job.refresh_from_db()
        self.assertFalse(job.artifact_file.name)

    def test_expired_background_export_is_not_rebuilt(self):
        from reports.services_generated_exports import build_generated_export_job

        job = GeneratedExportJob.objects.create(school=self.school, requested_by=self.manager, kind="school_zip", status="expired")
        with patch("reports.services_export.build_school_export_zip_file") as build:
            self.assertFalse(build_generated_export_job(job.pk))
        build.assert_not_called()

    def test_manager_preview_describes_complete_scope_and_exclusions(self):
        self._login()
        response = self.client.get(reverse("reports:school_data_export"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "مدرسة الأمانة الرسمية.zip")
        self.assertContains(response, "تقرير-الاكتمال.json")
        self.assertContains(response, "جميع السنوات والحالات")
        self.assertContains(response, "صور التوقيعات")
        self.assertGreater(response.context["record_total"], 0)
