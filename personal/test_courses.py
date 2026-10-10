"""Annual course journeys and certificate boundaries in the personal workspace."""

import json
from datetime import date
from decimal import Decimal
from io import BytesIO
from zipfile import ZipFile
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from reports.academic_years import MINISTRY_CALENDAR_URL
from reports.models import AcademicYear, Teacher

from .models import PersonalAcademicYear, PersonalCourse, PersonalEvidence, PersonalPortfolioEvidence, PersonalPortfolioSection, PersonalWorkspace
from .models import PersonalReport
from .course_forms import PersonalCourseForm
from .services import ensure_personal_subscription


@override_settings(ALLOWED_HOSTS=["testserver"], RATELIMIT_ENABLE=False)
class PersonalCourseJourneyTests(TestCase):
    YEAR = "1448-1449"
    PREVIOUS_YEAR = "1447-1448"

    def setUp(self):
        for year, starts, ends in (
            (self.YEAR, date(2026, 8, 23), date(2027, 6, 24)),
            (self.PREVIOUS_YEAR, date(2025, 8, 24), date(2026, 6, 25)),
        ):
            AcademicYear.objects.update_or_create(value=year, defaults={
                "is_active": True, "start_date": starts, "end_date": ends,
                "source_url": MINISTRY_CALENDAR_URL,
            })
        self.owner = Teacher.objects.create_user(phone="0557964311", name="معلمة مستقلة")
        self.other = Teacher.objects.create_user(phone="0557964312", name="معلم آخر")
        self.workspace = PersonalWorkspace.objects.create(owner=self.owner, current_academic_year=self.YEAR)
        self.other_workspace = PersonalWorkspace.objects.create(owner=self.other, current_academic_year=self.YEAR)
        self.subscription = ensure_personal_subscription(self.workspace)
        PersonalAcademicYear.objects.create(workspace=self.workspace, value=self.YEAR)
        PersonalAcademicYear.objects.create(workspace=self.workspace, value=self.PREVIOUS_YEAR)
        self.certificate = PersonalEvidence.objects.create(
            workspace=self.workspace, title="شهادة التطوير المهني", academic_year=self.YEAR,
            source_url="https://example.org/certificate",
        )
        self.client.force_login(self.owner)

    def payload(self, **changes):
        payload = {
            "title": "استراتيجيات التدريس", "provider": "مركز التدريب التربوي",
            "academic_year": self.YEAR, "start_date": "2026-09-01",
            "completion_date": "2026-09-05", "hours": "12.50",
            "description": "تطبيق استراتيجيات التعلم النشط في الصف.",
            "certificate": str(self.certificate.pk),
        }
        payload.update(changes)
        return payload

    def create_course(self, **changes):
        response = self.client.post(reverse("personal:course_create"), self.payload(**changes))
        self.assertEqual(response.status_code, 302)
        return PersonalCourse.objects.get(workspace=self.workspace, title=changes.get("title", "استراتيجيات التدريس"))

    def test_create_edit_search_and_completed_hours_are_annual(self):
        course = self.create_course()
        self.create_course(title="دورة سابقة", academic_year=self.PREVIOUS_YEAR, certificate="", hours="30")
        overview = self.client.get(reverse("personal:courses"), {"year": self.YEAR})
        self.assertContains(overview, "استراتيجيات التدريس")
        self.assertNotContains(overview, "دورة سابقة")
        self.assertEqual(overview.context["total_count"], 1)
        self.assertEqual(overview.context["completed_count"], 1)
        self.assertEqual(overview.context["completed_hours"], Decimal("12.5"))
        self.assertEqual(self.client.get(reverse("personal:courses"), {"year": self.YEAR, "q": "مركز التدريب"}).context["page_obj"].paginator.count, 1)
        edit = self.client.post(reverse("personal:course_edit", args=[course.pk]), self.payload(title="تطبيقات التعلم النشط", academic_year=self.PREVIOUS_YEAR))
        self.assertEqual(edit.status_code, 302)
        course.refresh_from_db()
        self.assertEqual(course.title, "تطبيقات التعلم النشط")
        self.assertEqual(course.academic_year, self.YEAR)
        self.assertEqual(self.workspace.evidence.count(), 1)

    def test_certificate_picker_rejects_foreign_owner_and_other_year(self):
        foreign = PersonalEvidence.objects.create(workspace=self.other_workspace, title="شهادة معلم آخر", academic_year=self.YEAR, source_url="https://example.org/foreign")
        previous = PersonalEvidence.objects.create(workspace=self.workspace, title="شهادة سنة سابقة", academic_year=self.PREVIOUS_YEAR, source_url="https://example.org/previous")
        for certificate in (foreign, previous):
            with self.subTest(certificate=certificate.pk):
                response = self.client.post(reverse("personal:course_create"), self.payload(certificate=str(certificate.pk)))
                self.assertEqual(response.status_code, 200)
                self.assertIn("certificate", response.context["form"].errors)
                self.assertFalse(self.workspace.courses.exists())
        page = self.client.get(reverse("personal:course_create"), {"year": self.YEAR})
        self.assertEqual(list(page.context["form"].fields["certificate"].queryset), [self.certificate])

    def test_date_validation_retains_input_and_connects_errors(self):
        response = self.client.post(reverse("personal:course_create"), self.payload(completion_date="2026-08-01"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("completion_date", response.context["form"].errors)
        self.assertContains(response, 'aria-invalid="true"')
        self.assertContains(response, "استراتيجيات التدريس")
        self.assertFalse(self.workspace.courses.exists())

    def test_certificate_picker_omits_inconsistent_legacy_report_links(self):
        foreign_report = PersonalReport.objects.create(
            workspace=self.other_workspace, academic_year=self.YEAR,
            title="تقرير معلم آخر", report_date=date(2026, 9, 1), teacher_name=self.other.name,
        )
        PersonalEvidence.objects.filter(pk=self.certificate.pk).update(report=foreign_report)
        page = self.client.get(reverse("personal:course_create"), {"year": self.YEAR})
        self.assertFalse(page.context["form"].fields["certificate"].queryset.exists())
        rejected = self.client.post(reverse("personal:course_create"), self.payload())
        self.assertIn("certificate", rejected.context["form"].errors)
        self.assertFalse(self.workspace.courses.exists())
        self.certificate.refresh_from_db()
        self.assertEqual(self.certificate.report_id, foreign_report.pk)

    def test_course_form_rejects_unpublished_historical_year(self):
        AcademicYear.objects.filter(value=self.PREVIOUS_YEAR).update(is_active=False)
        form = PersonalCourseForm(self.payload(academic_year=self.PREVIOUS_YEAR, certificate=""), workspace=self.workspace, year=self.YEAR)
        self.assertFalse(form.is_valid())
        self.assertIn("academic_year", form.errors)

    def test_optional_certificate_and_completion_date(self):
        self.create_course(certificate="", completion_date="")
        page = self.client.get(reverse("personal:courses"), {"year": self.YEAR})
        self.assertEqual(page.context["completed_count"], 0)
        self.assertEqual(page.context["completed_hours"], 0)
        self.assertContains(page, "دون شهادة مرفقة")
        self.assertContains(page, "لم يُسجل بعد")

    def test_nonpositive_training_hours_are_rejected(self):
        response = self.client.post(reverse("personal:course_create"), self.payload(hours="0"))
        self.assertIn("hours", response.context["form"].errors)
        self.assertFalse(self.workspace.courses.exists())

    def test_archived_year_and_inactive_subscription_block_all_writes(self):
        course = self.create_course()
        PersonalAcademicYear.objects.filter(workspace=self.workspace, value=self.YEAR).update(archived_at=timezone.now())
        for route, args, data in (
            ("personal:course_create", [], self.payload()),
            ("personal:course_edit", [course.pk], self.payload(title="تغيير ممنوع")),
            ("personal:course_delete", [course.pk], {"confirm_delete": "yes"}),
        ):
            self.assertEqual(self.client.post(reverse(route, args=args), data).status_code, 302)
        course.refresh_from_db()
        self.assertEqual(course.title, "استراتيجيات التدريس")
        PersonalAcademicYear.objects.filter(workspace=self.workspace, value=self.YEAR).update(archived_at=None)
        self.subscription.is_active = False
        self.subscription.save(update_fields=["is_active"])
        self.assertEqual(self.client.post(reverse("personal:course_edit", args=[course.pk]), self.payload(title="تعديل غير نشط")).status_code, 302)
        self.assertEqual(self.client.post(reverse("personal:course_delete", args=[course.pk]), {"confirm_delete": "yes"}).status_code, 302)
        self.assertEqual(self.workspace.courses.count(), 1)
        self.assertFalse(self.client.get(reverse("personal:courses"), {"year": self.YEAR}).context["can_edit"])

    def test_unpublished_historical_year_is_read_only(self):
        course = self.create_course()
        AcademicYear.objects.filter(value=self.YEAR).update(is_active=False)
        page = self.client.get(reverse("personal:courses"), {"year": self.YEAR})
        self.assertContains(page, course.title)
        self.assertFalse(page.context["can_edit"])
        self.client.post(reverse("personal:course_delete", args=[course.pk]), {"confirm_delete": "yes"})
        self.assertTrue(PersonalCourse.objects.filter(pk=course.pk).exists())

    def test_subscription_is_rechecked_inside_each_write_transaction(self):
        course = self.create_course()
        self.subscription.is_active = False
        self.subscription.save(update_fields=["is_active"])
        # Simulate a request that passed the preflight check before deactivation.
        with patch("personal.course_views._can_edit", return_value=True):
            create = self.client.post(reverse("personal:course_create"), self.payload(title="دورة لا يسمح بها"))
            edit = self.client.post(reverse("personal:course_edit", args=[course.pk]), self.payload(title="تعديل لا يسمح به"))
            deleted = self.client.post(reverse("personal:course_delete", args=[course.pk]), {"confirm_delete": "yes"})
        self.assertEqual((create.status_code, edit.status_code, deleted.status_code), (200, 200, 302))
        course.refresh_from_db()
        self.assertEqual(course.title, "استراتيجيات التدريس")
        self.assertEqual(self.workspace.courses.count(), 1)

    def test_foreign_workspace_course_routes_return_404(self):
        course = PersonalCourse.objects.create(
            workspace=self.other_workspace, title="دورة خاصة", provider="مركز",
            academic_year=self.YEAR, start_date=date(2026, 9, 1), hours=10,
        )
        for route in ("personal:course_edit", "personal:course_delete"):
            self.assertEqual(self.client.get(reverse(route, args=[course.pk])).status_code, 404)
            self.assertEqual(self.client.post(reverse(route, args=[course.pk]), self.payload(confirm_delete="yes")).status_code, 404)

    def test_direct_edit_and_delete_context_matches_record_without_changing_browsing_year(self):
        course = self.create_course()
        self.client.get(reverse("personal:courses"), {"year": self.PREVIOUS_YEAR})
        key = f"personal_academic_year:{self.workspace.pk}"
        for route in ("personal:course_edit", "personal:course_delete"):
            page = self.client.get(reverse(route, args=[course.pk]))
            self.assertEqual(page.context["personal_selected_year"], self.YEAR)
            self.assertEqual(self.client.session[key], self.PREVIOUS_YEAR)

    def test_existing_course_cancel_save_and_delete_return_to_browsing_year(self):
        course = self.create_course()
        self.client.get(reverse("personal:courses"), {"year": self.PREVIOUS_YEAR})
        key = f"personal_academic_year:{self.workspace.pk}"
        edit_url = reverse("personal:course_edit", args=[course.pk])
        edit_page = self.client.get(edit_url)
        self.assertEqual(edit_page.context["list_url"], reverse("personal:courses"))
        cancelled = self.client.get(edit_page.context["list_url"])
        self.assertEqual(cancelled.context["year"], self.PREVIOUS_YEAR)
        self.assertEqual(self.client.session[key], self.PREVIOUS_YEAR)
        course.refresh_from_db()
        self.assertEqual(course.title, "استراتيجيات التدريس")
        saved = self.client.post(edit_url, self.payload(title="تطوير استراتيجيات التدريس"), follow=True)
        self.assertEqual(saved.redirect_chain, [(reverse("personal:courses"), 302)])
        self.assertEqual(saved.context["year"], self.PREVIOUS_YEAR)
        self.assertEqual(self.client.session[key], self.PREVIOUS_YEAR)
        course.refresh_from_db()
        self.assertEqual((course.title, course.academic_year), ("تطوير استراتيجيات التدريس", self.YEAR))
        delete_url = reverse("personal:course_delete", args=[course.pk])
        delete_page = self.client.get(delete_url)
        self.assertEqual(delete_page.context["list_url"], reverse("personal:courses"))
        self.assertEqual(self.client.get(delete_page.context["list_url"]).context["year"], self.PREVIOUS_YEAR)
        self.assertTrue(PersonalCourse.objects.filter(pk=course.pk).exists())
        deleted = self.client.post(delete_url, {"confirm_delete": "yes"}, follow=True)
        self.assertEqual(deleted.redirect_chain, [(reverse("personal:courses"), 302)])
        self.assertEqual(deleted.context["year"], self.PREVIOUS_YEAR)
        self.assertEqual(self.client.session[key], self.PREVIOUS_YEAR)
        self.assertFalse(PersonalCourse.objects.filter(pk=course.pk).exists())
        self.assertTrue(PersonalEvidence.objects.filter(pk=self.certificate.pk).exists())

    def test_delete_requires_confirmation_and_preserves_certificate(self):
        course = self.create_course()
        url = reverse("personal:course_delete", args=[course.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertTrue(PersonalCourse.objects.filter(pk=course.pk).exists())
        rejected = self.client.post(url, {})
        self.assertEqual(rejected.status_code, 200)
        self.assertContains(rejected, "أكّد حذف سجل الدورة")
        self.assertTrue(PersonalCourse.objects.filter(pk=course.pk).exists())
        self.assertEqual(self.client.post(url, {"confirm_delete": "yes"}).status_code, 302)
        self.assertFalse(PersonalCourse.objects.filter(pk=course.pk).exists())
        self.assertTrue(PersonalEvidence.objects.filter(pk=self.certificate.pk).exists())

    def test_course_and_certificate_cannot_move_across_years_at_model_boundary(self):
        course = self.create_course()
        course.academic_year = self.PREVIOUS_YEAR
        course.certificate = None
        with self.assertRaises(ValidationError):
            course.save()
        self.certificate.academic_year = self.PREVIOUS_YEAR
        with self.assertRaises(ValidationError):
            self.certificate.save()

    def test_portfolio_linked_certificate_cannot_leave_section_scope(self):
        section = PersonalPortfolioSection.objects.create(workspace=self.workspace, academic_year=self.YEAR, code=1)
        PersonalPortfolioEvidence.objects.create(section=section, evidence=self.certificate)
        for field, value in (("academic_year", self.PREVIOUS_YEAR), ("workspace", self.other_workspace)):
            with self.subTest(field=field):
                setattr(self.certificate, field, value)
                with self.assertRaises(ValidationError):
                    self.certificate.save()
                self.certificate.refresh_from_db()
                self.assertEqual(self.certificate.workspace_id, self.workspace.pk)
                self.assertEqual(self.certificate.academic_year, self.YEAR)

    def test_deleting_certificate_retains_course_record(self):
        course = self.create_course()
        self.certificate.delete()
        course.refresh_from_db()
        self.assertIsNone(course.certificate_id)

    def test_annual_export_includes_course_and_certificate_reference(self):
        course = self.create_course()
        self.create_course(title="من العام السابق", academic_year=self.PREVIOUS_YEAR, certificate="")
        response = self.client.get(reverse("personal:year_export", args=[self.YEAR]))
        self.assertEqual(response.status_code, 200)
        content = b"".join(response.streaming_content)
        with ZipFile(BytesIO(content)) as bundle:
            manifest = json.loads(bundle.read("manifest.json"))
        self.assertEqual(len(manifest["courses"]), 1)
        self.assertEqual(manifest["courses"][0]["id"], course.pk)
        self.assertEqual(manifest["courses"][0]["certificate_evidence_id"], self.certificate.pk)
        self.assertEqual(manifest["evidence"][0]["id"], self.certificate.pk)
