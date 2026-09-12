"""What happens to background work when the Celery broker is unreachable.

The rule: work whose absence loses data still runs inline, while work that only
improves an already-correct result is dropped rather than allowed to block web
requests and cascade a broker outage into a site-wide slowdown.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings

from reports.task_dispatch import enqueue_named_task
from reports.utils import run_named_task_safe, run_task_safe


class _BrokerDown(Exception):
    pass


class FakeTask:
    """Stands in for a Celery task whose enqueue fails."""

    __name__ = "fake_task"

    def __init__(self):
        self.inline_calls = []

    def apply_async(self, args=None, kwargs=None, headers=None):
        raise _BrokerDown("broker unreachable")

    def apply(self, args=None, kwargs=None, throw=False):
        self.inline_calls.append((tuple(args or ()), dict(kwargs or {})))


@override_settings(DEBUG=False)
class TaskFallbackPolicyTests(TestCase):
    def _run(self, task, **kwargs):
        with self.captureOnCommitCallbacks(execute=True):
            run_task_safe(task, 42, **kwargs)

    def test_critical_task_still_runs_inline_when_the_broker_is_down(self):
        task = FakeTask()

        self._run(task)

        self.assertEqual(task.inline_calls, [((42,), {})])

    def test_optional_task_is_dropped_instead_of_blocking_the_request(self):
        task = FakeTask()

        self._run(task, inline_fallback=False)

        self.assertEqual(task.inline_calls, [])

    def test_dropping_a_task_is_recorded_for_alerting(self):
        task = FakeTask()

        with patch("core.opmetrics.increment") as increment:
            self._run(task, inline_fallback=False)

        recorded = {call.args[0] for call in increment.call_args_list}
        self.assertIn("celery.enqueue.failed", recorded)
        self.assertIn("celery.task.dropped", recorded)

    def test_broker_failure_is_recorded_even_when_the_task_runs_inline(self):
        task = FakeTask()

        with patch("core.opmetrics.increment") as increment:
            self._run(task)

        recorded = {call.args[0] for call in increment.call_args_list}
        self.assertIn("celery.enqueue.failed", recorded)

    def test_named_task_is_queued_without_importing_its_task_module(self):
        fallback_calls = []

        with (
            patch("reports.utils.enqueue_named_task") as enqueue,
            self.captureOnCommitCallbacks(execute=True),
        ):
            run_named_task_safe(
                "reports.tasks.example",
                fallback_calls.append,
                42,
            )

        enqueue.assert_called_once()
        _, call_kwargs = enqueue.call_args
        self.assertEqual(enqueue.call_args.args, ("reports.tasks.example",))
        self.assertEqual(call_kwargs["args"], (42,))
        self.assertEqual(call_kwargs["kwargs"], {})
        self.assertIn("trace_id", call_kwargs["headers"])
        self.assertEqual(fallback_calls, [])

    def test_named_task_keeps_inline_fallback_when_broker_is_down(self):
        fallback_calls = []

        with (
            patch(
                "reports.utils.enqueue_named_task",
                side_effect=_BrokerDown("broker unreachable"),
            ),
            self.captureOnCommitCallbacks(execute=True),
        ):
            run_named_task_safe(
                "reports.tasks.example",
                fallback_calls.append,
                42,
            )

        self.assertEqual(fallback_calls, [42])

    def test_named_dispatch_uses_registered_task_and_preserves_options(self):
        task = Mock()
        app = SimpleNamespace(
            tasks={"reports.tasks.example": task},
            send_task=Mock(),
        )

        enqueue_named_task(
            "reports.tasks.example",
            args=[42],
            queue="images",
            app=app,
        )

        task.apply_async.assert_called_once_with(
            args=[42],
            queue="images",
        )
        app.send_task.assert_not_called()

    def test_named_dispatch_can_publish_an_unregistered_task(self):
        app = SimpleNamespace(tasks={}, send_task=Mock())

        enqueue_named_task(
            "reports.tasks.example",
            args=[42],
            queue="images",
            app=app,
        )

        app.send_task.assert_called_once_with(
            "reports.tasks.example",
            args=[42],
            queue="images",
        )

    def test_image_compression_does_not_run_inline_on_report_save(self):
        """Compression is an optimisation — the uploaded images stay valid
        without it, so a broker outage must not push Pillow work into the
        request/response cycle."""
        import inspect

        from reports.model_parts import signals

        source = inspect.getsource(signals.trigger_report_background_tasks)

        self.assertIn("inline_fallback=False", source)
