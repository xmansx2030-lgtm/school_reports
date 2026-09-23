from pathlib import Path

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from reports.models import Teacher, TeacherTotpDevice, WebAuthnCredential
from reports.totp import encrypt_secret, generate_secret
from reports.views.totp import PENDING_USER_SESSION_KEY
from reports.webauthn import credential_hash


@override_settings(ALLOWED_HOSTS=["testserver"])
class AuthSecurityUiTests(TestCase):
    def setUp(self):
        self.user = Teacher.objects.create_user(
            phone="500990001",
            name="مستخدم أمان",
            password="StrongPass!234",
        )

    def test_account_security_card_presents_totp_and_passkey_state(self):
        secret = generate_secret()
        TeacherTotpDevice.objects.create(
            teacher=self.user,
            secret_encrypted=encrypt_secret(secret),
            confirmed_at="2026-09-21T12:00:00+00:00",
        )
        credential_id = b"security-ui-passkey"
        WebAuthnCredential.objects.create(
            teacher=self.user,
            credential_id=credential_id,
            credential_id_hash=credential_hash(credential_id),
            public_key_cose=b"public-key",
            device_name="ويندوز · Chrome",
        )
        self.client.force_login(self.user)

        response = self.client.get(reverse("reports:my_profile"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "أمان الحساب")
        self.assertContains(response, "المصادقة الثنائية")
        self.assertContains(response, "إدارة المصادقة الثنائية")
        self.assertContains(response, "مفاتيح المرور")
        self.assertContains(response, "ويندوز · Chrome")
        self.assertContains(response, "css/auth-security.css")

    def test_totp_settings_uses_the_security_workspace_and_never_prints_secret(self):
        secret = generate_secret()
        TeacherTotpDevice.objects.create(
            teacher=self.user,
            secret_encrypted=encrypt_secret(secret),
            confirmed_at="2026-09-21T12:00:00+00:00",
        )
        self.client.force_login(self.user)

        response = self.client.get(reverse("reports:totp_settings"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="twq-page twq-security-page"')
        self.assertContains(response, "الحماية مفعّلة")
        self.assertContains(response, 'autocomplete="current-password"')
        self.assertNotContains(response, secret)

    def test_enrollment_keeps_single_code_field_and_accessible_otp_hints(self):
        self.client.force_login(self.user)

        response = self.client.post(reverse("reports:totp_begin_enrollment"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="code"')
        self.assertContains(response, 'inputmode="numeric"')
        self.assertContains(response, 'autocomplete="one-time-code"')
        self.assertContains(response, 'maxlength="6"')
        self.assertContains(response, "لن تُفعّل الحماية")

    def test_challenge_uses_standalone_authentication_shell(self):
        session = self.client.session
        session[PENDING_USER_SESSION_KEY] = self.user.pk
        session.save()

        response = self.client.get(reverse("reports:totp_challenge"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "reports/auth_security_base.html")
        self.assertContains(response, 'class="twq-auth twq-auth--challenge"')
        self.assertContains(response, 'inputmode="text"')
        self.assertContains(response, 'autocomplete="one-time-code"')
        self.assertContains(response, 'spellcheck="false"')
        self.assertContains(response, "رمز التحقق أو رمز الاسترجاع")
        self.assertNotContains(response, "sidebar")

    def test_totp_state_mutations_require_csrf(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)

        begin = csrf_client.post(reverse("reports:totp_begin_enrollment"))
        disable = csrf_client.post(
            reverse("reports:totp_disable"),
            {"password": "StrongPass!234"},
        )

        self.assertEqual(begin.status_code, 403)
        self.assertEqual(disable.status_code, 403)

    def test_security_styles_use_tokens_and_no_transition_all(self):
        stylesheet = (
            Path(__file__).resolve().parents[2] / "static" / "css" / "auth-security.css"
        ).read_text(encoding="utf-8")

        self.assertIn("--twq-", stylesheet)
        self.assertNotIn("transition: all", stylesheet)
        self.assertNotRegex(stylesheet, r"#[0-9a-fA-F]{3,8}")

    def test_passkey_prompt_has_no_inline_style_block(self):
        template = (
            Path(__file__).resolve().parents[1]
            / "templates"
            / "reports"
            / "partials"
            / "passkey_enrollment_prompt.html"
        ).read_text(encoding="utf-8")

        self.assertNotIn("<style", template)
