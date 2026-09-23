"""UI, CRUD, approval, and tenant contracts for Report Types management."""

from datetime import date
from pathlib import Path

from django.conf import settings
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from reports.model_parts.approvals import ApprovalRoute
from reports.models import (
    Department,
    Report,
    ReportType,
    School,
    SchoolMembership,
    SchoolSubscription,
    SubscriptionPlan,
    Teacher,
)


@override_settings(ALLOWED_HOSTS=["testserver"], RATELIMIT_ENABLE=False)
class ReportTypesManagementUITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = School.objects.create(name="مدرسة أنواع التقارير", code="types-ui")
        cls.other_school = School.objects.create(name="مدرسة أخرى", code="types-other")
        plan = SubscriptionPlan.objects.create(
            name="خطة اختبار الأنواع", price=0, days_duration=30, max_teachers=0
        )
        SchoolSubscription.objects.create(school=cls.school, plan=plan)
        SchoolSubscription.objects.create(school=cls.other_school, plan=plan)
        cls.manager = Teacher.objects.create_user(
            phone="500940001", name="مدير الأنواع", password="test-pass", is_staff=True
        )
        SchoolMembership.objects.create(
            school=cls.school,
            teacher=cls.manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )
        cls.employee = Teacher.objects.create_user(
            phone="500940002", name="منسوب المدرسة", password="test-pass"
        )
        SchoolMembership.objects.create(
            school=cls.school,
            teacher=cls.employee,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        cls.owner = Teacher.objects.create_superuser(
            phone="500940003", name="مالك المنصة", password="test-pass"
        )
        cls.department = Department.objects.create(
            school=cls.school, name="قسم العلوم", slug="science-types"
        )
        cls.other_department = Department.objects.create(
            school=cls.other_school, name="قسم خارجي", slug="other-types"
        )

    def _activate(self, user, school=None, client=None):
        client = client or self.client
        client.force_login(user)
        session = client.session
        session["active_school_id"] = (school or self.school).pk
        session.save()
        return client

    def _payload(self, name="تقرير الإنجاز", **changes):
        return {
            "name": name,
            "description": "وصف تشغيلي مختصر",
            "approval_route": ApprovalRoute.DIRECT,
            "departments": [],
            "order": "2",
            "is_active": "on",
            **changes,
        }

    def test_list_uses_v1_workspace_and_school_scoped_data(self):
        report_type = ReportType.objects.create(
            school=self.school,
            code="science-progress",
            name="تقرير تقدم العلوم",
            description="متابعة تقدم الخطة",
            approval_route=ApprovalRoute.VIA_DEPUTY,
            order=3,
        )
        report_type.departments.add(self.department)
        ReportType.objects.create(
            school=self.other_school, code="private-type", name="نوع مدرسة أخرى"
        )
        Report.objects.create(
            school=self.school,
            teacher=self.employee,
            title="تقرير تجريبي",
            report_date=date(2026, 9, 21),
            category=report_type,
        )
        self._activate(self.manager)

        response = self.client.get(reverse("reports:reporttypes_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.school.name)
        self.assertContains(response, report_type.name)
        self.assertContains(response, report_type.description)
        self.assertContains(response, self.department.name)
        self.assertContains(response, "عبر الوكيل ثم مدير المدرسة")
        self.assertContains(response, "<strong>1</strong> تقرير", html=True)
        self.assertNotContains(response, "نوع مدرسة أخرى")
        self.assertContains(response, "css/report-types-management.css")
        self.assertContains(response, 'class="twq-table report-types-table"')
        template = (
            Path(settings.BASE_DIR)
            / "reports"
            / "templates"
            / "reports"
            / "reporttypes_list.html"
        ).read_text(encoding="utf-8")
        self.assertNotIn("<style", template)
    def test_create_allows_duplicate_names_and_generates_unique_codes(self):
        self._activate(self.manager)
        missing = self.client.post(
            reverse("reports:reporttype_create"), self._payload(name="")
        )
        self.assertEqual(missing.status_code, 200)
        self.assertIn("name", missing.context["form"].errors)
        self.assertContains(missing, 'id="reportTypeFormErrors"')
        self.assertContains(missing, 'aria-invalid="true"')

        first = self.client.post(
            reverse("reports:reporttype_create"), self._payload(), follow=False
        )
        second = self.client.post(
            reverse("reports:reporttype_create"), self._payload(), follow=False
        )
        self.assertRedirects(
            first, reverse("reports:reporttypes_list"), fetch_redirect_response=False
        )
        self.assertRedirects(
            second, reverse("reports:reporttypes_list"), fetch_redirect_response=False
        )
        created = list(
            ReportType.objects.filter(school=self.school).order_by("pk")
        )
        self.assertEqual(len(created), 2)
        self.assertEqual(created[0].name, created[1].name)
        self.assertNotEqual(created[0].code, created[1].code)

    def test_deputy_route_requires_own_school_department(self):
        self._activate(self.manager)
        missing_department = self.client.post(
            reverse("reports:reporttype_create"),
            self._payload(approval_route=ApprovalRoute.VIA_DEPUTY),
        )
        foreign_department = self.client.post(
            reverse("reports:reporttype_create"),
            self._payload(
                name="نوع بقسم خارجي",
                approval_route=ApprovalRoute.VIA_DEPUTY,
                departments=[str(self.other_department.pk)],
            ),
        )

        self.assertEqual(missing_department.status_code, 200)
        self.assertIn("departments", missing_department.context["form"].errors)
        self.assertContains(missing_department, "اختر قسمًا واحدًا على الأقل")
        self.assertEqual(foreign_department.status_code, 200)
        self.assertIn("departments", foreign_department.context["form"].errors)
        self.assertFalse(
            ReportType.objects.filter(school=self.school, name="نوع بقسم خارجي").exists()
        )

    def test_edit_changes_order_route_departments_and_active_state(self):
        report_type = ReportType.objects.create(
            school=self.school, code="editable-type", name="نوع قابل للتعديل"
        )
        self._activate(self.manager)

        response = self.client.post(
            reverse("reports:reporttype_update", args=[report_type.pk]),
            self._payload(
                name="نوع محدث",
                approval_route=ApprovalRoute.DEPUTY_FINAL,
                departments=[str(self.department.pk)],
                order="7",
                is_active="",
            ),
        )

        self.assertRedirects(
            response, reverse("reports:reporttypes_list"), fetch_redirect_response=False
        )
        report_type.refresh_from_db()
        self.assertEqual(report_type.name, "نوع محدث")
        self.assertEqual(report_type.order, 7)
        self.assertEqual(report_type.approval_route, ApprovalRoute.DEPUTY_FINAL)
        self.assertFalse(report_type.is_active)
        self.assertEqual(list(report_type.departments.all()), [self.department])

    def test_cross_school_edit_delete_are_rejected_for_manager(self):
        foreign = ReportType.objects.create(
            school=self.other_school, code="foreign-type", name="نوع خارجي"
        )
        self._activate(self.manager)

        edit = self.client.post(
            reverse("reports:reporttype_update", args=[foreign.pk]),
            self._payload(name="تعديل مزور"),
        )
        delete = self.client.post(
            reverse("reports:reporttype_delete", args=[foreign.pk])
        )

        self.assertEqual(edit.status_code, 404)
        self.assertEqual(delete.status_code, 404)
        foreign.refresh_from_db()
        self.assertEqual(foreign.name, "نوع خارجي")

    def test_delete_unused_and_protect_used_report_type(self):
        unused = ReportType.objects.create(
            school=self.school, code="unused-type", name="نوع غير مستخدم"
        )
        used = ReportType.objects.create(
            school=self.school, code="used-type", name="نوع مستخدم"
        )
        report = Report.objects.create(
            school=self.school,
            teacher=self.employee,
            title="سجل تاريخي محفوظ",
            report_date=date(2026, 9, 21),
            category=used,
        )
        self._activate(self.manager)

        removed = self.client.post(
            reverse("reports:reporttype_delete", args=[unused.pk]), follow=False
        )
        blocked = self.client.post(
            reverse("reports:reporttype_delete", args=[used.pk]), follow=True
        )

        self.assertRedirects(
            removed, reverse("reports:reporttypes_list"), fetch_redirect_response=False
        )
        self.assertFalse(ReportType.objects.filter(pk=unused.pk).exists())
        self.assertTrue(ReportType.objects.filter(pk=used.pk).exists())
        self.assertTrue(Report.objects.filter(pk=report.pk, category=used).exists())
        self.assertContains(blocked, "يمكنك تعطيله بدلًا من الحذف")

    def test_employee_denied_and_owner_list_uses_selected_school(self):
        own = ReportType.objects.create(
            school=self.school, code="own-type", name="نوع المدرسة الحالية"
        )
        other = ReportType.objects.create(
            school=self.other_school, code="owner-type", name="نوع المدرسة المختارة"
        )
        self._activate(self.employee)
        denied = self.client.get(reverse("reports:reporttypes_list"))
        self.assertNotEqual(denied.status_code, 200)

        self._activate(self.owner, self.other_school)
        response = self.client.get(reverse("reports:reporttypes_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, other.name)
        self.assertNotContains(response, own.name)

    def test_state_changes_require_csrf(self):
        report_type = ReportType.objects.create(
            school=self.school, code="csrf-type", name="نوع CSRF"
        )
        client = Client(enforce_csrf_checks=True)
        self._activate(self.manager, client=client)

        create = client.post(reverse("reports:reporttype_create"), self._payload())
        delete = client.post(
            reverse("reports:reporttype_delete", args=[report_type.pk])
        )

        self.assertEqual(create.status_code, 403)
        self.assertEqual(delete.status_code, 403)
        self.assertTrue(ReportType.objects.filter(pk=report_type.pk).exists())

    def test_form_template_has_no_inline_style_and_keeps_semantics(self):
        self._activate(self.manager)
        response = self.client.get(reverse("reports:reporttype_create"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "هذا الإعداد خاص بنوع التقرير نفسه")
        self.assertContains(response, '<fieldset class="report-types-form__fieldset')
        self.assertContains(response, 'name="approval_route"')
        template = (
            Path(settings.BASE_DIR)
            / "reports"
            / "templates"
            / "reports"
            / "reporttype_form.html"
        ).read_text(encoding="utf-8")
        self.assertNotIn("<style", template)
