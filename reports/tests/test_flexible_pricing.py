from decimal import Decimal

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from reports.flexible_pricing import (
    build_flexible_pricing_catalog,
    normalize_teacher_capacity,
    quote_for_selection,
)
from reports.models import (
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
from reports.views.billing_core import _apply_payment_effects, _archive_pricing


def _receipt():
    return valid_receipt()


class FlexiblePricingTests(TestCase):
    def setUp(self):
        cache.clear()
        self.annual_25 = SubscriptionPlan.objects.create(
            name="سنوية 25",
            price=Decimal("1290"),
            days_duration=365,
            max_teachers=25,
        )
        self.annual_50 = SubscriptionPlan.objects.create(
            name="سنوية 50",
            price=Decimal("1990"),
            days_duration=365,
            max_teachers=50,
        )
        self.annual_100 = SubscriptionPlan.objects.create(
            name="سنوية 100",
            price=Decimal("2990"),
            days_duration=365,
            max_teachers=100,
        )

    def test_27_teachers_are_quoted_as_30_not_50(self):
        self.assertEqual(normalize_teacher_capacity(27), 30)

        quote = quote_for_selection(self.annual_25.id, 27)

        self.assertEqual(quote["capacity"], 30)
        self.assertEqual(quote["price"], Decimal("1430"))
        self.assertEqual(quote["plan"], self.annual_25)

    def test_catalog_keeps_anchor_prices_and_adds_small_steps(self):
        annual = next(
            period
            for period in build_flexible_pricing_catalog()
            if period["key"] == "1y"
        )
        prices = {quote["capacity"]: quote["price"] for quote in annual["quotes"]}

        self.assertEqual(prices[25], Decimal("1290"))
        self.assertEqual(prices[30], Decimal("1430"))
        self.assertEqual(prices[50], Decimal("1990"))
        self.assertEqual(prices[100], Decimal("2990"))

    def test_subscription_uses_the_effective_capacity_override(self):
        school = School.objects.create(name="مدرسة مرنة", code="flex-school")
        subscription = SchoolSubscription.objects.create(
            school=school,
            plan=self.annual_25,
            teacher_limit_override=30,
        )

        self.assertEqual(subscription.teacher_limit, 30)

    def test_payment_recalculates_price_server_side_and_applies_capacity(self):
        school = School.objects.create(name="مدرسة دفع مرنة", code="flex-payment")
        subscription = SchoolSubscription.objects.create(
            school=school,
            plan=self.annual_25,
        )
        manager = Teacher.objects.create_user(
            phone="500000027",
            name="مدير المدرسة",
            password="pass",
        )
        SchoolMembership.objects.create(
            school=school,
            teacher=manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )
        self.client.force_login(manager)
        session = self.client.session
        session["active_school_id"] = school.id
        session.save()
        submission_key = checkout_submission_key(self.client)

        response = self.client.post(
            reverse("reports:payment_create"),
            {
                "unified": "1",
                "include_subscription": "1",
                "plan_id": str(self.annual_25.id),
                "teacher_capacity": "27",
                "checkout_submission_key": submission_key,
                "receipt_image": _receipt(),
            },
            REMOTE_ADDR="127.0.0.27",
        )

        self.assertEqual(response.status_code, 302)
        payment = Payment.objects.get(school=school)
        self.assertEqual(payment.amount, Decimal("1430"))
        self.assertEqual(payment.requested_teacher_limit, 30)

        _apply_payment_effects(payment, timezone.localdate(), _archive_pricing())
        subscription.refresh_from_db()
        self.assertEqual(subscription.teacher_limit, 30)
