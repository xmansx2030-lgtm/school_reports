from datetime import date, timedelta
from pathlib import Path

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.test import Client, SimpleTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from reports import capabilities as caps
from reports.lab_kinds import LabKind
from reports.model_parts.approvals import ApprovalState
from reports.models import (
    Delegation,
    Department,
    LabAsset,
    LabAssetHandover,
    LabExperiment,
    SchoolMembership,
    StaffScope,
    Teacher,
)
from reports.permissions import can_view_lab
from reports.services_approval import available_actions
from reports.services_lab import (
    assets_for_school,
    experiments_for_school,
    lab_kinds_for_user,
    record_handover,
)
from reports.tests.test_lab_technician_journey import LabTestCase, PASSWORD, _school


@override_settings(ALLOWED_HOSTS=["testserver"])
class LabPilotQueryTests(LabTestCase):
    def setUp(self):
        super().setUp()
        self.tech_membership.lab_kind = LabKind.SCIENCE
        self.tech_membership.save(update_fields=["lab_kind"])

        self.assets = []
        for index in range(8):
            asset = LabAsset.objects.create(
                school=self.school,
                lab_kind=LabKind.SCIENCE,
                name=f"صنف مختبر {index + 1}",
                code=f"LAB-{index + 1:03d}",
                category=LabAsset.Category.DEVICE,
                quantity=6,
                condition=(
                    LabAsset.Condition.NEEDS_MAINTENANCE
                    if index == 0
                    else LabAsset.Condition.GOOD
                ),
                location=f"دولاب {index + 1}",
                custodian=self.tech,
                recorded_by=self.tech,
            )
            self.assets.append(asset)
            if index < 3:
                record_handover(
                    asset,
                    direction=LabAssetHandover.Direction.OUT,
                    person=self.teacher,
                    quantity=1,
                    actor=self.tech,
                    note="Fixture قياس",
                )

        self.experiments = []
        for index in range(8):
            experiment = LabExperiment.objects.create(
                school=self.school,
                lab_kind=LabKind.SCIENCE,
                recorder=self.tech,
                requested_by=self.teacher,
                title=f"تجربة قياس {index + 1}",
                experiment_date=date(2026, 9, index + 1),
                subject="علوم",
                class_name="ثاني أ",
                procedure="خطوات تجربة قابلة للتكرار.",
                approval_state=(
                    ApprovalState.SUBMITTED if index < 3 else ApprovalState.DRAFT
                ),
            )
            experiment.assets.add(self.assets[index])
            self.experiments.append(experiment)

    def test_query_budget_is_bounded_for_realistic_lab_fixture(self):
        self._enter(self.tech)

        counts = {}
        routes = {
            "dashboard": reverse("reports:lab_dashboard"),
            "assets": reverse("reports:lab_assets"),
            "asset_detail": reverse(
                "reports:lab_asset_detail", args=[self.assets[0].pk]
            ),
            "experiments": reverse("reports:lab_experiments"),
            "experiment_detail": reverse(
                "reports:lab_experiment_detail", args=[self.experiments[0].pk]
            ),
        }
        for name, url in routes.items():
            with CaptureQueriesContext(connection) as queries:
                response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            counts[name] = len(queries)

        print(
            "LAB_QUERY_COUNTS",
            " ".join(f"{name}={count}" for name, count in counts.items()),
        )

        self.assertLessEqual(counts["dashboard"], 60)
        self.assertLessEqual(counts["assets"], 45)
        self.assertLessEqual(counts["asset_detail"], 45)
        self.assertLessEqual(counts["experiments"], 50)
        self.assertLessEqual(counts["experiment_detail"], 25)

    def test_inventory_quantities_use_annotations_without_per_asset_queries(self):
        rows = list(assets_for_school(self.school, user=self.tech))

        with CaptureQueriesContext(connection) as queries:
            values = [(asset.out_quantity, asset.available_quantity) for asset in rows]

        self.assertEqual(len(values), len(self.assets))
        self.assertEqual(len(queries), 0)


