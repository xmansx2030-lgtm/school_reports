from django import forms

from reports.academic_years import approved_academic_year_options

from .forms import PersonalFormStyleMixin
from .models import PersonalCourse
from .services import personal_evidence_for_year, personal_year_choices


class PersonalCourseForm(PersonalFormStyleMixin, forms.ModelForm):
    academic_year = forms.ChoiceField(label="السنة الدراسية")

    class Meta:
        model = PersonalCourse
        fields = ["title", "provider", "academic_year", "start_date", "completion_date", "hours", "description", "certificate"]
        widgets = {
            "start_date": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date", "dir": "ltr"}),
            "completion_date": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date", "dir": "ltr"}),
            "hours": forms.NumberInput(attrs={"min": "0.01", "step": "0.01", "dir": "ltr"}),
            "description": forms.Textarea(attrs={"rows": 4, "maxlength": "10000"}),
        }
        help_texts = {
            "completion_date": "اتركه فارغًا إذا لم تُكمل الدورة بعد.",
            "certificate": "اختر شهادة محفوظة في مكتبة شواهد السنة نفسها. إرفاق الشهادة اختياري.",
            "description": "دوّن المهارات المكتسبة وكيف ستطبقها في عملك التعليمي.",
        }

    def __init__(self, *args, workspace, year, **kwargs):
        self.workspace = workspace
        super().__init__(*args, **kwargs)
        self.instance.workspace = workspace
        self.fields["academic_year"].choices = personal_year_choices(workspace)
        if self.instance.pk:
            year = self.instance.academic_year
            self.fields["academic_year"].disabled = True
            self.fields["academic_year"].help_text = "تُحفظ الدورة في سنتها الأصلية، ولا تنتقل عند تغيير سنة العمل."
            self.fields["academic_year"].widget.attrs["aria-describedby"] = f"id_{self.add_prefix('academic_year')}_help"
        self.initial["academic_year"] = year
        certificate_year = self.data.get(self.add_prefix("academic_year"), year) if self.is_bound and not self.instance.pk else year
        self.fields["certificate"].queryset = personal_evidence_for_year(workspace, certificate_year)
        self.fields["certificate"].empty_label = "دون شهادة مرفقة"
        self.fields["description"].max_length = 10000

    def clean_academic_year(self):
        value = self.cleaned_data["academic_year"]
        if value not in approved_academic_year_options():
            raise forms.ValidationError("هذه السنة التاريخية متاحة للقراءة والتصدير فقط حتى تعتمدها إدارة المنصة.")
        return value

    def clean_description(self):
        description = self.cleaned_data.get("description", "")
        if len(description) > 10000:
            raise forms.ValidationError("الحد الأعلى للوصف 10000 حرف.")
        return description
