# reports/forms.py
# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Optional, List, Tuple
from decimal import Decimal
from io import BytesIO
import os
import logging
import uuid

from django import forms
from django.forms.models import BaseInlineFormSet
from django.contrib.auth.forms import PasswordChangeForm, PasswordResetForm, SetPasswordForm
from django.contrib.auth.password_validation import (
    CommonPasswordValidator,
    MinimumLengthValidator,
    NumericPasswordValidator,
    UserAttributeSimilarityValidator,
    get_default_password_validators,
)
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.core.files.uploadedfile import InMemoryUploadedFile
from django.db import models, transaction
from django.db.models import Q
from django.utils.text import slugify
from django.utils import timezone

from core.observability import report_degraded as _degraded, soft_fail

from .validators import validate_circular_attachment_file
from .form_widgets import DateTimeLocalInput
from .gender_labels import school_gender_labels
from .report_limits import (
    REPORT_DETAILS_MAX_LENGTH,
    REPORT_DETAILS_RECOMMENDED_LENGTH,
    report_details_length_error,
)
from .staff_assignments import assignment_cards, assignment_choices, get_assignment

# ==============================
# استيراد الموديلات (من models.py فقط)
# ==============================
from .models import (
    Teacher,
    Department,
    DepartmentMembership,
    ReportType,
    Report,
    ReportEvidence,
    Ticket,
    TicketNote,
    Notification,
    NotificationRecipient,
    School,
    SchoolAdditionRequest,
    SchoolMembership,
    PlatformSettings,
    SubscriptionPlan,
    SchoolSubscription,
    SchoolArchiveAddon,
    ArchiveStorageOption,
    DiscountCode,
    TeacherAchievementFile,
    AchievementSection,
    AchievementEvidenceImage,
    SchoolLeadershipPortfolio,
    LeadershipPortfolioSection,
)
from .model_parts.approvals import ApprovalRoute
from .lab_kinds import LabKind

logger = logging.getLogger(__name__)

# Avoid repeating the same warning on every request when broker is not configured.
_NOTIF_CELERY_FALLBACK_WARNED = False

# (تراثي – اختياري)
try:
    from .models import RequestTicket, REQUEST_DEPARTMENTS  # type: ignore
    HAS_REQUEST_TICKET = True
except Exception:
    RequestTicket = None  # type: ignore
    REQUEST_DEPARTMENTS = []  # type: ignore
    HAS_REQUEST_TICKET = False

# ==============================
# أدوات تحقق عامة (SA-specific)
# ==============================
digits10 = RegexValidator(r"^\d{10}$", "يجب أن يتكون من 10 أرقام.")
sa_phone = RegexValidator(r"^0\d{9}$", "رقم الجوال يجب أن يبدأ بـ 0 ويتكون من 10 أرقام.")


class SchoolAdditionRequestForm(forms.ModelForm):
    class Meta:
        model = SchoolAdditionRequest
        fields = [
            "school_name",
            "stage",
            "gender",
            "city",
            "phone",
            "email",
            "manager_notes",
        ]
        widgets = {
            "school_name": forms.TextInput(attrs={"class": "form-control", "placeholder": "اسم المدرسة الرسمي"}),
            "stage": forms.Select(attrs={"class": "form-select"}),
            "gender": forms.Select(attrs={"class": "form-select"}),
            "city": forms.TextInput(attrs={"class": "form-control", "placeholder": "المدينة"}),
            "phone": forms.TextInput(attrs={"class": "form-control", "placeholder": "05XXXXXXXX", "dir": "ltr", "inputmode": "tel"}),
            "email": forms.EmailInput(attrs={"class": "form-control", "placeholder": "school@example.com", "dir": "ltr"}),
            "manager_notes": forms.Textarea(attrs={"class": "form-control", "rows": 4, "placeholder": "ملاحظات اختيارية عن المدرسة"}),
        }

    def __init__(self, *args, requested_by=None, **kwargs):
        self.requested_by = requested_by
        super().__init__(*args, **kwargs)

    def clean_school_name(self):
        name = " ".join((self.cleaned_data.get("school_name") or "").split())
        if len(name) < 3:
            raise ValidationError("أدخل اسم المدرسة الرسمي.")
        return name

    def clean_phone(self):
        raw = (self.cleaned_data.get("phone") or "").strip()
        if not raw:
            return ""
        phone = raw.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
        phone = "".join(character for character in phone if character.isdigit())
        if phone.startswith("9665") and len(phone) == 12:
            phone = f"0{phone[3:]}"
        elif phone.startswith("5") and len(phone) == 9:
            phone = f"0{phone}"
        if len(phone) != 10 or not phone.startswith("05"):
            raise ValidationError("أدخل رقم جوال سعودي صحيحًا يبدأ بـ 05.")
        return phone


def _school_job_title_choices(active_school: Optional["School"] = None) -> tuple[tuple[str, str], ...]:
    """Display job-title labels using the active school's gender, without changing stored values."""
    labels = school_gender_labels(active_school)
    return (
        (SchoolMembership.JobTitle.TEACHER, str(labels["teacher_indefinite"])),
        (SchoolMembership.JobTitle.ADMIN_STAFF, str(labels["admin_staff"])),
        (SchoolMembership.JobTitle.LAB_TECH, str(labels["lab_tech"])),
    )


class MyProfilePhoneForm(forms.ModelForm):
    """تحديث رقم جوال المستخدم الحالي.

    مهم: phone هو USERNAME_FIELD، لذلك نتحقق من التفرد قبل الحفظ.
    """

    phone = forms.CharField(
        label="رقم الجوال",
        min_length=10,
        max_length=10,
        validators=[sa_phone],
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "05XXXXXXXX",
                "maxlength": "10",
                "inputmode": "numeric",
                "pattern": r"0\d{9}",
                "autocomplete": "tel",
            }
        ),
    )

    class Meta:
        model = Teacher
        fields = ["phone"]

    def clean_phone(self):
        phone = (self.cleaned_data.get("phone") or "").strip()
        if not phone:
            raise ValidationError("رقم الجوال مطلوب.")

        qs = Teacher.objects.filter(phone=phone)
        if getattr(self.instance, "pk", None):
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise ValidationError("هذا الرقم مستخدم بالفعل.")

        return phone


class MyProfileEmailForm(forms.ModelForm):
    """تحديث البريد الإلكتروني للمستخدم الحالي مع التحقق من التفرد."""

    email = forms.EmailField(
        label="البريد الإلكتروني",
        required=False,
        widget=forms.EmailInput(
            attrs={
                "class": "form-control",
                "placeholder": "name@example.com",
                "autocomplete": "email",
                "inputmode": "email",
                "dir": "ltr",
            }
        ),
    )

    class Meta:
        model = Teacher
        fields = ["email"]

    def clean_email(self):
        email = (self.cleaned_data.get("email") or "").strip().lower()
        if not email:
            return ""

        qs = Teacher.objects.filter(email__iexact=email)
        if getattr(self.instance, "pk", None):
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise ValidationError("هذا البريد الإلكتروني مستخدم في حساب آخر.")

        return email


class MyPasswordChangeForm(PasswordChangeForm):
    """نموذج تغيير كلمة المرور مع تحسين شكل الحقول."""

    email = forms.EmailField(
        label="البريد الإلكتروني",
        required=False,
        widget=forms.EmailInput(
            attrs={
                "autocomplete": "email",
                "inputmode": "email",
                "dir": "ltr",
                "placeholder": "name@example.com",
            }
        ),
    )

    # حد أدنى مبسّط للطول (بلا قيود "رقمية/شائعة/تشابه").
    SIMPLE_MIN_LENGTH = 6

    def __init__(self, *args, require_email: bool = False, **kwargs):
        self.require_email = require_email
        super().__init__(*args, **kwargs)
        self.fields["email"].required = require_email
        self.fields["email"].initial = (getattr(self.user, "email", "") or "").strip()
        self.password_requirements = self._build_password_requirements()
        # تبسيط: نتجاوز ما يضبطه المُدقّق الافتراضي ونعتمد حدًّا بسيطًا.
        self.password_min_length = self.SIMPLE_MIN_LENGTH
        for name, f in self.fields.items():
            try:
                f.widget.attrs.setdefault("class", "form-control")
                if name == "old_password":
                    f.widget.attrs.setdefault("autocomplete", "current-password")
                elif name == "email":
                    f.widget.attrs.setdefault("autocomplete", "email")
                else:
                    f.widget.attrs.setdefault("autocomplete", "new-password")
            except Exception:
                _degraded("forms.password_autocomplete_hint", field=name)

        self.fields["old_password"].widget.attrs.setdefault("placeholder", "أدخل كلمة المرور الحالية")
        self.fields["new_password1"].widget.attrs.setdefault("placeholder", "أدخل كلمة مرور جديدة قوية")
        self.fields["new_password2"].widget.attrs.setdefault("placeholder", "أعد إدخال كلمة المرور الجديدة")

    def clean_email(self):
        email = (self.cleaned_data.get("email") or "").strip().lower()
        if self.require_email and not email:
            raise forms.ValidationError("البريد الإلكتروني مطلوب لاستعادة كلمة المرور لاحقًا.")

        if email:
            existing = Teacher.objects.filter(email__iexact=email).exclude(pk=self.user.pk)
            if existing.exists():
                raise forms.ValidationError("هذا البريد الإلكتروني مستخدم في حساب آخر.")
        return email

    def save(self, commit=True):
        if self.require_email:
            self.user.email = self.cleaned_data["email"]
        return super().save(commit=commit)

    def _build_password_requirements(self) -> list[dict[str, str]]:
        requirements: list[dict[str, str]] = []

        for validator in get_default_password_validators():
            if isinstance(validator, MinimumLengthValidator):
                min_length = int(getattr(validator, "min_length", 8) or 8)
                self.password_min_length = min_length
                requirements.append(
                    {
                        "key": "min_length",
                        "label": f"أن تتكون من {min_length} أحرف على الأقل.",
                        "hint": "كلما زاد الطول كانت الحماية أفضل.",
                        "mode": "live",
                    }
                )
            elif isinstance(validator, UserAttributeSimilarityValidator):
                requirements.append(
                    {
                        "key": "not_similar",
                        "label": "ألا تكون قريبة من اسمك أو رقم الجوال.",
                        "hint": "تجنب أي كلمة يسهل توقعها من معلومات الحساب.",
                        "mode": "server",
                    }
                )
            elif isinstance(validator, CommonPasswordValidator):
                requirements.append(
                    {
                        "key": "not_common",
                        "label": "ألا تكون كلمة مرور شائعة أو سهلة التخمين.",
                        "hint": "مثل الكلمات الشائعة أو التسلسلات المعروفة.",
                        "mode": "server",
                    }
                )
            elif isinstance(validator, NumericPasswordValidator):
                requirements.append(
                    {
                        "key": "not_numeric",
                        "label": "ألا تتكون من أرقام فقط.",
                        "hint": "يفضل مزج الحروف مع الأرقام أو الرموز.",
                        "mode": "live",
                    }
                )

        requirements.append(
            {
                "key": "match",
                "label": "أن يتطابق تأكيد كلمة المرور مع الكلمة الجديدة.",
                "hint": "التطابق يساعد على تجنب أخطاء الكتابة قبل الحفظ.",
                "mode": "live",
            }
        )

        return requirements

    def clean_new_password2(self):
        """تبسيط القبول: نتجاوز مدققات Django الصارمة (الطول 8/الشائعة/الرقمية/التشابه)
        ونكتفي بتطابق التأكيد + حدّ أدنى بسيط للطول.
        """
        password1 = self.cleaned_data.get("new_password1")
        password2 = self.cleaned_data.get("new_password2")
        if password1 and password2 and password1 != password2:
            raise forms.ValidationError("كلمتا المرور غير متطابقتين.")
        if password2 and len(password2) < self.password_min_length:
            raise forms.ValidationError(
                f"كلمة المرور يجب أن تكون {self.password_min_length} أحرف على الأقل."
            )
        return password2


class AccountPasswordResetForm(PasswordResetForm):
    """Public password-recovery form without exposing account existence."""

    email = forms.EmailField(
        label="البريد الإلكتروني",
        max_length=254,
        widget=forms.EmailInput(
            attrs={
                "class": "recovery-input",
                "autocomplete": "email",
                "inputmode": "email",
                "dir": "ltr",
                "placeholder": "name@example.com",
                "autofocus": True,
            }
        ),
    )

    def clean_email(self):
        return (self.cleaned_data.get("email") or "").strip().lower()

    def get_users(self, email):
        for user in Teacher.objects.filter(email__iexact=email, is_active=True):
            yield user


