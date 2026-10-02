# -*- coding: utf-8 -*-
"""ترجمة صفوف ``AuditLog`` إلى لغة يفهمها صاحب الشأن.

السجل يُخزَّن بمفردات المطوّر: ``model_name="Report"`` و``action="create"``.
وعرضه هكذا يجعل الشاشة سجلّ نظام لا سجلّ أعمال — والمستخدم الذي يفتحها ليتذكر
ما فعله الأسبوع الماضي لا يعنيه اسم الصنف في بايثون.

هذه الوحدة **عرض فقط**: لا تُخزَّن أي من هذه النصوص في قاعدة البيانات، فتغيير
صياغة لا يستدعي ترحيلاً، وسجلٌّ لموديل لم يُترجم بعدُ يظهر باسمه الخام بدل أن
يختفي أو يُسقط الصفحة.
"""
from __future__ import annotations

from dataclasses import dataclass

# ─────────────────────────────────────────────────────────────────────────────
# الموديلات: (المفرد، المفرد منصوباً، الأيقونة)
# النصب مطلوب لأن الجملة العربية «أنشأ تقريراً» تحتاج المفعول منصوباً، وتركيبه
# آلياً بإلحاق ألف يُنتج «أنشأ مدرسةا».
# ─────────────────────────────────────────────────────────────────────────────
_MODELS: dict[str, tuple[str, str, str]] = {
    "Report": ("تقرير", "تقريراً", "fa-file-lines"),
    "Ticket": ("طلب", "طلباً", "fa-inbox"),
    "Teacher": ("مستخدم", "بيانات مستخدم", "fa-user"),
    "School": ("مدرسة", "مدرسة", "fa-school"),
    "Department": ("قسم", "قسماً", "fa-sitemap"),
    "SchoolMembership": ("عضوية مدرسة", "عضوية مدرسة", "fa-id-badge"),
    "DepartmentMembership": ("تكليف قسم", "تكليف قسم", "fa-user-tag"),
    "Notification": ("إشعار أو تعميم", "إشعاراً أو تعميماً", "fa-bullhorn"),
    "SchoolSubscription": ("اشتراك", "اشتراكاً", "fa-credit-card"),
    "TeacherAchievementFile": ("ملف إنجاز", "ملف إنجاز", "fa-folder-open"),
    "StaffScope": ("نطاق صلاحية", "نطاق صلاحية", "fa-user-shield"),
    "Assignment": ("تكليف", "تكليفاً", "fa-list-check"),
    "AssignmentEvidence": ("شاهد تكليف", "شاهد تكليف", "fa-paperclip"),
    "Meeting": ("اجتماع", "اجتماعاً", "fa-users-rectangle"),
    "MeetingMinutes": ("محضر اجتماع", "محضر اجتماع", "fa-file-signature"),
    "Decision": ("قرار", "قراراً", "fa-gavel"),
    "Plan": ("خطة", "خطة", "fa-compass-drafting"),
    "Initiative": ("مبادرة", "مبادرة", "fa-lightbulb"),
    "Document": ("وثيقة", "وثيقة", "fa-folder"),
    "CircularDraft": ("مسودة تعميم", "مسودة تعميم", "fa-file-pen"),
    "LabAsset": ("صنف عهدة", "صنف عهدة", "fa-flask"),
    "LabAssetHandover": ("حركة عهدة", "حركة عهدة", "fa-right-left"),
    "LabExperiment": ("تجربة مختبر", "تجربة مختبر", "fa-vials"),
    "Delegation": ("تفويض", "تفويضاً", "fa-handshake-angle"),
    "SchoolYearResetJob": ("تصفير سنة دراسية", "تصفير سنة دراسية", "fa-arrows-rotate"),
    "Auth": ("الحساب", "الحساب", "fa-right-to-bracket"),
    "PlatformSupportSession": ("جلسة صيانة", "جلسة صيانة", "fa-user-shield"),
    "PlatformSupportAction": ("إجراء صيانة", "إجراء صيانة", "fa-screwdriver-wrench"),
    "PersonalWorkspace": ("مساحة معلم مستقل", "مساحة معلم مستقل", "fa-user"),
    "PersonalSubscription": ("اشتراك فردي", "اشتراكاً فردياً", "fa-credit-card"),
    "PersonalReport": ("تقرير شخصي", "تقريراً شخصياً", "fa-file-lines"),
    "PersonalEvidence": ("شاهد شخصي", "شاهداً شخصياً", "fa-paperclip"),
    "PersonalAcademicYear": ("سنة شخصية", "سنة دراسية شخصية", "fa-calendar"),
    "PersonalInitiative": ("مبادرة شخصية", "مبادرة شخصية", "fa-lightbulb"),
    "PersonalPortfolioSection": ("محور ملف إنجاز", "محور ملف إنجاز", "fa-folder-open"),
    "PersonalPortfolioReport": ("ربط تقرير بملف الإنجاز", "ربط تقرير بملف الإنجاز", "fa-link"),
    "PersonalPortfolioEvidence": ("ربط شاهد بملف الإنجاز", "ربط شاهد بملف الإنجاز", "fa-link"),
    "PersonalShareLink": ("مشاركة شخصية", "مشاركة شخصية", "fa-share-nodes"),
}

