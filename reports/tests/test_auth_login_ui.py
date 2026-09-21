from pathlib import Path

from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from reports.models import Teacher


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class LoginUiSourceContractTests(SimpleTestCase):
    def setUp(self):
        self.template = (
            PROJECT_ROOT / "reports" / "templates" / "reports" / "login.html"
        ).read_text(encoding="utf-8")
        self.core_css = (PROJECT_ROOT / "static" / "css" / "auth-core.css").read_text(
            encoding="utf-8"
        )
        self.css = (PROJECT_ROOT / "static" / "css" / "auth-login.css").read_text(
            encoding="utf-8"
        )
        self.javascript = (
            PROJECT_ROOT / "static" / "js" / "auth-login.js"
        ).read_text(encoding="utf-8")

    def test_login_owns_external_css_and_javascript(self):
        self.assertIn("css/auth-core.css", self.template)
        self.assertIn("css/auth-login.css", self.template)
        self.assertIn("js/auth-login.js", self.template)
        self.assertNotIn("<style", self.template)
        self.assertNotIn("<script nonce=", self.template)

    def test_login_fields_preserve_the_authentication_contract(self):
        self.assertIn('name="phone"', self.template)
        self.assertIn('id="identifier"', self.template)
        self.assertIn('autocomplete="username webauthn"', self.template)
        self.assertIn("رقم الجوال أو الهوية الوطنية", self.template)
        self.assertIn('name="password"', self.template)
        self.assertIn('autocomplete="current-password"', self.template)
        self.assertIn('name="next"', self.template)
        self.assertIn("{% csrf_token %}", self.template)

    def test_login_keeps_recovery_passkey_and_accessibility_hooks(self):
        self.assertIn("reports:password_reset", self.template)
        self.assertIn("reports:passkey_login_options", self.template)
        self.assertIn("reports:passkey_login_verify", self.template)
        self.assertIn('type="button"', self.template)
        self.assertIn('aria-pressed="false"', self.template)
        self.assertIn('aria-describedby="identifierHelp"', self.template)
        self.assertIn('class="skip-link"', self.template)
        self.assertIn('id="mainContent"', self.template)

    def test_login_css_uses_tawtheeq_tokens_and_logical_properties(self):
        combined_css = self.core_css + self.css
        self.assertIn("var(--twq-primary)", combined_css)
        self.assertIn("var(--twq-surface-elevated)", combined_css)
        self.assertIn("padding-inline", combined_css)
        self.assertIn("inset-inline-start", combined_css)
        self.assertNotIn("transition: all", combined_css)
        self.assertNotRegex(combined_css, r"#[0-9a-fA-F]{3,8}\b")
        for physical_property in (
            "margin-left",
            "margin-right",
            "padding-left",
            "padding-right",
        ):
            self.assertNotIn(physical_property, combined_css)

    def test_login_javascript_preserves_password_and_passkey_behaviour(self):
        self.assertIn('getElementById("togglePass")', self.javascript)
        self.assertIn('setAttribute("aria-pressed"', self.javascript)
        self.assertIn('getElementById("passkeyLoginBtn")', self.javascript)
        self.assertIn('credentials: "same-origin"', self.javascript)
        self.assertIn('"X-CSRFToken"', self.javascript)
        self.assertIn("isConditionalMediationAvailable", self.javascript)


@override_settings(ALLOWED_HOSTS=["testserver"], RATELIMIT_ENABLE=False)
class LoginUiFunctionalContractTests(TestCase):
    password = "Safe-login-password-2026"

    @classmethod
    def setUpTestData(cls):
        cls.user = Teacher.objects.create_user(
            phone="0558100001",
            national_id="1100000001",
            name="مستخدم اختبار الدخول",
            password=cls.password,
        )
        cls.inactive_user = Teacher.objects.create_user(
            phone="0558100002",
            national_id="1100000002",
            name="مستخدم موقوف",
            password=cls.password,
        )
        cls.inactive_user.is_active = False
        cls.inactive_user.save(update_fields=["is_active"])

    def test_login_page_renders_focused_standalone_contract(self):
        response = self.client.get(reverse("reports:login"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<body class="auth-page">')
        self.assertContains(response, 'action="/login/"')
        self.assertContains(response, "المنصة الرسمية لإدارة أعمال المدرسة")
        self.assertNotContains(response, 'name="remember_me"')

    def test_platform_admin_login_reuses_the_page_with_its_own_action(self):
        response = self.client.get(reverse("reports:platform_login"))

        self.assertContains(response, "دخول مدير النظام")
        self.assertContains(response, 'action="/platform-login/"')

    def test_valid_login_accepts_national_id_and_preserves_safe_next(self):
        target = reverse("reports:password_reset")
        response = self.client.post(
            reverse("reports:login"),
            {
                "phone": self.user.national_id,
                "password": self.password,
                "next": target,
            },
        )

        self.assertRedirects(response, target, fetch_redirect_response=False)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.pk)

    def test_external_next_is_not_reflected_or_followed(self):
        response = self.client.get(
            reverse("reports:login"),
            {"next": "https://example.com/collect-session"},
        )

        self.assertEqual(response.context["next"], None)
        self.assertNotContains(response, "https://example.com/collect-session")

    def test_unknown_and_wrong_password_use_the_same_safe_message(self):
        wrong_password = Client().post(
            reverse("reports:login"),
            {"phone": self.user.phone, "password": "wrong-password"},
        )
        unknown_account = Client().post(
            reverse("reports:login"),
            {"phone": "0558199999", "password": "wrong-password"},
        )
        safe_message = "رقم الجوال/الهوية أو كلمة المرور غير صحيحة"

        self.assertContains(wrong_password, safe_message)
        self.assertContains(unknown_account, safe_message)
        self.assertNotContains(unknown_account, "الحساب غير موجود")

    def test_inactive_account_with_correct_password_is_rejected(self):
        response = self.client.post(
            reverse("reports:login"),
            {"phone": self.inactive_user.phone, "password": self.password},
        )

        self.assertContains(response, "حسابك موقوف")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_temporary_password_keeps_first_login_redirect(self):
        temporary_user = Teacher.objects.create_user(
            phone="0558100003",
            name="مستخدم دخول أول",
            password="0558100003",
        )

        response = self.client.post(
            reverse("reports:login"),
            {"phone": temporary_user.phone, "password": temporary_user.phone},
        )

        self.assertRedirects(
            response,
            reverse("reports:my_profile"),
            fetch_redirect_response=False,
        )
