# -*- coding: utf-8 -*-
"""Role-aware onboarding and school-readiness guidance.

The guidance is intentionally derived from persisted data. A checklist that is
manually dismissed can tell a manager that requests are ready while the school
still has no department or recipient; these checks cannot drift that way.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from django.urls import reverse
from django.utils import timezone

from .models import (
    AuditLog,
    Department,
    DepartmentMembership,
    Report,
    ReportType,
    School,
    SchoolGroupMembership,
    SchoolMembership,
    SchoolSubscription,
    StaffScope,
    TeacherAchievementFile,
    Ticket,
)
from .model_parts.base import MANAGER_SLUG


@dataclass(frozen=True)
class GuidanceStep:
    key: str
    title: str
    description: str
    url: str
    complete: bool
    impact: str = ""
    severity: str = "normal"
    applicable: bool = True

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "title": self.title,
            "description": self.description,
            "url": self.url,
            "complete": self.complete,
            "impact": self.impact,
            "severity": self.severity,
            "applicable": self.applicable,
        }


def _summary(steps: Iterable[GuidanceStep]) -> dict:
    items = list(steps)
    applicable_items = [step for step in items if step.applicable]
    completed = sum(1 for step in applicable_items if step.complete)
    total = len(applicable_items)
    percent = round((completed / total) * 100) if total else 100
    return {
        "steps": [step.as_dict() for step in items],
        "completed": completed,
        "total": total,
        "percent": percent,
        "ready": completed == total,
        "next_step": next(
            (
                step.as_dict()
                for step in applicable_items
                if not step.complete
            ),
            None,
        ),
    }


def school_readiness(school: School) -> dict:
    """Return the manager-facing operational readiness of one school."""
    today = timezone.localdate()
    subscription_ready = SchoolSubscription.objects.filter(
        school=school,
        is_active=True,
        end_date__gte=today,
    ).exists()
    profile_ready = bool(
        (school.phone or "").strip()
        and (school.current_academic_year or "").strip()
    )
    team_count = (
        SchoolMembership.objects.filter(
            school=school,
            is_active=True,
            role_type__in=SchoolMembership.STAFF_ROLES,
            teacher__is_active=True,
        )
        .values("teacher_id")
        .distinct()
        .count()
    )
    departments = Department.objects.filter(school=school, is_active=True)
    department_count = departments.count()
    officer_departments = departments.exclude(slug=MANAGER_SLUG)
    officer_department_required_count = officer_departments.count()
    report_type_count = ReportType.objects.filter(school=school, is_active=True).count()
    officer_department_count = (
        DepartmentMembership.objects.filter(
            department__in=officer_departments,
            role_type=DepartmentMembership.OFFICER,
        )
        .values("department_id")
        .distinct()
        .count()
    )
    scoped_roles = SchoolMembership.objects.filter(
        school=school,
        is_active=True,
        role_type__in=(
            SchoolMembership.RoleType.DEPUTY,
            SchoolMembership.RoleType.ADMIN_STAFF,
        ),
    ).select_related("teacher")
    scoped_role_count = scoped_roles.count()
    configured_membership_ids = set(
        StaffScope.objects.filter(membership__in=scoped_roles).values_list(
            "membership_id", flat=True
        )
    )
    configured_scope_count = len(configured_membership_ids)
    scope_gaps = [
        {
            "membership_id": membership.pk,
            "name": membership.teacher.name or membership.teacher.phone,
            "role": membership.get_role_type_display(),
            "url": reverse("reports:staff_role_scope", args=[membership.pk]),
        }
        for membership in scoped_roles
        if membership.pk not in configured_membership_ids
    ]

    steps = (
        GuidanceStep(
            "subscription",
            "اشتراك المدرسة",
            "تحقق من أن الاشتراك نشط وأن تاريخ الانتهاء معروف للإدارة.",
            reverse("reports:my_subscription"),
            subscription_ready,
            "تتوقف مسارات العمل عند انتهاء الاشتراك.",
            "critical",
        ),
        GuidanceStep(
            "profile",
            "بيانات المدرسة والسنة الحالية",
            "حدّد السنة الدراسية وراجع رقم الجوال لتظهر بيانات المدرسة الأساسية صحيحة.",
            reverse("reports:school_settings"),
            profile_ready,
            "تؤثر في الطباعة والتصنيف والتقارير التنفيذية.",
        ),
        GuidanceStep(
            "team",
            "فريق المدرسة",
            "أضف أول مستخدم وحدد دوره الوظيفي.",
            reverse("reports:bulk_import_teachers"),
            team_count > 0,
            "لا يمكن بدء التوثيق أو إسناد الطلبات بلا فريق.",
            "critical",
        ),
        GuidanceStep(
            "departments",
            "الأقسام",
            "أنشئ الأقسام التي تستقبل الطلبات وتنظم نطاق العمل.",
            reverse("reports:departments_list"),
            department_count > 0,
            "إنشاء الطلبات يعتمد على وجود قسم نشط.",
            "critical",
        ),
        GuidanceStep(
            "report_types",
            "أنواع التقارير",
            "حدد أنواع التقارير المتاحة للفريق واربطها بالأقسام المناسبة.",
            reverse("reports:reporttypes_list"),
            report_type_count > 0,
            "لن يظهر نموذج تقرير قابل للاستخدام قبل إنشاء نوع تقرير.",
            "critical",
        ),
        GuidanceStep(
            "department_owners",
            "مسؤولو الأقسام",
            "عيّن مسؤولًا لكل قسم ليستقبل الطلبات ويتابع أعماله.",
            reverse("reports:departments_list"),
            department_count > 0 and officer_department_count == officer_department_required_count,
            "القسم بلا مسؤول قد يظهر للمستخدم دون مستلم صالح.",
        ),
        GuidanceStep(
            "staff_scopes",
            "نطاقات الوكلاء والموظفين الإداريين",
            "راجع ما يستطيع كل مستخدم فعله والأقسام التي يعمل داخلها.",
            reverse("reports:staff_roles"),
            scoped_role_count == configured_scope_count,
            "النطاق غير المضبوط يمنع العمل الإشرافي والإداري المقصود.",
            applicable=scoped_role_count > 0,
        ),
    )
    summary = _summary(steps)
    summary.update(
        {
            "role": "manager",
            "title": "صحة المدرسة",
            "counts": {
                "team": team_count,
                "departments": department_count,
                "report_types": report_type_count,
                "department_owners": officer_department_count,
                "department_owners_required": officer_department_required_count,
                "scoped_roles": scoped_role_count,
                "configured_scopes": configured_scope_count,
                "scope_gaps": len(scope_gaps),
            },
            "scope_gaps": scope_gaps,
        }
    )
    return summary


def platform_guidance() -> dict:
    today = timezone.localdate()
    schools = School.objects.filter(is_active=True)
    schools_count = schools.count()
    manager_school_count = (
        SchoolMembership.objects.filter(
            school__in=schools,
            is_active=True,
            role_type=SchoolMembership.RoleType.MANAGER,
        )
        .values("school_id")
        .distinct()
        .count()
    )
    subscribed_school_count = (
        SchoolSubscription.objects.filter(
            school__in=schools,
            is_active=True,
            end_date__gte=today,
        )
        .values("school_id")
        .distinct()
        .count()
    )
    steps = (
        GuidanceStep(
            "schools",
            "إضافة المدارس",
            "أنشئ المدرسة وتحقق من بياناتها الأساسية.",
            reverse("reports:platform_schools_directory"),
            schools_count > 0,
        ),
        GuidanceStep(
            "managers",
            "ربط مدير بكل مدرسة",
            "تأكد أن لكل مدرسة نشطة مديرًا فعليًا واحدًا على الأقل.",
            reverse("reports:school_managers_list"),
            schools_count > 0 and manager_school_count == schools_count,
            "المدرسة بلا مدير لا تملك صاحب قرار داخل المنصة.",
            "critical",
        ),
        GuidanceStep(
            "subscriptions",
            "تفعيل الاشتراكات",
            "راجع المدارس النشطة التي لا تملك اشتراكًا ساريًا.",
            reverse("reports:platform_subscriptions_list"),
            schools_count > 0 and subscribed_school_count == schools_count,
            "المستخدمون المرتبطون بمدرسة منتهية لن يصلوا لمساحة العمل.",
            "critical",
        ),
        GuidanceStep(
            "audit",
            "مراجعة سجل النظام",
            "راقب الدخول والإنشاء والتعديل والحذف من سجل واحد.",
            reverse("reports:platform_audit_logs"),
            AuditLog.objects.exists(),
        ),
    )
    summary = _summary(steps)
    summary.update({"role": "platform", "title": "تشغيل المنصة"})
    return summary


def executive_guidance(user) -> dict:
    memberships = SchoolGroupMembership.objects.filter(
        user=user,
        is_active=True,
        role_type=SchoolGroupMembership.RoleType.EXECUTIVE_DIRECTOR,
    ).select_related("group")
    group_ids = list(memberships.values_list("group_id", flat=True))
    groups_count = len(group_ids)
    schools_count = School.objects.filter(group_id__in=group_ids, is_active=True).count()
    steps = (
        GuidanceStep(
            "group",
            "مراجعة نطاق المجموعة",
            "تحقق من المجموعة والمدارس التي تقع ضمن نطاق اطلاعك.",
            reverse("reports:executive_dashboard"),
            groups_count > 0,
            "لن تظهر أي مؤشرات دون عضوية مدير تنفيذي نشطة.",
            "critical",
        ),
        GuidanceStep(
            "schools",
            "المدارس المرتبطة",
            "راجع المدارس وحالة اشتراك كل مدرسة.",
            reverse("reports:executive_dashboard"),
            schools_count > 0,
        ),
        GuidanceStep(
            "report",
            "التقرير التنفيذي المقارن",
            "قارن المدارس ثم نزّل نسخة Excel أو PDF للإدارة العليا.",
            reverse("reports:group_report"),
            schools_count > 0,
        ),
    )
    summary = _summary(steps)
    summary.update({"role": "executive", "title": "رحلة المدير التنفيذي"})
    return summary


def staff_guidance(user, school: School, role: str) -> dict:
    membership = SchoolMembership.objects.filter(
        school=school,
        teacher=user,
        role_type=role,
        is_active=True,
    ).first()
    scope = StaffScope.objects.filter(membership=membership).first() if membership else None
    department_count = DepartmentMembership.objects.filter(
        teacher=user,
        department__school=school,
        department__is_active=True,
    ).count()
    assigned_count = Ticket.objects.filter(school=school, assignee=user).count()
    steps = (
        GuidanceStep(
            "scope",
            "اعرف صلاحياتك ونطاقك",
            "راجع الأقسام والصلاحيات التي منحها لك مدير المدرسة.",
            reverse("reports:my_profile"),
            scope is not None,
            "إن لم يظهر نطاقك، اطلب من مدير المدرسة إكمال ضبط الدور.",
            "critical",
        ),
        GuidanceStep(
            "departments",
            "الأقسام المرتبطة بك",
            "تأكد أن الأقسام التي تعمل معها ظاهرة في حسابك.",
            reverse("reports:home"),
            department_count > 0,
        ),
        GuidanceStep(
            "inbox",
            "صندوق الطلبات المحالة",
            "تابع الطلبات وحدّث الحالة وأضف ملاحظات واضحة لصاحب الطلب.",
            reverse("reports:assigned_to_me"),
            assigned_count > 0,
        ),
        GuidanceStep(
            "activity",
            "سجل أعمالك",
            "راجع الأثر المسجل لعملياتك داخل المنصة.",
            reverse("reports:my_activity_log"),
            AuditLog.objects.filter(teacher=user, school=school).exists(),
        ),
    )
    summary = _summary(steps)
    summary.update(
        {
            "role": role,
            "title": "رحلة الوكيل" if role == SchoolMembership.RoleType.DEPUTY else "رحلة الموظف الإداري",
        }
    )
    return summary


def teacher_guidance(user, school: School) -> dict:
    report_types_ready = ReportType.objects.filter(school=school, is_active=True).exists()
    reports = Report.objects.filter(school=school, teacher=user)
    achievements = TeacherAchievementFile.objects.filter(school=school, teacher=user)
    steps = (
        GuidanceStep(
            "profile",
            "أكمل بيانات حسابك",
            "أضف بريدًا صالحًا وراجع اسمك ورقم الجوال.",
            reverse("reports:my_profile"),
            bool((getattr(user, "email", "") or "").strip()),
        ),
        GuidanceStep(
            "report_type",
            "تحقق من أنواع التقارير المتاحة",
            "إذا لم يظهر نوع تقرير، فإعداد المدرسة لم يكتمل بعد.",
            reverse("reports:add_report"),
            report_types_ready,
            "لا يمكنك إنشاء تقرير حتى يضيف مدير المدرسة نوعًا نشطًا.",
            "critical",
        ),
        GuidanceStep(
            "first_report",
            "أنشئ تقريرك الأول",
            "وثّق العمل ثم راجع نسخة الطباعة قبل المشاركة.",
            reverse("reports:add_report"),
            reports.exists(),
        ),
        GuidanceStep(
            "achievement",
            "جهز ملف الإنجاز",
            "أضف عناصر الملف ثم أرسله للمراجعة عند اكتماله.",
            reverse("reports:achievement_my_files"),
            achievements.exists(),
        ),
        GuidanceStep(
            "requests",
            "استخدم الطلبات الداخلية",
            "أنشئ طلبًا لقسم المدرسة وتابع الرد وتغير الحالة.",
            reverse("reports:my_requests"),
            Ticket.objects.filter(school=school, creator=user, is_platform=False).exists(),
        ),
    )
    summary = _summary(steps)
    summary.update({"role": "teacher", "title": "رحلة المعلم"})
    return summary


def role_guidance(user, school: School | None = None) -> dict:
    """Resolve the most relevant role journey for the signed-in user."""
    if getattr(user, "is_superuser", False):
        return platform_guidance()

    executive_exists = SchoolGroupMembership.objects.filter(
        user=user,
        is_active=True,
        role_type=SchoolGroupMembership.RoleType.EXECUTIVE_DIRECTOR,
    ).exists()
    if school is None and executive_exists:
        return executive_guidance(user)
    if school is None:
        return _summary(()) | {
            "role": "unassigned",
            "title": "تهيئة الحساب",
            "steps": [],
            "ready": False,
            "next_step": None,
        }

    roles = set(
        SchoolMembership.objects.filter(
            school=school,
            teacher=user,
            is_active=True,
        ).values_list("role_type", flat=True)
    )
    if SchoolMembership.RoleType.MANAGER in roles:
        return school_readiness(school)
    if SchoolMembership.RoleType.DEPUTY in roles:
        return staff_guidance(user, school, SchoolMembership.RoleType.DEPUTY)
    if SchoolMembership.RoleType.ADMIN_STAFF in roles:
        return staff_guidance(user, school, SchoolMembership.RoleType.ADMIN_STAFF)
    if SchoolMembership.RoleType.TEACHER in roles:
        return teacher_guidance(user, school)
    if executive_exists:
        return executive_guidance(user)
    return _summary(()) | {
        "role": "unassigned",
        "title": "تهيئة الحساب",
        "steps": [],
        "ready": False,
        "next_step": None,
    }
