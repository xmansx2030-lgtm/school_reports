import hashlib
import json
from pathlib import Path
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from reports.models import Teacher, TeacherTotpDevice, WebAuthnCredential
from reports.totp import encrypt_secret, generate_secret
from reports.views.auth import (
    PASSKEY_ENROLL_PROMPT_SESSION_KEY,
    PASSKEY_PROMPT_SNOOZE_COOKIE,
    PASSKEY_PROMPT_SNOOZE_MAX_AGE,
    PASSKEY_UNSUPPORTED_DEVICE_COOKIE,
    PASSKEY_UNSUPPORTED_DEVICE_MAX_AGE,
    WEBAUTHN_AUTH_CHALLENGE_SESSION_KEY,
    WEBAUTHN_AUTH_DISCOVERABLE_SESSION_KEY,
    _passkey_device_label,
)
from reports.webauthn import (
    AuthenticatorData,
    b64url_encode,
    credential_hash,
    parse_authenticator_data,
)


@override_settings(ALLOWED_HOSTS=["testserver"])
class PasskeyEndpointTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = Teacher.objects.create_user(
            phone="555000111",
            name="Passkey User",
            password="pass",
        )
        self.credential_id = b"passkey-user-credential"
        WebAuthnCredential.objects.create(
            teacher=self.user,
            credential_id=self.credential_id,
            credential_id_hash=credential_hash(self.credential_id),
            public_key_cose=b"public-key",
            transports=["internal"],
        )

    def test_registration_options_require_login(self):
        response = self.client.post(reverse("reports:passkey_register_options"))

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])

    def test_registration_options_return_webauthn_payload(self):
        self.client.force_login(self.user)

        response = self.client.post(reverse("reports:passkey_register_options"))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertIn("challenge", payload["publicKey"])
        self.assertEqual(payload["publicKey"]["rp"]["id"], "testserver")
        self.assertEqual(payload["publicKey"]["user"]["name"], self.user.phone)
        self.assertEqual(
            payload["publicKey"]["authenticatorSelection"]["userVerification"],
            "required",
        )
        self.assertEqual(
            payload["publicKey"]["authenticatorSelection"]["residentKey"],
            "preferred",
        )
        self.assertFalse(
            payload["publicKey"]["authenticatorSelection"]["requireResidentKey"],
        )
        self.assertEqual(payload["publicKey"]["timeout"], 120000)
        self.assertNotIn(
            "authenticatorAttachment",
            payload["publicKey"]["authenticatorSelection"],
        )

    def test_password_login_offers_optional_passkey_enrollment(self):
        user = Teacher.objects.create_user(
            phone="555000444",
            name="New Passkey User",
            password="safe-pass",
        )

        response = self.client.post(
            reverse("reports:login"),
            {"phone": user.phone, "password": "safe-pass"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.client.session[PASSKEY_ENROLL_PROMPT_SESSION_KEY])

        profile_response = self.client.get(reverse("reports:my_profile"))
        self.assertContains(profile_response, 'id="account-security"')
        self.assertContains(profile_response, 'id="passkeyEnrollmentOffer"')
        self.assertContains(profile_response, "تفعيل الآن")
        self.assertContains(profile_response, "ذكّرني بعد 90 يومًا")
        self.assertContains(profile_response, "لا تعرض هذه الرسالة مجددًا")
        self.assertContains(profile_response, "لا تصل إلى المنصة ولا تُحفظ فيها")
        self.assertContains(profile_response, "isUserVerifyingPlatformAuthenticatorAvailable")
        self.assertContains(profile_response, "InvalidStateError")
        self.assertContains(profile_response, "NotSupportedError")
        self.assertNotContains(profile_response, "<dialog")
        self.assertNotContains(profile_response, "showModal")

    def test_password_login_does_not_prompt_user_with_active_passkey(self):
        response = self.client.post(
            reverse("reports:login"),
            {"phone": self.user.phone, "password": "pass"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertNotIn(PASSKEY_ENROLL_PROMPT_SESSION_KEY, self.client.session)
        profile_response = self.client.get(reverse("reports:my_profile"))
        self.assertNotContains(profile_response, 'id="passkeyEnrollmentOffer"')
        self.assertContains(profile_response, "الدخول بالبصمة مفعّل لحسابك")
        self.assertContains(profile_response, "إضافة مفتاح مرور لجهاز آخر")
        self.assertContains(profile_response, "البصمة مفعّلة بالفعل")
        self.assertNotContains(
            profile_response,
            "يوجد مفتاح مرور سابق لهذا الحساب في مدير كلمات مرور Google",
        )

    def test_password_change_requirement_takes_priority_over_passkey_prompt(self):
        user = Teacher.objects.create_user(
            phone="555000555",
            name="Default Password User",
            password="555000555",
        )

        response = self.client.post(
            reverse("reports:login"),
            {"phone": user.phone, "password": user.phone},
        )

        self.assertRedirects(
            response,
            reverse("reports:my_profile"),
            fetch_redirect_response=False,
        )
        self.assertNotIn(PASSKEY_ENROLL_PROMPT_SESSION_KEY, self.client.session)

    def test_user_can_dismiss_optional_prompt_for_current_session(self):
        user = Teacher.objects.create_user(
            phone="555000666",
            name="Dismiss Passkey User",
            password="safe-pass",
        )
        self.client.force_login(user)
        session = self.client.session
        session[PASSKEY_ENROLL_PROMPT_SESSION_KEY] = True
        session.save()

        response = self.client.post(reverse("reports:passkey_enroll_prompt_dismiss"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertNotIn(PASSKEY_ENROLL_PROMPT_SESSION_KEY, self.client.session)

    def test_snooze_hides_the_offer_for_ninety_days_on_this_device(self):
        user = Teacher.objects.create_user(
            phone="555000667",
            name="Snooze Passkey User",
            password="safe-pass",
        )
        self.client.force_login(user)

        response = self.client.post(
            reverse("reports:passkey_enroll_prompt_dismiss"),
            data=json.dumps({"action": "snooze"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["action"], "snooze")
        self.assertEqual(int(response.cookies[PASSKEY_PROMPT_SNOOZE_COOKIE]["max-age"]), PASSKEY_PROMPT_SNOOZE_MAX_AGE)
        user.refresh_from_db()
        self.assertFalse(user.passkey_prompt_opt_out)

    def test_never_choice_is_saved_on_the_account_across_browsers(self):
        user = Teacher.objects.create_user(
            phone="555000668",
            name="Never Prompt User",
            password="safe-pass",
        )
        self.client.force_login(user)

        response = self.client.post(
            reverse("reports:passkey_enroll_prompt_dismiss"),
            data=json.dumps({"action": "never"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["action"], "never")
        user.refresh_from_db()
        self.assertTrue(user.passkey_prompt_opt_out)

        self.client.get(reverse("reports:logout"))
        self.client.cookies.clear()
        login = self.client.post(
            reverse("reports:login"),
            {"phone": user.phone, "password": "safe-pass"},
        )
        self.assertEqual(login.status_code, 302)
        self.assertNotIn(PASSKEY_ENROLL_PROMPT_SESSION_KEY, self.client.session)
        profile = self.client.get(reverse("reports:my_profile"))
        self.assertNotContains(profile, 'id="passkeyEnrollmentOffer"')
        self.assertContains(profile, 'id="registerPasskeyBtn"')

    def test_unsupported_device_is_suppressed_for_one_year_without_account_opt_out(self):
        user = Teacher.objects.create_user(
            phone="555000669",
            name="Unsupported Device User",
            password="safe-pass",
        )
        self.client.force_login(user)

        response = self.client.post(
            reverse("reports:passkey_enroll_prompt_dismiss"),
            data=json.dumps({"action": "unsupported"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["action"], "unsupported")
        self.assertEqual(
            int(response.cookies[PASSKEY_UNSUPPORTED_DEVICE_COOKIE]["max-age"]),
            PASSKEY_UNSUPPORTED_DEVICE_MAX_AGE,
        )
        user.refresh_from_db()
        self.assertFalse(user.passkey_prompt_opt_out)

    def test_dismiss_rejects_unknown_choice(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("reports:passkey_enroll_prompt_dismiss"),
            data=json.dumps({"action": "later-ish"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "action_invalid")

    def test_dismiss_endpoint_requires_login(self):
        response = self.client.post(reverse("reports:passkey_enroll_prompt_dismiss"))

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])

    def test_login_options_are_available_before_login(self):
        response = self.client.post(
            reverse("reports:passkey_login_options"),
            data='{"identifier":"555000111"}',
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertIn("challenge", payload["publicKey"])
        self.assertEqual(payload["publicKey"]["rpId"], "testserver")
        self.assertEqual(payload["publicKey"]["timeout"], 120000)
        self.assertEqual(payload["publicKey"]["userVerification"], "required")
        self.assertEqual(payload["publicKey"]["allowCredentials"][0]["id"], b64url_encode(self.credential_id))

        login_response = self.client.get(reverse("reports:login"))
        self.assertContains(login_response, "js/auth-login.js")
        self.assertContains(login_response, "username webauthn")
        login_javascript = (
            Path(__file__).resolve().parents[2] / "static" / "js" / "auth-login.js"
        ).read_text(encoding="utf-8")
        self.assertIn("isConditionalMediationAvailable", login_javascript)
        self.assertIn("NotSupportedError", login_javascript)
        self.assertIn("NotAllowedError", login_javascript)

    def test_login_options_without_identifier_start_discoverable_ceremony(self):
        """No identifier means one-tap sign-in, not an error.

        The authenticator picks a discoverable passkey and reports which account
        it belongs to, which is what browser autofill relies on.
        """
        response = self.client.post(
            reverse("reports:passkey_login_options"),
            data="{}",
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["discoverable"])
        self.assertEqual(payload["publicKey"]["allowCredentials"], [])
        self.assertIn("challenge", payload["publicKey"])
        self.assertEqual(payload["publicKey"]["userVerification"], "required")

    def test_login_options_reject_user_without_passkey(self):
        Teacher.objects.create_user(
            phone="555000222",
            name="No Passkey User",
            password="pass",
        )

        response = self.client.post(
            reverse("reports:passkey_login_options"),
            data='{"identifier":"555000222"}',
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"], "passkey_not_enabled")

    def test_login_verify_rejects_credential_not_allowed_for_identifier(self):
        other_user = Teacher.objects.create_user(
            phone="555000333",
            name="Other Passkey User",
            password="pass",
        )
        other_credential_id = b"other-user-credential"
        WebAuthnCredential.objects.create(
            teacher=other_user,
            credential_id=other_credential_id,
            credential_id_hash=credential_hash(other_credential_id),
            public_key_cose=b"other-public-key",
            transports=["internal"],
        )

        self.client.post(
            reverse("reports:passkey_login_options"),
            data='{"identifier":"555000111"}',
            content_type="application/json",
        )
        response = self.client.post(
            reverse("reports:passkey_login_verify"),
            data=f'{{"rawId":"{b64url_encode(other_credential_id)}"}}',
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"], "credential_not_allowed")

    def test_login_verify_without_challenge_is_rejected(self):
        response = self.client.post(
            reverse("reports:passkey_login_verify"),
            data="{}",
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "challenge_missing")

    def test_successful_passkey_is_an_alternative_to_password_and_totp(self):
        """A verified passkey is the primary ceremony, not password factor one."""
        TeacherTotpDevice.objects.create(
            teacher=self.user,
            secret_encrypted=encrypt_secret(generate_secret()),
            confirmed_at="2026-09-21T12:00:00+00:00",
        )
        session = self.client.session
        session[WEBAUTHN_AUTH_CHALLENGE_SESSION_KEY] = "passkey-challenge"
        session[WEBAUTHN_AUTH_DISCOVERABLE_SESSION_KEY] = True
        session.save()

        with (
            mock.patch(
                "reports.views.auth.parse_client_data",
                return_value=b"client-data-hash",
            ),
            mock.patch(
                "reports.views.auth.parse_authenticator_data",
                return_value=AuthenticatorData(flags=0x05, sign_count=1),
            ),
            mock.patch("reports.views.auth.verify_signature", return_value=None),
        ):
            response = self.client.post(
                reverse("reports:passkey_login_verify"),
                data=json.dumps(
                    {
                        "rawId": b64url_encode(self.credential_id),
                        "response": {
                            "clientDataJSON": "stub",
                            "authenticatorData": "stub",
                            "signature": "stub",
                            "userHandle": b64url_encode(str(self.user.pk).encode("utf-8")),
                        },
                    }
                ),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertIn("_auth_user_id", self.client.session)
        self.assertNotEqual(response.json()["redirect"], reverse("reports:totp_challenge"))

    def test_discoverable_login_rejects_user_handle_of_another_account(self):
        """The handle the authenticator returns must name the credential's owner."""
        other_user = Teacher.objects.create_user(
            phone="555000555",
            name="Handle Mismatch User",
            password="pass",
        )

        self.client.post(
            reverse("reports:passkey_login_options"),
            data="{}",
            content_type="application/json",
        )
        response = self.client.post(
            reverse("reports:passkey_login_verify"),
            data=(
                '{"rawId":"%s","response":{"userHandle":"%s"}}'
                % (
                    b64url_encode(self.credential_id),
                    b64url_encode(str(other_user.pk).encode("utf-8")),
                )
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"], "user_handle_mismatch")

    def test_user_can_revoke_one_device(self):
        credential = WebAuthnCredential.objects.get(teacher=self.user)
        self.client.force_login(self.user)

        response = self.client.post(reverse("reports:passkey_delete", args=[credential.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["remaining"], 0)
        # Hard delete, not deactivation: ``credential_id`` is unique, so a
        # lingering row would block re-enrolling the same device.
        self.assertFalse(WebAuthnCredential.objects.filter(pk=credential.pk).exists())

    def test_user_cannot_revoke_a_device_of_another_account(self):
        intruder = Teacher.objects.create_user(
            phone="555000666",
            name="Intruder",
            password="pass",
        )
        credential = WebAuthnCredential.objects.get(teacher=self.user)
        self.client.force_login(intruder)

        response = self.client.post(reverse("reports:passkey_delete", args=[credential.pk]))

        self.assertEqual(response.status_code, 404)
        self.assertTrue(WebAuthnCredential.objects.filter(pk=credential.pk).exists())

    def test_dismissing_the_offer_stops_it_returning_on_the_next_login(self):
        user = Teacher.objects.create_user(
            phone="555000777",
            name="Declining User",
            password="safe-pass",
        )
        self.client.force_login(user)

        dismiss = self.client.post(reverse("reports:passkey_enroll_prompt_dismiss"))
        self.assertEqual(dismiss.status_code, 200)
        self.assertIn(PASSKEY_PROMPT_SNOOZE_COOKIE, dismiss.cookies)

        # The real logout view, not Client.logout(), which wipes every cookie
        # including the snooze this test is about.
        self.client.get(reverse("reports:logout"))
        self.client.post(
            reverse("reports:login"),
            data={"phone": "555000777", "password": "safe-pass"},
        )

        self.assertNotIn(PASSKEY_ENROLL_PROMPT_SESSION_KEY, self.client.session)


class PasskeyDeviceLabelTests(TestCase):
    """Every device used to be stored under the same placeholder name."""

    class _Request:
        def __init__(self, agent):
            self.META = {"HTTP_USER_AGENT": agent}

    def test_iphone_safari_is_named_after_the_device(self):
        label = _passkey_device_label(
            self._Request(
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
                "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
            )
        )

        self.assertEqual(label, "آيفون · Safari")

    def test_android_chrome_is_named_after_the_device(self):
        label = _passkey_device_label(
            self._Request(
                "Mozilla/5.0 (Linux; Android 14; SM-S911B) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
            )
        )

        self.assertEqual(label, "جهاز أندرويد · Chrome")

    def test_legacy_placeholder_is_replaced_by_a_real_name(self):
        label = _passkey_device_label(
            self._Request("Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"),
            provided="جهاز المستخدم",
        )

        self.assertEqual(label, "ويندوز · Chrome")

    def test_unknown_agent_falls_back_to_a_neutral_name(self):
        self.assertEqual(_passkey_device_label(self._Request("")), "جهاز مفعّل")


class PasskeyRpIdTests(TestCase):
    """معرّف النطاق الثابت يجعل البصمة تعمل عبر النطاقات الفرعية."""

    def _rp_id(self, host):
        from django.test import RequestFactory

        from reports.webauthn import rp_id_from_request

        request = RequestFactory().get("/", HTTP_HOST=host)
        return rp_id_from_request(request)

    @override_settings(ALLOWED_HOSTS=["app.tawtheeq-ksa.com", "www.tawtheeq-ksa.com", "tawtheeq-ksa.com"])
    def test_configured_rp_id_used_for_subdomains(self):
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"WEBAUTHN_RP_ID": "tawtheeq-ksa.com"}):
            self.assertEqual(self._rp_id("app.tawtheeq-ksa.com"), "tawtheeq-ksa.com")
            self.assertEqual(self._rp_id("www.tawtheeq-ksa.com"), "tawtheeq-ksa.com")
            self.assertEqual(self._rp_id("tawtheeq-ksa.com"), "tawtheeq-ksa.com")

    @override_settings(ALLOWED_HOSTS=["example-unrelated-host.com"])
    def test_configured_rp_id_ignored_for_unrelated_host(self):
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"WEBAUTHN_RP_ID": "tawtheeq-ksa.com"}):
            self.assertEqual(self._rp_id("example-unrelated-host.com"), "example-unrelated-host.com")

    @override_settings(ALLOWED_HOSTS=["testserver"])
    def test_falls_back_to_host_without_config(self):
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WEBAUTHN_RP_ID", None)
            self.assertEqual(self._rp_id("testserver"), "testserver")


class PasskeyUserVerificationTests(TestCase):
    def _authenticator_data(self, rp_id: str, flags: int) -> bytes:
        return hashlib.sha256(rp_id.encode("utf-8")).digest() + bytes([flags]) + (0).to_bytes(4, "big")

    def test_user_verification_flag_is_required_when_requested(self):
        with self.assertRaisesRegex(ValueError, "user_verification_required"):
            parse_authenticator_data(
                self._authenticator_data("testserver", 0x01),
                rp_id="testserver",
                require_user_verification=True,
            )

    def test_verified_user_flag_is_accepted(self):
        parsed = parse_authenticator_data(
            self._authenticator_data("testserver", 0x05),
            rp_id="testserver",
            require_user_verification=True,
        )

        self.assertEqual(parsed.flags, 0x05)
