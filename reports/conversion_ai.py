"""Safe Arabic outreach drafting for Tawtheeq conversion opportunities."""
from __future__ import annotations

import json
import logging
from urllib.error import HTTPError, URLError

from django.conf import settings

from .ai_client import extract_output_text, responses_create, truncation_reason
from .ai_usage import ai_usage_context


logger = logging.getLogger(__name__)

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "analysis_summary",
        "recommended_action",
        "email_subject",
        "email_body",
        "in_app_title",
        "in_app_body",
        "whatsapp_body",
        "call_script",
    ],
    "properties": {
        "analysis_summary": {"type": "string", "maxLength": 450},
        "recommended_action": {"type": "string", "maxLength": 300},
        "email_subject": {"type": "string", "maxLength": 140},
        "email_body": {"type": "string", "maxLength": 1800},
        "in_app_title": {"type": "string", "maxLength": 110},
        "in_app_body": {"type": "string", "maxLength": 500},
        "whatsapp_body": {"type": "string", "maxLength": 700},
        "call_script": {
            "type": "array",
            "minItems": 3,
            "maxItems": 5,
            "items": {"type": "string", "maxLength": 220},
        },
    },
}

INSTRUCTIONS = """
أنت مساعد تحويل اشتراكات داخلي لمنصة توثيق السعودية. اكتب بالعربية المهنية
المطمئنة لمدير مدرسة. مهمتك صياغة مسودة قابلة للمراجعة البشرية، لا اتخاذ قرار
الإرسال. ميّز توثيق كمنصة عمل مدرسية متكاملة: التقارير والشواهد، ملفات الإنجاز،
الأداء القيادي، التكليفات، الاجتماعات، الخطط، التعاميم، الأرشفة، والذكاء
الاصطناعي. اذكر فقط الخدمات الموجودة في المدخل والتي استخدمتها المدرسة.

لا تدّع مراقبة الأشخاص، ولا تقل إننا شاهدنا محتوى أعمالهم، ولا تخترع خصماً أو
ميزة أو رقماً أو ضماناً. لا تذكر درجة النية للعميل. إذا كانت الاستفادة ضعيفة،
اجعل الدعوة جلسة تفعيل تساعد المدرسة أولاً. وإذا كانت مرتفعة، اربط الباقة
باستمرارية الفائدة المحققة. اختم بدعوة واحدة واضحة وسهلة للرد. النص سعودي
رسمي، دافئ، ومختصر بلا مبالغة تسويقية.
""".strip()


class ConversionDraftUnavailable(RuntimeError):
    pass


def _clean(value, limit):
    return " ".join(str(value or "").split())[:limit]


def fallback_draft(snapshot: dict, *, objective: str, tone: str) -> dict:
    school = snapshot["school"]["display_name"]
    services = snapshot["signals"].get("service_labels") or []
    services_text = "، ".join(services[:3]) or "خدمات توثيق المدرسية"
    low_use = int(snapshot["signals"].get("utilization_score") or 0) < 20
    plan = snapshot["recommendation"].get("suggested_plan") or "الباقة المناسبة"
    if low_use or objective == "activate":
        lead = (
            f"نقترح لمدرستكم جلسة تفعيل قصيرة تساعد فريقكم على بدء الاستفادة "
            f"من {services_text} بخطوات عملية واضحة."
        )
        call_to_action = "يسعدنا تنسيق موعد مناسب والبدء بالخدمات الأعلى أولوية لديكم."
        analysis = "الاستفادة الحالية محدودة؛ الأفضل تقديم قيمة عملية قبل مناقشة الاشتراك المدفوع."
    else:
        lead = (
            f"استفادت مدرستكم من {services_text}، ويمكن للباقة المدفوعة أن تحافظ "
            "على استمرارية هذه الأعمال وتوسّع مشاركة فريق المدرسة ضمن مساحة عمل واحدة."
        )
        call_to_action = f"نقترح مراجعة {plan} معكم واختيار ما يناسب حجم الفريق واحتياجه."
        analysis = "توجد استفادة فعلية يمكن ربطها باستمرارية العمل، مع إبقاء العرض مرتبطًا باحتياج المدرسة."
    body = (
        f"سعادة مدير/ة {school}،\n\n{lead}\n\n"
        "توثيق يجمع العمل الإداري والتوثيق والمتابعة في منصة عربية واحدة، ويقلل تشتت الملفات والخطوات بين الأدوات.\n\n"
        f"{call_to_action}\n\nمع التقدير،\nفريق منصة توثيق"
    )
    return {
        "analysis_summary": analysis,
        "recommended_action": snapshot["recommendation"]["next_action"],
        "email_subject": f"خطوة مقترحة لتعظيم استفادة {school} من توثيق",
        "email_body": body,
        "in_app_title": "لنرفع استفادتكم من توثيق",
        "in_app_body": f"{lead} {call_to_action}",
        "whatsapp_body": f"السلام عليكم، معكم فريق توثيق. {lead} {call_to_action}",
        "call_script": [
            "اسأل عن أكثر عمل إداري يستهلك وقت الفريق حاليًا.",
            f"اربط الاحتياج بالخدمات المستخدمة فعليًا: {services_text}.",
            "اعرض جلسة تفعيل أو الباقة المناسبة وفق جاهزية المدرسة، ثم اتفق على خطوة تالية واحدة.",
        ],
        "ai_generated": False,
        "ai_model": "",
    }


