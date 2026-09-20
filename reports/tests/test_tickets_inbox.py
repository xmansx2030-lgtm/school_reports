import html
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from django.conf import settings
from django.test import TestCase, override_settings
from django.urls import reverse

from reports.models import (
    Department,
    DepartmentMembership,
    School,
    SchoolMembership,
    SchoolSubscription,
    SubscriptionPlan,
    Teacher,
    Ticket,
    TicketNote,
)


@override_settings(ALLOWED_HOSTS=["testserver"])
class TicketsInboxViewTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(name="Inbox School", code="inbox-school")
        plan = SubscriptionPlan.objects.create(
            name="Inbox Plan",
            price=0,
            days_duration=30,
            max_teachers=0,
        )
        SchoolSubscription.objects.create(school=self.school, plan=plan)
        self.user = Teacher.objects.create_user(
            phone="500000401",
            name="Inbox Teacher",
            password="pass",
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.user,
            role_type=SchoolMembership.RoleType.MANAGER,
        )

    def test_tickets_inbox_mine_filter_keeps_only_directly_visible_tickets(self):
        other_user = Teacher.objects.create_user(
            phone="500000402",
            name="Inbox Other",
            password="pass",
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=other_user,
            role_type=SchoolMembership.RoleType.TEACHER,
        )

        Ticket.objects.create(
            creator=other_user,
            assignee=self.user,
            school=self.school,
            is_platform=False,
            title="صندوق الوارد - مسندة لي",
            body="هذه التذكرة يجب أن تظهر مع mine=1.",
            status=Ticket.Status.DONE,
        )
        Ticket.objects.create(
            creator=other_user,
            assignee=None,
            school=self.school,
            is_platform=False,
            title="صندوق الوارد - لقسمي فقط",
            body="هذه التذكرة تظهر للمدير في inbox العادي لكن يجب استبعادها مع mine=1.",
            status=Ticket.Status.DONE,
        )
        Ticket.objects.create(
            creator=other_user,
            assignee=self.user,
            school=self.school,
            is_platform=True,
            title="صندوق الوارد - دعم منصي",
            body="هذه التذكرة تطابق البحث لكنها يجب أن تُستبعد.",
            status=Ticket.Status.DONE,
        )

        self.client.force_login(self.user)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()

        response = self.client.get(
            reverse("reports:tickets_inbox"),
            {"q": "صندوق الوارد", "status": Ticket.Status.DONE, "mine": "1"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "صندوق الوارد - مسندة لي")
        self.assertNotContains(response, "صندوق الوارد - لقسمي فقط")
        self.assertNotContains(response, "صندوق الوارد - دعم منصي")
        self.assertEqual(len(response.context["tickets"]), 1)

    def test_tickets_inbox_filters_do_not_escape_active_school_scope(self):
        other_school = School.objects.create(name="Inbox Other School", code="inbox-other-school")
        plan = SubscriptionPlan.objects.create(
            name="Inbox Other Plan",
            price=0,
            days_duration=30,
            max_teachers=0,
        )
        SchoolSubscription.objects.create(school=other_school, plan=plan)
        SchoolMembership.objects.create(
            school=other_school,
            teacher=self.user,
            role_type=SchoolMembership.RoleType.MANAGER,
        )

        Ticket.objects.create(
            creator=self.user,
            assignee=self.user,
            school=self.school,
            is_platform=False,
            title="صندوق المدرسة - الحالية",
            body="يجب أن تظهر داخل المدرسة النشطة.",
            status=Ticket.Status.DONE,
        )
        Ticket.objects.create(
            creator=self.user,
            assignee=self.user,
            school=other_school,
            is_platform=False,
            title="صندوق المدرسة - الأخرى",
            body="تطابق نفس q وstatus لكنها خارج المدرسة النشطة.",
            status=Ticket.Status.DONE,
        )

        self.client.force_login(self.user)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()

        response = self.client.get(
            reverse("reports:tickets_inbox"),
            {"q": "صندوق المدرسة", "status": Ticket.Status.DONE, "mine": "1"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "صندوق المدرسة - الحالية")
        self.assertNotContains(response, "صندوق المدرسة - الأخرى")
        self.assertEqual(len(response.context["tickets"]), 1)

    def test_tickets_inbox_includes_tickets_when_user_is_in_recipients(self):
        other_user = Teacher.objects.create_user(
            phone="500000403",
            name="Inbox Recipient Owner",
            password="pass",
        )
        third_user = Teacher.objects.create_user(
            phone="500000404",
            name="Inbox Recipient Other",
            password="pass",
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=other_user,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=third_user,
            role_type=SchoolMembership.RoleType.TEACHER,
        )

        visible_ticket = Ticket.objects.create(
            creator=other_user,
            assignee=other_user,
            school=self.school,
            is_platform=False,
            title="صندوق المستلمين - أظهرني",
            body="هذه التذكرة يجب أن تظهر لأن المستخدم الحالي ضمن recipients.",
            status=Ticket.Status.DONE,
        )
        visible_ticket.recipients.add(self.user)

        Ticket.objects.create(
            creator=other_user,
            assignee=third_user,
            school=self.school,
            is_platform=False,
            title="صندوق المستلمين - لا أظهر",
            body="هذه التذكرة مشابهة لكنها لا تحتوي المستخدم الحالي ضمن recipients.",
            status=Ticket.Status.DONE,
        )

        self.client.force_login(self.user)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()

        response = self.client.get(
            reverse("reports:tickets_inbox"),
            {"q": "صندوق المستلمين", "status": Ticket.Status.DONE, "mine": "1"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "صندوق المستلمين - أظهرني")
        self.assertNotContains(response, "صندوق المستلمين - لا أظهر")
        self.assertEqual(len(response.context["tickets"]), 1)

    def test_tickets_inbox_department_tickets_show_without_mine_and_hide_with_mine(self):
        staff_user = Teacher.objects.create_user(
            phone="500000405",
            name="Inbox Staff User",
            password="pass",
            is_staff=True,
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=staff_user,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        other_user = Teacher.objects.create_user(
            phone="500000406",
            name="Inbox Department Owner",
            password="pass",
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=other_user,
            role_type=SchoolMembership.RoleType.TEACHER,
        )

        user_department = Department.objects.create(
            school=self.school,
            name="قسم التقنية",
            slug="tickets-inbox-tech",
        )
        other_department = Department.objects.create(
            school=self.school,
            name="قسم الإدارة",
            slug="tickets-inbox-admin",
        )
        DepartmentMembership.objects.create(
            department=user_department,
            teacher=staff_user,
            role_type=DepartmentMembership.TEACHER,
        )

        Ticket.objects.create(
            creator=other_user,
            assignee=None,
            department=user_department,
            school=self.school,
            is_platform=False,
            title="صندوق الأقسام - يخص قسمي",
            body="يجب أن تظهر هذه التذكرة بدون mine لأنها ضمن قسم المستخدم.",
            status=Ticket.Status.DONE,
        )
        Ticket.objects.create(
            creator=other_user,
            assignee=None,
            department=other_department,
            school=self.school,
            is_platform=False,
            title="صندوق الأقسام - قسم آخر",
            body="هذه التذكرة لقسم آخر ويجب ألا تظهر.",
            status=Ticket.Status.DONE,
        )

        self.client.force_login(staff_user)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()

        response = self.client.get(
            reverse("reports:tickets_inbox"),
            {"q": "صندوق الأقسام", "status": Ticket.Status.DONE},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "صندوق الأقسام - يخص قسمي")
        self.assertNotContains(response, "صندوق الأقسام - قسم آخر")
        self.assertEqual(len(response.context["tickets"]), 1)

        mine_response = self.client.get(
            reverse("reports:tickets_inbox"),
            {"q": "صندوق الأقسام", "status": Ticket.Status.DONE, "mine": "1"},
        )

        self.assertEqual(mine_response.status_code, 200)
        self.assertNotContains(mine_response, "صندوق الأقسام - يخص قسمي")
        self.assertNotContains(mine_response, "صندوق الأقسام - قسم آخر")
        self.assertEqual(len(mine_response.context["tickets"]), 0)

    def test_tickets_inbox_invalid_status_is_ignored_without_breaking_page(self):
        Ticket.objects.create(
            creator=self.user,
            assignee=self.user,
            school=self.school,
            is_platform=False,
            title="حالة غير صالحة - مفتوحة",
            body="يجب أن تبقى ظاهرة مع status غير صالح.",
            status=Ticket.Status.OPEN,
        )
        Ticket.objects.create(
            creator=self.user,
            assignee=self.user,
            school=self.school,
            is_platform=False,
            title="حالة غير صالحة - مكتملة",
            body="يجب أن تبقى ظاهرة أيضًا مع status غير صالح.",
            status=Ticket.Status.DONE,
        )

        self.client.force_login(self.user)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()

        response = self.client.get(
            reverse("reports:tickets_inbox"),
            {"q": "حالة غير صالحة", "status": "bad-status", "mine": "1"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "حالة غير صالحة - مفتوحة")
        self.assertContains(response, "حالة غير صالحة - مكتملة")
        self.assertEqual(len(response.context["tickets"]), 2)

    def test_tickets_inbox_empty_status_is_ignored_without_breaking_page(self):
        Ticket.objects.create(
            creator=self.user,
            assignee=self.user,
            school=self.school,
            is_platform=False,
            title="حالة فارغة - قيد المعالجة",
            body="يجب أن تبقى ظاهرة مع status فارغ.",
            status=Ticket.Status.IN_PROGRESS,
        )
        Ticket.objects.create(
            creator=self.user,
            assignee=self.user,
            school=self.school,
            is_platform=False,
            title="حالة فارغة - مرفوضة",
            body="يجب أن تبقى ظاهرة أيضًا مع status فارغ.",
            status=Ticket.Status.REJECTED,
        )

        self.client.force_login(self.user)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()

        response = self.client.get(
            reverse("reports:tickets_inbox"),
            {"q": "حالة فارغة", "status": "", "mine": "1"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "حالة فارغة - قيد المعالجة")
        self.assertContains(response, "حالة فارغة - مرفوضة")
        self.assertEqual(len(response.context["tickets"]), 2)

    def test_tickets_inbox_clear_button_resets_q_status_and_mine(self):
        Ticket.objects.create(
            creator=self.user,
            assignee=self.user,
            school=self.school,
            is_platform=False,
            title="زر المسح - صندوق الوارد",
            body="تذكرة لاختبار رابط زر المسح.",
            status=Ticket.Status.DONE,
        )

        self.client.force_login(self.user)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()

        response = self.client.get(
            reverse("reports:tickets_inbox"),
            {"q": "زر المسح", "status": Ticket.Status.DONE, "mine": "1"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "مسح")
        self.assertContains(
            response,
            f'href="{reverse("reports:tickets_inbox")}" class="twq-btn twq-btn--secondary"',
        )

    def test_tickets_inbox_clear_button_is_hidden_without_active_filters(self):
        Ticket.objects.create(
            creator=self.user,
            assignee=self.user,
            school=self.school,
            is_platform=False,
            title="بدون مسح - صندوق الوارد",
            body="تذكرة لاختبار اختفاء زر المسح.",
            status=Ticket.Status.OPEN,
        )

        self.client.force_login(self.user)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()

        response = self.client.get(reverse("reports:tickets_inbox"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, ">مسح<", html=False)

    def test_inbox_pilot_keeps_desktop_mobile_status_and_detail_links(self):
        ticket = Ticket.objects.create(
            creator=self.user,
            assignee=self.user,
            school=self.school,
            is_platform=False,
            title="طلب وارد للمتابعة",
            body="تفاصيل الطلب",
            status=Ticket.Status.IN_PROGRESS,
        )
        self._enter_school()

        response = self.client.get(reverse("reports:tickets_inbox"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "css/requests-inbox.css")
        self.assertContains(response, 'class="twq-table requests-inbox__table"')
        self.assertContains(response, 'class="requests-inbox__cards"')
        self.assertContains(response, "twq-status--warning", count=2)
        self.assertContains(response, reverse("reports:ticket_detail", args=[ticket.pk]))
        source = (Path(settings.BASE_DIR) / "reports/templates/reports/tickets_inbox.html").read_text(encoding="utf-8")
        self.assertNotIn("<style", source)

    def test_manager_inbox_pagination_preserves_search_status_and_mine(self):
        Ticket.objects.bulk_create(
            [
                Ticket(
                    creator=self.user,
                    assignee=self.user,
                    school=self.school,
                    is_platform=False,
                    title=f"متابعة وارد {index}",
                    body="تفاصيل البحث",
                    status=Ticket.Status.OPEN,
                )
                for index in range(26)
            ]
        )
        self._enter_school()

        response = self.client.get(
            reverse("reports:manager_school_tickets"),
            {"q": "متابعة وارد", "status": Ticket.Status.OPEN, "mine": "1"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "twq-toolbar__active-filters")
        next_link = re.search(r'<a[^>]+href="([^"]+)"[^>]*>التالي</a>', response.content.decode())
        self.assertIsNotNone(next_link)
        query = parse_qs(urlsplit(html.unescape(next_link.group(1))).query)
        self.assertEqual(query, {"page": ["2"], "q": ["متابعة وارد"], "status": ["open"], "mine": ["1"]})
        self.assertEqual(response.context["tickets"].paginator.num_pages, 2)

    def _closed_ticket(self):
        return Ticket.objects.create(
            creator=self.user,
            assignee=self.user,
            school=self.school,
            is_platform=False,
            title="طلب مغلق للاختبار",
            body="يختبر مسار إعادة الفتح الصريح.",
            status=Ticket.Status.DONE,
        )

    def _enter_school(self):
        self.client.force_login(self.user)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()

    def test_closed_ticket_shows_one_explicit_reopen_action(self):
        ticket = self._closed_ticket()
        self._enter_school()

        response = self.client.get(reverse("reports:ticket_detail", args=[ticket.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "إعادة فتح الطلب")
        self.assertNotContains(response, "حفظ التحديث")
        self.assertNotContains(response, 'name="status"')

    def test_detail_uses_request_workspace_and_preserves_post_contract(self):
        ticket = self._closed_ticket()
        ticket.status = Ticket.Status.IN_PROGRESS
        ticket.save(update_fields=["status"])
        self._enter_school()

        response = self.client.get(reverse("reports:ticket_detail", args=[ticket.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "css/ticket-detail.css")
        self.assertContains(response, 'class="twq-page ticket-detail"')
        self.assertContains(response, 'id="ticketDetailTitle"')
        self.assertContains(response, "twq-status--warning")
        self.assertContains(response, "قيد المعالجة")
        self.assertContains(response, 'id="ticketStatus" name="status"')
        self.assertContains(response, 'id="ticketActionNote" name="note"')
        self.assertContains(response, 'id="ticketDirectLink"')
        self.assertContains(response, 'id="lightbox"')
        self.assertContains(response, reverse("reports:ticket_print", args=[ticket.pk]))
        detail_source = (Path(settings.BASE_DIR) / "reports/templates/reports/ticket_detail.html").read_text(encoding="utf-8")
        self.assertNotIn("<style", detail_source)

    def test_detail_closed_state_exposes_reopen_but_no_reply_field(self):
        ticket = self._closed_ticket()
        self._enter_school()

        response = self.client.get(reverse("reports:ticket_detail", args=[ticket.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "twq-status--success")
        self.assertContains(response, 'name="ticket_action" value="reopen"')
        self.assertNotContains(response, 'name="note"')

    def test_detail_thread_keeps_author_date_content_and_edit_route(self):
        ticket = self._closed_ticket()
        ticket.status = Ticket.Status.OPEN
        ticket.save(update_fields=["status"])
        note = TicketNote.objects.create(ticket=ticket, author=self.user, body="رد تجريبي على الطلب")
        self._enter_school()

        response = self.client.get(reverse("reports:ticket_detail", args=[ticket.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "رد تجريبي على الطلب")
        self.assertContains(response, self.user.name)
        self.assertContains(response, 'class="ticket-detail__thread"')
        self.assertContains(response, reverse("reports:ticket_note_edit", args=[note.pk]))
        self.assertContains(response, 'datetime="')

    def test_closed_ticket_cannot_be_reopened_through_generic_status_post(self):
        ticket = self._closed_ticket()
        self._enter_school()

        self.client.post(
            reverse("reports:ticket_detail", args=[ticket.pk]),
            {"status": Ticket.Status.OPEN},
            follow=True,
        )

        ticket.refresh_from_db()
        self.assertEqual(ticket.status, Ticket.Status.DONE)

    def test_closed_ticket_can_be_reopened_through_explicit_action(self):
        ticket = self._closed_ticket()
        self._enter_school()

        response = self.client.post(
            reverse("reports:ticket_detail", args=[ticket.pk]),
            {"ticket_action": "reopen"},
            follow=True,
        )

        ticket.refresh_from_db()
        self.assertEqual(ticket.status, Ticket.Status.OPEN)
        self.assertContains(response, "أُعيد فتح الطلب")
        self.assertTrue(ticket.notes.filter(body__startswith="إعادة فتح الطلب:").exists())

    def test_closing_ticket_saves_resolution_note_in_same_request(self):
        ticket = Ticket.objects.create(
            creator=self.user,
            assignee=self.user,
            school=self.school,
            is_platform=False,
            title="طلب مفتوح مع خلاصة حل",
            body="يختبر حفظ الملاحظة عند إكمال الطلب.",
            status=Ticket.Status.IN_PROGRESS,
        )
        self._enter_school()

        self.client.post(
            reverse("reports:ticket_detail", args=[ticket.pk]),
            {"status": Ticket.Status.DONE, "note": "تم تنفيذ الحل والتحقق منه."},
            follow=True,
        )

        ticket.refresh_from_db()
        self.assertEqual(ticket.status, Ticket.Status.DONE)
        self.assertTrue(ticket.notes.filter(body="تم تنفيذ الحل والتحقق منه.").exists())
