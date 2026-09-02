"""ما يقع بين إرسال الطلب إلى المزوّد واستلام نصٍّ يصلح للعرض.

ثلاث مسائل تتكرّر في المسارات الثلاثة (منصور، تحسين التقارير، التفريغ الصوتي)،
وكانت كلٌّ منها تُحلّ — أو تُهمَل — على حدة:

**١. العطل العابر.** كان أي خطأ يُترجَم فوراً إلى رسالة اعتذار. و502 من بوّابة
المزوّد يستغرق جزءاً من ثانية، فإعادة المحاولة تنجح غالباً قبل أن يلاحظ
المستخدم شيئاً. لكن الإعادة محكومة بالمهلة نفسها لا بعدد المحاولات: المهلة
المضبوطة تبقى السقف الذي يراه المستخدم، فلا تتحوّل «٢٥ ثانية» إلى ٥٠.

**٢. الإجابة المبتورة.** ``max_output_tokens`` يحدّ الإخراج المرئي **ورموز
التفكير معاً**. فإذا التهم التفكيرُ الميزانية عاد النصّ مقطوعاً في منتصف
الجملة، و``status`` عندها ``incomplete`` — بينما الحقل ``output_text`` يبدو
سليماً تماماً. فقرةٌ مبتورة تُعرض على معلّم كأنها تقريرٌ تامّ أسوأ من خطأ صريح:
الخطأ يُعيد المحاولة، والبتر يُعتمَد ويُرسَل.

**٣. ما لا يُقاس لا يُدار.** أرقام الاستخدام هي الطريقة الوحيدة لمعرفة أن
تخزين البادئة يعمل فعلاً في الإنتاج، لا في الاختبار وحده.
"""

from __future__ import annotations

import json
import logging
import random
import time
from typing import Any, Callable
from urllib.request import Request, urlopen

from .ai_errors import is_transient_openai_error


logger = logging.getLogger(__name__)

# محاولتان لا أكثر. الثالثة تضيف تأخيراً يشعر به المستخدم أمام عطلٍ ثابت.
MAX_ATTEMPTS = 2
# لا تُعاد المحاولة ما لم يتبقّ من المهلة ما يكفي لطلبٍ كامل. وبهذا لا تُعاد
# حالةُ انتهاء المهلة أصلاً — وهو الصواب: الطلب البطيء لن يصير أسرع بإعادته.
MIN_RETRY_BUDGET_SECONDS = 4.0
RETRY_BASE_DELAY_SECONDS = 0.4


def _retry_delay(attempt: int) -> float:
    """تراجعٌ أُسّي مع تشويش.

    التشويش ليس زينة: بدونه ترتدّ كل الطلبات المتزامنة على المزوّد في اللحظة
    نفسها بعد عطلٍ عام، فتصنع الذروة التي أسقطته.
    """
    # noqa: S311 — تشويشُ توقيتٍ لا قيمةٌ سرّية؛ لا يحرس شيئاً كي يحتاج CSPRNG.
    return RETRY_BASE_DELAY_SECONDS * (2**attempt) * (0.5 + random.random())  # noqa: S311


def request_json(
    request: Request,
    *,
    timeout: float,
    stage: str,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """ينفّذ الطلب ويعيد حمولته، مع محاولة ثانية للعطل العابر داخل المهلة."""
    deadline = time.monotonic() + float(timeout)
    for attempt in range(MAX_ATTEMPTS):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"AI {stage} budget exhausted")
        try:
            with urlopen(request, timeout=remaining) as response:  # noqa: S310
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            last_attempt = attempt + 1 >= MAX_ATTEMPTS
            if last_attempt or not is_transient_openai_error(exc):
                raise
            delay = _retry_delay(attempt)
            if (deadline - time.monotonic()) - delay < MIN_RETRY_BUDGET_SECONDS:
                raise
            logger.info(
                "AI %s retrying after %s.", stage, exc.__class__.__name__
            )
            sleep(delay)
    raise TimeoutError(f"AI {stage} exhausted its attempts")  # pragma: no cover


def truncation_reason(payload: dict[str, Any]) -> str:
    """سبب عدم اكتمال الاستجابة، أو نصٌّ فارغ إن كانت تامّة.

    القراءة متساهلة عمداً: يكفي أن ``status`` ليست ``completed`` للحكم بالبتر،
    فلا نعتمد على مطابقة نصّ ``reason`` بعينه حتى لا يمرّ بترٌ لأن المزوّد
    سمّى سببه باسمٍ جديد.
    """
    status = str(payload.get("status") or "").strip()
    if status in {"", "completed"}:
        return ""
    details = payload.get("incomplete_details")
    if isinstance(details, dict):
        reason = str(details.get("reason") or "").strip()
        if reason:
            return reason
    return status


def log_usage(payload: dict[str, Any], *, stage: str, model: str) -> None:
    """يسجّل أرقام الاستخدام، ومنها إصابة المخزَّن.

    ``cached`` هو الدليل الوحيد على أن تخزين البادئة يعمل في الإنتاج: إن بقي
    صفراً بينما البادئة ثابتة، فالبادئة ليست ثابتة فعلاً — أو أنها لم تبلغ
    الحدّ الأدنى للتخزين.
    """
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return
    input_details = usage.get("input_tokens_details")
    input_details = input_details if isinstance(input_details, dict) else {}
    output_details = usage.get("output_tokens_details")
    output_details = output_details if isinstance(output_details, dict) else {}
    logger.info(
        "AI usage stage=%s model=%s input=%s cached=%s cache_write=%s "
        "output=%s reasoning=%s",
        stage,
        model,
        usage.get("input_tokens"),
        input_details.get("cached_tokens"),
        input_details.get("cache_write_tokens"),
        usage.get("output_tokens"),
        output_details.get("reasoning_tokens"),
    )
