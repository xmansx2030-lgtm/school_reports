from __future__ import annotations

from django import forms

from .models import SchoolConversionOutreach


class ConversionDraftForm(forms.Form):
    objective = forms.ChoiceField(
        label="هدف التواصل",
        choices=SchoolConversionOutreach.Objective.choices,
    )
    tone = forms.ChoiceField(
        label="نبرة الرسالة",
        choices=SchoolConversionOutreach.Tone.choices,
        initial=SchoolConversionOutreach.Tone.EXECUTIVE,
    )


class ConversionSendForm(forms.ModelForm):
    send_email = forms.BooleanField(label="إرسال بريد رسمي", required=False, initial=True)
    send_in_app = forms.BooleanField(label="إرسال إشعار داخل توثيق", required=False, initial=True)
    confirm_reviewed = forms.BooleanField(
        label="راجعت الأرقام والنص وأعتمد الإرسال",
        required=True,
    )

    class Meta:
        model = SchoolConversionOutreach
        fields = (
            "email_subject",
            "email_body",
            "in_app_title",
            "in_app_body",
            "whatsapp_body",
        )
        widgets = {
            "email_subject": forms.TextInput(attrs={"maxlength": 160}),
            "email_body": forms.Textarea(attrs={"rows": 9}),
            "in_app_title": forms.TextInput(attrs={"maxlength": 120}),
            "in_app_body": forms.Textarea(attrs={"rows": 5}),
            "whatsapp_body": forms.Textarea(attrs={"rows": 6}),
        }

    def clean(self):
        cleaned = super().clean()
        email = bool(cleaned.get("send_email"))
        in_app = bool(cleaned.get("send_in_app"))
        if not email and not in_app:
            raise forms.ValidationError("اختر قناة إرسال واحدة على الأقل.")
        if email and (not cleaned.get("email_subject") or not cleaned.get("email_body")):
            raise forms.ValidationError("عنوان البريد ونصه مطلوبان عند اختيار البريد.")
        if in_app and (not cleaned.get("in_app_title") or not cleaned.get("in_app_body")):
            raise forms.ValidationError("عنوان الإشعار ونصه مطلوبان عند اختيار الإشعار الداخلي.")
        return cleaned
