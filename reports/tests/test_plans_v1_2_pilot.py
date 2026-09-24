# -*- coding: utf-8 -*-
"""V1.2 pilot acceptance and regression guards for Plans / Initiatives."""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from django.db import connection
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from reports import capabilities as caps
from reports.model_parts.approvals import ApprovalState
from reports.models import (
    Assignment,
    Delegation,
    Department,
    Initiative,
    Plan,
    PlanGoal,
    PlanTask,
    School,
    SchoolMembership,
    SchoolSubscription,
    StaffScope,
    SubscriptionPlan,
    Teacher,
)
from reports.services_approval import ApprovalError, approve, submit
from reports.services_plans import PlanError, convert_task_to_assignment, plans_visible_to


def _user(name: str, phone: str) -> Teacher:
    return Teacher.objects.create_user(phone=phone, name=name, password="Passw0rd!123")


@override_settings(ALLOWED_HOSTS=["testserver"])
class PlansPilotBase(TestCase):
    def setUp(self):
        subscription_plan = SubscriptionPlan.objects.create(
            name="باقة الخطط التجريبية", price=0, days_duration=365, max_teachers=0
        )
        self.school_a = School.objects.create(name="مدرسة ألف", code="plans-a")
        self.school_b = School.objects.create(name="مدرسة باء", code="plans-b")
        SchoolSubscription.objects.create(school=self.school_a, plan=subscription_plan)
        SchoolSubscription.objects.create(school=self.school_b, plan=subscription_plan)

        self.manager = _user("مدير المدرستين", "0500091201")
        for school in (self.school_a, self.school_b):
            SchoolMembership.objects.create(
                school=school,
                teacher=self.manager,
                role_type=SchoolMembership.RoleType.MANAGER,
            )

        self.deputy = _user("وكيل الخطط", "0500091202")
        self.deputy_membership = SchoolMembership.objects.create(
            school=self.school_a,
            teacher=self.deputy,
            role_type=SchoolMembership.RoleType.DEPUTY,
        )
        self.teacher = _user("منفذ الخطة", "0500091203")
        SchoolMembership.objects.create(
            school=self.school_a,
            teacher=self.teacher,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        self.department_a = Department.objects.create(
            school=self.school_a, name="القسم ألف", slug="plans-dept-a"
        )
        self.department_b = Department.objects.create(
            school=self.school_a, name="القسم باء", slug="plans-dept-b"
        )

    def _enter(self, user, school=None, *, enforce_csrf=False):
        client = Client(enforce_csrf_checks=enforce_csrf)
        client.force_login(user)
        session = client.session
        session["active_school_id"] = (school or self.school_a).pk
        session.save()
        return client

    def _plan(self, *, school=None, owner=None, title="خطة تشغيلية", **overrides):
        values = {
            "scope": Plan.Scope.SCHOOL,
            "school": school or self.school_a,
            "owner": owner or self.manager,
            "title": title,
            "academic_year": "1447-1448",
        }
        values.update(overrides)
        return Plan.objects.create(**values)

    def _task(self, plan, *, department=None, **overrides):
        values = {"plan": plan, "title": "مهمة تشغيلية", "department": department}
        values.update(overrides)
        return PlanTask.objects.create(**values)


class PlansPilotSecurityTests(PlansPilotBase):
    def test_owner_relationship_does_not_bypass_active_school(self):
        """Ownership never bypasses the active-school boundary."""
        plan = self._plan(school=self.school_a, owner=self.manager)
        client = self._enter(self.manager, self.school_b)
        routes = [
            ("get", reverse("reports:plan_detail", args=[plan.pk]), None),
            ("get", reverse("reports:plan_edit", args=[plan.pk]), None),
            ("get", reverse("reports:plan_print", args=[plan.pk]), None),
            ("post", reverse("reports:plan_delete", args=[plan.pk]), {}),
            (
                "post",
                reverse("reports:plan_action", args=[plan.pk]),
                {"plan_action": "add_goal", "title": "هدف عابر للمدرسة"},
            ),
            (
                "post",
                reverse("reports:plan_approval_action", args=[plan.pk]),
                {"approval_action": "issue"},
            ),
        ]
        for method, url, data in routes:
            with self.subTest(url=url):
                response = getattr(client, method)(url, data or {})
                self.assertEqual(response.status_code, 404)
        self.assertTrue(Plan.objects.filter(pk=plan.pk).exists())
        self.assertFalse(plan.goals.exists())

    def test_manager_relationship_does_not_bypass_active_school(self):
        plan = self._plan(school=self.school_a, owner=self.teacher)
        client = self._enter(self.manager, self.school_b)
        self.assertEqual(
            client.get(reverse("reports:plan_detail", args=[plan.pk])).status_code,
            404,
        )

    def test_direct_track_plans_is_limited_to_staffscope_departments(self):
        """Direct tracking authority remains inside the granted department scope."""
        scope = StaffScope.objects.create(
            membership=self.deputy_membership, capabilities=[caps.TRACK_PLANS]
        )
        scope.departments.add(self.department_a)
        plan_a = self._plan(title="خطة القسم ألف")
        plan_b = self._plan(title="خطة القسم باء")
        self._task(plan_a, department=self.department_a)
        self._task(plan_b, department=self.department_b)
        visible_ids = set(plans_visible_to(self.deputy, self.school_a).values_list("id", flat=True))
        self.assertEqual(visible_ids, {plan_a.pk})
        client = self._enter(self.deputy)
        self.assertEqual(
            client.get(reverse("reports:plan_detail", args=[plan_a.pk])).status_code,
            200,
        )
        self.assertEqual(
            client.get(reverse("reports:plan_detail", args=[plan_b.pk])).status_code,
            404,
        )

    def test_empty_staffscope_does_not_mean_every_plan(self):
        """An empty tracking scope grants no plans."""
        StaffScope.objects.create(
            membership=self.deputy_membership, capabilities=[caps.TRACK_PLANS]
        )
        plan = self._plan(title="خطة لا تقع في نطاق")
        self.assertFalse(plans_visible_to(self.deputy, self.school_a).exists())
        client = self._enter(self.deputy)
        self.assertEqual(
            client.get(reverse("reports:plan_detail", args=[plan.pk])).status_code,
            404,
        )

    def test_delegated_track_plans_preserves_staffscope_departments(self):
        """Delegated tracking authority keeps the existing department scope."""
        scope = StaffScope.objects.create(membership=self.deputy_membership)
        scope.departments.add(self.department_a)
        Delegation.objects.create(
            school=self.school_a,
            delegator=self.manager,
            delegate=self.deputy,
            capabilities=[caps.TRACK_PLANS],
            starts_at=timezone.now() - timedelta(hours=1),
            ends_at=timezone.now() + timedelta(hours=1),
        )
        plan_a = self._plan(title="خطة مفوضة ألف")
        plan_b = self._plan(title="خطة مفوضة باء")
        self._task(plan_a, department=self.department_a)
        self._task(plan_b, department=self.department_b)
        visible_ids = set(plans_visible_to(self.deputy, self.school_a).values_list("id", flat=True))
        self.assertEqual(visible_ids, {plan_a.pk})
        client = self._enter(self.deputy)
        self.assertEqual(
            client.get(reverse("reports:plan_detail", args=[plan_b.pk])).status_code,
            404,
        )

    def test_wrong_school_expired_and_revoked_delegations_grant_no_plan_scope(self):
        scope = StaffScope.objects.create(membership=self.deputy_membership)
        scope.departments.add(self.department_a)
        plan = self._plan(title="خطة لا يفتحها تفويض غير سار")
        self._task(plan, department=self.department_a)

        wrong_school = Delegation.objects.create(
            school=self.school_b,
            delegator=self.manager,
            delegate=self.deputy,
            capabilities=[caps.TRACK_PLANS],
            starts_at=timezone.now() - timedelta(hours=1),
            ends_at=timezone.now() + timedelta(hours=1),
        )
        expired = Delegation.objects.create(
            school=self.school_a,
            delegator=self.manager,
            delegate=self.deputy,
            capabilities=[caps.TRACK_PLANS],
            starts_at=timezone.now() - timedelta(hours=2),
            ends_at=timezone.now() - timedelta(hours=1),
        )
        revoked = Delegation.objects.create(
            school=self.school_a,
            delegator=self.manager,
            delegate=self.deputy,
            capabilities=[caps.TRACK_PLANS],
            starts_at=timezone.now() - timedelta(hours=1),
            ends_at=timezone.now() + timedelta(hours=1),
            revoked_at=timezone.now(),
            revoked_by=self.manager,
        )
        self.assertFalse(plans_visible_to(self.deputy, self.school_a).exists())
        self.assertEqual(
            {wrong_school.state, expired.state, revoked.state},
            {"active", "expired", "revoked"},
        )

    def test_submitted_plan_goal_cannot_be_removed_by_crafted_post(self):
        """Submitted plans preserve their review task structure."""
        plan = self._plan(approval_state=ApprovalState.SUBMITTED)
        goal = PlanGoal.objects.create(plan=plan, title="هدف مقفل")
        client = self._enter(self.manager)
        client.post(
            reverse("reports:plan_action", args=[plan.pk]),
            {"plan_action": "remove_goal", "goal_id": goal.pk},
        )
        self.assertTrue(PlanGoal.objects.filter(pk=goal.pk).exists())

    def test_approved_plan_task_cannot_be_removed_by_crafted_post(self):
        """Approved plans preserve their issued task structure."""
        plan = self._plan(approval_state=ApprovalState.APPROVED)
        task = self._task(plan)
        client = self._enter(self.manager)
        client.post(
            reverse("reports:plan_action", args=[plan.pk]),
            {"plan_action": "remove_task", "task_id": task.pk},
        )
        self.assertTrue(PlanTask.objects.filter(pk=task.pk).exists())

    def test_submitted_and_approved_plans_reject_all_item_mutations(self):
        client = self._enter(self.manager)
        for approval_state in (ApprovalState.SUBMITTED, ApprovalState.APPROVED):
            plan = self._plan(
                title=f"خطة {approval_state}", approval_state=approval_state
            )
            goal = PlanGoal.objects.create(plan=plan, title="هدف ثابت")
            task = self._task(plan)
            attempts = [
                {"plan_action": "add_goal", "title": "هدف دخيل"},
                {
                    "plan_action": "add_task",
                    "title": "مهمة دخيلة",
                    "description": "",
                    "goal": "",
                    "responsible": "",
                    "department": "",
                    "due_at": "",
                },
                {"plan_action": "remove_goal", "goal_id": goal.pk},
                {"plan_action": "remove_task", "task_id": task.pk},
            ]
            for payload in attempts:
                with self.subTest(state=approval_state, action=payload["plan_action"]):
                    client.post(reverse("reports:plan_action", args=[plan.pk]), payload)
            self.assertEqual(plan.goals.count(), 1)
            self.assertEqual(plan.tasks.count(), 1)

    def test_closed_plan_rejects_new_items(self):
        """Closed means no structural mutation and no new execution link."""
        plan = self._plan(stage=Plan.Stage.CLOSED)
        goal = PlanGoal.objects.create(plan=plan, title="هدف قبل الإغلاق")
        task = self._task(
            plan,
            goal=goal,
            responsible=self.teacher,
            due_at=timezone.now() + timedelta(days=3),
        )
        client = self._enter(self.manager)
        attempts = [
            {"plan_action": "add_goal", "title": "هدف بعد الإغلاق"},
            {
                "plan_action": "add_task",
                "title": "مهمة بعد الإغلاق",
                "description": "",
                "goal": "",
                "responsible": "",
                "department": "",
                "due_at": "",
            },
            {"plan_action": "remove_goal", "goal_id": goal.pk},
            {"plan_action": "remove_task", "task_id": task.pk},
            {"plan_action": "track_task", "task_id": task.pk},
        ]
        for payload in attempts:
            client.post(reverse("reports:plan_action", args=[plan.pk]), payload)
        task.refresh_from_db()
        self.assertEqual(
            (plan.goals.count(), plan.tasks.count(), task.assignment_id),
            (1, 1, None),
        )

    def test_stale_task_instance_cannot_create_a_second_assignment(self):
        """Stale task instances cannot create duplicate assignments."""
        plan = self._plan()
        task = self._task(
            plan,
            responsible=self.teacher,
            due_at=timezone.now() + timedelta(days=3),
        )
        stale = PlanTask.objects.get(pk=task.pk)
        convert_task_to_assignment(task, self.manager)
        with self.assertRaises(PlanError):
            convert_task_to_assignment(stale, self.manager)
        self.assertEqual(Assignment.objects.filter(source=Assignment.Source.PLAN).count(), 1)

    def test_repeated_conversion_post_creates_one_assignment(self):
        plan = self._plan()
        task = self._task(
            plan,
            responsible=self.teacher,
            due_at=timezone.now() + timedelta(days=3),
        )
        client = self._enter(self.manager)
        url = reverse("reports:plan_action", args=[plan.pk])
        payload = {"plan_action": "track_task", "task_id": task.pk}
        client.post(url, payload)
        client.post(url, payload)
        task.refresh_from_db()
        self.assertIsNotNone(task.assignment_id)
        self.assertEqual(Assignment.objects.filter(source=Assignment.Source.PLAN).count(), 1)

    def test_stale_approval_cannot_overwrite_a_newer_state(self):
        plan = self._plan(owner=self.deputy)
        self._task(plan)
        submit(plan, self.deputy, school=self.school_a)
        stale = Plan.objects.get(pk=plan.pk)
        Plan.objects.filter(pk=plan.pk).update(approval_state=ApprovalState.APPROVED)
        with self.assertRaises(ApprovalError):
            approve(stale, self.manager, school=self.school_a)
        self.assertEqual(
            Plan.objects.get(pk=plan.pk).approval_state, ApprovalState.APPROVED
        )

    def test_foreign_goal_and_task_ids_do_not_mutate_the_open_plan(self):
        plan = self._plan(title="الخطة المفتوحة")
        foreign = self._plan(title="الخطة الأخرى")
        goal = PlanGoal.objects.create(plan=foreign, title="هدف أجنبي")
        task = self._task(foreign)
        client = self._enter(self.manager)
        client.post(
            reverse("reports:plan_action", args=[plan.pk]),
            {"plan_action": "remove_goal", "goal_id": goal.pk},
        )
        client.post(
            reverse("reports:plan_action", args=[plan.pk]),
            {"plan_action": "remove_task", "task_id": task.pk},
        )
        self.assertTrue(PlanGoal.objects.filter(pk=goal.pk).exists())
        self.assertTrue(PlanTask.objects.filter(pk=task.pk).exists())

    def test_initiative_post_requires_csrf(self):
        client = self._enter(self.teacher, enforce_csrf=True)
        response = client.post(
            reverse("reports:initiative_list"),
            {"title": "مبادرة", "summary": "فكرة وأثر", "plan": ""},
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Initiative.objects.filter(title="مبادرة").exists())

    def test_plan_action_requires_csrf(self):
        plan = self._plan()
        client = self._enter(self.manager, enforce_csrf=True)
        response = client.post(
            reverse("reports:plan_action", args=[plan.pk]),
            {"plan_action": "add_goal", "title": "هدف بلا CSRF"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(plan.goals.filter(title="هدف بلا CSRF").exists())

    def test_initiative_actions_are_anchored_to_the_active_school(self):
        initiative = Initiative.objects.create(
            school=self.school_a,
            teacher=self.manager,
            title="مبادرة مدرسة ألف",
            summary="مبادرة لا تعبر المدرسة النشطة",
            approval_state=ApprovalState.APPROVED,
        )
        client = self._enter(self.manager, self.school_b)
        response = client.post(
            reverse("reports:initiative_action", args=[initiative.pk]),
            {"initiative_action": "share"},
        )
        self.assertEqual(response.status_code, 404)
        initiative.refresh_from_db()
        self.assertIsNone(initiative.shared_at)

    def test_foreign_assignee_is_rejected_by_the_task_form(self):
        foreign = _user("منسوب مدرسة أخرى", "0500091299")
        SchoolMembership.objects.create(
            school=self.school_b,
            teacher=foreign,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        plan = self._plan()
        client = self._enter(self.manager)
        client.post(
            reverse("reports:plan_action", args=[plan.pk]),
            {
                "plan_action": "add_task",
                "title": "مهمة مصاغة",
                "description": "",
                "goal": "",
                "responsible": foreign.pk,
                "department": "",
                "due_at": "",
            },
        )
        self.assertFalse(plan.tasks.filter(title="مهمة مصاغة").exists())


class PlansPilotPerformanceTests(PlansPilotBase):
    """Acceptance-fixture query counts; these are not production benchmarks."""

    def test_query_profile_is_bounded(self):
        plans = []
        for plan_index in range(8):
            plan = self._plan(title=f"خطة قياس {plan_index}")
            plans.append(plan)
            for task_index in range(4):
                task = self._task(
                    plan,
                    title=f"مهمة {plan_index}-{task_index}",
                    responsible=self.teacher,
                    due_at=timezone.now() + timedelta(days=task_index + 2),
                )
                convert_task_to_assignment(task, self.manager)

        for initiative_index in range(8):
            Initiative.objects.create(
                school=self.school_a,
                teacher=self.teacher,
                title=f"مبادرة قياس {initiative_index}",
                summary="وصف المبادرة وأثرها",
                approval_state=ApprovalState.APPROVED,
            )

        client = self._enter(self.manager)
        with CaptureQueriesContext(connection) as plan_list_queries:
            response = client.get(reverse("reports:plan_list"))
            self.assertEqual(response.status_code, 200)

        detail_plan = plans[0]
        with CaptureQueriesContext(connection) as plan_detail_queries:
            response = client.get(reverse("reports:plan_detail", args=[detail_plan.pk]))
            self.assertEqual(response.status_code, 200)

        detail_plan.approval_state = ApprovalState.SUBMITTED
        detail_plan.save(update_fields=["approval_state"])
        with CaptureQueriesContext(connection) as approval_detail_queries:
            response = client.get(reverse("reports:plan_detail", args=[detail_plan.pk]))
            self.assertEqual(response.status_code, 200)

        with CaptureQueriesContext(connection) as initiative_list_queries:
            response = client.get(reverse("reports:initiative_list"))
            self.assertEqual(response.status_code, 200)

        counts = {
            "plan_list": len(plan_list_queries),
            "plan_detail": len(plan_detail_queries),
            "approval_detail": len(approval_detail_queries),
            "initiative_list": len(initiative_list_queries),
        }
        print(f"PLANS_QUERY_PROFILE={counts}")
        self.assertLessEqual(counts["plan_list"], 40)
        self.assertLessEqual(counts["plan_detail"], 35)
        self.assertLessEqual(counts["initiative_list"], 25)


class PlansPilotPresentationTests(PlansPilotBase):
    def test_core_templates_use_owned_assets_without_embedded_presentation(self):
        template_root = Path(__file__).resolve().parents[1] / "templates" / "reports"
        names = [
            "plan_list.html",
            "plan_create.html",
            "plan_edit.html",
            "plan_detail.html",
            "initiative_list.html",
        ]
        joined = "\n".join((template_root / name).read_text(encoding="utf-8") for name in names)
        theme = (template_root / "_plan_theme.html").read_text(encoding="utf-8")
        self.assertNotIn("<style", joined)
        self.assertNotIn(" style=", joined)
        self.assertNotIn("<script", joined)
        self.assertIn("css/plans.css", theme)
        self.assertIn("js/plans.js", theme)

    def test_plan_list_is_an_operational_table_with_mobile_labels(self):
        self._plan(title="خطة القابلية للاستخدام")
        client = self._enter(self.manager)
        response = client.get(reverse("reports:plan_list"))
        self.assertContains(response, 'class="twq-table plan-table"')
        self.assertContains(response, 'data-label="الإجراء التالي"')
        self.assertContains(response, "فتح مساحة الخطة")
        self.assertEqual(response.content.decode("utf-8").count("<h1"), 1)

    def test_invalid_plan_form_exposes_a_focusable_error_summary(self):
        client = self._enter(self.manager)
        response = client.post(
            reverse("reports:plan_create"),
            {"title": "", "description": "", "academic_year": "", "starts_on": "", "ends_on": ""},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "data-plan-error-summary")
        self.assertContains(response, 'role="alert"')
        self.assertContains(response, 'href="#id_title"')

    def test_plan_detail_exposes_progress_and_assignment_boundary(self):
        plan = self._plan(title="خطة مساحة العمل")
        self._task(plan, responsible=self.teacher, due_at=timezone.now() + timedelta(days=2))
        client = self._enter(self.manager)
        response = client.get(reverse("reports:plan_detail", args=[plan.pk]))
        self.assertContains(response, "مساحة الخطة")
        self.assertContains(response, "المهام والتكليفات")
        self.assertContains(response, "حوّلها إلى تكليف")
        self.assertEqual(response.content.decode("utf-8").count("<h1"), 1)
