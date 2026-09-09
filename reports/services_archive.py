from __future__ import annotations

from typing import Iterable

from django.db.models import Count, Q, Sum
from django.utils import timezone

from .models import (
    Assignment,
    AchievementEvidenceImage,
    AchievementEvidenceReport,
    Document,
    Initiative,
    LabAsset,
    LabAssetHandover,
    LabExperiment,
    LeadershipEvidenceImage,
    Notification,
    Meeting,
    Plan,
    Report,
    ReportEvidence,
    School,
    SchoolArchiveAddon,
    SchoolYearArchive,
    SchoolLeadershipPortfolio,
    TeacherAchievementFile,
    Ticket,
    TicketImage,
    school_has_archive_addon,
)

UNCLASSIFIED_YEAR = "__unclassified__"

# Where the manager starts being warned. Early enough to act — buying space or
# clearing an archived year both take a few minutes — without nagging.
STORAGE_WARNING_PERCENT = 80
STORAGE_CRITICAL_PERCENT = 95


def _year_sort_key(value: str) -> tuple[int, str]:
    value = (value or "").strip()
    try:
        return (int(value.split("-", 1)[0]), value)
    except Exception:
        return (-1, value)


def _clean_years(values: Iterable[str]) -> list[str]:
    years = {str(v).strip() for v in values if str(v or "").strip()}
    return sorted(years, key=_year_sort_key, reverse=True)


def archive_year_label(value: str) -> str:
    if value == UNCLASSIFIED_YEAR:
        return "تقارير غير مصنفة بسنة"
    return f"{value} هـ"


def school_archive_enabled(school: School | None) -> bool:
    return school_has_archive_addon(school)


def _file_size(value) -> int:
    if not value:
        return 0
    try:
        if not getattr(value, "name", ""):
            return 0
    except Exception:
        return 0
    try:
        return int(value.size or 0)
    except Exception:
        return 0


def _incoming_size(files) -> int:
    total = 0
    for file_obj in files or []:
        try:
            total += int(getattr(file_obj, "size", 0) or 0)
        except Exception:
            pass
    return total


def calculate_school_archive_storage_bytes(school: School | None) -> int:
    """Best-effort sum of every file the school holds, both buckets together.

    This is the reconciliation scan behind ``School.storage_used_bytes``. It must
    list exactly the models registered in ``storage_tracking.connect_all()`` — a
    model missing from either side silently drifts: gated on upload, invisible in
    the totals, and then wiped from them by the next backfill.
    """
    if school is None:
        return 0

    total = 0
    for report in Report.objects.filter(school=school).only("image1", "image2", "image3", "image4"):
        total += _file_size(report.image1)
        total += _file_size(report.image2)
        total += _file_size(report.image3)
        total += _file_size(report.image4)

    for evidence in ReportEvidence.objects.filter(report__school=school).only("image"):
        total += _file_size(evidence.image)

    for ach_file in TeacherAchievementFile.objects.filter(school=school).only("pdf_file"):
        total += _file_size(ach_file.pdf_file)

    evidence_images = AchievementEvidenceImage.objects.filter(section__file__school=school).only("image")
    for evidence in evidence_images:
        total += _file_size(evidence.image)

    evidence_reports = AchievementEvidenceReport.objects.filter(section__file__school=school).only(
        "archived_image1",
        "archived_image2",
        "archived_image3",
        "archived_image4",
    )
    for evidence in evidence_reports:
        total += _file_size(evidence.archived_image1)
        total += _file_size(evidence.archived_image2)
        total += _file_size(evidence.archived_image3)
        total += _file_size(evidence.archived_image4)

    for evidence in LeadershipEvidenceImage.objects.filter(
        section__portfolio__school=school
    ).only("image"):
        total += _file_size(evidence.image)

    for document in Document.objects.filter(school=school).only("file"):
        total += _file_size(document.file)

    for year_archive in SchoolYearArchive.objects.filter(school=school).only("archive_file"):
        total += _file_size(year_archive.archive_file)

    for ticket in Ticket.objects.filter(school=school).only("attachment"):
        total += _file_size(ticket.attachment)

    for ticket_image in TicketImage.objects.filter(ticket__school=school).only("image"):
        total += _file_size(ticket_image.image)

    for notification in Notification.objects.filter(school=school).only("attachment"):
        total += _file_size(notification.attachment)

    return total


# The work bucket, item by item. Order is the order the manager reads them in.
WORK_BREAKDOWN_KEYS = (
    "reports",
    "achievements",
    "leadership",
    "documents",
    "tickets",
    "circulars",
    "notifications",
)


