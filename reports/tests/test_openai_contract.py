"""عقد التكامل مع OpenAI — ما يُرسَل على السلك فعلاً، ولمن، وبأي مَعالم.

**لماذا اختبارٌ للصيغة لا للسلوك؟** بقيّة الاختبارات تسأل «هل خرج النصّ
الصحيح؟» وترقّع الشبكة بردٍّ جاهز. وهذا يترك سؤالاً بلا جواب: **ماذا أرسلنا؟**
مَعلمةٌ خاطئة — نموذجٌ متقاعد، أو ``store`` منسيّة، أو مخطَّطٌ غير صارم — تمرّ
من كل تلك الاختبارات لأن الردّ المرقَّع لا يعتمد على الطلب.

وأثر الخطأ هنا لا يظهر في التطوير: يظهر فاتورةً، أو تسريبَ محتوى إلى مخزَن
المزوّد، أو انقطاعَ خدمةٍ صباح اليوم الذي يتقاعد فيه نموذج.

هذا الملف يلتقط الطلب قبل مغادرته ويحاكم **جسده**.
"""

from __future__ import annotations

import io
import json
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings

from reports import mansour_assistant, report_ai, report_review, voice_report
from reports.ai_client import OPENAI_RESPONSES_URL, OPENAI_TRANSCRIPTIONS_URL
from reports.models import AiUsageEvent, School


class _Capture:
    """يلتقط كل طلب يمرّ ويعيد ردّاً صالحاً."""

    # الردّ المرقَّع يجب أن يعبر حرّاس المنصة نفسها: يحفظ الرقم «45»، ويقارب
    # المدخل طولاً، ويشاركه كلماته — وإلا رفضه حارسُ انحراف الحقائق، فيبدو
    # عطلاً في العقد وهو في الحقيقة حارسٌ يعمل.
    def __init__(
        self,
        text=(
            "نُفِّذ برنامج التوعية بالأمن السيبراني في قاعة النشاط بحضور طلاب الصفوف "
            "الثلاثة، واستفاد منه 45 طالبًا، وتضمّن ورشة عملية على رسائل التصيّد."
        ),
    ):
        self.requests = []
        self._text = text

    def __call__(self, request, *args, **kwargs):
        body = None
        if request.data and request.get_header("Content-type", "").startswith("application/json"):
            body = json.loads(request.data.decode("utf-8"))
        self.requests.append(
            {
                "url": request.full_url,
                "method": request.get_method(),
                "headers": {k.lower(): v for k, v in request.header_items()},
                "body": body,
                "raw": request.data,
            }
        )
        return _Response(self._text)

    @property
    def last(self):
        return self.requests[-1]


class _Response:
    def __init__(self, text):
        self._text = text

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(
            {
                "status": "completed",
                "text": self._text,
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": self._text}],
                    }
                ],
                "usage": {
                    "input_tokens": 1800,
                    "input_tokens_details": {"cached_tokens": 1500},
                    "output_tokens": 220,
                    "output_tokens_details": {"reasoning_tokens": 60},
                },
            },
            ensure_ascii=False,
        ).encode("utf-8")


REPORT_TEXT = (
    "تم تنفيذ برنامج التوعية بالأمن السيبراني في قاعة النشاط بحضور طلاب الصفوف "
    "الثلاثة، واستفاد منه 45 طالبًا، وتضمّن ورشة عملية على رسائل التصيّد."
)

REVIEW_DRAFT = {
    "title": "برنامج التوعية بالأمن السيبراني",
    "category": "activity",
    "report_date": "2026-09-01",
    "goal": "رفع وعي طلاب المرحلة الثانوية بمخاطر مشاركة كلمات المرور.",
    "idea": REPORT_TEXT,
    "implementation_method": "عرض تقديمي ثم ورشة تطبيقية ثم نقاش ختامي مع الطلاب.",
    "results": "شارك 45 طالبًا، وارتفعت نسبة الإجابات الصحيحة إلى 82%.",
    "recommendations": "تكرار البرنامج مع طلاب المرحلة المتوسطة في الفصل الثاني.",
    "beneficiaries_count": "45",
    "evidence_count": 2,
    "sections": {
        "show_goal": True,
        "show_details": True,
        "show_implementation": True,
        "show_results": True,
        "show_recommendations": True,
        "show_beneficiaries": True,
    },
}


