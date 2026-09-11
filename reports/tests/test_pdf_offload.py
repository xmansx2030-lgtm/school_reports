"""تفريغ توليد PDF إلى عامل الوسائط — دون أن يتغيّر ما يصل المستخدم.

المقايضة كلها في ``reports/pdf_offload.py``. وما تحرسه هذه الاختبارات هو
**الشرط** الذي جعل التغيير مقبولاً: أن يبقى الناتج هو الناتج، وأن يظل المستخدم
يحصل على ملفه ولو سقطت البنية التي نُقل إليها العمل.

فالتفريغ تحسينُ أداء، ولا يجوز لتحسين الأداء أن يضيف مسار فشل جديداً.
"""
from __future__ import annotations


from django.core.cache import cache
from django.test import TestCase, override_settings

from reports.pdf_offload import (
    PDF_RESULT_TTL_SECONDS,
    render_pdf_offloaded,
    store_rendered_pdf,
)


class _BrokerDown(Exception):
    """ما يرميه Celery حين يتعذّر الوصول إلى الوسيط."""


class _FakeAsyncResult:
    def __init__(self, *, fails: bool = False, stores: bytes | None = None, key: str | None = None):
        self._fails = fails
        self._stores = stores
        self._key = key

    def get(self, timeout=None, propagate=True):
        if self._fails:
            raise _BrokerDown("broker unreachable")
        # يحاكي العامل: يضع البايتات في المفتاح ثم ينتهي.
        if self._stores is not None and self._key:
            store_rendered_pdf(self._key, self._stores)
        return True


class _FakeTask:
    """مهمة مزيّفة تلتقط الوسائط وتحاكي سلوك العامل."""

    def __init__(self, *, fails: bool = False, stores: bytes | None = None):
        self.fails = fails
        self.stores = stores
        self.calls: list[list] = []

    def apply_async(self, args=None, queue=None):
        args = list(args or [])
        self.calls.append(args)
        self.queue = queue
        return _FakeAsyncResult(fails=self.fails, stores=self.stores, key=args[-1])


# ``ALWAYS_EAGER`` يجعل المساعد يولّد محلياً عمداً، فيُعطَّل هنا كي نختبر
# مسار التفريغ الحقيقي لا اختصاره.
@override_settings(CELERY_TASK_ALWAYS_EAGER=False, PDF_OFFLOAD_ENABLED=True)
class PdfOffloadTests(TestCase):
    def setUp(self):
        cache.clear()
        self.local_calls = 0

    def _render_locally(self) -> bytes:
        self.local_calls += 1
        return b"%PDF-local"

    def test_worker_output_is_returned_and_nothing_is_rendered_locally(self):
        task = _FakeTask(stores=b"%PDF-worker")

        result = render_pdf_offloaded(
            task=task,
            task_args=[7, "https://example.test/"],
            render_locally=self._render_locally,
            label="achievement:7",
        )

        self.assertEqual(result, b"%PDF-worker")
        self.assertEqual(self.local_calls, 0, "لا يجوز حرق معالج الويب عند نجاح التفريغ")
        self.assertEqual(task.queue, "images")

    def test_the_result_key_is_appended_to_the_task_arguments(self):
        """المستدعي يمرّر وسائطه، والمساعد يضيف مفتاح النتيجة — لا العكس."""
        task = _FakeTask(stores=b"%PDF-worker")

        render_pdf_offloaded(
            task=task,
            task_args=[7, "https://example.test/"],
            render_locally=self._render_locally,
            label="achievement:7",
        )

        args = task.calls[0]
        self.assertEqual(args[:2], [7, "https://example.test/"])
        self.assertTrue(str(args[2]).startswith("pdf:render:"))

    def test_a_broker_failure_falls_back_to_local_rendering(self):
        """سقوط الوسيط لا يحرم المستخدم ملفه."""
        task = _FakeTask(fails=True)

        result = render_pdf_offloaded(
            task=task,
            task_args=[7, None],
            render_locally=self._render_locally,
            label="achievement:7",
        )

        self.assertEqual(result, b"%PDF-local")
        self.assertEqual(self.local_calls, 1)

    def test_a_successful_task_with_no_payload_falls_back_instead_of_returning_empty(self):
        """نجاحٌ بلا بايتات = ذاكرة مؤقتة ساقطة، لا ملفٌ فارغ."""
        task = _FakeTask(stores=None)

        result = render_pdf_offloaded(
            task=task,
            task_args=[7, None],
            render_locally=self._render_locally,
            label="achievement:7",
        )

        self.assertEqual(result, b"%PDF-local")
        self.assertEqual(self.local_calls, 1)

    def test_the_result_key_is_consumed_once_and_leaves_no_residue(self):
        """البايتات تُحذف فور قراءتها: Redis هنا محدود الذاكرة."""
        task = _FakeTask(stores=b"%PDF-worker")

        render_pdf_offloaded(
            task=task,
            task_args=[7, None],
            render_locally=self._render_locally,
            label="achievement:7",
        )

        key = task.calls[0][-1]
        self.assertIsNone(cache.get(key))

    def test_the_ttl_is_short_enough_to_bound_redis_growth(self):
        self.assertLessEqual(PDF_RESULT_TTL_SECONDS, 600)

    @override_settings(PDF_OFFLOAD_ENABLED=False)
    def test_the_switch_restores_the_previous_behaviour_entirely(self):
        """مفتاح إيقاف صريح: إن ساء التفريغ في الإنتاج يُطفأ بلا نشر جديد."""
        task = _FakeTask(stores=b"%PDF-worker")

        result = render_pdf_offloaded(
            task=task,
            task_args=[7, None],
            render_locally=self._render_locally,
            label="achievement:7",
        )

        self.assertEqual(result, b"%PDF-local")
        self.assertEqual(task.calls, [], "لا ينبغي أن تُرسل مهمة أصلاً وهو مُطفأ")


class AchievementPdfGeneratorContractTests(TestCase):
    """المولّد صار يعمل بلا ``request`` — وهو شرط تشغيله في العامل."""

    def test_the_generator_accepts_a_base_url_without_a_request(self):
        import inspect

        from reports.pdf_achievement import generate_achievement_pdf

        params = inspect.signature(generate_achievement_pdf).parameters
        self.assertIn("base_url", params)
        self.assertIs(params["request"].default, None)

    def test_the_filename_can_be_derived_without_rendering(self):
        """اسم الملف يلزم في الطلب قبل أن تعود بايتات العامل."""
        from reports.pdf_achievement import achievement_pdf_filename

        class _Stub:
            teacher_name = "معلم/التجربة"
            academic_year = "1447-1448"

        name = achievement_pdf_filename(_Stub())
        self.assertTrue(name.endswith(".pdf"))
        self.assertNotIn("/", name)


class AchievementPdfViewUsesTheWorkerTests(TestCase):
    """الشاشة تمرّ بالمساعد لا بالمولّد مباشرة."""

    def test_the_view_calls_the_offload_helper(self):
        import reports.views.achievements as achievements_view

        source = achievements_view.achievement_file_pdf.__wrapped__ if hasattr(
            achievements_view.achievement_file_pdf, "__wrapped__"
        ) else achievements_view.achievement_file_pdf
        import inspect

        body = inspect.getsource(source)
        self.assertIn("render_pdf_offloaded", body)
        self.assertIn("render_achievement_pdf_task", body)
