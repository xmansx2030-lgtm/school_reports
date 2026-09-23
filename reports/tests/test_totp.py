# -*- coding: utf-8 -*-
"""العامل الثاني: يمنع من يملك كلمة المرور وحدها، ولا يقفل صاحبه خارج حسابه.

الفحوص هنا تُقسَّم إلى:

* **الخوارزمية** — RFC 6238 منفَّذ هنا لا مستورَداً، فيجب أن يُقاس بمتّجهات
  معروفة لا بأنه «يعمل عندي».
* **إعادة الاستعمال** — كلمة مرورٍ *لمرة واحدة* تعني أن الرمز لا يُقبل مرتين.
  وبدون ذلك تبقى صالحةً ثلاثين ثانية بعد التقاطها.
* **البوّابة** — لا تُنشأ جلسة مصادَق عليها قبل العامل الثاني، مهما كان فرع
  الدخول.
* **الاسترجاع** — من فقد هاتفه يدخل، ورمزُه يُستهلك مرة واحدة.
* **السرّ** — مُعمّى في القاعدة، فتسريب نسخةٍ منها لا يُبطل الحماية للجميع.
"""
from __future__ import annotations

import time

from django.test import TestCase, override_settings
from django.urls import reverse

from reports.models import (
    School,
    SchoolMembership,
    SchoolSubscription,
    SubscriptionPlan,
    Teacher,
    TeacherTotpDevice,
    TotpRecoveryCode,
)
from reports.totp import (
    TOTP_PERIOD_SECONDS,
    code_for_counter,
    decrypt_secret,
    encrypt_secret,
    generate_recovery_codes,
    generate_secret,
    hash_recovery_code,
    provisioning_uri,
    verify_code,
)


