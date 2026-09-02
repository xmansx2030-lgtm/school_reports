from __future__ import annotations

import json
from urllib.error import HTTPError, URLError


AI_SERVICE_PAUSED_MESSAGE = (
    "خدمة الذكاء الاصطناعي متوقفة مؤقتًا حاليًا. "
    "يمكنك متابعة استخدام بقية خدمات المنصة كالمعتاد."
)

OPENAI_SPEND_LIMIT_ERROR_CODES = frozenset(
    {
        "organization_spend_limit_exceeded",
        "project_spend_limit_exceeded",
    }
)


# الحالات التي تستحق محاولة ثانية: عطلٌ عابر لدى المزوّد أو في الشبكة، لا خطأ
# في طلبنا. و400 و401 و404 ليست منها — إعادتها تضيّع وقت المستخدم بلا أمل.
TRANSIENT_HTTP_STATUSES = frozenset({408, 409, 500, 502, 503, 504})


def openai_error_code(exc: HTTPError) -> str:
    """يقرأ رمز الخطأ من جسم الاستجابة مرة واحدة ويحتفظ به.

    جسم ``HTTPError`` تيّارٌ يُستهلك بالقراءة الأولى. فالنسخة السابقة كانت تعيد
    ``False`` في أي استدعاء ثانٍ مهما كان الخطأ، وهو ما يحوّل «تجاوزتَ حدّ
    الإنفاق» إلى «تعذّر الاتصال» متى فُحص الاستثناء مرتين — وهذا ما يحدث حين
    يقرّر المُعيد أيَعيد المحاولة ثم يقرّر المتّصل أي رسالة يعرض.
    """
    cached = getattr(exc, "_tawtheeq_error_code", None)
    if cached is not None:
        return cached

    code = ""
    try:
        payload = json.loads(exc.read().decode("utf-8"))
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            code = str(error.get("code") or "").strip()
    except (AttributeError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        code = ""

    try:
        exc._tawtheeq_error_code = code
    except AttributeError:  # pragma: no cover - استثناء لا يقبل الإسناد
        pass
    return code


def is_openai_spend_limit_error(exc: HTTPError) -> bool:
    """Return whether an OpenAI HTTP response represents a hard spend limit."""
    if getattr(exc, "code", None) != 429:
        return False
    return openai_error_code(exc) in OPENAI_SPEND_LIMIT_ERROR_CODES


def is_transient_openai_error(exc: BaseException) -> bool:
    """هل يستحق هذا العطل محاولة ثانية؟

    حدّ الإنفاق ليس عابراً: إعادته ثلاث مرات تُنفق ثلاثة طلبات على رفضٍ مؤكّد
    وتؤخّر رسالةَ «الخدمة متوقفة» التي يستحق المستخدم رؤيتها فوراً.
    """
    if isinstance(exc, HTTPError):
        if exc.code == 429:
            return not is_openai_spend_limit_error(exc)
        return exc.code in TRANSIENT_HTTP_STATUSES
    return isinstance(exc, (URLError, TimeoutError))
