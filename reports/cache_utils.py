# reports/cache_utils.py
# -*- coding: utf-8 -*-
"""
Centralized cache helpers for the reports app.
Provides cache key builders and invalidation utilities
to keep hot-path queries fast.
"""
from __future__ import annotations

import logging
import secrets
import time
from contextlib import contextmanager

from core.observability import report_degraded as _degraded, soft_call, soft_fail

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

# ── Default TTLs (seconds) ──────────────────────────────────────────
CACHE_TTL_SHORT = 60           # 1 minute  (notification counts, etc.)
CACHE_TTL_MEDIUM = 300         # 5 minutes (dashboard stats)
CACHE_TTL_LONG = 900           # 15 minutes (report-type lists, department lists)


# ── Key builders ────────────────────────────────────────────────────
def _k(prefix: str, *parts) -> str:
    """Build a namespaced cache key."""
    return ":".join(str(p) for p in (prefix, *parts))


def key_school_stats(school_id: int) -> str:
    return _k("school_stats", school_id)


def key_department_list(school_id: int) -> str:
    return _k("dept_list", school_id)


def key_reporttype_list(school_id: int) -> str:
    return _k("rtype_list", school_id)


def key_unread_count(user_id: int) -> str:
    return _k("unread", user_id)


def key_teacher_count(school_id: int) -> str:
    return _k("teacher_cnt", school_id)


def _dashboard_version_key(school_id: int) -> str:
    return _k("school_dashboard", "version", school_id)


def _dashboard_payload_key(school_id: int, period: str, version: int) -> str:
    return _k("school_dashboard", "payload", school_id, period, version)


def _dashboard_stale_key(school_id: int, period: str, version: int) -> str:
    return _k("school_dashboard", "stale", school_id, period, version)


# ── Invalidation helpers ────────────────────────────────────────────
def invalidate_school(school_id: int) -> None:
    """Bust all caches related to a specific school."""
    keys = [
        key_school_stats(school_id),
        key_department_list(school_id),
        key_reporttype_list(school_id),
        key_teacher_count(school_id),
    ]
    try:
        cache.delete_many(keys)
    except Exception:
        logger.debug("cache.delete_many failed for school %s", school_id)


def invalidate_school_dashboard(school_id: int) -> None:
    """Atomically move a school's dashboard to a fresh cache namespace.

    Versioned keys avoid Redis ``KEYS``/wildcard deletion, which can pause a
    busy instance. Old payloads expire naturally and remain usable only for
    requests that started before the mutation committed.
    """
    if not school_id:
        return
    key = _dashboard_version_key(int(school_id))
    try:
        cache.add(key, 1, timeout=None)
        cache.incr(key)
    except (ValueError, TypeError):
        soft_call("cache.version_bump_reset", lambda: cache.set(key, 2, timeout=None), default=None)
    except Exception:
        # Cache failure must never roll back the business write. A short TTL on
        # the old payload is the final consistency guard.
        logger.debug("dashboard cache version bump failed school=%s", school_id)


def school_dashboard_version(school_id: int) -> int:
    key = _dashboard_version_key(int(school_id or 0))
    try:
        version = cache.get(key)
        if version is None:
            cache.add(key, 1, timeout=None)
            version = cache.get(key)
        return max(1, int(version or 1))
    except Exception:
        return 1


@contextmanager
def redis_cache_lock(key: str, *, timeout: int = 15):
    """Non-blocking distributed lock with a safe local-cache fallback."""
    lock = None
    token = secrets.token_urlsafe(18)
    acquired = False
    try:
        lock_factory = getattr(cache, "lock", None)
        if callable(lock_factory):
            lock = lock_factory(key, timeout=max(1, int(timeout)), blocking_timeout=0)
            acquired = bool(lock.acquire(blocking=False))
        else:
            acquired = bool(cache.add(key, token, timeout=max(1, int(timeout))))
    except Exception:
        acquired = False
    try:
        yield acquired
    finally:
        if acquired:
            # قفلٌ لا يُحرَّر يبقى حتى ينتهي عمره — فيُعاد بناء اللوحة تسلسلياً
            # طوال تلك المدة بدل أن تُخدم من الكاش.
            with soft_fail("cache.release_rebuild_lock", key=key):
                if lock is not None:
                    lock.release()
                elif cache.get(key) == token:
                    cache.delete(key)


