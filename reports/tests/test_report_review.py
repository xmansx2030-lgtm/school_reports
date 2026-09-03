from __future__ import annotations

import json
from datetime import date
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from reports.models import (
    Report,
    ReportType,
    School,
    SchoolMembership,
    SchoolSubscription,
    SubscriptionPlan,
    Teacher,
)
from reports.report_review import (
    LEVEL_READY,
    SEVERITY_HIGH,
    SEVERITY_MEDIUM,
    normalise_draft,
    review_daily_remaining,
    review_report_draft,
    score_for,
    structural_issues,
)


def _complete_draft(**overrides):
    """مسودة سليمة الأركان — نقطة البدء التي تُدخِل عليها كل حالة عيبها."""
    payload = {
        "title": "برنامج التوعية بالأمن السيبراني",
        "category": "activity",
        "report_date": "2026-09-01",
        "goal": "رفع وعي طلاب المرحلة الثانوية بمخاطر مشاركة كلمات المرور.",
        "idea": (
            "نُفّذ البرنامج في قاعة النشاط بحضور طلاب الصفوف الثلاثة، وتضمّن عرضًا "
            "تعريفيًا ثم ورشة عملية على أمثلة واقعية من رسائل التصيّد، وختامًا نقاشًا "
            "مفتوحًا أجاب فيه المنفّذ عن أسئلة الطلاب."
        ),
        "implementation_method": "عرض تقديمي ثم ورشة تطبيقية على أجهزة المعمل ثم نقاش ختامي.",
        "results": "شارك 45 طالبًا، وارتفعت نسبة الإجابات الصحيحة في الاختبار البعدي إلى 82%.",
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
    sections = overrides.pop("sections", None)
    payload.update(overrides)
    if sections is not None:
        payload["sections"] = sections
    return payload


class _FakeResponse:
    """ردٌّ مهيكل كما يصل من واجهة الاستجابات."""

    def __init__(self, parsed, *, status="completed"):
        self._parsed = parsed
        self._status = status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return json.dumps(
            {
                "status": self._status,
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {"type": "output_text", "text": json.dumps(self._parsed, ensure_ascii=False)}
                        ],
                    }
                ],
                "usage": {"input_tokens": 800, "output_tokens": 120},
            },
            ensure_ascii=False,
        ).encode("utf-8")


class StructuralReviewTests(TestCase):
    """الفحص البنيوي — يعمل بلا نموذج، فيُختبر بلا نموذج."""

    def test_complete_draft_has_no_structural_issues(self):
        issues = structural_issues(normalise_draft(_complete_draft()))
        self.assertEqual(issues, [], msg=f"مسودة سليمة أُخذ عليها: {issues}")

    def test_enabled_but_empty_section_is_a_blocking_issue(self):
        draft = normalise_draft(_complete_draft(results=""))
        issues = structural_issues(draft)
        results = [issue for issue in issues if issue["field"] == "results"]
        self.assertTrue(results)
        self.assertEqual(results[0]["severity"], SEVERITY_HIGH)

    def test_disabled_section_is_never_asked_about(self):
        """البند المطفأ ليس جزءاً من التقرير — والتنبيه عليه يُغلق الأداة."""
        sections = _complete_draft()["sections"].copy()
        sections["show_results"] = False
        sections["show_recommendations"] = False
        draft = normalise_draft(_complete_draft(results="", recommendations="", sections=sections))
        fields = {issue["field"] for issue in structural_issues(draft)}
        self.assertNotIn("results", fields)
        self.assertNotIn("recommendations", fields)

    def test_missing_evidence_is_flagged(self):
        draft = normalise_draft(_complete_draft(evidence_count=0))
        fields = {issue["field"] for issue in structural_issues(draft)}
        self.assertIn("evidence", fields)

    def test_results_without_any_figure_are_flagged(self):
        draft = normalise_draft(
            _complete_draft(
                results="تحسّن مستوى الطلاب وزاد تفاعلهم بشكل ملحوظ خلال البرنامج.",
                beneficiaries_count="",
                sections={
                    "show_goal": True,
                    "show_details": True,
                    "show_implementation": True,
                    "show_results": True,
                    "show_recommendations": True,
                    "show_beneficiaries": False,
                },
            )
        )
        messages = [issue["message"] for issue in structural_issues(draft) if issue["field"] == "results"]
        self.assertTrue(any("رقم" in message for message in messages))

    def test_a_field_copied_into_another_is_caught(self):
        """أكثر ما يُرجع التقارير: «آلية التنفيذ» نسخةٌ من «التفاصيل»."""
        shared = (
            "نُفّذ البرنامج في قاعة النشاط بحضور طلاب الصفوف الثلاثة، وتضمّن عرضًا "
            "تعريفيًا ثم ورشة عملية على أمثلة واقعية من رسائل التصيّد."
        )
        draft = normalise_draft(_complete_draft(idea=shared, implementation_method=shared))
        fields = {issue["field"] for issue in structural_issues(draft)}
        self.assertIn("implementation_method", fields)

    def test_score_is_a_pure_function_of_the_issues(self):
        """الدرجة تُحسب هنا لا في النموذج، فهي ثابتة على المدخل نفسه."""
        issues = [{"severity": SEVERITY_HIGH}, {"severity": SEVERITY_MEDIUM}]
        self.assertEqual(score_for(issues), 100 - 22 - 10)
        self.assertEqual(score_for([]), 100)
        self.assertEqual(score_for([{"severity": SEVERITY_HIGH}] * 9), 0)


