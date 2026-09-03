"""الإملاء الصوتي للتقرير: البوابة، والحصة، وما يحدث حين تتعثّر الخدمة.

ثلاثة ثوابت تحرسها هذه الاختبارات:
  ١. الميزة لا تُستدعى من متصفّح عادي ما دام ``VOICE_REPORT_PWA_ONLY`` مفعّلاً.
  ٢. ثلاث محاولات يومياً لكل معلّم، تُحسب على الخادم لا في الواجهة.
  ٣. الحصة لا تُستهلك على طلبٍ لم ينتج نصاً — لا برفضٍ في التحقق ولا بعطلٍ
     لدى المزوّد.
"""

from __future__ import annotations

import io
from unittest.mock import patch

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from reports.models import (
    School,
    SchoolMembership,
    SchoolSubscription,
    SubscriptionPlan,
    Teacher,
)
from reports.voice_report import (
    VoiceReportError,
    VoiceReportUnavailable,
    normalise_audio_type,
    voice_report_daily_remaining,
)

PWA = {"HTTP_X_TAWTHEEQ_SURFACE": "standalone"}


def audio_upload(*, size: int = 40_000, content_type: str = "audio/webm;codecs=opus"):
    return SimpleUploadedFile("clip", b"\x1a\x45\xdf\xa3" + b"0" * (size - 4), content_type=content_type)


