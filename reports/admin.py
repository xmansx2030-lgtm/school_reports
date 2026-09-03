# reports/admin.py
# -*- coding: utf-8 -*-
from __future__ import annotations

from django import forms
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.db.models import Count
from django.utils import timezone
from django.utils.html import format_html

from .forms import DepartmentForm  # نموذج القسم الذي يحتوي على reporttypes

from .models import (
    Teacher,
    WebAuthnCredential,
    Department,
    ReportType,
    AcademicYear,
    Report,
    ReportEvidence,
    Ticket,
    TicketNote,
    School,
    SchoolAdditionRequest,
    SchoolGroup,
    SchoolGroupMembership,
    SchoolMembership,
    PlatformSettings,
    SubscriptionPlan,
    SchoolSubscription,
    SchoolArchiveAddon,
    SchoolYearArchive,
    SchoolYearArchiveDownload,
    ArchiveStorageOption,
    Payment,
    CustomerComplaint,
    AuditLog,
    AiUsageEvent,
    ErasureRequest,
    SchoolApiKey,
    TeacherTotpDevice,
)

# =========================
# نماذج إدارة المستخدم المخصص (Teacher)
# =========================
class TeacherCreationForm(forms.ModelForm):
    """
    نموذج إنشاء مستخدم في لوحة الإدارة مع حقلي كلمة مرور.
    ملاحظة: is_staff لا يظهر هنا لأنه يُحدَّث تلقائيًا من الدور.
    """
    password1 = forms.CharField(label="كلمة المرور", widget=forms.PasswordInput)
    password2 = forms.CharField(label="تأكيد كلمة المرور", widget=forms.PasswordInput)

    class Meta:
        model = Teacher
        fields = ("phone", "name", "national_id", "is_active")

    def clean_password2(self):
        p1 = self.cleaned_data.get("password1")
        p2 = self.cleaned_data.get("password2")
        if p1 and p2 and p1 != p2:
            raise forms.ValidationError("كلمتا المرور غير متطابقتين.")
        return p2

    def save(self, commit=True):
        user = super().save(commit=False)
        # تعيين كلمة المرور
        user.set_password(self.cleaned_data["password1"])
        if commit:
            user.save()
        return user


class TeacherChangeForm(forms.ModelForm):
    """
    نموذج تعديل مستخدم في لوحة الإدارة (لا يظهر كلمة المرور الحقيقية).
    is_staff للعرض فقط (read-only) لأنه يُحدَّث تلقائيًا حسب الدور.
    """
    class Meta:
        model = Teacher
        fields = (
            "phone",
            "name",
            "national_id",
            "is_active",
            "passkey_prompt_opt_out",
            "is_superuser",
            "groups",
            "user_permissions",
        )


# =========================
# إدارة المعلمين (Teacher)
# =========================
@admin.register(Teacher)
class TeacherAdmin(UserAdmin):
    add_form = TeacherCreationForm
    form = TeacherChangeForm
    model = Teacher

    list_display = ("name", "phone", "national_id", "is_active", "is_staff")
    list_filter = ("is_active", "is_staff", "is_superuser", "groups")
    search_fields = ("name", "phone", "national_id")
    ordering = ("name",)

    fieldsets = (
        (None, {"fields": ("phone", "password")}),
        ("المعلومات الشخصية", {"fields": ("name", "national_id")}),
        ("تفضيلات الأمان", {"fields": ("passkey_prompt_opt_out",)}),
        (
            "الصلاحيات",
            {
                "fields": (
                    "is_active",
                    "is_staff",       # للعرض فقط
                    "is_superuser",
                    "groups",
                    "user_permissions",
                )
            },
        ),
        ("تواريخ النظام", {"fields": ("last_login",)}),
    )
    readonly_fields = ("last_login", "is_staff")

    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": (
                    "phone",
                    "name",
                    "national_id",
                    "password1",
                    "password2",
                    "is_active",
                ),
            },
        ),
    )

    # ملاحظة مقصودة: حذف الحساب لم يعد يمحو سجل إجراءاته.
    # ``AuditLog.teacher`` صار SET_NULL مع لقطة اسم الفاعل، فيبقى الأثر منسوباً
    # لصاحبه بعد رحيل الحساب. محوُه كان يجعل حذف الحساب أداةً لطمس ما فعله.