def school_storage_breakdown(school: School | None) -> dict:
    """Fast manager-facing breakdown from incrementally maintained byte fields.

    ``work_total`` covers daily work; ``snapshots`` is the yearly-archive bucket
    and is deliberately kept out of that sum. Adding the two together was what
    made the work card charge a school for space the upload gate never counted.
    """
    empty = {key: 0 for key in WORK_BREAKDOWN_KEYS}
    if school is None:
        return {**empty, "snapshots": 0, "work_total": 0, "total": 0}

    def _sum(queryset) -> int:
        return int(queryset.aggregate(total=Sum("storage_bytes")).get("total") or 0)

    circulars_qs = Notification.objects.filter(
        school=school,
        kind=Notification.Kind.CIRCULAR,
    )
    notifications_qs = Notification.objects.filter(
        school=school,
        kind__in=(Notification.Kind.NOTIFICATION, Notification.Kind.NEWSLETTER),
    )
    values = {
        "reports": (
            _sum(Report.objects.filter(school=school))
            + _sum(ReportEvidence.objects.filter(report__school=school))
        ),
        "achievements": (
            _sum(TeacherAchievementFile.objects.filter(school=school))
            + _sum(AchievementEvidenceImage.objects.filter(section__file__school=school))
            + _sum(AchievementEvidenceReport.objects.filter(section__file__school=school))
        ),
        "leadership": _sum(
            LeadershipEvidenceImage.objects.filter(section__portfolio__school=school)
        ),
        "documents": _sum(Document.objects.filter(school=school)),
        "tickets": (
            _sum(Ticket.objects.filter(school=school))
            + _sum(TicketImage.objects.filter(ticket__school=school))
        ),
        "circulars": _sum(circulars_qs),
        "notifications": _sum(notifications_qs),
    }
    values["work_total"] = sum(values[key] for key in WORK_BREAKDOWN_KEYS)
    values["snapshots"] = _sum(SchoolYearArchive.objects.filter(school=school))
    values["total"] = values["work_total"] + values["snapshots"]
    return values


def school_administrative_archive_stats(school: School | None) -> dict:
    """School-wide records included as-of snapshot time (they have no academic-year field)."""
    if school is None:
        return {
            "tickets": 0,
            "circulars": 0,
            "notifications": 0,
            "system_notifications": 0,
            "user_notifications": 0,
            "assignments": 0,
            "meetings": 0,
            "lab_assets": 0,
            "lab_handovers": 0,
            "lab_experiments": 0,
            "total": 0,
        }
    notifications = Notification.objects.filter(school=school)
    values = {
        "tickets": Ticket.objects.filter(school=school).count(),
        "circulars": notifications.filter(kind=Notification.Kind.CIRCULAR).count(),
        "notifications": notifications.filter(
            kind__in=(Notification.Kind.NOTIFICATION, Notification.Kind.NEWSLETTER)
        ).count(),
        "system_notifications": notifications.filter(
            kind__in=(Notification.Kind.NOTIFICATION, Notification.Kind.NEWSLETTER),
            created_by__isnull=True,
        ).count(),
        "user_notifications": notifications.filter(
            kind__in=(Notification.Kind.NOTIFICATION, Notification.Kind.NEWSLETTER),
            created_by__isnull=False,
        ).count(),
        "assignments": Assignment.objects.filter(
            Q(school=school) | Q(targets__school=school)
        ).distinct().count(),
        "meetings": Meeting.objects.filter(school=school).count(),
        "lab_assets": LabAsset.objects.filter(school=school).count(),
        "lab_handovers": LabAssetHandover.objects.filter(school=school).count(),
        "lab_experiments": LabExperiment.objects.filter(school=school).count(),
    }
    values["total"] = sum(
        values[key]
        for key in (
            "tickets",
            "circulars",
            "notifications",
            "assignments",
            "meetings",
            "lab_assets",
            "lab_handovers",
            "lab_experiments",
        )
    )
    return values


def school_administrative_archive_payload(
    school: School | None,
    *,
    search: str = "",
) -> dict:
    """Detailed school-wide administrative records shown before snapshot creation.

    Administrative records currently have no academic-year field, so the same
    as-of list is included in every school-wide yearly snapshot.
    """
    if school is None:
        return {
            "tickets_qs": Ticket.objects.none(),
            "circulars_qs": Notification.objects.none(),
            "notifications_qs": Notification.objects.none(),
            "matches": {"tickets": 0, "circulars": 0, "notifications": 0, "total": 0},
        }

    tickets_qs = (
        Ticket.objects.filter(school=school)
        .select_related("creator", "assignee", "department")
        .prefetch_related("recipients")
        .order_by("-created_at", "-id")
    )
    notification_base = (
        Notification.objects.filter(school=school)
        .select_related("created_by")
        .prefetch_related("recipients__teacher")
        .order_by("-created_at", "-id")
    )
    circulars_qs = notification_base.filter(kind=Notification.Kind.CIRCULAR)
    notifications_qs = notification_base.filter(
        kind__in=(Notification.Kind.NOTIFICATION, Notification.Kind.NEWSLETTER)
    )

    search = (search or "").strip()
    if search:
        tickets_qs = tickets_qs.filter(
            Q(title__icontains=search)
            | Q(body__icontains=search)
            | Q(creator__name__icontains=search)
            | Q(creator__phone__icontains=search)
            | Q(assignee__name__icontains=search)
            | Q(department__name__icontains=search)
            | Q(recipients__name__icontains=search)
        ).distinct()
        notification_filter = (
            Q(title__icontains=search)
            | Q(message__icontains=search)
            | Q(created_by__name__icontains=search)
            | Q(created_by__phone__icontains=search)
            | Q(recipients__teacher__name__icontains=search)
            | Q(recipients__teacher__phone__icontains=search)
        )
        circulars_qs = circulars_qs.filter(notification_filter).distinct()
        notifications_qs = notifications_qs.filter(notification_filter).distinct()

    matches = {
        "tickets": tickets_qs.count(),
        "circulars": circulars_qs.count(),
        "notifications": notifications_qs.count(),
    }
    matches["total"] = matches["tickets"] + matches["circulars"] + matches["notifications"]
    return {
        "tickets_qs": tickets_qs,
        "circulars_qs": circulars_qs,
        "notifications_qs": notifications_qs,
        "matches": matches,
    }