@override_settings(
    VOICE_REPORT_ENABLED=True,
    VOICE_REPORT_PWA_ONLY=True,
    VOICE_REPORT_DAILY_LIMIT=3,
    OPENAI_API_KEY="sk-test-key",
    ALLOWED_HOSTS=["testserver"],
)
class VoiceReportEndpointTests(TestCase):
    def setUp(self):
        # الحصة وحدّ المعدّل يعيشان في الكاش المشترك بين الاختبارات.
        cache.clear()
        self.school = School.objects.create(name="مدرسة الصوت", code="voice-school")
        plan = SubscriptionPlan.objects.create(
            name="باقة", price=0, days_duration=365, max_teachers=0
        )
        SchoolSubscription.objects.create(school=self.school, plan=plan)
        self.teacher = Teacher.objects.create_user(
            phone="500990001", name="معلم الصوت", password="voice-pass"
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.teacher,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        self.client.force_login(self.teacher)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()
        self.url = reverse("reports:transcribe_report_voice")

    def _post(self, **extra):
        payload = extra.pop("data", {"audio": audio_upload()})
        headers = dict(PWA)
        headers.update(extra)
        return self.client.post(self.url, payload, **headers)

    # ── البوابة ──────────────────────────────────────────────────────
    def test_a_plain_browser_request_is_refused(self):
        response = self.client.post(self.url, {"audio": audio_upload()})

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["reason"], "pwa_required")
        self.assertEqual(voice_report_daily_remaining(self.teacher.pk), 3)

    @override_settings(VOICE_REPORT_PWA_ONLY=False)
    def test_the_gate_can_be_opened_by_configuration(self):
        with patch("reports.views.reports.transcribe_audio", return_value="نص"), patch(
            "reports.views.reports.polish_dictation", return_value="نص التقرير المفرَّغ."
        ):
            response = self.client.post(self.url, {"audio": audio_upload()})

        self.assertEqual(response.status_code, 200)

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get(self.url, **PWA).status_code, 405)

    @override_settings(VOICE_REPORT_ENABLED=False)
    def test_a_disabled_feature_answers_404(self):
        self.assertEqual(self._post().status_code, 404)

    # ── المسار الناجح ────────────────────────────────────────────────
    def test_a_recording_becomes_report_text(self):
        with patch(
            "reports.views.reports.transcribe_audio", return_value="اليوم نفذت نشاط يعني توعوي"
        ) as transcribe, patch(
            "reports.views.reports.polish_dictation", return_value="اليوم نُفِّذ نشاط توعوي."
        ):
            response = self._post()

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["text"], "اليوم نُفِّذ نشاط توعوي.")
        # التفريغ الحرفي يعود معه: بدونه لا يستطيع المعلّم كشف تفريغٍ مخالف
        # لما قاله، ولا نعرف نحن أي المرحلتين أخطأت حين يشتكي.
        self.assertEqual(body["raw_text"], "اليوم نفذت نشاط يعني توعوي")
        self.assertEqual(body["remaining"], 2)
        self.assertEqual(body["daily_limit"], 3)
        self.assertEqual(transcribe.call_count, 1)

    # ── الحصة ────────────────────────────────────────────────────────
    def test_three_a_day_then_the_fourth_is_refused(self):
        with patch("reports.views.reports.transcribe_audio", return_value="نص"), patch(
            "reports.views.reports.polish_dictation", return_value="نص مفرَّغ للتقرير."
        ):
            for expected in (2, 1, 0):
                response = self._post()
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["remaining"], expected)

            fourth = self._post()

        self.assertEqual(fourth.status_code, 429)
        self.assertEqual(fourth.json()["remaining"], 0)

    def test_the_voice_quota_is_separate_from_the_text_improver_quota(self):
        """تسجيلٌ واحد لا يجوز أن يأكل حقّ المعلّم في تحسين الصياغة."""
        from reports.report_ai import report_ai_daily_remaining

        with patch("reports.views.reports.transcribe_audio", return_value="نص"), patch(
            "reports.views.reports.polish_dictation", return_value="نص مفرَّغ للتقرير."
        ):
            self._post()

        self.assertEqual(voice_report_daily_remaining(self.teacher.pk), 2)
        self.assertEqual(report_ai_daily_remaining(self.teacher.pk), 3)

    # ── لا تُحتسب محاولة بلا نص ───────────────────────────────────────
    def test_a_provider_outage_does_not_consume_a_try(self):
        with patch(
            "reports.views.reports.transcribe_audio",
            side_effect=VoiceReportUnavailable("تعذر الوصول إلى خدمة التفريغ الآن."),
        ):
            response = self._post()

        self.assertEqual(response.status_code, 503)
        self.assertEqual(voice_report_daily_remaining(self.teacher.pk), 3)

    def test_unclear_audio_does_not_consume_a_try(self):
        with patch(
            "reports.views.reports.transcribe_audio",
            side_effect=VoiceReportError("لم أتبيّن كلامًا واضحًا في التسجيل."),
        ):
            response = self._post()

        self.assertEqual(response.status_code, 400)
        self.assertEqual(voice_report_daily_remaining(self.teacher.pk), 3)

    # ── التحقق من الملف ──────────────────────────────────────────────
    def test_a_rejected_file_never_reaches_the_provider_nor_the_quota(self):
        with patch("reports.views.reports.transcribe_audio") as transcribe:
            response = self._post(data={"audio": audio_upload(content_type="application/pdf")})

        self.assertEqual(response.status_code, 400)
        transcribe.assert_not_called()
        self.assertEqual(voice_report_daily_remaining(self.teacher.pk), 3)

    def test_a_missing_recording_is_rejected(self):
        self.assertEqual(self._post(data={}).status_code, 400)

    def test_a_tiny_recording_is_rejected(self):
        response = self._post(data={"audio": audio_upload(size=500)})
        self.assertEqual(response.status_code, 400)

    @override_settings(VOICE_REPORT_MAX_BYTES=200_000)
    def test_an_oversized_recording_is_rejected(self):
        response = self._post(data={"audio": audio_upload(size=260_000)})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(voice_report_daily_remaining(self.teacher.pk), 3)

    def test_anonymous_visitors_are_redirected_to_login(self):
        self.client.logout()
        response = self.client.post(self.url, {"audio": audio_upload()}, **PWA)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("reports:login"), response["Location"])


