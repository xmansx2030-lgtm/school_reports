from __future__ import annotations

import contextvars


_trace_id_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar("trace_id", default=None)


def normalize_trace_id(value: str | None) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    # Keep header-safe ASCII and avoid excessively long values in logs/headers.
    safe = "".join(ch for ch in raw if 32 <= ord(ch) < 127)
    return safe[:64]


def set_trace_id(trace_id: str | None) -> contextvars.Token[str | None]:
    return _trace_id_ctx.set(normalize_trace_id(trace_id) or None)


def get_trace_id() -> str | None:
    return _trace_id_ctx.get()


def reset_trace_id(token: contextvars.Token[str | None]) -> None:
    try:
        _trace_id_ctx.reset(token)
    except (RuntimeError, ValueError):
        # A token may already have been reset or may belong to another context
        # during cancellation cleanup. The typed API rejects unrelated values.
        return
