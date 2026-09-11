from __future__ import annotations

from .approvals import ApprovalMixin, ApprovalState
from .base import *
from .schools import Department, School, SchoolGroup, Teacher

__all__ = ["Assignment", "AssignmentTarget", "AssignmentEvidence"]


def _assignment_evidence_upload_to(instance, filename: str) -> str:
    target = getattr(instance, "target", None)
    assignment_id = getattr(target, "assignment_id", None) or "unknown"
    school = getattr(target, "school", None) or getattr(getattr(target, "assignment", None), "school", None)
    return school_file_path(
        school,
        "assignments/evidence",
        filename,
        parts=(f"assignment-{assignment_id}",),
        fallback="evidence",
    )


class Assignment(models.Model):
    """تكليف — المطلوب والموعد والشواهد.

    **مستويان بنموذج واحد.** المدير التنفيذي يكلّف مديري مدارس مجموعته، ومدير
    المدرسة يكلّف منسوبيه. الفرق بينهما ``scope`` لا نموذجان: التكليف هو التكليف،
    وازدواج النماذج يعني ازدواج الشاشات وسير الاعتماد وتقارير التأخر — ثم
    تباعدها عند أول تعديل.

    **الموعد إلزامي.** قبل هذا النموذج لم يكن في المنصة كلها حقل استحقاق واحد،
    فكان بند «متابعة الأعمال والتقارير المتأخرة» بلا أي مقابل. وتكليفٌ بلا
    موعد لا يتأخر أبداً، فلا يُتابَع أبداً.

    **التكليف يُلغى ولا يُحذف.** ما وصل المكلَّف صار واقعة؛ ومحوه يجعل من نُفّذ
    عليه إجراءٌ بلا سبب ظاهر.
    """

    class Scope(models.TextChoices):
        SCHOOL = "school", "داخل المدرسة"
        GROUP = "group", "على مستوى المجموعة"

    class Source(models.TextChoices):
        MANUAL = "manual", "تكليف مباشر"
        DECISION = "decision", "منبثق عن قرار"
        PLAN = "plan", "مهمة من خطة"

    class Priority(models.TextChoices):
        NORMAL = "normal", "اعتيادي"
        HIGH = "high", "عاجل"

    scope = models.CharField(
        "نطاق التكليف",
        max_length=16,
        choices=Scope.choices,
        default=Scope.SCHOOL,
        db_index=True,
    )
    school = models.ForeignKey(
        School,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="assignments",
        verbose_name="المدرسة",
        help_text="للتكليف داخل مدرسة واحدة.",
    )
    group = models.ForeignKey(
        SchoolGroup,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="assignments",
        verbose_name="المجموعة",
        help_text="لتكليف المدير التنفيذي على مدارس مجموعته.",
    )
    issuer = models.ForeignKey(
        Teacher,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assignments_issued",
        verbose_name="المكلِّف",
    )
    issuer_name = models.CharField(
        "اسم المكلِّف (وقت الإصدار)", max_length=150, blank=True, default=""
    )

    title = models.CharField("عنوان التكليف", max_length=200)
    description = models.TextField("المطلوب", blank=True, default="")
    source = models.CharField(
        "مصدر التكليف",
        max_length=16,
        choices=Source.choices,
        default=Source.MANUAL,
    )
    priority = models.CharField(
        "الأولوية",
        max_length=8,
        choices=Priority.choices,
        default=Priority.NORMAL,
    )

    due_at = models.DateTimeField(
        "موعد التسليم",
        db_index=True,
        help_text="تكليفٌ بلا موعد لا يتأخر أبداً، فلا يُتابَع أبداً.",
    )
    requires_evidence = models.BooleanField(
        "يتطلب شواهد؟",
        default=False,
        help_text="عند التفعيل لا يُقبل إرسال التكليف للمراجعة بلا مرفقات.",
    )
    min_evidence_count = models.PositiveSmallIntegerField(
        "أقل عدد شواهد", default=1
    )

    department = models.ForeignKey(
        Department,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assignments",
        verbose_name="القسم المعني",
        help_text="يُستعمل لتحديد من يراجع التنفيذ ضمن نطاقه.",
    )

    cancelled_at = models.DateTimeField("أُلغي في", null=True, blank=True)
    cancelled_by = models.ForeignKey(
        Teacher,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assignments_cancelled",
        verbose_name="ألغاه",
    )
    cancel_reason = models.CharField("سبب الإلغاء", max_length=255, blank=True, default="")

    created_at = models.DateTimeField("أُصدر في", auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(fields=["school", "-created_at"]),
            models.Index(fields=["group", "-created_at"]),
            models.Index(fields=["issuer", "-created_at"]),
            models.Index(fields=["due_at"]),
        ]
        verbose_name = "تكليف"
        verbose_name_plural = "التكليفات"

    def __str__(self) -> str:
        return self.title

    # ------------------------------------------------------------------
    @property
    def is_cancelled(self) -> bool:
        return self.cancelled_at is not None

    @property
    def target_count(self) -> int:
        return self.targets.count()

    @property
    def completed_count(self) -> int:
        return self.targets.filter(approval_state=ApprovalState.APPROVED).count()

    @property
    def completion_percent(self) -> int:
        """نسبة الإنجاز على مستوى التكليف = المكلَّفون المعتمَدون ÷ الجميع.

        محسوبة لا مخزَّنة: رقمٌ مخزَّن يفترق عن الحقيقة أول ما يُحذف مكلَّف أو
        يُعاد عمله.
        """
        total = self.target_count
        if not total:
            return 0
        return round(self.completed_count * 100 / total)

    def clean(self):
        super().clean()
        if self.scope == self.Scope.SCHOOL and self.school_id is None:
            raise ValidationError({"school": "التكليف داخل المدرسة يحتاج مدرسة."})
        if self.scope == self.Scope.GROUP and self.group_id is None:
            raise ValidationError({"group": "تكليف المجموعة يحتاج مجموعة."})
        if self.requires_evidence and int(self.min_evidence_count or 0) < 1:
            raise ValidationError(
                {"min_evidence_count": "اشتراط الشواهد يعني شاهداً واحداً على الأقل."}
            )

    def save(self, *args, **kwargs):
        if self.issuer_id and not self.issuer_name:
            try:
                self.issuer_name = (getattr(self.issuer, "name", "") or "")[:150]
            except Exception:
                pass
        return super().save(*args, **kwargs)

    def cancel(self, *, by=None, reason: str = "") -> None:
        if self.cancelled_at is not None:
            return
        self.cancelled_at = timezone.now()
        self.cancelled_by = by
        self.cancel_reason = (reason or "")[:255]
        self.save(update_fields=["cancelled_at", "cancelled_by", "cancel_reason"])


