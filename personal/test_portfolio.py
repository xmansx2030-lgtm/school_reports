"""The personal achievement file shares school axes without school authority."""

from datetime import date
from io import BytesIO
from zipfile import ZipFile

from django.test import TestCase, override_settings
from django.urls import reverse

from reports.model_parts.achievements import AchievementSection
from reports.models import Teacher
from reports.services_data_rights import build_personal_data_export

from .models import (
    PersonalAcademicYear, PersonalEvidence, PersonalInitiative, PersonalPortfolioEvidence,
    PersonalPortfolioReport, PersonalPortfolioSection, PersonalReport,
    PersonalSubscription, PersonalWorkspace,
)


@override_settings(ALLOWED_HOSTS=["testserver"], RATELIMIT_ENABLE=False)
class PersonalPortfolioTests(TestCase):
    YEAR = "1447-1448"

    def setUp(self):
        self.teacher = Teacher.objects.create_user(
            phone="0557021101", name="معلمة مستقلة", password="Personal#2026"  # noqa: S106
        )
        self.workspace = PersonalWorkspace.objects.create(
            owner=self.teacher, school_name="اسم تعريفي", current_academic_year=self.YEAR
        )
        PersonalAcademicYear.objects.create(workspace=self.workspace, value=self.YEAR)
        self.other = Teacher.objects.create_user(
            phone="0557021102", name="معلمة أخرى", password="Personal#2026"  # noqa: S106
        )
        self.other_workspace = PersonalWorkspace.objects.create(
            owner=self.other, school_name="مدرسة أخرى", current_academic_year=self.YEAR
        )
        self.client.force_login(self.teacher)

    def post_axis(self, action, code=1, **data):
        return self.client.post(reverse("personal:portfolio"), {
            "year": self.YEAR, "section_code": str(code), "action": action, **data,
        })

    def test_school_axes_render_as_personal_workspace_without_approval(self):
        response = self.client.get(reverse("personal:portfolio"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["sections"]), len(AchievementSection.Code.choices))
        self.assertContains(response, "1- أداء الواجبات الوظيفية")
        self.assertContains(response, "11- تنوع أساليب التقويم")
        self.assertContains(response, "achievement.css")
        self.assertNotContains(response, "إرسال للاعتماد")
        self.assertFalse(PersonalPortfolioSection.objects.exists())

    def test_notes_links_print_archive_and_data_export(self):
        self.assertEqual(self.post_axis(
            "save_general", qualifications="بكالوريوس تربية",
            professional_experience="خمس سنوات", specialization="رياضيات",
            teaching_load="18 حصة", subjects_taught="رياضيات", contact_info="جوال مهني",
        ).status_code, 302)
        report = PersonalReport.objects.create(
            workspace=self.workspace, title="تقرير القراءة", academic_year=self.YEAR,
            report_date=date(2026, 9, 24), description="قراءة", teacher_name=self.teacher.name,
            school_name=self.workspace.school_name,
        )
        evidence = PersonalEvidence.objects.create(
            workspace=self.workspace, title="شاهد القراءة", academic_year=self.YEAR,
            source_url="https://example.com/proof",
        )
        self.assertEqual(self.post_axis("save_notes", teacher_notes="أثر موثق في الصف").status_code, 302)
        self.assertEqual(self.post_axis("link_report", report_id=report.pk).status_code, 302)
        self.assertEqual(self.post_axis("link_evidence", evidence_id=evidence.pk).status_code, 302)
        section = PersonalPortfolioSection.objects.get(workspace=self.workspace, code=1)
        self.assertEqual(section.teacher_notes, "أثر موثق في الصف")
        self.assertEqual(PersonalAcademicYear.objects.get(
            workspace=self.workspace, value=self.YEAR
        ).qualifications, "بكالوريوس تربية")
        self.assertEqual(PersonalPortfolioReport.objects.filter(section=section).count(), 1)
        self.assertEqual(PersonalPortfolioEvidence.objects.filter(section=section).count(), 1)
        response = self.client.get(reverse("personal:portfolio"))
        self.assertEqual(response.context["documented_count"], 1)
        self.assertContains(response, "تقرير القراءة")
        self.assertContains(self.client.get(reverse("personal:portfolio_print") + "?year=" + self.YEAR), "أثر موثق في الصف")
        export = self.client.get(reverse("personal:year_export", args=[self.YEAR]))
        with ZipFile(BytesIO(b"".join(export.streaming_content))) as bundle:
            manifest = bundle.read("manifest.json").decode("utf-8")
        export.close()
        self.assertIn("أثر موثق في الصف", manifest)
        self.assertIn("بكالوريوس تربية", manifest)
        self.assertIn("report_ids", manifest)
        personal_export = build_personal_data_export(self.teacher)["sections"]["personal_workspace"]
        self.assertEqual(personal_export["portfolio_sections"][0]["report_ids"], [report.pk])
        self.assertEqual(personal_export["years"][0]["qualifications"], "بكالوريوس تربية")

    def test_new_portfolio_evidence_uses_personal_library_and_year(self):
        response = self.post_axis(
            "upload_evidence", title="شاهد المحور", academic_year=self.YEAR,
            source_url="https://example.com/portfolio-proof",
        )
        self.assertEqual(response.status_code, 302)
        evidence = PersonalEvidence.objects.get(workspace=self.workspace)
        self.assertEqual(evidence.academic_year, self.YEAR)
        self.assertEqual(PersonalPortfolioEvidence.objects.get().evidence_id, evidence.pk)
        self.assertEqual(self.post_axis(
            "upload_evidence", title="شاهد بسنة مختلفة", academic_year="1448-1449",
            source_url="https://example.com/wrong-year",
        ).status_code, 302)
        self.assertEqual(PersonalEvidence.objects.count(), 1)

    def test_archive_counts_only_documented_axes(self):
        section = PersonalPortfolioSection.objects.create(
            workspace=self.workspace, academic_year=self.YEAR, code=1
        )
        response = self.client.get(reverse("personal:years"))
        self.assertEqual(response.context["years"][0].portfolio_count, 0)
        section.teacher_notes = "ممارسة موثقة"
        section.save(update_fields=["teacher_notes"])
        response = self.client.get(reverse("personal:years"))
        self.assertEqual(response.context["years"][0].portfolio_count, 1)

    def test_successful_practice_stays_personal_across_portfolio_and_exports(self):
        response = self.client.post(reverse("personal:initiatives"), {
            "title": "مبادرة القراءة", "academic_year": self.YEAR,
            "summary": "دوائر قراءة", "impact": "تحسن القراءة",
            "status": "complete", "is_best_practice": "on",
        })
        self.assertEqual(response.status_code, 302)
        initiative = PersonalInitiative.objects.get(workspace=self.workspace)
        self.assertTrue(initiative.is_best_practice)
        self.assertContains(self.client.get(reverse("personal:initiatives")), "ممارسة ناجحة")
        self.assertContains(self.client.get(reverse("personal:portfolio_print") + "?year=" + self.YEAR), "ممارسة ناجحة")
        export = self.client.get(reverse("personal:year_export", args=[self.YEAR]))
        with ZipFile(BytesIO(b"".join(export.streaming_content))) as bundle:
            manifest = bundle.read("manifest.json").decode("utf-8")
        export.close()
        self.assertIn('"is_best_practice": true', manifest)
        personal_export = build_personal_data_export(self.teacher)["sections"]["personal_workspace"]
        self.assertTrue(personal_export["initiatives"][0]["is_best_practice"])

    def test_links_require_owned_resources_same_year_and_section(self):
        foreign = PersonalReport.objects.create(
            workspace=self.other_workspace, title="تقرير غير مملوك", academic_year=self.YEAR,
            report_date=date(2026, 9, 24), description="خارج المساحة", teacher_name=self.other.name,
            school_name=self.other_workspace.school_name,
        )
        wrong_year = PersonalEvidence.objects.create(
            workspace=self.workspace, title="شاهد سنة أخرى", academic_year="1448-1449",
            source_url="https://example.com/other",
        )
        self.assertEqual(self.post_axis("link_report", report_id=foreign.pk).status_code, 404)
        self.assertEqual(self.post_axis("link_evidence", evidence_id=wrong_year.pk).status_code, 404)
        self.assertFalse(PersonalPortfolioReport.objects.exists())
        self.assertFalse(PersonalPortfolioEvidence.objects.exists())
        owned = PersonalReport.objects.create(
            workspace=self.workspace, title="تقريري", academic_year=self.YEAR,
            report_date=date(2026, 9, 24), description="ملكي", teacher_name=self.teacher.name,
            school_name=self.workspace.school_name,
        )
        self.post_axis("link_report", report_id=owned.pk)
        link = PersonalPortfolioReport.objects.get()
        self.assertEqual(self.post_axis("unlink_report", code=2, link_id=link.pk).status_code, 404)
        self.assertTrue(PersonalPortfolioReport.objects.filter(pk=link.pk).exists())
        self.client.force_login(self.other)
        self.assertNotContains(self.client.get(reverse("personal:portfolio")), "تقريري")
        self.assertEqual(self.client.post(reverse("personal:portfolio"), {
            "year": self.YEAR, "section_code": "1", "action": "unlink_report", "link_id": link.pk,
        }).status_code, 404)

    def test_archived_or_inactive_subscription_locks_mutation_but_keeps_reads(self):
        self.post_axis("save_notes", teacher_notes="وصف قديم")
        year = PersonalAcademicYear.objects.get(workspace=self.workspace, value=self.YEAR)
        from django.utils import timezone
        year.archived_at = timezone.now()
        year.save(update_fields=["archived_at"])
        self.post_axis("save_notes", teacher_notes="محاولة تغيير")
        section = PersonalPortfolioSection.objects.get(workspace=self.workspace, code=1)
        self.assertEqual(section.teacher_notes, "وصف قديم")
        response = self.client.get(reverse("personal:portfolio"))
        self.assertContains(response, "أرشيف للقراءة")
        self.assertNotContains(response, 'name="action" value="save_notes"')
        year.archived_at = None
        year.save(update_fields=["archived_at"])
        subscription = PersonalSubscription.objects.get(workspace=self.workspace)
        subscription.is_active = False
        subscription.save(update_fields=["is_active"])
        self.post_axis("save_notes", teacher_notes="محاولة ثانية")
        section.refresh_from_db()
        self.assertEqual(section.teacher_notes, "وصف قديم")
        self.assertEqual(self.client.get(reverse("personal:portfolio_print") + "?year=" + self.YEAR).status_code, 200)
