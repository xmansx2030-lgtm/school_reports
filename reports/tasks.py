from __future__ import annotations

import logging
import shutil
import time as time_module
from datetime import datetime, time as dt_time, timedelta
from celery import shared_task
from django.apps import apps
from django.conf import settings
from django.core.cache import cache as django_cache
from django.core.files import File
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Count, Q
from django.template.loader import render_to_string
from django.utils import timezone

from core.observability import report_degraded as _degraded, soft_fail

from .email_branding import email_brand_context, platform_url, render_branded_email
from .storage import _compress_image_file
from .telegram_alerts import TelegramDeliveryError, deliver_telegram_alert
from .web_push import WebPushTransientError

logger = logging.getLogger(__name__)

from core import opmetrics


def _locked_generated_export_job(job_id: int):
    """Lock only the job row; nullable joins make PostgreSQL reject FOR UPDATE."""
    GeneratedExportJob = apps.get_model("reports", "GeneratedExportJob")
    return GeneratedExportJob.objects.select_for_update().filter(pk=job_id).first()


@shared_task(
    bind=True,
    ignore_result=True,
    autoretry_for=(TelegramDeliveryError,),
    retry_backoff=True,
    retry_jitter=True,
    retry_kwargs={"max_retries": 4},
)
def send_telegram_alert_task(self, payload: dict[str, str]) -> str:
    """Deliver a safe operational alert without blocking the user request."""
    return deliver_telegram_alert(payload)


@shared_task(
    bind=True,
    ignore_result=True,
    autoretry_for=(WebPushTransientError,),
    retry_backoff=True,
    retry_jitter=True,
    retry_kwargs={"max_retries": 4},
)
def send_web_push_notification_task(self, notification_id: int, teacher_ids: list[int]) -> dict[str, int]:
    """Deliver a notification to installed devices without blocking the request."""
    from .web_push import deliver_notification_web_push

    return deliver_notification_web_push(notification_id, teacher_ids)


@shared_task(
    bind=True,
    ignore_result=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    retry_kwargs={"max_retries": 5},
)
def delete_orphaned_storage_file_task(
    self,
    model_label: str,
    field_name: str,
    file_name: str,
) -> bool:
    """Retry deletion of an unreferenced local/R2 object after transient errors."""
    from .file_cleanup import delete_file_if_unreferenced

    return delete_file_if_unreferenced(model_label, field_name, file_name)


def _periodic_lock(lock_name: str, ttl: int = 600) -> bool:
    """Acquire a cache-based lock to prevent overlapping periodic tasks.

    Returns True if the lock was acquired (caller should proceed).
    Returns False if another instance already holds it (caller should skip).
    """
    return bool(django_cache.add(f"periodic_lock:{lock_name}", 1, timeout=ttl))


def _task_ctx(task_obj) -> tuple[str | None, int, str | None]:
    try:
        req = getattr(task_obj, "request", None)
        task_id = getattr(req, "id", None)
        retries = int(getattr(req, "retries", 0) or 0)
        headers = getattr(req, "headers", None) or {}
        trace_id = headers.get("trace_id") if hasattr(headers, "get") else None
        if not trace_id and task_id:
            trace_id = f"task-{task_id}"
        return task_id, retries, trace_id
    except Exception:
        return None, 0, None


def _email_delivery_configured() -> bool:
    """Return False when production SMTP is still at its placeholder config."""
    backend = (getattr(settings, "EMAIL_BACKEND", "") or "").lower()
    if "console" in backend or "locmem" in backend:
        return True
    if backend == "reports.email_backends.resendemailbackend":
        return bool(getattr(settings, "RESEND_API_KEY", "")) and (
            "@" in str(getattr(settings, "DEFAULT_FROM_EMAIL", "") or "")
        )
    if "smtp" not in backend:
        return True

    host = (getattr(settings, "EMAIL_HOST", "") or "").strip().lower()
    env = (getattr(settings, "ENV", "") or "").strip().lower()
    if env == "production" and host in {"", "localhost", "127.0.0.1"}:
        return False
    return True


@shared_task(bind=True, ignore_result=True)
def cleanup_platform_email_task(self) -> int:
    """Delete only archived mailbox records after the owner-defined retention period."""
    if not _periodic_lock("cleanup_platform_email", ttl=1800):
        return 0
    PlatformEmail = apps.get_model("reports", "PlatformEmail")
    PlatformEmailConfiguration = apps.get_model("reports", "PlatformEmailConfiguration")
    config, _created = PlatformEmailConfiguration.objects.get_or_create(pk=1)
    retention_days = max(30, min(int(config.retention_days or 365), 3650))
    cutoff = timezone.now() - timedelta(days=retention_days)
    deleted, _details = PlatformEmail.objects.filter(
        is_archived=True,
        updated_at__lt=cutoff,
    ).delete()
    logger.info(
        "Task success name=cleanup_platform_email_task deleted=%s retention_days=%s",
        deleted,
        retention_days,
    )
    return int(deleted)


@shared_task(bind=True, ignore_result=True, autoretry_for=(Exception,), retry_backoff=True, retry_jitter=True, retry_kwargs={"max_retries": 3})
def cleanup_audit_logs_task(self, days: int | None = None, chunk_size: int = 2000) -> int:
    """Delete AuditLog rows older than N days.

    Note: archiving is intentionally handled via the management command because
    many production setups use ephemeral disks for workers.
    """
    AuditLog = apps.get_model("reports", "AuditLog")
    task_id, retries, trace_id = _task_ctx(self)
    logger.info(
        "Task start name=cleanup_audit_logs_task task_id=%s trace_id=%s retries=%s",
        task_id,
        trace_id,
        retries,
    )

    retention_days = int(days) if days is not None else int(getattr(settings, "AUDIT_LOG_RETENTION_DAYS", 30))
    retention_days = max(retention_days, 0)

    chunk_size = max(int(chunk_size), 100)
    cutoff = timezone.now() - timedelta(days=retention_days)

    qs = AuditLog.objects.filter(timestamp__lt=cutoff).order_by("pk")

    from .model_parts.audit import audit_retention_purge

    deleted_total = 0
    while True:
        batch_pks = list(qs.values_list("pk", flat=True)[:chunk_size])
        if not batch_pks:
            break
        # سجل الإجراءات محصَّن ضد الحذف؛ سياسة الاحتفاظ هي الاستثناء الوحيد،
        # وتُعلن عن نفسها هنا صراحةً بدل أن تمر ضمناً.
        with audit_retention_purge():
            deleted, _ = AuditLog.objects.filter(pk__in=batch_pks).delete()
        deleted_total += int(deleted)

    logger.info(
        "Task success name=cleanup_audit_logs_task task_id=%s trace_id=%s deleted=%s retention_days=%s",
        task_id,
        trace_id,
        deleted_total,
        retention_days,
    )
    opmetrics.increment("celery.task.success.cleanup_audit_logs_task")
    return deleted_total


@shared_task(bind=True, ignore_result=True, autoretry_for=(Exception,), retry_backoff=True, retry_jitter=True, retry_kwargs={"max_retries": 3})
def cleanup_ai_usage_task(self, days: int | None = None, chunk_size: int = 2000) -> int:
    """يقلّم وقائع استهلاك الذكاء الاصطناعي الأقدم من N يوماً.

    الجدول يكبر بصفٍّ لكل نداء، وسقف منصور وحده ألفا نداء يومياً. والوقائع
    القديمة لا تُحتَجّ بها كما يُحتَجّ بسجل الإجراءات — قيمتها في الاتجاه لا في
    الواقعة — فحذفها مباشر بلا أرشفة، خلافاً لـ``cleanup_audit_logs_task``.
    """
    AiUsageEvent = apps.get_model("reports", "AiUsageEvent")
    task_id, retries, trace_id = _task_ctx(self)
    logger.info(
        "Task start name=cleanup_ai_usage_task task_id=%s trace_id=%s retries=%s",
        task_id,
        trace_id,
        retries,
    )

    retention_days = int(days) if days is not None else int(getattr(settings, "AI_USAGE_RETENTION_DAYS", 180))
    retention_days = max(retention_days, 0)
    chunk_size = max(int(chunk_size), 100)
    cutoff = timezone.now() - timedelta(days=retention_days)

    qs = AiUsageEvent.objects.filter(created_at__lt=cutoff).order_by("pk")
    deleted_total = 0
    while True:
        batch_pks = list(qs.values_list("pk", flat=True)[:chunk_size])
        if not batch_pks:
            break
        deleted, _ = AiUsageEvent.objects.filter(pk__in=batch_pks).delete()
        deleted_total += int(deleted)

    logger.info(
        "Task success name=cleanup_ai_usage_task task_id=%s trace_id=%s deleted=%s retention_days=%s",
        task_id,
        trace_id,
        deleted_total,
        retention_days,
    )
    opmetrics.increment("celery.task.success.cleanup_ai_usage_task")
    return deleted_total