class TotpAlgorithmTests(TestCase):
    # متّجه RFC 4226 المرجعي: السرّ "12345678901234567890" بترميز Base32.
    RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    RFC_EXPECTED = [
        "755224", "287082", "359152", "969429", "338314",
        "254676", "287922", "162583", "399871", "520489",
    ]

    def test_matches_the_rfc_reference_vectors(self):
        """خوارزمية مكتوبة هنا تُقاس بمرجعها لا بانطباع."""
        for counter, expected in enumerate(self.RFC_EXPECTED):
            with self.subTest(counter=counter):
                self.assertEqual(code_for_counter(self.RFC_SECRET, counter), expected)

    def test_a_current_code_verifies(self):
        secret = generate_secret()
        now = time.time()
        code = code_for_counter(secret, int(now // TOTP_PERIOD_SECONDS))

        self.assertIsNotNone(verify_code(secret, code, moment=now))

    def test_clock_drift_of_one_step_is_tolerated(self):
        """ساعةُ الهاتف تنحرف؛ ورفضُ رمزٍ صحيح يدفع المستخدم لتعطيل الحماية."""
        secret = generate_secret()
        now = time.time()
        current = int(now // TOTP_PERIOD_SECONDS)

        for drift in (-1, 0, 1):
            with self.subTest(drift=drift):
                code = code_for_counter(secret, current + drift)
                self.assertIsNotNone(verify_code(secret, code, moment=now))

    def test_a_far_code_is_refused(self):
        secret = generate_secret()
        now = time.time()
        stale = code_for_counter(secret, int(now // TOTP_PERIOD_SECONDS) - 5)

        self.assertIsNone(verify_code(secret, stale, moment=now))

    def test_a_used_counter_is_never_accepted_again(self):
        """هذا هو الفرق بين «لمرة واحدة» و«قصيرة العمر»."""
        secret = generate_secret()
        now = time.time()
        counter = int(now // TOTP_PERIOD_SECONDS)
        code = code_for_counter(secret, counter)

        self.assertEqual(verify_code(secret, code, moment=now), counter)
        self.assertIsNone(
            verify_code(secret, code, last_used_counter=counter, moment=now)
        )

    def test_malformed_input_is_refused(self):
        secret = generate_secret()
        for bad in ("", "abc", "12345", "1234567", None):
            with self.subTest(value=bad):
                self.assertIsNone(verify_code(secret, bad))

    def test_the_provisioning_uri_is_well_formed(self):
        uri = provisioning_uri("ABCDEF", account="500111222", issuer="منصة توثيق")

        self.assertTrue(uri.startswith("otpauth://totp/"))
        self.assertIn("secret=ABCDEF", uri)
        self.assertIn("digits=6", uri)
        self.assertIn("period=30", uri)


class TotpSecretStorageTests(TestCase):
    def test_the_secret_round_trips_but_is_not_stored_in_the_clear(self):
        secret = generate_secret()
        token = encrypt_secret(secret)

        self.assertNotIn(secret, token)
        self.assertEqual(decrypt_secret(token), secret)

    def test_a_changed_encryption_key_fails_closed(self):
        """سرٌّ لا يمكن فكّه لا يُقبل: عاملٌ ثانٍ لا يُتحقَّق منه ليس عاملاً."""
        token = encrypt_secret(generate_secret())

        with override_settings(TOTP_SECRET_ENCRYPTION_KEY="a-completely-different-key"):
            self.assertIsNone(decrypt_secret(token))

    def test_recovery_codes_are_stored_hashed_only(self):
        codes = generate_recovery_codes()
        self.assertEqual(len(codes), 10)
        self.assertEqual(len(set(codes)), 10)

        for code in codes:
            self.assertNotEqual(hash_recovery_code(code), code)

    def test_recovery_codes_normalise_formatting(self):
        """المستخدم ينسخها بشرطات أو بدونها — ولا يُعاقَب على ذلك."""
        code = "ab12-cd34-ef56"
        self.assertEqual(hash_recovery_code(code), hash_recovery_code("AB12CD34EF56"))


@override_settings(ALLOWED_HOSTS=["testserver"], RATELIMIT_ENABLE=False)
class TotpLoginGateTests(TestCase):
    def setUp(self):
        plan = SubscriptionPlan.objects.create(
            name="Plan", price=0, days_duration=30, max_teachers=10
        )
        self.school = School.objects.create(name="مدرسة", code="totp-school")
        SchoolSubscription.objects.create(school=self.school, plan=plan)
        self.user = Teacher.objects.create_user(
            phone="500900001", name="معلم", password="StrongPass!234"
        )
        SchoolMembership.objects.create(
            school=self.school, teacher=self.user,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        self.secret = generate_secret()
        self.device = TeacherTotpDevice.objects.create(
            teacher=self.user,
            secret_encrypted=encrypt_secret(self.secret),
            confirmed_at="2026-01-01T00:00:00+00:00",
        )

    def _current_code(self) -> str:
        return code_for_counter(self.secret, int(time.time() // TOTP_PERIOD_SECONDS))

    def _submit_password(self):
        return self.client.post(
            reverse("reports:login"),
            {"identifier": "500900001", "password": "StrongPass!234"},
        )

    def test_password_alone_does_not_create_a_session(self):
        """جوهر العامل الثاني كلّه."""
        response = self._submit_password()

        self.assertRedirects(
            response, reverse("reports:totp_challenge"), fetch_redirect_response=False
        )
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_the_challenge_page_cannot_be_reached_without_passing_the_password(self):
        response = self.client.get(reverse("reports:totp_challenge"))

        self.assertRedirects(
            response, reverse("reports:login"), fetch_redirect_response=False
        )

    def test_a_correct_code_completes_the_login(self):
        self._submit_password()
        response = self.client.post(
            reverse("reports:totp_challenge"), {"code": self._current_code()}
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("_auth_user_id", self.client.session)
        self.assertEqual(
            int(self.client.session["_auth_user_id"]), self.user.pk
        )

    def test_a_wrong_code_does_not_log_in(self):
        self._submit_password()
        response = self.client.post(reverse("reports:totp_challenge"), {"code": "000000"})

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_direct_module_url_cannot_bypass_the_pending_challenge(self):
        self._submit_password()

        response = self.client.get(reverse("reports:home"))

        self.assertRedirects(
            response,
            f'{reverse("reports:login")}?next={reverse("reports:home")}',
            fetch_redirect_response=False,
        )
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_a_code_cannot_be_replayed_in_a_new_session(self):
        """الرمز المُلتقَط لا يُعاد استعماله داخل نافذته."""
        code = self._current_code()
        self._submit_password()
        self.client.post(reverse("reports:totp_challenge"), {"code": code})
        self.assertIn("_auth_user_id", self.client.session)

        self.client.logout()
        self._submit_password()
        self.client.post(reverse("reports:totp_challenge"), {"code": code})

        self.assertNotIn("_auth_user_id", self.client.session)

    def test_a_recovery_code_works_once(self):
        codes = generate_recovery_codes(2)
        TotpRecoveryCode.objects.bulk_create(
            [TotpRecoveryCode(device=self.device, code_hash=hash_recovery_code(c)) for c in codes]
        )

        self._submit_password()
        self.client.post(reverse("reports:totp_challenge"), {"code": codes[0]})
        self.assertIn("_auth_user_id", self.client.session)

        self.client.logout()
        self._submit_password()
        self.client.post(reverse("reports:totp_challenge"), {"code": codes[0]})
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_an_unconfirmed_device_does_not_gate_login(self):
        """تسجيلٌ بدأ ولم يكتمل لا يقفل صاحبه خارج حسابه."""
        self.device.confirmed_at = None
        self.device.save(update_fields=["confirmed_at"])

        self._submit_password()
        self.assertIn("_auth_user_id", self.client.session)


@override_settings(ALLOWED_HOSTS=["testserver"])
class TotpEnrollmentTests(TestCase):
    def setUp(self):
        plan = SubscriptionPlan.objects.create(
            name="Plan", price=0, days_duration=30, max_teachers=10
        )
        self.school = School.objects.create(name="مدرسة", code="totp-enroll")
        SchoolSubscription.objects.create(school=self.school, plan=plan)
        self.user = Teacher.objects.create_user(
            phone="500900002", name="معلم", password="StrongPass!234"
        )
        SchoolMembership.objects.create(
            school=self.school, teacher=self.user,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        self.client.force_login(self.user)

    def test_enrollment_requires_proving_a_working_code(self):
        """بلا إثبات يُقفل من أخطأ في الإعداد خارج حسابه فوراً."""
        self.client.post(reverse("reports:totp_begin_enrollment"))
        secret = self.client.session["_totp_enroll_secret"]

        self.client.post(reverse("reports:totp_confirm_enrollment"), {"code": "000000"})
        self.assertFalse(
            TeacherTotpDevice.objects.filter(
                teacher=self.user, confirmed_at__isnull=False
            ).exists()
        )

        code = code_for_counter(secret, int(time.time() // TOTP_PERIOD_SECONDS))
        self.client.post(reverse("reports:totp_confirm_enrollment"), {"code": code})
        self.assertTrue(
            TeacherTotpDevice.objects.filter(
                teacher=self.user, confirmed_at__isnull=False
            ).exists()
        )

    def test_confirming_issues_recovery_codes_once(self):
        self.client.post(reverse("reports:totp_begin_enrollment"))
        secret = self.client.session["_totp_enroll_secret"]
        code = code_for_counter(secret, int(time.time() // TOTP_PERIOD_SECONDS))
        self.client.post(reverse("reports:totp_confirm_enrollment"), {"code": code})

        device = TeacherTotpDevice.objects.get(teacher=self.user)
        self.assertEqual(device.recovery_codes.count(), 10)

        # تُعرض مرة واحدة ثم تختفي من الجلسة.
        first = self.client.get(reverse("reports:totp_settings"))
        self.assertIsNotNone(first.context["new_recovery_codes"])
        second = self.client.get(reverse("reports:totp_settings"))
        self.assertIsNone(second.context["new_recovery_codes"])

    def test_disabling_requires_the_password(self):
        """بلا كلمة مرور يصير التعطيل أضعف من التفعيل."""
        TeacherTotpDevice.objects.create(
            teacher=self.user,
            secret_encrypted=encrypt_secret(generate_secret()),
            confirmed_at="2026-01-01T00:00:00+00:00",
        )

        self.client.post(reverse("reports:totp_disable"), {"password": "wrong"})
        self.assertTrue(TeacherTotpDevice.objects.filter(teacher=self.user).exists())

        self.client.post(reverse("reports:totp_disable"), {"password": "StrongPass!234"})
        self.assertFalse(TeacherTotpDevice.objects.filter(teacher=self.user).exists())

    def test_the_settings_page_never_shows_the_secret(self):
        secret = generate_secret()
        TeacherTotpDevice.objects.create(
            teacher=self.user,
            secret_encrypted=encrypt_secret(secret),
            confirmed_at="2026-01-01T00:00:00+00:00",
        )
        response = self.client.get(reverse("reports:totp_settings"))

        self.assertNotContains(response, secret)