@admin.register(WebAuthnCredential)
class WebAuthnCredentialAdmin(admin.ModelAdmin):
    list_display = ("teacher", "device_name", "is_active", "last_used_at", "created_at")
    list_filter = ("is_active", "created_at", "last_used_at")
    search_fields = ("teacher__name", "teacher__phone", "device_name", "credential_id_hash")
    readonly_fields = ("credential_id_hash", "last_used_at", "created_at")


# =========================
# إدارة التصنيفات/الأقسام (ديناميكي)
# =========================
@admin.register(ReportType)
class ReportTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "order", "is_active", "created_at", "updated_at")
    list_filter = ("is_active", "created_at", "updated_at")
    search_fields = ("name", "code", "description")
    list_editable = ("order", "is_active")
    ordering = ("order", "name")
    prepopulated_fields = {"code": ("name",)}


@admin.register(AcademicYear)
class AcademicYearAdmin(admin.ModelAdmin):
    list_display = ("value", "is_active", "order", "created_at")
    list_filter = ("is_active",)
    list_editable = ("is_active", "order")
    search_fields = ("value",)
    ordering = ("-value",)


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    # ✅ تسجيل واحد فقط للقسم — لا تكرار!
    form = DepartmentForm  # يحتوي على حقل reporttypes
    list_display = ("name", "slug", "role_label", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "slug", "role_label")
    prepopulated_fields = {"slug": ("name",)}
    filter_horizontal = ("reporttypes",)  # اختيار متعدد لأنواع التقارير


# =========================
# إدارة التقارير (Report)
# =========================
class ReportEvidenceInline(admin.TabularInline):
    model = ReportEvidence
    extra = 0
    fields = ("order", "image", "description", "display_size", "fit_mode", "show_in_print")


@admin.register(Report)
class ReportAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "teacher",
        "category",
        "report_date",
        "day_name",
        "beneficiaries_count",
        "created_at",
        "preview_image1",
    )
    list_filter = ("category", "report_date", "created_at", "teacher")
    search_fields = (
        "title",
        "idea",
        "teacher__name",
        "teacher__phone",
        "teacher__national_id",
        "category__name",
        "category__code",
    )
    date_hierarchy = "report_date"
    autocomplete_fields = ("teacher", "category")
    list_select_related = ("teacher", "category")
    readonly_fields = ("created_at",)
    inlines = (ReportEvidenceInline,)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if not request.user.is_superuser:
            school_ids = SchoolMembership.objects.filter(
                teacher=request.user, is_active=True
            ).values_list("school_id", flat=True)
            qs = qs.filter(school_id__in=school_ids)
        return qs

    def preview_image1(self, obj):
        evidence = obj.evidences.order_by("order", "id").first()
        if evidence and evidence.image:
            return format_html(
                '<img src="{}" width="60" height="60" style="object-fit:contain;border-radius:6px;" />',
                evidence.image.url,
            )
        if getattr(obj, "image1", None):
            url = getattr(getattr(obj, "image1", None), "url", "")
            if url:
                return format_html(
                    '<img src="{}" width="60" height="60" style="object-fit:cover;border-radius:6px;" />',
                    url,
                )
        return "—"

    preview_image1.short_description = "معاينة الصورة"


# =========================
# إدارة التذاكر والملاحظات (Ticket / TicketNote)
# =========================
class TicketNoteInline(admin.TabularInline):
    model = TicketNote
    extra = 0
    fields = ("author", "is_public", "body", "created_at")
    readonly_fields = ("created_at",)
    autocomplete_fields = ("author",)


