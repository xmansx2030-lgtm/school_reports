# reports/views/schools.py
# -*- coding: utf-8 -*-
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, time as dt_time, timedelta

from django.db import transaction
from django.db.models import Count, Q
from django.db.models.functions import TruncWeek
from django.urls import reverse

from core.observability import report_degraded as _degraded, soft_call, soft_fail

from ._helpers import *
from ._helpers import (
    _is_staff, _role_display_map, _filter_by_school,
    _model_has_field, _get_active_school, _user_manager_schools,
    _clean_query_params, _clean_query_value, _parse_date_safe,
)
from ..academic_years import hijri_academic_year_options
from ..hijri_utils import hijri_date
from ..context_processors import nav_context
from ..cache_utils import get_school_dashboard_payload
from ..gender_labels import school_gender_labels
from ..guidance import school_readiness
from ..audit_export import audit_csv_response
from ..models import Assignment, Meeting, Plan
from ..coverage import documented_teacher_ids, pending_documenters


# ========= دعم الأقسام =========
def _dept_code_for(dept_obj_or_code) -> str:
    if hasattr(dept_obj_or_code, "slug") and dept_obj_or_code.slug:
        return dept_obj_or_code.slug
    if hasattr(dept_obj_or_code, "code") and dept_obj_or_code.code:
        return dept_obj_or_code.code
    return str(dept_obj_or_code or "").strip()

def _arabic_label_for_in_school(dept_obj_or_code, active_school: Optional[School] = None) -> str:
    """نسخة آمنة من _arabic_label_for تربط التسمية بالمدرسة النشطة لتجنب تداخل slugs بين المدارس."""
    if hasattr(dept_obj_or_code, "name") and dept_obj_or_code.name:
        return dept_obj_or_code.name
    code = (
        getattr(dept_obj_or_code, "slug", None)
        or getattr(dept_obj_or_code, "code", None)
        or (dept_obj_or_code if isinstance(dept_obj_or_code, str) else "")
    )
    return _role_display_map(active_school).get(code, code or "—")

def _resolve_department_by_code_or_pk(code_or_pk: str, school: Optional[School] = None) -> Tuple[Optional[object], str, str]:
    dept_obj = None
    dept_code = (code_or_pk or "").strip()

    if Department is not None:
        try:
            qs = Department.objects.all()
            if school is not None:
                qs = qs.filter(school=school)
            dept_obj = qs.filter(slug__iexact=dept_code).first()
            if not dept_obj:
                try:
                    dept_obj = qs.filter(pk=int(dept_code)).first()
                except (ValueError, TypeError):
                    dept_obj = None
        except Exception:
            dept_obj = None

        if dept_obj:
            dept_code = getattr(dept_obj, "slug", dept_code)

    dept_label = _arabic_label_for_in_school(dept_obj or dept_code, school)
    return dept_obj, dept_code, dept_label


_DASHBOARD_PERIOD_LABELS = {
    "all": "الكل",
    "year": "هذا العام",
    "quarter": "هذا الربع",
    "month": "هذا الشهر",
}

# كم اسماً من المتأخرين تعرضه اللوحة قبل أن تُحيل إلى القائمة الكاملة.
# اللوحة تُعطي بداية الخيط لا الكشف كلّه — والصفحة تُكمل.
_COVERAGE_PREVIEW = 5


def _normalize_dashboard_period(raw: str | None) -> str:
    value = (raw or "all").strip().lower()
    return value if value in _DASHBOARD_PERIOD_LABELS else "all"


