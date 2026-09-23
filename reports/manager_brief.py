"""Executive manager brief built from trusted dashboard aggregates.

The database remains the source of truth.  This module ranks already-computed
signals and builds every action locally; the language model may only rewrite
the short headline and narrative.  It never receives staff names, report
content, ticket content, URLs, or permission data and it never returns an
action that the application did not create itself.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from copy import deepcopy
from urllib.error import HTTPError, URLError

from django.conf import settings
from django.core.cache import cache
from django.urls import reverse

from .ai_client import extract_output_text, responses_create, truncation_reason
from .ai_features import FEATURE_INTERNAL_HELP, platform_ai_toggle_enabled
from .ai_usage import ai_usage_context


logger = logging.getLogger(__name__)

BRIEF_CACHE_SECONDS = 15 * 60
MAX_HEADLINE_LENGTH = 110
MAX_SUMMARY_LENGTH = 420


def manager_brief_ai_enabled() -> bool:
    """Use the existing governed text-AI switch; the local brief always works."""

    return bool(
        platform_ai_toggle_enabled(FEATURE_INTERNAL_HELP)
        and getattr(settings, "REPORT_AI_ENABLED", False)
        and str(getattr(settings, "OPENAI_API_KEY", "") or "").strip()
    )


def _integer(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _percentage(value) -> int:
    return min(100, _integer(value))


def _trend_label(trend: dict | None) -> str:
    direction = str((trend or {}).get("direction") or "flat")
    return {"up": "صاعد", "down": "منخفض", "flat": "مستقر"}.get(
        direction, "غير متاح"
    )


def _routes() -> dict[str, str]:
    """Permission-valid manager destinations; AI never writes these values."""

    return {
        "reports": reverse("reports:admin_reports"),
        "tickets": f"{reverse('reports:manager_school_tickets')}?status=attention",
        "coverage": f"{reverse('reports:notifications_create')}?remind=coverage",
        "teachers": reverse("reports:manage_teachers"),
        "dashboard_coverage": "#managerCoverage",
        "performance": "#managerPerformance",
    }


def build_manager_brief(dashboard_payload: dict) -> dict:
    """Build a useful brief without an external call.

    ``dashboard_payload`` is the same school-scoped payload used by the manager
    dashboard and its refresh endpoint.  The ranking is deterministic and
    therefore auditable and testable.
    """

    payload = dashboard_payload if isinstance(dashboard_payload, dict) else {}
    kpis = payload.get("kpis") if isinstance(payload.get("kpis"), dict) else {}
    coverage = (
        payload.get("coverage") if isinstance(payload.get("coverage"), dict) else {}
    )
    trends = payload.get("trends") if isinstance(payload.get("trends"), dict) else {}
    responsiveness = (
        payload.get("responsiveness")
        if isinstance(payload.get("responsiveness"), dict)
        else {}
    )
    routes = _routes()

    period_label = str(payload.get("period_label") or "الفترة المختارة")[:40]
    reports_count = _integer(kpis.get("reports_count"))
    tickets_open = _integer(kpis.get("tickets_open"))
    tickets_total = _integer(kpis.get("tickets_total"))
    tickets_done = _integer(kpis.get("tickets_done"))
    teachers_count = _integer(kpis.get("teachers_count"))
    coverage_percent = _percentage(coverage.get("percent"))
    coverage_pending = _integer(coverage.get("pending"))
    has_staff = bool(coverage.get("has_staff", teachers_count > 0))
    completion_rate = round(tickets_done * 100 / tickets_total) if tickets_total else 0

    priorities: list[dict] = []
    if not has_staff:
        priorities.append(
            {
                "key": "team",
                "tone": "warning",
                "title": "ابدأ بتكوين فريق المدرسة",
                "description": "لا يوجد منسوبون نشطون بعد؛ أضف الفريق قبل قياس التغطية أو الأداء.",
                "action_label": "إدارة المنسوبين",
                "url": routes["teachers"],
                "weight": 100,
            }
        )
    elif coverage_pending:
        priorities.append(
            {
                "key": "coverage",
                "tone": "warning" if coverage_percent < 70 else "info",
                "title": "ارفع تغطية التوثيق",
                "description": f"لم يوثّق {coverage_pending} من أصل {teachers_count} خلال {period_label}.",
                "action_label": "تذكير غير الموثقين",
                "url": routes["coverage"],
                "weight": 90 if coverage_percent < 70 else 72,
            }
        )

    if tickets_open:
        priorities.append(
            {
                "key": "tickets",
                "tone": "warning" if tickets_open >= 5 else "info",
                "title": "راجع الطلبات المفتوحة",
                "description": f"يوجد {tickets_open} من طلبات المدرسة بانتظار المتابعة أو الإسناد.",
                "action_label": "فتح الطلبات",
                "url": routes["tickets"],
                "weight": 85 if tickets_open >= 5 else 65,
            }
        )

    report_trend = trends.get("reports_count") if isinstance(trends.get("reports_count"), dict) else {}
    if report_trend.get("direction") == "down":
        priorities.append(
            {
                "key": "reports",
                "tone": "info",
                "title": "تحقق من انخفاض نشاط التقارير",
                "description": "عدد التقارير أقل من الفترة السابقة المكافئة؛ راجع الأقسام قبل اتخاذ إجراء.",
                "action_label": "مراجعة التقارير",
                "url": routes["reports"],
                "weight": 60,
            }
        )

    if not priorities:
        priorities.append(
            {
                "key": "steady",
                "tone": "success",
                "title": "المؤشرات التشغيلية مستقرة",
                "description": "لا يظهر في مؤشرات الفترة ما يستدعي تدخلاً مباشرًا الآن.",
                "action_label": "استعراض الأداء",
                "url": routes["performance"],
                "weight": 20,
            }
        )

    priorities.sort(key=lambda item: (-item["weight"], item["key"]))
    priorities = priorities[:3]
    for index, item in enumerate(priorities, start=1):
        item["rank"] = index
        item.pop("weight", None)

    signals = [
        {
            "key": "coverage",
            "label": "تغطية التوثيق",
            "value": f"{coverage_percent}%" if has_staff else "—",
            "context": (
                f"{coverage_pending} بانتظار التوثيق"
                if has_staff and coverage_pending
                else ("اكتملت التغطية" if has_staff else "لا يوجد فريق بعد")
            ),
            "tone": (
                "success"
                if has_staff and coverage_pending == 0
                else ("warning" if has_staff else "neutral")
            ),
            "url": routes["dashboard_coverage"],
        },
        {
            "key": "reports",
            "label": "التقارير",
            "value": str(reports_count),
            "context": f"الاتجاه {_trend_label(report_trend)}",
            "tone": "info",
            "url": routes["reports"],
        },
        {
            "key": "tickets",
            "label": "الطلبات المفتوحة",
            "value": str(tickets_open),
            "context": f"نسبة الإكمال {completion_rate}%",
            "tone": "warning" if tickets_open else "success",
            "url": routes["tickets"],
        },
    ]

    if not has_staff:
        headline = "ابدأ بالفريق قبل قراءة الأداء"
        summary = (
            "لا توجد قاعدة تشغيلية كافية لقياس المدرسة بعد. أضف المنسوبين، ثم ابدأ "
            "بالتقارير والطلبات لتظهر لك قراءة إدارية قابلة للمقارنة."
        )
        status = "setup"
    elif coverage_pending or tickets_open:
        headline = "الأولوية الآن للتغطية والمتابعة"
        fragments = []
        if coverage_pending:
            fragments.append(f"تغطية التوثيق عند {coverage_percent}%")
        if tickets_open:
            fragments.append(f"والطلبات المفتوحة عددها {tickets_open}")
        summary = "، ".join(fragments) + ". ابدأ بالأولوية الأولى أدناه ثم راجع أثرها في الفترة التالية."
        status = "attention"
    else:
        headline = "المشهد التشغيلي مطمئن"
        summary = (
            f"اكتملت تغطية التوثيق ولا توجد طلبات مفتوحة في {period_label}. "
            "واصل المتابعة الدورية وراقب الاتجاه مقارنة بالفترة السابقة."
        )
        status = "steady"

    oldest_open = responsiveness.get("oldest_open")
    if isinstance(oldest_open, dict) and oldest_open.get("age_label"):
        oldest_open_label = str(oldest_open.get("age_label"))[:80]
    else:
        oldest_open_label = ""

    return {
        "status": status,
        "period": str(payload.get("period") or "all")[:16],
        "period_label": period_label,
        "generated_at": str(payload.get("generated_at") or "")[:32],
        "headline": headline,
        "summary": summary,
        "signals": signals,
        "priorities": priorities,
        "oldest_open_label": oldest_open_label,
        "ai_generated": False,
        "source_label": "تحليل مباشر من بيانات النظام",
    }


def safe_ai_snapshot(dashboard_payload: dict, brief: dict) -> dict:
    """Return the only aggregate data allowed to leave Tawtheeq."""

    payload = dashboard_payload if isinstance(dashboard_payload, dict) else {}
    kpis = payload.get("kpis") if isinstance(payload.get("kpis"), dict) else {}
    coverage = payload.get("coverage") if isinstance(payload.get("coverage"), dict) else {}
    trends = payload.get("trends") if isinstance(payload.get("trends"), dict) else {}
    return {
        "period": str(payload.get("period_label") or "الفترة المختارة")[:40],
        "metrics": {
            "reports": _integer(kpis.get("reports_count")),
            "open_requests": _integer(kpis.get("tickets_open")),
            "completed_requests": _integer(kpis.get("tickets_done")),
            "active_staff": _integer(kpis.get("teachers_count")),
            "documentation_coverage_percent": _percentage(coverage.get("percent")),
            "staff_pending_documentation": _integer(coverage.get("pending")),
            "report_trend": _trend_label(
                trends.get("reports_count") if isinstance(trends.get("reports_count"), dict) else {}
            ),
        },
        "ranked_priorities": [
            {
                "title": str(item.get("title") or "")[:120],
                "reason": str(item.get("description") or "")[:220],
            }
            for item in brief.get("priorities", [])[:3]
        ],
    }


_AI_INSTRUCTIONS = """
أنت مستشار تشغيل مدرسي سعودي يكتب موجزًا تنفيذيًا هادئًا لمدير المدرسة.

