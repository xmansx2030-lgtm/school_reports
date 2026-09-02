"""طبقة النقل إلى المزوّد: المحاولة الثانية، وكشف البتر، وقراءة الأخطاء.

الثابت الذي تحرسه هذه الاختبارات واحد: **لا يصل المستخدمَ نصٌّ ناقص**. الردّ
المقطوع عند سقف الرموز يعود بحقل ``output_text`` سليم الشكل تماماً، فلا شيء في
النصّ نفسه يفضحه — والحقل الوحيد الذي يكشفه هو ``status``. وفقرةٌ تنتهي في
منتصف جملة تُعتمَد تقريراً رسمياً أسوأ من خطأٍ صريح يدفع المعلّم لإعادة
المحاولة.
"""

from __future__ import annotations

from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from django.test import SimpleTestCase

from reports.ai_client import request_json, truncation_reason
from reports.ai_errors import (
    is_openai_spend_limit_error,
    is_transient_openai_error,
    openai_error_code,
)


def _http_error(code: int, error_code: str = "") -> HTTPError:
    body = b"{}" if not error_code else (
        b'{"error": {"code": "' + error_code.encode() + b'"}}'
    )
    return HTTPError(
        url="https://api.openai.com/v1/responses",
        code=code,
        msg="error",
        hdrs=None,
        fp=BytesIO(body),
    )


class TruncationTests(SimpleTestCase):
    def test_a_completed_response_reports_no_truncation(self):
        self.assertEqual(truncation_reason({"status": "completed"}), "")

    def test_a_payload_without_a_status_is_treated_as_complete(self):
        """الاستجابات القديمة والمُختبَرة لا تحمل الحقل؛ لا تُعدّ مبتورة."""
        self.assertEqual(truncation_reason({"output": []}), "")

    def test_the_token_ceiling_is_reported_as_the_reason(self):
        payload = {
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
        }
        self.assertEqual(truncation_reason(payload), "max_output_tokens")

    def test_an_unnamed_reason_still_counts_as_truncation(self):
        """لا نطابق نصّ السبب: سببٌ جديد الاسم يجب ألّا يمرّ كردٍّ مكتمل."""
        self.assertEqual(truncation_reason({"status": "incomplete"}), "incomplete")
        self.assertEqual(
            truncation_reason({"status": "incomplete", "incomplete_details": None}),
            "incomplete",
        )
        self.assertEqual(truncation_reason({"status": "failed"}), "failed")


class ErrorClassificationTests(SimpleTestCase):
    def test_the_spend_limit_verdict_survives_a_second_reading(self):
        """جسم ``HTTPError`` يُستهلك بالقراءة الأولى.

        بدون حفظ الرمز يعود الفحص الثاني ``False`` مهما كان الخطأ، فتتحوّل
        «تجاوزتَ حدّ الإنفاق» إلى «تعذّر الاتصال» — وهو ما يحدث فعلاً لأن
        المُعيد يفحص الاستثناء ثم يفحصه المتّصل ليختار الرسالة.
        """
        exc = _http_error(429, "organization_spend_limit_exceeded")

        self.assertTrue(is_openai_spend_limit_error(exc))
        self.assertTrue(is_openai_spend_limit_error(exc))
        self.assertEqual(openai_error_code(exc), "organization_spend_limit_exceeded")

    def test_a_spend_limit_is_never_retried(self):
        """رفضٌ مؤكّد: إعادته تنفق طلبات وتؤخّر الرسالة التي يستحقها المستخدم."""
        self.assertFalse(is_transient_openai_error(_http_error(429, "project_spend_limit_exceeded")))

    def test_an_ordinary_rate_limit_is_retried(self):
        self.assertTrue(is_transient_openai_error(_http_error(429, "rate_limit_exceeded")))

    def test_provider_faults_are_retried_and_our_own_mistakes_are_not(self):
        for code in (500, 502, 503, 504, 408):
            with self.subTest(code=code):
                self.assertTrue(is_transient_openai_error(_http_error(code)))
        for code in (400, 401, 403, 404, 422):
            with self.subTest(code=code):
                self.assertFalse(is_transient_openai_error(_http_error(code)))

    def test_network_faults_are_retried(self):
        self.assertTrue(is_transient_openai_error(URLError("connection refused")))
        self.assertTrue(is_transient_openai_error(TimeoutError()))


class RetryTests(SimpleTestCase):
    def _request(self):
        from urllib.request import Request

        return Request("https://api.openai.com/v1/responses", data=b"{}", method="POST")

    def _response(self, body: bytes = b'{"status": "completed"}'):
        class _R(BytesIO):
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

        return _R(body)

    def test_a_transient_fault_is_retried_once_and_succeeds(self):
        calls = []

        def fake_urlopen(request, timeout=None):
            calls.append(timeout)
            if len(calls) == 1:
                raise _http_error(503)
            return self._response()

        with patch("reports.ai_client.urlopen", fake_urlopen):
            payload = request_json(
                self._request(), timeout=25, stage="t", sleep=lambda _: None
            )

        self.assertEqual(payload, {"status": "completed"})
        self.assertEqual(len(calls), 2)

    def test_the_retry_never_exceeds_the_configured_timeout(self):
        """المهلة المضبوطة تبقى السقف الذي يراه المستخدم، لا سقف كل محاولة."""
        seen = []

        def fake_urlopen(request, timeout=None):
            seen.append(timeout)
            if len(seen) == 1:
                raise _http_error(503)
            return self._response()

        import time

        with patch("reports.ai_client.urlopen", fake_urlopen):
            request_json(
                self._request(), timeout=25, stage="t", sleep=lambda _: time.sleep(0.02)
            )

        # المحاولة الثانية ترث ما تبقّى من الميزانية، لا مهلةً جديدة كاملة.
        self.assertLessEqual(seen[0], 25)
        self.assertLess(seen[1], seen[0])
        self.assertGreater(seen[1], 0)

    def test_a_permanent_error_is_raised_without_a_second_call(self):
        calls = []

        def fake_urlopen(request, timeout=None):
            calls.append(1)
            raise _http_error(400)

        with patch("reports.ai_client.urlopen", fake_urlopen):
            with self.assertRaises(HTTPError):
                request_json(self._request(), timeout=25, stage="t", sleep=lambda _: None)

        self.assertEqual(len(calls), 1)

    def test_a_spend_limit_is_raised_without_a_second_call(self):
        calls = []

        def fake_urlopen(request, timeout=None):
            calls.append(1)
            raise _http_error(429, "organization_spend_limit_exceeded")

        with patch("reports.ai_client.urlopen", fake_urlopen):
            with self.assertRaises(HTTPError) as caught:
                request_json(self._request(), timeout=25, stage="t", sleep=lambda _: None)

        self.assertEqual(len(calls), 1)
        # ويبقى الاستثناء صالحاً للتصنيف بعد أن فحصه المُعيد.
        self.assertTrue(is_openai_spend_limit_error(caught.exception))

    def test_a_short_timeout_leaves_no_budget_for_a_second_attempt(self):
        """مهلةٌ ضيقة تعني أن الإعادة تتجاوز ما وعدنا به المستخدم."""
        calls = []

        def fake_urlopen(request, timeout=None):
            calls.append(1)
            raise _http_error(503)

        with patch("reports.ai_client.urlopen", fake_urlopen):
            with self.assertRaises(HTTPError):
                request_json(self._request(), timeout=3, stage="t", sleep=lambda _: None)

        self.assertEqual(len(calls), 1)
