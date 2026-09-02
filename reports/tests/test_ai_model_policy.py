"""سياسة نماذج الذكاء الاصطناعي: ألّا يوقف تقاعدُ نموذجٍ المنصةَ فجأة.

OpenAI تُغلق جيل GPT-5 كاملاً في 11 ديسمبر 2026، و``gpt-4o-mini-transcribe``
في 26 فبراير 2027. وخوادمنا تثبّت المعرّف في ``.env`` صراحةً، فتغيير القيمة
الافتراضية في الكود لا يبلغها. لذلك يُعاد تعيين المعرّف المتقاعد عند القراءة،
وهذه الاختبارات تحرس ذلك — لأن كسرها يظهر يوم الإغلاق لا قبله.
"""

from __future__ import annotations

from django.test import SimpleTestCase

from config.settings import (
    AI_FAST_REASONING_EFFORT,
    DEFAULT_OPENAI_TEXT_MODEL,
    DEFAULT_TRANSCRIPTION_MODEL,
    OPENAI_REASONING_EFFORTS,
    RETIRED_OPENAI_REASONING_EFFORTS,
    RETIRED_OPENAI_TEXT_MODELS,
    RETIRED_TRANSCRIPTION_MODELS,
    _openai_text_model,
)


class RetiredTextModelTests(SimpleTestCase):
    def test_a_retired_id_pinned_in_the_environment_is_replaced(self):
        """السيناريو الحقيقي: ``.env`` الإنتاج يحمل ``gpt-5-mini``."""
        for retired in ("gpt-5-mini", "gpt-5-mini-2025-08-07", "gpt-5-nano"):
            with self.subTest(model=retired):
                self.assertEqual(_openai_text_model(retired), DEFAULT_OPENAI_TEXT_MODEL)

    def test_an_empty_value_falls_back_to_the_default_tier(self):
        for value in ("", "   ", None):
            with self.subTest(value=value):
                self.assertEqual(_openai_text_model(value), DEFAULT_OPENAI_TEXT_MODEL)

    def test_a_current_model_is_forwarded_untouched(self):
        """المَخرج يبقى مفتوحاً: من أراد Terra يضبطه فيصل كما هو."""
        for current in ("gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"):
            with self.subTest(model=current):
                self.assertEqual(_openai_text_model(current), current)

    def test_the_default_tier_is_not_itself_a_retired_id(self):
        self.assertNotIn(DEFAULT_OPENAI_TEXT_MODEL, RETIRED_OPENAI_TEXT_MODELS)
        self.assertNotIn(DEFAULT_TRANSCRIPTION_MODEL, RETIRED_TRANSCRIPTION_MODELS)

    def test_every_replacement_is_a_live_model(self):
        """خريطةٌ تحيل متقاعداً إلى متقاعد تؤجّل العطب ولا تمنعه."""
        for retired, replacement in RETIRED_OPENAI_TEXT_MODELS.items():
            with self.subTest(model=retired):
                self.assertNotIn(replacement, RETIRED_OPENAI_TEXT_MODELS)
        for retired, replacement in RETIRED_TRANSCRIPTION_MODELS.items():
            with self.subTest(model=retired):
                self.assertNotIn(replacement, RETIRED_TRANSCRIPTION_MODELS)


class ReasoningEffortTests(SimpleTestCase):
    def test_the_gpt5_minimal_effort_maps_onto_its_gpt56_name(self):
        """``minimal`` لم يعد ضمن السلّم، وإرساله يعيد 400 على كل طلب."""
        self.assertEqual(RETIRED_OPENAI_REASONING_EFFORTS["minimal"], "none")
        self.assertNotIn("minimal", OPENAI_REASONING_EFFORTS)

    def test_the_ladder_matches_the_gpt56_generation(self):
        self.assertEqual(
            set(OPENAI_REASONING_EFFORTS),
            {"none", "low", "medium", "high", "xhigh", "max"},
        )

    def test_the_fast_effort_is_a_value_the_api_accepts(self):
        self.assertIn(AI_FAST_REASONING_EFFORT, OPENAI_REASONING_EFFORTS)
