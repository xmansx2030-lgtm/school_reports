from __future__ import annotations

from .approvals import ApprovalMixin
from .base import *
from .schools import Department, School, Teacher
from ..lab_kinds import LabKind

__all__ = ["LabKind", "LabAsset", "LabAssetHandover", "LabExperiment"]


class LabAsset(models.Model):
    """صنف في عهدة المختبر — جهاز أو زجاجية أو مادة كيميائية أو أداة.

    **لا دورة اعتماد عليه، عن قصد.** الجرد سجلٌّ تشغيلي يعكس واقعاً ماديّاً:
    الميكروسكوب موجود أو مفقود، والكمية أربع أو ثلاث. وإخضاعُه لدورة «مسودة →
    مُرسل → معتمد» يعني أن نقصاً اكتشفه المحضّر اليوم لا يُصبح مقروءاً حتى
    يعتمده المدير — فيتأخّر أخطرُ ما في الجرد: أنّ شيئاً نقص.
    وحركة العهدة تُوثَّق في ``LabAssetHandover``، والتغييرات كلها في سجل
    الإجراءات — فالمساءلة محفوظة بلا بوابة موافقة.

    **الكمية والحالة حقلان لا حقل.** أربع قطع سليمة وأربع تالفة ليستا حالة
    واحدة، وضغطُهما في «العدد الصالح» يُخفي التالف بدل أن يلاحقه.
    """

    class Category(models.TextChoices):
        """نوع الصنف.

        قائمة في الكود لا جدول في القاعدة: تصنيفات محتويات المختبر مستقرّة بين
        المدارس، وجدولٌ لكل مدرسة يعني شاشة إدارة لقائمة لا تكاد تتغيّر — وهو
        القرار نفسه المتّخذ في ``Document.Kind``.
        """

        DEVICE = "device", "جهاز"
        GLASSWARE = "glassware", "زجاجيات"
        CHEMICAL = "chemical", "مادة كيميائية"
        TOOL = "tool", "أداة"
        MODEL = "model", "مجسّم أو نموذج"
        SAFETY = "safety", "معدّات سلامة"
        OTHER = "other", "أخرى"

    class Condition(models.TextChoices):
        GOOD = "good", "سليم"
        NEEDS_MAINTENANCE = "needs_maintenance", "يحتاج صيانة"
        DAMAGED = "damaged", "تالف"
        MISSING = "missing", "مفقود"
        CONSUMED = "consumed", "مستهلك"

    # ما يستوجب تنبيهاً في لوحة المحضّر ومدير المدرسة. تسميته هنا تجعل إضافة
    # حالة رابعة تعديلاً في سطر بدل شرطٍ مبثوث في الشاشات والخدمة.
    ATTENTION_CONDITIONS: tuple[str, ...] = (
        Condition.NEEDS_MAINTENANCE,
        Condition.DAMAGED,
        Condition.MISSING,
    )

    school = models.ForeignKey(
        School,
        on_delete=models.CASCADE,
        related_name="lab_assets",
        verbose_name="المدرسة",
        db_index=True,
    )
    department = models.ForeignKey(
        Department,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="lab_assets",
        verbose_name="المختبر / القسم",
        help_text="يفصل عهدة مختبر العلوم عن عهدة مختبر الحاسب الآلي.",
    )
    lab_kind = models.CharField(
        "المختبر",
        max_length=16,
        choices=LabKind.choices,
        blank=True,
        default="",
        db_index=True,
        help_text="مختبر العلوم أو مختبر الحاسب الآلي، مستقل عن أقسام التقارير.",
    )
    name = models.CharField("اسم الصنف", max_length=200)
    code = models.CharField(
        "رقم العهدة / الرقم التسلسلي",
        max_length=64,
        blank=True,
        default="",
        help_text="اختياري — يُستعمل في المطابقة مع كشف العهدة الرسمي.",
    )
    category = models.CharField(
        "النوع", max_length=16, choices=Category.choices, default=Category.DEVICE
    )
    quantity = models.PositiveIntegerField("الكمية", default=1)
    unit = models.CharField(
        "الوحدة",
        max_length=32,
        blank=True,
        default="",
        help_text="قطعة، عبوة، لتر… تُترك فارغة إن كان العدّ بالقطعة.",
    )
    condition = models.CharField(
        "الحالة", max_length=24, choices=Condition.choices, default=Condition.GOOD
    )
    location = models.CharField(
        "موقع الحفظ", max_length=120, blank=True, default="", help_text="الدولاب أو الرف."
    )
    custodian = models.ForeignKey(
        Teacher,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="lab_assets_in_custody",
        verbose_name="المسؤول عن العهدة",
    )
    recorded_by = models.ForeignKey(
        Teacher,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="lab_assets_recorded",
        verbose_name="من سجّله",
    )
    notes = models.TextField("ملاحظات", blank=True, default="")
    is_active = models.BooleanField("مُدرَج في الجرد", default=True)
    created_at = models.DateTimeField("أُضيف في", auto_now_add=True)
    updated_at = models.DateTimeField("آخر تحديث", auto_now=True)

    class Meta:
        ordering = ("name", "id")
        verbose_name = "صنف في عهدة المختبر"
        verbose_name_plural = "عهدة المختبر"
        indexes = [
            models.Index(fields=["school", "lab_kind", "condition"]),
            models.Index(fields=["school", "lab_kind", "category"]),
            models.Index(fields=["school", "department", "condition"]),
            models.Index(fields=["school", "department", "category"]),
        ]

    def __str__(self) -> str:
        return self.name

    @property
    def needs_attention(self) -> bool:
        return self.condition in self.ATTENTION_CONDITIONS

    @property
    def out_quantity(self) -> int:
        """الكمية الخارجة الآن — تُحسب من الحركة لا تُخزَّن.

        رقمٌ مخزَّن إلى جانب سجلّ الحركة مصدرُ حقيقةٍ ثانٍ: يفترقان عند أول
        حركة تُحذف أو تُعدَّل، فيقول الجرد إن قطعتين خارج المختبر ولا يذكر
        السجل إلا واحدة.
        """
        # قوائم المختبر تُحمِّل هذين المجموعين كـ annotations. استخدامهما هنا
        # يمنع استعلاماً إضافياً لكل صف، مع إبقاء fallback نفسه للكائنات التي
        # تأتي من نموذج أو خدمة بلا annotations.
        if "handed_out" in self.__dict__ or "handed_back" in self.__dict__:
            return max(
                0,
                int(self.__dict__.get("handed_out") or 0)
                - int(self.__dict__.get("handed_back") or 0),
            )

        totals = self.handovers.aggregate(
            out=models.Sum(
                "quantity",
                filter=models.Q(direction=LabAssetHandover.Direction.OUT),
            ),
            back=models.Sum(
                "quantity",
                filter=models.Q(direction=LabAssetHandover.Direction.IN),
            ),
        )
        return max(0, int(totals.get("out") or 0) - int(totals.get("back") or 0))

    @property
    def available_quantity(self) -> int:
        return max(0, int(self.quantity or 0) - self.out_quantity)

    def clean(self):
        super().clean()
        self.name = (self.name or "").strip()
        if not self.name:
            raise ValidationError({"name": "اسم الصنف مطلوب."})
        if (
            self.department_id
            and self.school_id
            and self.department.school_id != self.school_id
        ):
            raise ValidationError(
                {"department": "المختبر المختار لا يتبع المدرسة الحالية."}
            )


