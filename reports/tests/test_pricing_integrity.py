"""Commercial correctness of the capacity-based pricing model.

Customers buy a teacher capacity, and the price between the 25/50/100 anchors is
interpolated. That only holds together if the price curve rises smoothly and no
entitlement steps at an anchor — otherwise there is a band where buying MORE
capacity is cheaper in real terms.
"""
from __future__ import annotations

from decimal import Decimal

from itertools import pairwise

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from reports.flexible_pricing import (
    PERIODS,
    build_flexible_pricing_catalog,
    normalize_teacher_capacity,
    period_key_for_days,
)
from reports.models import School, SchoolMembership, SchoolSubscription, SubscriptionPlan, Teacher
from reports.pricing import (
    DEFAULT_ARCHIVE_PRICING,
    DEFAULT_SUBSCRIPTION_PLANS,
    SUBSCRIPTION_INCLUDED_FEATURES,
)


def _paid_anchors():
    return [spec for spec in DEFAULT_SUBSCRIPTION_PLANS if Decimal(spec["price"]) > 0]


class AnchorEntitlementTests(SimpleTestCase):
    """The invariant documented at the top of reports/pricing.py."""

    ENTITLEMENT_FIELDS = (
        "support_level",
        "onboarding_sessions",
        "included_archive_storage_gb",
    )

    def test_every_paid_anchor_carries_identical_entitlements(self):
        anchors = _paid_anchors()
        self.assertTrue(anchors)

        for field in self.ENTITLEMENT_FIELDS:
            values = {spec.get(field) for spec in anchors}
            self.assertEqual(
                len(values),
                1,
                f"'{field}' differs across paid anchors ({values}); an entitlement "
                f"step turns the smooth price curve into a band where buying more "
                f"capacity costs less in real terms.",
            )

    def test_no_anchor_bundles_archive_storage(self):
        """Archive storage is a paid add-on on equal terms for every capacity.

        Bundling it into a single anchor is what made a 100-seat annual purchase
        cheaper in real terms than a 90-seat one.
        """
        for spec in _paid_anchors():
            self.assertEqual(spec.get("included_archive_storage_gb"), 0, spec["name"])

    def test_plan_descriptions_do_not_promise_removed_extras(self):
        removed = ("أرشيف 50GB", "جلسة إعداد", "جلستان")
        for spec in _paid_anchors():
            for phrase in removed:
                self.assertNotIn(phrase, spec.get("description", ""), spec["name"])


class PriceCurveTests(SimpleTestCase):
    def _catalog(self):
        class _Plan:
            counter = 0

            def __init__(self, spec):
                _Plan.counter += 1
                self.pk = _Plan.counter
                self.name = spec["name"]
                self.price = spec["price"]
                self.days_duration = spec["days_duration"]
                self.max_teachers = spec["max_teachers"]
                self.is_active = True

        return build_flexible_pricing_catalog(
            plans=[_Plan(spec) for spec in DEFAULT_SUBSCRIPTION_PLANS]
        )

    def test_price_rises_with_capacity_in_every_period(self):
        for group in self._catalog():
            previous = None
            for quote in group["quotes"]:
                if previous is not None:
                    self.assertGreater(
                        quote["price"],
                        previous["price"],
                        f"{group['label']}: {quote['capacity']} seats is not dearer "
                        f"than {previous['capacity']}",
                    )
                previous = quote

    def test_cost_per_teacher_falls_as_capacity_grows(self):
        """Buying a larger capacity must stay a better unit rate, never worse."""
        for group in self._catalog():
            rates = [
                (quote["capacity"], quote["price"] / Decimal(quote["capacity"]))
                for quote in group["quotes"]
            ]
            for (_, earlier), (capacity, later) in pairwise(rates):
                self.assertLess(later, earlier, f"{group['label']} at {capacity} seats")

    def test_longer_periods_are_cheaper_per_month(self):
        by_period = {group["key"]: group for group in self._catalog()}
        self.assertIn("1m", by_period)

        for capacity in (25, 50, 100):
            monthly = next(
                q for q in by_period["1m"]["quotes"] if q["capacity"] == capacity
            )
            for period_key in ("6m", "1y"):
                if period_key not in by_period:
                    continue
                longer = next(
                    q for q in by_period[period_key]["quotes"] if q["capacity"] == capacity
                )
                self.assertLess(
                    longer["price"],
                    monthly["price"] * PERIODS[period_key]["months"],
                    f"{period_key} at {capacity} seats offers no saving",
                )

    def test_every_published_capacity_is_quotable(self):
        for group in self._catalog():
            capacities = {quote["capacity"] for quote in group["quotes"]}
            for teacher_count in range(1, 101):
                capacity = normalize_teacher_capacity(teacher_count)
                if capacity is None:
                    continue
                self.assertIn(capacity, capacities, f"{group['label']} / {teacher_count}")

    def test_archive_addon_price_is_not_undercut_by_a_capacity_step(self):
        """The regression this whole invariant exists for.

        Every capacity step must cost less than the archive add-on it used to
        bundle; otherwise the larger purchase is the cheaper one.
        """
        addon_price = Decimal(DEFAULT_ARCHIVE_PRICING["annual_price"])
        for group in self._catalog():
            quotes = group["quotes"]
            for earlier, later in pairwise(quotes):
                step = later["price"] - earlier["price"]
                self.assertGreater(step, 0)
                if later["capacity"] == quotes[-1]["capacity"]:
                    # The top anchor previously bundled the add-on; assert the
                    # bundle is gone rather than that the step covers its price.
                    self.assertLess(step, addon_price * 100)


