import re
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.core.cache import cache
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from reports.models import Teacher


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class PasswordRecoveryUiSourceTests(SimpleTestCase):
    def setUp(self):
        template_root = PROJECT_ROOT / "reports" / "templates" / "reports"
        self.base = (template_root / "auth_recovery_base.html").read_text(encoding="utf-8")
        self.templates = {
            name: (template_root / name).read_text(encoding="utf-8")
            for name in (
                "password_reset_form.html",
                "password_reset_done.html",
                "password_reset_confirm.html",
                "password_reset_complete.html",
            )
        }
        self.core_css = (PROJECT_ROOT / "static" / "css" / "auth-core.css").read_text(
            encoding="utf-8"
        )
        self.recovery_css = (
            PROJECT_ROOT / "static" / "css" / "auth-recovery.css"
        ).read_text(encoding="utf-8")
        self.javascript = (
            PROJECT_ROOT / "static" / "js" / "auth-password-toggle.js"
        ).read_text(encoding="utf-8")

    def test_recovery_pages_use_the_shared_authentication_shell(self):
        for template in self.templates.values():
            self.assertIn('extends "reports/auth_recovery_base.html"', template)
        self.assertIn("css/auth-core.css", self.base)
        self.assertIn("css/auth-recovery.css", self.base)
        self.assertIn("js/auth-password-toggle.js", self.base)
        self.assertNotIn("<style", self.base)

    def test_shell_preserves_standalone_security_and_accessibility_contracts(self):
        self.assertIn('lang="ar" dir="rtl"', self.base)
        self.assertIn('name="robots" content="noindex,nofollow,noarchive"', self.base)
        self.assertIn('class="skip-link"', self.base)
        self.assertIn('id="mainContent"', self.base)
        self.assertEqual(self.base.count("<h1"), 1)
        self.assertIn('aria-label="مراحل استعادة كلمة المرور"', self.base)
        self.assertIn('aria-current="step"', self.base)

    def test_request_and_confirm_keep_form_security_hooks(self):
        request_template = self.templates["password_reset_form.html"]
        confirm_template = self.templates["password_reset_confirm.html"]
        self.assertIn("{% csrf_token %}", request_template)
        self.assertIn("{% csrf_token %}", confirm_template)
        self.assertIn("form.email.id_for_label", request_template)
        self.assertIn("form.new_password1.id_for_label", confirm_template)
        self.assertIn("form.new_password2.id_for_label", confirm_template)
        self.assertIn('type="button"', confirm_template)
        self.assertIn('aria-pressed="false"', confirm_template)
        self.assertNotIn("{{ token", self.base + "".join(self.templates.values()))

    def test_authentication_css_uses_tokens_and_logical_properties(self):
        css = self.core_css + self.recovery_css
        self.assertIn("var(--twq-primary)", css)
        self.assertIn("var(--twq-surface-elevated)", css)
        self.assertIn("padding-inline", css)
        self.assertIn("border-inline-start", css)
        self.assertNotIn("transition: all", css)
        self.assertNotRegex(css, r"#[0-9a-fA-F]{3,8}\b")
        for physical_property in (
            "margin-left",
            "margin-right",
            "padding-left",
            "padding-right",
        ):
            self.assertNotIn(physical_property, css)

    def test_password_toggle_is_progressive_and_accessible(self):
        self.assertIn('[data-password-toggle]', self.javascript)
        self.assertIn('setAttribute("aria-pressed"', self.javascript)
        self.assertIn('input.type = shouldShow ? "text" : "password"', self.javascript)