@override_settings(ALLOWED_HOSTS=["testserver"])
class LabPilotSecurityRegressionTests(LabTestCase):
    def setUp(self):
        super().setUp()
        self.tech_membership.lab_kind = LabKind.SCIENCE
        self.tech_membership.save(update_fields=["lab_kind"])

    def test_active_school_blocks_asset_read_and_mutation(self):
        asset = self._asset(lab_kind=LabKind.SCIENCE)
        other_school = _school("مدرسة أخرى", "other-lab-school")
        SchoolMembership.objects.create(
            school=other_school,
            teacher=self.tech,
            role_type=SchoolMembership.RoleType.ADMIN_STAFF,
            job_title=SchoolMembership.JobTitle.LAB_TECH,
            lab_kind=LabKind.SCIENCE,
        )
        self.client.force_login(self.tech)
        session = self.client.session
        session["active_school_id"] = other_school.pk
        session.save()

        detail = self.client.get(reverse("reports:lab_asset_detail", args=[asset.pk]))
        mutation = self.client.post(
            reverse("reports:lab_asset_action", args=[asset.pk]),
            {"lab_action": "condition", "condition": LabAsset.Condition.DAMAGED},
        )

        self.assertEqual(detail.status_code, 404)
        self.assertEqual(mutation.status_code, 404)
        asset.refresh_from_db()
        self.assertEqual(asset.condition, LabAsset.Condition.GOOD)

    def test_cross_lab_asset_and_experiment_posts_do_not_mutate(self):
        foreign_asset = self._asset(
            name="حاسب خارج نطاق العلوم",
            lab_kind=LabKind.COMPUTER,
            condition=LabAsset.Condition.GOOD,
        )
        foreign_experiment = self._experiment(
            lab_kind=LabKind.COMPUTER,
            approval_state=ApprovalState.DRAFT,
        )
        self._enter(self.tech)

        asset_response = self.client.post(
            reverse("reports:lab_asset_action", args=[foreign_asset.pk]),
            {"lab_action": "condition", "condition": LabAsset.Condition.DAMAGED},
        )
        experiment_response = self.client.post(
            reverse("reports:lab_experiment_action", args=[foreign_experiment.pk]),
            {"approval_action": "submit"},
        )

        self.assertEqual(asset_response.status_code, 404)
        self.assertEqual(experiment_response.status_code, 404)
        foreign_asset.refresh_from_db()
        foreign_experiment.refresh_from_db()
        self.assertEqual(foreign_asset.condition, LabAsset.Condition.GOOD)
        self.assertEqual(foreign_experiment.approval_state, ApprovalState.DRAFT)

    def test_foreign_school_recipient_is_rejected_without_handover(self):
        asset = self._asset(lab_kind=LabKind.SCIENCE)
        other_school = _school("مدرسة المستلم الغريب", "foreign-recipient-school")
        foreign_teacher = Teacher.objects.create_user(
            phone="0500099901", name="مستلم من مدرسة أخرى", password=PASSWORD
        )
        SchoolMembership.objects.create(
            school=other_school,
            teacher=foreign_teacher,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        self._enter(self.tech)

        response = self.client.post(
            reverse("reports:lab_asset_action", args=[asset.pk]),
            {
                "lab_action": "handover",
                "direction": LabAssetHandover.Direction.OUT,
                "person": foreign_teacher.pk,
                "quantity": 1,
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(asset.handovers.exists())

    def test_mutation_routes_enforce_csrf(self):
        asset = self._asset(lab_kind=LabKind.SCIENCE)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.tech)
        session = csrf_client.session
        session["active_school_id"] = self.school.pk
        session.save()

        response = csrf_client.post(
            reverse("reports:lab_asset_action", args=[asset.pk]),
            {"lab_action": "condition", "condition": LabAsset.Condition.DAMAGED},
        )

        self.assertEqual(response.status_code, 403)
        asset.refresh_from_db()
        self.assertEqual(asset.condition, LabAsset.Condition.GOOD)


@override_settings(ALLOWED_HOSTS=["testserver"])
class LabDelegationScopeSecurityTests(LabTestCase):
    def setUp(self):
        super().setUp()
        self.tech_membership.lab_kind = LabKind.SCIENCE
        self.tech_membership.save(update_fields=["lab_kind"])
        self.computer_department = Department.objects.create(
            school=self.school,
            name="قسم الحاسب الآلي",
            slug="lab-computer",
        )
        self.science_asset = self._asset(
            name="ميكروسكوب العلوم",
            lab_kind=LabKind.SCIENCE,
            department=self.department,
        )
        self.computer_asset = self._asset(
            name="حاسب المختبر",
            lab_kind=LabKind.COMPUTER,
            department=self.computer_department,
        )
        self.science_experiment = self._experiment(
            title="تجربة العلوم",
            lab_kind=LabKind.SCIENCE,
            department=self.department,
            approval_state=ApprovalState.SUBMITTED,
        )
        self.computer_experiment = self._experiment(
            title="تجربة الحاسب",
            lab_kind=LabKind.COMPUTER,
            department=self.computer_department,
            approval_state=ApprovalState.SUBMITTED,
        )

    def _scope(self, *departments, capabilities=None):
        scope = StaffScope.objects.create(
            membership=self.deputy_membership,
            capabilities=capabilities or [],
        )
        scope.departments.set(departments)
        cache.clear()
        return scope

    def _delegate(
        self,
        *,
        school=None,
        capabilities=None,
        starts_at=None,
        ends_at=None,
    ):
        now = timezone.now()
        delegation = Delegation.objects.create(
            school=school or self.school,
            delegator=self.manager,
            delegate=self.deputy,
            capabilities=capabilities or [caps.MANAGE_LAB],
            starts_at=starts_at or now - timedelta(hours=1),
            ends_at=ends_at or now + timedelta(hours=1),
            reason="تغطية تشغيل المختبر مؤقتاً",
        )
        cache.clear()
        return delegation

    def _delegate_science_only(self, *, recommendation=True):
        capabilities = [caps.RECOMMEND_APPROVAL] if recommendation else []
        self._scope(self.department, capabilities=capabilities)
        return self._delegate()

    def test_delegated_manage_lab_reuses_science_staff_scope_everywhere(self):
        self._delegate_science_only()
        self._enter(self.deputy)

        assets = self.client.get(reverse("reports:lab_assets"))
        experiments = self.client.get(reverse("reports:lab_experiments"))
        asset_print = self.client.get(reverse("reports:lab_assets_print"))
        science_asset = self.client.get(
            reverse("reports:lab_asset_detail", args=[self.science_asset.pk])
        )
        computer_asset = self.client.get(
            reverse("reports:lab_asset_detail", args=[self.computer_asset.pk])
        )
        science_experiment = self.client.get(
            reverse(
                "reports:lab_experiment_detail", args=[self.science_experiment.pk]
            )
        )
        computer_experiment = self.client.get(
            reverse(
                "reports:lab_experiment_detail", args=[self.computer_experiment.pk]
            )
        )
        science_print = self.client.get(
            reverse(
                "reports:lab_experiment_print", args=[self.science_experiment.pk]
            )
        )
        computer_print = self.client.get(
            reverse(
                "reports:lab_experiment_print", args=[self.computer_experiment.pk]
            )
        )

        self.assertEqual(lab_kinds_for_user(self.deputy, self.school), (LabKind.SCIENCE,))
        self.assertContains(assets, self.science_asset.name)
        self.assertNotContains(assets, self.computer_asset.name)
        self.assertContains(experiments, self.science_experiment.title)
        self.assertNotContains(experiments, self.computer_experiment.title)
        self.assertContains(asset_print, self.science_asset.name)
        self.assertNotContains(asset_print, self.computer_asset.name)
        self.assertEqual(science_asset.status_code, 200)
        self.assertEqual(computer_asset.status_code, 404)
        self.assertEqual(science_experiment.status_code, 200)
        self.assertIn("start_review", science_experiment.context["actions"])
        self.assertEqual(computer_experiment.status_code, 404)
        self.assertEqual(science_print.status_code, 200)
        self.assertEqual(computer_print.status_code, 404)

    def test_delegated_science_scope_blocks_all_computer_asset_mutations(self):
        self._delegate_science_only()
        self._enter(self.deputy)
        actions = (
            {"lab_action": "condition", "condition": LabAsset.Condition.DAMAGED},
            {
                "lab_action": "handover",
                "direction": LabAssetHandover.Direction.OUT,
                "person": self.teacher.pk,
                "quantity": 1,
            },
            {
                "lab_action": "handover",
                "direction": LabAssetHandover.Direction.IN,
                "person": self.teacher.pk,
                "quantity": 1,
            },
            {"lab_action": "retire"},
            {"lab_action": "restore"},
        )

        edit = self.client.post(
            reverse("reports:lab_asset_detail", args=[self.computer_asset.pk]),
            {"name": "تعديل غير مسموح"},
        )
        self.assertEqual(edit.status_code, 404)
        for payload in actions:
            with self.subTest(action=payload["lab_action"], direction=payload.get("direction")):
                response = self.client.post(
                    reverse("reports:lab_asset_action", args=[self.computer_asset.pk]),
                    payload,
                )
                # MANAGE_LAB is read/review authority, not inventory recording.
                # The existing action route rejects the missing record capability
                # before resolving any asset, so its established response is a
                # safe redirect rather than an object-scoped 404.
                self.assertEqual(response.status_code, 302)

        self.computer_asset.refresh_from_db()
        self.assertEqual(self.computer_asset.name, "حاسب المختبر")
        self.assertEqual(self.computer_asset.condition, LabAsset.Condition.GOOD)
        self.assertTrue(self.computer_asset.is_active)
        self.assertFalse(self.computer_asset.handovers.exists())

    def test_delegated_science_scope_blocks_computer_experiment_actions(self):
        self._delegate_science_only()
        draft = self._experiment(
            title="مسودة حاسب محمية",
            lab_kind=LabKind.COMPUTER,
            department=self.computer_department,
            approval_state=ApprovalState.DRAFT,
        )
        manager_owned = self._experiment(
            title="تجربة مدير في الحاسب",
            recorder=self.manager,
            lab_kind=LabKind.COMPUTER,
            department=self.computer_department,
            approval_state=ApprovalState.RECOMMENDED,
            reviewed_by=self.deputy,
        )
        self._enter(self.deputy)

        edit = self.client.post(
            reverse("reports:lab_experiment_detail", args=[draft.pk]),
            {
                "title": "تعديل غير مسموح",
                "experiment_date": "2026-09-23",
                "procedure": "خطوات غير مسموحة",
            },
        )
        submit = self.client.post(
            reverse("reports:lab_experiment_action", args=[draft.pk]),
            {"approval_action": "submit"},
        )
        for action in ("start_review", "request_info", "recommend", "return"):
            with self.subTest(action=action):
                response = self.client.post(
                    reverse(
                        "reports:lab_experiment_action",
                        args=[self.computer_experiment.pk],
                    ),
                    {"approval_action": action, "note": "طلب مصاغ يدوياً"},
                )
                self.assertEqual(response.status_code, 404)
        approve = self.client.post(
            reverse("reports:lab_experiment_action", args=[manager_owned.pk]),
            {"approval_action": "approve", "note": "طلب مصاغ يدوياً"},
        )

        self.assertEqual(edit.status_code, 404)
        self.assertEqual(submit.status_code, 404)
        self.assertEqual(approve.status_code, 404)
        draft.refresh_from_db()
        self.computer_experiment.refresh_from_db()
        manager_owned.refresh_from_db()
        self.assertEqual(draft.title, "مسودة حاسب محمية")
        self.assertEqual(draft.approval_state, ApprovalState.DRAFT)
        self.assertEqual(
            self.computer_experiment.approval_state, ApprovalState.SUBMITTED
        )
        self.assertEqual(manager_owned.approval_state, ApprovalState.RECOMMENDED)
        self.assertEqual(
            available_actions(self.computer_experiment, self.deputy, school=self.school),
            [],
        )
        self.assertNotIn(
            "approve",
            available_actions(manager_owned, self.deputy, school=self.school),
        )

    def test_delegated_reviewer_can_finalize_only_manager_work_in_science_scope(self):
        self._delegate_science_only()
        science = self._experiment(
            title="تجربة مدير في العلوم",
            recorder=self.manager,
            lab_kind=LabKind.SCIENCE,
            department=self.department,
            approval_state=ApprovalState.RECOMMENDED,
            reviewed_by=self.deputy,
        )
        self._enter(self.deputy)

        actions = available_actions(science, self.deputy, school=self.school)
        response = self.client.post(
            reverse("reports:lab_experiment_action", args=[science.pk]),
            {"approval_action": "approve", "note": "اعتماد داخل نطاق العلوم"},
        )

        science.refresh_from_db()
        self.assertIn("approve", actions)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(science.approval_state, ApprovalState.APPROVED)

    def test_empty_scope_delegation_exposes_zero_labs_and_no_review_actions(self):
        self._scope(capabilities=[caps.RECOMMEND_APPROVAL])
        self._delegate()
        self._enter(self.deputy)

        assets = self.client.get(reverse("reports:lab_assets"))
        experiments = self.client.get(reverse("reports:lab_experiments"))

        self.assertEqual(lab_kinds_for_user(self.deputy, self.school), ())
        self.assertEqual(assets.context["summary"]["assets_total"], 0)
        self.assertEqual(experiments.context["summary"]["experiments_total"], 0)
        self.assertEqual(assets_for_school(self.school, user=self.deputy).count(), 0)
        self.assertEqual(
            experiments_for_school(self.school, user=self.deputy).count(), 0
        )
        self.assertEqual(
            self.client.get(
                reverse("reports:lab_asset_detail", args=[self.science_asset.pk])
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                reverse(
                    "reports:lab_experiment_detail",
                    args=[self.science_experiment.pk],
                )
            ).status_code,
            404,
        )
        self.assertEqual(
            available_actions(self.science_experiment, self.deputy, school=self.school),
            [],
        )

    def test_delegated_manage_lab_supports_multiple_explicit_lab_scopes(self):
        self._scope(self.department, self.computer_department)
        self._delegate()
        self._enter(self.deputy)

        self.assertEqual(
            set(lab_kinds_for_user(self.deputy, self.school)),
            {LabKind.SCIENCE, LabKind.COMPUTER},
        )
        self.assertEqual(assets_for_school(self.school, user=self.deputy).count(), 2)
        self.assertEqual(
            experiments_for_school(self.school, user=self.deputy).count(), 2
        )
        self.assertEqual(
            self.client.get(
                reverse("reports:lab_asset_detail", args=[self.computer_asset.pk])
            ).status_code,
            200,
        )

    def test_direct_scope_manager_and_technician_contracts_are_unchanged(self):
        scope = self._scope(self.department, capabilities=[caps.MANAGE_LAB])
        self.assertEqual(lab_kinds_for_user(self.deputy, self.school), (LabKind.SCIENCE,))
        self.assertEqual(
            set(lab_kinds_for_user(self.manager, self.school)),
            {LabKind.SCIENCE, LabKind.COMPUTER},
        )
        self.assertEqual(lab_kinds_for_user(self.tech, self.school), (LabKind.SCIENCE,))
        self.assertTrue(can_view_lab(self.deputy, self.school))
        self.assertEqual(scope.capabilities, [caps.MANAGE_LAB])

    def test_wrong_school_expired_and_revoked_delegations_grant_no_lab_scope(self):
        other_school = _school("مدرسة نطاق أخرى", "lab-delegation-other")
        SchoolMembership.objects.create(
            school=other_school,
            teacher=self.deputy,
            role_type=SchoolMembership.RoleType.DEPUTY,
        )
        self._scope(self.department)
        school_a_delegation = self._delegate()

        self.assertFalse(can_view_lab(self.deputy, other_school))
        self.assertEqual(lab_kinds_for_user(self.deputy, other_school), ())

        school_a_delegation.revoke(by=self.manager)
        cache.clear()
        self.assertFalse(can_view_lab(self.deputy, self.school))
        self.assertEqual(lab_kinds_for_user(self.deputy, self.school), ())

        now = timezone.now()
        self._delegate(
            starts_at=now - timedelta(days=2),
            ends_at=now - timedelta(days=1),
        )
        self.assertFalse(can_view_lab(self.deputy, self.school))
        self.assertEqual(lab_kinds_for_user(self.deputy, self.school), ())


class LabPilotFrontendDebtTests(SimpleTestCase):
    core_templates = (
        "_lab_theme.html",
        "_lab_field.html",
        "_lab_asset_form.html",
        "_lab_experiment_form.html",
        "lab_dashboard.html",
        "lab_assets.html",
        "lab_asset_detail.html",
        "lab_experiments.html",
        "lab_experiment_detail.html",
    )

    def test_interactive_templates_have_no_embedded_or_inline_styles(self):
        template_root = Path(settings.BASE_DIR) / "reports" / "templates" / "reports"
        for filename in self.core_templates:
            source = (template_root / filename).read_text(encoding="utf-8")
            with self.subTest(filename=filename):
                self.assertNotIn("<style", source.lower())
                self.assertNotIn("style=", source.lower())

    def test_lab_javascript_is_external_and_does_not_write_styles(self):
        template_root = Path(settings.BASE_DIR) / "reports" / "templates" / "reports"
        theme = (template_root / "_lab_theme.html").read_text(encoding="utf-8")
        javascript = (Path(settings.BASE_DIR) / "static" / "js" / "lab.js").read_text(
            encoding="utf-8"
        )

        self.assertIn('src="{% static \'js/lab.js\'', theme)
        self.assertNotIn(".style", javascript)
        self.assertNotIn('setAttribute("style"', javascript)

    def test_lab_css_uses_tokens_and_logical_properties(self):
        stylesheet = (Path(settings.BASE_DIR) / "static" / "css" / "lab.css").read_text(
            encoding="utf-8"
        )
        lowered = stylesheet.lower()

        self.assertNotRegex(lowered, r"#[0-9a-f]{3,8}\b")
        self.assertNotRegex(lowered, r"\b(?:rgb|rgba|hsl|hsla)\(")
        self.assertNotRegex(
            lowered,
            r"\b(?:margin|padding|border)-(?:left|right)\b|\b(?:left|right)\s*:",
        )
