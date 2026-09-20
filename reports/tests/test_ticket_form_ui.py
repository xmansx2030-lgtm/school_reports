"""Presentation contracts for the two existing Ticket creation paths."""

from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from reports.models import (
    Department,
    DepartmentMembership,
    School,
    SchoolMembership,
    SchoolSubscription,
    SubscriptionPlan,
    Teacher,
    Ticket,
)


TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "templates" / "reports"


@override_settings(ALLOWED_HOSTS=["testserver"])
class TicketFormUITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = School.objects.create(name="مدرسة النماذج", code="ticket-forms")
        plan = SubscriptionPlan.objects.create(
            name="خطة النماذج", price=0, days_duration=30, max_teachers=0
        )
        SchoolSubscription.objects.create(school=cls.school, plan=plan)
        cls.teacher = Teacher.objects.create_user(
            phone="500092101", name="معلم النماذج", password="pass"
        )
        cls.manager = Teacher.objects.create_user(
            phone="500092102", name="مدير النماذج", password="pass", is_staff=True
        )
        SchoolMembership.objects.create(
            school=cls.school,
            teacher=cls.teacher,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        SchoolMembership.objects.create(
            school=cls.school,
            teacher=cls.manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )

    def _login(self, user):
        self.client.force_login(user)
        session = self.client.session
        session["active_school_id"] = self.school.pk
        session.save()

    def test_school_create_keeps_routing_upload_and_accessible_form(self):
        self._login(self.teacher)

        response = self.client.get(reverse("reports:request_create"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "reports/request_create.html")
        for snippet in (
            'id="ticket-form"',
            'enctype="multipart/form-data"',
            'name="department"',
            'id="id_recipients"',
            'data-members-url="/api/department-members/"',
            'name="images"',
            'id="submitBtn"',
            "css/ticket-form.css",
            'class="twq-page ticket-form-page ticket-page"',
        ):
            self.assertContains(response, snippet)
        self.assertNotIn("<style", (TEMPLATE_ROOT / "request_create.html").read_text(encoding="utf-8"))

    def test_school_create_validation_stays_visible_near_fields(self):
        self._login(self.teacher)

        response = self.client.post(reverse("reports:request_create"), data={})

        self.assertEqual(response.status_code, 200)
        self.assertIn("title", response.context["form"].errors)
        self.assertIn("body", response.context["form"].errors)
        self.assertIn("department", response.context["form"].errors)
        self.assertContains(response, 'aria-invalid="true"')
        self.assertContains(response, 'role="alert"')

    def test_support_create_uses_same_presentation_without_school_routing_fields(self):
        self._login(self.manager)

        response = self.client.get(reverse("reports:support_ticket_create"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "reports/support_ticket_create.html")
        for snippet in (
            'id="supportForm"',
            'enctype="multipart/form-data"',
            'name="attachment"',
            'id="attachPreview"',
            'id="submitBtn"',
            "css/ticket-form.css",
            'class="twq-page ticket-form-page ticket-form-page--support"',
        ):
            self.assertContains(response, snippet)
        self.assertNotContains(response, 'name="department"')
        self.assertNotIn(
            "<style", (TEMPLATE_ROOT / "support_ticket_create.html").read_text(encoding="utf-8")
        )

    def test_support_create_remains_manager_only(self):
        self._login(self.teacher)

        response = self.client.get(reverse("reports:support_ticket_create"))

        self.assertNotEqual(response.status_code, 200)

    def test_support_validation_remains_visible(self):
        self._login(self.manager)

        response = self.client.post(reverse("reports:support_ticket_create"), data={})

        self.assertEqual(response.status_code, 200)
        self.assertIn("title", response.context["form"].errors)
        # SupportTicketForm currently allows an empty body; the page must not
        # advertise a stricter rule than the live form contract.
        self.assertNotIn("body", response.context["form"].errors)
        self.assertContains(response, 'role="alert"')

    def test_school_create_still_redirects_to_my_requests(self):
        department = Department.objects.create(
            school=self.school, name="قسم تجريبي", slug="ticket-form-test"
        )
        DepartmentMembership.objects.create(
            department=department,
            teacher=self.manager,
            role_type=DepartmentMembership.TEACHER,
        )
        self._login(self.teacher)

        response = self.client.post(
            reverse("reports:request_create"),
            {
                "department": department.slug,
                "recipients": [str(self.manager.pk)],
                "title": "طلب تجربة النموذج",
                "body": "وصف الطلب التجريبي",
            },
        )

        self.assertRedirects(response, reverse("reports:my_requests"))
        self.assertTrue(
            Ticket.objects.filter(
                creator=self.teacher, school=self.school, title="طلب تجربة النموذج"
            ).exists()
        )

    def test_support_create_upload_still_redirects_to_support_list(self):
        self._login(self.manager)
        image = Image.new("RGB", (12, 12), "white")
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        attachment = SimpleUploadedFile(
            "test-evidence.png", buffer.getvalue(), content_type="image/png"
        )

        with TemporaryDirectory() as media_root, self.settings(MEDIA_ROOT=media_root):
            response = self.client.post(
                reverse("reports:support_ticket_create"),
                {
                    "title": "دعم تجريبي",
                    "body": "وصف للمشكلة التجريبية",
                    "attachment": attachment,
                },
            )

            self.assertRedirects(response, reverse("reports:my_support_tickets"))
            ticket = Ticket.objects.get(title="دعم تجريبي")
            self.assertEqual(ticket.creator, self.manager)
            self.assertEqual(ticket.school, self.school)
            self.assertTrue(ticket.attachment)
