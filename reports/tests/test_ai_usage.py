from __future__ import annotations

import json
from decimal import Decimal
from io import BytesIO, StringIO
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings

from reports.ai_usage import (
    ai_usage_context,
    current_context,
    estimate_cost,
    model_pricing,
    record_ai_call,
    usage_numbers,
)
from reports.models import (
    AiUsageEvent,
    School,
    SchoolMembership,
    SchoolSubscription,
    SubscriptionPlan,
    Teacher,
)
from reports.report_review import review_report_draft


PRICING = {
    "gpt-5.6-luna": {"input": 0.05, "cached_input": 0.005, "output": 0.40},
}


def _payload(*, status="completed", text="{}", usage=True):
    body = {
        "status": status,
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
    }
    if usage:
        body["usage"] = {
            "input_tokens": 2000,
            "input_tokens_details": {"cached_tokens": 1500},
            "output_tokens": 300,
            "output_tokens_details": {"reasoning_tokens": 120},
        }
    return body


class _FakeResponse:
    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return json.dumps(self._body, ensure_ascii=False).encode("utf-8")


def _draft():
    return {
        "title": "برنامج التوعية بالأمن السيبراني",
        "category": "activity",
        "report_date": "2026-09-01",
        "goal": "رفع وعي طلاب المرحلة الثانوية بمخاطر مشاركة كلمات المرور.",
        "idea": (
            "نُفّذ البرنامج في قاعة النشاط بحضور طلاب الصفوف الثلاثة، وتضمّن عرضًا "
            "تعريفيًا ثم ورشة عملية على أمثلة واقعية من رسائل التصيّد."
        ),
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


class PricingTests(TestCase):
    def test_no_configured_price_means_no_invented_cost(self):
        """سعرٌ مفترض يُنتج فاتورة تبدو دقيقة وهي ليست كذلك."""
        with override_settings(AI_MODEL_PRICING={}):
            self.assertIsNone(
                estimate_cost("gpt-5.6-luna", input_tokens=1000, cached_input_tokens=0, output_tokens=100)
            )

    @override_settings(AI_MODEL_PRICING=PRICING)
    def test_cached_input_is_charged_at_its_own_rate(self):
        full = estimate_cost(
            "gpt-5.6-luna", input_tokens=1_000_000, cached_input_tokens=0, output_tokens=0
        )
        cached = estimate_cost(
            "gpt-5.6-luna", input_tokens=1_000_000, cached_input_tokens=1_000_000, output_tokens=0
        )
        self.assertEqual(full, Decimal("0.050000"))
        self.assertEqual(cached, Decimal("0.005000"))

    @override_settings(AI_MODEL_PRICING={"m": {"input": 1, "output": 2}})
    def test_missing_cached_rate_falls_back_to_the_full_input_rate(self):
        """تقديرٌ أعلى من الحقيقة — وهو الاتجاه الآمن في تقدير فاتورة."""
        cost = estimate_cost("m", input_tokens=1_000_000, cached_input_tokens=1_000_000, output_tokens=0)
        self.assertEqual(cost, Decimal("1.000000"))

    @override_settings(AI_MODEL_PRICING='{"m": {"input": 1, "output": 2}}')
    def test_pricing_may_arrive_as_a_json_string(self):
        self.assertIn("m", model_pricing())

    @override_settings(AI_MODEL_PRICING="not json at all")
    def test_broken_pricing_config_disables_costing_instead_of_crashing(self):
        self.assertEqual(model_pricing(), {})

    @override_settings(AI_MODEL_PRICING={"m": {"input": "oops", "output": 2}})
    def test_an_unreadable_price_is_dropped_not_guessed(self):
        self.assertEqual(model_pricing(), {})

    @override_settings(AI_MODEL_PRICING=PRICING)
    def test_a_call_with_no_tokens_has_unknown_cost_not_zero_cost(self):
        """التفريغ الصوتي لا يعيد ``usage``، و«$0.0000» في تقريرٍ يعني «مجاني»."""
        self.assertIsNone(
            estimate_cost("gpt-5.6-luna", input_tokens=0, cached_input_tokens=0, output_tokens=0)
        )

    def test_unknown_model_stays_unpriced(self):
        with override_settings(AI_MODEL_PRICING=PRICING):
            self.assertIsNone(
                estimate_cost("some-other-model", input_tokens=10, cached_input_tokens=0, output_tokens=10)
            )


class UsageNumbersTests(TestCase):
    def test_reads_the_nested_details(self):
        numbers = usage_numbers(_payload())
        self.assertEqual(numbers, {"input": 2000, "cached": 1500, "output": 300, "reasoning": 120})

    def test_a_response_without_usage_reads_as_zeros(self):
        """مسار التفريغ الصوتي لا يعيد ``usage`` — وتُسجَّل الواقعة رغم ذلك."""
        self.assertEqual(
            usage_numbers({"text": "..."}),
            {"input": 0, "cached": 0, "output": 0, "reasoning": 0},
        )

    def test_malformed_usage_does_not_raise(self):
        self.assertEqual(
            usage_numbers({"usage": {"input_tokens": "many", "input_tokens_details": []}}),
            {"input": 0, "cached": 0, "output": 0, "reasoning": 0},
        )


class UsageContextTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(name="مدرسة القياس", code="ai-usage")
        self.teacher = Teacher.objects.create_user(
            phone="500009001", name="معلم القياس", password="test-pass"
        )

    def test_context_is_restored_on_exit(self):
        self.assertIsNone(current_context().school_id)
        with ai_usage_context(school=self.school, teacher=self.teacher):
            self.assertEqual(current_context().school_id, self.school.pk)
            self.assertEqual(current_context().teacher_id, self.teacher.pk)
        self.assertIsNone(current_context().school_id)

    def test_context_is_restored_even_when_the_call_raises(self):
        with self.assertRaises(ValueError):
            with ai_usage_context(school=self.school, teacher=self.teacher):
                raise ValueError("boom")
        self.assertIsNone(current_context().school_id)

    def test_an_anonymous_visitor_records_without_attribution(self):
        with ai_usage_context(school=None, teacher=None):
            record_ai_call(stage="mansour", model="gpt-5.6-luna", outcome="success", payload=_payload())
        event = AiUsageEvent.objects.get()
        self.assertIsNone(event.school_id)
        self.assertIsNone(event.teacher_id)


@override_settings(AI_MODEL_PRICING=PRICING, AI_USAGE_TRACKING_ENABLED=True)
class RecordingTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(name="مدرسة القياس", code="ai-usage")

    def test_a_successful_call_stores_tokens_and_frozen_cost(self):
        with ai_usage_context(school=self.school):
            record_ai_call(
                stage="report-review",
                model="gpt-5.6-luna",
                outcome="success",
                payload=_payload(),
                duration_ms=1234,
            )
        event = AiUsageEvent.objects.get()
        self.assertEqual(event.stage, AiUsageEvent.Stage.REPORT_REVIEW)
        self.assertEqual(event.input_tokens, 2000)
        self.assertEqual(event.cached_input_tokens, 1500)
        self.assertEqual(event.output_tokens, 300)
        self.assertEqual(event.reasoning_tokens, 120)
        self.assertEqual(event.duration_ms, 1234)
        self.assertEqual(event.school_id, self.school.pk)
        # 500 إدخال طازج + 1500 مخزَّن + 300 إخراج
        expected = (
            Decimal(500) * Decimal("0.05")
            + Decimal(1500) * Decimal("0.005")
            + Decimal(300) * Decimal("0.40")
        ) / Decimal(1_000_000)
        self.assertEqual(event.estimated_cost, expected.quantize(Decimal("0.000001")))
        self.assertAlmostEqual(event.cache_hit_ratio, 0.75)
        self.assertEqual(event.billable_input_tokens, 500)

    def test_a_cost_stays_frozen_when_the_price_changes_later(self):
        """كلفةُ شهرٍ ماضٍ لا تُعاد كتابتها بسعر اليوم."""
        with ai_usage_context(school=self.school):
            record_ai_call(stage="mansour", model="gpt-5.6-luna", outcome="success", payload=_payload())
        original = AiUsageEvent.objects.get().estimated_cost

        with override_settings(AI_MODEL_PRICING={"gpt-5.6-luna": {"input": 99, "output": 99}}):
            self.assertEqual(AiUsageEvent.objects.get().estimated_cost, original)

    def test_an_unknown_stage_is_stored_as_other_rather_than_dropped(self):
        record_ai_call(stage="something-new", model="m", outcome="success", payload=_payload())
        self.assertEqual(AiUsageEvent.objects.get().stage, AiUsageEvent.Stage.OTHER)

    @override_settings(AI_USAGE_TRACKING_ENABLED=False)
    def test_the_switch_stops_recording(self):
        record_ai_call(stage="mansour", model="m", outcome="success", payload=_payload())
        self.assertEqual(AiUsageEvent.objects.count(), 0)

    def test_a_failing_recorder_never_breaks_the_call_it_measures(self):
        with patch(
            "reports.models.AiUsageEvent.objects.create", side_effect=RuntimeError("db down")
        ):
            record_ai_call(stage="mansour", model="m", outcome="success", payload=_payload())
        self.assertEqual(AiUsageEvent.objects.count(), 0)


@override_settings(
    OPENAI_API_KEY="test-key",
    REPORT_REVIEW_ENABLED=True,
    AI_MODEL_PRICING=PRICING,
)
class EndToEndRecordingTests(TestCase):
    """القياس مركّب على الباب الموحّد، فيكفي مسارٌ واحد لإثبات أنه يمرّ به."""

    def setUp(self):
        cache.clear()
        self.school = School.objects.create(name="مدرسة القياس", code="ai-usage-e2e")

    def test_a_real_review_call_writes_one_event(self):
        response = _FakeResponse(_payload(text=json.dumps({"issues": [], "strengths": []})))
        with patch("reports.ai_client.urlopen", return_value=response):
            with ai_usage_context(school=self.school):
                result = review_report_draft(_draft(), user_id=1)

        self.assertTrue(result["semantic"])
        event = AiUsageEvent.objects.get()
        self.assertEqual(event.stage, AiUsageEvent.Stage.REPORT_REVIEW)
        self.assertEqual(event.outcome, AiUsageEvent.Outcome.SUCCESS)
        self.assertEqual(event.model_name, "gpt-5.6-luna")
        self.assertEqual(event.school_id, self.school.pk)
        self.assertGreater(event.input_tokens, 0)

    def test_a_truncated_response_is_recorded_apart_from_a_failure(self):
        """البتر يُدفع ثمنه كاملاً، وعلاجه رفع السقف لا إعادة المحاولة."""
        response = _FakeResponse(_payload(status="incomplete"))
        with patch("reports.ai_client.urlopen", return_value=response):
            review_report_draft(_draft(), user_id=2)

        event = AiUsageEvent.objects.get()
        self.assertEqual(event.outcome, AiUsageEvent.Outcome.TRUNCATED)
        # الرموز مسجَّلة رغم البتر — لأنها كُلّفت فعلاً.
        self.assertEqual(event.input_tokens, 2000)

    def test_a_failed_call_is_recorded_with_its_error_kind(self):
        error = HTTPError("https://api.openai.com/v1/responses", 500, "boom", {}, BytesIO(b"{}"))
        with patch("reports.ai_client.urlopen", side_effect=error):
            review_report_draft(_draft(), user_id=3)

        event = AiUsageEvent.objects.get()
        self.assertEqual(event.outcome, AiUsageEvent.Outcome.FAILED)
        self.assertEqual(event.error_kind, "HTTP 500")
        self.assertEqual(event.input_tokens, 0)

    def test_a_network_failure_records_the_exception_name(self):
        with patch("reports.ai_client.urlopen", side_effect=URLError("offline")):
            review_report_draft(_draft(), user_id=4)
        self.assertEqual(AiUsageEvent.objects.get().error_kind, "URLError")

    def test_a_cached_review_result_makes_no_second_event(self):
        response = _FakeResponse(_payload(text=json.dumps({"issues": [], "strengths": []})))
        with patch("reports.ai_client.urlopen", return_value=response):
            review_report_draft(_draft(), user_id=5)
            review_report_draft(_draft(), user_id=5)
        self.assertEqual(AiUsageEvent.objects.count(), 1)


class UsageReportCommandTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(name="مدرسة التقرير", code="ai-report")

    def _event(self, **kwargs):
        defaults = {
            "stage": AiUsageEvent.Stage.MANSOUR,
            "model_name": "gpt-5.6-luna",
            "outcome": AiUsageEvent.Outcome.SUCCESS,
            "input_tokens": 2000,
            "cached_input_tokens": 1500,
            "output_tokens": 300,
            "duration_ms": 900,
            "school": self.school,
        }
        defaults.update(kwargs)
        return AiUsageEvent.objects.create(**defaults)

    def test_empty_window_reports_nothing_rather_than_crashing(self):
        out = StringIO()
        call_command("ai_usage_report", "--days", "7", stdout=out)
        self.assertIn("لا توجد نداءات", out.getvalue())

    def test_report_answers_the_four_questions(self):
        self._event()
        self._event(stage=AiUsageEvent.Stage.MANSOUR_REWRITE)
        self._event(outcome=AiUsageEvent.Outcome.TRUNCATED)
        self._event(outcome=AiUsageEvent.Outcome.FAILED, error_kind="HTTP 500")

        out = StringIO()
        with override_settings(AI_MODEL_PRICING=PRICING):
            call_command("ai_usage_report", "--days", "30", stdout=out)
        text = out.getvalue()

        self.assertIn("إصابة المخزَّن", text)
        self.assertIn("75.0%", text)          # ‎1500 من 2000‎
        self.assertIn("إعادة صياغة منصور", text)
        self.assertIn("مدرسة التقرير", text)
        self.assertIn("المبتور", text)

    def test_arabic_counts_use_the_dual_and_plural_forms(self):
        for _ in range(6):
            self._event(stage=AiUsageEvent.Stage.MANSOUR_REWRITE)
        for _ in range(46):
            self._event(stage=AiUsageEvent.Stage.MANSOUR)

        out = StringIO()
        call_command("ai_usage_report", stdout=out)
        text = out.getvalue()
        self.assertIn("6 إعادات", text)   # ٣–١٠ جمع
        self.assertIn("46 ردًّا", text)    # ما فوق العشرة مفردٌ منصوب
        self.assertNotIn("6 إعادة من", text)

    def test_report_warns_when_no_price_is_configured(self):
        self._event()
        out = StringIO()
        with override_settings(AI_MODEL_PRICING={}):
            call_command("ai_usage_report", stdout=out)
        self.assertIn("AI_MODEL_PRICING غير مضبوط", out.getvalue())


class RetentionTests(TestCase):
    def test_cleanup_removes_only_rows_past_the_window(self):
        from datetime import timedelta

        from django.utils import timezone

        from reports.tasks import cleanup_ai_usage_task

        fresh = AiUsageEvent.objects.create(stage=AiUsageEvent.Stage.MANSOUR, model_name="m")
        stale = AiUsageEvent.objects.create(stage=AiUsageEvent.Stage.MANSOUR, model_name="m")
        # ``auto_now_add`` يتجاهل القيمة الممرّرة، فيُزحزح الوقت بعد الإنشاء.
        AiUsageEvent.objects.filter(pk=stale.pk).update(
            created_at=timezone.now() - timedelta(days=200)
        )

        deleted = cleanup_ai_usage_task(days=180)
        self.assertEqual(deleted, 1)
        self.assertTrue(AiUsageEvent.objects.filter(pk=fresh.pk).exists())
        self.assertFalse(AiUsageEvent.objects.filter(pk=stale.pk).exists())


@override_settings(OPENAI_API_KEY="test-key", REPORT_AI_ENABLED=True, AI_MODEL_PRICING=PRICING)
class ViewAttributionTests(TestCase):
    """النسبة تُوضع عند حافة الطلب، فتُختبر من الطلب."""

    def setUp(self):
        cache.clear()
        self.school = School.objects.create(name="مدرسة النسبة", code="ai-attr")
        plan = SubscriptionPlan.objects.create(
            name="خطة النسبة", price=0, days_duration=30, max_teachers=20
        )
        SchoolSubscription.objects.create(school=self.school, plan=plan)
        self.teacher = Teacher.objects.create_user(
            phone="500009101", name="معلم النسبة", password="test-pass"
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.teacher,
            role_type=SchoolMembership.RoleType.TEACHER,
        )

    def test_improving_a_report_is_attributed_to_its_school_and_teacher(self):
        from django.urls import reverse

        self.client.force_login(self.teacher)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()

        improved = (
            "نُفّذ البرنامج صباح الأحد واستفاد منه 35 طالبًا، وتضمّن أنشطة توعوية "
            "منظمة حققت الأهداف المذكورة في الخطة."
        )
        response = _FakeResponse(_payload(text=improved))
        with patch("reports.ai_client.urlopen", return_value=response):
            result = self.client.post(
                reverse("reports:improve_report_text"),
                data=json.dumps(
                    {
                        "text": (
                            "تم تنفيذ البرنامج صباح الأحد واستفاد منه 35 طالب وكان فيه "
                            "أنشطة توعوية حققت الأهداف."
                        )
                    },
                    ensure_ascii=False,
                ),
                content_type="application/json",
            )

        self.assertEqual(result.status_code, 200)
        event = AiUsageEvent.objects.get()
        self.assertEqual(event.stage, AiUsageEvent.Stage.REPORT_IMPROVE)
        self.assertEqual(event.school_id, self.school.pk)
        self.assertEqual(event.teacher_id, self.teacher.pk)
