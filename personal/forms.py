import re
import uuid

from django import forms
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator
from django.forms.models import BaseInlineFormSet
from django.utils import timezone

from reports.forms import _compress_image_upload
from reports.hijri_utils import current_academic_year
from reports.model_parts.schools import normalize_sa_mobile_identity
from reports.models import Teacher
from reports.report_limits import REPORT_DETAILS_MAX_LENGTH, REPORT_DETAILS_RECOMMENDED_LENGTH, report_details_length_error
from reports.validators import validate_circular_attachment_file, validate_image_file

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
            "report_ai_daily_limit", "voice_report_daily_limit",
        ]
        widgets = {"description": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Existing plan clients submit the original capacity fields only.
        # Keep their saved AI entitlements when those new inputs are absent.
        for name in ("report_ai_daily_limit", "voice_report_daily_limit"):
            self.fields[name].required = False
        for name, field in self.fields.items():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs["class"] = "teacher-plan-form__checkbox"
            else:
                field.widget.attrs["class"] = "twq-control"
            if field.help_text:
                field.widget.attrs["aria-describedby"] = f"id_{name}_help"

    def clean(self):
        cleaned = super().clean()
        for name in ("report_ai_daily_limit", "voice_report_daily_limit"):
            if cleaned.get(name) is None:
                cleaned[name] = (
                    getattr(self.instance, name, 0)
                    if self.instance.pk and name not in self.data else 0
                )
        return cleaned


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
            elif self.instance.portfolio_links.exists():
                self.add_error("academic_year", "لا يمكن تغيير سنة التقرير وهو مرتبط بمحور ملف الإنجاز.")
        return data


PERSONAL_REPORT_CATEGORY_CHOICES = (
    ("نشاط", "نشاط"),
    ("برنامج", "برنامج"),
    ("مبادرة", "مبادرة"),
    ("تطوير مهني", "تطوير مهني"),
    ("مهني", "عمل مهني"),
    ("تطوع", "عمل تطوعي"),
    ("إنجاز", "إنجاز"),
    ("أخرى", "أخرى"),
)


class PersonalSchoolParityReportForm(PersonalFormStyleMixin, forms.ModelForm):
    """The school report editor contract, saved into an owned personal report."""

    section_selection_enabled = forms.BooleanField(required=False, initial=True, widget=forms.HiddenInput)
    client_submission_id = forms.UUIDField(required=False, widget=forms.HiddenInput)
    day_name = forms.CharField(required=False, widget=forms.HiddenInput)
    evidence_page_mode = forms.CharField(required=False, initial="inline", widget=forms.HiddenInput)
    show_goal = forms.BooleanField(required=False, widget=forms.CheckboxInput(attrs={"class": "ar-section-checkbox"}))
    goal = forms.CharField(required=False, widget=forms.Textarea(attrs={
        "class": "textarea", "rows": 3,
        "placeholder": "ما الهدف الذي يسعى النشاط أو البرنامج إلى تحقيقه؟",
    }))
    idea = forms.CharField(required=False, widget=forms.Textarea(attrs={
        "class": "textarea", "rows": 5,
        "placeholder": "اكتب ملخصًا واضحًا لما تم تنفيذه وأبرز تفاصيله",
        "maxlength": str(REPORT_DETAILS_MAX_LENGTH),
        "data-recommended-length": str(REPORT_DETAILS_RECOMMENDED_LENGTH),
        "data-max-length": str(REPORT_DETAILS_MAX_LENGTH),
        "aria-describedby": "report-details-guidance report-details-status",
    }))
    implementation_method = forms.CharField(required=False, widget=forms.Textarea(attrs={
        "class": "textarea", "rows": 4,
        "placeholder": "وضح الخطوات والإجراءات وطريقة تنفيذ النشاط",
    }))
    academic_year = forms.CharField(label="السنة الدراسية", validators=[clean_academic_year])
    category = forms.ChoiceField(label="نوع التقرير", choices=(), widget=forms.Select(attrs={"class": "form-select"}))

    class Meta:
        model = PersonalReport
        fields = (
            "title", "category", "report_date", "academic_year", "day_name",
            "show_goal", "goal", "show_details", "idea", "show_implementation",
            "implementation_method", "show_results", "results", "show_recommendations",
            "recommendations", "show_beneficiaries", "beneficiaries_count", "status",
            "section_selection_enabled", "evidence_page_mode",
        )
        widgets = {
            "title": forms.TextInput(attrs={"class": "input", "placeholder": "العنوان / البرنامج", "maxlength": "255", "autocomplete": "off"}),
            "report_date": forms.DateInput(attrs={"class": "input", "type": "date"}, format="%Y-%m-%d"),
            "show_details": forms.CheckboxInput(attrs={"class": "ar-section-checkbox"}),
            "show_implementation": forms.CheckboxInput(attrs={"class": "ar-section-checkbox"}),
            "show_results": forms.CheckboxInput(attrs={"class": "ar-section-checkbox"}),
            "show_recommendations": forms.CheckboxInput(attrs={"class": "ar-section-checkbox"}),
            "show_beneficiaries": forms.CheckboxInput(attrs={"class": "ar-section-checkbox"}),
            "results": forms.Textarea(attrs={"class": "textarea", "rows": 4, "placeholder": "اذكر النتائج والمخرجات التي تحققت"}),
            "recommendations": forms.Textarea(attrs={"class": "textarea", "rows": 4, "placeholder": "أضف التوصيات أو فرص التحسين المستقبلية"}),
            "beneficiaries_count": forms.NumberInput(attrs={"class": "input", "min": "0", "inputmode": "numeric"}),
            "status": forms.HiddenInput(),
        }

    def __init__(self, *args, workspace=None, **kwargs):
        self.workspace = workspace
        instance = kwargs.get("instance")
        assigned_year = (
            instance.academic_year if instance and instance.pk
            else (getattr(workspace, "current_academic_year", "") or current_academic_year())
        )
        bound_data = args[0] if args else kwargs.get("data")
        if bound_data is not None:
            # Status is not part of the school editor; personal state remains
            # server-owned even if the hidden input is changed by a client.
            data = bound_data.copy()
            data["status"] = instance.status if instance and instance.pk else PersonalReport.Status.COMPLETE
            data["academic_year"] = assigned_year
            if args:
                args = (data, *args[1:])
            else:
                kwargs["data"] = data
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.initial["academic_year"] = assigned_year
            self.initial["status"] = instance.status if instance and instance.pk else PersonalReport.Status.COMPLETE
            self.initial["client_submission_id"] = (
                instance.client_submission_id if instance and instance.pk else uuid.uuid4()
            )
            if instance and instance.pk:
                self.initial.update({
                    "show_goal": instance.show_goals,
                    "goal": instance.goals,
                    "idea": instance.description,
                    "implementation_method": instance.implementation,
                })
        if instance and instance.pk and len(instance.description or "") > REPORT_DETAILS_MAX_LENGTH:
            # Older personal reports may predate the school editor's length
            # limit. Keep them editable without truncating their saved text.
            self.fields["idea"].widget.attrs.pop("maxlength", None)
        categories = {code: label for code, label in PERSONAL_REPORT_CATEGORY_CHOICES}
        if workspace is not None:
            for value in workspace.reports.exclude(category="").values_list("category", flat=True).distinct()[:100]:
                categories.setdefault(value, value)
        if instance and instance.category:
            categories.setdefault(instance.category, instance.category)
        self.fields["category"].choices = [("", "— اختر نوع التقرير —"), *categories.items()]

    def clean_idea(self):
        value = self.cleaned_data.get("idea") or ""
        if len(value) > REPORT_DETAILS_MAX_LENGTH and value != (self.instance.description or ""):
            raise ValidationError(report_details_length_error())
        return value

    def clean_report_date(self):
        value = self.cleaned_data["report_date"]
        if value > timezone.localdate() and value != self.instance.report_date:
            raise ValidationError("لا يمكن اختيار تاريخ مستقبلي.")
        return value

    def clean_academic_year(self):
        value = clean_academic_year(self.cleaned_data["academic_year"])
        if self.instance.pk and value != self.instance.academic_year:
            if self.instance.evidence.exists():
                raise ValidationError("لا يمكن تغيير سنة التقرير وهو مرتبط بشواهد.")
            if self.instance.portfolio_links.exists():
                raise ValidationError("لا يمكن تغيير سنة التقرير وهو مرتبط بمحور ملف الإنجاز.")
        return value

    def clean(self):
        data = super().clean()
        sections = (
            ("show_goal", "goal", "الهدف"),
            ("show_details", "idea", "تفاصيل التقرير"),
            ("show_implementation", "implementation_method", "آلية التنفيذ"),
            ("show_results", "results", "النتائج"),
            ("show_recommendations", "recommendations", "التوصيات"),
        )
        if not any(data.get(flag) for flag, _field, _label in sections) and not data.get("show_beneficiaries"):
            raise ValidationError("اختر بندًا واحدًا على الأقل ليظهر في التقرير.")
        for flag, field, label in sections:
            if data.get(flag) and not (data.get(field) or "").strip():
                self.add_error(field, f"أدخل محتوى بند {label} أو ألغِ اختياره.")
        if data.get("show_beneficiaries") and data.get("beneficiaries_count") is None:
            self.add_error("beneficiaries_count", "أدخل عدد المستفيدين أو ألغِ اختيار هذا البند.")
        return data

    def save(self, commit=True):
        report = super().save(commit=False)
        report.show_goals = bool(self.cleaned_data["show_goal"])
        report.goals = self.cleaned_data.get("goal") or ""
        report.description = self.cleaned_data.get("idea") or ""
        report.implementation = self.cleaned_data.get("implementation_method") or ""
        if commit:
            report.save()
            self.save_m2m()
        return report


class PersonalReportEvidenceForm(forms.ModelForm):
    """School image-card controls backed by a personal witness record."""

    image = forms.ImageField(required=False, widget=forms.ClearableFileInput(attrs={
        "accept": "image/jpeg,image/png,image/webp", "data-evidence-file": "",
    }))
    description = forms.CharField(required=False, max_length=220, widget=forms.TextInput(attrs={
        "placeholder": "مثال: صورة من تنفيذ النشاط", "maxlength": "220",
    }))

    class Meta:
        model = PersonalEvidence
        fields = ("image", "order", "description", "display_size", "fit_mode", "show_in_print")
        widgets = {
            "order": forms.HiddenInput(),
            "display_size": forms.Select(),
            "fit_mode": forms.Select(),
            "show_in_print": forms.CheckboxInput(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._stored_description = self.instance.description if self.instance.pk else ""
        self.fields["display_size"].required = False
        self.fields["fit_mode"].required = False
        if not self.is_bound:
            if self.instance.pk:
                self.initial["description"] = self.instance.report_card_caption
            else:
                try:
                    index = int(str(self.prefix).rsplit("-", 1)[-1])
                except (TypeError, ValueError):
                    index = 0
                self.initial.setdefault("order", index + 1)
                self.initial.setdefault("show_in_print", True)

    def clean_display_size(self):
        return self.cleaned_data.get("display_size") or (
            self.instance.display_size if self.instance.pk else PersonalEvidence.DisplaySize.AUTO
        )

    def clean_fit_mode(self):
        return self.cleaned_data.get("fit_mode") or (
            self.instance.fit_mode if self.instance.pk else PersonalEvidence.FitMode.CONTAIN
        )

    def has_changed(self):
        if not self.instance.pk and not self.files.get(self.add_prefix("image")):
            return False
        return super().has_changed()

    def clean_image(self):
        image = self.cleaned_data.get("image")
        if not image:
            return image
        validate_image_file(image)
        try:
            image = _compress_image_upload(image, max_px=2000, quality=86)
        except Exception as exc:
            raise ValidationError("تعذر تجهيز الصورة. اختر JPG أو PNG أو WebP صالحًا.") from exc
        if image.size > 2 * 1024 * 1024:
            raise ValidationError("حجم الصورة بعد التحسين ما زال أكبر من 2MB.")
        return image

    def save(self, commit=True):
        evidence = super().save(commit=False)
        caption = (self.cleaned_data.get("description") or "").strip() or "شاهد التقرير"
        old_caption = self.instance.report_card_caption if self.instance.pk else ""
        evidence.title = caption[:200]
        evidence.report_caption = caption
        evidence.description = (
            caption if self.instance.pk and not self._stored_description and caption != old_caption
            else self._stored_description
        )
        image = self.cleaned_data.get("image")
        if image:
            evidence.file = image
            evidence.file_size = image.size
            evidence.source_url = ""
        if commit:
            evidence.save()
            self.save_m2m()
        return evidence


class BasePersonalReportEvidenceFormSet(BaseInlineFormSet):
    def clean(self):
        # Check raw IDs before Django excludes deleted forms from form errors.
        if self.is_bound:
            submitted = set()
            for form in self.forms:
                raw = self.data.get(form.add_prefix("id"))
                if raw in (None, ""):
                    continue
                try:
                    submitted.add(int(raw))
                except (TypeError, ValueError):
                    raise ValidationError("أحد الشواهد المرسلة غير صالح لهذا التقرير.") from None
            owned = set(self.get_queryset().filter(pk__in=submitted).values_list("pk", flat=True))
            if submitted != owned:
                raise ValidationError("أحد الشواهد المرسلة غير صالح لهذا التقرير.")
        super().clean()
        if any(self.errors):
            return
        active = [form for form in self.forms if form.cleaned_data and not form.cleaned_data.get("DELETE")
                  and (form.cleaned_data.get("image") or getattr(form.instance.file, "name", ""))]
        active.sort(key=lambda form: (form.cleaned_data.get("order") or 999, form.prefix))
        for number, form in enumerate(active, start=1):
            form.cleaned_data["order"] = number
            form.instance.order = number


def personal_report_evidence_formset(max_images=8):
    return forms.inlineformset_factory(
        PersonalReport, PersonalEvidence, form=PersonalReportEvidenceForm,
        formset=BasePersonalReportEvidenceFormSet,
        fields=("image", "order", "description", "display_size", "fit_mode", "show_in_print"),
        extra=1, can_delete=True, max_num=max_images, validate_max=True,
    )


PersonalReportEvidenceFormSet = personal_report_evidence_formset()


class PersonalEvidenceForm(PersonalFormStyleMixin, forms.ModelForm):
    academic_year = forms.CharField(label="السنة الدراسية", validators=[clean_academic_year])
    presentation_enabled = forms.BooleanField(required=False, initial=True, widget=forms.HiddenInput)

    class Meta:
        model = PersonalEvidence
        fields = [
            "title", "description", "academic_year", "report", "initiative",
            "file", "source_url", "display_size", "fit_mode", "show_in_print",
        ]
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, workspace, **kwargs):
        super().__init__(*args, **kwargs)
        self.workspace = workspace
        self.fields["report"].queryset = PersonalReport.objects.filter(
            workspace=workspace, trashed_at__isnull=True,
        )
        self.fields["report"].label = "التقرير المرتبط"
        self.fields["report"].required = False
        self.fields["initiative"].queryset = PersonalInitiative.objects.filter(workspace=workspace)
        self.fields["initiative"].required = False
        self.fields["display_size"].required = False
        self.fields["fit_mode"].required = False

    def clean_academic_year(self):
        return clean_academic_year(self.cleaned_data["academic_year"])

    def clean(self):
        data = super().clean()
        data["display_size"] = data.get("display_size") or (
            self.instance.display_size if self.instance.pk else PersonalEvidence.DisplaySize.AUTO
        )
        data["fit_mode"] = data.get("fit_mode") or (
            self.instance.fit_mode if self.instance.pk else PersonalEvidence.FitMode.CONTAIN
        )
        if self.is_bound and "presentation_enabled" not in self.data:
            data["show_in_print"] = self.instance.show_in_print if self.instance.pk else True
        if self.instance.pk and self.instance.report_id and not data.get("report"):
            if self.instance.report.trashed_at:
                data["report"] = self.instance.report
        if not data.get("file") and not data.get("source_url"):
            raise ValidationError("أرفق ملفًا أو رابطًا للشاهد.")
        if self.instance.pk and data.get("academic_year") != self.instance.academic_year:
            if self.instance.portfolio_links.exists():
                self.add_error("academic_year", "لا يمكن تغيير سنة شاهد مرتبط بمحور ملف الإنجاز.")
        report = data.get("report")
        if report and data.get("academic_year") and report.academic_year != data["academic_year"]:
            self.add_error("report", "سنة التقرير يجب أن تطابق سنة الشاهد.")
        if report and report.evidence.exclude(pk=self.instance.pk).count() >= 8:
            self.add_error("report", "الحد الأعلى للتقرير 8 شواهد؛ اختر تقريرًا آخر أو أزل شاهدًا منه.")
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
    presentation_enabled = forms.BooleanField(required=False, initial=True, widget=forms.HiddenInput)
    title = forms.CharField(label="وصف الشاهد", max_length=200, required=False)
    file = forms.FileField(
        label="صورة أو PDF", required=False,
        validators=[validate_circular_attachment_file, FileExtensionValidator(["pdf", "jpg", "jpeg", "png"])],
    )
    source_url = forms.URLField(label="رابط الشاهد", required=False)
    display_size = forms.ChoiceField(
        label="حجم العرض", choices=PersonalEvidence.DisplaySize.choices,
        initial=PersonalEvidence.DisplaySize.AUTO, required=False,
    )
    fit_mode = forms.ChoiceField(
        label="طريقة الملاءمة", choices=PersonalEvidence.FitMode.choices,
        initial=PersonalEvidence.FitMode.CONTAIN, required=False,
    )
    show_in_print = forms.BooleanField(label="إظهار في الطباعة", initial=True, required=False)

    def clean(self):
        data = super().clean()
        if any(data.get(key) for key in ("title", "file", "source_url")):
            if not data.get("title"):
                self.add_error("title", "صف الشاهد قبل حفظه.")
            if not data.get("file") and not data.get("source_url"):
                raise ValidationError("أرفق ملفًا أو رابطًا للشاهد.")
        return data


def personal_inline_evidence_formset(max_new=8):
    return forms.formset_factory(
        PersonalInlineEvidenceForm, extra=min(3, max_new), max_num=max_new, validate_max=True,
    )


PersonalInlineEvidenceFormSet = personal_inline_evidence_formset()
