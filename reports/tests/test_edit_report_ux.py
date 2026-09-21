from __future__ import annotations

from datetime import date
from io import BytesIO
from pathlib import Path
import re
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from reports.models import (
    ApprovalState,
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


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _image(name: str, color=(18, 108, 60)) -> SimpleUploadedFile:
    output = BytesIO()
    Image.new("RGB", (320, 180), color).save(output, format="PNG")
    return SimpleUploadedFile(name, output.getvalue(), content_type="image/png")


@override_settings(ALLOWED_HOSTS=["testserver"])
class ReportEditPilotTests(TestCase):
    def setUp(self):
        self.media = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.media.cleanup)
        self.media_override = override_settings(MEDIA_ROOT=self.media.name)
        self.media_override.enable()
        self.addCleanup(self.media_override.disable)

        self.school = School.objects.create(
            name="مدرسة تحرير التقارير",
            code="edit-report-school",
            report_approval_enabled=True,
        )
        self.other_school = School.objects.create(
            name="مدرسة أخرى",
            code="edit-report-other",
            report_approval_enabled=True,
        )
        plan = SubscriptionPlan.objects.create(
            name="خطة اختبار التحرير",
            price=0,
            days_duration=30,
            max_teachers=20,
        )
        for school in (self.school, self.other_school):
            SchoolSubscription.objects.create(school=school, plan=plan)
            SchoolArchiveAddon.objects.create(school=school, is_enabled=True, storage_limit_gb=10)

        self.teacher = Teacher.objects.create_user(
            phone="500771001", name="معد التقرير", password="test-pass"
        )
        self.other_teacher = Teacher.objects.create_user(
            phone="500771002", name="مستخدم آخر", password="test-pass"
        )
        self.manager = Teacher.objects.create_user(
            phone="500771003", name="مدير المدرسة", password="test-pass", is_staff=True
        )
        self.other_manager = Teacher.objects.create_user(
            phone="500771004", name="مدير المدرسة الأخرى", password="test-pass", is_staff=True
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.teacher,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.other_teacher,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )
        SchoolMembership.objects.create(
            school=self.other_school,
            teacher=self.other_manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )
        self.category = ReportType.objects.create(
            school=self.school, code="edit-type", name="نوع قابل للتحرير"
        )
        self.other_category = ReportType.objects.create(
            school=self.other_school, code="foreign-type", name="نوع خارجي"
        )
        self.report = self._report()

    def _report(self, *, owner=None, school=None, category=None, state=ApprovalState.DRAFT, title="تقرير قابل للتعديل"):
        target_school = school or self.school
        return Report.objects.create(
            school=target_school,
            teacher=owner or self.teacher,
            teacher_name=(owner or self.teacher).name,
            title=title,
            report_date=date(2026, 9, 20),
            category=category or self.category,
            approval_state=state,
            show_details=True,
            idea="تفاصيل التقرير الأصلية.",
        )

    def _login(self, user=None, school=None):
        self.client.force_login(user or self.teacher)
        session = self.client.session
        session["active_school_id"] = (school or self.school).pk
        session.save()

    def _payload(self, report=None, **overrides):
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

    def test_edit_surface_uses_shared_authoring_assets_without_embedded_code(self):
        source = (PROJECT_ROOT / "reports" / "templates" / "reports" / "edit_report.html").read_text(encoding="utf-8")
        self.assertIn('class="twq-page report-authoring-page report-authoring--edit"', source)
        self.assertIn('data-report-authoring-mode="edit"', source)
        self.assertIn("css/report-authoring.css", source)
        self.assertIn("js/report-authoring.js", source)
        self.assertNotIn("client_submission_id", source)
        self.assertNotIn("<style", source.lower())
        self.assertNotRegex(source, r"<script(?![^>]*\bsrc=)[^>]*>")
        self.assertNotRegex(source, r"\sstyle\s*=")
        self.assertNotIn(".style.", source)
        self.assertEqual(len(re.findall(r"<h1\b", source, flags=re.I)), 1)

    def test_shared_authoring_assets_keep_create_and_edit_modes_separate(self):
        create = (PROJECT_ROOT / "reports" / "templates" / "reports" / "add_report.html").read_text(encoding="utf-8")
        script = (PROJECT_ROOT / "static" / "js" / "report-authoring.js").read_text(encoding="utf-8")
        stylesheet = (PROJECT_ROOT / "static" / "css" / "report-authoring.css").read_text(encoding="utf-8")
        self.assertIn('data-report-authoring-mode="create"', create)
        self.assertIn('mode === "edit"', script)
        self.assertIn('mode !== "create"', script)
        self.assertIn(".report-authoring-page", stylesheet)
        self.assertNotRegex(stylesheet, r"#[0-9a-fA-F]{3,8}\b|rgba?\(")

    def test_edit_page_exposes_real_state_context_and_existing_evidence(self):
        evidence = ReportEvidence.objects.create(
            report=self.report,
            image=_image("existing.png"),
            order=1,
            description="شاهد قائم",
        )
        self._login()

        response = self.client.get(reverse("reports:edit_my_report", args=[self.report.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "مساحة تحرير تقرير قائم")
        self.assertContains(response, self.report.get_approval_state_display())
        self.assertContains(response, self.report.teacher_display_name)
        self.assertContains(response, evidence.description)
        self.assertContains(response, 'data-report-authoring-mode="edit"')
        self.assertContains(response, 'data-pwa-draft-key="report-edit-')

    def test_owner_edits_draft_without_changing_identity_school_or_state(self):
        self._login()
        response = self.client.post(
            reverse("reports:edit_my_report", args=[self.report.pk]),
            self._payload(
                title="عنوان التقرير بعد التحرير",
                idea="تفاصيل التقرير بعد التحرير.",
                teacher=self.other_teacher.pk,
                school=self.other_school.pk,
                approval_state=ApprovalState.APPROVED,
                decided_by=self.manager.pk,
            ),
        )

        self.assertRedirects(response, reverse("reports:my_reports"), fetch_redirect_response=False)
        self.report.refresh_from_db()
        self.assertEqual(self.report.title, "عنوان التقرير بعد التحرير")
        self.assertEqual(self.report.teacher, self.teacher)
        self.assertEqual(self.report.school, self.school)
        self.assertEqual(self.report.approval_state, ApprovalState.DRAFT)
        self.assertIsNone(self.report.decided_by_id)

    def test_returned_report_is_editable_and_keeps_returned_state(self):
        returned = self._report(state=ApprovalState.RETURNED, title="تقرير معاد")
        returned.review_note = "استكمل الشاهد المصور."
        returned.save(update_fields=["review_note"])
        self._login()

        page = self.client.get(reverse("reports:edit_my_report", args=[returned.pk]))
        self.assertContains(page, "التقرير متاح للاستكمال")
        self.assertContains(page, returned.review_note)

        response = self.client.post(
            reverse("reports:edit_my_report", args=[returned.pk]),
            self._payload(returned, title="تقرير معاد ومحدّث"),
        )
        self.assertRedirects(response, reverse("reports:my_reports"), fetch_redirect_response=False)
        returned.refresh_from_db()
        self.assertEqual(returned.title, "تقرير معاد ومحدّث")
        self.assertEqual(returned.approval_state, ApprovalState.RETURNED)

    def test_owner_cannot_open_or_mutate_locked_states(self):
        self._login()
        for state in (
            ApprovalState.SUBMITTED,
            ApprovalState.UNDER_REVIEW,
            ApprovalState.RECOMMENDED,
            ApprovalState.APPROVED,
        ):
            locked = self._report(state=state, title=f"مقفل {state}")
            url = reverse("reports:edit_my_report", args=[locked.pk])
            with self.subTest(state=state, method="GET"):
                response = self.client.get(url)
                self.assertRedirects(response, reverse("reports:my_reports"), fetch_redirect_response=False)
            with self.subTest(state=state, method="POST"):
                response = self.client.post(url, self._payload(locked, title="تعديل مرفوض"))
                self.assertRedirects(response, reverse("reports:my_reports"), fetch_redirect_response=False)
                locked.refresh_from_db()
                self.assertEqual(locked.title, f"مقفل {state}")

    def test_manager_can_edit_approved_report_without_changing_approval_state(self):
        approved = self._report(state=ApprovalState.APPROVED, title="تقرير معتمد")
        self._login(self.manager)
        response = self.client.post(
            reverse("reports:edit_my_report", args=[approved.pk]),
            self._payload(approved, title="تقرير معتمد محدّث"),
        )
        self.assertRedirects(response, reverse("reports:admin_reports"), fetch_redirect_response=False)
        approved.refresh_from_db()
        self.assertEqual(approved.title, "تقرير معتمد محدّث")
        self.assertEqual(approved.approval_state, ApprovalState.APPROVED)

    def test_existing_evidence_can_be_retained_replaced_and_removed_on_save(self):
        retained = ReportEvidence.objects.create(
            report=self.report, image=_image("retain.png"), order=1, description="يبقى"
        )
        replaced = ReportEvidence.objects.create(
            report=self.report, image=_image("replace.png"), order=2, description="يستبدل"
        )
        removed = ReportEvidence.objects.create(
            report=self.report, image=_image("remove.png"), order=3, description="يحذف"
        )
        self._login()
        payload = self._payload()
        payload.update(
            {
                "evidence-TOTAL_FORMS": "3",
                "evidence-INITIAL_FORMS": "3",
                "evidence-MIN_NUM_FORMS": "0",
                "evidence-MAX_NUM_FORMS": "8",
            }
        )
        for index, evidence in enumerate((retained, replaced, removed)):
            payload.update(
                {
                    f"evidence-{index}-id": str(evidence.pk),
                    f"evidence-{index}-order": str(index + 1),
                    f"evidence-{index}-description": evidence.description,
                    f"evidence-{index}-display_size": "auto",
                    f"evidence-{index}-fit_mode": "contain",
                    f"evidence-{index}-show_in_print": "on",
                }
            )
        payload["evidence-1-image"] = _image("replacement.png", color=(120, 80, 20))
        payload["evidence-2-DELETE"] = "on"

        response = self.client.post(reverse("reports:edit_my_report", args=[self.report.pk]), payload)

        self.assertRedirects(response, reverse("reports:my_reports"), fetch_redirect_response=False)
        self.assertTrue(ReportEvidence.objects.filter(pk=retained.pk).exists())
        replaced.refresh_from_db()
        self.assertTrue(replaced.image.name.endswith(".webp"))
        self.assertFalse(ReportEvidence.objects.filter(pk=removed.pk).exists())
        self.assertEqual(list(self.report.evidences.values_list("order", flat=True)), [1, 2])

    def test_foreign_evidence_id_is_rejected_without_mutation(self):
        foreign_report = self._report(owner=self.other_teacher, title="تقرير شاهد خارجي")
        foreign = ReportEvidence.objects.create(
            report=foreign_report, image=_image("foreign.png"), order=1, description="شاهد خارجي"
        )
        self._login()
        payload = self._payload(title="عنوان يجب ألا يحفظ")
        payload.update(
            {
                "evidence-TOTAL_FORMS": "1",
                "evidence-INITIAL_FORMS": "1",
                "evidence-MIN_NUM_FORMS": "0",
                "evidence-MAX_NUM_FORMS": "8",
                "evidence-0-id": str(foreign.pk),
                "evidence-0-order": "1",
                "evidence-0-description": "محاولة استيلاء",
                "evidence-0-display_size": "auto",
                "evidence-0-fit_mode": "contain",
                "evidence-0-show_in_print": "on",
            }
        )

        response = self.client.post(reverse("reports:edit_my_report", args=[self.report.pk]), payload)

        self.assertEqual(response.status_code, 200)
        self.report.refresh_from_db()
        foreign.refresh_from_db()
        self.assertEqual(self.report.title, "تقرير قابل للتعديل")
        self.assertEqual(foreign.description, "شاهد خارجي")

    def test_foreign_evidence_delete_flag_is_rejected_without_mutation(self):
        foreign_report = self._report(owner=self.other_teacher, title="تقرير شاهد خارجي للحذف")
        foreign = ReportEvidence.objects.create(
            report=foreign_report, image=_image("foreign-delete.png"), order=1, description="محمي"
        )
        self._login()
        payload = self._payload()
        payload.update(
            {
                "evidence-TOTAL_FORMS": "1",
                "evidence-INITIAL_FORMS": "1",
                "evidence-MIN_NUM_FORMS": "0",
                "evidence-MAX_NUM_FORMS": "8",
                "evidence-0-id": str(foreign.pk),
                "evidence-0-order": "1",
                "evidence-0-description": "محاولة حذف",
                "evidence-0-display_size": "auto",
                "evidence-0-fit_mode": "contain",
                "evidence-0-show_in_print": "on",
                "evidence-0-DELETE": "on",
            }
        )

        response = self.client.post(
            reverse("reports:edit_my_report", args=[self.report.pk]),
            payload,
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 422)
        self.assertContains(
            response,
            "أحد الشواهد المرسلة غير صالح لهذا التقرير.",
            status_code=422,
        )
        self.report.refresh_from_db()
        foreign.refresh_from_db()
        self.assertEqual(self.report.title, "تقرير قابل للتعديل")
        self.assertEqual(foreign.description, "محمي")
        self.assertEqual(foreign.report, foreign_report)

    def test_foreign_category_and_cross_school_report_are_rejected(self):
        self._login()
        response = self.client.post(
            reverse("reports:edit_my_report", args=[self.report.pk]),
            self._payload(category=self.other_category.code, title="محاولة نوع خارجي"),
        )
        self.assertEqual(response.status_code, 200)
        self.report.refresh_from_db()
        self.assertEqual(self.report.title, "تقرير قابل للتعديل")
        self.assertEqual(self.report.category, self.category)

        cross_school = self._report(
            owner=self.other_manager,
            school=self.other_school,
            category=self.other_category,
            title="تقرير المدرسة الأخرى",
        )
        response = self.client.get(reverse("reports:edit_my_report", args=[cross_school.pk]))
        self.assertEqual(response.status_code, 404)

    def test_other_teacher_cannot_open_or_post_to_report(self):
        self._login(self.other_teacher)
        url = reverse("reports:edit_my_report", args=[self.report.pk])
        self.assertEqual(self.client.get(url).status_code, 404)
        response = self.client.post(url, self._payload(title="تعديل غير مصرح"))
        self.assertEqual(response.status_code, 404)
        self.report.refresh_from_db()
        self.assertEqual(self.report.title, "تقرير قابل للتعديل")

    def test_internal_next_is_preserved_and_external_next_is_dropped(self):
        self._login()
        url = reverse("reports:edit_my_report", args=[self.report.pk])
        internal = reverse("reports:my_reports") + "?status=draft"
        page = self.client.get(url, {"next": internal})
        self.assertEqual(page.context["next_url"], internal)
        self.assertContains(page, f'name="next" value="{internal.replace("&", "&amp;")}"', html=False)

        response = self.client.post(url, self._payload(next=internal))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, internal)

        external = "https://attacker.example/leave"
        page = self.client.get(url, {"next": external})
        self.assertIsNone(page.context["next_url"])
        self.assertNotContains(page, external)
        response = self.client.post(url, self._payload(next=external))
        self.assertRedirects(response, reverse("reports:my_reports"), fetch_redirect_response=False)
