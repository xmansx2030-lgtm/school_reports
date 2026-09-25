"""Daily AI limits for personal workspaces, separate from school report tools."""

from __future__ import annotations

import logging
from typing import Literal

from django.core.cache import cache
from django.utils import timezone


logger = logging.getLogger(__name__)
AssistantKind = Literal["improvement", "voice"]
QUOTA_TIMEOUT_SECONDS = 60 * 60 * 48


class PersonalAssistantQuotaUnavailable(RuntimeError):
    pass


def _key(kind: AssistantKind, user_id: int) -> str:
    if kind not in ("improvement", "voice"):
        raise ValueError("Unknown personal assistant quota")
    return f"personal-report-{kind}:daily:v1:{timezone.localdate().isoformat()}:{int(user_id)}"


def daily_remaining(kind: AssistantKind, user_id: int, limit: int) -> int:
    try:
        used = max(0, int(cache.get(_key(kind, user_id), 0) or 0))
    except Exception:
        logger.exception("Unable to read personal %s quota user_id=%s", kind, user_id)
        return 0
    return max(0, int(limit) - used)


def reserve_daily_slot(kind: AssistantKind, user_id: int, limit: int) -> int | None:
    key = _key(kind, user_id)
    try:
        cache.add(key, 0, timeout=QUOTA_TIMEOUT_SECONDS)
        used = int(cache.incr(key))
        if used > int(limit):
            cache.decr(key)
            return None
    except Exception as exc:
        logger.exception("Unable to reserve personal %s quota user_id=%s", kind, user_id)
        raise PersonalAssistantQuotaUnavailable from exc
    return max(0, int(limit) - used)


def release_daily_slot(kind: AssistantKind, user_id: int) -> None:
    key = _key(kind, user_id)
    try:
        used = int(cache.decr(key))
        if used < 0:
            cache.set(key, 0, timeout=QUOTA_TIMEOUT_SECONDS)
    except Exception:
        logger.exception("Unable to release personal %s quota user_id=%s", kind, user_id)
