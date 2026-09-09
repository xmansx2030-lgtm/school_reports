from __future__ import annotations

from datetime import timedelta

from .base import *  # noqa: F401,F403


class ErasureRequest(models.Model):  # noqa: F405
    """طلب إتلاف بيانات، مسجَّلاً لا مُهمَلاً.

    **لماذا طلبٌ لا زرُّ حذفٍ فوري.** لأن الحذف الفوري في هذه المنصة خطأٌ من
    ثلاث جهات مجتمعة:

    * **المحتوى ليس كلُّه ملكَ صاحب الحساب.** تقريرُ المعلّم عملٌ مدرسي،
      والمدرسةُ هي المتحكّم فيه. ومحوُه بضغطة يُفرِّغ أرشيف المدرسة من عملٍ
      وُثِّق واعتُمد وبُني عليه.
    * **سجلّ التدقيق مقصودٌ بقاؤه.** ``AuditLog.teacher`` صار ``SET_NULL`` مع
      لقطة اسم الفاعل عمداً — «محوُه كان يجعل حذف الحساب أداةً لطمس ما فعله».
      وزرُّ حذفٍ ذاتي فوري يُعيد فتح ذلك الباب بالضبط.
    * **النظام نفسه لا يوجب الفورية.** نظام حماية البيانات الشخصية يُقيّد حق
      الإتلاف بـ«الحالات المقررة»، ويستثني ما يلزم لتنفيذ عقد أو للوفاء
      بالتزام نظامي — وسياسة المنصة تقول ذلك حرفياً.

    فالصواب طلبٌ **يُسجَّل بتاريخه ولا يضيع**، ويصل من يملك الموازنة بين حق
    صاحب البيانات وحقوق المدرسة والالتزام النظامي. والفرق بين هذا وبين نموذج
    الشكاوى الحالي أن الطلب هنا مرتبطٌ بالحساب، ومُتتبَّعُ الحالة، ولصاحبه أن
    يرى أين وصل — فلا يبقى «أرسلتُ بريداً ولم يردّ أحد».
    """

    class Status(models.TextChoices):
        RECEIVED = "received", "مستلَم"
        IN_REVIEW = "in_review", "قيد الدراسة"
        COMPLETED = "completed", "نُفِّذ"
        REFUSED = "refused", "مرفوض بمسوّغ نظامي"

    teacher = models.ForeignKey(  # noqa: F405
        "reports.Teacher",
        on_delete=models.CASCADE,
        related_name="erasure_requests",
        verbose_name="صاحب الطلب",
    )
    reason = models.TextField(
        "سبب الطلب",
        blank=True,
        default="",
        help_text="اختياري. يساعد على تحديد المسوّغ النظامي بدقة.",
    )
    status = models.CharField(
        "الحالة",
        max_length=20,
        choices=Status.choices,
        default=Status.RECEIVED,
    )
    response_note = models.TextField(
        "ردّ المنصة",
        blank=True,
        default="",
        help_text="يُبلَّغ به صاحب الطلب. الرفض يجب أن يذكر مسوّغه النظامي.",
    )
    execution_evidence = models.TextField(
        "محضر التنفيذ الداخلي",
        blank=True,
        default="",
        help_text="إلزامي عند اعتبار الطلب منفذاً: دوّن ما أُتلف وما استُبقي ومرجع الإجراء.",
    )
    created_at = models.DateTimeField("تاريخ الطلب", default=timezone.now)  # noqa: F405
    extended_until = models.DateTimeField(
        "الموعد بعد التمديد",
        null=True,
        blank=True,
        help_text="تمديد واحد لا يتجاوز 30 يوماً إضافياً، مع بيان السبب لصاحب الطلب.",
    )
    extension_reason = models.TextField(
        "سبب التمديد",
        blank=True,
        default="",
        help_text="إلزامي عند تمديد موعد الرد، ويظهر لصاحب الطلب.",
    )
    extension_notified_at = models.DateTimeField(
        "تاريخ إشعار صاحب الطلب بالتمديد", null=True, blank=True
    )
    resolved_at = models.DateTimeField("تاريخ البتّ", null=True, blank=True)
    resolved_by = models.ForeignKey(  # noqa: F405
        "reports.Teacher",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="erasure_requests_resolved",
        verbose_name="بتَّ فيه",
    )

    class Meta:
        verbose_name = "طلب إتلاف بيانات"
        verbose_name_plural = "طلبات إتلاف البيانات"
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(fields=["status", "-created_at"]),  # noqa: F405
            models.Index(fields=["teacher", "-created_at"]),  # noqa: F405
        ]
        constraints = [
            # طلبٌ مفتوحٌ واحد لكل شخص: تكرارُ الإرسال لا يُنتج صفوفاً تُشتّت
            # المعالجة، والحالة الواحدة تكفي لتتبّعها.
            models.UniqueConstraint(  # noqa: F405
                fields=["teacher"],
                condition=models.Q(status__in=["received", "in_review"]),  # noqa: F405
                name="one_open_erasure_request_per_teacher",
            ),
        ]

    def __str__(self) -> str:
        return f"ErasureRequest#{self.pk} · teacher#{self.teacher_id} · {self.status}"

    @property
    def is_open(self) -> bool:
        return self.status in {self.Status.RECEIVED, self.Status.IN_REVIEW}

    @property
    def response_due_at(self):
        return self.created_at + timedelta(days=30)

    @property
    def effective_due_at(self):
        return self.extended_until or self.response_due_at

    def clean(self):
        super().clean()
        errors = {}
        if self.status in {self.Status.COMPLETED, self.Status.REFUSED} and not self.response_note.strip():
            errors["response_note"] = "اكتب رداً واضحاً لصاحب الطلب قبل إغلاقه."
        if self.status == self.Status.COMPLETED and not self.execution_evidence.strip():
            errors["execution_evidence"] = "وثّق ما أُتلف وما استُبقي قبل اعتبار الطلب منفذاً."
        if self.extended_until:
            if not self.extension_reason.strip():
                errors["extension_reason"] = "اذكر سبب التمديد الذي سيظهر لصاحب الطلب."
            if self.extended_until <= self.response_due_at:
                errors["extended_until"] = "موعد التمديد يجب أن يكون بعد مهلة الرد الأصلية."
            elif self.extended_until > self.response_due_at + timedelta(days=30):
                errors["extended_until"] = "لا يجوز أن يتجاوز التمديد 30 يوماً إضافياً."
        elif self.extension_reason.strip():
            errors["extended_until"] = "حدّد الموعد الجديد المرتبط بسبب التمديد."
        if errors:
            raise ValidationError(errors)  # noqa: F405