@override_settings(
    OPENAI_API_KEY="sk-test-contract",
    MANSOUR_ASSISTANT_ENABLED=True,
    REPORT_AI_ENABLED=True,
    REPORT_REVIEW_ENABLED=True,
    VOICE_REPORT_ENABLED=True,
    AI_USAGE_TRACKING_ENABLED=False,
)
class WireContractTests(SimpleTestCase):
    """كل مسار يُنادى مرة، ويُحاكَم الطلب الذي غادر."""

    def _run(self, call):
        capture = _Capture()
        with patch("reports.ai_client.urlopen", side_effect=capture):
            call()
        return capture

    # ── الوجهة والاعتماد ────────────────────────────────────────────
    def test_every_text_path_posts_to_the_responses_endpoint_with_the_key(self):
        cases = {
            "report-improve": lambda: report_ai.improve_report_text(REPORT_TEXT),
            "meeting-improve": lambda: report_ai.improve_meeting_minutes_text(REPORT_TEXT),
            "voice-polish": lambda: voice_report.polish_dictation(REPORT_TEXT),
            "mansour": lambda: mansour_assistant.ask_mansour("كيف أضيف تقريرًا جديدًا؟"),
            "report-review": lambda: report_review.review_report_draft(REVIEW_DRAFT, user_id=1),
        }
        for name, call in cases.items():
            with self.subTest(path=name):
                capture = self._run(call)
                self.assertTrue(capture.requests, f"{name} لم يرسل أي طلب")
                sent = capture.last
                self.assertEqual(sent["url"], OPENAI_RESPONSES_URL)
                self.assertEqual(sent["method"], "POST")
                self.assertEqual(sent["headers"]["authorization"], "Bearer sk-test-contract")
                self.assertEqual(sent["headers"]["content-type"], "application/json")

    # ── لا يُخزَّن محتوى المستخدم لدى المزوّد ────────────────────────
    def test_no_path_lets_the_provider_store_the_conversation(self):
        """``store: false`` في كل نداء — وهو ما يجعل الخصوصية قراراً لا أملاً."""
        cases = [
            lambda: report_ai.improve_report_text(REPORT_TEXT),
            lambda: report_ai.improve_meeting_minutes_text(REPORT_TEXT),
            lambda: voice_report.polish_dictation(REPORT_TEXT),
            lambda: mansour_assistant.ask_mansour("كيف أضيف تقريرًا جديدًا؟"),
            lambda: report_review.review_report_draft(REVIEW_DRAFT, user_id=2),
        ]
        for index, call in enumerate(cases):
            with self.subTest(case=index):
                capture = self._run(call)
                for sent in capture.requests:
                    self.assertIs(sent["body"].get("store"), False)

    def test_no_path_sends_a_previous_response_id(self):
        """سلسلةٌ محفوظة لدى المزوّد تنقض ``store: false`` من الباب الخلفي."""
        capture = self._run(lambda: mansour_assistant.ask_mansour("كيف أضيف تقريرًا؟"))
        for sent in capture.requests:
            self.assertNotIn("previous_response_id", sent["body"])

    # ── النماذج ─────────────────────────────────────────────────────
    def test_no_path_asks_for_a_retired_model(self):
        """نموذجٌ متقاعد لا يُخطئ في التطوير — يُخطئ صباح يوم إيقافه."""
        from config.settings import RETIRED_OPENAI_TEXT_MODELS, RETIRED_TRANSCRIPTION_MODELS

        retired = set(RETIRED_OPENAI_TEXT_MODELS) | set(RETIRED_TRANSCRIPTION_MODELS)
        cases = [
            lambda: report_ai.improve_report_text(REPORT_TEXT),
            lambda: voice_report.polish_dictation(REPORT_TEXT),
            lambda: mansour_assistant.ask_mansour("كيف أضيف تقريرًا؟"),
            lambda: report_review.review_report_draft(REVIEW_DRAFT, user_id=3),
        ]
        for index, call in enumerate(cases):
            with self.subTest(case=index):
                capture = self._run(call)
                for sent in capture.requests:
                    self.assertNotIn(sent["body"]["model"], retired)

    def test_every_request_caps_its_output(self):
        """بلا سقف، ردٌّ شارد يكلّف بلا حدّ."""
        cases = [
            lambda: report_ai.improve_report_text(REPORT_TEXT),
            lambda: voice_report.polish_dictation(REPORT_TEXT),
            lambda: mansour_assistant.ask_mansour("كيف أضيف تقريرًا؟"),
            lambda: report_review.review_report_draft(REVIEW_DRAFT, user_id=4),
        ]
        for index, call in enumerate(cases):
            with self.subTest(case=index):
                capture = self._run(call)
                for sent in capture.requests:
                    cap = sent["body"].get("max_output_tokens")
                    self.assertIsInstance(cap, int)
                    self.assertGreater(cap, 0)
                    self.assertLessEqual(cap, 2000)

    # ── الفحص: مخطَّط صارم ──────────────────────────────────────────
    def test_the_review_asks_for_a_strict_schema(self):
        capture = self._run(lambda: report_review.review_report_draft(REVIEW_DRAFT, user_id=5))
        fmt = capture.last["body"]["text"]["format"]
        self.assertEqual(fmt["type"], "json_schema")
        self.assertTrue(fmt["strict"])
        schema = fmt["schema"]
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), {"issues", "strengths"})
        item = schema["properties"]["issues"]["items"]
        self.assertFalse(item["additionalProperties"])
        self.assertEqual(set(item["required"]), {"field", "severity", "message", "hint"})
        # القيم المسموحة تُقيَّد في المخطَّط لا في القارئ وحده.
        self.assertIn("enum", item["properties"]["field"])
        self.assertIn("enum", item["properties"]["severity"])

    # ── منصور: تخزين البادئة ────────────────────────────────────────
    def test_mansour_puts_the_cacheable_prefix_first_with_an_explicit_breakpoint(self):
        """تطابق المخزَّن يشترط تطابق البادئة كاملةً، فترتيبها هو الميزة."""
        capture = self._run(lambda: mansour_assistant.ask_mansour("كيف أضيف تقريرًا جديدًا؟"))
        body = capture.requests[0]["body"]
        blocks = body["input"]
        self.assertEqual(blocks[0]["role"], "developer")
        first = blocks[0]["content"][0]
        self.assertEqual(first["type"], "input_text")
        self.assertEqual(first["prompt_cache_breakpoint"], {"mode": "explicit"})
        # السياق المتغيّر يجيء بعد نقطة الفصل لا قبلها.
        self.assertEqual(blocks[1]["role"], "developer")
        self.assertNotIn("prompt_cache_breakpoint", json.dumps(blocks[1]))
        self.assertEqual(body["prompt_cache_options"], {"mode": "explicit"})
        self.assertTrue(body["prompt_cache_key"].startswith("mansour-"))

    def test_the_cache_key_changes_when_the_prefix_changes(self):
        """مفتاحٌ ثابت فوق بادئةٍ تغيّرت يخلط صياغتين على مخزَنٍ واحد."""
        first = mansour_assistant._prompt_cache_options("gpt-5.6-luna", audience="teacher")
        with patch.object(mansour_assistant, "_STATIC_INSTRUCTIONS_VERSION", "deadbeef1234"):
            second = mansour_assistant._prompt_cache_options("gpt-5.6-luna", audience="teacher")
        self.assertNotEqual(first["prompt_cache_key"], second["prompt_cache_key"])

    def test_cache_options_are_withheld_from_a_model_that_cannot_read_them(self):
        self.assertEqual(mansour_assistant._prompt_cache_options("gpt-4o", audience="teacher"), {})

    # ── منصور: هوية آمنة ───────────────────────────────────────────
    def test_mansour_sends_a_scrubbed_safety_identifier(self):
        capture = self._run(
            lambda: mansour_assistant.ask_mansour(
                "كيف أضيف تقريرًا؟", safety_identifier="tawtheeq_ab12<script>"
            )
        )
        sent = capture.requests[0]["body"]["safety_identifier"]
        self.assertNotIn("<", sent)
        self.assertNotIn(">", sent)
        self.assertLessEqual(len(sent), 64)

    # ── نسبة كل نداء إلى حساب ───────────────────────────────────────
    def test_every_authenticated_path_carries_a_safety_identifier(self):
        """بلاغُ إساءةٍ بلا نسبة يُعلَّق على المنصة كلها بدل حسابٍ واحد.

        كان المعرّف في منصور وحده، وثلاثةُ مسارات تصل المزوّد بلا نسبة.
        """
        from reports.ai_usage import ai_usage_context

        cases = {
            "report-improve": lambda: report_ai.improve_report_text(REPORT_TEXT),
            "meeting-improve": lambda: report_ai.improve_meeting_minutes_text(REPORT_TEXT),
            "voice-polish": lambda: voice_report.polish_dictation(REPORT_TEXT),
            "report-review": lambda: report_review.review_report_draft(REVIEW_DRAFT, user_id=7),
        }
        for name, call in cases.items():
            with self.subTest(path=name):
                capture = _Capture()
                with patch("reports.ai_client.urlopen", side_effect=capture):
                    with ai_usage_context(teacher=7):
                        call()
                for sent in capture.requests:
                    identifier = sent["body"].get("safety_identifier", "")
                    self.assertTrue(identifier.startswith("tawtheeq_"), f"{name}: {identifier!r}")
                    self.assertNotIn("7", identifier[9:12])

    def test_the_identifier_never_carries_the_account_number(self):
        from reports.ai_usage import safety_identifier_for

        self.assertNotIn("4242", safety_identifier_for(4242))
        self.assertEqual(safety_identifier_for(4242), safety_identifier_for(4242))
        self.assertNotEqual(safety_identifier_for(4242), safety_identifier_for(4243))
        self.assertEqual(safety_identifier_for(None), "")

    def test_an_anonymous_visitor_is_not_given_a_fabricated_identity(self):
        """منصور يخدم زائراً بلا حساب — ولا يُخترع له معرّف."""
        capture = _Capture()
        with patch("reports.ai_client.urlopen", side_effect=capture):
            mansour_assistant.ask_mansour("كم سعر الاشتراك؟")
        for sent in capture.requests:
            self.assertNotIn("safety_identifier", sent["body"])

    # ── التفريغ الصوتي ──────────────────────────────────────────────
    def test_transcription_posts_multipart_to_the_audio_endpoint(self):
        capture = _Capture()
        with patch("reports.ai_client.urlopen", side_effect=capture):
            voice_report.transcribe_audio(b"\x00" * 4000, "webm")
        sent = capture.last
        self.assertEqual(sent["url"], OPENAI_TRANSCRIPTIONS_URL)
        self.assertTrue(sent["headers"]["content-type"].startswith("multipart/form-data; boundary="))
        raw = sent["raw"]
        self.assertIn(b'name="model"', raw)
        self.assertIn(b'name="temperature"', raw)
        # اسم الملف يُصاغ عندنا من امتدادٍ في قائمة بيضاء، ولا يأتي من العميل.
        self.assertIn(b'filename="report.webm"', raw)

    def test_transcription_never_sends_a_narrative_prompt(self):
        """حقل ``prompt`` كان يتسرّب إلى المخرَج عند الصمت فيصير «تفريغاً» مختلقاً."""
        capture = _Capture()
        with patch("reports.ai_client.urlopen", side_effect=capture):
            voice_report.transcribe_audio(b"\x00" * 4000, "webm")
        self.assertNotIn(b'name="prompt"', capture.last["raw"])


