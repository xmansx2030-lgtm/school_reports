"""Bounded log display and deterministic diagnosis without sending logs to AI."""

from __future__ import annotations

import re
from collections import Counter

MAX_LINES = 250
MAX_CHARS = 60000

_REDACTIONS = (
    (re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer|basic|ops-token)\s+)\S+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)((?:password|passwd|secret|api[_-]?key|access[_-]?token|refresh[_-]?token|cookie|set-cookie)\s*[:=]\s*)[^\s,;&]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)([?&](?:token|key|secret|signature|code)=)[^&\s]+"), r"\1[REDACTED]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"), "[REDACTED_JWT]"),
)

_CATEGORIES = {
    "database": re.compile(r"(?i)postgres|psycopg|database|sqlstate|connection pool|pgbouncer"),
    "cache_queue": re.compile(r"(?i)redis|celery|broker|queue|rabbitmq"),
    "memory": re.compile(r"(?i)out of memory|oom|killed process|memoryerror"),
    "disk": re.compile(r"(?i)no space left|disk full|read-only file system"),
    "network": re.compile(r"(?i)timeout|timed out|connection refused|connection reset|dns failure"),
    "http_5xx": re.compile(r"\b(?:500|502|503|504)\b|(?i:bad gateway|service unavailable)"),
    "authentication": re.compile(r"(?i)unauthorized|forbidden|invalid token|authentication failed"),
}

_GUIDANCE = {
    "database": "تحقق من اتصال قاعدة البيانات وPgBouncer قبل إعادة تشغيل التطبيق.",
    "cache_queue": "افحص Redis وطول الطوابير وحالة العمال.",
    "memory": "افحص OOM واستهلاك ذاكرة الحاوية والخادم قبل إعادة التشغيل.",
    "disk": "افحص المساحة وinodes ومسار النسخ قبل أي تنظيف.",
    "network": "افحص DNS والوجهة وزمن الاستجابة؛ السطر وحده لا يحدد السبب.",
    "http_5xx": "قارن وقت خطأ HTTP مع سجلات التطبيق والوكيل وقاعدة البيانات.",
    "authentication": "تحقق من إعداد المصادقة دون كشف بيانات الاعتماد.",
}


def sanitize_and_analyze(raw: str) -> tuple[str, dict]:
    lines = str(raw or "").splitlines()[-MAX_LINES:]
    cleaned = []
    levels: Counter[str] = Counter()
    categories: Counter[str] = Counter()
    for line in lines:
        safe = line[:500]
        for pattern, replacement in _REDACTIONS:
            safe = pattern.sub(replacement, safe)
        cleaned.append(safe)
        if re.search(r"(?i)\b(error|exception|fatal|critical|traceback)\b", safe):
            levels["error"] += 1
        elif re.search(r"(?i)\b(warn|warning)\b", safe):
            levels["warning"] += 1
        for name, pattern in _CATEGORIES.items():
            if pattern.search(safe):
                categories[name] += 1
    content = "\n".join(cleaned)[-MAX_CHARS:]
    return content, {
        "lines": len(cleaned),
        "errors": levels["error"],
        "warnings": levels["warning"],
        "categories": dict(categories.most_common()),
        "findings": [
            {"category": name, "count": count, "next_check": _GUIDANCE[name]}
            for name, count in categories.most_common()
        ],
        "truncated": len(str(raw or "").splitlines()) > MAX_LINES or len(content) >= MAX_CHARS,
    }