@shared_task(bind=True, ignore_result=True)
def monitor_infrastructure_capacity_task(self) -> dict:
    """Warn before Redis or the session table runs the platform into trouble.

    Redis holds the cache, the sessions and the Celery queues on one instance.
    ``volatile-lru`` keeps a full instance from rejecting writes, but silent
    eviction still shows up as users being logged out and rate limits resetting,
    so the memory ratio needs to be visible *before* it gets there.
    """
    task_id, retries, trace_id = _task_ctx(self)
    report: dict = {
        "redis_used_percent": None,
        "expired_sessions": None,
        "cpu_percent": None,
        "memory_percent": None,
        "disk_percent": None,
        "queue_lengths": {},
        "http_5xx_percent": None,
        "http_average_ms": None,
        "alerts": [],
    }

    def _threshold(name: str, default: int) -> int:
        # Deliberately not `value or default`: a configured 0 means "always
        # alert" and must survive, not fall back to the default.
        value = getattr(settings, name, default)
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    threshold = _threshold("REDIS_MEMORY_ALERT_PERCENT", 80)

    try:
        from django_redis import get_redis_connection

        info = get_redis_connection("default").info(section="memory")
        used = int(info.get("used_memory") or 0)
        limit = int(info.get("maxmemory") or 0)
        if limit > 0 and used > 0:
            percent = round((used / limit) * 100, 1)
            report["redis_used_percent"] = percent
            if percent >= threshold:
                message = (
                    f"Redis memory at {percent}% of its limit "
                    f"({round(used / (1024 * 1024), 1)} MB of {round(limit / (1024 * 1024), 1)} MB)."
                )
                report["alerts"].append(message)
                logger.error("Infrastructure capacity warning: %s", message)
                opmetrics.increment("infra.redis.memory_high")
    except Exception:
        # A missing Redis (local/dev) must not fail the periodic job.
        logger.debug("Redis memory probe unavailable", exc_info=True)

    try:
        Session = apps.get_model("sessions", "Session")
        expired = Session.objects.filter(expire_date__lt=timezone.now()).count()
        report["expired_sessions"] = expired
        if expired > _threshold("EXPIRED_SESSION_ALERT_THRESHOLD", 100_000):
            message = f"{expired} expired session rows are still pending cleanup."
            report["alerts"].append(message)
            logger.error("Infrastructure capacity warning: %s", message)
            opmetrics.increment("infra.sessions.backlog_high")
    except Exception:
        logger.debug("Session backlog probe failed", exc_info=True)

    # Host/container resource pressure. psutil sees host resources on the
    # current Docker deployment; disk_usage('/') observes the same backing
    # filesystem whose exhaustion would stop uploads and PostgreSQL writes.
    try:
        def _cpu_sample():
            with open("/proc/stat", "r", encoding="ascii") as proc_stat:
                fields = [int(value) for value in proc_stat.readline().split()[1:]]
            idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
            return sum(fields), idle

        total_before, idle_before = _cpu_sample()
        time_module.sleep(0.15)
        total_after, idle_after = _cpu_sample()
        total_delta = max(1, total_after - total_before)
        cpu_percent = round(
            max(0.0, min(100.0, (1 - ((idle_after - idle_before) / total_delta)) * 100)),
            1,
        )

        meminfo = {}
        with open("/proc/meminfo", "r", encoding="ascii") as proc_mem:
            for line in proc_mem:
                key, raw = line.split(":", 1)
                meminfo[key] = int(raw.strip().split()[0])
        total_memory = int(meminfo.get("MemTotal") or 0)
        available_memory = int(meminfo.get("MemAvailable") or 0)
        memory_percent = round(
            ((total_memory - available_memory) / total_memory) * 100,
            1,
        ) if total_memory else 0.0
        report["cpu_percent"] = cpu_percent
        report["memory_percent"] = memory_percent
        if cpu_percent >= _threshold("CPU_ALERT_PERCENT", 85):
            report["alerts"].append(f"CPU usage at {cpu_percent}%.")
            opmetrics.increment("infra.cpu.high")
        if memory_percent >= _threshold("MEMORY_ALERT_PERCENT", 85):
            report["alerts"].append(f"Memory usage at {memory_percent}%.")
            opmetrics.increment("infra.memory.high")
    except Exception:
        logger.debug("CPU/memory capacity probe failed", exc_info=True)

    try:
        disk = shutil.disk_usage("/")
        disk_percent = round((disk.used / disk.total) * 100, 1) if disk.total else 0.0
        report["disk_percent"] = disk_percent
        if disk_percent >= _threshold("DISK_ALERT_PERCENT", 80):
            report["alerts"].append(
                f"Disk usage at {disk_percent}% ({round(disk.free / (1024 ** 3), 1)} GB free)."
            )
            opmetrics.increment("infra.disk.high")
    except Exception:
        logger.debug("Disk capacity probe failed", exc_info=True)

    try:
        import redis as redis_client

        broker_url = str(getattr(settings, "CELERY_BROKER_URL", "") or "")
        broker = redis_client.from_url(broker_url, socket_connect_timeout=2, socket_timeout=2)
        queue_limit = _threshold("CELERY_QUEUE_ALERT_LENGTH", 200)
        for queue_name in ("default", "notifications", "images", "periodic"):
            length = int(broker.llen(queue_name) or 0)
            report["queue_lengths"][queue_name] = length
            if length >= queue_limit:
                report["alerts"].append(
                    f"Celery queue '{queue_name}' contains {length} pending tasks."
                )
                opmetrics.increment(f"infra.queue.high.{queue_name}")
    except Exception:
        logger.debug("Celery queue probe failed", exc_info=True)

    # Application health from the current UTC-hour bucket. Minimum samples keep
    # one isolated failure/slow request from producing a misleading percentage.
    try:
        metric_snapshot = opmetrics.snapshot()
        request_count = int(metric_snapshot.get("http.requests.total") or 0)
        error_count = int(metric_snapshot.get("http.responses.5xx") or 0)
        timing_count = int(metric_snapshot.get("http.response.duration.count") or 0)
        timing_sum = int(metric_snapshot.get("http.response.duration.sum_ms") or 0)
        min_samples = _threshold("HTTP_ALERT_MIN_SAMPLES", 20)
        if request_count:
            error_percent = round(error_count * 100 / request_count, 2)
            report["http_5xx_percent"] = error_percent
            if request_count >= min_samples and error_percent >= float(
                getattr(settings, "HTTP_5XX_ALERT_PERCENT", 2.0) or 2.0
            ):
                report["alerts"].append(
                    f"HTTP 5xx rate at {error_percent}% ({error_count}/{request_count})."
                )
                opmetrics.increment("infra.http.5xx_high")
        if timing_count:
            average_ms = round(timing_sum / timing_count, 1)
            report["http_average_ms"] = average_ms
            if timing_count >= min_samples and average_ms >= _threshold(
                "HTTP_LATENCY_ALERT_MS", 2000
            ):
                report["alerts"].append(
                    f"Average HTTP response time at {average_ms} ms ({timing_count} samples)."
                )
                opmetrics.increment("infra.http.latency_high")
    except Exception:
        logger.debug("HTTP operational metrics probe failed", exc_info=True)

    if report["alerts"]:
        try:
            from .telegram_alerts import TelegramAlert, queue_telegram_alert

            queue_telegram_alert(
                TelegramAlert(
                    # One alert per hour while a condition persists: prompt
                    # enough for an outage, bounded enough to avoid alert spam.
                    event_key=(
                        "infra:capacity:"
                        + timezone.localtime().strftime("%Y%m%d%H")
                    ),
                    category="support",
                    text="⚠️ <b>تنبيه سعة البنية التحتية</b>\n" + "\n".join(report["alerts"]),
                )
            )
        except Exception:
            logger.exception("Unable to queue infrastructure capacity alert")

    try:
        from operations.tasks import store_capacity_snapshot_task

        store_capacity_snapshot_task.delay(report)
    except Exception:
        logger.exception("Unable to queue operations capacity snapshot")

    logger.info(
        "Task success name=monitor_infrastructure_capacity_task task_id=%s trace_id=%s retries=%s report=%s",
        task_id,
        trace_id,
        retries,
        report,
    )
    return report


@shared_task(bind=True, ignore_result=True, autoretry_for=(Exception,), retry_backoff=True, retry_jitter=True, retry_kwargs={"max_retries": 3})
def cleanup_expired_sessions_task(self, chunk_size: int = 5000) -> int:
    """Delete expired ``django_session`` rows.

    Django never prunes this table on its own. Public traffic (registration
    forms, logins, checkout returns) keeps adding rows, so without this job the
    table grows without bound and every ``cached_db`` session miss gets slower.
    Deleting in chunks keeps the statement short enough to avoid long locks on
    a busy database.
    """
    Session = apps.get_model("sessions", "Session")
    task_id, retries, trace_id = _task_ctx(self)
    logger.info(
        "Task start name=cleanup_expired_sessions_task task_id=%s trace_id=%s retries=%s",
        task_id,
        trace_id,
        retries,
    )

    chunk_size = max(int(chunk_size), 100)
    expired = Session.objects.filter(expire_date__lt=timezone.now()).order_by("expire_date")

    deleted_total = 0
    while True:
        batch_keys = list(expired.values_list("session_key", flat=True)[:chunk_size])
        if not batch_keys:
            break
        deleted, _ = Session.objects.filter(session_key__in=batch_keys).delete()
        deleted_total += int(deleted)

    logger.info(
        "Task success name=cleanup_expired_sessions_task task_id=%s trace_id=%s deleted=%s",
        task_id,
        trace_id,
        deleted_total,
    )
    opmetrics.increment("celery.task.success.cleanup_expired_sessions_task")
    return deleted_total


@shared_task(bind=True, ignore_result=True, autoretry_for=(Exception,), retry_backoff=True, retry_jitter=True, retry_kwargs={"max_retries": 3}, rate_limit="30/m")
def process_report_images(self, report_id: int) -> bool:
    """
    Task to process images for a report (compression/optimization).
    """
    task_id, retries, trace_id = _task_ctx(self)
    logger.info(
        "Task start name=process_report_images task_id=%s trace_id=%s retries=%s report_id=%s",
        task_id,
        trace_id,
        retries,
        report_id,
    )

    Report = apps.get_model("reports", "Report")
    try:
        report = Report.objects.get(pk=report_id)
    except Report.DoesNotExist:
        logger.error("Report %s not found for image processing.", report_id)
        opmetrics.increment("celery.task.failure.process_report_images")
        return False

    updated = False
    fields = ["image1", "image2", "image3", "image4"]

    for field_name in fields:
        image_field = getattr(report, field_name, None)
        if not image_field or not hasattr(image_field, "file"):
            continue

        try:
            processed_file = _compress_image_file(image_field.file)
            if not processed_file:
                continue

            # مقارنة آمنة: لو الحجم تغيّر نعتبره تحديث
            try:
                old_size = getattr(image_field.file, "size", None)
                new_size = getattr(processed_file, "size", None)
            except Exception:
                old_size, new_size = None, None

            if (new_size is not None and old_size is not None and new_size != old_size) or processed_file != image_field.file:
                image_field.save(image_field.name, processed_file, save=False)
                updated = True

        except Exception as e:
            logger.exception("Error processing %s for report %s: %s", field_name, report_id, e)
            opmetrics.increment("celery.task.failure.process_report_images")

    if updated:
        report.save(update_fields=fields)
        logger.info(
            "Task success name=process_report_images task_id=%s trace_id=%s report_id=%s updated=%s",
            task_id,
            trace_id,
            report_id,
            updated,
        )
    opmetrics.increment("celery.task.success.process_report_images")

    return True


