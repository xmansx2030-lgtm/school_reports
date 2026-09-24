# reports/views/onboarding.py
# -*- coding: utf-8 -*-
"""
Self-service school registration & trial provisioning.

Flow:
1. Principal fills in school details and personal info.
2. A School + Manager account + Trial subscription are created atomically.
3. The user is immediately logged in and redirected to the dashboard.
"""
from __future__ import annotations

import logging

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods
from django_ratelimit.decorators import ratelimit

from ._helpers import _get_active_school
from ..guidance import role_guidance, school_readiness
from ..marketing_attribution import (
    capture_marketing_attribution,
    school_marketing_fields,
)
from ..permissions import role_required
from ..models import (
    School,
    SchoolArchiveAddon,
    SchoolMembership,
    SchoolSubscription,
    SubscriptionPlan,
    Teacher,
)
from ..model_parts.schools import normalize_sa_mobile_identity, normalize_school_name_identity
from ..pricing import FREE_TRIAL_DAYS


# ── Trial policy and resource settings ────────────────────────
TRIAL_DAYS = FREE_TRIAL_DAYS
TRIAL_PLAN_NAME = getattr(settings, "TRIAL_PLAN_NAME", "التجربة المجانية")
TRIAL_MAX_TEACHERS = int(getattr(settings, "TRIAL_MAX_TEACHERS", 5))
TRIAL_ARCHIVE_STORAGE_GB = int(getattr(settings, "TRIAL_ARCHIVE_STORAGE_GB", 1))
REGISTRATION_RECEIPT_SESSION_KEY = "school_registration_receipt"

logger = logging.getLogger(__name__)


def _generate_unique_school_code(school_name: str) -> str:
    """Generate a unique slug-like school code from school name."""
    max_length = School._meta.get_field("code").max_length
    base_code = slugify((school_name or "").strip(), allow_unicode=False) or "school"
    base_code = base_code[:max_length]

    candidate = base_code
    suffix_index = 2
    while School.objects.filter(code=candidate).exists():
        suffix = f"-{suffix_index}"
        prefix_max = max_length - len(suffix)
        candidate = f"{base_code[:prefix_max]}{suffix}"
        suffix_index += 1

    return candidate


