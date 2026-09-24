from __future__ import annotations

from decimal import Decimal
from io import BytesIO
from pathlib import Path
import tempfile
from unittest.mock import patch

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from PIL import Image, features

from reports.models import (
    Payment,
    School,
    SchoolMembership,
    SchoolSubscription,
    SubscriptionPlan,
    Teacher,
)
from reports.validators import MAX_IMAGE_BYTES


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _receipt(name: str = "receipt.png") -> SimpleUploadedFile:
    stream = BytesIO()
    Image.new("RGB", (2, 2), color=(18, 94, 62)).save(stream, format="PNG")
    return SimpleUploadedFile(name, stream.getvalue(), content_type="image/png")


def _generated_receipt(fmt: str, name: str, content_type: str) -> SimpleUploadedFile:
    stream = BytesIO()
    Image.new("RGB", (2, 2), color=(18, 94, 62)).save(stream, format=fmt)
    return SimpleUploadedFile(name, stream.getvalue(), content_type=content_type)


class SchoolBillingV12FrontendDebtTests(SimpleTestCase):
    def test_core_billing_templates_use_owned_external_assets(self):
        template_root = PROJECT_ROOT / "reports/templates/reports"
        core_templates = (
            "my_subscription.html",
            "subscription_history.html",
            "subscription_expired.html",
        )
        for filename in core_templates:
            source = (template_root / filename).read_text(encoding="utf-8")
            with self.subTest(filename=filename):
                self.assertNotIn("<style", source)
                self.assertNotRegex(source, r"\sstyle\s*=")
                self.assertNotRegex(source, r"<script\b(?![^>]*\bsrc=)")
                self.assertIn("css/subscription.css", source)

        source = (template_root / "my_subscription.html").read_text(encoding="utf-8")
        self.assertIn("js/subscription.js", source)
        self.assertIn("js/subscription-checkout.js", source)

    def test_subscription_assets_use_tokens_logical_properties_and_no_style_writes(self):
        css = (PROJECT_ROOT / "static/css/subscription.css").read_text(encoding="utf-8")
        javascript = (PROJECT_ROOT / "static/js/subscription.js").read_text(encoding="utf-8")

        self.assertNotRegex(css, r"#[0-9a-fA-F]{3,8}\b")
        self.assertNotRegex(css, r"\b(?:rgb|rgba|hsl|hsla)\(")
        self.assertNotRegex(css, r"(?:margin|padding|border)-(?:left|right)\b")
        self.assertNotRegex(css, r"\b(?:left|right)\s*:")
        self.assertNotRegex(javascript, r"\.style(?:\.|\[)")
        self.assertNotIn('setAttribute("style"', javascript)
        self.assertNotIn("{%", javascript)
        self.assertNotIn("{{", javascript)


