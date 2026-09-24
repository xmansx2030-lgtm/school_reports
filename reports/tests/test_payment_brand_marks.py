"""A payment brand mark is a claim that we accept that method.

Tamara is available behind ``TAMARA_ENABLED``. Its name must disappear from
public and member pages while disabled, and appear with the checkout only when
the production operator deliberately enables it.

**لماذا تُعدّ الصفحات كلها لا صفحة الهبوط وحدها؟** النسخة الأولى من هذا
الاختبار غطّت ثلاث صفحات عامة، وكانت سياسة الخصوصية — وهي عامة كذلك — تسمّي
البوابة اسماً صريحاً غير مشروط بأي عَلَم، فمرّ الذكرُ من ثغرة التغطية لا من
ثغرة المنطق. فالقائمة هنا تشمل ما يراه الزائر وما يراه المشترك بعد الدخول:
صفحة الاشتراك وسجلّ المدفوعات.
"""

from django.test import TestCase, override_settings
from django.urls import reverse

from reports.models import (
    School,
    SchoolMembership,
    SchoolSubscription,
    SubscriptionPlan,
    Teacher,
)


TAMARA_LOGO = "paymentPanelTamara"
TAMARA_WORD = "تمارا"
PAYMENT_LOGO_ASSETS = (
    "img/payment/mada.svg",
    "img/payment/visa.svg",
    "img/payment/mastercard.svg",
    "img/payment/tamara-wordmark-ar.png",
)

# كل ما يبلغه زائر غير مسجَّل ويحمل — أو قد يحمل — ذكراً لوسيلة دفع.
PUBLIC_PAGES = (
    "reports:landing",
    "reports:user_guide",
    "reports:faq",
    "reports:privacy_policy",
    "reports:terms_conditions",
    "reports:refund_policy",
    "reports:service_delivery_policy",
    "reports:complaints_policy",
)

# وما يبلغه المشترك بعد الدخول: هنا يقع اختيار وسيلة الدفع فعلاً.
MEMBER_PAGES = (
    "reports:my_subscription",
    "reports:subscription_history",
)


def _assert_clean(testcase, bodies):
    offenders = []
    for name, body in bodies:
        if TAMARA_LOGO in body:
            offenders.append(f"{name}: logo")
        if TAMARA_WORD in body:
            offenders.append(f"{name}: wordmark text")

    testcase.assertEqual(
        offenders,
        [],
        "تمارا غير مفعّلة، فلا يجوز أن يظهر شعارها أو اسمها:\n"
        + "\n".join(offenders),
    )


@override_settings(ALLOWED_HOSTS=["testserver"])
class TamaraBrandMarkIsGatedOnPublicPagesTests(TestCase):
    def _bodies(self):
        for name in PUBLIC_PAGES:
            response = self.client.get(reverse(name), follow=True)
            self.assertEqual(response.status_code, 200, name)
            yield name, response.content.decode("utf-8", errors="replace")

    @override_settings(TAMARA_ENABLED=False)
    def test_no_public_page_mentions_tamara_while_disabled(self):
        _assert_clean(self, self._bodies())

    @override_settings(MOYASAR_ENABLED=True)
    def test_the_privacy_policy_names_only_the_gateways_actually_in_use(self):
        """الإفصاح عن معالِج بيانات لا نستعمله خطأٌ نظامي لا تفصيل واجهة."""
        body = self.client.get(
            reverse("reports:privacy_policy"), follow=True
        ).content.decode("utf-8", errors="replace")

        self.assertNotIn(TAMARA_WORD, body)
        self.assertIn("ميسر", body)

    @override_settings(TAMARA_ENABLED=True, MOYASAR_ENABLED=False)
    def test_public_disclosure_names_tamara_when_enabled(self):
        body = self.client.get(
            reverse("reports:privacy_policy"), follow=True
        ).content.decode("utf-8", errors="replace")
        self.assertIn(TAMARA_WORD, body)

    @override_settings(MOYASAR_ENABLED=True, TAMARA_ENABLED=True)
    def test_payment_logos_are_not_repeated_across_other_public_pages(self):
        for name in PUBLIC_PAGES[1:]:
            response = self.client.get(reverse(name), follow=True)
            self.assertEqual(response.status_code, 200, name)
            body = response.content.decode("utf-8", errors="replace")
            for asset in PAYMENT_LOGO_ASSETS:
                self.assertNotIn(asset, body, f"{asset} appeared on {name}")


@override_settings(ALLOWED_HOSTS=["testserver"])
class TamaraBrandMarkIsGatedOnMemberPagesTests(TestCase):
    """ما يراه المشترك بعد الدخول يخضع للقاعدة نفسها.

    اختيار وسيلة الدفع يقع في «اشتراكي»، وسجلّ المدفوعات يعرض حالة البوابة —
    وكلاهما خلف تسجيل دخول، فلا يبلغهما اختبار الصفحات العامة.
    """

    def setUp(self):
        self.school = School.objects.create(name="مدرسة الدفع", code="pay-school")
        plan = SubscriptionPlan.objects.create(
            name="باقة", price=100, days_duration=365, max_teachers=25
        )
        SchoolSubscription.objects.create(school=self.school, plan=plan)
        self.manager = Teacher.objects.create_user(
            phone="500770001", name="مدير", password="pass"
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

    @override_settings(TAMARA_ENABLED=False, MOYASAR_ENABLED=True)
    def test_no_member_page_mentions_tamara_while_disabled(self):
        def bodies():
            for name in MEMBER_PAGES:
                response = self.client.get(reverse(name), follow=True)
                self.assertEqual(response.status_code, 200, name)
                yield name, response.content.decode("utf-8", errors="replace")

        _assert_clean(self, bodies())

    @override_settings(MOYASAR_ENABLED=True)
    def test_the_electronic_payment_option_is_still_offered_through_moyasar(self):
        """تعطيل تمارا لا يجوز أن يُخفي الدفع الإلكتروني نفسه."""
        body = self.client.get(
            reverse("reports:my_subscription"), follow=True
        ).content.decode("utf-8", errors="replace")

        self.assertIn("paymentPanelMoyasar", body)
        self.assertNotIn("paymentPanelTamara", body)

    @override_settings(MOYASAR_ENABLED=True, TAMARA_ENABLED=True)
    def test_only_checkout_shows_the_active_payment_logos(self):
        checkout = self.client.get(
            reverse("reports:my_subscription"), follow=True
        ).content.decode("utf-8", errors="replace")
        history = self.client.get(
            reverse("reports:subscription_history"), follow=True
        ).content.decode("utf-8", errors="replace")

        for asset in PAYMENT_LOGO_ASSETS:
            self.assertIn(asset, checkout)
            self.assertNotIn(asset, history)
        self.assertNotIn("img/payment/apple-pay.svg", checkout)
        self.assertNotIn("img/payment/samsung-pay.svg", checkout)
