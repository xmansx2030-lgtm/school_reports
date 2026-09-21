from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from reports.models import Teacher, TeacherTotpDevice, WebAuthnCredential
from reports.totp import encrypt_secret, generate_secret
from reports.webauthn import credential_hash


PROJECT_ROOT = Path(settings.BASE_DIR)


class PasswordChangeSourceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.template = (PROJECT_ROOT / "reports" / "templates" / "reports" / "my_profile.html").read_text(
            encoding="utf-8"
        )
        cls.styles = (PROJECT_ROOT / "static" / "css" / "auth-password.css").read_text(encoding="utf-8")
        cls.javascript = (PROJECT_ROOT / "static" / "js" / "auth-password-toggle.js").read_text(
            encoding="utf-8"
        )

    def test_profile_loads_owned_password_assets(self):
        self.assertIn("css/auth-password.css", self.template)
        self.assertIn("js/auth-password-toggle.js", self.template)
        self.assertNotIn("data-toggle-password", self.template)
        self.assertEqual(self.template.count("data-password-toggle="), 3)

    def test_password_fields_keep_semantic_labels_and_autocomplete(self):
        self.assertIn('for="{{ pwd_form.old_password.id_for_label }}"', self.template)
        self.assertIn('for="{{ pwd_form.new_password1.id_for_label }}"', self.template)
        self.assertIn('for="{{ pwd_form.new_password2.id_for_label }}"', self.template)
        self.assertIn('autocomplete="current-password"', self.template)
        self.assertGreaterEqual(self.template.count('autocomplete="new-password"'), 2)
        self.assertIn('aria-pressed="false"', self.template)

    def test_password_styles_use_semantic_tokens_and_logical_properties(self):
        self.assertIn("var(--twq-", self.styles)
        self.assertNotRegex(self.styles, r"#[0-9a-fA-F]{3,8}\b")
        self.assertNotRegex(self.styles, r"\b(?:rgb|hsl)a?\(")
        self.assertNotIn("transition: all", self.styles)
        self.assertNotRegex(
            self.styles,
            r"(?m)^\s*(?:margin|padding|border)-(?:left|right)\s*:",
        )
        self.assertIn("inline-size", self.styles)
        self.assertIn("inset-inline-start", self.styles)

    def test_shared_password_toggle_is_accessible_and_presentation_only(self):
        self.assertIn('[data-password-toggle]', self.javascript)
        self.assertIn('button.setAttribute("aria-pressed"', self.javascript)
        self.assertIn('button.setAttribute("aria-label"', self.javascript)
        self.assertNotIn("fetch(", self.javascript)
        self.assertNotIn("submit(", self.javascript)


@override_settings(ALLOWED_HOSTS=["testserver"], EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class PasswordChangeExperienceTests(TestCase):
    def setUp(self):
        self.profile_url = reverse("reports:my_profile")
        self.current_password = "Current-safe-password"
        self.user = Teacher.objects.create_user(
            phone="0557800001",
            name="مستخدم تغيير كلمة المرور",
            password=self.current_password,
            email="user@example.com",
        )
        self.client.force_login(self.user)

    def _payload(self, *, old=None, new="New-safe-password", confirmation=None, email=None):
        payload = {
            "update_password": "1",
            "pwd-old_password": old if old is not None else self.current_password,
            "pwd-new_password1": new,
            "pwd-new_password2": new if confirmation is None else confirmation,
        }
        if email is not None:
            payload["pwd-email"] = email
        return payload

    def test_normal_change_renders_the_actual_contract(self):
        response = self.client.get(self.profile_url)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["force_password_change"])
        self.assertContains(response, 'data-password-mode="normal"')
        self.assertContains(response, 'name="pwd-old_password"')
        self.assertContains(response, 'autocomplete="current-password"')
        self.assertContains(response, 'autocomplete="new-password"', count=2)
        self.assertNotContains(response, 'name="pwd-email"')
        self.assertContains(response, "8 أحرف على الأقل")
        self.assertContains(response, "ألا تتكون من أرقام فقط")
        self.assertContains(response, "ألا تكون كلمة مرور شائعة")

    def test_wrong_current_password_is_rejected_without_changing_password(self):
        response = self.client.post(self.profile_url, self._payload(old="wrong-current-password"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["pwd_form"].errors.get("old_password"))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(self.current_password))

    def test_short_password_is_rejected(self):
        response = self.client.post(self.profile_url, self._payload(new="12345"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["pwd_form"].errors.get("new_password2"))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(self.current_password))

    def test_mismatched_confirmation_is_rejected(self):
        response = self.client.post(
            self.profile_url,
            self._payload(new="New-safe-password", confirmation="Different-password"),
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["pwd_form"].errors.get("new_password2"))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(self.current_password))

    @patch("reports.utils.run_task_safe")
    def test_success_rotates_session_and_keeps_user_authenticated(self, run_task_safe):
        old_session_key = self.client.session.session_key

        response = self.client.post(self.profile_url, self._payload())

        self.assertRedirects(response, self.profile_url, fetch_redirect_response=False)
        self.user.refresh_from_db()
        new_session_key = self.client.session.session_key
        self.assertTrue(self.user.check_password("New-safe-password"))
        self.assertFalse(self.user.check_password(self.current_password))
        self.assertNotEqual(old_session_key, new_session_key)
        self.assertEqual(self.user.current_session_key, new_session_key)
        self.assertEqual(str(self.client.session.get("_auth_user_id")), str(self.user.pk))
        run_task_safe.assert_called_once()

    @patch("reports.utils.run_task_safe")
    def test_password_change_preserves_totp_and_passkeys(self, run_task_safe):
        device = TeacherTotpDevice.objects.create(
            teacher=self.user,
            secret_encrypted=encrypt_secret(generate_secret()),
            confirmed_at="2026-09-21T12:00:00+00:00",
        )
        credential_id = b"password-change-passkey"
        credential = WebAuthnCredential.objects.create(
            teacher=self.user,
            credential_id=credential_id,
            credential_id_hash=credential_hash(credential_id),
            public_key_cose=b"public-key",
        )

        response = self.client.post(self.profile_url, self._payload())

        self.assertRedirects(response, self.profile_url, fetch_redirect_response=False)
        self.assertTrue(TeacherTotpDevice.objects.filter(pk=device.pk).exists())
        self.assertTrue(WebAuthnCredential.objects.filter(pk=credential.pk).exists())
        run_task_safe.assert_called_once()

    def test_current_policy_rejects_six_digit_numeric_password(self):
        response = self.client.post(self.profile_url, self._payload(new="123456"))

        self.assertEqual(response.status_code, 200)
        error_codes = {error.code for error in response.context["pwd_form"].errors.as_data()["new_password2"]}
        self.assertIn("password_too_short", error_codes)
        self.assertIn("password_entirely_numeric", error_codes)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(self.current_password))

    def test_current_policy_rejects_common_password(self):
        response = self.client.post(self.profile_url, self._payload(new="password"))

        self.assertEqual(response.status_code, 200)
        error_codes = {error.code for error in response.context["pwd_form"].errors.as_data()["new_password2"]}
        self.assertIn("password_too_common", error_codes)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(self.current_password))

    def test_password_change_requires_csrf(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)

        response = csrf_client.post(self.profile_url, self._payload())

        self.assertEqual(response.status_code, 403)