class AssignmentTarget(ApprovalMixin):
    """حصة مكلَّف واحد من التكليف.

    **الحالة على المكلَّف لا على التكليف.** تكليفٌ واحد يوزَّع على عشرة، فيُنجزه
    ثلاثة ويتأخر سبعة — وحالةٌ واحدة على الأب تخفي ذلك كله. ولذلك يرث هذا
    النموذج ``ApprovalMixin`` ولا يرثه ``Assignment``.

    ولأنه يرثه، فإن سير الاعتماد وسجل الانتقالات وصندوق المراجعة تعمل عليه
    بلا سطر جديد — وهذا هو العائد الفعلي من بناء المكوّن قبله.
    """

    assignment = models.ForeignKey(
        Assignment,
        on_delete=models.CASCADE,
        related_name="targets",
        verbose_name="التكليف",
    )
    assignee = models.ForeignKey(
        Teacher,
        on_delete=models.CASCADE,
        related_name="assignment_targets",
        verbose_name="المكلَّف",
        db_index=True,
    )
    school = models.ForeignKey(
        School,
        on_delete=models.CASCADE,
        related_name="assignment_targets",
        verbose_name="المدرسة",
        null=True,
        blank=True,
    )

    accepted_at = models.DateTimeField("قُبل في", null=True, blank=True)
    clarification_note = models.TextField(
        "طلب توضيح", blank=True, default="",
        help_text="يكتبه المكلَّف حين يحتاج بيان المطلوب قبل الشروع.",
    )
    progress_percent = models.PositiveSmallIntegerField(
        "نسبة الإنجاز",
        default=0,
        validators=[MinValueValidator(0)],
    )
    progress_note = models.TextField("ملاحظات التنفيذ", blank=True, default="")

    created_at = models.DateTimeField("أُسند في", auto_now_add=True)

    class Meta:
        ordering = ("assignment_id", "id")
        constraints = [
            models.UniqueConstraint(
                fields=["assignment", "assignee"],
                name="uniq_assignment_target_per_assignee",
            )
        ]
        indexes = [
            models.Index(fields=["assignee", "approval_state"]),
            models.Index(fields=["school", "approval_state"]),
        ]
        verbose_name = "مكلَّف"
        verbose_name_plural = "المكلَّفون"

    def __str__(self) -> str:
        return f"{self.assignee} · {self.assignment}"

    # ------------------------------------------------------------------
    @property
    def due_at(self):
        return getattr(self.assignment, "due_at", None)

    @property
    def approval_block_reason(self) -> str:
        """سبب قفل دورة الاعتماد، إن كان السجل أرشيفياً لا يقبل انتقالاً."""
        if getattr(self.assignment, "is_cancelled", False):
            return "هذا التكليف ملغى ومحفوظ للسجل فقط."
        return ""

    @property
    def is_overdue(self) -> bool:
        """متأخر: فات موعده ولم يُعتمد بعد.

        المعتمد لا يتأخر مهما فات موعده — وقد سُلّم فعلاً. وحصر التأخر في غير
        المعتمد هو ما يجعل «قائمة المتأخرات» قابلة للتصرف بدل أن تكون تأنيباً
        على ما انتهى.
        """
        due = self.due_at
        if due is None or self.approval_state == ApprovalState.APPROVED:
            return False
        if getattr(self.assignment, "is_cancelled", False):
            return False
        return timezone.now() > due

    @property
    def days_remaining(self) -> int | None:
        due = self.due_at
        if due is None:
            return None
        return (due - timezone.now()).days

    @property
    def evidence_count(self) -> int:
        return self.evidence.count()

    def evidence_shortfall(self) -> int:
        """كم شاهداً ينقص لاستيفاء شرط التكليف. صفر يعني مستوفياً."""
        if not getattr(self.assignment, "requires_evidence", False):
            return 0
        needed = int(getattr(self.assignment, "min_evidence_count", 1) or 1)
        return max(0, needed - self.evidence_count)

    def assert_ready_for_submission(self) -> None:
        """يفحصه مكوّن الاعتماد قبل الإرسال — فلا يمر تكليف ناقص الشواهد."""
        from ..services_assignments import ensure_submittable

        ensure_submittable(self)

    def _is_issuer(self, user) -> bool:
        return (
            getattr(self.assignment, "issuer_id", None) == getattr(user, "pk", None)
            and user is not None
        )

    def can_review_approval(self, user, school):
        """من يراجع تنفيذ التكليف.

        **المكلِّف أولاً، وبلا اشتراط صلاحية.** من أصدر التكليف أعلمُ بما طلب،
        ومراجعتُه لما كلّف به ليست منحةً تُمنح له بل هي التكليف نفسه. واشتراط
        صلاحية مدرسية هنا كان يحجب المدير التنفيذي عن متابعة ما أصدره — فهو لا
        يملك عضوية في أي مدرسة أصلاً، وذلك مقصود في تنظيمه.

        ومن سواه يحتاج صلاحية المراجعة **وأن يكون قسم التكليف في نطاقه**.
        و``None`` لا تُرجَع هنا لأن التكليف يحدّد مراجعه دائماً.
        """
        if self._is_issuer(user):
            return True

        from ..capabilities import REVIEW_REPORTS
        from ..permissions import capability_source, supervised_department_ids

        if capability_source(user, REVIEW_REPORTS, school) is None:
            return False

        department_id = getattr(self.assignment, "department_id", None)
        if department_id is None:
            return False
        return department_id in supervised_department_ids(user, school)

    def can_finalize_approval(self, user, school):
        """من يعتمد تنفيذ التكليف نهائياً.

        المكلِّف — فاعتماد الإنجاز إقرارٌ بأن ما طُلب قد سُلّم، ولا يقرّ بذلك
        إلا من طلبه. وهذا ما يتيح للمدير التنفيذي اعتماد ردود مدارسه دون أن
        يملك أي صلاحية داخلها، ولمدير المدرسة اعتماد تنفيذ منسوبيه.

        قاعدة «لا يعتمد أحد عمله» تبقى فوق هذا كله وتُفحَص في الخدمة.
        """
        if self._is_issuer(user):
            return True
        return None