@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "title",
        "is_platform",
        "status",
        "department",
        "creator",
        "assignee",
        "created_at",
        "updated_at",
    )
    list_filter = ("is_platform", "status", "department", "created_at", "updated_at", "assignee")
    search_fields = (
        "id",
        "title",
        "body",
        "creator__name",
        "creator__phone",
        "assignee__name",
        "assignee__phone",
        "department__name",
        "department__slug",
    )
    date_hierarchy = "created_at"
    autocomplete_fields = ("creator", "assignee", "department")
    list_select_related = ("creator", "assignee", "department")
    readonly_fields = ("created_at", "updated_at")
    inlines = (TicketNoteInline,)

    fieldsets = (
        (None, {"fields": ("title", "body", "attachment", "is_platform")}),
        ("الملكية والتعيين", {"fields": ("creator", "assignee", "department")}),
        ("الحالة", {"fields": ("status",)}),
        ("أخرى", {"fields": ("created_at", "updated_at")}),
    )

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if not request.user.is_superuser:
            school_ids = SchoolMembership.objects.filter(
                teacher=request.user, is_active=True
            ).values_list("school_id", flat=True)
            qs = qs.filter(school_id__in=school_ids)
        return qs


@admin.register(TicketNote)
class TicketNoteAdmin(admin.ModelAdmin):
    list_display = ("id", "ticket", "author", "is_public", "created_at")
    list_filter = ("is_public", "created_at", "author")
    search_fields = ("ticket__id", "ticket__title", "body", "author__name")
    autocomplete_fields = ("ticket", "author")
    date_hierarchy = "created_at"
    readonly_fields = ("created_at",)


# =========================
# إدارة المدارس وعضوياتها
# =========================
class SchoolGroupMembershipInline(admin.TabularInline):
    model = SchoolGroupMembership
    extra = 0
    autocomplete_fields = ("user",)
    verbose_name = "مدير تنفيذي"
    verbose_name_plural = "المدير التنفيذي"


@admin.register(SchoolGroup)
class SchoolGroupAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "education_department", "schools_count", "is_active", "created_at")
    list_filter = ("is_active", "education_department")
    search_fields = ("name", "code", "education_department")
    prepopulated_fields = {"code": ("name",)}
    inlines = (SchoolGroupMembershipInline,)

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_schools_count=Count("schools"))

    @admin.display(description="عدد المدارس", ordering="_schools_count")
    def schools_count(self, obj):
        return getattr(obj, "_schools_count", 0)


@admin.register(SchoolGroupMembership)
class SchoolGroupMembershipAdmin(admin.ModelAdmin):
    list_display = ("user", "group", "role_type", "is_active", "created_at")
    list_filter = ("role_type", "is_active")
    search_fields = ("user__name", "user__phone", "group__name", "group__code")
    autocomplete_fields = ("user", "group")


@admin.register(School)
class SchoolAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "code",
        "marketing_source",
        "marketing_campaign",
        "current_academic_year",
        "is_active",
        "created_at",
    )
    list_filter = ("is_active", "marketing_source", "marketing_medium", "created_at")
    search_fields = ("name", "code", "marketing_campaign", "marketing_click_id")
    prepopulated_fields = {"code": ("name",)}
    readonly_fields = (
        "marketing_source",
        "marketing_medium",
        "marketing_campaign",
        "marketing_content",
        "marketing_term",
        "marketing_click_id",
        "marketing_referrer",
        "created_at",
        "updated_at",
    )

    # عرض سجل العمليات الخاصة بهذه المدرسة داخل صفحة المدرسة في Django Admin
    inlines = ()

    def has_delete_permission(self, request, obj=None):
        # Only superusers can delete schools.
        return bool(getattr(request.user, "is_superuser", False))

    def get_actions(self, request):
        actions = super().get_actions(request)
        if not bool(getattr(request.user, "is_superuser", False)):
            actions.pop("delete_selected", None)
        return actions

    def delete_model(self, request, obj):
        from .middleware import set_audit_logging_suppressed

        set_audit_logging_suppressed(True)
        try:
            return super().delete_model(request, obj)
        finally:
            set_audit_logging_suppressed(False)

    def delete_queryset(self, request, queryset):
        from .middleware import set_audit_logging_suppressed

        set_audit_logging_suppressed(True)
        try:
            return super().delete_queryset(request, queryset)
        finally:
            set_audit_logging_suppressed(False)


class AuditLogInline(admin.TabularInline):
    model = AuditLog
    extra = 0
    can_delete = False
    show_change_link = False
    fields = ("timestamp", "teacher", "action", "model_name", "object_repr", "ip_address")
    readonly_fields = fields
    ordering = ("-timestamp",)

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


