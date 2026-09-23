# -*- coding: utf-8 -*-
"""أرشيف الوثائق.

خاصيتان تحرسهما هذه الاختبارات:

1. **الأرشفة قرار لا فعل رفع.** الوثيقة تُرفع مسودةً ثم يُعتمد نقلها — ورافعها
   لا يعتمد نقل وثيقته.
2. **النطاق قبل البحث لا بعده.** مرشّحُ الطلب لا يوسّع ما ضاقت به الصلاحية،
   والوثيقة قيد المراجعة شأنُ صاحبها ومراجعِها وحدهما.
"""
from __future__ import annotations

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from reports import capabilities as caps
from reports.model_parts.approvals import ApprovalState
from reports.models import (
    Department,
    Document,
    School,
    SchoolMembership,
    SchoolSubscription,
    StaffScope,
    SubscriptionPlan,
    Teacher,
)
from reports.services_approval import (
    ApprovalError,
    approve,
    available_actions,
    issue,
    submit,
)
from reports.services_documents import apply_document_filters, visible_documents


def _user(name: str, phone: str) -> Teacher:
    return Teacher.objects.create_user(phone=phone, name=name, password="Passw0rd!123")


def _pdf(name: str = "doc.pdf") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n", content_type="application/pdf")


@override_settings(MEDIA_ROOT="/tmp/doc-test-media")
class DocumentBase(TestCase):
    def setUp(self):
        plan = SubscriptionPlan.objects.create(
            name="باقة", price=0, days_duration=365, max_teachers=0
        )
        self.school = School.objects.create(
            name="مدرسة الوثائق", code="doc-school", current_academic_year="1447-1448"
        )
        self.school.allowed_academic_years = ["1447-1448", "1446-1447"]
        self.school.save(update_fields=["allowed_academic_years"])
        SchoolSubscription.objects.create(school=self.school, plan=plan)

        self.manager = _user("المدير", "0500080001")
        SchoolMembership.objects.create(
            school=self.school, teacher=self.manager, role_type=SchoolMembership.RoleType.MANAGER
        )
        self.staff = _user("الموظف", "0500080002")
        self.staff_membership = SchoolMembership.objects.create(
            school=self.school, teacher=self.staff, role_type=SchoolMembership.RoleType.ADMIN_STAFF
        )
        self.department = Department.objects.create(
            school=self.school, name="الشؤون الإدارية", slug="doc-ops"
        )
        self.staff_scope = StaffScope.objects.create(
            membership=self.staff_membership,
            capabilities=[caps.ARCHIVE_DOCUMENTS],
        )
        self.staff_scope.departments.add(self.department)

    def _document(self, owner=None, **overrides):
        data = {
            "school": self.school,
            "owner": owner or self.staff,
            "uploaded_by": owner or self.staff,
            "title": "محضر لجنة السلامة",
            "academic_year": "1447-1448",
            "department": self.department,
            "kind": Document.Kind.MINUTES,
            "file": _pdf(),
        }
        data.update(overrides)
        return Document.objects.create(**data)


class DocumentModelTests(DocumentBase):
    def test_the_owner_name_is_snapshotted(self):
        document = self._document()
        self.assertEqual(document.owner_name, "الموظف")

    def test_the_file_size_is_recorded(self):
        document = self._document()
        self.assertGreater(document.storage_bytes, 0)

    def test_a_document_without_a_year_cannot_be_submitted(self):
        """الأرشيف يُقلَّب بالسنة أولاً."""
        document = self._document(academic_year="")
        with self.assertRaises(ValidationError):
            submit(document, self.staff, school=self.school)

    def test_an_archived_document_reports_itself_as_such(self):
        document = self._document()
        submit(document, self.staff, school=self.school)
        approve(document, self.manager, school=self.school)

        self.assertTrue(document.is_archived)
        self.assertFalse(document.is_editable_by_owner)