class AssignmentEvidence(models.Model):
    """شاهد على تنفيذ التكليف.

    مرتبط بالمكلَّف لا بالتكليف: عشرة منفّذين يرفعون عشرة شواهد مختلفة، وربطها
    بالأب يخلطها فلا يُعرف شاهدُ من.
    """

    target = models.ForeignKey(
        AssignmentTarget,
        on_delete=models.CASCADE,
        related_name="evidence",
        verbose_name="المكلَّف",
    )
    file = models.FileField(
        "الشاهد",
        upload_to=_assignment_evidence_upload_to,
        storage=PublicRawMediaStorage(),
        validators=[
            FileExtensionValidator(["pdf", "jpg", "jpeg", "png", "doc", "docx", "xlsx"]),
            validate_attachment_file,
        ],
        help_text="PDF أو صورة أو مستند حتى 5MB.",
    )
    note = models.CharField("وصف الشاهد", max_length=255, blank=True, default="")
    uploaded_by = models.ForeignKey(
        Teacher,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assignment_evidence_uploaded",
        verbose_name="رفعه",
    )
    storage_bytes = models.PositiveBigIntegerField(default=0, editable=False)
    created_at = models.DateTimeField("رُفع في", auto_now_add=True)

    class Meta:
        ordering = ("-created_at", "-id")
        verbose_name = "شاهد تكليف"
        verbose_name_plural = "شواهد التكليفات"

    def __str__(self) -> str:
        return self.note or (getattr(self.file, "name", "") or "شاهد")

    def save(self, *args, **kwargs):
        try:
            self.storage_bytes = int(getattr(self.file, "size", 0) or 0)
        except Exception:
            self.storage_bytes = 0
        return super().save(*args, **kwargs)
