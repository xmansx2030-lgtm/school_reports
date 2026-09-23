from django.test import TestCase, override_settings
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
class MyNotificationsReadBehaviorTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(name="مدرسة", code="notif-read-school")
        self.plan = SubscriptionPlan.objects.create(
            name="Plan", price=0, days_duration=30, max_teachers=0
        )
        SchoolSubscription.objects.create(school=self.school, plan=self.plan)
        self.user = Teacher.objects.create_user(
            phone="500909090", name="معلم", password="pass"
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.user,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        self.n = Notification.objects.create(
            title="إشعار", message="نص", requires_signature=False, school=self.school
        )
        self.rec = NotificationRecipient.objects.create(
            notification=self.n, teacher=self.user
        )
        self.client.force_login(self.user)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()

    def test_opening_list_does_not_mark_read(self):
        resp = self.client.get(reverse("reports:my_notifications"))
        self.assertEqual(resp.status_code, 200)
        self.rec.refresh_from_db()
        self.assertFalse(self.rec.is_read)  # يبقى غير مقروء بعد فتح القائمة

    def test_opening_detail_marks_read(self):
        self.client.get(
            reverse("reports:my_notification_detail", args=[self.rec.pk])
        )
        self.rec.refresh_from_db()
        self.assertTrue(self.rec.is_read)  # يُعلَّم مقروءًا عند فتح التفاصيل

    def test_mark_one_endpoint_marks_read(self):
        self.client.post(
            reverse("reports:notification_mark_read", args=[self.rec.pk])
        )
        self.rec.refresh_from_db()
        self.assertTrue(self.rec.is_read)

    def _add_second_school(self):
        school = School.objects.create(name="مدرسة أخرى", code="notif-other-school")
        SchoolSubscription.objects.create(school=school, plan=self.plan)
        SchoolMembership.objects.create(
            school=school,
            teacher=self.user,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        return school

    def _recipient(self, *, school, requires_signature=False, title="إشعار إضافي"):
        notification = Notification.objects.create(
            title=title,
            message="نص",
            requires_signature=requires_signature,
            school=school,
        )
        return NotificationRecipient.objects.create(
            notification=notification,
            teacher=self.user,
        )

    def test_mark_all_notifications_is_scoped_to_active_school_and_globals(self):
        other_school = self._add_second_school()
        other_recipient = self._recipient(school=other_school)
        global_recipient = self._recipient(school=None, title="إشعار عام")

        response = self.client.post(reverse("reports:notifications_mark_all_read"))

        self.assertEqual(response.status_code, 302)
        self.rec.refresh_from_db()
        other_recipient.refresh_from_db()
        global_recipient.refresh_from_db()
        self.assertTrue(self.rec.is_read)
        self.assertTrue(global_recipient.is_read)
        self.assertFalse(other_recipient.is_read)

    def test_opening_circular_list_does_not_mark_circular_read(self):
        circular = self._recipient(
            school=self.school,
            requires_signature=True,
            title="تعميم",
        )

        response = self.client.get(reverse("reports:my_circulars"))

        self.assertEqual(response.status_code, 200)
        circular.refresh_from_db()
        self.assertFalse(circular.is_read)

    def test_mark_all_circulars_is_scoped_to_active_school_and_globals(self):
        other_school = self._add_second_school()
        active_circular = self._recipient(
            school=self.school, requires_signature=True, title="تعميم المدرسة"
        )
        other_circular = self._recipient(
            school=other_school, requires_signature=True, title="تعميم مدرسة أخرى"
        )
        global_circular = self._recipient(
            school=None, requires_signature=True, title="تعميم عام"
        )

        response = self.client.post(reverse("reports:circulars_mark_all_read"))

        self.assertEqual(response.status_code, 302)
        active_circular.refresh_from_db()
        other_circular.refresh_from_db()
        global_circular.refresh_from_db()
        self.assertTrue(active_circular.is_read)
        self.assertTrue(global_circular.is_read)
        self.assertFalse(other_circular.is_read)

    def test_mark_all_without_selected_school_only_marks_global_items(self):
        self._add_second_school()
        global_recipient = self._recipient(school=None, title="إشعار عام")
        session = self.client.session
        session.pop("active_school_id", None)
        session.save()

        self.client.post(reverse("reports:notifications_mark_all_read"))

        self.rec.refresh_from_db()
        global_recipient.refresh_from_db()
        self.assertFalse(self.rec.is_read)
        self.assertTrue(global_recipient.is_read)

    def test_home_uses_non_blocking_card_without_marking_read(self):
        response = self.client.get(reverse("reports:home"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="twq-section teacher-daily-attention"')
        self.assertContains(response, 'class="teacher-daily-attention-item"')
        self.assertContains(
            response,
            reverse("reports:my_notification_detail", args=[self.rec.pk]),
        )
        self.assertNotContains(response, "data-mark-url")
        self.rec.refresh_from_db()
        self.assertFalse(self.rec.is_read)