@override_settings(ALLOWED_HOSTS=["testserver"], RATELIMIT_ENABLE=False)
class SchoolBillingV12SecurityTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._media_dir = tempfile.TemporaryDirectory()
        cls._media_override = override_settings(MEDIA_ROOT=cls._media_dir.name)
        cls._media_override.enable()

    @classmethod
    def tearDownClass(cls):
        cls._media_override.disable()
        cls._media_dir.cleanup()
        super().tearDownClass()

    def setUp(self):
        self.plan = SubscriptionPlan.objects.create(
            name="الباقة السنوية",
            price=Decimal("1290.00"),
            days_duration=365,
            max_teachers=25,
        )
        self.school_a = School.objects.create(name="مدرسة ألف", code="billing-a")
        self.school_b = School.objects.create(name="مدرسة باء", code="billing-b")
        self.subscription_a = SchoolSubscription.objects.create(
            school=self.school_a,
            plan=self.plan,
        )
        self.subscription_b = SchoolSubscription.objects.create(
            school=self.school_b,
            plan=self.plan,
        )
        self.manager = Teacher.objects.create_user(
            phone="0558012200",
            name="مدير متعدد المدارس",
            password="Billing#2026",
        )
        for school in (self.school_a, self.school_b):
            SchoolMembership.objects.create(
                school=school,
                teacher=self.manager,
                role_type=SchoolMembership.RoleType.MANAGER,
            )
        self.client.force_login(self.manager)
        session = self.client.session
        session["active_school_id"] = self.school_a.pk
        session.save()
        self.submission_key = self._new_submission_key()

    def _new_submission_key(self, **query):
        response = self.client.get(reverse("reports:my_subscription"), query)
        self.assertEqual(response.status_code, 200)
        return response.context["checkout_submission_key"]

    def _subscription_order(self, **overrides):
        payload = {
            "unified": "1",
            "include_subscription": "1",
            "plan_id": str(self.plan.pk),
            "teacher_capacity": "25",
            "receipt_image": _receipt(),
            "checkout_submission_key": self.submission_key,
        }
        payload.update(overrides)
        return self.client.post(reverse("reports:payment_create"), payload)

    def test_price_currency_and_status_fields_are_not_browser_authority(self):
        self._subscription_order(
            amount="1.00",
            price="1.00",
            total="1.00",
            currency="USD",
            status=Payment.Status.APPROVED,
            is_active="0",
        )

        payment = Payment.objects.get(school=self.school_a)
        self.assertEqual(payment.amount, Decimal("1290.00"))
        self.assertEqual(payment.status, Payment.Status.PENDING)
        self.assertEqual(payment.payment_method, Payment.Method.BANK_TRANSFER)

    def test_invoice_stays_bound_to_the_active_school(self):
        payment = Payment.objects.create(
            school=self.school_b,
            subscription=self.subscription_b,
            requested_plan=self.plan,
            amount=self.plan.price,
            status=Payment.Status.APPROVED,
            created_by=self.manager,
        )

        for route_name in (
            "reports:subscription_invoice",
            "reports:subscription_invoice_pdf",
        ):
            with self.subTest(route_name=route_name):
                response = self.client.get(reverse(route_name, args=[payment.pk]))
                self.assertEqual(response.status_code, 404)

    def test_payment_creation_requires_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.manager)
        session = client.session
        session["active_school_id"] = self.school_a.pk
        session.save()

        response = client.post(
            reverse("reports:payment_create"),
            {
                "unified": "1",
                "include_subscription": "1",
                "plan_id": str(self.plan.pk),
                "teacher_capacity": "25",
                "receipt_image": _receipt(),
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(Payment.objects.exists())

    def test_manager_cannot_override_active_school_with_on_behalf_parameter(self):
        """BILLING-ACTIVE-SCHOOL-1: manager membership must not bypass active school."""
        page = self.client.get(
            reverse("reports:my_subscription"),
            {"school": self.school_b.pk},
        )
        self._subscription_order(on_behalf_school=str(self.school_b.pk))

        self.assertNotEqual(page.status_code, 200)
        self.assertFalse(Payment.objects.filter(school=self.school_b).exists())

    def test_duplicate_subscription_submission_is_idempotent(self):
        """BILLING-DUPLICATE-ORDER-1: a replay must not create a second pending order."""
        self._subscription_order()
        self._subscription_order(receipt_image=_receipt("receipt-two.png"))

        self.assertEqual(
            Payment.objects.filter(
                school=self.school_a,
                purpose=Payment.Purpose.SUBSCRIPTION,
                status=Payment.Status.PENDING,
            ).count(),
            1,
        )

    def test_invalid_receipt_is_rejected_before_payment_persists(self):
        """BILLING-RECEIPT-VALIDATION-1: model validators are not implicit on save."""
        invalid = SimpleUploadedFile(
            "renamed.jpg",
            b"MZ-not-an-image",
            content_type="image/jpeg",
        )
        storage = Payment._meta.get_field("receipt_image").storage
        with patch.object(storage, "save", wraps=storage.save) as storage_save:
            self._subscription_order(receipt_image=invalid)

        self.assertFalse(Payment.objects.filter(school=self.school_a).exists())
        storage_save.assert_not_called()

    def test_history_and_addon_cannot_override_active_school(self):
        history = self.client.get(
            reverse("reports:subscription_history"),
            {"school": self.school_b.pk},
        )
        addon = self.client.post(
            reverse("reports:payment_create"),
            {
                "unified": "1",
                "include_archive_addon": "1",
                "on_behalf_school": str(self.school_b.pk),
                "checkout_submission_key": self.submission_key,
                "receipt_image": _receipt("addon.png"),
            },
        )

        self.assertNotEqual(history.status_code, 200)
        self.assertEqual(addon.status_code, 302)
        self.assertFalse(Payment.objects.filter(school=self.school_b).exists())

    def test_different_submission_key_allows_independent_same_payload(self):
        self._subscription_order()
        self.submission_key = self._new_submission_key()
        self._subscription_order(receipt_image=_receipt("second-valid.png"))

        self.assertEqual(
            Payment.objects.filter(
                school=self.school_a,
                purpose=Payment.Purpose.SUBSCRIPTION,
            ).count(),
            2,
        )

    def test_same_submission_key_rejects_changed_payload(self):
        other_plan = SubscriptionPlan.objects.create(
            name="باقة مختلفة",
            price=Decimal("700.00"),
            days_duration=180,
            max_teachers=25,
        )
        self._subscription_order()
        self._subscription_order(plan_id=str(other_plan.pk))

        self.assertEqual(Payment.objects.filter(school=self.school_a).count(), 1)

    def test_storage_file_is_removed_when_later_batch_save_fails(self):
        original_save = Payment.save
        save_calls = 0
        existing_files = {
            path.relative_to(self._media_dir.name)
            for path in Path(self._media_dir.name).rglob("*")
            if path.is_file()
        }

        def fail_second_save(instance, *args, **kwargs):
            nonlocal save_calls
            save_calls += 1
            if save_calls == 2:
                raise OSError("simulated payment persistence failure")
            return original_save(instance, *args, **kwargs)

        with patch.object(Payment, "save", autospec=True, side_effect=fail_second_save):
            with self.assertRaises(OSError):
                self._subscription_order(include_archive_addon="1")

        self.assertFalse(Payment.objects.filter(school=self.school_a).exists())
        remaining_files = {
            path.relative_to(self._media_dir.name)
            for path in Path(self._media_dir.name).rglob("*")
            if path.is_file()
        }
        self.assertEqual(remaining_files, existing_files)

    def test_invalid_binary_oversized_and_mime_mismatch_are_rejected(self):
        invalid_files = (
            SimpleUploadedFile(
                "invalid.png", b"not-an-image", content_type="image/png"
            ),
            SimpleUploadedFile(
                "oversized.png",
                b"\x89PNG\r\n\x1a\n" + b"0" * MAX_IMAGE_BYTES,
                content_type="image/png",
            ),
            _generated_receipt("PNG", "mismatch.jpg", "image/jpeg"),
        )
        for invalid in invalid_files:
            with self.subTest(filename=invalid.name):
                self.submission_key = self._new_submission_key()
                self._subscription_order(receipt_image=invalid)
                self.assertFalse(Payment.objects.filter(school=self.school_a).exists())

    def test_valid_jpeg_png_and_webp_receipts_are_accepted(self):
        formats = [
            ("JPEG", "receipt.jpg", "image/jpeg"),
            ("PNG", "receipt.png", "image/png"),
        ]
        if features.check("webp"):
            formats.append(("WEBP", "receipt.webp", "image/webp"))

        for index, (fmt, name, content_type) in enumerate(formats, start=1):
            with self.subTest(fmt=fmt):
                self.submission_key = self._new_submission_key()
                self._subscription_order(
                    receipt_image=_generated_receipt(fmt, name, content_type)
                )
                self.assertEqual(
                    Payment.objects.filter(school=self.school_a).count(), index
                )


@override_settings(ALLOWED_HOSTS=["testserver"], RATELIMIT_ENABLE=False)
class SchoolBillingV12PerformanceTests(TestCase):
    def setUp(self):
        self.plan = SubscriptionPlan.objects.create(
            name="باقة قياس الأداء",
            price=Decimal("1290.00"),
            days_duration=365,
            max_teachers=25,
        )
        self.school = School.objects.create(name="مدرسة قياس المالية", code="billing-perf")
        self.subscription = SchoolSubscription.objects.create(
            school=self.school,
            plan=self.plan,
        )
        self.manager = Teacher.objects.create_user(
            phone="0558012299",
            name="مدير قياس المالية",
            password="Billing#2026",
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )
        for index in range(8):
            Payment.objects.create(
                school=self.school,
                subscription=self.subscription,
                requested_plan=self.plan,
                amount=self.plan.price,
                status=(
                    Payment.Status.APPROVED
                    if index == 0
                    else Payment.Status.PENDING
                ),
                batch_ref=f"billing-perf-{index}" if index == 0 else "",
                created_by=self.manager,
            )
        self.invoice_payment = Payment.objects.get(batch_ref="billing-perf-0")
        self.client.force_login(self.manager)
        session = self.client.session
        session["active_school_id"] = self.school.pk
        session.save()
        response = self.client.get(reverse("reports:my_subscription"))
        self.submission_key = response.context["checkout_submission_key"]

    def _count(self, method: str, url: str, data=None) -> int:
        cache.clear()
        with CaptureQueriesContext(connection) as captured:
            response = getattr(self.client, method)(url, data or {})
            self.assertLess(response.status_code, 400)
        return len(captured)

    def test_school_billing_query_profile(self):
        subscription = self._count("get", reverse("reports:my_subscription"))
        history = self._count("get", reverse("reports:subscription_history"))
        invoice = self._count(
            "get",
            reverse("reports:subscription_invoice", args=[self.invoice_payment.pk]),
        )
        checkout_post = self._count(
            "post",
            reverse("reports:payment_create"),
            {
                "unified": "1",
                "include_subscription": "1",
                "plan_id": str(self.plan.pk),
                "teacher_capacity": "25",
                "checkout_submission_key": self.submission_key,
                "receipt_image": _receipt("performance.png"),
            },
        )
        print(
            "BILLING_QUERY_COUNTS "
            f"subscription={subscription} history={history} "
            f"invoice={invoice} checkout_post={checkout_post}"
        )
