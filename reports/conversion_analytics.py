"""Deterministic conversion signals for the platform-owner workspace.

The module intentionally keeps commercial scoring outside the language model.
Every point displayed to the owner comes from an auditable database aggregate;
AI receives only the resulting safe snapshot and is allowed to write copy.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, Max, Q
from django.utils import timezone

from .models import (
    AiUsageEvent,
    ApprovalState,
    Assignment,
    AssignmentTarget,
    CircularDraft,
    Document,
    Meeting,
    Payment,
    Plan,
    Report,
    SchoolLeadershipPortfolio,
    SchoolMembership,
    SchoolSubscription,
    SubscriptionPlan,
    TeacherAchievementFile,
    Ticket,
)


SERVICE_LABELS = {
    "reports": "التقارير والتوثيق",
    "achievements": "ملفات الإنجاز",
    "leadership": "الأداء القيادي",
    "assignments": "التكليفات",
    "meetings": "الاجتماعات والمحاضر",
    "plans": "الخطط التشغيلية",
    "documents": "الوثائق والأرشفة",
    "circulars": "التعاميم",
    "tickets": "الطلبات الداخلية",
    "ai": "مساعدات الذكاء الاصطناعي",
}


def _aggregate(queryset, school_ids, *, school_field="school_id", date_field="created_at"):
    rows = (
        queryset.filter(**{f"{school_field}__in": school_ids})
        .values(school_field)
        .annotate(total=Count("pk"), latest=Max(date_field))
    )
    return {
        int(row[school_field]): {
            "count": int(row["total"] or 0),
            "latest": row["latest"],
        }
        for row in rows
    }


def _payment_map(school_ids):
    rows = (
        Payment.objects.filter(
            school_id__in=school_ids,
            purpose=Payment.Purpose.SUBSCRIPTION,
        )
        .values("school_id")
        .annotate(
            pending=Count("pk", filter=Q(status=Payment.Status.PENDING)),
            approved=Count("pk", filter=Q(status=Payment.Status.APPROVED)),
            latest=Max("created_at"),
        )
    )
    return {int(row["school_id"]): row for row in rows}


def _manager_map(school_ids):
    managers = (
        SchoolMembership.objects.filter(
            school_id__in=school_ids,
            role_type=SchoolMembership.RoleType.MANAGER,
            is_active=True,
            teacher__is_active=True,
        )
        .select_related("teacher")
        .order_by("school_id", "teacher_id")
    )
    result = defaultdict(list)
    for membership in managers:
        result[membership.school_id].append(membership.teacher)
    return result


def _recommended_plans(schools, seats_by_school):
    paid = list(
        SubscriptionPlan.objects.filter(is_active=True, price__gt=Decimal("0"))
        .order_by("price", "max_teachers", "id")
    )
    result = {}
    for school in schools:
        seats = max(int(seats_by_school.get(school.id, 0)), 1)
        candidates = [plan for plan in paid if not plan.max_teachers or plan.max_teachers >= seats]
        result[school.id] = candidates[0] if candidates else (paid[-1] if paid else None)
    return result


def build_conversion_rows(schools) -> list[dict]:
    """Return scored rows for an iterable/queryset of schools in bounded queries."""
    schools = list(schools)
    school_ids = [school.id for school in schools]
    if not school_ids:
        return []

    subscriptions = {
        item.school_id: item
        for item in SchoolSubscription.objects.filter(school_id__in=school_ids).select_related("plan")
    }
    seats = SchoolMembership.seats_used_by_school(school_ids)
    active_members = dict(
        SchoolMembership.objects.filter(school_id__in=school_ids, is_active=True)
        .values("school_id")
        .annotate(total=Count("teacher_id", distinct=True))
        .values_list("school_id", "total")
    )
    managers = _manager_map(school_ids)
    payments = _payment_map(school_ids)

    service_maps = {
        "reports": _aggregate(Report.objects, school_ids),
        "achievements": _aggregate(TeacherAchievementFile.objects, school_ids),
        "leadership": _aggregate(SchoolLeadershipPortfolio.objects, school_ids),
        "assignments": _aggregate(Assignment.objects, school_ids),
        "meetings": _aggregate(Meeting.objects, school_ids),
        "plans": _aggregate(Plan.objects, school_ids),
        "documents": _aggregate(Document.objects, school_ids),
        "circulars": _aggregate(CircularDraft.objects, school_ids),
        "tickets": _aggregate(Ticket.objects.filter(is_platform=False), school_ids),
        "ai": _aggregate(
            AiUsageEvent.objects.filter(outcome=AiUsageEvent.Outcome.SUCCESS), school_ids
        ),
    }

    completed_maps = {
        "reports": _aggregate(
            Report.objects.filter(approval_state=ApprovalState.APPROVED), school_ids
        ),
        "achievements": _aggregate(
            TeacherAchievementFile.objects.filter(
                status=TeacherAchievementFile.Status.APPROVED
            ),
            school_ids,
        ),
        "leadership": _aggregate(
            SchoolLeadershipPortfolio.objects.filter(
                status=SchoolLeadershipPortfolio.Status.COMPLETED
            ),
            school_ids,
        ),
        "assignments": _aggregate(
            AssignmentTarget.objects.filter(approval_state=ApprovalState.APPROVED),
            school_ids,
        ),
        "meetings": _aggregate(
            Meeting.objects.filter(status=Meeting.Status.HELD), school_ids
        ),
        "plans": _aggregate(
            Plan.objects.filter(stage__in=(Plan.Stage.RUNNING, Plan.Stage.CLOSED)),
            school_ids,
        ),
        "documents": _aggregate(
            Document.objects.filter(approval_state=ApprovalState.APPROVED), school_ids
        ),
        "circulars": _aggregate(
            CircularDraft.objects.filter(published_at__isnull=False), school_ids
        ),
        "tickets": _aggregate(
            Ticket.objects.filter(is_platform=False, status=Ticket.Status.DONE), school_ids
        ),
    }
    plans = _recommended_plans(schools, seats)
    now = timezone.now()
    rows = []
    for school in schools:
        sid = school.id
        subscription = subscriptions.get(sid)
        school_managers = managers.get(sid, [])
        manager_last_login = max(
            (manager.last_login for manager in school_managers if manager.last_login),
            default=None,
        )
        service_counts = {
            key: int(values.get(sid, {}).get("count", 0))
            for key, values in service_maps.items()
        }
        completed_counts = {
            key: int(values.get(sid, {}).get("count", 0))
            for key, values in completed_maps.items()
        }
        latest_candidates = [
            values.get(sid, {}).get("latest")
            for values in service_maps.values()
            if values.get(sid, {}).get("latest")
        ]
        if manager_last_login:
            latest_candidates.append(manager_last_login)
        last_activity = max(latest_candidates, default=None)
        active_services = [key for key, count in service_counts.items() if count]
        completed_total = sum(completed_counts.values())
        member_count = int(active_members.get(sid, 0))

        setup_points = 0
        if school_managers:
            setup_points += 7
        if member_count > 1:
            setup_points += 8
        breadth_points = min(20, len(active_services) * 4)
        completion_points = min(30, completed_total * 5)
        recency_points = 0
        days_since_activity = None
        if last_activity:
            aware_activity = last_activity
            if timezone.is_naive(aware_activity):
                aware_activity = timezone.make_aware(aware_activity)
            days_since_activity = max(0, (now - aware_activity).days)
            if days_since_activity <= 7:
                recency_points = 15
            elif days_since_activity <= 30:
                recency_points = 10
            elif days_since_activity <= 90:
                recency_points = 5
        manager_points = 10 if manager_last_login and (now - manager_last_login) <= timedelta(days=30) else 0
        team_points = min(10, max(0, member_count - 1) * 2)
        utilization = min(
            100,
            setup_points + breadth_points + completion_points + recency_points + manager_points + team_points,
        )

        payment = payments.get(sid, {})
        pending_payment = int(payment.get("pending") or 0)
        approved_payment = int(payment.get("approved") or 0)
        intent = 0
        intent_factors = []
        if pending_payment:
            intent += 35
            intent_factors.append("يوجد طلب دفع قيد المراجعة")
        if approved_payment:
            intent += 20
            intent_factors.append("لدى المدرسة سجل دفع سابق")
        if utilization >= 60:
            intent += 25
            intent_factors.append("استفادة فعلية مرتفعة من الخدمات")
        elif utilization >= 30:
            intent += 15
            intent_factors.append("بدأت المدرسة استخدام أكثر من مسار")
        if days_since_activity is not None and days_since_activity <= 14:
            intent += 15
            intent_factors.append("نشاط حديث خلال أسبوعين")
        if manager_points:
            intent += 5
            intent_factors.append("عودة حديثة لمدير المدرسة")
        intent = min(100, intent)

        plan_is_free = bool(subscription and subscription.plan.price == 0)
        expired = bool(subscription and subscription.is_expired)
        cancelled = bool(subscription and subscription.is_cancelled)
        if not subscription:
            segment, segment_label = "no_subscription", "بلا اشتراك"
        elif cancelled:
            segment, segment_label = "cancelled", "اشتراك ملغي"
        elif plan_is_free and expired:
            segment, segment_label = "trial_expired", "مجانية منتهية"
        elif plan_is_free:
            segment, segment_label = "trial_active", "مجانية نشطة"
        elif expired:
            segment, segment_label = "paid_expired", "مدفوعة منتهية"
        else:
            segment, segment_label = "paid_active", "مدفوعة نشطة"

        if pending_payment:
            priority = "urgent"
            priority_label = "متابعة طلب الدفع"
        elif segment in {"trial_expired", "paid_expired"} and utilization >= 35:
            priority = "high"
            priority_label = "فرصة تحويل مرتفعة"
        elif utilization < 20:
            priority = "activate"
            priority_label = "تفعيل الاستفادة أولًا"
        elif segment == "trial_active":
            priority = "medium"
            priority_label = "تهيئة للتحويل"
        else:
            priority = "low"
            priority_label = "متابعة هادئة"

        if utilization < 20:
            recommended_action = "ابدأ بجلسة تفعيل قصيرة على خدمتين مناسبتين قبل عرض الباقة."
        elif pending_payment:
            recommended_action = "تابع طلب الدفع الحالي وساعد الإدارة على إكماله دون إنشاء طلب جديد."
        elif segment == "paid_expired":
            recommended_action = "اربط التجديد بالأعمال التي أنجزتها المدرسة واستمراريتها."
        else:
            recommended_action = "اعرض الباقة المناسبة بالاستناد إلى الخدمات التي أثبتت فائدتها للمدرسة."

        score_factors = [
            {"label": "اكتمال الإعداد", "points": setup_points, "max": 15},
            {"label": "تنوع الخدمات", "points": breadth_points, "max": 20},
            {"label": "أعمال مكتملة", "points": completion_points, "max": 30},
            {"label": "حداثة النشاط", "points": recency_points, "max": 15},
            {"label": "تفاعل المدير", "points": manager_points, "max": 10},
            {"label": "تبني الفريق", "points": team_points, "max": 10},
        ]
        rows.append(
            {
                "school": school,
                "subscription": subscription,
                "segment": segment,
                "segment_label": segment_label,
                "is_target": segment != "paid_active",
                "utilization_score": utilization,
                "intent_score": intent,
                "priority": priority,
                "priority_label": priority_label,
                "recommended_action": recommended_action,
                "service_counts": service_counts,
                "completed_counts": completed_counts,
                "services": [
                    {"key": key, "label": SERVICE_LABELS[key], "count": service_counts[key]}
                    for key in active_services
                ],
                "services_used": len(active_services),
                "completed_total": completed_total,
                "active_members": member_count,
                "managers": school_managers,
                "manager_emails": sorted({manager.email.strip().lower() for manager in school_managers if manager.email.strip()}),
                "last_activity": last_activity,
                "days_since_activity": days_since_activity,
                "pending_payments": pending_payment,
                "approved_payments": approved_payment,
                "intent_factors": intent_factors,
                "score_factors": score_factors,
                "recommended_plan": plans.get(sid),
                "days_remaining": subscription.days_remaining if subscription else None,
            }
        )
    return rows


def safe_ai_snapshot(row: dict) -> dict:
    """Build the only payload allowed to leave Tawtheeq for copy generation."""
    school = row["school"]
    plan = row.get("recommended_plan")
    subscription = row.get("subscription")
    return {
        "school": {
            "display_name": school.name,
            "stage": school.get_stage_display(),
            "gender": school.get_gender_display(),
            "city": school.city or "غير محددة",
        },
        "subscription": {
            "segment": row["segment_label"],
            "current_plan": subscription.plan.name if subscription else "لا يوجد",
            "end_date": subscription.end_date.isoformat() if subscription else None,
        },
        "signals": {
            "utilization_score": row["utilization_score"],
            "intent_score": row["intent_score"],
            "services_used": row["services_used"],
            "service_labels": [item["label"] for item in row["services"]],
            "completed_workflows": row["completed_total"],
            "active_team_members": row["active_members"],
            "days_since_last_activity": row["days_since_activity"],
            "pending_subscription_payment": bool(row["pending_payments"]),
        },
        "recommendation": {
            "next_action": row["recommended_action"],
            "suggested_plan": plan.name if plan else "تواصل لاكتشاف الاحتياج",
            "suggested_plan_price": str(plan.price) if plan else None,
            "basis": "تقدير أولي بحسب عدد المنسوبين الحالي والخدمات المستخدمة؛ يراجع قبل الإرسال.",
        },
    }


def default_objective(row: dict) -> str:
    if row["utilization_score"] < 20:
        return "activate"
    if row["segment"] == "paid_expired":
        return "renew"
    if row["segment"] in {"trial_expired", "cancelled"}:
        return "recover"
    return "convert"