# ربط الـ inline بعد تعريفه (حتى لا يتطلب ترتيب تعريفات مختلف)
SchoolAdmin.inlines = (AuditLogInline,)


@admin.register(SchoolMembership)
class SchoolMembershipAdmin(admin.ModelAdmin):
    list_display = ("teacher", "school", "role_type", "is_active", "created_at")
    list_filter = ("role_type", "is_active", "school")
    search_fields = ("teacher__name", "teacher__phone", "school__name", "school__code")
    autocomplete_fields = ("teacher", "school")
    list_select_related = ("teacher", "school")


from django.contrib import admin
from .models import Notification, NotificationRecipient, WebPushDelivery, WebPushSubscription

@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "is_important", "created_by", "created_at", "expires_at")
    search_fields = ("title", "message")
    list_filter = ("is_important", "created_at")
    list_select_related = ("created_by",)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if not request.user.is_superuser:
            school_ids = SchoolMembership.objects.filter(
                teacher=request.user, is_active=True
            ).values_list("school_id", flat=True)
            qs = qs.filter(school_id__in=school_ids)
        return qs

@admin.register(NotificationRecipient)
class NotificationRecipientAdmin(admin.ModelAdmin):
    list_display = ("id", "notification", "teacher", "is_read", "created_at", "read_at")
    list_filter = ("is_read", "created_at")
    search_fields = ("notification__title", "teacher__name")
    list_select_related = ("notification", "teacher")


@admin.register(WebPushSubscription)
class WebPushSubscriptionAdmin(admin.ModelAdmin):
    list_display = ("id", "teacher", "is_active", "failure_count", "last_success_at", "updated_at")
    list_filter = ("is_active", "updated_at")
    search_fields = ("teacher__name", "teacher__phone", "endpoint")
    readonly_fields = (
        "teacher", "endpoint", "p256dh", "auth", "user_agent", "failure_count",
        "last_success_at", "created_at", "updated_at",
    )
    list_select_related = ("teacher",)

    def has_add_permission(self, request):
        return False


@admin.register(WebPushDelivery)
class WebPushDeliveryAdmin(admin.ModelAdmin):
    list_display = ("id", "notification", "subscription", "status", "attempts", "sent_at")
    list_filter = ("status", "created_at")
    search_fields = ("notification__title", "subscription__teacher__name")
    readonly_fields = (
        "subscription", "notification", "status", "attempts", "last_error",
        "last_attempt_at", "sent_at", "created_at",
    )
    list_select_related = ("notification", "subscription", "subscription__teacher")

    def has_add_permission(self, request):
        return False


# =========================
# إدارة الاشتراكات والمالية
# =========================
@admin.register(SchoolAdditionRequest)
class SchoolAdditionRequestAdmin(admin.ModelAdmin):
    list_display = (
        "school_name", "requested_by", "status",
        "created_school", "created_at", "reviewed_at",
    )
    list_filter = ("status", "stage", "gender", "created_at")
    search_fields = ("school_name", "city", "requested_by__name", "requested_by__phone")
    list_select_related = ("requested_by", "created_school", "reviewed_by")


@admin.register(SubscriptionPlan)
class SubscriptionPlanAdmin(admin.ModelAdmin):
    list_display = (
        "name", "price", "days_duration", "max_teachers", "support_level",
        "onboarding_sessions", "included_archive_storage_gb", "is_active", "created_at",
    )
    list_filter = ("is_active", "support_level", "created_at")
    search_fields = ("name", "description")
    ordering = ("price",)


