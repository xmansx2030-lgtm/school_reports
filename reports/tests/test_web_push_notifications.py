from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.test import TestCase, override_settings
from django.urls import reverse

from reports.models import (
    Notification,
    NotificationRecipient,
    Teacher,
    WebPushDelivery,
    WebPushSubscription,
)
from reports.web_push import (
    deliver_notification_web_push,
    queue_notification_web_push,
    save_browser_subscription,
)


PUSH_SETTINGS = {
    "WEB_PUSH_ENABLED": True,
    "WEB_PUSH_VAPID_PRIVATE_KEY": "a" * 43,
    "WEB_PUSH_VAPID_PUBLIC_KEY": "B" + "a" * 86,
    "WEB_PUSH_SUBJECT": "mailto:test@example.com",
    "WEB_PUSH_ALLOWED_ENDPOINT_HOSTS": (
        "fcm.googleapis.com",
        "push.services.mozilla.com",
        "push.apple.com",
    ),
    "CELERY_BROKER_URL": "",
}


@override_settings(**PUSH_SETTINGS)
class WebPushSubscriptionApiTests(TestCase):
    def setUp(self):
        self.user = Teacher.objects.create_user(phone="0500000011", name="مستخدم", password="pass-12345")
        self.client.force_login(self.user)
        self.payload = {
            "endpoint": "https://fcm.googleapis.com/fcm/send/device-token",
            "keys": {"p256dh": "p" * 88, "auth": "a" * 24},
        }

    def test_config_requires_login_and_returns_public_key(self):
        # Avoid a literal /config/ path: Cloudflare managed WAF rules classify
        # it as a sensitive-file probe and block the request at the edge.
        self.assertEqual(reverse("reports:web_push_config"), "/push/status/")

        self.client.logout()
        response = self.client.get(reverse("reports:web_push_config"))
        self.assertEqual(response.status_code, 302)

        self.client.force_login(self.user)
        response = self.client.get(reverse("reports:web_push_config"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["enabled"])
        self.assertEqual(response.json()["publicKey"], PUSH_SETTINGS["WEB_PUSH_VAPID_PUBLIC_KEY"])
        self.assertIn("no-cache", response["Cache-Control"])

    def test_subscribe_saves_current_device_and_unsubscribe_removes_only_its_owner(self):
        response = self.client.post(
            reverse("reports:web_push_subscribe"),
            data=json.dumps({"subscription": self.payload}),
            content_type="application/json",
            HTTP_USER_AGENT="Installed PWA",
        )
        self.assertEqual(response.status_code, 200)
        saved = WebPushSubscription.objects.get()
        self.assertEqual(saved.teacher, self.user)
        self.assertEqual(saved.user_agent, "Installed PWA")

        response = self.client.post(
            reverse("reports:web_push_unsubscribe"),
            data=json.dumps({"endpoint": self.payload["endpoint"]}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(WebPushSubscription.objects.exists())

    def test_same_browser_subscription_moves_to_the_current_account(self):
        first = save_browser_subscription(teacher=self.user, subscription=self.payload)
        other = Teacher.objects.create_user(phone="0500000012", name="مستخدم آخر", password="pass-12345")
        second = save_browser_subscription(teacher=other, subscription=self.payload)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(WebPushSubscription.objects.get(pk=first.pk).teacher, other)

    def test_subscription_rejects_arbitrary_https_endpoint_to_prevent_ssrf(self):
        self.payload["endpoint"] = "https://internal.example.test/push"
        response = self.client.post(
            reverse("reports:web_push_subscribe"),
            data=json.dumps({"subscription": self.payload}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(WebPushSubscription.objects.exists())


@override_settings(**PUSH_SETTINGS)
class WebPushDeliveryTests(TestCase):
    def setUp(self):
        self.user = Teacher.objects.create_user(phone="0500000021", name="مستلم", password="pass-12345")
        self.notification = Notification.objects.create(
            title="تنبيه مهم",
            message="يرجى مراجعة الطلب الجديد.",
            is_important=True,
        )
        self.recipient = NotificationRecipient.objects.create(
            notification=self.notification,
            teacher=self.user,
        )
        self.subscription = WebPushSubscription.objects.create(
            teacher=self.user,
            endpoint="https://fcm.googleapis.com/fcm/send/device-token",
            p256dh="p" * 88,
            auth="a" * 24,
        )

    @patch("pywebpush.webpush")
    def test_delivery_is_visible_and_idempotent_per_notification_and_device(self, webpush_mock):
        webpush_mock.return_value = SimpleNamespace(status_code=201)

        first = deliver_notification_web_push(self.notification.pk, [self.user.pk])
        second = deliver_notification_web_push(self.notification.pk, [self.user.pk])

        self.assertEqual(first["sent"], 1)
        self.assertEqual(second["skipped"], 1)
        self.assertEqual(webpush_mock.call_count, 1)
        payload = json.loads(webpush_mock.call_args.kwargs["data"])
        self.assertEqual(payload["title"], "تنبيه مهم")
        self.assertIn(str(self.recipient.pk), payload["url"])
        self.assertTrue(payload["requireInteraction"])
        delivery = WebPushDelivery.objects.get()
        self.assertEqual(delivery.status, WebPushDelivery.Status.SENT)
        self.subscription.refresh_from_db()
        self.assertIsNotNone(self.subscription.last_success_at)

    @patch("pywebpush.webpush")
    def test_expired_push_endpoint_is_removed_automatically(self, webpush_mock):
        from pywebpush import WebPushException

        webpush_mock.side_effect = WebPushException(
            "gone",
            response=SimpleNamespace(status_code=410),
        )
        stats = deliver_notification_web_push(self.notification.pk, [self.user.pk])
        self.assertEqual(stats["expired"], 1)
        self.assertFalse(WebPushSubscription.objects.exists())

    @patch("reports.web_push.queue_notification_web_push")
    def test_individual_recipient_creation_schedules_closed_app_delivery(self, queue_mock):
        other_notification = Notification.objects.create(title="جديد", message="نص")
        NotificationRecipient.objects.create(notification=other_notification, teacher=self.user)
        queue_mock.assert_called_once()
        self.assertEqual(queue_mock.call_args.kwargs["teacher_ids"], [self.user.pk])

    @override_settings(CELERY_BROKER_URL="memory://")
    @patch("reports.web_push.enqueue_named_task")
    def test_queue_uses_stable_task_name_and_notification_queue(self, enqueue):
        from reports.task_names import SEND_WEB_PUSH_NOTIFICATION_TASK
        from reports.tasks import send_web_push_notification_task

        with self.captureOnCommitCallbacks(execute=True):
            queue_notification_web_push(
                notification=self.notification,
                teacher_ids=[self.user.pk, self.user.pk],
            )

        self.assertEqual(
            send_web_push_notification_task.name,
            SEND_WEB_PUSH_NOTIFICATION_TASK,
        )
        enqueue.assert_called_once_with(
            SEND_WEB_PUSH_NOTIFICATION_TASK,
            args=(self.notification.pk, [self.user.pk]),
            queue="notifications",
        )


class WebPushFrontendContractTests(TestCase):
    @staticmethod
    def _source(path: str) -> str:
        return (Path(settings.BASE_DIR) / path).read_text(encoding="utf-8")

    def test_service_worker_handles_background_push_and_notification_click(self):
        worker = self._source("static/sw.js")
        self.assertIn('self.addEventListener("push"', worker)
        self.assertIn("self.registration.showNotification", worker)
        self.assertIn('self.addEventListener("notificationclick"', worker)
        self.assertIn("self.clients.openWindow", worker)
        # يُرفع مع كل تغيير في ``CORE_ASSETS`` وإلا بقيت الأجهزة على الملفات
        # القديمة. رُفع إلى v12 عند إضافة خط واجهة عدم الاتصال إلى الأصول الأساسية.
        self.assertIn('const CACHE_NAME = "tawtheeq-v12"', worker)

    def test_client_only_requests_permission_after_an_explicit_click(self):
        script = self._source("static/js/web-push.js")
        self.assertIn('enableButton.addEventListener("click", enable)', script)
        self.assertIn("Notification.requestPermission()", script)
        self.assertIn("registration.pushManager.subscribe", script)
        self.assertIn("applicationServerKey", script)
        self.assertIn("DISMISS_DAYS = 30", script)
        self.assertIn("isStandalone()", script)
        self.assertIn("Notification.permission === \"granted\"", script)
        self.assertIn("iosNeedsInstallation()", script)
        self.assertIn('show({ explicit: true })', script)
        self.assertNotIn('!isStandalone()) return', script)

    def test_sensitive_registration_receipt_never_auto_opens_push_prompt(self):
        template = self._source(
            "reports/templates/reports/partials/web_push_prompt.html"
        )
        script = self._source("static/js/web-push.js")

        self.assertIn("request.resolver_match.url_name == 'registration_success'", template)
        self.assertIn('var autoPromptAllowed = root.getAttribute("data-auto-prompt")', script)
        self.assertIn(
            'autoPromptAllowed && currentPermission() === "default"',
            script,
        )

    def test_mobile_drawer_has_persistent_install_and_push_actions(self):
        template = self._source("reports/templates/base.html")

        self.assertIn("data-pwa-install-trigger", template)
        self.assertIn("data-web-push-trigger", template)
        self.assertIn("data-web-push-trigger-label", template)
        self.assertIn("PWA_INSTALL_ENABLED", template)

    def test_mobile_prompts_account_for_safe_areas_and_short_landscape_screens(self):
        push_css = self._source("static/css/web-push.css")
        install_css = self._source("static/css/pwa-install.css")

        for source in (push_css, install_css):
            self.assertIn("env(safe-area-inset-top, 0px)", source)
            self.assertIn("env(safe-area-inset-bottom, 0px)", source)
            self.assertIn("env(safe-area-inset-right, 0px)", source)
            self.assertIn("env(safe-area-inset-left, 0px)", source)
            self.assertIn("var(--header-bottom, var(--header-h, 72px))", source)
            self.assertIn("@media (max-height: 520px)", source)

        self.assertIn("var(--web-push-bottom-clearance, 0px)", push_css)
        self.assertIn("var(--pwa-install-bottom-clearance, 0px)", install_css)

    def test_authenticated_base_page_exposes_the_opt_in_panel(self):
        user = Teacher.objects.create_user(phone="0500000031", name="مثبت", password="pass-12345")
        self.client.force_login(user)
        response = self.client.get(reverse("reports:home"))
        self.assertContains(response, 'id="webPushPrompt"')
        self.assertContains(response, 'id="webPushEnable"')
        self.assertContains(response, "js/web-push.js")
        self.assertContains(response, "css/web-push.css")