class PeriodMappingTests(SimpleTestCase):
    def test_every_paid_anchor_maps_to_a_sellable_period(self):
        for spec in _paid_anchors():
            self.assertIsNotNone(
                period_key_for_days(spec["days_duration"]),
                f"{spec['name']} ({spec['days_duration']} days) is unsellable",
            )

    def test_capacity_normalisation_never_undersells(self):
        for teacher_count in range(1, 101):
            capacity = normalize_teacher_capacity(teacher_count)
            self.assertIsNotNone(capacity)
            self.assertGreaterEqual(capacity, teacher_count)

    def test_capacity_beyond_the_published_range_needs_a_custom_quote(self):
        self.assertIsNone(normalize_teacher_capacity(101))


@override_settings(ALLOWED_HOSTS=["testserver"])
class ManagerSubscriptionPageTests(TestCase):
    """The manager picks a teacher count, sees the amount, and can read what the
    subscription covers before paying."""

    def setUp(self):
        for spec in DEFAULT_SUBSCRIPTION_PLANS:
            SubscriptionPlan.objects.create(is_active=True, **spec)

        self.school = School.objects.create(name="مدرسة التسعير", code="pricing-school")
        SchoolSubscription.objects.create(
            school=self.school,
            plan=SubscriptionPlan.objects.get(name="تشغيل | سنوي"),
        )
        self.manager = Teacher.objects.create_user(
            phone="500011223",
            name="مدير التسعير",
            password="strong-pass-123",
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

    def _page(self):
        return self.client.get(reverse("reports:my_subscription"))

    def test_page_offers_a_teacher_count_selector_and_a_live_amount(self):
        response = self._page()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "data-flex-teacher-count")
        self.assertContains(response, "data-flex-price")
        self.assertContains(response, 'name="teacher_capacity"')

    def test_page_lists_what_the_subscription_includes_before_payment(self):
        response = self._page()

        self.assertContains(response, "ما الذي يشمله الاشتراك؟")
        for feature in SUBSCRIPTION_INCLUDED_FEATURES:
            self.assertContains(response, feature["title"])

    def test_page_separates_paid_addons_from_what_is_included(self):
        response = self._page()

        self.assertContains(response, "خدمات تُشترى بشكل منفصل")

    def test_selected_capacity_and_amount_are_recapped_next_to_the_features(self):
        response = self._page()

        self.assertContains(response, 'data-flex-summary-target="#subIncludesSummary"')
        self.assertContains(response, 'id="subIncludesSummary"')


@override_settings(ALLOWED_HOSTS=["testserver"])
class PlatformPlansGuardTests(TestCase):
    """Admins can edit anchors freely, so the anomaly must be caught at runtime
    too — not just by the test on the shipped defaults."""

    def setUp(self):
        for spec in DEFAULT_SUBSCRIPTION_PLANS:
            SubscriptionPlan.objects.create(is_active=True, **spec)
        self.admin = Teacher.objects.create_superuser(
            phone="500099887",
            name="مدير النظام",
            password="strong-pass-123",
        )
        self.client.force_login(self.admin)

    def _page(self):
        return self.client.get(reverse("reports:platform_plans_list"))

    def test_consistent_anchors_raise_no_warning(self):
        response = self._page()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["pricing_warnings"], [])
        self.assertNotContains(response, "إعدادات التسعير غير متسقة")

    def test_reintroducing_a_bundled_archive_is_flagged(self):
        plan = SubscriptionPlan.objects.get(name="قيادة | سنوي")
        plan.included_archive_storage_gb = 50
        plan.save(update_fields=["included_archive_storage_gb"])

        response = self._page()

        warnings = response.context["pricing_warnings"]
        self.assertTrue(any("الأرشيف المشمول" in warning for warning in warnings))
        self.assertContains(response, "إعدادات التسعير غير متسقة")

    def test_a_larger_capacity_priced_below_a_smaller_one_is_flagged(self):
        plan = SubscriptionPlan.objects.get(name="قيادة | سنوي")
        plan.price = Decimal("1000.00")
        plan.save(update_fields=["price"])

        warnings = self._page().context["pricing_warnings"]

        self.assertTrue(any("ليس أعلى من" in warning for warning in warnings))

    def test_mismatched_support_level_is_flagged(self):
        plan = SubscriptionPlan.objects.get(name="انطلاقة | شهري")
        plan.support_level = "standard"
        plan.save(update_fields=["support_level"])

        warnings = self._page().context["pricing_warnings"]

        self.assertTrue(any("مستوى الدعم" in warning for warning in warnings))


