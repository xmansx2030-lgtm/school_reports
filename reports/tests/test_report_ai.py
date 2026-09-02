from __future__ import annotations

import json
from datetime import date
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from reports.ai_errors import AI_SERVICE_PAUSED_MESSAGE
from reports.report_ai import REPORT_AI_DAILY_LIMIT, report_ai_daily_remaining
from reports.models import (
    Report,
    ReportType,
    PlatformSettings,
    School,
    SchoolMembership,
    SchoolSubscription,
    SubscriptionPlan,
    Teacher,
)


class _FakeOpenAIResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return json.dumps(
            {
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": (
                                    "نُفّذ البرنامج صباح يوم الأحد، واستفاد منه 35 طالبًا، "
                                    "وتضمّن أنشطة توعوية منظمة حققت الأهداف المذكورة."
                                ),
                            }
                        ],
                    }
                ]
            },
            ensure_ascii=False,
        ).encode("utf-8")


class _FakeTextOpenAIResponse:
    """A stubbed rewrite, so the fact-integrity guard can be exercised."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def __init__(self, text: str, *, status: str = "completed", reason: str = ""):
        self.text = text
        self.status = status
        self.reason = reason

    def read(self):
        payload = {
            "status": self.status,
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": self.text}],
                }
            ],
        }
        if self.reason:
            payload["incomplete_details"] = {"reason": self.reason}
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _spend_limit_error(code: str = "organization_spend_limit_exceeded") -> HTTPError:
    body = json.dumps({"error": {"code": code}}).encode("utf-8")
    return HTTPError(
        url="https://api.openai.com/v1/responses",
        code=429,
        msg="Too Many Requests",
        hdrs=None,
        fp=BytesIO(body),
    )


@override_settings(
    ALLOWED_HOSTS=["testserver"],
    OPENAI_API_KEY="test-report-ai-key",
    REPORT_AI_ENABLED=True,
    REPORT_AI_MODEL="gpt-5.6-luna",
    REPORT_AI_MAX_OUTPUT_TOKENS=700,
    REPORT_AI_TIMEOUT_SECONDS=25,
    RATELIMIT_ENABLE=False,
)
class ReportAIImprovementTests(TestCase):
    def setUp(self):
        cache.clear()
        self.school = School.objects.create(name="مدرسة تحسين التقارير", code="report-ai")
        plan = SubscriptionPlan.objects.create(
            name="خطة تحسين التقارير",
            price=0,
            days_duration=30,
            max_teachers=20,
        )
        SchoolSubscription.objects.create(school=self.school, plan=plan)
        self.teacher = Teacher.objects.create_user(
            phone="500008801",
            name="معلم تحسين التقارير",
            password="test-pass",
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.teacher,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        self.category = ReportType.objects.create(
            school=self.school,
            code="activity",
            name="نشاط مدرسي",
        )

    def _login(self):
        self.client.force_login(self.teacher)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()

    def test_report_forms_show_ai_improvement_review_controls(self):
        self._login()
        report = Report.objects.create(
            school=self.school,
            teacher=self.teacher,
            title="برنامج توعوي",
            report_date=date(2026, 8, 1),
            beneficiaries_count=35,
            idea="تم تنفيذ برنامج توعوي للطلاب.",
            category=self.category,
        )

        for url in (
            reverse("reports:add_report"),
            reverse("reports:edit_my_report", args=[report.pk]),
        ):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "تحسين الصياغة بالذكاء الاصطناعي")
                self.assertContains(response, "3 تحسينات احترافية كل يوم")
                self.assertContains(response, "المتبقي اليوم:")
                self.assertContains(response, "لن يتغير التقرير حتى تعتمد النص")
                self.assertContains(response, reverse("reports:improve_report_text"))
                self.assertContains(response, "css/report-ai-improver.css")
                self.assertContains(response, "js/report-ai-improver.js")
                self.assertContains(response, "css/report-details-limit.css")
                self.assertContains(response, "js/report-details-limit.js")
                self.assertContains(response, 'maxlength="600"')
                self.assertContains(response, "الطول المثالي حتى 450 حرفًا")

    @override_settings(REPORT_AI_ENABLED=False)
    def test_report_forms_hide_ai_controls_when_service_is_disabled(self):
        self._login()
        report = Report.objects.create(
            school=self.school,
            teacher=self.teacher,
            title="برنامج توعوي",
            report_date=date(2026, 8, 1),
            beneficiaries_count=35,
            idea="تم تنفيذ برنامج توعوي للطلاب.",
            category=self.category,
        )

        for url in (
            reverse("reports:add_report"),
            reverse("reports:edit_my_report", args=[report.pk]),
        ):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, "تحسين الصياغة بالذكاء الاصطناعي")
                self.assertNotContains(response, "css/report-ai-improver.css")
                self.assertNotContains(response, "js/report-ai-improver.js")
                self.assertContains(response, "css/report-details-limit.css")
                self.assertContains(response, "js/report-details-limit.js")

    def test_report_form_rejects_details_over_the_final_limit(self):
        from reports.forms import ReportForm

        form = ReportForm(
            data={
                "title": "برنامج توعوي",
                "report_date": "2026-08-01",
                "category": self.category.code,
                "section_selection_enabled": "1",
                "show_details": "on",
                "idea": "أ" * 601,
            },
            active_school=self.school,
        )

        self.assertFalse(form.is_valid())
        self.assertIn("600 حرفًا", form.errors["idea"][0])

    @patch("reports.ai_client.urlopen")
    def test_platform_switch_hides_and_blocks_report_improvement(self, mocked_urlopen):
        self._login()
        platform_settings = PlatformSettings.get_solo()
        platform_settings.report_ai_enabled = False
        platform_settings.save(update_fields=["report_ai_enabled", "updated_at"])

        page = self.client.get(reverse("reports:add_report"))
        api_response = self.client.post(
            reverse("reports:improve_report_text"),
            data=json.dumps({"text": "هذا نص تقرير مكتمل يحتاج إلى تحسين الصياغة اللغوية."}),
            content_type="application/json",
        )

        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "تحسين الصياغة بالذكاء الاصطناعي")
        self.assertNotContains(page, "js/report-ai-improver.js")
        self.assertEqual(api_response.status_code, 404)
        self.assertFalse(api_response.json()["ok"])
        self.assertEqual(report_ai_daily_remaining(self.teacher.pk), REPORT_AI_DAILY_LIMIT)
        mocked_urlopen.assert_not_called()

    @patch("reports.ai_client.urlopen")
    def test_meeting_minutes_use_a_domain_specific_conservative_prompt(self, mocked_urlopen):
        from reports.report_ai import improve_meeting_minutes_text

        original = "ناقش المجتمعون الخطة واقترح أحدهم تنفيذها خلال 5 أيام"
        mocked_urlopen.return_value = _FakeTextOpenAIResponse(
            "ناقش المجتمعون الخطة، واقترح أحدهم تنفيذها خلال 5 أيام."
        )

        improved = improve_meeting_minutes_text(original)

        self.assertIn("5 أيام", improved)
        request_body = json.loads(mocked_urlopen.call_args.args[0].data.decode("utf-8"))
        instructions = request_body["instructions"]
        self.assertIn("لا تحوّل اقتراحًا أو نقاشًا إلى قرار معتمد", instructions)
        self.assertIn("المناقشات والقرارات والتوصيات والمهام", instructions)

    def test_improvement_endpoint_requires_login(self):
        response = self.client.post(
            reverse("reports:improve_report_text"),
            data=json.dumps({"text": "نص تقرير مدرسي يحتاج إلى تحسين الصياغة."}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("reports:login"), response.url)

    @patch("reports.ai_client.urlopen", return_value=_FakeOpenAIResponse())
    def test_endpoint_returns_preview_without_saving_or_sending_extra_data(self, mocked_urlopen):
        self._login()
        original = "نفذنا برنامج يوم الأحد واستفاد 35 طالب وكان فيه أنشطة توعوية."

        response = self.client.post(
            reverse("reports:improve_report_text"),
            data=json.dumps({"text": original, "title": "يجب ألا يُرسل"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["remaining"], 2)
        self.assertEqual(response.json()["daily_limit"], REPORT_AI_DAILY_LIMIT)
        self.assertEqual(response.json()["recommended_length"], 450)
        self.assertEqual(response.json()["max_length"], 600)
        self.assertIn("35 طالبًا", response.json()["improved_text"])
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertEqual(Report.objects.count(), 0)

        api_request = mocked_urlopen.call_args.args[0]
        request_body = json.loads(api_request.data.decode("utf-8"))
        self.assertEqual(request_body["model"], "gpt-5.6-luna")
        self.assertEqual(request_body["input"], original)
        self.assertFalse(request_body["store"])
        self.assertIn("لا تخترع", request_body["instructions"])
        self.assertIn("بين 350 و450 حرفًا", request_body["instructions"])
        self.assertIn("لا تتجاوز 600 حرف", request_body["instructions"])
        self.assertNotIn("يجب ألا يُرسل", api_request.data.decode("utf-8"))
        self.assertNotIn("test-report-ai-key", api_request.data.decode("utf-8"))

    @patch("reports.ai_client.urlopen")
    def test_short_text_is_rejected_without_api_call(self, mocked_urlopen):
        self._login()

        response = self.client.post(
            reverse("reports:improve_report_text"),
            data=json.dumps({"text": "نص قصير"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["ok"])
        self.assertIn("20 حرفًا", response.json()["message"])
        mocked_urlopen.assert_not_called()

    @patch("reports.ai_client.urlopen")
    def test_report_text_over_600_characters_is_rejected_without_api_call(self, mocked_urlopen):
        self._login()

        response = self.client.post(
            reverse("reports:improve_report_text"),
            data=json.dumps({"text": "أ" * 601}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("600 حرفًا", response.json()["message"])
        self.assertEqual(report_ai_daily_remaining(self.teacher.pk), REPORT_AI_DAILY_LIMIT)
        mocked_urlopen.assert_not_called()

    @patch("reports.ai_client.urlopen")
    def test_ai_rewrite_over_600_characters_is_rejected_and_refunded(self, mocked_urlopen):
        self._login()
        mocked_urlopen.return_value = _FakeTextOpenAIResponse("ب" * 601)

        response = self.client.post(
            reverse("reports:improve_report_text"),
            data=json.dumps({"text": "أ" * 320}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("أطول من المساحة", response.json()["message"])
        self.assertEqual(report_ai_daily_remaining(self.teacher.pk), REPORT_AI_DAILY_LIMIT)

    @patch(
        "reports.ai_client.urlopen",
        # The rewrite has to match the text being improved: a canned reply that
        # introduces a figure the teacher never wrote is now refused outright.
        return_value=_FakeTextOpenAIResponse(
            "نُفِّذ برنامج تدريبي للمعلمين بهدف تحسين الممارسات التعليمية داخل المدرسة."
        ),
    )
    def test_daily_limit_allows_three_successes_then_blocks_without_api_call(self, mocked_urlopen):
        self._login()
        url = reverse("reports:improve_report_text")
        payload = json.dumps(
            {"text": "تم تنفيذ برنامج تدريبي للمعلمين بهدف تحسين الممارسات التعليمية."}
        )

        for expected_remaining in (2, 1, 0):
            response = self.client.post(url, data=payload, content_type="application/json")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["remaining"], expected_remaining)

        limited = self.client.post(url, data=payload, content_type="application/json")

        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited.json()["remaining"], 0)
        self.assertIn("الثلاثة", limited.json()["message"])
        self.assertEqual(mocked_urlopen.call_count, 3)

    @patch("reports.ai_client.urlopen", side_effect=_spend_limit_error())
    def test_spend_limit_returns_clear_service_paused_message(self, _mocked_urlopen):
        self._login()

        response = self.client.post(
            reverse("reports:improve_report_text"),
            data=json.dumps(
                {"text": "تم تنفيذ برنامج تدريبي للمعلمين بهدف تحسين الممارسات التعليمية."}
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json()["ok"])
        self.assertEqual(response.json()["message"], AI_SERVICE_PAUSED_MESSAGE)
        self.assertEqual(report_ai_daily_remaining(self.teacher.pk), REPORT_AI_DAILY_LIMIT)

    @override_settings(REPORT_AI_ENABLED=False)
    @patch("reports.ai_client.urlopen")
    def test_failed_service_call_does_not_consume_daily_allowance(self, mocked_urlopen):
        self._login()

        response = self.client.post(
            reverse("reports:improve_report_text"),
            data=json.dumps({"text": "هذا نص تقرير مكتمل يحتاج إلى تحسين الصياغة اللغوية."}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(report_ai_daily_remaining(self.teacher.pk), REPORT_AI_DAILY_LIMIT)
        mocked_urlopen.assert_not_called()

    @patch("reports.ai_client.urlopen")
    def test_rewrite_that_changes_a_figure_is_rejected_and_refunded(self, mocked_urlopen):
        """A polished sentence with the wrong number is worse than no polish."""
        self._login()
        mocked_urlopen.return_value = _FakeTextOpenAIResponse(
            "نُفّذ البرنامج صباح يوم الأحد، واستفاد منه 53 طالبًا، وتضمّن أنشطة توعوية منظمة."
        )

        response = self.client.post(
            reverse("reports:improve_report_text"),
            data=json.dumps(
                {"text": "نفذنا برنامج يوم الأحد واستفاد 35 طالب وكان فيه أنشطة توعوية."}
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["ok"])
        self.assertIn("الأرقام", response.json()["message"])
        self.assertEqual(report_ai_daily_remaining(self.teacher.pk), REPORT_AI_DAILY_LIMIT)

    @patch("reports.ai_client.urlopen")
    def test_rewrite_that_drops_a_figure_is_rejected(self, mocked_urlopen):
        self._login()
        mocked_urlopen.return_value = _FakeTextOpenAIResponse(
            "نُفّذ البرنامج صباح يوم الأحد وتضمّن أنشطة توعوية منظمة حققت أهدافها المرجوة."
        )

        response = self.client.post(
            reverse("reports:improve_report_text"),
            data=json.dumps(
                {"text": "نفذنا برنامج يوم الأحد واستفاد 35 طالب وكان فيه أنشطة توعوية."}
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("الأرقام", response.json()["message"])

    @patch("reports.ai_client.urlopen")
    def test_arabic_indic_digits_count_as_the_same_figure(self, mocked_urlopen):
        self._login()
        mocked_urlopen.return_value = _FakeTextOpenAIResponse(
            "نُفّذ البرنامج صباح يوم الأحد، واستفاد منه 35 طالبًا، وتضمّن أنشطة توعوية منظمة."
        )

        response = self.client.post(
            reverse("reports:improve_report_text"),
            data=json.dumps(
                {"text": "نفذنا برنامج يوم الأحد واستفاد ٣٥ طالب وكان فيه أنشطة توعوية."}
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])

    @patch("reports.ai_client.urlopen")
    def test_rewrite_that_summarises_instead_of_editing_is_rejected(self, mocked_urlopen):
        self._login()
        mocked_urlopen.return_value = _FakeTextOpenAIResponse("نُفّذ برنامج توعوي ناجح.")

        response = self.client.post(
            reverse("reports:improve_report_text"),
            data=json.dumps(
                {
                    "text": (
                        "نفذنا برنامج توعوي يوم الأحد بمشاركة معلمي القسم، وتضمن ورش عمل "
                        "وأنشطة تفاعلية، وتفاعل الطلاب معه بشكل جيد وحقق أهدافه المرجوة."
                    )
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("اختصرت", response.json()["message"])
        self.assertEqual(report_ai_daily_remaining(self.teacher.pk), REPORT_AI_DAILY_LIMIT)

    @override_settings(REPORT_AI_ENABLED=False)
    @patch("reports.ai_client.urlopen")
    def test_disabled_feature_returns_safe_service_message(self, mocked_urlopen):
        self._login()

        response = self.client.post(
            reverse("reports:improve_report_text"),
            data=json.dumps({"text": "هذا نص تقرير مكتمل يحتاج إلى تحسين الصياغة اللغوية."}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json()["ok"])
        self.assertIn("غير مفعلة", response.json()["message"])
        self.assertEqual(response.json()["remaining"], REPORT_AI_DAILY_LIMIT)
        mocked_urlopen.assert_not_called()

    @patch("reports.ai_client.urlopen")
    def test_a_rewrite_cut_off_at_the_token_ceiling_is_never_delivered(
        self, mocked_urlopen
    ):
        """نصف جملة تُعتمَد تقريراً رسمياً أسوأ من خطأ صريح.

        ``max_output_tokens`` سقفٌ للتفكير والإخراج المرئي معاً، فقد يلتهم
        التفكيرُ الميزانية ويعود النصّ مقطوعاً — وحقل ``output_text`` يبدو
        سليماً تماماً، فلا يفضحه إلا ``status``. والنصّ هنا يحمل الأرقام نفسها
        وطولاً معقولاً، أي أنه يعبر حارسَي الأرقام والطول بلا مشكلة.
        """
        self._login()
        mocked_urlopen.return_value = _FakeTextOpenAIResponse(
            "نُفّذ البرنامج صباح يوم الأحد، واستفاد منه 35 طالبًا، وتضمّن أنشطة توعوية و",
            status="incomplete",
            reason="max_output_tokens",
        )

        response = self.client.post(
            reverse("reports:improve_report_text"),
            data=json.dumps(
                {"text": "نفذنا برنامج يوم الأحد واستفاد 35 طالب وكان فيه أنشطة توعوية."}
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["ok"])
        self.assertIn("لم تكتمل", response.json()["message"])
        # ولا تُحتسب محاولة على صياغة لم تُسلَّم.
        self.assertEqual(report_ai_daily_remaining(self.teacher.pk), REPORT_AI_DAILY_LIMIT)
