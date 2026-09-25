from datetime import date
from io import BytesIO
from importlib import import_module
import json
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from django.apps import apps
from django.core.files.storage import FileSystemStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from reports.models import Teacher
from reports.model_parts.achievements import AchievementSection
from reports.services_data_rights import build_personal_data_export

from .models import (
    PersonalAcademicYear, PersonalEvidence, PersonalPortfolioEvidence,
    PersonalPortfolioSection, PersonalReport, PersonalWorkspace,
)


@override_settings(ALLOWED_HOSTS=["testserver"], RATELIMIT_ENABLE=False)
class PersonalEvidencePresentationTests(TestCase):
    def setUp(self):
        self.owner = Teacher.objects.create_user(
            phone="0557990130", name="معلمة مستقلة", password="Personal#2026"  # noqa: S106
        )
        self.workspace = PersonalWorkspace.objects.create(owner=self.owner)
        self.report = PersonalReport.objects.create(
            workspace=self.workspace, title="توثيق مبادرة", report_date=date(2026, 9, 24),
            academic_year="1447-1448", description="نشاط", teacher_name=self.owner.name,
            school_name="",
        )
        self.first = PersonalEvidence.objects.create(
            workspace=self.workspace, report=self.report, title="الأول",
            academic_year=self.report.academic_year, source_url="https://example.org/one", order=1,
        )
        self.second = PersonalEvidence.objects.create(
            workspace=self.workspace, report=self.report, title="الثاني",
            academic_year=self.report.academic_year, source_url="https://example.org/two", order=2,
        )
        self.client.force_login(self.owner)

    def test_owner_edits_print_options_and_reorders_report_evidence(self):
        edit_url = reverse("personal:evidence_edit", args=[self.second.pk])
        form = self.client.get(edit_url)
        self.assertContains(form, "حجم العرض")
        self.assertContains(form, "طريقة الملاءمة")
        self.assertContains(form, "إظهار هذا الشاهد في النسخة المطبوعة")
        edited = self.client.post(edit_url, {
            "title": "الثاني المعدل", "academic_year": self.report.academic_year,
            "report": self.report.pk, "source_url": "https://example.org/two",
            "presentation_enabled": "on", "display_size": "large", "fit_mode": "cover",
        })
        self.assertRedirects(edited, reverse("personal:report_detail", args=[self.report.pk]))
        self.second.refresh_from_db()
        self.assertEqual((self.second.display_size, self.second.fit_mode), ("large", "cover"))
        self.assertFalse(self.second.show_in_print)
        printed = self.client.get(reverse("personal:report_print", args=[self.report.pk]))
        self.assertNotContains(printed, "الثاني المعدل")

        moved = self.client.post(reverse("personal:report_evidence_move", args=[self.report.pk]), {
            "evidence_id": self.second.pk, "direction": "up",
        })
        self.assertRedirects(moved, reverse("personal:report_detail", args=[self.report.pk]))
        ordered = list(self.report.evidence.order_by("order", "id").values_list("pk", flat=True))
        self.assertEqual(ordered, [self.second.pk, self.first.pk])
        detail = self.client.get(reverse("personal:report_detail", args=[self.report.pk]))
        self.assertContains(detail, "الثاني المعدل")
        self.assertContains(detail, "مخفي في الطباعة")
        self.assertContains(detail, reverse("personal:evidence_edit", args=[self.second.pk]))
        listed = self.client.get(reverse("personal:reports"))
        listed_report = next(row for row in listed.context["reports"] if row.pk == self.report.pk)
        self.assertEqual([item.pk for item in listed_report.evidence.all()], [
            self.second.pk, self.first.pk,
        ])

        shown = self.client.post(edit_url, {
            "title": "الثاني المعدل", "academic_year": self.report.academic_year,
            "report": self.report.pk, "source_url": "https://example.org/two",
            "presentation_enabled": "on", "display_size": "large", "fit_mode": "cover",
            "show_in_print": "on",
        })
        self.assertEqual(shown.status_code, 302)
        printed = self.client.get(reverse("personal:report_print", args=[self.report.pk]))
        self.assertContains(printed, "الثاني المعدل")
        printed_html = printed.content.decode("utf-8")
        self.assertLess(printed_html.find("الثاني المعدل"), printed_html.find("الأول"))

    def test_moving_evidence_to_another_report_appends_its_order(self):
        destination = PersonalReport.objects.create(
            workspace=self.workspace, title="تقرير آخر", report_date=date(2026, 9, 24),
            academic_year=self.report.academic_year, teacher_name=self.owner.name,
        )
        PersonalEvidence.objects.create(
            workspace=self.workspace, report=destination, title="شاهد سابق",
            academic_year=destination.academic_year,
            source_url="https://example.org/existing", order=1,
        )
        response = self.client.post(reverse("personal:evidence_edit", args=[self.first.pk]), {
            "title": self.first.title, "academic_year": self.report.academic_year,
            "report": destination.pk, "source_url": self.first.source_url,
        })
        self.assertRedirects(response, reverse("personal:report_detail", args=[destination.pk]))
        self.first.refresh_from_db()
        self.assertEqual((self.first.report_id, self.first.order), (destination.pk, 2))

    def test_edit_and_reorder_are_owner_and_year_scoped(self):
        other = Teacher.objects.create_user(
            phone="0557990131", name="معلم آخر", password="Personal#2026"  # noqa: S106
        )
        PersonalWorkspace.objects.create(owner=other)
        self.client.force_login(other)
        self.assertEqual(self.client.get(reverse("personal:evidence_edit", args=[self.first.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("personal:report_evidence_move", args=[self.report.pk]), {
            "evidence_id": self.first.pk, "direction": "down",
        }).status_code, 404)
        self.client.force_login(self.owner)
        archived = PersonalAcademicYear.objects.create(
            workspace=self.workspace, value=self.report.academic_year, archived_at=timezone.now()
        )
        self.assertRedirects(
            self.client.get(reverse("personal:evidence_edit", args=[self.first.pk])),
            reverse("personal:evidence"),
        )
        self.assertRedirects(
            self.client.post(reverse("personal:report_evidence_move", args=[self.report.pk]), {
                "evidence_id": self.first.pk, "direction": "down",
            }),
            reverse("personal:report_detail", args=[self.report.pk]),
        )
        self.assertEqual(list(self.report.evidence.order_by("order", "id").values_list("pk", flat=True)), [
            self.first.pk, self.second.pk,
        ])
        archived.delete()

    def test_inline_evidence_saves_presentation_without_school_record(self):
        created = self.client.post(reverse("personal:report_create"), {
            "title": "تقرير جديد", "category": "نشاط", "report_date": "2026-09-24",
            "academic_year": "1447-1448", "description": "تفاصيل",
            "show_details": "on", "selection_enabled": "on", "status": "complete",
            "evidence-TOTAL_FORMS": "3", "evidence-INITIAL_FORMS": "0",
            "evidence-MIN_NUM_FORMS": "0", "evidence-MAX_NUM_FORMS": "5",
            "evidence-0-title": "شاهد العرض", "evidence-0-source_url": "https://example.org/proof",
            "evidence-0-presentation_enabled": "on",
            "evidence-0-display_size": "medium", "evidence-0-fit_mode": "cover",
        })
        self.assertEqual(created.status_code, 302)
        item = PersonalEvidence.objects.get(title="شاهد العرض")
        self.assertEqual((item.display_size, item.fit_mode, item.order), ("medium", "cover", 1))
        self.assertFalse(item.show_in_print)

    def test_print_uses_school_image_size_and_fit_contract(self):
        image = PersonalEvidence.objects.create(
            workspace=self.workspace, report=self.report, title="صورة التنفيذ",
            academic_year=self.report.academic_year, file="personal/proof.png",
            order=3, display_size=PersonalEvidence.DisplaySize.LARGE,
            fit_mode=PersonalEvidence.FitMode.COVER,
        )
        printed = self.client.get(reverse("personal:report_print", args=[self.report.pk]))
        self.assertContains(printed, 'class="img-box img-box--large"')
        self.assertContains(printed, 'class="img-fit--cover"')
        self.assertContains(printed, reverse("personal:evidence_preview", args=[image.pk]))
        self.assertContains(printed, "@media screen { .img-box img.img-fit--cover { object-fit: cover; } }")

    def test_print_preserves_configured_order_across_links_images_and_pdf(self):
        self.first.order = 4
        self.first.save(update_fields=["order"])
        self.second.order = 1
        self.second.save(update_fields=["order"])
        image = PersonalEvidence.objects.create(
            workspace=self.workspace, report=self.report, title="صورة الوسط",
            academic_year=self.report.academic_year, file="personal/middle.png", order=2,
        )
        pdf = PersonalEvidence.objects.create(
            workspace=self.workspace, report=self.report, title="ملف بعد الصورة",
            academic_year=self.report.academic_year, file="personal/document.pdf", order=3,
        )
        printed = self.client.get(reverse("personal:report_print", args=[self.report.pk]))
        html = printed.content.decode("utf-8").split('class="images-grid', 1)[1]
        self.assertLess(html.index(self.second.title), html.index(image.title))
        self.assertLess(html.index(image.title), html.index(pdf.title))
        self.assertLess(html.index(pdf.title), html.index(self.first.title))
        self.assertContains(printed, 'class="img-box img-box--auto"')
        self.assertContains(printed, 'class="personal-print-evidence-link"')

        portfolio = self.client.get(reverse("personal:portfolio_print"), {"year": self.report.academic_year})
        library = portfolio.content.decode("utf-8").split("<h1>مكتبة الشواهد</h1>", 1)[1]
        self.assertLess(library.index(self.second.title), library.index(image.title))
        self.assertLess(library.index(image.title), library.index(pdf.title))
        self.assertLess(library.index(pdf.title), library.index(self.first.title))

    def test_portfolio_upload_defaults_to_print_and_linked_evidence_keeps_its_year(self):
        self.workspace.current_academic_year = self.report.academic_year
        self.workspace.save(update_fields=["current_academic_year"])
        uploaded = self.client.post(reverse("personal:portfolio"), {
            "year": self.report.academic_year, "action": "upload_evidence",
            "section_code": AchievementSection.Code.choices[0][0],
            "title": "شاهد المحور", "academic_year": self.report.academic_year,
            "source_url": "https://example.org/axis", "report": self.report.pk,
        })
        self.assertEqual(uploaded.status_code, 302)
        axis_evidence = PersonalEvidence.objects.get(title="شاهد المحور")
        self.assertTrue(axis_evidence.show_in_print)
        self.assertEqual(axis_evidence.order, 3)
        self.assertTrue(PersonalPortfolioEvidence.objects.filter(evidence=axis_evidence).exists())

        changed = self.client.post(reverse("personal:evidence_edit", args=[axis_evidence.pk]), {
            "title": axis_evidence.title, "academic_year": "1448-1449",
            "source_url": axis_evidence.source_url,
        })
        self.assertEqual(changed.status_code, 200)
        self.assertContains(changed, "لا يمكن تغيير سنة شاهد مرتبط بمحور ملف الإنجاز")
        axis_evidence.refresh_from_db()
        self.assertEqual(axis_evidence.academic_year, self.report.academic_year)
        self.assertTrue(axis_evidence.show_in_print)

    def test_replacing_evidence_file_uses_shared_after_commit_cleanup(self):
        field = PersonalEvidence._meta.get_field("file")
        original_storage = field.storage
        with TemporaryDirectory() as directory:
            storage = FileSystemStorage(location=directory)
            field.storage = storage
            try:
                self.first.file = SimpleUploadedFile(
                    "old.pdf", b"%PDF-1.4\n%%EOF", content_type="application/pdf",
                )
                self.first.file_size = self.first.file.size
                self.first.save(update_fields=["file", "file_size"])
                old_name = self.first.file.name
                self.assertTrue(storage.exists(old_name))
                with self.captureOnCommitCallbacks(execute=True):
                    replaced = self.client.post(reverse("personal:evidence_edit", args=[self.first.pk]), {
                        "title": self.first.title, "academic_year": self.report.academic_year,
                        "report": self.report.pk, "source_url": self.first.source_url,
                        "file": SimpleUploadedFile(
                            "new.pdf", b"%PDF-1.4\nnew\n%%EOF", content_type="application/pdf",
                        ),
                    })
                self.assertEqual(replaced.status_code, 302)
                self.first.refresh_from_db()
                self.assertNotEqual(self.first.file.name, old_name)
                self.assertFalse(storage.exists(old_name))
                self.assertTrue(storage.exists(self.first.file.name))
            finally:
                field.storage = original_storage

    def test_report_accepts_eight_evidence_and_blocks_ninth_from_library_or_edit(self):
        payload = {
            "title": "تقرير ثمانية شواهد", "category": "نشاط", "report_date": "2026-09-24",
            "academic_year": self.report.academic_year, "description": "تفاصيل",
            "show_details": "on", "selection_enabled": "on", "status": "complete",
            "evidence-TOTAL_FORMS": "8", "evidence-INITIAL_FORMS": "0",
            "evidence-MIN_NUM_FORMS": "0", "evidence-MAX_NUM_FORMS": "8",
        }
        for index in range(8):
            payload[f"evidence-{index}-title"] = f"شاهد {index + 1}"
            payload[f"evidence-{index}-source_url"] = f"https://example.org/proof-{index + 1}"
        created = self.client.post(reverse("personal:report_create"), payload)
        self.assertEqual(created.status_code, 302)
        full_report = PersonalReport.objects.get(title="تقرير ثمانية شواهد")
        self.assertEqual(full_report.evidence.count(), 8)
        self.assertEqual(list(full_report.evidence.order_by("order").values_list("order", flat=True)), list(range(1, 9)))
        edit_form = self.client.get(reverse("personal:report_edit", args=[full_report.pk]))
        self.assertContains(edit_form, 'name="evidence-MAX_NUM_FORMS" value="0"')

        extra = self.client.post(reverse("personal:evidence_create"), {
            "title": "التاسع", "academic_year": self.report.academic_year,
            "report": full_report.pk, "source_url": "https://example.org/ninth",
        })
        self.assertEqual(extra.status_code, 200)
        self.assertContains(extra, "الحد الأعلى للتقرير 8 شواهد")
        self.assertFalse(PersonalEvidence.objects.filter(title="التاسع").exists())

        moved = self.client.post(reverse("personal:evidence_edit", args=[self.first.pk]), {
            "title": self.first.title, "academic_year": self.report.academic_year,
            "report": full_report.pk, "source_url": self.first.source_url,
        })
        self.assertEqual(moved.status_code, 200)
        self.first.refresh_from_db()
        self.assertEqual(self.first.report_id, self.report.pk)
        self.assertEqual(full_report.evidence.count(), 8)

    def test_year_and_privacy_exports_include_evidence_presentation(self):
        self.first.display_size = PersonalEvidence.DisplaySize.MEDIUM
        self.first.fit_mode = PersonalEvidence.FitMode.COVER
        self.first.show_in_print = False
        self.first.save(update_fields=["display_size", "fit_mode", "show_in_print"])
        PersonalAcademicYear.objects.create(workspace=self.workspace, value=self.report.academic_year)
        response = self.client.get(reverse("personal:year_export", args=[self.report.academic_year]))
        self.assertEqual(response.status_code, 200)
        with ZipFile(BytesIO(b"".join(response.streaming_content))) as bundle:
            manifest = json.loads(bundle.read("manifest.json"))
        response.close()
        exported = next(item for item in manifest["evidence"] if item["id"] == self.first.pk)
        self.assertEqual([exported[key] for key in ("order", "display_size", "fit_mode", "show_in_print")], [
            1, "medium", "cover", False,
        ])
        privacy = build_personal_data_export(self.owner)["sections"]["personal_workspace"]
        exported = next(item for item in privacy["evidence"] if item["id"] == self.first.pk)
        self.assertEqual([exported[key] for key in ("order", "display_size", "fit_mode", "show_in_print")], [
            1, "medium", "cover", False,
        ])

    def test_migration_backfills_legacy_report_order(self):
        migration = import_module("personal.migrations.0013_personal_evidence_presentation")
        migration.backfill_report_evidence_order(apps, type("Editor", (), {"connection": connection})())
        self.assertEqual(list(self.report.evidence.order_by("order", "id").values_list("pk", flat=True)), [
            self.second.pk, self.first.pk,
        ])

    def test_portfolio_print_hides_evidence_marked_out_of_print_and_updates_counts(self):
        self.report.show_details = False
        self.report.description = "وصف مخفي من الطباعة"
        self.report.save(update_fields=["show_details", "description"])
        self.first.show_in_print = False
        self.first.save(update_fields=["show_in_print"])
        section = PersonalPortfolioSection.objects.create(
            workspace=self.workspace, academic_year=self.report.academic_year,
            code=AchievementSection.Code.choices[0][0],
        )
        PersonalPortfolioEvidence.objects.create(section=section, evidence=self.first)
        regular = self.client.get(reverse("personal:portfolio"), {"year": self.report.academic_year})
        self.assertContains(regular, self.first.title)
        self.assertEqual(regular.context["documented_count"], 1)

        printed = self.client.get(reverse("personal:portfolio_print"), {"year": self.report.academic_year})
        self.assertEqual(printed.status_code, 200)
        self.assertNotContains(printed, self.first.source_url)
        self.assertNotContains(printed, self.report.description)
        self.assertContains(printed, self.second.title)
        self.assertEqual(printed.context["evidence"].count(), 1)
        self.assertEqual(printed.context["documented_count"], 0)
        self.assertEqual(printed.context["sections"][0]["evidence"], [])
