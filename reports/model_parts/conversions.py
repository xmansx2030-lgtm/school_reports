from __future__ import annotations

from .base import *
from .billing import SchoolSubscription
from .notifications import Notification
from .schools import School, Teacher


__all__ = ["SchoolConversionOutreach"]


class SchoolConversionOutreach(models.Model):
    """مراجعة بشرية قابلة للتدقيق قبل التواصل التجاري مع مدرسة.

    الأرقام التي بُنيت عليها الرسالة محفوظة لقطةً وقت الإنشاء. الذكاء
    الاصطناعي يقترح الصياغة فقط؛ الاعتماد والإرسال يظلان إجراءين صريحين من
    مالك المنصة، وحالة كل قناة تمنع إعادة إرسال قناة نجحت عند إعادة المحاولة.
    """

    class Objective(models.TextChoices):
        ACTIVATE = "activate", "إكمال أول استفادة"
        CONVERT = "convert", "التحويل إلى اشتراك مدفوع"
        RENEW = "renew", "تجديد اشتراك مدفوع"
        RECOVER = "recover", "استعادة مدرسة منتهية"

    class Tone(models.TextChoices):
        EXECUTIVE = "executive", "تنفيذي وواثق"
        SUPPORTIVE = "supportive", "مساند وودود"
        CONCISE = "concise", "مختصر ومباشر"

    class Status(models.TextChoices):
        DRAFT = "draft", "مسودة بانتظار المراجعة"
        SENDING = "sending", "جارٍ الإرسال"
        SENT = "sent", "أُرسلت"
        PARTIAL = "partial", "أُرسلت جزئيًا"
        FAILED = "failed", "تعذر الإرسال"

    class ChannelStatus(models.TextChoices):
        NOT_REQUESTED = "not_requested", "غير مطلوبة"
        PENDING = "pending", "بانتظار الإرسال"
        SENT = "sent", "أُرسلت"
        FAILED = "failed", "فشلت"

    school = models.ForeignKey(
        School,
        on_delete=models.CASCADE,
        related_name="conversion_outreaches",
        verbose_name="المدرسة",
    )
    subscription = models.ForeignKey(
        SchoolSubscription,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="conversion_outreaches",
        verbose_name="لقطة الاشتراك",
    )
    created_by = models.ForeignKey(
        Teacher,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="conversion_outreaches_created",
        verbose_name="أنشأها",
    )
    approved_by = models.ForeignKey(
        Teacher,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="conversion_outreaches_approved",
        verbose_name="اعتمدها",
    )

    objective = models.CharField(
        "الهدف",
        max_length=16,
        choices=Objective.choices,
        default=Objective.CONVERT,
    )
    tone = models.CharField(
        "نبرة الرسالة",
        max_length=16,
        choices=Tone.choices,
        default=Tone.EXECUTIVE,
    )
    status = models.CharField(
        "الحالة",
        max_length=16,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
    )

    context_snapshot = models.JSONField("لقطة المؤشرات", default=dict)
    analysis_summary = models.TextField("تحليل الحالة", blank=True, default="")
    recommended_action = models.TextField("الإجراء المقترح", blank=True, default="")
    email_subject = models.CharField("عنوان البريد", max_length=160, blank=True, default="")
    email_body = models.TextField("نص البريد", blank=True, default="")
    in_app_title = models.CharField("عنوان الإشعار", max_length=120, blank=True, default="")
    in_app_body = models.TextField("نص الإشعار", blank=True, default="")
    whatsapp_body = models.TextField("نص واتساب المقترح", blank=True, default="")
    call_script = models.JSONField("محاور المكالمة", default=list, blank=True)

    ai_generated = models.BooleanField("مولدة بالذكاء الاصطناعي", default=False)
    ai_model = models.CharField("نموذج الذكاء الاصطناعي", max_length=64, blank=True, default="")
    requested_channels = models.JSONField("القنوات المطلوبة", default=list, blank=True)
    email_status = models.CharField(
        "حالة البريد",
        max_length=16,
        choices=ChannelStatus.choices,
        default=ChannelStatus.NOT_REQUESTED,
    )
    in_app_status = models.CharField(
        "حالة الإشعار الداخلي",
        max_length=16,
        choices=ChannelStatus.choices,
        default=ChannelStatus.NOT_REQUESTED,
    )
    platform_email_ids = models.JSONField("رسائل البريد الناتجة", default=list, blank=True)
    notification = models.ForeignKey(
        Notification,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="conversion_outreaches",
        verbose_name="الإشعار الناتج",
    )
    delivery_result = models.JSONField("نتيجة الإرسال", default=dict, blank=True)
    failure_reason = models.TextField("سبب التعذر", blank=True, default="")

    approved_at = models.DateTimeField("اعتمدت في", null=True, blank=True)
    sent_at = models.DateTimeField("أرسلت في", null=True, blank=True)
    created_at = models.DateTimeField("أنشئت في", auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField("حدثت في", auto_now=True)

    class Meta:
        ordering = ("-created_at", "-id")
        verbose_name = "تواصل تحويل مدرسة"
        verbose_name_plural = "تواصلات تحويل المدارس"
        indexes = [
            models.Index(fields=("school", "-created_at"), name="rpt_conv_school_recent"),
            models.Index(fields=("status", "-created_at"), name="rpt_conv_status_recent"),
        ]

    def __str__(self) -> str:
        return f"{self.school} · {self.get_objective_display()} · {self.get_status_display()}"
