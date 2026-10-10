import json
import time
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from reports.models import (
    School,
    SchoolMembership,
    SchoolSubscription,
    SubscriptionPlan,
    Teacher,
    TeacherTotpDevice,
    WebAuthnCredential,
)
from reports.totp import TOTP_PERIOD_SECONDS, code_for_counter, encrypt_secret, generate_secret
from reports.views.auth import (
    WEBAUTHN_AUTH_CHALLENGE_SESSION_KEY,
    WEBAUTHN_AUTH_DISCOVERABLE_SESSION_KEY,
)
from reports.views.totp import PENDING_NEXT_SESSION_KEY, PENDING_USER_SESSION_KEY
from reports.webauthn import AuthenticatorData, b64url_encode, credential_hash


@override_settings(ALLOWED_HOSTS=["testserver"], RATELIMIT_ENABLE=False)
class LogoutJourneyTests(TestCase):
    password = "Logout-journey-safe-2026"

    @classmethod
    def setUpTestData(cls):
        cls.user = Teacher.objects.create_user(
            phone="0558140001", name="مدير اختبار الخروج", password=cls.password,
        )
        cls.school = School.objects.create(name="مدرسة اختبار الخروج", code="logout-journey")
        plan = SubscriptionPlan.objects.create(name="Logout QA", price=0, days_duration=365)
        SchoolSubscription.objects.create(school=cls.school, plan=plan)
        SchoolMembership.objects.create(
            school=cls.school, teacher=cls.user, role_type=SchoolMembership.RoleType.MANAGER,
        )

    def test_anonymous_logout_returns_plain_login_without_logout_next(self):
        response = self.client.get(reverse("reports:logout"))

        self.assertRedirects(response, reverse("reports:login"), fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_repeated_logout_clears_session_and_keeps_plain_login(self):
        self.client.force_login(self.user)
        session = self.client.session
        session["active_school_id"] = self.school.pk
        session.save()

        for _ in range(2):
            response = self.client.get(reverse("reports:logout"))
            self.assertRedirects(response, reverse("reports:login"), fetch_redirect_response=False)
            self.assertNotIn("_auth_user_id", self.client.session)
            self.assertNotIn("active_school_id", self.client.session)

    def test_login_does_not_render_logout_as_its_destination(self):
        for target in ("/logout/", "/logout/?from=menu", "/logout/#done", "/logout", "/lo%67out/", "/school/../logout/"):
            with self.subTest(target=target):
                response = self.client.get(reverse("reports:login"), {"next": target})
                self.assertIsNone(response.context["next"])

    def test_password_login_from_stale_logout_next_keeps_authenticated_session(self):
        response = self.client.post(
            reverse("reports:login") + "?next=/logout/",
            {"phone": self.user.phone, "password": self.password, "next": "/logout/"},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.redirect_chain[0][0], reverse("reports:admin_dashboard"))
        self.assertFalse(any("/logout" in url for url, _ in response.redirect_chain))
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.pk)

    def _enable_totp(self):
        secret = generate_secret()
        TeacherTotpDevice.objects.create(
            teacher=self.user, secret_encrypted=encrypt_secret(secret), confirmed_at=timezone.now(),
        )
        return code_for_counter(secret, int(time.time() // TOTP_PERIOD_SECONDS))

    def test_password_with_totp_never_saves_logout_as_pending_destination(self):
        self._enable_totp()
        response = self.client.post(
            reverse("reports:login"),
            {"phone": self.user.phone, "password": self.password, "next": "/logout/"},
        )

        self.assertRedirects(response, reverse("reports:totp_challenge"), fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(self.client.session[PENDING_NEXT_SESSION_KEY], "")

    def test_totp_also_discards_logout_destination_saved_by_an_older_session(self):
        code = self._enable_totp()
        session = self.client.session
        session[PENDING_USER_SESSION_KEY] = self.user.pk
        session[PENDING_NEXT_SESSION_KEY] = "/logout/?from=old-login"
        session.save()

        response = self.client.post(reverse("reports:totp_challenge"), {"code": code}, follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(any("/logout" in url for url, _ in response.redirect_chain))
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.pk)

    def test_verified_passkey_discards_logout_destination(self):
        credential_id = b"logout-journey-credential"
        WebAuthnCredential.objects.create(
            teacher=self.user, credential_id=credential_id,
            credential_id_hash=credential_hash(credential_id), public_key_cose=b"public-key",
        )
        session = self.client.session
        session[WEBAUTHN_AUTH_CHALLENGE_SESSION_KEY] = "logout-passkey-challenge"
        session[WEBAUTHN_AUTH_DISCOVERABLE_SESSION_KEY] = True
        session.save()
        with (
            mock.patch("reports.views.auth.parse_client_data", return_value=b"client-data-hash"),
            mock.patch("reports.views.auth.parse_authenticator_data", return_value=AuthenticatorData(flags=0x05, sign_count=1)),
            mock.patch("reports.views.auth.verify_signature", return_value=None),
        ):
            response = self.client.post(
                reverse("reports:passkey_login_verify"),
                data=json.dumps({
                    "rawId": b64url_encode(credential_id), "next": "/logout/",
                    "response": {
                        "clientDataJSON": "stub", "authenticatorData": "stub", "signature": "stub",
                        "userHandle": b64url_encode(str(self.user.pk).encode("utf-8")),
                    },
                }),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["redirect"], reverse("reports:admin_dashboard"))
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.pk)
