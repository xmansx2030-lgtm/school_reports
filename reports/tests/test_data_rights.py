# -*- coding: utf-8 -*-
"""حقوق صاحب البيانات: نسخةٌ كاملة عنه، ولا حرفٌ عن غيره، ولا سرٌّ فيها.

سياسة الخصوصية تَعِد بـ«الوصول، وطلب نسخة مقروءة… وطلب الإتلاف في الحالات
المقررة». والوعد الآن منفَّذ في الكود، فيجب أن يُحرَس فيه:

* **الشمول** — نسخةٌ ناقصة تُخلّ بالحق الذي وُعد به.
* **الحصر** — بيانات شخصٍ آخر في نسختي تسريبٌ يرتكبه الحق نفسه.
* **الأسرار** — كلمة المرور ومفاتيح المصادقة ومفاتيح الدفع ليست «بيانات
  شخصية تُسلَّم»: تسليمها يخلق الخطر الذي جاء الحق ليحمي منه.
"""
from __future__ import annotations

import json
from datetime import timedelta

from django.core import mail
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from reports.models import (
    AiUsageEvent,
    CircularDraft,
    ErasureRequest,
    Notification,
    NotificationRecipient,
    Plan,
    Report,
    ReportType,
    School,
    SchoolMembership,
    SchoolSubscription,
    Initiative,
    SubscriptionPlan,
    Teacher,
    TeacherPrivateComment,
    TeacherTotpDevice,
    TotpRecoveryCode,
    Ticket,
    WebAuthnCredential,
)
from reports.services_data_rights import FORBIDDEN_KEYS, build_personal_data_export


