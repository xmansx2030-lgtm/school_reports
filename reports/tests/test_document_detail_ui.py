"""Presentation contracts for the document-archive detail pilot."""

from django.urls import reverse

from reports.model_parts.approvals import ApprovalState
from reports.models import School, SchoolMembership, SchoolSubscription, SubscriptionPlan
from reports.services_approval import approve, submit
from reports.tests.test_documents import DocumentBase, _user


class DocumentDetailUITests(DocumentBase):
    def _enter(self, user):
        self.client.force_login(user)
        session = self.client.session
        session["active_school_id"] = self.school.pk
        session.save()

    def test_list_to_detail_keeps_file_and_metadata_actions(self):
        document = self._document(description="وصف تجريبي\nسطر ثانٍ")
        self._enter(self.staff)

        archive = self.client.get(reverse("reports:document_archive"))
        detail_url = reverse("reports:document_detail", args=[document.pk])
        self.assertContains(archive, detail_url)

        response = self.client.get(detail_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="twq-page document-detail-page"')
        self.assertContains(response, 'data-status="draft"')
        self.assertContains(response, "بيانات الوثيقة")
        self.assertContains(response, "وصف تجريبي")
        self.assertContains(response, "<bdi>")
        self.assertContains(response, document.file.url)
        self.assertContains(response, 'data-dc="submit"')
        self.assertContains(response, 'id="docAction"')
        self.assertContains(response, 'name="approval_action"')
        self.assertNotContains(response, 'data-dc="approve"')

    def test_missing_file_has_no_false_open_or_download_action(self):
        document = self._document(file="")
        self._enter(self.staff)

        response = self.client.get(reverse("reports:document_detail", args=[document.pk]))

        self.assertContains(response, "لا يوجد ملف مرفق")
        self.assertNotContains(response, "فتح ملف الوثيقة")
        self.assertNotContains(response, "تنزيل ملف الوثيقة")

    def test_manager_review_and_approved_history_remain_visible(self):
        document = self._document()
        submit(document, self.staff, school=self.school)
        self._enter(self.manager)
        url = reverse("reports:document_detail", args=[document.pk])

        pending = self.client.get(url)
        self.assertContains(pending, 'data-dc="approve"')
        self.assertContains(pending, 'name="note"')
        self.assertContains(pending, "سجل الأرشفة")

        approve(document, self.manager, school=self.school)
        complete = self.client.get(url)
        self.assertContains(complete, 'data-status="approved"')
        self.assertContains(complete, "مؤرشفة معتمدة")
        self.assertNotContains(complete, 'data-dc="approve"')
        self.assertEqual(complete.context["document"].approval_state, ApprovalState.APPROVED)

    def test_other_employee_and_wrong_school_cannot_open_private_detail(self):
        private = self._document()
        other = _user("زميل", "0500080091")
        SchoolMembership.objects.create(
            school=self.school, teacher=other, role_type=SchoolMembership.RoleType.TEACHER
        )
        self._enter(other)
        self.assertEqual(
            self.client.get(reverse("reports:document_detail", args=[private.pk])).status_code,
            404,
        )

        plan = SubscriptionPlan.objects.create(
            name="باقة مدرسة أخرى", price=0, days_duration=365, max_teachers=0
        )
        elsewhere = School.objects.create(name="مدرسة أخرى", code="doc-ui-far")
        SchoolSubscription.objects.create(school=elsewhere, plan=plan)
        manager = _user("مدير آخر", "0500080092")
        SchoolMembership.objects.create(
            school=elsewhere,
            teacher=manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )
        far = self._document(school=elsewhere, owner=manager, uploaded_by=manager)
        self._enter(self.manager)
        self.assertEqual(
            self.client.get(reverse("reports:document_detail", args=[far.pk])).status_code,
            404,
        )