class IncludedFeatureAccuracyTests(SimpleTestCase):
    """The "what's included" panel is the last thing a manager reads before
    paying, so every line has to match what the platform actually ships."""

    def test_no_feature_promises_a_downloadable_mobile_app(self):
        """There is no native app — the product is a PWA served from the browser."""
        forbidden = ("تحميل التطبيق", "متجر التطبيقات", "App Store", "Google Play")
        for feature in SUBSCRIPTION_INCLUDED_FEATURES:
            text = f"{feature['title']} {feature['detail']}"
            for phrase in forbidden:
                self.assertNotIn(phrase, text, feature["title"])

    def test_the_mobile_feature_describes_the_pwa_it_actually_is(self):
        mobile = next(
            feature
            for feature in SUBSCRIPTION_INCLUDED_FEATURES
            if feature["icon"] == "fa-mobile-screen"
        )

        self.assertIn("المتصفح", mobile["detail"])
        self.assertIn("لا يوجد تطبيق منفصل", mobile["detail"])

    def test_pricing_does_not_overpromise_push_delivery_guarantees(self):
        """Web Push now ships, but pricing must not imply an unconditional
        guarantee: device permission, OS settings and connectivity still apply."""
        for feature in SUBSCRIPTION_INCLUDED_FEATURES:
            text = f"{feature['title']} {feature['detail']}"
            self.assertNotIn("إشعارات فورية", text, feature["title"])
            self.assertNotIn("push", text.lower(), feature["title"])

    def test_every_feature_has_a_title_an_icon_and_a_detail(self):
        for feature in SUBSCRIPTION_INCLUDED_FEATURES:
            self.assertTrue(feature.get("icon", "").startswith("fa-"), feature)
            self.assertTrue(feature.get("title"))
            self.assertTrue(feature.get("detail"))


