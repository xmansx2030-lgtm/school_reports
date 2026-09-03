"""سجل نداءات الذكاء الاصطناعي — واقعةٌ لكل نداء، ناجحاً كان أو فاشلاً.

**لماذا جدول وليس سطر لوق؟** ``ai_client.log_usage`` كان يكتب الأرقام إلى اللوق
وحده، وهو مكانٌ يصلح للقراءة عند التحقيق في عطل، ولا يصلح للإجابة عن سؤال:

- كم أنفقت هذه المدرسة هذا الشهر؟
- ما نسبة إصابة المخزَّن فعلاً في الإنتاج — لا في الاختبار؟
- كم مرة تُستدعى إعادة صياغة منصور؟ (وهي نداءٌ ثانٍ كامل، أي ضعف الكلفة.)
- هل يُبتر الرد كثيراً بحيث يستحق ``max_output_tokens`` رفعاً؟

ثلاثتها تحتاج تجميعاً عبر الزمن وربطاً بمدرسة، وكلاهما لا يُفعل على نصٍّ حرّ.

**الكلفة تُجمَّد وقت الاستهلاك.** السعر يتغيّر، فحسابُ كلفةِ شهرٍ ماضٍ بسعر
اليوم يعيد كتابة التاريخ. ولهذا تُخزَّن ``estimated_cost`` محسوبةً لحظتها، وتبقى
الرموز هي الحقيقة التي لا تتغيّر ويمكن إعادة الحساب منها عند الحاجة.

**ولا سعر مخترَع.** جدول الأسعار يأتي من الإعداد، وإن كان فارغاً بقيت الكلفة
``NULL`` — وهو أصدق من رقمٍ مبنيّ على سعرٍ مفترض.

**لا يحمل هذا السجل نصّ المستخدم ولا نصّ الرد.** أرقامٌ ووسوم فقط: ما يلزم
لإدارة الكلفة، لا نسخةٌ ثانية من محتوى التقارير.
"""

from __future__ import annotations

from .base import *
from .schools import School


__all__ = ["AiUsageEvent"]


class AiUsageEvent(models.Model):
    """قياسُ نداءٍ واحد إلى مزوّد الذكاء الاصطناعي."""

    class Stage(models.TextChoices):
        MANSOUR = "mansour", "منصور"
        MANSOUR_REWRITE = "mansour-rewrite", "منصور — إعادة صياغة"
        REPORT_IMPROVE = "report-improve", "تحسين الصياغة"
        REPORT_REVIEW = "report-review", "فحص الجاهزية"
        TRANSCRIPTION = "transcription", "تفريغ صوتي"
        VOICE_POLISH = "voice-polish", "تجميل التفريغ"
        OTHER = "other", "غير ذلك"

    class Outcome(models.TextChoices):
        SUCCESS = "success", "نجح"
        # المخرَج وصل ناقصاً عند سقف الرموز. ليس عطلاً في الشبكة، ويُدفع ثمنه
        # كاملاً، ولذلك يستحق تمييزاً عن الفشل: ارتفاعه يعني رفع السقف.
        TRUNCATED = "truncated", "مبتور"
        FAILED = "failed", "فشل"

    created_at = models.DateTimeField("وقت النداء", auto_now_add=True, db_index=True)
    stage = models.CharField("المرحلة", max_length=32, choices=Stage.choices, db_index=True)
    model_name = models.CharField("النموذج", max_length=64, blank=True, default="")
    outcome = models.CharField(
        "النتيجة",
        max_length=16,
        choices=Outcome.choices,
        default=Outcome.SUCCESS,
        db_index=True,
    )
    error_kind = models.CharField(
        "نوع العطل",
        max_length=64,
        blank=True,
        default="",
        help_text="اسم الاستثناء أو رمز HTTP. لا يحمل نص الرسالة.",
    )

    school = models.ForeignKey(
        School,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ai_usage_events",
        verbose_name="المدرسة",
    )
    teacher = models.ForeignKey(
        "Teacher",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ai_usage_events",
        verbose_name="المستخدم",
    )

    input_tokens = models.PositiveIntegerField("رموز الإدخال", default=0)
    cached_input_tokens = models.PositiveIntegerField(
        "المقروء من المخزَّن",
        default=0,
        help_text="الدليل الوحيد على أن تخزين البادئة يعمل في الإنتاج.",
    )
    output_tokens = models.PositiveIntegerField("رموز الإخراج", default=0)
    reasoning_tokens = models.PositiveIntegerField(
        "رموز التفكير",
        default=0,
        help_text="محسوبة ضمن الإخراج، وتُفرد لأنها تلتهم سقف الرموز وتسبّب البتر.",
    )

    duration_ms = models.PositiveIntegerField("زمن النداء (مللي ثانية)", default=0)
    estimated_cost = models.DecimalField(
        "الكلفة التقديرية (دولار)",
        max_digits=12,
        decimal_places=6,
        null=True,
        blank=True,
        help_text="محسوبة بسعر لحظة الاستهلاك. تبقى فارغة إن لم يُضبط سعر للنموذج.",
    )

    class Meta:
        verbose_name = "استهلاك ذكاء اصطناعي"
        verbose_name_plural = "استهلاك الذكاء الاصطناعي"
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["stage", "created_at"]),
            models.Index(fields=["school", "created_at"]),
            models.Index(fields=["outcome", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.stage} · {self.model_name} · {self.created_at:%Y-%m-%d %H:%M}"

    @property
    def billable_input_tokens(self) -> int:
        """الإدخال الذي دُفع بسعره الكامل — أي ما لم يُقرأ من المخزَّن."""
        return max(0, int(self.input_tokens) - int(self.cached_input_tokens))

    @property
    def cache_hit_ratio(self) -> float:
        total = int(self.input_tokens)
        if total <= 0:
            return 0.0
        return round(int(self.cached_input_tokens) / total, 4)
