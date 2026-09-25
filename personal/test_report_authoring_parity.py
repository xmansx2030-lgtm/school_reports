"""Personal report authoring uses the school editor without school ownership."""

from datetime import date, timedelta
from io import BytesIO
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from reports.middleware import FORCE_PASSWORD_CHANGE_SESSION_KEY
from reports.model_parts.achievements import AchievementSection
from reports.models import (
    Report, ReportEvidence, School, SchoolMembership, SchoolSubscription,
    SubscriptionPlan, Teacher,
)
from reports.report_ai import ReportAIUnavailable, report_ai_daily_remaining
from reports.voice_report import VoiceReportUnavailable, voice_report_daily_remaining

from .assistant_quota import daily_remaining
from .models import (
    PersonalAcademicYear,
    PersonalEvidence,
    PersonalPlan,
    PersonalPortfolioEvidence,
    PersonalPortfolioSection,
    PersonalReport,
    PersonalWorkspace,
)
from .services import ensure_personal_subscription


def image_upload(name="proof.png", *, color=(20, 100, 60)):
    output = BytesIO()
    Image.new("RGB", (400, 300), color).save(output, format="PNG")
    return SimpleUploadedFile(name, output.getvalue(), content_type="image/png")


@override_settings(ALLOWED_HOSTS=["testserver"], RATELIMIT_ENABLE=False)
class PersonalSchoolReportAuthoringParityTests(TestCase):
    YEAR = "1447-1448"

    def setUp(self):
        cache.clear()
        media = TemporaryDirectory()
        self.addCleanup(media.cleanup)
        settings_override = override_settings(MEDIA_ROOT=media.name)
        settings_override.enable()
        self.addCleanup(settings_override.disable)

        self.owner = Teacher.objects.create_user(
            phone="0557990170", name="معلمة مستقلة", password="Personal#2026",  # noqa: S106
        )
        self.workspace = PersonalWorkspace.objects.create(
            owner=self.owner, current_academic_year=self.YEAR,
        )
        self.subscription = ensure_personal_subscription(self.workspace)
        self.client.force_login(self.owner)

    def enable_paid_assistance(self, *, limit=3):
        paid = PersonalPlan.objects.create(
            code="personal_parity_paid", name="باقة المعلم المدفوعة", price="99.00",
            duration_days=365, report_ai_daily_limit=limit, voice_report_daily_limit=limit,
        )
        self.subscription.plan = paid
        self.subscription.save()
        return paid

    @staticmethod
    def voice_upload():
        return SimpleUploadedFile(
            "clip.webm", b"\x1a\x45\xdf\xa3" + b"0" * 40_000,
            content_type="audio/webm;codecs=opus",
        )

    def editor_payload(self, **changes):
        payload = {
            "section_selection_enabled": "on",
            "title": "عمل مهني شخصي",
            "category": "نشاط",
            "report_date": "2026-09-24",
            "academic_year": self.YEAR,
            "status": "complete",
            "show_goal": "on",
            "goal": "تحسين تعلم الطالبات",
            "show_details": "on",
            "idea": "نُفذ النشاط وسُجلت نتائجه.",
            "evidence-TOTAL_FORMS": "1",
            "evidence-INITIAL_FORMS": "0",
            "evidence-MIN_NUM_FORMS": "0",
            "evidence-MAX_NUM_FORMS": "8",
        }
        payload.update(changes)
        return payload

    def make_report(self, title="تقرير محفوظ"):
        return PersonalReport.objects.create(
            workspace=self.workspace,
            title=title,
            category="نشاط",
            report_date=date(2026, 9, 24),
            academic_year=self.YEAR,
            description="تفاصيل التقرير",
            teacher_name=self.owner.name,
            school_name="",
        )

    def test_create_renders_the_school_authoring_components_and_field_hooks(self):
        response = self.client.get(reverse("personal:report_create"))

        self.assertEqual(response.status_code, 200)
        for partial in (
            "reports/partials/report_evidence_formset.html",
            "reports/partials/report_evidence_card.html",
            "reports/partials/report_summary_rail.html",
            "reports/partials/report_mobile_actions.html",
        ):
            self.assertTemplateUsed(response, partial)
        for hook in (
            'id="report-form"',
            'class="report-authoring-layout"',
            'data-report-evidence-editor',
            'data-evidence-template',
            'name="section_selection_enabled"',
            'name="show_goal"',
            'name="goal"',
            'name="idea"',
            'name="implementation_method"',
            'id="validationAlert"',
        ):
            self.assertContains(response, hook)
        for asset in (
            "css/report-evidence.css",
            "css/report-rail.css",
            "css/report-mobile-actions.css",
            "js/report-authoring.js",
            "js/report-evidence-editor.js",
            "js/report-summary-rail.js",
        ):
            self.assertContains(response, asset)
        self.assertNotContains(response, reverse("reports:add_report"))

    @override_settings(PWA_INSTALL_ENABLED=True)
    def test_personal_dashboard_uses_shared_standalone_manifest_and_install_prompt(self):
        dashboard = self.client.get(reverse("personal:dashboard"))
        self.assertEqual(dashboard.status_code, 200)
        self.assertContains(dashboard, 'href="/static/manifest.json"')
        self.assertContains(dashboard, "js/pwa-install.js")
        self.assertContains(dashboard, "data-pwa-install-trigger")
        self.assertContains(dashboard, 'id="pwaInstallPrompt"')
        self.assertNotContains(dashboard, "js/web-push.js")
        self.assertNotContains(dashboard, 'id="webPushPrompt"')
        self.assertRedirects(
            self.client.get(reverse("reports:home")), reverse("personal:dashboard"),
        )

    def test_shared_pwa_home_resumes_personal_space_with_stale_expired_school(self):
        school = School.objects.create(
            name="مدرسة اشتراكها منتهٍ", code="expired-pwa-home", stage="primary", gender="boys",
        )
        plan = SubscriptionPlan.objects.create(name="اشتراك مدرسة منتهٍ", price=0, days_duration=30)
        subscription = SchoolSubscription.objects.create(school=school, plan=plan)
        SchoolSubscription.objects.filter(pk=subscription.pk).update(
            start_date=timezone.localdate() - timedelta(days=50),
            end_date=timezone.localdate() - timedelta(days=20),
        )
        SchoolMembership.objects.create(
            school=school, teacher=self.owner, role_type=SchoolMembership.RoleType.TEACHER,
            is_active=True,
        )
        session = self.client.session
        session["active_school_id"] = school.pk
        session.save()

        self.assertRedirects(
            self.client.get(reverse("reports:home")), reverse("personal:dashboard"),
        )
        for school_route, personal_route in (
            ("reports:my_notifications", "personal:notices"),
            ("reports:role_guidance", "personal:dashboard"),
            ("reports:my_profile", "personal:account"),
        ):
            self.assertRedirects(
                self.client.get(reverse(school_route)), reverse(personal_route),
            )

    def test_shared_school_shortcuts_route_personal_only_and_keep_current_school_teacher(self):
        shortcuts = (
            ("reports:my_notifications", "personal:notices"),
            ("reports:role_guidance", "personal:dashboard"),
            ("reports:my_profile", "personal:account"),
        )
        for school_route, personal_route in shortcuts:
            self.assertRedirects(
                self.client.get(reverse(school_route)), reverse(personal_route),
            )

        school = School.objects.create(
            name="مدرسة اشتراكها نشط", code="active-pwa-shortcuts", stage="primary", gender="boys",
        )
        plan = SubscriptionPlan.objects.create(name="اشتراك مدرسة نشط", price=0, days_duration=30)
        SchoolSubscription.objects.create(school=school, plan=plan)
        SchoolMembership.objects.create(
            school=school, teacher=self.owner, role_type=SchoolMembership.RoleType.TEACHER,
            is_active=True,
        )
        session = self.client.session
        session["active_school_id"] = school.pk
        session.save()
        for school_route, _personal_route in shortcuts:
            response = self.client.get(reverse(school_route))
            self.assertEqual(response.status_code, 200, msg=school_route)

    def test_forced_password_change_opens_school_profile_for_personal_owner(self):
        session = self.client.session
        session[FORCE_PASSWORD_CHANGE_SESSION_KEY] = True
        session.save()

        response = self.client.get(reverse("reports:my_profile"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "reports/my_profile.html")
        self.assertTrue(response.context["force_password_change"])
        self.assertContains(response, 'name="update_password"')

    def test_scheduled_school_cancellation_keeps_school_home_for_personal_owner(self):
        school = School.objects.create(
            name="مدرسة إلغاؤها مجدول", code="scheduled-pwa-home",
            stage="primary", gender="boys",
        )
        plan = SubscriptionPlan.objects.create(
            name="اشتراك مدرسة بإلغاء مجدول", price=0, days_duration=30,
        )
        subscription = SchoolSubscription.objects.create(school=school, plan=plan)
        subscription.canceled_at = timezone.now() + timedelta(days=10)
        subscription.save(update_fields=["canceled_at"])
        SchoolMembership.objects.create(
            school=school, teacher=self.owner,
            role_type=SchoolMembership.RoleType.TEACHER, is_active=True,
        )
        session = self.client.session
        session["active_school_id"] = school.pk
        session.save()

        response = self.client.get(reverse("reports:home"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "reports/home.html")

    def test_school_style_submission_creates_only_personal_report_and_image(self):
        response = self.client.post(reverse("personal:report_create"), self.editor_payload(**{
            "academic_year": "1448-1449",  # The active personal year is server owned.
            "status": "archived",  # The editor must keep this server owned.
            "evidence-0-image": image_upload(),
            "evidence-0-order": "1",
            "evidence-0-description": "صورة من التنفيذ",
            "evidence-0-display_size": "large",
            "evidence-0-fit_mode": "cover",
            "evidence-0-show_in_print": "on",
        }))

        report = PersonalReport.objects.get(workspace=self.workspace)
        self.assertRedirects(response, reverse("personal:report_detail", args=[report.pk]))
        self.assertEqual((report.goals, report.description), (
            "تحسين تعلم الطالبات", "نُفذ النشاط وسُجلت نتائجه.",
        ))
        self.assertEqual(report.teacher_name, self.owner.name)
        self.assertEqual(report.academic_year, self.YEAR)
        self.assertEqual(report.status, PersonalReport.Status.COMPLETE)
        evidence = PersonalEvidence.objects.get(report=report)
        self.assertEqual(evidence.workspace_id, self.workspace.pk)
        self.assertEqual(evidence.academic_year, self.YEAR)
        self.assertEqual(evidence.title, "صورة من التنفيذ")
        self.assertEqual((evidence.display_size, evidence.fit_mode), ("large", "cover"))
        self.assertTrue(evidence.is_image)
        self.assertTrue(evidence.show_in_print)
        preview = self.client.get(reverse("personal:evidence_preview", args=[evidence.pk]))
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview["Content-Type"], "image/webp")
        preview.close()
        self.assertEqual(Report.objects.count(), 0)
        self.assertEqual(ReportEvidence.objects.count(), 0)

    def test_newly_registered_teacher_receives_a_savable_academic_year(self):
        self.workspace.current_academic_year = ""
        self.workspace.save(update_fields=["current_academic_year"])

        editor = self.client.get(reverse("personal:report_create"))
        self.assertEqual(editor.status_code, 200)
        year = editor.context["form"]["academic_year"].value()
        self.assertRegex(year, r"^\d{4}-\d{4}$")

        response = self.client.post(reverse("personal:report_create"), self.editor_payload(**{
            "academic_year": year,
        }))
        report = PersonalReport.objects.get(workspace=self.workspace)
        self.assertRedirects(response, reverse("personal:report_detail", args=[report.pk]))
        self.assertEqual(report.academic_year, year)
        self.assertEqual(Report.objects.count(), 0)

    def test_school_length_image_caption_survives_personal_save_edit_and_print(self):
        caption = "ش" * 210
        response = self.client.post(reverse("personal:report_create"), self.editor_payload(**{
            "evidence-0-image": image_upload(),
            "evidence-0-order": "1",
            "evidence-0-description": caption,
            "evidence-0-show_in_print": "on",
        }))

        self.assertEqual(response.status_code, 302, msg=(
            response.context["form"].errors,
            response.context["evidence_formset"].errors,
            response.context["evidence_formset"].non_form_errors(),
        ) if response.context else None)
        report = PersonalReport.objects.get(workspace=self.workspace)
        self.assertRedirects(response, reverse("personal:report_detail", args=[report.pk]))
        evidence = PersonalEvidence.objects.get(report=report)
        self.assertEqual(evidence.report_caption, caption)
        self.assertLessEqual(len(evidence.title), 200)
        self.assertTrue(evidence.show_in_print)
        editor = self.client.get(reverse("personal:report_edit", args=[report.pk]))
        self.assertEqual(
            editor.context["evidence_formset"].initial_forms[0]["description"].value(), caption,
        )
        printed = self.client.get(reverse("personal:report_print", args=[report.pk]))
        self.assertEqual(printed.status_code, 200)
        self.assertTrue(caption in printed.content.decode("utf-8"), "Print omitted the full caption")

    def test_old_long_description_survives_edit_but_new_overlimit_text_is_rejected(self):
        report = self.make_report()
        original = "ق" * 650
        report.description = original
        report.save(update_fields=["description"])
        edit_url = reverse("personal:report_edit", args=[report.pk])
        editor = self.client.get(edit_url)
        self.assertEqual(editor.context["form"]["idea"].value(), original)

        unchanged = self.client.post(edit_url, self.editor_payload(**{"idea": original}))
        self.assertRedirects(unchanged, reverse("personal:report_detail", args=[report.pk]))
        report.refresh_from_db()
        self.assertEqual(report.description, original)

        changed = self.client.post(
            edit_url, self.editor_payload(**{"idea": "س" * 601}),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(changed.status_code, 422)
        report.refresh_from_db()
        self.assertEqual(report.description, original)

    def test_edit_reorders_replaces_and_deletes_images_without_touching_link_or_pdf(self):
        report = self.make_report()
        first = PersonalEvidence.objects.create(
            workspace=self.workspace, report=report, academic_year=self.YEAR,
            title="الأول", file=image_upload("first.png"), order=1,
        )
        second = PersonalEvidence.objects.create(
            workspace=self.workspace, report=report, academic_year=self.YEAR,
            title="الثاني", file=image_upload("second.png"), order=2,
        )
        third = PersonalEvidence.objects.create(
            workspace=self.workspace, report=report, academic_year=self.YEAR,
            title="الثالث", file=image_upload("third.png"), order=3,
        )
        fourth = PersonalEvidence.objects.create(
            workspace=self.workspace, report=report, academic_year=self.YEAR,
            title="الرابع", file=image_upload("fourth.png"), order=4,
        )
        section = PersonalPortfolioSection.objects.create(
            workspace=self.workspace, academic_year=self.YEAR,
            code=AchievementSection.Code.choices[0][0],
        )
        portfolio_link = PersonalPortfolioEvidence.objects.create(
            section=section, evidence=third,
        )
        link = PersonalEvidence.objects.create(
            workspace=self.workspace, report=report, academic_year=self.YEAR,
            title="رابط محفوظ", source_url="https://example.org/proof", order=5,
        )
        pdf = PersonalEvidence.objects.create(
            workspace=self.workspace, report=report, academic_year=self.YEAR,
            title="PDF محفوظ", file=SimpleUploadedFile(
                "saved.pdf", b"%PDF-1.4\n%%EOF", content_type="application/pdf",
            ), order=6,
        )
        edit_url = reverse("personal:report_edit", args=[report.pk])
        editor = self.client.get(edit_url)
        self.assertEqual(editor.status_code, 200)
        self.assertTemplateUsed(editor, "reports/partials/report_evidence_formset.html")
        self.assertContains(editor, 'data-existing-document-count="2"')
        self.assertContains(editor, 'data-max-forms="6"')
        self.assertContains(editor, "رابط محفوظ")
        self.assertContains(editor, "PDF محفوظ")
        self.assertContains(editor, reverse("personal:evidence_edit", args=[link.pk]))
        self.assertContains(editor, reverse("personal:evidence_edit", args=[pdf.pk]))
        image_rows = {
            form.instance.pk: index
            for index, form in enumerate(editor.context["evidence_formset"].initial_forms)
        }
        self.assertEqual(set(image_rows), {first.pk, second.pk, third.pk, fourth.pk})
        old_file_name = first.file.name
        payload = self.editor_payload(**{
            "evidence-TOTAL_FORMS": str(len(image_rows)),
            "evidence-INITIAL_FORMS": str(len(image_rows)),
        })
        for evidence in (first, second, third, fourth):
            index = image_rows[evidence.pk]
            payload.update({
                f"evidence-{index}-id": str(evidence.pk),
                f"evidence-{index}-order": (
                    "2" if evidence == first else "1" if evidence == second else (
                        "3" if evidence == third else "4"
                    )
                ),
                f"evidence-{index}-description": "باقٍ بعد التعديل" if evidence == first else evidence.title,
                f"evidence-{index}-display_size": "medium",
                f"evidence-{index}-fit_mode": "contain",
                f"evidence-{index}-show_in_print": "on",
            })
        payload[f"evidence-{image_rows[first.pk]}-image"] = image_upload(
            "replacement.png", color=(100, 20, 60),
        )
        payload[f"evidence-{image_rows[third.pk]}-DELETE"] = "on"
        payload[f"evidence-{image_rows[fourth.pk]}-DELETE"] = "on"

        response = self.client.post(edit_url, payload)

        self.assertEqual(response.status_code, 302, msg=(
            response.context["form"].errors,
            response.context["evidence_formset"].errors,
            response.context["evidence_formset"].non_form_errors(),
        ) if response.context else None)
        self.assertRedirects(response, reverse("personal:report_detail", args=[report.pk]))
        first.refresh_from_db()
        self.assertEqual(first.description, "باقٍ بعد التعديل")
        self.assertNotEqual(first.file.name, old_file_name)
        second.refresh_from_db()
        self.assertEqual((second.order, first.order), (1, 2))
        third.refresh_from_db()
        self.assertIsNone(third.report_id)
        self.assertTrue(PersonalPortfolioEvidence.objects.filter(pk=portfolio_link.pk).exists())
        self.assertFalse(PersonalEvidence.objects.filter(pk=fourth.pk).exists())
        self.assertTrue(PersonalEvidence.objects.filter(pk=link.pk, report=report).exists())
        self.assertTrue(PersonalEvidence.objects.filter(pk=pdf.pk, report=report).exists())
        self.assertEqual(Report.objects.count(), 0)

    def test_reordering_images_keeps_interleaved_pdf_position(self):
        report = self.make_report()
        first = PersonalEvidence.objects.create(
            workspace=self.workspace, report=report, academic_year=self.YEAR,
            title="صورة أولى", file=image_upload("first.png"), order=1,
        )
        pdf = PersonalEvidence.objects.create(
            workspace=self.workspace, report=report, academic_year=self.YEAR,
            title="ملف بين الصورتين", file=SimpleUploadedFile(
                "between.pdf", b"%PDF-1.4\n%%EOF", content_type="application/pdf",
            ), order=2,
        )
        second = PersonalEvidence.objects.create(
            workspace=self.workspace, report=report, academic_year=self.YEAR,
            title="صورة ثانية", file=image_upload("second.png"), order=3,
        )
        edit_url = reverse("personal:report_edit", args=[report.pk])
        editor = self.client.get(edit_url)
        rows = {
            form.instance.pk: index
            for index, form in enumerate(editor.context["evidence_formset"].initial_forms)
        }
        payload = self.editor_payload(**{
            "evidence-TOTAL_FORMS": "2", "evidence-INITIAL_FORMS": "2",
        })
        for item, wanted_order in ((first, 2), (second, 1)):
            index = rows[item.pk]
            payload.update({
                f"evidence-{index}-id": str(item.pk),
                f"evidence-{index}-description": item.title,
                f"evidence-{index}-order": str(wanted_order),
                f"evidence-{index}-show_in_print": "on",
            })

        response = self.client.post(edit_url, payload)

        self.assertEqual(response.status_code, 302, msg=(
            response.context["form"].errors,
            response.context["evidence_formset"].errors,
            response.context["evidence_formset"].non_form_errors(),
        ) if response.context else None)
        self.assertRedirects(response, reverse("personal:report_detail", args=[report.pk]))
        first.refresh_from_db()
        second.refresh_from_db()
        pdf.refresh_from_db()
        self.assertEqual((second.order, pdf.order, first.order), (1, 2, 3))

    def test_new_image_dragged_before_existing_keeps_interleaved_pdf_position(self):
        report = self.make_report()
        existing = PersonalEvidence.objects.create(
            workspace=self.workspace, report=report, academic_year=self.YEAR,
            title="الصورة القديمة", file=image_upload("old.png"), order=1,
        )
        pdf = PersonalEvidence.objects.create(
            workspace=self.workspace, report=report, academic_year=self.YEAR,
            title="الملف الأوسط", file=SimpleUploadedFile(
                "middle.pdf", b"%PDF-1.4\n%%EOF", content_type="application/pdf",
            ), order=2,
        )
        edit_url = reverse("personal:report_edit", args=[report.pk])
        editor = self.client.get(edit_url)
        self.assertEqual(editor.context["evidence_formset"].initial_form_count(), 1)
        payload = self.editor_payload(**{
            "evidence-TOTAL_FORMS": "2", "evidence-INITIAL_FORMS": "1",
            "evidence-0-id": str(existing.pk),
            "evidence-0-order": "2",
            "evidence-0-description": existing.title,
            "evidence-0-show_in_print": "on",
            "evidence-1-image": image_upload("new.png"),
            "evidence-1-order": "1",
            "evidence-1-description": "الصورة الجديدة",
            "evidence-1-show_in_print": "on",
        })

        response = self.client.post(edit_url, payload)

        self.assertEqual(response.status_code, 302, msg=(
            response.context["form"].errors,
            response.context["evidence_formset"].errors,
            response.context["evidence_formset"].non_form_errors(),
        ) if response.context else None)
        existing.refresh_from_db()
        pdf.refresh_from_db()
        added = PersonalEvidence.objects.get(report=report, title="الصورة الجديدة")
        self.assertEqual((added.order, pdf.order, existing.order), (1, 2, 3))

    def test_foreign_image_id_even_with_delete_cannot_mutate_another_workspace(self):
        report = self.make_report()
        own = PersonalEvidence.objects.create(
            workspace=self.workspace, report=report, academic_year=self.YEAR,
            title="شاهد خاص", file=image_upload("own.png"), order=1,
        )
        other = Teacher.objects.create_user(
            phone="0557990171", name="معلم آخر", password="Personal#2026",  # noqa: S106
        )
        foreign_workspace = PersonalWorkspace.objects.create(owner=other)
        foreign_report = PersonalReport.objects.create(
            workspace=foreign_workspace, title="تقرير خارجي",
            report_date=date(2026, 9, 24), academic_year=self.YEAR,
            teacher_name=other.name, school_name="",
        )
        foreign = PersonalEvidence.objects.create(
            workspace=foreign_workspace, report=foreign_report, academic_year=self.YEAR,
            title="محمي", file=image_upload("foreign.png"), order=1,
        )
        response = self.client.post(
            reverse("personal:report_edit", args=[report.pk]),
            self.editor_payload(**{
                "title": "عنوان غير مصرح بحفظه",
                "evidence-INITIAL_FORMS": "1",
                "evidence-0-id": str(foreign.pk),
                "evidence-0-order": "1",
                "evidence-0-DELETE": "on",
            }),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 422)
        report.refresh_from_db()
        foreign.refresh_from_db()
        self.assertEqual(report.title, "تقرير محفوظ")
        self.assertEqual(foreign.report_id, foreign_report.pk)
        self.assertTrue(PersonalEvidence.objects.filter(pk=own.pk).exists())

    def test_another_report_image_id_even_with_delete_is_rejected(self):
        report = self.make_report("التقرير المستهدف")
        other_report = self.make_report("تقرير آخر للمعلمة نفسها")
        own = PersonalEvidence.objects.create(
            workspace=self.workspace, report=report, academic_year=self.YEAR,
            title="شاهد التقرير المستهدف", file=image_upload("own.png"), order=1,
        )
        foreign = PersonalEvidence.objects.create(
            workspace=self.workspace, report=other_report, academic_year=self.YEAR,
            title="شاهد التقرير الآخر", file=image_upload("other.png"), order=1,
        )

        response = self.client.post(
            reverse("personal:report_edit", args=[report.pk]),
            self.editor_payload(**{
                "title": "تغيير غير مسموح",
                "evidence-INITIAL_FORMS": "1",
                "evidence-0-id": str(foreign.pk),
                "evidence-0-order": "1",
                "evidence-0-DELETE": "on",
            }),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 422)
        report.refresh_from_db()
        foreign.refresh_from_db()
        self.assertEqual(report.title, "التقرير المستهدف")
        self.assertEqual(foreign.report_id, other_report.pk)
        self.assertTrue(PersonalEvidence.objects.filter(pk=own.pk, report=report).exists())

    def test_invalid_xhr_image_rejects_entire_report(self):
        response = self.client.post(
            reverse("personal:report_create"),
            self.editor_payload(**{
                "evidence-0-image": SimpleUploadedFile(
                    "invalid.png", b"not an image", content_type="image/png",
                ),
                "evidence-0-order": "1",
            }),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 422)
        self.assertContains(response, "تعذّر حفظ التقرير", status_code=422)
        self.assertEqual(PersonalReport.objects.count(), 0)
        self.assertEqual(PersonalEvidence.objects.count(), 0)

    def test_new_editor_rejects_forged_category_but_legacy_saved_category_remains_usable(self):
        forged_category = "تصنيف حر قديم"
        denied = self.client.post(
            reverse("personal:report_create"),
            self.editor_payload(**{"category": forged_category}),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(denied.status_code, 422)
        self.assertEqual(PersonalReport.objects.count(), 0)

        legacy = self.client.post(reverse("personal:report_create"), {
            "title": "تقرير قديم", "category": forged_category,
            "report_date": "2026-09-24", "academic_year": self.YEAR,
            "selection_enabled": "on", "show_details": "on",
            "description": "تفاصيل محفوظة", "status": "complete",
        })
        report = PersonalReport.objects.get(workspace=self.workspace)
        self.assertRedirects(legacy, reverse("personal:report_detail", args=[report.pk]))
        self.assertEqual(report.category, forged_category)
        editor = self.client.get(reverse("personal:report_edit", args=[report.pk]))
        self.assertContains(editor, f'value="{forged_category}"')

    def test_school_editor_image_upload_obeys_personal_plan_evidence_limit(self):
        self.subscription.plan.max_evidence = 1
        self.subscription.plan.save(update_fields=["max_evidence"])
        PersonalEvidence.objects.create(
            workspace=self.workspace, academic_year=self.YEAR,
            title="شاهد سابق", source_url="https://example.org/previous",
        )

        response = self.client.post(
            reverse("personal:report_create"),
            self.editor_payload(**{
                "evidence-0-image": image_upload(),
                "evidence-0-order": "1",
            }),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(PersonalReport.objects.count(), 0)
        self.assertEqual(PersonalEvidence.objects.count(), 1)

    def test_school_editor_image_upload_obeys_personal_plan_storage_limit(self):
        self.subscription.plan.storage_limit_mb = 1
        self.subscription.plan.save(update_fields=["storage_limit_mb"])
        existing_file = SimpleUploadedFile(
            "almost-full.pdf", b"%PDF-1.4\n" + b"x" * (1024 * 1024 - 50),
            content_type="application/pdf",
        )
        PersonalEvidence.objects.create(
            workspace=self.workspace, academic_year=self.YEAR,
            title="ملف سابق", file=existing_file, file_size=existing_file.size,
        )

        response = self.client.post(
            reverse("personal:report_create"),
            self.editor_payload(**{
                "evidence-0-image": image_upload(),
                "evidence-0-order": "1",
            }),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(PersonalReport.objects.count(), 0)
        self.assertEqual(PersonalEvidence.objects.count(), 1)

    def test_mark_complete_is_owner_only_and_requires_active_year_and_subscription(self):
        report = self.make_report()
        endpoint = reverse("personal:report_mark_complete", args=[report.pk])
        self.assertEqual(self.client.get(endpoint).status_code, 405)

        other = Teacher.objects.create_user(
            phone="0557990173", name="معلم آخر", password="Personal#2026",  # noqa: S106
        )
        PersonalWorkspace.objects.create(owner=other, current_academic_year=self.YEAR)
        self.client.force_login(other)
        self.assertEqual(self.client.post(endpoint).status_code, 404)

        self.client.force_login(self.owner)
        year = PersonalAcademicYear.objects.create(
            workspace=self.workspace, value=self.YEAR, archived_at=timezone.now(),
        )
        self.assertRedirects(self.client.post(endpoint), reverse("personal:report_detail", args=[report.pk]))
        report.refresh_from_db()
        self.assertEqual(report.status, PersonalReport.Status.DRAFT)

        year.archived_at = None
        year.save(update_fields=["archived_at"])
        self.subscription.is_active = False
        self.subscription.save(update_fields=["is_active"])
        self.assertRedirects(self.client.post(endpoint), reverse("personal:report_detail", args=[report.pk]))
        report.refresh_from_db()
        self.assertEqual(report.status, PersonalReport.Status.DRAFT)

        self.subscription.is_active = True
        self.subscription.save(update_fields=["is_active"])
        self.assertRedirects(self.client.post(endpoint), reverse("personal:report_detail", args=[report.pk]))
        report.refresh_from_db()
        self.assertEqual(report.status, PersonalReport.Status.COMPLETE)

    @override_settings(OPENAI_API_KEY="test-key", REPORT_REVIEW_ENABLED=True)
    def test_readiness_uses_personal_structural_endpoint_without_ai_or_school_context(self):
        endpoint = reverse("personal:review_report_readiness")
        school_endpoint = reverse("reports:review_report_readiness")
        editor = self.client.get(reverse("personal:report_create"))
        self.assertContains(editor, f'data-endpoint="{endpoint}"')
        self.assertNotContains(editor, f'data-endpoint="{school_endpoint}"')

        draft = {
            "title": "",
            "category": "نشاط",
            "report_date": "2026-09-24",
            "idea": "",
            "evidence_count": 0,
            "sections": {"show_details": True},
        }
        with patch("reports.report_review.semantic_review") as semantic, patch(
            "reports.views.reports._get_active_school"
        ) as active_school:
            response = self.client.post(
                endpoint, data=json.dumps(draft, ensure_ascii=False),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertTrue(result["ok"])
        self.assertFalse(result["semantic"])
        self.assertTrue({"title", "idea"}.issubset({item["field"] for item in result["issues"]}))
        semantic.assert_not_called()
        active_school.assert_not_called()
        self.assertEqual(PersonalReport.objects.count(), 0)

        school_user = Teacher.objects.create_user(
            phone="0557990172", name="معلم بلا مساحة شخصية", password="Personal#2026",  # noqa: S106
        )
        self.client.force_login(school_user)
        unauthorized = self.client.post(
            endpoint, data=json.dumps(draft, ensure_ascii=False),
            content_type="application/json",
        )
        self.assertRedirects(unauthorized, reverse("personal:setup"))

    @override_settings(
        OPENAI_API_KEY="test-key", REPORT_AI_ENABLED=True, REPORT_REVIEW_ENABLED=True,
        VOICE_REPORT_ENABLED=True, VOICE_REPORT_PWA_ONLY=False,
    )
    def test_personal_only_account_cannot_use_school_paid_provider_routes(self):
        school = School.objects.create(
            name="مدرسة لا يملك عضويتها", code="forged-ai-school", stage="primary", gender="boys",
        )
        plan = SubscriptionPlan.objects.create(name="اشتراك مدرسة أخرى", price=0, days_duration=30)
        SchoolSubscription.objects.create(school=school, plan=plan)
        session = self.client.session
        session["active_school_id"] = school.pk
        session.save()
        text = "نُفّذ برنامج تعليمي وحقق النتائج المتوقعة خلال اليوم الدراسي."
        with patch("reports.views.reports.platform_ai_toggle_enabled", return_value=True), patch(
            "reports.views.reports.improve_report_text_with_ai"
        ) as improve, patch(
            "reports.views.reports.review_report_draft"
        ) as review, patch("reports.views.reports.transcribe_audio") as transcribe, patch(
            "reports.views.reports.reserve_report_ai_daily_slot"
        ) as text_slot, patch("reports.views.reports.reserve_voice_report_daily_slot") as voice_slot:
            responses = (
                self.client.post(
                    reverse("reports:improve_report_text"),
                    data=json.dumps({"text": text}, ensure_ascii=False),
                    content_type="application/json",
                ),
                self.client.post(
                    reverse("reports:review_report_readiness"),
                    data=json.dumps({"title": "تقرير", "idea": text}, ensure_ascii=False),
                    content_type="application/json",
                ),
                self.client.post(reverse("reports:transcribe_report_voice"), {
                    "audio": SimpleUploadedFile(
                        "clip.webm", b"\x1a\x45\xdf\xa3" + b"0" * 40_000,
                        content_type="audio/webm;codecs=opus",
                    ),
                }),
            )

        for response in responses:
            self.assertEqual(response.status_code, 403)
            self.assertFalse(response.json()["ok"])
            self.assertEqual(response.json()["reason"], "school_membership_required")
            self.assertIn("no-store", response["Cache-Control"])
        improve.assert_not_called()
        review.assert_not_called()
        transcribe.assert_not_called()
        text_slot.assert_not_called()
        voice_slot.assert_not_called()

    @override_settings(
        OPENAI_API_KEY="test-key", REPORT_AI_ENABLED=True,
        VOICE_REPORT_ENABLED=True, VOICE_REPORT_PWA_ONLY=True,
    )
    def test_paid_personal_editor_uses_personal_ai_and_voice_with_separate_quotas(self):
        self.enable_paid_assistance()
        editor = self.client.get(reverse("personal:report_create"))
        self.assertContains(editor, "js/report-ai-improver.js")
        self.assertContains(editor, "js/report-voice.js")
        self.assertContains(editor, f'data-endpoint="{reverse("personal:improve_report_text")}"')
        self.assertContains(editor, f'data-endpoint="{reverse("personal:transcribe_report_voice")}"')
        self.assertNotContains(editor, f'data-endpoint="{reverse("reports:improve_report_text")}"')
        self.assertNotContains(editor, f'data-endpoint="{reverse("reports:transcribe_report_voice")}"')

        original = "نُفّذ نشاط مهني للطالبات، وسُجلت نتائجه وملاحظات التحسين."
        with patch(
            "personal.assistant_views.improve_report_text_with_ai",
            return_value="نُفّذ نشاط مهني للطالبات وسُجلت نتائجه بوضوح.",
        ) as improve, patch(
            "personal.assistant_views.transcribe_audio", return_value="اليوم نفذت نشاط مهني"
        ) as transcribe, patch(
            "personal.assistant_views.polish_dictation", return_value="نُفّذ اليوم نشاط مهني."
        ):
            improved = self.client.post(
                reverse("personal:improve_report_text"),
                data=json.dumps({"text": original}, ensure_ascii=False),
                content_type="application/json",
            )
            browser_voice = self.client.post(
                reverse("personal:transcribe_report_voice"), {"audio": self.voice_upload()},
            )
            standalone_voice = self.client.post(
                reverse("personal:transcribe_report_voice"), {"audio": self.voice_upload()},
                HTTP_X_TAWTHEEQ_SURFACE="standalone",
            )

        self.assertEqual(improved.status_code, 200)
        self.assertTrue(improved.json()["ok"])
        self.assertEqual(improved.json()["remaining"], 2)
        self.assertEqual(improve.call_count, 1)
        self.assertEqual(browser_voice.status_code, 403)
        self.assertEqual(browser_voice.json()["reason"], "pwa_required")
        self.assertEqual(standalone_voice.status_code, 200)
        self.assertEqual(standalone_voice.json()["raw_text"], "اليوم نفذت نشاط مهني")
        self.assertEqual(standalone_voice.json()["remaining"], 2)
        self.assertEqual(transcribe.call_count, 1)
        self.assertEqual(daily_remaining("improvement", self.owner.pk, 3), 2)
        self.assertEqual(daily_remaining("voice", self.owner.pk, 3), 2)
        self.assertEqual(report_ai_daily_remaining(self.owner.pk), 3)
        self.assertEqual(voice_report_daily_remaining(self.owner.pk), 3)
        self.assertEqual(PersonalReport.objects.count(), 0)

    @override_settings(
        OPENAI_API_KEY="test-key", REPORT_AI_ENABLED=True,
        VOICE_REPORT_ENABLED=True, VOICE_REPORT_PWA_ONLY=False,
    )
    def test_free_and_inactive_personal_plans_never_reach_paid_providers(self):
        text_payload = json.dumps({
            "text": "نُفّذ نشاط مهني للطالبات، وسُجلت نتائجه وملاحظات التحسين."
        }, ensure_ascii=False)
        with patch("personal.assistant_views.improve_report_text_with_ai") as improve, patch(
            "personal.assistant_views.transcribe_audio"
        ) as transcribe, patch("personal.assistant_views.reserve_daily_slot") as reserve:
            free_page = self.client.get(reverse("personal:report_create"))
            self.assertNotContains(free_page, "js/report-ai-improver.js")
            self.assertNotContains(free_page, "js/report-voice.js")
            free_text = self.client.post(
                reverse("personal:improve_report_text"), data=text_payload,
                content_type="application/json",
            )
            free_voice = self.client.post(
                reverse("personal:transcribe_report_voice"), {"audio": self.voice_upload()},
            )
            self.enable_paid_assistance()
            self.subscription.is_active = False
            self.subscription.save(update_fields=["is_active"])
            inactive_text = self.client.post(
                reverse("personal:improve_report_text"), data=text_payload,
                content_type="application/json",
            )
            inactive_voice = self.client.post(
                reverse("personal:transcribe_report_voice"), {"audio": self.voice_upload()},
            )

        self.assertEqual((free_text.status_code, free_voice.status_code), (403, 403))
        self.assertEqual(free_text.json()["reason"], "plan_upgrade_required")
        self.assertEqual(free_voice.json()["reason"], "plan_upgrade_required")
        self.assertEqual((inactive_text.status_code, inactive_voice.status_code), (403, 403))
        self.assertEqual(inactive_text.json()["reason"], "subscription_required")
        self.assertEqual(inactive_voice.json()["reason"], "subscription_required")
        improve.assert_not_called()
        transcribe.assert_not_called()
        reserve.assert_not_called()

    @override_settings(
        OPENAI_API_KEY="test-key", REPORT_AI_ENABLED=True,
        VOICE_REPORT_ENABLED=True, VOICE_REPORT_PWA_ONLY=False,
    )
    def test_personal_assistant_quotas_refund_failed_calls_and_stop_at_plan_limit(self):
        self.enable_paid_assistance(limit=1)
        text_url = reverse("personal:improve_report_text")
        voice_url = reverse("personal:transcribe_report_voice")
        text_payload = json.dumps({
            "text": "نُفّذ نشاط مهني للطالبات، وسُجلت نتائجه وملاحظات التحسين."
        }, ensure_ascii=False)
        with patch("personal.assistant_views.improve_report_text_with_ai", side_effect=
                   ReportAIUnavailable("تعذر الاتصال بالخدمة")):
            failed_text = self.client.post(text_url, data=text_payload, content_type="application/json")
        self.assertEqual(failed_text.status_code, 503)
        self.assertEqual(daily_remaining("improvement", self.owner.pk, 1), 1)

        with patch("personal.assistant_views.improve_report_text_with_ai", return_value="نص محسّن") as improve:
            success_text = self.client.post(text_url, data=text_payload, content_type="application/json")
            exhausted_text = self.client.post(text_url, data=text_payload, content_type="application/json")
        self.assertEqual(success_text.status_code, 200)
        self.assertEqual(exhausted_text.status_code, 429)
        self.assertEqual(improve.call_count, 1)
        self.assertEqual(daily_remaining("improvement", self.owner.pk, 1), 0)

        with patch("personal.assistant_views.transcribe_audio", side_effect=
                   VoiceReportUnavailable("تعذر الاتصال بالخدمة")):
            failed_voice = self.client.post(voice_url, {"audio": self.voice_upload()})
        self.assertEqual(failed_voice.status_code, 503)
        self.assertEqual(daily_remaining("voice", self.owner.pk, 1), 1)

        with patch("personal.assistant_views.transcribe_audio", return_value="نص مسموع") as transcribe, patch(
            "personal.assistant_views.polish_dictation", return_value="نص مفرغ مصحح"
        ):
            success_voice = self.client.post(voice_url, {"audio": self.voice_upload()})
            exhausted_voice = self.client.post(voice_url, {"audio": self.voice_upload()})
        self.assertEqual(success_voice.status_code, 200)
        self.assertEqual(exhausted_voice.status_code, 429)
        self.assertEqual(transcribe.call_count, 1)
        self.assertEqual(daily_remaining("voice", self.owner.pk, 1), 0)
        self.assertEqual(report_ai_daily_remaining(self.owner.pk), 3)
        self.assertEqual(voice_report_daily_remaining(self.owner.pk), 3)

    def test_archived_year_and_expired_subscription_block_new_editor_post(self):
        PersonalAcademicYear.objects.create(
            workspace=self.workspace, value=self.YEAR, archived_at=timezone.now(),
        )
        archived = self.client.post(
            reverse("personal:report_create"), self.editor_payload(),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(archived.status_code, 422)
        self.assertEqual(PersonalReport.objects.count(), 0)

        self.subscription.is_active = False
        self.subscription.save(update_fields=["is_active"])
        inactive = self.client.post(reverse("personal:report_create"), self.editor_payload())
        self.assertRedirects(inactive, reverse("personal:dashboard"))
        self.assertEqual(PersonalReport.objects.count(), 0)
