"""Explicit school-owned record and original-file scope for the full ZIP export.

This is an offline content export, not a database restore or a credential backup.
Keep the registry explicit: a new model must have its tenant boundary reviewed
before it can be included in a manager's download.
"""
from __future__ import annotations

import json
import re
from pathlib import PurePosixPath

from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.db.models import Q

from . import models as records


EXCLUDED_CONTENT = [
    "بيانات الدخول وكلمات المرور والجلسات ومفاتيح API ووسائل التحقق وروابط المشاركة السرية",
    "مساحات المعلمين الشخصية ومحتويات المدارس الأخرى وبيانات إدارة المنصة العامة",
    "الطلبات التراثية غير المرتبطة بمدرسة؛ لا يمكن إسنادها بأمان إلى مدرسة معينة",
    "ملفات التصدير المؤقتة واشتراكات إشعارات المتصفح وقياسات الذكاء الاصطناعي التشغيلية",
    "بيانات جلسة الدفع وبيانات الدخول أو سياق جلسة الدعم داخل سجل التغييرات",
]

_EXCLUDED_FIELDS = {
    "Teacher": {
        "password", "last_login", "is_superuser", "is_staff", "current_session_key",
        "passkey_prompt_opt_out",
    },
    "ShareLink": {"token"},
    "Payment": {"gateway_checkout_id"},
}

_CREDENTIAL_KEY = re.compile(
    r"password|secret|session|token|api[_-]?key|credential|recovery|passkey|otp|authorization",
    re.IGNORECASE,
)


def _redact_audit_credentials(value, *, path=()):
    """Keep business changes; remove actual support/auth bearer credentials."""
    if isinstance(value, dict):
        return {
            key: "[محجوب: بيانات دخول]"
            if _CREDENTIAL_KEY.search(str(key)) or (path and path[-1] == "support" and key == "context")
            else _redact_audit_credentials(item, path=(*path, str(key).lower()))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_audit_credentials(item, path=path) for item in value]
    return value


def school_record_querysets(school):
    """All years and all states, including inactive members and report trash."""
    sid = school.pk
    scopes = (
        ("School", Q(pk=sid)),
        ("Report", Q(school_id=sid)),
        ("ReportEvidence", Q(report__school_id=sid)),
        ("TeacherAchievementFile", Q(school_id=sid)),
        ("AchievementSection", Q(file__school_id=sid)),
        ("AchievementEvidenceImage", Q(section__file__school_id=sid)),
        ("AchievementEvidenceReport", Q(section__file__school_id=sid)),
        ("SchoolLeadershipPortfolio", Q(school_id=sid)),
        ("LeadershipPortfolioSection", Q(portfolio__school_id=sid)),
        ("LeadershipEvidenceImage", Q(section__portfolio__school_id=sid)),
        ("LeadershipEvidenceReport", Q(section__portfolio__school_id=sid)),
        ("Ticket", Q(school_id=sid)),
        ("TicketImage", Q(ticket__school_id=sid)),
        ("TicketRecipient", Q(ticket__school_id=sid)),
        ("TicketNote", Q(ticket__school_id=sid)),
        ("Notification", Q(school_id=sid)),
        ("NotificationRecipient", Q(notification__school_id=sid)),
        ("CircularDraft", Q(school_id=sid)),
        ("Document", Q(school_id=sid)),
        ("Meeting", Q(school_id=sid)),
        ("MeetingAgendaItem", Q(meeting__school_id=sid)),
        ("MeetingAttendee", Q(meeting__school_id=sid)),
        ("MeetingMinutes", Q(meeting__school_id=sid)),
        ("Decision", Q(meeting__school_id=sid)),
        ("Plan", Q(school_id=sid)),
        ("PlanGoal", Q(plan__school_id=sid)),
        ("PlanTask", Q(plan__school_id=sid)),
        ("Initiative", Q(school_id=sid)),
        ("Assignment", Q(school_id=sid) | Q(targets__school_id=sid)),
        ("AssignmentTarget", Q(school_id=sid) | Q(school__isnull=True, assignment__school_id=sid)),
        ("AssignmentEvidence", Q(target__school_id=sid) | Q(target__school__isnull=True, target__assignment__school_id=sid)),
        ("LabAsset", Q(school_id=sid)),
        ("LabAssetHandover", Q(school_id=sid)),
        ("LabExperiment", Q(school_id=sid)),
        ("SchoolMembership", Q(school_id=sid)),
        ("Teacher", Q(school_memberships__school_id=sid)),
        ("Department", Q(school_id=sid)),
        ("DepartmentMembership", Q(department__school_id=sid)),
        ("StaffScope", Q(membership__school_id=sid)),
        ("Delegation", Q(school_id=sid)),
        ("ReportType", Q(school_id=sid)),
        ("TeacherPrivateComment", Q(school_id=sid)),
        ("ApprovalTransition", Q(school_id=sid)),
        ("AuditLog", Q(school_id=sid)),
        ("ShareLink", Q(school_id=sid)),
        ("SchoolSubscription", Q(school_id=sid)),
        ("SchoolArchiveAddon", Q(school_id=sid)),
        ("Payment", Q(school_id=sid)),
        ("DiscountRedemption", Q(school_id=sid)),
        ("SchoolYearArchive", Q(school_id=sid)),
        ("SchoolYearArchiveDownload", Q(archive__school_id=sid)),
    )
    for model_name, scope in scopes:
        model = getattr(records, model_name)
        manager = model.all_objects if model_name == "Report" else model.objects
        yield model_name, manager.filter(scope).distinct().order_by("pk")


