from __future__ import annotations

import random
import time
from datetime import timedelta
from typing import Any

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q
from django.contrib.auth.hashers import make_password
from django.utils import timezone

from reports.models import (
    Department,
    DepartmentMembership,
    Notification,
    NotificationRecipient,
    Report,
    ReportType,
    School,
    SchoolMembership,
    SchoolSubscription,
    SubscriptionPlan,
    Teacher,
    Ticket,
)
from reports.permissions import restrict_queryset_for_user


class Command(BaseCommand):
    help = "Seed multi-school data and run lightweight query benchmarks (200-school ready)."

    def add_arguments(self, parser):
        parser.add_argument("--schools", type=int, default=200)
        parser.add_argument("--teachers-per-school", type=int, default=10)
        parser.add_argument("--days", type=int, default=7)
        parser.add_argument("--tickets-per-school", type=int, default=20)
        parser.add_argument("--notifications-per-school", type=int, default=3)
        parser.add_argument("--seed", type=int, default=2025)
        parser.add_argument("--skip-benchmark", action="store_true", default=False)
        parser.add_argument("--skip-seed", action="store_true", default=False)

    def handle(self, *args: Any, **options: Any):
        schools_n: int = options["schools"]
        teachers_per_school: int = options["teachers_per_school"]
        days: int = options["days"]
        tickets_per_school: int = options["tickets_per_school"]
        notifications_per_school: int = options["notifications_per_school"]
        seed: int = options["seed"]
        skip_benchmark: bool = options["skip_benchmark"]
        skip_seed: bool = options["skip_seed"]

        rnd = random.Random(seed)
        password_hash = make_password("pass12345")

        dept_defs = [
            ("manager", "الإدارة"),
            ("activity", "النشاط"),
            ("volunteer", "التطوع"),
            ("affairs", "الشؤون المدرسية"),
            ("admin", "الشؤون الإدارية"),
        ]
        rtype_defs = [
            ("behavior", "سلوك"),
            ("activity", "نشاط"),
            ("volunteer", "تطوع"),
            ("discipline", "انضباط"),
        ]

        now = timezone.now().date()

        if days < 1:
            raise ValueError("--days must be >= 1")

        plan = self._ensure_plan()

        if skip_seed:
            self.stdout.write(self.style.MIGRATE_HEADING("Skipping seed; running benchmarks only..."))
            if not skip_benchmark:
                self._benchmark(rnd)
            return

        self.stdout.write(self.style.MIGRATE_HEADING("Seeding schools/teachers/departments/report-types..."))

        created_schools = 0
        created_teachers = 0
        created_reports = 0
        created_tickets = 0
        created_notifications = 0

        with transaction.atomic():
            for i in range(1, schools_n + 1):
                code = f"s{i:03d}"
                school, s_created = School.objects.get_or_create(
                    code=code,
                    defaults={
                        "name": f"مدرسة {i}",
                        "is_active": True,
                    },
                )
                if s_created:
                    created_schools += 1

                # ensure subscription
                SchoolSubscription.objects.get_or_create(
                    school=school,
                    defaults={
                        "plan": plan,
                        "start_date": now - timedelta(days=7),
                        "end_date": now + timedelta(days=365),
                        "is_active": True,
                    },
                )

                # departments (same slugs across schools)
                departments: dict[str, Department] = {}
                for slug, name in dept_defs:
                    dep, _ = Department.objects.get_or_create(
                        school=school,
                        slug=slug,
                        defaults={
                            "name": name,
                            "role_label": name,
                            "is_active": True,
                        },
                    )
                    departments[slug] = dep

                # report types (same codes across schools)
                rtypes: dict[str, ReportType] = {}
                for code2, name2 in rtype_defs:
                    rt, _ = ReportType.objects.get_or_create(
                        school=school,
                        code=code2,
                        defaults={
                            "name": name2,
                            "is_active": True,
                            "order": 0,
                        },
                    )
                    rtypes[code2] = rt

                # link dept -> report types
                try:
                    departments["activity"].reporttypes.set([rtypes["activity"], rtypes["discipline"]])
                    departments["volunteer"].reporttypes.set([rtypes["volunteer"], rtypes["discipline"]])
                    departments["affairs"].reporttypes.set([rtypes["behavior"], rtypes["discipline"]])
                    departments["admin"].reporttypes.set([rtypes["discipline"]])
                    departments["manager"].reporttypes.set(list(rtypes.values()))
                except Exception:
                    pass

                # manager user per school
                mgr_phone = f"05{i:03d}00000"  # 10 digits
                manager, mgr_created = Teacher.objects.get_or_create(
                    phone=mgr_phone,
                    defaults={"name": f"مدير {school.name}", "is_active": True},
                )
                if mgr_created:
                    manager.password = password_hash
                    manager.save(update_fields=["password"])
                    created_teachers += 1

                SchoolMembership.objects.update_or_create(
                    school=school,
                    teacher=manager,
                    role_type=SchoolMembership.RoleType.MANAGER,
                    defaults={"is_active": True},
                )

                # teachers
                teachers: list[Teacher] = [manager]
                for t in range(1, teachers_per_school + 1):
                    phone = f"05{i:03d}{t:05d}"  # 10 digits
                    teacher, t_created = Teacher.objects.get_or_create(
                        phone=phone,
                        defaults={"name": f"معلم {t} - {school.name}", "is_active": True},
                    )
                    if t_created:
                        teacher.password = password_hash
                        teacher.save(update_fields=["password"])
                        created_teachers += 1

                    SchoolMembership.objects.update_or_create(
                        school=school,
                        teacher=teacher,
                        role_type=SchoolMembership.RoleType.TEACHER,
                        defaults={"is_active": True},
                    )
                    teachers.append(teacher)

                # assign department officers + memberships
                officer_by_dept: dict[str, Teacher] = {}
                for slug, _name in dept_defs:
                    if slug == "manager":
                        continue
                    officer = rnd.choice(teachers[1:])
                    officer_by_dept[slug] = officer
                    DepartmentMembership.objects.get_or_create(
                        department=departments[slug],
                        teacher=officer,
                        defaults={"role_type": DepartmentMembership.OFFICER},
                    )

                # regular teacher memberships: each teacher in one random dept
                for teacher in teachers[1:]:
                    slug = rnd.choice(["activity", "volunteer", "affairs", "admin"])
                    DepartmentMembership.objects.get_or_create(
                        department=departments[slug],
                        teacher=teacher,
                        defaults={"role_type": DepartmentMembership.TEACHER},
                    )

                # reports
                categories = list(rtypes.values())
                existing_report_keys = set(
                    Report.objects.filter(
                        school=school,
                        teacher__in=teachers[1:],
                        report_date__gte=now - timedelta(days=days - 1),
                        report_date__lte=now,
                    ).values_list("teacher_id", "report_date")
                )
                reports_to_create = []
                for teacher in teachers[1:]:
                    for d in range(days):
                        cat = rnd.choice(categories)
                        dt = now - timedelta(days=d)
                        if (teacher.pk, dt) in existing_report_keys:
                            continue
                        reports_to_create.append(
                            Report(
                                school=school,
                                teacher=teacher,
                                teacher_name=teacher.name,
                                title=f"تقرير يومي - {teacher.name}",
                                report_date=dt,
                                category=cat,
                                idea="تقرير تجريبي",
                            )
                        )
                # Benchmark fixtures must not run production post-save hooks:
                # thousands of synthetic rows previously invalidated caches and
                # dispatched Celery notifications, making the seeder measure its
                # own side effects instead of database/query performance.
                Report.objects.bulk_create(reports_to_create, batch_size=500)
                created_reports += len(reports_to_create)

                # tickets
                dept_choices = [d for d in departments.values() if d.slug != "manager"]
                existing_ticket_titles = set(
                    Ticket.objects.filter(school=school).values_list("title", flat=True)
                )
                tickets_to_create = []
                for k in range(tickets_per_school):
                    creator = rnd.choice(teachers[1:])
                    dep = rnd.choice(dept_choices)
                    assignee = officer_by_dept.get(dep.slug) or rnd.choice(teachers[1:])
                    title = f"طلب {k + 1} - {school.code}"
                    if title in existing_ticket_titles:
                        continue
                    tickets_to_create.append(
                        Ticket(
                            school=school,
                            creator=creator,
                            department=dep,
                            title=title,
                            body="طلب تجريبي",
                            assignee=assignee,
                            status=Ticket.Status.OPEN,
                        )
                    )
                Ticket.objects.bulk_create(tickets_to_create, batch_size=500)
                created_tickets += len(tickets_to_create)

                # notifications
                for n in range(notifications_per_school):
                    notif, n_created = Notification.objects.get_or_create(
                        school=school,
                        title=f"تنبيه {n + 1} - {school.code}",
                        defaults={"message": "تنبيه تجريبي"},
                    )
                    if n_created:
                        created_notifications += 1

                    # recipients: random slice
                    recipients = rnd.sample(teachers, k=min(5, len(teachers)))
                    for t in recipients:
                        NotificationRecipient.objects.get_or_create(notification=notif, teacher=t)

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. created schools={created_schools}, teachers={created_teachers}, reports={created_reports}, tickets={created_tickets}, notifications={created_notifications}"
            )
        )

        if not skip_benchmark:
            self.stdout.write(self.style.MIGRATE_HEADING("Running lightweight benchmarks..."))
            self._benchmark(rnd)

    def _ensure_plan(self) -> SubscriptionPlan:
        plan, _ = SubscriptionPlan.objects.get_or_create(
            name="Basic",
            defaults={
                "price": 0,
                "days_duration": 365,
                "description": "Seed plan",
                "is_active": True,
            },
        )
        return plan

    def _bench(self, label: str, fn):
        from django.db import connection

        class _Counter:
            def __init__(self):
                self.n = 0

            def __call__(self, execute, sql, params, many, context):
                self.n += 1
                return execute(sql, params, many, context)

        counter = _Counter()
        with connection.execute_wrapper(counter):
            t0 = time.perf_counter()
            result = fn()
            t1 = time.perf_counter()

        ms = (t1 - t0) * 1000.0
        self.stdout.write(f"- {label}: {ms:.1f}ms, queries={counter.n}")
        return result

    def _benchmark(self, rnd: random.Random):
        # pick a sample school that exists
        school = School.objects.order_by("id").first()
        if not school:
            self.stdout.write(self.style.WARNING("No schools found for benchmark."))
            return

        manager_mem = SchoolMembership.objects.filter(
            school=school,
            role_type=SchoolMembership.RoleType.MANAGER,
            is_active=True,
        ).select_related("teacher").first()
        manager = manager_mem.teacher if manager_mem else None

        officer_mem = DepartmentMembership.objects.filter(
            department__school=school,
            role_type=DepartmentMembership.OFFICER,
        ).select_related("teacher", "department").first()
        officer = officer_mem.teacher if officer_mem else None

        sample_teacher = (
            SchoolMembership.objects.filter(school=school, role_type=SchoolMembership.RoleType.TEACHER, is_active=True)
            .select_related("teacher")
            .first()
        )
        teacher = sample_teacher.teacher if sample_teacher else None

        if manager:
            def q_admin_reports():
                qs = Report.objects.select_related("teacher", "category").filter(school=school).order_by("-report_date", "-id")
                qs = restrict_queryset_for_user(qs, manager, school)
                return list(qs[:20])

            self._bench("admin_reports page(20)", q_admin_reports)

        if officer:
            def q_officer_allowed_reporttypes():
                qs = (
                    ReportType.objects.filter(
                        is_active=True,
                        departments__memberships__teacher=officer,
                        departments__memberships__role_type=DepartmentMembership.OFFICER,
                        departments__school=school,
                    )
                    .distinct()
                    .order_by("order", "name")
                )
                return list(qs)

            self._bench("officer allowed reporttypes", q_officer_allowed_reporttypes)

            def q_officer_tickets_inbox():
                qs = Ticket.objects.filter(school=school, assignee=officer).order_by("-created_at")
                return list(qs[:20])

            self._bench("officer tickets inbox page(20)", q_officer_tickets_inbox)

        if teacher:
            def q_unread_notifications():
                qs = NotificationRecipient.objects.filter(
                    teacher=teacher,
                    is_read=False,
                ).filter(
                    Q(notification__school=school) | Q(notification__school__isnull=True)
                )
                return qs.count()

            self._bench("unread notifications count", q_unread_notifications)
