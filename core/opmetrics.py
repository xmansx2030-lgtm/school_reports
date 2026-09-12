from __future__ import annotations

"""Lightweight operational metrics using Django cache as the counter backend.

Counters are stored per UTC-hour bucket.  In production (Redis via django-redis)
``cache.keys()`` enables a full snapshot; on other backends (LocMemCache,
DatabaseCache) all writes succeed while ``snapshot()`` returns an empty dict.

Public API
----------
increment(metric, amount=1)   — increment a named counter for the current hour
read_current(metric)           — read the current-hour counter value
snapshot()                     — dict of all metrics for the current bucket
"""

import logging
from datetime import datetime

from django.core.cache import cache

logger = logging.getLogger(__name__)

_PREFIX = "opmetrics"
_BUCKET_SECONDS = 3600  # 1-hour rolling window; keys live 2 × window


def _now_bucket() -> str:
    """Return a UTC-hour bucket string, e.g. '2026040113'."""
    return datetime.utcnow().strftime("%Y%m%d%H")


def _make_key(metric: str, bucket: str) -> str:
    return f"{_PREFIX}:{metric}:{bucket}"


def _registry_key(bucket: str) -> str:
    return f"{_PREFIX}:registry:{bucket}"


def _register_metric(metric: str, bucket: str) -> None:
    """Track metric names per bucket without depending on cache.keys()."""
    try:
        key = _registry_key(bucket)
        current = cache.get(key) or []
        if metric in current:
            return
        # Keep registry bounded in practice; metric cardinality is fixed by code constants.
        updated = list(current) + [metric]
        cache.set(key, updated, timeout=_BUCKET_SECONDS * 2)
    except Exception:
        # Observability cannot be allowed to fail the operation it observes.
        logger.debug("Unable to register operational metric %s", metric, exc_info=True)


def increment(metric: str, amount: int = 1) -> None:
    """Increment *metric* counter for the current UTC hour.

    Thread/process-safe via cache.add + cache.incr.
    Silently swallows all errors so metrics never break application flow.
    """
    try:
        bucket = _now_bucket()
        key = _make_key(metric, bucket)
        _register_metric(metric, bucket)
        # Ensure key exists before incr; TTL = 2 × window keeps 2 buckets.
        cache.add(key, 0, timeout=_BUCKET_SECONDS * 2)
        cache.incr(key, amount)
    except Exception:
        logger.debug("Unable to increment operational metric %s", metric, exc_info=True)


def read_current(metric: str) -> int:
    """Return the current-hour counter value for *metric*. Returns 0 on any error."""
    try:
        key = _make_key(metric, _now_bucket())
        return int(cache.get(key) or 0)
    except Exception:
        return 0


def timing(metric: str, duration_ms: float) -> None:
    """Record a duration sample (milliseconds) for *metric*.

    Stores the latest value and a running count so we can compute
    average from  metric.sum / metric.count  if needed.
    Lightweight: 3 cache operations, silently swallowed on failure.
    """
    try:
        bucket = _now_bucket()
        # Count of samples
        count_key = _make_key(f"{metric}.count", bucket)
        _register_metric(f"{metric}.count", bucket)
        cache.add(count_key, 0, timeout=_BUCKET_SECONDS * 2)
        cache.incr(count_key)

        # Sum of durations
        sum_key = _make_key(f"{metric}.sum_ms", bucket)
        _register_metric(f"{metric}.sum_ms", bucket)
        cache.add(sum_key, 0, timeout=_BUCKET_SECONDS * 2)
        cache.incr(sum_key, int(duration_ms))
    except Exception:
        logger.debug("Unable to record operational timing %s", metric, exc_info=True)


def snapshot() -> dict[str, int]:
    """Return {metric_name: count} for the current UTC hour.

    Works on all Django cache backends by reading an explicit per-bucket
    metric registry maintained by ``increment()``.
    """
    try:
        bucket = _now_bucket()
        result: dict[str, int] = {}
        metrics = cache.get(_registry_key(bucket)) or []
        for metric_name in metrics:
            raw = cache.get(_make_key(metric_name, bucket))
            if raw is not None:
                result[metric_name] = int(raw)
        return result
    except Exception:
        return {}
