# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.test import Client, SimpleTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from reports.model_parts.approvals import ApprovalState
from reports.models import (
    Assignment,
    Decision,
    Meeting,
    MeetingAgendaItem,
    MeetingAttendee,
    MeetingMinutes,
    SchoolMembership,
)
from reports.services_approval import ApprovalError, approve, return_for_changes
from reports.services_meetings import MeetingError, convert_decision_to_assignment
from reports.tests.test_meetings import MeetingBase, _school


class SchoolMeetingsPilotFrontendDebtTests(SimpleTestCase):
    core_templates = (
        "_meeting_theme.html",
        "meeting_list.html",
        "meeting_create.html",
        "meeting_detail.html",
        "partials/_meeting_copy_link.html",
    )

    def test_core_templates_have_no_embedded_or_inline_presentation_code(self):
        template_root = Path(settings.BASE_DIR) / "reports" / "templates" / "reports"
        for filename in self.core_templates:
            source = (template_root / filename).read_text(encoding="utf-8").lower()
            with self.subTest(filename=filename):
                self.assertNotIn("<style", source)
                self.assertNotIn(" style=", source)
                self.assertNotRegex(source, r"<script\b(?![^>]*\bsrc=)")

    def test_meetings_javascript_has_no_direct_style_writes(self):
        source = (Path(settings.BASE_DIR) / "static/js/meetings.js").read_text(
            encoding="utf-8"
        )

        self.assertNotIn(".style.", source)
        self.assertNotIn('setAttribute("style"', source)

    def test_meetings_css_uses_tokens_and_logical_properties(self):
        source = (Path(settings.BASE_DIR) / "static/css/meetings.css").read_text(
            encoding="utf-8"
        )
        lowered = source.lower()

        self.assertNotRegex(lowered, r"#[0-9a-f]{3,8}\b")
        self.assertNotRegex(lowered, r"\b(?:rgb|rgba|hsl|hsla)\(")
        self.assertNotRegex(
            lowered,
            r"\b(?:margin|padding|border)-(?:left|right)\b|\b(?:left|right)\s*:",
        )


class SchoolMeetingsPilotPerformanceTests(MeetingBase):
    def setUp(self):
        super().setUp()
        for index in range(6):
            meeting = self._meeting(
                title=f"اجتماع قياس {index}",
                scheduled_at=timezone.now() + timedelta(days=index + 1),
            )
            MeetingAgendaItem.objects.create(
                meeting=meeting,
                order=1,
                title=f"بند {index}",
            )
            Decision.objects.create(
                meeting=meeting,
                order=1,
                title=f"قرار {index}",
            )
            if index % 2:
                meeting.status = Meeting.Status.HELD
                meeting.held_at = timezone.now()
                meeting.save(update_fields=["status", "held_at"])
                MeetingMinutes.objects.create(
                    meeting=meeting,
                    recorder=self.manager,
                    body=f"محضر {index}",
                )

        self.client.force_login(self.manager)
        session = self.client.session
        session["active_school_id"] = self.school.pk
        session.save()

    def _count(self, method, url, data=None):
        cache.clear()
        with CaptureQueriesContext(connection) as captured:
            response = getattr(self.client, method)(url, data or {})
            self.assertLess(response.status_code, 400)
        return len(captured)

    def test_meeting_query_profile(self):
        detail = Meeting.objects.filter(
            school=self.school,
            status=Meeting.Status.SCHEDULED,
        ).order_by("pk").first()
        attendance_meeting = Meeting.objects.filter(
            school=self.school,
            status=Meeting.Status.HELD,
        ).order_by("pk").first()
        attendee = attendance_meeting.attendees.first()
        list_count = self._count("get", reverse("reports:meeting_list"))
        detail_count = self._count(
            "get", reverse("reports:meeting_detail", args=[detail.pk])
        )
        attendance_count = self._count(
            "post",
            reverse("reports:meeting_action", args=[attendance_meeting.pk]),
            {
                "meeting_action": "attendance",
                f"attendance_{attendee.pk}": MeetingAttendee.Status.PRESENT,
            },
        )
        print(
            "MEETING_QUERY_COUNTS "
            f"list={list_count} detail={detail_count} attendance={attendance_count}"
        )


