# -*- coding: utf-8 -*-
"""الاجتماعات والقرارات.

الخاصية المركزية التي تحرسها هذه الاختبارات هي **الجسر**: أن القرار يتحوّل إلى
تكليف قابل للمتابعة بموعده وشواهده واعتماده. فبدون هذا التحويل يبقى بند «متابعة
تنفيذ القرارات» في التوصيف بلا مقابل مهما كثُرت المحاضر.

ويليها في الأهمية أن **المحضر يرث دورة الاعتماد ولا يعيد تعريفها** — فقاعدة «لا
يعتمد أحد عمله» تسري على كاتب المحضر بلا سطر جديد.
"""
from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from reports import capabilities as caps
from reports.forms_meetings import SchoolMeetingForm
from reports.model_parts.approvals import ApprovalState
from reports.models import (
    Assignment,
    Decision,
    Department,
    DepartmentMembership,
    Meeting,
    MeetingAgendaItem,
    MeetingAttendee,
    MeetingMinutes,
    School,
    SchoolMembership,
    SchoolSubscription,
    StaffScope,
    SubscriptionPlan,
    Teacher,
)
from reports.services_approval import (
    approve,
    available_actions,
    issue,
    submit,
)
from reports.services_meetings import (
    MeetingError,
    cancel_meeting,
    convert_decision_to_assignment,
    ensure_minutes,
    mark_held,
    set_attendance,
)


def _user(name: str, phone: str) -> Teacher:
    return Teacher.objects.create_user(phone=phone, name=name, password="Passw0rd!123")


def _school(name: str, code: str) -> School:
    plan = SubscriptionPlan.objects.create(
        name=f"باقة {code}", price=0, days_duration=365, max_teachers=0
    )
    school = School.objects.create(name=name, code=code)
    SchoolSubscription.objects.create(school=school, plan=plan)
    return school


class MeetingBase(TestCase):
    def setUp(self):
        self.school = _school("مدرسة الاجتماعات", "mtg-school")
        self.manager = _user("المدير", "0500050001")
        SchoolMembership.objects.create(
            school=self.school, teacher=self.manager, role_type=SchoolMembership.RoleType.MANAGER
        )
        self.department = Department.objects.create(
            school=self.school, name="اللجنة التعليمية", slug="edu-committee"
        )
        self.staff = _user("الموظف", "0500050002")
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.staff,
            role_type=SchoolMembership.RoleType.ADMIN_STAFF,
        )
        DepartmentMembership.objects.create(department=self.department, teacher=self.staff)

    def _meeting(self, organizer=None, attendees=None, **overrides):
        data = {
            "scope": Meeting.Scope.SCHOOL,
            "school": self.school,
            "organizer": organizer or self.manager,
            "department": self.department,
            "title": "اجتماع اللجنة الأول",
            "scheduled_at": timezone.now() + timedelta(days=2),
        }
        data.update(overrides)
        meeting = Meeting.objects.create(**data)
        for person in attendees if attendees is not None else [self.staff]:
            MeetingAttendee.objects.create(meeting=meeting, person=person)
        return meeting


class MeetingLifecycleTests(MeetingBase):
    def test_a_school_meeting_requires_a_school(self):
        meeting = Meeting(
            scope=Meeting.Scope.SCHOOL,
            organizer=self.manager,
            title="بلا مدرسة",
            scheduled_at=timezone.now() + timedelta(days=1),
        )
        with self.assertRaises(ValidationError):
            meeting.full_clean()

    def test_being_held_is_recorded_not_inferred_from_time(self):
        """اجتماعٌ فات وقته ولم ينعقد ليس منعقداً."""
        meeting = self._meeting(scheduled_at=timezone.now() - timedelta(days=3))
        self.assertFalse(meeting.is_held)

        mark_held(meeting, self.manager)
        self.assertTrue(meeting.is_held)
        self.assertIsNotNone(meeting.held_at)

    def test_only_the_organizer_may_mark_it_held(self):
        meeting = self._meeting()
        with self.assertRaises(PermissionDenied):
            mark_held(meeting, self.staff)

    def test_a_held_meeting_cannot_be_cancelled(self):
        meeting = self._meeting()
        mark_held(meeting, self.manager)
        with self.assertRaises(MeetingError):
            cancel_meeting(meeting, self.manager, reason="تغيّر الرأي")

    def test_cancelling_keeps_the_record(self):
        """الدعوة وصلت المدعوين، فمحوها يمحو الواقعة."""
        meeting = self._meeting()
        cancel_meeting(meeting, self.manager, reason="تعارض المواعيد")

        meeting.refresh_from_db()
        self.assertTrue(meeting.is_cancelled)
        self.assertEqual(meeting.cancel_reason, "تعارض المواعيد")
        self.assertTrue(Meeting.objects.filter(pk=meeting.pk).exists())


