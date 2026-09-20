# -*- coding: utf-8 -*-
"""نموذج تعميم المدير التنفيذي على مدارس مجموعته."""
from __future__ import annotations

from django import forms
from django.utils import timezone
from .form_widgets import DateTimeLocalInput

from .models import School
from .validators import validate_circular_attachment_file


class GroupNotificationForm(forms.Form):
    """يُبنى دائماً بمجموعة المدارس المسموحة، فلا يقبل الحقل غيرها.

    تقييد ``queryset`` عند البناء هو خط الدفاع الأول: قيمة مدسوسة في الطلب
    تسقط في التحقق قبل أن تصل إلى أي منطق. وفحص الصلاحية في العرض يبقى
    خط الدفاع الثاني.
    """

    schools = forms.ModelMultipleChoiceField(
        queryset=School.objects.none(),
        label="المدارس المستهدفة",
        error_messages={
            "required": "اختر مدرسة واحدة على الأقل.",
            "invalid_choice": "إحدى المدارس المختارة ليست ضمن مجموعتك.",
        },
    )
    audience = forms.ChoiceField(
        label="المستقبلون",
        choices=(),
        initial="managers",
    )
    title = forms.CharField(max_length=120, required=False, label="العنوان (اختياري)")
    message = forms.CharField(widget=forms.Textarea(attrs={"rows": 6}), label="نص التعميم")
    is_important = forms.BooleanField(required=False, initial=False, label="مهم")

    requires_signature = forms.BooleanField(
        required=False,
        initial=False,
        label="يتطلب توقيعاً إلزامياً",
        help_text="سيُطلب من المستلم الإقرار ورسم توقيعه قبل الاعتماد.",
    )
    signature_deadline_at = forms.DateTimeField(
        required=False,
        label="آخر موعد للتوقيع (اختياري)",
        widget=DateTimeLocalInput(),
    )
    signature_ack_text = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
        label="نص الإقرار",
    )
    attachment = forms.FileField(
        required=False,
        label="مرفق (اختياري)",
        help_text="PDF أو صورة، بحد أقصى 5MB. يُحتسب على تخزين كل مدرسة مستهدفة.",
        validators=[validate_circular_attachment_file],
        widget=forms.ClearableFileInput(attrs={"accept": ".pdf,.jpg,.jpeg,.png"}),
    )

    def __init__(self, *args, allowed_schools=None, **kwargs):
        super().__init__(*args, **kwargs)
        from .models import GroupNotificationBatch

        self.fields["audience"].choices = GroupNotificationBatch.Audience.choices
        if allowed_schools is not None:
            self.fields["schools"].queryset = allowed_schools

    def clean_signature_deadline_at(self):
        deadline = self.cleaned_data.get("signature_deadline_at")
        if deadline and deadline <= timezone.now():
            raise forms.ValidationError("آخر موعد للتوقيع يجب أن يكون في المستقبل.")
        return deadline

    def clean(self):
        cleaned = super().clean()
        # المهلة والإقرار لا معنى لهما بلا توقيع، فتُطرحان بدل تخزين قيم مضلّلة.
        if not cleaned.get("requires_signature"):
            cleaned["signature_deadline_at"] = None
            cleaned["signature_ack_text"] = ""
        return cleaned
