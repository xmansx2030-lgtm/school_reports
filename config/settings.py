# config/settings.py
from __future__ import annotations

from pathlib import Path
from ipaddress import ip_address
import os
import logging
from urllib.parse import urlsplit, urlunsplit

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

# حاول استخدام dj_database_url إن كان مُثبتًا، بدون كسر المشروع لو غير موجود
try:
    import dj_database_url  # type: ignore
except Exception:
    dj_database_url = None  # type: ignore

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent


# ----------------- Helpers -----------------
def _env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return str(val).strip().lower() in {"1", "true", "yes", "on"}


def _split_env_list(val: str) -> list[str]:
    return [x.strip() for x in (val or "").split(",") if x.strip()]


def _media_querystring_auth_enabled(
    *,
    public_access_enabled: bool,
    requested_querystring_auth: bool,
) -> bool:
    """Never allow unsigned media URLs unless public access is explicit."""
    return (not public_access_enabled) or bool(requested_querystring_auth)


def _validated_site_url(value: str, *, environment: str) -> str:
    """Validate the canonical origin before it can drive permanent redirects."""
    site_url = str(value or "").strip().rstrip("/")
    parsed = urlsplit(site_url)
    hostname = (parsed.hostname or "").lower().rstrip(".")

    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ImproperlyConfigured(
            "SITE_URL must be a plain origin such as https://tawtheeq-ksa.com."
        )

    if environment == "production":
        local_hostname = hostname == "localhost" or hostname.endswith(".localhost")
        try:
            address = ip_address(hostname)
            local_hostname = local_hostname or address.is_loopback or address.is_unspecified
        except ValueError:
            pass
        if parsed.scheme != "https" or local_hostname:
            raise ImproperlyConfigured(
                "SITE_URL must use HTTPS and a public hostname in production; "
                f"got {site_url!r}."
            )

    return site_url


# ----------------- Environment -----------------
ENV = os.getenv("ENV", "development").strip().lower()

# يمكنك أيضًا فرض DEBUG عبر DEBUG=1
DEBUG = (ENV != "production") if os.getenv("DEBUG") is None else _env_bool("DEBUG", False)

# في الإنتاج نمنع السقوط على backends غير موزعة (SQLite/LocMem/InMemory) بشكل افتراضي.
PRODUCTION_STRICT_MODE = _env_bool("PRODUCTION_STRICT_MODE", ENV == "production")

# ----------------- Logging (early) -----------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO))
logger = logging.getLogger(__name__)

logger.info("Current Environment: %s", ENV)
logger.info("DEBUG: %s", DEBUG)


# ----------------- Error monitoring -----------------
SENTRY_DSN = (os.getenv("SENTRY_DSN") or "").strip()
SENTRY_RELEASE = (os.getenv("SENTRY_RELEASE") or "").strip() or None
try:
    SENTRY_TRACES_SAMPLE_RATE = min(
        1.0,
        max(0.0, float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.05") or "0.05")),
    )
except (TypeError, ValueError):
    SENTRY_TRACES_SAMPLE_RATE = 0.05

# أسماء الحقول التي لا تغادر الخادم أبداً. المطابقة على **جزء** من الاسم لا على
# مطابقته كاملاً: الحقل قد يصل باسم ``new_password1`` أو ``teacher_phone`` أو
# ``id_number``، وقائمةُ أسماءٍ دقيقة تفوت ما لم يُتوقَّع.
_SENTRY_SENSITIVE_HINTS = (
    "password", "passwd", "secret", "token", "authorization", "auth",
    "csrf", "session", "cookie", "api_key", "apikey", "otp", "vapid",
    "phone", "mobile", "national", "identity", "id_number", "iqama",
    "iban", "card", "cvv",
    # ``identifier`` هو اسم المتغيّر الذي يحمل رقم الجوال أو الهوية في مسار
    # تسجيل الدخول (``reports.views.auth.login_view``) — وهو أكثر الإطارات
    # احتمالاً لأن يُلتقط، لأن مسار الفشل هناك هو ما يرمي.
    "identifier", "username", "email",
)


def _sentry_scrub(event, hint):  # pragma: no cover - يعمل في الإنتاج وحده
    """ينزع البيانات الشخصية والأسرار قبل مغادرة العملية.

    ``send_default_pii=False`` يمنع Sentry من **إضافة** بيانات المستخدم وعنوانه،
    لكنه لا يمسّ ما التقطه أصلاً من محتوى الطلب ومتغيّرات الإطارات. ودالةٌ رمت
    استثناءً وفيها متغيّر ``password`` أو ``national_id`` تُرسل قيمته كما هي —
    وهي بالضبط البيانات التي يحكمها نظام حماية البيانات الشخصية السعودي.

    والتنقية هنا لا في مراجعة لاحقة: ما يغادر الخادم لا يُستعاد.
    """

    def _scrub(value, depth=0):
        if depth > 6:
            return value
        if isinstance(value, dict):
            cleaned = {}
            for key, item in value.items():
                lowered = str(key).lower()
                if any(hint_word in lowered for hint_word in _SENTRY_SENSITIVE_HINTS):
                    cleaned[key] = "[Filtered]"
                else:
                    cleaned[key] = _scrub(item, depth + 1)
            return cleaned
        if isinstance(value, (list, tuple)):
            return type(value)(_scrub(item, depth + 1) for item in value)
        return value

    try:
        request = event.get("request")
        if isinstance(request, dict):
            request.pop("cookies", None)
            for field in ("data", "headers", "env"):
                if isinstance(request.get(field), dict):
                    request[field] = _scrub(request[field])
            # سلسلة الاستعلام تصل نصاً خاماً، فلا يمكن تنقيتها بالمفتاح.
            if request.get("query_string"):
                request["query_string"] = "[Filtered]"

        if isinstance(event.get("extra"), dict):
            event["extra"] = _scrub(event["extra"])

        # متغيّرات الإطارات المحلية — أخطر مصدر: تحمل ما مرّ بالدالة كاملاً.
        for exception in (event.get("exception") or {}).get("values") or []:
            for frame in (exception.get("stacktrace") or {}).get("frames") or []:
                if isinstance(frame.get("vars"), dict):
                    frame["vars"] = _scrub(frame["vars"])
    except Exception:
        # تنقيةٌ تعطّلت يجب ألا تُسقط تقرير الخطأ ولا الطلب. وإسقاط الحدث أأمن
        # من إرساله غير منقّى.
        return None
    return event


if SENTRY_DSN:
    import sentry_sdk

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        environment=ENV,
        release=SENTRY_RELEASE,
        traces_sample_rate=SENTRY_TRACES_SAMPLE_RATE,
        send_default_pii=False,
        before_send=_sentry_scrub,
    )


# ----------------- SECRET_KEY -----------------
SECRET_KEY = (os.getenv("SECRET_KEY") or "").strip()

if ENV == "production":
    if not SECRET_KEY or SECRET_KEY == "unsafe-secret":
        raise ImproperlyConfigured("SECRET_KEY must be set to a strong unique value in production.")
    if DEBUG:
        raise ImproperlyConfigured("DEBUG must be False in production.")
else:
    # للتطوير فقط
    if not SECRET_KEY:
        SECRET_KEY = "unsafe-secret"


# ----------------- Allowed Hosts / CSRF Trusted Origins -----------------
def _default_allowed_hosts() -> list[str]:
    hosts: list[str] = ["localhost", "127.0.0.1"]

    # Known deployed domains (backwards compatible)
    hosts += [
        "app.tawtheeq-ksa.com",
        "tawtheeq-ksa.com",
        "www.tawtheeq-ksa.com",
    ]

    # De-dupe
    seen = set()
    out: list[str] = []
    for h in hosts:
        if h and h not in seen:
            seen.add(h)
            out.append(h)
    return out


_allowed_hosts_env = (os.getenv("ALLOWED_HOSTS") or "").strip()
ALLOWED_HOSTS = _split_env_list(_allowed_hosts_env) if _allowed_hosts_env else _default_allowed_hosts()


def _default_csrf_trusted_origins() -> list[str]:
    origins: list[str] = []

    # Static known origins (backwards compatible)
    origins += [
        "https://app.tawtheeq-ksa.com",
        "https://tawtheeq-ksa.com",
        "https://www.tawtheeq-ksa.com",
    ]

    # Derive trusted origins from allowed hosts to reduce host-mismatch CSRF issues.
    for host in ALLOWED_HOSTS:
        h = (host or "").strip()
        if not h or h in {"*", "."}:
            continue
        if h.startswith("."):
            # Django CSRF trusted origins requires explicit origins, skip wildcard-like host.
            continue
        # Production-like domains: https
        origins.append(f"https://{h}")
        # Local/dev convenience
        if h in {"localhost", "127.0.0.1", "[::1]"}:
            origins.append(f"http://{h}")

    # De-dupe
    seen = set()
    out: list[str] = []
    for o in origins:
        if o and o not in seen:
            seen.add(o)
            out.append(o)
    return out


_csrf_env = (os.getenv("CSRF_TRUSTED_ORIGINS") or "").strip()
CSRF_TRUSTED_ORIGINS = _split_env_list(_csrf_env) if _csrf_env else _default_csrf_trusted_origins()
CSRF_FAILURE_VIEW = "core.views.csrf_failure"


# ----------------- Share Links (public, no-account) -----------------
try:
    SHARE_LINK_DEFAULT_DAYS = int(os.getenv("SHARE_LINK_DEFAULT_DAYS", "7").strip() or "7")
except Exception:
    SHARE_LINK_DEFAULT_DAYS = 7

# Public security contact exposed by /.well-known/security.txt.
SECURITY_CONTACT_EMAIL = (
    os.getenv("SECURITY_CONTACT_EMAIL") or "support@tawtheeq-ksa.com"
).strip()

# ----------------- Mansour public AI assistant -----------------
# The secret is server-side only. The widget remains visible without it and
# reports a safe temporary-unavailable message until production is configured.
OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
MANSOUR_ASSISTANT_ENABLED = _env_bool(
    "MANSOUR_ASSISTANT_ENABLED",
    bool(OPENAI_API_KEY),
)
# OpenAI retires the whole GPT-5 family on 11 Dec 2026. A server whose ``.env``
# still names ``gpt-5-mini`` would fail every AI request that morning, so a
# retired id is remapped here rather than trusted — changing the default alone
# would not reach a deployment that sets the variable explicitly, and ours do.
#
# The replacement OpenAI names for ``gpt-5-mini`` is ``gpt-5.6-terra``, at eight
# times its input price. ``gpt-5.6-luna`` is the honest swap: a newer generation
# than the model it replaces and cheaper than it ($0.20/$1.20 against
# $0.25/$2.00 per 1M tokens). Quality is bought with ``reasoning.effort``, which
# costs output tokens on the cheap tier, long before it is worth buying a tier.
DEFAULT_OPENAI_TEXT_MODEL = "gpt-5.6-luna"
RETIRED_OPENAI_TEXT_MODELS = {
    "gpt-5-nano": DEFAULT_OPENAI_TEXT_MODEL,
    "gpt-5-nano-2025-08-07": DEFAULT_OPENAI_TEXT_MODEL,
    "gpt-5-mini": DEFAULT_OPENAI_TEXT_MODEL,
    "gpt-5-mini-2025-08-07": DEFAULT_OPENAI_TEXT_MODEL,
    "gpt-5": "gpt-5.6-sol",
    "gpt-5-2025-08-07": "gpt-5.6-sol",
}