@override_settings(ALLOWED_HOSTS=["testserver"], EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class ForcedFirstLoginExperienceTests(TestCase):
    def setUp(self):
        self.phone = "0557800011"
        self.user = Teacher.objects.create_user(
            phone=self.phone,
            name="مستخدم الدخول الأول",
            password=self.phone,
        )
        self.profile_url = reverse("reports:my_profile")
        self.client.force_login(self.user)

    def _payload(self, *, new="First-login-safe-password"):
        return {
            "update_password": "1",
            "pwd-email": "first.login@example.com",
            "pwd-old_password": self.phone,
            "pwd-new_password1": new,
            "pwd-new_password2": new,
        }

    def test_first_login_uses_focused_mandatory_context(self):
        response = self.client.get(self.profile_url)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["force_password_change"])
        self.assertContains(response, 'data-password-mode="first-login"')
        self.assertContains(response, 'class="profile-scope profile-scope--forced"')
        self.assertContains(response, 'name="pwd-email"')
        self.assertContains(response, "خطوة أمان إلزامية")
        self.assertContains(response, "كلمة المرور المؤقتة")
        self.assertNotContains(response, "مطابقة لرقم الجوال")

    def test_first_login_cannot_bypass_with_html_or_json_navigation(self):
        html_response = self.client.get(
            reverse("reports:home"),
            HTTP_ACCEPT="text/html",
            HTTP_SEC_FETCH_MODE="navigate",
            HTTP_SEC_FETCH_DEST="document",
        )
        json_response = self.client.get(
            reverse("reports:home"),
            HTTP_ACCEPT="application/json",
        )

        self.assertRedirects(html_response, self.profile_url, fetch_redirect_response=False)
        self.assertEqual(json_response.status_code, 403)
        self.assertEqual(json_response.json(), {"detail": "password_change_required"})

    @patch("reports.utils.run_task_safe")
    def test_first_login_change_clears_gate_and_preserves_session(self, run_task_safe):
        response = self.client.post(self.profile_url, self._payload())

        self.assertRedirects(response, self.profile_url, fetch_redirect_response=False)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "first.login@example.com")
        self.assertFalse(self.user.check_password(self.phone))
        self.assertTrue(self.user.check_password("First-login-safe-password"))
        self.assertEqual(str(self.client.session.get("_auth_user_id")), str(self.user.pk))

        profile_response = self.client.get(self.profile_url)
        self.assertFalse(profile_response.context["force_password_change"])
        home_response = self.client.get(
            reverse("reports:home"),
            HTTP_ACCEPT="text/html",
            HTTP_SEC_FETCH_MODE="navigate",
            HTTP_SEC_FETCH_DEST="document",
        )
        self.assertNotEqual(home_response.headers.get("Location"), self.profile_url)