@override_settings(
    OPENAI_API_KEY="test-key",
    REPORT_REVIEW_ENABLED=True,
    REPORT_REVIEW_DAILY_LIMIT=5,
)
class SemanticReviewTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_semantic_notes_are_merged_and_scored(self):
        parsed = {
            "issues": [
                {
                    "field": "recommendations",
                    "severity": "medium",
                    "message": "التوصية عامة ولا ترتبط بنتيجة الاختبار البعدي المذكورة.",
                    "hint": "اربط التوصية بالنسبة التي وصلت إليها.",
                }
            ],
            "strengths": ["الهدف محدد والنتائج تقيسه برقم."],
        }
        with patch("reports.ai_client.urlopen", return_value=_FakeResponse(parsed)):
            result = review_report_draft(_complete_draft(), user_id=1)

        self.assertTrue(result["semantic"])
        self.assertEqual(result["score"], 90)
        self.assertEqual([issue["field"] for issue in result["issues"]], ["recommendations"])
        self.assertEqual(result["issues"][0]["source"], "ai")
        self.assertEqual(result["issues"][0]["field_label"], "التوصيات")
        self.assertEqual(result["issues"][0]["anchor"], "#id_recommendations")
        self.assertEqual(result["strengths"], ["الهدف محدد والنتائج تقيسه برقم."])
        self.assertEqual(result["remaining"], 4)

    def test_a_ready_report_with_a_note_says_both(self):
        """«جاهز للإرسال» فوق ملاحظةٍ ظاهرة تناقضٌ يقرؤه المستخدم في سطرين."""
        parsed = {
            "issues": [
                {
                    "field": "recommendations",
                    "severity": "low",
                    "message": "يمكن ربط التوصية بنتيجة الاختبار البعدي.",
                    "hint": "اذكر النسبة التي وصلت إليها.",
                }
            ],
            "strengths": [],
        }
        with patch("reports.ai_client.urlopen", return_value=_FakeResponse(parsed)):
            result = review_report_draft(_complete_draft(), user_id=71)

        self.assertEqual(result["level"], LEVEL_READY)
        self.assertTrue(result["ready"])
        self.assertEqual(result["headline"], "جاهز للإرسال، وفيه ما يمكن تحسينه")

    def test_a_clean_report_keeps_the_plain_headline(self):
        with patch(
            "reports.ai_client.urlopen",
            return_value=_FakeResponse({"issues": [], "strengths": []}),
        ):
            result = review_report_draft(_complete_draft(), user_id=72)
        self.assertEqual(result["headline"], "التقرير جاهز للإرسال")

    def test_note_about_a_disabled_section_is_dropped(self):
        """المخطَّط الصارم يضمن الشكل لا الصدق: بندٌ مطفأ لا يُعلَّق عليه."""
        sections = _complete_draft()["sections"].copy()
        sections["show_recommendations"] = False
        parsed = {
            "issues": [
                {
                    "field": "recommendations",
                    "severity": "high",
                    "message": "لا توجد توصيات في التقرير إطلاقًا.",
                    "hint": "أضف توصيتين.",
                }
            ],
            "strengths": [],
        }
        with patch("reports.ai_client.urlopen", return_value=_FakeResponse(parsed)):
            result = review_report_draft(
                _complete_draft(recommendations="", sections=sections), user_id=1
            )
        self.assertEqual(result["issues"], [])
        self.assertEqual(result["level"], LEVEL_READY)

    def test_links_are_stripped_from_generated_notes(self):
        parsed = {
            "issues": [
                {
                    "field": "results",
                    "severity": "low",
                    "message": "راجع الدليل على https://example.com/guide قبل الإرسال هنا.",
                    "hint": "افتح /guide/#reports لمزيد من التفاصيل الآن.",
                }
            ],
            "strengths": [],
        }
        with patch("reports.ai_client.urlopen", return_value=_FakeResponse(parsed)):
            result = review_report_draft(_complete_draft(), user_id=1)
        issue = result["issues"][0]
        self.assertNotIn("https://", issue["message"])
        self.assertNotIn("/guide", issue["hint"])

    def test_structural_blocker_skips_the_model_and_costs_nothing(self):
        """من ترك التصنيف فارغاً لا يحتاج مراجعاً دلالياً بعد، بل أن يُكمل."""
        with patch("reports.ai_client.urlopen") as urlopen:
            result = review_report_draft(_complete_draft(category=""), user_id=7)
        urlopen.assert_not_called()
        self.assertFalse(result["semantic"])
        self.assertEqual(result["reason"], "structure_first")
        # ملاحظةٌ واحدة توجب المراجعة في تقرير مكتمل تبقيه «قريباً» لا «ناقصاً»،
        # لكنها تمنع «جاهز» مهما ارتفعت الدرجة.
        self.assertFalse(result["ready"])
        self.assertNotEqual(result["level"], LEVEL_READY)
        self.assertEqual(review_daily_remaining(7), 5)

    def test_identical_content_is_served_from_cache_without_spending(self):
        parsed = {"issues": [], "strengths": ["نتائج مقيسة برقم."]}
        with patch("reports.ai_client.urlopen", return_value=_FakeResponse(parsed)) as urlopen:
            first = review_report_draft(_complete_draft(), user_id=11)
            second = review_report_draft(_complete_draft(), user_id=11)

        self.assertEqual(urlopen.call_count, 1)
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(second["remaining"], first["remaining"])
        self.assertEqual(second["issues"], first["issues"])

    def test_edited_content_is_checked_again(self):
        parsed = {"issues": [], "strengths": []}
        with patch("reports.ai_client.urlopen", return_value=_FakeResponse(parsed)) as urlopen:
            review_report_draft(_complete_draft(), user_id=12)
            review_report_draft(_complete_draft(results="شارك 60 طالبًا وارتفعت النتيجة إلى 90%."), user_id=12)
        self.assertEqual(urlopen.call_count, 2)

    def test_provider_failure_degrades_to_the_structural_result(self):
        """الأداة لا تفشل: تعذّر النداء ينقص النتيجة ولا يُسقطها، ولا يُحتسب."""
        error = HTTPError("https://api.openai.com/v1/responses", 500, "boom", {}, BytesIO(b"{}"))
        with patch("reports.ai_client.urlopen", side_effect=error):
            result = review_report_draft(_complete_draft(evidence_count=0), user_id=21)

        self.assertFalse(result["semantic"])
        self.assertEqual(review_daily_remaining(21), 5)
        self.assertIn("evidence", {issue["field"] for issue in result["issues"]})

    def test_network_failure_degrades_the_same_way(self):
        with patch("reports.ai_client.urlopen", side_effect=URLError("offline")):
            result = review_report_draft(_complete_draft(), user_id=22)
        self.assertFalse(result["semantic"])
        self.assertEqual(review_daily_remaining(22), 5)

    def test_truncated_json_is_discarded_rather_than_half_read(self):
        parsed = {"issues": [], "strengths": []}
        with patch(
            "reports.ai_client.urlopen",
            return_value=_FakeResponse(parsed, status="incomplete"),
        ):
            result = review_report_draft(_complete_draft(), user_id=23)
        self.assertFalse(result["semantic"])
        self.assertEqual(review_daily_remaining(23), 5)

    def test_daily_limit_stops_the_paid_pass_but_not_the_free_one(self):
        parsed = {"issues": [], "strengths": []}
        variants = [
            _complete_draft(title=f"برنامج التوعية رقم {index} في المدرسة")
            for index in range(6)
        ]
        with patch("reports.ai_client.urlopen", return_value=_FakeResponse(parsed)) as urlopen:
            for payload in variants[:5]:
                self.assertTrue(review_report_draft(payload, user_id=31)["semantic"])
            last = review_report_draft(variants[5], user_id=31)

        self.assertEqual(urlopen.call_count, 5)
        self.assertFalse(last["semantic"])
        self.assertEqual(last["reason"], "quota_exhausted")
        self.assertEqual(last["remaining"], 0)
        self.assertTrue(last["headline"])

    def test_request_body_asks_for_a_strict_schema_and_no_storage(self):
        captured = {}

        def _capture(request, *args, **kwargs):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            captured["url"] = request.full_url
            return _FakeResponse({"issues": [], "strengths": []})

        with patch("reports.ai_client.urlopen", side_effect=_capture):
            review_report_draft(_complete_draft(), user_id=41)

        body = captured["body"]
        self.assertEqual(captured["url"], "https://api.openai.com/v1/responses")
        self.assertFalse(body["store"])
        self.assertEqual(body["text"]["format"]["type"], "json_schema")
        self.assertTrue(body["text"]["format"]["strict"])
        self.assertTrue(body["safety_identifier"].startswith("tawtheeq_"))
        # لا يصل إلى المزوّد بندٌ أطفأه المعلّم، ولا اسمٌ ولا مدرسة.
        self.assertIn("النتائج", body["input"])
        self.assertNotIn("id_results", body["input"])

    def test_a_draft_with_no_text_never_spends_a_slot(self):
        """كل البنود النصّية مطفأة: لا نصّ يُقرأ، فلا نداء ولا رصيد."""
        sections = {key: False for key in _complete_draft()["sections"]}
        with patch("reports.ai_client.urlopen") as urlopen:
            result = review_report_draft(
                _complete_draft(
                    goal="", idea="", implementation_method="",
                    results="", recommendations="", beneficiaries_count="",
                    sections=sections,
                ),
                user_id=61,
            )
        urlopen.assert_not_called()
        self.assertFalse(result["semantic"])
        self.assertEqual(review_daily_remaining(61), 5)

    @override_settings(REPORT_REVIEW_ENABLED=False)
    def test_structural_review_still_runs_when_the_paid_pass_is_off(self):
        with patch("reports.ai_client.urlopen") as urlopen:
            result = review_report_draft(_complete_draft(evidence_count=0), user_id=51)
        urlopen.assert_not_called()
        self.assertFalse(result["semantic"])
        self.assertIn("evidence", {issue["field"] for issue in result["issues"]})