def _openai_text_model(value: str | None) -> str:
    """Resolve a configured text model, replacing ids OpenAI has retired."""
    name = (value or "").strip()
    if not name:
        return DEFAULT_OPENAI_TEXT_MODEL
    return RETIRED_OPENAI_TEXT_MODELS.get(name, name)


# GPT-5.6 dropped the GPT-5 ``minimal`` effort in favour of ``none`` and added
# ``xhigh``/``max`` above ``high``. An environment still carrying ``minimal``
# would be rejected by the API, so it is mapped instead of silently defaulted.
OPENAI_REASONING_EFFORTS = frozenset({"none", "low", "medium", "high", "xhigh", "max"})
RETIRED_OPENAI_REASONING_EFFORTS = {"minimal": "none"}

# The call sites that want the model to answer without deliberating: the report
# rewrite, the dictation polish, and Mansour's quality retry. All three edit
# text that is already written, so reasoning buys them nothing but latency.
AI_FAST_REASONING_EFFORT = "none"

_mansour_configured_model = os.getenv("MANSOUR_ASSISTANT_MODEL")
MANSOUR_ASSISTANT_QUALITY_MODE = _env_bool(
    "MANSOUR_ASSISTANT_QUALITY_MODE",
    True,
)
MANSOUR_ASSISTANT_MODEL = _openai_text_model(_mansour_configured_model)

_mansour_reasoning_effort = (
    os.getenv("MANSOUR_ASSISTANT_REASONING_EFFORT") or "low"
).strip().lower()
_mansour_reasoning_effort = RETIRED_OPENAI_REASONING_EFFORTS.get(
    _mansour_reasoning_effort, _mansour_reasoning_effort
)
if _mansour_reasoning_effort not in OPENAI_REASONING_EFFORTS:
    _mansour_reasoning_effort = "low"
MANSOUR_ASSISTANT_REASONING_EFFORT = _mansour_reasoning_effort

_mansour_text_verbosity = (
    os.getenv("MANSOUR_ASSISTANT_TEXT_VERBOSITY") or "medium"
).strip().lower()
if _mansour_text_verbosity not in {"low", "medium", "high"}:
    _mansour_text_verbosity = "medium"
MANSOUR_ASSISTANT_TEXT_VERBOSITY = _mansour_text_verbosity

try:
    MANSOUR_ASSISTANT_MAX_OUTPUT_TOKENS = max(
        700 if MANSOUR_ASSISTANT_QUALITY_MODE else 100,
        min(900, int(os.getenv("MANSOUR_ASSISTANT_MAX_OUTPUT_TOKENS", "700"))),
    )
except (TypeError, ValueError):
    MANSOUR_ASSISTANT_MAX_OUTPUT_TOKENS = 700

try:
    MANSOUR_ASSISTANT_TIMEOUT_SECONDS = max(
        5.0,
        min(30.0, float(os.getenv("MANSOUR_ASSISTANT_TIMEOUT_SECONDS", "20"))),
    )
except (TypeError, ValueError):
    MANSOUR_ASSISTANT_TIMEOUT_SECONDS = 20.0

# Platform-wide daily ceiling on paid assistant calls. The per-IP limit alone
# cannot bound the bill: the widget is public, so a viral launch or a
# distributed scraper simply arrives from many addresses. Set to 0 to disable
# the ceiling (not recommended in production).
try:
    MANSOUR_ASSISTANT_DAILY_GLOBAL_LIMIT = max(
        0,
        int(os.getenv("MANSOUR_ASSISTANT_DAILY_GLOBAL_LIMIT", "2000") or "2000"),
    )
except (TypeError, ValueError):
    MANSOUR_ASSISTANT_DAILY_GLOBAL_LIMIT = 2000

# ----------------- AI report writing assistant -----------------
REPORT_AI_ENABLED = _env_bool("REPORT_AI_ENABLED", bool(OPENAI_API_KEY))
REPORT_AI_MODEL = _openai_text_model(
    os.getenv("REPORT_AI_MODEL") or MANSOUR_ASSISTANT_MODEL
)

try:
    REPORT_AI_MAX_OUTPUT_TOKENS = max(
        200,
        min(1400, int(os.getenv("REPORT_AI_MAX_OUTPUT_TOKENS", "700"))),
    )
except (TypeError, ValueError):
    REPORT_AI_MAX_OUTPUT_TOKENS = 700

try:
    REPORT_AI_TIMEOUT_SECONDS = max(
        5.0,
        min(35.0, float(os.getenv("REPORT_AI_TIMEOUT_SECONDS", "25"))),
    )
except (TypeError, ValueError):
    REPORT_AI_TIMEOUT_SECONDS = 25.0


# ----------------- Voice dictation for reports -----------------
# التسجيل يصل في الذاكرة ولا يُكتب على القرص ولا يُخزَّن، ويُفرَّغ ثم يُنسى.
VOICE_REPORT_ENABLED = _env_bool("VOICE_REPORT_ENABLED", bool(OPENAI_API_KEY))
# ``gpt-4o-mini-transcribe`` يُغلق في 26 فبراير 2027، وبديله ``gpt-transcribe``.
# البديل يقبل ``languages`` و``keywords`` بدل ``language`` المفردة، فالمعرّف
# وحده لا يكفي — انظر ``reports/voice_report.py``.
DEFAULT_TRANSCRIPTION_MODEL = "gpt-transcribe"
RETIRED_TRANSCRIPTION_MODELS = {
    "whisper-1": DEFAULT_TRANSCRIPTION_MODEL,
    "gpt-4o-transcribe": DEFAULT_TRANSCRIPTION_MODEL,
    "gpt-4o-mini-transcribe": DEFAULT_TRANSCRIPTION_MODEL,
}
_voice_configured_model = (os.getenv("VOICE_REPORT_MODEL") or "").strip()
VOICE_REPORT_MODEL = RETIRED_TRANSCRIPTION_MODELS.get(
    _voice_configured_model,
    _voice_configured_model or DEFAULT_TRANSCRIPTION_MODEL,
)
# نموذج مرحلة الترقيم والتنسيق. يتبع نموذج تحسين التقارير ما لم يُضبط صراحةً.
VOICE_REPORT_POLISH_MODEL = _openai_text_model(
    os.getenv("VOICE_REPORT_POLISH_MODEL") or REPORT_AI_MODEL
)

# مصطلحات حرفية يمرّرها ``gpt-transcribe`` في ``keywords`` لرفع دقّة أسماء
# المنصّة والمصطلحات المدرسية. فارغة افتراضياً عن قصد: المصطلح المُلقَّن قد
# يظهر في التفريغ دون أن يُنطق، وهو ما سبق أن أفسد تقرير معلّم عبر ``prompt``.
# لا تُملأ إلا بعد قياسٍ على تسجيلات حقيقية. قائمة مفصولة بفواصل.
VOICE_REPORT_KEYWORDS = tuple(
    part.strip()
    for part in (os.getenv("VOICE_REPORT_KEYWORDS") or "").split(",")
    if part.strip()
)

# القيد الفعلي على الخادم هو الحجم؛ أما المدة فيفرضها المسجّل في المتصفّح
# بإيقافٍ تلقائي، لأن قياس مدة مقطع مضغوط خادمياً يحتاج فكّ ترميز كامل.
try:
    VOICE_REPORT_MAX_SECONDS = max(30, min(600, int(os.getenv("VOICE_REPORT_MAX_SECONDS", "180"))))
except (TypeError, ValueError):
    VOICE_REPORT_MAX_SECONDS = 180

try:
    VOICE_REPORT_MAX_BYTES = max(
        200_000,
        min(25 * 1024 * 1024, int(os.getenv("VOICE_REPORT_MAX_BYTES", str(10 * 1024 * 1024)))),
    )
except (TypeError, ValueError):
    VOICE_REPORT_MAX_BYTES = 10 * 1024 * 1024

try:
    VOICE_REPORT_DAILY_LIMIT = max(0, min(20, int(os.getenv("VOICE_REPORT_DAILY_LIMIT", "3"))))
except (TypeError, ValueError):
    VOICE_REPORT_DAILY_LIMIT = 3

# رفعُ صوتٍ ثم تفريغه أبطأ من نداء نصّي، فمهلته أوسع.
try:
    VOICE_REPORT_TIMEOUT_SECONDS = max(
        15.0,
        min(120.0, float(os.getenv("VOICE_REPORT_TIMEOUT_SECONDS", "60"))),
    )
except (TypeError, ValueError):
    VOICE_REPORT_TIMEOUT_SECONDS = 60.0

try:
    VOICE_REPORT_MAX_OUTPUT_TOKENS = max(
        300,
        min(2000, int(os.getenv("VOICE_REPORT_MAX_OUTPUT_TOKENS", "1200"))),
    )
except (TypeError, ValueError):
    VOICE_REPORT_MAX_OUTPUT_TOKENS = 1200

# قصرُ الميزة على التطبيق المثبَّت قرارٌ منتَجي لا حاجزٌ أمني: الترويسة التي
# يرسلها العميل يمكن تزويرها. الحدّ الذي يحمي التكلفة فعلاً هو الحصة اليومية
# المحسوبة على الخادم. اضبطه False لإتاحتها في المتصفّح العادي أيضاً.
VOICE_REPORT_PWA_ONLY = _env_bool("VOICE_REPORT_PWA_ONLY", True)


# ----------------- Notifications: Local fallback (no broker) -----------------
NOTIFICATIONS_LOCAL_FALLBACK_ENABLED = _env_bool("NOTIFICATIONS_LOCAL_FALLBACK_ENABLED", True)
NOTIFICATIONS_LOCAL_FALLBACK_THREAD = _env_bool("NOTIFICATIONS_LOCAL_FALLBACK_THREAD", True)

# ----------------- PWA Web Push -----------------
# Keep the private VAPID key in the runtime environment only.  The public key
# is intentionally exposed to authenticated browsers when they subscribe.
WEB_PUSH_VAPID_PRIVATE_KEY = (os.getenv("WEB_PUSH_VAPID_PRIVATE_KEY") or "").strip()
WEB_PUSH_VAPID_PUBLIC_KEY = (os.getenv("WEB_PUSH_VAPID_PUBLIC_KEY") or "").strip()
WEB_PUSH_SUBJECT = (
    os.getenv("WEB_PUSH_SUBJECT")
    or os.getenv("BUSINESS_SUPPORT_EMAIL")
    or "mailto:support@tawtheeq-ksa.com"
).strip()
if "@" in WEB_PUSH_SUBJECT and not WEB_PUSH_SUBJECT.startswith(("mailto:", "https://")):
    WEB_PUSH_SUBJECT = f"mailto:{WEB_PUSH_SUBJECT}"
WEB_PUSH_ENABLED = _env_bool(
    "WEB_PUSH_ENABLED",
    bool(WEB_PUSH_VAPID_PRIVATE_KEY and WEB_PUSH_VAPID_PUBLIC_KEY),
)
WEB_PUSH_ALLOWED_ENDPOINT_HOSTS = tuple(
    _split_env_list(
        os.getenv("WEB_PUSH_ALLOWED_ENDPOINT_HOSTS")
        or (
            "fcm.googleapis.com,push.services.mozilla.com,"
            "updates.push.services.mozilla.com,push.apple.com,"
            "notify.windows.com,push.microsoft.com"
        )
    )
)
try:
    WEB_PUSH_TIMEOUT_SECONDS = max(
        2.0,
        min(30.0, float(os.getenv("WEB_PUSH_TIMEOUT_SECONDS", "10") or "10")),
    )