@override_settings(
    OPENAI_API_KEY="sk-test-contract",
    REPORT_REVIEW_ENABLED=True,
    REPORT_AI_ENABLED=True,
    VOICE_REPORT_ENABLED=True,
    MANSOUR_ASSISTANT_ENABLED=True,
    AI_USAGE_TRACKING_ENABLED=True,
)
class FailureBehaviourTests(TestCase):
    """كيف تتصرّف المنصة حين يتعثّر المزوّد — وهو ما يقع فعلاً في الإنتاج."""

    def setUp(self):
        self.school = School.objects.create(name="مدرسة العقد", code="contract")

    def _http_error(self, code, payload=b"{}"):
        from urllib.error import HTTPError

        return HTTPError(OPENAI_RESPONSES_URL, code, "boom", {}, io.BytesIO(payload))

    def test_a_spend_limit_is_told_to_the_user_as_a_pause_not_an_error(self):
        body = json.dumps({"error": {"code": "project_spend_limit_exceeded"}}).encode("utf-8")
        with patch("reports.ai_client.urlopen", side_effect=self._http_error(429, body)):
            with self.assertRaises(report_ai.ReportAIUnavailable) as ctx:
                report_ai.improve_report_text(REPORT_TEXT)
        from reports.ai_errors import AI_SERVICE_PAUSED_MESSAGE

        self.assertEqual(str(ctx.exception), AI_SERVICE_PAUSED_MESSAGE)

    def test_a_spend_limit_is_not_retried(self):
        """إعادةُ رفضٍ مؤكّد تُنفق طلبات وتؤخّر الرسالة التي يستحقها المستخدم."""
        body = json.dumps({"error": {"code": "organization_spend_limit_exceeded"}}).encode("utf-8")
        with patch("reports.ai_client.urlopen", side_effect=self._http_error(429, body)) as urlopen:
            with self.assertRaises(report_ai.ReportAIUnavailable):
                report_ai.improve_report_text(REPORT_TEXT)
        self.assertEqual(urlopen.call_count, 1)

    def test_a_transient_failure_is_retried_once_only(self):
        with patch("reports.ai_client.urlopen", side_effect=self._http_error(503)) as urlopen:
            with self.assertRaises(report_ai.ReportAIUnavailable):
                report_ai.improve_report_text(REPORT_TEXT)
        self.assertEqual(urlopen.call_count, 2)

    def test_every_failure_is_recorded_with_its_kind(self):
        with patch("reports.ai_client.urlopen", side_effect=self._http_error(500)):
            with self.assertRaises(report_ai.ReportAIUnavailable):
                report_ai.improve_report_text(REPORT_TEXT)
        event = AiUsageEvent.objects.get()
        self.assertEqual(event.outcome, AiUsageEvent.Outcome.FAILED)
        self.assertEqual(event.error_kind, "HTTP 500")
        self.assertEqual(event.stage, AiUsageEvent.Stage.REPORT_IMPROVE)

    def test_a_truncated_answer_is_never_shown_as_a_finished_one(self):
        """``output_text`` يصل سليم الشكل، و``status`` وحده يفضح البتر."""

        class _Truncated(_Response):
            def read(self):
                return json.dumps(
                    {
                        "status": "incomplete",
                        "incomplete_details": {"reason": "max_output_tokens"},
                        "output": [
                            {
                                "type": "message",
                                "role": "assistant",
                                "content": [{"type": "output_text", "text": "نصّ ينتهي في منتصف"}],
                            }
                        ],
                    },
                    ensure_ascii=False,
                ).encode("utf-8")

        with patch("reports.ai_client.urlopen", return_value=_Truncated("")):
            with self.assertRaises(report_ai.ReportAIError):
                report_ai.improve_report_text(REPORT_TEXT)
        self.assertEqual(AiUsageEvent.objects.get().outcome, AiUsageEvent.Outcome.TRUNCATED)

    def test_the_three_daily_quotas_are_independent(self):
        """تفريغٌ صوتي واحد لا يجوز أن يأكل حقّ المعلّم في تحسين الصياغة."""
        from django.core.cache import cache

        cache.clear()
        report_ai.reserve_report_ai_daily_slot(900)
        self.assertEqual(report_ai.report_ai_daily_remaining(900), report_ai.REPORT_AI_DAILY_LIMIT - 1)
        self.assertEqual(voice_report.voice_report_daily_remaining(900), voice_report.voice_report_daily_limit())
        self.assertEqual(report_review.review_daily_remaining(900), report_review.review_daily_limit())

    def test_a_provider_outage_never_takes_the_assistant_offline(self):
        """منصور يسقط إلى ردٍّ محلّي موثّق بدل رسالة عطل."""
        from urllib.error import URLError

        with patch("reports.ai_client.urlopen", side_effect=URLError("offline")):
            answer, sources = mansour_assistant.ask_mansour("كيف أضيف تقريرًا جديدًا؟")
        self.assertTrue(answer.strip())
        self.assertIsInstance(sources, list)