@override_settings(ALLOWED_HOSTS=["testserver"])
class PricingMatrixEditorTests(TestCase):
    """Nine anchor prices drive every published price, and they only make sense
    relative to each other — so they are edited and validated together."""

    def setUp(self):
        for spec in DEFAULT_SUBSCRIPTION_PLANS:
            SubscriptionPlan.objects.create(is_active=True, **spec)
        self.admin = Teacher.objects.create_superuser(
            phone="500077665",
            name="مدير التسعير",
            password="strong-pass-123",
        )
        self.client.force_login(self.admin)
        self.url = reverse("reports:platform_pricing_matrix")

    def _valid_payload(self, **overrides):
        payload = {
            "price_25_1m": "149", "price_25_6m": "799", "price_25_1y": "1290",
            "price_50_1m": "229", "price_50_6m": "1190", "price_50_1y": "1990",
            "price_100_1m": "349", "price_100_6m": "1790", "price_100_1y": "2990",
        }
        payload.update(overrides)
        return payload

    def test_editor_is_superuser_only(self):
        self.client.logout()
        teacher = Teacher.objects.create_user(
            phone="500077111", name="معلم", password="strong-pass-123"
        )
        self.client.force_login(teacher)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 302)

    def test_editor_loads_current_prices_into_the_grid(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        initial = response.context["form"].initial
        self.assertEqual(initial["price_25_1m"], Decimal("149.00"))
        self.assertEqual(initial["price_100_1y"], Decimal("2990.00"))

    def test_saving_the_matrix_updates_every_published_price(self):
        response = self.client.post(
            self.url,
            self._valid_payload(price_50_1y="2100"),
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        plan = SubscriptionPlan.objects.get(days_duration=365, max_teachers=50)
        self.assertEqual(plan.price, Decimal("2100"))

    def test_a_saved_anchor_immediately_changes_the_interpolated_prices(self):
        self.client.post(self.url, self._valid_payload(price_50_1y="2100"), follow=True)

        annual = next(
            group for group in build_flexible_pricing_catalog() if group["key"] == "1y"
        )
        sixty = next(q for q in annual["quotes"] if q["capacity"] == 60)
        # 60 sits between the 50 and 100 anchors: 2100 + (2990-2100) * 0.2
        self.assertEqual(sixty["price"], Decimal("2278"))

    def test_a_larger_capacity_priced_below_a_smaller_one_is_rejected(self):
        response = self.client.post(self.url, self._valid_payload(price_100_1y="1500"))

        self.assertEqual(response.status_code, 200)
        self.assertIn("price_100_1y", response.context["form"].errors)
        # Nothing may be written when the matrix as a whole is invalid.
        self.assertEqual(
            SubscriptionPlan.objects.get(days_duration=365, max_teachers=100).price,
            Decimal("2990.00"),
        )

    def test_a_longer_period_without_a_real_saving_is_rejected(self):
        response = self.client.post(self.url, self._valid_payload(price_25_1y="1800"))

        self.assertEqual(response.status_code, 200)
        self.assertIn("price_25_1y", response.context["form"].errors)

    def test_zero_and_negative_prices_are_rejected(self):
        for bad in ("0", "-50"):
            with self.subTest(price=bad):
                response = self.client.post(self.url, self._valid_payload(price_25_1m=bad))
                self.assertIn("price_25_1m", response.context["form"].errors)

    def test_a_missing_anchor_is_created_with_the_uniform_entitlements(self):
        SubscriptionPlan.objects.filter(days_duration=180, max_teachers=100).delete()

        self.client.post(self.url, self._valid_payload(), follow=True)

        created = SubscriptionPlan.objects.get(days_duration=180, max_teachers=100)
        self.assertEqual(created.price, Decimal("1790"))
        self.assertEqual(created.support_level, "priority")
        self.assertEqual(created.onboarding_sessions, 0)
        self.assertEqual(created.included_archive_storage_gb, 0)
        self.assertTrue(created.is_active)

    def test_editor_previews_what_the_customer_will_see(self):
        response = self.client.get(self.url)

        self.assertContains(response, "ما سيراه العميل")
        capacities = {
            quote["capacity"]
            for group in response.context["flexible_pricing_catalog"]
            for quote in group["quotes"]
        }
        self.assertIn(60, capacities)

    def test_editor_shows_the_features_the_customer_is_promised(self):
        response = self.client.get(self.url)

        for feature in SUBSCRIPTION_INCLUDED_FEATURES:
            self.assertContains(response, feature["title"])

    def test_plans_page_links_to_the_matrix_editor(self):
        response = self.client.get(reverse("reports:platform_plans_list"))

        self.assertContains(response, reverse("reports:platform_pricing_matrix"))


@override_settings(ALLOWED_HOSTS=["testserver"], SITE_URL="https://tawtheeq.example")
class UnifiedPricingSurfacesTests(TestCase):
    """A visitor and a paying manager must be shown the same model: pick a
    teacher count, see the amount, read what it covers."""

    def setUp(self):
        for spec in DEFAULT_SUBSCRIPTION_PLANS:
            SubscriptionPlan.objects.create(is_active=True, **spec)

    def test_landing_sells_by_teacher_count(self):
        response = self.client.get(reverse("reports:landing"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "data-flex-teacher-count")
        self.assertContains(response, "كم معلماً سيستخدم اشتراك المدرسة؟")

    def test_landing_lists_the_same_features_as_the_manager_page(self):
        response = self.client.get(reverse("reports:landing"))

        self.assertContains(response, "ما الذي يشمله اشتراك المدرسة؟")
        for feature in SUBSCRIPTION_INCLUDED_FEATURES:
            self.assertContains(response, feature["title"])

    def test_landing_recaps_the_selected_capacity_next_to_the_features(self):
        response = self.client.get(reverse("reports:landing"))

        self.assertContains(response, 'data-flex-summary-target="#landingIncludesSummary"')
        self.assertContains(response, 'id="landingIncludesSummary"')

    def test_both_surfaces_read_the_same_catalog(self):
        school = School.objects.create(name="مدرسة الموحّد", code="unified-school")
        SchoolSubscription.objects.create(
            school=school, plan=SubscriptionPlan.objects.get(name="تشغيل | سنوي")
        )
        manager = Teacher.objects.create_user(
            phone="500066554", name="مدير", password="strong-pass-123"
        )
        SchoolMembership.objects.create(
            school=school, teacher=manager, role_type=SchoolMembership.RoleType.MANAGER
        )

        landing = self.client.get(reverse("reports:landing"))

        self.client.force_login(manager)
        session = self.client.session
        session["active_school_id"] = school.id
        session.save()
        manager_page = self.client.get(reverse("reports:my_subscription"))

        def quoted(response):
            return {
                (group["key"], quote["capacity"], quote["price"])
                for group in response.context["flexible_pricing_catalog"]
                for quote in group["quotes"]
            }

        self.assertEqual(quoted(landing), quoted(manager_page))