class AccountSetPasswordForm(SetPasswordForm):
    """Style the one-time reset form while retaining Django validators."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["new_password1"].label = "كلمة المرور الجديدة"
        self.fields["new_password2"].label = "تأكيد كلمة المرور الجديدة"
        self.fields["new_password1"].widget.attrs.update(
            {
                "class": "recovery-input",
                "autocomplete": "new-password",
                "placeholder": "أدخل كلمة مرور جديدة",
                "autofocus": True,
            }
        )
        self.fields["new_password2"].widget.attrs.update(
            {
                "class": "recovery-input",
                "autocomplete": "new-password",
                "placeholder": "أعد إدخال كلمة المرور الجديدة",
            }
        )


def _validate_academic_year_hijri(value: str) -> str:
    """تحقق من صيغة السنة الدراسية الهجرية 1447-1448."""
    value = (value or "").strip().replace("–", "-").replace("—", "-")
    import re

    if not re.fullmatch(r"\d{4}-\d{4}", value):
        raise ValidationError("صيغة السنة الدراسية يجب أن تكون مثل 1447-1448")
    s, e = value.split("-", 1)
    if int(e) != int(s) + 1:
        raise ValidationError("السنة الدراسية يجب أن تكون مثل 1447-1448 (فرق سنة واحدة)")
    return value

# ==============================
# مساعدات داخلية للأقسام/المستخدمين
# ==============================
def _has_multi_active_schools() -> bool:
    try:
        return School.objects.filter(is_active=True).count() > 1
    except Exception:
        return False


def _teachers_for_dept(dept_slug: str, school: Optional["School"] = None):
    """
    إرجاع QuerySet للمعلمين المنتمين لقسم معيّن.
    - عبر عضوية DepartmentMembership (department ←→ teacher)

    ملاحظة: لا نعتمد على Role.slug لأن الأقسام أصبحت مخصصة لكل مدرسة ويمكن تكرار slugs.
    """
    if not dept_slug:
        return Teacher.objects.none()

    # في وضع تعدد المدارس لا نسمح بحل قسم عبر slug بدون تحديد school
    if school is None and hasattr(Department, "school") and _has_multi_active_schools():
        return Teacher.objects.none()

    dep_qs = Department.objects.filter(slug__iexact=dept_slug)
    if school is not None and hasattr(Department, "school"):
        dep_qs = dep_qs.filter(school=school)
    dep = dep_qs.first()
    if not dep:
        return Teacher.objects.none()

    base_qs = Teacher.objects.filter(is_active=True)
    if school is not None:
        base_qs = base_qs.filter(
            school_memberships__school=school,
        )

    teacher_ids = DepartmentMembership.objects.filter(department=dep).values_list("teacher_id", flat=True)
    return base_qs.filter(id__in=teacher_ids).only("id", "name").order_by("name").distinct()


def _is_teacher_in_dept(teacher: Teacher, dept_slug: str, school: Optional["School"] = None) -> bool:
    """هل المعلّم ينتمي للقسم؟"""
    if not teacher or not dept_slug:
        return False

    # في وضع تعدد المدارس لا نسمح بحل قسم عبر slug بدون تحديد school
    if school is None and hasattr(Department, "school") and _has_multi_active_schools():
        return False

    dept_slug_norm = (dept_slug or "").strip().lower()
    dep_qs = Department.objects.filter(slug__iexact=dept_slug_norm)
    if school is not None and hasattr(Department, "school"):
        dep_qs = dep_qs.filter(school=school)
    dep = dep_qs.first()
    if not dep:
        return False

    return DepartmentMembership.objects.filter(department=dep, teacher=teacher).exists()


def _is_teacher_in_department(teacher: Teacher, department: Optional[Department]) -> bool:
    """هل المعلّم ينتمي لكائن قسم محدد (بدون lookup بالـ slug)؟"""
    if not teacher or not department:
        return False

    return DepartmentMembership.objects.filter(department=department, teacher=teacher).exists()


def _compress_image_upload(f, *, max_px: int = 1600, quality: int = 85) -> InMemoryUploadedFile:
    """توحيد صورة مرفوعة قبل التخزين مع تصحيح EXIF وحفظ الشفافية.

    - يقلّص الأبعاد القصوى إلى max_px.
    - يحاول الحفظ بصيغة WEBP، مع fallback إلى PNG/JPEG.
    """
    from PIL import Image, ImageOps

    try:
        f.seek(0)
    except (AttributeError, OSError, ValueError):
        pass
    img = Image.open(f)
    img = ImageOps.exif_transpose(img)
    has_alpha = img.mode in ("RGBA", "LA", "P")
    img = img.convert("RGBA" if has_alpha else "RGB")

    if max(img.size) > max_px:
        img.thumbnail((max_px, max_px), Image.LANCZOS)

    buf = BytesIO()
    try:
        img.save(buf, format="WEBP", quality=quality, method=6, exact=has_alpha)
        new_ext, ctype = ".webp", "image/webp"
    except Exception:
        buf = BytesIO()
        fmt = "PNG" if has_alpha else "JPEG"
        save_kwargs = {"optimize": True}
        if fmt == "JPEG":
            save_kwargs["quality"] = quality
        img.save(buf, format=fmt, **save_kwargs)
        new_ext = ".png" if has_alpha else ".jpg"
        ctype = "image/png" if has_alpha else "image/jpeg"

    buf.seek(0)
    base = os.path.splitext(getattr(f, "name", "image"))[0]
    return InMemoryUploadedFile(
        buf,
        getattr(f, "field_name", None) or "image",
        f"{base}{new_ext}",
        ctype,
        buf.getbuffer().nbytes,
        None,
    )


class ReportEvidenceForm(forms.ModelForm):
    class Meta:
        model = ReportEvidence
        fields = (
            "image",
            "order",
            "description",
            "display_size",
            "fit_mode",
            "show_in_print",
        )
        widgets = {
            "image": forms.ClearableFileInput(
                attrs={
                    "accept": "image/jpeg,image/png,image/webp",
                    "data-evidence-file": "",
                }
            ),
            "order": forms.HiddenInput(),
            "description": forms.TextInput(
                attrs={"placeholder": "مثال: صورة من تنفيذ النشاط", "maxlength": "220"}
            ),
            "display_size": forms.Select(),
            "fit_mode": forms.Select(),
            "show_in_print": forms.CheckboxInput(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance.pk and not self.is_bound:
            try:
                index = int(str(self.prefix).rsplit("-", 1)[-1])
            except (TypeError, ValueError):
                index = 0
            self.initial.setdefault("order", index + 1)
            self.initial.setdefault("show_in_print", True)

    def has_changed(self):
        """Do not turn an empty evidence slot into a required image form.

        The report editor renders four ready-to-use evidence rows.  Their
        ordering and ``show_in_print`` controls are posted even when the user
        did not choose an image, which made Django consider every row changed
        and reject the whole formset because ``image`` was missing.

        A new evidence row has no meaning without an uploaded image, so ignore
        the presentation-only values until a file is actually present.  Saved
        evidence rows keep the normal ModelForm change detection.
        """
        if not self.instance.pk:
            image = self.files.get(self.add_prefix("image"))
            if not image:
                return False
        return super().has_changed()

    def clean_image(self):
        image = self.cleaned_data.get("image")
        if not image:
            return image
        # الملفات الموجودة اجتازت المعالجة عند رفعها، فلا نعيد ضغطها عند كل تعديل.
        if not hasattr(image, "temporary_file_path") and not hasattr(image, "content_type"):
            return image
        try:
            image = _compress_image_upload(image, max_px=2000, quality=86)
            if image.size > 2 * 1024 * 1024:
                raise ValidationError("حجم الصورة بعد التحسين ما زال أكبر من 2MB.")
            from PIL import Image

            with Image.open(image) as normalized:
                self._normalized_dimensions = normalized.size
            image.seek(0)
            return image
        except ValidationError:
            raise
        except Exception as exc:
            _degraded("forms.compress_report_evidence", error=type(exc).__name__)
            raise ValidationError(
                "تعذر تجهيز الصورة. جرّب ملف JPG أو PNG أو WebP صالحًا."
            ) from exc

    def save(self, commit=True):
        instance = super().save(commit=False)
        dimensions = getattr(self, "_normalized_dimensions", None)
        if dimensions:
            instance.width_px, instance.height_px = dimensions
        if commit:
            instance.save()
            self.save_m2m()
        return instance


class BaseReportEvidenceFormSet(BaseInlineFormSet):
    """يحفظ إعادة الترتيب دون اصطدام بالقيد الفريد أثناء تبديل موضعين."""

    def clean(self):
        super().clean()
        if any(self.errors):
            return
        active_forms = [
            form
            for form in self.forms
            if form.cleaned_data
            and not form.cleaned_data.get("DELETE")
            and (form.cleaned_data.get("image") or getattr(form.instance.image, "name", ""))
        ]
        active_forms.sort(key=lambda form: (form.cleaned_data.get("order") or 999, form.prefix))
        for order, form in enumerate(active_forms, start=1):
            form.cleaned_data["order"] = order
            form.instance.order = order

    def save(self, commit=True):
        if not commit:
            return super().save(commit=False)
        with transaction.atomic():
            if self.instance.pk:
                self.instance.evidences.update(order=models.F("order") + 100)
            saved = []
            active_forms = []
            for form in self.forms:
                if not getattr(form, "cleaned_data", None):
                    continue
                if form.cleaned_data.get("DELETE"):
                    if form.instance.pk:
                        form.instance.delete()
                    continue
                image = form.cleaned_data.get("image") or getattr(form.instance.image, "name", "")
                if image:
                    active_forms.append(form)
            active_forms.sort(key=lambda form: (form.cleaned_data.get("order") or 999, form.prefix))
            for order, form in enumerate(active_forms, start=1):
                obj = form.save(commit=False)
                obj.report = self.instance
                obj.order = order
                obj.save()
                saved.append(obj)
            return saved


ReportEvidenceFormSet = forms.inlineformset_factory(
    Report,
    ReportEvidence,
    form=ReportEvidenceForm,
    formset=BaseReportEvidenceFormSet,
    fields=("image", "order", "description", "display_size", "fit_mode", "show_in_print"),
    extra=1,
    can_delete=True,
    max_num=8,
    validate_max=True,
)


# ==============================
# 📌 نموذج التقرير العام
# ==============================
class ReportForm(forms.ModelForm):
    """
    يعتمد اعتمادًا كاملاً على ReportType (ديناميكي من قاعدة البيانات)
    ويستخدم قيمة code كقيمة ثابتة في الخيارات (to_field_name="code").
    """

    section_selection_enabled = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.HiddenInput(),
    )
    client_submission_id = forms.UUIDField(required=False, widget=forms.HiddenInput())

    class Meta:
        model = Report
        fields = [
            "title",
            "report_date",
            "day_name",
            "show_goal",
            "goal",
            "show_details",
            "idea",
            "show_implementation",
            "implementation_method",
            "show_results",
            "results",
            "show_recommendations",
            "recommendations",
            "show_beneficiaries",
            "beneficiaries_count",
            "category",
            "evidence_page_mode",
        ]
        widgets = {
            "title": forms.TextInput(
                attrs={
                    "class": "input",
                    "placeholder": "العنوان / البرنامج",
                    "maxlength": "255",
                    "autocomplete": "off",
                }
            ),
            "report_date": forms.DateInput(attrs={"class": "input", "type": "date"}),
            "day_name": forms.TextInput(attrs={"class": "input", "readonly": "readonly"}),
            "beneficiaries_count": forms.NumberInput(attrs={"class": "input", "min": "0", "inputmode": "numeric"}),
            "goal": forms.Textarea(attrs={"class": "textarea", "rows": 3, "placeholder": "ما الهدف الذي يسعى النشاط أو البرنامج إلى تحقيقه؟"}),
            "idea": forms.Textarea(
                attrs={
                    "class": "textarea",
                    "rows": 5,
                    "placeholder": "اكتب ملخصًا واضحًا لما تم تنفيذه وأبرز تفاصيله",
                    "maxlength": str(REPORT_DETAILS_MAX_LENGTH),
                    "data-recommended-length": str(REPORT_DETAILS_RECOMMENDED_LENGTH),
                    "data-max-length": str(REPORT_DETAILS_MAX_LENGTH),
                    "aria-describedby": "report-details-guidance report-details-status",
                }
            ),
            "implementation_method": forms.Textarea(attrs={"class": "textarea", "rows": 4, "placeholder": "وضح الخطوات والإجراءات وطريقة تنفيذ النشاط"}),
            "results": forms.Textarea(attrs={"class": "textarea", "rows": 4, "placeholder": "اذكر النتائج والمخرجات التي تحققت"}),
            "recommendations": forms.Textarea(attrs={"class": "textarea", "rows": 4, "placeholder": "أضف التوصيات أو فرص التحسين المستقبلية"}),
            "show_goal": forms.CheckboxInput(attrs={"class": "ar-section-checkbox"}),
            "show_details": forms.CheckboxInput(attrs={"class": "ar-section-checkbox"}),
            "show_implementation": forms.CheckboxInput(attrs={"class": "ar-section-checkbox"}),
            "show_results": forms.CheckboxInput(attrs={"class": "ar-section-checkbox"}),
            "show_recommendations": forms.CheckboxInput(attrs={"class": "ar-section-checkbox"}),
            "show_beneficiaries": forms.CheckboxInput(attrs={"class": "ar-section-checkbox"}),
            "evidence_page_mode": forms.Select(attrs={"class": "form-select"}),
        }

    def __init__(self, *args, **kwargs):
        active_school = kwargs.pop("active_school", None)
        self.gender_labels = school_gender_labels(active_school)

        # توافق الطلبات القديمة التي سبقت واجهة اختيار البنود.
        bound_data = args[0] if args else kwargs.get("data")
        if bound_data is not None and "section_selection_enabled" not in bound_data:
            data = bound_data.copy()
            data["show_details"] = "on"
            data["show_beneficiaries"] = "on"
            if args:
                args = (data, *args[1:])
            else:
                kwargs["data"] = data

        super().__init__(*args, **kwargs)

        if not self.is_bound:
            current_key = getattr(self.instance, "submission_key", None)
            self.fields["client_submission_id"].initial = current_key or uuid.uuid4()

        current_category_id = getattr(self.instance, "category_id", None)
        qs = ReportType.objects.filter(
            Q(is_active=True) | Q(pk=current_category_id)
        ).order_by("order", "name")
        if active_school is not None and hasattr(ReportType, "school"):
            qs = qs.filter(school=active_school)

        self.fields["category"] = forms.ModelChoiceField(
            label="نوع التقرير",
            queryset=qs,
            required=True,
            empty_label="— اختر نوع التقرير —",
            to_field_name="code",
            widget=forms.Select(attrs={"class": "form-select"}),
        )
        # ``BaseModelForm`` builds ``self.initial`` before the field above is
        # replaced.  At that point Django stores the category's numeric PK,
        # while this field deliberately posts ``ReportType.code``.  On edit
        # pages the mismatch left the select on its empty option, so every
        # update (including a newly attached image) failed validation.
        if not self.is_bound and current_category_id:
            try:
                self.initial["category"] = self.instance.category.code
            except ReportType.DoesNotExist:
                self.initial["category"] = None
        self.fields["beneficiaries_count"].label = f"عدد {self.gender_labels['beneficiaries_object']}"
        # تطبيقات أو تبويبات فُتحت قبل إضافة الخيار لا ترسله في POST؛ الوضع
        # التلقائي هو التوافق الآمن ولا ينبغي أن يمنع حفظ تقرير مكتمل.
        self.fields["evidence_page_mode"].required = False
        self.fields["evidence_page_mode"].widget = forms.HiddenInput()
        self.fields["evidence_page_mode"].initial = Report.EvidencePageMode.INLINE

    def clean_evidence_page_mode(self):
        # صفحة التقرير الرسمية موحّدة: التفاصيل والشواهد والتواقيع في ورقة
        # واحدة.  نحول أيضاً القيم القديمة ``auto`` و``separate`` إلى inline
        # عند أول حفظ تالٍ للتقرير.
        return Report.EvidencePageMode.INLINE

    def clean_beneficiaries_count(self):
        val = self.cleaned_data.get("beneficiaries_count")
        if val is None:
            return val
        if val < 0:
            raise ValidationError(f"عدد {self.gender_labels['beneficiaries_object']} لا يمكن أن يكون سالبًا.")
        return val

    def clean_idea(self):
        value = self.cleaned_data.get("idea")
        if value and len(value) > REPORT_DETAILS_MAX_LENGTH:
            raise ValidationError(report_details_length_error())
        return value

    def clean(self):
        cleaned = super().clean()

        section_fields = (
            ("show_goal", "goal", "الهدف"),
            ("show_details", "idea", "تفاصيل التقرير"),
            ("show_implementation", "implementation_method", "آلية التنفيذ"),
            ("show_results", "results", "النتائج"),
            ("show_recommendations", "recommendations", "التوصيات"),
        )
        selected = [flag for flag, _field, _label in section_fields if cleaned.get(flag)]
        if cleaned.get("show_beneficiaries"):
            selected.append("show_beneficiaries")

        if not selected:
            raise ValidationError("اختر بندًا واحدًا على الأقل ليظهر في التقرير.")

        for flag, field_name, label in section_fields:
            if cleaned.get(flag) and not (cleaned.get(field_name) or "").strip():
                self.add_error(field_name, f"أدخل محتوى بند {label} أو ألغِ اختياره.")

        if cleaned.get("show_beneficiaries") and cleaned.get("beneficiaries_count") is None:
            self.add_error(
                "beneficiaries_count",
                f"أدخل عدد {self.gender_labels['beneficiaries_object']} أو ألغِ اختيار هذا البند.",
            )

        return cleaned

# ==============================
# 📌 نموذج إدارة المعلّم (إضافة/تعديل)
# ==============================
TEACHERS_DEPT_SLUGS = {"teachers", "معلمين", "المعلمين"}

class TeacherForm(forms.ModelForm):
    """
    إنشاء/تعديل معلّم:
    - إن كان القسم من أقسام "المعلمين" → الدور داخل القسم يقتصر على (معلم) فقط.
    - بقية الأقسام: (مسؤول القسم | موظف/معلم).
    - يضبط Teacher.role تلقائيًا.
    - ينشئ/يحدّث DepartmentMembership.
    """
    password = forms.CharField(
        label="كلمة المرور",
        required=False,
        strip=False,
        widget=forms.PasswordInput(attrs={
            "class": "form-control",
            "placeholder": "اتركه فارغًا للإبقاء على الحالية",
            "autocomplete": "new-password",
        }),
    )

    department = forms.ModelChoiceField(
        label="القسم",
        queryset=Department.objects.none(),
        required=True,
        empty_label="— اختر القسم —",
        to_field_name="slug",
        widget=forms.Select(attrs={"class": "form-select", "id": "id_department"}),
    )

    membership_role = forms.ChoiceField(
        label="الدور داخل القسم",
        choices=[],  # تُضبط ديناميكيًا في __init__
        required=True,
        widget=forms.Select(attrs={"class": "form-select", "id": "id_membership_role"}),
    )

    phone = forms.CharField(
        label="رقم الجوال",
        min_length=10, max_length=10,
        validators=[sa_phone],
        widget=forms.TextInput(attrs={
            "class": "form-control", "placeholder": "05XXXXXXXX", "maxlength": "10",
            "inputmode": "numeric", "pattern": r"0\d{9}", "autocomplete": "off"
        }),
    )
    national_id = forms.CharField(
        label="رقم الهوية الوطنية",
        min_length=10, max_length=10, required=False,
        validators=[digits10],
        widget=forms.TextInput(attrs={
            "class": "form-control", "placeholder": "رقم الهوية (10 أرقام)",
            "maxlength": "10", "inputmode": "numeric", "pattern": r"\d{10}",
            "autocomplete": "off"
        }),
    )

    class Meta:
        model = Teacher
        fields = ["name", "phone", "national_id", "is_active", "department", "membership_role"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control", "placeholder": "الاسم الكامل", "maxlength": "150"}),
        }

    ROLE_CHOICES_ALL = (
        (DepartmentMembership.OFFICER, "مسؤول القسم"),
        (DepartmentMembership.TEACHER, "موظف/معلم"),
    )
    ROLE_CHOICES_TEACHERS_ONLY = (
        (DepartmentMembership.TEACHER, "معلم"),
    )

    def _current_department_slug(self) -> Optional[str]:
        if self.is_bound:
            val = (self.data.get("department") or "").strip()
            if val:
                return val.lower()

        init_dep = (self.initial.get("department") or "")
        if init_dep:
            return str(init_dep).lower()

        dep_slug = None
        if getattr(self.instance, "pk", None):
            try:
                memb = self.instance.dept_memberships.select_related("department").first()  # type: ignore[attr-defined]
                if memb and getattr(memb.department, "slug", None):
                    dep_slug = memb.department.slug
            except Exception:
                dep_slug = None
            if not dep_slug:
                dep_slug = getattr(getattr(self.instance, "role", None), "slug", None)

        return (dep_slug or "").lower() or None

    def __init__(self, *args, **kwargs):
        active_school = kwargs.pop("active_school", None)
        super().__init__(*args, **kwargs)

        # حصر الأقسام على المدرسة النشطة فقط
        if Department is not None:
            dept_qs = Department.objects.filter(is_active=True)
            if hasattr(Department, "school"):
                if active_school is not None:
                    dept_qs = dept_qs.filter(school=active_school)
                elif _has_multi_active_schools():
                    # لا نعرض أقسامًا عشوائية عبر مدارس متعددة بدون active_school
                    dept_qs = Department.objects.none()
            self.fields["department"].queryset = dept_qs.order_by("name") if hasattr(dept_qs, "order_by") else dept_qs
        dep_slug = self._current_department_slug()
        if dep_slug and dep_slug in {s.lower() for s in TEACHERS_DEPT_SLUGS}:
            self.fields["membership_role"].choices = self.ROLE_CHOICES_TEACHERS_ONLY
            self.initial.setdefault("membership_role", DepartmentMembership.TEACHER)
        else:
            self.fields["membership_role"].choices = self.ROLE_CHOICES_ALL

    def clean_national_id(self):
        nid = (self.cleaned_data.get("national_id") or "").strip()
        if nid:
            if not nid.isdigit() or len(nid) != 10:
                raise ValidationError("رقم الهوية يجب أن يتكون من 10 أرقام.")
        return nid or None

    def save(self, commit: bool = True):
        instance: Teacher = super().save(commit=False)
        new_pwd = (self.cleaned_data.get("password") or "").strip()
        dep: Optional[Department] = self.cleaned_data.get("department")

        if new_pwd:
            instance.set_password(new_pwd)
        elif self.instance and self.instance.pk:
            instance.password = self.instance.password  # إبقاء كلمة المرور

        if dep and dep.slug in TEACHERS_DEPT_SLUGS:
            role_in_dept = DepartmentMembership.TEACHER
        else:
            role_in_dept = self.cleaned_data.get("membership_role") or DepartmentMembership.TEACHER

        with transaction.atomic():
            instance.save()

            if dep:
                DepartmentMembership.objects.update_or_create(
                    department=dep,
                    teacher=instance,
                    defaults={"role_type": role_in_dept},
                )

        return instance


class TeacherCreateForm(forms.ModelForm):
    """إنشاء حساب منسوب واختيار تكليفه المدرسي الأول.

    - يعرض الكتالوج نفسه الذي تستخدمه شاشة الأدوار والصلاحيات.
    - لا ينشئ DepartmentMembership نهائيًا.
    - كتابة SchoolMembership موحّدة في خدمة الإسناد ويستدعيها الـ view.
    """

    phone = forms.CharField(
        label="رقم الجوال",
        min_length=10,
        max_length=10,
        validators=[sa_phone],
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "05XXXXXXXX",
                "maxlength": "10",
                "inputmode": "numeric",
                "pattern": r"0\d{9}",
                "autocomplete": "off",
            }
        ),
    )

    national_id = forms.CharField(
        label="رقم الهوية الوطنية",
        min_length=10,
        max_length=10,
        required=False,
        validators=[digits10],
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "رقم الهوية (10 أرقام)",
                "maxlength": "10",
                "inputmode": "numeric",
                "pattern": r"\d{10}",
                "autocomplete": "off",
            }
        ),
    )

    class Meta:
        model = Teacher
        fields = ["name", "phone", "national_id", "is_active"]
        widgets = {
            "name": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "الاسم الكامل", "maxlength": "150"}
            ),
        }

    job_title = forms.ChoiceField(
        label="الدور في المدرسة",
        required=True,
        choices=(),
        widget=forms.RadioSelect,
        error_messages={"invalid_choice": "اختر دورًا معتمدًا من الخيارات الظاهرة."},
    )
    keep_teaching_role = forms.BooleanField(
        label="لديه نصاب تدريسي أيضًا",
        required=False,
        help_text="ينشئ له مساحة المعلّم وملف الإنجاز بجانب دوره الأساسي.",
    )
    lab_kind = forms.ChoiceField(
        label="المختبر",
        choices=(("", "— اختر المختبر —"),) + tuple(LabKind.choices),
        required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
        help_text="مطلوب لمحضر المختبر، ومستقل عن الأقسام المرتبطة بالتقارير.",
    )

    def __init__(self, *args, **kwargs):
        self._active_school = kwargs.pop("active_school", None)
        super().__init__(*args, **kwargs)
        self.fields["job_title"].choices = assignment_choices(self._active_school)
        self.assignment_cards = assignment_cards(self._active_school)
        self.initial.setdefault("job_title", SchoolMembership.RoleType.TEACHER)

    def clean(self):
        cleaned = super().clean()
        code = cleaned.get("job_title")
        if not code:
            return cleaned
        assignment = get_assignment(code)
        if not assignment.supports_teaching_load:
            cleaned["keep_teaching_role"] = False
        if code == SchoolMembership.JobTitle.LAB_TECH:
            if not cleaned.get("lab_kind"):
                self.add_error(
                    "lab_kind",
                    "حدّد مختبر العلوم أو مختبر الحاسب الآلي قبل إنشاء المحضّر.",
                )
        else:
            cleaned["lab_kind"] = ""
        return cleaned

    def clean_national_id(self):
        nid = (self.cleaned_data.get("national_id") or "").strip()
        if nid:
            if not nid.isdigit() or len(nid) != 10:
                raise ValidationError("رقم الهوية يجب أن يتكون من 10 أرقام.")
        return nid or None

    def save(self, commit: bool = True):
        instance: Teacher = super().save(commit=False)
        # كلمة المرور المؤقتة هي رقم الجوال لتسهيل الدخول الأول، ويجبر
        # النظام المستخدم على تغييرها فور تسجيل الدخول للمرة الأولى.
        instance.set_password(self.cleaned_data["phone"])

        if commit:
            instance.save()
        return instance


class TeacherEditForm(forms.ModelForm):
    """نموذج مبسّط لتعديل بيانات المعلّم فقط (بدون أي تكليفات).

    - لا يعرض/لا يطلب قسم أو دور داخل قسم.
    - لا ينشئ/لا يحدّث DepartmentMembership.
    - كلمة المرور اختيارية: إن تُركت فارغة تبقى الحالية.
    """

    password = forms.CharField(
        label="كلمة المرور",
        required=False,
        strip=False,
        widget=forms.PasswordInput(
            attrs={
                "class": "form-control",
                "placeholder": "اتركه فارغًا للإبقاء على كلمة المرور الحالية",
                "autocomplete": "new-password",
            }
        ),
    )

    phone = forms.CharField(
        label="رقم الجوال",
        min_length=10,
        max_length=10,
        validators=[sa_phone],
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "05XXXXXXXX",
                "maxlength": "10",
                "inputmode": "numeric",
                "pattern": r"0\d{9}",
                "autocomplete": "off",
            }
        ),
    )

    national_id = forms.CharField(
        label="رقم الهوية الوطنية",
        min_length=10,
        max_length=10,
        required=False,
        validators=[digits10],
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "رقم الهوية (10 أرقام)",
                "maxlength": "10",
                "inputmode": "numeric",
                "pattern": r"\d{10}",
                "autocomplete": "off",
            }
        ),
    )

    class Meta:
        model = Teacher
        fields = ["name", "phone", "national_id", "is_active"]
        widgets = {
            "name": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "الاسم الكامل", "maxlength": "150"}
            ),
        }

    job_title = forms.ChoiceField(
        label="الدور",
        required=False,
        choices=SchoolMembership.JobTitle.choices,
        widget=forms.Select(attrs={"class": "form-control"}),
        help_text="(بنفس الصلاحيات) — للاسم المعروض داخل المدرسة فقط.",
    )

    def __init__(self, *args, **kwargs):
        self._active_school = kwargs.pop("active_school", None)
        super().__init__(*args, **kwargs)
        self.fields["job_title"].choices = _school_job_title_choices(self._active_school)

        # initial job title from membership for active school (if available)
        try:
            if self._active_school is not None and self.instance and self.instance.pk:
                m = SchoolMembership.objects.filter(
                    school=self._active_school,
                    teacher=self.instance,
                    role_type__in=SchoolMembership.STAFF_ROLES,
                ).only("job_title").first()
                if m is not None and getattr(m, "job_title", None):
                    self.fields["job_title"].initial = m.job_title
        except Exception:
            # المسمّى الوظيفي يظهر فارغاً فيُحفظ فارغاً — بيانات تضيع بصمت.
            _degraded("forms.load_job_title", teacher_id=getattr(self.instance, "pk", None))

    def clean_national_id(self):
        nid = (self.cleaned_data.get("national_id") or "").strip()
        if nid:
            if not nid.isdigit() or len(nid) != 10:
                raise ValidationError("رقم الهوية يجب أن يتكون من 10 أرقام.")
        return nid or None

    def save(self, commit: bool = True):
        instance: Teacher = super().save(commit=False)
        new_pwd = (self.cleaned_data.get("password") or "").strip()

        if new_pwd:
            instance.set_password(new_pwd)
        elif self.instance and getattr(self.instance, "pk", None):
            instance.password = self.instance.password

        if commit:
            instance.save()

            try:
                if self._active_school is not None and instance.pk:
                    jt = (self.cleaned_data.get("job_title") or "").strip() or None
                    if jt:
                        SchoolMembership.objects.filter(
                            school=self._active_school,
                            teacher=instance,
                            role_type__in=SchoolMembership.STAFF_ROLES,
                        ).update(job_title=jt)
            except Exception:
                # المدير يرى «تم الحفظ» والمسمّى لم يُحفظ.
                _degraded("forms.save_job_title", teacher_id=getattr(instance, "pk", None))
        return instance


class ManagerCreateForm(forms.ModelForm):
    """نموذج مبسّط لإنشاء مدير مدرسة:

    - لا يطلب تحديد قسم أو دور داخل القسم.
    - يضبط كلمة المرور للمستخدم الجديد.
    - يُستخدم مع منطق SchoolMembership في views لربط المدير بالمدارس.
    """

    password = forms.CharField(
        label="كلمة المرور",
        required=True,
        strip=False,
        widget=forms.PasswordInput(
            attrs={
                "class": "form-control",
                "placeholder": "كلمة المرور للحساب الجديد",
                "autocomplete": "new-password",
            }
        ),
    )

    phone = forms.CharField(
        label="رقم الجوال",
        min_length=10,
        max_length=10,
        validators=[sa_phone],
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "05XXXXXXXX",
                "maxlength": "10",
                "inputmode": "numeric",
                "pattern": r"0\d{9}",
                "autocomplete": "off",
            }
        ),
    )

    email = forms.EmailField(
        label="البريد الإلكتروني",
        required=False,
        widget=forms.EmailInput(
            attrs={
                "class": "form-control",
                "placeholder": "manager@school.edu.sa",
                "autocomplete": "email",
            }
        ),
    )

    national_id = forms.CharField(
        label="رقم الهوية الوطنية",
        min_length=10,
        max_length=10,
        required=False,
        validators=[digits10],
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "رقم الهوية (10 أرقام)",
                "maxlength": "10",
                "inputmode": "numeric",
                "pattern": r"\d{10}",
                "autocomplete": "off",
            }
        ),
    )

    class Meta:
        model = Teacher
        fields = ["name", "phone", "email", "national_id", "is_active"]
        widgets = {
            "name": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "الاسم الكامل", "maxlength": "150"}
            ),
        }

    def __init__(self, *args, **kwargs):
        self._require_email = bool(kwargs.pop("require_email", False))
        super().__init__(*args, **kwargs)

        if self._require_email:
            self.fields["email"].required = True
            self.fields["email"].widget.attrs["required"] = "required"
            self.fields["email"].help_text = "إجباري لمدير المدرسة لاستعادة كلمة المرور وتنبيهات الأمان."

        if self.instance and self.instance.pk:
            self.fields["password"].required = False
            self.fields["password"].widget.attrs["placeholder"] = "اتركها فارغة للإبقاء على الحالية"

    def clean_email(self):
        email = (self.cleaned_data.get("email") or "").strip()
        if self._require_email and not email:
            raise ValidationError("البريد الإلكتروني إلزامي لمدير المدرسة.")
        return email

    def clean_national_id(self):
        nid = (self.cleaned_data.get("national_id") or "").strip()
        if nid:
            if not nid.isdigit() or len(nid) != 10:
                raise ValidationError("رقم الهوية يجب أن يتكون من 10 أرقام.")
        return nid or None

    def save(self, commit=True):
        instance = super().save(commit=False)
        password = self.cleaned_data.get("password")
        if password:
            instance.set_password(password)
        if commit:
            instance.save()
        return instance


class PlatformSchoolNotificationForm(forms.Form):
    target_scope = forms.ChoiceField(
        label="نطاق المدارس",
        required=True,
        choices=(
            ("current", "المدرسة الحالية"),
            ("selected", "مدارس محددة"),
            ("all", "كل المدارس ضمن صلاحياتي"),
        ),
        initial="current",
        widget=forms.RadioSelect,
    )
    selected_schools = forms.ModelMultipleChoiceField(
        label="المدارس المحددة",
        queryset=School.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text="اختر مدرسة واحدة أو أكثر عند استخدام نطاق مدارس محددة.",
    )
    title = forms.CharField(
        label="العنوان",
        required=False,
        max_length=120,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "عنوان مختصر (اختياري)"}),
    )
    message = forms.CharField(
        label="الرسالة",
        required=True,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 5, "placeholder": "اكتب نص الإشعار هنا…"}),
    )
    is_important = forms.BooleanField(label="مهم؟", required=False)

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop("user", None)
        self.active_school = kwargs.pop("active_school", None)
        super().__init__(*args, **kwargs)

        from .permissions import platform_allowed_schools_qs

        allowed_schools = platform_allowed_schools_qs(self.user).order_by("name", "id")
        self.fields["selected_schools"].queryset = allowed_schools

        scope_choices = []
        if self.active_school is not None and allowed_schools.filter(pk=self.active_school.pk).exists():
            scope_choices.append(("current", f"المدرسة الحالية: {self.active_school.name}"))
            self.fields["target_scope"].initial = "current"
        else:
            self.fields["target_scope"].initial = "selected"

        scope_choices.extend(
            (
                ("selected", "مدارس محددة"),
                ("all", "كل المدارس ضمن صلاحياتي"),
            )
        )
        self.fields["target_scope"].choices = scope_choices

    def clean(self):
        cleaned = super().clean()
        scope = cleaned.get("target_scope") or "selected"
        selected_schools = cleaned.get("selected_schools")

        from .permissions import platform_allowed_schools_qs

        allowed_schools = platform_allowed_schools_qs(self.user)
        if not allowed_schools.exists():
            raise ValidationError("لا توجد مدارس متاحة ضمن صلاحياتك.")

        if scope == "current":
            active_school = self.active_school
            if active_school is None:
                raise ValidationError("لا توجد مدرسة حالية. اختر مدارس محددة أو كل المدارس.")
            if not allowed_schools.filter(pk=active_school.pk).exists():
                raise ValidationError("المدرسة الحالية خارج نطاق صلاحياتك.")

        if scope == "selected" and not selected_schools:
            self.add_error("selected_schools", "اختر مدرسة واحدة على الأقل.")

        return cleaned

    def target_schools(self):
        scope = self.cleaned_data.get("target_scope") or "selected"

        from .permissions import platform_allowed_schools_qs

        allowed_schools = platform_allowed_schools_qs(self.user).order_by("name", "id")
        if scope == "all":
            return allowed_schools
        if scope == "current" and self.active_school is not None:
            return allowed_schools.filter(pk=self.active_school.pk)
        return self.cleaned_data.get("selected_schools", School.objects.none()).order_by("name", "id")


class PrivateCommentForm(forms.Form):
    body = forms.CharField(
        label="تعليق للمعلّم",
        required=True,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 3, "placeholder": "اكتب تعليقًا يظهر للمعلم فقط…"}),
    )

# ==============================
# 📌 تذاكر — إنشاء/إجراءات/ملاحظات
# ==============================

# ==== داخل reports/forms.py (استبدل تعريف TicketCreateForm فقط بهذا) ====
from io import BytesIO
from django.core.files.uploadedfile import InMemoryUploadedFile

class MultiImageInput(forms.ClearableFileInput):
    """عنصر إدخال يسمح باختيار عدة صور."""
    allow_multiple_selected = True

class MultiFileField(forms.FileField):
    """
    حقل ملفات متعدد:
    - يقبل [] بدون أخطاء عندما لا تُرفع صور.
    - يعيد list[UploadedFile] عند وجود صور.
    """
    def to_python(self, data):
        if not data:
            return []
        # في حال مر ملف مفرد من متصفح قديم
        if not isinstance(data, (list, tuple)):
            return [data]
        return list(data)

    def validate(self, data):
        # لا نريد رسالة "لم يتم إرسال ملف..." عند عدم وجود صور
        if self.required and not data:
            raise forms.ValidationError(self.error_messages["required"], code="required")
        # أي تحقق إضافي خاص بالحقل نفسه يمكن وضعه هنا (نحن نتحقق لاحقًا في form.clean)

class TicketCreateForm(forms.ModelForm):
    """
    إنشاء تذكرة جديدة مع رفع حتى 4 صور (JPG/PNG/WebP) بحجم أقصى 5MB للصورة.
    - department يُرسل slug (to_field_name="slug")
    - recipients يُبنى ديناميكيًا (اختيار متعدد)
    - images اختيارية ومتعددة (MultiFileField)
    """

    department = forms.ModelChoiceField(
        label="القسم",
        queryset=Department.objects.none(),
        required=True,
        empty_label="— اختر القسم —",
        to_field_name="slug",
        widget=forms.Select(attrs={"class": "form-select", "id": "id_department"}),
    )

    recipients = forms.ModelMultipleChoiceField(
        label="المستلمون",
        queryset=Teacher.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple(attrs={"id": "id_recipients"}),
        help_text="يمكن اختيار أكثر من مستلم واحد.",
    )

    # ✅ حقل متعدد ينسجم مع الـ multiple في القالب
    images = MultiFileField(
        label="الصور (حتى 4)",
        required=False,
        widget=MultiImageInput(attrs={"accept": "image/*", "multiple": True, "id": "id_images"}),
        help_text="حتى 4 صور، ‎JPG/PNG/WebP، الحد الأقصى لكل صورة 5MB.",
    )

    class Meta:
        model = Ticket
        fields = ["department", "recipients", "title", "body"]
        labels = {
            "title": "عنوان الطلب",
            "body": "تفاصيل الطلب",
        }
        widgets = {
            "title": forms.TextInput(attrs={
                "class": "input", "placeholder": "عنوان الطلب", "maxlength": "255", "autocomplete": "off"
            }),
            "body": forms.Textarea(attrs={"class": "textarea", "rows": 4, "placeholder": "تفاصيل الطلب"}),
        }

    def __init__(self, *args, **kwargs):
        kwargs.pop("user", None)  # يُمرَّر في save
        active_school = kwargs.pop("active_school", None)
        super().__init__(*args, **kwargs)

        self.active_school = active_school
        # الواجهة تضع علامة «مطلوب» لأن الطلب بلا وصف لا يستطيع القسم
        # تنفيذه. اجعل العقد البرمجي مطابقاً للنص المرئي وللتحقق الخادمي.
        self.fields["body"].required = True
        self.fields["body"].widget.attrs["required"] = True

        # عزل الأقسام حسب المدرسة النشطة
        if Department is not None:
            dept_qs = Department.objects.filter(is_active=True)
            if hasattr(Department, "school"):
                if active_school is not None:
                    dept_qs = dept_qs.filter(school=active_school)
                elif _has_multi_active_schools():
                    dept_qs = Department.objects.none()
            self.fields["department"].queryset = dept_qs.order_by("name") if hasattr(dept_qs, "order_by") else dept_qs

        # تأكيد اختياريّة الصور (تحصين إضافي)
        self.fields["images"].required = False

        # بناء قائمة المستلمين حسب القسم
        dept_value = (self.data.get("department") or "").strip() if self.is_bound \
            else getattr(getattr(self.instance, "department", None), "slug", "") or ""
        base_qs = _teachers_for_dept(dept_value, active_school) if dept_value else Teacher.objects.none()
        self.fields["recipients"].queryset = base_qs

        # سنخزن النسخ المضغوطة بعد التحقق
        self._compressed_images: List[InMemoryUploadedFile] = []

    # ضغط صورة مع fallback
    def _compress_image(self, f, *, max_px=1600, quality=85) -> InMemoryUploadedFile:
        from PIL import Image
        img = Image.open(f)
        has_alpha = img.mode in ("RGBA", "LA", "P")
        img = img.convert("RGBA" if has_alpha else "RGB")
        if max(img.size) > max_px:
            img.thumbnail((max_px, max_px), Image.LANCZOS)

        buf = BytesIO()
        try:
            img.save(buf, format="WEBP", quality=quality, optimize=True)
            new_ext, ctype = ".webp", "image/webp"
        except Exception:
            buf = BytesIO()
            fmt = "PNG" if has_alpha else "JPEG"
            save_kwargs = {"optimize": True}
            if fmt == "JPEG":
                save_kwargs["quality"] = quality
            img.save(buf, format=fmt, **save_kwargs)
            new_ext = ".png" if has_alpha else ".jpg"
            ctype = "image/png" if has_alpha else "image/jpeg"
        buf.seek(0)

        base = os.path.splitext(getattr(f, "name", "image"))[0]
        return InMemoryUploadedFile(buf, "images", f"{base}{new_ext}", ctype, buf.getbuffer().nbytes, None)

    def clean(self):
        cleaned = super().clean()

        dept: Optional[Department] = cleaned.get("department")
        recipients = list(cleaned.get("recipients") or [])

        if not dept:
            self.add_error("department", "الرجاء اختيار القسم.")

        # المستلمون: نطلب على الأقل مستلمًا واحدًا إذا وُجدت خيارات
        if dept:
            qs = self.fields["recipients"].queryset
            if qs.count() > 0 and not recipients:
                self.add_error("recipients", "يرجى اختيار مستلم واحد على الأقل.")

            # تحصين: كل المستلمين يجب أن يكونوا ضمن QuerySet القسم
            if recipients:
                allowed_ids = set(qs.values_list("id", flat=True)) if hasattr(qs, "values_list") else set()
                bad = [t for t in recipients if getattr(t, "id", None) not in allowed_ids]
                if bad:
                    self.add_error("recipients", "يوجد مستلم/مستلمون لا ينتمون إلى هذا القسم.")

        # الآن images هي list[UploadedFile] قادمة من الحقل نفسه
        files = self.cleaned_data.get("images") or []
        if files:
            if len(files) > 4:
                self.add_error("images", "الحد الأقصى 4 صور.")
            ok_ext = {".jpg", ".jpeg", ".png", ".webp"}
            for f in files:
                name = (getattr(f, "name", "") or "").lower()
                ext = os.path.splitext(name)[1]
                ctype = (getattr(f, "content_type", "") or "").lower()

                if getattr(f, "size", 0) > 5 * 1024 * 1024:
                    self.add_error("images", f"({name}) حجم الصورة أكبر من 5MB.")
                    break
                if not (ctype.startswith("image/") and ext in ok_ext):
                    self.add_error("images", f"({name}) يُسمح فقط بصور JPG/PNG/WebP.")
                    break

            if not self.errors.get("images"):
                self._compressed_images = [self._compress_image(f) for f in files]

        return cleaned

    def save(self, commit: bool = True, user: Optional[Teacher] = None):
        obj: Ticket = super().save(commit=False)

        # تعيين المُنشئ لأول مرة
        if user is not None and not obj.pk:
            obj.creator = user
        if self.active_school is not None and hasattr(obj, "school_id"):
            obj.school = self.active_school

        # حالة افتراضية إن وُجدت في الموديل
        if not getattr(obj, "status", None):
            with soft_fail("forms.default_ticket_status"):
                obj.status = Ticket.Status.OPEN  # type: ignore[attr-defined]

        # تعيين assignee كمرجع/مسؤول رئيسي للتوافق الخلفي (أول مستلم)
        try:
            recipients = list(self.cleaned_data.get("recipients") or [])
        except Exception:
            recipients = []
        if recipients:
            obj.assignee = recipients[0]

        if commit:
            obj.save()

            # حفظ المستلمين (ManyToMany through)
            if recipients:
                try:
                    obj.recipients.set(recipients)
                except Exception:
                    # fallback آمن عبر through model (في حال قيود بيئية)
                    from .models import TicketRecipient
                    TicketRecipient.objects.bulk_create(
                        [TicketRecipient(ticket=obj, teacher=t) for t in recipients],
                        ignore_conflicts=True,
                    )

            # حفظ الصور (إن وُجدت)
            if self._compressed_images:
                from .models import TicketImage
                for f in self._compressed_images:
                    TicketImage.objects.create(ticket=obj, image=f)

        return obj

class TicketActionForm(forms.Form):
    status = forms.ChoiceField(
        choices=Ticket.Status.choices,
        required=False,
        widget=forms.Select(attrs={"class": "input"}),
        label="تغيير الحالة",
    )
    note = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 3, "class": "textarea", "placeholder": "اكتب ملاحظة (تظهر للمرسل)"}),
        label="ملاحظة",
    )

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("status") and not (cleaned.get("note") or "").strip():
            raise forms.ValidationError("أدخل ملاحظة أو غيّر الحالة.")
        return cleaned

class TicketNoteForm(forms.ModelForm):
    class Meta:
        model = TicketNote
        fields = ["body", "is_public"]
        widgets = {
            "body": forms.Textarea(attrs={"rows": 3, "class": "textarea", "placeholder": "أضف ملاحظة"}),
        }


class TicketNoteEditForm(forms.ModelForm):
    class Meta:
        model = TicketNote
        fields = ["body"]
        widgets = {
            "body": forms.Textarea(attrs={"rows": 4, "class": "textarea", "placeholder": "عدّل ملاحظتك"}),
        }

# ==============================
# 📌 نموذج الطلب التراثي (اختياري)
# ==============================
if HAS_REQUEST_TICKET and RequestTicket is not None:

    class RequestTicketForm(forms.ModelForm):
        department = forms.ChoiceField(
            choices=[],
            required=True,
            widget=forms.Select(attrs={"class": "form-select"}),
            label="القسم",
        )
        assignee = forms.ModelChoiceField(
            queryset=Teacher.objects.none(),
            required=False,
            widget=forms.Select(attrs={"class": "form-select"}),
            label="المستلم",
        )

        class Meta:
            model = RequestTicket
            fields = ["department", "assignee", "title", "body", "attachment"]
            widgets = {
                "title": forms.TextInput(attrs={"class": "input", "placeholder": "عنوان مختصر", "maxlength": "200"}),
                "body": forms.Textarea(attrs={"class": "textarea", "rows": 5, "placeholder": "اكتب تفاصيل الطلب..."}),
            }

        def __init__(self, *args, **kwargs):
            kwargs.pop("user", None)
            active_school = kwargs.pop("active_school", None)
            super().__init__(*args, **kwargs)

            self.active_school = active_school

            # مصادر الاختيارات لقسم تراثي
            choices: List[Tuple[str, str]] = []
            try:
                field = RequestTicket._meta.get_field("department")
                model_choices = list(getattr(field, "choices", []))
                choices = [(v, l) for (v, l) in model_choices if v not in ("", None)]
            except Exception:
                if REQUEST_DEPARTMENTS:
                    choices = list(REQUEST_DEPARTMENTS)
            self.fields["department"].choices = [("", "— اختر القسم —")] + choices

            # إعداد assignee بحسب القسم
            if self.is_bound:
                dept_value = (self.data.get("department") or "").strip()
            elif getattr(self.instance, "pk", None):
                dept_value = getattr(self.instance, "department", None)
            else:
                dept_value = ""

            if dept_value:
                qs = _teachers_for_dept(dept_value, self.active_school)
                self.fields["assignee"].queryset = qs
                if qs.count() == 1 and not self.is_bound and not getattr(self.instance, "assignee_id", None):
                    self.initial["assignee"] = qs.first().pk
            else:
                self.fields["assignee"].queryset = Teacher.objects.none()

        def clean(self):
            cleaned = super().clean()
            dept = (cleaned.get("department") or "").strip()
            assignee: Optional[Teacher] = cleaned.get("assignee")
            if dept:
                qs = _teachers_for_dept(dept, getattr(self, "active_school", None))
                if qs.count() > 1 and assignee is None:
                    self.add_error("assignee", "يرجى اختيار الموظّف المستلم.")
                if assignee and not qs.filter(id=assignee.id).exists():
                    self.add_error("assignee", "الموظّف المختار لا ينتمي إلى هذا القسم.")
            return cleaned

else:
    # في حال إزالة النماذج التراثية من المشروع
    class RequestTicketForm(forms.Form):
        title = forms.CharField(disabled=True)
        body = forms.CharField(widget=forms.Textarea, disabled=True)

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.add_error(None, "نموذج الطلب التراثي غير مفعّل في هذا المشروع.")

# ==============================
# 📌 نموذج إدارة القسم (اختيار أنواع التقارير)
# ==============================
class DepartmentForm(forms.ModelForm):
    """
    نموذج إدارة القسم مع اختيار أنواع التقارير المسموح بها لهذا القسم.
    سيُزامن الدور تلقائيًا عبر إشعار m2m في models.py.
    """
    reporttypes = forms.ModelMultipleChoiceField(
        label="أنواع التقارير المرتبطة",
        queryset=ReportType.objects.filter(is_active=True).order_by("order", "name"),
        required=False,
        widget=forms.CheckboxSelectMultiple(
            attrs={"aria-label": "اختر نوع/أنواع التقارير للقسم"}
        ),
        help_text="المسؤولون عن هذا القسم سيشاهدون التقارير من هذه الأنواع فقط.",
    )

    class Meta:
        model = Department
        fields = ["name", "slug", "is_active", "reporttypes"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control", "maxlength": "120"}),
            # الكود (slug) يُولَّد تلقائيًا من الاسم — مخفي تمامًا عن المستخدم.
            "slug": forms.HiddenInput(),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def _slugify_english(self, text: str) -> str:
        # توليد slug ASCII (إنجليزي) حتى لو كان الاسم عربيًا.
        try:
            from unidecode import unidecode  # type: ignore

            text = unidecode(text or "")
        except ImportError:
            # fallback: بدون تحويل
            pass
        return slugify(text or "", allow_unicode=False)

    def clean_slug(self):
        slug = (self.cleaned_data.get("slug") or "").strip().lower()
        if not slug:
            slug = self._slugify_english(self.cleaned_data.get("name") or "")
        # fallback في حال كان الاسم غير قابل للتحويل
        if not slug:
            slug = "dept"

        # في وضع تعدد المدارس لا نسمح بفحص/إنشاء slug بدون مدرسة نشطة محددة
        active_school = getattr(self, "active_school", None)
        if active_school is None and hasattr(Department, "school") and _has_multi_active_schools():
            raise forms.ValidationError("فضلاً اختر مدرسة أولاً.")

        qs = Department.objects.filter(slug=slug)
        # حصر فحص التعارض داخل المدرسة النشطة عند توفرها
        if active_school is not None and hasattr(Department, "school"):
            qs = qs.filter(school=active_school)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("المعرّف (slug) مستخدم مسبقًا لقسم آخر.")
        return slug

    def __init__(self, *args, **kwargs):
        active_school = kwargs.pop("active_school", None)
        super().__init__(*args, **kwargs)

        self.active_school = active_school

        # الكود (slug) مخفي ويُولَّد تلقائيًا من الاسم في clean_slug،
        # لذا لا يجب أن يكون مطلوبًا على مستوى الحقل.
        if "slug" in self.fields:
            self.fields["slug"].required = False

        # حصر أنواع التقارير على المدرسة النشطة
        if ReportType is not None:
            rt_qs = ReportType.objects.filter(is_active=True).order_by("order", "name")
            if active_school is not None and hasattr(ReportType, "school"):
                rt_qs = rt_qs.filter(school=active_school)
            self.fields["reporttypes"].queryset = rt_qs


class ReportTypeForm(forms.ModelForm):
    """Report type form with an internal auto-generated code."""

    departments = forms.ModelMultipleChoiceField(
        label="الأقسام المستلمة",
        queryset=Department.objects.none(),
        required=False,
        help_text="تظهر تقارير هذا النوع للوكلاء المرتبطين بقسم واحد على الأقل من هذه الأقسام.",
        widget=forms.CheckboxSelectMultiple(
            attrs={"aria-label": "اختر الأقسام المستلمة لهذا النوع"}
        ),
    )
    approval_route = forms.ChoiceField(
        label="مسار الاعتماد",
        choices=ApprovalRoute.choices,
        help_text="حدّد الجهة التي تستلم التقرير بعد إرساله للمراجعة.",
        widget=forms.Select(attrs={"class": "smart-input"}),
    )

    class Meta:
        model = ReportType
        fields = [
            "name",
            "description",
            "approval_route",
            "departments",
            "order",
            "is_active",
        ]
        widgets = {
            "name": forms.TextInput(attrs={"class": "smart-input", "maxlength": "120"}),
            "description": forms.Textarea(attrs={"class": "smart-input", "rows": 6}),
            "order": forms.NumberInput(attrs={"class": "smart-input", "min": "0", "inputmode": "numeric"}),
            "is_active": forms.CheckboxInput(),
        }

    def __init__(self, *args, **kwargs):
        self.active_school = kwargs.pop("active_school", None)
        super().__init__(*args, **kwargs)
        departments = Department.objects.filter(is_active=True).order_by("name", "id")
        if self.active_school is not None:
            departments = departments.filter(school=self.active_school)
        elif hasattr(Department, "school"):
            departments = departments.none()
        self.fields["departments"].queryset = departments
        if self.instance.pk:
            self.fields["departments"].initial = self.instance.departments.all()

    def clean(self):
        cleaned = super().clean()
        route = cleaned.get("approval_route")
        departments = cleaned.get("departments")
        if route in {ApprovalRoute.VIA_DEPUTY, ApprovalRoute.DEPUTY_FINAL} and not departments:
            self.add_error(
                "departments",
                "اختر قسمًا واحدًا على الأقل حتى يعرف النظام أي وكيل يستلم التقرير.",
            )
        return cleaned

    def _save_m2m(self):
        super()._save_m2m()
        self.instance.departments.set(self.cleaned_data.get("departments") or [])

    def _slugify_english(self, text: str) -> str:
        try:
            from unidecode import unidecode  # type: ignore

            text = unidecode(text or "")
        except ImportError:
            # حزمةٌ اختيارية بحقّ: بدونها يُشتقّ المعرّف من النص كما هو.
            pass
        return slugify(text or "", allow_unicode=False)

    def _generate_unique_code(self, name: str) -> str:
        max_length = ReportType._meta.get_field("code").max_length
        base_code = self._slugify_english((name or "").strip()) or "report-type"
        base_code = base_code[:max_length]

        school = self.active_school
        if school is None and getattr(self.instance, "school_id", None):
            school = getattr(self.instance, "school", None)

        qs = ReportType.objects.all()
        if school is not None and hasattr(ReportType, "school"):
            qs = qs.filter(school=school)
        elif hasattr(ReportType, "school"):
            qs = qs.filter(school__isnull=True)

        if getattr(self.instance, "pk", None):
            qs = qs.exclude(pk=self.instance.pk)

        candidate = base_code
        suffix_index = 2
        while qs.filter(code=candidate).exists():
            suffix = f"-{suffix_index}"
            prefix_max = max_length - len(suffix)
            candidate = f"{base_code[:prefix_max]}{suffix}"
            suffix_index += 1

        return candidate

    def save(self, commit: bool = True):
        instance = super().save(commit=False)
        if hasattr(instance, "school") and self.active_school is not None:
            instance.school = self.active_school
        instance.code = self._generate_unique_code(self.cleaned_data.get("name") or instance.name or "")
        if commit:
            instance.save()
            self._save_m2m()
        else:
            # Match Django's ModelForm contract: callers that save the instance
            # later (the create view does this to attach the active school) must
            # still be able to persist both model and reverse M2M fields.
            self.save_m2m = self._save_m2m
        return instance

# ==============================
# 📌 إنشاء إشعار
# ==============================
class FlexibleModelMultipleChoiceField(forms.ModelMultipleChoiceField):
    """Accept legacy single values while exposing a real multi-select field."""

    def clean(self, value):
        if value not in self.empty_values and not isinstance(value, (list, tuple)):
            value = [value]
        return super().clean(value)


class NotificationCreateForm(forms.Form):
    title = forms.CharField(max_length=120, required=False, label="عنوان (اختياري)")
    message = forms.CharField(widget=forms.Textarea(attrs={"rows":5}), label="نص الإشعار")
    is_important = forms.BooleanField(required=False, initial=False, label="مهم")
    expires_at = forms.DateTimeField(required=False, label="ينتهي في (اختياري)",
                                     widget=DateTimeLocalInput())

    attachment = forms.FileField(
        required=False,
        label="مرفق (اختياري)",
        help_text="PDF/صور (حد أقصى 5MB).",
        validators=[validate_circular_attachment_file],
        widget=forms.ClearableFileInput(
            attrs={
                "accept": ".pdf,.jpg,.jpeg,.png",
            }
        ),
    )

    # ==============================
    # التعميمات والتوقيع الإلزامي
    # ==============================
    requires_signature = forms.BooleanField(
        required=False,
        initial=False,
        label="يتطلب توقيع إلزامي (تعميم)",
        help_text="عند تفعيل هذا الخيار سيُطلب من المستلم إدخال جواله المسجل + الإقرار قبل اعتماد التوقيع.",
    )
    signature_deadline_at = forms.DateTimeField(
        required=False,
        label="آخر موعد للتوقيع (اختياري)",
        widget=DateTimeLocalInput(),
    )
    signature_ack_text = forms.CharField(
        required=False,
        label="نص الإقرار (اختياري)",
        widget=forms.Textarea(attrs={"rows": 3}),
        initial="أقرّ بأنني اطلعت على هذا التعميم وفهمت ما ورد فيه وأتعهد بالالتزام به.",
        help_text="سيظهر نص الإقرار للمستلم داخل صفحة التوقيع.",
    )
    audience_scope = forms.ChoiceField(
        label="نطاق الإرسال",
        required=False,
        choices=(
            ("school", "مدرسة معيّنة"),
            ("all", "كل المدارس"),
        ),
        initial="school",
        help_text="لمالك النظام فقط: اختر ما إذا كان الإشعار موجهاً لمدرسة واحدة أو لكل المدارس.",
    )
    target_school = forms.ModelChoiceField(
        queryset=School.objects.none(),
        required=False,
        label="المدرسة المستهدفة",
        help_text="اختر المدرسة التي سيتم إرسال الإشعار لمستخدميها.",
    )
    target_department = FlexibleModelMultipleChoiceField(
        queryset=Department.objects.none(),
        required=False,
        label="اختر قسمًا أو أكثر",
        help_text="سيُضاف جميع أعضاء الأقسام المختارة إلى المستلمين، ويمكنك إضافة أفراد من القائمة أدناه.",
        widget=forms.CheckboxSelectMultiple(),
    )
    teachers = forms.ModelMultipleChoiceField(
        queryset=Teacher.objects.none(),
        required=False,
        label="المستلمون (يمكن اختيار أكثر من معلم)",
        help_text="اختيار المستلمين يدويًا يجعل الإرسال يقتصر عليهم فقط.",
        widget=forms.CheckboxSelectMultiple()
    )

    def __init__(self, *args, **kwargs):
        user = kwargs.pop("user", None)
        active_school = kwargs.pop("active_school", None)
        mode = (kwargs.pop("mode", None) or "notification").strip().lower()
        super().__init__(*args, **kwargs)

        self.user = user
        self.active_school = active_school
        self.mode = mode if mode in {"notification", "circular"} else "notification"
        is_circular = self.mode == "circular"

        if is_circular:
            self.fields["title"].required = True
            self.fields["title"].label = "عنوان التعميم"
            self.fields["message"].label = "نص التعميم"

        # المرفقات للتعاميم فقط
        if not is_circular:
            self.fields.pop("attachment", None)

        is_superuser = bool(getattr(user, "is_superuser", False))
        labels = school_gender_labels(active_school)
        teacher_singular = str(labels["teacher_indefinite"])
        teachers_plural = str(labels["teachers"])
        teachers_obj = str(labels["teachers_object"])
        
        # التحقق مما إذا كان المستخدم مديراً ضمن المدرسة النشطة (عزل مدارس)
        try:
            from .views._helpers import _is_manager_in_school
            is_manager = bool(_is_manager_in_school(user, active_school))
        except Exception:
            is_manager = False

        # إعداد حقول نطاق الإرسال/المدرسة حسب نوع المستخدم
        if is_superuser:
            self.fields["target_school"].queryset = School.objects.filter(is_active=True).order_by("name")

            if "target_department" in self.fields:
                self.fields["target_department"].queryset = Department.objects.filter(is_active=True).order_by("name")
        else:
            # لا يحتاج المدير/الضابط لاختيار النطاق أو المدرسة؛ نستخدم المدرسة النشطة تلقائياً
            self.fields.pop("audience_scope", None)
            self.fields.pop("target_school", None)

            # جلب أقسام المدرسة النشطة فقط (للمدير فقط حسب الطلب)
            if is_manager and active_school:
                self.fields["target_department"].queryset = Department.objects.filter(
                    models.Q(school=active_school),
                    is_active=True
                ).order_by("name")
            else:
                self.fields.pop("target_department", None)

        # في وضع التعميم: الأقسام متاحة لمدير المدرسة فقط. أما مدير النظام
        # فيوجّه التعميم إلى مدراء المدارس لا إلى أقسامها.
        if is_circular:
            if not is_manager:
                self.fields.pop("target_department", None)

            # التعميم دائمًا يتطلب توقيعًا (والـ view يفرضه كذلك)
            if "requires_signature" in self.fields:
                with soft_fail("forms.circular_signature_default"):
                    self.fields["requires_signature"].initial = True

        qs = Teacher.objects.filter(is_active=True).order_by("name")

        # ==============================
        # فصل التعميمات 100% (المستلمون)
        # ==============================
        if is_circular:
            # مدير النظام (superuser): يرسل التعميمات لمدراء المدارس فقط
            if is_superuser:
                qs = qs.filter(
                    school_memberships__role_type=SchoolMembership.RoleType.MANAGER,
                    school_memberships__is_active=True,
                    school_memberships__school__is_active=True,
                ).distinct()

                # إعادة تسمية الحقل ليتوافق مع الواقع (مدراء مدارس)
                if "teachers" in self.fields:
                    self.fields["teachers"].label = "مدراء المدارس (يمكن اختيار أكثر من مدير)"
                    self.fields["teachers"].help_text = "يمكنك ترك الاختيار فارغًا لإرسال التعميم لجميع مدراء المدارس ضمن النطاق المحدد."

                # لو اختار السوبر مدرسة محددة، قيد المدراء بهذه المدرسة
                scope_val = (self.data.get("audience_scope") or self.initial.get("audience_scope") or "").strip()
                school_id = self.data.get("target_school") or self.initial.get("target_school")
                if (not scope_val or scope_val == "school") and school_id:
                    try:
                        qs = qs.filter(school_memberships__school_id=int(school_id)).distinct()
                    except ValueError:
                        # معرّفُ مدرسةٍ غير رقمي: القائمة تبقى بلا حصر بالمدرسة،
                        # فيرى المُرسل مرشّحين أوسع مما قصد. يُسجَّل ليُصلَح المصدر.
                        _degraded("forms.invalid_target_school", value=str(school_id)[:32])

            # مدير المدرسة: يرسل التعميمات للمعلمين ضمن مدرسته فقط
            else:
                if active_school is not None:
                    qs = qs.filter(
                        school_memberships__school=active_school,
                        school_memberships__is_active=True,
                        school_memberships__role_type__in=SchoolMembership.STAFF_ROLES,
                    ).distinct()
                else:
                    qs = qs.none()

                if "teachers" in self.fields:
                    self.fields["teachers"].label = f"{teachers_plural} (يمكن اختيار {teacher_singular} أو أكثر)"
                    self.fields["teachers"].help_text = f"يجب تحديد مستلم واحد على الأقل قبل إرسال التعميم إلى {teachers_obj}."

            self.fields["teachers"].queryset = qs
            return

        # تقليص القائمة حسب الأقسام التي يديرها المستخدم (للضباط)
        try:
            role_slug = getattr(getattr(user, "role", None), "slug", None)
            if role_slug and role_slug not in (None, "manager"):
                # عزل: اجلب أقسام الضابط داخل المدرسة النشطة فقط
                try:
                    from .permissions import get_officer_departments
                    officer_depts = get_officer_departments(user, active_school=active_school)
                    codes = [d.slug for d in officer_depts if getattr(d, "slug", None)]
                except Exception:
                    codes = []
                if codes:
                    qs = qs.filter(
                        models.Q(role__slug__in=codes)
                        | models.Q(dept_memberships__department__slug__in=codes)
                    ).distinct()
        except Exception:
            # فلترةٌ لم تُطبَّق تعني قائمةَ مستلمين **أوسع** من المقصود.
            _degraded("forms.recipient_scope_filter")

        # تقليص حسب المدرسة النشطة للمدير/الضابط
        if active_school is not None:
            qs = qs.filter(
                school_memberships__school=active_school,
                school_memberships__is_active=True,
            )
            if is_manager:
                # واجهة مدير المدرسة موجهة لمنسوبي المدرسة من المعلمين، فلا
                # نعرض حساب المدير نفسه كأنه معلم.
                qs = qs.filter(
                    school_memberships__role_type__in=SchoolMembership.STAFF_ROLES,
                ).exclude(
                    pk=getattr(user, "pk", None),
                )
            qs = qs.distinct()

        # لمالك النظام: لو اختار "مدرسة معيّنة" في الطلب، نقيّد القائمة بهذه المدرسة
        if is_superuser:
            scope_val = (self.data.get("audience_scope") or self.initial.get("audience_scope") or "").strip()
            school_id = self.data.get("target_school") or self.initial.get("target_school")
            if (not scope_val or scope_val == "school") and school_id:
                try:
                    qs = qs.filter(
                        school_memberships__school_id=int(school_id),
                    ).distinct()
                except ValueError:
                    _degraded("forms.invalid_target_school_admin", value=str(school_id)[:32])

        self.fields["teachers"].queryset = qs

    def clean(self):
        cleaned = super().clean()
        user = getattr(self, "user", None)
        is_superuser = bool(getattr(user, "is_superuser", False))

        mode = getattr(self, "mode", "notification") or "notification"
        is_circular = mode == "circular"

        if is_superuser:
            scope = cleaned.get("audience_scope") or "school"
            target_school = cleaned.get("target_school")
            if scope == "school" and not target_school:
                raise ValidationError("الرجاء اختيار مدرسة مستهدفة أو تغيير النطاق إلى \"كل المدارس\".")

        # التعميمات داخل المدرسة: مدير المدرسة يجب أن يحدد مستلمين صراحةً.
        if is_circular and not is_superuser:
            selected_teachers = cleaned.get("teachers")
            target_departments = cleaned.get("target_department")
            if not selected_teachers and not target_departments:
                self.add_error("teachers", "لا يمكن إرسال التعميم والمستلمون = 0. يرجى تحديد المستلمين أولاً.")
            if target_departments and not selected_teachers:
                dept_recipients_qs = Teacher.objects.filter(
                    is_active=True,
                    dept_memberships__department__in=target_departments,
                )
                active_school = getattr(self, "active_school", None)
                if active_school is not None:
                    dept_recipients_qs = dept_recipients_qs.filter(
                        school_memberships__school=active_school,
                        school_memberships__is_active=True,
                    )
                if not dept_recipients_qs.distinct().exists():
                    self.add_error(
                        "target_department",
                        "الأقسام المحددة لا تحتوي على مستلمين نشطين حاليًا. يرجى اختيار مستلمين يدويًا.",
                    )

        # للإشعارات العادية (داخل المدرسة): لا نسمح بالإرسال بدون تحديد مستلمين.
        # يمكن التحديد إما عبر اختيار معلمين بشكل مباشر أو اختيار قسم كامل.
        if not is_circular and not is_superuser:
            selected_teachers = cleaned.get("teachers")
            target_departments = cleaned.get("target_department")
            if not selected_teachers and not target_departments:
                raise ValidationError("يرجى تحديد المستلمين (اختيار معلم/معلمة أو قسم) قبل إرسال الإشعار.")
            if target_departments and not selected_teachers:
                dept_recipients_qs = Teacher.objects.filter(
                    is_active=True,
                    dept_memberships__department__in=target_departments,
                )
                active_school = getattr(self, "active_school", None)
                if active_school is not None:
                    dept_recipients_qs = dept_recipients_qs.filter(
                        school_memberships__school=active_school,
                        school_memberships__is_active=True,
                    )
                if not dept_recipients_qs.distinct().exists():
                    self.add_error(
                        "target_department",
                        "القسم المحدد لا يحتوي على مستلمين نشطين حاليًا. يرجى اختيار مستلمين يدويًا.",
                    )
        return cleaned

    def save(self, creator, default_school=None, force_requires_signature: Optional[bool] = None):
        from .tasks import send_notification_task
        from django.db import transaction

        cleaned = self.cleaned_data

        # تحديد المدرسة المرتبطة بالإشعار
        school_for_notification = default_school
        is_superuser = bool(getattr(creator, "is_superuser", False))

        if is_superuser:
            scope = cleaned.get("audience_scope") or "school"
            if scope == "all":
                school_for_notification = None
            else:
                school_for_notification = cleaned.get("target_school") or None

        requires_signature = bool(cleaned.get("requires_signature"))
        if force_requires_signature is not None:
            requires_signature = bool(force_requires_signature)

        # المرفقات للتعاميم فقط
        attachment = None
        if requires_signature:
            attachment = cleaned.get("attachment") if "attachment" in cleaned else None

        n = Notification.objects.create(
            title=cleaned.get("title") or "",
            message=cleaned["message"],
            is_important=bool(cleaned.get("is_important")),
            expires_at=cleaned.get("expires_at") or None,
            attachment=attachment,
            requires_signature=requires_signature,
            signature_deadline_at=(cleaned.get("signature_deadline_at") or None) if requires_signature else None,
            signature_ack_text=(cleaned.get("signature_ack_text") or "").strip()
            or "أقرّ بأنني اطلعت على هذا التعميم وفهمت ما ورد فيه وأتعهد بالالتزام به.",
            created_by=creator,
            school=school_for_notification,
        )
        
        # تجميع المستهدفين
        teacher_ids_set = set()
        
        # 1. المعلمون المختارون يدوياً
        selected_teachers = cleaned.get("teachers")
        if selected_teachers:
            teacher_ids_set.update([t.pk for t in selected_teachers])
            
        # 2. توجيه حسب القسم (للإشعارات، ولتعاميم مدير المدرسة)
        target_departments = cleaned.get("target_department")
        # الأقسام والأفراد مصدران متكاملان للمستلمين؛ تزيل المجموعة أي تكرار.
        if target_departments and (
            not bool(requires_signature)
            or not is_superuser
        ):
            from .models import DepartmentMembership
            dept_teachers = DepartmentMembership.objects.filter(
                department__in=target_departments,
                teacher__is_active=True,
            )
            if school_for_notification is not None:
                dept_teachers = dept_teachers.filter(
                    teacher__school_memberships__school=school_for_notification,
                    teacher__school_memberships__is_active=True,
                )
            dept_teachers = dept_teachers.values_list("teacher_id", flat=True).distinct()
            teacher_ids_set.update(dept_teachers)
        
        teacher_ids = list(teacher_ids_set) if teacher_ids_set else None

        # التعميمات (requires_signature=True): مدير النظام يرسل لمدراء المدارس.
        # لو لم يحدد أسماء، نعتبره "إرسال للكل" ضمن النطاق المحدد.
        if bool(requires_signature) and bool(getattr(creator, "is_superuser", False)):
            if not teacher_ids:
                try:
                    qs = self.fields["teachers"].queryset
                    teacher_ids = list(qs.values_list("pk", flat=True))
                except Exception:
                    teacher_ids = None

        # Circulars inside a school require explicit recipients selection.
        # Keep teacher_ids as-is; do not expand an empty selection to all teachers.

        # Reliability guard: when recipients are explicitly known, create the
        # DB recipient rows immediately.  Celery may still run later for
        # realtime pushes, but page delivery no longer depends on a live worker.
        if teacher_ids:
            try:
                NotificationRecipient.objects.bulk_create(
                    [NotificationRecipient(notification=n, teacher_id=tid) for tid in teacher_ids],
                    ignore_conflicts=True,
                )
                try:
                    from .realtime_notifications import push_new_notification_to_teachers

                    push_new_notification_to_teachers(notification=n, teacher_ids=teacher_ids)
                except Exception:
                    logger.exception("Immediate realtime notification dispatch failed for notification %s", n.pk)
                with soft_fail("forms.invalidate_recipient_caches", count=len(teacher_ids)):
                    from .cache_utils import invalidate_user_notifications

                    for tid in teacher_ids:
                        invalidate_user_notifications(int(tid))
            except Exception:
                logger.exception("Immediate notification recipient creation failed for notification %s", n.pk)

        # Trigger background task to create recipients
        # - Prefer async (Celery)
        # - Fallback to local execution if broker/worker is unavailable
        def _dispatch():
            import time

            try:
                from django.conf import settings
            except Exception:
                settings = None  # type: ignore

            broker_url = ""
            try:
                broker_url = (getattr(settings, "CELERY_BROKER_URL", "") or "").strip()
            except Exception:
                broker_url = ""

            # Best-effort anti-double-send across async/local paths.
            try:
                from django.core.cache import cache
                lock_ttl = int(getattr(settings, "NOTIFICATIONS_DISPATCH_LOCK_TTL_SECONDS", 900))
            except Exception:
                cache = None  # type: ignore
                lock_ttl = 900

            # Versioned lock key to keep uniqueness stable across future changes.
            lock_key = f"notif_dispatch_lock:v1:notification:{n.pk}"

            def _acquire_lock() -> bool:
                if cache is None:
                    return True
                try:
                    return bool(cache.add(lock_key, "1", timeout=max(60, int(lock_ttl))))
                except Exception:
                    return True

            def _release_lock() -> None:
                if cache is None:
                    return
                # قفلٌ لا يُحرَّر يمنع كل إرسالٍ لاحق حتى ينتهي عمره.
                with soft_fail("notifications.release_dispatch_lock", key=lock_key):
                    cache.delete(lock_key)

            def _run_local(*, warn_seconds: float, is_debug: bool) -> bool:
                started = time.monotonic()

                with soft_fail("notifications.close_stale_connections"):
                    from django.db import close_old_connections
                    close_old_connections()

                ok = False
                try:
                    send_notification_task.apply(args=(n.pk, teacher_ids), throw=True)
                    ok = True
                except Exception as exc:
                    if is_debug:
                        logger.exception("Local notification dispatch failed")
                    else:
                        logger.error("Local notification dispatch failed: %s", exc)
                finally:
                    dur = time.monotonic() - started
                    if dur > float(warn_seconds or 0):
                        logger.warning("Local notification dispatch took %.2fs (notification=%s)", dur, n.pk)

                return ok

            recipient_count = None if teacher_ids is None else len(teacher_ids)

            # ------------------ No broker configured: local fallback path ------------------
            if not broker_url:
                try:
                    if not bool(getattr(settings, "NOTIFICATIONS_LOCAL_FALLBACK_ENABLED", True)):
                        logger.error(
                            "Celery broker not configured and local fallback disabled; notification %s will not be dispatched",
                            n.pk,
                        )
                        return

                    use_thread = bool(getattr(settings, "NOTIFICATIONS_LOCAL_FALLBACK_THREAD", True))
                    max_sync = int(getattr(settings, "NOTIFICATIONS_LOCAL_FALLBACK_MAX_RECIPIENTS", 500))
                    hard_stop = int(getattr(settings, "NOTIFICATIONS_LOCAL_FALLBACK_HARD_STOP_RECIPIENTS", 500))
                    warn_seconds = float(getattr(settings, "NOTIFICATIONS_LOCAL_FALLBACK_WARN_SECONDS", 2.5))
                    is_debug = bool(getattr(settings, "DEBUG", False))
                except Exception:
                    use_thread = True
                    max_sync = 500
                    hard_stop = 500
                    warn_seconds = 2.5
                    is_debug = False

                # Hard-stop: do not run heavy/unknown workloads inside web.
                # When teacher_ids is None it means "all school teachers" – resolve the count
                # from DB so we can apply the hard_stop guard without silently dropping the send.
                if recipient_count is None:
                    try:
                        from .models import SchoolMembership
                        _school = getattr(n, "school", None)
                        if _school is not None:
                            recipient_count = (
                                SchoolMembership.objects.filter(
                                    school=_school,
                                    is_active=True,
                                    role_type__in=SchoolMembership.STAFF_ROLES,
                                )
                                .values("teacher_id")
                                .distinct()
                                .count()
                            )
                        else:
                            recipient_count = 0
                    except Exception:
                        recipient_count = 0

                if int(recipient_count) > int(hard_stop):
                    logger.error(
                        "Local notification fallback refused (recipients=%s, hard_stop=%s). Configure broker to dispatch notification %s.",
                        recipient_count,
                        hard_stop,
                        n.pk,
                    )
                    return

                if not _acquire_lock():
                    logger.info("Notification %s dispatch already in progress; skipping", n.pk)
                    return

                global _NOTIF_CELERY_FALLBACK_WARNED
                if not _NOTIF_CELERY_FALLBACK_WARNED:
                    logger.warning("Celery broker not configured; using local fallback for notifications")
                    _NOTIF_CELERY_FALLBACK_WARNED = True

                should_thread = bool(use_thread) and int(recipient_count) > int(max_sync)
                if should_thread:
                    try:
                        import threading

                        threading.Thread(
                            target=lambda: (_run_local(warn_seconds=warn_seconds, is_debug=is_debug) or _release_lock()),
                            name="notif_local_dispatch",
                            daemon=True,
                        ).start()
                        return
                    except Exception:
                        # خيطٌ لم يبدأ يعني إشعاراً لم يُرسَل. لا نسقط الطلب —
                        # يسقط إلى المسار المتزامن أدناه — لكن الأثر يُسجَّل.
                        _degraded("notifications.start_local_dispatch_thread")

                ok = False
                try:
                    ok = _run_local(warn_seconds=warn_seconds, is_debug=is_debug)
                finally:
                    if not ok:
                        _release_lock()
                return

            # ------------------ Broker configured: async path ------------------
            if not _acquire_lock():
                logger.info("Notification %s dispatch already in progress; skipping", n.pk)
                return

            enqueued = False
            try:
                try:
                    from core.trace_context import get_trace_id as _get_trace_id
                    _tid = _get_trace_id()
                except Exception:
                    _tid = None
                if not _tid:
                    import secrets
                    _tid = secrets.token_hex(8)
                send_notification_task.apply_async(
                    args=[n.pk, teacher_ids],
                    headers={"trace_id": _tid},
                )
                enqueued = True
                return
            except Exception:
                logger.exception("Celery enqueue failed; attempting local fallback")
            finally:
                # If enqueue failed, release the lock so fallback (or later retry) can proceed.
                if not enqueued:
                    _release_lock()

            # If enqueue failed (broker down), local fallback may be allowed for small/known workloads.
            try:
                if not bool(getattr(settings, "NOTIFICATIONS_LOCAL_FALLBACK_ENABLED", True)):
                    logger.error("Celery enqueue failed and local fallback disabled; notification %s not dispatched", n.pk)
                    return

                use_thread = bool(getattr(settings, "NOTIFICATIONS_LOCAL_FALLBACK_THREAD", True))
                max_sync = int(getattr(settings, "NOTIFICATIONS_LOCAL_FALLBACK_MAX_RECIPIENTS", 500))
                hard_stop = int(getattr(settings, "NOTIFICATIONS_LOCAL_FALLBACK_HARD_STOP_RECIPIENTS", 500))
                warn_seconds = float(getattr(settings, "NOTIFICATIONS_LOCAL_FALLBACK_WARN_SECONDS", 2.5))
                is_debug = bool(getattr(settings, "DEBUG", False))
            except Exception:
                use_thread = True
                max_sync = 500
                hard_stop = 500
                warn_seconds = 2.5
                is_debug = False

            # Resolve recipient_count if still unknown (teacher_ids was None).
            if recipient_count is None:
                try:
                    from .models import SchoolMembership
                    _school = getattr(n, "school", None)
                    if _school is not None:
                        recipient_count = (
                            SchoolMembership.objects.filter(
                                school=_school,
                                is_active=True,
                                role_type__in=SchoolMembership.STAFF_ROLES,
                            )
                            .values("teacher_id")
                            .distinct()
                            .count()
                        )
                    else:
                        recipient_count = 0
                except Exception:
                    recipient_count = 0

            if int(recipient_count) > int(hard_stop):
                logger.error(
                    "Celery enqueue failed; local fallback refused (recipients=%s, hard_stop=%s) for notification %s",
                    recipient_count,
                    hard_stop,
                    n.pk,
                )
                return

            # Acquire lock for local attempt (avoid duplicates if concurrent retries).
            if not _acquire_lock():
                logger.info("Notification %s dispatch already in progress; skipping", n.pk)
                return

            should_thread = bool(use_thread) and int(recipient_count) > int(max_sync)
            if should_thread:
                try:
                    import threading

                    threading.Thread(
                        target=lambda: (_run_local(warn_seconds=warn_seconds, is_debug=is_debug) or _release_lock()),
                        name="notif_local_dispatch",
                        daemon=True,
                    ).start()
                    return
                except Exception:
                    _degraded("notifications.start_local_dispatch_thread_fallback")

            ok = False
            try:
                ok = _run_local(warn_seconds=warn_seconds, is_debug=is_debug)
            finally:
                if not ok:
                    _release_lock()

        transaction.on_commit(_dispatch)
        
        return n


class SupportTicketForm(forms.ModelForm):
    """نموذج إنشاء تذكرة دعم فني للمنصة."""

    # نستخدم ImageField هنا لضمان التحقق من أنه صورة قبل الحفظ،
    # وللسماح بضغط الصورة قبل التحقق من حد الحجم النهائي.
    attachment = forms.ImageField(
        required=False,
        widget=forms.ClearableFileInput(attrs={
            "class": "form-control",
            "accept": "image/*",
        }),
    )

    class Meta:
        model = Ticket
        fields = ["title", "body", "attachment"]
        widgets = {
            "title": forms.TextInput(attrs={
                "class": "form-control", "placeholder": "عنوان المشكلة أو الاستفسار", "maxlength": "255"
            }),
            "body": forms.Textarea(attrs={"class": "form-control", "rows": 5, "placeholder": "اشرح المشكلة بالتفصيل..."}),
        }

    def clean_attachment(self):
        f = self.cleaned_data.get("attachment")
        if not f:
            return f

        # ضغط/تصغير قبل الرفع
        try:
            from PIL import Image, ImageOps, UnidentifiedImageError

            img = Image.open(f)
            img = ImageOps.exif_transpose(img)
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise ValidationError("الملف المرفق ليس صورة صالحة.") from exc

        has_alpha = img.mode in ("RGBA", "LA", "P")
        img = img.convert("RGBA" if has_alpha else "RGB")

        max_px = 1600
        if max(img.size) > max_px:
            img.thumbnail((max_px, max_px), Image.LANCZOS)

        buf = BytesIO()
        base = os.path.splitext(getattr(f, "name", "image"))[0]

        if has_alpha:
            # PNG للصور ذات الشفافية
            img.save(buf, format="PNG", optimize=True, compress_level=9)
            new_name = f"{base}.png"
            ctype = "image/png"
        else:
            # JPEG للصور العادية (ضغط أعلى)
            img.save(buf, format="JPEG", quality=82, optimize=True, progressive=True)
            new_name = f"{base}.jpg"
            ctype = "image/jpeg"

        buf.seek(0)
        out = InMemoryUploadedFile(
            buf,
            getattr(f, "field_name", None) or "attachment",
            new_name,
            ctype,
            buf.getbuffer().nbytes,
            None,
        )

        # حد الحجم بعد الضغط (مطابق للحد في الموديل: 5MB)
        max_bytes = 5 * 1024 * 1024
        if out.size > max_bytes:
            raise ValidationError("حجم الصورة بعد الضغط ما يزال كبيرًا (الحد الأقصى 5MB).")

        return out

    def save(self, commit=True, user=None):
        ticket = super().save(commit=False)
        if user:
            ticket.creator = user
        ticket.is_platform = True
        if commit:
            ticket.save()
        return ticket


# ==============================
# نماذج الاشتراكات (Platform Admin)
# ==============================
class SubscriptionPlanForm(forms.ModelForm):
    class Meta:
        model = SubscriptionPlan
        fields = [
            "name",
            "description",
            "price",
            "days_duration",
            "max_teachers",
            "support_level",
            "onboarding_sessions",
            "included_archive_storage_gb",
            "is_active",
        ]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control", "placeholder": "مثال: الاحترافية | سنوية"}),
            "description": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 5,
                    "placeholder": "اكتب كل ميزة في سطر مستقل؛ تظهر أول ثلاث ميزات في صفحة الأسعار.",
                }
            ),
            "price": forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": 0}),
            "days_duration": forms.NumberInput(attrs={"class": "form-control", "min": 1}),
            "max_teachers": forms.NumberInput(attrs={"class": "form-control", "min": 0}),
            "support_level": forms.Select(attrs={"class": "form-select"}),
            "onboarding_sessions": forms.NumberInput(attrs={"class": "form-control", "min": 0}),
            "included_archive_storage_gb": forms.NumberInput(attrs={"class": "form-control", "min": 0}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }
        labels = {
            "name": "اسم الخطة",
            "description": "الوصف",
            "price": "السعر (ريال)",
            "days_duration": "المدة (بالأيام)",
            "max_teachers": "حد المعلمين",
            "support_level": "مستوى الدعم",
            "onboarding_sessions": "جلسات الإعداد المشمولة",
            "included_archive_storage_gb": "مساحة الأرشيف المشمولة (GB)",
            "is_active": "نشط؟",
        }
        help_texts = {
            "description": "اكتب ثلاث جمل قصيرة، كل جملة في سطر مستقل، لتظهر كمميزات واضحة في بطاقة السعر.",
            "price": "السعر النهائي المطلوب من العميل. استخدم 0 للتجربة المجانية فقط.",
            "days_duration": "30 للشهر، 180 لستة أشهر، و365 للسنة.",
            "max_teachers": "لا يشمل مدير المدرسة. القيمة 0 تعني سعة غير محدودة.",
            "included_archive_storage_gb": "استخدم 50 للباقة القيادية السنوية، و0 إذا كان الأرشيف إضافة مستقلة.",
        }

    def clean(self):
        cleaned_data = super().clean()
        days = cleaned_data.get("days_duration")
        max_teachers = cleaned_data.get("max_teachers")

        if days and max_teachers is not None:
            duplicate = SubscriptionPlan.objects.filter(
                days_duration=days,
                max_teachers=max_teachers,
            )
            if self.instance.pk:
                duplicate = duplicate.exclude(pk=self.instance.pk)
            if duplicate.exists():
                self.add_error(
                    None,
                    "توجد باقة أخرى بنفس المدة وحد المعلمين. عدّل الباقة الحالية بدل إنشاء نسخة مكررة.",
                )
        return cleaned_data


class SchoolSubscriptionForm(forms.ModelForm):
    """نموذج اشتراك المدرسة (للوحة المنصة).

    المطلوب: حساب التواريخ تلقائياً حسب مدة الباقة (days_duration) اعتماداً على التاريخ الميلادي.
    - start_date = اليوم
    - end_date = اليوم + (days_duration - 1)
    """

    class Meta:
        model = SchoolSubscription
        fields = ["school", "plan", "teacher_limit_override", "is_active"]
        widgets = {
            "school": forms.Select(attrs={"class": "form-select"}),
            "plan": forms.Select(attrs={"class": "form-select"}),
            "teacher_limit_override": forms.NumberInput(
                attrs={"class": "form-control", "min": "1", "max": "100", "step": "1"}
            ),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }
        labels = {
            "school": "المدرسة",
            "plan": "الباقة",
            "teacher_limit_override": "السعة الفعلية المشتراة",
            "is_active": "نشط؟",
        }
        help_texts = {
            "teacher_limit_override": "اختياري. اتركه فارغاً لتطبيق حد الباقة، أو أدخل السعة المرنة المعتمدة للمدرسة.",
        }

    def __init__(self, *args, **kwargs):
        self._allow_plan_change = bool(kwargs.pop("allow_plan_change", False))
        super().__init__(*args, **kwargs)
        # ✅ عند تعديل اشتراك موجود: لا نسمح بتغيير المدرسة.
        # ✅ الباقة: افتراضياً لا نسمح بتغييرها، لكن يمكن السماح بذلك في حالات
        # تجديد اشتراك مُلغى/منتهي من لوحة المنصة.
        try:
            if getattr(self.instance, "pk", None):
                if "school" in self.fields:
                    self.fields["school"].disabled = True
                if (not self._allow_plan_change) and "plan" in self.fields:
                    self.fields["plan"].disabled = True
        except Exception:
            # حقلٌ كان يجب أن يُقفل فبقي مفتوحاً: تعديلٌ مسموح لا يجوز.
            _degraded("forms.lock_subscription_fields")

    def clean_school(self):
        # تحصين: حتى مع التلاعب بالـ POST لا نسمح بتغيير المدرسة للاشتراك الموجود.
        if getattr(self.instance, "pk", None):
            return self.instance.school
        return self.cleaned_data.get("school")

    def clean_plan(self):
        # تحصين: حتى مع التلاعب بالـ POST لا نسمح بتغيير الباقة للاشتراك الموجود.
        if getattr(self.instance, "pk", None) and (not self._allow_plan_change):
            return self.instance.plan
        return self.cleaned_data.get("plan")

    def save(self, commit=True):
        from datetime import timedelta

        subscription: SchoolSubscription = super().save(commit=False)
        plan = self.cleaned_data.get("plan")

        # ✅ عند الإنشاء فقط: احسب التواريخ تلقائياً.
        # عند التعديل: لا نغير التواريخ (التجديد له زر/مسار مستقل).
        if getattr(subscription, "pk", None) is None:
            today = timezone.localdate()
            subscription.start_date = today

            days = int(getattr(plan, "days_duration", 0) or 0)
            if days <= 0:
                subscription.end_date = today
            else:
                # end_date = اليوم + (المدة - 1) حتى تكون الأيام الفعلية = days_duration
                subscription.end_date = today + timedelta(days=days - 1)

        if commit:
            subscription.save()
        return subscription


class SchoolArchiveAddonForm(forms.ModelForm):
    class Meta:
        model = SchoolArchiveAddon
        fields = ["school", "is_enabled", "start_date", "end_date", "storage_limit_gb", "paid_amount", "notes"]
        widgets = {
            "school": forms.Select(attrs={"class": "form-select"}),
            "is_enabled": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "start_date": forms.DateInput(attrs={"class": "form-control", "type": "date"}),
            "end_date": forms.DateInput(attrs={"class": "form-control", "type": "date"}),
            "storage_limit_gb": forms.NumberInput(attrs={"class": "form-control", "min": "1", "step": "1"}),
            "paid_amount": forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": "0"}),
            "notes": forms.Textarea(attrs={"class": "form-control", "rows": 4}),
        }
        labels = {
            "school": "المدرسة",
            "is_enabled": "مفعّل؟",
            "start_date": "تاريخ بداية الملحق",
            "end_date": "تاريخ نهاية الملحق",
            "storage_limit_gb": "حد مساحة النسخ السنوية (GB)",
            "paid_amount": "قيمة الملحق",
            "notes": "ملاحظات",
        }
        help_texts = {
            # الملحق يشتري مساحة أرشفة فقط؛ مساحة العمل اليومي تأتي من سعة
            # المعلمين ومن المساحة الإضافية المشتراة، ولا تتأثر بهذا الحقل.
            "storage_limit_gb": "مساحة النسخ السنوية وحدها — مستقلة عن مساحة عمل المدرسة.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        try:
            self.fields["school"].queryset = School.objects.order_by("name", "id")
            if getattr(self.instance, "pk", None):
                self.fields["school"].disabled = True
        except Exception:
            _degraded("forms.lock_addon_school_field")

    def clean_school(self):
        if getattr(self.instance, "pk", None):
            return self.instance.school
        school = self.cleaned_data.get("school")
        if school is not None and SchoolArchiveAddon.objects.filter(school=school).exists():
            raise ValidationError("هذه المدرسة لديها ملحق أرشيف بالفعل. استخدم التعديل بدلاً من الإضافة.")
        return school


class PlatformSettingsForm(forms.ModelForm):
    class Meta:
        model = PlatformSettings
        fields = [
            "maintenance_mode_enabled",
            "maintenance_message",
            "mansour_public_enabled",
            "report_ai_enabled",
            "internal_ai_help_enabled",
            "voice_report_enabled",
            "report_review_enabled",
            "archive_addon_annual_price",
            "archive_included_storage_gb",
            "storage_mb_per_teacher",
            "free_storage_mb",
        ]
        widgets = {
            "maintenance_mode_enabled": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "mansour_public_enabled": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "report_ai_enabled": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "internal_ai_help_enabled": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "voice_report_enabled": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "report_review_enabled": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "maintenance_message": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 3,
                    "placeholder": "مثال: نعمل حالياً على تحسين المنصة، سنعود قريباً بإذن الله.",
                }
            ),
            "archive_addon_annual_price": forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "0.01"}),
            "archive_included_storage_gb": forms.NumberInput(attrs={"class": "form-control", "min": "1", "step": "1"}),
            "storage_mb_per_teacher": forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "50"}),
            "free_storage_mb": forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1"}),
        }
        labels = {
            "maintenance_mode_enabled": "تفعيل وضع الصيانة والتطوير",
            "maintenance_message": "رسالة تظهر للمستخدمين",
            "mansour_public_enabled": "المساعد منصور",
            "report_ai_enabled": "تحسين التقارير ومحاضر الاجتماعات",
            "internal_ai_help_enabled": "المساعدة داخل النظام",
            "voice_report_enabled": "الكتابة بالصوت للتقارير والمحاضر",
            "report_review_enabled": "فحص جاهزية التقرير",
            "archive_addon_annual_price": "سعر الأرشفة السنوي",
            "archive_included_storage_gb": "المساحة المضمنة مع الأرشفة (GB)",
            "storage_mb_per_teacher": "مساحة عمل المدرسة لكل معلم (ميجابايت)",
            "free_storage_mb": "حد التخزين المجاني لكل مدرسة غير مشتركة (ميجابايت)",
        }
        help_texts = {
            "storage_mb_per_teacher": (
                "تُضرب في سعة المعلمين المشتراة. مثال: 400MB × سعة 50 معلماً = 19.5GB. "
                "امتلاء هذه المساحة يوقف رفع الملفات في المدرسة، فاخفضها بحذر."
            ),
        }

    def clean(self):
        cleaned = super().clean()
        annual_price = cleaned.get("archive_addon_annual_price")
        included = cleaned.get("archive_included_storage_gb")
        per_teacher = cleaned.get("storage_mb_per_teacher")
        free_mb = cleaned.get("free_storage_mb")

        if annual_price is not None and annual_price <= 0:
            self.add_error("archive_addon_annual_price", "سعر الأرشفة يجب أن يكون أكبر من صفر.")
        if included is not None and included < 1:
            self.add_error("archive_included_storage_gb", "المساحة المضمنة يجب أن تكون 1GB أو أكثر.")
        if free_mb is not None and free_mb < 0:
            self.add_error("free_storage_mb", "القيمة يجب أن تكون 0 أو أكثر (0 = غير محدود).")
        if per_teacher is not None:
            if per_teacher < 0:
                self.add_error("storage_mb_per_teacher", "القيمة يجب أن تكون 0 أو أكثر.")
            elif 0 < per_teacher < 50:
                # Below this a 25-teacher school starts under 1.2GB, which one
                # term of report photos exhausts. Zero stays allowed: it means
                # "no derived space" and falls back to the free floor.
                self.add_error(
                    "storage_mb_per_teacher",
                    "قيمة أقل من 50MB لكل معلم تجعل مساحة المدرسة غير عملية. "
                    "استخدم 0 لإلغاء المساحة المشتقة، أو 50 فأكثر.",
                )
        return cleaned


class ArchiveStorageOptionForm(forms.ModelForm):
    class Meta:
        model = ArchiveStorageOption
        fields = ["bucket", "storage_gb", "price", "sort_order", "is_active"]
        widgets = {
            "bucket": forms.Select(attrs={"class": "form-select"}),
            "storage_gb": forms.NumberInput(attrs={"class": "form-control", "min": "1", "step": "1"}),
            "price": forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "0.01"}),
            "sort_order": forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1"}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }
        labels = {
            "bucket": "المساحة المستهدفة",
            "storage_gb": "المساحة (GB)",
            "price": "السعر",
            "sort_order": "الترتيب",
            "is_active": "مفعّل",
        }

    def clean_price(self):
        price = self.cleaned_data.get("price")
        if price is not None and price <= 0:
            raise ValidationError("السعر يجب أن يكون أكبر من صفر.")
        return price


# ==============================
# 📁 ملف إنجاز المعلّم (سنوي)
# ==============================
class AchievementCreateYearForm(forms.Form):
    """اختيار سنة دراسية من قائمة لتفادي أخطاء الكتابة."""

    BASE_HIJRI_YEARS: List[str] = [
        "1447-1448",
        "1448-1449",
        "1449-1450",
    ]

    academic_year = forms.ChoiceField(
        label="السنة الدراسية (هجري)",
        choices=[],
        widget=forms.Select(attrs={"class": "input"}),
        help_text="اختر السنة من القائمة.",
    )

    def __init__(
        self,
        *args,
        year_choices: Optional[List[str]] = None,
        allowed_years: Optional[List[str]] = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)

        def _norm(v: str) -> str:
            return (v or "").strip().replace("–", "-").replace("—", "-")

        def _parse(v: str) -> Optional[Tuple[int, int]]:
            import re

            vv = _norm(v)
            if not re.fullmatch(r"\d{4}-\d{4}", vv):
                return None
            s, e = vv.split("-", 1)
            try:
                si, ei = int(s), int(e)
            except Exception:
                return None
            if ei != si + 1:
                return None
            return si, ei

        existing = [_norm(y) for y in (year_choices or []) if (y or "").strip()]

        # إذا تم تمرير سنوات مسموحة (من إعدادات المدرسة) نستخدمها فقط
        # وإلا نستخدم القائمة الافتراضية
        if allowed_years is not None:
            base_set = set([_norm(y) for y in allowed_years])
            # القائمة المحددة للإنشاء لا تختلط بالسنوات التاريخية للملفات السابقة.
            all_years = base_set
        else:
            all_years = set([_norm(y) for y in self.BASE_HIJRI_YEARS] + existing)
            # توليد سنوات مستقبلية تلقائيًا في الحالة الافتراضية
            parsed = [_parse(y) for y in all_years]
            parsed_ok = [p for p in parsed if p is not None]
            max_end = max([e for _, e in parsed_ok], default=1450)
            for i in range(0, 2):
                s = max_end + i
                all_years.add(f"{s}-{s + 1}")

        valid = sorted(
            [y for y in all_years if _parse(y) is not None],
            key=lambda v: int(v.split("-", 1)[0]),
            reverse=False,
        )
        choices = [(y, f"{y} هـ") for y in valid]
        self.fields["academic_year"].choices = choices
        if choices:
            is_in_choices = False
            if self.initial.get("academic_year"):
                 # Check if initial is in choices
                 if any(c[0] == self.initial["academic_year"] for c in choices):
                     is_in_choices = True
            
            if not is_in_choices: 
                 self.fields["academic_year"].initial = choices[0][0]

    def clean_academic_year(self):
        return _validate_academic_year_hijri(self.cleaned_data.get("academic_year", ""))


class TeacherAchievementFileForm(forms.ModelForm):
    class Meta:
        model = TeacherAchievementFile
        fields = [
            "qualifications",
            "professional_experience",
            "specialization",
            "teaching_load",
            "subjects_taught",
            "contact_info",
        ]
        widgets = {
            "qualifications": forms.Textarea(attrs={"class": "textarea", "rows": 4}),
            "professional_experience": forms.Textarea(attrs={"class": "textarea", "rows": 4}),
            "specialization": forms.Textarea(attrs={"class": "textarea", "rows": 3}),
            "teaching_load": forms.Textarea(attrs={"class": "textarea", "rows": 2}),
            "subjects_taught": forms.Textarea(attrs={"class": "textarea", "rows": 3}),
            "contact_info": forms.Textarea(attrs={"class": "textarea", "rows": 3}),
        }


class AchievementSectionNotesForm(forms.ModelForm):
    class Meta:
        model = AchievementSection
        fields = ["teacher_notes"]
        widgets = {"teacher_notes": forms.Textarea(attrs={"class": "textarea", "rows": 3})}


class _AchievementMultiImageInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class AchievementEvidenceUploadForm(forms.Form):
    images = forms.FileField(
        label="إضافة صور الشواهد",
        required=False,
        widget=_AchievementMultiImageInput(attrs={"multiple": True, "class": "input", "accept": "image/*"}),
        help_text="حد أقصى 8 صور لكل محور.",
    )


class AchievementManagerNotesForm(forms.ModelForm):
    class Meta:
        model = TeacherAchievementFile
        fields = ["manager_notes"]
        widgets = {
            "manager_notes": forms.Textarea(
                attrs={
                    "class": "textarea",
                    "rows": 4,
                    "placeholder": "اكتب شكرًا/تحفيزًا عند الاعتماد، أو سبب الرفض عند الإرجاع…",
                }
            )
        }


class LeadershipPortfolioForm(forms.ModelForm):
    class Meta:
        model = SchoolLeadershipPortfolio
        fields = [
            "leadership_vision",
            "executive_summary",
            "notable_achievements",
            "improvement_priorities",
        ]
        widgets = {
            field: forms.Textarea(attrs={"class": "lp-textarea", "rows": 4})
            for field in fields
        }


class LeadershipPortfolioSectionForm(forms.ModelForm):
    class Meta:
        model = LeadershipPortfolioSection
        fields = ["notes", "is_completed"]
        widgets = {
            "notes": forms.Textarea(
                attrs={
                    "class": "lp-textarea",
                    "rows": 5,
                    "placeholder": "صف الممارسة والنتيجة، ثم أرفق الشواهد الداعمة.",
                }
            )
        }


class PricingMatrixForm(forms.Form):
    """Edit the whole anchor price matrix on one screen.

    Nine numbers drive every price the customer sees, and they only make sense
    relative to each other: a capacity must cost more than the one below it, and
    a longer commitment must beat paying month by month. Editing them as nine
    separate plan forms gave no way to check that, so a single edit could create
    a band where a school pays more and gets less.

    Entitlements are deliberately absent — they are identical across all paid
    anchors by design (see reports/pricing.py), so there is nothing per-plan to
    decide here.
    """

    PERIOD_ORDER = ("1m", "6m", "1y")

    def __init__(self, *args, **kwargs):
        from .flexible_pricing import ANCHOR_CAPACITIES, PERIODS

        self.capacities = tuple(kwargs.pop("capacities", ANCHOR_CAPACITIES))
        super().__init__(*args, **kwargs)

        self.periods = PERIODS
        for capacity in self.capacities:
            for period_key in self.PERIOD_ORDER:
                self.fields[self.field_name(capacity, period_key)] = forms.DecimalField(
                    label=f"{capacity} معلماً · {PERIODS[period_key]['label']}",
                    min_value=Decimal("1"),
                    max_digits=10,
                    decimal_places=2,
                    widget=forms.NumberInput(
                        attrs={"class": "form-control", "step": "1", "min": "1", "inputmode": "numeric"}
                    ),
                )

    @staticmethod
    def field_name(capacity: int, period_key: str) -> str:
        return f"price_{capacity}_{period_key}"

    def grid(self):
        """Rows of (capacity, [bound fields]) for template rendering."""
        return [
            (capacity, [self[self.field_name(capacity, key)] for key in self.PERIOD_ORDER])
            for capacity in self.capacities
        ]

    def price_for(self, capacity: int, period_key: str) -> Optional[Decimal]:
        return self.cleaned_data.get(self.field_name(capacity, period_key))

    def clean(self):
        cleaned = super().clean()
        from .flexible_pricing import PERIODS

        # A larger capacity must never cost less than a smaller one, or the
        # interpolated curve between the anchors runs downhill.
        for period_key in self.PERIOD_ORDER:
            previous_capacity = None
            previous_price = None
            for capacity in self.capacities:
                price = cleaned.get(self.field_name(capacity, period_key))
                if price is None:
                    continue
                if previous_price is not None and price <= previous_price:
                    self.add_error(
                        self.field_name(capacity, period_key),
                        f"يجب أن يكون أعلى من سعر سعة {previous_capacity} معلماً "
                        f"({previous_price:,.0f} ريال) في نفس المدة.",
                    )
                previous_capacity = capacity
                previous_price = price

        # A longer commitment must be cheaper than paying monthly for the same
        # span, otherwise nobody has a reason to take it.
        for capacity in self.capacities:
            monthly = cleaned.get(self.field_name(capacity, "1m"))
            if monthly is None:
                continue
            for period_key in ("6m", "1y"):
                price = cleaned.get(self.field_name(capacity, period_key))
                if price is None:
                    continue
                months = Decimal(PERIODS[period_key]["months"])
                if price >= monthly * months:
                    self.add_error(
                        self.field_name(capacity, period_key),
                        f"يجب أن يكون أقل من {monthly * months:,.0f} ريال "
                        f"(سعر {int(months)} أشهر بالسعر الشهري) حتى يقدّم توفيراً حقيقياً.",
                    )

        return cleaned

class DiscountCodeForm(forms.ModelForm):
    class Meta:
        model = DiscountCode
        fields = [
            "code",
            "discount_type",
            "value",
            "max_uses",
            "valid_from",
            "valid_until",
            "is_active",
            "notes",
        ]
        widgets = {
            "code": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "مثال: BACK2SCHOOL",
                    "dir": "ltr",
                    "autocomplete": "off",
                }
            ),
            "discount_type": forms.Select(attrs={"class": "form-select"}),
            "value": forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": "0.01"}),
            "max_uses": forms.NumberInput(attrs={"class": "form-control", "min": "1", "step": "1"}),
            "valid_from": forms.DateInput(attrs={"class": "form-control", "type": "date"}),
            "valid_until": forms.DateInput(attrs={"class": "form-control", "type": "date"}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "notes": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }
        labels = {
            "code": "الكود",
            "discount_type": "نوع الخصم",
            "value": "قيمة الخصم",
            "max_uses": "عدد الاستخدامات الكلي",
            "valid_from": "يسري من",
            "valid_until": "يسري حتى",
            "is_active": "نشط؟",
            "notes": "ملاحظات داخلية",
        }
        help_texts = {
            "code": "أحرف إنجليزية كبيرة وأرقام (والشرطة -)، من 4 إلى 32 خانة.",
            "value": "نسبة مئوية (حتى 100) أو مبلغ بالريال حسب النوع المختار.",
            "max_uses": "إجمالي مرات الاستخدام لجميع المدارس. كل مدرسة تستخدم الكود مرة واحدة فقط.",
            "valid_until": "بعد هذا اليوم يُرفض الكود تلقائياً. اتركه فارغاً بلا انتهاء.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # الكود هوية السجل: تغييره بعد أول استخدام يفصل الاستخدامات عن معناها.
        if getattr(self.instance, "pk", None) and self.instance.redemptions.exists():
            self.fields["code"].disabled = True

    def clean_code(self):
        import re

        if self.fields["code"].disabled:
            return self.instance.code
        code = (self.cleaned_data.get("code") or "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9-]{4,32}", code):
            raise ValidationError(
                "الكود يتكون من أحرف إنجليزية كبيرة وأرقام (والشرطة -)، من 4 إلى 32 خانة."
            )
        duplicate = DiscountCode.objects.filter(code=code)
        if self.instance.pk:
            duplicate = duplicate.exclude(pk=self.instance.pk)
        if duplicate.exists():
            raise ValidationError("يوجد كود آخر بنفس الاسم.")
        return code

    def clean(self):
        cleaned = super().clean()
        discount_type = cleaned.get("discount_type")
        value = cleaned.get("value")
        valid_from = cleaned.get("valid_from")
        valid_until = cleaned.get("valid_until")
        max_uses = cleaned.get("max_uses")

        if (
            discount_type == DiscountCode.DiscountType.PERCENT
            and value is not None
            and value > 100
        ):
            self.add_error("value", "النسبة المئوية لا تتجاوز 100.")
        if valid_from and valid_until and valid_from > valid_until:
            self.add_error("valid_until", "تاريخ الانتهاء يسبق تاريخ البداية.")
        if (
            self.instance.pk
            and max_uses is not None
            and max_uses < self.instance.used_count
        ):
            self.add_error(
                "max_uses",
                f"لا يمكن خفض العدد تحت الاستخدامات المسجلة فعلاً ({self.instance.used_count}).",
            )
        return cleaned