class DocumentApprovalTests(DocumentBase):
    def test_the_staff_submits_and_the_manager_approves(self):
        document = self._document()
        submit(document, self.staff, school=self.school)
        self.assertEqual(document.approval_state, ApprovalState.SUBMITTED)

        approve(document, self.manager, school=self.school)
        self.assertEqual(document.approval_state, ApprovalState.APPROVED)
        self.assertEqual(document.decided_by_id, self.manager.pk)

    def test_the_uploader_cannot_approve_their_own_document(self):
        """الأرشفة قرار — ورافعُ الوثيقة لا يقرّره على نفسه."""
        document = self._document()
        submit(document, self.staff, school=self.school)

        with self.assertRaises((PermissionDenied, ApprovalError)):
            approve(document, self.staff, school=self.school)

        document.refresh_from_db()
        self.assertNotEqual(document.approval_state, ApprovalState.APPROVED)

    def test_the_manager_issues_their_own_document_instead_of_self_approving(self):
        document = self._document(owner=self.manager, uploaded_by=self.manager)
        submit(document, self.manager, school=self.school)

        actions = available_actions(document, self.manager, school=self.school)
        self.assertIn("issue", actions)
        self.assertIn("withdraw", actions)

        with self.assertRaises((PermissionDenied, ApprovalError)):
            approve(document, self.manager, school=self.school)

        issue(document, self.manager, school=self.school)
        document.refresh_from_db()
        self.assertEqual(document.approval_state, ApprovalState.APPROVED)
        self.assertEqual(document.decided_by_id, self.manager.pk)

    def test_an_archived_document_cannot_be_resubmitted(self):
        document = self._document()
        submit(document, self.staff, school=self.school)
        approve(document, self.manager, school=self.school)

        with self.assertRaises(ApprovalError):
            submit(document, self.staff, school=self.school)

    def test_a_deputy_supervising_the_department_may_approve(self):
        deputy = _user("الوكيل", "0500080010")
        membership = SchoolMembership.objects.create(
            school=self.school, teacher=deputy, role_type=SchoolMembership.RoleType.DEPUTY
        )
        scope = StaffScope.objects.create(
            membership=membership,
            capabilities=[caps.ARCHIVE_DOCUMENTS, caps.RECOMMEND_APPROVAL],
        )
        scope.departments.add(self.department)

        document = self._document()
        submit(document, self.staff, school=self.school)

        actions = available_actions(document, deputy, school=self.school)
        self.assertIn("return", actions)

    def test_a_deputy_outside_the_department_cannot_review(self):
        other = Department.objects.create(
            school=self.school, name="قسم آخر", slug="doc-other"
        )
        deputy = _user("وكيل بعيد", "0500080011")
        membership = SchoolMembership.objects.create(
            school=self.school, teacher=deputy, role_type=SchoolMembership.RoleType.DEPUTY
        )
        scope = StaffScope.objects.create(
            membership=membership, capabilities=[caps.ARCHIVE_DOCUMENTS]
        )
        scope.departments.add(other)

        document = self._document()
        submit(document, self.staff, school=self.school)

        self.assertEqual(available_actions(document, deputy, school=self.school), [])

    def test_a_document_without_a_department_is_reviewed_by_the_manager_only(self):
        """وثيقةٌ بلا قسم لا نطاق يشملها."""
        deputy = _user("وكيل", "0500080012")
        membership = SchoolMembership.objects.create(
            school=self.school, teacher=deputy, role_type=SchoolMembership.RoleType.DEPUTY
        )
        scope = StaffScope.objects.create(
            membership=membership, capabilities=[caps.ARCHIVE_DOCUMENTS]
        )
        scope.departments.add(self.department)

        document = self._document(department=None)
        submit(document, self.staff, school=self.school)

        self.assertEqual(available_actions(document, deputy, school=self.school), [])
        self.assertIn("approve", available_actions(document, self.manager, school=self.school))


class DocumentScopeTests(DocumentBase):
    """النطاق قبل البحث."""

    def test_the_manager_sees_every_document(self):
        self._document()
        self._document(owner=self.manager, title="وثيقة المدير")

        visible = visible_documents(self.manager, self.school)
        self.assertEqual(visible.count(), 2)

    def test_a_colleague_does_not_see_a_draft_of_another(self):
        """الوثيقة قيد المراجعة شأنُ صاحبها ومراجعِها وحدهما."""
        self._document(title="مسودة الموظف")
        other = _user("زميل", "0500080020")
        SchoolMembership.objects.create(
            school=self.school, teacher=other, role_type=SchoolMembership.RoleType.TEACHER
        )

        visible = visible_documents(other, self.school)
        self.assertEqual(visible.count(), 0)

    def test_everyone_sees_the_approved_archive(self):
        """الأرشيف المعتمَد مرجعٌ مشترك."""
        document = self._document(title="محضر معتمد")
        submit(document, self.staff, school=self.school)
        approve(document, self.manager, school=self.school)

        other = _user("زميل", "0500080021")
        SchoolMembership.objects.create(
            school=self.school, teacher=other, role_type=SchoolMembership.RoleType.TEACHER
        )
        titles = [item.title for item in visible_documents(other, self.school)]
        self.assertEqual(titles, ["محضر معتمد"])

    def test_the_owner_sees_their_own_draft(self):
        self._document(title="مسودتي")
        titles = [item.title for item in visible_documents(self.staff, self.school)]
        self.assertIn("مسودتي", titles)

    def test_filters_cannot_widen_the_scope(self):
        """المرشّح يُبنى فوق النطاق لا بدلاً منه."""
        self._document(title="مسودة الموظف")
        other = _user("زميل", "0500080022")
        SchoolMembership.objects.create(
            school=self.school, teacher=other, role_type=SchoolMembership.RoleType.TEACHER
        )

        scoped = visible_documents(other, self.school)
        widened = apply_document_filters(scoped, year="1447-1448", kind="minutes")
        self.assertEqual(widened.count(), 0)

    def test_filters_narrow_correctly(self):
        self._document(title="محضر", kind=Document.Kind.MINUTES)
        self._document(title="مستند مالي", kind=Document.Kind.FINANCIAL)

        scoped = visible_documents(self.manager, self.school)
        self.assertEqual(apply_document_filters(scoped, kind="financial").count(), 1)
        self.assertEqual(apply_document_filters(scoped, year="1446-1447").count(), 0)
        self.assertEqual(apply_document_filters(scoped, term="مالي").count(), 1)

    def test_an_unknown_kind_filter_is_ignored(self):
        self._document()
        scoped = visible_documents(self.manager, self.school)
        self.assertEqual(apply_document_filters(scoped, kind="'; DROP--").count(), 1)


