"""Celery dispatch boundary shared by application services and producers.

Task producers should know the stable task name and fallback behavior, but they
must not import the task module that imports them back.  Keeping that rule here
prevents service/task cycles while preserving Celery eager-mode behavior.
"""

from __future__ import annotations

import logging
import secrets
import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from celery import current_app
from django.conf import settings
from django.db import transaction

from core.observability import soft_fail
from core.trace_context import get_trace_id

logger = logging.getLogger(__name__)


def enqueue_named_task(
    task_name: str,
    *,
    args: Sequence[Any] | None = None,
    kwargs: Mapping[str, Any] | None = None,
    app: Any = None,
    **options: Any,
) -> Any:
    """Queue a task by stable name without importing its implementation."""
    celery_app = app or current_app
    call_options = dict(options)
    if args is not None:
        call_options["args"] = args
    if kwargs is not None:
        call_options["kwargs"] = kwargs
    task = celery_app.tasks.get(task_name)
    if task is not None:
        return task.apply_async(**call_options)
    return celery_app.send_task(task_name, **call_options)


def run_task_safe(
    task_func: Any,
    *args: Any,
    force_thread: bool = False,
    inline_fallback: bool = True,
    **kwargs: Any,
) -> None:
    """Publish work and apply the documented broker-failure policy.

    Development may use a background thread. Production runs critical work
    inline when publishing fails, while callers may explicitly drop optional
    work with ``inline_fallback=False``.
    """

    def _thread_wrapper(func: Callable[..., Any], *f_args: Any, **f_kwargs: Any) -> None:
        from django.db import connections

        connections.close_all()
        try:
            func(*f_args, **f_kwargs)
        finally:
            connections.close_all()

    def _execute() -> None:
        debug_mode = bool(getattr(settings, "DEBUG", False))
        allow_thread = bool(force_thread) or debug_mode
        trace_id = get_trace_id() or secrets.token_hex(8)

        try:
            if hasattr(task_func, "apply_async"):
                async_result = task_func.apply_async(
                    args=args,
                    kwargs=kwargs,
                    headers={"trace_id": trace_id},
                )
            else:
                async_result = task_func.delay(*args, **kwargs)
            logger.info(
                "Task queued via Celery task=%s task_id=%s trace_id=%s args_count=%s",
                getattr(task_func, "__name__", str(task_func)),
                getattr(async_result, "id", None),
                trace_id,
                len(args),
            )
            return
        except Exception as exc:
            # This is an intentional broker boundary. The fallback below is
            # part of the application's durability policy, and failure is
            # observable without serializing task arguments or secrets.
            logger.warning(
                "Celery enqueue failed task=%s trace_id=%s error=%s",
                getattr(task_func, "__name__", str(task_func)),
                trace_id,
                exc,
            )
            with soft_fail("celery.count_enqueue_failure"):
                from core import opmetrics

                opmetrics.increment("celery.enqueue.failed")

        if allow_thread:
            thread = threading.Thread(
                target=_thread_wrapper,
                args=(task_func, *args),
                kwargs=kwargs,
                daemon=True,
            )
            thread.start()
            logger.info(
                "Task fallback thread-started task=%s trace_id=%s debug=%s force_thread=%s",
                getattr(task_func, "__name__", str(task_func)),
                trace_id,
                debug_mode,
                force_thread,
            )
            return

        if not inline_fallback:
            logger.error(
                "Task dropped (inline fallback disabled) task=%s trace_id=%s args_count=%s",
                getattr(task_func, "__name__", str(task_func)),
                trace_id,
                len(args),
            )
            with soft_fail("celery.count_dropped_task"):
                from core import opmetrics

                opmetrics.increment("celery.task.dropped")
            return

        try:
            if hasattr(task_func, "apply"):
                task_func.apply(args=args, kwargs=kwargs, throw=True)
            else:
                task_func(*args, **kwargs)
            logger.info(
                "Task executed inline fallback task=%s trace_id=%s args_count=%s",
                getattr(task_func, "__name__", str(task_func)),
                trace_id,
                len(args),
            )
        except Exception:
            # Execution already crossed the broker boundary and the fallback
            # must not hide its own failure.
            logger.exception(
                "Task %s failed even with inline fallback.",
                getattr(task_func, "__name__", str(task_func)),
            )

    transaction.on_commit(_execute)


class _NamedTaskProxy:
    """Queue a Celery task by stable name while retaining a local fallback."""

    def __init__(self, task_name: str, fallback: Callable[..., Any]) -> None:
        self.name = task_name
        self.__name__ = task_name
        self._fallback = fallback

    def apply_async(
        self,
        *,
        args: Sequence[Any] | None = None,
        kwargs: Mapping[str, Any] | None = None,
        headers: Mapping[str, Any] | None = None,
    ) -> Any:
        return enqueue_named_task(
            self.name,
            args=args,
            kwargs=kwargs,
            headers=headers,
        )

    def apply(
        self,
        *,
        args: Sequence[Any] | None = None,
        kwargs: Mapping[str, Any] | None = None,
        throw: bool = False,
    ) -> Any:
        del throw
        return self._fallback(*(args or ()), **(kwargs or {}))

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._fallback(*args, **kwargs)


def run_named_task_safe(
    task_name: str,
    fallback: Callable[..., Any],
    *args: Any,
    force_thread: bool = False,
    inline_fallback: bool = True,
    **kwargs: Any,
) -> None:
    """Apply :func:`run_task_safe` without importing the named task module."""
    run_task_safe(
        _NamedTaskProxy(task_name, fallback),
        *args,
        force_thread=force_thread,
        inline_fallback=inline_fallback,
        **kwargs,
    )


__all__ = ("enqueue_named_task", "run_named_task_safe", "run_task_safe")
