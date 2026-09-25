import re
import uuid

from django import forms
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator

from reports.model_parts.schools import normalize_sa_mobile_identity
from reports.models import Teacher
from reports.validators import validate_circular_attachment_file

from .models import PersonalEvidence, PersonalInitiative, PersonalNotice, PersonalPlan, PersonalReport, PersonalWorkspace
from .services import current_school_membership_for


class PersonalFormStyleMixin:
    """Apply shared Tawtheeq controls without changing form field contracts."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            classes = field.widget.attrs.get("class", "").split()
            if "twq-control" not in classes:
                classes.append("twq-control")
            field.widget.attrs["class"] = " ".join(classes)
            described_by = field.widget.attrs.get("aria-describedby", "").split()
            if field.help_text:
                described_by.append(f"id_{self.add_prefix(name)}_help")
            if described_by:
                field.widget.attrs["aria-describedby"] = " ".join(dict.fromkeys(described_by))

    def full_clean(self):
        super().full_clean()
        for name, field in self.fields.items():
            if name in self.errors:
                field.widget.attrs["aria-invalid"] = "true"
                described_by = field.widget.attrs.get("aria-describedby", "").split()
                described_by.append(f"id_{self.add_prefix(name)}_errors")
                field.widget.attrs["aria-describedby"] = " ".join(dict.fromkeys(described_by))


class PersonalPlanForm(forms.ModelForm):
    class Meta:
        model = PersonalPlan
        fields = [
            "name", "description", "price", "duration_days", "max_reports",
            "max_evidence", "storage_limit_mb", "display_order", "is_active", "is_published",
        ]
        widgets = {"description": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs["class"] = "teacher-plan-form__checkbox"
            else:
                field.widget.attrs["class"] = "twq-control"
            if field.help_text:
                field.widget.attrs["aria-describedby"] = f"id_{name}_help"


class PersonalRegistrationForm(PersonalFormStyleMixin, forms.Form):
    name = forms.CharField(label="الاسم الكامل", max_length=150)
    gender = forms.ChoiceField(
        label="صيغة المخاطبة",
        choices=(
            ("", "يرجى تحديد صيغة المخاطبة"),
            (Teacher.Gender.MALE, "المعلم"),
            (Teacher.Gender.FEMALE, "المعلمة"),
        ),
        help_text="تُستخدم هذه الصيغة في اسم المساحة والنصوص الموجّهة داخلها.",
    )
    phone = forms.CharField(label="رقم الجوال", max_length=16)
    email = forms.EmailField(label="البريد الإلكتروني للفواتير والتنبيهات")
    school_name = forms.CharField(label="اسم المدرسة", max_length=200)
    principal_name = forms.CharField(label="اسم مدير المدرسة", max_length=150, required=False)
    password = forms.CharField(label="كلمة المرور", widget=forms.PasswordInput)
    password_confirm = forms.CharField(label="تأكيد كلمة المرور", widget=forms.PasswordInput)
    accept_policies = forms.BooleanField(label="أوافق على الشروط وسياسة الخصوصية")

    def clean_phone(self):
        phone = normalize_sa_mobile_identity(self.cleaned_data["phone"])
        if not re.fullmatch(r"05\d{8}", phone):
            raise ValidationError("يرجى إدخال رقم جوال سعودي صحيح يبدأ بـ 05.")
        existing_teacher = Teacher.objects.filter(phone=phone).first()
        if existing_teacher:
            membership = current_school_membership_for(existing_teacher)
            if membership:
                raise ValidationError(
                    "هذا الرقم مرتبط بحساب مدرسي اشتراكه ساري. الدخول بالحساب المدرسي يتيح استخدام "
                    "المساحة دون إنشاء حساب أو اشتراك شخصي منفصل."
                )
            raise ValidationError("هذا الرقم مرتبط بحساب. يمكن تسجيل الدخول واستخدام المساحة الشخصية منه.")
        return phone

    def clean_email(self):
        email = (self.cleaned_data.get("email") or "").strip().lower()
        if email:
            existing_teacher = Teacher.objects.filter(email__iexact=email).first()
            if existing_teacher:
                membership = current_school_membership_for(existing_teacher)
                if membership:
                    raise ValidationError(
                        "هذا البريد مرتبط بحساب مدرسي اشتراكه ساري. الدخول بالحساب المدرسي يتيح استخدام "
                        "المساحة دون إنشاء اشتراك شخصي منفصل."
                    )
                raise ValidationError("هذا البريد مرتبط بحساب. يمكن تسجيل الدخول بالحساب الموجود.")
        return email

    def clean_password(self):
        password = self.cleaned_data["password"]
        validate_password(password)
        return password

    def clean(self):
        data = super().clean()
        if data.get("password") and data.get("password_confirm") != data["password"]:
            self.add_error("password_confirm", "كلمتا المرور غير متطابقتين.")
        return data


class PersonalGenderForm(PersonalFormStyleMixin, forms.Form):
    gender = forms.ChoiceField(
        label="صيغة المخاطبة",
        choices=(
            ("", "غير محددة"),
            (Teacher.Gender.MALE, "المعلم"),
            (Teacher.Gender.FEMALE, "المعلمة"),
        ),
        required=False,
        help_text="تُستخدم هذه الصيغة في اسم المساحة والنصوص الموجّهة داخلها.",
    )

    def __init__(self, *args, teacher=None, **kwargs):
        kwargs.setdefault("initial", {})
        kwargs["initial"].setdefault("gender", getattr(teacher, "gender", ""))
        super().__init__(*args, **kwargs)


class PersonalWorkspaceForm(PersonalFormStyleMixin, forms.ModelForm):
    class Meta:
        model = PersonalWorkspace
        fields = ["school_name", "principal_name", "school_stage", "specialization"]


class PersonalAccountForm(PersonalFormStyleMixin, forms.ModelForm):
    class Meta:
        model = Teacher
        fields = ["name", "email", "gender"]

    def clean_email(self):
        email = (self.cleaned_data.get("email") or "").strip().lower()
        if email and Teacher.objects.filter(email__iexact=email).exclude(pk=self.instance.pk).exists():
            raise ValidationError("هذا البريد مرتبط بحساب آخر.")
        return email


class PersonalEmailForm(PersonalFormStyleMixin, forms.Form):
    email = forms.EmailField(
        label="البريد الإلكتروني للفواتير والتنبيهات",
        max_length=254,
        required=False,
        help_text="ستُرسل إلى هذا العنوان رسالة تأكيد الدفع والفاتورة الإلكترونية.",
    )

    def __init__(self, *args, teacher=None, require_email=False, **kwargs):
        self.teacher = teacher
        kwargs.setdefault("initial", {})
        kwargs["initial"].setdefault("email", getattr(teacher, "email", ""))
        super().__init__(*args, **kwargs)
        self.fields["email"].required = bool(require_email)

    def clean_email(self):
        email = (self.cleaned_data.get("email") or "").strip().lower()
        if not email:
            return email
        duplicates = Teacher.objects.filter(email__iexact=email)
        if self.teacher and self.teacher.pk:
            duplicates = duplicates.exclude(pk=self.teacher.pk)
        if duplicates.exists():
            raise ValidationError("هذا البريد مرتبط بحساب آخر. يلزم تخصيص بريد مستقل لهذا الحساب.")
        return email


def clean_academic_year(value):
    value = (value or "").strip().replace("/", "-")
    if not re.fullmatch(r"\d{4}-\d{4}", value):
        raise ValidationError("الصيغة المطلوبة للسنة: 1447-1448 أو 2025-2026.")
    first, second = map(int, value.split("-"))
    if second != first + 1:
        raise ValidationError("يجب أن تنتهي السنة الدراسية في العام التالي.")
    return value


class PersonalReportForm(PersonalFormStyleMixin, forms.ModelForm):
    selection_enabled = forms.BooleanField(required=False, initial=True, widget=forms.HiddenInput)
    client_submission_id = forms.UUIDField(required=False, widget=forms.HiddenInput)
    academic_year = forms.CharField(label="السنة الدراسية", validators=[clean_academic_year])

    def __init__(self, *args, **kwargs):
        bound = args[0] if args else kwargs.get("data")
        if bound is not None and "selection_enabled" not in bound:
            data = bound.copy()
            for flag, field in (
                ("show_details", "description"),
                ("show_goals", "goals"), ("show_implementation", "implementation"),
                ("show_results", "results"), ("show_recommendations", "recommendations"),
            ):
                if (data.get(field) or "").strip():
                    data[flag] = "on"
            if (data.get("beneficiaries_count") or "").strip():
                data["show_beneficiaries"] = "on"
            if args:
                args = (data, *args[1:])
            else:
                kwargs["data"] = data
        super().__init__(*args, **kwargs)
        self.fields["description"].required = False

    class Meta:
        model = PersonalReport
        fields = [
            "title", "category", "report_date", "academic_year", "show_details", "description",
            "show_goals", "goals", "show_implementation", "implementation",
            "show_results", "results", "show_recommendations", "recommendations",
            "show_beneficiaries", "beneficiaries_count", "status",
        ]
        widgets = {
            "report_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "description": forms.Textarea(attrs={"rows": 5}),
            "goals": forms.Textarea(attrs={"rows": 3}),
            "implementation": forms.Textarea(attrs={"rows": 3}),
            "results": forms.Textarea(attrs={"rows": 3}),
            "recommendations": forms.Textarea(attrs={"rows": 3}),
        }

    def clean_academic_year(self):
        return clean_academic_year(self.cleaned_data["academic_year"])

    def clean(self):
        data = super().clean()
        if not any(data.get(flag) for flag in (
            "show_details", "show_goals", "show_implementation", "show_results",
            "show_recommendations", "show_beneficiaries",
        )):
            self.add_error(None, "اختر بندًا واحدًا على الأقل من محتوى التقرير.")
        if data.get("show_details") and not (data.get("description") or "").strip():
            self.add_error("description", "أدخل وصف العمل أو ألغِ اختيار هذا البند.")
        for flag, field, label in (
            ("show_goals", "goals", "الأهداف"),
            ("show_implementation", "implementation", "آلية التنفيذ"),
            ("show_results", "results", "النتائج"),
            ("show_recommendations", "recommendations", "التوصيات"),
        ):
            if data.get(flag) and not (data.get(field) or "").strip():
                self.add_error(field, f"أدخل محتوى {label} أو ألغِ اختيار هذا البند.")
        if data.get("show_beneficiaries") and data.get("beneficiaries_count") is None:
            self.add_error("beneficiaries_count", "أدخل عدد المستفيدين أو ألغِ اختيار هذا البند.")
        if self.instance.pk and data.get("academic_year") != self.instance.academic_year:
            if self.instance.evidence.exists():
                self.add_error("academic_year", "لا يمكن تغيير سنة التقرير وهو مرتبط بشواهد.")
        return data


class PersonalEvidenceForm(PersonalFormStyleMixin, forms.ModelForm):
    academic_year = forms.CharField(label="السنة الدراسية", validators=[clean_academic_year])

    class Meta:
        model = PersonalEvidence
        fields = ["title", "description", "academic_year", "report", "initiative", "file", "source_url"]
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, workspace, **kwargs):
        super().__init__(*args, **kwargs)
        self.workspace = workspace
        self.fields["report"].queryset = PersonalReport.objects.filter(workspace=workspace)
        self.fields["report"].label = "التقرير المرتبط"
        self.fields["report"].required = False
        self.fields["initiative"].queryset = PersonalInitiative.objects.filter(workspace=workspace)
        self.fields["initiative"].required = False

    def clean_academic_year(self):
        return clean_academic_year(self.cleaned_data["academic_year"])

    def clean(self):
        data = super().clean()
        if not data.get("file") and not data.get("source_url"):
            raise ValidationError("أرفق ملفًا أو رابطًا للشاهد.")
        report = data.get("report")
        if report and data.get("academic_year") and report.academic_year != data["academic_year"]:
            self.add_error("report", "سنة التقرير يجب أن تطابق سنة الشاهد.")
        initiative = data.get("initiative")
        if initiative and data.get("academic_year") and initiative.academic_year != data["academic_year"]:
            self.add_error("initiative", "سنة المبادرة يجب أن تطابق سنة الشاهد.")
        return data


class PersonalYearForm(PersonalFormStyleMixin, forms.Form):
    value = forms.CharField(label="السنة الدراسية", validators=[clean_academic_year], max_length=20)

    def clean_value(self):
        return clean_academic_year(self.cleaned_data["value"])


class PersonalInitiativeForm(PersonalFormStyleMixin, forms.ModelForm):
    academic_year = forms.CharField(label="السنة الدراسية", validators=[clean_academic_year])

    def __init__(self, *args, **kwargs):
        instance = kwargs.get("instance")
        self.original_academic_year = instance.academic_year if instance and instance.pk else None
        super().__init__(*args, **kwargs)

    class Meta:
        model = PersonalInitiative
        fields = ["title", "academic_year", "summary", "impact", "is_best_practice", "status"]
        widgets = {"summary": forms.Textarea(attrs={"rows": 5}), "impact": forms.Textarea(attrs={"rows": 3})}

    def clean_academic_year(self):
        return clean_academic_year(self.cleaned_data["academic_year"])

    def clean(self):
        data = super().clean()
        if self.original_academic_year and data.get("academic_year") != self.original_academic_year:
            if self.instance.evidence.exists():
                self.add_error("academic_year", "لا يمكن تغيير سنة المبادرة وهي مرتبطة بشواهد.")
        return data


class PersonalNoticeForm(PersonalFormStyleMixin, forms.ModelForm):
    submission_key = forms.UUIDField(widget=forms.HiddenInput, initial=uuid.uuid4)
    audience = forms.ChoiceField(label="المستلمون", choices=(("", "اختر المستلمين"), ("all", "جميع أصحاب المساحات الشخصية"), ("one", "مشترك محدد")))
    recipient_phone = forms.CharField(label="جوال المشترك", required=False, max_length=20)

    class Meta:
        model = PersonalNotice
        fields = ["title", "message"]
        widgets = {"message": forms.Textarea(attrs={"rows": 6})}

    def clean(self):
        data = super().clean()
        if data.get("audience") == "one" and not (data.get("recipient_phone") or "").strip():
            self.add_error("recipient_phone", "أدخل جوال المشترك المستهدف.")
        return data


class PersonalInlineEvidenceForm(PersonalFormStyleMixin, forms.Form):
    title = forms.CharField(label="وصف الشاهد", max_length=200, required=False)
    file = forms.FileField(
        label="صورة أو PDF", required=False,
        validators=[validate_circular_attachment_file, FileExtensionValidator(["pdf", "jpg", "jpeg", "png"])],
    )
    source_url = forms.URLField(label="رابط الشاهد", required=False)

    def clean(self):
        data = super().clean()
        if any(data.get(key) for key in ("title", "file", "source_url")):
            if not data.get("title"):
                self.add_error("title", "صف الشاهد قبل حفظه.")
            if not data.get("file") and not data.get("source_url"):
                raise ValidationError("أرفق ملفًا أو رابطًا للشاهد.")
        return data


PersonalInlineEvidenceFormSet = forms.formset_factory(PersonalInlineEvidenceForm, extra=3, max_num=5, validate_max=True)
