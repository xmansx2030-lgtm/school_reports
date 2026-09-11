# reports/services_achievement.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import os
from typing import Optional

from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from .models import (
    AchievementEvidenceReport,
    AchievementSection,
    Report,
    School,
    TeacherAchievementFile,
)


def ensure_achievement_sections(achievement_file: TeacherAchievementFile) -> None:
    """Create only the missing canonical sections for an achievement file."""
    existing_codes = set(
        AchievementSection.objects.filter(file=achievement_file).values_list("code", flat=True)
    )
    missing_sections = [
        AchievementSection(file=achievement_file, code=int(code), title=str(title))
        for code, title in AchievementSection.Code.choices
        if int(code) not in existing_codes
    ]
    if missing_sections:
        AchievementSection.objects.bulk_create(missing_sections)


def achievement_picker_reports_qs(*, teacher, active_school: Optional[School], q: str):
    qs = Report.objects.select_related("category").filter(teacher=teacher)
    if active_school is not None:
        qs = qs.filter(school=active_school)
    q = (q or "").strip()
    if q:
        from .search_utils import smart_search_q, REPORT_SEARCH_FIELDS

        search_q = smart_search_q(q, REPORT_SEARCH_FIELDS)
        if search_q:
            qs = qs.filter(search_q).distinct()
    return qs.order_by("-report_date", "-id")


def add_report_evidence(*, section: AchievementSection, report: Report) -> AchievementEvidenceReport:
    obj, _ = AchievementEvidenceReport.objects.get_or_create(section=section, report=report)
    return obj


def remove_report_evidence(*, section: AchievementSection, evidence_id: int) -> bool:
    deleted, _ = AchievementEvidenceReport.objects.filter(pk=evidence_id, section=section).delete()
    return bool(deleted)


def _safe_ext(path: str) -> str:
    try:
        ext = os.path.splitext(path or "")[1].lower()
    except Exception:
        ext = ""
    if not ext or len(ext) > 10:
        return ".img"
    return ext


def _copy_field_to_archived(*, src_field, dest_field, filename: str) -> None:
    if not src_field:
        return
    try:
        if not getattr(src_field, "name", None):
            return

        src_field.open("rb")
        data = src_field.read()
        if not data:
            return
        dest_field.save(filename, ContentFile(data), save=False)
    except Exception:
        # snapshot is best-effort; we still freeze textual data
        return
    finally:
        try:
            src_field.close()
        except Exception:
            pass


def freeze_achievement_report_evidences(*, ach_file: TeacherAchievementFile) -> int:
    """Freeze all linked reports for an achievement file.

    Copies report fields + archives the report images into the achievement file evidence.
    Returns number of frozen evidence items.
    """

    now = timezone.now()
    qs = (
        AchievementEvidenceReport.objects.select_related(
            "section",
            "section__file",
            "report",
            "report__category",
            "report__teacher",
        )
        .filter(section__file=ach_file, frozen_at__isnull=True)
        .order_by("id")
    )

    frozen_count = 0

    with transaction.atomic():
        for ev in qs:
            r = getattr(ev, "report", None)

            if r is None:
                ev.frozen_data = {
                    "missing": True,
                    "note": "source report was deleted before freezing",
                    "frozen_at": timezone.localtime(now).isoformat(),
                }
                ev.frozen_at = now
                ev.save(update_fields=["frozen_at", "frozen_data"])
                frozen_count += 1
                continue

            ev.frozen_data = {
                "report_id": getattr(r, "id", None),
                "title": (getattr(r, "title", "") or "").strip(),
                "report_date": getattr(r, "report_date", None).isoformat() if getattr(r, "report_date", None) else None,
                "day_name": (getattr(r, "day_name", "") or "").strip(),
                "beneficiaries_count": getattr(r, "beneficiaries_count", None),
                "idea": getattr(r, "idea", None),
                "category": getattr(getattr(r, "category", None), "name", None),
                "teacher_name": (getattr(r, "teacher_display_name", "") or "").strip() or (getattr(r, "teacher_name", "") or "").strip(),
                "created_at": getattr(r, "created_at", None).isoformat() if getattr(r, "created_at", None) else None,
            }

            # archive images (best-effort)
            for idx in (1, 2, 3, 4):
                src = getattr(r, f"image{idx}", None)
                dest = getattr(ev, f"archived_image{idx}")
                if not src or not getattr(src, "name", None):
                    continue
                ext = _safe_ext(getattr(src, "name", ""))
                fname = f"report_{r.id}_img{idx}{ext}"
                _copy_field_to_archived(src_field=src, dest_field=dest, filename=fname)

            ev.frozen_at = now
            ev.save(
                update_fields=[
                    "frozen_at",
                    "frozen_data",
                    "archived_image1",
                    "archived_image2",
                    "archived_image3",
                    "archived_image4",
                ]
            )
            frozen_count += 1

    return frozen_count
