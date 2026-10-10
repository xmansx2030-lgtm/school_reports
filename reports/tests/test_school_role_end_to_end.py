"""School staff journeys through the real HTTP entry points and persistence.

These scenarios deliberately join provisioning, login, execution, review and
receipt instead of testing each view in isolation. All uploads use temporary
storage, and every ordinary account signs in using its real credentials.
"""
from __future__ import annotations

import tempfile
from datetime import timedelta

from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from reports import capabilities as caps
from reports.model_parts.approvals import ApprovalRoute, ApprovalState, ApprovalTransition
from reports.models import (
    Assignment,
    AssignmentTarget,
    CircularDraft,
    Department,
    DepartmentMembership,
    LabAsset,
    LabAssetHandover,
    LabExperiment,
    NotificationRecipient,
    Report,
    ReportType,
    School,
    SchoolMembership,
    SchoolSubscription,
    StaffScope,
    SubscriptionPlan,
    Teacher,
)
from reports.tests.test_circular_deadline import drawn_signature_data


PASSWORD = "School-journey-safe-2026"


@override_settings(ALLOWED_HOSTS=["testserver"], RATELIMIT_ENABLE=False)
class SchoolRoleEndToEndTests(TestCase):
    def setUp(self):
        cache.clear()
        media = tempfile.TemporaryDirectory()
        self.addCleanup(media.cleanup)
        media_settings = override_settings(MEDIA_ROOT=media.name)
        media_settings.enable()
        self.addCleanup(media_settings.disable)

        self.school = School.objects.create(
            name="مدرسة رحلة الأدوار المتكاملة",
            code="school-role-e2e",
            report_approval_enabled=True,
        )
        self.plan = SubscriptionPlan.objects.create(
            name="باقة اختبار الرحلة", price=0, days_duration=365, max_teachers=0
        )
        SchoolSubscription.objects.create(school=self.school, plan=self.plan)
        self.manager = Teacher.objects.create_user(
            phone="0556100000", name="مدير رحلة المدرسة", password=PASSWORD,
            email="manager-journey@example.com",
        )
        SchoolMembership.objects.create(
            school=self.school, teacher=self.manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )
        self.department = Department.objects.create(
            school=self.school, name="قسم الرحلة التعليمية", slug="journey-education"
        )
        self.category = ReportType.objects.create(
            school=self.school, code="role-journey-report", name="تقرير رحلة الأدوار",
            approval_route=ApprovalRoute.VIA_DEPUTY,
        )
        self.category.departments.add(self.department)
        self.manager_client = self._login(self.manager)

    def _login(self, user, password=PASSWORD):
        client = Client(HTTP_ACCEPT="text/html")
        response = client.post(
            reverse("reports:login"), {"identifier": user.phone, "password": password}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(client.session.get("_auth_user_id"), str(user.pk))
        self.assertEqual(client.session.get("active_school_id"), self.school.pk)
        return client

    def _staff(self, code, phone, *, activate=True):
        payload = {
            "name": f"منسوب الرحلة {code}", "phone": phone,
            "job_title": code, "is_active": "on",
        }
        if code == SchoolMembership.JobTitle.LAB_TECH:
            payload["lab_kind"] = "science"
        created = self.manager_client.post(reverse("reports:add_teacher"), payload)
        self.assertEqual(created.status_code, 302, created.content[:500])
        user = Teacher.objects.get(phone=phone)
        self.assertTrue(user.check_password(phone))
        membership = SchoolMembership.objects.get(teacher=user, school=self.school)
        expected_role = "admin_staff" if code == "lab_tech" else code
        self.assertEqual(membership.role_type, expected_role)
        DepartmentMembership.objects.create(department=self.department, teacher=user)
        client = self._login(user, phone)
        self.assertTrue(client.session.get("force_password_change_required"))
        if activate:
            response = client.post(reverse("reports:my_profile"), {
                "update_password": "1", "pwd-email": f"journey-{phone}@example.com",
                "pwd-old_password": phone, "pwd-new_password1": PASSWORD,
                "pwd-new_password2": PASSWORD,
            })
            self.assertEqual(response.status_code, 302)
            user.refresh_from_db()
            self.assertTrue(user.check_password(PASSWORD))
            self.assertEqual(user.email, f"journey-{phone}@example.com")
            profile = client.get(reverse("reports:my_profile"))
            self.assertFalse(profile.context["force_password_change"])
        return user, membership, client

    def _grant(self, membership, *codes):
        scope, _ = StaffScope.objects.get_or_create(membership=membership)
        scope.capabilities = list(codes)
        scope.save()
        scope.departments.set([self.department])
        cache.clear()
        return scope

    def _report(self, user, client, *, title="تقرير التنفيذ المتكامل"):
        response = client.post(reverse("reports:add_report"), {
            "section_selection_enabled": "1", "title": title,
            "report_date": timezone.localdate().isoformat(),
            "category": self.category.code, "show_details": "on",
            "idea": "تنفيذ العمل وتوثيق نتائجه عبر رحلة فعلية من الواجهة.",
            "show_beneficiaries": "on", "beneficiaries_count": "12",
            "evidence-TOTAL_FORMS": "0", "evidence-INITIAL_FORMS": "0",
            "evidence-MIN_NUM_FORMS": "0", "evidence-MAX_NUM_FORMS": "8",
            # The server must ignore crafted owner/tenant values.
            "teacher": self.manager.pk, "teacher_name": "اسم مصاغ",
            "school": "999999",
        })
        self.assertEqual(response.status_code, 302,
                         getattr(response.context and response.context.get("form"), "errors", ""))
        report = Report.objects.get(teacher=user, title=title)
        self.assertEqual(report.school_id, self.school.pk)
        self.assertEqual(report.teacher_name, user.name)
        self.assertEqual(report.approval_state, ApprovalState.DRAFT)
        response = client.post(reverse("reports:approval_action", args=[report.pk]),
                               {"approval_action": "submit"}, follow=True)
        self.assertEqual(response.status_code, 200)
        report.refresh_from_db()
        self.assertEqual(report.approval_state, ApprovalState.SUBMITTED)
        return report

    def test_manager_login_and_every_provisioned_role_finishes_first_login(self):
        self.assertEqual(self.manager_client.get(reverse("reports:admin_dashboard")).status_code, 200)
        for index, code in enumerate(("teacher", "deputy", "lab_tech", "admin_staff"), 1):
            with self.subTest(role=code):
                user, membership, client = self._staff(code, f"055610001{index}")
                home = client.get(reverse("reports:home"))
                self.assertEqual(home.status_code, 200)
                self.assertEqual(client.session["active_school_id"], self.school.pk)
                # A fresh login must use the new credentials and keep role landing.
                fresh = self._login(user)
                self.assertFalse(fresh.session.get("force_password_change_required", False))
                self.assertEqual(fresh.get(reverse("reports:home")).status_code, 200)
                self.assertTrue(membership.is_active)

    def test_first_login_cannot_enter_work_or_change_phone_until_account_is_secured(self):
        for index, code in enumerate(("teacher", "deputy", "lab_tech", "admin_staff"), 1):
            with self.subTest(role=code):
                user, _, client = self._staff(code, f"055610002{index}", activate=False)
                blocked = client.get(reverse("reports:add_report"))
                self.assertRedirects(blocked, reverse("reports:my_profile"),
                                     fetch_redirect_response=False)
                incomplete = client.post(reverse("reports:my_profile"), {
                    "update_password": "1", "pwd-email": "",
                    "pwd-old_password": user.phone, "pwd-new_password1": PASSWORD,
                    "pwd-new_password2": PASSWORD,
                })
                self.assertEqual(incomplete.status_code, 200)
                self.assertIn("email", incomplete.context["pwd_form"].errors)
                client.post(reverse("reports:my_profile"), {
                    "update_phone": "1", "phone-phone": "0556199999"
                })
                user.refresh_from_db()
                self.assertEqual(user.phone, f"055610002{index}")
                self.assertTrue(user.check_password(user.phone))

    def test_every_staff_workspace_action_reaches_an_authorized_screen(self):
        for index, code in enumerate(("deputy", "admin_staff", "lab_tech"), 1):
            with self.subTest(role=code):
                _, membership, client = self._staff(code, f"055610011{index}")
                self._grant(membership, *sorted(caps.AVAILABLE_CODES))
                home = client.get(reverse("reports:home"))
                self.assertEqual(home.status_code, 200)
                actions = []
                for key in ("deputy_workspace_actions", "admin_staff_core_actions",
                            "admin_staff_workspace_actions"):
                    actions.extend(home.context[key])
                self.assertTrue(actions)
                for action in actions:
                    with self.subTest(role=code, destination=action["url"]):
                        response = client.get(action["url"])
                        self.assertEqual(response.status_code, 200,
                                         f"{code}: {action['label']} -> {action['url']}")

    def test_teacher_submission_deputy_recommendation_and_manager_final_decision(self):
        teacher, _, teacher_client = self._staff("teacher", "0556100031")
        deputy, membership, deputy_client = self._staff("deputy", "0556100032")
        self._grant(membership, caps.REVIEW_REPORTS, caps.RECOMMEND_APPROVAL)
        report = self._report(teacher, teacher_client)
        inbox = deputy_client.get(reverse("reports:approval_inbox"))
        self.assertContains(inbox, report.title)
        recommended = deputy_client.post(reverse("reports:approval_action", args=[report.pk]),
                                         {"approval_action": "recommend", "note": "تمت المراجعة"}, follow=True)
        self.assertEqual(recommended.status_code, 200)
        report.refresh_from_db()
        self.assertEqual(report.approval_state, ApprovalState.RECOMMENDED)
        self.assertEqual(report.reviewed_by_id, deputy.pk)
        approved = self.manager_client.post(reverse("reports:approval_action", args=[report.pk]),
                                            {"approval_action": "approve"}, follow=True)
        self.assertEqual(approved.status_code, 200)
        report.refresh_from_db()
        self.assertEqual(report.approval_state, ApprovalState.APPROVED)
        self.assertEqual(report.decided_by_id, self.manager.pk)
        self.assertNotContains(self.manager_client.get(reverse("reports:approval_inbox")), report.title)
        timeline = ApprovalTransition.objects.filter(
            content_type=ContentType.objects.get_for_model(Report), object_id=report.pk
        ).order_by("pk")
        self.assertEqual(list(timeline.values_list("action", flat=True)), ["submit", "recommend", "approve"])

    def test_deputy_can_read_result_after_returning_or_requesting_information(self):
        teacher, _, teacher_client = self._staff("teacher", "0556100041")
        _, membership, deputy_client = self._staff("deputy", "0556100042")
        self._grant(membership, caps.REVIEW_REPORTS)
        for action, state in (("return", ApprovalState.RETURNED),
                              ("request_info", ApprovalState.NEEDS_INFO)):
            with self.subTest(action=action):
                report = self._report(teacher, teacher_client, title=f"تقرير القرار {action}")
                result = deputy_client.post(reverse("reports:approval_action", args=[report.pk]),
                                             {"approval_action": action, "note": "يرجى استكمال الشواهد"}, follow=True)
                self.assertEqual(result.status_code, 200)
                report.refresh_from_db()
                self.assertEqual(report.approval_state, state)
                self.assertContains(result, "يرجى استكمال الشواهد")
                self.assertNotContains(deputy_client.get(reverse("reports:approval_inbox")), report.title)
                self.assertEqual(teacher_client.get(reverse("reports:edit_my_report", args=[report.pk])).status_code, 200)

    def test_deputy_final_approval_returns_a_readable_receipt(self):
        self.category.approval_route = ApprovalRoute.DEPUTY_FINAL
        self.category.save(update_fields=["approval_route"])
        teacher, _, teacher_client = self._staff("teacher", "0556100051")
        deputy, membership, deputy_client = self._staff("deputy", "0556100052")
        self._grant(membership, caps.REVIEW_REPORTS, caps.RECOMMEND_APPROVAL)
        report = self._report(teacher, teacher_client)
        result = deputy_client.post(reverse("reports:approval_action", args=[report.pk]),
                                     {"approval_action": "approve"}, follow=True)
        self.assertEqual(result.status_code, 200)
        report.refresh_from_db()
        self.assertEqual(report.approval_state, ApprovalState.APPROVED)
        self.assertEqual(report.decided_by_id, deputy.pk)
        self.assertEqual(result.context["actions"], [])
        # Final records stay locked for their author.
        rejected = teacher_client.post(reverse("reports:delete_my_report", args=[report.pk]))
        self.assertEqual(rejected.status_code, 302)
        self.assertTrue(Report.objects.filter(pk=report.pk).exists())

    def test_completed_review_still_requires_current_scope_and_never_exposes_a_draft(self):
        teacher, _, teacher_client = self._staff("teacher", "0556100061")
        _, membership, deputy_client = self._staff("deputy", "0556100062")
        scope = self._grant(membership, caps.REVIEW_REPORTS, caps.RECOMMEND_APPROVAL)
        report = self._report(teacher, teacher_client)
        deputy_client.post(reverse("reports:approval_action", args=[report.pk]),
                           {"approval_action": "start_review"})
        self.manager_client.post(reverse("reports:approval_action", args=[report.pk]),
                                 {"approval_action": "approve"})
        url = reverse("reports:approval_detail", args=[report.pk])
        self.assertEqual(deputy_client.get(url).status_code, 200)
        draft = Report.objects.create(school=self.school, teacher=teacher, category=self.category,
                                     title="مسودة لم تُرسل", report_date=timezone.localdate())
        self.assertEqual(deputy_client.get(reverse("reports:approval_detail", args=[draft.pk])).status_code, 404)
        unrelated_final = Report.objects.create(
            school=self.school, teacher=teacher, category=self.category,
            title="تقرير لم يراجعه الوكيل", report_date=timezone.localdate(),
            approval_state=ApprovalState.APPROVED, decided_by=self.manager,
        )
        self.assertEqual(deputy_client.get(
            reverse("reports:approval_detail", args=[unrelated_final.pk])
        ).status_code, 404)
        scope.departments.clear()
        cache.clear()
        self.assertEqual(deputy_client.get(url).status_code, 404)
        scope.departments.add(self.department)
        scope.capabilities = []
        scope.save()
        cache.clear()
        self.assertEqual(deputy_client.get(url).status_code, 404)

    def test_admin_assignment_is_issued_accepted_completed_and_approved(self):
        staff, _, client = self._staff("admin_staff", "0556100071")
        due = timezone.localtime() + timedelta(days=3)
        issued = self.manager_client.post(reverse("reports:assignment_create"), {
            "title": "تنفيذ تجهيز ملف المدرسة", "department": self.department.pk,
            "priority": Assignment.Priority.NORMAL, "due_at": due.strftime("%Y-%m-%dT%H:%M"),
            "min_evidence_count": 0, "assignees": [staff.pk],
        })
        self.assertEqual(issued.status_code, 302)
        assignment = Assignment.objects.get(title="تنفيذ تجهيز ملف المدرسة")
        target = AssignmentTarget.objects.get(assignment=assignment, assignee=staff)
        self.assertContains(client.get(reverse("reports:my_assignments")), assignment.title)
        action_url = reverse("reports:assignment_target_action", args=[target.pk])
        client.post(action_url, {"target_action": "accept"})
        client.post(action_url, {"target_action": "progress", "percent": 100, "note": "اكتمل التجهيز"})
        approval_url = reverse("reports:assignment_approval_action", args=[target.pk])
        submitted = client.post(approval_url, {"approval_action": "submit"}, follow=True)
        self.assertEqual(submitted.status_code, 200)
        target.refresh_from_db()
        self.assertEqual(target.approval_state, ApprovalState.SUBMITTED)
        final = self.manager_client.post(approval_url, {"approval_action": "approve"}, follow=True)
        self.assertEqual(final.status_code, 200)
        target.refresh_from_db()
        self.assertEqual(target.approval_state, ApprovalState.APPROVED)
        self.assertEqual(target.progress_percent, 100)
        self.assertEqual(assignment.completion_percent, 100)

    def test_admin_circular_draft_publishes_on_approval_and_teacher_signs_real_document(self):
        staff, membership, client = self._staff("admin_staff", "0556100081")
        teacher, _, teacher_client = self._staff("teacher", "0556100082")
        self._grant(membership, caps.DRAFT_CIRCULARS)
        created = client.post(reverse("reports:circular_draft_list"), {
            "title": "تعميم رحلة الإقرار", "body": "يرجى الاطلاع على تعليمات المدرسة.",
            "audience": CircularDraft.Audience.ALL, "requires_signature": "on",
        })
        self.assertEqual(created.status_code, 302)
        draft = CircularDraft.objects.get(owner=staff, title="تعميم رحلة الإقرار")
        action = reverse("reports:circular_draft_action", args=[draft.pk])
        client.post(action, {"approval_action": "submit"})
        approved = self.manager_client.post(action, {"approval_action": "approve"}, follow=True)
        self.assertEqual(approved.status_code, 200)
        draft.refresh_from_db()
        self.assertTrue(draft.is_published)
        recipient = NotificationRecipient.objects.get(notification=draft.published_notification, teacher=teacher)
        detail = teacher_client.get(reverse("reports:my_circular_detail", args=[recipient.pk]))
        self.assertContains(detail, draft.title)
        recipient.refresh_from_db()
        self.assertTrue(recipient.is_read)
        signed = teacher_client.post(reverse("reports:circular_sign", args=[recipient.pk]),
                                     {"ack": "1", "signature_data": drawn_signature_data()}, follow=True)
        self.assertEqual(signed.status_code, 200)
        recipient.refresh_from_db()
        self.assertTrue(recipient.is_signed)
        self.assertTrue(recipient.signed_document_digest)
        self.assertTrue(recipient.signature_evidence_digest)
        self.assertEqual(teacher_client.get(reverse("reports:circular_receipt", args=[recipient.pk])).status_code, 200)

    def test_lab_technician_inventory_handover_and_experiment_reach_manager_approval(self):
        tech, _, client = self._staff("lab_tech", "0556100091")
        teacher, _, _ = self._staff("teacher", "0556100092")
        created = client.post(reverse("reports:lab_assets"), {
            "name": "عهدة المجهر", "category": LabAsset.Category.TOOL,
            "quantity": 2, "unit": "قطعة", "condition": LabAsset.Condition.GOOD,
        })
        self.assertEqual(created.status_code, 302)
        asset = LabAsset.objects.get(name="عهدة المجهر", recorded_by=tech)
        moved = client.post(reverse("reports:lab_asset_action", args=[asset.pk]), {
            "lab_action": "handover", "direction": LabAssetHandover.Direction.OUT,
            "person": teacher.pk, "quantity": 1,
        }, follow=True)
        self.assertEqual(moved.status_code, 200)
        self.assertEqual(asset.out_quantity, 1)
        self.assertEqual(asset.available_quantity, 1)
        created = client.post(reverse("reports:lab_experiments"), {
            "title": "تجربة الرحلة المختبرية", "experiment_date": timezone.localdate().isoformat(),
            "subject": "العلوم", "class_name": "الثالث المتوسط", "students_count": 20,
            "procedure": "تنفيذ التجربة وتسجيل النتائج وإعادة الأدوات إلى العهدة.",
        })
        self.assertEqual(created.status_code, 302)
        experiment = LabExperiment.objects.get(recorder=tech, title="تجربة الرحلة المختبرية")
        action = reverse("reports:lab_experiment_action", args=[experiment.pk])
        client.post(action, {"approval_action": "submit"})
        approved = self.manager_client.post(action, {"approval_action": "approve"}, follow=True)
        self.assertEqual(approved.status_code, 200)
        experiment.refresh_from_db()
        self.assertEqual(experiment.approval_state, ApprovalState.APPROVED)

    def test_another_school_never_opens_staff_work_even_for_same_account(self):
        user, _, client = self._staff("teacher", "0556100101")
        report = self._report(user, client)
        other = School.objects.create(name="مدرسة السياق الآخر", code="role-e2e-other")
        SchoolSubscription.objects.create(school=other, plan=self.plan)
        SchoolMembership.objects.create(school=other, teacher=user, role_type=SchoolMembership.RoleType.TEACHER)
        switched = client.post(reverse("reports:switch_school"), {"school_id": other.pk})
        self.assertEqual(switched.status_code, 302)
        self.assertEqual(client.session["active_school_id"], other.pk)
        for name in ("reports:approval_detail", "reports:report_print", "reports:edit_my_report"):
            with self.subTest(destination=name):
                self.assertEqual(client.get(reverse(name, args=[report.pk])).status_code, 404)
        self.assertNotContains(client.get(reverse("reports:my_reports")), report.title)
