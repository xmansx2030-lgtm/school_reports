from __future__ import annotations

from .approvals import ApprovalMixin
from .base import *
from .schools import Department, School, Teacher

__all__ = ["CircularDraft"]


class CircularDraft(ApprovalMixin):
    """مسودة تعميم — مقترحٌ لتعميم لا تعميمٌ بعد.

    **لماذا نموذج مستقل ولم يُركَّب المكوّن على ``Notification`` نفسه؟** لأن
    المسودة ليست تعميماً ناقصاً بل شيء آخر: التعميم واقعةٌ وصلت مستلميها ولها
    تواقيعهم، والمسودة ورقةٌ تُتداول قبل أن تصل أحداً. وخلطهما في جدول واحد
    كان يعني أن كل استعلام على التعاميم — وهي في عشرات المواضع — يجب أن يستثني
    المسودات، وأن نسيان استثناءٍ واحد يُظهر للمعلمين تعميماً لم يُعتمد بعد.

    والفصل يجعل مسار التعميم المُختبَر يبقى كما هو حرفياً: النشر ينشئ
    ``Notification`` عادياً بمستلميه، فيسري عليه كل ما بُني له من توقيع ومهلة
    وتقارير اطّلاع بلا تعديل سطر.

    **من يُعدّها ومن ينشرها**: يُعدّها الموظف الإداري أو الوكيل بصلاحية
    ``draft_circulars``، ويعتمدها مدير المدرسة — واعتمادُه هو نشرُها.
    """

    class Audience(models.TextChoices):
        ALL = "all", "جميع المنسوبين"
        DEPARTMENT = "department", "قسم بعينه"

    school = models.ForeignKey(
        School, on_delete=models.CASCADE, related_name="circular_drafts",
        verbose_name="المدرسة", db_index=True,
    )
    owner = models.ForeignKey(
        Teacher, on_delete=models.CASCADE, related_name="circular_drafts",
        verbose_name="مُعِدّ المسودة", db_index=True,
    )
    owner_name = models.CharField(
        "اسم المُعِدّ (وقت الإعداد)", max_length=150, blank=True, default=""
    )

    title = models.CharField("عنوان التعميم", max_length=120)
    body = models.TextField("نص التعميم")
    audience = models.CharField(
        "الفئة المستهدفة", max_length=16, choices=Audience.choices, default=Audience.ALL
    )
    department = models.ForeignKey(
        Department, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="circular_drafts", verbose_name="القسم المستهدف",
    )
    requires_signature = models.BooleanField(
        "يتطلب توقيعاً؟", default=True,
        help_text="عند التفعيل يُطلب من المستلم الإقرار وإدخال جواله قبل اعتماد توقيعه.",
    )
    signature_deadline_at = models.DateTimeField(
        "آخر موعد للتوقيع", null=True, blank=True
    )

    published_notification = models.OneToOneField(
        "Notification", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="source_draft", verbose_name="التعميم المنشور",
    )
    published_at = models.DateTimeField("نُشر في", null=True, blank=True)

    created_at = models.DateTimeField("أُنشئت في", auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField("آخر تعديل", auto_now=True)

    class Meta:
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(fields=["school", "-created_at"]),
            models.Index(fields=["school", "approval_state"]),
            models.Index(fields=["owner", "-created_at"]),
        ]
        verbose_name = "مسودة تعميم"
        verbose_name_plural = "مسودات التعاميم"

    def __str__(self) -> str:
        return self.title

    # ------------------------------------------------------------------
    @property
    def is_published(self) -> bool:
        return self.published_notification_id is not None

    def assert_ready_for_submission(self) -> None:
        if not (self.body or "").strip():
            raise ValidationError("لا تُرسَل مسودة تعميم بلا نص.")
        if self.audience == self.Audience.DEPARTMENT and self.department_id is None:
            raise ValidationError("اخترتَ قسماً مستهدفاً ولم تحدّده.")

    def can_review_approval(self, user, school):
        """اعتماد المسودة — ونشرُها — بيد مدير المدرسة وحده.

        الوكيل يُعدّ المسودة ولا ينشرها: التوصيف ينصّ صراحةً على أنه «لا ينشر
        تعميماً رسمياً دون اعتماد المدير». وإرجاع ``False`` لغير المدير هنا
        قاطعٌ لا يترك للنطاق مدخلاً.
        """
        return False

    def allows_issuance(self, user, school) -> bool:
        """مدير المدرسة يُصدر مسودته مباشرةً — لا مراجع فوقه فيها.

        ومن سواه يُرسلها إليه، فالتوصيف ينصّ على أنه «لا ينشر تعميماً رسمياً
        دون اعتماد المدير».
        """
        if self.owner_id != getattr(user, "pk", None):
            return False
        from ..permissions import is_school_manager

        return is_school_manager(user, active_school=self.school)

    def clean(self):
        super().clean()
        if self.audience == self.Audience.DEPARTMENT and self.department_id is None:
            raise ValidationError({"department": "حدّد القسم المستهدف."})

    def save(self, *args, **kwargs):
        if self.owner_id and not self.owner_name:
            try:
                self.owner_name = (getattr(self.owner, "name", "") or "")[:150]
            except Exception:
                pass
        return super().save(*args, **kwargs)
