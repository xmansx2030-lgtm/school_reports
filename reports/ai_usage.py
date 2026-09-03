"""تسجيل استهلاك الذكاء الاصطناعي — نقطةٌ واحدة يمرّ بها كل نداء.

**السياق ينتقل بـ``contextvars`` لا بالوسائط.** نسبةُ النداء إلى مدرسةٍ ومستخدم
تحتاج معرفتهما في ``report_ai`` و``voice_report`` و``mansour_assistant``، وهذه
وحداتُ خدمةٍ لا ترى الطلب. وتمريرُهما وسيطاً يعني تغيير توقيع خمس دوال عامة
عبر ثلاث وحدات لأجل قياس. فالحدّ يُوضع عند حافة الطلب:

    with ai_usage_context(school=..., teacher=...):
        improve_report_text(text)

وهو نمطٌ قائم في المشروع أصلاً — ``core/trace_context.py``.

**والقياس لا يُسقط نداءً أبداً.** كل ما هنا مغلَّفٌ بـ``try/except``: تعذُّر
الكتابة في الجدول يعني ضياع صفٍّ من القياس، لا فشلَ تحسينِ صياغةٍ ينتظرها معلّم.

**السعر من الإعداد، ولا سعر افتراضي.** أسعار المزوّد تتغيّر ولا تُخمَّن، فإن لم
يُضبط ``AI_MODEL_PRICING`` بقيت الكلفة ``NULL`` والرموزُ محفوظةٌ كاملة — ويمكن
حساب الكلفة منها لاحقاً متى عُرف السعر.
"""

from __future__ import annotations

import contextvars
import json
import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.utils.crypto import salted_hmac

from core import opmetrics


logger = logging.getLogger(__name__)

TOKENS_PER_PRICE_UNIT = Decimal(1_000_000)


@dataclass(frozen=True)
class AiUsageContext:
    """من يُنسب إليه النداء. كلاهما اختياري: منصور يخدم زائراً بلا حساب."""

    school_id: int | None = None
    teacher_id: int | None = None


_EMPTY = AiUsageContext()
_context: contextvars.ContextVar[AiUsageContext] = contextvars.ContextVar(
    "ai_usage_context", default=_EMPTY
)


class ai_usage_context:  # noqa: N801 - يُستعمل مدير سياق لا صنفاً
    """يربط كل نداء داخله بمدرسةٍ ومستخدم."""

    def __init__(self, *, school=None, teacher=None):
        self._value = AiUsageContext(
            school_id=_pk_of(school),
            teacher_id=_pk_of(teacher),
        )
        self._token = None

    def __enter__(self) -> AiUsageContext:
        self._token = _context.set(self._value)
        return self._value

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        if self._token is not None:
            try:
                _context.reset(self._token)
            except ValueError:  # pragma: no cover - سياقٌ عُبر عنه في خيط آخر
                pass
        return False


def _pk_of(value) -> int | None:
    if value is None:
        return None
    pk = getattr(value, "pk", value)
    try:
        pk = int(pk)
    except (TypeError, ValueError):
        return None
    return pk if pk > 0 else None


def safety_identifier_for(teacher_id: int | None) -> str:
    """معرّفٌ مستقرّ للمستخدم لدى المزوّد، بلا كشف هويته.

    هو ما يجعل بلاغ إساءةٍ من المزوّد قابلاً للتتبّع إلى حسابٍ واحد بدل أن
    يُعلَّق على المنصة كلها. و``salted_hmac`` يعني أنه ثابتٌ لنا وعديم المعنى
    لغيرنا — فلا رقم جوال ولا اسم يغادر الخادم.
    """
    if not teacher_id:
        return ""
    digest = salted_hmac("ai.safety.identifier", str(int(teacher_id))).hexdigest()[:24]
    return f"tawtheeq_{digest}"


def current_context() -> AiUsageContext:
    try:
        return _context.get()
    except LookupError:  # pragma: no cover
        return _EMPTY


# ── الأسعار ──────────────────────────────────────────────────────────────
def model_pricing() -> dict[str, dict[str, Decimal]]:
    """جدول الأسعار لكل مليون رمز، من الإعداد.

    الشكل::

        {"gpt-5.6-luna": {"input": 0.05, "cached_input": 0.005, "output": 0.40}}

    ``cached_input`` اختياري؛ وغيابه يعني حساب المخزَّن بسعر الإدخال الكامل —
    وهو تقديرٌ أعلى من الحقيقة، وهذا الاتجاه الآمن في تقدير فاتورة.
    """
    raw = getattr(settings, "AI_MODEL_PRICING", None) or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("AI_MODEL_PRICING is not valid JSON; costs stay unpriced.")
            return {}
    if not isinstance(raw, dict):
        return {}

    table: dict[str, dict[str, Decimal]] = {}
    for name, prices in raw.items():
        if not isinstance(prices, dict):
            continue
        entry: dict[str, Decimal] = {}
        for key in ("input", "cached_input", "output"):
            if key not in prices:
                continue
            try:
                entry[key] = Decimal(str(prices[key]))
            except (InvalidOperation, TypeError, ValueError):
                continue
        if "input" in entry and "output" in entry:
            table[str(name).strip()] = entry
    return table


