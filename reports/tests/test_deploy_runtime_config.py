from __future__ import annotations

import base64
import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from deploy.hetzner.apply_runtime_config import (
    _assert_resend_can_boot,
    _assert_tamara_can_boot,
    _assert_web_push_can_boot,
    _collect,
    _rewrite,
    _write_fcm_service_account,
)


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


class WebPushRuntimeConfigTests(SimpleTestCase):
    def _args(self, **overrides):
        values = {
            "tamara_enabled": None,
            "tamara_environment": None,
            "tamara_config_from_stdin": False,
            "moyasar_enabled": None,
            "moyasar_environment": None,
            "pdf_offload_enabled": None,
            "celery_media_concurrency": None,
            "web_concurrency": None,
            "moyasar_key_from_stdin": False,
            "web_push_enabled": "True",
            "web_push_config_from_stdin": True,
            "resend_config_from_stdin": False,
            "resend_system_backend": False,
            "fcm_service_account_from_stdin": False,
            "operations_github_repository": None,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_tamara_tokens_are_collected_without_printing(self):
        stdin = io.StringIO("api_token_1234567890\nnotification_token_1234567890\n")
        with patch("sys.stdin", stdin):
            values = _collect(
                self._args(
                    tamara_config_from_stdin=True,
                    web_push_enabled=None,
                    web_push_config_from_stdin=False,
                )
            )
        self.assertEqual(values["TAMARA_API_TOKEN"], "api_token_1234567890")
        self.assertEqual(
            values["TAMARA_NOTIFICATION_TOKEN"],
            "notification_token_1234567890",
        )

    def test_tamara_cannot_be_enabled_without_both_tokens(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            path = Path(directory) / "env.production"
            path.write_text(
                "TAMARA_ENABLED=False\nTAMARA_ENVIRONMENT=production\n",
                encoding="utf-8",
            )
            with self.assertRaisesMessage(SystemExit, "TAMARA_API_TOKEN"):
                _assert_tamara_can_boot(path, {"TAMARA_ENABLED": "True"})

    def test_valid_vapid_pair_is_collected_without_printing_or_reformatting(self):
        private_key = _b64(b"p" * 32)
        public_key = _b64(b"\x04" + b"q" * 64)
        stdin = io.StringIO(f"{private_key}\n{public_key}\nmailto:test@example.com\n")
        with patch("sys.stdin", stdin):
            values = _collect(self._args())
        self.assertEqual(values["WEB_PUSH_ENABLED"], "True")
        self.assertEqual(values["WEB_PUSH_VAPID_PRIVATE_KEY"], private_key)
        self.assertEqual(values["WEB_PUSH_VAPID_PUBLIC_KEY"], public_key)

    def test_enable_is_rejected_when_server_has_no_keys(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            path = Path(directory) / "env.production"
            path.write_text("WEB_PUSH_ENABLED=False\n", encoding="utf-8")
            with self.assertRaisesMessage(SystemExit, "both stable VAPID keys"):
                _assert_web_push_can_boot(
                    path,
                    {"WEB_PUSH_ENABLED": "True"},
                )

    def test_rewrite_preserves_unrelated_production_values(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            path = Path(directory) / "env.production"
            path.write_text("SECRET_KEY=untouched\nWEB_PUSH_ENABLED=False\n", encoding="utf-8")
            changed = _rewrite(
                path,
                {"WEB_PUSH_ENABLED": "True", "WEB_PUSH_SUBJECT": "mailto:test@example.com"},
            )
            content = path.read_text(encoding="utf-8")
        self.assertIn("SECRET_KEY=untouched", content)
        self.assertIn("WEB_PUSH_ENABLED=True", content)
        self.assertEqual(set(changed), {"WEB_PUSH_ENABLED", "WEB_PUSH_SUBJECT"})

    def test_operations_repository_is_validated_and_collected(self):
        values = _collect(
            self._args(
                web_push_enabled=None,
                web_push_config_from_stdin=False,
                operations_github_repository="xmansx2030-lgtm/school_reports",
            )
        )
        self.assertEqual(
            values["OPERATIONS_GITHUB_REPOSITORY"],
            "xmansx2030-lgtm/school_reports",
        )

    def test_invalid_operations_repository_is_rejected(self):
        with self.assertRaisesMessage(SystemExit, "owner/repository"):
            _collect(
                self._args(
                    web_push_enabled=None,
                    web_push_config_from_stdin=False,
                    operations_github_repository="https://github.com/owner/repo",
                )
            )

    def test_fcm_service_account_is_validated_and_collected(self):
        import json

        account = {
            "type": "service_account",
            "project_id": "tawtheeq-operations",
            "client_email": "firebase-adminsdk-fbsvc@tawtheeq-operations.iam.gserviceaccount.com",
            "private_key": "-----BEGIN PRIVATE KEY-----\ntest\n-----END PRIVATE KEY-----\n",
        }
        with patch("sys.stdin", io.StringIO(json.dumps(account))):
            args = self._args(
                web_push_enabled=None,
                web_push_config_from_stdin=False,
                fcm_service_account_from_stdin=True,
            )
            values = _collect(args)
        self.assertEqual(values["FCM_PROJECT_ID"], "tawtheeq-operations")
        self.assertEqual(
            values["GOOGLE_APPLICATION_CREDENTIALS"],
            "/run/secrets/firebase-service-account.json",
        )
        self.assertEqual(args._fcm_service_account["project_id"], "tawtheeq-operations")

    def test_fcm_service_account_is_owned_by_runtime_user_and_owner_read_only(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            env_path = Path(directory) / "env.production"
            target = Path(directory) / "firebase-service-account.json"
            with (
                patch("deploy.hetzner.apply_runtime_config.os.chown", create=True) as chown,
                patch.object(Path, "chmod") as chmod,
            ):
                _write_fcm_service_account(env_path, {"project_id": "tawtheeq-operations"})
        chown.assert_called_once_with(target, 10001, -1)
        chmod.assert_called_once_with(0o400)

    def test_resend_secrets_are_collected_from_exactly_two_lines(self):
        stdin = io.StringIO("re_platformKey_123456789\nwhsec_platformSecret_123456789\n")
        with patch("sys.stdin", stdin):
            values = _collect(
                self._args(
                    web_push_enabled=None,
                    web_push_config_from_stdin=False,
                    resend_config_from_stdin=True,
                )
            )
        self.assertEqual(values["RESEND_API_KEY"], "re_platformKey_123456789")
        self.assertEqual(values["RESEND_WEBHOOK_SECRET"], "whsec_platformSecret_123456789")

    def test_invalid_resend_secret_is_rejected(self):
        stdin = io.StringIO("re_platformKey_123456789\ninvalid-secret\n")
        with patch("sys.stdin", stdin):
            with self.assertRaisesMessage(SystemExit, "RESEND_WEBHOOK_SECRET"):
                _collect(
                    self._args(
                        web_push_enabled=None,
                        web_push_config_from_stdin=False,
                        resend_config_from_stdin=True,
                    )
                )

    def test_resend_system_backend_uses_existing_key_without_reading_secret(self):
        values = _collect(
            self._args(
                web_push_enabled=None,
                web_push_config_from_stdin=False,
                resend_system_backend=True,
            )
        )
        self.assertEqual(values["EMAIL_BACKEND"], "reports.email_backends.ResendEmailBackend")

    def test_system_email_channels_can_be_enabled_together(self):
        values = _collect(
            self._args(
                web_push_enabled=None,
                web_push_config_from_stdin=False,
                password_change_email_enabled="True",
                subscription_activation_email_enabled="True",
                subscription_expiry_reminder_email_enabled="True",
            )
        )

        self.assertEqual(values["PASSWORD_CHANGE_EMAIL_ENABLED"], "True")
        self.assertEqual(values["SUBSCRIPTION_ACTIVATION_EMAIL_ENABLED"], "True")
        self.assertEqual(values["SUBSCRIPTION_EXPIRY_REMINDER_EMAIL_ENABLED"], "True")

    def test_resend_system_backend_refuses_missing_server_key(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            path = Path(directory) / "env.production"
            path.write_text("EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend\n", encoding="utf-8")
            with self.assertRaisesMessage(SystemExit, "RESEND_API_KEY"):
                _assert_resend_can_boot(
                    path,
                    {"EMAIL_BACKEND": "reports.email_backends.ResendEmailBackend"},
                )
