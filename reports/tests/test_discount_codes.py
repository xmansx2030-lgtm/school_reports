# -*- coding: utf-8 -*-
"""أكواد الخصم: الحساب، حدود الاستخدام، الحجز والتحرير، والتفعيل المجاني.

القواعد محل الاختبار:
- الخصم يُحسب في الخادم ويسري على بند الاشتراك وحده.
- عدد استخدامات كلي (max_uses): المستخدم بعد النفاد يرى «انتهت جميع الاستخدامات».
- كل مدرسة تستخدم الكود مرة واحدة (قيد فريد في قاعدة البيانات).
- الحجز عند إنشاء الطلب، والتحرير عند رفضه أو إلغائه قبل تطبيق الأثر.
- خصم 100% يفعّل الاشتراك فوراً بلا إيصال وبدفعة معتمدة بمبلغ صفر.
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.messages import get_messages
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from reports.billing_invoices import build_invoice_context
from reports.discount_codes import (
    DiscountCodeError,
    MSG_ALREADY_USED,
    MSG_EXHAUSTED,
    MSG_EXPIRED,
    MSG_INVALID,
    find_usable_code,
    release_dead_redemptions,
)
from reports.models import (
    DiscountCode,
    DiscountRedemption,
    Payment,
    School,
    SchoolMembership,
    SchoolSubscription,
    SubscriptionPlan,
    Teacher,
)
from reports.tests.billing_test_helpers import (
    checkout_submission_key,
    valid_receipt,
)


def _receipt():
    return valid_receipt()


@override_settings(ALLOWED_HOSTS=["testserver"], RATELIMIT_ENABLE=False)
class DiscountCodeBaseTests(TestCase):
    def setUp(self):
        self.plan = SubscriptionPlan.objects.create(
            name="باقة سنوية", price=1200, days_duration=365, max_teachers=0
        )
        self.school = School.objects.create(name="مدرسة الاختبار", code="coupon-school")
        self.subscription = SchoolSubscription.objects.create(
            school=self.school, plan=self.plan
        )
        self.manager = Teacher.objects.create_user(
            phone="500111333", name="مدير", password="pass"
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )
        self.client.force_login(self.manager)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()
        self.submission_key = checkout_submission_key(self.client)

    def _make_code(self, **overrides):
        defaults = {
            "code": "SAVE10",
            "discount_type": DiscountCode.DiscountType.PERCENT,
            "value": Decimal("10"),
            "max_uses": 5,
            "is_active": True,
        }
        defaults.update(overrides)
        return DiscountCode.objects.create(**defaults)

    def _post_subscription(self, data=None, with_receipt=True):
        payload = {
            "unified": "1",
            "include_subscription": "1",
            "plan_id": str(self.plan.id),
            "checkout_submission_key": self.submission_key,
        }
        payload.update(data or {})
        if with_receipt:
            payload.setdefault("receipt_image", _receipt())
        return self.client.post(reverse("reports:payment_create"), payload, follow=True)

    def _messages(self, response):
        return " ".join(str(m) for m in get_messages(response.wsgi_request))


class DiscountComputationTests(DiscountCodeBaseTests):
    def test_percent_discount_rounds_half_up(self):
        code = self._make_code(value=Decimal("12.5"))
        self.assertEqual(code.discount_for(Decimal("999.99")), Decimal("125.00"))

    def test_fixed_discount_is_capped_at_amount(self):
        code = self._make_code(
            discount_type=DiscountCode.DiscountType.FIXED, value=Decimal("5000")
        )
        self.assertEqual(code.discount_for(Decimal("1200")), Decimal("1200.00"))

    def test_find_usable_code_normalizes_input(self):
        code = self._make_code()
        self.assertEqual(find_usable_code("  save10 ", self.school).pk, code.pk)

    def test_unknown_and_inactive_codes_are_invalid(self):
        self._make_code(is_active=False)
        for raw in ("SAVE10", "NOPE"):
            with self.assertRaises(DiscountCodeError) as ctx:
                find_usable_code(raw, self.school)
            self.assertEqual(str(ctx.exception), MSG_INVALID)

    def test_expired_code_message(self):
        from datetime import timedelta

        from django.utils import timezone

        self._make_code(valid_until=timezone.localdate() - timedelta(days=1))
        with self.assertRaises(DiscountCodeError) as ctx:
            find_usable_code("SAVE10", self.school)
        self.assertEqual(str(ctx.exception), MSG_EXPIRED)

    def test_exhausted_code_message_for_next_school(self):
        """أصدرتَ كوداً بعدد محدد؛ من يأتي بعد نفاده يرى «انتهت جميع الاستخدامات»."""
        code = self._make_code(max_uses=1)
        other = School.objects.create(name="مدرسة أخرى", code="other-school")
        DiscountRedemption.objects.create(code=code, school=other)
        with self.assertRaises(DiscountCodeError) as ctx:
            find_usable_code("SAVE10", self.school)
        self.assertEqual(str(ctx.exception), MSG_EXHAUSTED)

    def test_school_cannot_reuse_code(self):
        code = self._make_code()
        DiscountRedemption.objects.create(code=code, school=self.school)
        with self.assertRaises(DiscountCodeError) as ctx:
            find_usable_code("SAVE10", self.school)
        self.assertEqual(str(ctx.exception), MSG_ALREADY_USED)


class DiscountBankFlowTests(DiscountCodeBaseTests):
    def test_discount_applied_to_subscription_and_reserved(self):
        code = self._make_code()
        resp = self._post_subscription({"discount_code": "save10"})
        self.assertEqual(resp.status_code, 200)

        payment = Payment.objects.get(school=self.school)
        self.assertEqual(payment.amount, Decimal("1080.00"))
        self.assertEqual(payment.discount_amount, Decimal("120.00"))
        self.assertEqual(payment.discount_code_id, code.pk)
        self.assertEqual(payment.original_amount, Decimal("1200.00"))
        self.assertEqual(payment.status, Payment.Status.PENDING)

        redemption = DiscountRedemption.objects.get(code=code, school=self.school)
        self.assertEqual(redemption.payment_id, payment.pk)
        self.assertEqual(redemption.amount_discounted, Decimal("120.00"))

    def test_discount_applies_to_subscription_item_only_in_unified_order(self):
        self._make_code(
            discount_type=DiscountCode.DiscountType.FIXED, value=Decimal("200")
        )
        resp = self._post_subscription(
            {"discount_code": "SAVE10", "include_archive_addon": "1"}
        )
        self.assertEqual(resp.status_code, 200)

        payments = {
            p.purpose: p for p in Payment.objects.filter(school=self.school)
        }
        self.assertEqual(
            payments[Payment.Purpose.SUBSCRIPTION].amount, Decimal("1000.00")
        )
        self.assertEqual(
            payments[Payment.Purpose.SUBSCRIPTION].discount_amount, Decimal("200.00")
        )
        self.assertEqual(payments[Payment.Purpose.ARCHIVE_ADDON].discount_amount, 0)
        self.assertIsNone(payments[Payment.Purpose.ARCHIVE_ADDON].discount_code_id)

    def test_code_without_subscription_item_is_rejected(self):
        from reports.models import ArchiveStorageOption

        option = ArchiveStorageOption.objects.create(
            storage_gb=50, price=99, is_active=True
        )
        self._make_code()
        resp = self.client.post(
            reverse("reports:payment_create"),
            {
                "unified": "1",
                "include_archive_storage": "1",
                "archive_storage_option_id": str(option.id),
                "discount_code": "SAVE10",
                "checkout_submission_key": self.submission_key,
                "receipt_image": _receipt(),
            },
            follow=True,
        )
        self.assertIn("بند الاشتراك فقط", self._messages(resp))
        self.assertEqual(Payment.objects.filter(school=self.school).count(), 0)

    def test_exhausted_code_creates_no_payment(self):
        code = self._make_code(max_uses=1)
        other = School.objects.create(name="مدرسة أخرى", code="exh-school")
        DiscountRedemption.objects.create(code=code, school=other)

        resp = self._post_subscription({"discount_code": "SAVE10"})
        self.assertIn(MSG_EXHAUSTED, self._messages(resp))
        self.assertEqual(Payment.objects.filter(school=self.school).count(), 0)

    def test_rejecting_payment_releases_the_reservation(self):
        code = self._make_code()
        self._post_subscription({"discount_code": "SAVE10"})
        payment = Payment.objects.get(school=self.school)
        self.assertEqual(code.redemptions.count(), 1)

        payment.status = Payment.Status.REJECTED
        payment.save(update_fields=["status"])
        released = release_dead_redemptions(payment_id=payment.pk)

        self.assertEqual(released, 1)
        self.assertEqual(code.redemptions.count(), 0)
        # المدرسة تستطيع استخدام الكود من جديد بعد التحرير.
        find_usable_code("SAVE10", self.school)

    def test_release_skips_payments_whose_effects_applied(self):
        from django.utils import timezone

        code = self._make_code()
        self._post_subscription({"discount_code": "SAVE10"})
        payment = Payment.objects.get(school=self.school)
        payment.status = Payment.Status.REJECTED
        payment.effects_applied_at = timezone.now()
        payment.save(update_fields=["status", "effects_applied_at"])

        self.assertEqual(release_dead_redemptions(payment_id=payment.pk), 0)
        self.assertEqual(code.redemptions.count(), 1)


class FullDiscountActivationTests(DiscountCodeBaseTests):
    def test_hundred_percent_code_activates_without_receipt(self):
        code = self._make_code(code="FREE100", value=Decimal("100"))
        resp = self._post_subscription(
            {"discount_code": "FREE100"}, with_receipt=False
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("تم تفعيل الاشتراك مجاناً", self._messages(resp))

        payment = Payment.objects.get(school=self.school)
        self.assertEqual(payment.status, Payment.Status.APPROVED)
        self.assertEqual(payment.amount, Decimal("0.00"))
        self.assertEqual(payment.discount_amount, Decimal("1200.00"))
        self.assertIsNotNone(payment.effects_applied_at)
        self.assertEqual(code.redemptions.filter(school=self.school).count(), 1)

        self.subscription.refresh_from_db()
        self.assertFalse(self.subscription.is_expired)

    def test_free_activation_invoice_shows_discount(self):
        self._make_code(code="FREE100", value=Decimal("100"))
        self._post_subscription({"discount_code": "FREE100"}, with_receipt=False)
        payment = Payment.objects.get(school=self.school)

        context = build_invoice_context(payment)
        self.assertEqual(context["subtotal"], Decimal("1200.00"))
        self.assertEqual(context["discount_total"], Decimal("1200.00"))
        self.assertEqual(context["total"], Decimal("0.00"))
        self.assertIn("FREE100", context["discount_codes_label"])


class DiscountCheckEndpointTests(DiscountCodeBaseTests):
    def test_valid_code_returns_server_computed_amounts(self):
        self._make_code()
        resp = self.client.post(
            reverse("reports:discount_code_check"),
            {"discount_code": "save10", "plan_id": str(self.plan.id)},
        )
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["code"], "SAVE10")
        self.assertEqual(data["discount_amount"], "120.00")
        self.assertEqual(data["amount_after"], "1080.00")

    def test_invalid_code_returns_message(self):
        resp = self.client.post(
            reverse("reports:discount_code_check"), {"discount_code": "NOPE"}
        )
        data = resp.json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["message"], MSG_INVALID)

    def test_non_manager_is_refused(self):
        outsider = Teacher.objects.create_user(
            phone="500999888", name="خارجي", password="pass"
        )
        self.client.force_login(outsider)
        resp = self.client.post(
            reverse("reports:discount_code_check"), {"discount_code": "SAVE10"}
        )
        self.assertEqual(resp.status_code, 403)

    def test_expired_manager_can_check_code_while_renewing(self):
        """SubscriptionMiddleware must not block the renewal helper endpoint."""
        self._make_code()
        self.subscription.end_date = timezone.localdate() - timedelta(days=1)
        self.subscription.save(update_fields=["end_date"])

        resp = self.client.post(
            reverse("reports:discount_code_check"),
            {"discount_code": "SAVE10", "plan_id": str(self.plan.id)},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(resp.status_code, 200, resp.headers)
        self.assertTrue(resp.json()["ok"])

    def test_manager_with_temporary_password_can_check_code_while_renewing(self):
        """ForcePasswordChangeMiddleware must allow the whole payment journey."""
        self._make_code()
        self.manager.set_password(self.manager.phone)
        self.manager.save(update_fields=["password"])
        # Changing a logged-in user's password invalidates Django's existing
        # session hash; log the fixture in again to exercise the middleware,
        # not the login-required redirect.
        self.client.force_login(self.manager)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()

        resp = self.client.post(
            reverse("reports:discount_code_check"),
            {"discount_code": "SAVE10", "plan_id": str(self.plan.id)},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(resp.status_code, 200, resp.headers)
        self.assertTrue(resp.json()["ok"])


@override_settings(ALLOWED_HOSTS=["testserver"], RATELIMIT_ENABLE=False)
class PlatformDiscountCodeViewsTests(TestCase):
    def setUp(self):
        self.admin = Teacher.objects.create_superuser(
            phone="500777666", name="مالك المنصة", password="pass"
        )

    def test_superuser_can_create_code(self):
        self.client.force_login(self.admin)
        resp = self.client.post(
            reverse("reports:platform_discount_code_add"),
            {
                "code": "welcome-25",
                "discount_type": DiscountCode.DiscountType.PERCENT,
                "value": "25",
                "max_uses": "20",
                "is_active": "on",
            },
        )
        self.assertEqual(resp.status_code, 302)
        code = DiscountCode.objects.get()
        self.assertEqual(code.code, "WELCOME-25")
        self.assertEqual(code.created_by_id, self.admin.pk)

    def test_used_code_cannot_be_deleted(self):
        self.client.force_login(self.admin)
        code = DiscountCode.objects.create(
            code="KEEP", discount_type="percent", value=10, max_uses=5
        )
        school = School.objects.create(name="مدرسة", code="del-school")
        DiscountRedemption.objects.create(code=code, school=school)

        self.client.post(
            reverse("reports:platform_discount_code_delete", args=[code.pk])
        )
        self.assertTrue(DiscountCode.objects.filter(pk=code.pk).exists())

    def test_non_superuser_is_redirected(self):
        outsider = Teacher.objects.create_user(
            phone="500555444", name="مستخدم", password="pass"
        )
        self.client.force_login(outsider)
        resp = self.client.get(reverse("reports:platform_discount_codes_list"))
        self.assertEqual(resp.status_code, 302)