@shared_task(bind=True, ignore_result=True, autoretry_for=(Exception,), retry_backoff=True, retry_jitter=True, retry_kwargs={"max_retries": 3}, rate_limit="30/m")
def process_ticket_image(self, ticket_image_id: int) -> bool:
    """
    Task to process a single ticket image (compression/optimization).
    """
    task_id, retries, trace_id = _task_ctx(self)
    logger.info(
        "Task start name=process_ticket_image task_id=%s trace_id=%s retries=%s ticket_image_id=%s",
        task_id,
        trace_id,
        retries,
        ticket_image_id,
    )

    TicketImage = apps.get_model("reports", "TicketImage")
    try:
        ticket_image = TicketImage.objects.get(pk=ticket_image_id)
    except TicketImage.DoesNotExist:
        logger.error("TicketImage %s not found for image processing.", ticket_image_id)
        opmetrics.increment("celery.task.failure.process_ticket_image")
        return False

    image_field = getattr(ticket_image, "image", None)
    if not image_field or not hasattr(image_field, "file"):
        opmetrics.increment("celery.task.failure.process_ticket_image")
        return False

    try:
        processed_file = _compress_image_file(image_field.file)
        if not processed_file:
            return True

        try:
            old_size = getattr(image_field.file, "size", None)
            new_size = getattr(processed_file, "size", None)
        except Exception:
            old_size, new_size = None, None

        if (new_size is not None and old_size is not None and new_size != old_size) or processed_file != image_field.file:
            image_field.save(image_field.name, processed_file, save=False)
            ticket_image.save(update_fields=["image"])
            logger.info(
                "Task success name=process_ticket_image task_id=%s trace_id=%s ticket_image_id=%s updated=%s",
                task_id,
                trace_id,
                ticket_image_id,
                True,
            )

        opmetrics.increment("celery.task.success.process_ticket_image")

        return True

    except Exception as e:
        logger.exception(
            "Task failure name=process_ticket_image task_id=%s trace_id=%s ticket_image_id=%s error=%s",
            task_id,
            trace_id,
            ticket_image_id,
            e,
        )
        opmetrics.increment("celery.task.failure.process_ticket_image")
        return False


@shared_task(bind=True, ignore_result=True, autoretry_for=(Exception,), retry_backoff=True, retry_jitter=True, retry_kwargs={"max_retries": 3}, soft_time_limit=600, time_limit=900)
def send_notification_task(self, notification_id: int, teacher_ids=None) -> bool:
    """
    Task to create NotificationRecipient objects in the background.
    """
    task_id, retries, trace_id = _task_ctx(self)
    logger.info(
        "Task start name=send_notification_task task_id=%s trace_id=%s retries=%s notification_id=%s explicit_recipients=%s",
        task_id,
        trace_id,
        retries,
        notification_id,
        0 if not teacher_ids else len(teacher_ids),
    )

    Notification = apps.get_model("reports", "Notification")
    NotificationRecipient = apps.get_model("reports", "NotificationRecipient")
    Teacher = apps.get_model("reports", "Teacher")

    try:
        n = Notification.objects.get(pk=notification_id)
    except Notification.DoesNotExist:
        logger.error("Notification %s not found.", notification_id)
        opmetrics.increment("celery.task.failure.send_notification_task")
        return False

    if teacher_ids:
        teachers = Teacher.objects.filter(pk__in=teacher_ids, is_active=True).only("id")
    else:
        qs = (
            Teacher.objects.filter(is_active=True)
            .filter(
                school_memberships__school__is_active=True,
            )
            .distinct()
            .only("id")
        )
        _role_types = ["teacher"]
        if getattr(n, "school", None):
            qs = qs.filter(
                school_memberships__school=n.school,
                school_memberships__is_active=True,
                school_memberships__role_type__in=_role_types,
            ).distinct()
        else:
            qs = qs.filter(
                school_memberships__is_active=True,
                school_memberships__role_type__in=_role_types,
            ).distinct()

        teachers = qs

    batch_size = 500

    try:
        from .realtime_notifications import push_new_notification_to_teachers
    except Exception:
        push_new_notification_to_teachers = None

    # Stream teachers in chunks via values_list to avoid loading all objects
    # into memory.  At 50K schools × 25 teachers = 1.25M users, the old
    # `list(teachers)` would consume gigabytes of RAM.
    teacher_id_qs = teachers.values_list("id", flat=True)
    total_recipients = 0

    batch_ids: list[int] = []
    for tid in teacher_id_qs.iterator(chunk_size=batch_size):
        batch_ids.append(tid)
        if len(batch_ids) >= batch_size:
            NotificationRecipient.objects.bulk_create(
                [NotificationRecipient(notification=n, teacher_id=t) for t in batch_ids],
                ignore_conflicts=True,
            )
            if push_new_notification_to_teachers is not None:
                try:
                    push_new_notification_to_teachers(
                        notification=n,
                        teacher_ids=batch_ids,
                        trace_id=trace_id,
                    )
                except Exception:
                    # الإشعار حُفظ فعلاً؛ ما فشل هو الدفع اللحظي. المستلم يراه
                    # عند التحديث التالي — لكن «لا يصل فوراً» عطلٌ يجب أن يُقاس.
                    _degraded("realtime.push_batch", count=len(batch_ids))
            total_recipients += len(batch_ids)
            batch_ids = []

    # Flush remaining
    if batch_ids:
        NotificationRecipient.objects.bulk_create(
            [NotificationRecipient(notification=n, teacher_id=t) for t in batch_ids],
            ignore_conflicts=True,
        )
        if push_new_notification_to_teachers is not None:
            try:
                push_new_notification_to_teachers(
                    notification=n,
                    teacher_ids=batch_ids,
                    trace_id=trace_id,
                )
            except Exception:
                _degraded("realtime.push_batch_tail", count=len(batch_ids))
        total_recipients += len(batch_ids)

    logger.info(
        "Task success name=send_notification_task task_id=%s trace_id=%s notification_id=%s recipients=%s",
        task_id,
        trace_id,
        notification_id,
        total_recipients,
    )
    opmetrics.increment("celery.task.success.send_notification_task")
    return True


def _is_valid_email(value: str) -> bool:
    email = (value or "").strip()
    if not email:
        return False
    try:
        validate_email(email)
        return True
    except ValidationError:
        return False


def _build_school_details_url(school_id: int) -> str:
    base = (getattr(settings, "SITE_URL", "") or "").strip().rstrip("/")
    path = f"/staff/schools/{int(school_id)}/profile/"
    if base:
        return f"{base}{path}"
    return path


def _build_weekly_message(
    school_name: str,
    period_text: str,
    reports_count: int,
    open_tickets_count: int,
    closed_tickets_count: int,
    details_url: str,
) -> str:
    return (
        f"الملخص الأسبوعي - {school_name}\n\n"
        f"فترة التقرير: {period_text}\n"
        f"عدد التقارير: {int(reports_count)}\n"
        f"البلاغات المفتوحة: {int(open_tickets_count)}\n"
        f"البلاغات المغلقة: {int(closed_tickets_count)}\n\n"
        "عرض التفاصيل:\n"
        f"{details_url}"
    )


def _weekly_summary_window(reference_dt=None):
    """Return the weekly reporting window: Sunday 00:00 -> Thursday 16:00 (exclusive)."""
    tz = timezone.get_current_timezone()
    now_local = timezone.localtime(reference_dt or timezone.now(), tz)

    # Python weekday: Monday=0 ... Sunday=6, Thursday=3
    days_since_thursday = (now_local.weekday() - 3) % 7
    thursday_date = (now_local - timedelta(days=days_since_thursday)).date()
    end_dt = timezone.make_aware(datetime.combine(thursday_date, dt_time(hour=16, minute=0)), tz)

    # If run before this week's Thursday 16:00, use the previous week's window.
    if now_local < end_dt:
        end_dt -= timedelta(days=7)

    start_date = (end_dt - timedelta(days=4)).date()  # Sunday
    start_dt = timezone.make_aware(datetime.combine(start_date, dt_time.min), tz)
    return start_dt, end_dt


def _send_inapp_notification(
    *,
    school,
    manager_ids: list[int],
    subject: str,
    message_text: str,
) -> bool:
    if not manager_ids:
        return False

    Notification = apps.get_model("reports", "Notification")
    NotificationRecipient = apps.get_model("reports", "NotificationRecipient")

    try:
        notification = Notification.objects.create(
            title=subject,
            message=message_text,
            school=school,
            is_important=True,
        )
        NotificationRecipient.objects.bulk_create(
            [
                NotificationRecipient(
                    notification=notification,
                    teacher_id=manager_id,
                )
                for manager_id in manager_ids
            ],
            ignore_conflicts=True,
        )
    except Exception:
        logger.exception(
            "Daily manager in-app notification create failed for school=%s",
            getattr(school, "id", None),
        )
        return False

    try:
        from .realtime_notifications import push_new_notification_to_teachers
    except Exception:
        push_new_notification_to_teachers = None

    if push_new_notification_to_teachers is not None:
        with soft_fail("realtime.push_managers", count=len(manager_ids)):
            push_new_notification_to_teachers(
                notification=notification,
                teacher_ids=manager_ids,
            )

    return True