except (TypeError, ValueError):
    WEB_PUSH_TIMEOUT_SECONDS = 10.0

if WEB_PUSH_ENABLED and not (WEB_PUSH_VAPID_PRIVATE_KEY and WEB_PUSH_VAPID_PUBLIC_KEY):
    raise ImproperlyConfigured(
        "WEB_PUSH_ENABLED requires WEB_PUSH_VAPID_PRIVATE_KEY and WEB_PUSH_VAPID_PUBLIC_KEY."
    )

try:
    NOTIFICATIONS_LOCAL_FALLBACK_MAX_RECIPIENTS = int(
        (os.getenv("NOTIFICATIONS_LOCAL_FALLBACK_MAX_RECIPIENTS", "30") or "30").strip()
    )
except Exception:
    NOTIFICATIONS_LOCAL_FALLBACK_MAX_RECIPIENTS = 30

try:
    NOTIFICATIONS_LOCAL_FALLBACK_HARD_STOP_RECIPIENTS = int(
        (os.getenv("NOTIFICATIONS_LOCAL_FALLBACK_HARD_STOP_RECIPIENTS", "200") or "200").strip()
    )
except Exception:
    NOTIFICATIONS_LOCAL_FALLBACK_HARD_STOP_RECIPIENTS = 200

try:
    NOTIFICATIONS_LOCAL_FALLBACK_WARN_SECONDS = float(
        (os.getenv("NOTIFICATIONS_LOCAL_FALLBACK_WARN_SECONDS", "2") or "2").strip()
    )
except Exception:
    NOTIFICATIONS_LOCAL_FALLBACK_WARN_SECONDS = 2.0

try:
    NOTIFICATIONS_DISPATCH_LOCK_TTL_SECONDS = int(
        (os.getenv("NOTIFICATIONS_DISPATCH_LOCK_TTL_SECONDS", "3600") or "3600").strip()
    )
except Exception:
    NOTIFICATIONS_DISPATCH_LOCK_TTL_SECONDS = 3600


# ----------------- Telegram operational alerts -----------------
TELEGRAM_ALERTS_ENABLED = _env_bool("TELEGRAM_ALERTS_ENABLED", False)
TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
TELEGRAM_ALERT_CHAT_ID = (os.getenv("TELEGRAM_ALERT_CHAT_ID") or "").strip()
TELEGRAM_ALERT_CATEGORIES = set(
    _split_env_list(
        os.getenv(
            "TELEGRAM_ALERT_CATEGORIES",
            "support,subscriptions,registration,payments,complaints",
        )
    )
)
try:
    TELEGRAM_ALERT_TIMEOUT_SECONDS = float(
        (os.getenv("TELEGRAM_ALERT_TIMEOUT_SECONDS", "10") or "10").strip()
    )
except Exception:
    TELEGRAM_ALERT_TIMEOUT_SECONDS = 10.0
try:
    TELEGRAM_ALERT_DEDUP_TTL_SECONDS = int(
        (os.getenv("TELEGRAM_ALERT_DEDUP_TTL_SECONDS", "2592000") or "2592000").strip()
    )
except Exception:
    TELEGRAM_ALERT_DEDUP_TTL_SECONDS = 2_592_000


# ----------------- Short-TTL DB Load Shedding -----------------
try:
    NAV_CONTEXT_CACHE_TTL_SECONDS = int(os.getenv("NAV_CONTEXT_CACHE_TTL_SECONDS", "20").strip() or "20")
except Exception:
    NAV_CONTEXT_CACHE_TTL_SECONDS = 20

try:
    UNREAD_COUNT_CACHE_TTL_SECONDS = int(os.getenv("UNREAD_COUNT_CACHE_TTL_SECONDS", "15").strip() or "15")
except Exception:
    UNREAD_COUNT_CACHE_TTL_SECONDS = 15

# School dashboard aggregate queries are hot and change much less often than
# they are read. Keep the fresh TTL inside the agreed 30–60 second window; a
# stale copy is used only while one worker owns the Redis rebuild lock.
try:
    SCHOOL_DASHBOARD_CACHE_TTL_SECONDS = max(
        30, min(60, int(os.getenv("SCHOOL_DASHBOARD_CACHE_TTL_SECONDS", "45") or "45"))
    )
except (TypeError, ValueError):
    SCHOOL_DASHBOARD_CACHE_TTL_SECONDS = 45
SCHOOL_DASHBOARD_STALE_TTL_SECONDS = int(os.getenv("SCHOOL_DASHBOARD_STALE_TTL_SECONDS", "300") or "300")
SCHOOL_DASHBOARD_LOCK_TTL_SECONDS = int(os.getenv("SCHOOL_DASHBOARD_LOCK_TTL_SECONDS", "15") or "15")
SCHOOL_DASHBOARD_LOCK_WAIT_SECONDS = float(os.getenv("SCHOOL_DASHBOARD_LOCK_WAIT_SECONDS", "1.5") or "1.5")


# ----------------- Applications -----------------
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.humanize",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third-party
    "channels",
    "django_celery_results",
    "rest_framework",
    # Our apps
    "core",
    "reports",
    "maintenance",
    "operations",
]


# ----------------- Storage Backend (optional R2) -----------------
R2_ACCESS_KEY_ID = (os.getenv("R2_ACCESS_KEY_ID") or os.getenv("R2_ACCESS_KEY") or "").strip()
R2_SECRET_ACCESS_KEY = (os.getenv("R2_SECRET_ACCESS_KEY") or os.getenv("R2_SECRET_KEY") or "").strip()
R2_BUCKET_NAME = (os.getenv("R2_BUCKET_NAME") or "").strip()
R2_ENDPOINT_URL = (
    os.getenv("R2_ENDPOINT_URL")
    or os.getenv("R2_ENDPOINT")
    or os.getenv("Default_Endpoint")
    or ""
).strip()

_r2_effective_bucket = R2_BUCKET_NAME
_r2_effective_endpoint = R2_ENDPOINT_URL
if R2_ENDPOINT_URL:
    try:
        parts = urlsplit(R2_ENDPOINT_URL)
        path = (parts.path or "").strip("/")
        if path:
            bucket_from_path = path.split("/", 1)[0]
            if not _r2_effective_bucket:
                _r2_effective_bucket = bucket_from_path
            _r2_effective_endpoint = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
    except Exception:
        pass

R2_BUCKET_NAME = _r2_effective_bucket
R2_ENDPOINT_URL = _r2_effective_endpoint

_use_r2 = bool(R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY and R2_BUCKET_NAME and R2_ENDPOINT_URL)
if _use_r2 and "storages" not in INSTALLED_APPS:
    INSTALLED_APPS.append("storages")

if ENV == "production" and PRODUCTION_STRICT_MODE and not _use_r2:
    raise ImproperlyConfigured(
        "Private S3/R2 media storage is required in production. Local container media is not durable."
    )


# ----------------- Load shedding -----------------
# Ceiling on simultaneously-processed requests per web process. Django's ASGI
# path gives every in-flight request its own thread *and* its own database
# connection, with no built-in cap, so a traffic spike can exhaust PostgreSQL's
# max_connections (default 100) and take the whole platform down — Celery
# included. Keep this comfortably below:
#     max_connections - (celery workers + beat + admin headroom)
# Set to 0 to disable shedding entirely.
#
# When MAX_CONCURRENT_REQUESTS is not set explicitly, it is derived from the
# database budget so the ceiling stays correct after someone scales
# WEB_CONCURRENCY without revisiting this file:
#
#   (DB_MAX_CONNECTIONS - DB_RESERVED_CONNECTIONS) / WEB_CONCURRENCY
#
# DB_RESERVED_CONNECTIONS covers the Celery workers, beat, and a superuser slot
# kept free for an operator to connect during an incident.
def _derive_max_concurrent_requests() -> int:
    explicit = (os.getenv("MAX_CONCURRENT_REQUESTS") or "").strip()
    if explicit:
        try:
            return max(0, int(explicit))
        except ValueError:
            pass
    try:
        db_max = int(os.getenv("DB_MAX_CONNECTIONS", "100") or "100")
        reserved = int(os.getenv("DB_RESERVED_CONNECTIONS", "15") or "15")
        workers = max(1, int(os.getenv("WEB_CONCURRENCY", "1") or "1"))
    except ValueError:
        return 50
    budget = (db_max - reserved) // workers
    # Never so low that ordinary traffic is shed, never so high that the budget
    # is meaningless.
    return max(10, min(200, budget))


MAX_CONCURRENT_REQUESTS = _derive_max_concurrent_requests()

try:
    OVERLOAD_RETRY_AFTER_SECONDS = max(1, int(os.getenv("OVERLOAD_RETRY_AFTER_SECONDS", "5") or "5"))
except (TypeError, ValueError):
    OVERLOAD_RETRY_AFTER_SECONDS = 5

# Aggregate tenant budget. This complements per-user/IP throttles and prevents
# one large school from consuming the whole database/concurrency allowance.
SCHOOL_RATE_LIMIT_ENABLED = _env_bool("SCHOOL_RATE_LIMIT_ENABLED", True)
try:
    SCHOOL_RATE_LIMIT_REQUESTS = max(
        60, int(os.getenv("SCHOOL_RATE_LIMIT_REQUESTS", "900") or "900")
    )
except (TypeError, ValueError):
    SCHOOL_RATE_LIMIT_REQUESTS = 900
try:
    SCHOOL_RATE_LIMIT_WINDOW_SECONDS = max(
        10, int(os.getenv("SCHOOL_RATE_LIMIT_WINDOW_SECONDS", "60") or "60")
    )
except (TypeError, ValueError):
    SCHOOL_RATE_LIMIT_WINDOW_SECONDS = 60


# ----------------- Middleware -----------------
MIDDLEWARE = [
    "core.middleware.RequestTraceMiddleware",
    # Shed load before anything touches the session store or the database.
    "core.middleware.ConcurrencyLimitMiddleware",
    "core.middleware.BlockBadPathsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "reports.middleware.CanonicalHostMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # MessageMiddleware يسبق EnforceSingleSession عمداً: الأخير يضيف رسالة
    # «سُجّل الخروج لأن الحساب دخل من جهاز آخر»، و``messages.add_message`` يحتاج
    # ``request._messages`` الذي ينشئه MessageMiddleware في مرحلة الطلب. وبالترتيب
    # المعكوس كان النداء يرمي ``MessageFailure`` فيبتلعه ``except`` ولا تصل
    # الرسالة أبداً — يخرج المستخدم بلا تفسير ويظنه عطلاً.
    "django.contrib.messages.middleware.MessageMiddleware",
    "reports.middleware_single_session.EnforceSingleSessionMiddleware",
    "reports.middleware.AuditLogMiddleware",
    "reports.middleware.MaintenanceModeMiddleware",
    "reports.middleware.SearchEngineIndexingMiddleware",
    "reports.middleware.IdleLogoutMiddleware",
    "reports.middleware.ActiveSchoolGuardMiddleware",
    # ActiveSchoolGuard has already authorised and attached request.active_school,
    # so the tenant limiter adds no database query.
    "core.middleware.SchoolRateLimitMiddleware",
    "reports.middleware.SubscriptionMiddleware",
    "reports.middleware.ForcePasswordChangeMiddleware",
    "reports.middleware.ContentSecurityPolicyMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]


# ----------------- URLs / Templates -----------------
ROOT_URLCONF = "config.urls"

# ✅ تم إصلاح سبب الخطأ: حذف المفتاح الغلط (" reed")
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "reports.context_processors.nav_context",
                "reports.ai_features.ai_feature_flags",
                "reports.context_processors.csp",
                "reports.context_processors.seo",
                "reports.context_processors.payment_gateways",
            ],
        },
    },
]