@override_settings(
    ALLOWED_HOSTS=["testserver"],
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="no-reply@tawtheeq-ksa.com",
    PASSWORD_RESET_TIMEOUT=3600,
)
class PasswordRecoveryUiFunctionalTests(TestCase):
    old_password = "Old-safe-password-2026"
    new_password = "Cedar!River-4827"

    def setUp(self):
        cache.clear()
        self.user = Teacher.objects.create_user(
            phone="0558300001",
            name="مستخدم رحلة الاستعادة",
            password=self.old_password,
            email="recovery-ui@example.com",
        )

    def _request_reset(self, email=None, *, client=None):
        return (client or self.client).post(
            reverse("reports:password_reset"),
            {"email": email or self.user.email},
        )

    def _reset_path_from_email(self):
        match = re.search(r"https?://[^\s]+", mail.outbox[-1].body)
        self.assertIsNotNone(match)
        return match.group(0).split("testserver", 1)[1]

    def _set_password_path(self):
        self._request_reset()
        response = self.client.get(self._reset_path_from_email())
        self.assertEqual(response.status_code, 302)
        return response.headers["Location"]

    def test_request_page_renders_email_contract_and_safe_guidance(self):
        response = self.client.get(reverse("reports:password_reset"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<body class="recovery-page">')
        self.assertContains(response, 'name="email"')
        self.assertContains(response, 'autocomplete="email"')
        self.assertContains(response, 'aria-describedby="recoveryEmailHint recoveryEmailError"')
        self.assertContains(response, "لا نكشف ما إذا كان البريد مرتبطًا بحساب")

    def test_known_unknown_and_inactive_accounts_share_public_result(self):
        inactive = Teacher.objects.create_user(
            phone="0558300002",
            name="حساب غير نشط",
            password=self.old_password,
            email="inactive-recovery@example.com",
        )
        inactive.is_active = False
        inactive.save(update_fields=["is_active"])

        known = self._request_reset(self.user.email)
        self.assertRedirects(known, reverse("reports:password_reset_done"), fetch_redirect_response=False)
        self.assertEqual(len(mail.outbox), 1)

        mail.outbox.clear()
        unknown = self._request_reset("unknown-recovery@example.com")
        self.assertRedirects(unknown, reverse("reports:password_reset_done"), fetch_redirect_response=False)
        self.assertEqual(len(mail.outbox), 0)

        inactive_response = self._request_reset(inactive.email)
        self.assertRedirects(
            inactive_response,
            reverse("reports:password_reset_done"),
            fetch_redirect_response=False,
        )
        self.assertEqual(len(mail.outbox), 0)

    def test_valid_form_exposes_password_manager_and_accessibility_contract(self):
        set_password_path = self._set_password_path()
        response = self.client.get(set_password_path)
        content = response.content.decode()

        self.assertContains(response, 'name="new_password1"')
        self.assertContains(response, 'name="new_password2"')
        self.assertEqual(content.count('autocomplete="new-password"'), 2)
        self.assertContains(response, 'data-password-toggle="id_new_password1"')
        self.assertContains(response, 'data-password-toggle="id_new_password2"')
        self.assertNotIn(mail.outbox[-1].body.split("/confirm/", 1)[1].split("/", 1)[0], content)

    def test_password_validation_and_confirmation_errors_remain_server_side(self):
        set_password_path = self._set_password_path()
        response = self.client.post(
            set_password_path,
            {"new_password1": "password", "new_password2": "different"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="newPassword2Error"')
        self.assertContains(response, 'role="alert"')

    def test_success_changes_password_invalidates_old_password_and_reuses_login(self):
        set_password_path = self._set_password_path()
        response = self.client.post(
            set_password_path,
            {"new_password1": self.new_password, "new_password2": self.new_password},
        )

        self.assertRedirects(
            response,
            reverse("reports:password_reset_complete"),
            fetch_redirect_response=False,
        )
        self.user.refresh_from_db()
        self.assertFalse(self.user.check_password(self.old_password))
        self.assertTrue(self.user.check_password(self.new_password))

        old_client = Client()
        old_client.post(
            reverse("reports:login"),
            {"phone": self.user.phone, "password": self.old_password},
        )
        self.assertNotIn("_auth_user_id", old_client.session)
        new_client = Client()
        new_login = new_client.post(
            reverse("reports:login"),
            {"phone": self.user.phone, "password": self.new_password},
        )
        self.assertEqual(new_login.status_code, 302)
        self.assertEqual(int(new_client.session["_auth_user_id"]), self.user.pk)

    def test_recovery_replaces_the_temporary_phone_password_before_login(self):
        temporary_user = Teacher.objects.create_user(
            phone="0558300003",
            name="مستخدم استعادة الدخول الأول",
            password="0558300003",
            email="first-login-recovery@example.com",
        )
        reset_client = Client()
        request_response = self._request_reset(
            temporary_user.email,
            client=reset_client,
        )
        self.assertRedirects(
            request_response,
            reverse("reports:password_reset_done"),
            fetch_redirect_response=False,
        )
        raw_path = self._reset_path_from_email()
        raw_response = reset_client.get(raw_path)
        self.assertEqual(raw_response.status_code, 302)

        reset_response = reset_client.post(
            raw_response.headers["Location"],
            {"new_password1": self.new_password, "new_password2": self.new_password},
        )
        self.assertRedirects(
            reset_response,
            reverse("reports:password_reset_complete"),
            fetch_redirect_response=False,
        )

        login_client = Client()
        login_response = login_client.post(
            reverse("reports:login"),
            {"phone": temporary_user.phone, "password": self.new_password},
        )
        self.assertEqual(login_response.status_code, 302)
        self.assertNotEqual(login_response.headers["Location"], reverse("reports:my_profile"))

    @override_settings(PASSWORD_RESET_TIMEOUT=1)
    def test_expired_token_uses_nontechnical_invalid_state(self):
        created_at = datetime(2026, 1, 1, 12, 0, 0)
        with patch(
            "django.contrib.auth.tokens.PasswordResetTokenGenerator._now",
            return_value=created_at,
        ):
            token = default_token_generator.make_token(self.user)
        path = reverse(
            "reports:password_reset_confirm",
            kwargs={
                "uidb64": urlsafe_base64_encode(force_bytes(self.user.pk)),
                "token": token,
            },
        )

        with patch(
            "django.contrib.auth.tokens.PasswordResetTokenGenerator._now",
            return_value=created_at + timedelta(seconds=2),
        ):
            response = self.client.get(path)

        self.assertContains(response, "الرابط غير صالح")
        self.assertContains(response, "طلب رابط استعادة جديد")
        self.assertNotContains(response, token)

    def test_request_requires_csrf(self):
        strict_client = Client(enforce_csrf_checks=True)
        response = strict_client.post(
            reverse("reports:password_reset"),
            {"email": self.user.email},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(len(mail.outbox), 0)

    def test_request_rate_limit_remains_five_posts_per_ten_minutes(self):
        for index in range(5):
            response = self._request_reset(f"unknown-{index}@example.com")
            self.assertEqual(response.status_code, 302)

        blocked = self._request_reset("unknown-blocked@example.com")
        self.assertEqual(blocked.status_code, 403)
        self.assertEqual(len(mail.outbox), 0)
