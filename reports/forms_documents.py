# -*- coding: utf-8 -*-
"""نموذج رفع الوثائق وتصنيفها."""
from __future__ import annotations

from django import forms
from django.core.exceptions import ValidationError

from .academic_years import hijri_academic_year_options, school_current_academic_year
from .models import Department, Document
from .permissions import is_school_manager, supervised_department_ids

__all__ = ["DocumentUploadForm"]


class DocumentUploadForm(forms.ModelForm):
    class Meta:
        model = Document
        fields = ("title", "description", "academic_year", "department", "kind", "file")
        widgets = {
            "title": forms.TextInput(attrs={"placeholder": "مثال: محضر لجنة التوجيه — الجلسة الثانية"}),
            "description": forms.Textarea(
                attrs={"rows": 2, "placeholder": "وصف مختصر يعين على البحث لاحقاً"}
            ),
        }

    def __init__(self, *args, school=None, uploader=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.school = school
        self.uploader = uploader

        # القائمة من المصدر المشترك لا من حقلَي المدرسة وحدهما: المدرسة الجديدة
        # تُنشأ وحقلاها فارغان، وكان ذلك يترك حقلاً **إلزامياً بلا خيار واحد**
        # فيتعذّر رفع أي وثيقة حتى يمرّ المدير على «بيانات المدرسة» — وهو ما لا
        # تدلّ عليه رسالة الحقل.
        years = sorted(hijri_academic_year_options(school), reverse=True)

        self.fields["academic_year"] = forms.ChoiceField(
            label="السنة الدراسية",
            required=True,
            choices=[("", "— اختر السنة —")] + [(item, f"{item} هـ") for item in years],
            error_messages={"required": "السنة الدراسية أول ما يُقلَّب به الأرشيف."},
        )
        if school is not None:
            self.fields["academic_year"].initial = (
                school_current_academic_year(school)
            )
            if not self.instance.pk and not self.initial.get("academic_year"):
                self.initial["academic_year"] = self.fields["academic_year"].initial

        departments = (
            Department.objects.filter(school=school, is_active=True).order_by("name")
            if school is not None
            else Department.objects.none()
        )
        if (
            school is not None
            and uploader is not None
            and not is_school_manager(uploader, active_school=school)
        ):
            departments = departments.filter(
                pk__in=supervised_department_ids(uploader, school)
            )
        self.fields["department"].queryset = departments
        self.fields["department"].required = False
        self.fields["department"].help_text = (
            "يحدّد من يراجع أرشفتها ضمن نطاقه. تركه فارغاً يجعل الاعتماد لمدير المدرسة."
        )
        self.fields["file"].help_text = "PDF أو صورة أو مستند حتى 5MB."

    def clean_department(self):
        department = self.cleaned_data.get("department")
        if department is not None and self.school is not None:
            if department.school_id != self.school.pk:
                raise ValidationError("هذا القسم ليس من أقسام مدرستك.")
            if (
                self.uploader is not None
                and not is_school_manager(self.uploader, active_school=self.school)
                and department.pk
                not in supervised_department_ids(self.uploader, self.school)
            ):
                raise ValidationError("هذا القسم خارج نطاق إشرافك.")
        return department
