"""Bounded log display and deterministic diagnosis without sending logs to AI."""

from __future__ import annotations

import re
from collections import Counter

MAX_LINES = 1000
MAX_CHARS = 200000

_SENSITIVE_KEY = r"(?:password|passwd|secret|client[_-]?secret|api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|cookie|set-cookie|private[_-]?key|csrfmiddlewaretoken|csrftoken|sessionid)"
_QUOTED_VALUE = r'''(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')'''
_SENSITIVE_VALUE = rf'''(?:{_QUOTED_VALUE}|[^\s,;&}}\]]+)'''
_REDACTIONS = (
    # Cookie headers contain several independently named credentials separated
    # by semicolons. Redact the entire value, including escaped JSON quotes.
    (re.compile(rf'''(?i)((?:["']?(?:set-cookie|cookie)["']?)\s*[:=]\s*)(?:{_QUOTED_VALUE}|[^\r\n]*)'''), r"\1[REDACTED]"),
    (re.compile(r'''(?i)((?:["']?authorization["']?)\s*[:=]\s*["']?(?:bearer|basic|ops-token)\s+)[^\s,"'}]+'''), r"\1[REDACTED]"),
    (re.compile(rf'''(?i)((?:["']?{_SENSITIVE_KEY}["']?)\s*[:=]\s*){_SENSITIVE_VALUE}'''), r"\1[REDACTED]"),
    (re.compile(r"(?i)([?&](?:token|key|secret|password|signature|code)=)[^&\s]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)\b((?:postgres(?:ql)?|redis(?:s)?|amqp(?:s)?|mysql)://[^:\s/@]+:)[^@\s/]+@"), r"\1[REDACTED]@"),
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
    # Redact the complete bounded host response before cutting lines or bytes.
    # Otherwise a BEGIN marker or a quoted secret can be cut off first.
    lines = str(raw or "").splitlines()
    cleaned = []
    in_private_key = False
    for line in lines:
        safe = line
        prefix = re.match(r"^(\d{4}-\d{2}-\d{2}T\S+Z\s+\[[^\]]+\]\s+)", safe)
        label = prefix.group(1) if prefix else ""
        if re.search(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", safe):
            in_private_key = True
            cleaned.append(label + "[REDACTED_PRIVATE_KEY]")
            continue
        if in_private_key:
            if re.search(r"-----END [A-Z ]*PRIVATE KEY-----", safe):
                in_private_key = False
            continue
        for pattern, replacement in _REDACTIONS:
            safe = pattern.sub(replacement, safe)
        # A Docker tail can begin in the middle of a PEM block.
        if re.search(r"(?:^|\s)[A-Za-z0-9+/]{48,}={0,2}\s*$", safe):
            safe = label + "[REDACTED_KEY_MATERIAL]"
        cleaned.append(safe[:2000])
    truncated = len(cleaned) > MAX_LINES or any(len(line) > 2000 for line in lines)
    selected = []
    size = 0
    for line in reversed(cleaned[-MAX_LINES:]):
        if size + len(line) + 1 > MAX_CHARS:
            truncated = True
            break
        selected.append(line)
        size += len(line) + 1
    omitted_lines = len(cleaned) - len(selected)
    cleaned = list(reversed(selected))
    levels: Counter[str] = Counter()
    categories: Counter[str] = Counter()
    for safe in cleaned:
        if re.search(r"(?i)\b(error|exception|fatal|critical|traceback)\b|\b\w+(?:Error|Exception)\b", safe):
            levels["error"] += 1
        elif re.search(r"(?i)\b(warn|warning)\b", safe):
            levels["warning"] += 1
        for name, pattern in _CATEGORIES.items():
            if pattern.search(safe):
                categories[name] += 1
    content = "\n".join(cleaned)
    return content, {
        "lines": len(cleaned),
        "errors": levels["error"],
        "warnings": levels["warning"],
        "categories": dict(categories.most_common()),
        "findings": [
            {"category": name, "count": count, "next_check": _GUIDANCE[name]}
            for name, count in categories.most_common()
        ],
        "truncated": truncated,
        "omitted_lines": omitted_lines,
    }