# ----------------- WSGI/ASGI -----------------
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"


# ----------------- Redis URLs (Broker/Cache/Channels) -----------------
REDIS_URL = os.getenv("REDIS_URL", "").strip()

# Celery broker: يفضل نفس REDIS_URL إن ما عندك غيره
CELERY_BROKER_URL = (os.getenv("CELERY_BROKER_URL") or REDIS_URL).strip()

# Cache Redis URL: لو ما انكتب، نشتقه من broker بتغيير DB index
REDIS_CACHE_URL = os.getenv("REDIS_CACHE_URL", "").strip()
REDIS_CHANNEL_LAYER_URL = (os.getenv("REDIS_CHANNEL_LAYER_URL") or "").strip() or REDIS_URL

# Database URL is needed by strict production checks below.
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()


def _derive_cache_redis_url(broker_url: str) -> str:
    if not broker_url:
        return ""
    try:
        parts = urlsplit(broker_url)
        path = (parts.path or "/0").strip()
        path_num = path[1:] if path.startswith("/") else path
        if path_num.isdigit():
            db = int(path_num)
            new_path = "/1" if db == 0 else f"/{db + 1}"
            return urlunsplit((parts.scheme, parts.netloc, new_path, parts.query, parts.fragment))
        return broker_url
    except Exception:
        return broker_url


if not REDIS_CACHE_URL:
    REDIS_CACHE_URL = _derive_cache_redis_url(CELERY_BROKER_URL)

if ENV == "production" and PRODUCTION_STRICT_MODE:
    if not DATABASE_URL:
        raise ImproperlyConfigured("DATABASE_URL is required in production when PRODUCTION_STRICT_MODE is enabled.")
    if not REDIS_URL:
        raise ImproperlyConfigured("REDIS_URL is required in production when PRODUCTION_STRICT_MODE is enabled.")


# django-ratelimit normally reads REMOTE_ADDR, which is the reverse proxy in
# this deployment. Resolve X-Real-IP only when the direct peer is trusted.
TRUSTED_PROXY_CIDRS = _split_env_list(
    os.getenv(
        "TRUSTED_PROXY_CIDRS",
        "127.0.0.1/32,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16",
    )
)
RATELIMIT_IP_META_KEY = "core.client_ip.client_ip_for_ratelimit"


# ----------------- Caching -----------------
# ── Redis scaling notes ─────────────────────────────────────────
# Current: single Redis instance — DB 0 (broker + channels), DB 1 (cache).
# Adequate up to ~500 schools.  Split thresholds:
#   - Cache memory > 200 MB  → separate REDIS_CACHE_URL instance
#   - Celery queue depth > 1000 for >5 min → separate broker instance
#   - WS connections > 5K concurrent → separate channels Redis
# Monitor: redis INFO memory, connected_clients, used_memory_rss.
#
# ── Key prefix map (Phase 6E) ──────────────────────────────────
# Prefix       DB   Purpose
# sr:*         1    Django cache (views, opmetrics, sessions, locks)
# celery*      0    Celery broker (task queues + results)
# asgi:*       0    Channels layer (WebSocket groups)
# opmetrics:*  1    Operational counters (via cache backend)
# To split: set separate REDIS_CACHE_URL / CELERY_BROKER_URL / channel hosts.
# ────────────────────────────────────────────────────────────────
if REDIS_CACHE_URL:
    CACHES = {
        "default": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": REDIS_CACHE_URL,
            "KEY_PREFIX": "sr",
            "TIMEOUT": 300,
            "OPTIONS": {
                "CLIENT_CLASS": "django_redis.client.DefaultClient",
                "IGNORE_EXCEPTIONS": True,
            },
        }
    }
else:
    if ENV == "production" and PRODUCTION_STRICT_MODE:
        raise ImproperlyConfigured("REDIS_CACHE_URL is required in production when PRODUCTION_STRICT_MODE is enabled.")
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "school_reports_locmem",
        }
    }


# ----------------- Rate-limit / throttle store -----------------
# ── لماذا مخزن منفصل عن كاش العرض ───────────────────────────────
# عدّادات الحدود ليست كاشاً. الكاش يُعاد بناؤه عند الضياع، أما العدّاد الضائع
# فيُقرأ «صفر محاولات» — أي أن ضياعه **يُلغي الحماية** بدل أن يُبطئها.
#
# وكاش العرض يعمل بـ ``volatile-lru`` و``IGNORE_EXCEPTIONS: True`` عن قصد:
# صفحةٌ بطيئة أهون من صفحة معطّلة. لكن السلوكين معاً قاتلان للعدّادات — الإخلاء
# يمسحها صامتاً تحت الضغط، وابتلاعُ الاستثناء يخفي أن ذلك حدث. والنتيجة أن حدود
# الدخول وميزانيات المستأجر وسقف فاتورة الذكاء الاصطناعي تختفي **في لحظة
# الذروة تحديداً**، وهي اللحظة التي وُجدت لأجلها.
#
# فمخزن الحدود ``noeviction`` ولا يبتلع استثناءً. وحجمه صغير بطبعه: عدّاد لكل
# معرِّف/عنوان بنافذة دقائق، فـ96MB سقفٌ واسع.
#
# والسقوط إلى ``default`` مقصود: بيئة لم تُفصل بعد تظل عاملة، والفصل ترقية
# تشغيلية لا شرط تشغيل. أما ``LOGIN_THROTTLE_FAIL_CLOSED`` فيفترض هذا الفصل —
# راجع ``reports.views.auth._login_account_locked``.
REDIS_LIMITS_URL = os.getenv("REDIS_LIMITS_URL", "").strip()
if REDIS_LIMITS_URL:
    CACHES["limits"] = {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": REDIS_LIMITS_URL,
        "KEY_PREFIX": "lim",
        "TIMEOUT": 900,
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
            # لا IGNORE_EXCEPTIONS هنا عمداً: المخزن يجب أن يرفع الاستثناء حين
            # يتعثّر ليقرأه المتصل ويفشل مغلقاً، لا أن يعيد None فيُقرأ «صفر».
        },
    }
    RATELIMIT_USE_CACHE = "limits"

# تعذُّر التحقق من عدّاد الدخول يُعامل كـ«مقفل» — راجع التعليل الكامل في
# ``reports.views.auth._login_account_locked``.
#
# **والافتراض مشتقّ لا ثابت.** الفشل المغلق يفترض مخزناً موثوقاً: تشغيله على
# الكاش المشترك — وهو ``volatile-lru`` — يحوّل إخلاءَ مفتاحٍ روتينياً إلى منعِ
# دخولٍ للجميع. وثابتُ ``True`` كان يعني أن كل بيئة لم تُضف فيها
# ``REDIS_LIMITS_URL`` بعد تبدأ من الحالة الخطرة، ويصير الأمان رهن ترتيب
# خطوتين في نشرٍ يدوي — وهو ترتيبٌ يُنسى مرة واحدة فيقفل المنصة.
#
# فالربط هنا يجعل الحالتين صحيحتين بذاتهما: بلا مخزن مستقل يبقى السلوك كما كان
# (مفتوح، وحدُّ الـ IP قائم)، وبإضافته يشتدّ تلقائياً بلا خطوة ثانية يتذكرها
# أحد. والتجاوز الصريح متاح للحالتين.
LOGIN_THROTTLE_FAIL_CLOSED = _env_bool("LOGIN_THROTTLE_FAIL_CLOSED", bool(REDIS_LIMITS_URL))


# ----------------- Channels Layer -----------------
if REDIS_CHANNEL_LAYER_URL:
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels_redis.core.RedisChannelLayer",
            "CONFIG": {"hosts": [REDIS_CHANNEL_LAYER_URL]},
        }
    }
else:
    if ENV == "production" and PRODUCTION_STRICT_MODE:
        raise ImproperlyConfigured("REDIS_CHANNEL_LAYER_URL is required in production when PRODUCTION_STRICT_MODE is enabled.")
    # NOTE: InMemory مناسب للتجربة فقط (لا يصلح لعدة نسخ/سيرفرات)
    CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}


# ----------------- Database -----------------
# ── Scaling notes ───────────────────────────────────────────────
# Current: single PostgreSQL, CONN_MAX_AGE=0 (see the reasoning below).
# Peak connections per web process are bounded by MAX_CONCURRENT_REQUESTS,
# because ASGI gives each in-flight request its own thread and connection.
# At 500+ schools: consider PgBouncer or another managed connection pooling layer.
# At 1000+ schools: evaluate read replica for nav_context / dashboard queries.
# Hot tables: NotificationRecipient, AuditLog, Report (see docs/PHASE5 report).
#
# ── PgBouncer readiness (Phase 6D) ─────────────────────────────
# Recommended PgBouncer settings when adding a connection pooler:
#   pool_mode = transaction          (required: Django uses SET/RESET per query)
#   default_pool_size = 20           (per-user pool, tune to DB max_connections)
#   max_client_conn = 200            (allow all workers + web + beat to connect)
#   server_idle_timeout = 300        (match CONN_MAX_AGE / 2)
#   server_lifetime = 3600
# Django side:
#   - Set CONN_MAX_AGE=0 (let PgBouncer manage pooling, not Django)
#   - Or keep CONN_MAX_AGE=600 if using session mode (not recommended)
#   - Point DATABASE_URL to PgBouncer host:port instead of Postgres directly
# Rollback: revert DATABASE_URL to direct Postgres endpoint, restore CONN_MAX_AGE
# ────────────────────────────────────────────────────────────────
DB_SSL = _env_bool("DB_SSL", False)

# الحد الأقصى لعمر الاتصال (ثوانٍ). 0 يعني إغلاق الاتصال بعد كل طلب.
#
# ── Why 0 and not a persistent connection under ASGI ────────────
# Persistent connections only pay off when a later request reuses the same
# connection. That cannot happen here: Django's ASGI handler opens a
# ThreadSensitiveContext per request and asgiref allocates a fresh
# ThreadPoolExecutor(max_workers=1) for each one, while Django's connection
# registry is thread-local. Every request therefore starts on a brand-new
# thread with no connection to reuse and dials PostgreSQL anyway.
#
# A non-zero value does change one thing, for the worse: the connection is
# marked "keep until close_at", so it is *not* closed when the request ends.
# The thread then dies and the connection lingers until garbage collection —
# which is exactly how a traffic burst exhausts max_connections.
#
# 0 closes the connection deterministically at the end of each request. Same
# number of connects, bounded connection count. Reintroduce a non-zero value
# only behind PgBouncer in transaction mode (see the PgBouncer notes above),
# where the pooler — not Django — owns connection reuse.
_CONN_MAX_AGE = int(os.getenv("CONN_MAX_AGE", "0"))

if DATABASE_URL and dj_database_url:
    DATABASES = {
        "default": dj_database_url.parse(
            DATABASE_URL,
            conn_max_age=_CONN_MAX_AGE,
            ssl_require=DB_SSL,
        )
    }