class AudioTypeTests(TestCase):
    def test_the_recorder_codec_suffix_is_tolerated(self):
        self.assertEqual(normalise_audio_type("audio/webm;codecs=opus"), "webm")
        self.assertEqual(normalise_audio_type("AUDIO/MP4"), "mp4")

    def test_anything_outside_the_allow_list_is_refused(self):
        for value in ("application/pdf", "image/png", "", "text/plain", "audio/aiff"):
            with self.subTest(content_type=value):
                with self.assertRaises(VoiceReportError):
                    normalise_audio_type(value)


@override_settings(
    VOICE_REPORT_ENABLED=True,
    OPENAI_API_KEY="sk-test-key",
    VOICE_REPORT_MODEL="gpt-transcribe",
)
class TranscriptionRequestTests(TestCase):
    """ما يُرسل فعلاً إلى المزوّد."""

    def _fake_response(self, payload: bytes):
        class _Response(io.BytesIO):
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

        return _Response(payload)

    def test_the_request_is_multipart_with_a_server_side_filename(self):
        from reports.voice_report import transcribe_audio

        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["content_type"] = request.headers.get("Content-type", "")
            captured["body"] = request.data
            import json as _json

            return self._fake_response(
                _json.dumps({"text": "اليوم نُفِّذ نشاط توعوي في الإذاعة المدرسية"}).encode("utf-8")
            )

        with patch("reports.ai_client.urlopen", fake_urlopen):
            text = transcribe_audio(b"0" * 5000, "webm")

        self.assertEqual(text, "اليوم نُفِّذ نشاط توعوي في الإذاعة المدرسية")
        self.assertEqual(captured["url"], "https://api.openai.com/v1/audio/transcriptions")
        self.assertTrue(captured["content_type"].startswith("multipart/form-data; boundary="))
        body = captured["body"]
        self.assertIn(b'name="model"', body)
        self.assertIn(b"gpt-transcribe", body)
        # ``gpt-transcribe`` استبدل ``language`` المفردة بـ``languages``،
        # والوثيقة تمنع إرسال الحقلين معاً.
        self.assertIn(b'name="languages[]"', body)
        self.assertNotIn(b'name="language"', body)
        self.assertIn("ar".encode(), body)
        # اسم الملف يُصاغ على الخادم؛ اسم العميل لا يصل الترويسة أبدًا.
        self.assertIn(b'filename="report.webm"', body)
        # ولا ``prompt``: النموذج يسرّبه إلى المخرَج عند الصمت والضجيج، فيعود
        # نصّ السياق «تفريغاً» لكلامٍ لم يُقل.
        self.assertNotIn(b'name="prompt"', body)

    def test_no_vocabulary_prompt_is_sent_with_the_minutes_recording(self):
        """المحضر كالتقرير: لا سياق يُرسل مع الصوت، وله اسم ملفّه وحده."""
        from reports.voice_report import transcribe_meeting_audio

        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["body"] = request.data
            import json as _json

            return self._fake_response(
                _json.dumps({"text": "نوقشت الخطة وأوصت اللجنة بالمتابعة"}).encode("utf-8")
            )

        with patch("reports.ai_client.urlopen", fake_urlopen):
            text = transcribe_meeting_audio(b"0" * 5000, "webm")

        self.assertEqual(text, "نوقشت الخطة وأوصت اللجنة بالمتابعة")
        self.assertIn(b'filename="meeting-minutes.webm"', captured["body"])
        self.assertNotIn(b'name="prompt"', captured["body"])
        self.assertNotIn("مصطلحات متوقعة".encode("utf-8"), captured["body"])

    def _captured_transcription_body(self) -> bytes:
        from reports.voice_report import transcribe_audio

        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["body"] = request.data
            import json as _json

            return self._fake_response(
                _json.dumps({"text": "اليوم نُفِّذ نشاط توعوي في الإذاعة"}).encode("utf-8")
            )

        with patch("reports.ai_client.urlopen", fake_urlopen):
            transcribe_audio(b"0" * 5000, "webm")
        return captured["body"]

    @override_settings(VOICE_REPORT_MODEL="gpt-4o-mini-transcribe")
    def test_a_legacy_model_still_receives_the_singular_language_field(self):
        """النماذج قبل ``gpt-transcribe`` لا تعرف ``languages``."""
        body = self._captured_transcription_body()

        self.assertIn(b'name="language"', body)
        self.assertNotIn(b'name="languages[]"', body)

    def test_no_keywords_are_sent_unless_they_are_configured(self):
        """الافتراضي فارغ: مصطلحٌ مُلقَّن قد يظهر في تفريغ لم يُنطق فيه."""
        self.assertNotIn(b'name="keywords[]"', self._captured_transcription_body())

    @override_settings(VOICE_REPORT_KEYWORDS=("توثيق", "الإذاعة المدرسية"))
    def test_configured_keywords_are_sent_as_repeated_fields(self):
        body = self._captured_transcription_body()

        self.assertEqual(body.count(b'name="keywords[]"'), 2)
        self.assertIn("الإذاعة المدرسية".encode("utf-8"), body)

    @override_settings(VOICE_REPORT_KEYWORDS=("سليم", "خطر <script>", "سطر\nثانٍ", ""))
    def test_a_malformed_keyword_is_dropped_instead_of_failing_the_recording(self):
        """الواجهة ترفض الطلب كاملاً على محرف واحد، فيضيع تسجيل المعلّم."""
        body = self._captured_transcription_body()

        self.assertEqual(body.count(b'name="keywords[]"'), 1)
        self.assertIn("سليم".encode("utf-8"), body)
        self.assertNotIn(b"<script>", body)

    def test_meeting_polish_keeps_raw_text_when_a_number_changes(self):
        from reports.voice_report import polish_meeting_dictation

        raw = "ناقشت اللجنة 3 توصيات واعتمدت تنفيذها خلال 5 أيام"
        with patch(
            "reports.voice_report._post_responses",
            return_value=self._polished("ناقشت اللجنة 4 توصيات واعتمدت تنفيذها خلال 5 أيام."),
        ):
            self.assertEqual(polish_meeting_dictation(raw), raw)

    def test_a_polish_failure_falls_back_to_the_raw_transcript(self):
        """تعثّر التجميل لا يجوز أن يضيّع كلام المعلّم."""
        from reports.voice_report import polish_dictation

        with patch(
            "reports.voice_report._post_responses",
            side_effect=VoiceReportUnavailable("outage"),
        ):
            self.assertEqual(polish_dictation("نص خام بلا ترقيم"), "نص خام بلا ترقيم")

    def _polished(self, text: str, *, status: str = "completed", reason: str = "") -> dict:
        payload = {
            "status": status,
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": text}],
                }
            ],
        }
        if reason:
            payload["incomplete_details"] = {"reason": reason}
        return payload

    def test_a_polish_cut_off_at_the_token_ceiling_keeps_the_raw_transcript(self):
        """التفريغ الخام كاملٌ وإن كان بلا ترقيم، والمجمَّل المبتور ناقص.

        النصّ هنا يحمل أرقام التفريغ نفسها وطولاً كافياً وتداخلاً لفظياً تاماً،
        أي أنه يعبر الحرّاس الثلاثة القائمة. ولا يكشفه إلا ``status``.
        """
        from reports.voice_report import polish_dictation

        raw = "اليوم نفذنا نشاط توعوي وحضره 54 طالب وكان في الاذاعه المدرسيه"
        with patch(
            "reports.voice_report._post_responses",
            return_value=self._polished(
                "اليوم نُفِّذ نشاط توعوي وحضره 54 طالبًا وكان في الإذاعة",
                status="incomplete",
                reason="max_output_tokens",
            ),
        ):
            self.assertEqual(polish_dictation(raw), raw)

    def test_a_polish_that_changes_a_dictated_number_is_discarded(self):
        """نصٌّ أنيق برقمٍ لم يقله المعلّم أسوأ من تفريغٍ بلا ترقيم."""
        from reports.voice_report import polish_dictation

        raw = "اليوم نفذت نشاط توعوي يعني وحضره ٤٥ طالب"

        with patch(
            "reports.voice_report._post_responses",
            return_value=self._polished("اليوم نُفِّذ نشاط توعوي وحضره 54 طالبًا."),
        ):
            self.assertEqual(polish_dictation(raw), raw)

    def test_a_polish_that_keeps_the_numbers_is_accepted(self):
        from reports.voice_report import polish_dictation

        with patch(
            "reports.voice_report._post_responses",
            return_value=self._polished("اليوم نُفِّذ نشاط توعوي وحضره 45 طالبًا."),
        ):
            polished = polish_dictation("اليوم نفذت نشاط توعوي يعني وحضره ٤٥ طالب")

        self.assertEqual(polished, "اليوم نُفِّذ نشاط توعوي وحضره 45 طالبًا.")

    def test_a_polish_that_writes_different_words_is_discarded(self):
        """الشكوى التي أطلقت هذا الحارس: طولٌ واحد، وبلا أرقام، وكلامٌ آخر."""
        from reports.voice_report import polish_dictation

        raw = "تم عمل دورة تدريبية"

        with patch(
            "reports.voice_report._post_responses",
            return_value=self._polished("ابدأ اليوم بتقرير عن"),
        ):
            self.assertEqual(polish_dictation(raw), raw)

    def test_editing_the_same_words_survives_the_overlap_guard(self):
        """التصحيح الإملائي والربط ليسا كلاماً آخر، فلا يجوز أن يسقطا."""
        from reports.voice_report import polish_dictation

        raw = "اليوم يعني نفذنا نشاط توعوي في الاذاعه المدرسيه وتفاعل الطلاب معه"
        polished = "اليوم نُفِّذ نشاط توعوي في الإذاعة المدرسية، وتفاعل الطلاب معه."

        with patch("reports.voice_report._post_responses", return_value=self._polished(polished)):
            self.assertEqual(polish_dictation(raw), polished)

    def test_a_provider_outage_during_polish_returns_the_raw_transcript(self):
        """اختبارٌ عند الحدّ الحقيقي لا عند دالةٍ داخلية.

        بقية اختبارات التجميل ترقّع ``_post_responses``، فلا ترى نوع الاستثناء
        الذي يصعد من طبقة الشبكة فعلاً. ولمّا وُحّد نداء المزوّد صار الصاعد
        ``HTTPError`` لا ``VoiceReportError``، فكاد التجميل المتعثّر يُسقط
        الطلب كلّه ويضيّع على المعلّم كلامه. هذا ما يمسك ذلك.
        """
        from urllib.error import HTTPError

        from reports.voice_report import polish_dictation

        raw = "اليوم نفذنا نشاط توعوي وحضره 45 طالب"
        error = HTTPError("https://api.openai.com/v1/responses", 500, "boom", {}, io.BytesIO(b"{}"))
        with patch("reports.ai_client.urlopen", side_effect=error):
            self.assertEqual(polish_dictation(raw), raw)

    def test_a_polish_that_swallows_most_of_the_dictation_is_discarded(self):
        from reports.voice_report import polish_dictation

        raw = (
            "اليوم يعني نفذنا برنامج توعوي في الإذاعة المدرسية وشارك فيه معلمو القسم "
            "وآآ تفاعل الطلاب معه بشكل جيد وحقق أهدافه"
        )

        with patch(
            "reports.voice_report._post_responses",
            return_value=self._polished("نُفِّذ برنامج توعوي."),
        ):
            self.assertEqual(polish_dictation(raw), raw)
