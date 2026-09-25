"""Public personal links must keep teacher ownership and witness scope intact."""

from datetime import date, timedelta
from tempfile import TemporaryDirectory

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from reports.models import Teacher
from reports.services_data_rights import build_personal_data_export

from .models import (
    PersonalAcademicYear,
    PersonalEvidence, PersonalInitiative,
    PersonalPortfolioEvidence,
    PersonalPortfolioReport, PersonalPortfolioSection,
    PersonalReport,
    PersonalShareLink,
    PersonalWorkspace,
)
from .services import ensure_personal_subscription


@override_settings(ALLOWED_HOSTS=["testserver"], RATELIMIT_ENABLE=False)
class PersonalSharingTests(TestCase):
    YEAR = "1447-1448"

    def setUp(self):
        self.media = TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        media_override = override_settings(MEDIA_ROOT=self.media.name)
        media_override.enable()
        self.addCleanup(media_override.disable)

        self.owner = Teacher.objects.create_user(
            phone="0557990170", name="معلمة مستقلة", password="Personal#2026",  # noqa: S106
        )
        self.workspace = PersonalWorkspace.objects.create(
            owner=self.owner, current_academic_year=self.YEAR,
        )
        self.subscription = ensure_personal_subscription(self.workspace)
        self.year_record = PersonalAcademicYear.objects.create(
            workspace=self.workspace, value=self.YEAR,
            qualifications="بكالوريوس تعليم", contact_info="سر التواصل الخاص 0550000000",
        )
        self.report = PersonalReport.objects.create(
            workspace=self.workspace, title="تقرير القراءة", academic_year=self.YEAR,
            report_date=date(2026, 9, 24), description="درس القراءة", teacher_name=self.owner.name,
        )
        self.visible = self._file_evidence("شاهد ظاهر", report=self.report)
        self.hidden = self._file_evidence("شاهد مخفي", report=self.report, show_in_print=False)
        self.unrelated = self._file_evidence("شاهد غير مرتبط")

        self.other = Teacher.objects.create_user(
            phone="0557990171", name="معلمة أخرى", password="Personal#2026",  # noqa: S106
        )
        self.other_workspace = PersonalWorkspace.objects.create(
            owner=self.other, current_academic_year=self.YEAR,
        )
        ensure_personal_subscription(self.other_workspace)
        self.foreign = self._file_evidence("شاهد حساب آخر", workspace=self.other_workspace)
        self.client.force_login(self.owner)

    def _file_evidence(self, title, *, report=None, workspace=None, show_in_print=True):
        workspace = workspace or self.workspace
        return PersonalEvidence.objects.create(
            workspace=workspace, report=report, academic_year=self.YEAR, title=title,
            file=SimpleUploadedFile(f"{title}.png", b"\x89PNG\r\n\x1a\nimage", content_type="image/png"),
            show_in_print=show_in_print,
        )

    def _enable_report(self):
        response = self.client.post(
            reverse("personal:report_share_manage", args=[self.report.pk]),
            {"action": "enable", "expiry_days": "7"},
        )
        self.assertEqual(response.status_code, 302)
        return PersonalShareLink.objects.get(report=self.report, is_active=True)

    def _assert_private_404(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response["X-Robots-Tag"], "noindex, nofollow")
        self.assertEqual(response["Referrer-Policy"], "no-referrer")

    def test_report_link_rotation_revoke_and_exact_witness_scope(self):
        manage = reverse("personal:report_share_manage", args=[self.report.pk])
        self.assertContains(self.client.get(manage), "محتوى النسخة العامة")
        self.assertEqual(self.client.post(manage, {
            "action": "enable", "expiry_days": "3650",
        }).status_code, 400)
        self.assertFalse(PersonalShareLink.objects.exists())

        first = self._enable_report()
        self.assertGreaterEqual(len(first.token), 40)
        self.assertContains(self.client.get(reverse(
            "personal:report_detail", args=[self.report.pk],
        )), manage)
        exported = build_personal_data_export(self.owner)["sections"]["personal_workspace"]
        self.assertEqual(exported["shares"][0]["report_id"], self.report.pk)
        self.assertEqual(exported["shares"][0]["kind"], "report")
        self.assertNotIn(first.token, str(exported))
        public = reverse("personal:share_public_report", args=[first.token])
        response = self.client.get(public)
        self.assertContains(response, self.report.title)
        self.assertContains(response, self.visible.title)
        self.assertNotContains(response, self.hidden.title)
        self.assertNotContains(response, self.unrelated.title)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response["X-Robots-Tag"], "noindex, nofollow")
        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        self.assertNotContains(response, "سر التواصل الخاص")
        first.refresh_from_db()
        self.assertEqual(first.access_count, 1)

        preview = self.client.get(reverse("personal:share_evidence", args=[
            first.token, self.visible.pk, "preview",
        ]))
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview["Content-Type"], "image/png")
        preview.close()
        for witness in (self.hidden, self.unrelated, self.foreign):
            self._assert_private_404(reverse("personal:share_evidence", args=[
                first.token, witness.pk, "download",
            ]))
        self.visible.show_in_print = False
        self.visible.save(update_fields=["show_in_print"])
        self._assert_private_404(reverse("personal:share_evidence", args=[
            first.token, self.visible.pk, "download",
        ]))
        self.assertNotContains(self.client.get(public), self.visible.title)

        second = self._enable_report()
        self.assertNotEqual(first.token, second.token)
        self._assert_private_404(public)
        self.client.post(manage, {"action": "disable"})
        self._assert_private_404(reverse("personal:share_public_report", args=[second.token]))

    def test_report_link_expires_and_trash_invalidates_target(self):
        link = self._enable_report()
        public = reverse("personal:share_public_report", args=[link.token])
        PersonalShareLink.objects.filter(pk=link.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        self._assert_private_404(public)
        PersonalShareLink.objects.filter(pk=link.pk).update(
            expires_at=timezone.now() + timedelta(days=7),
        )
        self.assertEqual(self.client.get(public).status_code, 200)
        self.assertEqual(self.client.post(reverse(
            "personal:report_delete", args=[self.report.pk],
        )).status_code, 302)
        link.refresh_from_db()
        self.assertFalse(link.is_active)
        self._assert_private_404(public)
        self._assert_private_404(reverse("personal:share_evidence", args=[
            link.token, self.visible.pk, "download",
        ]))
        self.assertEqual(self.client.get(reverse(
            "personal:report_share_manage", args=[self.report.pk],
        )).status_code, 404)
        self.assertEqual(self.client.post(reverse(
            "personal:report_restore", args=[self.report.pk],
        )).status_code, 302)
        self._assert_private_404(public)

    def test_portfolio_public_excludes_contact_and_unlinked_or_hidden_files(self):
        section = PersonalPortfolioSection.objects.create(
            workspace=self.workspace, academic_year=self.YEAR, code=1,
            teacher_notes="ممارسة موثقة",
        )
        PersonalPortfolioReport.objects.create(section=section, report=self.report)
        axis_file = self._file_evidence("شاهد محور")
        PersonalPortfolioEvidence.objects.create(section=section, evidence=axis_file)
        hidden_axis = self._file_evidence("شاهد محور مخفي", show_in_print=False)
        PersonalPortfolioEvidence.objects.create(section=section, evidence=hidden_axis)
        draft = PersonalReport.objects.create(
            workspace=self.workspace, title="مسودة غير مختارة", academic_year=self.YEAR,
            report_date=date(2026, 9, 25), teacher_name=self.owner.name,
        )
        draft_evidence = self._file_evidence("شاهد المسودة", report=draft)
        PersonalInitiative.objects.create(
            workspace=self.workspace, academic_year=self.YEAR,
            title="مبادرة غير مختارة", summary="تفاصيل مبادرة خاصة",
        )
        manage = reverse("personal:portfolio_share_manage")
        self.assertContains(self.client.get(reverse("personal:portfolio"), {
            "year": self.YEAR,
        }), f"{manage}?year={self.YEAR}")
        response = self.client.post(manage, {
            "action": "enable", "year": self.YEAR, "expiry_days": "14",
        })
        self.assertEqual(response.status_code, 302)
        link = PersonalShareLink.objects.get(kind="portfolio", workspace=self.workspace)
        response = self.client.get(reverse("personal:share_public_portfolio", args=[link.token]))
        self.assertContains(response, "ممارسة موثقة")
        self.assertContains(response, self.visible.title)
        self.assertContains(response, axis_file.title)
        self.assertNotContains(response, self.hidden.title)
        self.assertNotContains(response, hidden_axis.title)
        self.assertNotContains(response, self.unrelated.title)
        self.assertNotContains(response, draft.title)
        self.assertNotContains(response, draft_evidence.title)
        self.assertNotContains(response, "مبادرة غير مختارة")
        self.assertNotContains(response, "تفاصيل مبادرة خاصة")
        self.assertNotContains(response, "سر التواصل الخاص")
        self.assertNotIn("contact_info", response.context["year_record"])
        for witness in (self.hidden, hidden_axis, self.unrelated, draft_evidence, self.foreign):
            self._assert_private_404(reverse("personal:share_evidence", args=[
                link.token, witness.pk, "download",
            ]))
        with CaptureQueriesContext(connection) as queries:
            axis_download = self.client.get(reverse("personal:share_evidence", args=[
                link.token, axis_file.pk, "download",
            ]))
        self.assertEqual(axis_download.status_code, 200)
        self.assertLess(len(queries), 10)
        axis_download.close()

    def test_other_teacher_cannot_manage_and_expired_subscription_cannot_create(self):
        manage = reverse("personal:report_share_manage", args=[self.report.pk])
        link = self._enable_report()
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(manage).status_code, 404)
        self.assertEqual(self.client.post(manage, {
            "action": "disable",
        }).status_code, 404)
        self.client.force_login(self.owner)
        self.subscription.is_active = False
        self.subscription.save(update_fields=["is_active"])
        self.assertEqual(self.client.get(reverse(
            "personal:share_public_report", args=[link.token],
        )).status_code, 200)
        self.client.post(manage, {"action": "enable", "expiry_days": "7"})
        self.assertEqual(PersonalShareLink.objects.filter(report=self.report).count(), 1)
        self.client.post(manage, {"action": "disable"})
        self._assert_private_404(reverse("personal:share_public_report", args=[link.token]))

    def test_year_change_revokes_old_report_link_and_allows_new_year_link(self):
        old_link = self._enable_report()
        old_url = reverse("personal:share_public_report", args=[old_link.token])
        self.report.academic_year = "1448-1449"
        self.report.save(update_fields=["academic_year"])
        old_link.refresh_from_db()
        self.assertFalse(old_link.is_active)
        self._assert_private_404(old_url)
        manage = reverse("personal:report_share_manage", args=[self.report.pk])
        self.assertIsNone(self.client.get(manage).context["active_link"])
        new_link = self._enable_report()
        self.assertNotEqual(new_link.token, old_link.token)
        self.assertEqual(new_link.academic_year, "1448-1449")
        self.assertEqual(self.client.get(reverse(
            "personal:share_public_report", args=[new_link.token],
        )).status_code, 200)
