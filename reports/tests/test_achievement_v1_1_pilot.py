from datetime import date, timedelta
from io import BytesIO
from pathlib import Path
import tempfile
from unittest.mock import patch

from PIL import Image, features
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from reports.models import (
    AchievementEvidenceReport,
    AchievementEvidenceImage,
    AchievementSection,
    Report,
    ReportType,
    School,
    SchoolMembership,
    SchoolSubscription,
    ShareLink,
    SubscriptionPlan,
    Teacher,
    TeacherAchievementFile,
)
from reports.validators import MAX_IMAGE_BYTES


def _image_upload(name: str, image_format: str, content_type: str) -> SimpleUploadedFile:
    output = BytesIO()
    Image.new("RGB", (16, 16), (32, 96, 64)).save(output, format=image_format)
    return SimpleUploadedFile(name, output.getvalue(), content_type=content_type)


@override_settings(ALLOWED_HOSTS=["testserver"])
class AchievementPilotQueryTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(
            name="مدرسة قياس ملف الإنجاز",
            code="achievement-pilot-query",
            current_academic_year="1447-1448",
        )
        plan = SubscriptionPlan.objects.create(
            name="خطة قياس ملف الإنجاز",
            price=0,
            days_duration=30,
            max_teachers=0,
        )
        SchoolSubscription.objects.create(school=self.school, plan=plan)
        self.manager = Teacher.objects.create_user(
            phone="500880001",
            name="مدير قياس ملف الإنجاز",
            password="manager-pass",
            is_staff=True,
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )
        self.teacher = Teacher.objects.create_user(
            phone="500880002",
            name="معلم قياس ملف الإنجاز",
            password="teacher-pass",
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.teacher,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        self.report_type = ReportType.objects.create(
            school=self.school,
            code="achievement-pilot-query",
            name="تقارير قياس ملف الإنجاز",
        )
        self.files = []
        for index in range(6):
            owner = self.teacher
            if index:
                owner = Teacher.objects.create_user(
                    phone=f"50088100{index}",
                    name=f"معلم قياس {index}",
                    password="teacher-pass",
                )
                SchoolMembership.objects.create(
                    school=self.school,
                    teacher=owner,
                    role_type=SchoolMembership.RoleType.TEACHER,
                )
            achievement_file = TeacherAchievementFile.objects.create(
                teacher=owner,
                school=self.school,
                academic_year="1447-1448",
            )
            self.files.append(achievement_file)
            for code, _label in AchievementSection.Code.choices:
                section = AchievementSection.objects.create(
                    file=achievement_file,
                    code=code,
                    teacher_notes="موثق" if code % 2 else "",
                )
                if owner == self.teacher and code <= 3:
                    report = Report.objects.create(
                        teacher=self.teacher,
                        school=self.school,
                        teacher_name=self.teacher.name,
                        category=self.report_type,
                        title=f"تقرير قياس {code}",
                        report_date=date.today(),
                        academic_year="1447-1448",
                    )
                    AchievementEvidenceReport.objects.create(section=section, report=report)

    def _enter(self, user):
        self.client.force_login(user)
        session = self.client.session
        session["active_school_id"] = self.school.pk
        session.save()

    def test_query_budget_is_bounded_for_realistic_portfolio_fixture(self):
        self._enter(self.teacher)
        with CaptureQueriesContext(connection) as teacher_queries:
            response = self.client.get(reverse("reports:achievement_my_files"))
        self.assertEqual(response.status_code, 200)

        with CaptureQueriesContext(connection) as detail_queries:
            response = self.client.get(
                reverse("reports:achievement_file_detail", args=[self.files[0].pk])
            )
        self.assertEqual(response.status_code, 200)

        self.client.logout()
        self._enter(self.manager)
        with CaptureQueriesContext(connection) as review_queries:
            response = self.client.get(reverse("reports:achievement_school_files"))
        self.assertEqual(response.status_code, 200)

        print(
            "ACHIEVEMENT_QUERY_COUNTS",
            f"teacher={len(teacher_queries)}",
            f"review={len(review_queries)}",
            f"detail={len(detail_queries)}",
        )
        self.assertLessEqual(len(teacher_queries), 36)
        self.assertLessEqual(len(review_queries), 35)
        self.assertLessEqual(len(detail_queries), 20)


@override_settings(ALLOWED_HOSTS=["testserver"])
class AchievementPilotTokenBoundaryTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(
            name="مدرسة حدود المشاركة",
            code="achievement-pilot-share",
            current_academic_year="1447-1448",
        )
        self.teacher = Teacher.objects.create_user(
            phone="500882001",
            name="معلم حدود المشاركة",
            password="teacher-pass",
        )
        self.achievement_file = TeacherAchievementFile.objects.create(
            teacher=self.teacher,
            school=self.school,
            academic_year="1447-1448",
        )
        for code, _label in AchievementSection.Code.choices:
            AchievementSection.objects.create(file=self.achievement_file, code=code)

    def _link(self, *, active=True, expires_at=None):
        return ShareLink.objects.create(
            token=ShareLink.generate_token(),
            kind=ShareLink.Kind.ACHIEVEMENT,
            created_by=self.teacher,
            school=self.school,
            achievement_file=self.achievement_file,
            is_active=active,
            expires_at=expires_at or timezone.now() + timedelta(days=1),
        )

    def test_invalid_expired_and_revoked_tokens_do_not_expose_the_portfolio(self):
        invalid = self.client.get(reverse("reports:share_public", args=["not-a-token"]))
        expired_link = self._link(expires_at=timezone.now() - timedelta(minutes=1))
        expired = self.client.get(reverse("reports:share_public", args=[expired_link.token]))
        revoked_link = self._link(active=False)
        revoked = self.client.get(reverse("reports:share_public", args=[revoked_link.token]))

        self.assertEqual(invalid.status_code, 404)
        self.assertEqual(expired.status_code, 404)
        self.assertEqual(revoked.status_code, 404)

        invalid_pdf = self.client.get(
            reverse("reports:share_achievement_pdf", args=["not-a-token"])
        )
        expired_pdf = self.client.get(
            reverse("reports:share_achievement_pdf", args=[expired_link.token])
        )
        revoked_pdf = self.client.get(
            reverse("reports:share_achievement_pdf", args=[revoked_link.token])
        )
        self.assertEqual(invalid_pdf.status_code, 404)
        self.assertEqual(expired_pdf.status_code, 404)
        self.assertEqual(revoked_pdf.status_code, 404)

    def test_public_portfolio_has_no_internal_review_controls(self):
        link = self._link()

        response = self.client.get(reverse("reports:share_public", args=[link.token]))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="action" value="approve"')
        self.assertNotContains(response, 'name="action" value="return"')
        self.assertNotContains(response, "تعليقات خاصة")


@override_settings(ALLOWED_HOSTS=["testserver"])
class AchievementPilotIsolationTests(TestCase):
    def setUp(self):
        plan = SubscriptionPlan.objects.create(
            name="خطة عزل ملف الإنجاز",
            price=0,
            days_duration=30,
            max_teachers=0,
        )
        self.school_a = School.objects.create(
            name="مدرسة ملف الإنجاز أ",
            code="achievement-pilot-a",
            current_academic_year="1447-1448",
        )
        self.school_b = School.objects.create(
            name="مدرسة ملف الإنجاز ب",
            code="achievement-pilot-b",
            current_academic_year="1447-1448",
        )
        SchoolSubscription.objects.create(school=self.school_a, plan=plan)
        SchoolSubscription.objects.create(school=self.school_b, plan=plan)
        self.teacher_a = Teacher.objects.create_user(
            phone="500883001",
            name="معلم العزل أ",
            password="teacher-pass",
        )
        self.teacher_b = Teacher.objects.create_user(
            phone="500883002",
            name="معلم العزل ب",
            password="teacher-pass",
        )
        self.manager_a = Teacher.objects.create_user(
            phone="500883003",
            name="مدير العزل أ",
            password="manager-pass",
            is_staff=True,
        )
        for school in (self.school_a, self.school_b):
            SchoolMembership.objects.create(
                school=school,
                teacher=self.teacher_a,
                role_type=SchoolMembership.RoleType.TEACHER,
            )
        SchoolMembership.objects.create(
            school=self.school_a,
            teacher=self.teacher_b,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        SchoolMembership.objects.create(
            school=self.school_a,
            teacher=self.manager_a,
            role_type=SchoolMembership.RoleType.MANAGER,
        )
        SchoolMembership.objects.create(
            school=self.school_b,
            teacher=self.manager_a,
            role_type=SchoolMembership.RoleType.MANAGER,
        )
        self.file_a = TeacherAchievementFile.objects.create(
            teacher=self.teacher_a,
            school=self.school_a,
            academic_year="1447-1448",
        )
        self.file_a_old = TeacherAchievementFile.objects.create(
            teacher=self.teacher_a,
            school=self.school_a,
            academic_year="1446-1447",
        )
        self.file_b = TeacherAchievementFile.objects.create(
            teacher=self.teacher_b,
            school=self.school_a,
            academic_year="1447-1448",
        )
        for achievement_file in (self.file_a, self.file_a_old, self.file_b):
            for code, _label in AchievementSection.Code.choices:
                AchievementSection.objects.create(file=achievement_file, code=code)

    def _enter(self, user, school):
        self.client.force_login(user)
        session = self.client.session
        session["active_school_id"] = school.pk
        session.save()

    def test_active_school_blocks_private_read_update_delete_picker_print_and_pdf(self):
        self._enter(self.teacher_a, self.school_b)
        original_year = self.file_a.academic_year

        detail = self.client.get(
            reverse("reports:achievement_file_detail", args=[self.file_a.pk])
        )
        picker = self.client.get(
            reverse("reports:achievement_report_picker", args=[self.file_a.pk]),
            {"section_id": self.file_a.sections.first().pk},
        )
        printed = self.client.get(
            reverse("reports:achievement_file_print", args=[self.file_a.pk])
        )
        pdf = self.client.get(reverse("reports:achievement_file_pdf", args=[self.file_a.pk]))
        updated = self.client.post(
            reverse("reports:achievement_file_update_year", args=[self.file_a.pk]),
            {"academic_year": "1447-1448"},
        )
        deleted = self.client.post(
            reverse("reports:achievement_file_delete", args=[self.file_a.pk])
        )

        self.assertEqual(detail.status_code, 404)
        self.assertEqual(picker.status_code, 404)
        self.assertEqual(printed.status_code, 404)
        self.assertEqual(pdf.status_code, 404)
        self.assertEqual(updated.status_code, 302)
        self.assertEqual(deleted.status_code, 302)
        self.file_a.refresh_from_db()
        self.assertEqual(self.file_a.academic_year, original_year)
        self.assertTrue(TeacherAchievementFile.objects.filter(pk=self.file_a.pk).exists())

    def test_foreign_image_and_report_ids_cannot_mutate_the_target_file(self):
        self._enter(self.teacher_a, self.school_a)
        section_a = self.file_a.sections.first()
        section_b = self.file_a_old.sections.first()
        foreign_image = AchievementEvidenceImage.objects.create(
            section=section_b,
            image="achievements/test/foreign.jpg",
        )
        report_type = ReportType.objects.create(
            school=self.school_b,
            code="foreign-achievement-report",
            name="تقرير مدرسة أخرى",
        )
        foreign_report = Report.objects.create(
            teacher=self.teacher_a,
            school=self.school_b,
            teacher_name=self.teacher_a.name,
            category=report_type,
            title="تقرير خارج المدرسة النشطة",
            report_date=date.today(),
            academic_year="1447-1448",
        )

        image_response = self.client.post(
            reverse("reports:achievement_file_detail", args=[self.file_a.pk]),
            {"action": "delete_evidence", "image_id": foreign_image.pk},
        )
        report_response = self.client.post(
            reverse("reports:achievement_file_detail", args=[self.file_a.pk]),
            {
                "action": "add_report_evidence",
                "section_id": section_a.pk,
                "report_id": foreign_report.pk,
            },
        )

        self.assertEqual(image_response.status_code, 404)
        self.assertEqual(report_response.status_code, 404)
        self.assertTrue(AchievementEvidenceImage.objects.filter(pk=foreign_image.pk).exists())
        self.assertFalse(
            AchievementEvidenceReport.objects.filter(
                section=section_a,
                report=foreign_report,
            ).exists()
        )

    def test_cross_teacher_and_unauthorized_review_posts_are_rejected(self):
        self._enter(self.teacher_b, self.school_a)
        cross_teacher = self.client.post(
            reverse("reports:achievement_file_detail", args=[self.file_a.pk]),
            {"action": "save_section", "section_id": self.file_a.sections.first().pk},
        )
        unauthorized_review = self.client.post(
            reverse("reports:achievement_file_detail", args=[self.file_b.pk]),
            {"action": "approve", "manager_notes": "محاولة غير مصرح بها"},
        )

        self.assertEqual(cross_teacher.status_code, 403)
        self.assertIn(unauthorized_review.status_code, {200, 403})
        self.file_b.refresh_from_db()
        self.assertEqual(self.file_b.status, TeacherAchievementFile.Status.DRAFT)

    def test_approved_file_rejects_owner_mutation_posts(self):
        self.file_a.status = TeacherAchievementFile.Status.APPROVED
        self.file_a.save(update_fields=["status", "updated_at"])
        self._enter(self.teacher_a, self.school_a)

        response = self.client.post(
            reverse("reports:achievement_file_detail", args=[self.file_a.pk]),
            {"action": "save_general", "qualifications": "قيمة معدلة"},
        )

        self.assertEqual(response.status_code, 403)
        self.file_a.refresh_from_db()
        self.assertNotEqual(self.file_a.qualifications, "قيمة معدلة")

    def test_csrf_is_required_for_sensitive_mutations(self):
        client = self.client_class(enforce_csrf_checks=True)
        client.force_login(self.teacher_a)
        session = client.session
        session["active_school_id"] = self.school_a.pk
        session.save()

        response = client.post(
            reverse("reports:achievement_file_detail", args=[self.file_a.pk]),
            {"action": "submit"},
        )

        self.assertEqual(response.status_code, 403)
        self.file_a.refresh_from_db()
        self.assertEqual(self.file_a.status, TeacherAchievementFile.Status.DRAFT)

    def test_share_management_owner_is_scoped_to_active_school_for_get_and_post(self):
        self._enter(self.teacher_a, self.school_a)
        allowed = self.client.get(
            reverse("reports:achievement_share_manage", args=[self.file_a.pk])
        )
        enabled = self.client.post(
            reverse("reports:achievement_share_manage", args=[self.file_a.pk]),
            {"action": "enable", "expiry_days": "7"},
        )
        link = ShareLink.objects.get(
            achievement_file=self.file_a,
            kind=ShareLink.Kind.ACHIEVEMENT,
            is_active=True,
        )
        disabled = self.client.post(
            reverse("reports:achievement_share_manage", args=[self.file_a.pk]),
            {"action": "disable"},
        )
        link.refresh_from_db()

        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(enabled.status_code, 302)
        self.assertEqual(disabled.status_code, 302)
        self.assertFalse(link.is_active)

        self._enter(self.teacher_a, self.school_b)
        wrong_school_get = self.client.get(
            reverse("reports:achievement_share_manage", args=[self.file_a.pk])
        )
        wrong_school_post = self.client.post(
            reverse("reports:achievement_share_manage", args=[self.file_a.pk]),
            {"action": "enable", "expiry_days": "7"},
        )

        self.assertEqual(wrong_school_get.status_code, 404)
        self.assertEqual(wrong_school_post.status_code, 404)
        self.assertFalse(
            ShareLink.objects.filter(
                achievement_file=self.file_a,
                is_active=True,
            ).exists()
        )

    def test_share_management_preserves_owner_only_contract(self):
        url = reverse("reports:achievement_share_manage", args=[self.file_a.pk])

        self._enter(self.teacher_b, self.school_a)
        foreign_teacher = self.client.get(url)

        self._enter(self.manager_a, self.school_a)
        manager_correct_school = self.client.get(url)

        self._enter(self.manager_a, self.school_b)
        manager_wrong_school = self.client.get(url)

        self.assertEqual(foreign_teacher.status_code, 404)
        self.assertEqual(manager_correct_school.status_code, 404)
        self.assertEqual(manager_wrong_school.status_code, 404)

    def test_image_upload_rejects_invalid_payloads_without_persistence(self):
        self._enter(self.teacher_a, self.school_a)
        section = self.file_a.sections.first()
        cases = {
            "renamed executable": SimpleUploadedFile(
                "renamed.jpg", b"MZ" + b"\x00" * 128, content_type="image/jpeg"
            ),
            "invalid binary": SimpleUploadedFile(
                "invalid.png", b"not-an-image", content_type="image/png"
            ),
            "oversized": SimpleUploadedFile(
                "oversized.png",
                b"\x89PNG\r\n\x1a\n" + b"0" * MAX_IMAGE_BYTES,
                content_type="image/png",
            ),
            "mime mismatch": _image_upload(
                "mismatch.png", "PNG", "application/octet-stream"
            ),
            "unsupported extension": _image_upload("unsupported.gif", "GIF", "image/gif"),
        }

        with tempfile.TemporaryDirectory() as media_root, self.settings(MEDIA_ROOT=media_root):
            for label, payload in cases.items():
                with self.subTest(case=label):
                    response = self.client.post(
                        reverse("reports:achievement_file_detail", args=[self.file_a.pk]),
                        {
                            "action": "upload_evidence",
                            "section_id": section.pk,
                            "images": payload,
                        },
                    )
                    self.assertEqual(response.status_code, 302)
                    self.assertFalse(
                        AchievementEvidenceImage.objects.filter(section=section).exists()
                    )
                    self.assertEqual(
                        [path for path in Path(media_root).rglob("*") if path.is_file()],
                        [],
                    )

    def test_image_upload_accepts_valid_jpeg_png_and_supported_webp(self):
        self._enter(self.teacher_a, self.school_a)
        section = self.file_a.sections.first()
        uploads = [
            _image_upload("valid.jpg", "JPEG", "image/jpeg"),
            _image_upload("valid.png", "PNG", "image/png"),
        ]
        if features.check("webp"):
            uploads.append(_image_upload("valid.webp", "WEBP", "image/webp"))

        with tempfile.TemporaryDirectory() as media_root, self.settings(MEDIA_ROOT=media_root):
            response = self.client.post(
                reverse("reports:achievement_file_detail", args=[self.file_a.pk]),
                {
                    "action": "upload_evidence",
                    "section_id": section.pk,
                    "images": uploads,
                },
            )

            self.assertEqual(response.status_code, 302)
            self.assertEqual(
                AchievementEvidenceImage.objects.filter(section=section).count(),
                len(uploads),
            )
            self.assertEqual(
                len([path for path in Path(media_root).rglob("*") if path.is_file()]),
                len(uploads),
            )

    def test_mixed_valid_and_invalid_upload_is_all_or_nothing(self):
        self._enter(self.teacher_a, self.school_a)
        section = self.file_a.sections.first()
        uploads = [
            _image_upload("valid.png", "PNG", "image/png"),
            SimpleUploadedFile("invalid.png", b"invalid", content_type="image/png"),
        ]

        with tempfile.TemporaryDirectory() as media_root, self.settings(MEDIA_ROOT=media_root):
            response = self.client.post(
                reverse("reports:achievement_file_detail", args=[self.file_a.pk]),
                {
                    "action": "upload_evidence",
                    "section_id": section.pk,
                    "images": uploads,
                },
            )

            self.assertEqual(response.status_code, 302)
            self.assertFalse(AchievementEvidenceImage.objects.filter(section=section).exists())
            self.assertEqual(
                [path for path in Path(media_root).rglob("*") if path.is_file()],
                [],
            )

    def test_upload_storage_is_cleaned_when_later_save_fails(self):
        self._enter(self.teacher_a, self.school_a)
        section = self.file_a.sections.first()
        uploads = [
            _image_upload("first.png", "PNG", "image/png"),
            _image_upload("second.png", "PNG", "image/png"),
        ]
        original_save = AchievementEvidenceImage.save
        save_calls = 0

        def fail_second_save(instance, *args, **kwargs):
            nonlocal save_calls
            save_calls += 1
            if save_calls == 2:
                raise OSError("simulated storage/database failure")
            return original_save(instance, *args, **kwargs)

        with tempfile.TemporaryDirectory() as media_root, self.settings(MEDIA_ROOT=media_root):
            with patch.object(AchievementEvidenceImage, "save", new=fail_second_save):
                response = self.client.post(
                    reverse("reports:achievement_file_detail", args=[self.file_a.pk]),
                    {
                        "action": "upload_evidence",
                        "section_id": section.pk,
                        "images": uploads,
                    },
                )

            self.assertEqual(response.status_code, 302)
            self.assertFalse(AchievementEvidenceImage.objects.filter(section=section).exists())
            self.assertEqual(
                [path for path in Path(media_root).rglob("*") if path.is_file()],
                [],
            )

    def test_submitted_file_can_be_approved_and_returned(self):
        self._enter(self.manager_a, self.school_a)

        self.file_a.status = TeacherAchievementFile.Status.SUBMITTED
        self.file_a.save(update_fields=["status", "updated_at"])
        approved = self.client.post(
            reverse("reports:achievement_file_detail", args=[self.file_a.pk]),
            {"action": "approve", "manager_notes": "اعتماد صحيح"},
        )
        self.file_a.refresh_from_db()
        self.assertEqual(approved.status_code, 302)
        self.assertEqual(self.file_a.status, TeacherAchievementFile.Status.APPROVED)
        self.assertEqual(self.file_a.decided_by, self.manager_a)

        self.file_a.status = TeacherAchievementFile.Status.SUBMITTED
        self.file_a.save(update_fields=["status", "updated_at"])
        returned = self.client.post(
            reverse("reports:achievement_file_detail", args=[self.file_a.pk]),
            {"action": "return", "manager_notes": "أكمل الشواهد"},
        )
        self.file_a.refresh_from_db()
        self.assertEqual(returned.status_code, 302)
        self.assertEqual(self.file_a.status, TeacherAchievementFile.Status.RETURNED)
        self.assertEqual(self.file_a.manager_notes, "أكمل الشواهد")

    def test_approve_rejects_draft_returned_approved_and_stale_posts(self):
        self._enter(self.manager_a, self.school_a)
        for state in (
            TeacherAchievementFile.Status.DRAFT,
            TeacherAchievementFile.Status.RETURNED,
            TeacherAchievementFile.Status.APPROVED,
        ):
            with self.subTest(state=state):
                TeacherAchievementFile.objects.filter(pk=self.file_a.pk).update(
                    status=state,
                    manager_notes="ملاحظة سابقة",
                )
                response = self.client.post(
                    reverse("reports:achievement_file_detail", args=[self.file_a.pk]),
                    {"action": "approve", "manager_notes": "قرار غير صالح"},
                )
                self.assertEqual(response.status_code, 302)
                self.file_a.refresh_from_db()
                self.assertEqual(self.file_a.status, state)
                self.assertEqual(self.file_a.manager_notes, "ملاحظة سابقة")

        # A reviewer may have loaded the submitted page before another request
        # returned it. The old approval POST must use the current DB state.
        TeacherAchievementFile.objects.filter(pk=self.file_a.pk).update(
            status=TeacherAchievementFile.Status.RETURNED
        )
        stale = self.client.post(
            reverse("reports:achievement_file_detail", args=[self.file_a.pk]),
            {"action": "approve", "manager_notes": "طلب قديم"},
        )
        self.assertEqual(stale.status_code, 302)
        self.file_a.refresh_from_db()
        self.assertEqual(self.file_a.status, TeacherAchievementFile.Status.RETURNED)

    def test_return_rejects_draft_returned_and_approved(self):
        self._enter(self.manager_a, self.school_a)
        for state in (
            TeacherAchievementFile.Status.DRAFT,
            TeacherAchievementFile.Status.RETURNED,
            TeacherAchievementFile.Status.APPROVED,
        ):
            with self.subTest(state=state):
                TeacherAchievementFile.objects.filter(pk=self.file_a.pk).update(
                    status=state,
                    manager_notes="ملاحظة سابقة",
                )
                response = self.client.post(
                    reverse("reports:achievement_file_detail", args=[self.file_a.pk]),
                    {"action": "return", "manager_notes": "إرجاع غير صالح"},
                )
                self.assertEqual(response.status_code, 302)
                self.file_a.refresh_from_db()
                self.assertEqual(self.file_a.status, state)
                self.assertEqual(self.file_a.manager_notes, "ملاحظة سابقة")

    def test_workflow_guard_rejects_foreign_school_and_unauthorized_reviewer(self):
        self.file_a.status = TeacherAchievementFile.Status.SUBMITTED
        self.file_a.save(update_fields=["status", "updated_at"])

        self._enter(self.manager_a, self.school_b)
        foreign_school = self.client.post(
            reverse("reports:achievement_file_detail", args=[self.file_a.pk]),
            {"action": "approve", "manager_notes": "مدرسة خاطئة"},
        )

        self._enter(self.teacher_b, self.school_a)
        unauthorized = self.client.post(
            reverse("reports:achievement_file_detail", args=[self.file_a.pk]),
            {"action": "return", "manager_notes": "مراجع غير مخول"},
        )

        self.assertEqual(foreign_school.status_code, 404)
        self.assertEqual(unauthorized.status_code, 403)
        self.file_a.refresh_from_db()
        self.assertEqual(self.file_a.status, TeacherAchievementFile.Status.SUBMITTED)


class AchievementPilotTemplateDebtTests(TestCase):
    interactive_templates = (
        "reports/templates/reports/achievement_my_files.html",
        "reports/templates/reports/achievement_school_files.html",
        "reports/templates/reports/achievement_file.html",
        "reports/templates/reports/achievement_share_manage.html",
        "reports/templates/reports/partials/achievement_report_picker_list.html",
    )

    def test_interactive_pilot_templates_have_no_embedded_css_or_behavior_js(self):
        root = Path(settings.BASE_DIR)
        for relative_path in self.interactive_templates:
            source = (root / relative_path).read_text(encoding="utf-8")
            with self.subTest(template=relative_path):
                self.assertNotIn("<style", source)
                self.assertNotIn("<script nonce=", source)
                self.assertNotIn(" style=", source)

    def test_new_achievement_css_uses_tokens_and_logical_properties(self):
        source = (Path(settings.BASE_DIR) / "static/css/achievement.css").read_text(
            encoding="utf-8"
        )
        self.assertNotRegex(source, r"#[0-9a-fA-F]{3,8}\b|rgba?\(")
        self.assertNotRegex(
            source,
            r"(?m)^\s*(?:left|right|margin-left|margin-right|padding-left|padding-right)\s*:",
        )
        self.assertNotIn("transition: all", source)

    def test_new_achievement_javascript_has_no_direct_style_writes(self):
        source = (Path(settings.BASE_DIR) / "static/js/achievement.js").read_text(
            encoding="utf-8"
        )
        self.assertNotIn(".style.", source)
        self.assertNotIn("setAttribute(\"style\"", source)
