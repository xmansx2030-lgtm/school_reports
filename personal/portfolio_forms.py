from django import forms

from .forms import PersonalFormStyleMixin
from .models import PersonalAcademicYear


class PersonalPortfolioProfileForm(PersonalFormStyleMixin, forms.ModelForm):
    class Meta:
        model = PersonalAcademicYear
        fields = [
            "qualifications", "professional_experience", "specialization",
            "teaching_load", "subjects_taught", "contact_info",
        ]
        widgets = {
            "qualifications": forms.Textarea(attrs={"rows": 2}),
            "professional_experience": forms.Textarea(attrs={"rows": 2}),
            "specialization": forms.Textarea(attrs={"rows": 2}),
            "teaching_load": forms.Textarea(attrs={"rows": 2}),
            "subjects_taught": forms.Textarea(attrs={"rows": 2}),
            "contact_info": forms.Textarea(attrs={"rows": 2}),
        }

    def clean(self):
        data = super().clean()
        for name in self.Meta.fields:
            if len(data.get(name) or "") > 3000:
                self.add_error(name, "الحد الأعلى لهذا الحقل 3000 حرف.")
        return data