def generate_conversion_draft(snapshot: dict, *, objective: str, tone: str, school, teacher) -> dict:
    api_key = str(getattr(settings, "OPENAI_API_KEY", "") or "").strip()
    if not api_key:
        return fallback_draft(snapshot, objective=objective, tone=tone)

    model = str(
        getattr(
            settings,
            "CONVERSION_AI_MODEL",
            getattr(settings, "REPORT_AI_MODEL", getattr(settings, "MANSOUR_ASSISTANT_MODEL", "gpt-5.6-luna")),
        )
    )
    body = {
        "model": model,
        "instructions": INSTRUCTIONS,
        "input": json.dumps(
            {"objective": objective, "tone": tone, "aggregate_snapshot": snapshot},
            ensure_ascii=False,
        ),
        "reasoning": {"effort": str(getattr(settings, "AI_FAST_REASONING_EFFORT", "none"))},
        "text": {
            "verbosity": "low",
            "format": {
                "type": "json_schema",
                "name": "tawtheeq_conversion_outreach",
                "strict": True,
                "schema": SCHEMA,
            },
        },
        "max_output_tokens": int(getattr(settings, "CONVERSION_AI_MAX_OUTPUT_TOKENS", 1800)),
        "store": False,
    }
    try:
        with ai_usage_context(school=school, teacher=teacher):
            payload = responses_create(
                body,
                api_key=api_key,
                timeout=float(getattr(settings, "CONVERSION_AI_TIMEOUT_SECONDS", 30)),
                stage="conversion-outreach",
            )
        if truncation_reason(payload):
            raise ConversionDraftUnavailable("AI response was incomplete")
        parsed = json.loads(extract_output_text(payload))
        if not isinstance(parsed, dict):
            raise ConversionDraftUnavailable("AI response was not an object")
        draft = {
            "analysis_summary": _clean(parsed.get("analysis_summary"), 450),
            "recommended_action": _clean(parsed.get("recommended_action"), 300),
            "email_subject": _clean(parsed.get("email_subject"), 140),
            "email_body": str(parsed.get("email_body") or "").strip()[:1800],
            "in_app_title": _clean(parsed.get("in_app_title"), 110),
            "in_app_body": str(parsed.get("in_app_body") or "").strip()[:500],
            "whatsapp_body": str(parsed.get("whatsapp_body") or "").strip()[:700],
            "call_script": [_clean(item, 220) for item in (parsed.get("call_script") or [])[:5]],
            "ai_generated": True,
            "ai_model": model,
        }
        if not all((draft["email_subject"], draft["email_body"], draft["in_app_title"], draft["in_app_body"])):
            raise ConversionDraftUnavailable("AI response was missing required copy")
        return draft
    except (HTTPError, URLError, TimeoutError, ValueError, TypeError, json.JSONDecodeError, ConversionDraftUnavailable) as exc:
        logger.warning("Conversion drafting fell back to deterministic copy: %s", exc.__class__.__name__)
        return fallback_draft(snapshot, objective=objective, tone=tone)
