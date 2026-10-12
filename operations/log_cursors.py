"""Nanosecond cursors for Docker's inclusive --until timestamp boundary."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone


def previous_timestamp(stamp: str) -> str:
    """Return exactly one nanosecond before an RFC3339 UTC log timestamp."""
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?Z", stamp)
    if match is None:
        raise ValueError("Invalid log cursor timestamp")
    second = datetime.fromisoformat(match.group(1)).replace(tzinfo=timezone.utc)
    nanoseconds = int((match.group(2) or "").ljust(9, "0"))
    if nanoseconds:
        nanoseconds -= 1
    else:
        second -= timedelta(seconds=1)
        nanoseconds = 999999999
    base = second.isoformat(timespec="seconds").removesuffix("+00:00")
    return f"{base}.{nanoseconds:09d}Z"
