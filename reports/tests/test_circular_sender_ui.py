from django.test import TestCase, override_settings
from django.template.loader import get_template
from django.urls import reverse

from reports.models import (
    Notification,
    NotificationRecipient,
    School,
    SchoolMembership,
    SchoolSubscription,
    SubscriptionPlan,
    Teacher,
)


@override_settings(ALLOWED_HOSTS=["testserver"])
class CircularSenderUiTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(name="مدرسة الإرسال", code="circular-sender-ui")
        self.other_school = School.objects.create(name="مدرسة أخرى", code="circular-sender-other")
        plan = SubscriptionPlan.objects.create(
            name="خطة الإرسال", price=0, days_duration=30, max_teachers=10
        )
        SchoolSubscription.objects.create(school=self.school, plan=plan)
        self.manager = Teacher.objects.create_user(
            phone="500790001", name="مدير الإرسال", password="pass", is_staff=True
        )
        self.recipient = Teacher.objects.create_user(
            phone="500790002", name="مستلم الإرسال", password="pass"
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.recipient,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        self.client.force_login(self.manager)
        session = self.client.session
        session["active_school_id"] = self.school.pk
        session.save()

    def test_composer_preserves_recipient_and_signature_form_contract(self):
        response = self.client.get(reverse("reports:circulars_create"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "css/circular-composer.css")
        self.assertContains(response, 'id="notifyForm"')
        self.assertContains(response, 'enctype="multipart/form-data"')
        for field in (
            "title", "message", "teachers", "target_department", "attachment",
            "requires_signature", "signature_deadline_at", "signature_ack_text",
        ):
            self.assertContains(response, f'name="{field}"')
        self.assertContains(response, "توقيع مرسوم")
        self.assertContains(response, "ملخص المستلمين")
        self.assertNotIn("<style", get_template("reports/send_circular.html").template.source)

    def test_platform_owner_sees_existing_audience_scope_fields(self):
        self.manager.is_superuser = True
        self.manager.save(update_fields=["is_superuser"])

        response = self.client.get(reverse("reports:circulars_create"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="audience_scope"')
        self.assertContains(response, 'name="target_school"')
        self.assertContains(response, "مدراء المدارس")

    def test_sent_list_uses_actual_read_and_signature_counts(self):
        signed = Notification.objects.create(
            kind=Notification.Kind.CIRCULAR,
            title="تعميم بتوقيع",
            message="النص",
            school=self.school,
            created_by=self.manager,
            requires_signature=True,
        )
        reading = Notification.objects.create(
            kind=Notification.Kind.CIRCULAR,
            title="تعميم للاطلاع",
            message="النص",
            school=self.school,
            created_by=self.manager,
            requires_signature=False,
        )
        NotificationRecipient.objects.create(
            notification=signed, teacher=self.recipient, is_read=True, is_signed=True
        )
        NotificationRecipient.objects.create(
            notification=reading, teacher=self.recipient, is_read=True
        )

        response = self.client.get(reverse("reports:circulars_sent"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "css/sent-circulars.css")
        self.assertContains(response, "تعميم بتوقيع")
        self.assertContains(response, "تعميم للاطلاع")
        self.assertContains(response, "أقرّوا بالتوقيع")
        self.assertContains(response, "للاطلاع فقط")
        self.assertEqual(response.content.decode().count("1/1"), 3)
        self.assertContains(response, reverse("reports:notification_detail", args=[signed.pk]))
        self.assertContains(response, reverse("reports:notification_delete", args=[signed.pk]))
        self.assertContains(response, 'class="js-delete-form sent-circulars__delete"')
        self.assertNotIn("<style", get_template("reports/circulars_sent.html").template.source)

    def test_sent_list_keeps_school_and_sender_scope(self):
        own = Notification.objects.create(
            kind=Notification.Kind.CIRCULAR,
            title="تعميم المدير",
            message="النص",
            school=self.school,
            created_by=self.manager,
        )
        Notification.objects.create(
            kind=Notification.Kind.CIRCULAR,
            title="تعميم مدرسة أخرى",
            message="النص",
            school=self.other_school,
            created_by=self.manager,
        )
        Notification.objects.create(
            kind=Notification.Kind.CIRCULAR,
            title="تعميم مرسل آخر",
            message="النص",
            school=self.school,
            created_by=self.recipient,
        )

        response = self.client.get(reverse("reports:circulars_sent"))

        self.assertContains(response, own.title)
        self.assertNotContains(response, "تعميم مدرسة أخرى")
        self.assertNotContains(response, "تعميم مرسل آخر")

    def test_recipient_and_other_school_manager_follow_sender_permissions(self):
        self.client.force_login(self.recipient)
        session = self.client.session
        session["active_school_id"] = self.school.pk
        session.save()
        self.assertNotEqual(
            self.client.get(reverse("reports:circulars_create")).status_code, 200
        )
        self.assertNotEqual(
            self.client.get(reverse("reports:circulars_sent")).status_code, 200
        )

        other_plan = SubscriptionPlan.objects.create(
            name="خطة المدرسة الأخرى", price=0, days_duration=30, max_teachers=10
        )
        SchoolSubscription.objects.create(school=self.other_school, plan=other_plan)
        other_manager = Teacher.objects.create_user(
            phone="500790003", name="مدير المدرسة الأخرى", password="pass", is_staff=True
        )
        SchoolMembership.objects.create(
            school=self.other_school,
            teacher=other_manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )
        Notification.objects.create(
            kind=Notification.Kind.CIRCULAR,
            title="تعميم المدرسة الأولى",
            message="النص",
            school=self.school,
            created_by=self.manager,
        )
        self.client.force_login(other_manager)
        session = self.client.session
        session["active_school_id"] = self.other_school.pk
        session.save()
        self.assertEqual(
            self.client.get(reverse("reports:circulars_create")).status_code, 200
        )
        self.assertNotContains(
            self.client.get(reverse("reports:circulars_sent")),
            "تعميم المدرسة الأولى",
        )

    def test_sent_list_retains_existing_page_navigation(self):
        for number in range(21):
            Notification.objects.create(
                kind=Notification.Kind.CIRCULAR,
                title=f"تعميم {number}",
                message="النص",
                school=self.school,
                created_by=self.manager,
            )

        first = self.client.get(reverse("reports:circulars_sent"))
        second = self.client.get(reverse("reports:circulars_sent"), {"page": "2"})

        self.assertContains(first, "?page=2")
        self.assertContains(first, "صفحة 1 من 2")
        self.assertContains(second, "?page=1")
        self.assertContains(second, "صفحة 2 من 2")