class SchoolMeetingsPilotSecurityGapTests(MeetingBase):
    def _enter(self, user, school):
        self.client.force_login(user)
        session = self.client.session
        session["active_school_id"] = school.pk
        session.save()

    def test_sensitive_meeting_action_requires_csrf(self):
        meeting = self._meeting()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.manager)
        session = client.session
        session["active_school_id"] = self.school.pk
        session.save()

        response = client.post(
            reverse("reports:meeting_action", args=[meeting.pk]),
            {"meeting_action": "mark_held"},
        )
        meeting.refresh_from_db()

        self.assertEqual(response.status_code, 403)
        self.assertEqual(meeting.status, Meeting.Status.SCHEDULED)

    def test_organizer_cannot_open_meeting_from_another_active_school(self):
        other_school = _school("مدرسة أخرى", "mtg-other-school")
        SchoolMembership.objects.create(
            school=other_school,
            teacher=self.manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )
        meeting = self._meeting(organizer=self.manager)
        self._enter(self.manager, other_school)

        response = self.client.get(
            reverse("reports:meeting_detail", args=[meeting.pk])
        )

        self.assertEqual(response.status_code, 404)

    def test_all_object_routes_deny_wrong_active_school(self):
        other_school = _school("مدرسة النطاق الآخر", "mtg-route-scope")
        SchoolMembership.objects.create(
            school=other_school,
            teacher=self.manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )
        meeting = self._meeting(
            organizer=self.manager,
            status=Meeting.Status.HELD,
            held_at=timezone.now(),
        )
        minutes = MeetingMinutes.objects.create(
            meeting=meeting,
            recorder=self.staff,
            body="محضر",
            approval_state=ApprovalState.SUBMITTED,
        )
        decision = Decision.objects.create(
            meeting=meeting,
            title="قرار",
            responsible=self.staff,
            due_at=timezone.now() + timedelta(days=3),
        )
        self._enter(self.manager, other_school)

        routes = (
            ("get", reverse("reports:meeting_detail", args=[meeting.pk]), None),
            ("get", reverse("reports:meeting_print", args=[meeting.pk]), None),
            ("get", reverse("reports:meeting_pdf", args=[meeting.pk]), None),
            (
                "post",
                reverse("reports:meeting_action", args=[meeting.pk]),
                {"meeting_action": "track_decision", "decision_id": decision.pk},
            ),
            (
                "post",
                reverse("reports:minutes_approval_action", args=[meeting.pk]),
                {"approval_action": "approve"},
            ),
        )
        for method, url, data in routes:
            with self.subTest(url=url):
                response = getattr(self.client, method)(url, data or {})
                self.assertEqual(response.status_code, 404)

        with patch(
            "reports.views.meetings._meeting_ai_feature_enabled", return_value=True
        ):
            response = self.client.post(
                reverse("reports:improve_meeting_minutes", args=[meeting.pk]),
                data=json.dumps({"text": "نص"}),
                content_type="application/json",
            )
            self.assertEqual(response.status_code, 404)

        with patch(
            "reports.views.meetings._meeting_voice_feature_enabled", return_value=True
        ):
            response = self.client.post(
                reverse("reports:transcribe_meeting_minutes_voice", args=[meeting.pk]),
                {},
            )
            self.assertEqual(response.status_code, 404)

        minutes.refresh_from_db()
        decision.refresh_from_db()
        self.assertEqual(minutes.approval_state, ApprovalState.SUBMITTED)
        self.assertIsNone(decision.assignment_id)
        self.assertEqual(Assignment.objects.count(), 0)

    def test_invitee_and_foreign_manager_cannot_cross_active_school(self):
        other_school = _school("مدرسة المدعو", "mtg-invitee-scope")
        SchoolMembership.objects.create(
            school=other_school,
            teacher=self.staff,
            role_type=SchoolMembership.RoleType.ADMIN_STAFF,
        )
        meeting = self._meeting()
        self._enter(self.staff, other_school)
        response = self.client.get(reverse("reports:meeting_detail", args=[meeting.pk]))
        self.assertEqual(response.status_code, 404)

        other_manager = self.manager.__class__.objects.create_user(
            phone="0500050099", name="مدير المدرسة الأخرى", password="Passw0rd!123"
        )
        SchoolMembership.objects.create(
            school=other_school,
            teacher=other_manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )
        self._enter(other_manager, other_school)
        response = self.client.get(reverse("reports:meeting_detail", args=[meeting.pk]))
        self.assertEqual(response.status_code, 404)

    def test_attendance_cannot_be_recorded_before_meeting_is_held(self):
        meeting = self._meeting()
        attendee = meeting.attendees.first()
        self._enter(self.manager, self.school)

        self.client.post(
            reverse("reports:meeting_action", args=[meeting.pk]),
            {
                "meeting_action": "attendance",
                f"attendance_{attendee.pk}": MeetingAttendee.Status.PRESENT,
            },
        )
        attendee.refresh_from_db()

        self.assertEqual(attendee.status, MeetingAttendee.Status.INVITED)

    def test_attendance_is_allowed_only_for_held_meetings(self):
        for status in (Meeting.Status.HELD, Meeting.Status.CANCELLED):
            with self.subTest(status=status):
                meeting = self._meeting(
                    status=status,
                    held_at=timezone.now() if status == Meeting.Status.HELD else None,
                )
                attendee = meeting.attendees.first()
                self._enter(self.manager, self.school)
                self.client.post(
                    reverse("reports:meeting_action", args=[meeting.pk]),
                    {
                        "meeting_action": "attendance",
                        f"attendance_{attendee.pk}": MeetingAttendee.Status.PRESENT,
                    },
                )
                attendee.refresh_from_db()
                expected = (
                    MeetingAttendee.Status.PRESENT
                    if status == Meeting.Status.HELD
                    else MeetingAttendee.Status.INVITED
                )
                self.assertEqual(attendee.status, expected)

    def test_minutes_cannot_be_created_before_meeting_is_held(self):
        meeting = self._meeting()
        self._enter(self.manager, self.school)

        self.client.post(
            reverse("reports:meeting_action", args=[meeting.pk]),
            {
                "meeting_action": "save_minutes",
                "format_mode": MeetingMinutes.FormatMode.FREEFORM,
                "body": "محضر سابق للانعقاد",
            },
        )

        self.assertFalse(MeetingMinutes.objects.filter(meeting=meeting).exists())

    def test_minutes_cannot_be_created_for_cancelled_meeting(self):
        meeting = self._meeting(status=Meeting.Status.CANCELLED)
        self._enter(self.manager, self.school)
        self.client.post(
            reverse("reports:meeting_action", args=[meeting.pk]),
            {
                "meeting_action": "save_minutes",
                "format_mode": MeetingMinutes.FormatMode.FREEFORM,
                "body": "محضر لاجتماع ملغى",
            },
        )
        self.assertFalse(MeetingMinutes.objects.filter(meeting=meeting).exists())

    def test_decisions_cannot_be_recorded_before_meeting_is_held(self):
        meeting = self._meeting()
        self._enter(self.manager, self.school)

        self.client.post(
            reverse("reports:meeting_action", args=[meeting.pk]),
            {
                "meeting_action": "add_decision",
                "kind": Decision.Kind.DECISION,
                "title": "قرار سابق للانعقاد",
                "body": "",
                "agenda_item": "",
                "responsible": "",
                "due_at": "",
            },
        )

        self.assertFalse(Decision.objects.filter(meeting=meeting).exists())

    def test_decisions_cannot_be_recorded_for_cancelled_meeting(self):
        meeting = self._meeting(status=Meeting.Status.CANCELLED)
        self._enter(self.manager, self.school)
        self.client.post(
            reverse("reports:meeting_action", args=[meeting.pk]),
            {
                "meeting_action": "add_decision",
                "kind": Decision.Kind.DECISION,
                "title": "قرار لاجتماع ملغى",
            },
        )
        self.assertFalse(Decision.objects.filter(meeting=meeting).exists())

    def test_agenda_items_cannot_be_deleted_after_meeting_is_held(self):
        meeting = self._meeting(status=Meeting.Status.HELD, held_at=timezone.now())
        item = MeetingAgendaItem.objects.create(
            meeting=meeting,
            order=1,
            title="بند محفوظ في المحضر",
        )
        self._enter(self.manager, self.school)

        self.client.post(
            reverse("reports:meeting_action", args=[meeting.pk]),
            {
                "meeting_action": "remove_agenda",
                "item_id": item.pk,
            },
        )

        self.assertTrue(MeetingAgendaItem.objects.filter(pk=item.pk).exists())

    def test_agenda_add_and_delete_follow_scheduled_only_contract(self):
        scheduled = self._meeting()
        item = MeetingAgendaItem.objects.create(
            meeting=scheduled, order=1, title="بند قابل للحذف"
        )
        self._enter(self.manager, self.school)
        self.client.post(
            reverse("reports:meeting_action", args=[scheduled.pk]),
            {"meeting_action": "remove_agenda", "item_id": item.pk},
        )
        self.assertFalse(MeetingAgendaItem.objects.filter(pk=item.pk).exists())

        for status in (Meeting.Status.HELD, Meeting.Status.CANCELLED):
            with self.subTest(status=status):
                meeting = self._meeting(
                    status=status,
                    held_at=timezone.now() if status == Meeting.Status.HELD else None,
                )
                self.client.post(
                    reverse("reports:meeting_action", args=[meeting.pk]),
                    {
                        "meeting_action": "add_agenda",
                        "title": "بند متأخر",
                        "note": "",
                    },
                )
                self.assertFalse(meeting.agenda_items.filter(title="بند متأخر").exists())

    def test_agenda_controls_are_visible_only_while_scheduled(self):
        self._enter(self.manager, self.school)
        for status in (
            Meeting.Status.SCHEDULED,
            Meeting.Status.HELD,
            Meeting.Status.CANCELLED,
        ):
            with self.subTest(status=status):
                meeting = self._meeting(
                    status=status,
                    held_at=timezone.now() if status == Meeting.Status.HELD else None,
                )
                MeetingAgendaItem.objects.create(
                    meeting=meeting,
                    order=1,
                    title="بند ظاهر حسب الحالة",
                )
                response = self.client.get(
                    reverse("reports:meeting_detail", args=[meeting.pk])
                )
                controls = (
                    b'value="add_agenda"' in response.content
                    and b'value="remove_agenda"' in response.content
                )
                self.assertEqual(
                    controls,
                    status == Meeting.Status.SCHEDULED,
                )

    def test_decision_cannot_be_converted_before_meeting_is_held(self):
        meeting = self._meeting()
        decision = Decision.objects.create(
            meeting=meeting,
            title="قرار سابق للانعقاد",
            responsible=self.staff,
            due_at=timezone.now() + timedelta(days=5),
        )

        with self.assertRaises(MeetingError):
            convert_decision_to_assignment(decision, self.manager)

    def test_decision_conversion_rejects_cancelled_meeting_and_foreign_responsible(self):
        cancelled = self._meeting(status=Meeting.Status.CANCELLED)
        cancelled_decision = Decision.objects.create(
            meeting=cancelled,
            title="قرار ملغى",
            responsible=self.staff,
            due_at=timezone.now() + timedelta(days=5),
        )
        with self.assertRaises(MeetingError):
            convert_decision_to_assignment(cancelled_decision, self.manager)

        held = self._meeting(status=Meeting.Status.HELD, held_at=timezone.now())
        outsider = self.manager.__class__.objects.create_user(
            phone="0500050098", name="مسؤول غير مدعو", password="Passw0rd!123"
        )
        foreign_responsible = Decision.objects.create(
            meeting=held,
            title="قرار لمسؤول غير مدعو",
            responsible=outsider,
            due_at=timezone.now() + timedelta(days=5),
        )
        with self.assertRaises(MeetingError):
            convert_decision_to_assignment(foreign_responsible, self.manager)
        self.assertEqual(Assignment.objects.count(), 0)

    def test_stale_minutes_transition_cannot_overwrite_current_state(self):
        meeting = self._meeting(status=Meeting.Status.HELD, held_at=timezone.now())
        minutes = MeetingMinutes.objects.create(
            meeting=meeting,
            recorder=self.staff,
            body="محضر صالح",
            approval_state=ApprovalState.SUBMITTED,
        )
        stale = MeetingMinutes.objects.select_related("meeting").get(pk=minutes.pk)
        MeetingMinutes.objects.filter(pk=minutes.pk).update(
            approval_state=ApprovalState.APPROVED
        )

        with self.assertRaises(ApprovalError):
            return_for_changes(
                stale,
                self.manager,
                school=self.school,
                note="إعادة قديمة",
            )
        minutes.refresh_from_db()

        self.assertEqual(minutes.approval_state, ApprovalState.APPROVED)

    def test_stale_approve_cannot_overwrite_current_state(self):
        meeting = self._meeting(status=Meeting.Status.HELD, held_at=timezone.now())
        minutes = MeetingMinutes.objects.create(
            meeting=meeting,
            recorder=self.staff,
            body="محضر صالح",
            approval_state=ApprovalState.SUBMITTED,
        )
        stale = MeetingMinutes.objects.select_related("meeting").get(pk=minutes.pk)
        MeetingMinutes.objects.filter(pk=minutes.pk).update(
            approval_state=ApprovalState.RETURNED
        )

        with self.assertRaises(ApprovalError):
            approve(stale, self.manager, school=self.school)
        minutes.refresh_from_db()
        self.assertEqual(minutes.approval_state, ApprovalState.RETURNED)

    def test_minutes_approval_requires_a_held_meeting(self):
        meeting = self._meeting()
        minutes = MeetingMinutes.objects.create(
            meeting=meeting,
            recorder=self.staff,
            body="سجل قديم لمحضر سابق للانعقاد",
            approval_state=ApprovalState.SUBMITTED,
        )

        with self.assertRaises(ApprovalError):
            approve(minutes, self.manager, school=self.school)
        minutes.refresh_from_db()
        self.assertEqual(minutes.approval_state, ApprovalState.SUBMITTED)
