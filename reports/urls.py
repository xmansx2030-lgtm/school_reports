# reports/urls.py
from django.urls import path
from . import views

app_name = "reports"

urlpatterns = [
    # =========================
    # الدخول والخروج
    # =========================
    path("", views.platform_landing, name="landing"),
    path("assistant/mansour/", views.mansour_assistant_reply, name="mansour_assistant_reply"),
    path("guide/", views.user_guide, name="user_guide"),
    path("guide/my-role/", views.role_guidance_center, name="role_guidance"),
    path("guide/download/", views.user_guide_download, name="user_guide_download"),
    path("guide/download/pdf/", views.user_guide_download_pdf, name="user_guide_download_pdf"),
    path("login/", views.login_view, name="login"),
    path("password-reset/", views.AccountPasswordResetView.as_view(), name="password_reset"),
    path("password-reset/done/", views.AccountPasswordResetDoneView.as_view(), name="password_reset_done"),
    path(
        "password-reset/confirm/<uidb64>/<token>/",
        views.AccountPasswordResetConfirmView.as_view(),
        name="password_reset_confirm",
    ),
    path(
        "password-reset/complete/",
        views.AccountPasswordResetCompleteView.as_view(),
        name="password_reset_complete",
    ),
    path("platform-login/", views.login_view, {"admin_only": True}, name="platform_login"),
    path("login/passkey/options/", views.passkey_login_options, name="passkey_login_options"),
    path("login/passkey/verify/", views.passkey_login_verify, name="passkey_login_verify"),
    path("register/", views.register_school, name="register_school"),
    path("register/success/", views.registration_success, name="registration_success"),
    path("logout/", views.logout_view, name="logout"),
    path("profile/", views.my_profile, name="my_profile"),
    # سجل الإجراءات كما يراه صاحبه — متاح لكل مستخدم، ومقيّد بنفسه بحكم البناء.
    path("profile/activity/", views.my_activity_log, name="my_activity_log"),
    # حقوق صاحب البيانات (نظام حماية البيانات الشخصية): نسخة مقروءة فورية،
    # وطلب إتلاف مسجَّل ومُتتبَّع.
    path("profile/my-data/", views.my_data, name="my_data"),
    path("profile/my-data/download/", views.my_data_download, name="my_data_download"),
    path("profile/my-data/erasure/", views.request_erasure, name="request_erasure"),
    # مفاتيح التكامل — لمدير المدرسة وحده.
    path("integrations/keys/", views.api_keys_list, name="api_keys"),
    path("integrations/keys/create/", views.api_key_create, name="api_key_create"),
    path("integrations/keys/<int:pk>/revoke/", views.api_key_revoke, name="api_key_revoke"),
    # المصادقة الثنائية (TOTP) — إلى جانب Passkeys لا بدلاً منها.
    path("security/two-factor/", views.totp_settings, name="totp_settings"),
    path("security/two-factor/start/", views.totp_begin_enrollment, name="totp_begin_enrollment"),
    path("security/two-factor/confirm/", views.totp_confirm_enrollment, name="totp_confirm_enrollment"),
    path("security/two-factor/disable/", views.totp_disable, name="totp_disable"),
    path("login/two-factor/", views.totp_challenge, name="totp_challenge"),
    path("profile/work/", views.my_work_archive, name="my_work_archive"),
    path("profile/passkey/register/options/", views.passkey_register_options, name="passkey_register_options"),
    path("profile/passkey/register/verify/", views.passkey_register_verify, name="passkey_register_verify"),
    path("profile/passkey/prompt/dismiss/", views.passkey_enroll_prompt_dismiss, name="passkey_enroll_prompt_dismiss"),
    path("profile/passkey/<int:pk>/delete/", views.passkey_delete, name="passkey_delete"),

    # =========================
    # الصفحة الرئيسية
    # =========================
    path("home/", views.home, name="home"),
    # مؤشرات نطاق الوكيل والموظف الإداري — ليست لوحة المدير مصغَّرةً بل ما
    # يقع تحت إشرافهما وحده.
    path("scope/", views.staff_dashboard, name="staff_dashboard"),

    # =========================
    # المختبر (محضّر المختبر)
    # =========================
    path("lab/", views.lab_dashboard, name="lab_dashboard"),
    path("lab/assets/", views.lab_assets, name="lab_assets"),
    path("lab/assets/print/", views.lab_assets_print, name="lab_assets_print"),
    path("lab/assets/<int:pk>/", views.lab_asset_detail, name="lab_asset_detail"),
    path("lab/assets/<int:pk>/action/", views.lab_asset_action, name="lab_asset_action"),
    path("lab/experiments/", views.lab_experiments, name="lab_experiments"),
    path("lab/experiments/<int:pk>/", views.lab_experiment_detail, name="lab_experiment_detail"),
    path("lab/experiments/<int:pk>/action/", views.lab_experiment_action, name="lab_experiment_action"),
    path("lab/experiments/<int:pk>/print/", views.lab_experiment_print, name="lab_experiment_print"),

    # =========================
    # التقارير (للمعلّم)
    # =========================
    path("reports/add/", views.add_report, name="add_report"),
    path("reports/ai/improve/", views.improve_report_text, name="improve_report_text"),
    path("reports/ai/voice/", views.transcribe_report_voice, name="transcribe_report_voice"),
    path("reports/ai/review/", views.review_report_readiness, name="review_report_readiness"),
    path("reports/my/", views.my_reports, name="my_reports"),
    path("reports/<int:pk>/edit/", views.edit_my_report, name="edit_my_report"),
    path("reports/<int:pk>/delete/", views.delete_my_report, name="delete_my_report"),
    path("reports/trash/", views.report_trash, name="report_trash"),
    path("reports/trash/<int:pk>/restore/", views.report_restore, name="report_restore"),

    # الطباعة والتصدير
    path("reports/<int:pk>/print/", views.report_print, name="report_print"),

    # مشاركة التقرير (اختياري للمعلم)
    path("reports/<int:pk>/share/", views.report_share_manage, name="report_share_manage"),
    path("share-links/", views.share_links_dashboard, name="share_links_dashboard"),

    # =========================
    # تقارير الإدارة (Staff/Manager)
    # =========================
    path("reports/admin/", views.admin_reports, name="admin_reports"),
    path("reports/admin/<int:pk>/delete/", views.admin_delete_report, name="admin_delete_report"),
    path("archive/", views.school_archive, name="school_archive"),
    path("archive/create/", views.school_archive_create, name="school_archive_create"),
    path("archive/download/<int:pk>/", views.school_archive_download, name="school_archive_download"),
    path("archive/delete/<int:pk>/", views.school_archive_delete, name="school_archive_delete"),
    path("archive/export/", views.school_archive_export, name="school_archive_export"),

    # =========================
    # =========================
    # ملف إنجاز المعلّم
    # =========================
    path("achievement/my/", views.achievement_my_files, name="achievement_my_files"),
    path("achievement/school/", views.achievement_school_files, name="achievement_school_files"),
    path("achievement/school/teachers/", views.achievement_school_teachers, name="achievement_school_teachers"),
    path("achievement/<int:pk>/", views.achievement_file_detail, name="achievement_file_detail"),
    path("achievement/<int:pk>/delete/", views.achievement_file_delete, name="achievement_file_delete"),
    path("achievement/<int:pk>/update-year/", views.achievement_file_update_year, name="achievement_file_update_year"),
    path("achievement/<int:pk>/print/", views.achievement_file_print, name="achievement_file_print"),
    path("achievement/<int:pk>/pdf/", views.achievement_file_pdf, name="achievement_file_pdf"),
    path("achievement/<int:pk>/report-picker/", views.achievement_report_picker, name="achievement_report_picker"),

    # ملف الأداء القيادي لمدير/مديرة المدرسة
    path("leadership-portfolio/", views.leadership_portfolio_list, name="leadership_portfolio_list"),
    path("leadership-portfolio/<int:pk>/", views.leadership_portfolio_detail, name="leadership_portfolio_detail"),
    path("leadership-portfolio/<int:pk>/print/", views.leadership_portfolio_print, name="leadership_portfolio_print"),
    path("leadership-portfolio/<int:pk>/pdf/", views.leadership_portfolio_pdf, name="leadership_portfolio_pdf"),

    # مشاركة ملف الإنجاز (اختياري للمعلم)
    path("achievement/<int:pk>/share/", views.achievement_share_manage, name="achievement_share_manage"),

    # مشاركة عامة عبر token
    path("share/<str:token>/", views.share_public, name="share_public"),
    path("share/<str:token>/report-image/<int:slot>/", views.share_report_image, name="share_report_image"),
    path("share/<str:token>/achievement-pdf/", views.share_achievement_pdf, name="share_achievement_pdf"),

    # =========================
    # إدارة المعلّمين (للمدير)
    # =========================
    path("staff/teachers/", views.manage_teachers, name="manage_teachers"),
    path("staff/teachers/add/", views.add_teacher, name="add_teacher"),
    path("staff/teachers/import/", views.teacher_onboarding, name="bulk_import_teachers"),
    path("staff/teachers/import/template/", views.bulk_import_teachers_template, name="bulk_import_teachers_template"),
    path("staff/teachers/import/issues/", views.bulk_import_teachers_issues, name="bulk_import_teachers_issues"),
    path("staff/teachers/import/result/", views.bulk_import_teachers_result, name="bulk_import_teachers_result"),
    path("staff/teachers/<int:pk>/edit/", views.edit_teacher, name="edit_teacher"),
    path("staff/teachers/<int:pk>/delete/", views.delete_teacher, name="delete_teacher"),

    # =========================
    # إدارة الأقسام + التكليف
    # (اعتمدنا slug:code، ووفّرنا aliases للأسماء/المسارات القديمة)
    # =========================
    # =========================
    # أرشيف الوثائق
    # =========================
    path("circulars/drafts/", views.circular_draft_list, name="circular_draft_list"),
    path("circulars/drafts/<int:pk>/", views.circular_draft_detail, name="circular_draft_detail"),
    path(
        "circulars/drafts/<int:pk>/action/",
        views.circular_draft_action,
        name="circular_draft_action",
    ),

    path("documents/", views.document_archive, name="document_archive"),
    path("documents/<int:pk>/", views.document_detail, name="document_detail"),
    path("documents/<int:pk>/action/", views.document_action, name="document_action"),

    # =========================
    # الخطط والمبادرات
    # =========================
    path("plans/", views.plan_list, name="plan_list"),
    path("plans/new/", views.plan_create, name="plan_create"),
    path("plans/<int:pk>/", views.plan_detail, name="plan_detail"),
    path("plans/<int:pk>/edit/", views.plan_edit, name="plan_edit"),
    path("plans/<int:pk>/delete/", views.plan_delete, name="plan_delete"),
    path("plans/<int:pk>/print/", views.plan_print, name="plan_print"),
    path("plans/<int:pk>/action/", views.plan_action, name="plan_action"),
    path("plans/<int:pk>/approval/", views.plan_approval_action, name="plan_approval_action"),
    path("initiatives/", views.initiative_list, name="initiative_list"),
    path("initiatives/<int:pk>/action/", views.initiative_action, name="initiative_action"),

    # =========================
    # الاجتماعات والقرارات
    # =========================
    path("meetings/", views.meeting_list, name="meeting_list"),
    path("meetings/new/", views.meeting_create, name="meeting_create"),
    path("meetings/<int:pk>/", views.meeting_detail, name="meeting_detail"),
    path("meetings/<int:pk>/print/", views.meeting_print, name="meeting_print"),
    path("meetings/<int:pk>/pdf/", views.meeting_pdf, name="meeting_pdf"),
    path("meetings/<int:pk>/action/", views.meeting_action, name="meeting_action"),
    path(
        "meetings/<int:pk>/minutes/ai/improve/",
        views.improve_meeting_minutes,
        name="improve_meeting_minutes",
    ),
    path(
        "meetings/<int:pk>/minutes/ai/voice/",
        views.transcribe_meeting_minutes_voice,
        name="transcribe_meeting_minutes_voice",
    ),
    path(
        "meetings/<int:pk>/minutes/approval/",
        views.minutes_approval_action,
        name="minutes_approval_action",
    ),

    # =========================
    # التكليفات
    # =========================
    path("assignments/mine/", views.my_assignments, name="my_assignments"),
    path("assignments/board/", views.assignment_board, name="assignment_board"),
    path("assignments/new/", views.assignment_create, name="assignment_create"),
    path("assignments/<int:pk>/", views.assignment_view, name="assignment_view"),
    path("assignments/<int:pk>/print/", views.assignment_print, name="assignment_print"),
    path("assignments/<int:pk>/cancel/", views.assignment_cancel, name="assignment_cancel"),
    path("assignments/target/<int:pk>/", views.assignment_detail, name="assignment_detail"),
    path(
        "assignments/target/<int:pk>/action/",
        views.assignment_target_action,
        name="assignment_target_action",
    ),
    path(
        "assignments/target/<int:pk>/approval/",
        views.assignment_approval_action,
        name="assignment_approval_action",
    ),

    # =========================
    # المراجعة والاعتماد (المدير والوكيل وصاحب العمل)
    # =========================
    path("approvals/", views.approval_inbox, name="approval_inbox"),
    path("approvals/<int:pk>/", views.approval_detail, name="approval_detail"),
    path("approvals/<int:pk>/action/", views.approval_action, name="approval_action"),

    # =========================
    # الأدوار والصلاحيات (مدير المدرسة)
    # =========================
    path("staff/roles/", views.staff_roles, name="staff_roles"),
    path("staff/roles/<int:pk>/scope/", views.staff_role_scope, name="staff_role_scope"),
    path("staff/delegations/<int:pk>/revoke/", views.delegation_revoke, name="delegation_revoke"),

    path("staff/departments/", views.departments_list, name="departments_list"),

    # إضافة قسم
    path("staff/departments/add/", views.department_create, name="department_create"),

    # تعديل بالأكواد الدلالية (slug/code)
    path("staff/departments/<slug:code>/edit/", views.department_edit, name="department_edit"),

    # الأعضاء بالأكواد الدلالية
    path("staff/departments/<slug:code>/members/", views.department_members, name="department_members"),

    # حذف بالأكواد الدلالية
    path("staff/departments/<slug:code>/delete/", views.department_delete, name="department_delete"),

    # =========================
    # لوحة المدير
    # =========================
    # لوحة المدير التنفيذي: خارج سياق المدرسة الواحدة عمداً.
    path("group/", views.executive_dashboard, name="executive_dashboard"),
    # تكليفات المجموعة — خارج سياق المدرسة الواحدة عمداً، كلوحة المجموعة.
    path("group/assignments/", views.group_assignment_board, name="group_assignment_board"),
    path("group/assignments/new/", views.group_assignment_create, name="group_assignment_create"),
    path(
        "group/assignments/<int:pk>/",
        views.group_assignment_detail,
        name="group_assignment_detail",
    ),
    path(
        "group/assignments/<int:pk>/action/",
        views.group_assignment_action,
        name="group_assignment_action",
    ),
    path(
        "group/assignments/<int:pk>/cancel/",
        views.group_assignment_cancel,
        name="group_assignment_cancel",
    ),
    # مجلس مجموعة المدارس
    path("group/council/", views.council_list, name="council_list"),
    path("group/council/new/", views.council_create, name="council_create"),
    path("group/council/<int:pk>/", views.council_detail, name="council_detail"),
    path("group/council/<int:pk>/action/", views.council_action, name="council_action"),
    path(
        "group/council/<int:pk>/minutes/",
        views.council_minutes_action,
        name="council_minutes_action",
    ),

    path("group/practices/", views.group_practices, name="group_practices"),
    path("group/schools/<int:pk>/", views.group_school_detail, name="group_school_detail"),
    path("group/subscriptions/", views.group_subscriptions, name="group_subscriptions"),
    path("group/approvals/", views.group_approval_inbox, name="group_approval_inbox"),
    path("group/audit/", views.group_audit_log, name="group_audit_log"),
    path("group/archive/", views.group_archive, name="group_archive"),

    # التقرير التنفيذي المجمَّع
    path("group/report/", views.group_report, name="group_report"),
    path("group/report/xlsx/", views.group_report_xlsx, name="group_report_xlsx"),
    path("group/report/pdf/", views.group_report_pdf, name="group_report_pdf"),

    path("group/notify/", views.group_notification_create, name="group_notification_create"),
    path("group/notify/sent/", views.group_notifications_sent, name="group_notifications_sent"),
    path("group/notify/<int:pk>/", views.group_notification_report, name="group_notification_report"),
    path("staff/select-school/", views.select_school, name="select_school"),
    path("staff/switch-school/", views.switch_school, name="switch_school"),
    path("staff/my-school/", views.school_settings, name="school_settings"),
    path("staff/school-health/", views.school_health, name="school_health"),
    path("staff/schools/", views.schools_admin_list, name="schools_admin_list"),
    path("staff/schools/add/", views.school_create, name="school_create"),
    path("staff/schools/<int:pk>/profile/", views.school_profile, name="school_profile"),
    path("staff/schools/<int:pk>/edit/", views.school_update, name="school_update"),
    path("staff/schools/<int:pk>/delete/", views.school_delete, name="school_delete"),
    path("staff/schools/managers/", views.school_managers_list, name="school_managers_list"),
    path("staff/schools/managers/<int:pk>/edit/", views.school_manager_update, name="school_manager_update"),
    path("staff/schools/managers/<int:pk>/delete/", views.school_manager_delete, name="school_manager_delete"),
    path("staff/schools/managers/add/", views.school_manager_create, name="school_manager_create"),
    path("staff/schools/<int:pk>/managers/", views.school_managers_manage, name="school_managers_manage"),
    path("staff/schools/request-addition/", views.school_addition_requests, name="school_addition_requests"),
    path("platform/school-addition-requests/", views.platform_school_addition_requests, name="platform_school_addition_requests"),
    path(
        "platform/school-addition-requests/<int:pk>/review/",
        views.platform_school_addition_request_review,
        name="platform_school_addition_request_review",
    ),
    path("platform/audit-logs/", views.platform_audit_logs, name="platform_audit_logs"),
    path("platform/operations/", views.platform_operations, name="platform_operations"),
    path("platform-dashboard/", views.platform_admin_dashboard, name="platform_admin_dashboard"),

    # =========================
    # لوحة إدارة المنصة (مالك النظام)
    # =========================
    path("platform/schools/", views.platform_schools_directory, name="platform_schools_directory"),
    path("platform/schools/<int:pk>/enter/", views.platform_enter_school, name="platform_enter_school"),
    path("platform/school/", views.platform_school_dashboard, name="platform_school_dashboard"),
    path("platform/school/reports/", views.platform_school_reports, name="platform_school_reports"),
    path("platform/school/tickets/", views.platform_school_tickets, name="platform_school_tickets"),
    path("platform/school/notify/", views.platform_school_notify, name="platform_school_notify"),

    # المدراء التنفيذيون ومجموعات المدارس المتكاملة (مالك النظام وحده)
    path("platform/executives/", views.platform_executive_directors, name="platform_executive_directors"),
    path("platform/executives/add/", views.platform_executive_director_form, name="platform_executive_director_add"),
    path(
        "platform/executives/<int:pk>/edit/",
        views.platform_executive_director_form,
        name="platform_executive_director_edit",
    ),
    path(
        "platform/executives/<int:pk>/toggle/",
        views.platform_executive_director_toggle,
        name="platform_executive_director_toggle",
    ),
    path(
        "platform/executives/<int:pk>/delete/",
        views.platform_executive_director_delete,
        name="platform_executive_director_delete",
    ),

    path("admin-dashboard/", views.admin_dashboard, name="admin_dashboard"),
    path("staff/audit-logs/", views.school_audit_logs, name="school_audit_logs"),

    # =========================
    # أنواع التقارير
    # =========================
    path("staff/report-types/", views.reporttypes_list, name="reporttypes_list"),
    path("staff/report-types/add/", views.reporttype_create, name="reporttype_create"),
    path("staff/report-types/<int:pk>/edit/", views.reporttype_update, name="reporttype_update"),
    path("staff/report-types/<int:pk>/delete/", views.reporttype_delete, name="reporttype_delete"),

    # =========================
    # تصدير بيانات المدرسة (مدير المدرسة)
    # =========================
    path("staff/export/", views.school_data_export, name="school_data_export"),
    path("staff/export/download/", views.school_data_export_download, name="school_data_export_download"),
    path("staff/export/download/zip/", views.school_data_export_zip, name="school_data_export_zip"),

    # =========================
    # التذاكر (Requests/Tickets)
    # =========================
    path("requests/new/", views.request_create, name="request_create"),
    path("requests/mine/", views.my_requests, name="my_requests"),
    path("requests/school/", views.manager_school_tickets, name="manager_school_tickets"),
    path("requests/inbox/", views.tickets_inbox, name="tickets_inbox"),
    path("requests/assigned/", views.assigned_to_me, name="assigned_to_me"),
    path("requests/<int:pk>/", views.ticket_detail, name="ticket_detail"),
    path("requests/<int:pk>/print/", views.ticket_print, name="ticket_print"),
    path("requests/notes/<int:pk>/edit/", views.ticket_note_edit, name="ticket_note_edit"),
    path("requests/admin/<int:pk>/", views.admin_request_update, name="admin_request_update"),

    # الدعم الفني للمنصة
    path("support/new/", views.support_ticket_create, name="support_ticket_create"),
    path("support/mine/", views.my_support_tickets, name="my_support_tickets"),

    # Officer
    path("officer/reports/", views.officer_reports, name="officer_reports"),
    path("officer/reports/<int:pk>/delete/", views.officer_delete_report, name="officer_delete_report"),

    # Department member (read-only)
    path("department/reports/", views.department_reports, name="department_reports"),

    # =========================
    # API
    # =========================
    path("api/department-members/", views.api_department_members, name="api_department_members"),
    path("api/notification-teachers/", views.api_notification_teachers, name="api_notification_teachers"),
    path("api/school-departments/", views.api_school_departments, name="api_school_departments"),
    # البحث الموحّد — نتائجه محدودة بالمدرسة النشطة وصلاحيات صاحب الطلب.
    path("api/search/", views.global_search, name="global_search"),
    path("api/dashboard/school/", views.admin_dashboard_data, name="api_admin_dashboard_data"),
    path("api/dashboard/platform/", views.platform_admin_dashboard_data, name="api_platform_dashboard_data"),
    path("api/dashboard/platform/search/", views.platform_admin_dashboard_search, name="api_platform_dashboard_search"),

    # =========================
    # الإشعارات
    # =========================
    path("notifications/unread-count/", views.unread_notifications_count, name="unread_notifications_count"),
    # Cloudflare managed rules treat a public URL ending in /config/ as a
    # sensitive-file probe and block it before the request reaches Django.
    # Keep the route name stable for templates, but expose a WAF-safe path.
    path("push/status/", views.web_push_config, name="web_push_config"),
    path("push/subscribe/", views.web_push_subscribe, name="web_push_subscribe"),
    path("push/unsubscribe/", views.web_push_unsubscribe, name="web_push_unsubscribe"),
    path("notifications/<int:pk>/", views.notification_detail, name="notification_detail"),
    path(
        "notifications/<int:pk>/recipients/add/",
        views.circular_recipients_add,
        name="circular_recipients_add",
    ),
    path("notifications/<int:pk>/delete/", views.notification_delete, name="notification_delete"),
    path("notifications/send/", views.send_notification, name="send_notification"),  # تحويل للإنشاء (توافق قديم)
    # إشعارات (تنبيه/رسالة)
    path(
        "notifications/create/",
        views.notifications_create,
        {"mode": "notification"},
        name="notifications_create",
    ),
    path(
        "notifications/sent/",
        views.notifications_sent,
        {"mode": "notification"},
        name="notifications_sent",
    ),

    # تعاميم (قد تتطلب توقيع وتتبع)
    path(
        "circulars/create/",
        views.notifications_create,
        {"mode": "circular"},
        name="circulars_create",
    ),
    path(
        "circulars/sent/",
        views.notifications_sent,
        {"mode": "circular"},
        name="circulars_sent",
    ),
    path("notifications/mine/", views.my_notifications, name="my_notifications"),
    path("circulars/mine/", views.my_circulars, name="my_circulars"),
    path("notifications/mine/<int:pk>/", views.my_notification_detail, name="my_notification_detail"),
    path("circulars/mine/<int:pk>/", views.my_notification_detail, name="my_circular_detail"),
    path("notifications/mine/<int:pk>/sign/", views.notification_sign, name="notification_sign"),
    path("circulars/mine/<int:pk>/sign/", views.notification_sign, name="circular_sign"),
    path("notifications/<int:pk>/read/", views.notification_mark_read, name="notification_mark_read"),
    path("notifications/mark-all-read/", views.notifications_mark_all_read, name="notifications_mark_all_read"),
    path("circulars/mark-all-read/", views.circulars_mark_all_read, name="circulars_mark_all_read"),
    # جديد: تعليم كمقروء بالاعتماد على رقم الإشعار (للهيرو/الواجهة)
    path(
        "notifications/<int:pk>/read-by-notification/",
        views.notification_mark_read_by_notification,
        name="notification_mark_read_by_notification",
    ),

    # تقارير التواقيع للتعاميم (للمدير/المسؤول)
    path(
        "notifications/<int:pk>/signatures/print/",
        views.notification_signatures_print,
        name="notification_signatures_print",
    ),
    path(
        "notifications/<int:pk>/signatures.csv",
        views.notification_signatures_csv,
        name="notification_signatures_csv",
    ),

    # =========================
    # الاشتراكات والمالية
    # =========================
    path("subscription/expired/", views.subscription_expired, name="subscription_expired"),
    path("subscription/my/", views.my_subscription, name="my_subscription"),
    path("subscription/history/", views.subscription_history, name="subscription_history"),
    path(
        "subscription/invoices/<int:payment_id>/",
        views.subscription_invoice,
        name="subscription_invoice",
    ),
    path(
        "subscription/invoices/<int:payment_id>/pdf/",
        views.subscription_invoice_pdf,
        name="subscription_invoice_pdf",
    ),
    path("subscription/payment/create/", views.payment_create, name="payment_create"),
    path("subscription/payment/moyasar/", views.moyasar_checkout_create, name="moyasar_checkout_create"),
    path(
        "subscription/payment/moyasar/return/<str:batch_ref>/",
        views.moyasar_return,
        name="moyasar_return",
    ),
    path(
        "payments/moyasar/callback/<str:batch_ref>/",
        views.moyasar_callback,
        name="moyasar_callback",
    ),
    path(
        "subscription/payment/moyasar/<int:payment_id>/cancel/",
        views.moyasar_checkout_cancel,
        name="moyasar_checkout_cancel",
    ),
    path("subscription/payment/tamara/", views.tamara_checkout_create, name="tamara_checkout_create"),
    path(
        "subscription/payment/tamara/<int:payment_id>/cancel/",
        views.tamara_checkout_cancel,
        name="tamara_checkout_cancel",
    ),
    path(
        "subscription/payment/tamara/return/<str:result>/",
        views.tamara_return,
        name="tamara_return",
    ),
    path("payments/tamara/webhook/", views.tamara_webhook, name="tamara_webhook"),
    path(
        "subscription/discount-code/check/",
        views.discount_code_check,
        name="discount_code_check",
    ),

    # =========================
    # إدارة المنصة (Custom Views)
    # =========================
    path("platform/subscriptions/", views.platform_subscriptions_list, name="platform_subscriptions_list"),
    path("platform/settings/", views.platform_settings, name="platform_settings"),
    path("platform/mansour-content/", views.platform_mansour_content, name="platform_mansour_content"),
    path("platform/academic-years/", views.platform_academic_years, name="platform_academic_years"),
    path("platform/subscriptions/add/", views.platform_subscription_form, name="platform_subscription_add"),
    path("platform/subscriptions/<int:pk>/", views.platform_subscription_detail, name="platform_subscription_detail"),
    path("platform/subscriptions/<int:pk>/renew/", views.platform_subscription_renew, name="platform_subscription_renew"),
    path(
        "platform/subscriptions/<int:pk>/record-payment/",
        views.platform_subscription_record_payment,
        name="platform_subscription_record_payment",
    ),
    path("platform/subscriptions/<int:pk>/delete/", views.platform_subscription_delete, name="platform_subscription_delete"),
    path("platform/pricing/", views.platform_pricing_matrix, name="platform_pricing_matrix"),
    path("platform/plans/", views.platform_plans_list, name="platform_plans_list"),
    path("platform/plans/add/", views.platform_plan_form, name="platform_plan_add"),
    path("platform/plans/<int:pk>/edit/", views.platform_plan_form, name="platform_plan_edit"),
    path("platform/plans/<int:pk>/delete/", views.platform_plan_delete, name="platform_plan_delete"),
    path("platform/payments/", views.platform_payments_list, name="platform_payments_list"),
    path("platform/payments/<int:pk>/", views.platform_payment_detail, name="platform_payment_detail"),
    path("platform/tickets/", views.platform_tickets_list, name="platform_tickets_list"),
    path(
        "platform/complaints/",
        views.platform_complaints_list,
        name="platform_complaints_list",
    ),
    path(
        "platform/complaints/<int:pk>/",
        views.platform_complaint_detail,
        name="platform_complaint_detail",
    ),
    path("platform/email/", views.platform_email_inbox, name="platform_email_inbox"),
    path("platform/email/compose/", views.platform_email_compose, name="platform_email_compose"),
    path("platform/email/settings/", views.platform_email_settings, name="platform_email_settings"),
    path("platform/email/sync/", views.platform_email_sync, name="platform_email_sync"),
    path("platform/email/<int:pk>/", views.platform_email_detail, name="platform_email_detail"),
    path("platform/email/<int:pk>/action/", views.platform_email_action, name="platform_email_action"),
    path(
        "platform/email/<int:pk>/attachments/<int:attachment_pk>/",
        views.platform_email_attachment_download,
        name="platform_email_attachment_download",
    ),
    path("webhooks/resend/", views.resend_webhook, name="resend_webhook"),
    path("platform/archive-addons/", views.platform_archive_addons_list, name="platform_archive_addons_list"),
    path("platform/archive-addons/add/", views.platform_archive_addon_form, name="platform_archive_addon_add"),
    path("platform/archive-addons/<int:pk>/edit/", views.platform_archive_addon_form, name="platform_archive_addon_edit"),
    path("platform/archive-addons/<int:pk>/toggle/", views.platform_archive_addon_toggle, name="platform_archive_addon_toggle"),
    path("platform/discount-codes/", views.platform_discount_codes_list, name="platform_discount_codes_list"),
    path("platform/discount-codes/add/", views.platform_discount_code_form, name="platform_discount_code_add"),
    path("platform/discount-codes/<int:pk>/", views.platform_discount_code_detail, name="platform_discount_code_detail"),
    path("platform/discount-codes/<int:pk>/edit/", views.platform_discount_code_form, name="platform_discount_code_edit"),
    path("platform/discount-codes/<int:pk>/toggle/", views.platform_discount_code_toggle, name="platform_discount_code_toggle"),
    path("platform/discount-codes/<int:pk>/delete/", views.platform_discount_code_delete, name="platform_discount_code_delete"),

    # =========================
    # صفحات المحتوى (Footer)
    # =========================
    path("faq/", views.faq, name="faq"),
    path("privacy/", views.privacy_policy, name="privacy_policy"),
    path("terms/", views.terms_conditions, name="terms_conditions"),
    path("refund-policy/", views.refund_policy, name="refund_policy"),
    path(
        "service-delivery/",
        views.service_delivery_policy,
        name="service_delivery_policy",
    ),
    path("complaints/", views.complaints_policy, name="complaints_policy"),
]
