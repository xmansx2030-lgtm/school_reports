import tempfile
import threading
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.cache import cache
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from reports.forms import NotificationCreateForm
from reports.models import (
    Department,
    DepartmentMembership,
    Notification,
    NotificationRecipient,
    NotificationSendSubmission,
    School,
    SchoolMembership,
    SchoolSubscription,
    SubscriptionPlan,
    Teacher,
)
from reports.services_notification_idempotency import (
    notification_submission_fingerprint,
    reserve_notification_submission,
)


@override_settings(
    ALLOWED_HOSTS=["testserver"],
    CELERY_BROKER_URL="",
    NOTIFICATIONS_LOCAL_FALLBACK_ENABLED=False,
)
class NotificationSendIdempotencyTests(TestCase):
    def setUp(self):
        cache.clear()
        self._media_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._media_directory.cleanup)
        self._media_override = override_settings(MEDIA_ROOT=self._media_directory.name)
        self._media_override.enable()
        self.addCleanup(self._media_override.disable)

        plan = SubscriptionPlan.objects.create(
            name="Idempotency plan",
            price=0,
            days_duration=30,
            max_teachers=20,
        )
        self.school = School.objects.create(name="Idempotency school", code="idempotency")
        SchoolSubscription.objects.create(school=self.school, plan=plan)
        self.sender = Teacher.objects.create_user(
            phone="500917001",
            name="Idempotency sender",
            password="pass",
            is_staff=True,
        )
        self.recipient = Teacher.objects.create_user(
            phone="500917002",
            name="Idempotency recipient",
            password="pass",
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.sender,
            role_type=SchoolMembership.RoleType.MANAGER,
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.recipient,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        self.second_recipient = Teacher.objects.create_user(
            phone="500917003",
            name="Second idempotency recipient",
            password="pass",
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.second_recipient,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        self.department = Department.objects.create(
            school=self.school,
            name="Idempotency department",
            slug="idempotency-department",
            is_active=True,
        )
        DepartmentMembership.objects.create(
            department=self.department,
            teacher=self.recipient,
        )
        DepartmentMembership.objects.create(
            department=self.department,
            teacher=self.second_recipient,
        )
        self.client = Client()
        self.client.force_login(self.sender)
        session = self.client.session
        session["active_school_id"] = self.school.pk
        session.save()

    def _payload(self, submission_key, **overrides):
        payload = {
            "submission_key": str(submission_key),
            "communication_type": "notification",
            "title": "Idempotent notification",
            "message": "This logical send must happen once.",
            "teachers": [str(self.recipient.pk)],
        }
        payload.update(overrides)
        return payload

    def test_sequential_retry_creates_one_notification_and_dispatches_once(self):
        submission_key = uuid4()
        url = reverse("reports:notifications_create")

        with patch(
            "reports.realtime_notifications.push_new_notification_to_teachers"
        ) as realtime:
            with self.captureOnCommitCallbacks(execute=True):
                first = self.client.post(url, self._payload(submission_key))
            with self.captureOnCommitCallbacks(execute=True):
                second = self.client.post(url, self._payload(submission_key))

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        notifications = Notification.objects.filter(title="Idempotent notification")
        self.assertEqual(notifications.count(), 1)
        self.assertEqual(
            NotificationRecipient.objects.filter(notification__in=notifications).count(),
            1,
        )
        self.assertEqual(realtime.call_count, 1)

    def test_same_key_with_different_payload_is_rejected_without_mutation(self):
        submission_key = uuid4()
        url = reverse("reports:notifications_create")

        first = self.client.post(url, self._payload(submission_key))
        second = self.client.post(
            url,
            self._payload(submission_key, message="A different message must not be sent."),
        )

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 200)
        self.assertContains(second, "تعذّر إعادة استخدام طلب الإرسال")
        self.assertEqual(Notification.objects.filter(created_by=self.sender).count(), 1)
        self.assertEqual(NotificationRecipient.objects.filter(teacher=self.recipient).count(), 1)

    def test_same_payload_with_different_keys_creates_two_intentional_sends(self):
        url = reverse("reports:notifications_create")

        first = self.client.post(url, self._payload(uuid4()))
        second = self.client.post(url, self._payload(uuid4()))

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(Notification.objects.filter(created_by=self.sender).count(), 2)
        self.assertEqual(NotificationSendSubmission.objects.filter(sender=self.sender).count(), 2)

    def test_invalid_then_corrected_reuses_key_and_creates_one_notification(self):
        submission_key = uuid4()
        url = reverse("reports:notifications_create")
        invalid = self._payload(submission_key, message="", teachers=[])

        invalid_response = self.client.post(url, invalid)

        self.assertEqual(invalid_response.status_code, 200)
        self.assertEqual(NotificationSendSubmission.objects.count(), 0)
        self.assertEqual(
            str(invalid_response.context["form"]["submission_key"].value()),
            str(submission_key),
        )

        corrected = self.client.post(url, self._payload(submission_key))

        self.assertEqual(corrected.status_code, 302)
        self.assertEqual(Notification.objects.filter(created_by=self.sender).count(), 1)
        self.assertEqual(NotificationSendSubmission.objects.filter(sender=self.sender).count(), 1)

    def test_missing_or_malformed_key_is_rejected_before_creation(self):
        url = reverse("reports:notifications_create")
        missing_key = self._payload(uuid4())
        missing_key.pop("submission_key")

        missing_response = self.client.post(url, missing_key)
        malformed_response = self.client.post(
            url,
            self._payload("not-a-uuid"),
        )

        self.assertEqual(missing_response.status_code, 200)
        self.assertEqual(malformed_response.status_code, 200)
        self.assertEqual(NotificationSendSubmission.objects.count(), 0)
        self.assertFalse(
            Notification.objects.filter(created_by=self.sender, title="Idempotent notification").exists()
        )

    def test_each_new_composer_get_receives_a_new_server_key(self):
        url = reverse("reports:notifications_create")

        first = self.client.get(url)
        second = self.client.get(url)

        first_key = first.context["form"]["submission_key"].value()
        second_key = second.context["form"]["submission_key"].value()
        self.assertTrue(first_key)
        self.assertTrue(second_key)
        self.assertNotEqual(first_key, second_key)
        self.assertContains(first, 'name="submission_key"')

    def test_recipient_and_department_order_do_not_change_fingerprint(self):
        submission_key = uuid4()
        url = reverse("reports:notifications_create")
        first_payload = self._payload(
            submission_key,
            teachers=[str(self.recipient.pk), str(self.second_recipient.pk)],
            target_department=[str(self.department.pk)],
        )
        second_payload = self._payload(
            submission_key,
            teachers=[str(self.second_recipient.pk), str(self.recipient.pk)],
            target_department=[str(self.department.pk)],
        )

        self.client.post(url, first_payload)
        self.client.post(url, second_payload)

        notification = Notification.objects.get(created_by=self.sender)
        self.assertEqual(
            set(notification.recipients.values_list("teacher_id", flat=True)),
            {self.recipient.pk, self.second_recipient.pk},
        )
        self.assertEqual(Notification.objects.filter(created_by=self.sender).count(), 1)

    def test_attachment_retry_creates_one_file_and_one_notification(self):
        submission_key = uuid4()
        url = reverse("reports:notifications_create")

        def payload():
            return self._payload(
                submission_key,
                communication_type="newsletter",
                title="Idempotent newsletter",
                attachment=SimpleUploadedFile(
                    "newsletter.pdf",
                    b"%PDF-1.4\n% idempotency attachment\n",
                    content_type="application/pdf",
                ),
            )

        first = self.client.post(url, payload())
        second = self.client.post(url, payload())

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        newsletter = Notification.objects.get(created_by=self.sender)
        self.assertTrue(newsletter.attachment)
        stored_files = [path for path in Path(self._media_directory.name).rglob("*") if path.is_file()]
        self.assertEqual(len(stored_files), 1)

    def test_attachment_fingerprint_rewinds_uploaded_file(self):
        attachment = SimpleUploadedFile(
            "newsletter.pdf",
            b"%PDF-1.4\n% pointer test\n",
            content_type="application/pdf",
        )
        form = NotificationCreateForm(
            data={
                "submission_key": str(uuid4()),
                "communication_type": "newsletter",
                "title": "Pointer test",
                "message": "Attachment remains readable.",
                "teachers": [str(self.recipient.pk)],
            },
            files={"attachment": attachment},
            user=self.sender,
            active_school=self.school,
            require_submission_key=True,
        )
        self.assertTrue(form.is_valid(), form.errors.as_data())

        notification_submission_fingerprint(
            cleaned_data=form.cleaned_data,
            sender=self.sender,
            default_school=self.school,
            mode="notification",
        )

        self.assertEqual(attachment.tell(), 0)
        self.assertEqual(attachment.read(), b"%PDF-1.4\n% pointer test\n")

    def test_invalid_attachment_does_not_consume_key_before_corrected_retry(self):
        submission_key = uuid4()
        url = reverse("reports:notifications_create")
        common = {
            "communication_type": "newsletter",
            "title": "Corrected attachment newsletter",
        }
        invalid = self.client.post(
            url,
            self._payload(
                submission_key,
                **common,
                attachment=SimpleUploadedFile(
                    "invalid.txt",
                    b"not an allowed notification attachment",
                    content_type="text/plain",
                ),
            ),
        )

        self.assertEqual(invalid.status_code, 200)
        self.assertEqual(NotificationSendSubmission.objects.count(), 0)

        corrected = self.client.post(
            url,
            self._payload(
                submission_key,
                **common,
                attachment=SimpleUploadedFile(
                    "valid.pdf",
                    b"%PDF-1.4\n% corrected retry\n",
                    content_type="application/pdf",
                ),
            ),
        )

        self.assertEqual(corrected.status_code, 302)
        self.assertEqual(NotificationSendSubmission.objects.count(), 1)
        self.assertEqual(Notification.objects.filter(title=common["title"]).count(), 1)

    def test_rolled_back_reservation_does_not_consume_key(self):
        submission_key = uuid4()
        url = reverse("reports:notifications_create")
        notification_count_before = Notification.objects.count()

        with patch.object(NotificationCreateForm, "save", side_effect=RuntimeError("probe")):
            failed = self.client.post(url, self._payload(submission_key))

        self.assertEqual(failed.status_code, 200)
        self.assertEqual(NotificationSendSubmission.objects.count(), 0)
        self.assertEqual(Notification.objects.count(), notification_count_before)

        retried = self.client.post(url, self._payload(submission_key))
        self.assertEqual(retried.status_code, 302)
        self.assertEqual(Notification.objects.count(), notification_count_before + 1)
        self.assertEqual(NotificationSendSubmission.objects.count(), 1)

    def test_deleted_result_remains_a_tombstone_and_is_not_resent(self):
        submission_key = uuid4()
        url = reverse("reports:notifications_create")
        self.client.post(url, self._payload(submission_key))
        notification = Notification.objects.get(created_by=self.sender)
        notification.delete()

        retry = self.client.post(url, self._payload(submission_key))

        self.assertEqual(retry.status_code, 302)
        self.assertEqual(Notification.objects.filter(created_by=self.sender).count(), 0)
        submission = NotificationSendSubmission.objects.get(sender=self.sender)
        self.assertIsNone(submission.notification)

    def test_same_uuid_is_isolated_between_senders(self):
        other_sender = Teacher.objects.create_user(
            phone="500917004",
            name="Other idempotency sender",
            password="pass",
            is_staff=True,
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=other_sender,
            role_type=SchoolMembership.RoleType.ADMIN_STAFF,
        )
        other_client = Client()
        other_client.force_login(other_sender)
        session = other_client.session
        session["active_school_id"] = self.school.pk
        session.save()
        submission_key = uuid4()
        url = reverse("reports:notifications_create")

        self.client.post(url, self._payload(submission_key))
        other_client.post(url, self._payload(submission_key))

        self.assertEqual(Notification.objects.filter(title="Idempotent notification").count(), 2)
        self.assertEqual(NotificationSendSubmission.objects.count(), 2)

    def test_platform_owner_all_school_retry_is_idempotent(self):
        platform_owner = Teacher.objects.create_superuser(
            phone="500917005",
            name="Platform idempotency sender",
            password="pass",
        )
        platform_client = Client()
        platform_client.force_login(platform_owner)
        submission_key = uuid4()
        payload = {
            "submission_key": str(submission_key),
            "communication_type": "notification",
            "audience_scope": "all",
            "title": "Platform-wide notification",
            "message": "One platform send.",
            "teachers": [str(self.recipient.pk)],
        }
        url = reverse("reports:notifications_create")

        first = platform_client.post(url, payload)
        second = platform_client.post(url, payload)

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(
            Notification.objects.filter(
                created_by=platform_owner,
                title="Platform-wide notification",
            ).count(),
            1,
        )
        submission = NotificationSendSubmission.objects.get(sender=platform_owner)
        self.assertIsNone(submission.school)

    def test_platform_owner_cannot_reuse_key_for_another_school_scope(self):
        plan = SchoolSubscription.objects.get(school=self.school).plan
        other_school = School.objects.create(name="Other scope school", code="other-idem-scope")
        SchoolSubscription.objects.create(school=other_school, plan=plan)
        other_recipient = Teacher.objects.create_user(
            phone="500917006",
            name="Other scope recipient",
            password="pass",
        )
        SchoolMembership.objects.create(
            school=other_school,
            teacher=other_recipient,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        platform_owner = Teacher.objects.create_superuser(
            phone="500917007",
            name="Scoped platform sender",
            password="pass",
        )
        platform_client = Client()
        platform_client.force_login(platform_owner)
        submission_key = uuid4()
        url = reverse("reports:notifications_create")
        first_payload = {
            "submission_key": str(submission_key),
            "communication_type": "notification",
            "audience_scope": "school",
            "target_school": str(self.school.pk),
            "title": "Scoped notification",
            "message": "One school only.",
            "teachers": [str(self.recipient.pk)],
        }
        second_payload = {
            **first_payload,
            "target_school": str(other_school.pk),
            "teachers": [str(other_recipient.pk)],
        }

        first = platform_client.post(url, first_payload)
        second = platform_client.post(url, second_payload)

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 200)
        self.assertContains(second, "تعذّر إعادة استخدام طلب الإرسال")
        self.assertEqual(
            Notification.objects.filter(created_by=platform_owner, title="Scoped notification").count(),
            1,
        )
        self.assertFalse(
            Notification.objects.filter(created_by=platform_owner, school=other_school).exists()
        )

    def test_csrf_is_still_required_before_idempotency_processing(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.sender)
        session = csrf_client.session
        session["active_school_id"] = self.school.pk
        session.save()

        response = csrf_client.post(
            reverse("reports:notifications_create"),
            self._payload(uuid4()),
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(NotificationSendSubmission.objects.count(), 0)

    def test_database_constraint_and_race_recovery_use_sender_and_key(self):
        submission_key = uuid4()
        existing = NotificationSendSubmission.objects.create(
            sender=self.sender,
            school=self.school,
            submission_key=submission_key,
            payload_fingerprint="a" * 64,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                NotificationSendSubmission.objects.create(
                    sender=self.sender,
                    school=self.school,
                    submission_key=submission_key,
                    payload_fingerprint="a" * 64,
                )

        with patch.object(
            NotificationSendSubmission.objects,
            "create",
            side_effect=IntegrityError("simulated concurrent uniqueness race"),
        ):
            recovered, created = reserve_notification_submission(
                sender=self.sender,
                school=self.school,
                submission_key=submission_key,
                payload_fingerprint="a" * 64,
            )

        self.assertFalse(created)
        self.assertEqual(recovered.pk, existing.pk)

    def test_retry_schedules_websocket_and_webpush_once(self):
        submission_key = uuid4()
        url = reverse("reports:notifications_create")

        with (
            patch("reports.realtime_notifications._push_delta_to_users_batch") as websocket,
            patch("reports.web_push.queue_notification_web_push") as webpush,
        ):
            with self.captureOnCommitCallbacks(execute=True):
                self.client.post(url, self._payload(submission_key))
            with self.captureOnCommitCallbacks(execute=True):
                self.client.post(url, self._payload(submission_key))

        self.assertEqual(websocket.call_count, 1)
        self.assertEqual(webpush.call_count, 1)

    @override_settings(
        NOTIFICATIONS_LOCAL_FALLBACK_ENABLED=True,
        NOTIFICATIONS_LOCAL_FALLBACK_THREAD=False,
    )
    def test_retry_registers_local_fallback_dispatch_once(self):
        submission_key = uuid4()
        url = reverse("reports:notifications_create")

        with patch("reports.services_notifications.dispatch_notification_recipients") as dispatch:
            with self.captureOnCommitCallbacks(execute=True):
                self.client.post(url, self._payload(submission_key))
            with self.captureOnCommitCallbacks(execute=True):
                self.client.post(url, self._payload(submission_key))

        self.assertEqual(dispatch.call_count, 1)


@override_settings(CELERY_BROKER_URL="", NOTIFICATIONS_LOCAL_FALLBACK_ENABLED=False)
class NotificationSendIdempotencyConcurrencyTests(TransactionTestCase):
    """Exercise the real uniqueness race on databases with row-level concurrency."""

    def setUp(self):
        self.school = School.objects.create(name="Concurrency school", code="idem-concurrency")
        self.sender = Teacher.objects.create_user(
            phone="500917101",
            name="Concurrency sender",
            password="pass",
            is_staff=True,
        )

    def test_two_concurrent_reservations_create_one_notification(self):
        if connection.vendor == "sqlite":
            self.skipTest(
                "SQLite serializes/locks writes and cannot model PostgreSQL's uniqueness wait reliably."
            )

        submission_key = uuid4()
        barrier = threading.Barrier(2)
        created_flags = []
        errors = []

        def worker():
            close_old_connections()
            try:
                sender = Teacher.objects.get(pk=self.sender.pk)
                school = School.objects.get(pk=self.school.pk)
                barrier.wait(timeout=5)
                with transaction.atomic():
                    submission, created = reserve_notification_submission(
                        sender=sender,
                        submission_key=submission_key,
                        school=school,
                        payload_fingerprint="b" * 64,
                    )
                    if created:
                        notification = Notification.objects.create(
                            title="Concurrent notification",
                            message="One durable result.",
                            school=school,
                            created_by=sender,
                        )
                        submission.notification = notification
                        submission.save(update_fields=["notification"])
                created_flags.append(created)
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                close_old_connections()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertFalse(errors)
        self.assertEqual(sorted(created_flags), [False, True])
        self.assertEqual(NotificationSendSubmission.objects.count(), 1)
        self.assertEqual(Notification.objects.count(), 1)
