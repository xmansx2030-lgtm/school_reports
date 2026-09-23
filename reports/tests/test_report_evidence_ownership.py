from __future__ import annotations

from datetime import date
from io import BytesIO
from pathlib import Path
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from reports.forms import BaseReportEvidenceFormSet
from reports.models import (
    Report,
    ReportEvidence,
    ReportType,
    School,
    SchoolArchiveAddon,
    SchoolMembership,
    SchoolSubscription,
    SubscriptionPlan,
    Teacher,
)


def _image(name: str, color=(18, 108, 60)) -> SimpleUploadedFile:
    output = BytesIO()
    Image.new("RGB", (320, 180), color).save(output, format="PNG")
    return SimpleUploadedFile(name, output.getvalue(), content_type="image/png")


class ReportEvidenceOwnershipTests(TestCase):
    def setUp(self):
        self.media = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.media.cleanup)
        self.media_override = override_settings(MEDIA_ROOT=self.media.name)
        self.media_override.enable()
        self.addCleanup(self.media_override.disable)

        self.school = School.objects.create(name="مدرسة ملكية الشواهد", code="evidence-owner")
        self.other_school = School.objects.create(
            name="مدرسة شواهد أخرى", code="evidence-owner-other"
        )
        plan = SubscriptionPlan.objects.create(
            name="خطة ملكية الشواهد", price=0, days_duration=30, max_teachers=20
        )
        for school in (self.school, self.other_school):
            SchoolSubscription.objects.create(school=school, plan=plan)
            SchoolArchiveAddon.objects.create(
                school=school, is_enabled=True, storage_limit_gb=10
            )

        self.owner = Teacher.objects.create_user(
            phone="500781001", name="صاحب التقرير", password="test-pass"
        )
        self.other_owner = Teacher.objects.create_user(
            phone="500781002", name="صاحب تقرير آخر", password="test-pass"
        )
        self.manager = Teacher.objects.create_user(
            phone="500781003", name="مدير المدرسة", password="test-pass", is_staff=True
        )
        self.platform_owner = Teacher.objects.create_superuser(
            phone="500781004", name="مالك المنصة", password="test-pass"
        )
        for teacher, role in (
            (self.owner, SchoolMembership.RoleType.TEACHER),
            (self.other_owner, SchoolMembership.RoleType.TEACHER),
            (self.manager, SchoolMembership.RoleType.MANAGER),
        ):
            SchoolMembership.objects.create(
                school=self.school, teacher=teacher, role_type=role
            )

        self.report_type = ReportType.objects.create(
            school=self.school, code="ownership-type", name="نوع اختبار الملكية"
        )
        self.other_report_type = ReportType.objects.create(
            school=self.other_school,
            code="ownership-other-type",
            name="نوع المدرسة الأخرى",
        )
        self.report = self._report(
            school=self.school,
            owner=self.owner,
            category=self.report_type,
            title="التقرير المستهدف",
        )
        self.same_school_report = self._report(
            school=self.school,
            owner=self.other_owner,
            category=self.report_type,
            title="تقرير آخر في المدرسة",
        )
        self.cross_school_report = self._report(
            school=self.other_school,
            owner=self.other_owner,
            category=self.other_report_type,
            title="تقرير المدرسة الأخرى",
        )

    def _report(self, *, school, owner, category, title):
        return Report.objects.create(
            school=school,
            teacher=owner,
            teacher_name=owner.name,
            title=title,
            report_date=date(2026, 9, 21),
            category=category,
            show_details=True,
            idea="تفاصيل التقرير الأصلية.",
        )

    def _login(self, user, *, school=None):
        self.client.force_login(user)
        session = self.client.session
        session["active_school_id"] = (school or self.school).pk
        session.save()

    def _report_payload(self, report=None, **overrides):
        report = report or self.report
        payload = {
            "section_selection_enabled": "1",
            "title": report.title,
            "report_date": report.report_date.isoformat(),
            "day_name": report.day_name or "الأحد",
            "category": report.category.code,
            "show_details": "on",
            "idea": report.idea,
        }
        payload.update(overrides)
        return payload

    def _with_evidence_rows(self, payload, rows):
        payload.update(
            {
                "evidence-TOTAL_FORMS": str(len(rows)),
                "evidence-INITIAL_FORMS": str(len(rows)),
                "evidence-MIN_NUM_FORMS": "0",
                "evidence-MAX_NUM_FORMS": "8",
            }
        )
        for index, row in enumerate(rows):
            evidence = row["evidence"]
            payload.update(
                {
                    f"evidence-{index}-id": str(evidence.pk),
                    f"evidence-{index}-order": str(row.get("order", index + 1)),
                    f"evidence-{index}-description": row.get(
                        "description", evidence.description
                    ),
                    f"evidence-{index}-display_size": row.get(
                        "display_size", ReportEvidence.DisplaySize.AUTO
                    ),
                    f"evidence-{index}-fit_mode": row.get(
                        "fit_mode", ReportEvidence.FitMode.CONTAIN
                    ),
                }
            )
            if row.get("show_in_print", True):
                payload[f"evidence-{index}-show_in_print"] = "on"
            if row.get("delete"):
                payload[f"evidence-{index}-DELETE"] = "on"
            if row.get("image"):
                payload[f"evidence-{index}-image"] = row["image"]
        return payload

    def _post(self, payload, *, report=None):
        return self.client.post(
            reverse("reports:edit_my_report", args=[(report or self.report).pk]),
            payload,
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

    def _assert_ownership_error(self, response):
        self.assertEqual(response.status_code, 422)
        self.assertContains(
            response,
            BaseReportEvidenceFormSet.ownership_error_message,
            status_code=422,
        )

    def test_foreign_delete_fails_atomically_before_any_mutation(self):
        own = ReportEvidence.objects.create(
            report=self.report, image=_image("own-delete.png"), order=1, description="شاهد أصلي"
        )
        foreign = ReportEvidence.objects.create(
            report=self.same_school_report,
            image=_image("foreign-delete.png"),
            order=1,
            description="شاهد تقرير آخر",
        )
        own_name = own.image.name
        foreign_name = foreign.image.name
        self._login(self.owner)
        payload = self._with_evidence_rows(
            self._report_payload(title="عنوان يجب ألا يُحفظ"),
            [
                {"evidence": own, "delete": True},
                {"evidence": foreign, "delete": True},
            ],
        )

        response = self._post(payload)

        self._assert_ownership_error(response)
        self.report.refresh_from_db()
        own.refresh_from_db()
        foreign.refresh_from_db()
        self.assertEqual(self.report.title, "التقرير المستهدف")
        self.assertEqual(own.report, self.report)
        self.assertEqual(foreign.report, self.same_school_report)
        self.assertTrue(own.image.storage.exists(own_name))
        self.assertTrue(foreign.image.storage.exists(foreign_name))

    def test_foreign_replace_and_metadata_mutation_are_rejected(self):
        foreign = ReportEvidence.objects.create(
            report=self.same_school_report,
            image=_image("foreign-replace.png"),
            order=1,
            description="الوصف الأصلي",
            show_in_print=True,
        )
        original_name = foreign.image.name
        files_before = sorted(
            path.relative_to(self.media.name)
            for path in Path(self.media.name).rglob("*")
            if path.is_file()
        )
        self._login(self.owner)
        payload = self._with_evidence_rows(
            self._report_payload(title="تعديل مرفوض"),
            [
                {
                    "evidence": foreign,
                    "image": _image("replacement.png", color=(120, 80, 20)),
                    "description": "وصف متلاعب به",
                    "order": 7,
                    "display_size": ReportEvidence.DisplaySize.LARGE,
                    "fit_mode": ReportEvidence.FitMode.COVER,
                    "show_in_print": False,
                }
            ],
        )

        response = self._post(payload)

        self._assert_ownership_error(response)
        self.report.refresh_from_db()
        foreign.refresh_from_db()
        self.assertEqual(self.report.title, "التقرير المستهدف")
        self.assertEqual(foreign.description, "الوصف الأصلي")
        self.assertEqual(foreign.order, 1)
        self.assertEqual(foreign.display_size, ReportEvidence.DisplaySize.AUTO)
        self.assertEqual(foreign.fit_mode, ReportEvidence.FitMode.CONTAIN)
        self.assertTrue(foreign.show_in_print)
        self.assertEqual(foreign.image.name, original_name)
        files_after = sorted(
            path.relative_to(self.media.name)
            for path in Path(self.media.name).rglob("*")
            if path.is_file()
        )
        self.assertEqual(files_after, files_before)

    def test_cross_school_evidence_uses_same_safe_error_without_leakage(self):
        foreign = ReportEvidence.objects.create(
            report=self.cross_school_report,
            image=_image("cross-school.png"),
            order=1,
            description="وصف حساس لمدرسة أخرى",
        )
        self._login(self.owner)
        payload = self._with_evidence_rows(
            self._report_payload(), [{"evidence": foreign, "delete": True}]
        )

        response = self._post(payload)

        self._assert_ownership_error(response)
        content = response.content.decode("utf-8")
        self.assertNotIn(self.other_school.name, content)
        self.assertNotIn(self.cross_school_report.title, content)
        self.assertTrue(ReportEvidence.objects.filter(pk=foreign.pk).exists())

    def test_manager_and_platform_owner_cannot_rebind_foreign_evidence(self):
        foreign = ReportEvidence.objects.create(
            report=self.same_school_report,
            image=_image("privileged-foreign.png"),
            order=1,
            description="شاهد محمي",
        )
        for actor in (self.manager, self.platform_owner):
            with self.subTest(actor=actor.phone):
                self._login(actor)
                payload = self._with_evidence_rows(
                    self._report_payload(title="محاولة بصلاحية مرتفعة"),
                    [{"evidence": foreign, "description": "تغيير مرفوض"}],
                )
                response = self._post(payload)
                self._assert_ownership_error(response)
                self.report.refresh_from_db()
                foreign.refresh_from_db()
                self.assertEqual(self.report.title, "التقرير المستهدف")
                self.assertEqual(foreign.description, "شاهد محمي")

    def test_valid_retain_replace_and_delete_still_work(self):
        retained = ReportEvidence.objects.create(
            report=self.report, image=_image("retain.png"), order=1, description="يبقى"
        )
        replaced = ReportEvidence.objects.create(
            report=self.report, image=_image("replace.png"), order=2, description="يستبدل"
        )
        removed = ReportEvidence.objects.create(
            report=self.report, image=_image("remove.png"), order=3, description="يحذف"
        )
        self._login(self.owner)
        payload = self._with_evidence_rows(
            self._report_payload(),
            [
                {"evidence": retained},
                {
                    "evidence": replaced,
                    "image": _image("valid-replacement.png", color=(120, 80, 20)),
                },
                {"evidence": removed, "delete": True},
            ],
        )

        response = self._post(payload)

        self.assertRedirects(
            response, reverse("reports:my_reports"), fetch_redirect_response=False
        )
        self.assertTrue(ReportEvidence.objects.filter(pk=retained.pk).exists())
        replaced.refresh_from_db()
        self.assertTrue(replaced.image.name.endswith(".webp"))
        self.assertFalse(ReportEvidence.objects.filter(pk=removed.pk).exists())
        self.assertEqual(
            list(self.report.evidences.values_list("order", flat=True)), [1, 2]
        )
