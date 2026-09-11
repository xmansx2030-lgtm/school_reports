from __future__ import annotations

import threading
from contextlib import contextmanager

from .base import *
from .schools import School

__all__ = ["AuditLog", "AuditLogImmutableError", "audit_retention_purge"]


class AuditLogImmutableError(ValidationError):
    """يُرفع عند محاولة تعديل أو حذف سجل إجراءات خارج مسار الاحتفاظ."""


_purge_state = threading.local()


@contextmanager
def audit_retention_purge():
    """المنفذ الوحيد المسموح لحذف سجلات الإجراءات.

    السجل شهادةٌ على ما جرى، فتعديله تزوير وحذفه طمس. ومع ذلك تبقى سياسة
    الاحتفاظ حاجةً مشروعة: قاعدة بيانات لا تُقلَّم تتضخم بلا حد. فالحل ليس ترك
    الحذف مفتوحاً ولا إغلاقه كلياً، بل حصره في **مسار واحد مُسمّى** يُؤرشف قبل
    أن يحذف — وهو ``cleanup_audit_logs`` — فيصير كل حذف خارج هذا المسار خطأً
    صريحاً يظهر وقت وقوعه لا بعد سنة.

    النطاق thread-local عمداً: تفعيله في خيط الاحتفاظ لا يفتح الباب في خيط
    يخدم طلب مستخدم في اللحظة نفسها.
    """
    previous = getattr(_purge_state, "allowed", False)
    _purge_state.allowed = True
    try:
        yield
    finally:
        _purge_state.allowed = previous


def _purge_allowed() -> bool:
    return bool(getattr(_purge_state, "allowed", False))


class AuditLogQuerySet(models.QuerySet):
    """يمنع الحذف والتحديث الجماعيين — وهما الطريقان اللذان يلتفّان على النموذج."""

    def delete(self):
        if not _purge_allowed():
            raise AuditLogImmutableError(
                "سجل الإجراءات لا يُحذف. استخدم أمر الاحتفاظ cleanup_audit_logs الذي يؤرشف قبل الحذف."
            )
        return super().delete()

    def update(self, **kwargs):
        raise AuditLogImmutableError("سجل الإجراءات لا يُعدّل بعد كتابته.")


class AuditLog(models.Model):
    """سجل إجراءات غير قابل للتعديل.

    ثلاث خصائص تجعل هذا السجل صالحاً للاحتجاج به:

    1. **لا يُعدَّل مطلقاً** — لا عبر ``save()`` ولا عبر ``queryset.update()``.
    2. **لا يُحذف** إلا عبر :func:`audit_retention_purge` الذي يؤرشف أولاً.
    3. **لا تُمحى هويّة فاعله بحذف حسابه** — الاسم والدور محفوظان لقطةً وقت
       الحدث، على غرار ``Report.teacher_name``. فحساب يُحذف بعد سنة لا يجوز أن
       يُفرِّغ سجلّ ما فعله، وإلا صار حذف الحساب وسيلةً لطمس الأثر.
    """

    class Action(models.TextChoices):
        CREATE = "create", "إنشاء"
        UPDATE = "update", "تعديل"
        DELETE = "delete", "حذف"
        LOGIN = "login", "تسجيل دخول"
        LOGOUT = "logout", "تسجيل خروج"

    school = models.ForeignKey(
        School,
        on_delete=models.CASCADE,
        related_name="audit_logs",
        verbose_name="المدرسة",
        null=True,
        blank=True
    )
    teacher = models.ForeignKey(
        "Teacher",
        # SET_NULL لا CASCADE: حذف الحساب يزيل صاحب الأثر لا الأثر نفسه.
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_logs",
        verbose_name="المستخدم"
    )
    actor_name = models.CharField(
        "اسم الفاعل (وقت الحدث)",
        max_length=150,
        blank=True,
        default="",
        help_text="لقطة تبقى بعد حذف الحساب.",
    )
    actor_role = models.CharField(
        "دور الفاعل (وقت الحدث)",
        max_length=64,
        blank=True,
        default="",
        help_text="الدور كما كان لحظة تنفيذ الإجراء، لا كما هو اليوم.",
    )
    action = models.CharField("العملية", max_length=20, choices=Action.choices)
    model_name = models.CharField("اسم النموذج", max_length=100, blank=True)
    object_id = models.PositiveIntegerField("معرف السجل", null=True, blank=True)
    object_repr = models.CharField("وصف السجل", max_length=255, blank=True)
    changes = models.JSONField("التغييرات", null=True, blank=True)
    ip_address = models.GenericIPAddressField("عنوان IP", null=True, blank=True)
    user_agent = models.TextField("متصفح المستخدم", blank=True)
    timestamp = models.DateTimeField("الوقت", auto_now_add=True)

    objects = AuditLogQuerySet.as_manager()

    class Meta:
        ordering = ("-timestamp",)
        verbose_name = "سجل عمليات"
        verbose_name_plural = "سجلات العمليات"
        indexes = [
            models.Index(fields=["school", "timestamp"]),
            models.Index(fields=["teacher", "timestamp"]),
            # «سجل أعمالي» يقرأ دائماً بفاعل واحد مرتباً زمنياً.
            models.Index(fields=["teacher", "-timestamp"], name="reports_audit_actor_recent"),
        ]

    def __str__(self):
        who = self.actor_display
        return f"{who} - {self.get_action_display()} - {self.model_name} ({self.timestamp})"

    # ------------------------------------------------------------------
    # العرض
    # ------------------------------------------------------------------
    @property
    def actor_display(self) -> str:
        """اسم الفاعل: من اللقطة أولاً، فهي وحدها ما يصمد بعد حذف الحساب."""
        snapshot = (self.actor_name or "").strip()
        if snapshot:
            return snapshot
        if self.teacher_id:
            return (getattr(self.teacher, "name", "") or "").strip() or "مستخدم"
        return "حساب محذوف"

    # ------------------------------------------------------------------
    # الحصانة
    # ------------------------------------------------------------------
    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise AuditLogImmutableError(
                "سجل الإجراءات لا يُعدّل بعد كتابته — فهو شهادة على ما جرى."
            )
        if self.teacher_id and not self.actor_name:
            try:
                self.actor_name = (getattr(self.teacher, "name", "") or "")[:150]
            except Exception:
                pass
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if not _purge_allowed():
            raise AuditLogImmutableError(
                "سجل الإجراءات لا يُحذف. استخدم أمر الاحتفاظ cleanup_audit_logs الذي يؤرشف قبل الحذف."
            )
        return super().delete(*args, **kwargs)


# =========================
# Audit Log Signals
