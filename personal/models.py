import uuid
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator, MaxValueValidator, MinValueValidator
from django.db import models, transaction
from django.utils import timezone

from reports.validators import validate_circular_attachment_file, validate_image_file
from reports.model_parts.achievements import AchievementSection


def personal_evidence_path(instance, filename):
    suffix = Path(filename).suffix.lower()
    return f"personal/{instance.workspace_id}/evidence/{uuid.uuid4().hex}{suffix}"


def validate_personal_evidence_file(file_obj):
    """Accept the school's normalized WebP images alongside existing PDFs."""
    if Path(getattr(file_obj, "name", "")).suffix.lower() == ".webp":
        validate_image_file(file_obj)
    else:
        validate_circular_attachment_file(file_obj)


class PersonalWorkspace(models.Model):
    owner = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="personal_workspace"
    )
    school_name = models.CharField("اسم المدرسة للتعريف", max_length=200)
    principal_name = models.CharField("اسم مدير المدرسة للتعريف", max_length=150, blank=True)
    school_stage = models.CharField("المرحلة", max_length=50, blank=True)
    specialization = models.CharField("التخصص", max_length=120, blank=True)
    current_academic_year = models.CharField("السنة الدراسية الحالية", max_length=20, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "مساحة معلم شخصية"
        verbose_name_plural = "مساحات المعلمين الشخصية"

    def __str__(self):
        return f"المساحة الشخصية: {self.owner.name}"


class PersonalPlan(models.Model):
    code = models.SlugField("رمز الباقة", max_length=40, unique=True)
    name = models.CharField("اسم الباقة", max_length=100)
    description = models.CharField("وصف مختصر للعرض", max_length=240, blank=True)
    price = models.DecimalField(
        "السعر بالريال", max_digits=9, decimal_places=2, default=0,
        validators=[MinValueValidator(0)],
    )
    duration_days = models.PositiveIntegerField(
        "مدة الاشتراك بالأيام", default=0,
        help_text="صفر للباقة المستمرة؛ الباقات المدفوعة تتطلب مدة محددة.",
    )
    max_reports = models.PositiveIntegerField("الحد الأعلى للتقارير", default=500, validators=[MinValueValidator(1)])
    max_evidence = models.PositiveIntegerField("الحد الأعلى للشواهد", default=250, validators=[MinValueValidator(1)])
    storage_limit_mb = models.PositiveIntegerField("سعة الملفات بالميجابايت", default=500, validators=[MinValueValidator(1)])
    report_ai_daily_limit = models.PositiveSmallIntegerField(
        "تحسينات التقارير يوميًا", default=0, validators=[MaxValueValidator(3)],
        help_text="صفر لتعطيل التحسين في الباقة؛ الباقة الأساسية المجانية لا تمنح استخدامًا مدفوعًا.",
    )
    voice_report_daily_limit = models.PositiveSmallIntegerField(
        "تسجيلات التقارير يوميًا", default=0, validators=[MaxValueValidator(3)],
        help_text="صفر لتعطيل التفريغ في الباقة؛ الباقة الأساسية المجانية لا تمنح استخدامًا مدفوعًا.",
    )
    is_active = models.BooleanField("متاحة للاشتراكات الجديدة", default=True)
    is_published = models.BooleanField("تظهر في صفحة الهبوط", default=True)
    display_order = models.PositiveSmallIntegerField("ترتيب العرض", default=0)

    class Meta:
        verbose_name = "باقة معلم شخصية"
        verbose_name_plural = "باقات المعلمين الشخصية"

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        if self.code != "personal_free" and self.price == 0:
            raise ValidationError({"price": "الباقات الإضافية تتطلب سعرًا؛ الباقة الأساسية وحدها يمكن أن تكون مجانية."})
        if self.price and not self.duration_days:
            raise ValidationError({"duration_days": "حدد مدة للباقة المدفوعة."})
        if self.price == 0 and (self.report_ai_daily_limit or self.voice_report_daily_limit):
            raise ValidationError({
                "report_ai_daily_limit": "الأدوات المدفوعة تتطلب باقة مدفوعة.",
                "voice_report_daily_limit": "الأدوات المدفوعة تتطلب باقة مدفوعة.",
            })
        if self.pk and PersonalPlan.objects.filter(pk=self.pk, code="personal_free").exists():
            if self.code != "personal_free":
                raise ValidationError({"code": "لا يمكن تغيير رمز الباقة الأساسية."})

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class PersonalSubscription(models.Model):
    workspace = models.OneToOneField(
        PersonalWorkspace, on_delete=models.CASCADE, related_name="subscription"
    )
    plan = models.ForeignKey(PersonalPlan, on_delete=models.PROTECT)
    start_date = models.DateField(default=timezone.localdate)
    end_date = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "اشتراك معلم شخصي"
        verbose_name_plural = "اشتراكات المعلمين الشخصية"

    def save(self, *args, **kwargs):
        previous_plan_id = None
        if self.pk:
            previous_plan_id = PersonalSubscription.objects.filter(pk=self.pk).values_list("plan_id", flat=True).first()
        if self._state.adding or previous_plan_id != self.plan_id:
            self.start_date = timezone.localdate()
            days = self.plan.duration_days
            self.end_date = self.start_date + timedelta(days=days - 1) if days and self.is_active else None
        return super().save(*args, **kwargs)

    @property
    def is_current(self):
        today = timezone.localdate()
        return self.is_active and self.start_date <= today and (
            self.end_date is None or today <= self.end_date
        )


class PersonalPayment(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "بانتظار الدفع"
        PAID = "paid", "مدفوع ومفعّل"
        FAILED = "failed", "فشل الدفع"
        CANCELLED = "cancelled", "ملغي"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        PersonalWorkspace, on_delete=models.CASCADE, related_name="payments"
    )
    plan = models.ForeignKey(PersonalPlan, on_delete=models.PROTECT)
    plan_name = models.CharField("اسم الباقة وقت الشراء", max_length=100)
    duration_days = models.PositiveIntegerField("مدة الباقة وقت الشراء")
    amount = models.DecimalField("المبلغ بالريال", max_digits=9, decimal_places=2)
    customer_name = models.CharField("اسم المشترك وقت الشراء", max_length=150)
    customer_email = models.EmailField("بريد الفاتورة", max_length=254)
    school_name = models.CharField("المدرسة للتعريف وقت الشراء", max_length=200, blank=True)
    status = models.CharField(
        "الحالة", max_length=12, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    gateway_invoice_id = models.CharField(max_length=160, blank=True, default="", db_index=True)
    gateway_payment_id = models.CharField(max_length=160, blank=True, default="")
    gateway_status = models.CharField(max_length=32, blank=True, default="", db_index=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    activated_at = models.DateTimeField(null=True, blank=True)
    email_sent_at = models.DateTimeField(null=True, blank=True)
    email_sending_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "created_at"])]
        constraints = [
            models.UniqueConstraint(
                fields=["gateway_invoice_id"],
                condition=~models.Q(gateway_invoice_id=""),
                name="personal_payment_gateway_invoice_uniq",
            ),
        ]
        verbose_name = "دفعة مساحة شخصية"
        verbose_name_plural = "دفعات المساحات الشخصية"

    def __str__(self):
        return f"{self.plan_name} · {self.amount} SAR · {self.status}"


