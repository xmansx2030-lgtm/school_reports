from datetime import date, timedelta
from io import BytesIO
import json
import secrets
import uuid
from zipfile import ZipFile

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from reports.model_parts.achievements import AchievementSection
from reports.models import Teacher
from reports.services_data_rights import build_personal_data_export

from .models import (
    PersonalAcademicYear, PersonalEvidence, PersonalPortfolioReport,
    PersonalPortfolioSection, PersonalReport, PersonalShareLink, PersonalWorkspace,
)
from .services import ensure_personal_subscription


@override_settings(ALLOWED_HOSTS=["testserver"], RATELIMIT_ENABLE=False)
class PersonalReportTrashTests(TestCase):
    YEAR = "1447-1448"

    def setUp(self):
        self.owner = Teacher.objects.create_user(
            phone="0557990190", name="معلمة التقرير", password="Personal#2026",  # noqa: S106
        )
        self.workspace = PersonalWorkspace.objects.create(
            owner=self.owner, current_academic_year=self.YEAR,
        )
        self.subscription = ensure_personal_subscription(self.workspace)
        self.year_record = PersonalAcademicYear.objects.create(workspace=self.workspace, value=self.YEAR)
        self.report = PersonalReport.objects.create(
            workspace=self.workspace, title="تقرير محفوظ للاستعادة",
            report_date=date(2026, 9, 24), academic_year=self.YEAR,
            description="تفاصيل العمل", teacher_name=self.owner.name,
        )
        self.evidence = PersonalEvidence.objects.create(
            workspace=self.workspace, report=self.report, title="شاهد محفوظ",
            academic_year=self.YEAR, source_url="https://example.org/proof",
        )
        self.section = PersonalPortfolioSection.objects.create(
            workspace=self.workspace, academic_year=self.YEAR,
            code=AchievementSection.Code.choices[0][0],
        )
        self.link = PersonalPortfolioReport.objects.create(section=self.section, report=self.report)
        self.client.force_login(self.owner)

    def _move_to_trash(self):
        response = self.client.post(reverse("personal:report_delete", args=[self.report.pk]))
        self.assertRedirects(response, reverse("personal:reports"))
        self.report.refresh_from_db()
        self.assertIsNotNone(self.report.trashed_at)

    def test_trash_hides_active_surfaces_and_restores_evidence_and_portfolio_link(self):
        self.assertIsNone(self.report.trashed_at)
        self.assertContains(self.client.get(reverse("personal:reports")), self.report.title)
        self._move_to_trash()
        self.assertEqual(self.report.trashed_by_id, self.owner.pk)
        self.assertTrue(PersonalEvidence.objects.filter(pk=self.evidence.pk, report=self.report).exists())
        self.assertTrue(PersonalPortfolioReport.objects.filter(pk=self.link.pk).exists())

        active_list = self.client.get(reverse("personal:reports"))
        self.assertNotContains(active_list, self.report.title)
        self.assertContains(active_list, reverse("personal:report_trash"))
        dashboard = self.client.get(reverse("personal:dashboard"))
        self.assertEqual(dashboard.context["stats"]["reports"], 0)
        self.assertEqual(list(dashboard.context["recent_reports"]), [])
        self.assertEqual(list(dashboard.context["recent_evidence"]), [])
        for route in ("report_detail", "report_edit", "report_print"):
            self.assertEqual(self.client.get(reverse(f"personal:{route}", args=[self.report.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("personal:report_evidence_move", args=[self.report.pk]), {
            "evidence_id": self.evidence.pk, "direction": "up",
        }).status_code, 404)

        portfolio = self.client.get(reverse("personal:portfolio"), {"year": self.YEAR})
        self.assertNotContains(portfolio, self.report.title)
        self.assertEqual(portfolio.context["documented_count"], 0)
        self.assertNotContains(
            self.client.get(reverse("personal:portfolio_print"), {"year": self.YEAR}),
            self.report.title,
        )
        self.assertEqual(self.client.post(reverse("personal:portfolio"), {
            "year": self.YEAR, "section_code": self.section.code,
            "action": "link_report", "report_id": self.report.pk,
        }).status_code, 404)
        evidence_list = self.client.get(reverse("personal:evidence"))
        self.assertContains(evidence_list, "التقرير المرتبط في السلة")
        self.assertContains(evidence_list, self.evidence.title)
        evidence_edit = self.client.get(reverse("personal:evidence_edit", args=[self.evidence.pk]))
        self.assertFalse(evidence_edit.context["form"].fields["report"].queryset.filter(pk=self.report.pk).exists())
        self.assertFalse(self.client.get(reverse("personal:evidence_create")).context["form"].fields[
            "report"
        ].queryset.filter(pk=self.report.pk).exists())
        forged_evidence = self.client.post(reverse("personal:evidence_create"), {
            "title": "شاهد جديد غير مسموح", "academic_year": self.YEAR,
            "report": self.report.pk, "source_url": "https://example.org/new",
        })
        self.assertEqual(forged_evidence.status_code, 200)
        self.assertFalse(PersonalEvidence.objects.filter(title="شاهد جديد غير مسموح").exists())
        edited = self.client.post(reverse("personal:evidence_edit", args=[self.evidence.pk]), {
            "title": "شاهد محدث", "academic_year": self.YEAR,
            "source_url": self.evidence.source_url,
        })
        self.assertEqual(edited.status_code, 302)
        self.evidence.refresh_from_db()
        self.assertEqual(self.evidence.report_id, self.report.pk)

        trash = self.client.get(reverse("personal:report_trash"))
        self.assertContains(trash, self.report.title)
        self.assertContains(trash, reverse("personal:report_restore", args=[self.report.pk]))
        restored = self.client.post(reverse("personal:report_restore", args=[self.report.pk]))
        self.assertRedirects(restored, reverse("personal:report_detail", args=[self.report.pk]))
        self.report.refresh_from_db()
        self.assertIsNone(self.report.trashed_at)
        self.assertIsNone(self.report.trashed_by_id)
        self.assertContains(self.client.get(reverse("personal:reports")), self.report.title)
        self.assertContains(self.client.get(reverse("personal:portfolio"), {"year": self.YEAR}), self.report.title)
        self.assertEqual(self.evidence.report_id, self.report.pk)
        self.assertTrue(PersonalPortfolioReport.objects.filter(pk=self.link.pk).exists())

    def test_trash_stays_in_plan_count_and_year_and_privacy_exports(self):
        plan = self.subscription.plan
        plan.max_reports = 1
        plan.save(update_fields=["max_reports"])
        self._move_to_trash()
        self.assertEqual(self.workspace.reports.count(), 1)
        self.assertRedirects(self.client.get(reverse("personal:report_create")), reverse("personal:reports"))
        self.assertEqual(self.client.get(reverse("personal:billing")).context["report_count"], 1)
        exported = self.client.get(reverse("personal:year_export", args=[self.YEAR]))
        with ZipFile(BytesIO(b"".join(exported.streaming_content))) as bundle:
            manifest = json.loads(bundle.read("manifest.json"))
        exported.close()
        self.assertEqual(manifest["reports"][0]["id"], self.report.pk)
        self.assertIsNotNone(manifest["reports"][0]["trashed_at"])
        self.assertEqual(manifest["evidence"][0]["report_id"], self.report.pk)
        self.assertEqual(manifest["portfolio_sections"][0]["report_ids"], [self.report.pk])
        privacy = build_personal_data_export(self.owner)["sections"]["personal_workspace"]
        self.assertIsNotNone(privacy["reports"][0]["trashed_at"])
        self.assertEqual(privacy["reports"][0]["url"], reverse("personal:report_trash"))

    def test_retrying_trashed_report_submission_returns_to_trash(self):
        submission_id = uuid.uuid4()
        self.report.client_submission_id = submission_id
        self.report.save(update_fields=["client_submission_id"])
        self._move_to_trash()
        retried = self.client.post(reverse("personal:report_create"), {
            "client_submission_id": str(submission_id),
            "title": self.report.title, "category": "نشاط", "report_date": "2026-09-24",
            "academic_year": self.YEAR, "description": self.report.description,
            "show_details": "on", "selection_enabled": "on", "status": "complete",
        })
        self.assertRedirects(retried, reverse("personal:report_trash"))
        self.assertEqual(self.workspace.reports.count(), 1)

    def test_trash_and_restore_are_owner_scoped_and_post_only(self):
        self._move_to_trash()
        self.assertEqual(self.client.get(reverse("personal:report_restore", args=[self.report.pk])).status_code, 405)
        self.assertEqual(self.client.get(reverse("personal:report_delete", args=[self.report.pk])).status_code, 405)
        other = Teacher.objects.create_user(
            phone="0557990191", name="معلم آخر", password="Personal#2026",  # noqa: S106
        )
        PersonalWorkspace.objects.create(owner=other, current_academic_year=self.YEAR)
        self.client.force_login(other)
        self.assertNotContains(self.client.get(reverse("personal:report_trash")), self.report.title)
        self.assertEqual(self.client.post(reverse("personal:report_restore", args=[self.report.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("personal:report_delete", args=[self.report.pk])).status_code, 404)
        self.report.refresh_from_db()
        self.assertIsNotNone(self.report.trashed_at)

    def test_share_link_is_revoked_on_trash_and_stays_revoked_after_restore(self):
        link = PersonalShareLink.objects.create(
            workspace=self.workspace, report=self.report, academic_year=self.YEAR,
            kind=PersonalShareLink.Kind.REPORT, token=secrets.token_urlsafe(32),
            is_active=True, expires_at=timezone.now() + timedelta(days=7),
        )
        public_url = reverse("personal:share_public_report", args=[link.token])
        self.assertEqual(self.client.get(public_url).status_code, 200)
        self._move_to_trash()
        link.refresh_from_db()
        self.assertFalse(link.is_active)
        self.assertEqual(self.client.get(public_url).status_code, 404)
        self.assertRedirects(
            self.client.post(reverse("personal:report_restore", args=[self.report.pk])),
            reverse("personal:report_detail", args=[self.report.pk]),
        )
        link.refresh_from_db()
        self.assertFalse(link.is_active)
        self.assertEqual(self.client.get(public_url).status_code, 404)

    def test_archived_year_and_inactive_plan_keep_trash_readable_but_lock_restore(self):
        self._move_to_trash()
        self.year_record.archived_at = timezone.now()
        self.year_record.save(update_fields=["archived_at"])
        archived = self.client.get(reverse("personal:report_trash"))
        self.assertContains(archived, self.report.title)
        self.assertContains(archived, "أعد فتحها قبل الاستعادة")
        self.assertNotContains(archived, reverse("personal:report_restore", args=[self.report.pk]))
        self.assertRedirects(
            self.client.post(reverse("personal:report_restore", args=[self.report.pk])),
            reverse("personal:report_trash"),
        )
        self.assertEqual(self.client.get(reverse("personal:year_export", args=[self.YEAR])).status_code, 200)
        self.year_record.archived_at = None
        self.year_record.save(update_fields=["archived_at"])
        self.subscription.is_active = False
        self.subscription.save(update_fields=["is_active"])
        self.assertContains(self.client.get(reverse("personal:report_trash")), "يلزم اشتراك نشط للاستعادة")
        self.assertRedirects(
            self.client.post(reverse("personal:report_restore", args=[self.report.pk])),
            reverse("personal:report_trash"),
        )
        self.report.refresh_from_db()
        self.assertIsNotNone(self.report.trashed_at)
        self.subscription.is_active = True
        self.subscription.save(update_fields=["is_active"])
        self.assertRedirects(
            self.client.post(reverse("personal:report_restore", args=[self.report.pk])),
            reverse("personal:report_detail", args=[self.report.pk]),
        )
