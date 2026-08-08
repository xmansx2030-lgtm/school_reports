"""Create the first Hetzner production environment file without printing secrets."""

from __future__ import annotations

import os
from pathlib import Path
import secrets


TARGET = Path(__file__).with_name("env.production")


def token(length: int = 32) -> str:
    return secrets.token_urlsafe(length)


def main() -> None:
    if TARGET.exists():
        raise SystemExit(f"Refusing to overwrite existing {TARGET}")

    secret_key = token(48)
    postgres_password = token()
    redis_password = token()

    content = f"""\
APP_IMAGE=school-reports:20260727
ENV_FILE=deploy/hetzner/env.production
LOCAL_HTTP_PORT=18000

ENV=production
DEBUG=False
SECRET_KEY={secret_key}
SITE_URL=https://tawtheeq-ksa.com
CANONICAL_HOST_REDIRECT=True
ALLOWED_HOSTS=app.tawtheeq-ksa.com,tawtheeq-ksa.com,www.tawtheeq-ksa.com
CSRF_TRUSTED_ORIGINS=https://app.tawtheeq-ksa.com,https://tawtheeq-ksa.com,https://www.tawtheeq-ksa.com
WEBAUTHN_RP_ID=tawtheeq-ksa.com
PRODUCTION_STRICT_MODE=True
TRUSTED_PROXY_CIDRS=127.0.0.1/32,::1/128,172.16.0.0/12

# Complete before first production start. Never add a national ID or photo.
BUSINESS_LEGAL_NAME=
BUSINESS_COMMERCIAL_REGISTRATION=
BUSINESS_FREELANCE_DOCUMENT_NUMBER=
BUSINESS_FREELANCE_ACTIVITY=
BUSINESS_FREELANCE_DOCUMENT_EXPIRY=
BUSINESS_FREELANCE_DOCUMENT_URL=https://freelance.sa/certificate-validation
BUSINESS_TAX_NUMBER=
BUSINESS_LICENSES=
BUSINESS_VERIFICATION_URL=
BUSINESS_ADDRESS=
BUSINESS_SUPPORT_EMAIL=
BUSINESS_SUPPORT_PHONE=

POSTGRES_DB=school_reports
POSTGRES_USER=school_reports
POSTGRES_PASSWORD={postgres_password}
DATABASE_URL=postgresql://school_reports:{postgres_password}@postgres:5432/school_reports
DB_SSL=False

REDIS_PASSWORD={redis_password}
REDIS_URL=redis://:{redis_password}@redis:6379/0
REDIS_MAXMEMORY=192mb

R2_ACCESS_KEY_ID=
R2_SECRET_ACCESS_KEY=
R2_BUCKET_NAME=
R2_ENDPOINT_URL=
AWS_S3_REGION_NAME=nbg1
MEDIA_PUBLIC_ACCESS_ENABLED=False
AWS_QUERYSTRING_AUTH=True

WEB_CONCURRENCY=3
GUNICORN_TIMEOUT=120
GUNICORN_KEEPALIVE=5
GUNICORN_MAX_REQUESTS=800
GUNICORN_MAX_REQUESTS_JITTER=80
CELERY_CORE_CONCURRENCY=1
CELERY_MEDIA_CONCURRENCY=1
# MAX_CONCURRENT_REQUESTS=   # unset: derived from the budget below
OVERLOAD_RETRY_AFTER_SECONDS=5
SCHOOL_DASHBOARD_CACHE_TTL_SECONDS=45
SCHOOL_DASHBOARD_STALE_TTL_SECONDS=300
SCHOOL_DASHBOARD_LOCK_TTL_SECONDS=15
SCHOOL_RATE_LIMIT_ENABLED=True
SCHOOL_RATE_LIMIT_REQUESTS=900
SCHOOL_RATE_LIMIT_WINDOW_SECONDS=60
HEAVY_EXPORT_ASYNC_ENABLED=True
PDF_OFFLOAD_ENABLED=True
PDF_OFFLOAD_TIMEOUT_SECONDS=45
GENERATED_EXPORT_RETENTION_HOURS=6
INFRA_CAPACITY_MONITOR_ENABLED=True
REDIS_MEMORY_ALERT_PERCENT=80
CPU_ALERT_PERCENT=85
MEMORY_ALERT_PERCENT=85
DISK_ALERT_PERCENT=80
CELERY_QUEUE_ALERT_LENGTH=200
HTTP_5XX_ALERT_PERCENT=2.0
HTTP_LATENCY_ALERT_MS=2000
CONN_MAX_AGE=0
DATA_UPLOAD_MAX_MEMORY_SIZE=10485760
RUN_MIGRATIONS_ON_START=1
REDIS_MAXMEMORY_POLICY=volatile-lru
MANSOUR_ASSISTANT_DAILY_GLOBAL_LIMIT=2000
SESSION_CLEANUP_ENABLED=True

EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
EMAIL_HOST=
EMAIL_PORT=587
EMAIL_HOST_USER=
EMAIL_HOST_PASSWORD=
EMAIL_USE_TLS=True
EMAIL_USE_SSL=False
DEFAULT_FROM_EMAIL=no-reply@tawtheeq-ksa.com

NOTIFICATIONS_LOCAL_FALLBACK_ENABLED=False
PASSWORD_CHANGE_EMAIL_ENABLED=False
DAILY_MANAGER_REPORT_ENABLED=True
DAILY_MANAGER_REPORT_INAPP_ENABLED=True
DAILY_MANAGER_REPORT_EMAIL_ENABLED=False
DAILY_MANAGER_REPORT_WHATSAPP_ENABLED=False
DAILY_MANAGER_REPORT_HOUR=14
DAILY_MANAGER_REPORT_MINUTE=0

# Written out even though both gateways start off. Omitting them let the
# settings default decide in silence, and a server whose env never mentioned
# MOYASAR_ENABLED served a checkout page with no electronic payment on it and
# nothing anywhere saying why. Present-and-False is a setting; absent is a
# question nobody knows to ask. Enabling Moyasar needs the matching key —
# MOYASAR_ENVIRONMENT=live wants an sk_live_ key, and the app refuses to boot
# on a mismatch rather than take money against test credentials.
MOYASAR_ENABLED=False
MOYASAR_ENVIRONMENT=live
MOYASAR_SECRET_KEY=
MOYASAR_REQUEST_TIMEOUT=15
TAMARA_ENABLED=False
TAMARA_ENVIRONMENT=production
TAMARA_API_TOKEN=
TAMARA_NOTIFICATION_TOKEN=
TAMARA_INSTALMENTS=4
TAMARA_REQUEST_TIMEOUT=15

HEALTHZ_CHECK_CHANNELS=False
LOG_LEVEL=INFO
SENTRY_DSN=
SENTRY_RELEASE=
SENTRY_TRACES_SAMPLE_RATE=0.05
SECURITY_CONTACT_EMAIL=support@tawtheeq-ksa.com
"""

    fd = os.open(TARGET, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(content)


if __name__ == "__main__":
    main()