@shared_task(ignore_result=True, soft_time_limit=60, time_limit=120)
def _daily_summary_for_school(school_id: int) -> dict:
    """Process weekly manager summary for a single school (fan-out subtask)."""
    School = apps.get_model("reports", "School")
    SchoolMembership = apps.get_model("reports", "SchoolMembership")
    Report = apps.get_model("reports", "Report")
    Ticket = apps.get_model("reports", "Ticket")

    inapp_enabled = bool(getattr(settings, "DAILY_MANAGER_REPORT_INAPP_ENABLED", True))

    week_start, week_end = _weekly_summary_window()
    period_text = f"{week_start.strftime('%Y-%m-%d')} إلى {week_end.strftime('%Y-%m-%d %H:%M')}"
    open_ticket_statuses = ("open", "in_progress")
    closed_ticket_statuses = ("done", "rejected")

    result = {
        "school_id": school_id,
        "processed": False,
        "inapp_sent": 0,
    }

    try:
        school = School.objects.filter(pk=school_id, is_active=True).only("id", "name").first()
    except Exception:
        school = None
    if school is None:
        return result

    # The summary is delivered in-app, so the managers' ids are all that is
    # needed — no contact details are read for this task.
    manager_ids = list(
        SchoolMembership.objects.filter(
            school=school, role_type="manager", is_active=True, teacher__is_active=True
        )
        .values_list("teacher_id", flat=True)
        .distinct()
    )
    if not manager_ids:
        return result

    reports_count = Report.objects.filter(
        school=school, created_at__gte=week_start, created_at__lt=week_end,
    ).count()

    ticket_agg = Ticket.objects.filter(
        school=school,
        created_at__gte=week_start,
        created_at__lt=week_end,
    ).aggregate(
        open=Count("id", filter=Q(status__in=open_ticket_statuses)),
        closed=Count("id", filter=Q(status__in=closed_ticket_statuses)),
    )

    details_url = _build_school_details_url(school.id)
    message_text = _build_weekly_message(
        school_name=getattr(school, "name", "") or "المدرسة",
        period_text=period_text,
        reports_count=reports_count,
        open_tickets_count=ticket_agg["open"],
        closed_tickets_count=ticket_agg["closed"],
        details_url=details_url,
    )
    subject = f"الملخص الأسبوعي - {getattr(school, 'name', '') or 'المدرسة'}"

    if inapp_enabled:
        inapp_ok = _send_inapp_notification(
            school=school, manager_ids=manager_ids,
            subject=subject, message_text=message_text,
        )
        if inapp_ok:
            result["inapp_sent"] += len(manager_ids)

    result["processed"] = True
    return result


@shared_task(ignore_result=True, soft_time_limit=300, time_limit=600)
def send_daily_manager_summary_task() -> dict:
    """
    Weekly summary dispatcher — fans out to one subtask per active school.

    The summary is delivered as an in-app notification only: it is never
    emailed and there is no outbound webhook channel.
    """
    import time as _time
    _t0 = _time.monotonic()

    enabled = bool(getattr(settings, "DAILY_MANAGER_REPORT_ENABLED", True))

    if not _periodic_lock("daily_manager_summary", ttl=600):
        logger.info("Weekly manager summary task skipped: another instance is running.")
        return {"enabled": enabled, "skipped": "lock"}

    summary = {
        "enabled": enabled,
        "schools_seen": 0,
        "schools_processed": 0,
        "schools_without_manager": 0,
        "inapp_sent": 0,
        "inapp_failures": 0,
        "managers_missing_channels": 0,
    }

    if not enabled:
        logger.info("Weekly manager summary task skipped: feature disabled.")
        return summary

    School = apps.get_model("reports", "School")

    school_ids = list(
        School.objects.filter(is_active=True).values_list("id", flat=True)
    )
    summary["schools_seen"] = len(school_ids)

    # Fan-out: dispatch one subtask per school to the periodic queue.
    # Each subtask runs independently with its own time limits.
    dispatched = 0
    for sid in school_ids:
        try:
            _daily_summary_for_school.delay(sid)
            dispatched += 1
        except Exception:
            logger.exception("Failed to dispatch weekly summary for school=%s", sid)

    summary["schools_processed"] = dispatched
    logger.info("Weekly manager summary dispatched %d/%d school subtasks", dispatched, len(school_ids))
    opmetrics.timing("celery.periodic.daily_manager_summary", (_time.monotonic() - _t0) * 1000)
    return summary


# ═══════════════════════════════════════════════════════════════
# مهمة 1: تذكير بقرب انتهاء الاشتراك
# ═══════════════════════════════════════════════════════════════
@shared_task(ignore_result=True, soft_time_limit=120, time_limit=300)
def check_subscription_expiry_task() -> dict:
    """
    تفحص الاشتراكات النشطة وترسل إشعارات عند اقتراب انتهائها.

    - تعمل يومياً عبر Celery Beat.
    - ترسل إشعار داخلي + إيميل (اختياري) لمدراء المدارس.
    - تتجنب التكرار بفحص عدم وجود إشعار مماثل خلال آخر 24 ساعة.
    """
    import time as _time
    _t0 = _time.monotonic()

    enabled = bool(getattr(settings, "SUBSCRIPTION_EXPIRY_REMINDER_ENABLED", True))

    if not _periodic_lock("check_subscription_expiry", ttl=300):
        logger.info("Subscription expiry task skipped: another instance is running.")
        return {"enabled": enabled, "skipped": "lock"}

    # A production host still on placeholder SMTP would raise once per manager
    # per school; skip the channel instead and leave the in-app notice standing.
    email_enabled = bool(
        getattr(settings, "SUBSCRIPTION_EXPIRY_REMINDER_EMAIL_ENABLED", True)
    ) and _email_delivery_configured()
    if not email_enabled:
        logger.info("Subscription expiry reminder: email channel is off or unconfigured.")
    reminder_days = getattr(settings, "SUBSCRIPTION_EXPIRY_REMINDER_DAYS", [14, 7, 3, 1])
    from_email = (getattr(settings, "DEFAULT_FROM_EMAIL", "") or "no-reply@tawtheeq-ksa.com").strip()

    summary = {
        "enabled": enabled,
        "subscriptions_checked": 0,
        "reminders_sent": 0,
        "emails_sent": 0,
        "skipped_duplicate": 0,
    }

    if not enabled:
        logger.info("Subscription expiry reminder task skipped: feature disabled.")
        return summary

    SchoolSubscription = apps.get_model("reports", "SchoolSubscription")
    SchoolMembership = apps.get_model("reports", "SchoolMembership")
    Notification = apps.get_model("reports", "Notification")
    NotificationRecipient = apps.get_model("reports", "NotificationRecipient")

    today = timezone.localdate()
    now = timezone.now()
    dedup_cutoff = now - timedelta(hours=24)

    subs = (
        SchoolSubscription.objects
        .filter(is_active=True)
        .select_related("school", "plan")
        .only("id", "school__id", "school__name", "plan__name", "end_date", "is_active", "canceled_at")
    )

    for sub in subs.iterator():
        if sub.canceled_at:
            continue
        summary["subscriptions_checked"] += 1

        days_left = (sub.end_date - today).days
        if days_left < 0 or days_left not in reminder_days:
            continue

        school = sub.school
        school_name = getattr(school, "name", "")

        # تجنب التكرار: لا نرسل نفس التنبيه مرتين خلال 24 ساعة
        dedup_title = f"⏰ اشتراك {school_name} ينتهي خلال {days_left}"
        already_sent = Notification.objects.filter(
            title=dedup_title,
            school=school,
            created_at__gte=dedup_cutoff,
        ).exists()
        if already_sent:
            summary["skipped_duplicate"] += 1
            continue

        # جلب مدراء المدرسة
        manager_ids = list(
            SchoolMembership.objects.filter(
                school=school,
                role_type="manager",
                is_active=True,
                teacher__is_active=True,
            ).values_list("teacher_id", flat=True)
        )
        if not manager_ids:
            continue

        if days_left == 1:
            message = f"⚠️ اشتراك مدرسة {school_name} (باقة {sub.plan.name}) ينتهي غداً!\nيرجى تجديد الاشتراك لتجنب توقف الخدمة."
        elif days_left <= 3:
            message = f"⚠️ اشتراك مدرسة {school_name} (باقة {sub.plan.name}) ينتهي خلال {days_left} أيام.\nيرجى تجديد الاشتراك قريباً."
        else:
            message = f"تنبيه: اشتراك مدرسة {school_name} (باقة {sub.plan.name}) ينتهي خلال {days_left} يوماً.\nيرجى التجديد في الوقت المناسب."

        # إشعار داخلي
        notification = Notification.objects.create(
            title=dedup_title,
            message=message,
            school=school,
            is_important=(days_left <= 3),
        )
        NotificationRecipient.objects.bulk_create(
            [NotificationRecipient(notification=notification, teacher_id=mid) for mid in manager_ids],
            ignore_conflicts=True,
        )

        with soft_fail("realtime.push_subscription_notice", count=len(manager_ids)):
            from .realtime_notifications import push_new_notification_to_teachers
            push_new_notification_to_teachers(notification=notification, teacher_ids=manager_ids)

        summary["reminders_sent"] += 1

        # إيميل
        if email_enabled:
            # ``dedup_title`` is the de-duplication key, not a sentence — it
            # reads "ينتهي خلال 14" with no unit. The subject line is written
            # for the inbox instead.
            email_subject = (
                f"اشتراك {school_name} ينتهي "
                + ("غداً" if days_left == 1 else f"خلال {days_left} يوماً")
                + " | منصة توثيق"
            )
            Teacher = apps.get_model("reports", "Teacher")
            managers_with_email = Teacher.objects.filter(
                id__in=manager_ids, is_active=True
            ).exclude(email="").only("id", "email", "name")
            for mgr in managers_with_email:
                if _is_valid_email(mgr.email):
                    try:
                        expiry_phrase = "غدًا" if days_left == 1 else f"خلال {days_left} أيام"
                        remaining_text = "يوم واحد" if days_left == 1 else f"{days_left} أيام"
                        expiry_context = email_brand_context(
                            recipient_name=(mgr.name or "مدير المدرسة").strip(),
                            school_name=school_name,
                            plan_name=sub.plan.name,
                            end_date=sub.end_date.strftime("%Y-%m-%d"),
                            remaining_text=remaining_text,
                            action_url=platform_url("/subscription/my/"),
                        )
                        plain_message = render_to_string(
                            "reports/emails/subscription_expiry.txt", expiry_context
                        ).strip()
                        html_message = render_branded_email(
                            "subscription_expiry.html",
                            recipient_name=expiry_context["recipient_name"],
                            email_title=f"اشتراك {school_name} ينتهي {expiry_phrase}",
                            email_eyebrow="تنبيه الاشتراك",
                            email_preheader=f"متبقي {remaining_text} على انتهاء باقة {sub.plan.name}.",
                            email_tone="warning",
                            action_url=expiry_context["action_url"],
                            action_label="إدارة الاشتراك والتجديد",
                            meta_items=[
                                {"label": "المدرسة", "value": school_name},
                                {"label": "الباقة", "value": sub.plan.name},
                                {"label": "تاريخ الانتهاء", "value": expiry_context["end_date"]},
                                {"label": "المدة المتبقية", "value": remaining_text},
                            ],
                            notice_title="حافظ على استمرارية الخدمة",
                            notice_text="أكمل التجديد قبل تاريخ الانتهاء لتفادي توقف وصول فريق المدرسة إلى المنصة.",
                        )
                        send_mail(
                            subject=email_subject,
                            message=plain_message,
                            from_email=from_email,
                            recipient_list=[mgr.email],
                            html_message=html_message,
                            fail_silently=False,
                        )
                        summary["emails_sent"] += 1
                    except Exception:
                        logger.exception("Subscription expiry email failed for teacher=%s", mgr.id)

    logger.info("Subscription expiry reminder result: %s", summary)
    opmetrics.timing("celery.periodic.check_subscription_expiry", (_time.monotonic() - _t0) * 1000)
    return summary


