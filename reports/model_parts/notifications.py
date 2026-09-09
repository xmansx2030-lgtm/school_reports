from __future__ import annotations

from .base import *
from .schools import School, SchoolGroup, Teacher
from .tickets import Ticket


class GroupNotificationBatch(models.Model):
    """تعميم يرسله المدير التنفيذي إلى عدد من مدارس مجموعته.

    الدفعة **أبٌ خفيف** لا يُسلَّم بذاته: كل مدرسة مستهدفة تستقبل ``Notification``
    عادياً بمدرستها الصحيحة، فيراه مديرها تعميماً طبيعياً في شاشته المعتادة بلا
    أي تغيير في المنطق القائم. ووجود الأب هو ما يتيح للمدير التنفيذي تقريراً
    موحّداً عبر المدارس بدل N تقارير منفصلة.

    المدارس المستهدفة **لا تُخزَّن هنا** عمداً: تُشتق من ``self.notifications``،
    فلا يوجد مصدران للحقيقة يفترقان إن حُذف إشعار مدرسة.
    """

    class Audience(models.TextChoices):
        MANAGERS = "managers", "مديرو المدارس فقط"
        ALL = "all", "جميع المنسوبين"

    group = models.ForeignKey(
        SchoolGroup,
        on_delete=models.CASCADE,
        related_name="notification_batches",
        verbose_name="المجموعة",
    )
    sender = models.ForeignKey(
        Teacher,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="group_notification_batches",
        verbose_name="المرسِل",
    )
    audience = models.CharField(
        "المستقبلون",
        max_length=16,
        choices=Audience.choices,
        default=Audience.MANAGERS,
    )
    title = models.CharField("العنوان", max_length=120, blank=True, default="")
    requires_signature = models.BooleanField("يتطلب توقيعاً؟", default=False)
    created_at = models.DateTimeField("أُرسل في", default=timezone.now)

    class Meta:
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(fields=["group", "-created_at"]),
            models.Index(fields=["sender", "-created_at"]),
        ]
        verbose_name = "تعميم مجموعة"
        verbose_name_plural = "تعاميم المجموعات"

    def __str__(self) -> str:
        return self.title or f"تعميم #{self.pk}"

    @property
    def target_schools(self):
        return School.objects.filter(notifications__batch=self).distinct().order_by("name")


class Notification(models.Model):
    class Kind(models.TextChoices):
        NOTIFICATION = "notification", "إشعار"
        NEWSLETTER = "newsletter", "نشرة"
        CIRCULAR = "circular", "تعميم"

    kind = models.CharField(
        "نوع التواصل",
        max_length=16,
        choices=Kind.choices,
        default=Kind.NOTIFICATION,
        db_index=True,
        help_text="يفصل نوع الوثيقة عن طلب التوقيع؛ فقد تتطلب النشرة توقيعًا أو لا تتطلبه.",
    )
    title = models.CharField(max_length=120, blank=True, default="")
    message = models.TextField()
    is_important = models.BooleanField(default=False)
    expires_at = models.DateTimeField(null=True, blank=True)

    attachment = models.FileField(
        "مرفق (اختياري)",
        upload_to=_notification_attachment_upload_to,
        null=True,
        blank=True,
        validators=[validate_circular_attachment_file],
        help_text="يسمح بـ PDF/صور. حد أقصى 5MB.",
    )
    storage_bytes = models.PositiveBigIntegerField(
        "حجم المرفق",
        default=0,
        editable=False,
    )

    # =========================
    # التواقيع (للتعاميم الإلزامية)
    # =========================
    requires_signature = models.BooleanField(
        "يتطلب توقيع؟",
        default=False,
        help_text="عند التفعيل تتطلب الوثيقة إقرارًا وإدخال رقم الجوال لاعتماد التوقيع.",
    )
    is_broadcast = models.BooleanField(
        "للجميع (بث عام)؟",
        default=False,
        help_text="عند التفعيل يُعرض التواصل لجميع مستلميه دون تخصيص.",
    )
    signature_deadline_at = models.DateTimeField(
        "آخر موعد للتوقيع",
        null=True,
        blank=True,
        help_text="اختياري: يظهر للمعلمين في صفحة التوقيع ويستخدم للتقارير.",
    )
    signature_ack_text = models.TextField(
        "نص الإقرار",
        blank=True,
        default="أقرّ بأنني اطلعت على هذا التعميم وفهمت ما ورد فيه وأتعهد بالالتزام به.",
    )
    created_at = models.DateTimeField(default=timezone.now)
    school = models.ForeignKey(
        School,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="notifications",
        verbose_name="المدرسة المستهدفة",
        help_text="إن تُركت فارغة يكون الإشعار عامًا أو على مستوى كل المدارس.",
    )
    created_by = models.ForeignKey(
        Teacher, null=True, blank=True, on_delete=models.SET_NULL, related_name="notifications_created"
    )
    batch = models.ForeignKey(
        GroupNotificationBatch,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="notifications",
        verbose_name="دفعة تعميم المجموعة",
        help_text=(
            "يُملأ فقط لتعاميم المدير التنفيذي. حذف الدفعة لا يسحب التعميم من "
            "المدرسة — فقد وصلها فعلاً، وسحبه تزوير للسجل."
        ),
    )

    class Meta:
        db_table = "reports_notification"
        ordering = ("-created_at", "-id")

    # Backwards-compatible aliases for templates/legacy code.
    # Some templates reference n.body / n.content / n.text / n.details.
    # The canonical field is `message`.
    @property
    def body(self) -> str:
        return self.message

    @property
    def content(self) -> str:
        return self.message

    @property
    def text(self) -> str:
        return self.message

    @property
    def details(self) -> str:
        return self.message

    @property
    def is_newsletter(self) -> bool:
        return self.kind == self.Kind.NEWSLETTER

    @property
    def is_circular(self) -> bool:
        return self.kind == self.Kind.CIRCULAR

    @property
    def kind_label(self) -> str:
        return self.get_kind_display()

    def save(self, *args, **kwargs):
        # Backward compatibility for older internal callers that created a
        # circular by setting only ``requires_signature=True``.  A newsletter
        # always supplies its explicit kind and is therefore never rewritten.
        if self._state.adding and self.kind == self.Kind.NOTIFICATION and self.requires_signature:
            self.kind = self.Kind.CIRCULAR
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.title or (self.message[:30] + ("..." if len(self.message) > 30 else ""))