def _dashboard_period_start(period: str):
    now = timezone.now()
    if period == "year":
        return now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    if period == "quarter":
        quarter_month = ((now.month - 1) // 3) * 3 + 1
        return now.replace(month=quarter_month, day=1, hour=0, minute=0, second=0, microsecond=0)
    if period == "month":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return None


def _previous_period_window(period: str):
    """النافذة السابقة المكافئة للمقارنة — أو ``(None, None)`` إن تعذّرت.

    **لماذا مكافئة لا كاملة.** لو قُورن شهرٌ مضى منه ثلاثة أيام بشهرٍ كامل قبله
    لقال المؤشّر «انخفاض 90%» في اليوم الثالث من كل شهر، ثم يتعافى من تلقاء
    نفسه — وهو إنذارٌ كاذبٌ شهري يُعلّم المدير تجاهل السهم.

    فتُقاس المدة المنقضية من الفترة الحالية، ويُقارن بها **نفسُ المدة** من
    الفترة السابقة: ثلاثة أيام مقابل ثلاثة أيام.

    و«الكل» لا نظير له — المقارنة بما قبل بداية التاريخ لا معنى لها.
    """
    start = _dashboard_period_start(period)
    if start is None:
        return None, None

    now = timezone.now()
    elapsed = now - start

    if period == "year":
        previous_start = start.replace(year=start.year - 1)
    elif period == "quarter":
        month = start.month - 3
        year = start.year
        if month < 1:
            month += 12
            year -= 1
        previous_start = start.replace(year=year, month=month)
    else:  # month
        month = start.month - 1
        year = start.year
        if month < 1:
            month = 12
            year -= 1
        previous_start = start.replace(year=year, month=month)

    return previous_start, previous_start + elapsed


# كم يوماً يمتدّ أفق «ما هو قادم». أسبوعان: أطولُ من أن يفاجئ، وأقصرُ من أن
# يصير قائمةً تُتجاهل. وما فات موعده يبقى ظاهراً مهما بَعُد — الفائت لا يُخفى.
_AGENDA_HORIZON_DAYS = 14


def _school_agenda(active_school) -> dict:
    """المواعيد القادمة من مصادرها الأربعة على خطٍّ واحد.

    **لماذا تُجمع.** الاجتماعات في وحدة، والتكليفات في أخرى، والخطط في ثالثة،
    وتواقيع التعاميم في رابعة — ولكلٍّ صفحتها وتواريخها. ومدير المدرسة يعمل
    بتقويمٍ لا بأربع قوائم؛ فما لم تُجمع، يبقى الموعد معروفاً لمن يفتح صفحته
    ومنسيّاً عند من لا يفتحها.

    **ولماذا يبقى الفائت.** الأفق أربعةَ عشرَ يوماً إلى الأمام، أما ما فات
    موعده فيظهر مهما بَعُد ويتصدّر. فإخفاء المتأخّر بعد أسبوعٍ من فواته يجعل
    اللوحة أهدأ ممّا ينبغي، والصمت هنا ليس طمأنينة بل فقدُ أثر.

    استعلامٌ واحد لكل مصدر، وكلٌّ منها محدودٌ بعدده — فالتكلفة ثابتة مهما كبرت
    المدرسة. وأي مصدرٍ يتعثّر يسقط وحده: بقيّة التقويم أصدق من لا تقويم.
    """
    empty = {"items": [], "overdue": 0, "upcoming": 0, "horizon_days": _AGENDA_HORIZON_DAYS}
    if active_school is None:
        return empty

    now = timezone.now()
    horizon = now + timedelta(days=_AGENDA_HORIZON_DAYS)
    items: list[dict] = []

    def collect(source_name, builder):
        # مصدرٌ يتعثّر لا يُسقط التقويم كلّه — ويُسجَّل تعثّره ولا يُبتلع.
        try:
            items.extend(builder())
        except Exception:
            _degraded(f"dashboard.agenda.{source_name}", school_id=getattr(active_school, "pk", None))

    def meetings():
        rows = (
            Meeting.objects.filter(
                school=active_school,
                status=Meeting.Status.SCHEDULED,
                scheduled_at__lte=horizon,
            )
            .order_by("scheduled_at")
            .values("id", "title", "scheduled_at")[:20]
        )
        return [
            {
                "kind": "meeting",
                "label": "اجتماع",
                "icon": "fa-users-rectangle",
                "title": row["title"] or "اجتماع بلا عنوان",
                "at": row["scheduled_at"],
                "url": reverse("reports:meeting_detail", args=[row["id"]]),
            }
            for row in rows
        ]

    def assignments():
        rows = (
            Assignment.objects.filter(
                school=active_school,
                cancelled_at__isnull=True,
                due_at__isnull=False,
                due_at__lte=horizon,
            )
            .order_by("due_at")
            .values("id", "title", "due_at")[:20]
        )
        return [
            {
                "kind": "assignment",
                "label": "تكليف",
                "icon": "fa-list-check",
                "title": row["title"] or "تكليف بلا عنوان",
                "at": row["due_at"],
                # ‎assignment_detail‎ يأخذ مُعرّف *هدف* لا تكليف؛ والمدير مُصدِرٌ لا مُكلَّف.
                "url": reverse("reports:assignment_view", args=[row["id"]]),
            }
            for row in rows
        ]

    def signatures():
        rows = (
            Notification.objects.filter(
                school=active_school,
                requires_signature=True,
                signature_deadline_at__isnull=False,
                signature_deadline_at__lte=horizon,
            )
            .order_by("signature_deadline_at")
            .values("id", "title", "signature_deadline_at")[:20]
        )
        return [
            {
                "kind": "signature",
                "label": "توقيع تعميم",
                "icon": "fa-file-signature",
                "title": row["title"] or "تعميم بلا عنوان",
                "at": row["signature_deadline_at"],
                "url": reverse("reports:circulars_sent"),
            }
            for row in rows
        ]

    def plans():
        rows = (
            Plan.objects.filter(school=active_school, ends_on__isnull=False)
            .exclude(approval_state="draft")
            .order_by("ends_on")
            .values("id", "title", "ends_on")[:20]
        )
        out = []
        for row in rows:
            # ‎ends_on‎ تاريخٌ لا لحظة؛ يُنتهى منه بنهاية يومه لا بأوّله.
            ends_at = timezone.make_aware(
                datetime.combine(row["ends_on"], dt_time(23, 59)),
                timezone.get_current_timezone(),
            )
            if ends_at > horizon:
                continue
            out.append(
                {
                    "kind": "plan",
                    "label": "نهاية خطة",
                    "icon": "fa-diagram-project",
                    "title": row["title"] or "خطة بلا عنوان",
                    "at": ends_at,
                    "url": reverse("reports:plan_detail", args=[row["id"]]),
                }
            )
        return out

    collect("meetings", meetings)
    collect("assignments", assignments)
    collect("signatures", signatures)
    collect("plans", plans)

    for item in items:
        # الفرق يُحسب دائماً في اتجاهٍ موجب. فـ‎timedelta.days‎ يُقرِّب نحو
        # السالب لا نحو الصفر: موعدٌ فات قبل أربعين يوماً وثلاثِ ميكروثانية
        # يعطي ‎-41‎، فتصير قيمته المطلقة واحداً وأربعين — ويقرأ المدير
        # «متأخّر 41 يوماً» عن أربعين.
        item["is_overdue"] = item["at"] < now
        item["days"] = (now - item["at"]).days if item["is_overdue"] else (item["at"] - now).days
        item["hijri"] = hijri_date(timezone.localtime(item["at"]).date())
        item["time"] = timezone.localtime(item["at"]).strftime("%H:%M")

    # الفائت أولاً ثم الأقرب: الترتيب الزمني الصاعد يحقّق الأمرين معاً.
    items.sort(key=lambda item: item["at"])
    overdue = sum(1 for item in items if item["is_overdue"])

    return {
        "items": items[:8],
        "overdue": overdue,
        "upcoming": len(items) - overdue,
        "horizon_days": _AGENDA_HORIZON_DAYS,
    }


def _ticket_responsiveness(all_tickets_qs, closed_in_period_qs) -> dict:
    """سرعة الإغلاق وعمر أقدم مفتوح — لا نسبة الإنجاز وحدها.

    «إنجاز الطلبات 100%» جملةٌ تُخفي ما ينبغي أن تكشفه: طلبٌ بقي مفتوحاً ثلاثة
    أسابيع وطلبٌ أُغلق في ساعة كلاهما «مكتمل»، والنسبة تساويهما. والمدير لا
    يُسأل عن نسبته بل عن الطلب الذي شاخ عنده.

    فيُقاس أمران: **متوسط زمن الإغلاق** لما أُغلق في الفترة، و**عمر أقدم
    مفتوح** الآن — والثاني أهمّ، لأنه وحده يشير إلى شيءٍ قائم يُفعل به شيء.
    """
    empty = {
        "avg_close_hours": None,
        "avg_close_label": "",
        "oldest_open_days": None,
        "oldest_open_id": None,
        "oldest_open_title": "",
        "has_signal": False,
    }

    try:
        closed = list(
            closed_in_period_qs.filter(status=Ticket.Status.DONE)
            .values_list("created_at", "updated_at")[:500]
        )
        oldest = (
            all_tickets_qs.filter(
                status__in=[Ticket.Status.OPEN, Ticket.Status.IN_PROGRESS]
            )
            .order_by("created_at")
            .values("id", "title", "created_at")
            .first()
        )
    except Exception:
        logger.exception("Failed to measure ticket responsiveness")
        return empty

    result = dict(empty)

    spans = [
        (updated - created).total_seconds() / 3600
        for created, updated in closed
        if created and updated and updated >= created
    ]
    if spans:
        hours = sum(spans) / len(spans)
        result["avg_close_hours"] = round(hours, 1)
        # الساعات تُقرأ حتى يومين؛ وبعدهما الأيام أصدق في الذهن.
        if hours < 48:
            result["avg_close_label"] = f"{round(hours)} ساعة"
        else:
            result["avg_close_label"] = f"{round(hours / 24)} يوم"
        result["has_signal"] = True

    if oldest:
        age = (timezone.now() - oldest["created_at"]).days
        result["oldest_open_days"] = max(0, age)
        result["oldest_open_id"] = oldest["id"]
        result["oldest_open_title"] = oldest.get("title") or "بلا عنوان"
        result["has_signal"] = True

    return result


def _department_activity(active_school, reports_qs, documented_ids: set) -> list[dict]:
    """نشاط كل قسم داخل الفترة المختارة — مرتّباً بالأضعف أولاً.

    **لماذا يُنسب التقرير إلى كاتبه لا إلى نوعه.** في المشروع نسبةٌ قائمة عبر
    ``Department.reporttypes``، وهي تصلح لصفحة الأقسام لكنها لا تصلح للمقارنة:
    نوعٌ واحدٌ مشتركٌ بين قسمين يُحتسب مرتين، فيبدو القسمان أنشطَ مما هما.
    والمدير حين يقول «قسم العلوم كتب اثني عشر تقريراً» يعني معلّمي العلوم —
    فالنسبة عبر العضوية أقرب إلى ما يقصده.

    **ولماذا الأضعف أولاً.** القائمة تُقرأ من أعلاها، والمدير يتصرّف مع
    المتأخّر لا مع المتقدّم. فترتيبها بالأعلى تغطيةً يضع ما لا يحتاج عملاً في
    مقدمة النظر، ويدفن ما يحتاجه في ذيلها.

    استعلامان مجمّعان مهما بلغ عدد الأقسام — لا استعلامٌ لكل قسم.
    """
    if Department is None or DepartmentMembership is None or active_school is None:
        return []

    try:
        departments = list(
            Department.objects.filter(school=active_school, is_active=True).order_by("name")
        )
    except Exception:
        logger.exception("Failed to load departments for dashboard comparison")
        return []

    if not departments:
        return []

    department_ids = [d.pk for d in departments]
    members_by_department = defaultdict(set)
    try:
        rows = DepartmentMembership.objects.filter(
            department_id__in=department_ids
        ).values_list("department_id", "teacher_id")
        for department_id, teacher_id in rows:
            if department_id and teacher_id:
                members_by_department[int(department_id)].add(int(teacher_id))
    except Exception:
        logger.exception("Failed to batch department memberships for dashboard comparison")
        return []

    reports_by_teacher = defaultdict(int)
    try:
        for row in (
            reports_qs.exclude(teacher__isnull=True)
            .values("teacher_id")
            .annotate(total=Count("id"))
        ):
            reports_by_teacher[int(row["teacher_id"])] = int(row.get("total") or 0)
    except Exception:
        logger.exception("Failed to batch report counts for dashboard comparison")
        return []

    items = []
    for department in departments:
        member_ids = members_by_department.get(int(department.pk), set())
        members = len(member_ids)
        # قسمٌ بلا أعضاء لا يُقارَن: تغطيته ‎0%‎ ليست تأخّراً بل فراغ تنظيمي،
        # وإظهاره في صدارة المتأخرين يُغرق القائمة بما لا إجراء له.
        if not members:
            continue

        documented = len(member_ids & documented_ids)
        items.append(
            {
                "id": department.pk,
                "name": department.name,
                "members": members,
                "documented": documented,
                "pending": members - documented,
                "reports": sum(reports_by_teacher.get(tid, 0) for tid in member_ids),
                "percent": round(documented * 100 / members),
            }
        )

    items.sort(key=lambda item: (item["percent"], -item["members"]))
    return items


def _trend(current: int, previous: int) -> dict:
    """فرق النافذتين في صورةٍ جاهزة للعرض.

    ``direction`` ثلاثيّ لا ثنائي: ``flat`` حالةٌ قائمة بذاتها، إذ السهم
    الصاعد على فرقٍ صفر يكذب. و``percent`` يبقى ``None`` عند انطلاقٍ من صفر —
    فالنسبة من لا شيء ليست «زيادة 100%» بل لا نسبة لها.
    """
    delta = int(current) - int(previous)
    if delta > 0:
        direction = "up"
    elif delta < 0:
        direction = "down"
    else:
        direction = "flat"

    percent = None
    if previous > 0:
        percent = round(delta * 100 / previous)

    return {
        "previous": int(previous),
        "delta": delta,
        "direction": direction,
        "percent": percent,
    }


def _build_manager_focus_items(
    *,
    tickets_open: int,
    pending_achievement_files: int,
    assigned_to_me: int,
    notifications_unread: int,
    signatures_pending: int,
) -> list[dict]:
    """Build the manager's follow-up list once, for both the total and the chips.

    Rows flagged ``subset`` are already contained in another row — a ticket
    assigned to the manager is also an open school ticket — so they are shown as
    a drill-down but never counted twice in the headline number.
    """
    items = [
        {
            "key": "tickets",
            "count": int(tickets_open or 0),
            "title": "طلبات المدرسة المفتوحة",
            "hint": "تنتظر المتابعة أو الإسناد",
            "url": f"{reverse('reports:manager_school_tickets')}?status=attention",
            "subset": False,
        },
        {
            "key": "achievement",
            "count": int(pending_achievement_files or 0),
            "title": "اعتمادات الإنجاز",
            "hint": "ملفات مرسلة للمراجعة",
            "url": f"{reverse('reports:achievement_school_files')}?status=submitted",
            "subset": False,
        },
        {
            "key": "notifications",
            "count": int(notifications_unread or 0),
            "title": "إشعارات غير مقروءة",
            "hint": "آخر المستجدات",
            "url": reverse("reports:my_notifications"),
            "subset": False,
        },
        {
            "key": "signatures",
            "count": int(signatures_pending or 0),
            "title": "توقيعات مطلوبة منك",
            "hint": "تعاميم بانتظار الإقرار",
            "url": reverse("reports:my_circulars"),
            "subset": False,
        },
        {
            "key": "assigned",
            "count": int(assigned_to_me or 0),
            "title": "منها معيّنة لك",
            "hint": "ضمن طلبات المدرسة المفتوحة أعلاه",
            "url": reverse("reports:assigned_to_me"),
            "subset": True,
        },
    ]
    return [item for item in items if item["count"] > 0]


def _build_school_dashboard_payload(active_school: Optional[School], period: str, *, reporttypes_count: int = 0) -> dict:
    """Build the JSON payload used by the school dashboard UI.

    Keeping this server-side makes the dashboard API and the rendered page share
    one source of truth for counters and chart data.
    """
    period = _normalize_dashboard_period(period)
    start_at = _dashboard_period_start(period)
    now = timezone.now()

    teachers_qs = Teacher.objects.filter(is_active=True)
    if active_school is not None:
        teachers_qs = teachers_qs.filter(
            school_memberships__school=active_school,
            school_memberships__is_active=True,
            school_memberships__role_type__in=SchoolMembership.STAFF_ROLES,
        ).distinct()

    reports_qs = _filter_by_school(Report.objects.all(), active_school)
    all_school_tickets_qs = _filter_by_school(
        Ticket.objects.filter(is_platform=False),
        active_school,
    )
    tickets_qs = all_school_tickets_qs
    if start_at is not None:
        reports_qs = reports_qs.filter(created_at__gte=start_at)
        tickets_qs = tickets_qs.filter(created_at__gte=start_at)

    ticket_agg = tickets_qs.aggregate(
        total=Count("id"),
        done=Count("id", filter=Q(status="done")),
        rejected=Count("id", filter=Q(status="rejected")),
    )
    actionable_tickets_open = all_school_tickets_qs.filter(
        status__in=[Ticket.Status.OPEN, Ticket.Status.IN_PROGRESS],
    ).count()

    reports_count = int(reports_qs.count())
    teachers_count = int(teachers_qs.count())

    # ── تغطية التوثيق ────────────────────────────────────────────────────
    # كانت اللوحة تعرف طرفَي المسألة ولا تعرف ما بينهما: «44 معلماً» و«2 تقرير»،
    # ولا تقول أيُّ اثنين. ووظيفة المدير ليست معرفة العدد بل معرفة الاسم.
    #
    # التغطية تُقاس على الفترة المختارة لا على عمر المدرسة: من وثّق العام الماضي
    # ولم يوثّق هذا الشهر متأخّرٌ اليوم، لا مغطّى.
    # الحساب في ``reports.coverage`` لا هنا: تحتاجه شاشة التذكير أيضاً، ولو
    # نُسخ لاختلفت الشاشتان يوماً — فيقرأ المدير خمسةً ويُرسل إلى ستة.
    documented_ids = documented_teacher_ids(active_school, since=start_at)
    covered_count = len(documented_ids)
    # القائمة تُقتطع للعرض: اللوحة تُعطي الاسم والوجهة، والصفحة تُعطي البقية.
    pending_teachers = list(
        pending_documenters(active_school, since=start_at)[:_COVERAGE_PREVIEW]
    )
    coverage_percent = round(covered_count * 100 / teachers_count) if teachers_count else 0

    # ── الاتجاه ──────────────────────────────────────────────────────────
    # الرقم الذي لا يُقارَن لا معنى له: «2 تقرير» ليست جيدةً ولا سيئة حتى
    # تُوضع بجوار ما قبلها.
    previous_start, previous_end = _previous_period_window(period)
    trends = {}
    if previous_start is not None:
        previous_reports = _filter_by_school(Report.objects.all(), active_school).filter(
            created_at__gte=previous_start, created_at__lt=previous_end
        )
        previous_tickets = all_school_tickets_qs.filter(
            created_at__gte=previous_start, created_at__lt=previous_end
        )
        previous_documented = (
            previous_reports.exclude(teacher__isnull=True)
            .values_list("teacher_id", flat=True)
            .distinct()
            .count()
        )
        trends = {
            "reports_count": _trend(reports_count, previous_reports.count()),
            "tickets_total": _trend(int(ticket_agg.get("total") or 0), previous_tickets.count()),
            "coverage_covered": _trend(covered_count, int(previous_documented)),
        }

    chart_start = start_at or (now - timedelta(weeks=8))
    reports_by_week = (
        reports_qs.filter(created_at__gte=chart_start)
        .annotate(week=TruncWeek("created_at"))
        .values("week")
        .annotate(count=Count("id"))
        .order_by("week")
    )
    reports_labels = []
    reports_data = []
    for item in reports_by_week:
        week_value = item.get("week")
        if week_value:
            reports_labels.append(week_value.strftime("%d/%m"))
            reports_data.append(int(item.get("count") or 0))

    reports_by_category = (
        reports_qs.values("category__name")
        .annotate(count=Count("id"))
        .order_by("-count")[:6]
    )
    category_labels = []
    category_data = []
    for item in reports_by_category:
        category_labels.append(item.get("category__name") or "غير محدد")
        category_data.append(int(item.get("count") or 0))

    teachers_labels = []
    teachers_data = []
    if active_school is not None and Department is not None:
        teachers_by_dept_qs = (
            Department.objects.filter(school=active_school)
            .annotate(teacher_count=Count("memberships__teacher", distinct=True))
            .order_by("-teacher_count")[:6]
        )
        for dept in teachers_by_dept_qs:
            teachers_labels.append(dept.name)
            teachers_data.append(int(dept.teacher_count or 0))

    return {
        "period": period,
        "period_label": _DASHBOARD_PERIOD_LABELS.get(period, "الكل"),
        "has_comparison": previous_start is not None,
        "generated_at": timezone.localtime(now).strftime("%Y-%m-%d %H:%M"),
        "kpis": {
            "reports_count": reports_count,
            "teachers_count": teachers_count,
            "reporttypes_count": int(reporttypes_count or 0),
            "tickets_total": int(ticket_agg.get("total") or 0),
            # عناصر المتابعة يجب ألا تختفي عند تغيير فترة التحليلات.
            "tickets_open": int(actionable_tickets_open),
            "tickets_done": int(ticket_agg.get("done") or 0),
            "tickets_rejected": int(ticket_agg.get("rejected") or 0),
        },
        # نطاق كل مؤشّر مُعلَنٌ بدل الاعتذار بين قوسين تحت البطاقات. بعضها
        # يتبع الفترة وبعضها لا يتبعها عمداً — الطلبات المفتوحة يجب ألا تختفي
        # لأن المدير بدّل نافذة التحليل — والفرق يجب أن يُقال لا أن يُخمَّن.
        "kpi_scopes": {
            "reports_count": "period",
            "tickets_total": "period",
            "tickets_done": "period",
            "tickets_rejected": "period",
            "teachers_count": "current",
            "tickets_open": "current",
            "reporttypes_count": "current",
        },
        "trends": trends,
        "responsiveness": _ticket_responsiveness(all_school_tickets_qs, tickets_qs),
        "departments": _department_activity(active_school, reports_qs, documented_ids),
        "coverage": {
            "covered": covered_count,
            "total": teachers_count,
            "pending": max(0, teachers_count - covered_count),
            "percent": coverage_percent,
            # مدرسةٌ بلا منسوبين ليست مدرسةً وثّق جميعُها: صفرٌ من صفر ليس
            # نجاحاً بل غياب فريق. ولو تُرك للقالب أن يستنتج من ‎pending == 0‎
            # لقال لمدرسةٍ جديدة «وثّق الجميع» في أول يومٍ لها — وهي أول جملة
            # يقرؤها المدير، فتُعلّمه أن اللوحة تجامل.
            "has_staff": teachers_count > 0,
            "pending_preview": [
                {"id": t.pk, "name": t.name or t.phone}
                for t in pending_teachers
            ],
        },
        "charts": {
            "reports": {"labels": reports_labels, "data": reports_data},
            "categories": {"labels": category_labels, "data": category_data},
            "teachers": {"labels": teachers_labels, "data": teachers_data},
        },
    }

def _members_for_department(dept_code: str, school: Optional[School] = None):
    if not dept_code:
        return Teacher.objects.none()
    if DepartmentMembership is None:
        return Teacher.objects.none()

    mem_qs = DepartmentMembership.objects.filter(department__slug__iexact=dept_code)
    if school is not None:
        mem_qs = mem_qs.filter(department__school=school)
    member_ids = mem_qs.values_list("teacher_id", flat=True)

    qs = Teacher.objects.filter(is_active=True, id__in=member_ids).distinct()
    if school is not None:
        qs = qs.filter(
            school_memberships__school=school,
        )
    return qs.order_by("name")

def _user_department_codes(user, active_school: Optional[School] = None) -> list[str]:
    codes = set()

    # في وضع تعدد المدارس، يجب تحديد المدرسة النشطة لتجنب تداخل slugs بين المدارس
    try:
        if active_school is None and School.objects.filter(is_active=True)[:2].count() > 1:
            return []
    except Exception:
        # fail-closed إذا تعذر تحديد عدد المدارس
        if active_school is None:
            return []

    if DepartmentMembership is not None:
        try:
            mem_qs = DepartmentMembership.objects.filter(teacher=user)
            if active_school is not None:
                mem_qs = mem_qs.filter(department__school=active_school)
            mem_codes = mem_qs.values_list("department__slug", flat=True)
            for c in mem_codes:
                if c:
                    codes.add(c)
        except Exception:
            logger.exception("Failed to fetch user department codes")

    return list(codes)

def _tickets_stats_for_department(dept_code: str, school: Optional[School] = None) -> dict:
    from django.db.models import Count, Q as _Q
    qs = Ticket.objects.filter(department__slug=dept_code)
    qs = _filter_by_school(qs, school)
    stats = qs.aggregate(
        open=Count("id", filter=_Q(status="open")),
        in_progress=Count("id", filter=_Q(status="in_progress")),
        done=Count("id", filter=_Q(status="done")),
    )
    return stats

def _all_departments(active_school: Optional[School] = None):
    if Department is None:
        return []

    qs = Department.objects.all().order_by("id")
    if active_school is not None and hasattr(Department, "school"):
        qs = qs.filter(school=active_school)

    departments = list(qs)
    if not departments:
        return []

    department_ids = [d.pk for d in departments if getattr(d, "pk", None) is not None]
    department_codes = [_dept_code_for(d) for d in departments]

    ticket_stats_map = defaultdict(lambda: {"open": 0, "in_progress": 0, "done": 0})
    if Ticket is not None and department_ids:
        try:
            ticket_qs = Ticket.objects.filter(department_id__in=department_ids)
            if active_school is not None:
                ticket_qs = ticket_qs.filter(school=active_school)
            ticket_rows = ticket_qs.values("department_id").annotate(
                open_count=Count("id", filter=Q(status=Ticket.Status.OPEN)),
                in_progress_count=Count("id", filter=Q(status=Ticket.Status.IN_PROGRESS)),
                done_count=Count("id", filter=Q(status=Ticket.Status.DONE)),
            )
            for row in ticket_rows:
                ticket_stats_map[row["department_id"]] = {
                    "open": int(row.get("open_count") or 0),
                    "in_progress": int(row.get("in_progress_count") or 0),
                    "done": int(row.get("done_count") or 0),
                }
        except Exception:
            logger.exception("Failed to batch ticket stats for departments list")

    membership_teacher_ids_by_department = defaultdict(set)
    if DepartmentMembership is not None and department_ids:
        try:
            membership_rows = DepartmentMembership.objects.filter(
                department_id__in=department_ids
            ).values_list("department_id", "teacher_id")
            for department_id, teacher_id in membership_rows:
                if department_id and teacher_id:
                    membership_teacher_ids_by_department[int(department_id)].add(int(teacher_id))
        except Exception:
            logger.exception("Failed to batch department memberships for departments list")

    # عدد تقارير كل قسم = مجموع تقارير أنواع التقارير المرتبطة بالقسم (ضمن المدرسة النشطة)
    reports_count_by_department = defaultdict(int)
    if Report is not None and department_ids and hasattr(Department, "reporttypes"):
        try:
            report_qs = Report.objects.all()
            if active_school is not None:
                report_qs = report_qs.filter(school=active_school)
            counts_by_type = {
                int(r["category_id"]): int(r["c"] or 0)
                for r in report_qs.values("category_id").annotate(c=Count("id"))
                if r.get("category_id")
            }
            through = Department.reporttypes.through
            m2m_rows = (
                through.objects
                .filter(department_id__in=department_ids)
                .values_list("department_id", "reporttype_id")
            )
            for did, rtid in m2m_rows:
                if did and rtid:
                    reports_count_by_department[int(did)] += counts_by_type.get(int(rtid), 0)
        except Exception:
            logger.exception("Failed to batch report counts for departments list")

    items = []
    for department in departments:
        code = _dept_code_for(department)
        stats = ticket_stats_map.get(department.pk) or {"open": 0, "in_progress": 0, "done": 0}
        member_ids = membership_teacher_ids_by_department.get(int(department.pk), set())
        members_count = len(member_ids)
        reports_count = reports_count_by_department.get(int(department.pk), 0)

        items.append(
            {
                "pk": department.pk,
                "slug": code,
                "code": code,
                "name": _arabic_label_for_in_school(department, active_school),
                "is_active": getattr(department, "is_active", True),
                "members_count": members_count,
                "reports_count": reports_count,
                "stats": stats,
                "tickets_summary": f"{stats['open']} / {stats['in_progress']} / {stats['done']}",
            }
        )

    return items

class _DepartmentForm(forms.ModelForm):
    class Meta:
        model = Department
        fields: list[str] = []
        if model is not None:
            for fname in ("name", "slug", "role_label", "is_active"):
                if hasattr(model, fname):
                    fields.append(fname)

    def clean(self):
        cleaned = super().clean()
        return cleaned

def get_department_form():
    if Department is not None and 'DepartmentForm' in globals() and (DepartmentForm is not None):
        return DepartmentForm
    if Department is not None:
        return _DepartmentForm
    return None


# ---- إعدادات المدرسة الحالية (لمدير المدرسة أو مالك النظام) ----
# قائمة السنوات مشتركة مع نموذج رفع الوثائق — انظر ``reports/academic_years.py``.


class _SchoolSettingsForm(forms.ModelForm):
    class Meta:
        model = School
        fields = [
            "name",
            "stage",
            "gender",
            "city",
            "phone",
            "email",
            "current_academic_year",
            "share_link_default_days",
            # مفتاح دورة الاعتماد. مطفأ افتراضياً، فالمدرسة تتبنّاها عن قصد لا
            # بترقية تفاجئها — والميزة التي لا يمكن تشغيلها لا وجود لها.
            "report_approval_enabled",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        year_options = hijri_academic_year_options(self.instance)
        choices = [(y, f"{y} هـ") for y in year_options]

        # السنة الحالية: قائمة منسدلة بدل الإدخال اليدوي
        self.fields["current_academic_year"] = forms.ChoiceField(
            label="السنة الدراسية الحالية (هجري)",
            required=True,
            choices=[("", "— اختر السنة —")] + choices,
            widget=forms.Select(),
        )
        self.fields["current_academic_year"].initial = (
            getattr(self.instance, "current_academic_year", "") or ""
        ).strip()
        # ملاحظة: السنوات المتاحة للمدارس صارت تُدار مركزيًا من لوحة الآدمن
        # (نموذج AcademicYear)، لذا أُزيل حقل اختيارها هنا منعًا للتكرار/الالتباس.

    def clean_current_academic_year(self):
        import re

        value = (self.cleaned_data.get("current_academic_year") or "").strip().replace("–", "-").replace("—", "-")
        if not value:
            return value
        if not re.match(r"^\d{4}-\d{4}$", value):
            raise forms.ValidationError("صيغة السنة الحالية يجب أن تكون مثل 1447-1448")
        start, end = value.split("-", 1)
        if int(end) != int(start) + 1:
            raise forms.ValidationError("السنة الحالية يجب أن تكون بفارق سنة واحدة، مثل 1447-1448")
        return value

    def clean_email(self):
        return (self.cleaned_data.get("email") or "").strip().lower()


@login_required(login_url="reports:login")
@require_http_methods(["GET", "POST"])
def school_settings(request: HttpRequest) -> HttpResponse:
    """إعدادات المدرسة الحالية (الاسم، الشعار...).

    - متاحة لمدير المدرسة على مدرسته النشطة فقط.
    - متاحة لمالك النظام على أي مدرسة بعد اختيارها كـ active_school.
    """
    active_school = _get_active_school(request)
    if active_school is None:
        messages.error(request, "فضلاً اختر مدرسة أولاً.")
        return redirect("reports:select_school")

    # تحقق من الصلاحيات
    if not (getattr(request.user, "is_superuser", False) or active_school in _user_manager_schools(request.user)):
        messages.error(request, "لا تملك صلاحية تعديل إعدادات هذه المدرسة.")
        return redirect("reports:admin_dashboard")

    # حماية جزئية: منع التعديل على الحقول المطلوبة فقط.
    protected_fields = {"name", "stage", "gender", "city"}
    form = _SchoolSettingsForm(request.POST or None, request.FILES or None, instance=active_school)

    for field_name, field in form.fields.items():
        if field_name in protected_fields:
            field.disabled = True
            attrs = dict(getattr(field.widget, "attrs", {}) or {})
            attrs["disabled"] = True
            attrs["readonly"] = True
            field.widget.attrs = attrs

    if request.method == "POST":
        if form.is_valid():
            form.save()
            messages.success(request, "تم تحديث إعدادات المدرسة بنجاح.")
            return redirect("reports:admin_dashboard")
        # في حال وجود أخطاء نعرضها للمستخدم ليسهل معرفة سبب الفشل
        messages.error(request, "تعذّر الحفظ. تحقّق من الحقول.")
        try:
            for field, errors in form.errors.items():
                label = form.fields.get(field).label if field in form.fields else field
                joined = "; ".join(errors)
                messages.error(request, f"{label}: {joined}")
        except Exception:
            _degraded("forms.render_error_messages")
            # لا نكسر الصفحة إن حدث خطأ أثناء بناء الرسالة
            pass

    return render(
        request,
        "reports/school_settings.html",
        {"form": form, "school": active_school},
    )


# ---- إدارة المدارس (إنشاء/تعديل/حذف) لمالك النظام ----
class _SchoolAdminForm(forms.ModelForm):
    class Meta:
        model = School
        fields = [
            "name",
            "code",
            "stage",
            "gender",
            "city",
            "phone",
            "is_active",
        ]
        # الكود الداخلي مخفي عن المستخدم ويُولَّد تلقائيًا من الاسم.
        widgets = {"code": forms.HiddenInput()}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if "code" in self.fields:
            self.fields["code"].required = False

    def _slugify_code(self, text: str) -> str:
        from django.utils.text import slugify
        try:
            from unidecode import unidecode  # type: ignore
            text = unidecode(text or "")
        except ImportError:
            # حزمةٌ اختيارية بحقّ: بدونها يُشتقّ الرمز من النص كما هو.
            pass
        return slugify(text or "", allow_unicode=False)

    def clean_code(self):
        code = (self.cleaned_data.get("code") or "").strip().lower()
        if not code:
            code = self._slugify_code(self.cleaned_data.get("name") or "")
        if not code:
            code = "school"
        # ضمان كود فريد (مع استبعاد السجلّ الحالي عند التعديل)
        base = code[:60]
        candidate = base
        counter = 2
        qs = School.objects.all()
        if getattr(self.instance, "pk", None):
            qs = qs.exclude(pk=self.instance.pk)
        while qs.filter(code=candidate).exists():
            suffix = f"-{counter}"
            candidate = f"{base[:60 - len(suffix)]}{suffix}"
            counter += 1
        return candidate


@login_required(login_url="reports:platform_login")
@user_passes_test(lambda u: getattr(u, "is_superuser", False), login_url="reports:platform_login")
@require_http_methods(["GET", "POST"])
def school_create(request: HttpRequest) -> HttpResponse:
    form = _SchoolAdminForm(request.POST or None)
    if request.method == "POST":
        if form.is_valid():
            form.save()
            messages.success(request, "تم إنشاء المدرسة بنجاح.")
            return redirect("reports:schools_admin_list")
        messages.error(request, "تعذّر الحفظ. تحقّق من الحقول.")
    return render(request, "reports/school_form.html", {"form": form, "mode": "create"})


@login_required(login_url="reports:platform_login")
@user_passes_test(lambda u: getattr(u, "is_superuser", False), login_url="reports:platform_login")
@require_http_methods(["GET", "POST"])
def school_update(request: HttpRequest, pk: int) -> HttpResponse:
    school = get_object_or_404(School, pk=pk)
    form = _SchoolAdminForm(request.POST or None, instance=school)
    if request.method == "POST":
        if form.is_valid():
            form.save()
            messages.success(request, "تم تحديث بيانات المدرسة.")
            return redirect("reports:schools_admin_list")
        messages.error(request, "تعذّر الحفظ. تحقّق من الحقول.")
    return render(request, "reports/school_form.html", {"form": form, "mode": "edit", "school": school})


@login_required(login_url="reports:platform_login")
@user_passes_test(lambda u: getattr(u, "is_superuser", False), login_url="reports:platform_login")
@require_http_methods(["GET", "POST"])
def school_delete(request: HttpRequest, pk: int) -> HttpResponse:
    school = get_object_or_404(School, pk=pk)
    name = school.name
    code = school.code

    if request.method == "GET":
        related_counts = {
            "members": SchoolMembership.objects.filter(school=school).count(),
            "reports": Report.objects.filter(school=school).count(),
            "tickets": Ticket.objects.filter(school=school).count(),
        }
        return render(
            request,
            "reports/school_delete_confirm.html",
            {"school": school, "related_counts": related_counts},
        )

    confirm_name = (request.POST.get("confirm_name") or "").strip()
    if confirm_name != name:
        messages.error(request, "اكتب اسم المدرسة كما هو لتأكيد الحذف النهائي.")
        related_counts = {
            "members": SchoolMembership.objects.filter(school=school).count(),
            "reports": Report.objects.filter(school=school).count(),
            "tickets": Ticket.objects.filter(school=school).count(),
        }
        return render(
            request,
            "reports/school_delete_confirm.html",
            {"school": school, "related_counts": related_counts, "confirm_name": confirm_name},
            status=400,
        )

    from ..middleware import set_audit_logging_suppressed

    school_id = school.pk
    try:
        with transaction.atomic():
            set_audit_logging_suppressed(True)
            Report.all_objects.filter(school=school).delete()
            school.delete()
    except Exception:
        logger.exception("school_delete failed")
        messages.error(request, "تعذّر حذف المدرسة. ربما توجد قيود على البيانات المرتبطة.")
        return redirect("reports:schools_admin_list")
    finally:
        set_audit_logging_suppressed(False)

    try:
        AuditLog.objects.create(
            school=None,
            teacher=request.user,
            action=AuditLog.Action.DELETE,
            model_name="School",
            object_id=school_id,
            object_repr=name[:255],
            changes={
                "school_name": name,
                "school_code": code,
                "deletion_scope": "school_and_related_data",
            },
            ip_address=request.META.get("REMOTE_ADDR"),
            user_agent=(request.META.get("HTTP_USER_AGENT") or "")[:500],
        )
    except Exception:
        logger.exception("school deletion audit creation failed for school_id=%s", school_id)

    messages.success(request, f"تم حذف المدرسة «{name}» وكل بياناتها المرتبطة.")
    return redirect("reports:schools_admin_list")


# ---- لوحة إدارة المدارس ومدراء المدارس (للسوبر أدمن) ----
@login_required(login_url="reports:platform_login")
@user_passes_test(lambda u: getattr(u, "is_superuser", False), login_url="reports:platform_login")
@require_http_methods(["GET"])
def schools_admin_list(request: HttpRequest) -> HttpResponse:
    q = _clean_query_value(request.GET.get("q"))
    schools_qs = (
        School.objects.all()
        .select_related("subscription")
        .prefetch_related(
            Prefetch(
                "memberships",
                queryset=SchoolMembership.objects.select_related("teacher").filter(
                    role_type=SchoolMembership.RoleType.MANAGER,
                    is_active=True,
                ),
                to_attr="manager_memberships",
            )
        )
    )

    if q:
        schools_qs = schools_qs.filter(
            Q(name__icontains=q)
            | Q(code__icontains=q)
            | Q(city__icontains=q)
            | Q(phone__icontains=q)
            | Q(memberships__teacher__name__icontains=q)
            | Q(memberships__teacher__phone__icontains=q)
        ).distinct()

    page_obj = Paginator(schools_qs.order_by("name", "id"), 24).get_page(request.GET.get("page") or 1)

    items = []
    for s in page_obj:
        managers = [m.teacher for m in getattr(s, "manager_memberships", []) if m.teacher]
        items.append({"school": s, "managers": managers})

    return render(
        request,
        "reports/schools_admin_list.html",
        {
            "schools": items,
            "page_obj": page_obj,
            "q": q,
            "total_schools_count": page_obj.paginator.count,
            "query_params_without_page": _clean_query_params(request.GET),
        },
    )


@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def school_profile(request: HttpRequest, pk: int) -> HttpResponse:
    """بروفايل تفصيلي لمدرسة واحدة.

    - السوبر أدمن يمكنه عرض أي مدرسة.
    - مدير المدرسة يمكنه عرض المدارس التي يديرها فقط.
    """
    school = get_object_or_404(School, pk=pk)

    user = request.user
    allowed = False
    if getattr(user, "is_superuser", False):
        allowed = True
    elif _is_staff(user) and school in _user_manager_schools(user):
        allowed = True

    if not allowed:
        messages.error(request, "لا تملك صلاحية عرض هذه المدرسة.")
        return redirect("reports:admin_dashboard")

    # إحصائيات بسيطة للمدرسة
    reports_count = Report.objects.filter(school=school).count()

    # Single aggregate query for all ticket counts instead of 4 separate queries
    from django.db.models import Count, Q as _Q
    ticket_stats = Ticket.objects.filter(school=school).aggregate(
        total=Count("id"),
        open=Count("id", filter=_Q(status__in=["open", "in_progress"])),
        done=Count("id", filter=_Q(status="done")),
        rejected=Count("id", filter=_Q(status="rejected")),
    )
    tickets_total = ticket_stats["total"]
    tickets_open = ticket_stats["open"]
    tickets_done = ticket_stats["done"]
    tickets_rejected = ticket_stats["rejected"]

    teachers_qs = (
        Teacher.objects.filter(
            school_memberships__school=school,
            school_memberships__is_active=True,
        )
        .distinct()
        .order_by("name")
    )
    teachers_count = teachers_qs.count()

    departments_count = 0
    departments = []
    if Department is not None:
        try:
            depts_qs = Department.objects.filter(is_active=True)
            if DepartmentMembership is not None:
                depts_qs = (
                    depts_qs.filter(
                        memberships__teacher__school_memberships__school=school,
                        memberships__teacher__school_memberships__is_active=True,
                    )
                    .distinct()
                    .order_by("name")
                )
            departments_count = depts_qs.count()
            departments = list(depts_qs[:20])  # عرض عينات محدودة في القالب إن لزم
        except Exception:
            logger.exception("school_profile departments stats failed")

    context = {
        "school": school,
        "reports_count": reports_count,
        "tickets_total": tickets_total,
        "tickets_open": tickets_open,
        "tickets_done": tickets_done,
        "tickets_rejected": tickets_rejected,
        "teachers_count": teachers_count,
        "teachers": list(teachers_qs[:20]),  # أقصى 20 للعرض السريع
        "departments_count": departments_count,
        "departments": departments,
    }
    return render(request, "reports/school_profile.html", context)


@login_required(login_url="reports:platform_login")
@user_passes_test(lambda u: getattr(u, "is_superuser", False), login_url="reports:platform_login")
@require_http_methods(["GET", "POST"])
def school_managers_manage(request: HttpRequest, pk: int) -> HttpResponse:
    school = get_object_or_404(School, pk=pk)
    labels = school_gender_labels(school)

    if request.method == "POST":
        action = request.POST.get("action")
        teacher_id = coerce_pk(request.POST.get("teacher_id"))
        if not teacher_id:
            messages.error(request, f"الرجاء اختيار {labels['teacher']}.")
            return redirect("reports:school_managers_manage", pk=school.pk)
        try:
            teacher = Teacher.objects.get(pk=teacher_id)
        except Teacher.DoesNotExist:
            messages.error(request, f"{labels['teacher']} غير موجودة." if labels["is_girls"] else f"{labels['teacher']} غير موجود.")
            return redirect("reports:school_managers_manage", pk=school.pk)

        if action == "add":
            manager_email = (getattr(teacher, "email", "") or "").strip()
            if not manager_email:
                messages.error(request, f"لا يمكن تعيين {labels['manager']} بدون بريد إلكتروني. حدّث بيانات المستخدم أولاً.")
                return redirect("reports:school_managers_manage", pk=school.pk)

            # لا نسمح بأكثر من مدير نشط واحد لكل مدرسة
            other_manager_exists = SchoolMembership.objects.filter(
                school=school,
                role_type=SchoolMembership.RoleType.MANAGER,
                is_active=True,
            ).exclude(teacher=teacher).exists()
            if other_manager_exists:
                messages.error(request, "لا يمكن تعيين أكثر من حساب إدارة نشط للمدرسة نفسها. ألغِ تعيين الحساب الحالي أولاً.")
                return redirect("reports:school_managers_manage", pk=school.pk)

            SchoolMembership.objects.update_or_create(
                school=school,
                teacher=teacher,
                role_type=SchoolMembership.RoleType.MANAGER,
                defaults={"is_active": True},
            )
            messages.success(request, f"تم تعيين {teacher.name} بصفة {labels['manager']}.")
        elif action == "remove":
            SchoolMembership.objects.filter(
                school=school,
                teacher=teacher,
                role_type=SchoolMembership.RoleType.MANAGER,
            ).update(is_active=False)
            messages.success(request, f"تم إلغاء إدارة {teacher.name} لهذه المدرسة.")
        else:
            messages.error(request, "إجراء غير معروف.")

        return redirect("reports:school_managers_manage", pk=school.pk)

    managers = (
        Teacher.objects.filter(
            school_memberships__school=school,
            school_memberships__role_type=SchoolMembership.RoleType.MANAGER,
            school_memberships__is_active=True,
        )
        .distinct()
        .order_by("name")
    )

    # في قائمة الإضافة نظهر فقط الحسابات التي لديها عضوية إدارة فعلية في مدرسة ما.
    teachers = (
        Teacher.objects.filter(is_active=True)
        .filter(
            school_memberships__role_type=SchoolMembership.RoleType.MANAGER,
            school_memberships__is_active=True,
        )
        .exclude(
            school_memberships__school=school,
            school_memberships__role_type=SchoolMembership.RoleType.MANAGER,
            school_memberships__is_active=True,
        )
        .distinct()
        .order_by("name")
    )

    context = {
        "school": school,
        "managers": managers,
        "teachers": teachers,
    }
    return render(request, "reports/school_managers_manage.html", context)

# ---- لوحة المدير المجمعة ----
# ``role_required({"manager"})`` وحده يحسم الوصول هنا: يمرّ مالك النظام، ويمرّ
# مدير المدرسة النشطة، ويُردّ من عداهما إلى الرئيسية برسالة. وكان فوقه
# ``user_passes_test(_is_staff)`` لا يضيف قيداً — فمن يمرّ من الأول يمرّ من
# الثاني — بل يسبقه فيرمي المسجَّلَ بالفعل إلى **شاشة دخول** لا يحتاجها.
@login_required(login_url="reports:login")
@role_required({"manager"})
@require_http_methods(["GET", "POST"])
def admin_dashboard(request: HttpRequest) -> HttpResponse:
    """لوحة عمل مدير المدرسة."""
    import json
    
    # إذا لم يكن هناك مدرسة مختارة نوجّه لاختيار مدرسة أولاً
    active_school = _get_active_school(request)
    # السوبر يوزر يمكنه رؤية أي مدرسة، المدير مقيد بمدارسه فقط
    if School.objects.filter(is_active=True).exists():
        if active_school is None:
            return redirect("reports:select_school")
        if (not request.user.is_superuser) and active_school not in _user_manager_schools(request.user):
            messages.error(request, "ليست لديك صلاحية كمدير على هذه المدرسة.")
            return redirect("reports:select_school")

    ctx = {
        "has_dept_model": Department is not None,
        "active_school": active_school,
    }

    if request.method == "POST":
        # The dashboard has no forms of its own; every POST here is a stale or
        # tampered submission. Re-rendering the dashboard in response would
        # leave the browser able to re-submit it on refresh.
        messages.error(request, "إجراء غير معروف. أعد المحاولة من اللوحة.")
        return redirect("reports:admin_dashboard")

    has_reporttype = False
    reporttypes_count = 0
    try:
        from ..models import ReportType  # type: ignore
        has_reporttype = True

        # نعرض عدد الأنواع المعرّفة (وليس فقط المستخدمة) داخل المدرسة النشطة.
        rt_qs = ReportType.objects.filter(is_active=True)
        if active_school is not None:
            rt_qs = rt_qs.filter(school=active_school)
        reporttypes_count = rt_qs.count()
    except Exception:
        _degraded("dashboard.reporttypes_count", school_id=getattr(active_school, "pk", None))

    ctx.update({
        "has_reporttype": has_reporttype,
        "reporttypes_count": reporttypes_count,
    })
    
    # بيانات الاشتراك والأنشطة الحديثة فقط. إحصاءات اللوحة والرسوم تُبنى
    # لاحقًا من مصدر واحد (_build_school_dashboard_payload) لتجنب مضاعفة
    # الاستعلامات في الطلب نفسه.
    if active_school:
        now = timezone.now()

        # بيانات الاشتراك والتنبيهات
        subscription_warning = None
        try:
            from ..models import SchoolSubscription
            active_subscription = (
                SchoolSubscription.objects.filter(school=active_school)
                .select_related("plan")
                .first()
            )
            
            if active_subscription:
                days_remaining = (active_subscription.end_date - now.date()).days
                ctx['subscription'] = active_subscription
                ctx['days_remaining'] = days_remaining
                
                if active_subscription.is_expired:
                    subscription_warning = 'expired'
                elif days_remaining <= 7:
                    subscription_warning = 'critical'
                elif days_remaining <= 30:
                    subscription_warning = 'warning'
            else:
                subscription_warning = 'expired'
        except Exception:
            # تحذيرُ انتهاءٍ لا يظهر = مدرسةٌ تُفاجأ بتوقّف الخدمة.
            _degraded("dashboard.subscription_warning", school_id=getattr(active_school, "pk", None))
        
        ctx['subscription_warning'] = subscription_warning

        # A full work bucket silently stops every upload in the school, so the
        # manager has to learn it here rather than from a teacher's failed save.
        ctx['storage_pressure'] = school_storage_pressure(active_school)

        # ── السنة الدراسية غير محددة ────────────────────────────────────
        # ملف الإنجاز لا يُنشأ إلا على سنة المدرسة الحالية، وهو شرطٌ مقصود:
        # سنةٌ واحدة يقرّرها المدير أوضح من قائمةٍ يختار منها كل معلّم ما شاء.
        # لكنّ الشرط كان صامتاً في اتجاه المدير: المعلّم يرى «لم تُحدد السنة»
        # ولا يملك تحديدها، والمدير لا يرى شيئاً ولا يعلم أن مساراً كاملاً
        # متوقّف عنده. فيُرفع التنبيه هنا حيث تُرفع بقية العوائق، ومعه السنةُ
        # المقترحة من التقويم حتى يكون الضبط نظرةً لا بحثاً.
        if not (getattr(active_school, "current_academic_year", "") or "").strip():
            from ..hijri_utils import current_academic_year as _suggested_year

            ctx['academic_year_unset'] = True
            ctx['academic_year_suggestion'] = _suggested_year()

        # الاستهلاك الثلاثي معروضاً دائماً لا عند الخطر فقط: التنبيه وحده يخبر
        # المدير أنه اقترب من الحدّ، ولا يخبره أين هو منه قبل ذلك.
        ctx['consumption'] = school_consumption_summary(active_school)
        
        # آخر الأنشطة.
        #
        # ── لماذا عَلَمُ فشلٍ منفصل ────────────────────────────────────────
        # قائمةٌ فارغة تعني في الشاشة «لا نشاط بعد» — وهي رسالةٌ صحيحة لمدرسةٍ
        # جديدة، وكاذبةٌ تماماً لمدرسةٍ نشطة تعثّر استعلامها. والمدير لا يملك ما
        # يفرّق بينهما، فيقرأ الصمت طمأنينة.
        recent_activities = []
        recent_activities_failed = False
        try:
            recent_reports = _filter_by_school(
                Report.objects.all(),
                active_school
            ).select_related('teacher', 'category').order_by('-created_at')[:5]
            
            for report in recent_reports:
                teacher_name = getattr(getattr(report, 'teacher', None), 'name', None) or 'معلم'
                category_name = getattr(getattr(report, 'category', None), 'name', None) or 'قسم'
                recent_activities.append({
                    'type': 'report',
                    'icon': 'fa-file-alt',
                    'color': 'primary',
                    'title': 'تقرير جديد',
                    'description': f"{teacher_name} - {category_name}",
                    'time': report.created_at,
                    'url': reverse("reports:report_print", args=[report.pk]),
                })
            
            recent_tickets = _filter_by_school(
                Ticket.objects.filter(is_platform=False),
                active_school
            ).order_by('-created_at')[:3]
            
            for ticket in recent_tickets:
                ticket_title = (getattr(ticket, 'title', None) or '').strip()
                recent_activities.append({
                    'type': 'ticket',
                    'icon': 'fa-ticket-alt',
                    'color': 'warning',
                    'title': 'طلب جديد',
                    'description': (ticket_title[:50] if ticket_title else 'طلب بدون عنوان'),
                    'time': ticket.created_at,
                    'url': reverse("reports:ticket_detail", args=[ticket.pk]),
                })
            
            recent_activities.sort(key=lambda x: x['time'], reverse=True)
            recent_activities = recent_activities[:8]
        except Exception:
            _degraded("dashboard.recent_activities", school_id=getattr(active_school, "pk", None))
            recent_activities_failed = True
        
        ctx['recent_activities'] = recent_activities
        ctx['recent_activities_failed'] = recent_activities_failed

    selected_period = _normalize_dashboard_period(request.GET.get("period"))
    dashboard_payload = get_school_dashboard_payload(
        school_id=int(getattr(active_school, "pk", 0) or 0),
        period=selected_period,
        builder=lambda: _build_school_dashboard_payload(
            active_school,
            selected_period,
            reporttypes_count=reporttypes_count,
        ),
    )

    wants_json = (
        request.GET.get("format") == "json"
        or request.headers.get("x-requested-with") == "XMLHttpRequest"
        or "application/json" in (request.headers.get("accept") or "")
    )
    if wants_json:
        return JsonResponse(dashboard_payload, json_dumps_params={"ensure_ascii": False})

    # Let the rendered dashboard and the JSON refresh use the same payload.
    payload_kpis = dashboard_payload["kpis"]
    payload_charts = dashboard_payload["charts"]
    tickets_total = int(payload_kpis.get("tickets_total") or 0)
    tickets_done = int(payload_kpis.get("tickets_done") or 0)
    ticket_completion_rate = round((tickets_done / tickets_total) * 100) if tickets_total else 0

    pending_achievement_files = 0
    departments_count = 0
    if active_school is not None:
        try:
            from ..models import TeacherAchievementFile

            pending_achievement_files = TeacherAchievementFile.objects.filter(
                school=active_school,
                status=TeacherAchievementFile.Status.SUBMITTED,
            ).count()
        except Exception:
            pending_achievement_files = 0
        if Department is not None:
            try:
                departments_count = Department.objects.filter(
                    school=active_school,
                    is_active=True,
                ).count()
            except Exception:
                departments_count = 0

    setup = school_readiness(active_school) if active_school is not None else {
        "steps": [], "completed": 0, "total": 0, "percent": 100, "next_step": None
    }
    setup_steps = setup["steps"]
    setup_completed = setup["completed"]
    setup_total = setup["total"]
    setup_percent = setup["percent"]

    # The follow-up chips and the headline number must come from one list, or the
    # hero ends up contradicting the section directly beneath it. nav_context is
    # short-TTL cached, so the context processor's own call reuses this result.
    focus_items: list[dict] = []
    if active_school is not None:
        nav_counters = nav_context(request)
        focus_items = _build_manager_focus_items(
            tickets_open=payload_kpis.get("tickets_open"),
            pending_achievement_files=pending_achievement_files,
            assigned_to_me=nav_counters.get("NAV_ASSIGNED_TO_ME"),
            notifications_unread=nav_counters.get("NAV_NOTIFICATIONS_UNREAD"),
            signatures_pending=nav_counters.get("NAV_SIGNATURES_PENDING"),
        )
    attention_total = sum(item["count"] for item in focus_items if not item["subset"])

    ctx.update(
        {
            **payload_kpis,
            "focus_items": focus_items,
            "attention_total": attention_total,
            "initial_period": selected_period,
            "selected_period_label": dashboard_payload["period_label"],
            "coverage": dashboard_payload["coverage"],
            "trends": dashboard_payload["trends"],
            "has_comparison": dashboard_payload["has_comparison"],
            "departments": dashboard_payload["departments"],
            "responsiveness": dashboard_payload["responsiveness"],
            # التقويم خارج الحمولة المخزَّنة عمداً: هو حالةٌ قائمة الآن لا
            # حصيلةَ فترة، ولا يجوز أن يتأخّر خمسَ عشرة ثانية خلف ذاكرةٍ مؤقتة.
            "agenda": _school_agenda(active_school),
            # تاريخ اللقطة على الورق هجريّ كبقية تواريخ المنصة.
            "today_hijri": hijri_date(timezone.localdate()),
            "ticket_completion_rate": ticket_completion_rate,
            "pending_achievement_files": pending_achievement_files,
            "departments_count": departments_count,
            "setup_steps": setup_steps,
            "setup_completed": setup_completed,
            "setup_total": setup_total,
            "setup_percent": setup_percent,
            "setup_next_step": setup["next_step"],
            "dashboard_period_payload": dashboard_payload,
            "reports_labels": json.dumps(payload_charts["reports"]["labels"], ensure_ascii=False),
            "reports_data": json.dumps(payload_charts["reports"]["data"]),
            "dept_labels": json.dumps(payload_charts["categories"]["labels"], ensure_ascii=False),
            "dept_data": json.dumps(payload_charts["categories"]["data"]),
            "teachers_labels": json.dumps(payload_charts["teachers"]["labels"], ensure_ascii=False),
            "teachers_data": json.dumps(payload_charts["teachers"]["data"]),
        }
    )

    return render(request, "reports/admin_dashboard.html", ctx)


@require_http_methods(["GET"])
def admin_dashboard_data(request: HttpRequest) -> HttpResponse:
    """JSON data endpoint for the school dashboard.

    Authorisation is enforced here rather than through ``role_required`` because
    that decorator answers with an HTML redirect. The dashboard reaches this
    endpoint with ``fetch``, where a redirect surfaces as an unexplained parse
    error instead of a message the manager can act on.
    """
    user = request.user
    if not getattr(user, "is_authenticated", False):
        return JsonResponse({"detail": "authentication_required"}, status=401)
    if not (getattr(user, "is_superuser", False) or _is_staff(user)):
        return JsonResponse({"detail": "forbidden"}, status=403)

    active_school = _get_active_school(request)
    if School.objects.filter(is_active=True).exists():
        if active_school is None:
            return JsonResponse({"detail": "active_school_required"}, status=403)
        if (not user.is_superuser) and active_school not in _user_manager_schools(user):
            return JsonResponse({"detail": "forbidden"}, status=403)

    reporttypes_count = 0
    try:
        from ..models import ReportType  # type: ignore

        rt_qs = ReportType.objects.all()
        if hasattr(ReportType, "is_active"):
            rt_qs = rt_qs.filter(is_active=True)
        if active_school is not None:
            rt_qs = rt_qs.filter(school=active_school)
        reporttypes_count = rt_qs.count()
    except Exception:
        reporttypes_count = 0

    selected_period = _normalize_dashboard_period(request.GET.get("period"))
    payload = get_school_dashboard_payload(
        school_id=int(getattr(active_school, "pk", 0) or 0),
        period=selected_period,
        builder=lambda: _build_school_dashboard_payload(
            active_school,
            selected_period,
            reporttypes_count=reporttypes_count,
        ),
    )
    return JsonResponse(payload, json_dumps_params={"ensure_ascii": False})

@login_required(login_url="reports:login")
@require_http_methods(["GET"])
def school_audit_logs(request: HttpRequest) -> HttpResponse:
    """سجل العمليات — للمدير على مدرسته، وللوكيل في نطاق إشرافه.

    **النطاق يضيق بضيق الصلاحية.** المدير يرى كل ما جرى في مدرسته، والوكيل
    الذي مُنح ``view_audit_log`` يرى إجراءات من يشرف عليهم في أقسامه وحدهم —
    وهو بند صريح في توصيفه: «الاطلاع على سجل الإجراءات في نطاق اختصاصه».

    ونطاقٌ بلا أقسام يعني سجلاً فارغاً لا سجلاً كاملاً، على نفس القاعدة
    المطبَّقة في كل نطاقات هذا المشروع.
    """
    from ..capabilities import VIEW_AUDIT_LOG
    from ..permissions import capability_source, supervised_department_ids

    active_school = _get_active_school(request)
    if active_school is None:
        return redirect("reports:select_school")

    is_manager = request.user.is_superuser or active_school in _user_manager_schools(request.user)
    may_view_scoped = (
        not is_manager
        and capability_source(request.user, VIEW_AUDIT_LOG, active_school) is not None
    )
    if not (is_manager or may_view_scoped):
        messages.error(request, "لا تملك صلاحية الاطلاع على سجل الإجراءات.")
        return redirect("reports:home")

    # معرّفات من يقعون في نطاق الوكيل — تُحسب مرة وتُستعمل في التصفية.
    scoped_actor_ids = None
    if may_view_scoped:
        supervised = supervised_department_ids(request.user, active_school)
        scoped_actor_ids = set(
            DepartmentMembership.objects.filter(
                department_id__in=supervised
            ).values_list("teacher_id", flat=True)
        ) if supervised else set()
        # الوكيل يرى إجراءاته أيضاً — فهو من نطاق نفسه.
        scoped_actor_ids.add(request.user.pk)

    # ملاحظة: في بعض بيئات النشر قد لا تكون ترحيلات AuditLog مطبّقة بعد.
    # بدلاً من 500، نظهر الصفحة مع تنبيه واضح.
    try:
        from django.db.utils import OperationalError, ProgrammingError
    except Exception:  # pragma: no cover
        OperationalError = Exception  # type: ignore
        ProgrammingError = Exception  # type: ignore

    logs_qs = None
    try:
        logs_qs = AuditLog.objects.filter(school=active_school).select_related("teacher")
        if scoped_actor_ids is not None:
            # التصفية الأمنية على الاستعلام الأساس، قبل أي مرشّح من الطلب.
            logs_qs = logs_qs.filter(teacher_id__in=scoped_actor_ids)
    except (OperationalError, ProgrammingError):
        messages.error(
            request,
            "ميزة سجل العمليات غير مفعّلة حالياً (لم يتم تطبيق الترحيلات بعد). "
            "يرجى تشغيل migrate ثم إعادة المحاولة.",
        )

    # تصفية/عرض السجلات (لو كانت متاحة)
    teacher_id = _clean_query_value(request.GET.get("teacher"))
    action = _clean_query_value(request.GET.get("action"))
    model_name = _clean_query_value(request.GET.get("model"))
    query = _clean_query_value(request.GET.get("q"))[:120]
    start_date = _parse_date_safe(request.GET.get("start_date"))
    end_date = _parse_date_safe(request.GET.get("end_date"))
    allowed_actions = {value for value, _label in AuditLog.Action.choices}

    if logs_qs is not None:
        if teacher_id.isdigit():
            logs_qs = logs_qs.filter(teacher_id=teacher_id)
        else:
            teacher_id = ""
        if action in allowed_actions:
            logs_qs = logs_qs.filter(action=action)
        else:
            action = ""
        available_models = list(
            logs_qs.order_by("model_name")
            .values_list("model_name", flat=True)
            .distinct()
        )
        if model_name in available_models:
            logs_qs = logs_qs.filter(model_name=model_name)
        else:
            model_name = ""
        if query:
            logs_qs = logs_qs.filter(
                Q(actor_name__icontains=query)
                | Q(teacher__name__icontains=query)
                | Q(teacher__phone__icontains=query)
                | Q(object_repr__icontains=query)
                | Q(model_name__icontains=query)
            )
        if start_date is not None:
            logs_qs = logs_qs.filter(timestamp__date__gte=start_date)
        if end_date is not None:
            logs_qs = logs_qs.filter(timestamp__date__lte=end_date)

        if request.GET.get("export") == "csv":
            return audit_csv_response(
                logs_qs.select_related("school", "teacher"),
                filename=f"school-audit-{active_school.pk}.csv",
            )

        paginator = Paginator(logs_qs, 50)
        page = request.GET.get("page")
        logs = paginator.get_page(page)
    else:
        # لا نستخدم QuerySet هنا حتى لا نلمس قاعدة البيانات.
        logs = Paginator([], 50).get_page(1)
        available_models = []

    # ترجمة أسماء الموديلات إلى العربية — نفس الوحدة التي تخدم «سجل أعمالي»،
    # فلا تفترق تسمية الشيء الواحد بين شاشتين.
    from ..audit_labels import attach_views as _attach_audit_views, model_filter_choices

    _attach_audit_views(logs)

    # قائمة المعلمين في المدرسة للتصفية
    try:
        teachers = Teacher.objects.filter(
            school_memberships__school=active_school,
            school_memberships__is_active=True,
        ).distinct()
    except Exception:
        teachers = Teacher.objects.none()

    params = request.GET.copy()
    if "page" in params:
        params.pop("page")
    if "export" in params:
        params.pop("export")
    for key in list(params.keys()):
        cleaned = _clean_query_value(params.get(key))
        if cleaned:
            params[key] = cleaned
        else:
            params.pop(key)

    ctx = {
        "logs": logs,
        "teachers": teachers,
        "actions": AuditLog.Action.choices,
        "active_school": active_school,
        "q_teacher": teacher_id,
        "q_action": action,
        "q_model": model_name,
        "q": query,
        "models": model_filter_choices(available_models),
        "q_start": start_date.isoformat() if start_date else "",
        "q_end": end_date.isoformat() if end_date else "",
        "qs": params.urlencode(),
        "is_scoped_view": scoped_actor_ids is not None,
    }
    return render(request, "reports/audit_logs.html", ctx)


# ---- الأقسام: عرض/إنشاء/تعديل/حذف ----
@login_required(login_url="reports:login")
@role_required({"manager"})
@require_http_methods(["GET"])
def departments_list(request: HttpRequest) -> HttpResponse:
    active_school = _get_active_school(request)
    if School.objects.filter(is_active=True).exists():
        if active_school is None:
            messages.error(request, "فضلاً اختر مدرسة أولاً.")
            return redirect("reports:select_school")
        if (not request.user.is_superuser) and active_school not in _user_manager_schools(request.user):
            messages.error(request, "ليست لديك صلاحية على هذه المدرسة.")
            return redirect("reports:select_school")

    depts = _all_departments(active_school)
    return render(
        request,
        "reports/departments_list.html",
        {"departments": depts, "has_dept_model": Department is not None},
    )

@login_required(login_url="reports:login")
@role_required({"manager"})
@require_http_methods(["GET", "POST"])
def department_create(request: HttpRequest) -> HttpResponse:
    active_school = _get_active_school(request)
    if School.objects.filter(is_active=True).exists():
        if active_school is None:
            messages.error(request, "فضلاً اختر مدرسة أولاً.")
            return redirect("reports:select_school")
        if (not request.user.is_superuser) and active_school not in _user_manager_schools(request.user):
            messages.error(request, "ليست لديك صلاحية على هذه المدرسة.")
            return redirect("reports:select_school")

    FormCls = get_department_form()
    if not (Department is not None and FormCls is not None):
        messages.error(request, "إنشاء الأقسام يتطلب تفعيل موديل Department.")
        return redirect("reports:departments_list")

    form = FormCls(request.POST or None, active_school=active_school)
    if request.method == "POST":
        if form.is_valid():
            dep = form.save(commit=False)
            if hasattr(dep, "school") and active_school is not None:
                dep.school = active_school
            dep.save()
            # حفظ علاقات M2M بعد الحفظ الأولي
            if hasattr(form, "save_m2m"):
                form.save_m2m()
            messages.success(request, "تم إنشاء القسم.")
            return redirect("reports:departments_list")
        messages.error(request, "تعذّر الحفظ. تحقّق من الحقول.")
    return render(request, "reports/department_form.html", {"form": form, "mode": "create"})

@login_required(login_url="reports:login")
@role_required({"manager"})
@require_http_methods(["GET", "POST"])
def department_edit(request: HttpRequest, code: str) -> HttpResponse:
    active_school = _get_active_school(request)
    if School.objects.filter(is_active=True).exists():
        if active_school is None:
            messages.error(request, "فضلاً اختر مدرسة أولاً.")
            return redirect("reports:select_school")
        if (not request.user.is_superuser) and active_school not in _user_manager_schools(request.user):
            messages.error(request, "ليست لديك صلاحية على هذه المدرسة.")
            return redirect("reports:select_school")
    if Department is None:
        messages.error(request, "تعديل الأقسام غير متاح بدون موديل Department.")
        return redirect("reports:departments_list")

    obj, _, label = _resolve_department_by_code_or_pk(str(code), active_school)
    if not obj:
        messages.error(request, "القسم غير موجود.")
        return redirect("reports:departments_list")

    # عزل المدرسة: منع تعديل قسم يخص مدرسة أخرى.
    # الأقسام العامة (school is NULL) يسمح بها للسوبر فقط.
    # فحصُ العزل لا يُبتلع: تعثّره كان يُقرأ «مسموح» فيُفتح قسمُ مدرسةٍ أخرى
    # للتعديل. الفحص الذي يفشل مفتوحاً ليس فحصاً.
    if not getattr(request.user, "is_superuser", False):
        if getattr(obj, "school_id", None) is None:
            messages.error(request, "لا يمكنك تعديل قسم عام.")
            return redirect("reports:departments_list")
        if active_school is None or getattr(obj, "school_id", None) != getattr(active_school, "id", None):
            messages.error(request, "لا يمكنك تعديل قسم من مدرسة أخرى.")
            return redirect("reports:departments_list")

    FormCls = get_department_form()
    if not FormCls:
        messages.error(request, "DepartmentForm غير متاح.")
        return redirect("reports:departments_list")

    form = FormCls(request.POST or None, instance=obj, active_school=active_school)
    if request.method == "POST":
        if form.is_valid():
            form.save()
            messages.success(request, f"تم تحديث قسم «{label}».")
            return redirect("reports:departments_list")
        messages.error(request, "تعذّر الحفظ. تحقّق من الحقول.")
    return render(request, "reports/department_form.html", {"form": form, "mode": "edit", "department": obj})

@login_required(login_url="reports:login")
@user_passes_test(lambda u: getattr(u, "is_superuser", False), login_url="reports:login")
@require_http_methods(["GET", "POST"])
def school_manager_create(request: HttpRequest) -> HttpResponse:
    """إنشاء حساب مدير مدرسة وربطه بمدرسة واحدة على الأقل (ويمكن بأكثر من مدرسة).

    - يستخدم نموذج مبسّط (ManagerCreateForm) لإنشاء مستخدم مدير.
    - بعد الإنشاء يتم إسناد الدور "manager" وضبط عضويات SchoolMembership كمدير.
    """
    # مدارس متاحة للاختيار
    schools = School.objects.filter(is_active=True).order_by("name")
    initial_school_id = request.GET.get("school_id")

    form = ManagerCreateForm(request.POST or None, require_email=True)
    selected_ids = request.POST.getlist("schools") if request.method == "POST" else ([] if not initial_school_id else [initial_school_id])

    if request.method == "POST":
        if not selected_ids:
            messages.error(request, "يجب ربط المدير بمدرسة واحدة على الأقل.")
        if form.is_valid() and selected_ids:
            try:
                with transaction.atomic():
                    teacher = form.save(commit=True)

                    valid_schools = School.objects.filter(id__in=selected_ids, is_active=True)
                    if not valid_schools:
                        raise ValidationError("لا توجد مدارس صالحة للربط.")

                    # منع أكثر من مدير نشط واحد لكل مدرسة
                    conflict_exists = SchoolMembership.objects.filter(
                        school__in=valid_schools,
                        role_type=SchoolMembership.RoleType.MANAGER,
                        is_active=True,
                    ).exists()
                    if conflict_exists:
                        raise ValidationError("إحدى المدارس المختارة لديها مدير نشط بالفعل. لا يمكن تعيين أكثر من مدير واحد للمدرسة.")

                    for s in valid_schools:
                        SchoolMembership.objects.update_or_create(
                            school=s,
                            teacher=teacher,
                            role_type=SchoolMembership.RoleType.MANAGER,
                            defaults={"is_active": True},
                        )
                messages.success(request, "تم إنشاء حساب مدير/مديرة المدرسة وربطه بالمدارس المحددة.")
                return redirect("reports:schools_admin_list")
            except ValidationError as e:
                messages.error(request, " ".join(e.messages))
            except Exception:
                logger.exception("school_manager_create failed")
                messages.error(request, "تعذّر إنشاء حساب مدير/مديرة المدرسة. تحقّق من البيانات وحاول مرة أخرى.")

    context = {
        "form": form,
        "schools": schools,
        "selected_ids": [str(i) for i in selected_ids],
        "mode": "create",
        "manager": None,
    }
    return render(request, "reports/school_manager_create.html", context)


@login_required(login_url="reports:login")
@user_passes_test(lambda u: getattr(u, "is_superuser", False), login_url="reports:login")
@require_http_methods(["GET"])
def school_managers_list(request: HttpRequest) -> HttpResponse:
    """قائمة مدراء المدارس على مستوى المنصة."""
    q = _clean_query_value(request.GET.get("q"))
    manager_memberships = SchoolMembership.objects.select_related("school").filter(
        role_type=SchoolMembership.RoleType.MANAGER,
    )
    managers_qs = (
        Teacher.objects.filter(
            school_memberships__role_type=SchoolMembership.RoleType.MANAGER
        )
        .distinct()
        .order_by("name")
        .prefetch_related(
            Prefetch(
                "school_memberships",
                queryset=manager_memberships,
                to_attr="manager_school_memberships",
            )
        )
    )

    if q:
        managers_qs = managers_qs.filter(
            Q(name__icontains=q)
            | Q(phone__icontains=q)
            | Q(email__icontains=q)
            | Q(school_memberships__school__name__icontains=q)
            | Q(school_memberships__school__code__icontains=q)
            | Q(school_memberships__school__city__icontains=q)
        ).distinct()

    page_obj = Paginator(managers_qs, 24).get_page(request.GET.get("page") or 1)

    items: list[dict] = []
    for t in page_obj:
        schools = []
        seen_school_ids: set[int] = set()
        for membership in getattr(t, "manager_school_memberships", []):
            school = getattr(membership, "school", None)
            school_id = getattr(school, "id", None)
            if school is None or school_id in seen_school_ids:
                continue
            seen_school_ids.add(int(school_id))
            schools.append(school)
        items.append({"manager": t, "schools": schools})

    return render(
        request,
        "reports/school_managers_list.html",
        {
            "managers": items,
            "page_obj": page_obj,
            "q": q,
            "total_managers_count": page_obj.paginator.count,
            "query_params_without_page": _clean_query_params(request.GET),
        },
    )


@login_required(login_url="reports:login")
@user_passes_test(lambda u: getattr(u, "is_superuser", False), login_url="reports:login")
@require_http_methods(["POST"])
def school_manager_delete(request: HttpRequest, pk: int) -> HttpResponse:
    """تبديل حالة مدير مدرسة (تفعيل/تعطيل).

    لا نحذف السجل نهائيًا للحفاظ على السجلات المرتبطة.
    """

    manager = get_object_or_404(Teacher, pk=pk)

    try:
        with transaction.atomic():
            if manager.is_active:
                manager.is_active = False
                msg = "🗑️ تم إيقاف حساب المدير وإلغاء صلاحياته في المدارس."
                # عند التعطيل، نعطّل العضويات أيضاً
                SchoolMembership.objects.filter(
                    teacher=manager,
                    role_type=SchoolMembership.RoleType.MANAGER,
                ).update(is_active=False)
            else:
                manager.is_active = True
                msg = "✅ تم إعادة تفعيل حساب المدير بنجاح."
                # ملاحظة: لا نفعّل العضويات تلقائياً هنا لأننا لا نعرف أي مدرسة يجب تفعيلها 
                # يفضل أن يقوم المدير بتعديل المدارس من صفحة التعديل.

            manager.save(update_fields=["is_active"])

        messages.success(request, msg)
    except Exception:
        logger.exception("school_manager_toggle failed")
        messages.error(request, "تعذّر تغيير حالة المدير. حاول لاحقًا.")

    return redirect("reports:school_managers_list")


@login_required(login_url="reports:login")
@user_passes_test(lambda u: getattr(u, "is_superuser", False), login_url="reports:login")
@require_http_methods(["GET", "POST"])
def school_manager_update(request: HttpRequest, pk: int) -> HttpResponse:
    """تعديل بيانات مدير مدرسة موجود باستخدام نفس نموذج الإنشاء.

    - يمكن ترك كلمة المرور فارغة للإبقاء على الحالية.
    - يمكن تغيير المدارس المرتبطة بالمدير.
    """

    manager = get_object_or_404(
        Teacher.objects.prefetch_related("school_memberships__school"),
        pk=pk,
    )

    schools = School.objects.filter(is_active=True).order_by("name")

    if request.method == "POST":
        form = ManagerCreateForm(request.POST or None, instance=manager, require_email=True)
        selected_ids = request.POST.getlist("schools")
        if not selected_ids:
            messages.error(request, "يجب ربط المدير بمدرسة واحدة على الأقل.")
        if form.is_valid() and selected_ids:
            try:
                with transaction.atomic():
                    teacher = form.save(commit=True)
                    valid_schools = School.objects.filter(id__in=selected_ids, is_active=True)

                    # منع أكثر من مدير نشط واحد لكل مدرسة: نسمح فقط إن كانت المدرسة
                    # بدون مدير أو أن المدير الحالي هو نفس المستخدم الجاري تعديله.
                    conflict_exists = SchoolMembership.objects.filter(
                        school__in=valid_schools,
                        role_type=SchoolMembership.RoleType.MANAGER,
                        is_active=True,
                    ).exclude(teacher=teacher).exists()
                    if conflict_exists:
                        raise ValidationError("إحدى المدارس المختارة لديها مدير آخر نشط بالفعل. لا يمكن تعيين أكثر من مدير واحد للمدرسة.")

                    # تعطيل أي عضويات إدارة مدارس لم تعد مختارة
                    SchoolMembership.objects.filter(
                        teacher=teacher,
                        role_type=SchoolMembership.RoleType.MANAGER,
                    ).exclude(school__in=valid_schools).update(is_active=False)

                    # تفعيل/إنشاء العضويات المختارة
                    for s in valid_schools:
                        SchoolMembership.objects.update_or_create(
                            school=s,
                            teacher=teacher,
                            role_type=SchoolMembership.RoleType.MANAGER,
                            defaults={"is_active": True},
                        )
                messages.success(request, "تم تحديث بيانات مدير/مديرة المدرسة بنجاح.")
                return redirect("reports:school_managers_list")
            except ValidationError as e:
                messages.error(request, " ".join(e.messages))
            except Exception:
                logger.exception("school_manager_update failed")
                messages.error(request, "تعذّر تحديث بيانات مدير/مديرة المدرسة. تحقّق من البيانات وحاول مرة أخرى.")
        # في حال وجود أخطاء نمرّر selected_ids كما هي
    else:
        existing_ids = SchoolMembership.objects.filter(
            teacher=manager,
            role_type=SchoolMembership.RoleType.MANAGER,
            is_active=True,
        ).values_list("school_id", flat=True)
        selected_ids = [str(i) for i in existing_ids]
        form = ManagerCreateForm(instance=manager, require_email=True)

    context = {
        "form": form,
        "schools": schools,
        "selected_ids": [str(i) for i in selected_ids],
        "mode": "edit",
        "manager": manager,
    }
    return render(request, "reports/school_manager_create.html", context)

@login_required(login_url="reports:login")
@role_required({"manager"})
@require_http_methods(["POST"])
def department_delete(request: HttpRequest, code: str) -> HttpResponse:
    active_school = _get_active_school(request)
    if School.objects.filter(is_active=True).exists():
        if active_school is None:
            messages.error(request, "فضلاً اختر مدرسة أولاً.")
            return redirect("reports:select_school")
        if (not request.user.is_superuser) and active_school not in _user_manager_schools(request.user):
            messages.error(request, "ليست لديك صلاحية على هذه المدرسة.")
            return redirect("reports:select_school")

    if Department is None:
        messages.error(request, "حذف الأقسام غير متاح بدون موديل Department.")
        return redirect("reports:departments_list")

    obj, _, label = _resolve_department_by_code_or_pk(str(code), active_school)
    if not obj:
        messages.error(request, "القسم غير موجود.")
        return redirect("reports:departments_list")

    # عزل المدرسة: لا يُسمح بحذف قسم يخص مدرسة أخرى.
    # الأقسام العامة (school is NULL) يُسمح بها للسوبر فقط (باستثناء قسم المدير الدائم الذي يمنع حذفه أصلاً).
    dep_school_id = getattr(obj, "school_id", None)
    if dep_school_id is None:
        if not getattr(request.user, "is_superuser", False):
            messages.error(request, "لا يمكنك حذف قسم عام على مستوى المنصة.")
            return redirect("reports:departments_list")
    elif active_school is not None and dep_school_id != active_school.id:
        messages.error(request, "لا يمكنك حذف قسم يخص مدرسة أخرى.")
        return redirect("reports:departments_list")

    try:
        obj.delete()
        messages.success(request, f"تم حذف قسم «{label}».")
    except ProtectedError:
        messages.error(request, f"لا يمكن حذف «{label}» لوجود سجلات مرتبطة به. عطّل القسم أو احذف السجلات المرتبطة أولاً.")
    except Exception:
        logger.exception("department_delete failed")
        messages.error(request, "تعذّر حذف القسم.")
    return redirect("reports:departments_list")

def _dept_m2m_field_name_to_teacher(dep_obj) -> str | None:
    try:
        if dep_obj is None:
            return None
        for f in dep_obj._meta.get_fields():
            if isinstance(f, ManyToManyField) and getattr(f.remote_field, "model", None) is Teacher:
                return f.name
    except Exception:
        logger.exception("Failed to detect forward M2M Department→Teacher")
    return None

def _deptmember_field_names() -> tuple[str | None, str | None]:
    dep_field = tea_field = None
    try:
        if DepartmentMembership is None:
            return (None, None)

        for f in DepartmentMembership._meta.get_fields():
            if isinstance(f, ForeignKey):
                if getattr(f.remote_field, "model", None) is Department and dep_field is None:
                    dep_field = f.name
                elif getattr(f.remote_field, "model", None) is Teacher and tea_field is None:
                    tea_field = f.name
            if dep_field and tea_field:
                break

        if dep_field is None:
            for n in ("department", "dept", "dept_fk"):
                if hasattr(DepartmentMembership, n):
                    dep_field = n
                    break
        if tea_field is None:
            for n in ("teacher", "member", "user", "teacher_fk"):
                if hasattr(DepartmentMembership, n):
                    tea_field = n
                    break
    except Exception:
        logger.exception("Failed to detect DepartmentMembership FKs")

    return (dep_field, tea_field)

def _dept_add_member(dep, teacher: Teacher) -> bool:
    try:
        m2m_name = _dept_m2m_field_name_to_teacher(dep)
        if m2m_name:
            getattr(dep, m2m_name).add(teacher)
            return True
    except Exception:
        logger.exception("Add via Department M2M failed")

    try:
        if DepartmentMembership is not None and Department is not None:
            dep_field, tea_field = _deptmember_field_names()
            if dep_field and tea_field:
                kwargs = {dep_field: dep, tea_field: teacher}
                DepartmentMembership.objects.get_or_create(**kwargs)
                return True
    except Exception:
        logger.exception("Add via DepartmentMembership failed")

    return False

def _dept_remove_member(dep, teacher: Teacher) -> bool:
    try:
        m2m_name = _dept_m2m_field_name_to_teacher(dep)
        if m2m_name:
            getattr(dep, m2m_name).remove(teacher)
            return True
    except Exception:
        logger.exception("Remove via Department M2M failed")

    try:
        if DepartmentMembership is not None and Department is not None:
            dep_field, tea_field = _deptmember_field_names()
            if dep_field and tea_field:
                kwargs = {dep_field: dep, tea_field: teacher}
                deleted, _ = DepartmentMembership.objects.filter(**kwargs).delete()
                return deleted > 0
    except Exception:
        logger.exception("Remove via DepartmentMembership failed")

    return False

def _dept_set_member_role(dep, teacher: Teacher, role_type: str) -> bool:
    try:
        if DepartmentMembership is None or Department is None:
            return False
        if getattr(dep, "slug", "").lower() == MANAGER_SLUG and role_type != DM_TEACHER:
            return False

        dep_field, tea_field = _deptmember_field_names()
        if not dep_field or not tea_field:
            return False

        if not hasattr(DepartmentMembership, "role_type"):
            return False

        kwargs = {dep_field: dep, tea_field: teacher}
        obj, created = DepartmentMembership.objects.get_or_create(
            defaults={"role_type": role_type},
            **kwargs,
        )
        if (not created) and getattr(obj, "role_type", None) != role_type:
            obj.role_type = role_type
            obj.save(update_fields=["role_type"])
        obj.refresh_from_db(fields=["role_type"])
        return getattr(obj, "role_type", None) == role_type
    except Exception:
        logger.exception("Failed to set DepartmentMembership role_type")
        return False

def _dept_set_officer(dep, teacher: Teacher) -> bool:
    try:
        if DepartmentMembership is None or Department is None:
            return False
        if getattr(dep, "slug", "").lower() == MANAGER_SLUG:
            return False

        dep_field, tea_field = _deptmember_field_names()
        if not dep_field or not tea_field:
            return False

        if not hasattr(DepartmentMembership, "role_type"):
            return False

        qs = DepartmentMembership.objects.filter(**{dep_field: dep})
        qs.filter(role_type=DM_OFFICER).exclude(**{tea_field: teacher}).update(role_type=DM_TEACHER)
        return _dept_set_member_role(dep, teacher, DM_OFFICER)
    except Exception:
        logger.exception("Failed to set department officer")
        return False

@login_required(login_url="reports:login")
@role_required({"manager"})
@require_http_methods(["GET", "POST"])
def department_members(request: HttpRequest, code: str | int) -> HttpResponse:
    active_school = _get_active_school(request)
    if School.objects.filter(is_active=True).exists():
        if active_school is None:
            messages.error(request, "فضلاً اختر مدرسة أولاً.")
            return redirect("reports:select_school")
        if (not request.user.is_superuser) and active_school not in _user_manager_schools(request.user):
            messages.error(request, "ليست لديك صلاحية على هذه المدرسة.")
            return redirect("reports:select_school")

    obj, dept_code, dept_label = _resolve_department_by_code_or_pk(str(code), active_school)
    if not dept_code:
        messages.error(request, "القسم غير موجود.")
        return redirect("reports:departments_list")

    # عزل المدرسة: لا نسمح بإدارة أعضاء قسم تابع لمدرسة أخرى.
    if obj is not None:
        dep_school_id = getattr(obj, "school_id", None)
        if dep_school_id is None:
            if not getattr(request.user, "is_superuser", False):
                messages.error(request, "لا يمكنك إدارة قسم عام على مستوى المنصة.")
                return redirect("reports:departments_list")
        elif active_school is not None and dep_school_id != active_school.id:
            messages.error(request, "لا يمكنك إدارة قسم يخص مدرسة أخرى.")
            return redirect("reports:departments_list")

    if request.method == "POST":
        teacher_id = coerce_pk(request.POST.get("teacher_id"))
        action = (request.POST.get("action") or "").strip()

        allowed_teachers = Teacher.objects.filter(is_active=True)
        if active_school is not None:
            allowed_teachers = allowed_teachers.filter(
                school_memberships__school=active_school,
                school_memberships__is_active=True,
            )
        teacher = allowed_teachers.filter(pk=teacher_id).first() if teacher_id else None
        if not teacher:
            labels = school_gender_labels(active_school)
            messages.error(request, f"{labels['teacher']} غير موجودة." if labels["is_girls"] else f"{labels['teacher']} غير موجود.")
            return redirect("reports:department_members", code=dept_code)

        if Department is not None and obj:
            try:
                with transaction.atomic():
                    ok = False
                    if action == "add":
                        ok = _dept_set_member_role(obj, teacher, DM_TEACHER) or _dept_add_member(obj, teacher)
                        if ok:
                            messages.success(request, f"تم تكليف {teacher.name} في قسم «{dept_label}».")
                        else:
                            labels = school_gender_labels(active_school)
                            messages.error(request, f"تعذّر إسناد {labels['teacher']} — تحقّق من بنية DepartmentMembership.")
                    elif action == "set_officer":
                        if getattr(obj, "slug", "").lower() == MANAGER_SLUG:
                            messages.error(request, "قسم الإدارة لا يحتاج مسؤول قسم منفصل؛ مدير المدرسة هو المسؤول عن هذا القسم.")
                            ok = False
                        else:
                            ok = _dept_set_officer(obj, teacher)
                        if ok:
                            messages.success(request, f"تم تعيين {teacher.name} مسؤولاً لقسم «{dept_label}». ")
                        elif not getattr(obj, "slug", "").lower() == MANAGER_SLUG:
                            messages.error(request, "تعذّر تعيين مسؤول القسم — تحقّق من دعم role_type.")
                    elif action == "unset_officer":
                        ok = _dept_remove_member(obj, teacher)
                        if ok:
                            messages.success(request, f"تم إلغاء تكليف {teacher.name} من القسم.")
                        else:
                            messages.error(request, "تعذّر إلغاء التكليف.")
                    elif action == "remove":
                        ok = _dept_remove_member(obj, teacher)
                        if ok:
                            messages.success(request, f"تم إلغاء تكليف {teacher.name}.")
                        else:
                            messages.error(request, "تعذّر إلغاء التكليف — تحقق من بنية العلاقات.")
                    else:
                        messages.error(request, "إجراء غير معروف.")
            except Exception:
                logger.exception("department_members mutation failed")
                messages.error(request, "حدث خطأ أثناء حفظ التغييرات.")
        else:
            messages.error(request, "إدارة الأعضاء تتطلب تفعيل موديل Department.")
            return redirect("reports:departments_list")

        return redirect("reports:department_members", code=dept_code)

    members_qs = _members_for_department(dept_code, active_school)

    officers_qs = Teacher.objects.none()
    teachers_qs = Teacher.objects.none()
    assigned_ids_qs = Teacher.objects.none()
    try:
        if DepartmentMembership is not None:
            mem_qs = DepartmentMembership.objects.filter(department__slug__iexact=dept_code)
            if active_school is not None:
                mem_qs = mem_qs.filter(department__school=active_school)
            officer_ids = mem_qs.filter(role_type=DM_OFFICER).values_list("teacher_id", flat=True)
            teacher_ids = mem_qs.filter(role_type=DM_TEACHER).values_list("teacher_id", flat=True)
            assigned_ids = mem_qs.values_list("teacher_id", flat=True)

            officers_qs = Teacher.objects.filter(is_active=True, id__in=officer_ids).distinct().order_by("name")
            teachers_qs = Teacher.objects.filter(is_active=True, id__in=teacher_ids).distinct().order_by("name")
            assigned_ids_qs = Teacher.objects.filter(id__in=assigned_ids)

            if active_school is not None:
                officers_qs = officers_qs.filter(
                    school_memberships__school=active_school,
                    school_memberships__is_active=True,
                )
                teachers_qs = teachers_qs.filter(
                    school_memberships__school=active_school,
                    school_memberships__is_active=True,
                )
                assigned_ids_qs = assigned_ids_qs.filter(
                    school_memberships__school=active_school,
                    school_memberships__is_active=True,
                )
    except Exception:
        logger.exception("Failed to compute officers/teachers memberships")

    all_teachers = Teacher.objects.filter(is_active=True)
    if active_school is not None:
        all_teachers = all_teachers.filter(
            school_memberships__school=active_school,
            school_memberships__is_active=True,
        )
    all_teachers = all_teachers.order_by("name")

    try:
        if hasattr(assigned_ids_qs, "values_list"):
            assigned_ids_list = assigned_ids_qs.values_list("id", flat=True)
            available = all_teachers.exclude(id__in=assigned_ids_list)
        else:
            available = all_teachers
    except Exception:
        available = all_teachers

    return render(
        request,
        "reports/department_members.html",
        {
            "department": obj if obj else {"code": dept_code, "name": dept_label},
            "dept_code": dept_code,
            "dept_label": dept_label,
            "members": members_qs,
            "officers": officers_qs,
            "teachers": teachers_qs,
            "all_teachers": all_teachers,
            "available_teachers": available,
            "has_dept_model": Department is not None,
            "officer_assignment_allowed": str(dept_code).lower() != MANAGER_SLUG,
        },
    )