def recompute_school_storage(school: School | None) -> int:
    """يعيد حساب التخزين الفعلي للمدرسة (مسح كامل دقيق) ويخزّنه على المدرسة.

    يُستخدم للتهيئة الأولية (backfill) أو المصالحة عند الاشتباه بانحراف.
    قد يقرأ أحجام الملفات من التخزين (شبكة) — لذا لا يُستدعى في المسار الساخن.
    """
    if school is None:
        return 0
    used = calculate_school_archive_storage_bytes(school)
    try:
        School.objects.filter(pk=school.pk).update(storage_used_bytes=used)
    except Exception:
        pass
    _sync_addon_usage(school)
    return used


def _sync_addon_usage(school: School) -> int:
    """يملأ ``SchoolArchiveAddon.storage_used_bytes`` بحجم النسخ السنوية وحده.

    الحقل يعيش على ملحق الأرشفة وتُقاس به شاشة المنصة أمام ``storage_limit_gb``،
    فكتابةُ إجمالي المدرسة فيه كانت تعرض مساحة العمل على أنها استهلاك الأرشيف.
    """
    used = school_snapshot_used_bytes(school)
    try:
        addon = SchoolArchiveAddon.objects.get(school=school)
        if addon.storage_used_bytes != used:
            addon.storage_used_bytes = used
            addon.save(update_fields=["storage_used_bytes", "updated_at"])
    except SchoolArchiveAddon.DoesNotExist:
        pass
    return used


def sync_school_archive_storage_usage(school: School | None) -> int:
    """يحدّث عدّاد ملحق الأرشفة من النسخ السنوية المحفوظة (بلا مسح شبكي)."""
    if school is None:
        return 0
    return _sync_addon_usage(school)


STORAGE_RATE_CACHE_KEY = "platform_storage_mb_per_teacher_v1"
FREE_STORAGE_CACHE_KEY = "platform_free_storage_mb_v1"
_STORAGE_RATE_CACHE_TTL = 600


def _platform_free_storage_bytes() -> int:
    """الحد الأدنى لمدرسة بلا اشتراك فعّال (بالبايت). 0 = غير محدود.

    مُخزَّنة كنظيرتها ``_storage_mb_per_teacher``: كل مدرسة بسعة صفر أو بلا
    اشتراك تمرّ من هنا، فكانت قائمة المدارس تُطلق استعلام إعدادات لكل صف.
    و``PlatformSettings.save()`` يُسقط المفتاح، فلا يُخدَم حدٌّ قديم بعد تعديل.
    """
    try:
        from django.core.cache import cache

        cached = cache.get(FREE_STORAGE_CACHE_KEY)
        if cached is not None:
            return max(0, int(cached)) * 1024 * 1024
    except Exception:
        cache = None

    try:
        from .models import PlatformSettings

        mb = int(getattr(PlatformSettings.get_solo(), "free_storage_mb", 0) or 0)
    except Exception:
        return 0

    mb = max(0, mb)
    try:
        if cache is not None:
            cache.set(FREE_STORAGE_CACHE_KEY, mb, _STORAGE_RATE_CACHE_TTL)
    except Exception:
        pass
    return mb * 1024 * 1024


def _storage_mb_per_teacher() -> int:
    """Per-teacher storage rate, cached because the landing page reads it.

    The public page has a query budget; this value changes only when an operator
    edits it, and PlatformSettings.save() drops the key, so the cache can never
    serve a stale rate after an edit.
    """
    try:
        from django.core.cache import cache

        cached = cache.get(STORAGE_RATE_CACHE_KEY)
        if cached is not None:
            return max(0, int(cached))
    except Exception:
        cache = None

    try:
        from .models import PlatformSettings

        value = max(0, int(getattr(PlatformSettings.get_solo(), "storage_mb_per_teacher", 0) or 0))
    except Exception:
        return 0

    try:
        if cache is not None:
            cache.set(STORAGE_RATE_CACHE_KEY, value, _STORAGE_RATE_CACHE_TTL)
    except Exception:
        pass
    return value


def storage_bytes_for_seats(seats: int) -> int:
    """Base storage a school gets for ``seats`` paid teacher slots.

    Quoted before purchase and enforced after it, so the calculator, the order
    summary and the live limit can never disagree about what was sold.
    """
    try:
        seats = max(0, int(seats or 0))
    except (TypeError, ValueError):
        return 0
    return seats * _storage_mb_per_teacher() * 1024 * 1024


def storage_display_for_seats(seats: int) -> str:
    """Human label for the base storage of a capacity, e.g. ``19.5GB``."""
    return _human_size(storage_bytes_for_seats(seats))


def _active_subscription(school: School | None):
    if school is None:
        return None
    try:
        subscription = getattr(school, "subscription", None)
    except Exception:
        return None
    if subscription is None:
        return None
    try:
        return None if subscription.is_expired else subscription
    except Exception:
        return None


