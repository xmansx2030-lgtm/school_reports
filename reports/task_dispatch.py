"""Backward-compatible report task-dispatch imports.

The implementation lives in :mod:`core.task_dispatch` so non-report services
can publish tasks without depending on the reports application.
"""

from __future__ import annotations

from core.task_dispatch import enqueue_named_task, run_named_task_safe, run_task_safe

__all__ = ("enqueue_named_task", "run_named_task_safe", "run_task_safe")