@shared_task(bind=True, ignore_result=True)
def send_subscription_activation_email_task(self, payment_id: int) -> dict:
    """Email school managers after a paid subscription has actually activated."""
    task_id, retries, trace_id = _task_ctx(self)
    summary = {"sent": 0, "skipped": 0, "failed": 0}
    logger.info(
        "Task start name=send_subscription_activation_email_task task_id=%s trace_id=%s retries=%s payment_id=%s",
        task_id,
        trace_id,
        retries,
        payment_id,
    )

    if not bool(getattr(settings, "SUBSCRIPTION_ACTIVATION_EMAIL_ENABLED", True)):
        summary["skipped"] = 1
        return summary
    if not _email_delivery_configured():
        logger.warning("Subscription activation email skipped: production SMTP is not configured.")
        summary["skipped"] = 1
        return summary

    Payment = apps.get_model("reports", "Payment")
    SchoolMembership = apps.get_model("reports", "SchoolMembership")
    Teacher = apps.get_model("reports", "Teacher")

    payment = (
        Payment.objects.select_related("school", "subscription__plan", "requested_plan")
        .filter(
            pk=payment_id,
            purpose=Payment.Purpose.SUBSCRIPTION,
            status=Payment.Status.APPROVED,
            effects_applied_at__isnull=False,
        )
        .first()
    )
    if payment is None:
        summary["skipped"] = 1
        return summary

    school = payment.school
    subscription = payment.subscription
    if subscription is None:
        try:
            subscription = school.subscription
        except Exception:
            subscription = None
    plan = getattr(subscription, "plan", None) or payment.requested_plan
    if subscription is None or plan is None:
        summary["skipped"] = 1
        return summary

    manager_ids = list(
        SchoolMembership.objects.filter(
            school=school,
            role_type=SchoolMembership.RoleType.MANAGER,
            is_active=True,
            teacher__is_active=True,
        ).values_list("teacher_id", flat=True)
    )
    managers = list(
        Teacher.objects.filter(id__in=manager_ids, is_active=True)
        .exclude(email="")
        .only("id", "name", "email")
    )
    recipients = [manager for manager in managers if _is_valid_email(manager.email)]
    if not recipients:
        summary["skipped"] = 1
        return summary

    from django.urls import reverse

    action_url = platform_url(reverse("reports:my_subscription"))
    invoice_url = platform_url(reverse("reports:subscription_invoice", args=[payment.pk]))
    from_email = (getattr(settings, "DEFAULT_FROM_EMAIL", "") or "no-reply@tawtheeq-ksa.com").strip()
    method_label = payment.get_payment_method_display()
    teacher_limit = int(getattr(subscription, "teacher_limit", 0) or 0)
    teacher_limit_label = "غير محدود" if teacher_limit <= 0 else f"{teacher_limit} مستخدم"
    paid_amount = f"{payment.amount:.2f}"
    currency = "SAR"
    start_date = subscription.start_date.strftime("%Y-%m-%d") if subscription.start_date else ""
    end_date = subscription.end_date.strftime("%Y-%m-%d") if subscription.end_date else ""
    subject = f"تم تفعيل اشتراك {school.name} | منصة توثيق"

    for manager in recipients:
        context = email_brand_context(
            recipient_name=(manager.name or "مدير المدرسة").strip(),
            school_name=school.name,
            plan_name=plan.name,
            start_date=start_date,
            end_date=end_date,
            teacher_limit=teacher_limit_label,
            paid_amount=paid_amount,
            currency=currency,
            action_url=action_url,
        )
        plain_message = render_to_string(
            "reports/emails/subscription_activated.txt", context
        ).strip()
        html_message = render_branded_email(
            "subscription_activated.html",
            recipient_name=context["recipient_name"],
            email_title="تم تفعيل الاشتراك",
            email_eyebrow="اشتراك المدرسة",
            email_preheader=f"تم تفعيل باقة {plan.name} لمدرسة {school.name}.",
            email_tone="success",
            action_url=action_url,
            action_label="إدارة الاشتراك وعرض الفاتورة",
            meta_items=[
                {"label": "المدرسة", "value": school.name},
                {"label": "الباقة", "value": plan.name},
                {"label": "تاريخ البداية", "value": start_date},
                {"label": "تاريخ الانتهاء", "value": end_date},
                {"label": "سعة المستخدمين", "value": teacher_limit_label},
                {"label": "المبلغ المعتمد", "value": f"{paid_amount} {currency}"},
                {"label": "طريقة السداد", "value": method_label},
            ],
            notice_title="الفاتورة متاحة داخل حسابك",
            notice_text=f"يمكنك فتح سجل المدفوعات أو عرض فاتورة الاشتراك من صفحة الاشتراك: {invoice_url}",
        )
        try:
            send_mail(
                subject=subject,
                message=plain_message,
                from_email=from_email,
                recipient_list=[manager.email],
                html_message=html_message,
                fail_silently=False,
            )
            summary["sent"] += 1
        except Exception:
            summary["failed"] += 1
            logger.exception(
                "Subscription activation email failed teacher=%s payment=%s",
                manager.id,
                payment.pk,
            )

    logger.info(
        "Task success name=send_subscription_activation_email_task task_id=%s trace_id=%s payment_id=%s summary=%s",
        task_id,
        trace_id,
        payment_id,
        summary,
    )
    return summary


# ═══════════════════════════════════════════════════════════════
# مهمة 2: تذكير بالتعاميم غير الموقّعة قبل الموعد النهائي
# ═══════════════════════════════════════════════════════════════
@shared_task(ignore_result=True, soft_time_limit=120, time_limit=300)
def remind_unsigned_circulars_task() -> dict:
    """
    ترسل تذكيرات للمعلمين الذين لم يوقّعوا على تعاميم لها موعد نهائي قريب.

    - تعمل مرتين يومياً عبر Celery Beat.
    - تفحص التعاميم ذات `requires_signature=True` و `signature_deadline_at` قريب.
    - ترسل إشعار داخلي فقط للمعلمين الذين لم يوقّعوا بعد.
    - تتجنب التكرار بعدم التذكير أكثر من مرة واحدة لنفس المستلم لنفس التعميم خلال 12 ساعة.
    """
    import time as _time
    _t0 = _time.monotonic()

    enabled = bool(getattr(settings, "CIRCULAR_SIGNATURE_REMINDER_ENABLED", True))

    if not _periodic_lock("remind_unsigned_circulars", ttl=300):
        logger.info("Unsigned circular reminder task skipped: another instance is running.")
        return {"enabled": enabled, "skipped": "lock"}

    reminder_hours = getattr(settings, "CIRCULAR_SIGNATURE_REMINDER_HOURS", [48, 24])

    summary = {
        "enabled": enabled,
        "circulars_checked": 0,
        "reminders_sent": 0,
        "skipped_duplicate": 0,
    }

    if not enabled:
        logger.info("Circular signature reminder task skipped: feature disabled.")
        return summary

    Notification = apps.get_model("reports", "Notification")
    NotificationRecipient = apps.get_model("reports", "NotificationRecipient")

    now = timezone.now()
    dedup_cutoff = now - timedelta(hours=12)

    # أكبر عدد ساعات في القائمة يحدد نافذة البحث
    max_hours = max(reminder_hours) if reminder_hours else 48
    window_end = now + timedelta(hours=max_hours)

    # التعاميم التي تتطلب توقيع ولها موعد نهائي بين الآن ونهاية النافذة
    circulars = Notification.objects.filter(
        requires_signature=True,
        signature_deadline_at__gt=now,
        signature_deadline_at__lte=window_end,
    ).select_related("school").only(
        "id", "title", "signature_deadline_at", "school__id", "school__name"
    )

    for circular in circulars.iterator():
        hours_until_deadline = (circular.signature_deadline_at - now).total_seconds() / 3600
        summary["circulars_checked"] += 1

        # تحديد هل يقع الموعد ضمن إحدى نوافذ التذكير
        should_remind = False
        for h in sorted(reminder_hours):
            if hours_until_deadline <= h:
                should_remind = True
                break

        if not should_remind:
            continue

        # المعلمون الذين لم يوقّعوا بعد
        unsigned_recipients = NotificationRecipient.objects.filter(
            notification=circular,
            is_signed=False,
        ).values_list("teacher_id", flat=True)

        unsigned_ids = list(unsigned_recipients)
        if not unsigned_ids:
            continue

        # تجنب التكرار: لا نذكّر نفس المعلمين عن نفس التعميم خلال 12 ساعة
        dedup_title = f"🔔 تذكير بالتوقيع: {circular.title[:60]}"
        existing_reminder = Notification.objects.filter(
            title=dedup_title,
            school=circular.school,
            created_at__gte=dedup_cutoff,
        ).first()

        if existing_reminder:
            # تحقق هل المستلمون أنفسهم موجودون بالفعل
            already_reminded = set(
                NotificationRecipient.objects.filter(
                    notification=existing_reminder,
                    teacher_id__in=unsigned_ids,
                ).values_list("teacher_id", flat=True)
            )
            unsigned_ids = [uid for uid in unsigned_ids if uid not in already_reminded]
            if not unsigned_ids:
                summary["skipped_duplicate"] += 1
                continue

        hours_display = int(hours_until_deadline)
        if hours_display >= 24:
            time_text = f"{hours_display // 24} يوم"
        else:
            time_text = f"{hours_display} ساعة"

        message = (
            f"لم يتم توقيعك على التعميم \"{circular.title}\" بعد.\n"
            f"الموعد النهائي للتوقيع: خلال {time_text}.\n"
            "يرجى التوقيع في أقرب وقت."
        )

        reminder_notif = Notification.objects.create(
            title=dedup_title,
            message=message,
            school=circular.school,
            is_important=True,
        )
        NotificationRecipient.objects.bulk_create(
            [NotificationRecipient(notification=reminder_notif, teacher_id=uid) for uid in unsigned_ids],
            ignore_conflicts=True,
        )

        with soft_fail("realtime.push_signature_reminder", count=len(unsigned_ids)):
            from .realtime_notifications import push_new_notification_to_teachers
            push_new_notification_to_teachers(notification=reminder_notif, teacher_ids=unsigned_ids)

        summary["reminders_sent"] += len(unsigned_ids)

    logger.info("Unsigned circular reminder result: %s", summary)
    opmetrics.timing("celery.periodic.remind_unsigned_circulars", (_time.monotonic() - _t0) * 1000)
    return summary


