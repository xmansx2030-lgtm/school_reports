"""Validation shared by the asynchronous operations request boundary."""

from __future__ import annotations

import re
from datetime import timedelta, timezone as dt_timezone

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .action_policy import MAX_LOG_LINES, MAX_LOG_MINUTES


def _boundary(value: str):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("Invalid log time")
    parsed = parse_datetime(value)
    if parsed is None or timezone.is_naive(parsed):
        raise ValueError("An explicit log timezone is required")
    utc = parsed.astimezone(dt_timezone.utc)
    # Preserve Docker's nanosecond cursor; datetime itself keeps microseconds.
    normalized = value if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z", value) else utc.isoformat().replace("+00:00", "Z")
    return utc, normalized


def log_parameters(data) -> dict:
    minutes = max(5, min(int(data.get("since_minutes") or 30), MAX_LOG_MINUTES))
    tail = max(50, min(int(data.get("tail") or 200), MAX_LOG_LINES))
    now = timezone.now()
    until_dt, until = _boundary(str(data.get("until") or now.isoformat()))
    since_dt, since = _boundary(str(data.get("since_at") or (now - timedelta(minutes=minutes)).isoformat()))
    if until_dt > now + timedelta(seconds=5) or since_dt >= until_dt:
        raise ValueError("Invalid log interval")
    if until_dt - since_dt > timedelta(minutes=MAX_LOG_MINUTES, seconds=5):
        raise ValueError("Log interval is too long")
    # Allow time to paginate a fixed seven-day interval without shifting its start.
    if since_dt < now - timedelta(minutes=MAX_LOG_MINUTES + 60):
        raise ValueError("Log interval is outside retained query history")
    return {"since_minutes": minutes, "tail": tail, "since_at": since, "until": until}
