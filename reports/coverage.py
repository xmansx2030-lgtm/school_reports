# reports/coverage.py
# -*- coding: utf-8 -*-
"""تغطية التوثيق: من وثّق ومن لم يوثّق خلال فترة.

**لماذا وحدةٌ مستقلة.** الحساب تحتاجه جهتان لا واحدة: اللوحة تعرضه، وشاشة
التذكير تبني عليه قائمة المستلمين. ولو بقي داخل باني حمولة اللوحة لَنُسخ في
الثانية، ولاختلفا يوماً — فيقرأ المدير في اللوحة خمسةً ويُرسل التذكير إلى ستة،
ولا يجد ما يفسّر الفرق.

**والفترة جزءٌ من السؤال لا زينة.** من وثّق العام الماضي ولم يوثّق هذا الشهر
متأخّرٌ اليوم لا مغطّى؛ فالتغطية تُقاس على نافذةٍ زمنية، وتغيير النافذة يغيّر
الجواب بحق.
"""
from __future__ import annotations

from typing import Iterable, Optional

from django.db.models import QuerySet

from .models import Report, School, SchoolMembership, Teacher


def school_staff_queryset(
    school: Optional[School],
    *,
    limit_to: Optional[Iterable[int]] = None,
) -> QuerySet:
    """منسوبو المدرسة النشطون — نفس التعريف الذي تعدّه اللوحة.

    ``limit_to`` يقصر النتيجة على مجموعةٍ من المعرّفات، وهو ما يحتاجه الوكيل:
    نطاقُه أقسامٌ بعينها لا المدرسة كلها.

    **ومجموعةٌ فارغة تعني «لا أحد» لا «الجميع».** وهي القاعدة نفسها في
    ``supervised_department_ids``: نطاقٌ لم يُضبط بعد لا يُقرأ صلاحيةً على
    المدرسة كاملة. ولذلك يُفرَّق هنا بين ``None`` (بلا تقييد) و``set()``
    (تقييدٌ إلى لا شيء) — والخلط بينهما يفتح ما أُغلق.
    """
    staff = Teacher.objects.filter(is_active=True)
    if school is None:
        return staff.none()

    staff = staff.filter(
        school_memberships__school=school,
        school_memberships__is_active=True,
        school_memberships__role_type__in=SchoolMembership.STAFF_ROLES,
    ).distinct()

    if limit_to is None:
        return staff
    ids = {int(pk) for pk in limit_to if pk is not None}
    return staff.filter(pk__in=ids) if ids else staff.none()


def documented_teacher_ids(school: Optional[School], since=None) -> set[int]:
    """معرّفات من كتب تقريراً واحداً على الأقل داخل النافذة.

    ``since=None`` يعني بلا حدّ زمني — أي «هل وثّق يوماً؟» لا «هل وثّق الآن؟».
    """
    if school is None:
        return set()

    reports = Report.objects.filter(school=school).exclude(teacher__isnull=True)
    if since is not None:
        reports = reports.filter(created_at__gte=since)

    return set(reports.values_list("teacher_id", flat=True).distinct())


def pending_documenters(
    school: Optional[School],
    since=None,
    *,
    limit_to: Optional[Iterable[int]] = None,
) -> QuerySet:
    """المنسوبون الذين لم يوثّقوا داخل النافذة، مرتّبين بالاسم.

    تُعاد كـ ``QuerySet`` لا كقائمة: المُستدعي قد يريد عدّها، أو اقتطاع خمسةٍ
    منها للعرض، أو تمريرها كاملةً إلى حقل مستلمين — وثلاثتها بلا جلبٍ زائد.
    """
    staff = school_staff_queryset(school, limit_to=limit_to)
    if school is None:
        return staff

    return staff.exclude(pk__in=documented_teacher_ids(school, since=since)).order_by("name")