def _record_data(obj, school):
    excluded = _EXCLUDED_FIELDS.get(type(obj).__name__, set())
    data = {}
    for field in obj._meta.concrete_fields:
        if field.name in excluded:
            continue
        value = getattr(obj, field.attname)
        if isinstance(obj, records.AuditLog) and field.name == "changes":
            value = _redact_audit_credentials(value)
        data[field.attname] = (value.name or "") if isinstance(field, models.FileField) else value
    # Explicit safe through-relations; avoid serializing user permission groups
    # or records from sibling schools through a malformed M2M association.
    if isinstance(obj, records.StaffScope):
        data["departments"] = list(obj.departments.filter(school=school).values_list("pk", flat=True))
    if isinstance(obj, records.LabExperiment):
        data["assets"] = list(obj.assets.filter(school=school).values_list("pk", flat=True))
    return data


def school_record_json_chunks(queryset, school):
    """Stream a valid JSON array without loading a school's history into RAM."""
    yield b"["
    first = True
    for obj in queryset.iterator(chunk_size=200):
        if not first:
            yield b",\n"
        yield json.dumps(_record_data(obj, school), ensure_ascii=False, cls=DjangoJSONEncoder).encode("utf-8")
        first = False
    yield b"]\n"


def school_original_file_fields(school):
    """Discover every stored file on registered school-owned records.

    This fills gaps in the human-readable annual layout: signatures, receipts,
    saved annual ZIPs, legacy report images and files in the report trash.
    Each path carries model + record + field identity to avoid name collisions.
    """
    for model_name, queryset in school_record_querysets(school):
        fields = [field for field in queryset.model._meta.concrete_fields if isinstance(field, models.FileField)]
        if not fields:
            continue
        for obj in queryset.iterator(chunk_size=100):
            for field in fields:
                value = getattr(obj, field.name)
                if not value.name:
                    continue
                base = PurePosixPath(value.name.replace("\\", "/")).name
                base = re.sub(r'[\x00-\x1f\\/:*?"<>|]', "_", base).strip(" .")[:100] or "file.bin"
                yield f"الملفات-الأصلية/{model_name}/{obj.pk}/{field.name}/{base}", value


def field_identity(field):
    return (id(field.storage), field.name)


def missing_file_details(archive_path, field):
    return {
        "archive_path": archive_path,
        "model": field.instance._meta.label,
        "record_id": field.instance.pk,
        "field": field.field.name,
        "stored_name": field.name,
    }