def school_storage_allowance(school: School | None) -> dict:
    """The school's storage entitlement, broken into its two independent parts.

    Storage is deliberately **not** tied to the yearly-archive add-on. It used to
    be: the limit lived on SchoolArchiveAddon.storage_limit_gb, so a school could
    not buy space without buying yearly archiving, and space it had paid for
    vanished when that add-on lapsed.

    - ``base``  scales with the teacher capacity the school actually bought, so a
      bigger school starts with more room and gains more by upgrading capacity.
    - ``extra`` is bought separately and stays for as long as the subscription is
      active.

    A school with no active subscription falls back to the platform floor.
    Returns bytes; a total of 0 means unlimited.
    """
    if school is None:
        return {"base_bytes": 0, "extra_bytes": 0, "total_bytes": 0, "is_unlimited": True, "seats": 0}

    subscription = _active_subscription(school)
    if subscription is None:
        floor = _platform_free_storage_bytes()
        return {
            "base_bytes": floor,
            "extra_bytes": 0,
            "total_bytes": floor,
            "is_unlimited": floor <= 0,
            "seats": 0,
        }

    try:
        seats = int(getattr(subscription, "teacher_limit", 0) or 0)
    except Exception:
        seats = 0

    per_teacher_mb = _storage_mb_per_teacher()
    base_bytes = seats * per_teacher_mb * 1024 * 1024
    extra_bytes = int(getattr(school, "extra_storage_gb", 0) or 0) * 1024 * 1024 * 1024

    total = base_bytes + extra_bytes
    if total <= 0:
        # Per-teacher storage disabled and nothing bought: fall back to the floor
        # rather than silently granting unlimited space.
        floor = _platform_free_storage_bytes()
        return {
            "base_bytes": floor,
            "extra_bytes": 0,
            "total_bytes": floor,
            "is_unlimited": floor <= 0,
            "seats": seats,
        }

    return {
        "base_bytes": base_bytes,
        "extra_bytes": extra_bytes,
        "total_bytes": total,
        "is_unlimited": False,
        "seats": seats,
    }


def school_storage_limit_bytes(school: School | None) -> int:
    """الحد الفعلي لتخزين المدرسة (بالبايت). 0 يعني غير محدود."""
    return school_storage_allowance(school)["total_bytes"]


def school_snapshot_used_bytes(school: School | None) -> int:
    """Bytes held by saved yearly snapshots only."""
    if school is None:
        return 0
    return int(
        SchoolYearArchive.objects.filter(school=school)
        .aggregate(total=Sum("storage_bytes"))
        .get("total")
        or 0
    )


def school_work_used_bytes(school: School | None) -> int:
    """Bytes held by live school work: everything except saved snapshots."""
    if school is None:
        return 0
    total = int(
        School.objects.filter(pk=school.pk)
        .values_list("storage_used_bytes", flat=True)
        .first()
        or 0
    )
    return max(0, total - school_snapshot_used_bytes(school))


def school_archive_allowance(school: School | None) -> dict:
    """Snapshot allowance: a separate product with its own defined space.

    Archiving is sold on its own. A school without the add-on gets no snapshot
    space at all, and the space it does get comes from the add-on rather than
    from the teacher capacity that sizes daily work.

    Keeping this bucket apart from the work bucket matters operationally: they
    used to share one pool, so a once-a-year administrative action — creating a
    snapshot — could exhaust the space that gates every teacher upload and
    freeze the whole school. Now a full archive only stops new snapshots.
    """
    inactive = {
        "limit_bytes": 0,
        "is_unlimited": False,
        "is_subscribed": False,
        "addon": None,
    }
    if school is None:
        return inactive

    try:
        addon = getattr(school, "archive_addon", None)
    except Exception:
        addon = None

    try:
        active = bool(addon and addon.is_active)
    except Exception:
        active = False

    if not active:
        return inactive

    try:
        limit_gb = max(0, int(getattr(addon, "storage_limit_gb", 0) or 0))
    except (TypeError, ValueError):
        limit_gb = 0

    return {
        "limit_bytes": limit_gb * 1024 * 1024 * 1024,
        # 0 GB on an active add-on is a misconfiguration, not "unlimited":
        # treating it as unlimited would hand out free space by accident.
        "is_unlimited": False,
        "is_subscribed": True,
        "addon": addon,
    }


def school_archive_overview(school: School | None) -> dict:
    """Manager-facing state of the snapshot bucket."""
    allowance = school_archive_allowance(school)
    limit = allowance["limit_bytes"]
    is_unlimited = allowance["is_unlimited"]
    used = school_snapshot_used_bytes(school)
    # limit is 0 for a school without the archive subscription, so never divide.
    percent = 0 if (is_unlimited or limit <= 0) else min(100, round((used / limit) * 100, 1))
    remaining = 0 if is_unlimited else max(0, limit - used)

    warning_level = "ok"
    if not is_unlimited:
        if percent >= 100:
            warning_level = "full"
        elif percent >= STORAGE_CRITICAL_PERCENT:
            warning_level = "critical"
        elif percent >= STORAGE_WARNING_PERCENT:
            warning_level = "warning"

    return {
        "used_bytes": used,
        "limit_bytes": limit,
        "remaining_bytes": remaining,
        "used_label": _human_size(used),
        "limit_label": "غير محدود" if is_unlimited else _human_size(limit),
        "remaining_label": "غير محدود" if is_unlimited else _human_size(remaining),
        "usage_percent": percent,
        "is_unlimited": is_unlimited,
        "is_subscribed": allowance["is_subscribed"],
        "warning_level": warning_level,
        "needs_attention": warning_level != "ok",
    }


def archive_snapshot_capacity_error(school: School | None, incoming_bytes: int) -> str:
    """Gate for creating a snapshot. Never blocks teacher uploads."""
    allowance = school_archive_allowance(school)
    if not allowance["is_subscribed"]:
        return (
            "خدمة الأرشيف السنوي غير مفعّلة لهذه المدرسة. "
            "فعّل الخدمة من صفحة اشتراك المدرسة للحصول على مساحة أرشيف مستقلة "
            "تحفظ فيها نسخة ثابتة لكل سنة دراسية."
        )
    if allowance["is_unlimited"]:
        return ""

    limit = allowance["limit_bytes"]
    used = school_snapshot_used_bytes(school)
    incoming = max(0, int(incoming_bytes or 0))
    if used + incoming <= limit:
        return ""

    return (
        "لا تتسع مساحة الأرشفة السنوية لنسخة جديدة. "
        f"المحفوظ حالياً {_human_size(used)} من أصل {_human_size(limit)}، "
        f"والنسخة الجديدة {_human_size(incoming)}. "
        "نزّل نسخة سنة سابقة على جهازك ثم احذفها من المنصة لتحرير المساحة، "
        "أو اطلب زيادة مساحة الأرشفة من صفحة اشتراك المدرسة. "
        "لا يؤثر ذلك على عمل المعلمين اليومي؛ الرفع والتوثيق يعملان كالمعتاد."
    )