@override_settings(ALLOWED_HOSTS=["testserver"], MEDIA_ROOT="/tmp/doc-test-media")
class DocumentScreenTests(DocumentBase):
    def _enter(self, user):
        self.client.force_login(user)
        session = self.client.session
        session["active_school_id"] = self.school.pk
        session.save()

    def test_the_archive_page_opens(self):
        self._document()
        self._enter(self.staff)

        response = self.client.get(reverse("reports:document_archive"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "محضر لجنة السلامة")

    def test_archive_list_uses_shared_search_and_document_patterns(self):
        document = self._document()
        self._enter(self.staff)

        response = self.client.get(reverse("reports:document_archive"))

        self.assertContains(response, 'class="twq-toolbar archive-list-toolbar"')
        self.assertContains(response, 'name="q"')
        self.assertContains(response, 'name="year"')
        self.assertContains(response, 'name="department"')
        self.assertContains(response, 'name="kind"')
        self.assertContains(response, 'class="twq-document-item archive-list-item"')
        self.assertContains(response, reverse("reports:document_detail", args=[document.pk]))
        self.assertContains(response, 'data-status="draft"')
        self.assertContains(response, 'id="docUploadForm"')

    def test_archive_filters_keep_search_and_show_active_context(self):
        self._document(title="محضر الجودة")
        self._document(title="مستند مالي", kind=Document.Kind.FINANCIAL)
        self._enter(self.staff)

        response = self.client.get(
            reverse("reports:document_archive"),
            {"q": "الجودة", "year": "1447-1448", "kind": Document.Kind.MINUTES},
        )

        self.assertEqual(response.context["page_obj"].paginator.count, 1)
        self.assertContains(response, "محضر الجودة")
        self.assertEqual(
            [item.title for item in response.context["page_obj"]],
            ["محضر الجودة"],
        )
        self.assertContains(response, "الفلاتر النشطة")
        self.assertContains(response, "مسح الفلاتر")

    def test_manager_review_queue_remains_visible_before_results(self):
        document = self._document()
        submit(document, self.staff, school=self.school)
        self._enter(self.manager)

        response = self.client.get(reverse("reports:document_archive"))
        html = response.content.decode()

        self.assertContains(response, "تنتظر اعتماد الأرشفة")
        self.assertContains(response, "مراجعة")
        self.assertLess(html.index("archive-list-review"), html.index("archive-list-results__summary"))

    def test_uploading_a_document_from_the_screen(self):
        self._enter(self.staff)
        response = self.client.post(
            reverse("reports:document_archive"),
            {
                "title": "خطاب إدارة التعليم",
                "description": "تعميم وارد",
                "academic_year": "1447-1448",
                "department": self.department.pk,
                "kind": Document.Kind.LETTER,
                "file": _pdf("letter.pdf"),
            },
        )
        self.assertEqual(response.status_code, 302)

        document = Document.objects.get(title="خطاب إدارة التعليم")
        self.assertEqual(document.approval_state, ApprovalState.DRAFT)
        self.assertEqual(document.owner_id, self.staff.pk)

    def test_upload_department_choices_are_limited_to_the_staff_scope(self):
        outside = Department.objects.create(
            school=self.school, name="قسم خارج النطاق", slug="doc-outside"
        )
        self._enter(self.staff)

        response = self.client.get(reverse("reports:document_archive"))

        choices = response.context["form"].fields["department"].queryset
        self.assertEqual(list(choices), [self.department])
        self.assertNotIn(outside, choices)

    def test_forged_upload_to_a_department_outside_the_staff_scope_is_refused(self):
        outside = Department.objects.create(
            school=self.school, name="قسم خارج النطاق", slug="doc-forged-outside"
        )
        self._enter(self.staff)

        response = self.client.post(
            reverse("reports:document_archive"),
            {
                "title": "وثيقة خارج النطاق",
                "description": "اختبار حد الصلاحية",
                "academic_year": "1447-1448",
                "department": outside.pk,
                "kind": Document.Kind.LETTER,
                "file": _pdf("outside.pdf"),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Document.objects.filter(title="وثيقة خارج النطاق").exists())
        self.assertIn("department", response.context["form"].errors)

    def test_manager_upload_form_keeps_all_school_departments(self):
        outside = Department.objects.create(
            school=self.school, name="قسم ثانٍ", slug="doc-manager-second"
        )
        self._enter(self.manager)

        response = self.client.get(reverse("reports:document_archive"))

        choices = response.context["form"].fields["department"].queryset
        self.assertEqual(set(choices), {self.department, outside})

    def test_a_year_is_required_on_upload(self):
        self._enter(self.staff)
        self.client.post(
            reverse("reports:document_archive"),
            {
                "title": "بلا سنة",
                "academic_year": "",
                "kind": Document.Kind.OTHER,
                "file": _pdf("x.pdf"),
            },
        )
        self.assertFalse(Document.objects.filter(title="بلا سنة").exists())

    def test_a_draft_of_another_user_reads_as_missing(self):
        document = self._document()
        other = _user("زميل", "0500080030")
        SchoolMembership.objects.create(
            school=self.school, teacher=other, role_type=SchoolMembership.RoleType.TEACHER
        )
        self._enter(other)

        response = self.client.get(reverse("reports:document_detail", args=[document.pk]))
        self.assertEqual(response.status_code, 404)

    def test_the_full_archiving_flow_through_the_screen(self):
        document = self._document()
        self._enter(self.staff)

        self.client.post(
            reverse("reports:document_action", args=[document.pk]),
            {"approval_action": "submit"},
        )
        document.refresh_from_db()
        self.assertEqual(document.approval_state, ApprovalState.SUBMITTED)

        self._enter(self.manager)
        self.client.post(
            reverse("reports:document_action", args=[document.pk]),
            {"approval_action": "approve", "note": "مطابق"},
        )
        document.refresh_from_db()
        self.assertEqual(document.approval_state, ApprovalState.APPROVED)

    def test_manager_owned_document_appears_in_inbox_and_can_be_issued(self):
        document = self._document(owner=self.manager, uploaded_by=self.manager)
        submit(document, self.manager, school=self.school)
        self._enter(self.manager)

        inbox = self.client.get(reverse("reports:approval_inbox"))

        self.assertEqual(inbox.status_code, 200)
        self.assertContains(inbox, document.title)
        self.assertContains(inbox, "إصدار وأرشفة")
        self.assertEqual(inbox.context["total"], 1)
        self.assertEqual(inbox.context["rows"][0]["kind"], "document")

        response = self.client.post(
            reverse("reports:document_action", args=[document.pk]),
            {"approval_action": "issue"},
        )

        self.assertRedirects(
            response,
            reverse("reports:document_detail", args=[document.pk]),
            fetch_redirect_response=False,
        )
        document.refresh_from_db()
        self.assertEqual(document.approval_state, ApprovalState.APPROVED)

    def test_the_uploader_posting_approve_is_refused(self):
        document = self._document()
        submit(document, self.staff, school=self.school)
        self._enter(self.staff)

        self.client.post(
            reverse("reports:document_action", args=[document.pk]),
            {"approval_action": "approve"},
        )
        document.refresh_from_db()
        self.assertNotEqual(document.approval_state, ApprovalState.APPROVED)

    def test_a_document_of_another_school_is_not_reachable(self):
        plan = SubscriptionPlan.objects.create(
            name="ب2", price=0, days_duration=365, max_teachers=0
        )
        elsewhere = School.objects.create(name="مدرسة أخرى", code="doc-far")
        SchoolSubscription.objects.create(school=elsewhere, plan=plan)
        stranger = _user("بعيد", "0500080031")
        SchoolMembership.objects.create(
            school=elsewhere, teacher=stranger, role_type=SchoolMembership.RoleType.MANAGER
        )
        far = Document.objects.create(
            school=elsewhere,
            owner=stranger,
            title="وثيقة بعيدة",
            academic_year="1447-1448",
            kind=Document.Kind.OTHER,
            file=_pdf("far.pdf"),
        )
        self._enter(self.manager)

        archive_page = self.client.get(reverse("reports:document_archive"))
        self.assertNotIn(far, archive_page.context["page_obj"].object_list)

        response = self.client.get(reverse("reports:document_detail", args=[far.pk]))
        self.assertEqual(response.status_code, 404)
