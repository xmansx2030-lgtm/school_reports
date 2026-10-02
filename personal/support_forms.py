"""Explicit platform-owner configuration of a personal subscription."""

from django import forms
from django.utils import timezone

from .models import PersonalSubscription


class PersonalMaintenanceSubscriptionForm(forms.ModelForm):
    class Meta:
        model = PersonalSubscription
        fields = ("plan", "is_active", "end_date")
        labels = {"plan": "الباقة", "is_active": "السماح باستخدام الاشتراك", "end_date": "تاريخ انتهاء الاشتراك"}
        widgets = {"end_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")}
        help_texts = {
            "is_active": "التفعيل هنا قرار إداري مباشر، ويُسجَّل باسمك.",
            "end_date": "اتركه فارغًا لاشتراك مستمر. تغيير الباقة يبدأ فترة جديدة ويعيد احتساب حدودها.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "twq-control"

    def clean(self):
        data = super().clean()
        if data.get("plan") != self.instance.plan and data.get("end_date"):
            if data["end_date"] < timezone.localdate():
                self.add_error("end_date", "اختر تاريخًا من اليوم فصاعدًا عند تغيير الباقة.")
        return data

    def save(self, commit=True):
        subscription = super().save(commit=commit)
        if commit and subscription.end_date != self.cleaned_data["end_date"]:
            subscription.end_date = self.cleaned_data["end_date"]
            subscription.save(update_fields=["end_date"])
        return subscription