else:
    if ENV == "production" and PRODUCTION_STRICT_MODE:
        raise ImproperlyConfigured("DATABASE_URL must be configured in production; SQLite fallback is disabled.")
    DB_ENGINE = os.getenv("DB_ENGINE", "django.db.backends.sqlite3").strip()
    DB_NAME = os.getenv("DB_NAME", "").strip()
    DB_USER = os.getenv("DB_USER", "").strip()
    DB_PASS = os.getenv("DB_PASSWORD", "").strip()
    DB_HOST = os.getenv("DB_HOST", "").strip()
    DB_PORT = os.getenv("DB_PORT", "5432").strip()

    if "sqlite" in DB_ENGINE.lower() or not (DB_NAME and DB_ENGINE and (DB_HOST or "sqlite" in DB_ENGINE.lower())):
        DATABASES = {
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": os.getenv("DB_NAME", BASE_DIR / "db.sqlite3"),
            }
        }
    else:
        engine = DB_ENGINE
        if DB_ENGINE.startswith("postgres") or DB_ENGINE.endswith("postgresql"):
            engine = "django.db.backends.postgresql"
        DATABASES = {
            "default": {
                "ENGINE": engine,
                "NAME": DB_NAME,
                "USER": DB_USER,
                "PASSWORD": DB_PASS,
                "HOST": DB_HOST,
                "PORT": DB_PORT,
                "CONN_MAX_AGE": _CONN_MAX_AGE,
                "OPTIONS": {"sslmode": "require"} if DB_SSL and "postgresql" in engine else {},
            }
        }


# خلف Proxy في الإنتاج: حافظ على HTTPS + اسم المضيف الأصلي
if ENV == "production":
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    USE_X_FORWARDED_HOST = True
else:
    SECURE_PROXY_SSL_HEADER = None
    USE_X_FORWARDED_HOST = False


# ----------------- Password Validators -----------------
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# ----------------- I18N / TZ -----------------
# ── اللغة ───────────────────────────────────────────────────────────────
# المنصة عربية، والعربية وحدها: نصوص القوالب والنماذج مكتوبة بالعربية مباشرة،
# ولا كتالوج ترجمة ولا مبدّل لغة ولا تفاوض عليها. ومع غياب ``LocaleMiddleware``
# تبقى اللغة النشطة ``LANGUAGE_CODE`` في كل طلب.
#
# و``USE_I18N`` يبقى مفعّلاً — لا لترجمة نصوصنا، بل لأن جانغو يترجم نصوصه هو:
# رسائل تحقّق النماذج ولوحة الإدارة، وله كتالوج عربي جاهز. وإطفاؤه يُرجعها
# إنجليزية، أي أن تعطيل الترجمة يُنجليز ما كان عربياً.
LANGUAGE_CODE = "ar"
TIME_ZONE = "Asia/Riyadh"
USE_I18N = True
USE_TZ = True


# ----------------- Celery -----------------
# ── Worker scaling notes ────────────────────────────────────────
# Current: 1 worker, concurrency=4, all queues on one process.
# Scaling path:
#   100 schools  → current config is fine
#   500 schools  → separate worker for 'images' queue (CPU-bound)
#   1000 schools → separate workers per queue; fan-out daily summary
# Monitor: celery inspect active, flower, or ops/metrics endpoint.
# ────────────────────────────────────────────────────────────────
CELERY_RESULT_BACKEND = (os.getenv("CELERY_RESULT_BACKEND") or REDIS_CACHE_URL or CELERY_BROKER_URL or "django-db").strip()
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_TRACK_STARTED = False
CELERY_TASK_TIME_LIMIT = 30 * 60  # 30 minutes
CELERY_WORKER_PREFETCH_MULTIPLIER = int(os.getenv("CELERY_PREFETCH_MULTIPLIER", "1"))
CELERY_TASK_ACKS_LATE = _env_bool("CELERY_TASK_ACKS_LATE", True)
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_RESULT_EXPIRES = int(os.getenv("CELERY_RESULT_EXPIRES", "3600"))  # 1 hour

# ── Queue routing ───────────────────────────────────────────────────
# Logical queue separation so image processing cannot starve notifications.
# All queues resolve to the same broker; to run dedicated workers per queue,
# start additional workers with  -Q notifications  or  -Q images  etc.
# The default worker (no -Q flag) consumes ALL queues, so this is backward-
# compatible and requires zero deployment changes.
from kombu import Queue  # noqa: E402

CELERY_TASK_QUEUES = [
    Queue("default"),
    Queue("notifications"),
    Queue("images"),
    Queue("periodic"),
]
CELERY_TASK_DEFAULT_QUEUE = "default"
CELERY_TASK_ROUTES = {
    "reports.tasks.send_telegram_alert_task": {"queue": "notifications"},
    "reports.tasks.send_notification_task": {"queue": "notifications"},
    "reports.tasks.send_password_change_email_task": {"queue": "notifications"},
    "reports.tasks.send_subscription_activation_email_task": {"queue": "notifications"},
    "reports.tasks.process_report_images": {"queue": "images"},
    "reports.tasks.process_ticket_image": {"queue": "images"},
    # توليد PDF: أثقل عملية في المنصة، ومكانها عامل الوسائط لا عامل الويب.
    "reports.tasks.render_achievement_pdf_task": {"queue": "images"},
    "reports.tasks.render_group_report_pdf_task": {"queue": "images"},
    "reports.tasks.render_leadership_pdf_task": {"queue": "images"},
    "reports.tasks.render_user_guide_pdf_task": {"queue": "images"},
    "reports.tasks.build_generated_export_task": {"queue": "images"},
    "reports.tasks.send_daily_manager_summary_task": {"queue": "periodic"},
    "reports.tasks._daily_summary_for_school": {"queue": "periodic"},
    "reports.tasks.check_subscription_expiry_task": {"queue": "periodic"},
    "reports.tasks.check_archive_addon_expiry_task": {"queue": "periodic"},
    "reports.tasks.check_storage_thresholds_task": {"queue": "periodic"},
    "reports.tasks.reconcile_pending_gateway_payments_task": {"queue": "periodic"},
    "reports.tasks.remind_unsigned_circulars_task": {"queue": "periodic"},
    "reports.tasks.cleanup_audit_logs_task": {"queue": "periodic"},
    "reports.tasks.cleanup_expired_sessions_task": {"queue": "periodic"},
    "reports.tasks.monitor_infrastructure_capacity_task": {"queue": "periodic"},
    "operations.tasks.run_operations_monitor_task": {"queue": "periodic"},
    "operations.tasks.store_capacity_snapshot_task": {"queue": "periodic"},
    "operations.tasks.sync_deployed_revisions_task": {"queue": "periodic"},
    "operations.tasks.monitor_deployment_state_task": {"queue": "periodic"},
    "operations.tasks.send_incident_push_task": {"queue": "notifications"},
    "operations.tasks.cleanup_operations_history_task": {"queue": "periodic"},
    "reports.tasks.cleanup_generated_exports_task": {"queue": "periodic"},
    # Core is deliberately independent from the media worker it may need to rescue.
    "reports.tasks.recover_stale_generated_exports_task": {"queue": "default"},
    "reports.tasks.cleanup_platform_email_task": {"queue": "periodic"},
}

# Heavy ZIPs are durable background jobs; PDFs render in the media worker and
# return through a short-lived Redis key. Both keep CPU work out of Gunicorn.
HEAVY_EXPORT_ASYNC_ENABLED = _env_bool(
    "HEAVY_EXPORT_ASYNC_ENABLED", ENV == "production"
)
PDF_OFFLOAD_ENABLED = _env_bool("PDF_OFFLOAD_ENABLED", True)
PDF_OFFLOAD_TIMEOUT_SECONDS = float(os.getenv("PDF_OFFLOAD_TIMEOUT_SECONDS", "45") or "45")
GENERATED_EXPORT_RETENTION_HOURS = max(
    1, int(os.getenv("GENERATED_EXPORT_RETENTION_HOURS", "6") or "6")
)
GENERATED_EXPORT_QUEUE_STALE_SECONDS = max(
    30, int(os.getenv("GENERATED_EXPORT_QUEUE_STALE_SECONDS", "120") or "120")
)
GENERATED_EXPORT_RUNNING_STALE_SECONDS = max(
    300,
    int(os.getenv("GENERATED_EXPORT_RUNNING_STALE_SECONDS", "2100") or "2100"),
)
GENERATED_EXPORT_RECOVERY_RETRY_SECONDS = max(
    60, int(os.getenv("GENERATED_EXPORT_RECOVERY_RETRY_SECONDS", "600") or "600")
)
GENERATED_EXPORT_RECOVERY_MAX_ATTEMPTS = max(
    1, int(os.getenv("GENERATED_EXPORT_RECOVERY_MAX_ATTEMPTS", "3") or "3")
)


# ----------------- Audit Logs Retention -----------------
# سنة لا شهر: نزاعٌ تجاري أو تحقيقٌ في حادثة أمنية نادراً ما يُكتشف خلال
# ثلاثين يوماً، ومن يمسح أثره اليوم كان يكفيه انتظار شهر. راقب حجم
# ``reports_auditlog`` بعد التمديد، وأرشِف ما تجاوز 90 يوماً إلى R2 إن نما
# أسرع من المتوقع.
AUDIT_LOG_RETENTION_DAYS = int(os.getenv("AUDIT_LOG_RETENTION_DAYS", "365"))
AUDIT_LOG_CLEANUP_ENABLED = _env_bool("AUDIT_LOG_CLEANUP_ENABLED", True)


# ----------------- Expired Session Cleanup -----------------
# Django does not prune django_session by itself. Public traffic keeps adding
# rows, so the table must be swept on a schedule or it grows without bound.
SESSION_CLEANUP_ENABLED = _env_bool("SESSION_CLEANUP_ENABLED", True)


# ----------------- Infrastructure capacity watch -----------------
# One Redis carries the cache, the sessions and the Celery queues. Eviction
# under `volatile-lru` is silent — it surfaces as users being logged out and
# rate limits resetting — so the memory ratio has to be watched, not discovered.
INFRA_CAPACITY_MONITOR_ENABLED = _env_bool("INFRA_CAPACITY_MONITOR_ENABLED", True)
try:
    REDIS_MEMORY_ALERT_PERCENT = max(
        10, min(99, int(os.getenv("REDIS_MEMORY_ALERT_PERCENT", "80") or "80"))
    )
except (TypeError, ValueError):
    REDIS_MEMORY_ALERT_PERCENT = 80
try:
    EXPIRED_SESSION_ALERT_THRESHOLD = max(
        1000, int(os.getenv("EXPIRED_SESSION_ALERT_THRESHOLD", "100000") or "100000")
    )
except (TypeError, ValueError):
    EXPIRED_SESSION_ALERT_THRESHOLD = 100_000