class PersonalReport(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "مسودة"
        COMPLETE = "complete", "مكتمل"
        ARCHIVED = "archived", "مؤرشف"

    workspace = models.ForeignKey(
        PersonalWorkspace, on_delete=models.CASCADE, related_name="reports"
    )
    client_submission_id = models.UUIDField(null=True, blank=True, unique=True, editable=False)
    title = models.CharField("عنوان العمل", max_length=255)
    category = models.CharField("نوع العمل", max_length=100, blank=True)
    report_date = models.DateField("تاريخ العمل")
    academic_year = models.CharField("السنة الدراسية", max_length=20)
    description = models.TextField("وصف العمل", blank=True)
    show_details = models.BooleanField("إظهار تفاصيل التقرير", default=True)
    goals = models.TextField("الأهداف", blank=True)
    implementation = models.TextField("التنفيذ", blank=True)
    results = models.TextField("النتائج", blank=True)
    recommendations = models.TextField("التوصيات", blank=True)
    beneficiaries_count = models.PositiveIntegerField("عدد المستفيدين", null=True, blank=True)
    show_goals = models.BooleanField("إظهار الأهداف", default=True)
    show_implementation = models.BooleanField("إظهار آلية التنفيذ", default=True)
    show_results = models.BooleanField("إظهار النتائج", default=True)
    show_recommendations = models.BooleanField("إظهار التوصيات", default=True)
    show_beneficiaries = models.BooleanField("إظهار عدد المستفيدين", default=False)
    status = models.CharField(
        "الحالة", max_length=12, choices=Status.choices, default=Status.DRAFT
    )
    teacher_name = models.CharField("اسم المعلم وقت الحفظ", max_length=150)
    school_name = models.CharField("اسم المدرسة وقت الحفظ", max_length=200)
    principal_name = models.CharField("اسم المدير وقت الحفظ", max_length=150, blank=True)
    trashed_at = models.DateTimeField("نُقل إلى سلة المحذوفات في", null=True, blank=True, db_index=True)
    trashed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="trashed_personal_reports", verbose_name="نُقل إلى السلة بواسطة",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-report_date", "-id"]
        indexes = [models.Index(fields=["workspace", "academic_year", "status"])]
        verbose_name = "تقرير معلم شخصي"
        verbose_name_plural = "تقارير المعلمين الشخصية"

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields")
        year_is_written = update_fields is None or "academic_year" in update_fields
        previous_year = (
            PersonalReport.objects.filter(pk=self.pk).values_list("academic_year", flat=True).first()
            if self.pk and year_is_written else None
        )
        if previous_year is not None and previous_year != self.academic_year:
            with transaction.atomic():
                result = super().save(*args, **kwargs)
                self.share_links.filter(is_active=True).update(is_active=False)
                return result
        return super().save(*args, **kwargs)

    def move_to_trash(self, *, by=None):
        with transaction.atomic():
            if self.trashed_at is None:
                self.trashed_at = timezone.now()
                self.trashed_by = by
                self.save(update_fields=["trashed_at", "trashed_by"])
            # Restoring the report must never reactivate a link issued before trashing.
            self.share_links.filter(is_active=True).update(is_active=False)

    def restore_from_trash(self):
        if self.trashed_at is not None:
            self.trashed_at = None
            self.trashed_by = None
            self.save(update_fields=["trashed_at", "trashed_by"])


class PersonalEvidence(models.Model):
    class DisplaySize(models.TextChoices):
        AUTO = "auto", "تلقائي"
        LARGE = "large", "كبير"
        MEDIUM = "medium", "متوسط"
        SMALL = "small", "صغير"

    class FitMode(models.TextChoices):
        CONTAIN = "contain", "احتواء الصورة كاملة"
        COVER = "cover", "ملء الإطار"

    workspace = models.ForeignKey(
        PersonalWorkspace, on_delete=models.CASCADE, related_name="evidence"
    )
    report = models.ForeignKey(
        PersonalReport, on_delete=models.SET_NULL, null=True, blank=True, related_name="evidence"
    )
    initiative = models.ForeignKey(
        "PersonalInitiative", on_delete=models.SET_NULL, null=True, blank=True, related_name="evidence"
    )
    title = models.CharField("عنوان الشاهد", max_length=200)
    report_caption = models.CharField("وصف الشاهد في التقرير", max_length=220, blank=True, default="")
    description = models.TextField("الوصف", blank=True)
    academic_year = models.CharField("السنة الدراسية", max_length=20)
    file = models.FileField(
        "الملف", upload_to=personal_evidence_path, blank=True,
        validators=[validate_personal_evidence_file, FileExtensionValidator(["pdf", "jpg", "jpeg", "png", "webp"])],
    )
    source_url = models.URLField("رابط الشاهد", blank=True)
    order = models.PositiveSmallIntegerField("الترتيب", default=1, db_index=True)
    display_size = models.CharField(
        "حجم العرض", max_length=10, choices=DisplaySize.choices, default=DisplaySize.AUTO
    )
    fit_mode = models.CharField(
        "طريقة الملاءمة", max_length=10, choices=FitMode.choices, default=FitMode.CONTAIN
    )
    show_in_print = models.BooleanField("إظهار في الطباعة", default=True)
    file_size = models.PositiveIntegerField(default=0, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["workspace", "academic_year"])]
        verbose_name = "شاهد شخصي"
        verbose_name_plural = "شواهد شخصية"

    def __str__(self):
        return self.title

    @property
    def report_card_caption(self):
        return self.report_caption or self.title

    @property
    def is_image(self):
        return bool(self.file and Path(self.file.name).suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"})

    def save(self, *args, **kwargs):
        if self.report_id and self.workspace_id:
            report = PersonalReport.objects.filter(pk=self.report_id, workspace_id=self.workspace_id).first()
            if report is None or report.academic_year != self.academic_year:
                raise ValidationError("الشاهد والتقرير يجب أن يكونا في المساحة الشخصية والسنة نفسها.")
        if self.initiative_id and self.workspace_id:
            initiative = PersonalInitiative.objects.filter(pk=self.initiative_id, workspace_id=self.workspace_id).first()
            if initiative is None or initiative.academic_year != self.academic_year:
                raise ValidationError("الشاهد والمبادرة يجب أن يكونا في المساحة الشخصية والسنة نفسها.")
        return super().save(*args, **kwargs)


class PersonalAcademicYear(models.Model):
    workspace = models.ForeignKey(PersonalWorkspace, on_delete=models.CASCADE, related_name="academic_years")
    value = models.CharField("السنة الدراسية", max_length=20)
    archived_at = models.DateTimeField("أُرشفت في", null=True, blank=True)
    qualifications = models.TextField("المؤهلات", blank=True)
    professional_experience = models.TextField("الخبرات المهنية", blank=True)
    specialization = models.TextField("التخصص", blank=True)
    teaching_load = models.TextField("نصاب الحصص", blank=True)
    subjects_taught = models.TextField("مواد التدريس", blank=True)
    contact_info = models.TextField("بيانات التواصل", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-value"]
        constraints = [models.UniqueConstraint(fields=["workspace", "value"], name="unique_personal_workspace_year")]

    def __str__(self):
        return self.value


class PersonalInitiative(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "مسودة"
        COMPLETE = "complete", "مكتملة"
        ARCHIVED = "archived", "مؤرشفة"

    workspace = models.ForeignKey(PersonalWorkspace, on_delete=models.CASCADE, related_name="initiatives")
    academic_year = models.CharField("السنة الدراسية", max_length=20)
    title = models.CharField("عنوان المبادرة", max_length=200)
    summary = models.TextField("الفكرة والتنفيذ")
    impact = models.TextField("الأثر والنتائج", blank=True)
    is_best_practice = models.BooleanField("ممارسة ناجحة", default=False)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["workspace", "academic_year", "status"])]

    def __str__(self):
        return self.title


class PersonalPortfolioSection(models.Model):
    """One teacher-owned achievement axis for a personal academic year."""

    workspace = models.ForeignKey(PersonalWorkspace, on_delete=models.CASCADE, related_name="portfolio_sections")
    academic_year = models.CharField("السنة الدراسية", max_length=20)
    code = models.PositiveSmallIntegerField("المحور", choices=AchievementSection.Code.choices)
    teacher_notes = models.TextField("وصف الممارسة", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["code", "id"]
        constraints = [models.UniqueConstraint(
            fields=["workspace", "academic_year", "code"], name="unique_personal_portfolio_axis"
        )]

    def __str__(self):
        return f"{self.workspace_id} · {self.academic_year} · {self.get_code_display()}"


class PersonalPortfolioReport(models.Model):
    section = models.ForeignKey(PersonalPortfolioSection, on_delete=models.CASCADE, related_name="linked_reports")
    report = models.ForeignKey(PersonalReport, on_delete=models.CASCADE, related_name="portfolio_links")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [models.UniqueConstraint(fields=["section", "report"], name="unique_personal_axis_report")]

    def clean(self):
        super().clean()
        if self.section_id and self.report_id and (
            self.section.workspace_id != self.report.workspace_id or
            self.section.academic_year != self.report.academic_year
        ):
            raise ValidationError("يجب أن يخص التقرير صاحب المحور وسنته الدراسية.")

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class PersonalPortfolioEvidence(models.Model):
    section = models.ForeignKey(PersonalPortfolioSection, on_delete=models.CASCADE, related_name="linked_evidence")
    evidence = models.ForeignKey(PersonalEvidence, on_delete=models.CASCADE, related_name="portfolio_links")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [models.UniqueConstraint(fields=["section", "evidence"], name="unique_personal_axis_evidence")]

    def clean(self):
        super().clean()
        if self.section_id and self.evidence_id and (
            self.section.workspace_id != self.evidence.workspace_id or
            self.section.academic_year != self.evidence.academic_year
        ):
            raise ValidationError("يجب أن يخص الشاهد صاحب المحور وسنته الدراسية.")

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class PersonalShareLink(models.Model):
    class Kind(models.TextChoices):
        REPORT = "report", "تقرير"
        PORTFOLIO = "portfolio", "ملف إنجاز"

    workspace = models.ForeignKey(
        PersonalWorkspace, on_delete=models.CASCADE, related_name="share_links",
    )
    kind = models.CharField(max_length=12, choices=Kind.choices, db_index=True)
    report = models.ForeignKey(
        PersonalReport, on_delete=models.CASCADE, null=True, blank=True, related_name="share_links",
    )
    academic_year = models.CharField(max_length=20)
    token = models.CharField(max_length=64, unique=True)
    is_active = models.BooleanField(default=True)
    expires_at = models.DateTimeField(db_index=True)
    access_count = models.PositiveIntegerField(default=0)
    last_accessed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        indexes = [models.Index(fields=["workspace", "kind", "is_active"])]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(kind="report", report__isnull=False)
                    | models.Q(kind="portfolio", report__isnull=True)
                ),
                name="personal_share_target_matches_kind",
            ),
            models.UniqueConstraint(
                fields=["workspace", "report"],
                condition=models.Q(kind="report", is_active=True),
                name="unique_active_personal_report_share",
            ),
            models.UniqueConstraint(
                fields=["workspace", "academic_year"],
                condition=models.Q(kind="portfolio", is_active=True),
                name="unique_active_personal_portfolio_share",
            ),
        ]

    def clean(self):
        super().clean()
        if self.report_id and (
            self.report.workspace_id != self.workspace_id
            or self.report.academic_year != self.academic_year
        ):
            raise ValidationError("رابط المشاركة يجب أن يخص التقرير والمساحة والسنة نفسها.")


class PersonalNotice(models.Model):
    submission_key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    title = models.CharField("العنوان", max_length=160)
    message = models.TextField("الرسالة")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="personal_notices_sent")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]


class PersonalNoticeRecipient(models.Model):
    notice = models.ForeignKey(PersonalNotice, on_delete=models.CASCADE, related_name="recipients")
    workspace = models.ForeignKey(PersonalWorkspace, on_delete=models.CASCADE, related_name="notices")
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["notice", "workspace"], name="unique_personal_notice_recipient")]
        indexes = [models.Index(fields=["workspace", "read_at"])]