def _human_size(num_bytes: int) -> str:
    """تنسيق دقيق للحجم: بايت/كيلو/ميجا/جيجا حسب المقدار."""
    b = max(0, int(num_bytes or 0))
    if b < 1024:
        return f"{b} بايت"
    kb = b / 1024
    if kb < 1024:
        return f"{round(kb, 1)}KB"
    mb = kb / 1024
    if mb < 1024:
        return f"{round(mb, 1)}MB"
    return f"{round(mb / 1024, 2)}GB"


def school_storage_pressure(school: School | None) -> dict:
    """Cheap read of how close the work bucket is to stopping uploads.

    school_storage_overview() runs a nine-way breakdown plus reclaimable-year
    scanning. The manager dashboard only needs to know whether to warn, and it
    loads on every visit, so this stays at one aggregate query.
    """
    empty = {
        "needs_attention": False,
        "warning_level": "ok",
        "usage_percent": 0,
        "used_label": "",
        "limit_label": "",
        "remaining_label": "",
    }
    if school is None:
        return empty

    allowance = school_storage_allowance(school)
    if allowance["is_unlimited"]:
        return empty

    limit = allowance["total_bytes"]
    if limit <= 0:
        return empty

    used = school_work_used_bytes(school)
    percent = min(100, round((used / limit) * 100, 1))

    if percent >= 100:
        level = "full"
    elif percent >= STORAGE_CRITICAL_PERCENT:
        level = "critical"
    elif percent >= STORAGE_WARNING_PERCENT:
        level = "warning"
    else:
        return empty

    return {
        "needs_attention": True,
        "warning_level": level,
        "usage_percent": percent,
        "used_label": _human_size(used),
        "limit_label": _human_size(limit),
        "remaining_label": _human_size(max(0, limit - used)),
    }


def school_storage_overview(school: School | None) -> dict:
    """Single source of truth for the **work** storage card.

    Reports what the upload gate reports: live work only. It used to read the
    school's grand total instead, so every byte of yearly snapshot was charged
    twice — once here and once on the archive card — and a school could be told
    its space was full while uploads carried on working normally.
    """
    breakdown = school_storage_breakdown(school)
    if school is None:
        total_held = 0
    else:
        total_held = int(
            School.objects.filter(pk=school.pk)
            .values_list("storage_used_bytes", flat=True)
            .first()
            or 0
        )
    # The same subtraction the upload gate makes, from the same stored total, so
    # the card and the gate can never quote two different numbers. The breakdown
    # itemises it; the running total remains the authority.
    used = max(0, total_held - breakdown["snapshots"])
    allowance = school_storage_allowance(school)
    limit = allowance["total_bytes"]
    is_unlimited = allowance["is_unlimited"]
    percent = 0 if is_unlimited else min(100, round((used / limit) * 100, 1))
    remaining = 0 if is_unlimited else max(0, limit - used)

    warning_level = "ok"
    if not is_unlimited:
        if percent >= 100:
            warning_level = "full"
        elif percent >= STORAGE_CRITICAL_PERCENT:
            warning_level = "critical"
        elif percent >= STORAGE_WARNING_PERCENT:
            warning_level = "warning"

    reclaimable = reclaimable_storage_by_year(school) if warning_level != "ok" else []
    reclaimable_bytes = sum(row["bytes"] for row in reclaimable)

    return {
        "used_bytes": used,
        "limit_bytes": limit,
        "remaining_bytes": remaining,
        "used_label": _human_size(used),
        "limit_label": "غير محدود" if is_unlimited else _human_size(limit),
        "remaining_label": "غير محدود" if is_unlimited else _human_size(remaining),
        "usage_percent": percent,
        "is_unlimited": is_unlimited,
        # Shown so the manager can see that upgrading capacity raises storage
        # too, and which part they bought separately.
        "base_bytes": allowance["base_bytes"],
        "base_label": _human_size(allowance["base_bytes"]),
        "extra_bytes": allowance["extra_bytes"],
        "extra_label": _human_size(allowance["extra_bytes"]),
        "seats": allowance["seats"],
        "warning_level": warning_level,
        "needs_attention": warning_level != "ok",
        "reclaimable_years": reclaimable,
        "reclaimable_bytes": reclaimable_bytes,
        "reclaimable_label": _human_size(reclaimable_bytes),
        # Work items only. Snapshots are reported by school_archive_overview()
        # against their own limit; listing them here read as work usage.
        "breakdown": {
            key: {"bytes": breakdown[key], "label": _human_size(breakdown[key])}
            for key in WORK_BREAKDOWN_KEYS
        },
        # Surfaced so a manager who wonders where the rest of the space went can
        # see the archive bucket named next to the work one.
        "snapshot_bytes": breakdown["snapshots"],
        "snapshot_label": _human_size(breakdown["snapshots"]),
        "total_held_bytes": total_held,
        "total_held_label": _human_size(total_held),
    }


