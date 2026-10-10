# -*- coding: utf-8 -*-
"""نماذج الخطط والمبادرات."""
from __future__ import annotations

from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone
from .form_widgets import DateTimeLocalInput
from .academic_years import school_current_academic_year, hijri_academic_year_options

from .models import (
    Department,
    Initiative,
    Plan,
    PlanGoal,
    PlanTask,
    SchoolMembership,
    Teacher,
)

__all__ = ["PlanForm", "PlanGoalForm", "PlanTaskForm", "InitiativeForm"]


class PlanForm(forms.ModelForm):
    class Meta:
        model = Plan
        fields = ("title", "description", "academic_year", "starts_on", "ends_on")
        widgets = {
            "starts_on": forms.DateInput(attrs={"type": "date"}),
            "ends_on": forms.DateInput(attrs={"type": "date"}),
            "description": forms.Textarea(attrs={"rows": 4, "placeholder": "غاية الخطة ومنطلقاتها…"}),
            "title": forms.TextInput(attrs={"placeholder": "مثال: خطة تحسين نواتج التعلم"}),
        }

    def __init__(self, *args, school=None, group=None, owner=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.school = school
        self.group = group

        self.instance.scope = Plan.Scope.GROUP if group is not None else Plan.Scope.SCHOOL
        self.instance.school = school
        self.instance.group = group
        self.instance.owner = owner

        if school is not None and not self.instance.pk:
            self.fields["academic_year"].initial = (
                school_current_academic_year(school)
            )
        years = set(hijri_academic_year_options(school))
        if self.instance.academic_year:
            years.add(self.instance.academic_year)
        self.fields["academic_year"] = forms.ChoiceField(
            label="السنة الدراسية", required=self.fields["academic_year"].required,
            choices=[("", "— اختر السنة —")] + [(value, f"{value} هـ") for value in sorted(years, reverse=True)],
            initial=school_current_academic_year(school) if not self.instance.pk else self.instance.academic_year,
            help_text="الأعوام المعتمدة مركزيًا مع سنوات هذه المدرسة المحفوظة.",
        )
        if not self.instance.pk and not self.initial.get("academic_year"):
            self.initial["academic_year"] = self.fields["academic_year"].initial


class PlanGoalForm(forms.ModelForm):
    class Meta:
        model = PlanGoal
        fields = ("title", "indicator", "target")
        widgets = {
            "title": forms.TextInput(attrs={"placeholder": "الهدف", "id": "id_plan_goal_title"}),
            "indicator": forms.TextInput(attrs={"placeholder": "مؤشر القياس"}),
            "target": forms.TextInput(attrs={"placeholder": "المستهدف"}),
        }


class PlanTaskForm(forms.ModelForm):
    """مهمة في الخطة.

    المسؤول والموعد اختياريان: خطةٌ تُصاغ مهامها قبل أن يُسمّى منفّذوها حالةٌ
    مشروعة، ومنعُها يدفع المُعِدّ إلى إسناد وهمي ليُكمل النموذج. وهما إلزاميان
    عند التحويل إلى تكليف.
    """

    class Meta:
        model = PlanTask
        fields = ("title", "description", "goal", "responsible", "department", "due_at")
        widgets = {
            "title": forms.TextInput(attrs={"placeholder": "المهمة", "id": "id_plan_task_title"}),
            "description": forms.Textarea(attrs={"rows": 2, "placeholder": "تفصيل (اختياري)"}),
            "due_at": DateTimeLocalInput(),
        }

    def __init__(self, *args, plan=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.plan = plan
        school = getattr(plan, "school", None)

        self.fields["goal"].queryset = (
            PlanGoal.objects.filter(plan=plan).order_by("order")
            if plan is not None
            else PlanGoal.objects.none()
        )
        self.fields["goal"].required = False
        self.fields["goal"].label = "الهدف المرتبط"

        if plan is not None and plan.scope == Plan.Scope.GROUP:
            # خطة مشتركة: المسؤولون مديرو مدارس المجموعة.
            self.fields["responsible"].queryset = Teacher.objects.filter(
                school_memberships__school__group=plan.group,
                school_memberships__role_type=SchoolMembership.RoleType.MANAGER,
                school_memberships__is_active=True,
            ).distinct().order_by("name")
            self.fields["department"].queryset = Department.objects.none()
        else:
            self.fields["responsible"].queryset = (
                Teacher.objects.filter(
                    is_active=True,
                    school_memberships__school=school,
                    school_memberships__is_active=True,
                    school_memberships__role_type__in=SchoolMembership.STAFF_ROLES,
                ).distinct().order_by("name")
                if school is not None
                else Teacher.objects.none()
            )
            self.fields["department"].queryset = (
                Department.objects.filter(school=school, is_active=True).order_by("name")
                if school is not None
                else Department.objects.none()
            )

        self.fields["responsible"].required = False
        self.fields["department"].required = False
        self.fields["due_at"].required = False
        self.fields["due_at"].help_text = "مطلوب لتحويل المهمة إلى تكليف متابَع."

    def clean_due_at(self):
        value = self.cleaned_data.get("due_at")
        if value and value <= timezone.now():
            raise ValidationError("موعد التنفيذ يجب أن يكون في المستقبل.")
        return value


class InitiativeForm(forms.ModelForm):
    class Meta:
        model = Initiative
        fields = ("title", "summary", "is_best_practice", "plan")
        widgets = {
            "title": forms.TextInput(attrs={"placeholder": "عنوان المبادرة"}),
            "summary": forms.Textarea(
                attrs={"rows": 5, "placeholder": "الفكرة، وكيف نُفِّذت، وما أثرها…"}
            ),
        }

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["plan"].queryset = (
            Plan.objects.filter(school=school).order_by("-created_at")
            if school is not None
            else Plan.objects.none()
        )
        self.fields["plan"].required = False
        self.fields["plan"].label = "الخطة المرتبطة (اختياري)"
        self.fields["is_best_practice"].help_text = (
            "المبادرة التي تصلح لغير مدرستك. مشاركتها مع المجموعة قرارٌ لاحق "
            "لمدير المدرسة بعد الاعتماد."
        )
