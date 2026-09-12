from django.core.cache import cache
from django.test import TransactionTestCase, override_settings
from django.urls import reverse

from reports.forms import NotificationCreateForm
from reports.models import (
    AuditLog,
    Department,
    DepartmentMembership,
    Notification,
    NotificationRecipient,
    School,
    SchoolMembership,
    SchoolSubscription,
    SubscriptionPlan,
    Teacher,
)


@override_settings(
    ALLOWED_HOSTS=["testserver"],
    CELERY_BROKER_URL="",
    NOTIFICATIONS_LOCAL_FALLBACK_ENABLED=True,
    NOTIFICATIONS_LOCAL_FALLBACK_THREAD=False,
    NOTIFICATIONS_LOCAL_FALLBACK_HARD_STOP_RECIPIENTS=50,
)
class NotificationDispatchTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        cache.clear()
        self.school = School.objects.create(name="Test School", code="test-school")
        plan = SubscriptionPlan.objects.create(
            name="Test Plan",
            price=0,
            days_duration=30,
            max_teachers=0,
        )
        SchoolSubscription.objects.create(school=self.school, plan=plan)
        self.department = Department.objects.create(
            school=self.school,
            name="Science",
            slug="science",
            is_active=True,
        )
        self.manager = Teacher.objects.create_user(
            phone="500000001",
            name="School Manager",
            password="pass",
            is_staff=True,
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )

        self.teachers = []
        memberships = []
        for idx in range(3):
            teacher = Teacher.objects.create_user(
                phone=f"50000010{idx}",
                name=f"Teacher {idx}",
                password="pass",
            )
            memberships.append(
                SchoolMembership(
                    school=self.school,
                    teacher=teacher,
                    role_type=SchoolMembership.RoleType.TEACHER,
                )
            )
            self.teachers.append(teacher)
        SchoolMembership.objects.bulk_create(memberships)
        DepartmentMembership.objects.bulk_create(
            [
                DepartmentMembership(department=self.department, teacher=self.teachers[0]),
                DepartmentMembership(department=self.department, teacher=self.teachers[1]),
            ]
        )

    def _recipient_ids_for(self, notification):
        return set(
            NotificationRecipient.objects.filter(notification=notification)
            .values_list("teacher_id", flat=True)
        )

    def test_notification_task_name_is_stable(self):
        from reports.task_names import SEND_NOTIFICATION_TASK
        from reports.tasks import send_notification_task

        self.assertEqual(send_notification_task.name, SEND_NOTIFICATION_TASK)

    def test_school_manager_notification_selected_teachers_dispatches_without_broker(self):
        form = NotificationCreateForm(
            data={
                "title": "Notification",
                "message": "Selected teachers only.",
                "teachers": [str(self.teachers[0].id), str(self.teachers[2].id)],
            },
            user=self.manager,
            active_school=self.school,
            mode="notification",
        )

        self.assertTrue(form.is_valid(), form.errors.as_data())
        notification = form.save(creator=self.manager, default_school=self.school)

        self.assertEqual(
            self._recipient_ids_for(notification),
            {self.teachers[0].id, self.teachers[2].id},
        )

    def test_school_manager_notification_department_dispatches_without_broker(self):
        form = NotificationCreateForm(
            data={
                "title": "Department Notification",
                "message": "Department members.",
                "target_department": str(self.department.id),
            },
            user=self.manager,
            active_school=self.school,
            mode="notification",
        )

        self.assertTrue(form.is_valid(), form.errors.as_data())
        notification = form.save(creator=self.manager, default_school=self.school)

        self.assertEqual(
            self._recipient_ids_for(notification),
            {self.teachers[0].id, self.teachers[1].id},
        )

    def test_school_manager_notification_combines_departments_and_individuals(self):
        second_department = Department.objects.create(
            school=self.school,
            name="Languages",
            slug="languages",
            is_active=True,
        )
        DepartmentMembership.objects.create(
            department=second_department,
            teacher=self.teachers[2],
        )
        form = NotificationCreateForm(
            data={
                "title": "Combined recipients",
                "message": "Departments plus an individual.",
                "target_department": [
                    str(self.department.id),
                    str(second_department.id),
                ],
                # Duplicate an existing department member to verify de-duplication.
                "teachers": [str(self.teachers[0].id)],
            },
            user=self.manager,
            active_school=self.school,
            mode="notification",
        )

        self.assertTrue(form.is_valid(), form.errors.as_data())
        notification = form.save(creator=self.manager, default_school=self.school)

        self.assertEqual(
            self._recipient_ids_for(notification),
            {teacher.id for teacher in self.teachers},
        )

    def test_school_manager_notification_department_without_active_members_is_invalid(self):
        empty_department = Department.objects.create(
            school=self.school,
            name="Empty Department",
            slug="empty-department",
            is_active=True,
        )
        form = NotificationCreateForm(
            data={
                "title": "Department Notification",
                "message": "No recipients in this department.",
                "target_department": str(empty_department.id),
            },
            user=self.manager,
            active_school=self.school,
            mode="notification",
        )

        self.assertFalse(form.is_valid())
        self.assertIn("target_department", form.errors)
        self.assertIn("لا يحتوي على مستلمين نشطين", " ".join(form.errors.get("target_department", [])))

    def test_school_manager_circular_selected_teachers_dispatches_without_broker(self):
        form = NotificationCreateForm(
            data={
                "title": "Circular",
                "message": "Selected circular.",
                "teachers": [str(self.teachers[1].id)],
            },
            user=self.manager,
            active_school=self.school,
            mode="circular",
        )

        self.assertTrue(form.is_valid(), form.errors.as_data())
        notification = form.save(
            creator=self.manager,
            default_school=self.school,
            force_requires_signature=True,
        )

        self.assertEqual(self._recipient_ids_for(notification), {self.teachers[1].id})

    def test_school_manager_circular_department_only_dispatches_without_broker(self):
        form = NotificationCreateForm(
            data={
                "title": "Department circular",
                "message": "Department members.",
                "target_department": [str(self.department.id)],
            },
            user=self.manager,
            active_school=self.school,
            mode="circular",
        )

        self.assertTrue(form.is_valid(), form.errors.as_data())
        notification = form.save(
            creator=self.manager,
            default_school=self.school,
            force_requires_signature=True,
        )

        self.assertEqual(
            self._recipient_ids_for(notification),
            {self.teachers[0].id, self.teachers[1].id},
        )

    def test_school_manager_circular_department_and_individual_dispatches_without_broker(self):
        form = NotificationCreateForm(
            data={
                "title": "Department circular",
                "message": "Department members plus an individual.",
                "target_department": [str(self.department.id)],
                # Teacher 0 is already in the department; teacher 2 is an addition.
                "teachers": [str(self.teachers[0].id), str(self.teachers[2].id)],
            },
            user=self.manager,
            active_school=self.school,
            mode="circular",
        )

        self.assertTrue(form.is_valid(), form.errors.as_data())
        notification = form.save(
            creator=self.manager,
            default_school=self.school,
            force_requires_signature=True,
        )

        self.assertEqual(
            self._recipient_ids_for(notification),
            {teacher.id for teacher in self.teachers},
        )

    def test_school_manager_circular_requires_explicit_recipients(self):
        form = NotificationCreateForm(
            data={
                "title": "Circular",
                "message": "Please read and sign.",
            },
            user=self.manager,
            active_school=self.school,
            mode="circular",
        )

        self.assertFalse(form.is_valid())
        self.assertIn("teachers", form.errors)
        self.assertIn("المستلمون = 0", " ".join(form.errors.get("teachers", [])))

    def test_circular_create_view_selected_teacher_reaches_teacher_circulars_page(self):
        self.client.force_login(self.manager)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()

        response = self.client.post(
            reverse("reports:circulars_create"),
            data={
                "title": "View Circular",
                "message": "Sent through the real create view.",
                "teachers": [str(self.teachers[0].id)],
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            NotificationRecipient.objects.filter(
                teacher=self.teachers[0],
                notification__requires_signature=True,
                notification__title="View Circular",
            ).count(),
            1,
        )

        self.client.force_login(self.teachers[0])
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()

        response = self.client.get(reverse("reports:my_circulars"))

        self.assertContains(response, "View Circular")

    def test_manager_can_append_new_school_recipient_with_auditable_provenance(self):
        notification = Notification.objects.create(
            title="Circular recipient snapshot",
            message="Original audience remains immutable.",
            school=self.school,
            created_by=self.manager,
            requires_signature=True,
        )
        original = NotificationRecipient.objects.create(
            notification=notification,
            teacher=self.teachers[0],
        )
        self.client.force_login(self.manager)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()

        response = self.client.post(
            reverse("reports:circular_recipients_add", args=[notification.pk]),
            {"teacher_ids": [str(self.teachers[1].pk)]},
        )

        self.assertRedirects(
            response,
            reverse("reports:notification_detail", args=[notification.pk]),
            fetch_redirect_response=False,
        )
        original.refresh_from_db()
        self.assertEqual(
            original.delivery_source,
            NotificationRecipient.DeliverySource.ORIGINAL,
        )
        self.assertIsNone(original.added_by_id)
        appended = NotificationRecipient.objects.get(
            notification=notification,
            teacher=self.teachers[1],
        )
        self.assertEqual(
            appended.delivery_source,
            NotificationRecipient.DeliverySource.MANUAL_ADDITION,
        )
        self.assertEqual(appended.added_by, self.manager)
        audit = AuditLog.objects.get(
            model_name="Notification",
            object_id=notification.pk,
            changes__action="append_circular_recipients",
        )
        self.assertEqual(audit.teacher, self.manager)
        self.assertEqual(audit.changes["teacher_ids"], [self.teachers[1].pk])

        repeated = self.client.post(
            reverse("reports:circular_recipients_add", args=[notification.pk]),
            {"teacher_ids": [str(self.teachers[1].pk)]},
        )
        self.assertEqual(repeated.status_code, 302)
        self.assertEqual(
            NotificationRecipient.objects.filter(
                notification=notification,
                teacher=self.teachers[1],
            ).count(),
            1,
        )
        self.assertEqual(
            AuditLog.objects.filter(
                model_name="Notification",
                object_id=notification.pk,
                changes__action="append_circular_recipients",
            ).count(),
            1,
        )

        detail = self.client.get(reverse("reports:notification_detail", args=[notification.pk]))
        self.assertContains(detail, "أضيف لاحقًا")
        self.assertContains(detail, "سجل الإصدار الأصلي محفوظ")
        self.assertNotContains(detail, f'value="{self.teachers[1].pk}"')

    def test_append_recipients_rejects_an_account_outside_active_school(self):
        other_school = School.objects.create(name="Other School", code="other-school")
        SchoolSubscription.objects.create(
            school=other_school,
            plan=SubscriptionPlan.objects.first(),
        )
        outsider = Teacher.objects.create_user(
            phone="500000299",
            name="Outside Teacher",
            password="pass",
        )
        SchoolMembership.objects.create(
            school=other_school,
            teacher=outsider,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        notification = Notification.objects.create(
            title="School-isolated circular",
            message="Only this school.",
            school=self.school,
            created_by=self.manager,
            requires_signature=True,
        )
        self.client.force_login(self.manager)
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()

        response = self.client.post(
            reverse("reports:circular_recipients_add", args=[notification.pk]),
            {"teacher_ids": [str(outsider.pk)]},
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(
            NotificationRecipient.objects.filter(
                notification=notification,
                teacher=outsider,
            ).exists()
        )
        self.assertFalse(
            AuditLog.objects.filter(
                object_id=notification.pk,
                changes__action="append_circular_recipients",
            ).exists()
        )

    def test_non_manager_cannot_append_circular_recipients(self):
        notification = Notification.objects.create(
            title="Protected circular",
            message="Managers only.",
            school=self.school,
            created_by=self.manager,
            requires_signature=True,
        )
        self.client.force_login(self.teachers[0])
        session = self.client.session
        session["active_school_id"] = self.school.id
        session.save()

        response = self.client.post(
            reverse("reports:circular_recipients_add", args=[notification.pk]),
            {"teacher_ids": [str(self.teachers[1].pk)]},
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(
            NotificationRecipient.objects.filter(
                notification=notification,
                teacher=self.teachers[1],
            ).exists()
        )

    @override_settings(CELERY_BROKER_URL="memory://")
    def test_circular_selected_teacher_creates_recipient_even_when_queued_without_worker(self):
        form = NotificationCreateForm(
            data={
                "title": "Queued Circular",
                "message": "Worker may be unavailable.",
                "teachers": [str(self.teachers[0].id)],
            },
            user=self.manager,
            active_school=self.school,
            mode="circular",
        )

        self.assertTrue(form.is_valid(), form.errors.as_data())
        notification = form.save(
            creator=self.manager,
            default_school=self.school,
            force_requires_signature=True,
        )

        self.assertEqual(self._recipient_ids_for(notification), {self.teachers[0].id})

    def test_platform_notify_without_active_school_can_send_to_all_schools(self):
        second_school = School.objects.create(name="Second School", code="second-school")
        SchoolSubscription.objects.create(
            school=second_school,
            plan=SubscriptionPlan.objects.first(),
        )
        second_teacher = Teacher.objects.create_user(
            phone="500000200",
            name="Second Teacher",
            password="pass",
        )
        SchoolMembership.objects.create(
            school=second_school,
            teacher=second_teacher,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        admin = Teacher.objects.create_superuser(
            phone="500000999",
            name="Platform Owner",
            password="pass",
        )
        self.client.force_login(admin)

        response = self.client.post(
            reverse("reports:platform_school_notify"),
            data={
                "target_scope": "all",
                "title": "Platform Notice",
                "message": "Sent to every school in scope.",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            Notification.objects.filter(title="Platform Notice", created_by=admin).count(),
            2,
        )
        self.assertEqual(
            NotificationRecipient.objects.filter(
                notification__title="Platform Notice",
                teacher__in=[self.manager, *self.teachers, second_teacher],
            ).count(),
            5,
        )

        school_notification = Notification.objects.get(title="Platform Notice", school=self.school)
        NotificationRecipient.objects.filter(notification=school_notification, teacher=self.teachers[0]).update(is_read=True)

        sent_response = self.client.get(reverse("reports:notifications_sent"))
        self.assertContains(sent_response, "Platform Notice")
        self.assertContains(sent_response, "1 / 4")
        self.assertEqual(
            sent_response.context["stats"][school_notification.id],
            {"total": 4, "read": 1, "signed": 0},
        )

    def test_platform_notify_without_active_school_requires_selected_schools_for_selected_scope(self):
        admin = Teacher.objects.create_superuser(
            phone="500000998",
            name="Platform Owner 2",
            password="pass",
        )
        self.client.force_login(admin)

        response = self.client.post(
            reverse("reports:platform_school_notify"),
            data={
                "target_scope": "selected",
                "title": "Platform Notice",
                "message": "Missing schools.",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "اختر مدرسة واحدة على الأقل")
        self.assertFalse(Notification.objects.filter(title="Platform Notice").exists())