class AttendanceTests(MeetingBase):
    def test_attendance_starts_as_invited(self):
        meeting = self._meeting()
        self.assertEqual(
            meeting.attendees.first().status, MeetingAttendee.Status.INVITED
        )

    def test_recording_attendance_updates_the_summary(self):
        other = _user("مدعو ثانٍ", "0500050010")
        SchoolMembership.objects.create(
            school=self.school, teacher=other, role_type=SchoolMembership.RoleType.TEACHER
        )
        meeting = self._meeting(attendees=[self.staff, other])
        mark_held(meeting, self.manager)

        rows = {
            str(item.pk): (
                MeetingAttendee.Status.PRESENT
                if item.person_id == self.staff.pk
                else MeetingAttendee.Status.ABSENT
            )
            for item in meeting.attendees.all()
        }
        set_attendance(meeting, self.manager, rows=rows)

        summary = meeting.attendance_summary
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["present"], 1)
        self.assertEqual(summary["absent"], 1)

    def test_unknown_attendee_ids_are_ignored_not_fatal(self):
        """نموذجٌ أُرسل بعد حذف مدعوّ لا يجوز أن يُسقط تسجيل البقية."""
        meeting = self._meeting()
        mark_held(meeting, self.manager)
        attendee = meeting.attendees.first()

        set_attendance(
            meeting,
            self.manager,
            rows={str(attendee.pk): "present", "999999": "present", "abc": "absent"},
        )
        attendee.refresh_from_db()
        self.assertEqual(attendee.status, MeetingAttendee.Status.PRESENT)

    def test_an_invalid_status_is_ignored(self):
        meeting = self._meeting()
        mark_held(meeting, self.manager)
        attendee = meeting.attendees.first()

        set_attendance(meeting, self.manager, rows={str(attendee.pk): "teleported"})
        attendee.refresh_from_db()
        self.assertEqual(attendee.status, MeetingAttendee.Status.INVITED)


