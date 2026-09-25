from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from reports.models import Report, Teacher

from .models import PersonalAcademicYear, PersonalEvidence, PersonalReport, PersonalWorkspace


@override_settings(ALLOWED_HOSTS=["testserver"], RATELIMIT_ENABLE=False)
class PersonalTeacherReportInterfaceTests(TestCase):
    def setUp(self):
        self.teacher = Teacher.objects.create_user(
            phone="0557990110", name="معلمة مستقلة", password="Personal#2026"  # noqa: S106
        )
        self.workspace = PersonalWorkspace.objects.create(owner=self.teacher)
        self.client.force_login(self.teacher)

    def test_personal_print_uses_school_image_layout_with_mixed_documents(self):
        report = PersonalReport.objects.create(
            workspace=self.workspace, title="تقرير بشواهد مختلطة", report_date=timezone.localdate(),
            academic_year="1447-1448", teacher_name=self.teacher.name, school_name="",
            status=PersonalReport.Status.COMPLETE,
        )

        def add_evidence(title, order, *, file="", source_url="", show_in_print=True):
            return PersonalEvidence.objects.create(
                workspace=self.workspace, report=report, academic_year=report.academic_year,
                title=title, order=order, file=file, source_url=source_url,
                show_in_print=show_in_print,
            )

        pdf = add_evidence("وثيقة PDF", 1, file="personal/evidence/document.pdf")
        first = add_evidence("الصورة الأولى", 2, file="personal/evidence/first.jpg")
        add_evidence("الصورة الثانية", 3, file="personal/evidence/second.png")
        add_evidence("الصورة الثالثة", 4, file="personal/evidence/third.webp")
        link = add_evidence("رابط المصدر", 5, source_url="https://example.org/source")
        add_evidence("صورة مخفية", 6, file="personal/evidence/hidden.jpg", show_in_print=False)

        printed = self.client.get(reverse("personal:report_print", args=[report.pk]))
        self.assertEqual(printed.status_code, 200)
        self.assertTemplateUsed(printed, "reports/report_print.html")
        self.assertNotContains(printed, "وزارة التعليم")
        self.assertContains(printed, "page--dense-evidence")
        self.assertContains(printed, "evidence-section--layout-3")
        self.assertContains(printed, 'class="images-grid images-grid--3 images-grid--mixed"')
        self.assertContains(printed, reverse("personal:evidence_preview", args=[first.pk]))
        self.assertContains(printed, reverse("personal:evidence_download", args=[pdf.pk]))
        self.assertContains(printed, "https://example.org/source")
        self.assertNotContains(printed, "صورة مخفية")
        html = printed.content.decode()
        self.assertLess(html.index("وثيقة PDF"), html.index("الصورة الأولى"))
        self.assertLess(html.index("الصورة الثالثة"), html.index("رابط المصدر"))

        add_evidence("الصورة الرابعة", 7, file="personal/evidence/fourth.jpeg")
        four_images = self.client.get(reverse("personal:report_print", args=[report.pk]))
        self.assertContains(four_images, "evidence-section--layout-4")
        self.assertContains(four_images, 'class="images-grid images-grid--4 images-grid--mixed"')

        pdf.delete()
        link.delete()
        images_only = self.client.get(reverse("personal:report_print", args=[report.pk]))
        self.assertContains(images_only, 'class="images-grid images-grid--4"')
        self.assertNotContains(images_only, 'class="images-grid images-grid--4 images-grid--mixed"')

    def test_school_report_components_serve_personal_report_journey_without_school_record(self):
        form = self.client.get(reverse("personal:report_create"))
        self.assertEqual(form.status_code, 200)
        self.assertTemplateUsed(form, "reports/teacher_report_form.html")
        self.assertContains(form, "css/report-authoring.css")
        self.assertContains(form, 'class="report-authoring-layout"')
        self.assertContains(form, 'data-report-evidence-editor')
        self.assertContains(form, 'name="show_details"')
        self.assertContains(form, "js/hijri-date.js")
        self.assertContains(form, 'id="draftBanner"')
        self.assertNotContains(form, reverse("reports:add_report"))

        saved = self.client.post(reverse("personal:report_create"), {
            "title": "عمل مهني مستقل", "category": "نشاط",
            "report_date": "2026-09-24", "academic_year": "1447-1448",
            "description": "تنفيذ النشاط مع شواهده", "show_details": "on", "status": "complete",
            "selection_enabled": "on", "evidence-TOTAL_FORMS": "3",
            "evidence-INITIAL_FORMS": "0", "evidence-MIN_NUM_FORMS": "0",
            "evidence-MAX_NUM_FORMS": "5", "evidence-0-title": "شاهد النشاط",
            "evidence-0-source_url": "https://example.org/proof",
        })
        report = PersonalReport.objects.get(workspace=self.workspace)
        self.assertRedirects(saved, reverse("personal:report_detail", args=[report.pk]))
        self.assertEqual(Report.objects.count(), 0)

        listing = self.client.get(reverse("personal:reports"), {"year": "1447-1448"})
        self.assertTemplateUsed(listing, "reports/my_reports.html")
        self.assertContains(listing, "css/my-reports.css")
        self.assertContains(listing, "عمل مهني مستقل")
        self.assertContains(listing, "شاهد النشاط")
        detail = self.client.get(reverse("personal:report_detail", args=[report.pk]))
        self.assertContains(detail, "شاهد النشاط")
        self.assertContains(detail, reverse("personal:report_print", args=[report.pk]))
        printed = self.client.get(reverse("personal:report_print", args=[report.pk]))
        self.assertContains(printed, "شاهد النشاط")
        self.assertContains(printed, 'class="report-identity"')
        self.assertContains(printed, "لا يمثل المستند اعتمادًا")
        self.assertNotContains(printed, 'class="approval-block"')
        self.assertContains(self.client.get(reverse("personal:evidence")), "شاهد النشاط")
        self.assertContains(self.client.get(reverse("personal:evidence_create")), "بيانات الشاهد")

    def test_sections_can_omit_details_but_require_one_and_replayed_submit_is_idempotent(self):
        token = str(self.client.get(reverse("personal:report_create")).context["form"]["client_submission_id"].value())
        payload = {
            "title": "تجربة الأهداف", "category": "مهني", "report_date": "2026-09-24",
            "academic_year": "1447-1448", "selection_enabled": "on",
            "show_goals": "on", "goals": "تحسين مهارة القراءة",
            "client_submission_id": token, "status": "complete",
        }
        first = self.client.post(reverse("personal:report_create"), payload)
        report = PersonalReport.objects.get(workspace=self.workspace)
        self.assertRedirects(first, reverse("personal:report_detail", args=[report.pk]))
        self.assertFalse(report.show_details)
        self.assertEqual(report.description, "")
        replay = self.client.post(reverse("personal:report_create"), payload)
        self.assertRedirects(replay, reverse("personal:report_detail", args=[report.pk]))
        self.assertEqual(PersonalReport.objects.filter(workspace=self.workspace).count(), 1)
        detail = self.client.get(reverse("personal:report_detail", args=[report.pk]))
        printed = self.client.get(reverse("personal:report_print", args=[report.pk]))
        self.assertContains(detail, "تحسين مهارة القراءة")
        self.assertNotContains(detail, "<h3>وصف العمل</h3>")
        self.assertContains(printed, "تحسين مهارة القراءة")
        self.assertNotContains(printed, "<span>وصف العمل</span>")
        year = PersonalAcademicYear.objects.get(workspace=self.workspace, value="1447-1448")
        year.archived_at = timezone.now()
        year.save(update_fields=["archived_at"])
        self.assertRedirects(
            self.client.get(reverse("personal:report_edit", args=[report.pk])),
            reverse("personal:report_detail", args=[report.pk]),
        )
        no_sections = self.client.post(reverse("personal:report_create"), {
            **payload, "title": "بلا بنود", "show_goals": "", "goals": "",
            "client_submission_id": "",
        })
        self.assertEqual(no_sections.status_code, 200)
        self.assertContains(no_sections, "اختر بندًا واحدًا على الأقل")
        self.assertEqual(PersonalReport.objects.filter(workspace=self.workspace).count(), 1)