CPU_ALERT_PERCENT = int(os.getenv("CPU_ALERT_PERCENT", "85") or "85")
MEMORY_ALERT_PERCENT = int(os.getenv("MEMORY_ALERT_PERCENT", "85") or "85")
DISK_ALERT_PERCENT = int(os.getenv("DISK_ALERT_PERCENT", "80") or "80")
CELERY_QUEUE_ALERT_LENGTH = int(os.getenv("CELERY_QUEUE_ALERT_LENGTH", "200") or "200")
HTTP_ALERT_MIN_SAMPLES = int(os.getenv("HTTP_ALERT_MIN_SAMPLES", "20") or "20")
HTTP_5XX_ALERT_PERCENT = float(os.getenv("HTTP_5XX_ALERT_PERCENT", "2.0") or "2.0")
HTTP_LATENCY_ALERT_MS = int(os.getenv("HTTP_LATENCY_ALERT_MS", "2000") or "2000")
OPERATIONS_CAPACITY_SUSTAINED_SAMPLES = int(os.getenv("OPERATIONS_CAPACITY_SUSTAINED_SAMPLES", "3") or "3")


# ----------------- Landing page pricing cache -----------------
# `/` is deliberately no-store so platform toggles apply at once, which means it
# renders in full for every campaign visitor. Its pricing model is derived only
# from the active plans, so it is cached and invalidated on plan changes rather
# than recomputed per visit.
try:
    LANDING_PRICING_CACHE_TTL_SECONDS = max(
        0, int(os.getenv("LANDING_PRICING_CACHE_TTL_SECONDS", "60") or "60")
    )
except (TypeError, ValueError):
    LANDING_PRICING_CACHE_TTL_SECONDS = 60


# ----------------- Daily Manager Report -----------------
DAILY_MANAGER_REPORT_ENABLED = _env_bool("DAILY_MANAGER_REPORT_ENABLED", True)
# The weekly summary is an in-app notification only — it is never emailed and
# has no outbound webhook channel, so this is the only delivery switch.
# See reports/tasks.py::_daily_summary_for_school.
DAILY_MANAGER_REPORT_INAPP_ENABLED = _env_bool("DAILY_MANAGER_REPORT_INAPP_ENABLED", True)

try:
    DAILY_MANAGER_REPORT_HOUR = int((os.getenv("DAILY_MANAGER_REPORT_HOUR", "16") or "16").strip())
except Exception:
    DAILY_MANAGER_REPORT_HOUR = 16
DAILY_MANAGER_REPORT_HOUR = max(0, min(23, DAILY_MANAGER_REPORT_HOUR))

try:
    DAILY_MANAGER_REPORT_MINUTE = int((os.getenv("DAILY_MANAGER_REPORT_MINUTE", "5") or "5").strip())
except Exception:
    DAILY_MANAGER_REPORT_MINUTE = 5
DAILY_MANAGER_REPORT_MINUTE = max(0, min(59, DAILY_MANAGER_REPORT_MINUTE))

DAILY_MANAGER_REPORT_DAY_OF_WEEK = (os.getenv("DAILY_MANAGER_REPORT_DAY_OF_WEEK", "thu") or "thu").strip().lower()
if DAILY_MANAGER_REPORT_DAY_OF_WEEK in {"thursday", "thur", "khamis"}:
    DAILY_MANAGER_REPORT_DAY_OF_WEEK = "thu"
if DAILY_MANAGER_REPORT_DAY_OF_WEEK in {"*", "all", "daily"}:
    DAILY_MANAGER_REPORT_DAY_OF_WEEK = "*"


# ----------------- Subscription Expiry Reminders -----------------
SUBSCRIPTION_EXPIRY_REMINDER_ENABLED = _env_bool("SUBSCRIPTION_EXPIRY_REMINDER_ENABLED", True)
# أيام التنبيه قبل انتهاء الاشتراك (قائمة مفصولة بفواصل)
try:
    SUBSCRIPTION_EXPIRY_REMINDER_DAYS = [
        int(d.strip()) for d in (os.getenv("SUBSCRIPTION_EXPIRY_REMINDER_DAYS", "14,7,3,1") or "14,7,3,1").split(",") if d.strip()
    ]
except Exception:
    SUBSCRIPTION_EXPIRY_REMINDER_DAYS = [14, 7, 3, 1]
# An expiring subscription is the one notice a manager cannot afford to miss:
# the service stops. The in-app notice only lands if they happen to log in, so
# email is on by default here — unlike the weekly summary, which is in-app only.
SUBSCRIPTION_EXPIRY_REMINDER_EMAIL_ENABLED = _env_bool("SUBSCRIPTION_EXPIRY_REMINDER_EMAIL_ENABLED", True)

# The archive add-on lapsing is more disruptive than a subscription lapsing:
# the storage limit falls back to the free tier while the stored data stays, so
# every upload in the platform stops for a school holding more than that.
ARCHIVE_ADDON_EXPIRY_REMINDER_ENABLED = _env_bool(
    "ARCHIVE_ADDON_EXPIRY_REMINDER_ENABLED", True
)

# Storage is its own product, sized from the purchased teacher capacity. Warn
# managers as they approach the limit rather than letting them find out from a
# rejected upload.
STORAGE_THRESHOLD_ALERTS_ENABLED = _env_bool("STORAGE_THRESHOLD_ALERTS_ENABLED", True)

# Electronic payments activate on the gateway callback or the customer's return
# to the site. Both can fail, so a sweep re-checks recent pending payments and
# finishes the ones the gateway actually captured.
PAYMENT_RECONCILIATION_ENABLED = _env_bool("PAYMENT_RECONCILIATION_ENABLED", True)
# How long an unpaid electronic order may sit before the reconciliation sweep
# cancels it. The hosted checkout URL is single-use and is not stored, so a
# customer who closes that tab can never return to it; the order would other-
# wise stay pending forever. Only orders the gateway still reports as unpaid
# are cancelled. Set to 0 to disable and keep the sweep a rescue pass only.
PAYMENT_ABANDON_AFTER_MINUTES = int(os.getenv("PAYMENT_ABANDON_AFTER_MINUTES", "60"))


# ----------------- Unsigned Circular Reminders -----------------
CIRCULAR_SIGNATURE_REMINDER_ENABLED = _env_bool("CIRCULAR_SIGNATURE_REMINDER_ENABLED", True)
# تذكير قبل N ساعة من الموعد النهائي (افتراضي: 48 و 24)
try:
    CIRCULAR_SIGNATURE_REMINDER_HOURS = [
        int(h.strip()) for h in (os.getenv("CIRCULAR_SIGNATURE_REMINDER_HOURS", "48,24") or "48,24").split(",") if h.strip()
    ]
except Exception:
    CIRCULAR_SIGNATURE_REMINDER_HOURS = [48, 24]


# ----------------- Password Change Email Confirmation -----------------
PASSWORD_CHANGE_EMAIL_ENABLED = _env_bool("PASSWORD_CHANGE_EMAIL_ENABLED", True)


# ----------------- Subscription Activation Email -----------------
SUBSCRIPTION_ACTIVATION_EMAIL_ENABLED = _env_bool("SUBSCRIPTION_ACTIVATION_EMAIL_ENABLED", True)


# ----------------- Email -----------------
EMAIL_BACKEND = (
    os.getenv("EMAIL_BACKEND")
    or ("django.core.mail.backends.console.EmailBackend" if ENV != "production" else "django.core.mail.backends.smtp.EmailBackend")
).strip()
EMAIL_HOST = (os.getenv("EMAIL_HOST") or "localhost").strip()
try:
    EMAIL_PORT = int((os.getenv("EMAIL_PORT", "25") or "25").strip())
except Exception:
    EMAIL_PORT = 25
EMAIL_HOST_USER = (os.getenv("EMAIL_HOST_USER") or "").strip()
EMAIL_HOST_PASSWORD = (os.getenv("EMAIL_HOST_PASSWORD") or "").strip()
EMAIL_USE_TLS = _env_bool("EMAIL_USE_TLS", False)
EMAIL_USE_SSL = _env_bool("EMAIL_USE_SSL", False)
DEFAULT_FROM_EMAIL = (os.getenv("DEFAULT_FROM_EMAIL") or "no-reply@tawtheeq-ksa.com").strip()
RESEND_API_KEY = (os.getenv("RESEND_API_KEY") or "").strip()
RESEND_WEBHOOK_SECRET = (os.getenv("RESEND_WEBHOOK_SECRET") or "").strip()
RESEND_API_BASE_URL = (os.getenv("RESEND_API_BASE_URL") or "https://api.resend.com").strip().rstrip("/")
try:
    RESEND_TIMEOUT = max(3, int((os.getenv("RESEND_TIMEOUT", "15") or "15").strip()))
except (TypeError, ValueError):
    RESEND_TIMEOUT = 15
try:
    RESEND_WEBHOOK_TOLERANCE = max(
        60,
        int((os.getenv("RESEND_WEBHOOK_TOLERANCE", "300") or "300").strip()),
    )
except (TypeError, ValueError):
    RESEND_WEBHOOK_TOLERANCE = 300
try:
    EMAIL_TIMEOUT = max(
        3,
        int((os.getenv("EMAIL_TIMEOUT", "15") or "15").strip()),
    )
except (TypeError, ValueError):
    EMAIL_TIMEOUT = 15
try:
    PASSWORD_RESET_TIMEOUT = max(
        300,
        int((os.getenv("PASSWORD_RESET_TIMEOUT", "3600") or "3600").strip()),
    )
except (TypeError, ValueError):
    PASSWORD_RESET_TIMEOUT = 3600

if ENV == "production" and PRODUCTION_STRICT_MODE:
    if EMAIL_BACKEND == "reports.email_backends.ResendEmailBackend":
        if not RESEND_API_KEY:
            raise ImproperlyConfigured("RESEND_API_KEY is required for the Resend email backend.")
    elif EMAIL_BACKEND == "django.core.mail.backends.smtp.EmailBackend":
        if not EMAIL_HOST or EMAIL_HOST.lower() in {"localhost", "127.0.0.1"}:
            raise ImproperlyConfigured("EMAIL_HOST must point to the production SMTP provider.")
        if EMAIL_USE_TLS and EMAIL_USE_SSL:
            raise ImproperlyConfigured("EMAIL_USE_TLS and EMAIL_USE_SSL cannot both be enabled.")
    else:
        raise ImproperlyConfigured("Use SMTP or reports.email_backends.ResendEmailBackend in production.")
    if "@" not in DEFAULT_FROM_EMAIL:
        raise ImproperlyConfigured("DEFAULT_FROM_EMAIL must be a valid sender address.")

try:
    from celery.schedules import crontab
except Exception:  # pragma: no cover
    crontab = None  # type: ignore

