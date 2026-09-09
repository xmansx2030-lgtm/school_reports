# reports/search.py
# -*- coding: utf-8 -*-
"""البحث الموحّد: مدخلٌ واحد إلى كل ما يملك المستخدم حقّ فتحه.

**القاعدة الحاكمة، ولا استثناء لها:**

    البحث لا يُظهر شيئاً لا يستطيع المستخدم فتحه أصلاً.

وهي ليست قاعدة ذوق. نتيجةُ بحثٍ تحمل عنوان تقرير أو اسم منسوب هي **تسريب
محتوى** حتى لو رُدّ المستخدم عند النقر: العنوان وحده يكشف أن الشيء موجود،
ومن هو صاحبه، وفي أي مدرسة. ومحرّك البحث أخطرُ سطحٍ في أي منصة متعدّدة
المستأجرين لأنه — بحكم تعريفه — يمرّ على كل الجداول دفعة واحدة.

ولذلك **لا يبني أيُّ مزوّد هنا شرطَ رؤيةٍ خاصاً به.** كل واحد يبدأ من دالة
الرؤية نفسها التي تستعملها الشاشة التي سيقود إليها:

    التقارير   → ``permissions.restrict_queryset_for_user``
    الوثائق    → ``services_documents.visible_documents``
    الاجتماعات → ``services_meetings.meetings_for_user``
    التذاكر    → قاعدة صندوق الوارد نفسها (مدير | مُسنَد | مستلِم | قسمه)
    الإشعارات  → ``NotificationRecipient`` الخاصة بالمستخدم
    المنسوبون  → المدير ورؤساء الأقسام وحدهم
    التكليفات  → ما أصدره، أو ما كُلِّف به

وسببُ ذلك بسيط: قاعدةُ رؤيةٍ تُكتب مرتين تتباعد مرة. فحين تتغيّر صلاحيةٌ في
الشاشة ولا تتغيّر هنا، يصير البحثُ باباً خلفياً — ولا يكتشفه أحد لأن الشاشة
تبدو صحيحة.

**العزل بالمدرسة النشطة شرطُ دخولٍ لا مرشِّح.** بلا مدرسة نشطة لا نتائج
البتّة؛ ومع مدرسة نشطة، كل استعلام مقيَّد بها. راجع
``reports/tests/test_unified_search.py`` حيث تُفحص كل هذه الحدود.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Callable, Sequence

from django.db.models import Q
from django.urls import reverse

from core.observability import soft_call

from .search_utils import smart_search_q

# أقلّ من حرفين يُطابق نصف قاعدة البيانات ولا يفيد أحداً.
MIN_QUERY_LENGTH = 2
# سقفٌ لكل نطاق ولمجموع النتائج. البحث مسارٌ تفاعلي يُنادى مع كل ضغطة مفتاح.
DEFAULT_PER_KIND = 5
MAX_TOTAL = 30


@dataclass(frozen=True)
class SearchHit:
    """نتيجةٌ واحدة، جاهزة للعرض ولا تحتاج القالبُ أن يعرف مصدرها."""

    kind: str
    label: str
    icon: str
    title: str
    subtitle: str
    url: str

    def as_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "label": self.label,
            "icon": self.icon,
            "title": self.title,
            "subtitle": self.subtitle,
            "url": self.url,
        }


def _clip(value: object, limit: int = 90) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ─────────────────────────────────────────────────────────────────────────
# المزوّدون — كلٌّ يبدأ من دالة رؤية قائمة، ولا يخترع شرطاً
# ─────────────────────────────────────────────────────────────────────────
def _search_reports(user, school, query: str, limit: int) -> list[SearchHit]:
    from .models import Report
    from .permissions import restrict_queryset_for_user

    qs = Report.objects.filter(school=school).select_related("teacher", "category")
    qs = restrict_queryset_for_user(qs, user, school)
    qs = qs.filter(
        smart_search_q(query, ("title", "idea", "teacher_name", "teacher__name", "category__name"))
    ).order_by("-report_date", "-id")

    return [
        SearchHit(
            kind="report",
            label="تقرير",
            icon="fa-file-lines",
            title=_clip(report.title or "تقرير بلا عنوان"),
            subtitle=_clip(
                " · ".join(
                    bit
                    for bit in (
                        getattr(report.teacher, "name", "") or report.teacher_name,
                        getattr(report.category, "name", ""),
                    )
                    if bit
                )
            ),
            url=reverse("reports:report_print", args=[report.pk]),
        )
        for report in qs[:limit]
    ]


def _search_documents(user, school, query: str, limit: int) -> list[SearchHit]:
    from .services_documents import visible_documents

    qs = visible_documents(user, school).filter(
        smart_search_q(query, ("title", "description", "owner_name", "owner__name"))
    ).order_by("-created_at", "-id")

    return [
        SearchHit(
            kind="document",
            label="وثيقة",
            icon="fa-folder-open",
            title=_clip(document.title),
            subtitle=_clip(
                getattr(document.owner, "name", "") or document.owner_name or "وثيقة"
            ),
            url=reverse("reports:document_detail", args=[document.pk]),
        )
        for document in qs[:limit]
    ]


def _search_meetings(user, school, query: str, limit: int) -> list[SearchHit]:
    from .services_meetings import meetings_for_user

    qs = meetings_for_user(user, school=school).filter(
        smart_search_q(query, ("title", "purpose", "location", "organizer_name"))
    )

    return [
        SearchHit(
            kind="meeting",
            label="اجتماع",
            icon="fa-users-rectangle",
            title=_clip(meeting.title),
            subtitle=_clip(meeting.get_status_display()),
            url=reverse("reports:meeting_detail", args=[meeting.pk]),
        )
        for meeting in qs[:limit]
    ]


def _search_tickets(user, school, query: str, limit: int) -> list[SearchHit]:
    """التذاكر بقاعدة صندوق الوارد نفسها.

    المدير يرى تذاكر مدرسته؛ وغيره يرى ما أنشأه، أو أُسنِد إليه، أو كان من
    مستلميه، أو كان في قسمه. وهي حرفياً شروط ``tickets_inbox`` مضافاً إليها
    ``creator`` — لأن ``my_requests`` تعرض ما أنشأه المستخدم، فهو يفتحه فعلاً.
    """
    from .models import Ticket
    from .permissions import is_school_manager

    qs = Ticket.objects.filter(school=school, is_platform=False).select_related(
        "assignee", "department"
    )
    if not is_school_manager(user, active_school=school):
        from .context_processors import _user_department_codes

        codes = _user_department_codes(user, school)
        visible = Q(creator=user) | Q(assignee=user) | Q(recipients=user)
        if codes:
            visible |= Q(department__slug__in=codes)
        qs = qs.filter(visible)

    qs = qs.filter(
        smart_search_q(query, ("title", "body", "assignee__name"))
    ).distinct().order_by("-created_at", "-id")

    return [
        SearchHit(
            kind="ticket",
            label="طلب",
            icon="fa-ticket",
            title=_clip(f"#{ticket.pk} — {ticket.title or 'طلب بلا عنوان'}"),
            subtitle=_clip(ticket.get_status_display()),
            url=reverse("reports:ticket_detail", args=[ticket.pk]),
        )
        for ticket in qs[:limit]
    ]


def _search_notifications(user, school, query: str, limit: int) -> list[SearchHit]:
    """ما وصل هذا المستخدم فعلاً — لا ما أُرسل في المدرسة.

    المرور عبر ``NotificationRecipient`` مقصود: الإشعار الذي لم يُرسَل إليه لا
    يعنيه، وإظهارُه في البحث يكشف مراسلات غيره.
    """
    from .models import NotificationRecipient

    qs = (
        NotificationRecipient.objects.filter(teacher=user)
        .filter(Q(notification__school=school) | Q(notification__school__isnull=True))
        .select_related("notification", "notification__created_by")
        .filter(
            smart_search_q(
                query,
                ("notification__title", "notification__message", "notification__created_by__name"),
            )
        )
        .order_by("-created_at", "-id")
    )

    hits: list[SearchHit] = []
    for recipient in qs[:limit]:
        notification = recipient.notification
        kind = str(getattr(notification, "kind", "") or "notification")
        is_circular = kind == "circular"
        is_newsletter = kind == "newsletter"
        hits.append(
            SearchHit(
                kind="circular" if is_circular else ("newsletter" if is_newsletter else "notification"),
                label="تعميم" if is_circular else ("نشرة" if is_newsletter else "إشعار"),
                icon="fa-file-signature" if is_circular else ("fa-newspaper" if is_newsletter else "fa-bell"),
                title=_clip(notification.title or "بلا عنوان"),
                subtitle=_clip(getattr(notification.created_by, "name", "") or "الإدارة"),
                url=reverse(
                    "reports:my_circular_detail" if is_circular else "reports:my_notification_detail",
                    args=[recipient.pk],
                ),
            )
        )
    return hits


def _search_teachers(user, school, query: str, limit: int) -> list[SearchHit]:
    """كشف المنسوبين لمن يديره وحده.

    المعلّم لا يبحث في زملائه: أسماؤهم وأرقامهم بيانات شخصية، وشاشة
    ``manage_teachers`` محجوزة للمدير أصلاً — فلا يجوز أن يلتفّ البحث عليها.
    """
    from .models import SchoolMembership, Teacher
    from .permissions import is_officer, is_school_manager

    if not (is_school_manager(user, active_school=school) or is_officer(user)):
        return []

    qs = (
        Teacher.objects.filter(
            school_memberships__school=school,
            school_memberships__is_active=True,
            school_memberships__role_type__in=SchoolMembership.STAFF_ROLES,
        )
        .filter(smart_search_q(query, ("name", "phone", "national_id")))
        .distinct()
        .order_by("name")
    )

    return [
        SearchHit(
            kind="teacher",
            label="منسوب",
            icon="fa-user",
            title=_clip(teacher.name),
            subtitle=_clip(teacher.phone or ""),
            url=f"{reverse('reports:manage_teachers')}?q={teacher.phone or teacher.name}",
        )
        for teacher in qs[:limit]
    ]


def _search_assignments(user, school, query: str, limit: int) -> list[SearchHit]:
    """ما أصدره المستخدم أو ما كُلِّف به — لا تكليفات المدرسة كلها."""
    from .models import Assignment
    from .permissions import is_school_manager

    qs = Assignment.objects.filter(school=school).select_related("issuer", "department")
    if not is_school_manager(user, active_school=school):
        qs = qs.filter(Q(issuer=user) | Q(targets__assignee=user))

    qs = qs.filter(
        smart_search_q(query, ("title", "description", "issuer_name", "issuer__name"))
    ).distinct().order_by("-created_at", "-id")

    return [
        SearchHit(
            kind="assignment",
            label="تكليف",
            icon="fa-diagram-project",
            title=_clip(assignment.title),
            subtitle=_clip(
                getattr(assignment.issuer, "name", "") or assignment.issuer_name or ""
            ),
            # Search returns the parent Assignment, not one recipient target.
            # Using ``assignment_detail`` here treated the parent primary key as
            # an AssignmentTarget id and could open a completely different task.
            url=reverse("reports:assignment_view", args=[assignment.pk]),
        )
        for assignment in qs[:limit]
    ]


# الترتيب هو ترتيب العرض: ما يبحث عنه المستخدم أكثر، أولاً.
#
# ويُسجَّل كل مزوّد **باسمه** لا بمرجعٍ إليه. والفرق ليس شكلياً: المرجع يُلتقط
# لحظة الاستيراد، فيصير السجلّ نسخةً مجمّدة لا يصلها أي استبدال لاحق للدالة في
# الوحدة — وهو ما يكسر عقد ``mock.patch`` المعتاد ويجعل مسار التدهور غير قابل
# للاختبار أصلاً. أما الحلّ بالاسم فيقرأ الدالة وقت النداء، فيرى ما في الوحدة
# الآن.
PROVIDERS: Sequence[tuple[str, str]] = (
    ("reports", "_search_reports"),
    ("tickets", "_search_tickets"),
    ("notifications", "_search_notifications"),
    ("documents", "_search_documents"),
    ("assignments", "_search_assignments"),
    ("meetings", "_search_meetings"),
    ("teachers", "_search_teachers"),
)


def _resolve(attribute: str) -> Callable[..., list[SearchHit]]:
    return getattr(sys.modules[__name__], attribute)


def search(
    user,
    active_school,
    query: str,
    *,
    per_kind: int = DEFAULT_PER_KIND,
    max_total: int = MAX_TOTAL,
) -> list[SearchHit]:
    """نتائج البحث لهذا المستخدم في مدرسته النشطة.

    **بلا مدرسة نشطة لا نتائج.** المستخدم قد يكون عضواً في أكثر من مدرسة،
    والبحث بلا سياق كان سيخلط بياناتها — وهو بالضبط ما يمنعه العزل في بقية
    المنصة. فالغياب يُعامَل كمنعٍ لا كـ«ابحث في الكل».

    وتعثّرُ مزوّدٍ واحد لا يُسقط البحث كلّه: النتائج الباقية تُعرض، ويُسجَّل
    التعثّر باسمه في ``opmetrics`` — فبحثٌ ناقص أفضل من بحثٍ معطّل، بشرط أن
    يُعرف أنه ناقص.
    """
    query = " ".join(str(query or "").split())
    if len(query) < MIN_QUERY_LENGTH:
        return []
    if active_school is None or not getattr(user, "is_authenticated", False):
        return []

    hits: list[SearchHit] = []
    for name, attribute in PROVIDERS:
        if len(hits) >= max_total:
            break
        provider = _resolve(attribute)
        found = soft_call(
            f"search.{name}",
            lambda p=provider: p(user, active_school, query, per_kind),
            default=[],
            user_id=getattr(user, "pk", None),
            school_id=getattr(active_school, "pk", None),
        )
        hits.extend(found or [])

    return hits[:max_total]
