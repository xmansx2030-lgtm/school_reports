"""Official annual navigation must never move teacher work or reset quotas."""

from datetime import date, timedelta
from io import BytesIO
import json
import secrets
from zipfile import ZipFile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from reports.academic_years import MINISTRY_CALENDAR_URL, current_academic_year_value
from reports.models import AcademicYear, Teacher

from .forms import PersonalEvidenceForm, PersonalInitiativeForm, PersonalReportForm, PersonalSchoolParityReportForm
from .models import (
    PersonalAcademicYear, PersonalCourse, PersonalEvidence, PersonalInitiative,
    PersonalPortfolioEvidence, PersonalPortfolioReport, PersonalPortfolioSection,
    PersonalReport, PersonalShareLink, PersonalWorkspace,
)
from .quotas import personal_quota_usage
from .services import ensure_personal_subscription, personal_year_values, selected_personal_year


@override_settings(ALLOWED_HOSTS=["testserver"], RATELIMIT_ENABLE=False)
class PersonalAcademicYearJourneyTests(TestCase):
    PREVIOUS = "1447-1448"
    CURRENT = "1448-1449"
    HISTORICAL = "1445-1446"

    def setUp(self):
        for value, start, end in (
            (self.PREVIOUS, date(2025, 8, 24), date(2026, 6, 25)),
            (self.CURRENT, date(2026, 8, 23), date(2027, 6, 24)),
        ):
            AcademicYear.objects.update_or_create(value=value, defaults={
                "is_active": True, "start_date": start, "end_date": end,
                "source_url": MINISTRY_CALENDAR_URL,
            })
        self.owner = Teacher.objects.create_user(phone="0557988111", name="معلم مستقل", email="annual@example.test")
        self.workspace = PersonalWorkspace.objects.create(owner=self.owner, current_academic_year=self.CURRENT)
        self.subscription = ensure_personal_subscription(self.workspace)
        self.client.force_login(self.owner)
        self.reports = {}
        self.evidence = {}
        for year in (self.PREVIOUS, self.CURRENT):
            PersonalAcademicYear.objects.create(workspace=self.workspace, value=year)
            self.reports[year] = PersonalReport.objects.create(
                workspace=self.workspace, academic_year=year, title=f"تقرير {year}",
                report_date=date(2026, 9, 20), description="توثيق مهني",
            )
            self.evidence[year] = PersonalEvidence.objects.create(
                workspace=self.workspace, academic_year=year, title=f"شاهد {year}",
                source_url="https://example.test/proof", report=self.reports[year],
            )
            PersonalInitiative.objects.create(workspace=self.workspace, academic_year=year, title=f"مبادرة {year}")
            PersonalCourse.objects.create(
                workspace=self.workspace, academic_year=year, title=f"دورة {year}",
                provider="جهة تدريب", start_date=date(2026, 9, 20), hours="4.00",
                certificate=self.evidence[year],
            )

    def report_payload(self, year, **changes):
        payload = {
            "academic_year": year, "title": "تقرير سنوي", "category": "نشاط",
            "report_date": "2026-09-20", "description": "عمل موثق", "status": "draft",
        }
        payload.update(changes)
        return payload

    def test_official_year_is_default_despite_obsolete_workspace_setting(self):
        self.workspace.current_academic_year = self.PREVIOUS
        self.workspace.save(update_fields=["current_academic_year"])
        page = self.client.get(reverse("personal:reports"))
        self.assertEqual(page.context["year"], current_academic_year_value())
        self.assertEqual(page.context["year"], self.CURRENT)
        self.assertEqual({row.academic_year for row in page.context["reports"]}, {self.CURRENT})

    def test_navigation_preserves_selection_on_all_annual_workflows(self):
        self.client.get(reverse("personal:reports"), {"year": self.PREVIOUS})
        for route, key in (("reports", "reports"), ("evidence", "evidence"), ("initiatives", "initiatives"), ("courses", "page_obj")):
            with self.subTest(route=route):
                page = self.client.get(reverse(f"personal:{route}"))
                self.assertEqual(page.status_code, 200)
                self.assertEqual(page.context["personal_selected_year"], self.PREVIOUS)
                self.assertEqual({row.academic_year for row in page.context[key]}, {self.PREVIOUS})
        portfolio = self.client.get(reverse("personal:portfolio"))
        self.assertEqual(portfolio.context["year"], self.PREVIOUS)
        self.assertEqual({row.academic_year for row in portfolio.context["reports"]}, {self.PREVIOUS})

    def test_dashboard_counts_selected_year_but_quota_stays_lifetime(self):
        before = personal_quota_usage(self.workspace)[1:]
        snapshot = list(self.workspace.reports.values_list("pk", "academic_year"))
        page = self.client.get(reverse("personal:dashboard"), {"year": self.PREVIOUS})
        self.assertEqual(page.context["stats"]["reports"], 1)
        self.assertEqual(page.context["stats"]["evidence"], 1)
        self.assertEqual(page.context["stats"]["initiatives"], 1)
        self.assertEqual(page.context["stats"]["courses"], 1)
        self.assertEqual(page.context["getting_started_total"], 6)
        self.assertEqual(page.context["quota"]["reports"]["used"], before[0])
        self.assertEqual(list(self.workspace.reports.values_list("pk", "academic_year")), snapshot)
        self.workspace.refresh_from_db()
        self.assertEqual(self.workspace.current_academic_year, self.CURRENT)
        self.assertEqual(personal_quota_usage(self.workspace)[1:], before)

    def test_switch_is_available_read_only_and_redirects_to_corresponding_list(self):
        self.subscription.is_active = False
        self.subscription.save(update_fields=["is_active"])
        result = self.client.post(reverse("personal:years"), {
            "action": "select", "year": self.PREVIOUS,
            "next": reverse("personal:report_detail", args=[self.reports[self.CURRENT].pk]),
        })
        self.assertRedirects(result, reverse("personal:reports"), fetch_redirect_response=False)
        self.assertEqual(self.client.session[f"personal_academic_year:{self.workspace.pk}"], self.PREVIOUS)
        self.workspace.refresh_from_db()
        self.assertEqual(self.workspace.current_academic_year, self.CURRENT)

    def test_manual_year_management_and_open_redirect_are_rejected(self):
        for action in ("add", "archive", "reopen"):
            result = self.client.post(reverse("personal:years"), {"action": action, "value": "1460-1461"})
            self.assertEqual(result.status_code, 400)
        self.assertFalse(self.workspace.academic_years.filter(value="1460-1461").exists())
        result = self.client.post(reverse("personal:years"), {
            "action": "select", "year": self.PREVIOUS, "next": "https://example.test/unsafe",
        })
        self.assertRedirects(result, reverse("personal:dashboard"), fetch_redirect_response=False)

    def test_unknown_or_another_teachers_historical_year_is_unavailable(self):
        other = Teacher.objects.create_user(phone="0557988112", name="معلم آخر")
        foreign = PersonalWorkspace.objects.create(owner=other)
        PersonalAcademicYear.objects.create(workspace=foreign, value=self.HISTORICAL)
        self.assertNotIn(self.HISTORICAL, personal_year_values(self.workspace))
        self.assertEqual(self.client.get(reverse("personal:reports"), {"year": self.HISTORICAL}).status_code, 404)
        self.assertEqual(self.client.get(reverse("personal:reports"), {"year": "1460-1461"}).status_code, 404)

    def test_session_selection_is_scoped_to_workspace(self):
        other = Teacher.objects.create_user(phone="0557988113", name="معلم ثالث")
        foreign = PersonalWorkspace.objects.create(owner=other)
        request = RequestFactory().get("/personal/")
        request.session = {f"personal_academic_year:{self.workspace.pk}": self.PREVIOUS}
        request.personal_workspace = foreign
        self.assertEqual(selected_personal_year(request), self.CURRENT)

    def test_create_form_uses_selected_official_dropdown(self):
        page = self.client.get(reverse("personal:report_create"), {"year": self.PREVIOUS})
        self.assertEqual(page.context["form"].initial["academic_year"], self.PREVIOUS)
        field = page.context["form"].fields["academic_year"]
        self.assertEqual(field.widget.input_type, "select")
        self.assertIn(self.PREVIOUS, dict(field.choices))
        form = PersonalReportForm(self.report_payload("1460-1461"), workspace=self.workspace)
        self.assertFalse(form.is_valid())
        self.assertIn("academic_year", form.errors)

    def test_successful_creation_returns_to_the_year_chosen_in_the_form(self):
        snapshots = {
            relation: list(getattr(self.workspace, relation).values_list("pk", "academic_year"))
            for relation in ("reports", "evidence", "initiatives", "courses")
        }
        workflows = (
            ("report_create", "reports", "reports", self.report_payload(self.CURRENT, title="تقرير السنة المختارة")),
            ("evidence_create", "evidence", "evidence", {
                "academic_year": self.CURRENT, "title": "شاهد السنة المختارة",
                "source_url": "https://example.test/annual-proof",
            }),
            ("initiatives", "initiatives", "initiatives", {
                "academic_year": self.CURRENT, "title": "مبادرة السنة المختارة",
                "summary": "تنفيذ مبادرة تربوية", "status": "draft",
            }),
            ("course_create", "courses", "page_obj", {
                "academic_year": self.CURRENT, "title": "دورة السنة المختارة",
                "provider": "جهة تدريب", "start_date": "2026-09-20", "hours": "4.00",
            }),
        )
        for create_route, list_route, context_key, payload in workflows:
            with self.subTest(route=create_route):
                self.client.get(reverse(f"personal:{list_route}"), {"year": self.PREVIOUS})
                created = self.client.post(reverse(f"personal:{create_route}"), payload, follow=True)
                self.assertEqual(created.status_code, 200)
                self.assertEqual(self.client.session[f"personal_academic_year:{self.workspace.pk}"], self.CURRENT)
                page = self.client.get(reverse(f"personal:{list_route}"))
                self.assertEqual(page.context["personal_selected_year"], self.CURRENT)
                self.assertIn(payload["title"], [record.title for record in page.context[context_key]])
        for relation, snapshot in snapshots.items():
            self.assertEqual(list(getattr(self.workspace, relation).filter(
                pk__in=[pk for pk, _year in snapshot],
            ).values_list("pk", "academic_year")), snapshot)
        self.subscription.refresh_from_db()
        self.assertEqual((self.subscription.reports_created, self.subscription.evidence_created), (3, 3))

    def test_editing_old_year_content_preserves_the_browsing_selection(self):
        self.client.get(reverse("personal:reports"), {"year": self.CURRENT})
        edits = (
            ("report_edit", self.reports[self.PREVIOUS], self.report_payload(self.PREVIOUS, title="تقرير محدث")),
            ("evidence_edit", self.evidence[self.PREVIOUS], {
                "academic_year": self.PREVIOUS, "title": "شاهد محدث", "source_url": "https://example.test/updated",
            }),
            ("initiative_edit", self.workspace.initiatives.get(academic_year=self.PREVIOUS), {
                "academic_year": self.PREVIOUS, "title": "مبادرة محدثة", "summary": "توثيق محدث", "status": "draft",
            }),
        )
        for route, record, payload in edits:
            with self.subTest(route=route):
                edited = self.client.post(reverse(f"personal:{route}", args=[record.pk]), payload, follow=True)
                self.assertEqual(edited.status_code, 200)
                self.assertEqual(self.client.session[f"personal_academic_year:{self.workspace.pk}"], self.CURRENT)
                record.refresh_from_db()
                self.assertEqual(record.title, payload["title"])
                self.assertEqual(record.academic_year, self.PREVIOUS)

    def test_evidence_target_sets_its_original_year_without_changing_browse_selection_before_save(self):
        targets = (
            ("report", self.reports[self.PREVIOUS]),
            ("initiative", self.workspace.initiatives.get(academic_year=self.PREVIOUS)),
        )
        for field, target in targets:
            with self.subTest(field=field):
                self.client.get(reverse("personal:evidence"), {"year": self.CURRENT})
                url = f"{reverse('personal:evidence_create')}?{field}={target.pk}"
                page = self.client.get(url)
                self.assertEqual(page.context["personal_selected_year"], self.PREVIOUS)
                self.assertEqual(page.context["form"].initial[field], target)
                self.assertEqual(page.context["form"].initial["academic_year"], self.PREVIOUS)
                self.assertEqual(self.client.session[f"personal_academic_year:{self.workspace.pk}"], self.CURRENT)
                title = f"شاهد مرتبط {field}"
                saved = self.client.post(url, {
                    "title": title, "academic_year": self.PREVIOUS, field: target.pk,
                    "source_url": "https://example.test/target-proof",
                }, follow=True)
                self.assertEqual(saved.status_code, 200)
                witness = self.workspace.evidence.get(title=title)
                self.assertEqual(witness.academic_year, self.PREVIOUS)
                self.assertEqual(getattr(witness, f"{field}_id"), target.pk)
                self.assertEqual(saved.context["personal_selected_year"], self.PREVIOUS)
                self.assertEqual(self.client.session[f"personal_academic_year:{self.workspace.pk}"], self.PREVIOUS)

    def test_saved_report_evidence_and_initiative_years_are_immutable(self):
        report = PersonalReportForm(self.report_payload(self.CURRENT), instance=self.reports[self.PREVIOUS], workspace=self.workspace)
        self.assertFalse(report.is_valid())
        self.assertIn("academic_year", report.errors)
        evidence = PersonalEvidenceForm({
            "academic_year": self.CURRENT, "title": "شاهد", "source_url": "https://example.test/proof",
        }, instance=self.evidence[self.PREVIOUS], workspace=self.workspace)
        self.assertFalse(evidence.is_valid())
        self.assertIn("academic_year", evidence.errors)
        initiative = self.workspace.initiatives.get(academic_year=self.PREVIOUS)
        form = PersonalInitiativeForm({"academic_year": self.CURRENT, "title": "مبادرة", "status": initiative.status}, instance=initiative, workspace=self.workspace)
        self.assertFalse(form.is_valid())
        self.assertIn("academic_year", form.errors)

    def test_school_editor_rejects_year_moving_post(self):
        report = self.reports[self.PREVIOUS]
        form = PersonalSchoolParityReportForm({
            "academic_year": self.CURRENT, "title": report.title, "category": "نشاط",
            "report_date": "2026-09-20", "show_details": "on", "idea": "تفاصيل",
            "section_selection_enabled": "on",
        }, instance=report, workspace=self.workspace)
        self.assertFalse(form.is_valid())
        self.assertIn("academic_year", form.errors)

    def test_evidence_link_dropdowns_exclude_other_years(self):
        form = PersonalEvidenceForm(workspace=self.workspace, initial={"academic_year": self.PREVIOUS})
        self.assertEqual(list(form.fields["report"].queryset), [self.reports[self.PREVIOUS]])
        self.assertEqual({row.academic_year for row in form.fields["initiative"].queryset}, {self.PREVIOUS})
        invalid = PersonalEvidenceForm({
            "academic_year": self.PREVIOUS, "title": "شاهد", "source_url": "https://example.test/proof",
            "report": self.reports[self.CURRENT].pk,
        }, workspace=self.workspace)
        self.assertFalse(invalid.is_valid())
        self.assertIn("report", invalid.errors)

    def test_unpublished_historical_content_remains_readable_and_uneditable(self):
        historical = PersonalReport.objects.create(
            workspace=self.workspace, academic_year=self.HISTORICAL, title="عمل تاريخي محفوظ",
            report_date=date(2024, 9, 20), description="بيانات قديمة",
        )
        page = self.client.get(reverse("personal:reports"), {"year": self.HISTORICAL})
        self.assertContains(page, historical.title)
        self.assertFalse(page.context["personal_year_can_write"])
        self.assertEqual(self.client.post(reverse("personal:report_delete", args=[historical.pk])).status_code, 302)
        historical.refresh_from_db()
        self.assertIsNone(historical.trashed_at)
        edit = self.client.get(reverse("personal:report_edit", args=[historical.pk]))
        self.assertEqual(edit.status_code, 302)

    def test_portfolio_hides_inconsistent_historical_links_and_rejects_cross_year_linking(self):
        section = PersonalPortfolioSection.objects.create(workspace=self.workspace, academic_year=self.PREVIOUS, code=1)
        linked = PersonalPortfolioReport.objects.create(section=section, report=self.reports[self.PREVIOUS])
        PersonalPortfolioReport.objects.filter(pk=linked.pk).update(report=self.reports[self.CURRENT])
        page = self.client.get(reverse("personal:portfolio"), {"year": self.PREVIOUS})
        self.assertEqual(page.context["sections"][0]["reports"], [])
        result = self.client.post(reverse("personal:portfolio"), {
            "year": self.PREVIOUS, "action": "link_evidence", "section_code": "1",
            "evidence_id": self.evidence[self.CURRENT].pk,
        })
        self.assertEqual(result.status_code, 404)

    def test_annual_progress_counts_only_documented_sections_with_valid_year_links(self):
        empty = PersonalPortfolioSection.objects.create(
            workspace=self.workspace, academic_year=self.PREVIOUS, code=1, teacher_notes="  \n\t  ",
        )
        report_section = PersonalPortfolioSection.objects.create(workspace=self.workspace, academic_year=self.PREVIOUS, code=2)
        evidence_section = PersonalPortfolioSection.objects.create(workspace=self.workspace, academic_year=self.PREVIOUS, code=3)
        PersonalPortfolioSection.objects.create(
            workspace=self.workspace, academic_year=self.PREVIOUS, code=4, teacher_notes="ملاحظة مهنية موثقة",
        )
        trash_section = PersonalPortfolioSection.objects.create(workspace=self.workspace, academic_year=self.PREVIOUS, code=5)
        malformed_section = PersonalPortfolioSection.objects.create(workspace=self.workspace, academic_year=self.PREVIOUS, code=6)
        trashed_report = PersonalReport.objects.create(
            workspace=self.workspace, academic_year=self.PREVIOUS, title="عمل محفوظ في السلة", description="تفاصيل محفوظة",
            report_date=date(2026, 9, 20),
        )
        trash_link = PersonalPortfolioReport.objects.create(section=trash_section, report=trashed_report)
        PersonalReport.objects.filter(pk=trashed_report.pk).update(trashed_at=timezone.now())
        malformed_evidence = PersonalEvidence.objects.create(
            workspace=self.workspace, academic_year=self.PREVIOUS, title="شاهد تاريخي بارتباط غير متسق",
            source_url="https://example.test/legacy-proof", report=self.reports[self.PREVIOUS],
        )
        malformed_link = PersonalPortfolioEvidence.objects.create(section=malformed_section, evidence=malformed_evidence)
        PersonalEvidence.objects.filter(pk=malformed_evidence.pk).update(report=self.reports[self.CURRENT])
        report_link = PersonalPortfolioReport.objects.create(section=report_section, report=self.reports[self.PREVIOUS])
        evidence_link = PersonalPortfolioEvidence.objects.create(section=evidence_section, evidence=self.evidence[self.PREVIOUS])
        # Preserve legacy corruption for the read-side isolation regression.
        PersonalPortfolioReport.objects.filter(pk=report_link.pk).update(report=self.reports[self.CURRENT])
        PersonalPortfolioEvidence.objects.filter(pk=evidence_link.pk).update(evidence=self.evidence[self.CURRENT])
        portfolio = self.client.get(reverse("personal:portfolio"), {"year": self.PREVIOUS})
        self.assertEqual(portfolio.context["documented_count"], 1)
        archive = self.client.get(reverse("personal:years"))
        row = next(row for row in archive.context["years"] if row.value == self.PREVIOUS)
        self.assertEqual(row.portfolio_count, 1)
        dashboard = self.client.get(reverse("personal:dashboard"))
        self.assertEqual(dashboard.context["stats"]["portfolio_documented"], 1)
        empty.refresh_from_db()
        self.assertEqual(empty.teacher_notes, "  \n\t  ")
        self.assertEqual(self.workspace.portfolio_sections.count(), 6)
        self.assertTrue(PersonalPortfolioReport.objects.filter(pk=trash_link.pk).exists())
        self.assertTrue(PersonalPortfolioEvidence.objects.filter(pk=malformed_link.pk).exists())
        self.assertTrue(PersonalReport.objects.filter(pk=trashed_report.pk, trashed_at__isnull=False).exists())
        self.assertTrue(PersonalEvidence.objects.filter(pk=malformed_evidence.pk, report=self.reports[self.CURRENT]).exists())

    def test_export_keeps_reports_courses_and_evidence_in_one_year(self):
        result = self.client.get(reverse("personal:year_export", args=[self.PREVIOUS]))
        self.assertEqual(result.status_code, 200)
        with ZipFile(BytesIO(b"".join(result.streaming_content))) as archive:
            manifest = json.loads(archive.read("manifest.json"))
        self.assertEqual(manifest["academic_year"], self.PREVIOUS)
        self.assertEqual([row["id"] for row in manifest["reports"]], [self.reports[self.PREVIOUS].pk])
        self.assertEqual([row["id"] for row in manifest["evidence"]], [self.evidence[self.PREVIOUS].pk])
        self.assertEqual(len(manifest["courses"]), 1)

    def test_annual_manifest_omits_cross_year_references_and_preserves_owned_files_and_raw_rows(self):
        owned_evidence = self.evidence[self.PREVIOUS]
        file_content = b"%PDF-1.4\nannual owned evidence\n%%EOF"
        owned_evidence.file = SimpleUploadedFile("annual-proof.pdf", file_content, content_type="application/pdf")
        owned_evidence.file_size = len(file_content)
        owned_evidence.save(update_fields=["file", "file_size"])
        current_initiative = self.workspace.initiatives.get(academic_year=self.CURRENT)
        section = PersonalPortfolioSection.objects.create(workspace=self.workspace, academic_year=self.PREVIOUS, code=1)
        report_link = PersonalPortfolioReport.objects.create(section=section, report=self.reports[self.PREVIOUS])
        evidence_link = PersonalPortfolioEvidence.objects.create(section=section, evidence=owned_evidence)
        course = self.workspace.courses.get(academic_year=self.PREVIOUS)
        PersonalEvidence.objects.filter(pk=owned_evidence.pk).update(
            report=self.reports[self.CURRENT], initiative=current_initiative,
        )
        PersonalCourse.objects.filter(pk=course.pk).update(certificate=self.evidence[self.CURRENT])
        PersonalPortfolioReport.objects.filter(pk=report_link.pk).update(report=self.reports[self.CURRENT])
        PersonalPortfolioEvidence.objects.filter(pk=evidence_link.pk).update(evidence=self.evidence[self.CURRENT])
        result = self.client.get(reverse("personal:year_export", args=[self.PREVIOUS]))
        self.assertEqual(result.status_code, 200)
        with ZipFile(BytesIO(b"".join(result.streaming_content))) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(archive.read(manifest["evidence"][0]["file"]), file_content)
        self.assertEqual(manifest["evidence"][0]["id"], owned_evidence.pk)
        self.assertIsNone(manifest["evidence"][0]["report_id"])
        self.assertIsNone(manifest["evidence"][0]["initiative_id"])
        self.assertIsNone(manifest["courses"][0]["certificate_evidence_id"])
        self.assertEqual(manifest["portfolio_sections"][0]["report_ids"], [])
        self.assertEqual(manifest["portfolio_sections"][0]["evidence_ids"], [])
        owned_evidence.refresh_from_db()
        course.refresh_from_db()
        report_link.refresh_from_db()
        evidence_link.refresh_from_db()
        self.assertEqual(owned_evidence.report_id, self.reports[self.CURRENT].pk)
        self.assertEqual(owned_evidence.initiative_id, current_initiative.pk)
        self.assertEqual(course.certificate_id, self.evidence[self.CURRENT].pk)
        self.assertEqual(report_link.report_id, self.reports[self.CURRENT].pk)
        self.assertEqual(evidence_link.evidence_id, self.evidence[self.CURRENT].pk)

    def test_approved_empty_year_export_does_not_create_workspace_year_rows(self):
        PersonalAcademicYear.objects.filter(workspace=self.workspace, value=self.CURRENT).delete()
        self.workspace.reports.filter(academic_year=self.CURRENT).delete()
        self.workspace.evidence.filter(academic_year=self.CURRENT).delete()
        self.workspace.initiatives.filter(academic_year=self.CURRENT).delete()
        self.workspace.courses.filter(academic_year=self.CURRENT).delete()
        result = self.client.get(reverse("personal:year_export", args=[self.CURRENT]))
        self.assertEqual(result.status_code, 200)
        with ZipFile(BytesIO(b"".join(result.streaming_content))) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(archive.namelist(), ["manifest.json"])
        self.assertEqual(manifest["academic_year"], self.CURRENT)
        for section in ("reports", "evidence", "initiatives", "courses", "portfolio_sections"):
            self.assertEqual(manifest[section], [])
        self.assertFalse(self.workspace.academic_years.filter(value=self.CURRENT).exists())

    def test_public_portfolio_cannot_publish_evidence_from_other_year(self):
        section = PersonalPortfolioSection.objects.create(workspace=self.workspace, academic_year=self.PREVIOUS, code=1)
        linked = PersonalPortfolioEvidence.objects.create(section=section, evidence=self.evidence[self.PREVIOUS])
        PersonalPortfolioEvidence.objects.filter(pk=linked.pk).update(evidence=self.evidence[self.CURRENT])
        share = PersonalShareLink.objects.create(
            workspace=self.workspace, kind="portfolio", academic_year=self.PREVIOUS,
            token=secrets.token_urlsafe(32), expires_at=timezone.now() + timedelta(days=1),
        )
        result = self.client.get(reverse("personal:share_public_portfolio", args=[share.token]))
        self.assertEqual(result.status_code, 200)
        self.assertNotContains(result, self.evidence[self.CURRENT].title)
        self.assertEqual(result.context["visible_evidence_count"], 0)
