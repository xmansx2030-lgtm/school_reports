from __future__ import annotations

import json
import logging

from django.conf import settings
from django.http import HttpRequest, JsonResponse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django_ratelimit.decorators import ratelimit

from core import opmetrics
from core.limits_cache import limits_cache

from ..mansour_assistant import (
    MansourAssistantError,
    MansourAssistantUnavailable,
    ask_mansour,
    infer_public_audience,
    normalise_audience,
)
from ..mansour_knowledge import (
    AUDIENCE_GENERAL,
    AUDIENCE_LABELS,
    AUDIENCE_MANAGER,
    AUDIENCE_TEACHER,
    PUBLIC_AUDIENCES,
)
from ..models import SubscriptionPlan
from ..ai_features import (
    FEATURE_INTERNAL_HELP,
    FEATURE_MANSOUR_PUBLIC,
    platform_ai_toggle_enabled,
    mansour_chat_scope,
)
from ..guidance import role_guidance
from ..permissions import effective_user_role_label, is_school_manager
from ._helpers import _get_active_school
from ..ai_usage import ai_usage_context


logger = logging.getLogger(__name__)

DAILY_BUDGET_CACHE_PREFIX = "mansour:daily-calls"


def _personal_assistant_context(request: HttpRequest, active_school) -> dict:
    """Return useful account state without sending identifying school data."""
    user = getattr(request, "user", None)
    if not getattr(user, "is_authenticated", False):
        return {}

    context = {
        "authenticated": True,
        "role_key": "",
        "role_label": effective_user_role_label(user, active_school=active_school),
        "active_school_state": "محددة" if active_school is not None else "غير محددة",
    }
    try:
        journey = role_guidance(user, active_school)
    except Exception:
        logger.warning("Mansour personal guidance context unavailable.", exc_info=True)
        return context

    context["role_key"] = str(journey.get("role") or "")
    context["journey_title"] = str(journey.get("title") or "")
    total = int(journey.get("total") or 0)
    completed = int(journey.get("completed") or 0)
    if total:
        context["readiness_summary"] = f"{completed} من {total} خطوات مكتملة"
    next_step = journey.get("next_step")
    if isinstance(next_step, dict):
        context["next_step_title"] = str(next_step.get("title") or "")
        context["next_step_description"] = str(next_step.get("description") or "")
    return context


def _json_response(payload: dict, *, status: int = 200) -> JsonResponse:
    return JsonResponse(
        payload,
        status=status,
        json_dumps_params={"ensure_ascii": False},
    )


# ``none`` تعني تنقّلاً مكتوباً في شريط العنوان أو تطبيق PWA مثبَّتاً — وكلاهما
# استعمال مشروع. والمرفوض هو ``cross-site`` وحده.
_ALLOWED_FETCH_SITES = frozenset({"same-origin", "same-site", "none"})


def _daily_budget_exhausted() -> bool:
    """Reserve one paid assistant call against the platform-wide daily ceiling.

    The per-IP limiter bounds a single visitor; this bounds the invoice. The
    counter lives in the dedicated limits store — not the view cache, which is
    deliberately evictable and swallows its errors. An evicted budget counter
    reads as "nothing spent yet", so it would lift the invoice ceiling exactly
    when traffic is heaviest.

    If the store is unavailable we still allow the call: an outage must not take
    the assistant offline. The cost of that gap is bounded by the per-IP limiter
    and is logged at ``error`` so it is visible rather than silent.
    """
    limit = int(getattr(settings, "MANSOUR_ASSISTANT_DAILY_GLOBAL_LIMIT", 0) or 0)
    if limit <= 0:
        return False

    key = f"{DAILY_BUDGET_CACHE_PREFIX}:{timezone.localdate().isoformat()}"
    try:
        store = limits_cache()
        # Two days of TTL so the key survives the timezone boundary.
        store.add(key, 0, timeout=60 * 60 * 48)
        used = int(store.incr(key))
    except Exception:
        logger.error(
            "Mansour daily budget counter unavailable; allowing the call.",
            exc_info=True,
        )
        opmetrics.increment("mansour.budget.store_unavailable")
        return False

    if used > limit:
        if used == limit + 1:
            logger.error(
                "Mansour assistant daily platform limit reached limit=%s date=%s",
                limit,
                timezone.localdate().isoformat(),
            )
        return True
    return False


def _resolve_audience(request: HttpRequest, requested_audience, question="", history=None) -> str:
    """Resolve trusted account roles server-side; visitors may select a public role."""
    user = getattr(request, "user", None)
    if not getattr(user, "is_authenticated", False):
        audience = normalise_audience(requested_audience)
        if audience in PUBLIC_AUDIENCES and audience != AUDIENCE_GENERAL:
            return audience
        inferred = infer_public_audience(question)
        if inferred != AUDIENCE_GENERAL:
            return inferred
        if isinstance(history, list):
            for message in reversed(history[-6:]):
                if not isinstance(message, dict) or message.get("role") != "user":
                    continue
                inferred = infer_public_audience(message.get("content"))
                if inferred != AUDIENCE_GENERAL:
                    return inferred
        return AUDIENCE_GENERAL

    if getattr(user, "is_superuser", False):
        return AUDIENCE_MANAGER

    active_school = _get_active_school(request)
    if is_school_manager(user, active_school=active_school):
        return AUDIENCE_MANAGER
    return AUDIENCE_TEACHER


