from django.contrib import admin

from .models import PersonalEvidence, PersonalPayment, PersonalPlan, PersonalReport, PersonalSubscription, PersonalWorkspace


@admin.register(PersonalPlan)
class PersonalPlanAdmin(admin.ModelAdmin):
    list_display = (
        "name", "code", "price", "duration_days", "max_reports", "max_evidence",
        "storage_limit_mb", "is_active", "is_published", "display_order",
        "report_ai_daily_limit", "voice_report_daily_limit",
    )
    list_filter = ("is_active", "is_published")
    search_fields = ("name", "code")


@admin.register(PersonalSubscription)
class PersonalSubscriptionAdmin(admin.ModelAdmin):
    list_display = ("workspace", "plan", "start_date", "end_date", "is_active")
    list_filter = ("is_active", "plan")
    search_fields = ("workspace__owner__name", "workspace__owner__phone")


@admin.register(PersonalPayment)
class PersonalPaymentAdmin(admin.ModelAdmin):
    list_display = (
        "created_at", "customer_name", "customer_email", "school_name", "plan_name",
        "amount", "status", "gateway_status", "activated_at", "email_sent_at",
    )
    list_filter = ("status", "gateway_status", "created_at")
    search_fields = (
        "customer_name", "customer_email", "school_name", "gateway_invoice_id",
        "gateway_payment_id", "workspace__owner__phone",
    )
    readonly_fields = tuple(field.name for field in PersonalPayment._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return True

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PersonalWorkspace)
class PersonalWorkspaceAdmin(admin.ModelAdmin):
    list_display = ("owner", "school_name", "principal_name", "created_at")
    search_fields = ("owner__name", "owner__phone", "school_name")


@admin.register(PersonalReport)
class PersonalReportAdmin(admin.ModelAdmin):
    list_display = ("title", "workspace", "academic_year", "status", "report_date")
    list_filter = ("status", "academic_year")
    search_fields = ("title", "workspace__owner__name")
    readonly_fields = ("teacher_name", "school_name", "principal_name")


@admin.register(PersonalEvidence)
class PersonalEvidenceAdmin(admin.ModelAdmin):
    list_display = ("title", "workspace", "academic_year", "file_size", "created_at")
    list_filter = ("academic_year",)
    search_fields = ("title", "workspace__owner__name")
