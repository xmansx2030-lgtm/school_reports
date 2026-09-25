"""A deleted report or witness must not refill a subscription's creation quota."""

from datetime import date, timedelta
from importlib import import_module
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from django.apps import apps
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from reports.models import Teacher

from .billing import apply_paid_personal_invoice
from .models import (
    PersonalAcademicYear, PersonalEvidence, PersonalPayment, PersonalPlan,
    PersonalReport, PersonalWorkspace,
)
from .quotas import personal_quota_usage
from .services import ensure_personal_subscription


@override_settings(ALLOWED_HOSTS=["testserver"], RATELIMIT_ENABLE=False)
class PersonalCreationQuotaTests(TestCase):
    YEAR = "1447-1448"

    def setUp(self):
        self.owner = Teacher.objects.create_user(
            phone="0557990188", name="معلم مستقل", password="Personal#2026",  # noqa: S106
        )
        self.workspace = PersonalWorkspace.objects.create(
            owner=self.owner, current_academic_year=self.YEAR,
        )
        self.subscription = ensure_personal_subscription(self.workspace)
        self.plan = self.subscription.plan
        self.plan.max_reports = 1
        self.plan.max_evidence = 1
        self.plan.save(update_fields=["max_reports", "max_evidence"])
        PersonalAcademicYear.objects.create(workspace=self.workspace, value=self.YEAR)
        self.client.force_login(self.owner)

    def report_payload(self, **changes):
        payload = {
            "title": "تقرير شخصي", "category": "نشاط", "report_date": "2026-09-24",
            "academic_year": self.YEAR, "description": "عمل موثق",
            "show_details": "on", "selection_enabled": "on", "status": "complete",
        }
        payload.update(changes)
        return payload

    def test_report_in_trash_still_consumes_creation_limit(self):
        created = self.client.post(reverse("personal:report_create"), self.report_payload())
        self.assertEqual(created.status_code, 302)
        report = PersonalReport.objects.get(workspace=self.workspace)
        self.client.post(reverse("personal:report_delete", args=[report.pk]))
        self.assertIsNotNone(PersonalReport.objects.get(pk=report.pk).trashed_at)
        self.assertRedirects(self.client.get(reverse("personal:report_create")), reverse("personal:reports"))
        self.assertEqual(self.client.get(reverse("personal:billing")).context["report_count"], 1)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.reports_created, 1)

    def test_hard_deleted_witness_still_consumes_creation_limit(self):
        created = self.client.post(reverse("personal:evidence_create"), {
            "title": "شاهد أول", "academic_year": self.YEAR,
            "source_url": "https://example.org/first",
        })
        self.assertEqual(created.status_code, 302)
        witness = PersonalEvidence.objects.get(workspace=self.workspace)
        self.assertEqual(self.client.post(reverse("personal:evidence_delete", args=[witness.pk])).status_code, 302)
        self.assertFalse(PersonalEvidence.objects.filter(pk=witness.pk).exists())
        self.assertRedirects(self.client.get(reverse("personal:evidence_create")), reverse("personal:evidence"))
        rejected = self.client.post(reverse("personal:portfolio"), {
            "year": self.YEAR, "action": "upload_evidence", "section_code": "1",
            "title": "شاهد بديل", "academic_year": self.YEAR,
            "source_url": "https://example.org/replacement",
        })
        self.assertEqual(rejected.status_code, 302)
        self.assertFalse(PersonalEvidence.objects.filter(title="شاهد بديل").exists())
        self.assertEqual(self.client.get(reverse("personal:billing")).context["evidence_count"], 1)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.evidence_created, 1)

    def test_legacy_witness_is_captured_before_deletion(self):
        witness = PersonalEvidence.objects.create(
            workspace=self.workspace, academic_year=self.YEAR,
            title="شاهد سابق", source_url="https://example.org/old",
        )
        self.assertEqual(self.subscription.evidence_created, 0)
        self.client.post(reverse("personal:evidence_delete", args=[witness.pk]))
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.evidence_created, 1)

    def test_migration_backfills_surviving_and_trashed_items(self):
        report = PersonalReport.objects.create(
            workspace=self.workspace, title="تقرير سابق", academic_year=self.YEAR,
            report_date=date(2026, 9, 24), teacher_name=self.owner.name,
        )
        report.move_to_trash(by=self.owner)
        PersonalEvidence.objects.create(
            workspace=self.workspace, academic_year=self.YEAR,
            title="شاهد سابق", source_url="https://example.org/legacy",
        )
        migration = import_module("personal.migrations.0018_personal_creation_quotas")
        migration.backfill_quota_usage(apps, SimpleNamespace(connection=connection))
        self.subscription.refresh_from_db()
        self.assertEqual((self.subscription.reports_created, self.subscription.evidence_created), (1, 1))

    def test_deletion_releases_storage_but_not_creation_count(self):
        self.plan.max_evidence = 2
        self.plan.storage_limit_mb = 1
        self.plan.save(update_fields=["max_evidence", "storage_limit_mb"])
        original_file = SimpleUploadedFile(
            "original.pdf", b"%PDF-1.4\n" + b"x" * (800 * 1024),
            content_type="application/pdf",
        )
        replacement_file = SimpleUploadedFile(
            "replacement.pdf", b"%PDF-1.4\n" + b"y" * (400 * 1024),
            content_type="application/pdf",
        )
        with TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            original = PersonalEvidence.objects.create(
                workspace=self.workspace, academic_year=self.YEAR,
                title="ملف سابق", file=original_file,
            )
            self.assertEqual(original.file_size, original_file.size)
            old_name = original.file.name
            with self.captureOnCommitCallbacks(execute=True):
                self.client.post(reverse("personal:evidence_delete", args=[original.pk]))
            self.assertFalse(original.file.storage.exists(old_name))
            created = self.client.post(reverse("personal:evidence_create"), {
                "title": "ملف جديد", "academic_year": self.YEAR,
                "file": replacement_file,
            })
            self.assertEqual(created.status_code, 302)
            self.assertEqual(PersonalEvidence.objects.get(title="ملف جديد").file_size, replacement_file.size)
            self.subscription.refresh_from_db()
            self.assertEqual(self.subscription.evidence_created, 2)
            self.assertEqual(self.client.get(reverse("personal:billing")).context["evidence_count"], 2)

    def test_dashboard_shows_used_and_remaining_after_deletion(self):
        self.plan.max_reports = 2
        self.plan.max_evidence = 2
        self.plan.storage_limit_mb = 1
        self.plan.save(update_fields=["max_reports", "max_evidence", "storage_limit_mb"])
        self.assertEqual(
            self.client.post(reverse("personal:report_create"), self.report_payload()).status_code, 302,
        )
        report = PersonalReport.objects.get(workspace=self.workspace)
        self.client.post(reverse("personal:report_delete", args=[report.pk]))
        with TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            uploaded = SimpleUploadedFile(
                "proof.pdf", b"%PDF-1.4\n" + b"x" * (256 * 1024),
                content_type="application/pdf",
            )
            witness = PersonalEvidence.objects.create(
                workspace=self.workspace, academic_year=self.YEAR,
                title="ملف", file=uploaded,
            )
            dashboard = self.client.get(reverse("personal:dashboard"))
            self.assertContains(dashboard, "استخدام الباقة والمتبقي")
            self.assertEqual(dashboard.context["quota"]["reports"], {
                "used": 1, "remaining": 1, "limit": 2,
            })
            self.assertEqual(dashboard.context["quota"]["evidence"], {
                "used": 1, "remaining": 1, "limit": 2,
            })
            self.assertEqual(dashboard.context["quota"]["storage"]["used_bytes"], uploaded.size)
            self.client.post(reverse("personal:evidence_delete", args=[witness.pk]))
            after_deletion = self.client.get(reverse("personal:dashboard"))
            self.assertEqual(after_deletion.context["quota"]["evidence"]["used"], 1)
            self.assertEqual(after_deletion.context["quota"]["storage"]["used_bytes"], 0)
            self.assertEqual(after_deletion.context["quota"]["storage"]["remaining_mb"], 1)

    def test_inline_witness_quota_rolls_back_report_and_counter_together(self):
        rejected = self.client.post(reverse("personal:report_create"), self.report_payload(**{
            "evidence-TOTAL_FORMS": "2", "evidence-INITIAL_FORMS": "0",
            "evidence-MIN_NUM_FORMS": "0", "evidence-MAX_NUM_FORMS": "8",
            "evidence-0-title": "شاهد أول", "evidence-0-source_url": "https://example.org/one",
            "evidence-1-title": "شاهد ثان", "evidence-1-source_url": "https://example.org/two",
        }))
        self.assertEqual(rejected.status_code, 200)
        self.assertFalse(PersonalReport.objects.filter(workspace=self.workspace).exists())
        self.assertFalse(PersonalEvidence.objects.filter(workspace=self.workspace).exists())
        self.assertEqual(personal_quota_usage(self.workspace)[1:], (0, 0))

    def _pay(self, plan, reference):
        payment = PersonalPayment.objects.create(
            workspace=self.workspace, plan=plan, plan_name=plan.name,
            duration_days=plan.duration_days, amount=plan.price,
            customer_name=self.owner.name, customer_email="teacher@example.org",
            gateway_invoice_id=reference,
        )
        apply_paid_personal_invoice(payment.pk, {
            "id": reference, "status": "paid", "currency": "SAR",
            "amount": int(plan.price * 100),
            "metadata": {
                "personal_payment_ref": str(payment.pk),
                "personal_workspace_id": str(self.workspace.pk),
            },
        })

    def test_paid_change_resets_quota_while_early_renewal_keeps_it(self):
        prior = PersonalReport.objects.create(
            workspace=self.workspace, title="سابق", academic_year=self.YEAR,
            report_date=date(2026, 9, 24), teacher_name=self.owner.name,
        )
        self.assertEqual(personal_quota_usage(self.workspace)[1], 1)
        paid = PersonalPlan.objects.create(
            code="quota_paid", name="باقة مدفوعة", price="49.00", duration_days=30,
            max_reports=1, max_evidence=1,
        )
        self._pay(paid, "quota-first")
        self.subscription.refresh_from_db()
        self.assertGreaterEqual(self.subscription.quota_report_after_id, prior.pk)
        self.assertEqual(self.subscription.reports_created, 0)
        self.assertEqual(personal_quota_usage(self.workspace)[1], 0)
        self.assertEqual(self.client.post(reverse("personal:report_create"), self.report_payload()).status_code, 302)
        self.assertEqual(personal_quota_usage(self.workspace)[1], 1)
        self._pay(paid, "quota-early-renewal")
        self.assertEqual(personal_quota_usage(self.workspace)[1], 1)
        self.subscription.refresh_from_db()
        self.subscription.end_date = timezone.localdate() - timedelta(days=1)
        self.subscription.save(update_fields=["end_date"])
        self._pay(paid, "quota-after-expiry")
        self.assertEqual(personal_quota_usage(self.workspace)[1], 0)
