from __future__ import annotations

from datetime import date
from pathlib import Path

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from reports import capabilities as caps
from reports.model_parts.approvals import ApprovalRoute, ApprovalState, ApprovalTransition
from reports.models import (
    Department,
    Report,
    ReportEvidence,
    ReportType,
    School,
    SchoolMembership,
    SchoolSubscription,
    StaffScope,
    SubscriptionPlan,
    Teacher,
)
from reports.services_approval import approve, start_review, submit


def _user(name: str, phone: str) -> Teacher:
    return Teacher.objects.create_user(phone=phone, name=name, password="Passw0rd!123")


def _school(name: str, code: str) -> School:
    plan = SubscriptionPlan.objects.create(
        name=f"باقة {code}", price=0, days_duration=365, max_teachers=0
    )
    school = School.objects.create(name=name, code=code, report_approval_enabled=True)
    SchoolSubscription.objects.create(school=school, plan=plan)
    return school


@override_settings(ALLOWED_HOSTS=["testserver"])
class ReportApprovalPresentationTests(TestCase):
    def setUp(self):
        self.school = _school("مدرسة واجهة الاعتماد", "approval-ui")
        self.manager = _user("مدير المدرسة", "0500071001")
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )
        self.department = Department.objects.create(
            school=self.school, name="القسم التعليمي", slug="approval-ui-dept"
        )
        self.category = ReportType.objects.create(
            school=self.school,
            code="approval-ui-report",
            name="تقرير الواجهة",
            approval_route=ApprovalRoute.VIA_DEPUTY,
        )
        self.category.departments.add(self.department)

        self.reviewer = _user("مراجع القسم", "0500071002")
        membership = SchoolMembership.objects.create(
            school=self.school,
            teacher=self.reviewer,
            role_type=SchoolMembership.RoleType.ADMIN_STAFF,
        )
        scope = StaffScope.objects.create(
            membership=membership,
            capabilities=[caps.REVIEW_REPORTS, caps.RECOMMEND_APPROVAL],
        )
        scope.departments.add(self.department)

        self.author = _user("معد التقرير", "0500071003")
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.author,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        self.report = Report.objects.create(
            school=self.school,
            teacher=self.author,
            title="تقرير رحلة القراءة",
            report_date=date(2026, 9, 21),
            category=self.category,
            idea="تنفيذ مبادرة قراءة مدرسية.",
            goal="رفع معدل القراءة الحرة.",
            implementation_method="جلسات قراءة أسبوعية.",
            results="مشاركة الطلاب في خمس جلسات.",
        )
        ReportEvidence.objects.create(
            report=self.report,
            image="reports/evidence/approval-example.jpg",
            description="شاهد من جلسة القراءة",
            order=1,
            show_in_print=True,
        )
        submit(self.report, self.author)

    def _enter(self, user: Teacher, *, client: Client | None = None) -> Client:
        client = client or self.client
        client.force_login(user)
        session = client.session
        session["active_school_id"] = self.school.pk
        session.save()
        return client

    def test_inbox_is_an_operational_queue_with_real_state_filter(self):
        self._enter(self.manager)

        response = self.client.get(reverse("reports:approval_inbox"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "approval-queue-table")
        self.assertContains(response, "دورك الآن")
        self.assertContains(response, "تقرير رحلة القراءة")
        self.assertContains(response, "?state=submitted")
        self.assertNotContains(response, 'role="search"')

    def test_department_officer_queue_is_scoped_and_actionable(self):
        self._enter(self.reviewer)

        response = self.client.get(reverse("reports:approval_inbox"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "تقرير رحلة القراءة")
        self.assertContains(response, "توصية")
        self.assertContains(response, "مراجعة ضمن النطاق")

    def test_detail_presents_route_content_evidence_and_native_actions(self):
        self._enter(self.reviewer)

        response = self.client.get(
            reverse("reports:approval_detail", args=[self.report.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "عبر الوكيل ثم مدير المدرسة")
        self.assertContains(response, self.department.name)
        self.assertContains(response, "شاهد من جلسة القراءة")
        self.assertContains(response, "آلية التنفيذ")
        self.assertContains(response, 'name="approval_action" value="start_review"')
        self.assertContains(response, "static/js/report-approval.js")

    def test_empty_state_distinguishes_filtered_results(self):
        approve(self.report, self.manager)
        self._enter(self.manager)

        response = self.client.get(
            reverse("reports:approval_inbox"), {"state": ApprovalState.SUBMITTED}
        )

        self.assertContains(response, "لا توجد نتائج بهذه الحالة")
        self.assertContains(response, "عرض جميع الحالات")

    def test_external_next_never_redirects_off_site(self):
        self._enter(self.manager)

        response = self.client.post(
            reverse("reports:approval_action", args=[self.report.pk]),
            {"approval_action": "approve", "next": "https://example.com/steal"},
        )

        self.assertRedirects(
            response,
            reverse("reports:approval_detail", args=[self.report.pk]),
            fetch_redirect_response=False,
        )

    def test_repeated_stale_action_does_not_create_a_second_transition(self):
        self._enter(self.manager)
        action_url = reverse("reports:approval_action", args=[self.report.pk])

        self.client.post(action_url, {"approval_action": "approve"})
        response = self.client.post(action_url, {"approval_action": "approve"})

        self.report.refresh_from_db()
        approvals = ApprovalTransition.objects.filter(
            object_id=self.report.pk,
            action=ApprovalTransition.Action.APPROVE,
        ).count()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.report.approval_state, ApprovalState.APPROVED)
        self.assertEqual(approvals, 1)

    def test_approval_mutation_requires_csrf(self):
        csrf_client = Client(enforce_csrf_checks=True)
        self._enter(self.manager, client=csrf_client)

        response = csrf_client.post(
            reverse("reports:approval_action", args=[self.report.pk]),
            {"approval_action": "approve"},
        )

        self.assertEqual(response.status_code, 403)
        self.report.refresh_from_db()
        self.assertEqual(self.report.approval_state, ApprovalState.SUBMITTED)

    def test_platform_owner_needs_school_role_semantics_to_act(self):
        owner = Teacher.objects.create_superuser(
            phone="0500071004", name="مالك المنصة", password="Passw0rd!123"
        )
        self._enter(owner)

        response = self.client.get(reverse("reports:approval_inbox"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "لا توجد أعمال تنتظر قرارك")
        self.assertNotContains(response, "تقرير رحلة القراءة")

    def test_approval_templates_own_no_embedded_css_or_inline_behavior(self):
        templates_dir = Path(__file__).resolve().parents[1] / "templates" / "reports"
        inbox = (templates_dir / "approval_inbox.html").read_text(encoding="utf-8")
        detail = (templates_dir / "approval_detail.html").read_text(encoding="utf-8")

        for source in (inbox, detail):
            self.assertNotIn("<style", source)
            self.assertNotIn("style=", source)
        self.assertNotIn("<script nonce=", detail)
        self.assertIn("report-approval.css", inbox)
        self.assertIn("report-approval.css", detail)

    def test_timeline_stays_visible_after_review_starts(self):
        start_review(self.report, self.reviewer)
        self._enter(self.manager)

        response = self.client.get(
            reverse("reports:approval_detail", args=[self.report.pk])
        )

        self.assertContains(response, "تاريخ الاعتماد")
        self.assertContains(response, "بدء المراجعة")