@admin.register(SchoolSubscription)
class SchoolSubscriptionAdmin(admin.ModelAdmin):
    list_display = ("school", "plan", "teacher_limit", "start_date", "end_date", "is_active", "is_expired")
    list_filter = ("is_active", "plan", "start_date", "end_date")
    search_fields = ("school__name", "school__code")
    autocomplete_fields = ("school", "plan")
    date_hierarchy = "start_date"

    def save_model(self, request, obj, form, change):
        """عند إضافة/تحديث اشتراك من Django Admin نسجل عملية مالية تلقائياً.

        الهدف: أي اشتراك يُفعّل يدوياً من الأدمن يجب أن يظهر في صفحة المالية بدون الحاجة لرفع إيصال.
        """
        prev = None
        if change and getattr(obj, "pk", None):
            try:
                prev = SchoolSubscription.objects.filter(pk=obj.pk).only(
                    "is_active",
                    "canceled_at",
                    "cancel_reason",
                    "start_date",
                    "end_date",
                    "plan_id",
                ).first()
            except Exception:
                prev = None

        super().save_model(request, obj, form, change)

        # ✅ إذا تم إلغاء الاشتراك (إلغاء مقصود)، نسجل حدث الإلغاء في المالية وسجل عمليات المدرسة.
        # لا نعتبر is_active=False وحده إلغاءً لأنه قد يُستخدم للإيقاف المؤقت.
        try:
            became_cancelled = bool(getattr(obj, "is_cancelled", False)) and (not bool(getattr(prev, "is_cancelled", False)))
        except Exception:
            became_cancelled = False

        if became_cancelled:
            try:
                today = timezone.localdate()
                reason = (getattr(obj, "cancel_reason", "") or "").strip()
                note = "تم إلغاء الاشتراك بواسطة Django Admin."
                if reason:
                    note = f"{note}\nسبب الإلغاء: {reason}"

                # 1) سجل حدث الإلغاء نفسه
                exists_cancel_event = Payment.objects.filter(
                    subscription=obj,
                    status=Payment.Status.CANCELLED,
                    payment_date=today,
                    amount=0,
                ).exists()
                if not exists_cancel_event:
                    Payment.objects.create(
                        school=obj.school,
                        subscription=obj,
                        requested_plan=obj.plan,
                        requested_teacher_limit=obj.teacher_limit,
                        amount=0,
                        receipt_image=None,
                        payment_date=today,
                        status=Payment.Status.CANCELLED,
                        notes=note,
                        created_by=getattr(request, "user", None),
                    )

                # 2) إلغاء أي مدفوعات معلّقة تخص فترة الاشتراك (تحصين حتى لا تُعتمد لاحقًا)
                period_start = getattr(obj, "start_date", None)
                pending_qs = Payment.objects.filter(subscription=obj, status=Payment.Status.PENDING)
                if period_start:
                    pending_qs = pending_qs.filter(payment_date__gte=period_start)
                for p in pending_qs.only("id", "status", "notes"):
                    p.status = Payment.Status.CANCELLED
                    p.notes = (f"{p.notes}\n" if (p.notes or "").strip() else "") + note
                    p.save(update_fields=["status", "notes", "updated_at"])
            except Exception:
                # لا نُفشل حفظ الاشتراك بسبب مشكلة تسجيل المالية.
                return

        # لا نسجل دفعات لاشتراك غير نشط أو باقة مجانية.
        try:
            if not bool(getattr(obj, "is_active", False)):
                return
            plan = getattr(obj, "plan", None)
            price = getattr(plan, "price", None)
            if price is None:
                return
            try:
                if float(price) <= 0:
                    return
            except Exception:
                pass

            period_start = getattr(obj, "start_date", None)
            qs = Payment.objects.filter(
                subscription=obj,
                requested_plan=obj.plan,
                status__in=[Payment.Status.PENDING, Payment.Status.APPROVED],
            )
            if period_start:
                qs = qs.filter(created_at__date__gte=period_start)
            if qs.exists():
                return

            today = timezone.localdate()
            Payment.objects.create(
                school=obj.school,
                subscription=obj,
                requested_plan=obj.plan,
                requested_teacher_limit=obj.teacher_limit,
                amount=obj.plan.price,
                receipt_image=None,
                payment_date=today,
                status=Payment.Status.APPROVED,
                notes="تم تسجيل الدفعة تلقائياً عند إضافة/تفعيل الاشتراك من Django Admin.",
                created_by=getattr(request, "user", None),
            )
        except Exception:
            # لا نُفشل حفظ الاشتراك بسبب مشكلة تسجيل المالية.
            return


