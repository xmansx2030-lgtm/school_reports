# reports/templatetags/arabic_tags.py
# -*- coding: utf-8 -*-
"""تطابق العدد والمعدود في العربية.

**لماذا لا يكفي ``pluralize``.** مرشّح جانغو مبنيٌّ على لغةٍ لها صيغتان: واحد
وغيره. والعربية لها خمس، وقاعدة تمييز العدد فيها تعكس ما يتوقّعه من جاء من
الإنجليزية — فالمعدود يُجمع في ‎3–10‎ ويُفرد في ‎11‎ فما فوق:

    ==========  =============  ====================
    العدد        الصيغة         مثال
    ==========  =============  ====================
    ‎0‎           جمع            لا عناصر
    ‎1‎           مفرد           عنصر واحد
    ‎2‎           مثنّى           عنصران
    ‎3–10‎        جمع            5 عناصر
    ‎11+‎         مفرد منصوب     15 عنصراً
    ==========  =============  ====================

وكانت اللوحة تكتب «3 عنصر يحتاج متابعة» و«1 طلبات مكتملة» — أي أنها تُلصق
الرقم بصيغةٍ واحدة مهما كان. وهو خطأٌ يراه كل مستخدمٍ عربي في أول نظرة،
ويصعب تصيّده واحداً واحداً لأنه منتشرٌ في القوالب.

الاستخدام::

    {% load arabic_tags %}
    {{ count|arabic_count:"عنصر,عنصران,عناصر,عنصراً" }}
        → «لا عناصر» · «عنصر واحد» · «عنصران» · «5 عناصر» · «15 عنصراً»

    {{ count|arabic_plural:"طلب,طلبان,طلبات,طلباً" }}
        → الصيغة وحدها بلا الرقم، لمن أراد وضع الرقم بنفسه.

الصيغ الأربع تُمرَّر مفصولةً بفواصل بالترتيب: مفرد، مثنّى، جمع، منصوب.
ويكفي تمرير المفرد والجمع؛ فما نقص يُشتقّ بأقرب صيغةٍ متاحة.
"""
from __future__ import annotations

from django import template

register = template.Library()

_ZERO_PREFIX = "لا "
_ONE_SUFFIX = " واحد"


def _forms(spec: str) -> tuple[str, str, str, str]:
    """يفكّ «مفرد,مثنّى,جمع,منصوب» ويسدّ ما نقص بأقرب صيغة."""
    parts = [part.strip() for part in str(spec).split(",")]
    parts = [part for part in parts if part]
    if not parts:
        return ("", "", "", "")

    singular = parts[0]
    dual = parts[1] if len(parts) > 1 else f"{singular}ان"
    plural = parts[2] if len(parts) > 2 else singular
    accusative = parts[3] if len(parts) > 3 else singular
    return (singular, dual, plural, accusative)


def _to_int(value) -> int | None:
    try:
        return int(str(value).strip().replace(",", "").replace("٬", ""))
    except (TypeError, ValueError):
        return None


@register.filter(name="arabic_plural")
def arabic_plural(value, forms: str = "") -> str:
    """يعيد صيغة المعدود المناسبة للعدد، بلا العدد نفسه."""
    singular, dual, plural, accusative = _forms(forms)
    count = _to_int(value)
    if count is None:
        return plural or singular

    # الحدود مأخوذة من قواعد الجمع العربية في ‎CLDR‎ — لا من اجتهاد.
    count = abs(count)
    remainder = count % 100
    if count == 0:
        return plural or singular          # zero
    if count == 1:
        return singular                    # one
    if count == 2:
        return dual                        # two
    if 3 <= remainder <= 10:
        return plural                      # few  — ‎3–10‎ و‎103‎ و‎110‎
    if 11 <= remainder <= 99:
        return accusative                  # many — ‎11–99‎ و‎111‎
    return singular                        # other — ‎100‎ و‎101‎ و‎1000‎


@register.filter(name="arabic_count")
def arabic_count(value, forms: str = "") -> str:
    """العدد ومعدوده معاً بصيغةٍ عربية سليمة."""
    singular, dual, plural, _accusative = _forms(forms)
    count = _to_int(value)
    if count is None:
        return f"{value} {plural or singular}".strip()

    word = arabic_plural(count, forms)
    if count == 0:
        return f"{_ZERO_PREFIX}{word}"
    if count == 1:
        return f"{singular}{_ONE_SUFFIX}"
    if count == 2:
        return dual
    # الفواصل الألفية تُترك لـ ‎intcomma‎ إن أرادها القالب — لا تُفرض هنا.
    return f"{count} {word}"