def reclaimable_storage_by_year(school: School | None) -> list[dict]:
    """Years whose live files are already preserved in a saved snapshot.

    This is what makes "delete old files" a safe suggestion rather than a
    request to destroy records: the yearly snapshot holds an immutable copy, so
    the live reports and evidence for that year can be removed to free space
    without losing the year itself.

    Years with no saved snapshot are deliberately excluded.
    """
    if school is None:
        return []

    archived_years = set(
        SchoolYearArchive.objects.filter(
            school=school,
            status__in=[SchoolYearArchive.Status.READY, SchoolYearArchive.Status.PARTIAL],
        )
        .exclude(academic_year="")
        .values_list("academic_year", flat=True)
    )
    if not archived_years:
        return []

    current_year = (getattr(school, "current_academic_year", "") or "").strip()

    rows = []
    for year in sorted(archived_years, key=_year_sort_key, reverse=True):
        if year == current_year:
            # Never suggest clearing the year the school is still working in.
            continue

        report_bytes = int(
            Report.objects.filter(school=school, academic_year=year)
            .aggregate(total=Sum("storage_bytes"))
            .get("total")
            or 0
        )
        report_bytes += int(
            ReportEvidence.objects.filter(
                report__school=school,
                report__academic_year=year,
            )
            .aggregate(total=Sum("storage_bytes"))
            .get("total")
            or 0
        )
        achievement_bytes = int(
            TeacherAchievementFile.objects.filter(school=school, academic_year=year)
            .aggregate(total=Sum("storage_bytes"))
            .get("total")
            or 0
        )
        evidence_bytes = int(
            AchievementEvidenceImage.objects.filter(
                section__file__school=school, section__file__academic_year=year
            )
            .aggregate(total=Sum("storage_bytes"))
            .get("total")
            or 0
        )
        # Frozen report images and leadership evidence go into the yearly ZIP
        # too, so leaving them out understated what clearing the year frees.
        # Documents stay out on purpose: the snapshot does not carry them, so
        # deleting them would lose them.
        evidence_bytes += int(
            AchievementEvidenceReport.objects.filter(
                section__file__school=school, section__file__academic_year=year
            )
            .aggregate(total=Sum("storage_bytes"))
            .get("total")
            or 0
        )
        leadership_bytes = int(
            LeadershipEvidenceImage.objects.filter(
                section__portfolio__school=school,
                section__portfolio__academic_year=year,
            )
            .aggregate(total=Sum("storage_bytes"))
            .get("total")
            or 0
        )
        total = report_bytes + achievement_bytes + evidence_bytes + leadership_bytes
        if total <= 0:
            continue

        rows.append(
            {
                "academic_year": year,
                "label": archive_year_label(year),
                "bytes": total,
                "size_label": _human_size(total),
                "reports_bytes": report_bytes,
                "achievements_bytes": achievement_bytes + evidence_bytes,
                "leadership_bytes": leadership_bytes,
            }
        )
    return rows


def archive_storage_capacity_error(school: School | None, incoming_files, *, replacing_files=None) -> str:
    """رسالة خطأ عربية عند تجاوز حدّ تخزين عمل المدرسة اليومي.

    يقيس مساحة العمل فقط (التقارير وملفات الإنجاز والطلبات والمرفقات). النسخ
    السنوية لها حدّها المستقل، حتى لا يوقف إنشاء نسخة أرشيف رفع المعلمين.
    الحساب يعتمد على الحجم الفعلي للملفات (دقيق).
    """
    if school is None:
        return ""

    incoming_bytes = _incoming_size(incoming_files)
    if incoming_bytes <= 0:
        return ""

    limit_bytes = school_storage_limit_bytes(school)
    if limit_bytes <= 0:
        # 0 = غير محدود
        return ""

    # مساحة العمل = الإجمالي التزايدي المخزّن ناقص النسخ السنوية المحفوظة.
    # يُحدَّث الإجمالي تلقائيًا عبر إشارات storage_tracking عند كل رفع/حذف.
    used_bytes = school_work_used_bytes(school)
    replaced_bytes = sum(_file_size(value) for value in (replacing_files or []))
    projected_used_bytes = max(0, used_bytes - replaced_bytes) + incoming_bytes
    if projected_used_bytes <= limit_bytes:
        return ""

    replaced_text = f"، وسيتم استبدال {_human_size(replaced_bytes)}" if replaced_bytes else ""
    base = (
        f"تم تجاوز حد مساحة عمل المدرسة. المستخدم حالياً {_human_size(used_bytes)}، "
        f"والملفات الجديدة {_human_size(incoming_bytes)}{replaced_text}، "
        f"والحد المتاح {_human_size(limit_bytes)}. "
    )

    # Two ways out, and the safe one is only offered when it really is safe:
    # a year can be cleared without losing it once a snapshot preserves it.
    reclaimable = reclaimable_storage_by_year(school)
    if reclaimable:
        biggest = reclaimable[0]
        return base + (
            f"يمكنك طلب زيادة مساحة العمل من صفحة الاشتراك، أو تفريغ مساحة بحذف ملفات "
            f"{biggest['label']} ({biggest['size_label']}) — لها نسخة سنوية محفوظة "
            "تحتفظ بها كاملة."
        )
    return base + "يمكنك طلب زيادة مساحة العمل من صفحة اشتراك المدرسة."