def estimate_cost(
    model: str,
    *,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
) -> Decimal | None:
    """كلفة النداء بأسعار اللحظة، أو ``None`` إن لم يُعرف سعر النموذج."""
    prices = model_pricing().get(str(model or "").strip())
    if not prices:
        return None

    # نداءٌ بلا رموز أصلاً — التفريغ الصوتي لا يعيد ``usage`` — كلفتُه **مجهولة**
    # لا صفر. و«$0.0000» في تقريرٍ يقرؤه محاسبٌ يعني «مجاني»، وهو ادّعاء.
    if int(input_tokens) <= 0 and int(output_tokens) <= 0:
        return None

    cached = max(0, int(cached_input_tokens))
    fresh = max(0, int(input_tokens) - cached)
    cached_price = prices.get("cached_input", prices["input"])

    total = (
        Decimal(fresh) * prices["input"]
        + Decimal(cached) * cached_price
        + Decimal(max(0, int(output_tokens))) * prices["output"]
    ) / TOKENS_PER_PRICE_UNIT
    return total.quantize(Decimal("0.000001"))


# ── التسجيل ──────────────────────────────────────────────────────────────
def usage_numbers(payload: dict) -> dict[str, int]:
    """يستخرج أرقام الاستخدام من رد المزوّد بقراءة متساهلة.

    مسار التفريغ الصوتي لا يعيد ``usage`` أصلاً، ورد المزوّد قد يغيّر تسمية
    حقلٍ فرعيّ. وفي الحالتين تُسجَّل الواقعة بأصفار بدل أن تسقط.
    """
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        return {"input": 0, "cached": 0, "output": 0, "reasoning": 0}

    input_details = usage.get("input_tokens_details")
    input_details = input_details if isinstance(input_details, dict) else {}
    output_details = usage.get("output_tokens_details")
    output_details = output_details if isinstance(output_details, dict) else {}

    def _int(value) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    return {
        "input": _int(usage.get("input_tokens")),
        "cached": _int(input_details.get("cached_tokens")),
        "output": _int(usage.get("output_tokens")),
        "reasoning": _int(output_details.get("reasoning_tokens")),
    }


def usage_recording_enabled() -> bool:
    return bool(getattr(settings, "AI_USAGE_TRACKING_ENABLED", True))


def record_ai_call(
    *,
    stage: str,
    model: str,
    outcome: str,
    payload: dict | None = None,
    duration_ms: int = 0,
    error_kind: str = "",
) -> None:
    """يكتب واقعة نداءٍ واحدة. لا يرفع استثناءً بحال."""
    if not usage_recording_enabled():
        return
    try:
        from .models import AiUsageEvent

        numbers = usage_numbers(payload or {})
        context = current_context()
        stage_value = (
            stage
            if stage in dict(AiUsageEvent.Stage.choices)
            else AiUsageEvent.Stage.OTHER
        )

        AiUsageEvent.objects.create(
            stage=stage_value,
            model_name=str(model or "")[:64],
            outcome=outcome,
            error_kind=str(error_kind or "")[:64],
            school_id=context.school_id,
            teacher_id=context.teacher_id,
            input_tokens=numbers["input"],
            cached_input_tokens=numbers["cached"],
            output_tokens=numbers["output"],
            reasoning_tokens=numbers["reasoning"],
            duration_ms=max(0, int(duration_ms)),
            estimated_cost=estimate_cost(
                model,
                input_tokens=numbers["input"],
                cached_input_tokens=numbers["cached"],
                output_tokens=numbers["output"],
            ),
        )
        _increment_counters(str(stage_value), outcome, numbers)
    except Exception:
        # قياسٌ يُسقط النداء الذي يقيسه أسوأ من غياب القياس.
        logger.exception("Unable to record AI usage stage=%s", stage)


def _increment_counters(stage: str, outcome: str, numbers: dict[str, int]) -> None:
    """عدّادات اللحظة في Redis — للإنذار السريع، والجدول للتحليل."""
    try:
        opmetrics.increment(f"ai.call.{outcome}")
        opmetrics.increment(f"ai.call.{stage}")
        if numbers["cached"] > 0:
            opmetrics.increment("ai.cache.hit")
        elif numbers["input"] > 0:
            opmetrics.increment("ai.cache.miss")
    except Exception:  # noqa: S110 - عدّادُ لحظةٍ لا يستحق سطر لوق عند كل تعذّر
        pass
