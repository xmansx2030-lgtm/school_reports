from __future__ import annotations

from core.observability import report_degraded as _degraded

from .base import *
from .schools import Department, School, Teacher


# =========================
# منظومة التذاكر الموحّدة
# =========================
MAX_ATTACHMENT_MB = 5
_MAX_BYTES = MAX_ATTACHMENT_MB * 1024 * 1024


def validate_attachment_size(file_obj):
    """تحقق الحجم ≤ 5MB"""
    if getattr(file_obj, "size", 0) > _MAX_BYTES:
        raise ValidationError(f"حجم المرفق يتجاوز {MAX_ATTACHMENT_MB}MB.")


class Ticket(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "جديد"
        IN_PROGRESS = "in_progress", "قيد المعالجة"
        DONE = "done", "مكتمل"
        REJECTED = "rejected", "مرفوض"

    school = models.ForeignKey(
        School,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tickets",
        verbose_name="المدرسة",
        db_index=True,
    )

    creator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="tickets_created",
        verbose_name="المرسل",
        db_index=True,
    )

    # القسم ديناميكي كـ FK
    department = models.ForeignKey(
        Department,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="tickets",
        verbose_name="القسم",
        db_index=True,
    )

    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="tickets_assigned",
        verbose_name="المستلم",
        blank=True,
        null=True,
        db_index=True,
    )

    # ✅ مستلمون متعددون (مع بقاء assignee كمرجع/مسؤول رئيسي للتوافق الخلفي)
    recipients = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        through="TicketRecipient",
        related_name="tickets_received",
        blank=True,
        verbose_name="المستلمون",
    )

    title = models.CharField("عنوان الطلب", max_length=255)
    body = models.TextField("تفاصيل الطلب", blank=True, null=True)

    # ✅ مرفق يُرفع إلى التخزين الافتراضي (R2 أو محلي)
    attachment = models.FileField(
        "مرفق",
        upload_to=_ticket_attachment_upload_to,
        # Historical storage class name; production access is private/signed.
        storage=PublicRawMediaStorage(),
        blank=True,
        null=True,
        validators=[
            FileExtensionValidator(["pdf", "jpg", "jpeg", "png", "doc", "docx"]),
            validate_attachment_file,
        ],
        help_text=f"يسمح بـ PDF/صور/DOCX حتى {MAX_ATTACHMENT_MB}MB",
    )
    storage_bytes = models.PositiveBigIntegerField(
        "حجم المرفق",
        default=0,
        editable=False,
    )

    status = models.CharField(
        "الحالة",
        max_length=20,
        choices=Status.choices,
        default=Status.OPEN,
        db_index=True,
    )
    
    is_platform = models.BooleanField(
        "دعم فني للمنصة؟", 
        default=False,
        help_text="إذا تم تحديده، يعتبر هذا الطلب موجهاً لإدارة المنصة وليس للمدرسة داخلياً."
    )

    created_at = models.DateTimeField("تاريخ الإنشاء", auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField("تاريخ التحديث", auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "طلب"
        verbose_name_plural = "الطلبات"
        indexes = [
            models.Index(fields=["department", "status", "created_at"]),
            models.Index(fields=["assignee", "status"]),
            # ✅ فهارس شائعة لصفحات المدرسة/الاستعلامات
            models.Index(fields=["school", "status", "created_at"]),
            models.Index(fields=["school", "assignee", "status"]),
            # ✅ فهارس للـ context processor (creator+status / school+creator+status)
            models.Index(fields=["creator", "status"]),
            models.Index(fields=["school", "creator", "status"]),
        ]

    def __str__(self):
        return f"Ticket #{self.pk} - {self.title[:40]}"


class TicketRecipient(models.Model):
    """ربط التذكرة بمستلمين متعددين.

    لا نخزن حالة لكل مستلم لأن الخيار (1) يعتمد حالة مشتركة للتذكرة.
    """

    ticket = models.ForeignKey(
        Ticket,
        on_delete=models.CASCADE,
        related_name="ticket_recipients",
        db_index=True,
    )
    teacher = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ticket_recipient_links",
        db_index=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "مستلم تذكرة"
        verbose_name_plural = "مستلمو التذاكر"
        constraints = [
            models.UniqueConstraint(fields=["ticket", "teacher"], name="uniq_ticket_recipient"),
        ]

    def __str__(self) -> str:
        return f"Ticket #{self.ticket_id} → {getattr(self.teacher, 'name', self.teacher_id)}"

    # ======== خصائص مساعدة للقوالب ========
    @property
    def attachment(self):
        """Backwards-compatible proxy to the parent ticket attachment."""
        return getattr(getattr(self, "ticket", None), "attachment", None)

    @property
    def attachment_name_lower(self) -> str:
        return (getattr(self.attachment, "name", "") or "").lower()

    @property
    def attachment_is_image(self) -> bool:
        return self.attachment_name_lower.endswith((".jpg", ".jpeg", ".png", ".webp"))

    @property
    def attachment_is_pdf(self) -> bool:
        return self.attachment_name_lower.endswith(".pdf")

    @property
    def attachment_download_url(self) -> str:
        """
        • أضف Content-Disposition عبر query كحل احتياطي لتلميح التحميل.
        """
        url = getattr(self.attachment, "url", "") or ""
        if not url:
            return ""

        filename = os.path.basename(getattr(self.attachment, "name", "")) or "download"

        # تلميح للتحميل
        sep = "&" if "?" in url else "?"
        dispo = quote(f"attachment; filename*=UTF-8''{filename}", safe="")
        return f"{url}{sep}response-content-disposition={dispo}"


class TicketNote(models.Model):
    ticket = models.ForeignKey(
        Ticket,
        on_delete=models.CASCADE,
        related_name="notes",
        verbose_name="التذكرة"
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ticket_notes",
        verbose_name="كاتب الملاحظة"
    )
    body = models.TextField("الملاحظة")
    is_public = models.BooleanField("ظاهرة للمرسل؟", default=True)
    created_at = models.DateTimeField("تاريخ الإضافة", auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "ملاحظة طلب"
        verbose_name_plural = "ملاحظات الطلبات"

    def __str__(self):
        return f"Note #{self.pk} on Ticket #{self.ticket_id}"


# =========================
# نماذج تراثية (اختياري للأرشفة)
# =========================
REQUEST_DEPARTMENTS = [
    (MANAGER_SLUG, "المدير"),
    ("activity_officer", "مسؤول النشاط"),
    ("volunteer_officer", "مسؤول التطوع"),
    ("affairs_officer", "مسؤول الشؤون المدرسية"),
    ("admin_officer", "مسؤول الشؤون الإدارية"),
]


class RequestTicket(models.Model):
    class Status(models.TextChoices):
        NEW = "new", "جديد"
        IN_PROGRESS = "in_progress", "قيد المعالجة"
        DONE = "done", "تم الإنجاز"
        REJECTED = "rejected", "مرفوض"

    requester = models.ForeignKey(
        Teacher,
        on_delete=models.CASCADE,
        related_name="created_tickets",
        verbose_name="صاحب الطلب",
        db_index=True,
    )
    department = models.CharField("القسم/الجهة", max_length=32, choices=REQUEST_DEPARTMENTS, db_index=True)
    assignee = models.ForeignKey(
        Teacher,
        on_delete=models.SET_NULL,
        related_name="assigned_tickets",
        verbose_name="المستلم",
        null=True,
        blank=True,
    )
    title = models.CharField("عنوان الطلب", max_length=200)
    body = models.TextField("تفاصيل الطلب")
    attachment = models.FileField(
        "مرفق (اختياري)",
        upload_to=_ticket_attachment_upload_to,
        blank=True,
        null=True,
        validators=[
            FileExtensionValidator(["pdf", "jpg", "jpeg", "png", "doc", "docx"]),
            validate_attachment_file,
        ],
    )
    status = models.CharField("الحالة", max_length=20, choices=Status.choices, default=Status.NEW, db_index=True)
    created_at = models.DateTimeField("تاريخ الإنشاء", auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField("تاريخ التحديث", auto_now=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["department", "status"]),
            models.Index(fields=["assignee", "status"]),
        ]
        verbose_name = "طلب (تراثي)"
        verbose_name_plural = "طلبات (تراثية)"

    def __str__(self):
        return f"#{self.pk} - {self.title} ({self.get_status_display()})"


class RequestLog(models.Model):
    ticket = models.ForeignKey(RequestTicket, on_delete=models.CASCADE, related_name="logs", verbose_name="الطلب")
    actor = models.ForeignKey(Teacher, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="منفّذ العملية")
    old_status = models.CharField("الحالة القديمة", max_length=20, choices=RequestTicket.Status.choices, blank=True)
    new_status = models.CharField("الحالة الجديدة", max_length=20, choices=RequestTicket.Status.choices, blank=True)
    note = models.TextField("ملاحظة", blank=True)
    created_at = models.DateTimeField("وقت الإنشاء", auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "سجل طلب (تراثي)"
        verbose_name_plural = "سجل الطلبات (تراثي)"

    def __str__(self):
        return f"Log for #{self.ticket_id} at {self.created_at:%Y-%m-%d %H:%M}"


# =========================
# إشارات تضمن القسم/الدور الدائم للمدير
# =========================
@receiver(post_migrate)
def ensure_manager_department_and_role(sender, **kwargs):
    """
    يضمن وجود قسم (الإدارة) الدائم داخل كل مدرسة بعد الهجرات.

    لماذا؟
    - الأقسام مخصصة لكل مدرسة، لذا نحتاج Department(slug='manager') لكل مدرسة.
    """
    try:
        with transaction.atomic():
            schools = School.objects.all().only("id")
            for s in schools:
                dep, _ = Department.objects.get_or_create(
                    school=s,
                    slug=MANAGER_SLUG,
                    defaults={"name": MANAGER_NAME, "role_label": MANAGER_ROLE_LABEL, "is_active": True},
                )
                updates = []
                if dep.name != MANAGER_NAME:
                    dep.name = MANAGER_NAME
                    updates.append("name")
                if dep.role_label != MANAGER_ROLE_LABEL:
                    dep.role_label = MANAGER_ROLE_LABEL
                    updates.append("role_label")
                if not dep.is_active:
                    dep.is_active = True
                    updates.append("is_active")
                if updates:
                    dep.save(update_fields=updates)
    except Exception:
        _degraded("migrate.ensure_manager_department")
        # لا نرفع خطأ أثناء post_migrate للحفاظ على استقرار الهجرات
        pass


# =========================
# الإشعارات
