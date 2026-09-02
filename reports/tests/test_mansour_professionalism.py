"""Behaviour a customer would call unprofessional, locked down as tests.

Every case here failed before: the assistant answered an unrelated question with
a sales pitch, recited the closest article to a question nobody documented, took
a pasted password in its stride, or opened a support ticket for a task it could
have walked the customer through.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from reports.mansour_assistant import (
    INTENT_CONTACT,
    INTENT_GENERAL,
    INTENT_OUT_OF_SCOPE,
    INTENT_PRICING,
    INTENT_SENSITIVE_DISCLOSURE,
    INTENT_SUPPORT,
    INTENT_VALUE,
    MIN_CONFIDENT_RETRIEVAL_SCORE,
    _detect_customer_intent,
    ask_mansour,
    contains_shared_secret,
    retrieval_confidence,
    select_knowledge,
)
from reports.mansour_quality import grade_answer


@override_settings(OPENAI_API_KEY="", MANSOUR_ASSISTANT_ENABLED=False)
class MansourIntentScopingTests(SimpleTestCase):
    """A word such as "اشتراك" is not by itself a request for a quotation."""

    def test_subscription_words_do_not_turn_every_question_into_a_price_pitch(self):
        for question in (
            "كم معلم أقدر أضيف في الباقة الحالية؟",
            "من يملك البيانات لو وقفنا الاشتراك؟",
            "كم يأخذ تفعيل الاشتراك بعد الدفع؟",
        ):
            with self.subTest(question=question):
                self.assertNotEqual(_detect_customer_intent(question), INTENT_PRICING)

    def test_a_real_price_question_is_still_priced(self):
        for question in ("كم سعر الباقة السنوية؟", "وش أسعار الباقات عندكم؟", "كيف أشترك؟"):
            with self.subTest(question=question):
                self.assertEqual(_detect_customer_intent(question), INTENT_PRICING)

    def test_export_request_is_not_answered_with_the_privacy_statement(self):
        intent = _detect_customer_intent("أبي تصدير لكل بيانات المدرسة قبل نهاية السنة")

        self.assertEqual(intent, INTENT_GENERAL)
        self.assertIn(
            "manager-export",
            [item.slug for item in select_knowledge(
                "أبي تصدير لكل بيانات المدرسة قبل نهاية السنة", audience="manager"
            )[:2]],
        )

    def test_asking_for_a_channel_is_a_contact_request_not_an_objection(self):
        self.assertEqual(
            _detect_customer_intent("ممكن رقم تواصل أو رقم واتساب للدعم؟"),
            INTENT_CONTACT,
        )
        # ...while naming WhatsApp as the tool the school already uses is one.
        self.assertEqual(
            _detect_customer_intent("عندنا قروبات واتساب ومجلدات درايف، وش الفرق؟"),
            INTENT_VALUE,
        )

    def test_everyday_fault_wording_is_recognised_as_a_problem(self):
        for question in (
            "المنصة بطيئة عندنا مرة",
            "المعلم يقول ما وصله التعميم",
            "ضاع مني ملف الإنجاز",
            "الصور تطلع مقلوبة في الطباعة",
        ):
            with self.subTest(question=question):
                self.assertEqual(_detect_customer_intent(question), INTENT_SUPPORT)

    def test_opinions_about_outside_bodies_are_declined(self):
        self.assertEqual(
            _detect_customer_intent("ايش رايك في وزارة التعليم؟"), INTENT_OUT_OF_SCOPE
        )
        # An opinion about the product itself stays in scope.
        self.assertNotEqual(
            _detect_customer_intent("وش رايك في الباقة المناسبة لمدرستنا؟"),
            INTENT_OUT_OF_SCOPE,
        )


@override_settings(OPENAI_API_KEY="", MANSOUR_ASSISTANT_ENABLED=False)
class MansourHonestyTests(SimpleTestCase):
    """Reciting the nearest article is how an assistant is confident and wrong."""

    def test_undocumented_questions_are_answered_honestly(self):
        for question, audience in (
            ("وش الفرق بينكم وبين نظام نور؟", "general"),
            ("هل بياناتنا مخزنة داخل السعودية أم خارجها؟", "general"),
        ):
            with self.subTest(question=question):
                answer, _sources = ask_mansour(question, plans=[], audience=audience)
                self.assertIn("ما عندي معلومة موثقة", answer)

    def test_documented_questions_keep_their_documented_answer(self):
        answer, sources = ask_mansour(
            "كيف أنشئ تقرير نشاط وأرفق له الصور؟", plans=[], audience="teacher"
        )

        self.assertNotIn("ما عندي معلومة موثقة", answer)
        self.assertTrue(sources[0]["url"].endswith("/guide/#teacher-report"))

    def test_confidence_separates_covered_from_uncovered_questions(self):
        covered = retrieval_confidence("كيف أضيف تقرير جديد؟", audience="teacher")
        uncovered = retrieval_confidence("هل المنصة معتمدة من وزارة التعليم؟", audience="general")

        self.assertGreaterEqual(covered, MIN_CONFIDENT_RETRIEVAL_SCORE)
        self.assertLess(uncovered, MIN_CONFIDENT_RETRIEVAL_SCORE)

    def test_undocumented_destructive_request_is_not_answered_from_a_neighbour(self):
        answer, _sources = ask_mansour(
            "أبي أحذف معلم نقل من المدرسة وش أسوي؟", plans=[], audience="manager"
        )

        self.assertIn("ما عندي معلومة موثقة", answer)
        self.assertNotIn("إضافة مدرسة أخرى", answer)

    def test_a_follow_up_with_no_subject_asks_instead_of_guessing(self):
        answer, sources = ask_mansour("وين ألقاها؟", plans=[], audience="teacher")

        self.assertIn("ما وصلني الموضوع", answer)
        self.assertEqual(sources, [])

    def test_the_same_follow_up_is_resolved_from_history(self):
        answer, _sources = ask_mansour(
            "وين ألقاها؟",
            history=[{"role": "user", "content": "كيف أنشئ ملف الإنجاز؟"}],
            plans=[],
            audience="teacher",
        )

        self.assertNotIn("ما وصلني الموضوع", answer)
        self.assertIn("الإنجاز", answer)

    def test_complete_capability_question_is_not_mistaken_for_a_follow_up(self):
        answer, sources = ask_mansour(
            "هل أقدر أنظم اجتماع وأكتب محضرًا رسميًا وأنزله PDF؟",
            plans=[],
            audience="manager",
        )

        self.assertNotIn("ما وصلني الموضوع", answer)
        self.assertIn("المحضر", answer)
        self.assertIn("PDF", answer)
        self.assertEqual(sources[0]["url"], "https://tawtheeq-ksa.com/guide/#manager-meetings")

    def test_public_deputy_question_explains_boundaries_without_forcing_two_roles(self):
        answer, _sources = ask_mansour(
            "أنا وكيل مدرسة، ما الذي أستطيع عمله وما الذي يبقى للمدير؟",
            plans=[],
            audience="general",
        )

        self.assertIn("الوكيل", answer)
        self.assertIn("الاعتماد النهائي", answer)
        self.assertNotIn("معلم أم مدير مدرسة", answer)


@override_settings(OPENAI_API_KEY="test-secret-key", MANSOUR_ASSISTANT_ENABLED=True)
class MansourSharedSecretTests(SimpleTestCase):
    """A pasted credential is an incident, and it must not be forwarded anywhere."""

    def test_pasted_credentials_are_recognised(self):
        for question in (
            "كلمة مروري 123456 ممكن تغيرها لي؟",
            "رمز التحقق هو 448921 ليش ما اشتغل؟",
            "رقم هويتي 1012345678 سجلوني",
        ):
            with self.subTest(question=question):
                self.assertTrue(contains_shared_secret(question))

    def test_ordinary_questions_are_not_flagged(self):
        for question in (
            "نسيت كلمة المرور كيف أستعيدها؟",
            "ما وصلني رمز التحقق على الجوال",
            "كم سعر الباقة السنوية؟",
        ):
            with self.subTest(question=question):
                self.assertFalse(contains_shared_secret(question))

    def test_a_pasted_password_never_reaches_the_model_provider(self):
        with patch("reports.ai_client.urlopen") as mocked_urlopen:
            answer, sources = ask_mansour(
                "كلمة مروري 123456 ممكن تغيرها لي؟", plans=[], audience="teacher"
            )

        mocked_urlopen.assert_not_called()
        self.assertIn("لا ترسل كلمة المرور", answer)
        self.assertIn("غيّر", answer)
        self.assertTrue(any(source["url"] == "/password-reset/" for source in sources))
        self.assertEqual(_detect_customer_intent("كلمة مروري 123456"), INTENT_SENSITIVE_DISCLOSURE)


@override_settings(OPENAI_API_KEY="", MANSOUR_ASSISTANT_ENABLED=False)
class MansourSalesRestraintTests(SimpleTestCase):
    """Nobody with a broken upload wants to hear what the platform can do for them."""

    def test_an_operational_question_is_not_answered_with_the_marketing_article(self):
        for question, audience in (
            ("أبي أحذف معلم من المدرسة", "manager"),
            ("المنصة بطيئة عندنا مرة", "manager"),
        ):
            with self.subTest(question=question):
                slugs = [
                    item.slug
                    for item in select_knowledge(question, audience=audience)[:2]
                ]
                self.assertNotIn("manager-benefits", slugs)
                self.assertNotIn("marketing-value", slugs)

    def test_a_value_question_still_reaches_the_marketing_article(self):
        slugs = [
            item.slug
            for item in select_knowledge(
                "وش الفايدة اللي بنشوفها من منصة توثيق؟", audience="manager"
            )[:3]
        ]

        self.assertTrue({"manager-benefits", "marketing-value"} & set(slugs))

    def test_the_rubric_flags_a_pitch_inside_a_support_answer(self):
        report = grade_answer(
            "تساعد منصة توثيق مدير المدرسة على توفير الوقت والجهد وتحسين جودة التوثيق.",
            audience="manager",
            kind="support",
        )

        self.assertEqual(report.dimension("sales_restraint").score, 0.0)

    def test_the_rubric_allows_a_pitch_inside_an_objection_answer(self):
        report = grade_answer(
            "أفهم وجهة نظرك. ابدأ بالتجربة المجانية وقارن النتيجة بطريقتكم الحالية.",
            audience="manager",
            kind="emotional",
        )

        self.assertFalse(report.dimension("sales_restraint").applicable)


@override_settings(OPENAI_API_KEY="", MANSOUR_ASSISTANT_ENABLED=False)
class MansourSupportRoutingTests(SimpleTestCase):
    """A ticket is the answer when nothing is documented — not before."""

    def test_a_documented_task_is_walked_through_instead_of_ticketed(self):
        answer, _sources = ask_mansour(
            "حفظت التقرير بتاريخ خطأ وأبغى أعدله", plans=[], audience="teacher"
        )

        self.assertIn("تعديل", answer)
        self.assertNotIn("سجّل التذكرة", answer)

    def test_an_undocumented_fault_still_offers_the_ticket_and_names_the_subject(self):
        answer, sources = ask_mansour(
            "ضاع مني ملف الإنجاز كله وين راح؟", plans=[], audience="teacher"
        )

        self.assertIn("ملف إنجاز", answer)
        self.assertIn("تذكرة دعم", answer)
        self.assertTrue(any(source["url"] == "/support/new/" for source in sources))

    def test_a_delivery_complaint_is_not_read_as_a_setup_request(self):
        answer, _sources = ask_mansour(
            "المعلم يقول ما وصله التعميم مع أني أرسلته أمس", plans=[], audience="manager"
        )

        self.assertNotIn("أضف المعلمين أو استوردهم", answer)
        self.assertIn("التعميم", answer)

    def test_a_struggling_user_is_reassured_before_being_instructed(self):
        answer, _sources = ask_mansour(
            "ما أعرف أستخدم المنصة أبدًا، كبير بالسن وصعبة علي",
            plans=[],
            audience="teacher",
        )

        self.assertIn("معك", answer)
        report = grade_answer(answer, audience="teacher", kind="emotional")
        self.assertEqual(report.dimension("empathy").score, 1.0)