def _walk_keys(node):
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from _walk_keys(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_keys(item)


def _walk_values(node):
    if isinstance(node, dict):
        for value in node.values():
            yield from _walk_values(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_values(item)
    elif node is not None:
        yield str(node)


@override_settings(ALLOWED_HOSTS=["testserver"])
class PersonalDataExportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        plan = SubscriptionPlan.objects.create(
            name="Plan", price=0, days_duration=30, max_teachers=20
        )
        cls.school = School.objects.create(name="مدرسة", code="rights-school")
        SchoolSubscription.objects.create(school=cls.school, plan=plan)
        cls.category = ReportType.objects.create(
            name="نشاط", code="rights-kind", school=cls.school
        )

        cls.subject = Teacher.objects.create_user(
            phone="500600001", name="صاحب البيانات", password="secret-pass-1",
            national_id="1122334455", email="subject@example.com",
        )
        SchoolMembership.objects.create(
            school=cls.school, teacher=cls.subject,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        cls.other = Teacher.objects.create_user(
            phone="500600002", name="شخص آخر", password="secret-pass-2",
            national_id="9988776655",
        )
        SchoolMembership.objects.create(
            school=cls.school, teacher=cls.other,
            role_type=SchoolMembership.RoleType.TEACHER,
        )

        Report.objects.create(
            school=cls.school, teacher=cls.subject, category=cls.category,
            title="تقريري أنا", idea="فكرة", report_date="2026-06-01",
        )
        Report.objects.create(
            school=cls.school, teacher=cls.other, category=cls.category,
            title="تقرير غيري", idea="فكرة", report_date="2026-06-02",
        )
        CircularDraft.objects.create(
            school=cls.school, owner=cls.subject, title="تعميمي أنا", body="محتواي",
        )
        CircularDraft.objects.create(
            school=cls.school, owner=cls.other, title="تعميم غيري", body="محتوى غيري",
        )
        own_plan = Plan.objects.create(
            scope=Plan.Scope.SCHOOL, school=cls.school, owner=cls.subject,
            title="خطتي أنا", description="وصف خطتي",
        )
        Plan.objects.create(
            scope=Plan.Scope.SCHOOL, school=cls.school, owner=cls.other,
            title="خطة غيري", description="وصف غيري",
        )
        Initiative.objects.create(
            school=cls.school, teacher=cls.subject, plan=own_plan,
            title="مبادرتي أنا", summary="أثري",
        )
        Initiative.objects.create(
            school=cls.school, teacher=cls.other,
            title="مبادرة غيري", summary="أثر غيري",
        )
        AiUsageEvent.objects.create(
            school=cls.school, teacher=cls.subject,
            stage=AiUsageEvent.Stage.REPORT_IMPROVE, model_name="my-model",
        )
        AiUsageEvent.objects.create(
            school=cls.school, teacher=cls.other,
            stage=AiUsageEvent.Stage.REPORT_REVIEW, model_name="other-model",
        )
        Ticket.objects.create(
            school=cls.school, creator=cls.subject, is_platform=False,
            title="طلبي أنا", body="نص",
        )
        Ticket.objects.create(
            school=cls.school, creator=cls.other, is_platform=False,
            title="طلب غيري", body="نص",
        )
        mine = Notification.objects.create(
            title="إشعار لي", message="نص", school=cls.school
        )
        NotificationRecipient.objects.create(notification=mine, teacher=cls.subject)
        theirs = Notification.objects.create(
            title="إشعار لغيري", message="نص", school=cls.school
        )
        NotificationRecipient.objects.create(notification=theirs, teacher=cls.other)

    # ── الشمول ──────────────────────────────────────────────────────────

    def test_the_export_contains_the_subjects_own_content(self):
        export = build_personal_data_export(self.subject)
        blob = json.dumps(export, ensure_ascii=False)

        self.assertEqual(export["sections"]["profile"]["name"], "صاحب البيانات")
        self.assertIn("تقريري أنا", blob)
        self.assertIn("طلبي أنا", blob)
        self.assertIn("إشعار لي", blob)
        self.assertIn("تعميمي أنا", blob)
        self.assertIn("خطتي أنا", blob)
        self.assertIn("مبادرتي أنا", blob)
        self.assertIn("my-model", blob)
        self.assertEqual(export["incomplete_sections"], [])

    def test_every_declared_section_is_present(self):
        from reports.services_data_rights import SECTIONS

        export = build_personal_data_export(self.subject)
        for name, _builder in SECTIONS:
            self.assertIn(name, export["sections"], f"قسم ناقص: {name}")

    # ── الحصر ───────────────────────────────────────────────────────────

    def test_no_other_persons_content_appears(self):
        blob = json.dumps(build_personal_data_export(self.subject), ensure_ascii=False)

        self.assertNotIn("تقرير غيري", blob)
        self.assertNotIn("طلب غيري", blob)
        self.assertNotIn("إشعار لغيري", blob)
        self.assertNotIn("9988776655", blob)
        self.assertNotIn("شخص آخر", blob)
        self.assertNotIn("تعميم غيري", blob)
        self.assertNotIn("خطة غيري", blob)
        self.assertNotIn("مبادرة غيري", blob)
        self.assertNotIn("other-model", blob)

    # ── الأسرار ─────────────────────────────────────────────────────────

    def test_no_forbidden_key_appears_anywhere_in_the_export(self):
        export = build_personal_data_export(self.subject)
        leaked = sorted(set(_walk_keys(export)) & FORBIDDEN_KEYS)

        self.assertEqual(leaked, [], f"مفاتيح محظورة في النسخة: {leaked}")

    def test_the_password_hash_never_leaves(self):
        """تسليم التجزئة تسليمُ الحساب لمن يكسرها دون اتصال."""
        blob = json.dumps(build_personal_data_export(self.subject), ensure_ascii=False)

        self.subject.refresh_from_db()
        self.assertNotIn(self.subject.password, blob)
        self.assertNotIn("pbkdf2", blob)
        self.assertNotIn("bcrypt", blob)

    def test_passkey_material_is_withheld_but_its_existence_is_disclosed(self):
        WebAuthnCredential.objects.create(
            teacher=self.subject,
            credential_id=b"raw-credential-id",
            credential_id_hash="a" * 64,
            public_key_cose=b"super-secret-key-material",
            device_name="جوالي",
        )
        export = build_personal_data_export(self.subject)
        blob = json.dumps(export, ensure_ascii=False)

        # حق العلم محفوظ: يعرف أن لديه مفتاحاً وباسم جهازه.
        self.assertIn("جوالي", blob)
        # ومادة المصادقة لا تُسلَّم.
        self.assertNotIn("a" * 64, blob)
        self.assertNotIn("super-secret-key-material", blob)

    def test_totp_is_disclosed_without_secret_or_recovery_hashes(self):
        device = TeacherTotpDevice.objects.create(
            teacher=self.subject,
            secret_encrypted="encrypted-secret-material",
            confirmed_at=timezone.now(),
        )
        TotpRecoveryCode.objects.create(device=device, code_hash="f" * 64)
        export = build_personal_data_export(self.subject)
        security = export["sections"]["security"]["two_factor_authentication"]
        blob = json.dumps(export, ensure_ascii=False)

        self.assertTrue(security["is_confirmed"])
        self.assertEqual(security["recovery_codes_available"], 1)
        self.assertNotIn("encrypted-secret-material", blob)
        self.assertNotIn("f" * 64, blob)

    def test_private_notes_are_counted_but_not_quoted(self):
        """نصّ الملاحظة رأيُ طرفٍ آخر — يُعلَم بوجودها لا بمحتواها."""
        TeacherPrivateComment.objects.create(
            teacher=self.subject, created_by=self.other, school=self.school,
            body="ملاحظة إدارية حسّاسة جداً",
        )
        export = build_personal_data_export(self.subject)
        blob = json.dumps(export, ensure_ascii=False)

        self.assertEqual(export["sections"]["notes_about_me"]["count"], 1)
        self.assertNotIn("ملاحظة إدارية حسّاسة جداً", blob)

    def test_no_value_looks_like_a_django_session_or_hash(self):
        blob_values = list(_walk_values(build_personal_data_export(self.subject)))
        for value in blob_values:
            self.assertFalse(
                value.startswith(("pbkdf2_", "argon2", "bcrypt$")),
                f"قيمة تشبه تجزئة كلمة مرور: {value[:24]}",
            )


@override_settings(
    ALLOWED_HOSTS=["testserver"],
    RATELIMIT_ENABLE=False,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="noreply@example.com",
)
class DataRightsEndpointTests(TestCase):
    def setUp(self):
        plan = SubscriptionPlan.objects.create(
            name="Plan", price=0, days_duration=30, max_teachers=10
        )
        self.school = School.objects.create(name="مدرسة", code="rights-endpoint")
        SchoolSubscription.objects.create(school=self.school, plan=plan)
        self.user = Teacher.objects.create_user(
            phone="500700001", name="معلم", password="pass", email="teacher@example.com"
        )
        SchoolMembership.objects.create(
            school=self.school, teacher=self.user,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        self.client.force_login(self.user)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()

    def test_the_page_renders(self):
        response = self.client.get(reverse("reports:my_data"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "عرض نسختي المقروءة")

    def test_download_is_an_attachment_and_never_cached(self):
        response = self.client.get(reverse("reports:my_data_download"))

        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers["Content-Disposition"])
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertIn("noindex", response.headers["X-Robots-Tag"])
        payload = json.loads(response.content.decode("utf-8"))
        self.assertEqual(payload["subject"], "معلم")

    def test_the_download_requires_login(self):
        self.client.logout()
        response = self.client.get(reverse("reports:my_data_download"))

        self.assertIn(response.status_code, {302, 403})

    def test_readable_copy_is_private_and_contains_arabic_sections(self):
        response = self.client.get(reverse("reports:my_data_readable"))

        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertContains(response, "نسخة بيانات معلم")
        self.assertContains(response, "الملف الشخصي")
        self.assertContains(response, "رقم الجوال")

    def test_an_erasure_request_is_recorded(self):
        response = self.client.post(
            reverse("reports:request_erasure"),
            {"reason": "لم أعد أعمل هنا", "acknowledge_review": "yes"},
        )

        self.assertEqual(response.status_code, 302)
        record = ErasureRequest.objects.get(teacher=self.user)
        self.assertEqual(record.status, ErasureRequest.Status.RECEIVED)
        self.assertEqual(record.reason, "لم أعد أعمل هنا")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("استلام طلب إتلاف", mail.outbox[0].subject)

    def test_acknowledgement_is_required_server_side(self):
        response = self.client.post(
            reverse("reports:request_erasure"), {"reason": "طلب بلا إقرار"}
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(ErasureRequest.objects.filter(teacher=self.user).exists())

    def test_resending_does_not_create_a_second_open_request(self):
        """طلبان مفتوحان يُشتّتان المعالجة، والقيد في القاعدة يمنعهما."""
        self.client.post(reverse("reports:request_erasure"), {"reason": "أول", "acknowledge_review": "yes"})
        self.client.post(reverse("reports:request_erasure"), {"reason": "ثانٍ", "acknowledge_review": "yes"})

        self.assertEqual(ErasureRequest.objects.filter(teacher=self.user).count(), 1)

    def test_erasure_is_a_request_not_an_immediate_deletion(self):
        """الحساب يبقى: المحتوى مدرسي وسجلّ التدقيق مقصودٌ بقاؤه."""
        self.client.post(reverse("reports:request_erasure"), {"reason": "طلب", "acknowledge_review": "yes"})

        self.user.refresh_from_db()
        self.assertTrue(Teacher.objects.filter(pk=self.user.pk).exists())
        self.assertTrue(self.user.is_active)

    def test_the_request_form_is_hidden_while_one_is_open(self):
        self.client.post(reverse("reports:request_erasure"), {"reason": "طلب", "acknowledge_review": "yes"})
        response = self.client.get(reverse("reports:my_data"))

        self.assertContains(response, "الطلب #")
        self.assertContains(response, "مستلَم")
        self.assertNotContains(response, "إرسال الطلب للمراجعة")

    def test_deadline_and_extension_are_bounded(self):
        record = ErasureRequest.objects.create(teacher=self.user)

        self.assertEqual(record.response_due_at, record.created_at + timedelta(days=30))
        record.extended_until = record.response_due_at + timedelta(days=31)
        record.extension_reason = "ازدحام موثق"
        with self.assertRaises(ValidationError):
            record.full_clean()

        record.extended_until = record.response_due_at + timedelta(days=15)
        record.full_clean()

    def test_closed_request_requires_a_response_note(self):
        record = ErasureRequest(
            teacher=self.user,
            status=ErasureRequest.Status.REFUSED,
        )
        with self.assertRaises(ValidationError):
            record.full_clean()

    def test_completed_request_requires_execution_evidence(self):
        record = ErasureRequest(
            teacher=self.user,
            status=ErasureRequest.Status.COMPLETED,
            response_note="تم تنفيذ طلبك.",
        )
        with self.assertRaises(ValidationError):
            record.full_clean()

        record.execution_evidence = "أُتلفت بيانات الملف الاختيارية، واستُبقي سجل التدقيق. مرجع 42."
        record.full_clean()