class NotificationRecipient(models.Model):
    class DeliverySource(models.TextChoices):
        ORIGINAL = "original", "ضمن قائمة الإصدار الأصلية"
        MANUAL_ADDITION = "manual_addition", "أُضيف لاحقًا بواسطة الإدارة"

    notification = models.ForeignKey(Notification, on_delete=models.CASCADE, related_name="recipients")
    teacher = models.ForeignKey(Teacher, on_delete=models.CASCADE, related_name="notifications")
    is_read = models.BooleanField(default=False)
    read_at = models.DateTimeField(null=True, blank=True)

    # توقيع التعميم (على مستوى المستلم)
    is_signed = models.BooleanField(default=False)
    signed_at = models.DateTimeField(null=True, blank=True)
    signature_attempt_count = models.PositiveSmallIntegerField(default=0)
    signature_last_attempt_at = models.DateTimeField(null=True, blank=True)
    delivery_source = models.CharField(
        "مصدر الإضافة",
        max_length=20,
        choices=DeliverySource.choices,
        default=DeliverySource.ORIGINAL,
        help_text="يميّز قائمة الإصدار الأصلية عن المستلمين الذين أُلحقوا لاحقًا.",
    )
    added_by = models.ForeignKey(
        Teacher,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="notification_recipient_additions",
        verbose_name="أضافه",
        help_text="يُملأ فقط عند إلحاق مستلم بعد إصدار التعميم.",
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "reports_notification_recipient"
        indexes = [
            models.Index(fields=["teacher", "is_read", "-created_at"]),
            models.Index(fields=["teacher", "is_signed", "-created_at"]),
            # ✅ فهرس لتسريع استعلامات notification__school_id عبر FK
            models.Index(fields=["notification", "teacher"]),
        ]
        unique_together = (("notification", "teacher"),)

    def __str__(self):
        return f"{self.teacher} ← {self.notification}"


class WebPushSubscription(models.Model):
    """A browser/device Push API subscription owned by one signed-in user.

    The endpoint is globally unique because one browser subscription must never
    keep delivering an earlier user's notifications after accounts change on a
    shared device.  Re-subscribing the same endpoint transfers it to the current
    authenticated user.
    """

    teacher = models.ForeignKey(
        Teacher,
        on_delete=models.CASCADE,
        related_name="web_push_subscriptions",
    )
    endpoint = models.TextField(unique=True)
    p256dh = models.TextField()
    auth = models.TextField()
    user_agent = models.CharField(max_length=500, blank=True, default="")
    is_active = models.BooleanField(default=True)
    failure_count = models.PositiveSmallIntegerField(default=0)
    last_success_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-updated_at", "-id")
        indexes = [
            models.Index(fields=["teacher", "is_active"]),
            models.Index(fields=["is_active", "updated_at"]),
        ]

    def __str__(self):
        return f"Web Push: {self.teacher_id} / {self.endpoint[:48]}"


class WebPushDelivery(models.Model):
    """Idempotency and delivery audit for a notification/device pair."""

    class Status(models.TextChoices):
        PENDING = "pending", "قيد الإرسال"
        SENT = "sent", "تم الإرسال"
        FAILED = "failed", "تعذر الإرسال"

    subscription = models.ForeignKey(
        WebPushSubscription,
        on_delete=models.CASCADE,
        related_name="deliveries",
    )
    notification = models.ForeignKey(
        Notification,
        on_delete=models.CASCADE,
        related_name="web_push_deliveries",
    )
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error = models.CharField(max_length=500, blank=True, default="")
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["subscription", "notification"],
                name="uniq_web_push_delivery_subscription_notification",
            ),
        ]
        indexes = [models.Index(fields=["status", "created_at"])]

    def __str__(self):
        return f"{self.notification_id} → {self.subscription_id} ({self.status})"


# reports/models.py  (بعد class Ticket)
class TicketImage(models.Model):
    ticket = models.ForeignKey(
        Ticket,
        on_delete=models.CASCADE,
        related_name="images",
        verbose_name="التذكرة",
        db_index=True,
    )
    image = models.ImageField(
        "الصورة",
        upload_to=_ticket_image_upload_to,
        blank=False,
        null=False,
        validators=[validate_image_file],
    )
    storage_bytes = models.PositiveBigIntegerField(
        "حجم الصورة",
        default=0,
        editable=False,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        verbose_name = "صورة تذكرة"
        verbose_name_plural = "صور التذكرة"

    def __str__(self):
        return f"TicketImage #{self.pk} for Ticket #{self.ticket_id}"


# =========================
# إدارة الاشتراكات والمالية