@admin.register(PlatformSettings)
class PlatformSettingsAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "maintenance_mode_enabled",
        "mansour_public_enabled",
        "report_ai_enabled",
        "internal_ai_help_enabled",
        "share_link_default_days",
        "archive_addon_annual_price",
        "archive_included_storage_gb",
        "free_storage_mb",
        "updated_at",
    )
    readonly_fields = ("created_at", "updated_at")
    fields = (
        "maintenance_mode_enabled",
        "maintenance_message",
        "mansour_public_enabled",
        "report_ai_enabled",
        "internal_ai_help_enabled",
        "share_link_default_days",
        "archive_addon_annual_price",
        "archive_included_storage_gb",
        "storage_mb_per_teacher",
        "free_storage_mb",
        "updated_by",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        if PlatformSettings.objects.exists():
            return False
        return super().has_add_permission(request)


@admin.register(SchoolArchiveAddon)
class SchoolArchiveAddonAdmin(admin.ModelAdmin):
    list_display = ("school", "is_enabled", "start_date", "end_date", "storage_limit_gb", "paid_amount", "is_active", "days_remaining")
    list_filter = ("is_enabled", "start_date", "end_date")
    search_fields = ("school__name", "school__code", "notes")
    autocomplete_fields = ("school",)
    date_hierarchy = "start_date"


@admin.register(ArchiveStorageOption)
class ArchiveStorageOptionAdmin(admin.ModelAdmin):
    list_display = ("bucket", "storage_gb", "price", "is_active", "sort_order", "updated_at")
    list_filter = ("bucket", "is_active")
    ordering = ("bucket", "sort_order", "storage_gb", "id")


@admin.register(SchoolYearArchive)
class SchoolYearArchiveAdmin(admin.ModelAdmin):
    list_display = (
        "school",
        "academic_year",
        "version",
        "status",
        "file_count",
        "leadership_count",
        "ticket_count",
        "circular_count",
        "storage_bytes",
        "created_by",
        "created_at",
    )
    list_filter = ("status", "academic_year", "created_at")
    search_fields = ("school__name", "school__code", "academic_year", "archive_sha256")
    autocomplete_fields = ("school", "created_by")
    readonly_fields = (
        "school",
        "academic_year",
        "version",
        "status",
        "archive_file",
        "storage_bytes",
        "archive_sha256",
        "file_count",
        "missing_file_count",
        "failed_pdf_count",
        "report_count",
        "achievement_count",
        "leadership_count",
        "ticket_count",
        "circular_count",
        "notification_count",
        "notes",
        "created_by",
        "created_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return bool(request.user.is_superuser)

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SchoolYearArchiveDownload)
class SchoolYearArchiveDownloadAdmin(admin.ModelAdmin):
    list_display = ("archive", "downloaded_by", "downloaded_at")
    list_filter = ("downloaded_at",)
    search_fields = ("archive__school__name", "archive__academic_year", "downloaded_by__name")
    readonly_fields = ("archive", "downloaded_by", "downloaded_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "school",
        "purpose",
        "requested_plan",
        "requested_teacher_limit",
        "amount",
        "status",
        "effects_applied_at",
        "payment_date",
        "created_at",
    )
    list_filter = ("purpose", "status", "payment_date", "created_at")
    search_fields = ("school__name", "notes")
    autocomplete_fields = ("school", "requested_plan")
    date_hierarchy = "created_at"
    readonly_fields = ("effects_applied_at", "created_at")


@admin.register(CustomerComplaint)
class CustomerComplaintAdmin(admin.ModelAdmin):
    list_display = ("reference", "subject", "name", "status", "created_at", "updated_at")
    list_filter = ("status", "created_at")
    search_fields = ("name", "email", "phone", "order_reference", "subject", "message")
    readonly_fields = (
        "name",
        "email",
        "phone",
        "order_reference",
        "subject",
        "message",
        "created_at",
        "updated_at",
    )
    date_hierarchy = "created_at"

    def save_model(self, request, obj, form, change):
        if change and obj.status == CustomerComplaint.Status.RESOLVED and not obj.resolved_at:
            obj.resolved_at = timezone.now()
        super().save_model(request, obj, form, change)

    def has_add_permission(self, request):
        return False


@admin.register(AiUsageEvent)
class AiUsageEventAdmin(admin.ModelAdmin):
    """قراءة فقط: هذه وقائع قياس، وتعديلها يُفسد الرقم الذي تقيسه."""

    list_display = (
        "created_at", "stage", "model_name", "outcome",
        "input_tokens", "cached_display", "output_tokens",
        "duration_ms", "estimated_cost", "school",
    )
    list_filter = ("stage", "outcome", "model_name", "created_at")
    search_fields = ("school__name", "teacher__name", "model_name", "error_kind")
    readonly_fields = tuple(
        field.name for field in AiUsageEvent._meta.fields
    )
    date_hierarchy = "created_at"
    list_select_related = ("school",)

    @admin.display(description="المخزَّن")
    def cached_display(self, obj):
        if not obj.input_tokens:
            return "—"
        return f"{obj.cached_input_tokens:,} ({obj.cache_hit_ratio:.0%})"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("timestamp", "actor_display", "actor_role", "action", "model_name", "object_repr", "school", "ip_address")
    list_filter = ("action", "model_name", "timestamp", "school")
    search_fields = ("teacher__name", "actor_name", "object_repr", "ip_address", "changes")
    readonly_fields = (
        "timestamp", "teacher", "actor_name", "actor_role", "action", "model_name",
        "object_id", "object_repr", "changes", "ip_address", "user_agent", "school",
    )
    date_hierarchy = "timestamp"

    @admin.display(description="الفاعل")
    def actor_display(self, obj):
        return obj.actor_display

    # السجل غير قابل للتعديل ولا للحذف — حتى من لوحة Django. الحذف الوحيد
    # المشروع يمر عبر أمر الاحتفاظ cleanup_audit_logs الذي يؤرشف قبل أن يحذف.
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_model_perms(self, request):
        """إظهار الموديل في قائمة Django Admin حتى لو لم تُمنح صلاحية view صراحةً للموظف.

        يظل الوصول فعلياً محكوماً بـ get_queryset (تصفية حسب عضوية المدارس) وبأن الصفحة read-only.
        """
        perms = super().get_model_perms(request)
        user = getattr(request, "user", None)
        if user is None:
            return perms
        if getattr(user, "is_superuser", False):
            return perms
        if getattr(user, "is_staff", False):
            perms["view"] = True
            perms["add"] = False
            perms["change"] = False
            perms["delete"] = False
        return perms

    def has_view_permission(self, request, obj=None):
        user = getattr(request, "user", None)
        return bool(user and getattr(user, "is_staff", False))

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        user = getattr(request, "user", None)
        if user is None:
            return qs.none()
        if getattr(user, "is_superuser", False):
            return qs

        # تقييد سجل العمليات داخل لوحة Django Admin:
        # مدير المدرسة/الموظف يرى فقط سجلات المدارس المرتبط بها عبر العضويات النشطة.
        from django.db.models import Q
        from .models import SchoolMembership

        allowed_school_ids = list(
            SchoolMembership.objects.filter(teacher=user, is_active=True).values_list("school_id", flat=True)
        )
        if not allowed_school_ids:
            # لا توجد عضويات: لا نُظهر سجلات (بدلاً من عرض كل شيء بالخطأ)
            return qs.none()

        qs = qs.filter(school_id__in=allowed_school_ids)

        # لا نعرض سجلات أنشأها مستخدمون خارج المدرسة (مثل مالك النظام)
        # حتى لو أثّرت على المدرسة، لتجنب خلط السجلات بين المدارس.
        qs = qs.filter(
            Q(teacher__isnull=True)
            | Q(
                teacher__school_memberships__school_id__in=allowed_school_ids,
                teacher__school_memberships__is_active=True,
            )
        ).distinct()
        return qs

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        # فقط السوبر يوزر يمكنه الحذف (اختياري)
        return request.user.is_superuser


@admin.register(ErasureRequest)
class ErasureRequestAdmin(admin.ModelAdmin):
    """طلبات الإتلاف — لوحة البتّ فيها.

    الطلب حقٌّ نظامي بمدد ردٍّ مقررة، فلا يجوز أن يعيش في بريدٍ يُنسى.
    وترتيبُ العرض بالأقدم أولاً مقصود: الأقدم هو الأقرب إلى تجاوز المدة.
    """

    list_display = ("teacher", "status", "created_at", "resolved_at", "resolved_by")
    list_filter = ("status", "created_at")
    search_fields = ("teacher__name", "teacher__phone", "reason", "response_note")
    autocomplete_fields = ("teacher", "resolved_by")
    readonly_fields = ("teacher", "reason", "created_at")
    ordering = ("status", "created_at")

    def has_add_permission(self, request):
        # الطلب يُنشئه صاحبه من شاشته وحده — إنشاؤه من هنا ينتحل إرادته.
        return False

    def save_model(self, request, obj, form, change):
        # البتّ يُختم بوقته وصاحبه تلقائياً: تركُ ذلك يدوياً يُنتج سجلاً ناقصاً
        # في أكثر ما يحتاج إثباتاً.
        if obj.status in {ErasureRequest.Status.COMPLETED, ErasureRequest.Status.REFUSED}:
            if obj.resolved_at is None:
                obj.resolved_at = timezone.now()
            if obj.resolved_by is None:
                obj.resolved_by = request.user
        else:
            obj.resolved_at = None
            obj.resolved_by = None
        super().save_model(request, obj, form, change)


@admin.register(SchoolApiKey)
class SchoolApiKeyAdmin(admin.ModelAdmin):
    """مفاتيح التكامل — للاطلاع والإبطال، لا للإنشاء.

    الإنشاء من شاشة المدير وحدها لأن السرّ يُعرض مرة واحدة عند التوليد؛ ونموذجُ
    الأدمن لا يستطيع عرضه، فمفتاحٌ يُنشأ هنا يُولَد ميتاً — لا أحد يعرف قيمته.
    """

    list_display = ("name", "school", "public_id", "scope", "acting_as", "is_active", "last_used_at")
    list_filter = ("scope", "is_active", "created_at")
    search_fields = ("name", "public_id", "school__name", "acting_as__name")
    autocomplete_fields = ("school", "acting_as", "created_by")
    # التجزئة والمعرِّف لا يُحرَّران: تغييرهما يفصل الصفَّ عن أي مفتاح موجود.
    readonly_fields = ("public_id", "key_hash", "last_used_at", "created_at", "created_by")

    def has_add_permission(self, request):
        return False


@admin.register(TeacherTotpDevice)
class TeacherTotpDeviceAdmin(admin.ModelAdmin):
    """أجهزة المصادقة الثنائية — للاطلاع والإزالة عند فقد الجهاز.

    السرّ لا يُعرض ولا يُحرَّر: هو مُعمّى في القاعدة، وعرضُه في لوحةٍ يُبطل
    كونَه عاملاً ثانياً. وما يحتاجه الدعم فعلاً هو **الإزالة** لمن فقد هاتفه
    واستنفد رموز الاسترجاع — لا الاطلاع.
    """

    list_display = ("teacher", "is_confirmed", "confirmed_at", "last_used_at")
    list_filter = ("confirmed_at",)
    search_fields = ("teacher__name", "teacher__phone")
    autocomplete_fields = ("teacher",)
    readonly_fields = ("teacher", "confirmed_at", "last_used_at", "created_at")
    exclude = ("secret_encrypted", "last_used_counter")

    def has_add_permission(self, request):
        # التسجيل يمرّ بإثبات رمزٍ عامل. جهازٌ يُنشأ هنا يُقفل صاحبه خارج حسابه.
        return False

    @admin.display(boolean=True, description="نافذ")
    def is_confirmed(self, obj):
        return obj.is_confirmed


from .models import DiscountCode, DiscountRedemption


@admin.register(DiscountCode)
class DiscountCodeAdmin(admin.ModelAdmin):
    list_display = (
        "code", "discount_type", "value", "max_uses", "used_count",
        "valid_from", "valid_until", "is_active", "created_at",
    )
    list_filter = ("discount_type", "is_active", "created_at")
    search_fields = ("code", "notes")
    readonly_fields = ("created_by", "created_at", "updated_at")

    def used_count(self, obj):
        return obj.used_count

    used_count.short_description = "الاستخدامات"


@admin.register(DiscountRedemption)
class DiscountRedemptionAdmin(admin.ModelAdmin):
    list_display = ("code", "school", "payment", "amount_discounted", "created_at")
    list_filter = ("created_at",)
    search_fields = ("code__code", "school__name", "school__code")
    list_select_related = ("code", "school", "payment")
    autocomplete_fields = ("code", "school")
