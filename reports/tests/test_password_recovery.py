import re
from urllib.parse import urlsplit
from unittest.mock import patch

from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from reports.models import Teacher, TeacherTotpDevice, WebAuthnCredential
from reports.totp import encrypt_secret, generate_secret
from reports.webauthn import credential_hash


@override_settings(
    ALLOWED_HOSTS=["testserver"],
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="no-reply@tawtheeq-ksa.com",
    PASSWORD_RESET_TIMEOUT=3600,
)
class PasswordRecoveryTests(TestCase):
    def setUp(self):
        # The view is rate-limited to 5 POSTs per IP per 10 minutes and the
        # counter lives in the shared cache; without this the last tests to run
        # get a 403 instead of exercising what they assert.
        cache.clear()
        self.user = Teacher.objects.create_user(
            phone="0558000001",
            name="مستخدم الاستعادة",
            password="Old-safe-password",
            email="recovery@example.com",
        )

    def _request_reset(self, email: str):
        return self.client.post(
            reverse("reports:password_reset"),
            {"email": email},
        )

    def _reset_path_from_email(self) -> str:
        match = re.search(r"https?://[^\s]+", mail.outbox[0].body)
        self.assertIsNotNone(match)
        return urlsplit(match.group(0)).path

    def test_login_links_to_password_recovery_form(self):
        response = self.client.get(reverse("reports:login"))

        self.assertContains(
            response,
            f'href="{reverse("reports:password_reset")}"',
        )
        self.assertNotContains(response, "يرجى التواصل مع إدارة المدرسة")

    def test_registered_email_receives_one_time_reset_link(self):
        response = self._request_reset("RECOVERY@example.com")

        self.assertRedirects(
            response,
            reverse("reports:password_reset_done"),
            fetch_redirect_response=False,
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(
            mail.outbox[0].subject,
            "استعادة كلمة المرور | منصة توثيق",
        )
        self.assertEqual(
            mail.outbox[0].from_email,
            "منصة توثيق <no-reply@tawtheeq-ksa.com>",
        )
        self.assertIn(
            reverse(
                "reports:password_reset_confirm",
                kwargs={"uidb64": "placeholder", "token": "placeholder"},
            ).rsplit("/", 3)[0],
            mail.outbox[0].body,
        )

    def test_recovery_email_is_branded_html_with_a_plain_text_part(self):
        """A bare-text recovery mail reads like a phishing attempt."""
        self._request_reset(self.user.email)
        sent = mail.outbox[0]

        # The plain-text part still carries the link, for clients that show it.
        self.assertIn("منصة توثيق", sent.body)
        self.assertIn("مستخدم الاستعادة", sent.body)
        self.assertIn("ساعة واحدة", sent.body)
        self.assertIn("://testserver/password-reset/confirm/", sent.body)

        self.assertEqual(len(sent.alternatives), 1)
        html, content_type = sent.alternatives[0]
        self.assertEqual(content_type, "text/html")
        self.assertIn("منصة توثيق", html)
        self.assertIn("تعيين كلمة مرور جديدة", html)
        self.assertIn("#075c36", html)  # هوية المنصة اللونية
        self.assertIn("توثيق أدق، متابعة أوضح", html)
        self.assertIn("مستخدم الاستعادة", html)
        self.assertIn("/static/img/logo1.png", html)
        self.assertIn("support@tawtheeq-ksa.com", html)
        # The button and the copyable fallback both carry the same link.
        reset_path = self._reset_path_from_email()
        self.assertEqual(html.count(reset_path), 2)

    @override_settings(PASSWORD_RESET_TIMEOUT=7200)
    def test_stated_validity_follows_the_configured_timeout(self):
        self._request_reset(self.user.email)

        self.assertIn("ساعتين", mail.outbox[0].body)
        self.assertIn("ساعتين", mail.outbox[0].alternatives[0][0])

    def test_unknown_email_has_same_public_result_without_sending(self):
        response = self._request_reset("unknown@example.com")

        self.assertRedirects(
            response,
            reverse("reports:password_reset_done"),
            fetch_redirect_response=False,
        )
        self.assertEqual(len(mail.outbox), 0)

        done_response = self.client.get(reverse("reports:password_reset_done"))
        self.assertContains(done_response, "إذا كان البريد مرتبطًا بحساب نشط")
        self.assertNotContains(done_response, "unknown@example.com")

    def test_duplicate_active_email_receives_reset_for_each_account(self):
        Teacher.objects.create_user(
            phone="0558000002",
            name="حساب قديم ببريد مكرر",
            password="Another-safe-password",
            email=self.user.email,
        )

        response = self._request_reset(self.user.email)

        self.assertRedirects(
            response,
            reverse("reports:password_reset_done"),
            fetch_redirect_response=False,
        )
        self.assertEqual(len(mail.outbox), 2)
        self.assertIn("مستخدم الاستعادة", mail.outbox[0].body)
        self.assertIn("حساب قديم ببريد مكرر", mail.outbox[1].body)

    def test_unusable_password_account_can_receive_recovery_link(self):
        user = Teacher.objects.create_user(
            phone="0558000003",
            name="حساب أنشئ من الإدارة",
            email="manager-created@example.com",
        )
        self.assertFalse(user.has_usable_password())

        response = self._request_reset("manager-created@example.com")

        self.assertRedirects(
            response,
            reverse("reports:password_reset_done"),
            fetch_redirect_response=False,
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("حساب أنشئ من الإدارة", mail.outbox[0].body)

    def test_delivery_failure_does_not_reveal_registered_account(self):
        with patch(
            "django.core.mail.EmailMultiAlternatives.send",
            side_effect=RuntimeError("smtp unavailable"),
        ), self.assertLogs("django.contrib.auth", level="ERROR"):
            response = self._request_reset(self.user.email)

        self.assertRedirects(
            response,
            reverse("reports:password_reset_done"),
            fetch_redirect_response=False,
        )

    def test_valid_link_changes_password_and_cannot_be_reused(self):
        self._request_reset(self.user.email)
        original_reset_path = self._reset_path_from_email()

        confirm_response = self.client.get(original_reset_path)
        self.assertEqual(confirm_response.status_code, 302)
        set_password_path = confirm_response["Location"]

        response = self.client.post(
            set_password_path,
            {
                "new_password1": "Cedar!River-4827",
                "new_password2": "Cedar!River-4827",
            },
        )

        self.assertRedirects(
            response,
            reverse("reports:password_reset_complete"),
            fetch_redirect_response=False,
        )
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("Cedar!River-4827"))

        reused_response = self.client.get(original_reset_path, follow=True)
        self.assertContains(reused_response, "الرابط غير صالح")

    def test_password_reset_preserves_totp_and_passkeys(self):
        device = TeacherTotpDevice.objects.create(
            teacher=self.user,
            secret_encrypted=encrypt_secret(generate_secret()),
            confirmed_at="2026-09-21T12:00:00+00:00",
        )
        credential_id = b"password-reset-passkey"
        credential = WebAuthnCredential.objects.create(
            teacher=self.user,
            credential_id=credential_id,
            credential_id_hash=credential_hash(credential_id),
            public_key_cose=b"public-key",
        )
        self._request_reset(self.user.email)
        original_reset_path = self._reset_path_from_email()
        confirm_response = self.client.get(original_reset_path)

        response = self.client.post(
            confirm_response["Location"],
            {
                "new_password1": "Cedar!River-5938",
                "new_password2": "Cedar!River-5938",
            },
        )

        self.assertRedirects(
            response,
            reverse("reports:password_reset_complete"),
            fetch_redirect_response=False,
        )
        self.assertTrue(TeacherTotpDevice.objects.filter(pk=device.pk).exists())
        self.assertTrue(WebAuthnCredential.objects.filter(pk=credential.pk).exists())

    def test_recovery_pages_are_not_indexable(self):
        for route_name in (
            "reports:password_reset",
            "reports:password_reset_done",
            "reports:password_reset_complete",
        ):
            response = self.client.get(reverse(route_name))
            self.assertEqual(
                response.headers.get("X-Robots-Tag"),
                "noindex, nofollow, noarchive",
            )