class MinutesApprovalTests(MeetingBase):
    """المحضر يرث المكوّن المشترك."""

    def _held_meeting_with_minutes(self, recorder=None):
        meeting = self._meeting()
        mark_held(meeting, self.manager)
        minutes = ensure_minutes(meeting, recorder=recorder or self.staff)
        minutes.body = "دار النقاش حول خطة الفصل."
        minutes.save(update_fields=["body"])
        return meeting, minutes

    def test_minutes_start_as_a_draft(self):
        meeting, minutes = self._held_meeting_with_minutes()
        self.assertEqual(minutes.approval_state, ApprovalState.DRAFT)
        self.assertTrue(minutes.is_editable_by_owner)

    def test_an_empty_minutes_cannot_be_submitted(self):
        meeting = self._meeting()
        mark_held(meeting, self.manager)
        minutes = ensure_minutes(meeting, recorder=self.staff)

        with self.assertRaises(ValidationError):
            submit(minutes, self.staff, school=self.school)

    def test_minutes_of_a_meeting_that_never_happened_cannot_be_submitted(self):
        meeting = self._meeting()
        # سجل قديم سابق لحارس الإنشاء يجب ألا يتجاوز شرط الإرسال.
        minutes = MeetingMinutes.objects.create(
            meeting=meeting,
            recorder=self.staff,
            body="محضر لاجتماع لم ينعقد",
        )

        with self.assertRaises(ValidationError):
            submit(minutes, self.staff, school=self.school)

    def test_the_recorder_submits_and_the_organizer_approves(self):
        meeting, minutes = self._held_meeting_with_minutes()

        submit(minutes, self.staff, school=self.school)
        self.assertEqual(minutes.approval_state, ApprovalState.SUBMITTED)

        approve(minutes, self.manager, school=self.school)
        self.assertEqual(minutes.approval_state, ApprovalState.APPROVED)
        self.assertEqual(minutes.decided_by_id, self.manager.pk)

    def test_the_recorder_cannot_approve_their_own_minutes(self):
        """القاعدة الكبرى تسري على المحضر بلا إعادة تعريف."""
        meeting = self._meeting(organizer=self.staff)
        mark_held(meeting, self.staff)
        minutes = ensure_minutes(meeting, recorder=self.staff)
        minutes.body = "محضر كتبه المنظّم نفسه"
        minutes.save(update_fields=["body"])
        submit(minutes, self.staff, school=self.school)

        with self.assertRaises(PermissionDenied):
            approve(minutes, self.staff, school=self.school)

    def test_an_admin_organizer_submits_their_minutes_to_the_manager(self):
        meeting = self._meeting(organizer=self.staff)
        mark_held(meeting, self.staff)
        minutes = ensure_minutes(meeting, recorder=self.staff)
        minutes.body = "محضر كتبه الموظف الإداري المنظّم"
        minutes.save(update_fields=["body"])

        actions = available_actions(minutes, self.staff, school=self.school)
        self.assertIn("submit", actions)
        self.assertNotIn("issue", actions)
        with self.assertRaises(PermissionDenied):
            issue(minutes, self.staff, school=self.school)

        submit(minutes, self.staff, school=self.school)
        approve(minutes, self.manager, school=self.school)
        minutes.refresh_from_db()
        self.assertEqual(minutes.approval_state, ApprovalState.APPROVED)
        self.assertEqual(minutes.decided_by_id, self.manager.pk)

    def test_a_manager_organizer_may_issue_their_own_minutes(self):
        meeting = self._meeting(organizer=self.manager)
        mark_held(meeting, self.manager)
        minutes = ensure_minutes(meeting, recorder=self.manager)
        minutes.body = "محضر اجتماع نظّمه مدير المدرسة"
        minutes.save(update_fields=["body"])

        self.assertIn(
            "issue",
            available_actions(minutes, self.manager, school=self.school),
        )
        issue(minutes, self.manager, school=self.school)
        minutes.refresh_from_db()
        self.assertEqual(minutes.approval_state, ApprovalState.APPROVED)
        self.assertEqual(minutes.decided_by_id, self.manager.pk)

    def test_a_returned_minutes_becomes_editable_again(self):
        meeting, minutes = self._held_meeting_with_minutes()
        submit(minutes, self.staff, school=self.school)

        from reports.services_approval import return_for_changes

        return_for_changes(minutes, self.manager, school=self.school, note="أضف أسماء الحضور")

        self.assertEqual(minutes.approval_state, ApprovalState.RETURNED)
        self.assertTrue(minutes.is_editable_by_owner)

    def test_an_unrelated_colleague_cannot_review_the_minutes(self):
        stranger = _user("غريب", "0500050020")
        SchoolMembership.objects.create(
            school=self.school, teacher=stranger, role_type=SchoolMembership.RoleType.TEACHER
        )
        meeting, minutes = self._held_meeting_with_minutes()
        submit(minutes, self.staff, school=self.school)

        with self.assertRaises(PermissionDenied):
            approve(minutes, stranger, school=self.school)

    def test_a_deputy_supervising_the_committee_may_review(self):
        deputy = _user("الوكيل", "0500050021")
        membership = SchoolMembership.objects.create(
            school=self.school, teacher=deputy, role_type=SchoolMembership.RoleType.DEPUTY
        )
        scope = StaffScope.objects.create(
            membership=membership, capabilities=[caps.MANAGE_MEETINGS]
        )
        scope.departments.add(self.department)

        meeting, minutes = self._held_meeting_with_minutes()
        submit(minutes, self.staff, school=self.school)

        from reports.services_approval import return_for_changes

        return_for_changes(minutes, deputy, school=self.school, note="راجع البند الثاني")
        self.assertEqual(minutes.approval_state, ApprovalState.RETURNED)