CELERY_BEAT_SCHEDULE: dict[str, dict] = {}
if crontab is not None:
    if AUDIT_LOG_CLEANUP_ENABLED:
        CELERY_BEAT_SCHEDULE["cleanup-audit-logs-daily"] = {
            "task": "reports.tasks.cleanup_audit_logs_task",
            "schedule": crontab(minute=15, hour=3),
            "args": (AUDIT_LOG_RETENTION_DAYS,),
        }

    if SESSION_CLEANUP_ENABLED:
        CELERY_BEAT_SCHEDULE["cleanup-expired-sessions-daily"] = {
            "task": "reports.tasks.cleanup_expired_sessions_task",
            "schedule": crontab(minute=45, hour=3),
        }

    if INFRA_CAPACITY_MONITOR_ENABLED:
        CELERY_BEAT_SCHEDULE["monitor-infrastructure-capacity"] = {
            "task": "reports.tasks.monitor_infrastructure_capacity_task",
            "schedule": crontab(minute="*/5"),
        }
        CELERY_BEAT_SCHEDULE["monitor-managed-projects"] = {
            "task": "operations.tasks.run_operations_monitor_task",
            "schedule": crontab(minute="*/2"),
        }
        CELERY_BEAT_SCHEDULE["cleanup-operations-history"] = {
            "task": "operations.tasks.cleanup_operations_history_task",
            "schedule": crontab(minute=25, hour=4),
        }
        CELERY_BEAT_SCHEDULE["monitor-deployment-state"] = {
            "task": "operations.tasks.monitor_deployment_state_task",
            "schedule": crontab(minute="*/5"),
        }
        CELERY_BEAT_SCHEDULE["sync-deployed-revisions"] = {
            "task": "operations.tasks.sync_deployed_revisions_task",
            "schedule": crontab(minute="*/5"),
        }

    CELERY_BEAT_SCHEDULE["cleanup-generated-exports-hourly"] = {
        "task": "reports.tasks.cleanup_generated_exports_task",
        "schedule": crontab(minute=20),
    }
    CELERY_BEAT_SCHEDULE["recover-stale-generated-exports"] = {
        "task": "reports.tasks.recover_stale_generated_exports_task",
        "schedule": 60,
        "options": {"queue": "default"},
    }

    CELERY_BEAT_SCHEDULE["cleanup-platform-email-daily"] = {
        "task": "reports.tasks.cleanup_platform_email_task",
        "schedule": crontab(minute=35, hour=3),
    }

    if DAILY_MANAGER_REPORT_ENABLED:
        CELERY_BEAT_SCHEDULE["send-daily-manager-summary"] = {
            "task": "reports.tasks.send_daily_manager_summary_task",
            "schedule": crontab(
                minute=DAILY_MANAGER_REPORT_MINUTE,
                hour=DAILY_MANAGER_REPORT_HOUR,
                day_of_week=DAILY_MANAGER_REPORT_DAY_OF_WEEK,
            ),
        }

    if SUBSCRIPTION_EXPIRY_REMINDER_ENABLED:
        CELERY_BEAT_SCHEDULE["check-subscription-expiry-daily"] = {
            "task": "reports.tasks.check_subscription_expiry_task",
            "schedule": crontab(minute=30, hour=8),  # يومياً الساعة 8:30 صباحاً
        }

    if ARCHIVE_ADDON_EXPIRY_REMINDER_ENABLED:
        CELERY_BEAT_SCHEDULE["check-archive-addon-expiry-daily"] = {
            "task": "reports.tasks.check_archive_addon_expiry_task",
            "schedule": crontab(minute=45, hour=8),
        }

    if STORAGE_THRESHOLD_ALERTS_ENABLED:
        CELERY_BEAT_SCHEDULE["check-storage-thresholds-daily"] = {
            "task": "reports.tasks.check_storage_thresholds_task",
            "schedule": crontab(minute=15, hour=9),
        }

    if PAYMENT_RECONCILIATION_ENABLED:
        CELERY_BEAT_SCHEDULE["reconcile-pending-gateway-payments"] = {
            "task": "reports.tasks.reconcile_pending_gateway_payments_task",
            "schedule": crontab(minute="*/20"),
        }

    if CIRCULAR_SIGNATURE_REMINDER_ENABLED:
        CELERY_BEAT_SCHEDULE["remind-unsigned-circulars"] = {
            "task": "reports.tasks.remind_unsigned_circulars_task",
            "schedule": crontab(minute=0, hour="8,14"),  # مرتين يومياً: 8 صباحاً و 2 ظهراً
        }


# ----------------- Static files -----------------
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}

if ENV == "production":
    STORAGES["staticfiles"] = {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    }
    WHITENOISE_MAX_AGE = 60 * 60 * 24 * 365


# ----------------- Media -----------------
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"


# ----------------- Upload limits -----------------
DATA_UPLOAD_MAX_NUMBER_FIELDS = int(os.getenv("DATA_UPLOAD_MAX_NUMBER_FIELDS", "20000"))
# Caps the non-file portion of a request body, which Django buffers in memory.
# Uploaded files are exempt (they spool to disk past FILE_UPLOAD_MAX_MEMORY_SIZE),
# so this only needs to cover form fields. The largest legitimate form here is a
# notification addressed to thousands of recipients — roughly 1 MB — so 10 MB
# leaves a wide margin while removing a cheap memory-exhaustion vector: at the
# previous 40 MB, a handful of concurrent crafted POSTs could OOM a 768 MB
# container.
DATA_UPLOAD_MAX_MEMORY_SIZE = int(os.getenv("DATA_UPLOAD_MAX_MEMORY_SIZE", str(10 * 1024 * 1024)))
FILE_UPLOAD_MAX_MEMORY_SIZE = int(os.getenv("FILE_UPLOAD_MAX_MEMORY_SIZE", str(2 * 1024 * 1024)))
DATA_UPLOAD_MAX_NUMBER_FILES = int(os.getenv("DATA_UPLOAD_MAX_NUMBER_FILES", "20"))


# ----------------- Cloudflare R2 (conditional) -----------------
# Uploaded school files are private by default. Direct public media URLs must be
# enabled explicitly because reports, tickets, circulars, achievement evidence,
# and payment receipts may contain sensitive data.
MEDIA_PUBLIC_ACCESS_ENABLED = _env_bool("MEDIA_PUBLIC_ACCESS_ENABLED", False)

# عمر الرابط الموقَّع = مدة استهلاكه المتوقعة، لا مدة الجلسة. الرابط يحمل
# التخويل في ذاته: من يحصل عليه يفتح الملف بلا حساب ولا عضوية. فطولُ عمره هو
# بالضبط طولُ نافذة التسريب — عبر سجل المتصفح، أو لقطة شاشة تُشارَك، أو جهاز
# مشترك. وربع ساعة تكفي أي فتح أو تنزيل مشروع.
#
# ويُعرَّف خارج فرع ``_use_r2`` عمداً: السياسة واحدة أياً كان مخزن الوسائط،
# وحصرُها داخل الفرع كان يجعلها غير قابلة للفحص في أي بيئة لا R2 فيها — فيمرّ
# اختبارُها على قيمةٍ افتراضية لا وجود لها في الإنتاج.
MEDIA_SIGNED_URL_EXPIRE_SECONDS = int(os.getenv("AWS_QUERYSTRING_EXPIRE", "900"))
if ENV == "production" and PRODUCTION_STRICT_MODE and MEDIA_PUBLIC_ACCESS_ENABLED:
    raise ImproperlyConfigured("MEDIA_PUBLIC_ACCESS_ENABLED must remain False for private school files.")

R2_PUBLIC_DOMAIN = (os.getenv("R2_PUBLIC_DOMAIN") or "").strip()
if R2_PUBLIC_DOMAIN:
    try:
        parts = urlsplit(R2_PUBLIC_DOMAIN)
        if parts.scheme and parts.netloc:
            R2_PUBLIC_DOMAIN = parts.netloc
    except Exception:
        pass
    R2_PUBLIC_DOMAIN = R2_PUBLIC_DOMAIN.strip().strip("/")
    if "/" in R2_PUBLIC_DOMAIN:
        R2_PUBLIC_DOMAIN = R2_PUBLIC_DOMAIN.split("/", 1)[0]

if _use_r2:
    STORAGES["default"] = {
        "BACKEND": "reports.storage.R2MediaStorage",
    }

    AWS_ACCESS_KEY_ID = R2_ACCESS_KEY_ID
    AWS_SECRET_ACCESS_KEY = R2_SECRET_ACCESS_KEY
    AWS_STORAGE_BUCKET_NAME = R2_BUCKET_NAME
    AWS_S3_ENDPOINT_URL = R2_ENDPOINT_URL

    AWS_S3_REGION_NAME = os.getenv("AWS_S3_REGION_NAME", "auto")
    AWS_S3_SIGNATURE_VERSION = os.getenv("AWS_S3_SIGNATURE_VERSION", "s3v4")
    AWS_S3_ADDRESSING_STYLE = os.getenv("AWS_S3_ADDRESSING_STYLE", "path")
    AWS_DEFAULT_ACL = None

    # Private is the safe default. When public media is disabled, ignore any
    # stale AWS_QUERYSTRING_AUTH=0 value and always issue expiring signed URLs.
    AWS_QUERYSTRING_AUTH = _media_querystring_auth_enabled(
        public_access_enabled=MEDIA_PUBLIC_ACCESS_ENABLED,
        requested_querystring_auth=_env_bool("AWS_QUERYSTRING_AUTH", False),
    )
    AWS_QUERYSTRING_EXPIRE = MEDIA_SIGNED_URL_EXPIRE_SECONDS
    AWS_S3_FILE_OVERWRITE = _env_bool("AWS_S3_FILE_OVERWRITE", True)

    AWS_S3_OBJECT_PARAMETERS = {
        "CacheControl": os.getenv("AWS_S3_CACHE_CONTROL", "max-age=31536000"),
    }

    if MEDIA_PUBLIC_ACCESS_ENABLED and R2_PUBLIC_DOMAIN and not AWS_QUERYSTRING_AUTH:
        AWS_S3_CUSTOM_DOMAIN = R2_PUBLIC_DOMAIN


