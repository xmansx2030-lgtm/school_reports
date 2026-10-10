# -*- coding: utf-8 -*-
"""Published Ministry calendars and preserved historical year choices.

Only the platform publishes academic years. Calendar dates, never an estimated
Hijri conversion, determine the operational year for schools and teachers.
"""
from __future__ import annotations

import datetime
import re
from urllib.parse import urlsplit

from django.db.models import F
from django.utils import timezone

__all__ = [
    "ACADEMIC_YEAR_RE",
    "hijri_academic_year_options",
    "approved_academic_year_options", "current_academic_year_value",
    "published_academic_years", "school_current_academic_year",
    "sync_academic_years", "MINISTRY_CALENDAR_URL", "is_ministry_calendar_source",
]

ACADEMIC_YEAR_RE = re.compile(r"^[0-9]{4}-[0-9]{4}$")
MINISTRY_CALENDAR_URL = "https://www.moe.gov.sa/ar/education/generaleducation/Pages/AcademicCalendar.aspx"


def is_ministry_calendar_source(value: str) -> bool:
    try:
        parsed = urlsplit(value or "")
        host = (parsed.hostname or "").lower()
    except ValueError:
        return False
    return parsed.scheme == "https" and (host == "moe.gov.sa" or host.endswith(".moe.gov.sa"))


def _is_valid_year_value(value: str) -> bool:
    return bool(ACADEMIC_YEAR_RE.fullmatch(value)) and int(value[5:]) == int(value[:4]) + 1


def published_academic_years():
    """Complete, published calendars in platform display order."""
    from .models import AcademicYear

    return AcademicYear.objects.filter(
        is_active=True, start_date__isnull=False, end_date__isnull=False,
        end_date__gte=F("start_date"),
    ).exclude(source_url="").order_by("-value", "order", "pk")


def approved_academic_year_options() -> list[str]:
    return [
        year.value for year in published_academic_years()
        if _is_valid_year_value(year.value) and is_ministry_calendar_source(year.source_url)
    ]


def current_academic_year_value(today: datetime.date | None = None) -> str:
    """Latest published year that has started, including its summer break.

    The following year becomes current on its approved start date. Publishing a
    future calendar never changes today's operational year prematurely.
    """
    today = today or timezone.localdate()
    for year in published_academic_years().filter(start_date__lte=today).order_by("-start_date", "-value"):
        if _is_valid_year_value(year.value) and is_ministry_calendar_source(year.source_url):
            return year.value
    return ""


def _saved_years(instance) -> set[str]:
    """ما تحفظه المدرسة فعلاً — يُضمّ دائماً كي لا تختفي قيمة مستعملة."""
    saved: set[str] = set()
    current = (getattr(instance, "current_academic_year", "") or "").strip()
    if ACADEMIC_YEAR_RE.match(current):
        saved.add(current)
    for year in getattr(instance, "allowed_academic_years", None) or []:
        value = str(year).strip()
        if ACADEMIC_YEAR_RE.match(value):
            saved.add(value)
    # A historical label must remain selectable after an operational rollover,
    # including when its central calendar is later unpublished.
    if getattr(instance, "pk", None):
        from django.apps import apps
        from django.db.models import CharField

        for model in apps.get_app_config("reports").get_models():
            fields = {field.name: field for field in model._meta.fields}
            if "school" in fields and isinstance(fields.get("academic_year"), CharField):
                saved.update(
                    value for value in model.objects.filter(school_id=instance.pk)
                    .exclude(academic_year="").values_list("academic_year", flat=True).distinct()
                    if ACADEMIC_YEAR_RE.fullmatch(value)
                )
    return saved


def academic_year_is_used(value: str) -> bool:
    """Protect string-based historical references as well as personal year rows."""
    from django.apps import apps
    from django.db.models import CharField
    from .models import School
    from personal.models import PersonalAcademicYear, PersonalWorkspace

    if School.objects.filter(current_academic_year=value).exists():
        return True
    if any(value in (years or []) for years in School.objects.values_list("allowed_academic_years", flat=True)):
        return True
    if PersonalAcademicYear.objects.filter(value=value).exists() or PersonalWorkspace.objects.filter(current_academic_year=value).exists():
        return True
    for model in apps.get_models():
        fields = {field.name: field for field in model._meta.fields}
        if isinstance(fields.get("academic_year"), CharField) and model.objects.filter(academic_year=value).exists():
            return True
    return False


def hijri_academic_year_options(instance=None) -> list[str]:
    """Published choices plus the school's own historical labels; no guesses."""
    saved = _saved_years(instance) if instance is not None else set()
    return sorted(set(approved_academic_year_options()) | saved)


def school_current_academic_year(school=None, today: datetime.date | None = None) -> str:
    """Read central context, retaining a legacy value if no calendar exists."""
    return current_academic_year_value(today) or (
        (getattr(school, "current_academic_year", "") or "").strip() if school is not None else ""
    )


def sync_academic_years(today: datetime.date | None = None) -> dict:
    """Idempotent rollover of current context only; never move or delete work."""
    from django.db import transaction
    from .models import School
    from personal.models import PersonalWorkspace

    value = current_academic_year_value(today)
    result = {"value": value, "schools": 0, "teachers": 0}
    if not value:
        return result
    with transaction.atomic():
        result["schools"] = School.objects.exclude(current_academic_year=value).update(current_academic_year=value)
        result["teachers"] = PersonalWorkspace.objects.exclude(current_academic_year=value).update(current_academic_year=value)
    return result
