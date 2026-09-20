# -*- coding: utf-8 -*-
"""فصل مختبر العلوم عن مختبر الحاسب الآلي، بياناتٍ وصلاحياتٍ ورحلةً."""
from __future__ import annotations

from datetime import date
from pathlib import Path

from django.core.cache import cache
from django.contrib.staticfiles import finders
from django.test import TestCase, override_settings
from django.urls import reverse

from reports import capabilities as caps
from reports.forms import TeacherCreateForm
from reports.models import (
    Department,
    DepartmentMembership,
    LabAsset,
    LabExperiment,
    School,
    SchoolMembership,
    SchoolSubscription,
    StaffScope,
    SubscriptionPlan,
    Teacher,
)
from reports.teacher_onboarding import build_preview
from reports.lab_kinds import LabKind


PASSWORD = "Passw0rd!123"


@override_settings(ALLOWED_HOSTS=["testserver"])
class LabDepartmentIsolationTests(TestCase):
    def setUp(self):
        cache.clear()
        self.school = School.objects.create(name="مدرسة المختبرين", code="two-labs")
        plan = SubscriptionPlan.objects.create(
            name="خطة المختبرين", price=0, days_duration=365, max_teachers=20
        )
        SchoolSubscription.objects.create(school=self.school, plan=plan)
        self.science = Department.objects.create(
            school=self.school, name="قسم العلوم", slug="science-lab"
        )
        self.computer = Department.objects.create(
            school=self.school, name="قسم الحاسب الآلي", slug="computer-lab"
        )

        self.manager = self._user("مدير المدرسة", "0500061001")
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )
        self.science_tech = self._lab_tech(
            "محضر مختبر العلوم", "0500061002", self.science, LabKind.SCIENCE
        )
        self.computer_tech = self._lab_tech(
            "محضر مختبر الحاسب", "0500061003", self.computer, LabKind.COMPUTER
        )
        self.deputy = self._user("وكيل الشؤون التعليمية", "0500061004")
        self.deputy_membership = SchoolMembership.objects.create(
            school=self.school,
            teacher=self.deputy,
            role_type=SchoolMembership.RoleType.DEPUTY,
        )

        self.science_asset = LabAsset.objects.create(
            school=self.school,
            department=self.science,
            lab_kind=LabKind.SCIENCE,
            name="ميكروسكوب العلوم",
            quantity=2,
            recorded_by=self.science_tech,
            custodian=self.science_tech,
        )
        self.computer_asset = LabAsset.objects.create(
            school=self.school,
            department=self.computer,
            lab_kind=LabKind.COMPUTER,
            name="حاسب المختبر",
            quantity=8,
            recorded_by=self.computer_tech,
            custodian=self.computer_tech,
        )
        self.science_experiment = LabExperiment.objects.create(
            school=self.school,
            department=self.science,
            lab_kind=LabKind.SCIENCE,
            recorder=self.science_tech,
            title="تجربة علوم",
            experiment_date=date(2026, 8, 27),
            procedure="خطوات تجربة العلوم.",
        )
        self.computer_experiment = LabExperiment.objects.create(
            school=self.school,
            department=self.computer,
            lab_kind=LabKind.COMPUTER,
            recorder=self.computer_tech,
            title="تجربة حاسب",
            experiment_date=date(2026, 8, 27),
            procedure="خطوات تجربة الحاسب.",
        )

    def _user(self, name, phone):
        return Teacher.objects.create_user(
            name=name, phone=phone, password=PASSWORD
        )

    def _lab_tech(self, name, phone, department, lab_kind):
        user = self._user(name, phone)
        SchoolMembership.objects.create(
            school=self.school,
            teacher=user,
            role_type=SchoolMembership.RoleType.ADMIN_STAFF,
            job_title=SchoolMembership.JobTitle.LAB_TECH,
            lab_kind=lab_kind,
        )
        DepartmentMembership.objects.create(
            department=department,
            teacher=user,
            role_type=DepartmentMembership.TEACHER,
        )
        return user

    def _enter(self, user):
        self.client.force_login(user)
        session = self.client.session
        session["active_school_id"] = self.school.pk
        session.save()

    def test_each_technician_sees_only_their_inventory_and_experiments(self):
        self._enter(self.science_tech)

        assets = self.client.get(reverse("reports:lab_assets"))
        experiments = self.client.get(reverse("reports:lab_experiments"))

        self.assertContains(assets, "ميكروسكوب العلوم")
        self.assertNotContains(assets, "حاسب المختبر")
        self.assertContains(experiments, "تجربة علوم")
        self.assertNotContains(experiments, "تجربة حاسب")
        self.assertEqual(assets.context["summary"]["assets_total"], 1)
        self.assertEqual(experiments.context["summary"]["experiments_total"], 1)

    def test_workspace_names_the_technicians_assigned_lab(self):
        self._enter(self.science_tech)

        response = self.client.get(reverse("reports:home"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "قسم العلوم")

    def test_cross_lab_detail_urls_are_not_disclosed(self):
        self._enter(self.science_tech)

        self.assertEqual(
            self.client.get(
                reverse("reports:lab_asset_detail", args=[self.computer_asset.pk])
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                reverse(
                    "reports:lab_experiment_detail",
                    args=[self.computer_experiment.pk],
                )
            ).status_code,
            404,
        )

    def test_new_records_are_bound_to_the_technicians_only_lab(self):
        self._enter(self.science_tech)

        self.client.post(
            reverse("reports:lab_assets"),
            {
                "name": "ميزان حساس",
                "category": LabAsset.Category.DEVICE,
                "quantity": 1,
                "condition": LabAsset.Condition.GOOD,
            },
        )
        self.client.post(
            reverse("reports:lab_experiments"),
            {
                "title": "تجربة الكثافة",
                "experiment_date": "2026-08-27",
                "procedure": "قياس الكتلة والحجم.",
            },
        )

        self.assertEqual(
            LabAsset.objects.get(name="ميزان حساس").lab_kind,
            LabKind.SCIENCE,
        )
        self.assertEqual(
            LabExperiment.objects.get(title="تجربة الكثافة").lab_kind,
            LabKind.SCIENCE,
        )

    def test_a_tampered_asset_choice_cannot_cross_labs(self):
        self._enter(self.science_tech)

        response = self.client.post(
            reverse("reports:lab_experiments"),
            {
                "title": "محاولة ربط عابرة",
                "experiment_date": "2026-08-27",
                "procedure": "طلب مصاغ يدوياً.",
                "assets": [self.computer_asset.pk],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            LabExperiment.objects.filter(title="محاولة ربط عابرة").exists()
        )
        self.assertIn("assets", response.context["form"].errors)

    def test_manager_sees_both_labs(self):
        self._enter(self.manager)

        assets = self.client.get(reverse("reports:lab_assets"))
        experiments = self.client.get(reverse("reports:lab_experiments"))

        self.assertContains(assets, "ميكروسكوب العلوم")
        self.assertContains(assets, "حاسب المختبر")
        self.assertContains(experiments, "تجربة علوم")
        self.assertContains(experiments, "تجربة حاسب")

    def test_inventory_lab_picker_is_independent_from_report_departments(self):
        Department.objects.create(
            school=self.school, name="النشاط الطلابي", slug="student-activity"
        )
        self._enter(self.manager)

        response = self.client.get(reverse("reports:lab_assets"))

        choices = list(response.context["form"].fields["lab_kind"].choices)
        self.assertEqual(
            choices,
            [
                ("", "— اختر المختبر —"),
                (LabKind.SCIENCE, "مختبر العلوم"),
                (LabKind.COMPUTER, "مختبر الحاسب الآلي"),
            ],
        )
        self.assertNotIn("department", response.context["form"].fields)

    def test_lab_picker_is_hidden_until_the_lab_technician_role_is_selected(self):
        self._enter(self.manager)

        add_response = self.client.get(reverse("reports:add_teacher"))
        roles_response = self.client.get(reverse("reports:staff_roles"))

        self.assertContains(
            add_response,
            'id="labDepartmentPanel" hidden aria-hidden="true"',
        )
        self.assertContains(
            add_response,
            "css/staff-create.css",
        )
        self.assertIn(
            ".staff-create-conditional[hidden]",
            Path(finders.find("css/staff-create.css")).read_text(encoding="utf-8"),
        )
        self.assertContains(
            roles_response,
            'id="assignLabKindPanel" hidden aria-hidden="true"',
        )
        self.assertContains(
            roles_response,
            ".rol-field[hidden] { display: none !important; }",
        )
        self.assertContains(roles_response, "syncAssignLabKind()")

    def test_deputy_lab_review_is_limited_to_the_assigned_department(self):
        scope = StaffScope.objects.create(
            membership=self.deputy_membership,
            capabilities=[caps.MANAGE_LAB],
        )
        scope.departments.set([self.science])
        cache.clear()
        self._enter(self.deputy)

        science = self.client.get(
            reverse(
                "reports:lab_experiment_detail", args=[self.science_experiment.pk]
            )
        )
        computer = self.client.get(
            reverse(
                "reports:lab_experiment_detail", args=[self.computer_experiment.pk]
            )
        )

        self.assertEqual(science.status_code, 200)
        self.assertEqual(computer.status_code, 404)

    def test_bulk_onboarding_rejects_an_unassigned_lab_technician(self):
        preview = build_preview(
            [
                {
                    "row_number": 1,
                    "name": "محضر بلا مختبر",
                    "phone": "0500061099",
                    "job_title": SchoolMembership.JobTitle.LAB_TECH,
                    "department": "",
                }
            ],
            self.school,
        )

        self.assertFalse(preview["can_confirm"])
        self.assertIn("يجب ربطه", " ".join(preview["rows"][0]["errors"]))

    def test_individual_onboarding_persists_the_lab_department(self):
        form = TeacherCreateForm(
            data={
                "name": "محضر علوم جديد",
                "phone": "0500061088",
                "national_id": "",
                "job_title": SchoolMembership.JobTitle.LAB_TECH,
                "lab_kind": LabKind.SCIENCE,
                "is_active": "on",
            },
            active_school=self.school,
        )
        self.assertTrue(form.is_valid(), form.errors)

        self._enter(self.manager)
        response = self.client.post(
            reverse("reports:add_teacher"),
            {
                "name": "محضر علوم جديد",
                "phone": "0500061088",
                "national_id": "",
                "job_title": SchoolMembership.JobTitle.LAB_TECH,
                "lab_kind": LabKind.SCIENCE,
                "is_active": "on",
            },
        )

        self.assertEqual(response.status_code, 302)
        user = Teacher.objects.get(phone="0500061088")
        self.assertTrue(
            SchoolMembership.objects.filter(
                teacher=user,
                school=self.school,
                job_title=SchoolMembership.JobTitle.LAB_TECH,
                lab_kind=LabKind.SCIENCE,
            ).exists()
        )