def archive_available_years(*, school: School, teacher=None, school_wide: bool = False) -> list[str]:
    years: set[str] = set()

    reports_qs = Report.objects.filter(school=school)
    achievements_qs = TeacherAchievementFile.objects.filter(school=school)
    leadership_qs = SchoolLeadershipPortfolio.objects.filter(school=school)
    if not school_wide and teacher is not None:
        reports_qs = reports_qs.filter(teacher=teacher)
        achievements_qs = achievements_qs.filter(teacher=teacher)

    years.update(reports_qs.exclude(academic_year="").values_list("academic_year", flat=True).distinct())
    years.update(achievements_qs.values_list("academic_year", flat=True).distinct())
    if school_wide:
        years.update(leadership_qs.values_list("academic_year", flat=True).distinct())
        years.update(
            Document.objects.filter(school=school)
            .exclude(academic_year="")
            .values_list("academic_year", flat=True)
            .distinct()
        )
        years.update(
            Plan.objects.filter(school=school)
            .exclude(academic_year="")
            .values_list("academic_year", flat=True)
            .distinct()
        )
        years.update(
            SchoolYearArchive.objects.filter(school=school)
            .exclude(academic_year="")
            .values_list("academic_year", flat=True)
            .distinct()
        )
        # هذه السجلات التشغيلية لا تحمل سنة دراسية في نماذجها. إذا كانت هي
        # المحتوى الوحيد، نستخدم سنة المدرسة الحالية كوعاء واضح للقطة الكاملة.
        has_administrative_records = (
            Ticket.objects.filter(school=school).exists()
            or Notification.objects.filter(school=school).exists()
            or Meeting.objects.filter(school=school).exists()
            or Initiative.objects.filter(school=school, plan__isnull=True).exists()
            or Assignment.objects.filter(
                Q(school=school) | Q(targets__school=school)
            ).exists()
            or LabAsset.objects.filter(school=school).exists()
            or LabAssetHandover.objects.filter(school=school).exists()
            or LabExperiment.objects.filter(school=school).exists()
        )
        current_year = (getattr(school, "current_academic_year", "") or "").strip()
        if has_administrative_records and current_year:
            years.add(current_year)

    sorted_years = _clean_years(years)
    if reports_qs.filter(Q(academic_year="") | Q(academic_year__isnull=True)).exists():
        sorted_years.append(UNCLASSIFIED_YEAR)
    return sorted_years


def archive_payload(
    *,
    school: School,
    selected_year: str,
    teacher=None,
    school_wide: bool = False,
    search: str = "",
    teacher_id: int | None = None,
    category_id: int | None = None,
) -> dict:
    reports_qs = (
        Report.objects.select_related("teacher", "category", "school")
        .filter(school=school)
        .order_by("-report_date", "-id")
    )
    achievements_qs = (
        TeacherAchievementFile.objects.select_related("teacher", "school", "decided_by")
        .filter(school=school)
        .order_by("-academic_year", "teacher__name", "-id")
    )

    if not school_wide and teacher is not None:
        reports_qs = reports_qs.filter(teacher=teacher)
        achievements_qs = achievements_qs.filter(teacher=teacher)

    if selected_year == UNCLASSIFIED_YEAR:
        reports_qs = reports_qs.filter(Q(academic_year="") | Q(academic_year__isnull=True))
        achievements_qs = achievements_qs.none()
    elif selected_year:
        reports_qs = reports_qs.filter(academic_year=selected_year)
        achievements_qs = achievements_qs.filter(academic_year=selected_year)

    search = (search or "").strip()
    if search:
        reports_qs = reports_qs.filter(
            Q(title__icontains=search)
            | Q(teacher__name__icontains=search)
            | Q(teacher__phone__icontains=search)
        )
        achievements_qs = achievements_qs.filter(
            Q(teacher__name__icontains=search)
            | Q(teacher__phone__icontains=search)
        )
    if teacher_id:
        reports_qs = reports_qs.filter(teacher_id=teacher_id)
        achievements_qs = achievements_qs.filter(teacher_id=teacher_id)
    if category_id:
        reports_qs = reports_qs.filter(category_id=category_id)

    report_stats = reports_qs.aggregate(
        total=Count("id"),
        with_images=Count(
            "id",
            filter=Q(image1__gt="") | Q(image2__gt="") | Q(image3__gt="") | Q(image4__gt=""),
        ),
    )
    achievement_stats = achievements_qs.aggregate(
        total=Count("id"),
        approved=Count("id", filter=Q(status=TeacherAchievementFile.Status.APPROVED)),
        submitted=Count("id", filter=Q(status=TeacherAchievementFile.Status.SUBMITTED)),
    )

    return {
        "reports_qs": reports_qs,
        "achievement_files_qs": achievements_qs,
        "report_stats": {
            "total": int(report_stats.get("total") or 0),
            "with_images": int(report_stats.get("with_images") or 0),
        },
        "achievement_stats": {
            "total": int(achievement_stats.get("total") or 0),
            "approved": int(achievement_stats.get("approved") or 0),
            "submitted": int(achievement_stats.get("submitted") or 0),
        },
    }