def get_school_dashboard_payload(
    *,
    school_id: int,
    period: str,
    builder,
):
    """Return a hot dashboard payload without allowing a cache stampede.

    A fresh value lives for 30–60 seconds (45 by default). A longer stale copy
    lets concurrent readers keep serving a coherent dashboard while one worker
    rebuilds it. Cold readers wait briefly for that worker rather than all
    issuing the same aggregate queries to PostgreSQL.
    """
    sid = int(school_id or 0)
    ttl = max(30, min(60, int(getattr(settings, "SCHOOL_DASHBOARD_CACHE_TTL_SECONDS", 45) or 45)))
    stale_ttl = max(ttl, int(getattr(settings, "SCHOOL_DASHBOARD_STALE_TTL_SECONDS", 300) or 300))
    lock_ttl = max(5, int(getattr(settings, "SCHOOL_DASHBOARD_LOCK_TTL_SECONDS", 15) or 15))
    wait_seconds = max(0.0, min(3.0, float(getattr(settings, "SCHOOL_DASHBOARD_LOCK_WAIT_SECONDS", 1.5) or 1.5)))

    version = school_dashboard_version(sid)
    fresh_key = _dashboard_payload_key(sid, period, version)
    stale_key = _dashboard_stale_key(sid, period, version)
    lock_key = _k("school_dashboard", "lock", sid, period, version)

    try:
        cached = cache.get(fresh_key)
        if isinstance(cached, dict):
            return cached
    except Exception:
        return builder()

    with redis_cache_lock(lock_key, timeout=lock_ttl) as acquired:
        if acquired:
            # Double-check after acquiring: another worker may have completed
            # between our initial miss and the lock acquisition.
            cached = cache.get(fresh_key)
            if isinstance(cached, dict):
                return cached
            payload = builder()
            cache.set(fresh_key, payload, timeout=ttl)
            cache.set(stale_key, payload, timeout=stale_ttl)
            return payload

    try:
        stale = cache.get(stale_key)
        if isinstance(stale, dict):
            return stale
    except Exception:
        return builder()

    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        time.sleep(0.05)
        try:
            cached = cache.get(fresh_key)
            if isinstance(cached, dict):
                return cached
        except Exception:
            break

    # A broken/expired lock must not make the page unavailable. This path is
    # limited to a cold-cache timeout and the fresh result is still published.
    payload = builder()
    with soft_fail("cache.publish_rebuilt_payload", key=fresh_key):
        cache.set(fresh_key, payload, timeout=ttl)
        cache.set(stale_key, payload, timeout=stale_ttl)
    return payload


def invalidate_user_notifications(user_id: int) -> None:
    """Bust notification count cache for a user."""
    try:
        uid = int(user_id)
        keys = [
            key_unread_count(uid),
            f"unreadcnt:v1:u{uid}:snone",
            f"ws:counts:{uid}:0",
        ]
        try:
            from .models import SchoolMembership

            school_ids = SchoolMembership.objects.filter(
                teacher_id=uid,
                is_active=True,
            ).values_list("school_id", flat=True)
            for school_id in school_ids:
                keys.append(f"unreadcnt:v1:u{uid}:s{int(school_id)}")
                keys.append(f"ws:counts:{uid}:{int(school_id)}")
        except Exception:
            # مفاتيحُ مدارس لم تُجمع = عدّادات لا تُبطَل في تلك المدارس.
            _degraded("cache.collect_user_school_keys", user_id=uid)
        cache.delete_many(keys)
    except Exception:
        _degraded("cache.invalidate_user_notifications", user_id=uid)


# ── Cached getters ──────────────────────────────────────────────────
def get_or_set(key: str, callback, ttl: int = CACHE_TTL_MEDIUM):
    """
    Safe wrapper around cache.get_or_set that falls back to the
    callback on any cache backend error.
    """
    val = soft_call("cache.get_or_set_read", lambda: cache.get(key), default=None, key=key)
    if val is not None:
        return val
    val = callback()
    soft_call("cache.get_or_set_write", lambda: cache.set(key, val, ttl), default=None, key=key)
    return val