# ─────────────────────────────────────────────────────────────────────────────
# الأفعال: (الفعل، النغمة، الأيقونة الافتراضية)
# النغمة تُترجَم في القالب إلى لون. الحذف أحمر لأنه الإجراء الوحيد غير القابل
# للتراجع، والدخول/الخروج محايدان لأنهما ضجيج في سياق «ماذا أنجزت».
# ─────────────────────────────────────────────────────────────────────────────
_ACTIONS: dict[str, tuple[str, str, str]] = {
    "create": ("إنشاء", "create", "fa-plus"),
    "update": ("تعديل", "update", "fa-pen"),
    "delete": ("حذف", "delete", "fa-trash"),
    "login": ("تسجيل دخول", "session", "fa-right-to-bracket"),
    "logout": ("تسجيل خروج", "session", "fa-right-from-bracket"),
}

# أحداث الجلسة لا موضوع لها، فجملتها تُصاغ وحدها بلا مفعول.
_SESSION_ACTIONS = {"login", "logout"}


@dataclass(frozen=True)
class AuditEntryView:
    """التمثيل المعروض لصف سجل واحد."""

    headline: str      # «أنشأ تقريراً»
    subject: str       # وصف السجل المتأثر
    tone: str          # create | update | delete | session
    icon: str          # أيقونة Font Awesome بلا البادئة
    model_label: str   # «تقرير» — يُعرض شارةً


def describe(log) -> AuditEntryView:
    """يبني التمثيل المعروض لصف ``AuditLog``.

    مبنيّ على ألا يفشل: أي قيمة غير معروفة تعود بنصها الخام. صفحة سجل تسقط
    لأن موديلاً جديداً لم يُضَف إلى القاموس هي أسوأ من صفحة تعرض ``Foo``.
    """
    action = (getattr(log, "action", "") or "").strip().lower()
    model_name = (getattr(log, "model_name", "") or "").strip()

    if model_name == "PlatformSupportSession":
        event = (getattr(log, "changes", None) or {}).get("event")
        headline = {
            "enter": "بدء جلسة إدارة وصيانة", "exit": "إنهاء جلسة الصيانة",
            "switch": "تبديل مساحة الصيانة", "expire": "انتهاء مدة جلسة الصيانة",
            "target_unavailable": "إغلاق جلسة لمساحة غير متاحة",
        }.get(event, "جلسة إدارة وصيانة")
        return AuditEntryView(headline, log.object_repr, "session", "fa-user-shield", "جلسة صيانة")
    if model_name == "PlatformSupportAction":
        return AuditEntryView("طلب إجراء أثناء الصيانة", log.object_repr, "update", "fa-screwdriver-wrench", "إجراء صيانة")

    verb, tone, action_icon = _ACTIONS.get(action, (action or "إجراء", "update", "fa-circle"))
    singular, accusative, model_icon = _MODELS.get(
        model_name, (model_name or "سجل", model_name or "سجلاً", "fa-circle-dot")
    )

    if action in _SESSION_ACTIONS:
        return AuditEntryView(
            headline=verb,
            subject="",
            tone=tone,
            icon=action_icon,
            model_label="الحساب",
        )

    return AuditEntryView(
        headline=f"{verb} {accusative}",
        subject=(getattr(log, "object_repr", "") or "").strip(),
        tone=tone,
        icon=model_icon,
        model_label=singular,
    )


def attach_views(logs) -> None:
    """يُلحق ``log.ui`` بكل صف في الصفحة الحالية.

    الإلحاق على الكائن بدل بناء قائمة موازية يُبقي كائن الترقيم (``Page``) كما
    هو، فيظل القالب يتعامل مع ``logs.has_next`` وأخواته بلا تغيير.
    """
    for log in logs:
        try:
            log.ui = describe(log)
        except Exception:  # pragma: no cover — الحصانة مقصودة هنا
            log.ui = AuditEntryView("إجراء", "", "update", "fa-circle-dot", "سجل")


def action_filter_choices() -> list[tuple[str, str]]:
    """خيارات التصفية بالترتيب الذي يهم المستخدم: الإنجاز أولاً، الجلسة آخراً."""
    order = ("create", "update", "delete", "login", "logout")
    return [(key, _ACTIONS[key][0]) for key in order if key in _ACTIONS]


def model_filter_choices(names) -> list[dict[str, str]]:
    """Translate distinct stored model names for a readable filter menu."""
    choices = []
    for name in names:
        raw = str(name or "").strip()
        if not raw:
            continue
        label = _MODELS.get(raw, (raw, raw, ""))[0]
        choices.append({"value": raw, "label": label})
    return choices
