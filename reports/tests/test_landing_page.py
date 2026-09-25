from __future__ import annotations

import json
import re

from django.test import TestCase, override_settings
from django.urls import reverse

from reports.models import SubscriptionPlan


@override_settings(
    ALLOWED_HOSTS=["testserver"],
    # A stale deployment setting must not override the approved policy.
    TRIAL_DAYS=14,
    SITE_URL="https://tawtheeq.example",
)
class LandingPageTests(TestCase):
    def test_landing_explains_the_product_and_has_a_clear_primary_action(self):
        response = self.client.get(reverse("reports:landing"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "مدرستك منظّمة.")
        self.assertContains(response, "وإنجازك موثّق.")
        self.assertContains(response, "إدارة التقارير والطلبات والخطط والاعتمادات من مكان واحد.")
        self.assertContains(response, "التعاميم")
        self.assertContains(response, "الأرشيف")
        self.assertContains(response, "ابدأ بمدرستك 30 يومًا مجانًا")
        self.assertNotContains(response, "ابدأ بمدرستك 14 يومًا مجانًا")
        self.assertContains(response, "أدخل عدد المعلمين واعرف السعر فورًا")
        self.assertContains(response, reverse("reports:register_school"))
        self.assertContains(response, "img/landing/dashboard-system.png")
        self.assertContains(response, "img/landing/report-system.png")
        self.assertContains(response, "img/landing/tickets-system.png")
        self.assertContains(response, "img/landing/archive-system.png")
        self.assertContains(response, "بيانات تجريبية")
        self.assertContains(response, 'id="productLightbox"')
        self.assertContains(response, "img/brand-mark.svg")
        self.assertNotContains(response, "+500K")
        self.assertNotContains(response, "100% رضا")

    def test_landing_exposes_prices_in_desktop_and_mobile_navigation(self):
        response = self.client.get(reverse("reports:landing"))
        html = response.content.decode("utf-8")

        header = html[html.index("<header"): html.index("</header>")]
        mobile_menu = html[html.index('id="mobileMenu"'): html.index("<main")]
        self.assertIn('href="#pricing">الأسعار</a>', header)
        self.assertIn('href="#pricing">الأسعار</a>', mobile_menu)
        self.assertNotIn('href="#pricing">الباقات</a>', html)
        self.assertContains(response, "سعر مدرستك")

    def test_landing_uses_a_short_decision_focused_information_architecture(self):
        response = self.client.get(reverse("reports:landing"))
        html = response.content.decode("utf-8")

        for section_id in ("features", "pricing", "product", "faq"):
            self.assertEqual(html.count(f'id="{section_id}"'), 1)
        self.assertLess(html.index('id="features"'), html.index('id="pricing"'))
        self.assertLess(html.index('id="pricing"'), html.index('id="product"'))
        self.assertLess(html.index('id="product"'), html.index('id="faq"'))
        for removed_section_id in (
            "roles",
            "start",
            "compare",
            "voice",
            "leadership",
            "group",
            "security",
            "verify",
        ):
            self.assertNotIn(f'id="{removed_section_id}"', html)

    def test_landing_metadata_names_the_product_outcome_and_core_audiences(self):
        response = self.client.get(reverse("reports:landing"))

        self.assertContains(
            response,
            "<title>منصة توثيق | للمدارس ومساحة إنجاز شخصية للمعلمين والمعلمات</title>",
            html=True,
        )
        for audience in ("مدير المدرسة", "المعلم", "المدير التنفيذي", "الموظف الإداري"):
            self.assertContains(response, audience)

    def test_landing_backs_each_feature_headline_with_concrete_capabilities(self):
        response = self.client.get(reverse("reports:landing"))
        html = response.content.decode("utf-8")

        # Tied to the card count rather than a number: a headline added without
        # its proof list is exactly the regression this guards against, and a
        # literal count only catches it until someone bumps the literal.
        self.assertEqual(
            html.count('class="feature-proof"'),
            html.count('class="feature-card'),
        )
        self.assertGreaterEqual(html.count('class="feature-proof"'), 6)
        self.assertContains(response, "PDF بهوية المدرسة")
        self.assertContains(response, "كتابة التقرير بالصوت")
        self.assertContains(response, "تحسين بالذكاء الاصطناعي")
        self.assertContains(response, "إدخال المحضر بالصوت")
        self.assertContains(response, "بصمة تحقق SHA-256")
        self.assertContains(response, "سجل اطلاع وتوقيع")

    def test_landing_has_accessible_navigation_and_single_main_heading(self):
        response = self.client.get(reverse("reports:landing"))
        html = response.content.decode("utf-8")

        self.assertEqual(len(re.findall(r"<h1\b", html, re.IGNORECASE)), 1)
        self.assertIn('href="#mainContent"', html)
        self.assertIn('aria-controls="mobileMenu"', html)
        self.assertIn('aria-expanded="false"', html)
        self.assertIn(
            'id="mobileMenu" role="dialog" aria-modal="true"',
            html,
        )
        self.assertLess(
            html.index("</header>"),
            html.index('id="mobileMenu"'),
            "The fixed mobile menu must stay outside the blurred sticky header.",
        )
        self.assertIn(f'href="{reverse("reports:terms_conditions")}"', html)
        self.assertIn(f'href="{reverse("reports:refund_policy")}"', html)
        self.assertIn("أسعار واضحة", html)
        self.assertNotIn("هوية مقدم الخدمة", html)
        self.assertNotIn('href="#business"', html)
        self.assertIn('<span>دخول</span>', html)
        self.assertIn('src="/static/js/landing.js?v=20260826.1"', html)
        self.assertNotIn("var periodButtons", html)
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertEqual(response.headers["CDN-Cache-Control"], "no-store")
        self.assertEqual(
            response.headers["Cloudflare-CDN-Cache-Control"],
            "no-store",
        )

    @override_settings(MOYASAR_ENABLED=True, TAMARA_ENABLED=True)
    def test_landing_shows_only_supported_payment_brands_in_the_footer(self):
        response = self.client.get(reverse("reports:landing"))
        html = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "اختر وسيلة الدفع الأنسب لمدرستك")
        self.assertNotContains(response, 'class="payment-options reveal"')
        self.assertContains(response, 'class="footer-payments"')
        self.assertContains(response, 'aria-label="وسائل الدفع المدعومة"')
        self.assertNotContains(response, "img/moyasar-icon-official.png")
        self.assertNotContains(response, 'aria-label="يونيون باي"')
        self.assertNotContains(response, "UnionPay")
        for asset in (
            "img/payment/mada.svg",
            "img/payment/visa.svg",
            "img/payment/mastercard.svg",
            "img/payment/tamara-wordmark-ar.png",
        ):
            self.assertContains(response, asset)
        for unsupported_label in ("أمريكان إكسبريس", "Apple Pay", "Google Pay", "Samsung Pay", "STC Pay"):
            self.assertNotContains(response, unsupported_label)
        self.assertContains(response, "تظهر وسائل الدفع المتاحة عند إتمام الدفع")
        self.assertNotContains(response, "ميسر")
        self.assertNotContains(response, "مُيسّر")
        self.assertGreater(html.index('class="footer-payments"'), html.index("<footer"))
        self.assertLess(html.index('class="footer-payments"'), html.index('class="footer-bottom"'))

    @override_settings(MOYASAR_ENABLED=True, TAMARA_ENABLED=False)
    def test_footer_advertises_only_payment_methods_the_platform_accepts(self):
        """إعلان وسيلة دفع غير مدعومة يقود الزائر إلى خيار لن يجده عند الدفع."""
        response = self.client.get(reverse("reports:landing"))

        self.assertContains(response, "تظهر وسائل الدفع المتاحة عند إتمام الدفع")
        self.assertNotContains(response, "ميسر")
        self.assertNotContains(response, "مُيسّر")
        self.assertContains(response, "img/payment/mada.svg")
        self.assertNotContains(response, "tamara")
        self.assertNotContains(response, "تمارا")

    @override_settings(MOYASAR_ENABLED=False, TAMARA_ENABLED=True)
    def test_tamara_only_displays_its_logo_without_card_networks(self):
        response = self.client.get(reverse("reports:landing"))

        self.assertContains(response, "img/payment/tamara-wordmark-ar.png")
        for asset in ("img/payment/mada.svg", "img/payment/visa.svg", "img/payment/mastercard.svg"):
            self.assertNotContains(response, asset)

    @override_settings(MOYASAR_ENABLED=False, TAMARA_ENABLED=False)
    def test_payment_footer_is_hidden_when_no_online_gateway_is_active(self):
        self.assertNotContains(self.client.get(reverse("reports:landing")), 'class="footer-payments"')

    def test_landing_exposes_complete_canonical_and_social_metadata(self):
        response = self.client.get(reverse("reports:landing"))
        html = response.content.decode("utf-8")

        self.assertContains(
            response,
            '<link rel="canonical" href="https://tawtheeq.example/">',
            html=True,
        )
        self.assertIn(
            '<meta name="robots" content="index,follow,max-image-preview:large',
            html,
        )
        self.assertIn('property="og:url" content="https://tawtheeq.example/"', html)
        self.assertIn('name="twitter:card" content="summary_large_image"', html)
        self.assertIn('"@type": "SoftwareApplication"', html)
        self.assertIn('"@type": "WebSite"', html)
        self.assertNotIn("X-Robots-Tag", response.headers)
        schemas = re.findall(
            r'<script type="application/ld\+json"[^>]*>(.*?)</script>',
            html,
            flags=re.DOTALL,
        )
        self.assertEqual(len(schemas), 1)
        self.assertIsInstance(json.loads(schemas[0]), dict)

    @override_settings(CSP_ENABLED=True, CSP_REPORT_ONLY=False)
    def test_landing_embeds_official_sbc_verification_seal(self):
        response = self.client.get(reverse("reports:landing"))
        html = response.content.decode("utf-8")
        seal_origin = "https://eauthenticate.saudibusiness.gov.sa"

        self.assertContains(response, 'class="sbc-verify-seal"')
        self.assertContains(
            response,
            'data-token="SUdjMEt0WXNwNW5IREVVeUNxajRkUT09"',
        )
        self.assertNotContains(response, 'data-position="bottom-right"')
        self.assertContains(response, 'class="footer-verification"')
        self.assertContains(response, "توثيق رسمي")
        self.assertGreater(html.index('class="sbc-verify-seal"'), html.index("<footer"))
        self.assertLess(
            html.index('class="sbc-verify-seal"'),
            html.index("</footer>"),
        )
        self.assertRegex(
            html,
            (
                r'<script\b[^>]*\bnonce="[^"]+"[^>]*'
                r'src="https://eauthenticate\.saudibusiness\.gov\.sa/'
                r'EAuthSealApi/seal\.js"[^>]*\basync\b[^>]*></script>'
            ),
        )

        # بديل ثابت داخل الحاوية: بدونه يترك فشلُ السكربت الخارجي فراغاً صامتاً.
        self.assertContains(response, 'class="sbc-seal-fallback"')
        self.assertContains(
            response,
            "https://eauthenticate.saudibusiness.gov.sa/certificate-details/0000314192",
        )
        self.assertContains(response, "متجر موثّق")

        policy = response.headers["Content-Security-Policy"]
        self.assertIn(seal_origin, policy)
        self.assertIn(f"frame-src 'self' {seal_origin}", policy)
        self.assertIn(
            (
                "script-src 'self' "
                f"'nonce-{response.context['CSP_NONCE']}' "
                f"{seal_origin}"
            ),
            policy,
        )
        self.assertIn(
            (
                "script-src-elem 'self' "
                f"'nonce-{response.context['CSP_NONCE']}' "
                f"{seal_origin}"
            ),
            policy,
        )
        # ختم الجهة الحكومية أصلٌ واحد بعينه. أما نطاق CDN عام فيخدم كل حزمة
        # منشورة عليه، فوجوده يحوّل ``script-src`` من حصرٍ إلى إذن عام —
        # وChart.js صار مُستضافاً في ``static/js/vendor/``.
        self.assertNotIn("cdn.jsdelivr.net", policy)

    def test_private_pages_send_noindex_header(self):
        response = self.client.get(reverse("reports:login"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers.get("X-Robots-Tag"),
            "noindex, nofollow, noarchive",
        )

    def test_public_content_pages_remain_indexable(self):
        for route_name in (
            "reports:faq",
            "reports:privacy_policy",
            "reports:terms_conditions",
            "reports:refund_policy",
            "reports:service_delivery_policy",
            "reports:complaints_policy",
            "reports:user_guide",
        ):
            response = self.client.get(reverse(route_name))
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("X-Robots-Tag", response.headers)
            self.assertContains(response, "https://tawtheeq.example")

        faq_response = self.client.get(reverse("reports:faq"))
        faq_schema = re.search(
            r'<script type="application/ld\+json"[^>]*>(.*?)</script>',
            faq_response.content.decode("utf-8"),
            flags=re.DOTALL,
        )
        self.assertIsNotNone(faq_schema)
        self.assertEqual(json.loads(faq_schema.group(1))["@type"], "FAQPage")

    def test_faq_interactions_are_csp_safe_and_keyboard_accessible(self):
        response = self.client.get(reverse("reports:faq"))
        html = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertNotRegex(html, r"\son(?:click|keyup)=")
        self.assertEqual(
            html.count(
                'class="faq-question" role="button" tabindex="0" '
                'aria-expanded="false"'
            ),
            22,
        )
        self.assertIn(
            "question.addEventListener('click', () => toggleFAQ(question))",
            html,
        )
        self.assertIn(
            "document.getElementById('faqSearch').addEventListener('input', searchFAQ)",
            html,
        )

    def test_published_plans_drive_the_teacher_count_calculator(self):
        SubscriptionPlan.objects.create(
            name="تجربة المدرسة",
            price=0,
            days_duration=21,
            max_teachers=5,
            description="تشغيل كامل للتجربة\nدعم البدء",
        )
        SubscriptionPlan.objects.create(
            name="مدرسة متوسطة شهري",
            price=229,
            days_duration=30,
            max_teachers=50,
            description="تقارير غير محدودة\nملفات إنجاز",
        )
        SubscriptionPlan.objects.create(
            name="مدرسة متوسطة",
            price=650,
            days_duration=180,
            max_teachers=50,
            description="تقارير غير محدودة\nملفات إنجاز",
        )
        SubscriptionPlan.objects.create(
            name="مدرسة متوسطة سنوي",
            price=1250,
            days_duration=365,
            max_teachers=50,
            description="تقارير غير محدودة\nملفات إنجاز",
        )

        response = self.client.get(reverse("reports:landing"))

        self.assertEqual(response.status_code, 200)

        # Selling is by teacher count: the calculator is the only purchase path,
        # and the three durations are chosen inside it.
        self.assertTrue(response.context["flexible_pricing_catalog"])
        self.assertContains(response, "data-flex-teacher-count")
        self.assertContains(response, 'data-period="1m"')
        self.assertContains(response, 'data-period="6m"')
        self.assertContains(response, 'data-period="1y"')

        # No package cards beside it — the visitor must not be asked to choose twice.
        html = response.content.decode("utf-8")
        self.assertNotIn("price-card paid-card", html)
        self.assertNotIn("pricing-grid--trial-only", html)
        # The free trial stays as a concise entry point, not another price card.
        self.assertContains(response, "تجربة مجانية 30 يومًا")
        self.assertContains(response, "كل اشتراك يخص مدرسة واحدة")
        self.assertContains(response, "إدارة عدة مدارس من الحساب نفسه")
        self.assertNotContains(response, "اشتراك مجمع")
        self.assertNotContains(response, "اشتراك موحد")
        self.assertNotContains(response, "باقة المجموعة")

    def test_legacy_trial_plan_cannot_change_the_approved_public_duration(self):
        SubscriptionPlan.objects.create(
            name="تجربة قديمة",
            price=0,
            days_duration=14,
            max_teachers=5,
            description="تجربة حقيقية لمدة 14 يومًا\nتشغيل كامل للتجربة",
        )

        response = self.client.get(reverse("reports:landing"))

        self.assertEqual(response.context["trial_days"], 30)
        self.assertEqual(response.context["pricing_trial_plan"]["duration_days"], 30)
        self.assertContains(response, "مجانية 30 يومًا")
        self.assertNotContains(response, "مجانية 14 يومًا")

    def test_inactive_plans_are_not_advertised(self):
        SubscriptionPlan.objects.create(
            name="باقة قديمة لا تظهر",
            price=999,
            days_duration=365,
            max_teachers=20,
            is_active=False,
        )

        response = self.client.get(reverse("reports:landing"))

        self.assertNotContains(response, "باقة قديمة لا تظهر")
        self.assertEqual(response.context["pricing_cards"], [])

    def test_landing_leaks_no_template_syntax_to_the_visitor(self):
        """A ``{# #}`` comment spanning two lines is printed, not stripped."""
        html = self.client.get(reverse("reports:landing")).content.decode("utf-8")

        # الصفحة تضمّن JSON مصغّراً (json_script وJSON-LD)، و``}}`` يظهر فيه
        # مشروعاً عند تداخل الكائنات — فالفحص على محدّدات القوالب وحدها.
        for token in ("{#", "#}", "{%", "%}"):
            self.assertNotIn(token, html, f"صيغة قالب ظاهرة للزائر: {token}")

    def test_landing_hides_the_floating_theme_toggle(self):
        """The marketing page must not put a floating button over its CTAs."""
        response = self.client.get(reverse("reports:landing"))

        self.assertContains(response, 'data-theme-toggle="off"')
