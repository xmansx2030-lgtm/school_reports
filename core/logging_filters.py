"""Logging filters for high-volume, explicitly handled runtime conditions."""

from __future__ import annotations

import logging


class SkipExpectedLoadShed(logging.Filter):
    """Avoid one framework error log for every deliberately shed request.

    The concurrency middleware already emits a sampled warning and an exact
    operational counter. Django otherwise logs every 503 again, which can turn
    a traffic spike into a disk/logging incident even though shedding works.
    Unexpected 503 responses remain visible because only marked responses are
    filtered.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        response = getattr(record, "response", None)
        request = getattr(record, "request", None)
        return not bool(
            getattr(response, "expected_load_shed", False)
            or getattr(request, "expected_load_shed", False)
        )


__all__ = ("SkipExpectedLoadShed",)