def school_consumption_summary(school: School | None) -> dict:
    """الاستهلاك الثلاثي للمدرسة في مكان واحد: التخزين والمقاعد والأرشفة.

    المدير يحتاج الأرقام الثلاثة مجتمعة ليعرف أين يقترب من حدّه، وكانت متفرقة:
    التخزين في صفحة الأرشيف، والمقاعد في صفحة الاشتراك، والأرشفة في لوحة المنصة.
    هذه الدالة تجمعها بلا إعادة حساب — تستدعي المصادر القائمة نفسها ليبقى
    الرقم واحداً أينما عُرض.

    الدلاء الثلاثة مستقلة عمداً: امتلاء الأرشفة يوقف النسخ السنوية وحدها، ولا
    يجمّد رفع المعلمين اليومي.
    """
    storage = school_storage_overview(school)
    archive = school_archive_overview(school)

    subscription = _active_subscription(school)
    seat_limit = 0
    if subscription is not None:
        try:
            seat_limit = int(getattr(subscription, "teacher_limit", 0) or 0)
        except (TypeError, ValueError):
            seat_limit = 0

    seats_used = 0
    if school is not None:
        from .models import SchoolMembership

        seats_used = SchoolMembership.seats_used(school)

    # سعة 0 تعني بلا حدّ في هذا المشروع، فلا تُقسم ولا تُعرض نسبة.
    seat_unlimited = seat_limit <= 0
    seat_percent = 0 if seat_unlimited else min(100, round(seats_used * 100 / seat_limit))
    seat_remaining = 0 if seat_unlimited else max(0, seat_limit - seats_used)

    seat_level = "ok"
    if not seat_unlimited:
        if seats_used >= seat_limit:
            seat_level = "full"
        elif seat_percent >= STORAGE_CRITICAL_PERCENT:
            seat_level = "critical"
        elif seat_percent >= STORAGE_WARNING_PERCENT:
            seat_level = "warning"

    return {
        "storage": storage,
        "archive": archive,
        "seats": {
            "used": seats_used,
            "limit": seat_limit,
            "remaining": seat_remaining,
            "usage_percent": seat_percent,
            "is_unlimited": seat_unlimited,
            "limit_label": "غير محدودة" if seat_unlimited else f"{seat_limit}",
            "remaining_label": "غير محدودة" if seat_unlimited else f"{seat_remaining}",
            "warning_level": seat_level,
            "needs_attention": seat_level != "ok",
        },
        "has_subscription": subscription is not None,
    }


def attach_school_consumption_rows(schools) -> None:
    """يُلحق ``consumption_row`` بكل مدرسة في قائمة، باستعلامات محدودة.

    ``school_consumption_summary`` تُجري عدة تجميعات للمدرسة الواحدة، فاستدعاؤها
    لكل صف في قائمة يعني عشرات الاستعلامات. هذه الدالة للقوائم: أربعة استعلامات
    مجمّعة لكل المدارس مهما كان عددها، فالتكلفة لا تنمو مع طول القائمة.

    تُستخدم في دليل مدارس المنصة وفي لوحة المدير التنفيذي معاً، فيرى الطرفان
    الأرقام نفسها.
    """
    schools = list(schools or [])
    if not schools:
        return

    from .models import SchoolArchiveAddon, SchoolMembership, SchoolSubscription, SchoolYearArchive

    school_ids = [school.pk for school in schools]

    seat_counts = SchoolMembership.seats_used_by_school(school_ids)
    snapshot_bytes = {
        row["school"]: int(row["total"] or 0)
        for row in SchoolYearArchive.objects.filter(school_id__in=school_ids)
        .values("school")
        .annotate(total=Sum("storage_bytes"))
    }
    subscriptions = {
        sub.school_id: sub
        for sub in SchoolSubscription.objects.filter(school_id__in=school_ids).select_related("plan")
    }
    addons = {
        addon.school_id: addon
        for addon in SchoolArchiveAddon.objects.filter(school_id__in=school_ids)
    }

    def _percent(used: int, limit: int) -> int:
        return 0 if limit <= 0 else min(100, round(used * 100 / limit))

    for school in schools:
        # ربط الاشتراك والملحق مسبقاً يمنع ``school_storage_allowance`` من
        # إطلاق استعلام لكل مدرسة عبر العلاقة العكسية.
        subscription = subscriptions.get(school.pk)
        school._state.fields_cache["subscription"] = subscription
        school._state.fields_cache["archive_addon"] = addons.get(school.pk)

        allowance = school_storage_allowance(school)
        storage_limit = int(allowance["total_bytes"] or 0)
        archive_used = snapshot_bytes.get(school.pk, 0)
        # Work usage is the stored total minus the snapshots, exactly as the
        # upload gate computes it — otherwise a school that archives a year
        # would appear to be running out of working space it never spent.
        storage_used = max(
            0, int(getattr(school, "storage_used_bytes", 0) or 0) - archive_used
        )
        storage_unlimited = bool(allowance["is_unlimited"]) or storage_limit <= 0

        seats_used = seat_counts.get(school.pk, 0)
        seat_limit = int(allowance["seats"] or 0)
        seat_unlimited = seat_limit <= 0

        archive = school_archive_allowance(school)
        archive_limit = int(archive["limit_bytes"] or 0)

        school.consumption_row = {
            "storage": {
                "used": storage_used,
                "limit": storage_limit,
                "percent": 0 if storage_unlimited else _percent(storage_used, storage_limit),
                "is_unlimited": storage_unlimited,
                "label": (
                    f"{_human_size(storage_used)} / غير محدود"
                    if storage_unlimited
                    else f"{_human_size(storage_used)} / {_human_size(storage_limit)}"
                ),
            },
            "seats": {
                "used": seats_used,
                "limit": seat_limit,
                "percent": 0 if seat_unlimited else _percent(seats_used, seat_limit),
                "is_unlimited": seat_unlimited,
                "label": (
                    f"{seats_used} / بلا حدّ" if seat_unlimited else f"{seats_used} / {seat_limit}"
                ),
            },
            "archive": {
                "used": archive_used,
                "limit": archive_limit,
                "percent": _percent(archive_used, archive_limit),
                "is_subscribed": bool(archive["is_subscribed"]),
                "label": (
                    f"{_human_size(archive_used)} / {_human_size(archive_limit)}"
                    if archive["is_subscribed"]
                    else "غير مفعّلة"
                ),
            },
            "has_subscription": subscription is not None,
        }