class LabAssetHandover(models.Model):
    """حركة عهدة: تسليمٌ لمنسوب أو إرجاعٌ إلى المختبر.

    **سجلّ حركة لا حالة.** البديل — حقلٌ على الصنف يقول «مع فلان» — يجيب سؤال
    «أين هو الآن؟» ويُسقط سؤال «من تسلّمه في الفصل الأول؟». والثاني هو ما يُسأل
    عند الجرد السنوي وعند فقد الصنف.

    والاتجاه صريح (``OUT``/``IN``) لا مشتق من وجود تاريخ إرجاع: صفٌّ واحد يحمل
    التسليم والإرجاع معاً يجعل تسليم قطعتين وإرجاع واحدة حالةً لا تُمثَّل.
    """

    class Direction(models.TextChoices):
        OUT = "out", "تسليم"
        IN = "in", "إرجاع"

    # المدرسة محفوظة هنا أيضاً وإن كانت تُشتق من الصنف: سجل الإجراءات ينسب كل
    # حدث إلى مدرسة، وحدثٌ بلا مدرسة لا يظهر في صفحة أي مدرسة — فهو سجل ضائع.
    school = models.ForeignKey(
        School,
        on_delete=models.CASCADE,
        related_name="lab_handovers",
        verbose_name="المدرسة",
        db_index=True,
    )
    asset = models.ForeignKey(
        LabAsset,
        on_delete=models.CASCADE,
        related_name="handovers",
        verbose_name="الصنف",
    )
    direction = models.CharField(
        "الحركة", max_length=8, choices=Direction.choices, default=Direction.OUT
    )
    person = models.ForeignKey(
        Teacher,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="lab_handovers_received",
        verbose_name="المستلم",
    )
    person_name = models.CharField(
        "اسم المستلم (لقطة)",
        max_length=150,
        blank=True,
        default="",
        help_text="تُلتقط لحظة الحركة فيبقى الكشف مقروءاً بعد حذف الحساب.",
    )
    quantity = models.PositiveIntegerField("الكمية", default=1)
    happened_at = models.DateTimeField("تاريخ الحركة", default=timezone.now)
    recorded_by = models.ForeignKey(
        Teacher,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="lab_handovers_recorded",
        verbose_name="من سجّلها",
    )
    note = models.CharField("ملاحظة", max_length=255, blank=True, default="")
    created_at = models.DateTimeField("أُنشئت في", auto_now_add=True)

    class Meta:
        ordering = ("-happened_at", "-id")
        verbose_name = "حركة عهدة"
        verbose_name_plural = "حركات العهدة"
        indexes = [
            models.Index(fields=["school", "-happened_at"]),
            models.Index(fields=["asset", "-happened_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.get_direction_display()} {self.asset_id} × {self.quantity}"

    def clean(self):
        super().clean()
        if int(self.quantity or 0) <= 0:
            raise ValidationError({"quantity": "الكمية يجب أن تكون واحداً على الأقل."})

        asset = getattr(self, "asset", None)
        if asset is None:
            return

        if self.direction == self.Direction.OUT:
            # لا يُسلَّم أكثر مما في المختبر. الفحص هنا لا في الشاشة وحدها:
            # الشاشة تُتجاوَز بطلب مُصاغ يدوياً، والجرد الذي يقول «خارج المختبر
            # خمس من أربع» جردٌ لا يُقرأ.
            available = asset.available_quantity
            if self.pk:
                available += int(
                    LabAssetHandover.objects.filter(pk=self.pk)
                    .values_list("quantity", flat=True)
                    .first()
                    or 0
                )
            if int(self.quantity) > available:
                raise ValidationError(
                    {"quantity": f"المتاح للتسليم {available} فقط من هذا الصنف."}
                )
        else:
            out_now = asset.out_quantity
            if self.pk:
                out_now += int(
                    LabAssetHandover.objects.filter(pk=self.pk)
                    .values_list("quantity", flat=True)
                    .first()
                    or 0
                )
            if int(self.quantity) > out_now:
                raise ValidationError(
                    {"quantity": f"لا يمكن إرجاع أكثر من الخارج فعلاً ({out_now})."}
                )

    def save(self, *args, **kwargs):
        # المدرسة تُشتق من الصنف: حركةٌ في مدرسة وصنفٌ في أخرى حالةٌ لا معنى لها.
        if self.asset_id and not self.school_id:
            self.school_id = self.asset.school_id
        if self.person_id and not self.person_name:
            try:
                self.person_name = (getattr(self.person, "name", "") or "")[:150]
            except Exception:
                pass
        return super().save(*args, **kwargs)


class LabExperiment(ApprovalMixin):
    """تجربة نُفِّذت في المختبر — عملٌ موثَّق يمرّ بالاعتماد.

    **تمرّ بدورة الاعتماد بخلاف العهدة.** الفرق ليس في الأهمية بل في الطبيعة:
    الجرد يوصف واقعاً، والتجربة **عملٌ أدّاه صاحبها** — ومدير المدرسة يعتمد
    عمل منسوبيه. وهي في هذا نظيرة تقرير المعلّم لا نظيرة سجل المخزون.

    **صاحبها ``recorder``، والاسم مقصود:** مكوّن الاعتماد يقرأ صاحب العمل
    بالترتيب ``assignee → recorder → manager → owner → teacher``، فتسميةُ الحقل
    ``recorder`` تجعل قاعدة «لا يعتمد أحد عمله» تحرسها بلا سطر إضافي — وحقلٌ
    باسم آخر كان سيتركها بلا صاحب معروف.

    **الشواهد بالربط لا بالرفع.** التجربة تُربط اختياراً بتقرير قائم، ومنه
    تُقرأ صورُها. ورفعُ صور مستقلة هنا يعني إدخال هذا الكيان في محاسبة مساحة
    المدرسة وتنظيف الملفات وحدود السعة — وهي أربعة أنظمة تُمسّ لأجل ما يوفّره
    الربط أصلاً.
    """

    school = models.ForeignKey(
        School,
        on_delete=models.CASCADE,
        related_name="lab_experiments",
        verbose_name="المدرسة",
        db_index=True,
    )
    department = models.ForeignKey(
        Department,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="lab_experiments",
        verbose_name="المختبر / القسم",
        help_text="يفصل تجارب مختبر العلوم عن تجارب مختبر الحاسب الآلي.",
    )
    lab_kind = models.CharField(
        "المختبر",
        max_length=16,
        choices=LabKind.choices,
        blank=True,
        default="",
        db_index=True,
        help_text="مختبر العلوم أو مختبر الحاسب الآلي، مستقل عن أقسام التقارير.",
    )
    recorder = models.ForeignKey(
        Teacher,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="lab_experiments_recorded",
        verbose_name="محضّر المختبر",
    )
    requested_by = models.ForeignKey(
        Teacher,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="lab_experiments_requested",
        verbose_name="المعلّم الطالب للتجربة",
    )
    # المسودة قد تُنشأ قبل اكتمال التفاصيل. الإرسال وحده هو الذي يفرض العنوان
    # والتاريخ والخطوات عبر ``assert_ready_for_submission`` أدناه.
    title = models.CharField("عنوان التجربة", max_length=200, blank=True, default="")
    experiment_date = models.DateField(
        "تاريخ التنفيذ", null=True, blank=True, db_index=True
    )
    subject = models.CharField("المادة", max_length=120, blank=True, default="")
    class_name = models.CharField(
        "الصف / الشعبة", max_length=120, blank=True, default=""
    )
    students_count = models.PositiveIntegerField("عدد الطلاب", default=0)
    objectives = models.TextField("أهداف التجربة", blank=True, default="")
    procedure = models.TextField("خطوات التنفيذ", blank=True, default="")
    materials_note = models.TextField(
        "المواد والأدوات المستخدمة",
        blank=True,
        default="",
        help_text="ما لا يُدرَج في العهدة يُكتب هنا نصاً.",
    )
    assets = models.ManyToManyField(
        LabAsset,
        blank=True,
        related_name="experiments",
        verbose_name="أصناف العهدة المستخدمة",
    )
    safety_notes = models.TextField(
        "إجراءات السلامة",
        blank=True,
        default="",
        help_text="ما اتُّخذ من احتياطات، وما وقع من ملاحظات سلامة إن وقع.",
    )
    report = models.ForeignKey(
        "Report",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="lab_experiments",
        verbose_name="التقرير المرتبط",
        help_text="اختياري — يُقرأ منه الشواهد والصور.",
    )
    created_at = models.DateTimeField("أُنشئت في", auto_now_add=True)
    updated_at = models.DateTimeField("آخر تحديث", auto_now=True)

    class Meta:
        ordering = ("-experiment_date", "-id")
        verbose_name = "تجربة مختبر"
        verbose_name_plural = "تجارب المختبر"
        indexes = [
            models.Index(fields=["school", "lab_kind", "-experiment_date"]),
            models.Index(fields=["school", "lab_kind", "approval_state"]),
            models.Index(fields=["school", "department", "-experiment_date"]),
            models.Index(fields=["school", "department", "approval_state"]),
        ]

    def __str__(self) -> str:
        return self.title or "مسودة تجربة بلا عنوان"

    def assert_ready_for_submission(self) -> None:
        """ما لا تُرسَل التجربة بدونه.

        ثلاثةٌ فقط: عنوانٌ وتاريخٌ وخطواتُ تنفيذ. وما دون ذلك — الأهداف والمادة
        والصف — يُترك اختياراً: تجربةٌ نُفِّذت ولم يُسجَّل صفُّها حالةٌ مشروعة،
        ومنعُ إرسالها يدفع صاحبها إلى إدخال وهمي ليُكمل النموذج.
        """
        if not (self.title or "").strip():
            raise ValidationError("اكتب عنوان التجربة قبل إرسالها.")
        if self.experiment_date is None:
            raise ValidationError("حدّد تاريخ تنفيذ التجربة.")
        if not (self.procedure or "").strip():
            raise ValidationError(
                "اكتب خطوات التنفيذ — التجربة بلا خطوات لا تُقرأ ولا تُكرَّر."
            )

    def clean(self):
        super().clean()
        if (
            self.department_id
            and self.school_id
            and self.department.school_id != self.school_id
        ):
            raise ValidationError(
                {"department": "المختبر المختار لا يتبع المدرسة الحالية."}
            )

    def can_review_approval(self, user, school):
        """من يراجع التجربة غير مدير المدرسة.

        من مُنح ``manage_lab`` في هذه المدرسة وفي نطاق المختبر نفسه. التفويض
        يمنح القدرة مؤقتاً لكنه لا يوسّع ``StaffScope`` أو يحوّله إلى وصول
        مدرسي شامل.
        """
        from ..capabilities import MANAGE_LAB
        from ..permissions import capability_source
        from ..services_lab import lab_resource_in_scope

        source = capability_source(user, MANAGE_LAB, school)
        if source is None:
            return False
        return lab_resource_in_scope(
            user,
            school,
            lab_kind=self.lab_kind,
            department_id=self.department_id,
        )

    def can_finalize_approval(self, user, school):
        """Break the manager-owned experiment deadlock without self-approval.

        A manager may legitimately record an experiment on behalf of the school,
        but the shared approval engine correctly prevents them from approving
        their own record.  In that one case, a *different* lab reviewer who
        has both laboratory management and approval authority may make the final
        decision.  For every other experiment ``None`` keeps the normal rule:
        final approval belongs to the school manager.
        """
        from ..capabilities import MANAGE_LAB, RECOMMEND_APPROVAL
        from ..permissions import (
            capability_source,
            is_school_manager,
        )
        from ..services_lab import lab_resource_in_scope

        recorder_id = getattr(self, "recorder_id", None)
        if not recorder_id or not is_school_manager(
            self.recorder, active_school=school
        ):
            return None
        if recorder_id == getattr(user, "pk", None):
            return False
        manage_source = capability_source(user, MANAGE_LAB, school)
        approval_source = capability_source(user, RECOMMEND_APPROVAL, school)
        if manage_source is None or approval_source is None:
            return False
        return lab_resource_in_scope(
            user,
            school,
            lab_kind=self.lab_kind,
            department_id=self.department_id,
        )