# ═══════════════════════════════════════════════════════════════
# مهمة 3: إرسال إيميل تأكيد تغيير كلمة المرور
# ═══════════════════════════════════════════════════════════════
@shared_task(bind=True, ignore_result=True, autoretry_for=(Exception,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def send_password_change_email_task(self, teacher_id: int) -> bool:
    """
    ترسل إيميل تأكيد للمعلم بعد تغيير كلمة المرور بنجاح.

    - تُستدعى من view الملف الشخصي بعد تغيير كلمة المرور.
    - ترسل فقط إذا كان لدى المعلم بريد إلكتروني صالح.
    - أفضل ممارسة أمنية لتنبيه المستخدم بأي تغيير في حسابه.
    """
    task_id, retries, trace_id = _task_ctx(self)
    logger.info(
        "Task start name=send_password_change_email_task task_id=%s trace_id=%s retries=%s teacher_id=%s",
        task_id,
        trace_id,
        retries,
        teacher_id,
    )

    enabled = bool(getattr(settings, "PASSWORD_CHANGE_EMAIL_ENABLED", True))
    if not enabled:
        opmetrics.increment("celery.task.failure.send_password_change_email_task")
        return False
    if not _email_delivery_configured():
        logger.warning("Password change email skipped: production SMTP is not configured.")
        opmetrics.increment("celery.task.failure.send_password_change_email_task")
        return False

    Teacher = apps.get_model("reports", "Teacher")
    try:
        teacher = Teacher.objects.get(pk=teacher_id, is_active=True)
    except Teacher.DoesNotExist:
        logger.warning("Password change email: teacher %s not found.", teacher_id)
        opmetrics.increment("celery.task.failure.send_password_change_email_task")
        return False

    email = (getattr(teacher, "email", "") or "").strip()
    if not _is_valid_email(email):
        logger.info("Password change email: teacher %s has no valid email.", teacher_id)
        opmetrics.increment("celery.task.failure.send_password_change_email_task")
        return False

    teacher_name = (getattr(teacher, "name", "") or "").strip() or "المستخدم"
    from_email = (getattr(settings, "DEFAULT_FROM_EMAIL", "") or "no-reply@tawtheeq-ksa.com").strip()
    now_text = timezone.localtime(timezone.now()).strftime("%Y-%m-%d %H:%M")

    subject = "تم تغيير كلمة المرور | منصة توثيق"
    email_context = email_brand_context(
        recipient_name=teacher_name,
        changed_at=now_text,
    )
    message = render_to_string(
        "reports/emails/password_changed.txt", email_context
    ).strip()
    html_message = render_branded_email(
        "password_changed.html",
        recipient_name=teacher_name,
        email_title="تم تغيير كلمة المرور",
        email_eyebrow="أمان الحساب",
        email_preheader=f"تأكيد تغيير كلمة مرور حساب {teacher_name} في منصة توثيق.",
        email_tone="success",
        meta_items=[{"label": "وقت التغيير", "value": now_text}],
        notice_title="لم تكن أنت؟",
        notice_text="تواصل فورًا مع إدارة المدرسة أو الدعم الفني لحماية حسابك والتحقق من النشاط.",
    )

    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=from_email,
            recipient_list=[email],
            html_message=html_message,
            fail_silently=False,
        )
        logger.info(
            "Task success name=send_password_change_email_task task_id=%s trace_id=%s teacher_id=%s",
            task_id,
            trace_id,
            teacher_id,
        )
        opmetrics.increment("celery.task.success.send_password_change_email_task")
        return True
    except Exception:
        logger.exception(
            "Task failure name=send_password_change_email_task task_id=%s trace_id=%s teacher_id=%s retries=%s",
            task_id,
            trace_id,
            teacher_id,
            retries,
        )
        opmetrics.increment("celery.task.failure.send_password_change_email_task")
        raise  # auto-retry


@shared_task(bind=True, ignore_result=True)
def check_archive_addon_expiry_task(self) -> dict:
    """Warn managers before the yearly-archive add-on lapses.

    Storage is a separate product, so lapsing no longer freezes uploads — it
    only stops the school creating new yearly snapshots. Snapshots already saved
    stay downloadable either way, and the reminder says so.
    """
    task_id, retries, trace_id = _task_ctx(self)
    summary = {"addons_checked": 0, "reminders_sent": 0, "skipped_duplicate": 0}

    if not bool(getattr(settings, "ARCHIVE_ADDON_EXPIRY_REMINDER_ENABLED", True)):
        return summary

    if not _periodic_lock("check_archive_addon_expiry", ttl=300):
        logger.info("Archive add-on expiry task skipped: another instance is running.")
        return {**summary, "skipped": "lock"}

    reminder_days = getattr(settings, "SUBSCRIPTION_EXPIRY_REMINDER_DAYS", [14, 7, 3, 1])

    SchoolArchiveAddon = apps.get_model("reports", "SchoolArchiveAddon")
    SchoolMembership = apps.get_model("reports", "SchoolMembership")
    Notification = apps.get_model("reports", "Notification")
    NotificationRecipient = apps.get_model("reports", "NotificationRecipient")
    School = apps.get_model("reports", "School")

    today = timezone.localdate()
    dedup_cutoff = timezone.now() - timedelta(hours=24)

    addons = (
        SchoolArchiveAddon.objects.filter(is_enabled=True, end_date__isnull=False)
        .select_related("school")
        .only("id", "end_date", "storage_limit_gb", "school__id", "school__name")
    )

    for addon in addons.iterator():
        days_left = (addon.end_date - today).days
        if days_left < 0 or days_left not in reminder_days:
            continue

        summary["addons_checked"] += 1
        school = addon.school
        school_name = getattr(school, "name", "")

        dedup_title = f"🗄️ أرشفة {school_name} تنتهي خلال {days_left}"
        if Notification.objects.filter(
            title=dedup_title, school=school, created_at__gte=dedup_cutoff
        ).exists():
            summary["skipped_duplicate"] += 1
            continue

        manager_ids = list(
            SchoolMembership.objects.filter(
                school=school,
                role_type="manager",
                is_active=True,
                teacher__is_active=True,
            ).values_list("teacher_id", flat=True)
        )
        if not manager_ids:
            continue

        lines = [
            f"إضافة الأرشفة السنوية لمدرسة {school_name} تنتهي خلال {days_left} يوماً "
            f"(بتاريخ {addon.end_date}).",
            "بعد الانتهاء لن تتمكن المدرسة من إنشاء نسخة سنوية جديدة حتى التجديد.",
            # Say plainly what does NOT break, so nobody renews out of fear of
            # losing data or storage.
            "النسخ المحفوظة تبقى قابلة للتنزيل، ومساحة التخزين لا تتأثر إطلاقاً "
            "لأنها مستقلة عن الأرشفة السنوية.",
        ]

        notification = Notification.objects.create(
            title=dedup_title,
            message="\n".join(lines),
            school=school,
            is_important=(days_left <= 3),
        )
        NotificationRecipient.objects.bulk_create(
            [
                NotificationRecipient(notification=notification, teacher_id=mid)
                for mid in manager_ids
            ],
            ignore_conflicts=True,
        )
        with soft_fail("realtime.push_storage_threshold_notice", count=len(manager_ids)):
            from .realtime_notifications import push_new_notification_to_teachers

            push_new_notification_to_teachers(
                notification=notification, teacher_ids=manager_ids
            )

        summary["reminders_sent"] += 1

    logger.info(
        "Task success name=check_archive_addon_expiry_task task_id=%s trace_id=%s retries=%s summary=%s",
        task_id,
        trace_id,
        retries,
        summary,
    )
    opmetrics.increment("celery.task.success.check_archive_addon_expiry_task")
    return summary


@shared_task(bind=True, ignore_result=True)
def check_storage_thresholds_task(self) -> dict:
    """Warn managers before either storage space runs out.

    Discovering a full disk from a failed upload — mid-lesson, with a photo the
    teacher wanted to file — is the worst possible moment. This gives managers
    days of notice and names both ways out: raise the limit, or clear a year
    that a saved snapshot already preserves.

    Both spaces are checked, because both are enforced. The yearly-archive space
    is the quieter of the two: nothing consumes it day to day, so it fills
    silently and only announces itself when the once-a-year archiving run is
    refused.
    """
    task_id, retries, trace_id = _task_ctx(self)
    summary = {"schools_checked": 0, "warnings_sent": 0, "skipped_duplicate": 0}

    if not bool(getattr(settings, "STORAGE_THRESHOLD_ALERTS_ENABLED", True)):
        return summary

    if not _periodic_lock("check_storage_thresholds", ttl=300):
        logger.info("Storage threshold task skipped: another instance is running.")
        return {**summary, "skipped": "lock"}

    from .services_archive import (
        STORAGE_CRITICAL_PERCENT,
        school_archive_overview,
        school_storage_overview,
    )

    School = apps.get_model("reports", "School")
    SchoolMembership = apps.get_model("reports", "SchoolMembership")
    Notification = apps.get_model("reports", "Notification")
    NotificationRecipient = apps.get_model("reports", "NotificationRecipient")

    dedup_cutoff = timezone.now() - timedelta(days=3)

    schools = School.objects.filter(is_active=True).select_related(
        "subscription", "subscription__plan", "archive_addon"
    )

    def _managers_of(school) -> list[int]:
        return list(
            SchoolMembership.objects.filter(
                school=school,
                role_type="manager",
                is_active=True,
                teacher__is_active=True,
            ).values_list("teacher_id", flat=True)
        )

    def _notify(school, manager_ids, *, title, lines, important) -> bool:
        # One title per level, so crossing from warning to critical still gets
        # through while a steady state does not repeat every day.
        if Notification.objects.filter(
            title=title, school=school, created_at__gte=dedup_cutoff
        ).exists():
            summary["skipped_duplicate"] += 1
            return False

        notification = Notification.objects.create(
            title=title,
            message="\n".join(lines),
            school=school,
            is_important=important,
        )
        NotificationRecipient.objects.bulk_create(
            [
                NotificationRecipient(notification=notification, teacher_id=mid)
                for mid in manager_ids
            ],
            ignore_conflicts=True,
        )
        with soft_fail("realtime.push_archive_addon_notice", count=len(manager_ids)):
            from .realtime_notifications import push_new_notification_to_teachers

            push_new_notification_to_teachers(
                notification=notification, teacher_ids=manager_ids
            )
        summary["warnings_sent"] += 1
        return True

    for school in schools.iterator():
        overview = school_storage_overview(school)
        archive = school_archive_overview(school)
        if not (overview["needs_attention"] or archive["needs_attention"]):
            continue

        summary["schools_checked"] += 1
        manager_ids = _managers_of(school)
        if not manager_ids:
            continue

        if overview["needs_attention"]:
            percent = overview["usage_percent"]
            level = overview["warning_level"]

            if level == "full":
                headline = (
                    f"امتلأت مساحة عمل {school.name} ({overview['used_label']} من "
                    f"{overview['limit_label']}). رفع أي ملف جديد متوقف الآن."
                )
            else:
                headline = (
                    f"مساحة عمل {school.name} وصلت {percent}% "
                    f"({overview['used_label']} من {overview['limit_label']})."
                )

            lines = [headline, "", "أمامك خياران:"]
            lines.append("• رفع حد مساحة العمل من صفحة الاشتراك.")
            if overview["reclaimable_years"]:
                biggest = overview["reclaimable_years"][0]
                lines.append(
                    f"• تفريغ {overview['reclaimable_label']} بحذف ملفات سنوات لها نسخة "
                    f"سنوية محفوظة (أكبرها {biggest['label']} بحجم {biggest['size_label']}). "
                    "النسخة المحفوظة تحتفظ بالسنة كاملة."
                )
            else:
                lines.append(
                    "• أو حفظ نسخة سنوية لسنة سابقة ثم حذف ملفاتها الحية لتفريغ مساحتها."
                )

            sent = _notify(
                school,
                manager_ids,
                title=f"💾 مساحة عمل {school.name} ({level})",
                lines=lines,
                important=level in {"critical", "full"},
            )
            if sent and percent >= STORAGE_CRITICAL_PERCENT:
                opmetrics.increment("storage.school.critical")

        # المساحة الثانية تحتاج تنبيهها الخاص: حدّها مستقل، وامتلاؤها لا يظهر
        # في أي مكان حتى تفشل أرشفة سنةٍ كاملة — وهي عملية تُجرى مرة في العام،
        # فاكتشاف العطل عندها يعني تأجيلها لا إصلاحها.
        if archive["needs_attention"]:
            archive_level = archive["warning_level"]
            if archive_level == "full":
                archive_headline = (
                    f"امتلأت مساحة الأرشفة السنوية في {school.name} "
                    f"({archive['used_label']} من {archive['limit_label']}). "
                    "حفظ أي نسخة سنوية جديدة متوقف الآن."
                )
            else:
                archive_headline = (
                    f"مساحة الأرشفة السنوية في {school.name} وصلت "
                    f"{archive['usage_percent']}% ({archive['used_label']} من "
                    f"{archive['limit_label']})."
                )

            archive_lines = [
                archive_headline,
                "",
                "لا يؤثر ذلك على عمل المعلمين اليومي؛ الرفع والتوثيق يعملان كالمعتاد.",
                "",
                "أمامك خياران:",
                "• طلب مساحة أرشفة إضافية من صفحة الاشتراك.",
                "• أو تنزيل نسخة سنة سابقة على جهازك ثم حذفها من المنصة لتحرير مساحتها.",
            ]
            sent = _notify(
                school,
                manager_ids,
                title=f"🗄️ مساحة الأرشفة {school.name} ({archive_level})",
                lines=archive_lines,
                important=archive_level in {"critical", "full"},
            )
            if sent and archive["usage_percent"] >= STORAGE_CRITICAL_PERCENT:
                opmetrics.increment("storage.archive.critical")

    logger.info(
        "Task success name=check_storage_thresholds_task task_id=%s trace_id=%s retries=%s summary=%s",
        task_id,
        trace_id,
        retries,
        summary,
    )
    opmetrics.increment("celery.task.success.check_storage_thresholds_task")
    return summary


@shared_task(bind=True, ignore_result=True)
def reconcile_pending_gateway_payments_task(self) -> dict:
    """Finish electronic payments the gateway never told us about.

    Activation used to hinge on the customer's browser returning to the site or
    on the gateway's callback reaching us. Either can fail, and nothing retried —
    leaving a school that had genuinely paid with no active subscription.
    """
    task_id, retries, trace_id = _task_ctx(self)

    if not bool(getattr(settings, "PAYMENT_RECONCILIATION_ENABLED", True)):
        return {"enabled": False}

    if not _periodic_lock("reconcile_gateway_payments", ttl=600):
        logger.info("Payment reconciliation skipped: another instance is running.")
        return {"skipped": "lock"}

    from .views.subscriptions import reconcile_pending_gateway_payments

    summary = reconcile_pending_gateway_payments()

    recovered_ids = summary.get("recovered_payment_ids") or []
    if summary.get("activated"):
        # A recovered payment means a customer-facing failure happened upstream;
        # make it visible instead of quietly papering over it.
        logger.warning(
            "Recovered %s payment(s) the gateway never confirmed to us: %s",
            summary["activated"],
            summary,
        )
        opmetrics.increment("payments.reconciled.activated", summary["activated"])

        # Tell the team as well. One alert per rescued payment, so a burst of
        # them reads as the webhook outage it is.
        if recovered_ids:
            try:
                Payment = apps.get_model("reports", "Payment")
                from .telegram_alerts import build_payment_recovery_alert, queue_telegram_alert

                for payment in Payment.objects.filter(pk__in=recovered_ids).select_related("school"):
                    queue_telegram_alert(build_payment_recovery_alert(payment))
            except Exception:
                # Alerting must never undo a successful recovery.
                logger.exception("Unable to queue payment recovery alerts")
    if summary.get("failed"):
        opmetrics.increment("payments.reconciled.failed", summary["failed"])

    logger.info(
        "Task success name=reconcile_pending_gateway_payments_task task_id=%s trace_id=%s retries=%s summary=%s",
        task_id,
        trace_id,
        retries,
        summary,
    )
    opmetrics.increment("celery.task.success.reconcile_pending_gateway_payments_task")
    return summary


# ─────────────────────────────────────────────────────────────────────────────
# توليد PDF في عامل الوسائط
# ─────────────────────────────────────────────────────────────────────────────
# الغرض والمقايضة مشروحان في ``reports/pdf_offload.py``. المهم هنا: هذه المهام
# **لا تُخزّن شيئاً ولا تعدّل صفاً** — تولّد البايتات وتضعها في مفتاح قصير العمر
# يقرؤه الطلب المنتظر. فلا تستهلك حصة تخزين المدرسة، ولا تترك ملفاً قد يقادم.
#
# ولا إعادة محاولة: الطلب ينتظر النتيجة الآن، ومحاولةٌ ثانية بعد ثوانٍ تصل بعد
# أن يكون قد ارتدّ إلى التوليد المحلي — فتحرق معالجاً لعملٍ لا قارئ له.
@shared_task(bind=True, ignore_result=False, max_retries=0, soft_time_limit=90, time_limit=120)
def render_achievement_pdf_task(self, achievement_file_id: int, base_url: str | None, cache_key: str) -> bool:
    from .models import TeacherAchievementFile
    from .pdf_achievement import generate_achievement_pdf
    from .pdf_offload import store_rendered_pdf

    ach_file = (
        TeacherAchievementFile.objects.select_related("school", "teacher")
        .filter(pk=achievement_file_id)
        .first()
    )
    if ach_file is None:
        logger.error("Achievement file %s not found for PDF rendering.", achievement_file_id)
        return False

    pdf_bytes, _filename = generate_achievement_pdf(ach_file=ach_file, base_url=base_url)
    store_rendered_pdf(cache_key, pdf_bytes)
    opmetrics.increment("pdf.rendered.achievement")
    return True


@shared_task(bind=True, ignore_result=False, max_retries=0, soft_time_limit=90, time_limit=120)
def render_group_report_pdf_task(self, group_id: int, base_url: str | None, cache_key: str) -> bool:
    from .models import SchoolGroup
    from .pdf_offload import store_rendered_pdf
    from .pdf_report import _generate_report_pdf_weasy
    from .services_group_export import build_group_snapshot

    group = SchoolGroup.objects.filter(pk=group_id).first()
    if group is None:
        return False
    html = render_to_string(
        "reports/pdf/group_report_pdf.html",
        {"snapshot": build_group_snapshot(group), "group": group},
    )
    payload = _generate_report_pdf_weasy(html=html, base_url=base_url)
    store_rendered_pdf(cache_key, payload)
    opmetrics.increment("pdf.rendered.group_report")
    return True


@shared_task(bind=True, ignore_result=False, max_retries=0, soft_time_limit=90, time_limit=120)
def render_leadership_pdf_task(self, portfolio_id: int, base_url: str | None, cache_key: str) -> bool:
    from .models import SchoolLeadershipPortfolio
    from .pdf_leadership import generate_leadership_portfolio_pdf
    from .pdf_offload import store_rendered_pdf

    portfolio = SchoolLeadershipPortfolio.objects.select_related("school").filter(pk=portfolio_id).first()
    if portfolio is None:
        return False
    payload = generate_leadership_portfolio_pdf(portfolio, base_url=base_url)
    store_rendered_pdf(cache_key, payload)
    opmetrics.increment("pdf.rendered.leadership")
    return True


@shared_task(bind=True, ignore_result=False, max_retries=0, soft_time_limit=90, time_limit=120)
def render_user_guide_pdf_task(self, base_url: str, cache_key: str) -> bool:
    from .pdf_offload import store_rendered_pdf
    from .pdf_user_guide import generate_user_guide_pdf

    store_rendered_pdf(cache_key, generate_user_guide_pdf(base_url=base_url))
    opmetrics.increment("pdf.rendered.user_guide")
    return True


@shared_task(bind=True, ignore_result=True, max_retries=0, soft_time_limit=25 * 60, time_limit=30 * 60)
def build_generated_export_task(self, job_id: int) -> bool:
    """Build a ZIP in the media worker and persist it to private R2 storage."""
    from .cache_utils import redis_cache_lock
    from .models import GeneratedExportJob, School, SchoolYearArchive, Teacher
    from .services_archive import (
        archive_snapshot_capacity_error,
        sync_school_archive_storage_usage,
    )
    from .services_export import (
        archive_zip_filename,
        build_school_export_zip_file,
        export_zip_filename,
    )

    with redis_cache_lock(f"generated-export:build:{int(job_id)}", timeout=31 * 60) as acquired:
        if not acquired:
            logger.info("Generated export already owned by another worker job=%s", job_id)
            return True

        with transaction.atomic():
            job = _locked_generated_export_job(job_id)
            if job is None:
                return False
            if job.status == GeneratedExportJob.Status.READY:
                return True
            job.status = GeneratedExportJob.Status.RUNNING
            job.started_at = timezone.now()
            job.error_message = ""
            job.save(update_fields=["status", "started_at", "error_message"])

        zip_file = None
        archive = None
        try:
            school = School.objects.get(pk=job.school_id)
            requested_by = Teacher.objects.filter(pk=job.requested_by_id).first()
            params = dict(job.parameters or {})

            if job.kind == GeneratedExportJob.Kind.SCHOOL_ZIP:
                zip_file = build_school_export_zip_file(school, request=None)
                filename = export_zip_filename(school)
                metadata = None
            else:
                academic_year = str(params.get("academic_year") or "").strip()
                if not academic_year:
                    raise ValueError("السنة الدراسية مطلوبة لإنشاء الأرشيف.")
                school_wide = bool(params.get("school_wide", True))
                zip_file, metadata = build_school_export_zip_file(
                    school,
                    academic_year=academic_year,
                    teacher=requested_by,
                    school_wide=school_wide,
                    request=None,
                    return_metadata=True,
                )
                filename = archive_zip_filename(school, academic_year)

            try:
                zip_file.seek(0, 2)
                size_bytes = int(zip_file.tell())
                zip_file.seek(0)
            except Exception:
                size_bytes = int((metadata or {}).get("archive_size_bytes") or 0)

            if job.kind == GeneratedExportJob.Kind.ARCHIVE_SNAPSHOT:
                capacity_error = archive_snapshot_capacity_error(school, size_bytes)
                if capacity_error:
                    raise ValueError(capacity_error)

                academic_year = str(params.get("academic_year") or "").strip()
                with transaction.atomic():
                    School.objects.select_for_update().get(pk=school.pk)
                    locked_job = GeneratedExportJob.objects.select_for_update().get(pk=job.pk)
                    if locked_job.status == GeneratedExportJob.Status.READY:
                        return True
                    latest_version = (
                        SchoolYearArchive.objects.filter(
                            school=school,
                            academic_year=academic_year,
                        )
                        .order_by("-version")
                        .values_list("version", flat=True)
                        .first()
                        or 0
                    )
                    archive = SchoolYearArchive(
                        school=school,
                        academic_year=academic_year,
                        version=int(latest_version) + 1,
                        status=(
                            SchoolYearArchive.Status.PARTIAL
                            if metadata["is_partial"]
                            else SchoolYearArchive.Status.READY
                        ),
                        archive_sha256=metadata["archive_sha256"],
                        file_count=metadata["file_count"],
                        missing_file_count=metadata["missing_file_count"],
                        failed_pdf_count=metadata["failed_pdf_count"],
                        report_count=metadata["report_count"],
                        achievement_count=metadata["achievement_count"],
                        leadership_count=metadata["leadership_count"],
                        ticket_count=metadata["ticket_count"],
                        circular_count=metadata["circular_count"],
                        notification_count=metadata["notification_count"],
                        assignment_count=int(metadata.get("assignment_count") or 0),
                        plan_count=int(metadata.get("plan_count") or 0),
                        initiative_count=int(metadata.get("initiative_count") or 0),
                        lab_asset_count=int(metadata.get("lab_asset_count") or 0),
                        lab_handover_count=int(metadata.get("lab_handover_count") or 0),
                        lab_experiment_count=int(metadata.get("lab_experiment_count") or 0),
                        notes=metadata["notes"],
                        created_by=requested_by,
                    )
                    archive.archive_file.save(filename, File(zip_file), save=False)
                    archive.save()
                    locked_job.status = GeneratedExportJob.Status.READY
                    locked_job.archive = archive
                    locked_job.filename = filename
                    locked_job.size_bytes = size_bytes
                    locked_job.completed_at = timezone.now()
                    locked_job.expires_at = None
                    locked_job.save(
                        update_fields=[
                            "status", "archive", "filename", "size_bytes",
                            "completed_at", "expires_at",
                        ]
                    )
                sync_school_archive_storage_usage(school)
            else:
                job.artifact_file.save(filename, File(zip_file), save=False)
                job.status = GeneratedExportJob.Status.READY
                job.filename = filename
                job.content_type = "application/zip"
                job.size_bytes = size_bytes
                job.completed_at = timezone.now()
                job.expires_at = timezone.now() + timedelta(
                    hours=max(1, int(getattr(settings, "GENERATED_EXPORT_RETENTION_HOURS", 6) or 6))
                )
                job.save(
                    update_fields=[
                        "artifact_file", "status", "filename", "content_type",
                        "size_bytes", "completed_at", "expires_at",
                    ]
                )

            opmetrics.increment(f"generated_export.success.{job.kind}")
            logger.info(
                "Generated export ready job=%s kind=%s school=%s bytes=%s",
                job.pk,
                job.kind,
                job.school_id,
                size_bytes,
            )
            return True
        except Exception as exc:
            logger.exception("Generated export failed job=%s", job_id)
            GeneratedExportJob.objects.filter(pk=job_id).update(
                status=GeneratedExportJob.Status.FAILED,
                error_message=str(exc)[:500],
                completed_at=timezone.now(),
            )
            opmetrics.increment("generated_export.failed")
            if archive is not None and getattr(archive.archive_file, "name", ""):
                try:
                    persisted = bool(
                        archive.pk
                        and SchoolYearArchive.objects.filter(pk=archive.pk).exists()
                    )
                    if not persisted:
                        archive.archive_file.delete(save=False)
                except Exception:
                    # ملفٌ يتيم في R2 يُحتسب على حصة المدرسة إلى الأبد.
                    _degraded("storage.orphan_archive_cleanup", archive_id=getattr(archive, "pk", None))
            if (
                job.kind != GeneratedExportJob.Kind.ARCHIVE_SNAPSHOT
                and getattr(job.artifact_file, "name", "")
            ):
                with soft_fail("storage.orphan_export_cleanup", job_id=job.pk):
                    job.artifact_file.delete(save=False)
            raise
        finally:
            if zip_file is not None:
                with soft_fail("export.close_zip_handle"):
                    zip_file.close()


@shared_task(ignore_result=True, soft_time_limit=300, time_limit=600)
def cleanup_generated_exports_task() -> int:
    """Delete expired ad-hoc artifacts while retaining their audit rows."""
    from .models import GeneratedExportJob

    expired = GeneratedExportJob.objects.filter(
        status=GeneratedExportJob.Status.READY,
        archive__isnull=True,
        expires_at__isnull=False,
        expires_at__lte=timezone.now(),
    ).exclude(artifact_file="")
    cleaned = 0
    for job in expired.iterator(chunk_size=100):
        try:
            job.artifact_file.delete(save=False)
            GeneratedExportJob.objects.filter(pk=job.pk).update(
                artifact_file="",
                status=GeneratedExportJob.Status.EXPIRED,
            )
            cleaned += 1
        except Exception:
            logger.exception("Unable to clean generated export job=%s", job.pk)
    opmetrics.increment("generated_export.cleaned", cleaned)
    return cleaned


@shared_task(ignore_result=True, soft_time_limit=25 * 60, time_limit=30 * 60)
def recover_stale_generated_exports_task() -> int:
    """Build one abandoned media export in the independently-run core worker."""
    from .generated_exports import recover_stale_generated_exports

    recovered = recover_stale_generated_exports(limit=1)
    opmetrics.increment("generated_export.recovered", recovered)
    return recovered