# ── Registration form ───────────────────────────────────────────────
class SchoolRegistrationForm(forms.Form):
    _DESCRIBED_BY = {
        "manager_phone": ("id_manager_phone_help",),
        "manager_email": ("id_manager_email_help",),
        "password": ("id_password_help",),
        "accept_policies": ("id_accept_policies_help",),
    }

    # School info
    school_name = forms.CharField(
        label="اسم المدرسة", max_length=200,
        widget=forms.TextInput(
            attrs={
                "class": "twq-control registration-control",
                "placeholder": "مثال: مدرسة الأمل الابتدائية",
                "autocomplete": "organization",
            }
        ),
    )
    stage = forms.ChoiceField(
        label="المرحلة", choices=School.Stage.choices,
        widget=forms.Select(attrs={"class": "twq-control registration-control"}),
    )
    gender = forms.ChoiceField(
        label="بنين / بنات", choices=School.Gender.choices,
        widget=forms.Select(attrs={"class": "twq-control registration-control"}),
    )
    city = forms.CharField(
        label="المدينة", max_length=120, required=False,
        widget=forms.TextInput(
            attrs={
                "class": "twq-control registration-control",
                "placeholder": "مثال: الرياض",
                "autocomplete": "address-level2",
            }
        ),
    )

    # Manager info
    manager_name = forms.CharField(
        label="اسم المسؤول عن إدارة المدرسة", max_length=120,
        widget=forms.TextInput(
            attrs={
                "class": "twq-control registration-control",
                "placeholder": "الاسم الكامل",
                "autocomplete": "name",
            }
        ),
    )
    manager_phone = forms.CharField(
        label="رقم الجوال", max_length=16,
        widget=forms.TextInput(
            attrs={
                "class": "twq-control registration-control",
                "dir": "ltr",
                "placeholder": "05XXXXXXXX",
                "inputmode": "tel",
                "autocomplete": "tel",
            }
        ),
    )
    manager_email = forms.EmailField(
        label="البريد الإلكتروني لإدارة المدرسة",
        required=True,
        widget=forms.EmailInput(
            attrs={
                "class": "twq-control registration-control",
                "dir": "ltr",
                "placeholder": "manager@school.edu.sa",
                "autocomplete": "email",
            }
        ),
    )
    password = forms.CharField(
        label="كلمة المرور", min_length=8,
        widget=forms.PasswordInput(
            attrs={
                "class": "twq-control registration-control",
                "autocomplete": "new-password",
                "placeholder": "8 أحرف على الأقل",
            }
        ),
    )
    password_confirm = forms.CharField(
        label="تأكيد كلمة المرور",
        widget=forms.PasswordInput(
            attrs={
                "class": "twq-control registration-control",
                "autocomplete": "new-password",
                "placeholder": "أعد كتابة كلمة المرور",
            }
        ),
    )
    accept_policies = forms.BooleanField(
        label="أوافق على الشروط والأحكام وسياسة الخصوصية وسياسة الإلغاء والاسترجاع",
        required=True,
        widget=forms.CheckboxInput(attrs={"class": "registration-policy__input"}),
        error_messages={"required": "يلزم الاطلاع على السياسات والموافقة عليها قبل إنشاء الحساب."},
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name, described_by in self._DESCRIBED_BY.items():
            self.fields[field_name].widget.attrs["aria-describedby"] = " ".join(described_by)

    def full_clean(self):
        """Attach rendered error semantics without changing validation rules."""
        super().full_clean()
        for field_name, field in self.fields.items():
            attrs = field.widget.attrs
            attrs.pop("aria-invalid", None)
            described_by = list(self._DESCRIBED_BY.get(field_name, ()))
            if field_name in self.errors:
                attrs["aria-invalid"] = "true"
                described_by.append(f"id_{field_name}_error")
            if described_by:
                attrs["aria-describedby"] = " ".join(described_by)
            else:
                attrs.pop("aria-describedby", None)

    def clean_school_name(self):
        name = " ".join((self.cleaned_data.get("school_name") or "").split())
        if len(name) < 3:
            raise forms.ValidationError("أدخل اسم المدرسة الرسمي.")

        normalized_name = normalize_school_name_identity(name)
        if School.objects.filter(normalized_name=normalized_name).exists():
            raise forms.ValidationError(
                "اسم المدرسة مسجّل مسبقاً. استخدم صفحة الدخول أو اطلب الانضمام للحساب الحالي."
            )
        return name

    def clean_manager_phone(self):
        phone = normalize_sa_mobile_identity(self.cleaned_data.get("manager_phone") or "")

        if len(phone) != 10 or not phone.startswith("05"):
            raise forms.ValidationError("أدخل رقم جوال سعودي صحيحًا يبدأ بـ 05 ويتكون من 10 أرقام.")
        if Teacher.objects.filter(phone=phone).exists():
            raise forms.ValidationError("رقم الجوال مسجّل مسبقاً. استخدم صفحة الدخول.")
        if School.objects.filter(phone=phone).exists():
            raise forms.ValidationError("رقم الجوال مستخدم في مدرسة مسجّلة مسبقاً.")
        return phone

    def clean_manager_email(self):
        email = (self.cleaned_data.get("manager_email") or "").strip().lower()
        if Teacher.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("البريد الإلكتروني مسجّل مسبقاً. استخدم صفحة الدخول.")
        if School.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("البريد الإلكتروني مستخدم في مدرسة مسجّلة مسبقاً.")
        return email

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("password") and cleaned.get("password_confirm"):
            if cleaned["password"] != cleaned["password_confirm"]:
                self.add_error("password_confirm", "كلمتا المرور غير متطابقتين.")
        return cleaned


def _get_or_create_trial_plan() -> SubscriptionPlan:
    """Return the canonical full-feature trial plan with a small user capacity."""
    plan = (
        SubscriptionPlan.objects.filter(name=TRIAL_PLAN_NAME, price=0)
        .order_by("-is_active", "id")
        .first()
    )
    if plan is None:
        return SubscriptionPlan.objects.create(
            name=TRIAL_PLAN_NAME,
            price=0,
            days_duration=TRIAL_DAYS,
            max_teachers=TRIAL_MAX_TEACHERS,
            description=(
                "تجربة كاملة للتقارير وملفات الإنجاز والطلبات والتعاميم والأرشيف"
            ),
            is_active=True,
        )

    changed_fields: list[str] = []
    desired_values = {
        "days_duration": TRIAL_DAYS,
        "max_teachers": TRIAL_MAX_TEACHERS,
        "is_active": True,
    }
    for field_name, desired_value in desired_values.items():
        if getattr(plan, field_name) != desired_value:
            setattr(plan, field_name, desired_value)
            changed_fields.append(field_name)
    if changed_fields:
        plan.save(update_fields=changed_fields)
    return plan


# ── View ─────────────────────────────────────────────────────────────
@ratelimit(key="ip", rate="5/h", method="POST", block=True)
@require_http_methods(["GET", "POST"])
def register_school(request):
    """Self-service school registration with automatic trial subscription."""
    if request.user.is_authenticated:
        return redirect("reports:home")

    capture_marketing_attribution(request)

    if request.method == "POST":
        form = SchoolRegistrationForm(request.POST)
        if form.is_valid():
            try:
                with transaction.atomic():
                    # 1. Create school
                    school = None
                    for _ in range(3):
                        generated_school_code = _generate_unique_school_code(form.cleaned_data["school_name"])
                        try:
                            # Savepoint protects the outer transaction if a rare unique race happens.
                            with transaction.atomic():
                                school = School.objects.create(
                                    name=form.cleaned_data["school_name"],
                                    code=generated_school_code,
                                    stage=form.cleaned_data["stage"],
                                    gender=form.cleaned_data["gender"],
                                    city=form.cleaned_data.get("city") or "",
                                    phone=form.cleaned_data["manager_phone"],
                                    email=form.cleaned_data["manager_email"],
                                    is_active=True,
                                    **school_marketing_fields(request),
                                )
                            break
                        except IntegrityError:
                            school = None
                    if school is None:
                        raise IntegrityError("تعذر توليد كود مدرسة فريد.")

                    # 2. Create manager account
                    manager = Teacher.objects.create_user(
                        phone=form.cleaned_data["manager_phone"],
                        name=form.cleaned_data["manager_name"],
                        email=form.cleaned_data["manager_email"],
                        password=form.cleaned_data["password"],
                    )

                    # 3. Link manager to school
                    SchoolMembership.objects.create(
                        school=school,
                        teacher=manager,
                        role_type=SchoolMembership.RoleType.MANAGER,
                        is_active=True,
                    )

                    # 4. Auto-provision trial subscription
                    plan = _get_or_create_trial_plan()
                    today = timezone.localdate()
                    subscription = SchoolSubscription.objects.create(
                        school=school,
                        plan=plan,
                        start_date=today,
                        end_date=today,
                    )

                    # 5. Give the trial access to the full archive journey too.
                    SchoolArchiveAddon.objects.create(
                        school=school,
                        is_enabled=True,
                        start_date=subscription.start_date,
                        end_date=subscription.end_date,
                        storage_limit_gb=max(1, TRIAL_ARCHIVE_STORAGE_GB),
                        paid_amount=0,
                        notes="مساحة أرشيف تجريبية تُوقف تلقائيًا بانتهاء التجربة المجانية.",
                    )

                # Log in only after the database transaction has committed.
                login(request, manager)
                request.session["active_school_id"] = school.id
                request.session[REGISTRATION_RECEIPT_SESSION_KEY] = {
                    "school_name": school.name,
                    "manager_name": manager.name,
                    "phone": manager.phone,
                    # Shown once on the next no-store page, then removed from session.
                    "password": form.cleaned_data["password"],
                    "trial_days": TRIAL_DAYS,
                    "trial_end_date": subscription.end_date.strftime("%Y/%m/%d"),
                    "teacher_limit": max(1, TRIAL_MAX_TEACHERS),
                    "archive_storage_gb": max(1, TRIAL_ARCHIVE_STORAGE_GB),
                }
                return redirect("reports:registration_success")

            except Exception:
                logger.exception(
                    "School self-registration failed trace_id=%s",
                    getattr(request, "trace_id", None),
                )
                messages.error(request, "تعذر إكمال التسجيل الآن. لم تُحفظ أي بيانات؛ حاول مرة أخرى.")
    else:
        form = SchoolRegistrationForm()

    return render(
        request,
        "reports/register_school.html",
        {
            "form": form,
            "trial_days": TRIAL_DAYS,
            "trial_teacher_limit": max(1, TRIAL_MAX_TEACHERS),
            "trial_archive_storage_gb": max(1, TRIAL_ARCHIVE_STORAGE_GB),
        },
    )


@never_cache
@login_required
@require_GET
def registration_success(request):
    """Show the new manager's credentials once, then remove them from session."""
    receipt = request.session.pop(REGISTRATION_RECEIPT_SESSION_KEY, None)
    if not receipt:
        return redirect("reports:admin_dashboard")
    return render(request, "reports/registration_success.html", {"receipt": receipt})


@login_required(login_url="reports:login")
@require_GET
def role_guidance_center(request):
    """A single, data-backed starting point tailored to the user's active role."""
    active_school = _get_active_school(request)
    journey = role_guidance(request.user, active_school)
    return render(
        request,
        "reports/role_guidance.html",
        {
            "active": "role_guidance",
            "active_school": active_school,
            "journey": journey,
        },
    )


@login_required(login_url="reports:login")
@role_required({"manager"})
@require_GET
def school_health(request):
    """Operational readiness and feature dependencies for the active school."""
    active_school = _get_active_school(request)
    if active_school is None:
        messages.error(request, "اختر مدرسة لعرض صحتها التشغيلية.")
        return redirect("reports:select_school")

    return render(
        request,
        "reports/school_health.html",
        {
            "active": "school_health",
            "active_school": active_school,
            "health": school_readiness(active_school),
        },
    )
