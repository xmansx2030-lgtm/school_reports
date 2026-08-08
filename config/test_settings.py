"""Fast, deterministic settings for automated tests and CI."""

from .settings import *  # noqa: F403

ENV = "test"
DEBUG = False

PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.MD5PasswordHasher",
]

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "school-reports-tests",
    }
}

CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels.layers.InMemoryChannelLayer",
    }
}

CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_BROKER_URL = "memory://"
CELERY_RESULT_BACKEND = "cache+memory://"

HEAVY_EXPORT_ASYNC_ENABLED = False
CPU_ALERT_PERCENT = 101
MEMORY_ALERT_PERCENT = 101
DISK_ALERT_PERCENT = 101
CELERY_QUEUE_ALERT_LENGTH = 10**9
HTTP_ALERT_MIN_SAMPLES = 10**9

MEDIA_PUBLIC_ACCESS_ENABLED = False
CSP_ENABLED = True
CSP_REPORT_ONLY = False