def _ask_mansour_attributed(request: HttpRequest, question, **kwargs):
    """ينادي منصور داخل سياق قياسٍ منسوب — إن كان هناك من يُنسب إليه.

    منصور وحده بين مسارات الذكاء الاصطناعي يخدم زائراً بلا حساب ولا مدرسة،
    فيُسجَّل نداؤه بلا نسبة. وهذا مقصود لا نقص: كلفةُ الزوّار رقمٌ يخصّ المنصة
    نفسها لا مدرسةً بعينها، والفصل بينهما هو ما يجعل «كم أنفقت هذه المدرسة»
    سؤالاً له جواب.
    """
    user = getattr(request, "user", None)
    if not getattr(user, "is_authenticated", False):
        user = None
    school = _get_active_school(request) if user is not None else None
    with ai_usage_context(school=school, teacher=user):
        return ask_mansour(question, **kwargs)


@csrf_exempt
@never_cache
@require_POST
@ratelimit(key="ip", rate="50/d", method="POST", block=False)
def mansour_assistant_reply(request: HttpRequest) -> JsonResponse:
    user = getattr(request, "user", None)
    feature = (
        FEATURE_INTERNAL_HELP
        if getattr(user, "is_authenticated", False)
        else FEATURE_MANSOUR_PUBLIC
    )
    if not platform_ai_toggle_enabled(feature):
        return _json_response(
            {"ok": False, "message": "المساعد غير متاح حالياً."},
            status=404,
        )

    if getattr(request, "limited", False):
        return _json_response(
            {
                "ok": False,
                "message": "وصلت إلى الحد اليومي البالغ 50 سؤالًا. يمكنك استخدام منصور مجددًا غدًا.",
            },
            status=429,
        )

    if request.content_type != "application/json":
        return _json_response(
            {"ok": False, "message": "صيغة الطلب غير صحيحة."},
            status=415,
        )

    # هذه النقطة ``csrf_exempt`` وتستدعي واجهة مدفوعة، فبلا هذا الشرط يستطيع
    # موقعٌ خارجي أن يستنزف الميزانية من متصفحات زوّاره: الطلبات تصل بعناوين
    # الضحايا فتتوزّع على حدّ الـ IP بدل أن تصطدم به.
    #
    # و``if fetch_site`` مقصود: المتصفحات القديمة لا ترسل الترويسة أصلاً، ورفضُ
    # من لا يرسلها يقطع الخدمة عن مستخدمين شرعيين لأجل ضابط تكميلي.
    fetch_site = (request.headers.get("Sec-Fetch-Site") or "").strip().lower()
    if fetch_site and fetch_site not in _ALLOWED_FETCH_SITES:
        logger.warning("Mansour cross-site request refused sec_fetch_site=%s", fetch_site)
        opmetrics.increment("mansour.request.cross_site_refused")
        return _json_response(
            {"ok": False, "message": "طلب غير مسموح."},
            status=403,
        )

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _json_response(
            {"ok": False, "message": "تعذر قراءة الاستفسار."},
            status=400,
        )

    if not isinstance(payload, dict):
        return _json_response(
            {"ok": False, "message": "صيغة الطلب غير صحيحة."},
            status=400,
        )

    if _daily_budget_exhausted():
        return _json_response(
            {
                "ok": False,
                "message": "منصور مشغول جدًا اليوم بسبب كثرة الأسئلة. جرّب مجددًا غدًا أو تواصل مع الدعم.",
            },
            status=429,
        )

    plans = list(
        SubscriptionPlan.objects.filter(is_active=True)
        .order_by("price", "max_teachers", "days_duration", "id")
        .values("name", "price", "days_duration", "max_teachers")
    )
    serialised_plans = []
    seen_free_trials = set()
    for plan in plans:
        price = f"{plan['price']:.2f}".rstrip("0").rstrip(".")
        if price == "0":
            free_trial_key = (plan["days_duration"], plan["max_teachers"])
            if free_trial_key in seen_free_trials:
                continue
            seen_free_trials.add(free_trial_key)
        serialised_plans.append({**plan, "price": price})

    active_school = _get_active_school(request) if getattr(user, "is_authenticated", False) else None
    personal_context = _personal_assistant_context(request, active_school)
    audience = _resolve_audience(
        request,
        payload.get("audience"),
        payload.get("question"),
        payload.get("history"),
    )
    if personal_context.get("role_key") in {
        "manager",
        "deputy",
        "admin_staff",
        "executive",
        "platform",
    }:
        audience = AUDIENCE_MANAGER

    try:
        if not getattr(request.session, "session_key", None):
            request.session.create()
        answer, sources = _ask_mansour_attributed(
            request,
            payload.get("question"),
            history=payload.get("history"),
            plans=serialised_plans,
            audience=audience,
            page_context=(
                payload.get("page_context")
                if getattr(request.user, "is_authenticated", False)
                else None
            ),
            personal_context=personal_context,
            safety_identifier=f"tawtheeq_{mansour_chat_scope(request)}",
        )
    except MansourAssistantError as exc:
        status = (
            503
            if isinstance(exc, MansourAssistantUnavailable)
            or not getattr(settings, "MANSOUR_ASSISTANT_ENABLED", False)
            or not getattr(settings, "OPENAI_API_KEY", "")
            else 400
        )
        return _json_response({"ok": False, "message": str(exc)}, status=status)

    return _json_response(
        {
            "ok": True,
            "answer": answer,
            "sources": sources,
            "audience": audience,
            "audience_label": AUDIENCE_LABELS[audience],
        }
    )
