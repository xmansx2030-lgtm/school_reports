"""Apply runtime configuration to the server's env.production, safely.

Why this exists
---------------
``env.production`` lives only on the server: it holds live secrets, and the
deploy deliberately excludes it so a release can never clobber it. The cost of
that correct decision is **drift** — the repository's ``env.production.example``
is a template nobody diffs against the real file. A release once shipped
advertising one gateway while the paying one was switched off: every test
green, the image correct, the site wrong. Nothing compared the two.

This script closes that gap without weakening the rule. It changes a **fixed,
named set of keys** and nothing else — it is not a generic "set any variable"
tool, because that would turn a config fix into a config-injection hole.

Guarantees
----------
- Secrets arrive on **stdin**, never in argv, so they stay out of the process
  list and the shell history.
- The file is backed up with a UTC timestamp before a single byte changes.
- Keys already present are rewritten in place; missing keys are appended. Order,
  comments and unrelated values survive untouched.
- The file is rewritten with ``0600`` permissions, matching the original intent.
- A value that fails validation aborts before anything is written.

Usage (from the deploy workflow, or by hand on the server)::

    printf '%s' "$MOYASAR_SECRET_KEY" | python3 apply_runtime_config.py \\
        --moyasar-enabled True --moyasar-environment live
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_ENV_PATH = Path(
    os.environ.get("DEPLOY_PATH", "/opt/school_reports")
) / "deploy" / "hetzner" / "env.production"

BOOL_CHOICES = ("True", "False")

# Enough to undo a bad change; beyond that each one is another live-secret copy.
BACKUPS_TO_KEEP = 5


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-path", type=Path, default=DEFAULT_ENV_PATH)
    parser.add_argument("--tamara-enabled", choices=BOOL_CHOICES)
    parser.add_argument("--tamara-environment", choices=("production", "sandbox"))
    parser.add_argument("--moyasar-enabled", choices=BOOL_CHOICES)
    parser.add_argument("--moyasar-environment", choices=("live", "test"))
    parser.add_argument("--pdf-offload-enabled", choices=BOOL_CHOICES)
    parser.add_argument("--celery-media-concurrency", type=int)
    parser.add_argument("--web-concurrency", type=int)
    parser.add_argument(
        "--tamara-config-from-stdin",
        action="store_true",
        help="Read TAMARA_API_TOKEN and TAMARA_NOTIFICATION_TOKEN from two stdin lines.",
    )
    parser.add_argument(
        "--moyasar-key-from-stdin",
        action="store_true",
        help="Read MOYASAR_SECRET_KEY from stdin. An empty read leaves it unchanged.",
    )
    parser.add_argument("--web-push-enabled", choices=BOOL_CHOICES)
    parser.add_argument(
        "--web-push-config-from-stdin",
        action="store_true",
        help=(
            "Read three lines from stdin: VAPID private key, public key, and "
            "mailto:/https: subject. Values are never printed."
        ),
    )
    parser.add_argument(
        "--resend-config-from-stdin",
        action="store_true",
        help="Read RESEND_API_KEY and RESEND_WEBHOOK_SECRET from two stdin lines.",
    )
    parser.add_argument(
        "--resend-system-backend",
        action="store_true",
        help="Use the existing RESEND_API_KEY for Django system emails.",
    )
    parser.add_argument("--password-change-email-enabled", choices=BOOL_CHOICES)
    parser.add_argument("--subscription-activation-email-enabled", choices=BOOL_CHOICES)
    parser.add_argument("--subscription-expiry-reminder-email-enabled", choices=BOOL_CHOICES)
    parser.add_argument(
        "--operations-github-repository",
        help="GitHub owner/repository used by the production operations monitor.",
    )
    parser.add_argument(
        "--fcm-service-account-from-stdin",
        action="store_true",
        help="Read and install a Firebase service-account JSON document from stdin.",
    )
    return parser.parse_args()


def _collect(args: argparse.Namespace) -> dict[str, str]:
    """Build the key set, validating every value before anything is written."""
    values: dict[str, str] = {}

    if getattr(args, "tamara_enabled", None):
        values["TAMARA_ENABLED"] = args.tamara_enabled
    if getattr(args, "tamara_environment", None):
        values["TAMARA_ENVIRONMENT"] = args.tamara_environment
    if args.moyasar_enabled:
        values["MOYASAR_ENABLED"] = args.moyasar_enabled
    if args.moyasar_environment:
        values["MOYASAR_ENVIRONMENT"] = args.moyasar_environment
    if args.pdf_offload_enabled:
        values["PDF_OFFLOAD_ENABLED"] = args.pdf_offload_enabled
    if args.celery_media_concurrency is not None:
        if not 1 <= args.celery_media_concurrency <= 8:
            raise SystemExit("CELERY_MEDIA_CONCURRENCY must be between 1 and 8.")
        values["CELERY_MEDIA_CONCURRENCY"] = str(args.celery_media_concurrency)
    if args.web_concurrency is not None:
        if not 1 <= args.web_concurrency <= 4:
            raise SystemExit("WEB_CONCURRENCY must be between 1 and 4 on this host.")
        values["WEB_CONCURRENCY"] = str(args.web_concurrency)

    if getattr(args, "operations_github_repository", None):
        repository = args.operations_github_repository.strip()
        if len(repository) > 160 or not re.fullmatch(
            r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository
        ):
            raise SystemExit("OPERATIONS_GITHUB_REPOSITORY must use owner/repository format.")
        values["OPERATIONS_GITHUB_REPOSITORY"] = repository

    if args.web_push_enabled:
        values["WEB_PUSH_ENABLED"] = args.web_push_enabled

    if getattr(args, "tamara_config_from_stdin", False):
        lines = sys.stdin.read().splitlines()
        if len(lines) != 2:
            raise SystemExit(
                "Tamara configuration requires exactly two stdin lines: API token and notification token."
            )
        api_token, notification_token = (line.strip() for line in lines)
        for label, token in (
            ("TAMARA_API_TOKEN", api_token),
            ("TAMARA_NOTIFICATION_TOKEN", notification_token),
        ):
            if len(token) < 16 or re.search(r"\s", token):
                raise SystemExit(f"{label} is empty or malformed.")
        values.update(
            {
                "TAMARA_API_TOKEN": api_token,
                "TAMARA_NOTIFICATION_TOKEN": notification_token,
            }
        )

    if args.moyasar_key_from_stdin:
        key = sys.stdin.read().strip()
        if key:
            # The same rule settings.py enforces at boot — but caught here, before
            # the file is written, so a wrong key never reaches a restart.
            if not re.fullmatch(r"sk_(live|test)_[A-Za-z0-9]{8,}", key):
                raise SystemExit("MOYASAR_SECRET_KEY does not look like an sk_live_/sk_test_ key.")
            env = values.get("MOYASAR_ENVIRONMENT")
            expected = "sk_live_" if env == "live" else "sk_test_"
            if env and not key.startswith(expected):
                raise SystemExit(
                    f"MOYASAR_SECRET_KEY does not match MOYASAR_ENVIRONMENT={env}."
                )
            values["MOYASAR_SECRET_KEY"] = key

    if args.web_push_config_from_stdin:
        lines = sys.stdin.read().splitlines()
        if len(lines) != 3:
            raise SystemExit(
                "Web Push configuration requires exactly three stdin lines: private, public, subject."
            )
        private_key, public_key, subject = (line.strip() for line in lines)

        def _decode_key(value: str, label: str) -> bytes:
            if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
                raise SystemExit(f"{label} is not base64url without padding.")
            try:
                return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
            except Exception as exc:
                raise SystemExit(f"{label} is not valid base64url.") from exc

        private_raw = _decode_key(private_key, "WEB_PUSH_VAPID_PRIVATE_KEY")
        public_raw = _decode_key(public_key, "WEB_PUSH_VAPID_PUBLIC_KEY")
        if len(private_raw) != 32:
            raise SystemExit("WEB_PUSH_VAPID_PRIVATE_KEY must decode to 32 bytes.")
        if len(public_raw) != 65 or public_raw[:1] != b"\x04":
            raise SystemExit("WEB_PUSH_VAPID_PUBLIC_KEY must be an uncompressed P-256 point.")
        if not subject.startswith(("mailto:", "https://")):
            raise SystemExit("WEB_PUSH_SUBJECT must start with mailto: or https://.")

        values.update(
            {
                "WEB_PUSH_VAPID_PRIVATE_KEY": private_key,
                "WEB_PUSH_VAPID_PUBLIC_KEY": public_key,
                "WEB_PUSH_SUBJECT": subject,
            }
        )

    if args.resend_config_from_stdin:
        lines = sys.stdin.read().splitlines()
        if len(lines) != 2:
            raise SystemExit(
                "Resend configuration requires exactly two stdin lines: API key and webhook secret."
            )
        api_key, webhook_secret = (line.strip() for line in lines)
        if not re.fullmatch(r"re_[A-Za-z0-9_]{16,}", api_key):
            raise SystemExit("RESEND_API_KEY does not look like a Resend API key.")
        if not re.fullmatch(r"whsec_[A-Za-z0-9_+/=-]{16,}", webhook_secret):
            raise SystemExit("RESEND_WEBHOOK_SECRET does not look like a webhook signing secret.")
        values.update(
            {
                "EMAIL_BACKEND": "reports.email_backends.ResendEmailBackend",
                "RESEND_API_KEY": api_key,
                "RESEND_WEBHOOK_SECRET": webhook_secret,
            }
        )

    if getattr(args, "resend_system_backend", False):
        values["EMAIL_BACKEND"] = "reports.email_backends.ResendEmailBackend"

    for arg_name, env_name in (
        ("password_change_email_enabled", "PASSWORD_CHANGE_EMAIL_ENABLED"),
        ("subscription_activation_email_enabled", "SUBSCRIPTION_ACTIVATION_EMAIL_ENABLED"),
        (
            "subscription_expiry_reminder_email_enabled",
            "SUBSCRIPTION_EXPIRY_REMINDER_EMAIL_ENABLED",
        ),
    ):
        value = getattr(args, arg_name, None)
        if value:
            values[env_name] = value

    if getattr(args, "fcm_service_account_from_stdin", False):
        try:
            account = json.loads(sys.stdin.read())
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SystemExit("FCM service account is not valid JSON.") from exc
        if not isinstance(account, dict) or account.get("type") != "service_account":
            raise SystemExit("FCM credential must be a service-account JSON document.")
        project_id = str(account.get("project_id") or "").strip()
        client_email = str(account.get("client_email") or "").strip()
        private_key = str(account.get("private_key") or "").strip()
        if project_id != "tawtheeq-operations":
            raise SystemExit("FCM credential belongs to an unexpected Firebase project.")
        if not client_email.endswith("@tawtheeq-operations.iam.gserviceaccount.com"):
            raise SystemExit("FCM service-account email is unexpected.")
        if not private_key.startswith("-----BEGIN PRIVATE KEY-----"):
            raise SystemExit("FCM service-account private key is missing or malformed.")
        args._fcm_service_account = account
        values.update(
            {
                "FCM_PROJECT_ID": project_id,
                "GOOGLE_APPLICATION_CREDENTIALS": "/run/secrets/firebase-service-account.json",
            }
        )

    if not values:
        raise SystemExit("Nothing to apply — pass at least one option.")
    return values


def _assert_gateway_can_boot(path: Path, values: dict[str, str]) -> None:
    """Refuse to enable Moyasar unless a matching key will be in place.

    ``settings.py`` raises ``ImproperlyConfigured`` at import time when Moyasar
    is enabled without a key, or with a key whose prefix disagrees with the
    environment. That is a module-level raise: every container refuses to boot
    and the site goes down.

    Turning the gateway on while leaving the key unchanged is the easy way into
    that state — the operator sees only a dropdown, and whether the server's env
    file already holds a key is invisible from the workflow form. So the check
    happens here, reading the file we are about to rewrite, and fails *before*
    anything is written.
    """
    if values.get("MOYASAR_ENABLED") != "True":
        return

    existing: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Z0-9_]+)=(.*)$", line)
        if match:
            existing[match.group(1)] = match.group(2).strip()

    key = values.get("MOYASAR_SECRET_KEY") or existing.get("MOYASAR_SECRET_KEY", "")
    if not key:
        raise SystemExit(
            "Refusing to enable Moyasar: no MOYASAR_SECRET_KEY on the server and "
            "none supplied. Re-run with the key (update_moyasar_key) — enabling "
            "without it stops every container from booting."
        )

    environment = values.get("MOYASAR_ENVIRONMENT") or existing.get(
        "MOYASAR_ENVIRONMENT", "test"
    )
    expected = "sk_live_" if environment == "live" else "sk_test_"
    if not key.startswith(expected):
        raise SystemExit(
            f"Refusing to enable Moyasar: the stored key does not match "
            f"MOYASAR_ENVIRONMENT={environment}. Supply a {expected}* key."
        )


def _assert_tamara_can_boot(path: Path, values: dict[str, str]) -> None:
    """Refuse to advertise Tamara until both merchant secrets are installed."""
    if values.get("TAMARA_ENABLED") != "True":
        return
    existing: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Z0-9_]+)=(.*)$", line)
        if match:
            existing[match.group(1)] = match.group(2).strip()
    missing = [
        key
        for key in ("TAMARA_API_TOKEN", "TAMARA_NOTIFICATION_TOKEN")
        if not (values.get(key) or existing.get(key, ""))
    ]
    if missing:
        raise SystemExit(
            "Refusing to enable Tamara without: " + ", ".join(missing)
        )
    environment = values.get("TAMARA_ENVIRONMENT") or existing.get(
        "TAMARA_ENVIRONMENT", "sandbox"
    )
    if environment != "production":
        raise SystemExit(
            "Refusing to enable live Tamara with TAMARA_ENVIRONMENT=" + environment
        )


def _assert_web_push_can_boot(path: Path, values: dict[str, str]) -> None:
    if values.get("WEB_PUSH_ENABLED") != "True":
        return
    existing: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Z0-9_]+)=(.*)$", line)
        if match:
            existing[match.group(1)] = match.group(2).strip()
    private_key = values.get("WEB_PUSH_VAPID_PRIVATE_KEY") or existing.get(
        "WEB_PUSH_VAPID_PRIVATE_KEY", ""
    )
    public_key = values.get("WEB_PUSH_VAPID_PUBLIC_KEY") or existing.get(
        "WEB_PUSH_VAPID_PUBLIC_KEY", ""
    )
    if not private_key or not public_key:
        raise SystemExit(
            "Refusing to enable Web Push without both stable VAPID keys."
        )


def _assert_resend_can_boot(path: Path, values: dict[str, str]) -> None:
    if values.get("EMAIL_BACKEND") != "reports.email_backends.ResendEmailBackend":
        return
    existing: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Z0-9_]+)=(.*)$", line)
        if match:
            existing[match.group(1)] = match.group(2).strip()
    if not existing.get("RESEND_API_KEY") and not values.get("RESEND_API_KEY"):
        raise SystemExit(
            "Refusing to enable ResendEmailBackend without an existing RESEND_API_KEY."
        )


def _prune_backups(path: Path, keep: int = BACKUPS_TO_KEEP) -> list[str]:
    """Keep the most recent backups and shred the rest.

    Every backup is a **complete copy of the live secrets**. Letting them pile up
    turns one 0600 file into a growing directory of them: more copies to leak,
    more to forget when rotating a key. A handful is enough to undo a bad change;
    beyond that they are liability, not safety.

    The timestamp is a sortable UTC stamp, so lexical order is chronological.
    """
    backups = sorted(path.parent.glob(f"{path.name}.bak.*"))
    removed: list[str] = []
    for old in backups[:-keep] if keep else backups:
        try:
            old.unlink()
            removed.append(old.name)
        except OSError:
            pass
    return removed


def _rewrite(path: Path, values: dict[str, str]) -> list[str]:
    original = path.read_text(encoding="utf-8").splitlines()
    remaining = dict(values)
    changed: list[str] = []
    output: list[str] = []

    for line in original:
        match = re.match(r"^([A-Z0-9_]+)=(.*)$", line)
        if match and match.group(1) in remaining:
            key = match.group(1)
            new_value = remaining.pop(key)
            if match.group(2) != new_value:
                changed.append(key)
            output.append(f"{key}={new_value}")
        else:
            output.append(line)

    for key, value in remaining.items():
        output.append(f"{key}={value}")
        changed.append(key)

    # Write to a sibling temp file, then rename over the original. ``rename``
    # within a directory is atomic on POSIX, so a reader either sees the whole
    # old file or the whole new one — never a half-written env that would stop
    # every container from booting. Writing in place left exactly that window;
    # the backup could undo it, but only after someone noticed the outage.
    #
    # 0600 is set on the temp file *before* the content lands in it, so the
    # secrets are never briefly world-readable.
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(output) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)

    path.chmod(0o600)
    return changed


def _write_fcm_service_account(path: Path, account: dict) -> None:
    target = path.parent / "firebase-service-account.json"
    tmp = target.with_name(f"{target.name}.tmp.{os.getpid()}")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(account, handle, ensure_ascii=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
        # The production image runs as the fixed, unprivileged UID 10001.
        # Keep the service-account private while allowing that runtime user to
        # read the bind-mounted credential.  A root-owned 0600 file is not
        # readable from inside the deliberately non-root container.
        os.chown(target, 10001, -1)
        target.chmod(0o400)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def main() -> None:
    args = _parse_args()
    path: Path = args.env_path
    if not path.is_file():
        raise SystemExit(f"{path} not found — run this on the server.")

    values = _collect(args)
    _assert_gateway_can_boot(path, values)
    _assert_tamara_can_boot(path, values)
    _assert_web_push_can_boot(path, values)
    _assert_resend_can_boot(path, values)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.bak.{timestamp}")
    shutil.copy2(path, backup)

    account = getattr(args, "_fcm_service_account", None)
    if account is not None:
        _write_fcm_service_account(path, account)
    changed = _rewrite(path, values)

    # Names only. Printing a value here would put a live payment key into the
    # workflow log, which is exactly what reading it from stdin was for.
    pruned = _prune_backups(path)

    print(f"[config] backup: {backup.name}")
    if pruned:
        print(f"[config] pruned {len(pruned)} older backup(s)")
    if account is not None:
        print("[config] installed Firebase service account")
    print(f"[config] updated: {', '.join(sorted(changed)) or 'nothing (already current)'}")


if __name__ == "__main__":
    main()