@override_settings(OPENAI_API_KEY="test-key", REPORT_REVIEW_ENABLED=True)
class ReportReviewEndpointTests(TestCase):
    def setUp(self):
        cache.clear()
        self.school = School.objects.create(name="مدرسة فحص الجاهزية", code="report-review")
        plan = SubscriptionPlan.objects.create(
            name="خطة فحص الجاهزية",
            price=0,
            days_duration=30,
            max_teachers=20,
        )
        SchoolSubscription.objects.create(school=self.school, plan=plan)
        self.teacher = Teacher.objects.create_user(
            phone="500008901",
            name="معلم فحص الجاهزية",
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
        self.url = reverse("reports:review_report_readiness")

    def _login(self):
        self.client.force_login(self.teacher)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()

    def test_endpoint_requires_authentication(self):
        response = self.client.post(self.url, data="{}", content_type="application/json")
        self.assertEqual(response.status_code, 302)

    def test_endpoint_returns_a_scored_review(self):
        self._login()
        parsed = {"issues": [], "strengths": ["الهدف واضح."]}
        with patch("reports.ai_client.urlopen", return_value=_FakeResponse(parsed)):
            response = self.client.post(
                self.url,
                data=json.dumps(_complete_draft(), ensure_ascii=False),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response["Cache-Control"])
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["score"], 100)
        self.assertTrue(payload["ready"])
        self.assertEqual(payload["issues"], [])

    def test_endpoint_rejects_a_non_json_request(self):
        self._login()
        response = self.client.post(self.url, data="x=1", content_type="text/plain")
        self.assertEqual(response.status_code, 415)

    def test_panel_is_rendered_on_both_report_forms(self):
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
                self.assertContains(response, "فحص الجاهزية")
                self.assertContains(response, "افحص جاهزية التقرير")
                self.assertContains(response, reverse("reports:review_report_readiness"))
                self.assertContains(response, "css/report-review.css")
                self.assertContains(response, "js/report-review.js")

    def test_platform_switch_hides_the_panel_and_closes_the_endpoint(self):
        from reports.ai_features import clear_platform_ai_feature_cache
        from reports.models import PlatformSettings

        settings_row = PlatformSettings.objects.create(report_review_enabled=False)
        self.addCleanup(settings_row.delete)
        self.addCleanup(clear_platform_ai_feature_cache)
        clear_platform_ai_feature_cache()

        self._login()
        response = self.client.get(reverse("reports:add_report"))
        self.assertNotContains(response, "افحص جاهزية التقرير")
        self.assertNotContains(response, "js/report-review.js")

        blocked = self.client.post(
            self.url,
            data=json.dumps(_complete_draft(), ensure_ascii=False),
            content_type="application/json",
        )
        self.assertEqual(blocked.status_code, 404)
