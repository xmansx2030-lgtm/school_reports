from __future__ import annotations

import os
import uuid

from .base import *
from .schools import School

class SubscriptionPlan(models.Model):
    class SupportLevel(models.TextChoices):
        STANDARD = "standard", "دعم اعتيادي"
        PRIORITY = "priority", "دعم بأولوية"

    name = models.CharField("اسم الباقة", max_length=100)
    price = models.DecimalField("السعر", max_digits=10, decimal_places=2)
    days_duration = models.PositiveIntegerField(
        "المدة بالأيام", 
        help_text="مدة الباقة الافتراضية بالأيام (مثلاً 90 للفصل، 365 للسنة)"
    )
    description = models.TextField("المميزات", blank=True)
    is_active = models.BooleanField("نشطة", default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    max_teachers = models.PositiveIntegerField(
        "حد المعلمين",
        default=0,
        help_text="الحد الأقصى لعدد حسابات المعلمين داخل المدرسة. 0 = غير محدود.",
    )
    support_level = models.CharField(
        "مستوى الدعم",
        max_length=16,
        choices=SupportLevel.choices,
        default=SupportLevel.STANDARD,
    )
    onboarding_sessions = models.PositiveSmallIntegerField(
        "جلسات الإعداد المشمولة",
        default=0,
    )
    included_archive_storage_gb = models.PositiveIntegerField(
        "مساحة الأرشيف المشمولة (GB)",
        default=0,
        help_text="تُفعّل تلقائياً عند اعتماد دفع الباقة. القيمة 0 تعني أن الأرشيف غير مشمول.",
    )

    class Meta:
        verbose_name = "باقة اشتراك"
        verbose_name_plural = "باقات الاشتراكات"

    def __str__(self):
        return f"{self.name} ({self.price} ريال)"


class SchoolSubscription(models.Model):
    school = models.OneToOneField(
        School,
        on_delete=models.CASCADE,
        related_name="subscription",
        verbose_name="المدرسة"
    )
    plan = models.ForeignKey(
        SubscriptionPlan,
        on_delete=models.PROTECT,
        verbose_name="الباقة الحالية"
    )
    start_date = models.DateField("تاريخ البدء")
    end_date = models.DateField("تاريخ الانتهاء", db_index=True)
    is_active = models.BooleanField(
        "نشط يدوياً", 
        default=True, 
        help_text="يمكن استخدامه لتعطيل الاشتراك مؤقتاً بغض النظر عن التاريخ"
    )
    teacher_limit_override = models.PositiveIntegerField(
        "سعة المعلمين المشتراة",
        null=True,
        blank=True,
        help_text="تُستخدم للسعات المرنة. عند تركها فارغة يُطبق حد المعلمين الموجود في الباقة.",
    )

    canceled_at = models.DateTimeField(
        "تاريخ الإلغاء",
        null=True,
        blank=True,
        help_text="يُعبّأ عند إلغاء الاشتراك من مدير النظام.",
    )
    cancel_reason = models.TextField(
        "سبب الإلغاء",
        blank=True,
        help_text="يظهر للمدرسة عند إلغاء الاشتراك.",
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "اشتراك مدرسة"
        verbose_name_plural = "اشتراكات المدارس"
        indexes = [
            models.Index(fields=['end_date', 'is_active']),
        ]

    def __str__(self):
        return f"اشتراك {self.school.name} - ينتهي في {self.end_date}"

    @property
    def teacher_limit(self) -> int:
        override = int(self.teacher_limit_override or 0)
        if override > 0:
            return override
        return int(getattr(self.plan, "max_teachers", 0) or 0)

    def save(self, *args, **kwargs):
        """ضبط تواريخ الاشتراك تلقائياً.

        المطلوب:
        - عند إنشاء اشتراك جديد: start_date = اليوم (ميلادي) و end_date حسب plan.days_duration.
        - عند تغيير الباقة (plan) في أي مكان (بما فيه Django admin): نعتبره تجديداً ونُعيد حساب التواريخ من اليوم.

        ملاحظة: لا نُعيد حساب التواريخ عند أي تعديل آخر (مثل تغيير is_active فقط)
        حتى لا يتم تمديد/تجديد الاشتراك بالخطأ.
        """
        from datetime import timedelta

        today = timezone.localdate()

        should_recalc = self.pk is None
        if not should_recalc and self.pk is not None:
            try:
                prev = SchoolSubscription.objects.filter(pk=self.pk).only("plan_id").first()
                if prev is not None and prev.plan_id != self.plan_id:
                    should_recalc = True
            except Exception:
                # في حال تعذّر مقارنة التغيير، لا نغيّر التواريخ على اشتراك موجود
                should_recalc = False

        if should_recalc:
            self.start_date = today
            days = int(getattr(self.plan, "days_duration", 0) or 0)
            if days <= 0:
                self.end_date = today
            else:
                # end_date = اليوم + (المدة - 1) حتى تكون الأيام الفعلية = days_duration
                self.end_date = today + timedelta(days=days - 1)

        return super().save(*args, **kwargs)

    @property
    def is_expired(self):
        if bool(self.is_cancelled):
            return True
        if not self.is_active:
            return True
        # localdate(), not now().date(): end_date is set from the local calendar
        # in save(), so comparing against the UTC date kept a subscription alive
        # through the first hours of each local day past its end.
        return timezone.localdate() > self.end_date

    @property
    def is_cancelled(self) -> bool:
        # الإلغاء المقصود: وجود تاريخ إلغاء (أو سبب) مع إيقاف الاشتراك.
        # لا نعتمد فقط على is_active=False لأن ذلك قد يُستخدم للإيقاف المؤقت.
        if bool(self.canceled_at) and not bool(self.is_active):
            return True
        if (self.cancel_reason or "").strip() and not bool(self.is_active):
            return True
        return False

    @property
    def days_remaining(self):
        delta = self.end_date - timezone.localdate()
        return delta.days


class Payment(models.Model):
    class Method(models.TextChoices):
        BANK_TRANSFER = "bank_transfer", "تحويل بنكي"
        TAMARA = "tamara", "تمارا"
        MOYASAR = "moyasar", "ميّسر"

    class Status(models.TextChoices):
        PENDING = "pending", "قيد المراجعة"
        APPROVED = "approved", "مقبول"
        REJECTED = "rejected", "مرفوض"
        CANCELLED = "cancelled", "ملغي"

    class Purpose(models.TextChoices):
        SUBSCRIPTION = "subscription", "اشتراك المدرسة"
        ARCHIVE_ADDON = "archive_addon", "إضافة الأرشفة"
        # القيمة المخزّنة تبقى ``archive_storage`` للتوافق مع السجلات القائمة،
        # لكن هذا الغرض كان دائمًا يضيف إلى مساحة العمل لا الأرشيف — والاسم
        # المعروض كان يقول العكس.
        WORK_STORAGE = "archive_storage", "زيادة مساحة عمل المدرسة"
        ARCHIVE_SPACE = "archive_space", "زيادة مساحة الأرشفة السنوية"

    school = models.ForeignKey(
        School,
        on_delete=models.CASCADE,
        related_name="payments",
        verbose_name="المدرسة"
    )

    requested_plan = models.ForeignKey(
        "SubscriptionPlan",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="payment_requests",
        verbose_name="الباقة المطلوبة",
    )
    requested_teacher_limit = models.PositiveIntegerField(
        "سعة المعلمين المطلوبة",
        null=True,
        blank=True,
        help_text="لقطة للسعة المرنة التي اختارتها المدرسة وقت إنشاء طلب الدفع.",
    )

    subscription = models.ForeignKey(
        SchoolSubscription,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="payments",
        verbose_name="الاشتراك المرتبط"
    )
    amount = models.DecimalField("المبلغ", max_digits=10, decimal_places=2)
    payment_method = models.CharField(
        "طريقة الدفع",
        max_length=20,
        choices=Method.choices,
        default=Method.BANK_TRANSFER,
        db_index=True,
    )
    gateway_order_id = models.CharField(
        "رقم طلب بوابة الدفع",
        max_length=160,
        blank=True,
        default="",
        db_index=True,
    )
    gateway_checkout_id = models.CharField(
        "رقم جلسة بوابة الدفع",
        max_length=160,
        blank=True,
        default="",
    )
    gateway_status = models.CharField(
        "حالة بوابة الدفع",
        max_length=32,
        blank=True,
        default="",
        db_index=True,
    )
    gateway_capture_id = models.CharField(
        "رقم تحصيل بوابة الدفع",
        max_length=160,
        blank=True,
        default="",
    )
    gateway_completed_at = models.DateTimeField(
        "وقت اكتمال عملية البوابة",
        null=True,
        blank=True,
        editable=False,
    )
    purpose = models.CharField(
        "نوع العملية",
        max_length=32,
        choices=Purpose.choices,
        default=Purpose.SUBSCRIPTION,
        db_index=True,
    )
    archive_storage_gb = models.PositiveIntegerField(
        "المساحة الإضافية المطلوبة (GB)",
        default=0,
        help_text=(
            "تُستخدم في طلبات زيادة المساحة. يحدّد ``الغرض`` أي مساحة تُضاف "
            "إليها: عمل المدرسة أم الأرشفة السنوية."
        ),
    )
    batch_ref = models.CharField(
        "مرجع الطلب الموحّد",
        max_length=32,
        blank=True,
        default="",
        db_index=True,
        help_text="يربط عمليات الدفع التي أُنشئت معًا ضمن طلب موحّد واحد (إيصال واحد).",
    )
    receipt_image = models.ImageField(
        "صورة الإيصال",
        upload_to=_payment_receipt_upload_to,
        help_text="يرجى إرفاق صورة التحويل البنكي",
        null=True,
        blank=True,
        validators=[validate_image_file],
    )
    payment_date = models.DateField("تاريخ التحويل", default=timezone.now)
    status = models.CharField(
        "الحالة", 
        max_length=20, 
        choices=Status.choices, 
        default=Status.PENDING, 
        db_index=True
    )
    notes = models.TextField("ملاحظات الإدارة", blank=True)
    
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        verbose_name="قام بالرفع"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    effects_applied_at = models.DateTimeField(
        "وقت تطبيق أثر الدفع",
        null=True,
        blank=True,
        editable=False,
        help_text="يمنع تطبيق التفعيل أو زيادة المساحة أكثر من مرة لنفس عملية الدفع.",
    )

    class Meta:
        verbose_name = "عملية دفع"
        verbose_name_plural = "المدفوعات والإيرادات"
        ordering = ['-created_at']

    def __str__(self):
        return f"دفع #{self.id} - {self.school.name} - {self.amount}"


# =========================


class SchoolArchiveAddon(models.Model):
    """استحقاق مستقل لأرشفة ملفات المدرسة وسجلاتها الإدارية.

    هذا الملحق منفصل عن باقة الاشتراك الأساسية، بحيث يمكن تفعيله لأي مدرسة
    بغض النظر عن الخطة الحالية.
    """

    school = models.OneToOneField(
        School,
        on_delete=models.CASCADE,
        related_name="archive_addon",
        verbose_name="المدرسة",
    )
    is_enabled = models.BooleanField("مفعّل؟", default=True, db_index=True)
    start_date = models.DateField("تاريخ بداية الملحق", default=timezone.localdate)
    end_date = models.DateField(
        "تاريخ نهاية الملحق",
        null=True,
        blank=True,
        help_text="اتركه فارغًا إذا كان الملحق مفتوح المدة.",
        db_index=True,
    )
    storage_limit_gb = models.PositiveIntegerField(
        "حد مساحة النسخ السنوية (GB)",
        default=50,
        help_text="مساحة الأرشفة وحدها، مستقلة عن مساحة عمل المدرسة اليومية.",
    )
    storage_used_bytes = models.PositiveBigIntegerField(
        "المستخدم من مساحة النسخ السنوية (بايت)",
        default=0,
        help_text=(
            "مجموع أحجام النسخ السنوية المحفوظة فقط. يُحدَّث تلقائيًا ولا يشمل "
            "ملفات العمل اليومي."
        ),
    )
    paid_amount = models.DecimalField("قيمة الملحق", max_digits=10, decimal_places=2, default=0)
    notes = models.TextField("ملاحظات", blank=True, default="")
    created_at = models.DateTimeField("تاريخ الإنشاء", auto_now_add=True)
    updated_at = models.DateTimeField("تاريخ التحديث", auto_now=True)

    class Meta:
        verbose_name = "ملحق أرشفة مدرسة"
        verbose_name_plural = "ملحقات أرشفة المدارس"
        indexes = [
            models.Index(fields=["is_enabled", "end_date"]),
        ]

    def __str__(self):
        return f"أرشفة {self.school.name}"

    @property
    def is_active(self) -> bool:
        if not self.is_enabled:
            return False
        today = timezone.localdate()
        if self.start_date and self.start_date > today:
            return False
        if self.end_date and self.end_date < today:
            return False
        return True

    @property
    def days_remaining(self):
        if not self.end_date:
            return None
        return (self.end_date - timezone.localdate()).days

    @property
    def storage_used_gb(self) -> float:
        return round((self.storage_used_bytes or 0) / (1024 ** 3), 2)

    @property
    def storage_usage_percent(self) -> int:
        if not self.storage_limit_gb:
            return 0
        used_gb = (self.storage_used_bytes or 0) / (1024 ** 3)
        return min(100, int((used_gb / self.storage_limit_gb) * 100))


def school_has_archive_addon(school: School | None) -> bool:
    if school is None:
        return False
    try:
        addon = getattr(school, "archive_addon", None)
    except Exception:
        addon = None
    try:
        return bool(addon and addon.is_active)
    except Exception:
        return False


class ArchiveStorageOption(models.Model):
    """باقات زيادة المساحة التي يسعّرها مدير النظام وتشتريها المدرسة.

    لكل خيار **دلوٌ** يحدّد أين تُضاف المساحة. المساحتان منفصلتان في النظام
    (حدّان وعدّادان وبوابتان)، فبيعهما بمنتج واحد كان يعني أن المدرسة تدفع
    لتوسعة مساحة غير التي امتلأت عندها.
    """

    class Bucket(models.TextChoices):
        WORK = "work", "مساحة عمل المدرسة"
        ARCHIVE = "archive", "مساحة الأرشفة السنوية"

    bucket = models.CharField(
        "المساحة المستهدفة",
        max_length=16,
        choices=Bucket.choices,
        default=Bucket.WORK,
        db_index=True,
        help_text=(
            "عمل المدرسة: التقارير والإنجاز والوثائق والمرفقات. "
            "الأرشفة السنوية: النسخ الثابتة المحفوظة لكل سنة."
        ),
    )
    storage_gb = models.PositiveIntegerField("المساحة (GB)", validators=[MinValueValidator(1)])
    price = models.DecimalField("السعر", max_digits=10, decimal_places=2, validators=[MinValueValidator(0)])
    is_active = models.BooleanField("مفعّل؟", default=True, db_index=True)
    sort_order = models.PositiveSmallIntegerField("الترتيب", default=10)
    created_at = models.DateTimeField("تاريخ الإنشاء", auto_now_add=True)
    updated_at = models.DateTimeField("تاريخ التحديث", auto_now=True)

    class Meta:
        verbose_name = "خيار زيادة مساحة"
        verbose_name_plural = "خيارات زيادة المساحة"
        ordering = ["bucket", "sort_order", "storage_gb", "id"]

    def __str__(self):
        return f"{self.get_bucket_display()} · {self.storage_gb}GB - {self.price} ريال"


def school_year_archive_upload_to(instance, filename: str) -> str:
    """Private, collision-resistant path for immutable yearly archive packages."""
    school_code = slugify(getattr(getattr(instance, "school", None), "code", "") or "school")
    year = slugify(str(getattr(instance, "academic_year", "") or "year"), allow_unicode=False) or "year"
    extension = os.path.splitext(filename or "")[1].lower() or ".zip"
    return f"school-archives/{school_code}/{year}/{uuid.uuid4().hex}{extension}"


class SchoolYearArchive(models.Model):
    """Immutable ZIP snapshot of one school academic year."""

    class Status(models.TextChoices):
        READY = "ready", "مكتمل"
        PARTIAL = "partial", "مكتمل مع ملاحظات"
        FAILED = "failed", "فشل الإنشاء"

    school = models.ForeignKey(
        School,
        on_delete=models.CASCADE,
        related_name="year_archives",
        verbose_name="المدرسة",
    )
    academic_year = models.CharField("السنة الدراسية", max_length=32, db_index=True)
    version = models.PositiveIntegerField("رقم النسخة", default=1)
    status = models.CharField(
        "حالة النسخة",
        max_length=16,
        choices=Status.choices,
        default=Status.READY,
        db_index=True,
    )
    archive_file = models.FileField(
        "ملف الأرشيف",
        upload_to=school_year_archive_upload_to,
        max_length=500,
        blank=True,
    )
    storage_bytes = models.PositiveBigIntegerField("حجم التخزين", default=0)
    archive_sha256 = models.CharField("بصمة ملف ZIP", max_length=64, blank=True, default="")
    file_count = models.PositiveIntegerField("عدد الملفات", default=0)
    missing_file_count = models.PositiveIntegerField("ملفات مفقودة", default=0)
    failed_pdf_count = models.PositiveIntegerField("ملفات PDF متعذرة", default=0)
    report_count = models.PositiveIntegerField("عدد التقارير", default=0)
    achievement_count = models.PositiveIntegerField("عدد ملفات الإنجاز", default=0)
    leadership_count = models.PositiveIntegerField("عدد ملفات الأداء القيادي", default=0)
    ticket_count = models.PositiveIntegerField("عدد التذاكر", default=0)
    circular_count = models.PositiveIntegerField("عدد التعاميم", default=0)
    notification_count = models.PositiveIntegerField("عدد الإشعارات", default=0)
    notes = models.TextField("تقرير الإنشاء", blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_school_year_archives",
        verbose_name="أنشأها",
    )
    created_at = models.DateTimeField("تاريخ إنشاء النسخة", auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-created_at", "-id")
        constraints = [
            models.UniqueConstraint(
                fields=["school", "academic_year", "version"],
                name="uniq_school_year_archive_version",
            ),
        ]
        indexes = [
            models.Index(
                fields=["school", "academic_year", "-created_at"],
                name="reports_sya_school_year_idx",
            ),
            models.Index(
                fields=["school", "status"],
                name="reports_sya_school_status_idx",
            ),
        ]
        verbose_name = "نسخة أرشيف سنة"
        verbose_name_plural = "نسخ أرشيف السنوات"

    def __str__(self) -> str:
        return f"{self.school.name} - {self.academic_year} - نسخة {self.version}"

    def save(self, *args, **kwargs):
        if self.pk:
            original = type(self).objects.filter(pk=self.pk).values(
                "school_id",
                "academic_year",
                "version",
                "status",
                "archive_file",
                "archive_sha256",
            ).first()
            if original:
                current = {
                    "school_id": self.school_id,
                    "academic_year": self.academic_year,
                    "version": self.version,
                    "status": self.status,
                    "archive_file": getattr(self.archive_file, "name", "") or "",
                    "archive_sha256": self.archive_sha256,
                }
                if any(original[field] != value for field, value in current.items()):
                    raise ValidationError("نسخة الأرشيف ثابتة ولا يمكن تعديل ملفها أو هويتها بعد الحفظ.")
        return super().save(*args, **kwargs)

    @property
    def is_downloadable(self) -> bool:
        return bool(
            self.status in {self.Status.READY, self.Status.PARTIAL}
            and getattr(self.archive_file, "name", "")
        )

    @property
    def archive_size_mb(self) -> float:
        return round((self.storage_bytes or 0) / (1024 ** 2), 2)


class SchoolYearArchiveDownload(models.Model):
    """Persistent audit trail for every downloaded yearly archive snapshot."""

    archive = models.ForeignKey(
        SchoolYearArchive,
        on_delete=models.CASCADE,
        related_name="downloads",
        verbose_name="نسخة الأرشيف",
    )
    downloaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="school_archive_downloads",
        verbose_name="نزّلها",
    )
    downloaded_at = models.DateTimeField("وقت التنزيل", auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-downloaded_at", "-id")
        indexes = [
            models.Index(
                fields=["archive", "-downloaded_at"],
                name="reports_syad_archive_date_idx",
            )
        ]
        verbose_name = "تنزيل نسخة أرشيف"
        verbose_name_plural = "تنزيلات نسخ الأرشيف"

    def __str__(self) -> str:
        return f"تنزيل {self.archive_id} بواسطة {self.downloaded_by_id or '—'}"


def generated_export_upload_to(instance, filename: str) -> str:
    extension = os.path.splitext(filename or "")[1].lower() or ".bin"
    school_id = int(getattr(instance, "school_id", 0) or 0)
    return f"generated-exports/school-{school_id}/{uuid.uuid4().hex}{extension}"


class GeneratedExportJob(models.Model):
    """Durable state for CPU/IO-heavy ZIP generation.

    Files live in the private default storage (R2 in production), never in
    Redis and never on an ephemeral worker filesystem. Short retention keeps
    ad-hoc downloads from silently becoming a second archive bucket.
    """

    class Kind(models.TextChoices):
        SCHOOL_ZIP = "school_zip", "تصدير المدرسة ZIP"
        YEAR_ZIP = "year_zip", "تصدير السنة ZIP"
        ARCHIVE_SNAPSHOT = "archive_snapshot", "نسخة أرشيف محفوظة"

    class Status(models.TextChoices):
        QUEUED = "queued", "في قائمة الانتظار"
        RUNNING = "running", "قيد الإنشاء"
        READY = "ready", "جاهز"
        FAILED = "failed", "تعذر الإنشاء"
        EXPIRED = "expired", "انتهت الصلاحية"

    school = models.ForeignKey(
        School,
        on_delete=models.CASCADE,
        related_name="generated_export_jobs",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="generated_export_jobs",
    )
    kind = models.CharField(max_length=32, choices=Kind.choices, db_index=True)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.QUEUED,
        db_index=True,
    )
    parameters = models.JSONField(default=dict, blank=True)
    artifact_file = models.FileField(
        upload_to=generated_export_upload_to,
        max_length=500,
        blank=True,
    )
    archive = models.ForeignKey(
        SchoolYearArchive,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="generation_jobs",
    )
    filename = models.CharField(max_length=255, blank=True, default="")
    content_type = models.CharField(max_length=100, blank=True, default="application/zip")
    size_bytes = models.PositiveBigIntegerField(default=0)
    error_message = models.CharField(max_length=500, blank=True, default="")
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(
                fields=["requested_by", "status", "-created_at"],
                name="reports_gej_user_status_idx",
            ),
            models.Index(
                fields=["school", "kind", "status"],
                name="reports_gej_school_kind_idx",
            ),
        ]

    @property
    def is_ready(self) -> bool:
        return self.status == self.Status.READY and bool(
            self.archive_id or getattr(self.artifact_file, "name", "")
        )

    def __str__(self) -> str:
        return f"{self.get_kind_display()} #{self.pk} ({self.get_status_display()})"