class DecisionToAssignmentTests(MeetingBase):
    """الجسر: من قرار موثَّق إلى عمل متابَع."""

    def _held_with_decision(self, **overrides):
        meeting = self._meeting()
        mark_held(meeting, self.manager)
        data = {
            "meeting": meeting,
            "title": "إعداد دليل الاختبارات",
            "order": 1,
        }
        data.update(overrides)
        return meeting, Decision.objects.create(**data)

    def test_a_decision_without_owner_or_date_stays_documented_only(self):
        """التوثيق يسبق المتابعة ولا يشترطها."""
        meeting, decision = self._held_with_decision()

        self.assertFalse(decision.can_become_assignment())
        self.assertEqual(decision.execution_state, "untracked")

    def test_converting_requires_a_responsible(self):
        meeting, decision = self._held_with_decision(
            due_at=timezone.now() + timedelta(days=5)
        )
        with self.assertRaises(MeetingError):
            convert_decision_to_assignment(decision, self.manager)

    def test_converting_requires_a_due_date(self):
        """قرارٌ بلا موعد لا يتأخر أبداً، فلا يُتابَع أبداً."""
        meeting, decision = self._held_with_decision(responsible=self.staff)
        with self.assertRaises(MeetingError):
            convert_decision_to_assignment(decision, self.manager)

    def test_converting_creates_a_tracked_assignment(self):
        meeting, decision = self._held_with_decision(
            responsible=self.staff, due_at=timezone.now() + timedelta(days=5)
        )
        assignment = convert_decision_to_assignment(decision, self.manager)

        self.assertEqual(assignment.source, Assignment.Source.DECISION)
        self.assertEqual(assignment.school_id, self.school.pk)
        self.assertEqual(assignment.department_id, self.department.pk)
        self.assertEqual(assignment.targets.count(), 1)
        self.assertEqual(assignment.targets.first().assignee_id, self.staff.pk)

        decision.refresh_from_db()
        self.assertTrue(decision.is_tracked)

    def test_the_decision_links_to_its_assignment_rather_than_copying_it(self):
        """مصدرا حقيقة يفترقان عند أول تعديل — فالرابط لا النسخة."""
        meeting, decision = self._held_with_decision(
            responsible=self.staff, due_at=timezone.now() + timedelta(days=5)
        )
        assignment = convert_decision_to_assignment(decision, self.manager)
        self.assertEqual(decision.assignment_id, assignment.pk)
        self.assertEqual(assignment.source_decision, decision)

    def test_converting_twice_is_refused(self):
        meeting, decision = self._held_with_decision(
            responsible=self.staff, due_at=timezone.now() + timedelta(days=5)
        )
        convert_decision_to_assignment(decision, self.manager)

        with self.assertRaises(MeetingError):
            convert_decision_to_assignment(decision, self.manager)

    def test_only_the_organizer_converts(self):
        meeting, decision = self._held_with_decision(
            responsible=self.staff, due_at=timezone.now() + timedelta(days=5)
        )
        with self.assertRaises(PermissionDenied):
            convert_decision_to_assignment(decision, self.staff)

    def test_execution_state_follows_the_assignment(self):
        meeting, decision = self._held_with_decision(
            responsible=self.staff, due_at=timezone.now() + timedelta(days=5)
        )
        convert_decision_to_assignment(decision, self.manager)
        target = decision.assignment.targets.first()

        self.assertEqual(decision.execution_state, "running")

        submit(target, self.staff, school=self.school)
        approve(target, self.manager, school=self.school)

        decision.refresh_from_db()
        self.assertEqual(decision.execution_state, "done")

    def test_execution_state_reports_lateness(self):
        meeting, decision = self._held_with_decision(
            responsible=self.staff, due_at=timezone.now() + timedelta(days=5)
        )
        convert_decision_to_assignment(decision, self.manager)
        Assignment.objects.filter(pk=decision.assignment_id).update(
            due_at=timezone.now() - timedelta(days=1)
        )

        decision.refresh_from_db()
        self.assertEqual(decision.execution_state, "late")


