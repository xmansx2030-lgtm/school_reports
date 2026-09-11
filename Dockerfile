# Use an official Python runtime as a parent image
FROM python:3.12-slim

# Environment
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PIP_NO_CACHE_DIR=1 \
    XDG_CACHE_HOME=/app/.cache \
    DEBIAN_FRONTEND=noninteractive \
    SERVICE_TYPE=web

# Set work directory
WORKDIR /app

# System libraries for WeasyPrint (PDF) and image handling.
#
# libmagic1 is required by python-magic. Without it the import fails and the
# upload validators silently fall back to extension + magic-byte checks only —
# i.e. the content-type sniffing that reports/validators.py documents was never
# actually running in production.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    python3-dev \
    python3-pip \
    python3-setuptools \
    python3-wheel \
    python3-cffi \
    libcairo2 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf-xlib-2.0-0 \
    libffi-dev \
    fonts-dejavu-core \
    fonts-noto-core \
    shared-mime-info \
    libmagic1 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
#
# من قفلٍ مُجزَّأ لا من requirements.txt مباشرةً. الأخير يثبّت الاعتماديات
# **المباشرة** وحدها، فكان `pip` يحلّ ما وراءها وقت البناء: صورتان تُبنيان في
# يومين مختلفين تحملان شجرتين مختلفتين، ولا سجلّ لما دخل الإنتاج فعلاً. وهكذا
# دخلت إصدارات من urllib3 وtwisted وmsgpack تحمل ثغرات معروفة دون أن يظهر ذلك
# في أي فحص يقرأ requirements.txt.
#
# `--require-hashes` يجعل البناء يفشل إن اختلف بايت واحد عمّا فُحص — فيغطي
# اختطاف الحزمة في المستودع، لا التثبيت وحده.
#
# التحديث:  pip-compile --generate-hashes --strip-extras \
#               --output-file=requirements.lock.txt requirements.txt
COPY requirements.txt requirements.lock.txt /app/
RUN pip install --upgrade pip && \
    pip install --no-cache-dir --require-hashes -r requirements.lock.txt

# Copy project files
COPY . /app/

# The application does not need root privileges at runtime.
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /nonexistent --shell /usr/sbin/nologin app \
    && mkdir -p /app/.cache /app/staticfiles /app/media \
    && chown -R app:app /app

USER app

# Expose application port
EXPOSE 10000

# Run service based on SERVICE_TYPE or START_CMD override
CMD ["sh", "-c", "\
set -e; \
if [ -n \"$START_CMD\" ]; then \
    echo \"[boot] Using START_CMD override\"; \
    exec sh -c \"$START_CMD\"; \
elif [ \"$SERVICE_TYPE\" = \"worker_default\" ]; then \
    echo \"[boot] Starting Celery worker: default\"; \
    exec celery -A config worker -Q default --concurrency=${CELERY_DEFAULT_CONCURRENCY:-2}; \
elif [ \"$SERVICE_TYPE\" = \"worker_notifications\" ]; then \
    echo \"[boot] Starting Celery worker: notifications\"; \
    exec celery -A config worker -Q notifications --concurrency=${CELERY_NOTIFICATIONS_CONCURRENCY:-2}; \
elif [ \"$SERVICE_TYPE\" = \"worker_images\" ]; then \
    echo \"[boot] Starting Celery worker: images\"; \
    exec celery -A config worker -Q images --concurrency=${CELERY_IMAGES_CONCURRENCY:-1}; \
elif [ \"$SERVICE_TYPE\" = \"worker_periodic\" ]; then \
    echo \"[boot] Starting Celery worker: periodic\"; \
    exec celery -A config worker -Q periodic --concurrency=${CELERY_PERIODIC_CONCURRENCY:-1}; \
elif [ \"$SERVICE_TYPE\" = \"beat\" ]; then \
    echo \"[boot] Starting Celery beat\"; \
    exec celery -A config beat; \
else \
    echo \"[boot] Starting web service\"; \
    if [ \"${RUN_MIGRATIONS_ON_START:-1}\" = \"1\" ]; then \
        python manage.py migrate --noinput; \
        python manage.py collectstatic --noinput; \
    else \
        echo \"[boot] Skipping migrate/collectstatic (RUN_MIGRATIONS_ON_START=0)\"; \
    fi; \
    exec gunicorn config.asgi:application \
        --bind 0.0.0.0:${PORT:-10000} \
        -k uvicorn.workers.UvicornWorker \
        --workers ${WEB_CONCURRENCY:-1} \
        --timeout ${GUNICORN_TIMEOUT:-120} \
        --keep-alive ${GUNICORN_KEEPALIVE:-5} \
        --max-requests ${GUNICORN_MAX_REQUESTS:-800} \
        --max-requests-jitter ${GUNICORN_MAX_REQUESTS_JITTER:-80}; \
fi"]
