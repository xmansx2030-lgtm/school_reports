import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from reports.models import (
    Report,
    ReportEvidence,
    School,
    SchoolMembership,
    SchoolSubscription,
    SubscriptionPlan,
    Teacher,
)


@override_settings(ALLOWED_HOSTS=["testserver"])
class ArchiveReportImageStatsTests(TestCase):
    year = "1447-1448"

    @classmethod
    def setUpTestData(cls):
        cls.school = School.objects.create(
            name="مدرسة إحصاء الصور",
            code="archive-image-stats",
            current_academic_year=cls.year,
        )
        cls.other_school = School.objects.create(
            name="مدرسة مستقلة",
            code="archive-image-stats-other",
            current_academic_year=cls.year,
        )
        plan = SubscriptionPlan.objects.create(
            name="خطة إحصاء الأرشيف", price=500, days_duration=365,
        )
        SchoolSubscription.objects.create(school=cls.school, plan=plan)
        cls.manager = Teacher.objects.create_user(
            phone="0500770101", name="مدير إحصاء الصور", password="safe-password",
        )
        cls.teacher = Teacher.objects.create_user(
            phone="0500770102", name="معلم إحصاء الصور", password="safe-password",
        )
        cls.other_teacher = Teacher.objects.create_user(
            phone="0500770103", name="معلم آخر", password="safe-password",
        )
        for teacher, role in (
            (cls.manager, SchoolMembership.RoleType.MANAGER),
            (cls.teacher, SchoolMembership.RoleType.TEACHER),
            (cls.other_teacher, SchoolMembership.RoleType.TEACHER),
        ):
            SchoolMembership.objects.create(
                school=cls.school, teacher=teacher, role_type=role,
            )

    def setUp(self):
        media = tempfile.TemporaryDirectory()
        self.addCleanup(media.cleanup)
        settings = override_settings(MEDIA_ROOT=media.name)
        settings.enable()
        self.addCleanup(settings.disable)
        self._login(self.manager)

    def _login(self, user):
        self.client.force_login(user)
        session = self.client.session
        session["active_school_id"] = self.school.pk
        session.save()

    @staticmethod
    def _image(name="evidence.png"):
        return SimpleUploadedFile(
            name,
            bytes.fromhex(
                "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
                "890000000a49444154789c6360000002000154a24f6f0000000049454e44ae426082"
            ),
            content_type="image/png",
        )

    def _report(self, title="تقرير موثق", **overrides):
        fields = {
            "school": self.school,
            "teacher": self.teacher,
            "title": title,
            "report_date": timezone.localdate(),
            "academic_year": self.year,
        }
        fields.update(overrides)
        return Report.objects.create(**fields)

    def _evidence(self, report, order=1, **overrides):
        fields = {"report": report, "order": order, "image": self._image()}
        fields.update(overrides)
        return ReportEvidence.objects.create(**fields)

    def _stats(self, **filters):
        response = self.client.get(
            reverse("reports:school_archive"), {"year": self.year, **filters},
        )
        self.assertEqual(response.status_code, 200)
        return response.context["report_stats"]

    def test_archive_page_counts_a_report_with_only_modern_image_evidence(self):
        report = self._report()
        self._evidence(report, show_in_print=False)

        self.assertFalse(any(getattr(report, f"image{i}") for i in range(1, 5)))
        self.assertEqual(self._stats(), {"total": 1, "with_images": 1})

    def test_modern_and_legacy_images_count_each_report_once_and_ignore_empty_evidence(self):
        modern = self._report("شواهد متعددة")
        for order in range(1, 4):
            self._evidence(modern, order=order)
        mixed = self._report("صور قديمة وحديثة", image1=self._image("legacy.png"))
        self._evidence(mixed)
        self._evidence(mixed, order=2)
        self._report("صورة قديمة فقط", image4=self._image("legacy-four.png"))
        empty = self._report("شاهد دون ملف")
        self._evidence(empty, image="")
        self._report("تقرير نصي")

        self.assertEqual(self._stats(), {"total": 5, "with_images": 3})

    def test_image_counts_keep_school_year_search_and_teacher_scope(self):
        own = self._report("تقرير مصور")
        self._evidence(own)
        self._report("تقرير نصي")
        colleague = self._report("تقرير زميل", teacher=self.other_teacher)
        self._evidence(colleague)
        historical = self._report("تقرير سابق", academic_year="1446-1447")
        self._evidence(historical)
        foreign = self._report("تقرير مدرسة أخرى", school=self.other_school)
        self._evidence(foreign)

        self.assertEqual(self._stats(), {"total": 3, "with_images": 2})
        self.assertEqual(self._stats(q="نصي"), {"total": 1, "with_images": 0})
        self.assertEqual(
            self._stats(teacher=self.teacher.pk), {"total": 2, "with_images": 1},
        )
        self.assertEqual(
            self._stats(year="1446-1447"), {"total": 1, "with_images": 1},
        )
        self._login(self.teacher)
        self.assertEqual(self._stats(), {"total": 2, "with_images": 1})