# ----------------- Security (production) -----------------
if ENV == "production":
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    CSRF_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = os.getenv("SESSION_COOKIE_SAMESITE", "Lax")
    CSRF_COOKIE_SAMESITE = os.getenv("CSRF_COOKIE_SAMESITE", "Lax")

    CSP_ENABLED = _env_bool("CSP_ENABLED", True)
    CSP_REPORT_ONLY = _env_bool("CSP_REPORT_ONLY", False)
    CONTENT_SECURITY_POLICY = (os.getenv("CONTENT_SECURITY_POLICY") or "").strip()

    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = os.getenv("SECURE_REFERRER_POLICY", "strict-origin-when-cross-origin")
    SECURE_CROSS_ORIGIN_OPENER_POLICY = os.getenv("SECURE_CROSS_ORIGIN_OPENER_POLICY", "same-origin")

    SECURE_HSTS_SECONDS = int(os.getenv("SECURE_HSTS_SECONDS", "31536000"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    X_FRAME_OPTIONS = "DENY"
else:
    SECURE_SSL_REDIRECT = False
    CSP_ENABLED = _env_bool("CSP_ENABLED", False)
    CSP_REPORT_ONLY = _env_bool("CSP_REPORT_ONLY", True)
    CONTENT_SECURITY_POLICY = (os.getenv("CONTENT_SECURITY_POLICY") or "").strip()


# ----------------- Logging (Django) -----------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
    "loggers": {
        "django.request": {"handlers": ["console"], "level": "ERROR", "propagate": False},
        "django.server": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "django.db.backends": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "channels": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "daphne": {"handlers": ["console"], "level": "WARNING", "propagate": False},
    },
}


# ----------------- Custom User / Auth redirects -----------------
AUTH_USER_MODEL = "reports.Teacher"

LOGIN_URL = "reports:login"
LOGIN_REDIRECT_URL = "reports:home"
LOGOUT_REDIRECT_URL = "reports:login"

MESSAGE_STORAGE = "django.contrib.messages.storage.cookie.CookieStorage"


# ----------------- Sessions / Idle logout -----------------
IDLE_LOGOUT_SECONDS = int(os.getenv("IDLE_LOGOUT_SECONDS", str(30 * 60)))

SESSION_COOKIE_AGE = IDLE_LOGOUT_SECONDS
SESSION_SAVE_EVERY_REQUEST = False

# ── Session backend ─────────────────────────────────────────────────
# cached_db: reads from cache (fast), writes to both cache + DB.
# If a cache key expires (TIMEOUT=300), Django transparently falls back
# to the DB row — so users are never logged out by cache eviction.
# Pure "cache" backend would lose sessions when Redis keys expire.
if REDIS_CACHE_URL:
    SESSION_ENGINE = "django.contrib.sessions.backends.cached_db"
    SESSION_CACHE_ALIAS = "default"
else:
    SESSION_ENGINE = "django.contrib.sessions.backends.db"


# ----------------- Django REST Framework -----------------
REST_FRAMEWORK = {
    # ترتيب المصادقة مقصود: مفتاح التكامل أولاً لأنه يُعرَّف بترويسة صريحة
    # ويُثبّت المدرسة النشطة على الطلب؛ ثم الجلسة لعملاء المتصفّح.
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "reports.api_auth.SchoolApiKeyAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 25,
    "DEFAULT_THROTTLE_CLASSES": [
        "reports.api_auth.ApiKeyRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
        "rest_framework.throttling.AnonRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "user": "120/min",
        "anon": "30/min",
        # حدُّ التكامل أعلى من حدّ الإنسان وأقل من اللانهاية: نظامٌ يزامن
        # دفعةً كبيرة سلوكُه مشروع، لكن مفتاحاً مسرَّباً يجب أن يصطدم بسقف.
        "api_key": "600/min",
    },
}

# ----------------- Misc -----------------
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

PRINT_MULTIHEAD_POLICY = "blank"  # أو "dept"
DEPARTMENT_HEAD_ROLE_SLUG = "department_head"

_site_url_from_env = (os.getenv("SITE_URL") or "").strip()
if not _site_url_from_env:
    _site_url_from_env = (
        "https://tawtheeq-ksa.com" if ENV == "production" else "http://127.0.0.1:8000"
    )
SITE_URL = _validated_site_url(_site_url_from_env, environment=ENV)

# A PWA is permanently tied to the origin it was installed from. Avoid
# advertising a localhost app unless installation testing is explicitly on.
PWA_INSTALL_ENABLED = _env_bool("PWA_INSTALL_ENABLED", ENV == "production")

# Native operations app alerts. Credentials are read by Google ADC from the
# file path in GOOGLE_APPLICATION_CREDENTIALS; the JSON key is never stored in
# Django settings or the repository.
FCM_PROJECT_ID = (os.getenv("FCM_PROJECT_ID") or "").strip()
OPERATIONS_MOBILE_TOKEN_HOURS = int(os.getenv("OPERATIONS_MOBILE_TOKEN_HOURS", "12") or "12")
OPERATIONS_PROBE_TIMEOUT_SECONDS = float(os.getenv("OPERATIONS_PROBE_TIMEOUT_SECONDS", "8") or "8")
OPERATIONS_HISTORY_RETENTION_DAYS = int(os.getenv("OPERATIONS_HISTORY_RETENTION_DAYS", "30") or "30")
OPERATIONS_GITHUB_REPOSITORY = (
    os.getenv("OPERATIONS_GITHUB_REPOSITORY") or "xmansx2030-lgtm/school_reports"
).strip()
OPERATIONS_GITHUB_BRANCH = (os.getenv("OPERATIONS_GITHUB_BRANCH") or "main").strip()
OPERATIONS_GITHUB_WORKFLOW = (os.getenv("OPERATIONS_GITHUB_WORKFLOW") or "ci.yml").strip()
OPERATIONS_GITHUB_TOKEN = (os.getenv("OPERATIONS_GITHUB_TOKEN") or "").strip()
OPERATIONS_DEPLOY_MONITOR_ENABLED = _env_bool("OPERATIONS_DEPLOY_MONITOR_ENABLED", True)
RELEASE_SHA = (os.getenv("RELEASE_SHA") or "").strip()
RELEASE_IMAGE = (os.getenv("RELEASE_IMAGE") or "").strip()

# Public business disclosure shown in a collapsed, low-prominence section on
# the landing and legal pages. Never place a national ID or personal photo here.
BUSINESS_LEGAL_NAME = (os.getenv("BUSINESS_LEGAL_NAME") or "").strip()
BUSINESS_COMMERCIAL_REGISTRATION = (
    os.getenv("BUSINESS_COMMERCIAL_REGISTRATION") or ""
).strip()
BUSINESS_FREELANCE_DOCUMENT_NUMBER = (
    os.getenv("BUSINESS_FREELANCE_DOCUMENT_NUMBER") or ""
).strip()
BUSINESS_FREELANCE_ACTIVITY = (
    os.getenv("BUSINESS_FREELANCE_ACTIVITY") or ""
).strip()
BUSINESS_FREELANCE_DOCUMENT_EXPIRY = (
    os.getenv("BUSINESS_FREELANCE_DOCUMENT_EXPIRY") or ""
).strip()
BUSINESS_FREELANCE_DOCUMENT_URL = (
    os.getenv("BUSINESS_FREELANCE_DOCUMENT_URL") or ""
).strip()
BUSINESS_TAX_NUMBER = (os.getenv("BUSINESS_TAX_NUMBER") or "").strip()
BUSINESS_LICENSES = (os.getenv("BUSINESS_LICENSES") or "").strip()
BUSINESS_VERIFICATION_URL = (os.getenv("BUSINESS_VERIFICATION_URL") or "").strip()
BUSINESS_ADDRESS = (os.getenv("BUSINESS_ADDRESS") or "").strip()
BUSINESS_SUPPORT_EMAIL = (os.getenv("BUSINESS_SUPPORT_EMAIL") or "").strip()
BUSINESS_SUPPORT_PHONE = (os.getenv("BUSINESS_SUPPORT_PHONE") or "").strip()

if ENV == "production" and PRODUCTION_STRICT_MODE:
    _business_disclosure_missing = [
        name
        for name, value in (
            ("BUSINESS_LEGAL_NAME", BUSINESS_LEGAL_NAME),
            ("BUSINESS_ADDRESS", BUSINESS_ADDRESS),
            ("BUSINESS_SUPPORT_EMAIL", BUSINESS_SUPPORT_EMAIL),
            ("BUSINESS_SUPPORT_PHONE", BUSINESS_SUPPORT_PHONE),
        )
        if not value
    ]
    if not (BUSINESS_COMMERCIAL_REGISTRATION or BUSINESS_FREELANCE_DOCUMENT_NUMBER):
        _business_disclosure_missing.append(
            "BUSINESS_COMMERCIAL_REGISTRATION or BUSINESS_FREELANCE_DOCUMENT_NUMBER"
        )
    if _business_disclosure_missing:
        raise ImproperlyConfigured(
            "Public business disclosure is incomplete in production: "
            + ", ".join(_business_disclosure_missing)
        )

# ----------------- Tamara payments -----------------
# Credentials are server-only and the switch stays off until the merchant,
# production token and signed notification webhook all belong to Tawtheeq.
TAMARA_ENABLED = _env_bool("TAMARA_ENABLED", False)
TAMARA_ENVIRONMENT = (os.getenv("TAMARA_ENVIRONMENT") or "sandbox").strip().lower()
TAMARA_API_TOKEN = (os.getenv("TAMARA_API_TOKEN") or "").strip()
TAMARA_NOTIFICATION_TOKEN = (os.getenv("TAMARA_NOTIFICATION_TOKEN") or "").strip()
if TAMARA_ENVIRONMENT not in {"sandbox", "production"}:
    raise ImproperlyConfigured("TAMARA_ENVIRONMENT must be either sandbox or production.")
if TAMARA_ENABLED:
    _tamara_missing = [
        name
        for name, value in (
            ("TAMARA_API_TOKEN", TAMARA_API_TOKEN),
            ("TAMARA_NOTIFICATION_TOKEN", TAMARA_NOTIFICATION_TOKEN),
        )
        if not value
    ]
    if _tamara_missing:
        raise ImproperlyConfigured(
            "Tamara is enabled but required credentials are missing: "
            + ", ".join(_tamara_missing)
        )
    if ENV == "production" and PRODUCTION_STRICT_MODE and TAMARA_ENVIRONMENT != "production":
        raise ImproperlyConfigured(
            "TAMARA_ENVIRONMENT must be production when Tamara is enabled in strict production mode."
        )
TAMARA_API_BASE_URL = (
    os.getenv("TAMARA_API_BASE_URL")
    or (
        "https://api.tamara.co"
        if TAMARA_ENVIRONMENT == "production"
        else "https://api-sandbox.tamara.co"
    )
).strip().rstrip("/")
try:
    TAMARA_INSTALMENTS = max(2, min(12, int(os.getenv("TAMARA_INSTALMENTS", "4"))))
except (TypeError, ValueError):
    TAMARA_INSTALMENTS = 4
try:
    TAMARA_REQUEST_TIMEOUT = max(2, min(60, int(os.getenv("TAMARA_REQUEST_TIMEOUT", "15"))))
except (TypeError, ValueError):
    TAMARA_REQUEST_TIMEOUT = 15

# ----------------- Moyasar payments -----------------
# Keep test mode isolated from strict production. The secret key is server-only;
# checkout is hosted by Moyasar and the returned invoice is verified server-side.
MOYASAR_ENABLED = _env_bool("MOYASAR_ENABLED", False)
MOYASAR_ENVIRONMENT = (os.getenv("MOYASAR_ENVIRONMENT") or "test").strip().lower()
MOYASAR_SECRET_KEY = (os.getenv("MOYASAR_SECRET_KEY") or "").strip()
if MOYASAR_ENVIRONMENT not in {"test", "live"}:
    raise ImproperlyConfigured("MOYASAR_ENVIRONMENT must be either test or live.")
if MOYASAR_ENABLED:
    if not MOYASAR_SECRET_KEY:
        raise ImproperlyConfigured(
            "Moyasar is enabled but MOYASAR_SECRET_KEY is missing."
        )
    expected_prefix = "sk_live_" if MOYASAR_ENVIRONMENT == "live" else "sk_test_"
    if not MOYASAR_SECRET_KEY.startswith(expected_prefix):
        raise ImproperlyConfigured(
            "MOYASAR_SECRET_KEY does not match MOYASAR_ENVIRONMENT."
        )
    if ENV == "production" and PRODUCTION_STRICT_MODE and MOYASAR_ENVIRONMENT != "live":
        raise ImproperlyConfigured(
            "MOYASAR_ENVIRONMENT must be live when Moyasar is enabled in strict production mode."
        )
MOYASAR_API_BASE_URL = (
    os.getenv("MOYASAR_API_BASE_URL") or "https://api.moyasar.com/v1"
).strip().rstrip("/")
MOYASAR_REQUEST_TIMEOUT = int(os.getenv("MOYASAR_REQUEST_TIMEOUT", "15"))
CANONICAL_HOST_REDIRECT = _env_bool(
    "CANONICAL_HOST_REDIRECT",
    ENV == "production",
)

# Self-service school trial. The free plan exposes the complete product journey
# while keeping teacher count and archive storage deliberately small.
TRIAL_PLAN_NAME = (os.getenv("TRIAL_PLAN_NAME") or "التجربة المجانية").strip()
TRIAL_MAX_TEACHERS = max(1, int(os.getenv("TRIAL_MAX_TEACHERS", "5") or "5"))
TRIAL_ARCHIVE_STORAGE_GB = max(
    1,
    int(os.getenv("TRIAL_ARCHIVE_STORAGE_GB", "1") or "1"),
)