اكتب عنوانًا وجملة موجزة تساعد المدير على فهم الأولوية، اعتمادًا حصراً على
المؤشرات والأولويات المرتبة في JSON. لا تخترع سببًا أو رقمًا أو اسمًا أو
مقارنة. لا تقترح عقوبة أو تقييماً للأشخاص، ولا تدّع أنك راجعت محتوى التقارير.
لا تغيّر ترتيب الأولويات ولا تنشئ إجراءات جديدة؛ الإجراءات تعرضها المنصة
من قواعدها الموثوقة. استخدم العربية الفصحى المباشرة، بلا مبالغة أو Markdown.
""".strip()

_AI_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string", "maxLength": MAX_HEADLINE_LENGTH},
        "summary": {"type": "string", "maxLength": MAX_SUMMARY_LENGTH},
    },
    "required": ["headline", "summary"],
    "additionalProperties": False,
}


def _clean_copy(value, limit: int) -> str:
    text = " ".join(str(value or "").replace("```", " ").split())
    if len(text) > limit:
        raise ValueError("manager brief copy exceeded its limit")
    return text.strip()


_ARABIC_INDIC_DIGITS = str.maketrans(
    "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹",
    "01234567890123456789",
)


def _figures_in(value) -> set[str]:
    return set(re.findall(r"\d+", str(value or "").translate(_ARABIC_INDIC_DIGITS)))


def _cache_key(school_id: int, safe_snapshot: dict) -> str:
    material = json.dumps(safe_snapshot, ensure_ascii=False, sort_keys=True).encode("utf-8")
    digest = hashlib.sha256(material).hexdigest()[:24]
    return f"manager-smart-brief:v1:{int(school_id)}:{digest}"


def generate_manager_brief(dashboard_payload: dict, *, school, teacher) -> dict:
    """Optionally polish the deterministic brief, with a silent safe fallback."""

    brief = build_manager_brief(dashboard_payload)
    if not manager_brief_ai_enabled():
        return brief

    snapshot = safe_ai_snapshot(dashboard_payload, brief)
    cache_key = _cache_key(getattr(school, "pk", 0) or 0, snapshot)
    try:
        cached = cache.get(cache_key)
    except Exception:
        cached = None
    if isinstance(cached, dict):
        return deepcopy(cached)

    model = str(
        getattr(
            settings,
            "REPORT_REVIEW_MODEL",
            getattr(settings, "REPORT_AI_MODEL", "gpt-5.6-luna"),
        )
    )
    body = {
        "model": model,
        "instructions": _AI_INSTRUCTIONS,
        "input": json.dumps(snapshot, ensure_ascii=False),
        "reasoning": {"effort": str(getattr(settings, "AI_FAST_REASONING_EFFORT", "none"))},
        "text": {
            "verbosity": "low",
            "format": {
                "type": "json_schema",
                "name": "tawtheeq_manager_brief",
                "strict": True,
                "schema": _AI_SCHEMA,
            },
        },
        "max_output_tokens": 500,
        "store": False,
    }
    try:
        with ai_usage_context(school=school, teacher=teacher):
            payload = responses_create(
                body,
                api_key=str(settings.OPENAI_API_KEY).strip(),
                timeout=min(float(getattr(settings, "REPORT_AI_TIMEOUT_SECONDS", 25)), 30.0),
                stage="manager-brief",
            )
        if truncation_reason(payload):
            raise ValueError("incomplete response")
        copy = json.loads(extract_output_text(payload))
        headline = _clean_copy(copy.get("headline"), MAX_HEADLINE_LENGTH)
        summary = _clean_copy(copy.get("summary"), MAX_SUMMARY_LENGTH)
        if not headline or not summary:
            raise ValueError("empty brief copy")
        if re.search(r"https?://|www\.", headline + " " + summary, flags=re.IGNORECASE):
            raise ValueError("unexpected external link")
        allowed_figures = _figures_in(json.dumps(snapshot, ensure_ascii=False))
        invented_figures = _figures_in(headline + " " + summary) - allowed_figures
        if invented_figures:
            raise ValueError("manager brief invented a figure")
    except (HTTPError, URLError, TimeoutError, ValueError, TypeError, json.JSONDecodeError) as exc:
        logger.warning("Manager smart brief used deterministic copy: %s", exc.__class__.__name__)
        return brief

    brief["headline"] = headline
    brief["summary"] = summary
    brief["ai_generated"] = True
    brief["source_label"] = "صياغة ذكية من مؤشرات المدرسة المجمعة"
    try:
        cache.set(cache_key, brief, BRIEF_CACHE_SECONDS)
    except Exception:
        logger.debug("Unable to cache manager smart brief", exc_info=True)
    return brief
