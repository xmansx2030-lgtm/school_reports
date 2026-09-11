# reports/models.py
# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import timedelta
from typing import Optional, TYPE_CHECKING
import secrets
import os

from urllib.parse import quote

from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, FileExtensionValidator
from django.db import models, transaction
from django.db.models.signals import post_migrate, post_save
from django.dispatch import receiver
from django.utils.text import slugify
from django.utils import timezone

# تخزين المرفقات (R2 أو محلي)
from ..storage import PublicRawMediaStorage
from ..school_storage import school_file_path, school_or_platform_file_path
from ..validators import validate_attachment_file, validate_circular_attachment_file, validate_image_file, validate_pdf_file

if TYPE_CHECKING:
    from .achievements import AchievementEvidenceImage, TeacherAchievementFile
    from .billing import Payment
    from .notifications import Notification, TicketImage
    from .reports import Report, ReportEvidence
    from .schools import School
    from .tickets import Ticket

# =========================
# ثوابت عامة
# =========================
MANAGER_SLUG = "manager"
MANAGER_NAME = "الإدارة"
MANAGER_ROLE_LABEL = "المدير"


def _normalize_academic_year_hijri(value: str) -> str:
    """تطبيع السنة الدراسية الهجرية بصيغة YYYY-YYYY (مثل 1447-1448)."""
    v = (value or "").strip()
    return v.replace("–", "-").replace("—", "-")


def _validate_academic_year_hijri(value: str) -> None:
    """يتحقق من الصيغة 1447-1448 وأن السنة الثانية = الأولى + 1."""
    import re

    v = _normalize_academic_year_hijri(value)
    if not re.fullmatch(r"\d{4}-\d{4}", v):
        raise ValidationError("صيغة السنة الدراسية يجب أن تكون مثل 1447-1448")
    start, end = v.split("-", 1)
    try:
        s, e = int(start), int(end)
    except Exception as exc:
        raise ValidationError("صيغة السنة الدراسية غير صحيحة") from exc
    if e != s + 1:
        raise ValidationError("السنة الدراسية يجب أن تكون مثل 1447-1448 (فرق سنة واحدة)")


def _achievement_pdf_upload_to(instance: "TeacherAchievementFile", filename: str) -> str:
    year = _normalize_academic_year_hijri(getattr(instance, "academic_year", "")) or "unknown"
    return school_file_path(
        instance.school,
        "achievements/pdfs",
        filename,
        parts=(year, f"teacher-{instance.teacher_id or 'unknown'}"),
        fallback="achievement.pdf",
    )


def _achievement_evidence_upload_to(instance: "AchievementEvidenceImage", filename: str) -> str:
    try:
        year = _normalize_academic_year_hijri(instance.section.file.academic_year)
    except Exception:
        year = "unknown"
    achievement_file = instance.section.file
    return school_file_path(
        achievement_file.school,
        "achievements/evidence",
        filename,
        parts=(year, f"section-{instance.section.code}", f"teacher-{achievement_file.teacher_id}"),
        fallback="evidence",
    )


def _leadership_evidence_upload_to(instance, filename: str) -> str:
    try:
        portfolio = instance.section.portfolio
        year = _normalize_academic_year_hijri(portfolio.academic_year) or "unknown"
        section_code = instance.section.code
    except Exception:
        year, section_code = "unknown", "section"
        portfolio = None
    return school_file_path(
        getattr(portfolio, "school", None),
        "leadership/evidence",
        filename,
        parts=(year, f"section-{section_code}"),
        fallback="evidence",
    )


def _payment_receipt_upload_to(instance: "Payment", filename: str) -> str:
    """مسار رفع صورة إيصال الدفع"""
    return school_file_path(instance.school, "payments/receipts", filename, fallback="receipt")


def _report_image_upload_to(instance: "Report", filename: str) -> str:
    """مسار رفع صور التقرير"""
    teacher_id = getattr(instance, "teacher_id", None) or "unknown"
    return school_or_platform_file_path(
        getattr(instance, "school", None),
        "reports/images",
        filename,
        parts=(f"teacher-{teacher_id}",),
        fallback="image",
    )


def _report_evidence_upload_to(instance: "ReportEvidence", filename: str) -> str:
    """مسار شاهد التقرير عبر صاحب التقرير الأب."""
    report = instance.report
    teacher_id = getattr(report, "teacher_id", None) or "unknown"
    return school_or_platform_file_path(
        getattr(report, "school", None),
        "reports/evidence",
        filename,
        parts=(f"teacher-{teacher_id}",),
        fallback="evidence",
    )


def _ticket_attachment_upload_to(instance: "Ticket", filename: str) -> str:
    """مسار رفع مرفقات التذاكر"""
    return school_or_platform_file_path(
        getattr(instance, "school", None),
        "tickets/attachments",
        filename,
        fallback="attachment",
    )


def _notification_attachment_upload_to(instance: "Notification", filename: str) -> str:
    """مسار رفع مرفقات الإشعارات/التعاميم"""
    return school_or_platform_file_path(
        getattr(instance, "school", None),
        "notifications/attachments",
        filename,
        fallback="attachment",
    )


# NOTE: kept for historical migrations that referenced it.
def _school_logo_upload_to(instance: "School", filename: str) -> str:
    """مسار رفع شعار المدرسة (legacy)."""
    return school_file_path(instance, "branding/logos", filename, fallback="logo")

def _ticket_image_upload_to(instance: "TicketImage", filename: str) -> str:
    """مسار رفع صور التذاكر"""
    return school_or_platform_file_path(
        getattr(instance.ticket, "school", None),
        "tickets/images",
        filename,
        fallback="image",
    )


# =========================
# المدرسة (Tenant)


# Make star imports include migration-facing helpers whose names start with "_".
__all__ = [name for name in globals() if not name.startswith("__")]
