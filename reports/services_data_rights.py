# reports/services_data_rights.py
# -*- coding: utf-8 -*-
"""حقوق صاحب البيانات: النسخة المقروءة، وطلب الإتلاف.

سياسة الخصوصية في المنصة تَعِد صراحةً بـ«الوصول، وطلب نسخة مقروءة… وطلب
الإتلاف في الحالات المقررة» عملاً بنظام حماية البيانات الشخصية السعودي. وكان
الوفاء بالوعد يمرّ بنموذج شكاوى ورسالة بريد ومعالجةٍ يدوية. هذه الوحدة تجعل
الأول فورياً، والثاني مسجَّلاً لا مُهمَلاً.

── ما يدخل النسخة ─────────────────────────────────────────────────────────
كل ما هو **عن صاحب الطلب**: ملفّه، وعضوياته، وما أنشأه أو كُلِّف به أو وصله.
والقاعدة في كل استعلام أن مفتاح المستخدم مثبَّتٌ في الشرط، لا مشتقٌّ من معطى
في الطلب — فليس في هذا المسار معامل يمكن التلاعب به للوصول إلى نسخة غيره.

── وما لا يدخلها، عمداً ───────────────────────────────────────────────────
**الأسرار ليست بيانات شخصية تُسلَّم.** ثلاثة أصناف مستثناة لأن تسليمها يخلق
الخطر الذي يُفترض أن يحمي منه هذا الحق:

* ``password`` و``current_session_key`` — تسليمهما تسليمُ الحساب نفسه.
* مادة WebAuthn (مُعرِّف الاعتماد والمفتاح العام) — تُسلَّم أسماء الأجهزة
  وتواريخها، لا ما يُصادَق به.
* عنوان اشتراك الدفع ومفاتيحه (``endpoint`` و``auth`` و``p256dh``) — من يملكها
  يستطيع دفعَ إشعارات إلى جهاز المستخدم. تُسلَّم حقيقةُ وجود اشتراك وتاريخه.

والملفات المرفوعة لا تُحزَم في النسخة: تُدرَج أسماؤها وأحجامها وروابطها داخل
المنصة. حزمُ عشرات الميغابايتات في طلب HTTP واحد يُسقط العامل، وللأرشفة
مسارُها المخصَّص الذي يعمل في الخلفية.

── التعليقات الخاصة: قرارٌ يستحق التصريح ──────────────────────────────────
``TeacherPrivateComment`` تعليقٌ يكتبه المدير **عن** المعلّم. وهو بيانات شخصية
عن صاحب الطلب بلا شك، لكنه في الوقت نفسه تقييمٌ إداري يخص طرفاً آخر ويحمل
رأيه. والنظام يُقيّد حق الوصول حين «يحمي حقوق شخص آخر» — وهو نصُّ سياسة
المنصة نفسها.

فالحل هنا وسط، وهو الوسط الصحيح: تُدرَج **حقيقةُ وجودها وعددُها وتواريخها**،
ولا يُدرَج نصُّها ولا كاتبُها. فصاحب البيانات يعلم أن عنه ملاحظات وكم هي ومتى
كُتبت — وهو جوهر «حق العلم» — ويبقى طلبُ النصّ مساراً يمرّ بمن يوازن الحقين.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from django.urls import reverse
from django.utils import timezone

from core.observability import soft_call


def _iso(value) -> str | None:
    if not value:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _file_reference(field) -> dict[str, Any] | None:
    """وصفُ ملفٍ لا محتواه: اسمُه وحجمُه وأين يُفتح."""
    name = getattr(field, "name", "") or ""
    if not name:
        return None
    return {
        "name": name.rsplit("/", 1)[-1],
        "path": name,
        "size_bytes": soft_call("data_rights.file_size", lambda: field.size, default=None),
    }


# ─────────────────────────────────────────────────────────────────────────
# أقسام النسخة
# ─────────────────────────────────────────────────────────────────────────
def _profile_section(user) -> dict[str, Any]:
    return {
        "name": user.name,
        "phone": user.phone,
        "email": user.email or None,
        "national_id": user.national_id or None,
        "date_joined": _iso(user.date_joined),
        "last_login": _iso(user.last_login),
        "is_active": bool(user.is_active),
    }


def _memberships_section(user) -> list[dict[str, Any]]:
    from .models import SchoolGroupMembership, SchoolMembership

    rows = (
        SchoolMembership.objects.filter(teacher=user)
        .select_related("school")
        .order_by("school__name", "id")
    )
    memberships = [
        {
            "membership_type": "school",
            "school": getattr(row.school, "name", None),
            "role": row.get_role_type_display(),
            "job_title": row.get_job_title_display() if row.job_title else None,
            "is_active": bool(row.is_active),
            "created_at": _iso(getattr(row, "created_at", None)),
        }
        for row in rows
    ]
    group_rows = (
        SchoolGroupMembership.objects.filter(user=user)
        .select_related("group")
        .order_by("group__name", "id")
    )
    memberships.extend(
        {
            "membership_type": "school_group",
            "group": getattr(row.group, "name", None),
            "role": row.get_role_type_display(),
            "is_active": bool(row.is_active),
            "created_at": _iso(row.created_at),
        }
        for row in group_rows
    )
    return memberships


def _reports_section(user) -> list[dict[str, Any]]:
    from .models import Report

    rows = (
        Report.objects.filter(teacher=user)
        .select_related("school", "category")
        .order_by("-report_date", "-id")
    )
    return [
        {
            "id": row.pk,
            "title": row.title,
            "school": getattr(row.school, "name", None),
            "category": getattr(row.category, "name", None),
            "report_date": _iso(row.report_date),
            "academic_year": row.academic_year or None,
            "idea": row.idea or None,
            "goal": getattr(row, "goal", None) or None,
            "results": getattr(row, "results", None) or None,
            "url": reverse("reports:report_print", args=[row.pk]),
        }
        for row in rows
    ]


def _tickets_section(user) -> list[dict[str, Any]]:
    from .models import Ticket, TicketNote

    tickets = (
        Ticket.objects.filter(creator=user)
        .select_related("school")
        .order_by("-created_at", "-id")
    )
    payload = [
        {
            "id": row.pk,
            "title": row.title,
            "body": row.body or None,
            "status": row.get_status_display(),
            "school": getattr(row.school, "name", None),
            "created_at": _iso(row.created_at),
        }
        for row in tickets
    ]
    notes = (
        TicketNote.objects.filter(author=user)
        .select_related("ticket")
        .order_by("-created_at", "-id")
    )
    return {
        "created": payload,
        "notes_written": [
            {
                "ticket_id": note.ticket_id,
                "body": note.body,
                "created_at": _iso(note.created_at),
            }
            for note in notes
        ],
    }


def _notifications_section(user) -> list[dict[str, Any]]:
    from .models import NotificationRecipient

    rows = (
        NotificationRecipient.objects.filter(teacher=user)
        .select_related("notification")
        .order_by("-created_at", "-id")
    )
    return [
        {
            "title": getattr(row.notification, "title", None),
            "is_circular": bool(getattr(row.notification, "requires_signature", False)),
            "received_at": _iso(row.created_at),
            "is_read": bool(row.is_read),
            "read_at": _iso(row.read_at),
            "is_signed": bool(getattr(row, "is_signed", False)),
            "signed_at": _iso(getattr(row, "signed_at", None)),
        }
        for row in rows
    ]


def _assignments_section(user) -> list[dict[str, Any]]:
    from .models import AssignmentTarget

    rows = (
        AssignmentTarget.objects.filter(assignee=user)
        .select_related("assignment")
        .order_by("-id")
    )
    return [
        {
            "assignment": getattr(row.assignment, "title", None),
            "due_at": _iso(getattr(row.assignment, "due_at", None)),
            "state": row.get_approval_state_display()
            if hasattr(row, "get_approval_state_display")
            else None,
        }
        for row in rows
    ]


def _achievements_section(user) -> list[dict[str, Any]]:
    from .models import TeacherAchievementFile

    rows = TeacherAchievementFile.objects.filter(teacher=user).order_by("-id")
    return [
        {
            "academic_year": row.academic_year,
            "pdf": _file_reference(getattr(row, "pdf_file", None)),
            "generated_at": _iso(getattr(row, "pdf_generated_at", None)),
        }
        for row in rows
    ]


def _documents_section(user) -> list[dict[str, Any]]:
    from .models import Document

    rows = Document.objects.filter(owner=user).order_by("-created_at", "-id")
    return [
        {
            "title": row.title,
            "description": row.description or None,
            "academic_year": row.academic_year or None,
            "created_at": _iso(row.created_at),
            "file": _file_reference(getattr(row, "file", None)),
        }
        for row in rows
    ]


def _activity_section(user, *, limit: int = 2000) -> list[dict[str, Any]]:
    from .models import AuditLog

    # ``AuditLog`` يسمّي عمود الوقت ``timestamp`` لا ``created_at``.
    rows = (
        AuditLog.objects.filter(teacher=user)
        .select_related("school")
        .order_by("-timestamp", "-id")[:limit]
    )
    return [
        {
            "action": row.get_action_display(),
            "model": row.model_name,
            "object": row.object_repr,
            "school": getattr(row.school, "name", None),
            "at": _iso(row.timestamp),
        }
        for row in rows
    ]


def _security_section(user) -> dict[str, Any]:
    """وجودُ وسائل الدخول وتواريخُها — لا موادُّها.

    راجع تعليل الاستثناء أعلى الملف: تسليم مادة الاعتماد أو مفاتيح الدفع
    يخلق الخطر الذي جاء حق الوصول ليحمي منه.
    """
    from .models import TeacherTotpDevice, WebAuthnCredential, WebPushSubscription

    passkeys = WebAuthnCredential.objects.filter(teacher=user).order_by("-created_at")
    subscriptions = WebPushSubscription.objects.filter(teacher=user).order_by("-created_at")
    totp = TeacherTotpDevice.objects.filter(teacher=user).first()
    return {
        "passkeys": [
            {
                "device_name": row.device_name or None,
                "is_active": bool(row.is_active),
                "created_at": _iso(row.created_at),
                "last_used_at": _iso(row.last_used_at),
            }
            for row in passkeys
        ],
        "push_subscriptions": [
            {
                "created_at": _iso(row.created_at),
                "is_active": bool(getattr(row, "is_active", True)),
            }
            for row in subscriptions
        ],
        "two_factor_authentication": (
            {
                "is_confirmed": bool(totp.is_confirmed),
                "created_at": _iso(totp.created_at),
                "confirmed_at": _iso(totp.confirmed_at),
                "last_used_at": _iso(totp.last_used_at),
                "recovery_codes_available": totp.recovery_codes.filter(used_at__isnull=True).count(),
                "recovery_codes_used": totp.recovery_codes.filter(used_at__isnull=False).count(),
            }
            if totp
            else None
        ),
    }


def _circular_drafts_section(user) -> list[dict[str, Any]]:
    from .models import CircularDraft

    rows = (
        CircularDraft.objects.filter(owner=user)
        .select_related("school", "department")
        .order_by("-created_at", "-id")
    )
    return [
        {
            "id": row.pk,
            "title": row.title,
            "body": row.body,
            "school": getattr(row.school, "name", None),
            "audience": row.get_audience_display(),
            "department": getattr(row.department, "name", None),
            "requires_signature": bool(row.requires_signature),
            "signature_deadline_at": _iso(row.signature_deadline_at),
            "approval_state": row.get_approval_state_display(),
            "published_at": _iso(row.published_at),
            "created_at": _iso(row.created_at),
            "updated_at": _iso(row.updated_at),
        }
        for row in rows
    ]


def _plans_section(user) -> list[dict[str, Any]]:
    from .models import Plan

    rows = (
        Plan.objects.filter(owner=user)
        .select_related("school", "group")
        .prefetch_related("tasks")
        .order_by("-created_at", "-id")
    )
    return [
        {
            "id": row.pk,
            "title": row.title,
            "description": row.description or None,
            "scope": row.get_scope_display(),
            "school": getattr(row.school, "name", None),
            "group": getattr(row.group, "name", None),
            "academic_year": row.academic_year or None,
            "starts_on": _iso(row.starts_on),
            "ends_on": _iso(row.ends_on),
            "stage": row.get_stage_display(),
            "approval_state": row.get_approval_state_display(),
            "progress_percent": row.progress_percent,
            "created_at": _iso(row.created_at),
            "updated_at": _iso(row.updated_at),
        }
        for row in rows
    ]


def _initiatives_section(user) -> list[dict[str, Any]]:
    from .models import Initiative

    rows = (
        Initiative.objects.filter(teacher=user)
        .select_related("school", "plan")
        .order_by("-created_at", "-id")
    )
    return [
        {
            "id": row.pk,
            "title": row.title,
            "summary": row.summary or None,
            "school": getattr(row.school, "name", None),
            "plan": getattr(row.plan, "title", None),
            "is_best_practice": bool(row.is_best_practice),
            "approval_state": row.get_approval_state_display(),
            "shared_at": _iso(row.shared_at),
            "created_at": _iso(row.created_at),
        }
        for row in rows
    ]


def _ai_usage_section(user) -> list[dict[str, Any]]:
    from .models import AiUsageEvent

    rows = AiUsageEvent.objects.filter(teacher=user).select_related("school").order_by("-created_at", "-id")
    return [
        {
            "stage": row.get_stage_display(),
            "model_name": row.model_name or None,
            "outcome": row.get_outcome_display(),
            "error_kind": row.error_kind or None,
            "school": getattr(row.school, "name", None),
            "input_tokens": row.input_tokens,
            "cached_input_tokens": row.cached_input_tokens,
            "output_tokens": row.output_tokens,
            "reasoning_tokens": row.reasoning_tokens,
            "duration_ms": row.duration_ms,
            "estimated_cost_usd": str(row.estimated_cost) if row.estimated_cost is not None else None,
            "created_at": _iso(row.created_at),
        }
        for row in rows
    ]


def _approval_actions_section(user) -> list[dict[str, Any]]:
    from .models import ApprovalTransition

    rows = (
        ApprovalTransition.objects.filter(actor=user)
        .select_related("content_type", "school")
        .order_by("-created_at", "-id")
    )
    return [
        {
            "record_type": row.content_type.name,
            "record_id": row.object_id,
            "school": getattr(row.school, "name", None),
            "action": row.get_action_display(),
            "acted_as": row.get_acted_as_display(),
            "from_state": row.from_state or None,
            "to_state": row.to_state or None,
            "note": row.note or None,
            "created_at": _iso(row.created_at),
        }
        for row in rows
    ]


def _erasure_requests_section(user) -> list[dict[str, Any]]:
    from .models import ErasureRequest

    return [
        {
            "id": row.pk,
            "reason": row.reason or None,
            "status": row.get_status_display(),
            "response_note": row.response_note or None,
            "created_at": _iso(row.created_at),
            "response_due_at": _iso(row.response_due_at),
            "extended_until": _iso(row.extended_until),
            "extension_reason": row.extension_reason or None,
            "resolved_at": _iso(row.resolved_at),
        }
        for row in ErasureRequest.objects.filter(teacher=user).order_by("-created_at", "-id")
    ]


def _notes_about_me_section(user) -> dict[str, Any]:
    """حقيقةُ وجود ملاحظات إدارية عن صاحب الطلب — بلا نصّها.

    راجع التعليل أعلى الملف: النصّ رأيُ طرفٍ آخر، والنظام يُقيّد الوصول حين
    يحمي حقوق شخص آخر. والعلمُ بوجودها وعددها وتواريخها هو جوهر «حق العلم».
    """
    from .models import TeacherPrivateComment

    rows = TeacherPrivateComment.objects.filter(teacher=user).order_by("-created_at")
    return {
        "count": rows.count(),
        "dates": [_iso(row.created_at) for row in rows[:200]],
        "note": (
            "نصّ هذه الملاحظات لا يُسلَّم آلياً لأنها تحمل رأي طرف آخر. "
            "لطلب الاطلاع عليها، استخدم نموذج الشكاوى."
        ),
    }


SECTIONS = (
    ("profile", _profile_section),
    ("memberships", _memberships_section),
    ("reports", _reports_section),
    ("tickets", _tickets_section),
    ("notifications", _notifications_section),
    ("assignments", _assignments_section),
    ("achievement_files", _achievements_section),
    ("documents", _documents_section),
    ("circular_drafts", _circular_drafts_section),
    ("plans", _plans_section),
    ("initiatives", _initiatives_section),
    ("approval_actions", _approval_actions_section),
    ("ai_usage", _ai_usage_section),
    ("erasure_requests", _erasure_requests_section),
    ("notes_about_me", _notes_about_me_section),
    ("security", _security_section),
    ("activity_log", _activity_section),
)


SECTION_LABELS = {
    "profile": "الملف الشخصي",
    "memberships": "العضويات والأدوار",
    "reports": "التقارير",
    "tickets": "طلبات الدعم والملاحظات",
    "notifications": "الإشعارات والتعاميم",
    "assignments": "التكليفات",
    "achievement_files": "ملفات الإنجاز",
    "documents": "الوثائق",
    "circular_drafts": "مسودات التعاميم",
    "plans": "الخطط",
    "initiatives": "المبادرات",
    "approval_actions": "إجراءات الاعتماد",
    "ai_usage": "استخدام مزايا الذكاء الاصطناعي",
    "erasure_requests": "طلبات إتلاف البيانات",
    "notes_about_me": "الملاحظات الإدارية عني",
    "security": "أمان الحساب والأجهزة",
    "activity_log": "سجل الإجراءات",
}

FIELD_LABELS = {
    "name": "الاسم", "phone": "رقم الجوال", "email": "البريد الإلكتروني",
    "national_id": "رقم الهوية", "date_joined": "تاريخ الانضمام", "last_login": "آخر دخول",
    "is_active": "نشط", "membership_type": "نوع العضوية", "school": "المدرسة",
    "group": "مجموعة المدارس", "role": "الدور", "job_title": "المسمى الوظيفي",
    "created_at": "تاريخ الإنشاء", "updated_at": "آخر تحديث", "id": "المعرّف",
    "title": "العنوان", "body": "النص", "description": "الوصف", "category": "التصنيف",
    "report_date": "تاريخ التقرير", "academic_year": "العام الدراسي", "idea": "الفكرة",
    "goal": "الهدف", "results": "النتائج", "url": "الرابط", "status": "الحالة",
    "received_at": "تاريخ الاستلام", "is_read": "تمت القراءة", "read_at": "تاريخ القراءة",
    "is_signed": "تم التوقيع", "signed_at": "تاريخ التوقيع", "due_at": "موعد الإنجاز",
    "state": "الحالة", "reason": "سبب الطلب", "response_note": "رد المنصة",
    "response_due_at": "موعد الرد", "extended_until": "الموعد بعد التمديد",
    "extension_reason": "سبب التمديد", "resolved_at": "تاريخ البت",
    "action": "الإجراء", "model": "نوع السجل", "object": "السجل", "at": "الوقت",
    "audience": "الفئة المستهدفة", "department": "القسم", "requires_signature": "يتطلب توقيعاً",
    "signature_deadline_at": "مهلة التوقيع", "approval_state": "حالة الاعتماد",
    "published_at": "تاريخ النشر", "scope": "النطاق", "starts_on": "تاريخ البدء",
    "ends_on": "تاريخ الانتهاء", "stage": "المرحلة", "progress_percent": "نسبة الإنجاز",
    "summary": "الملخص", "plan": "الخطة", "is_best_practice": "ممارسة ناجحة",
    "shared_at": "تاريخ المشاركة", "record_type": "نوع السجل", "record_id": "معرّف السجل",
    "acted_as": "صفة التنفيذ", "from_state": "من حالة", "to_state": "إلى حالة",
    "model_name": "النموذج", "outcome": "النتيجة", "error_kind": "نوع الخطأ",
    "input_tokens": "رموز الإدخال", "cached_input_tokens": "رموز الإدخال المخزنة",
    "output_tokens": "رموز الإخراج", "reasoning_tokens": "رموز التفكير",
    "duration_ms": "المدة بالمللي ثانية", "estimated_cost_usd": "التكلفة التقديرية بالدولار",
    "count": "العدد", "dates": "التواريخ", "note": "ملاحظة", "device_name": "اسم الجهاز",
    "last_used_at": "آخر استخدام", "is_confirmed": "مفعّل", "confirmed_at": "تاريخ التفعيل",
    "recovery_codes_available": "رموز الاسترجاع المتاحة", "recovery_codes_used": "رموز الاسترجاع المستخدمة",
    "size_bytes": "الحجم بالبايت", "path": "مسار الملف", "file": "الملف", "pdf": "ملف PDF",
    "generated_at": "تاريخ التجهيز", "created": "طلبات أنشأتها", "notes_written": "ملاحظات كتبتها",
    "passkeys": "مفاتيح المرور", "push_subscriptions": "اشتراكات الإشعارات",
    "two_factor_authentication": "المصادقة الثنائية", "is_circular": "تعميم",
}


def _display_value(value: Any) -> str:
    if value is True:
        return "نعم"
    if value is False:
        return "لا"
    if value in (None, ""):
        return "—"
    if isinstance(value, list):
        return "، ".join(_display_value(item) for item in value) if value else "—"
    if isinstance(value, str) and "T" in value:
        try:
            parsed = datetime.fromisoformat(value)
            if timezone.is_aware(parsed):
                parsed = timezone.localtime(parsed)
            return parsed.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            pass
    return str(value)


VALUE_LABELS = {
    "membership_type": {"school": "مدرسة", "school_group": "مجموعة مدارس"},
    "from_state": {
        "draft": "مسودة", "pending": "قيد المراجعة", "in_review": "قيد الدراسة",
        "needs_info": "يحتاج استكمالاً", "recommended": "موصى باعتماده",
        "returned": "معاد للملاحظة", "approved": "معتمد",
    },
    "to_state": {
        "draft": "مسودة", "pending": "قيد المراجعة", "in_review": "قيد الدراسة",
        "needs_info": "يحتاج استكمالاً", "recommended": "موصى باعتماده",
        "returned": "معاد للملاحظة", "approved": "معتمد",
    },
}


def _display_field(key: str, value: Any) -> str:
    return VALUE_LABELS.get(key, {}).get(value, _display_value(value))


def _flatten_fields(data: dict[str, Any], prefix: str = "") -> list[dict[str, str]]:
    fields: list[dict[str, str]] = []
    for key, value in data.items():
        label = FIELD_LABELS.get(key, key.replace("_", " "))
        full_label = f"{prefix} — {label}" if prefix else label
        if isinstance(value, dict):
            fields.extend(_flatten_fields(value, full_label))
        elif not isinstance(value, list):
            fields.append({"label": full_label, "value": _display_field(key, value)})
    return fields


def _direct_fields(data: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {"label": FIELD_LABELS.get(key, key.replace("_", " ")), "value": _display_field(key, value)}
        for key, value in data.items()
        if not isinstance(value, (dict, list))
    ]


def build_readable_sections(export: dict[str, Any]) -> list[dict[str, Any]]:
    """تهيئة النسخة التقنية إلى بطاقات عربية سهلة القراءة والطباعة."""
    output: list[dict[str, Any]] = []
    for section_name, section_value in export.get("sections", {}).items():
        records: list[dict[str, Any]] = []
        if isinstance(section_value, list):
            records = [{"title": f"سجل {index}", "fields": _flatten_fields(item)} for index, item in enumerate(section_value, 1)]
        elif isinstance(section_value, dict):
            scalar_fields = _direct_fields(section_value)
            if scalar_fields:
                records.append({"title": "البيانات", "fields": scalar_fields})
            for child_key, child_value in section_value.items():
                if isinstance(child_value, list):
                    child_label = FIELD_LABELS.get(child_key, child_key.replace("_", " "))
                    for index, item in enumerate(child_value, 1):
                        fields = _flatten_fields(item) if isinstance(item, dict) else [{"label": child_label, "value": _display_value(item)}]
                        records.append({"title": f"{child_label} · {index}", "fields": fields})
                elif isinstance(child_value, dict):
                    records.append({"title": FIELD_LABELS.get(child_key, child_key), "fields": _flatten_fields(child_value)})
        output.append({
            "key": section_name,
            "title": SECTION_LABELS.get(section_name, section_name.replace("_", " ")),
            "count": len(records),
            "records": records,
        })
    return output

# مفاتيح لا يجوز أن تظهر في النسخة مهما تغيّر الكود. يحرسها اختبار صريح.
FORBIDDEN_KEYS = frozenset(
    {
        "password",
        "current_session_key",
        "credential_id",
        "credential_id_hash",
        "public_key",
        "public_key_cose",
        "endpoint",
        "auth",
        "p256dh",
        "session_key",
        "secret",
        "token",
    }
)


def build_personal_data_export(user) -> dict[str, Any]:
    """النسخة الكاملة لصاحب الطلب.

    تعثّرُ قسمٍ لا يُسقط النسخة كلها: يُسجَّل باسمه ويُسلَّم الباقي مع بيانٍ
    بما نقص. ونسخةٌ ناقصة **مُعلَنة** أفضل من صفحة خطأ تترك صاحب الحق بلا شيء.
    """
    export: dict[str, Any] = {
        "generated_at": _iso(timezone.now()),
        "subject": user.name,
        "notice": (
            "هذه نسخة من بياناتك الشخصية في منصة توثيق، وفق نظام حماية البيانات "
            "الشخصية. لا تتضمّن كلمات المرور ولا مفاتيح المصادقة ولا مفاتيح "
            "الإشعارات — تسليمها يعرّض حسابك للخطر."
        ),
        "sections": {},
        "incomplete_sections": [],
    }

    for name, builder in SECTIONS:
        sentinel = object()
        value = soft_call(
            f"data_rights.section.{name}",
            lambda b=builder: b(user),
            default=sentinel,
            user_id=user.pk,
        )
        if value is sentinel:
            export["incomplete_sections"].append(name)
            continue
        export["sections"][name] = value

    return export