@override_settings(ALLOWED_HOSTS=["testserver"])
class MeetingScreenTests(MeetingBase):
    def _enter(self, user):
        self.client.force_login(user)
        session = self.client.session
        session["active_school_id"] = self.school.pk
        session.save()

    def test_the_organizer_opens_the_meeting(self):
        meeting = self._meeting()
        self._enter(self.manager)

        response = self.client.get(reverse("reports:meeting_detail", args=[meeting.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "اجتماع اللجنة الأول")

    def test_an_invitee_opens_the_meeting(self):
        meeting = self._meeting()
        self._enter(self.staff)

        response = self.client.get(reverse("reports:meeting_detail", args=[meeting.pk]))
        self.assertEqual(response.status_code, 200)

    def test_an_outsider_reads_it_as_missing(self):
        """لئلا يُكشف انعقاد اجتماع لمن لا يحق له معرفة أنه انعقد."""
        meeting = self._meeting()
        outsider = _user("غريب", "0500050030")
        SchoolMembership.objects.create(
            school=self.school, teacher=outsider, role_type=SchoolMembership.RoleType.TEACHER
        )
        self._enter(outsider)

        response = self.client.get(reverse("reports:meeting_detail", args=[meeting.pk]))
        self.assertEqual(response.status_code, 404)

    def test_a_plain_teacher_cannot_create_a_meeting(self):
        teacher = _user("معلم", "0500050031")
        SchoolMembership.objects.create(
            school=self.school, teacher=teacher, role_type=SchoolMembership.RoleType.TEACHER
        )
        self._enter(teacher)

        response = self.client.get(reverse("reports:meeting_create"))
        self.assertEqual(response.status_code, 302)

    def test_creating_a_meeting_invites_everyone_chosen(self):
        self._enter(self.manager)
        when = timezone.localtime() + timedelta(days=4)

        response = self.client.post(
            reverse("reports:meeting_create"),
            {
                "title": "اجتماع الخطة",
                "purpose": "مراجعة الخطة",
                "department": self.department.pk,
                "scheduled_at": when.strftime("%Y-%m-%dT%H:%M"),
                "location": "قاعة 1",
                "attendees": [self.staff.pk],
            },
        )
        self.assertEqual(response.status_code, 302)

        meeting = Meeting.objects.get(title="اجتماع الخطة")
        self.assertEqual(meeting.attendees.count(), 1)
        self.assertEqual(meeting.organizer_id, self.manager.pk)

    def test_full_department_scope_offers_every_active_school_member(self):
        other_department = Department.objects.create(
            school=self.school,
            name="اللجنة الإدارية",
            slug="admin-committee",
        )
        outsider = _user("منسوب بلا قسم", "0500050032")
        SchoolMembership.objects.create(
            school=self.school,
            teacher=outsider,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        membership = SchoolMembership.objects.get(
            school=self.school,
            teacher=self.staff,
        )
        scope = StaffScope.objects.create(
            membership=membership,
            capabilities=[caps.MANAGE_MEETINGS],
        )
        scope.departments.add(self.department)

        partial_form = SchoolMeetingForm(
            school=self.school,
            organizer=self.staff,
        )
        self.assertEqual(
            set(partial_form.fields["attendees"].queryset),
            {self.staff},
        )
        self.assertIn(
            "ضمن نطاقك فقط",
            partial_form.fields["attendees"].help_text,
        )

        scope.departments.add(other_department)
        full_form = SchoolMeetingForm(
            school=self.school,
            organizer=self.staff,
        )
        self.assertEqual(
            set(full_form.fields["attendees"].queryset),
            {self.manager, self.staff, outsider},
        )
        self.assertIn(
            "جميع منسوبي المدرسة",
            full_form.fields["attendees"].help_text,
        )

        self._enter(self.staff)
        response = self.client.get(reverse("reports:meeting_create"))
        self.assertContains(
            response,
            "يشمل جميع منسوبي المدرسة لأن نطاقك يغطي جميع الأقسام.",
        )

    def test_meeting_forms_render_unique_field_ids(self):
        meeting = self._meeting()
        self._enter(self.manager)

        scheduled_response = self.client.get(
            reverse("reports:meeting_detail", args=[meeting.pk])
        )
        self.assertContains(scheduled_response, 'id="id_agenda_title"', count=1)

        mark_held(meeting, self.manager)
        held_response = self.client.get(
            reverse("reports:meeting_detail", args=[meeting.pk])
        )

        self.assertNotContains(held_response, 'id="id_agenda_title"')
        self.assertContains(held_response, 'id="id_minutes_body"', count=1)
        self.assertContains(held_response, 'id="id_decision_body"', count=1)
        self.assertNotContains(held_response, 'id="id_body"')

    def test_mark_held_confirmation_uses_action_language_not_delete_language(self):
        meeting = self._meeting()
        self._enter(self.manager)

        response = self.client.get(
            reverse("reports:meeting_detail", args=[meeting.pk])
        )

        self.assertContains(response, 'data-confirm-type="confirm"')
        self.assertContains(
            response,
            'data-confirm-title="تأكيد تسجيل الانعقاد"',
        )
        self.assertContains(
            response,
            'data-confirm-ok="نعم، سجّل الانعقاد"',
        )

    def test_agenda_items_are_added_and_numbered(self):
        meeting = self._meeting()
        self._enter(self.manager)

        for title in ("البند الأول", "البند الثاني"):
            self.client.post(
                reverse("reports:meeting_action", args=[meeting.pk]),
                {"meeting_action": "add_agenda", "title": title, "note": ""},
            )

        orders = list(
            MeetingAgendaItem.objects.filter(meeting=meeting)
            .order_by("order")
            .values_list("order", flat=True)
        )
        self.assertEqual(orders, [1, 2])

    def test_an_invitee_cannot_edit_the_agenda(self):
        meeting = self._meeting()
        self._enter(self.staff)

        self.client.post(
            reverse("reports:meeting_action", args=[meeting.pk]),
            {"meeting_action": "add_agenda", "title": "بند متسلل", "note": ""},
        )
        self.assertFalse(
            MeetingAgendaItem.objects.filter(meeting=meeting, title="بند متسلل").exists()
        )

    def test_marking_held_opens_the_minutes(self):
        meeting = self._meeting()
        self._enter(self.manager)

        self.client.post(
            reverse("reports:meeting_action", args=[meeting.pk]),
            {"meeting_action": "mark_held"},
        )
        meeting.refresh_from_db()

        self.assertTrue(meeting.is_held)
        self.assertTrue(MeetingMinutes.objects.filter(meeting=meeting).exists())

    def test_an_invitee_cannot_create_or_save_minutes_by_direct_post(self):
        meeting = self._meeting()
        Meeting.objects.filter(pk=meeting.pk).update(
            status=Meeting.Status.HELD,
            held_at=timezone.now(),
        )
        self._enter(self.staff)

        response = self.client.post(
            reverse("reports:meeting_action", args=[meeting.pk]),
            {
                "meeting_action": "save_minutes",
                "format_mode": MeetingMinutes.FormatMode.FREEFORM,
                "body": "محضر أرسله مدعو مباشرة.",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(MeetingMinutes.objects.filter(meeting=meeting).exists())

    def test_structured_minutes_are_saved_and_printed(self):
        meeting = self._meeting()
        mark_held(meeting, self.manager)
        self._enter(self.manager)

        self.client.post(
            reverse("reports:meeting_action", args=[meeting.pk]),
            {
                "meeting_action": "save_minutes",
                "format_mode": MeetingMinutes.FormatMode.STRUCTURED,
                "body": "",
                "proceedings": "افتتح المنظم الاجتماع واستعرض جدول الأعمال.",
                "discussions": "نوقشت مؤشرات التحصيل.",
                "decisions_summary": "اعتماد خطة التحسين.",
                "recommendations": "مراجعة الأثر بعد شهر.",
                "assignments_summary": "إعداد التقرير من مسؤول اللجنة.",
            },
        )
        minutes = MeetingMinutes.objects.get(meeting=meeting)
        self.assertEqual(minutes.format_mode, MeetingMinutes.FormatMode.STRUCTURED)

        response = self.client.get(reverse("reports:meeting_print", args=[meeting.pk]))
        self.assertContains(response, "مجريات الاجتماع")
        self.assertContains(response, "مراجعة الأثر بعد شهر")

    def test_meeting_pdf_download_returns_a_pdf(self):
        meeting = self._meeting()
        mark_held(meeting, self.manager)
        minutes = ensure_minutes(meeting, recorder=self.manager)
        minutes.body = "ناقش المجتمعون خطة التحسين واعتمدوها."
        minutes.save()
        self._enter(self.manager)

        response = self.client.get(reverse("reports:meeting_pdf", args=[meeting.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF-"))

    def test_the_decision_is_converted_from_the_screen(self):
        meeting = self._meeting()
        mark_held(meeting, self.manager)
        decision = Decision.objects.create(
            meeting=meeting,
            title="رفع تقرير الأنشطة",
            responsible=self.staff,
            due_at=timezone.now() + timedelta(days=6),
        )
        self._enter(self.manager)

        self.client.post(
            reverse("reports:meeting_action", args=[meeting.pk]),
            {"meeting_action": "track_decision", "decision_id": decision.pk},
        )
        decision.refresh_from_db()

        self.assertTrue(decision.is_tracked)
        self.assertEqual(decision.assignment.source, Assignment.Source.DECISION)

    def test_the_assignee_sees_the_decision_assignment_in_their_list(self):
        """القرار يصل صاحبه عملاً لا نصاً في محضر."""
        meeting = self._meeting()
        mark_held(meeting, self.manager)
        decision = Decision.objects.create(
            meeting=meeting,
            title="تجهيز قاعة المصادر",
            responsible=self.staff,
            due_at=timezone.now() + timedelta(days=6),
        )
        convert_decision_to_assignment(decision, self.manager)

        self._enter(self.staff)
        response = self.client.get(reverse("reports:my_assignments"))
        self.assertContains(response, "تجهيز قاعة المصادر")


@override_settings(
    ALLOWED_HOSTS=["testserver"],
    OPENAI_API_KEY="sk-meeting-test",
    REPORT_AI_ENABLED=True,
    VOICE_REPORT_ENABLED=True,
    VOICE_REPORT_PWA_ONLY=True,
    VOICE_REPORT_DAILY_LIMIT=3,
    RATELIMIT_ENABLE=False,
)
class MeetingAIAssistantTests(MeetingBase):
    def setUp(self):
        super().setUp()
        cache.clear()

    def _enter(self, user):
        self.client.force_login(user)
        session = self.client.session
        session["active_school_id"] = self.school.pk
        session.save()

    def _held_meeting(self):
        meeting = self._meeting()
        mark_held(meeting, self.manager)
        minutes = ensure_minutes(meeting, recorder=self.manager)
        return meeting, minutes

    @staticmethod
    def _audio():
        return SimpleUploadedFile(
            "meeting.webm",
            b"\x1a\x45\xdf\xa3" + b"0" * 39996,
            content_type="audio/webm;codecs=opus",
        )

    def test_editable_minutes_show_voice_and_ai_assistance(self):
        meeting, _minutes = self._held_meeting()
        self._enter(self.manager)

        response = self.client.get(reverse("reports:meeting_detail", args=[meeting.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "مساعد المحضر الذكي")
        self.assertContains(response, "سجّل مجريات الاجتماع بصوتك")
        self.assertContains(response, "تحسين الصياغة بالذكاء الاصطناعي")
        self.assertContains(response, reverse("reports:improve_meeting_minutes", args=[meeting.pk]))
        self.assertContains(
            response,
            reverse("reports:transcribe_meeting_minutes_voice", args=[meeting.pk]),
        )
        self.assertContains(response, "js/report-ai-improver.js")
        self.assertContains(response, "js/report-voice.js")

    @patch(
        "reports.views.meetings.improve_meeting_minutes_text",
        return_value="نوقشت الخطة، ثم اعتُمد تنفيذها خلال 5 أيام.",
    )
    def test_ai_returns_a_preview_without_saving_minutes(self, improve):
        meeting, minutes = self._held_meeting()
        minutes.body = "ناقشنا الخطة وقررنا تنفيذها خلال 5 أيام"
        minutes.save(update_fields=["body"])
        self._enter(self.manager)

        response = self.client.post(
            reverse("reports:improve_meeting_minutes", args=[meeting.pk]),
            data=json.dumps({"text": minutes.body}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["improved_text"],
            "نوقشت الخطة، ثم اعتُمد تنفيذها خلال 5 أيام.",
        )
        self.assertEqual(response.json()["remaining"], 2)
        minutes.refresh_from_db()
        self.assertEqual(minutes.body, "ناقشنا الخطة وقررنا تنفيذها خلال 5 أيام")
        improve.assert_called_once_with(minutes.body)

    @patch(
        "reports.views.meetings.polish_meeting_dictation",
        return_value="نوقشت نتائج الفصل، وأوصى المجتمعون بإعداد خطة تحسين.",
    )
    @patch(
        "reports.views.meetings.transcribe_meeting_audio",
        return_value="ناقشنا نتائج الفصل يعني وأوصينا بإعداد خطة تحسين",
    )
    def test_voice_recording_is_polished_as_minutes_without_being_saved(
        self, transcribe, polish
    ):
        meeting, minutes = self._held_meeting()
        self._enter(self.manager)

        response = self.client.post(
            reverse("reports:transcribe_meeting_minutes_voice", args=[meeting.pk]),
            {"audio": self._audio()},
            HTTP_X_TAWTHEEQ_SURFACE="standalone",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["text"],
            "نوقشت نتائج الفصل، وأوصى المجتمعون بإعداد خطة تحسين.",
        )
        self.assertEqual(response.json()["remaining"], 2)
        minutes.refresh_from_db()
        self.assertEqual(minutes.body, "")
        transcribe.assert_called_once()
        polish.assert_called_once_with("ناقشنا نتائج الفصل يعني وأوصينا بإعداد خطة تحسين")

    def test_an_invitee_cannot_use_the_minutes_assistant(self):
        meeting, _minutes = self._held_meeting()
        self._enter(self.staff)

        ai_response = self.client.post(
            reverse("reports:improve_meeting_minutes", args=[meeting.pk]),
            data=json.dumps({"text": "هذا نص محضر مكتمل يحتاج إلى تحسين الصياغة."}),
            content_type="application/json",
        )
        voice_response = self.client.post(
            reverse("reports:transcribe_meeting_minutes_voice", args=[meeting.pk]),
            {"audio": self._audio()},
            HTTP_X_TAWTHEEQ_SURFACE="standalone",
        )

        self.assertEqual(ai_response.status_code, 403)
        self.assertEqual(voice_response.status_code, 403)

    def test_plain_browser_cannot_use_minutes_voice_recording(self):
        meeting, _minutes = self._held_meeting()
        self._enter(self.manager)

        response = self.client.post(
            reverse("reports:transcribe_meeting_minutes_voice", args=[meeting.pk]),
            {"audio": self._audio()},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["reason"], "pwa_required")
