# UI/UX Audit and Template Inventory

Generated from the repository on 2026-09-11. Every HTML template under `reports/templates` is listed exactly once below.

## Audit summary

- Templates: **234**
- User-facing screen templates: **165**
- Reusable partials: **42**
- Print/PDF templates: **14**
- Email templates: **10**
- Base/layout templates: **3**
- CSS assets: **35**; JavaScript assets: **28**
- Templates containing forms: **131**; tables: **55**; embedded style blocks: **146**
- Legacy `!important` occurrences: **637** (none were introduced in the new foundation)

## Key findings

- The application previously mixed page-local visual systems with the shared shell, producing inconsistent color, radius, spacing, button, card, and heading treatments.
- High-traffic reports, notifications, profile, teacher-management, and platform-request screens repeated large embedded CSS overlays instead of consuming reusable tokens.
- Several child templates created a second `main` landmark inside the shared layout; this harmed document hierarchy and screen-reader navigation.
- Dense report rows exposed several adjacent icon buttons; the primary action was hard to identify and mobile touch targets were inconsistent.
- Responsive debt centered on physical left/right declarations, fixed widths, oversized page heroes, and tables/actions that did not reduce cleanly on narrow screens.
- RTL was declared globally but not expressed through a central logical-property layer, so individual templates compensated locally.
- Standalone authentication/recovery/maintenance pages lacked a consistent focus, skip-link, form, surface, and installed-app treatment.
- Empty/error/loading feedback existed unevenly. The shared foundation now provides semantic empty, error, skeleton, busy, and message states while preserving page-specific content.
- The principal accessibility gaps were duplicate landmarks, uneven focus visibility, small touch targets, missing validation ARIA state, and non-semantic action clusters.
- The installed PWA needs explicit safe-area and standalone-mode checks in addition to ordinary browser viewport checks.

## Design system proposal implemented

- **Color:** restrained brand green primary, dark ink secondary, warm accent, semantic success/warning/danger/info, and separate canvas/surface/elevated/border/text roles with dark-mode equivalents.
- **Typography:** Cairo as the Arabic-first UI family, with a fixed title/section/card/body/small/caption scale and defined weights/line heights.
- **Spacing:** 4, 8, 12, 16, 20, 24, 32, 40, 48, and 64 px semantic tokens.
- **Radius:** small/medium/large/x-large/pill tokens; cards use a quiet border and restrained shadow rather than heavy elevation.
- **Motion:** 150–250 ms interaction durations and a global `prefers-reduced-motion` fallback.
- **Components:** unified app shell, page header, navigation, buttons, fields, validation, cards, statistics, tables, badges, alerts, toasts, action menus, dialogs, tabs, pagination, empty/error/loading/skeleton states, and installed-PWA safe areas.
- **RTL and accessibility:** logical CSS properties, one main landmark, visible keyboard focus, 44 px touch targets, explicit labels/ARIA enrichment, and semantic status regions.

## Complete template inventory

### 1. Authentication (15)

- `reports/templates/reports/emails/password_changed.html` — Email
- `reports/templates/reports/emails/password_reset_email.html` — Email
- `reports/templates/reports/login.html` — Screen
- `reports/templates/reports/partials/passkey_enrollment_prompt.html` — Partial
- `reports/templates/reports/password_reset_base.html` — Base/Layout
- `reports/templates/reports/password_reset_complete.html` — Screen
- `reports/templates/reports/password_reset_confirm.html` — Screen
- `reports/templates/reports/password_reset_done.html` — Screen
- `reports/templates/reports/password_reset_form.html` — Screen
- `reports/templates/reports/register_school.html` — Screen
- `reports/templates/reports/registration_success.html` — Screen
- `reports/templates/reports/select_school.html` — Screen
- `reports/templates/reports/totp_challenge.html` — Screen
- `reports/templates/reports/totp_enroll.html` — Screen
- `reports/templates/reports/totp_settings.html` — Screen

### 2. Dashboard (7)

- `reports/templates/reports/admin_dashboard.html` — Screen
- `reports/templates/reports/executive_dashboard.html` — Screen
- `reports/templates/reports/home.html` — Screen
- `reports/templates/reports/lab_dashboard.html` — Screen
- `reports/templates/reports/platform_admin_dashboard.html` — Screen
- `reports/templates/reports/platform_school_dashboard.html` — Screen
- `reports/templates/reports/staff_dashboard.html` — Screen

### 3. Management (74)

- `reports/templates/base.html` — Base/Layout
- `reports/templates/reports/_approval_theme.html` — Partial
- `reports/templates/reports/_assignment_theme.html` — Partial
- `reports/templates/reports/_document_theme.html` — Partial
- `reports/templates/reports/_identity.html` — Partial
- `reports/templates/reports/_lab_field.html` — Partial
- `reports/templates/reports/_lab_theme.html` — Partial
- `reports/templates/reports/_meeting_theme.html` — Partial
- `reports/templates/reports/_pagination.html` — Partial
- `reports/templates/reports/_plan_theme.html` — Partial
- `reports/templates/reports/approval_detail.html` — Screen
- `reports/templates/reports/assigned_to_me.html` — Screen
- `reports/templates/reports/assignment_board.html` — Screen
- `reports/templates/reports/assignment_create.html` — Screen
- `reports/templates/reports/assignment_detail.html` — Screen
- `reports/templates/reports/assignment_print.html` — Print/PDF
- `reports/templates/reports/assignment_view.html` — Screen
- `reports/templates/reports/billing_invoice.html` — Screen
- `reports/templates/reports/billing_invoice_pdf.html` — Print/PDF
- `reports/templates/reports/council_create.html` — Screen
- `reports/templates/reports/council_detail.html` — Screen
- `reports/templates/reports/department_members.html` — Screen
- `reports/templates/reports/document_detail.html` — Screen
- `reports/templates/reports/group_assignment_board.html` — Screen
- `reports/templates/reports/group_assignment_create.html` — Screen
- `reports/templates/reports/group_assignment_detail.html` — Screen
- `reports/templates/reports/group_practices.html` — Screen
- `reports/templates/reports/group_school_detail.html` — Screen
- `reports/templates/reports/lab_asset_detail.html` — Screen
- `reports/templates/reports/lab_assets.html` — Screen
- `reports/templates/reports/lab_assets_print.html` — Print/PDF
- `reports/templates/reports/lab_experiment_detail.html` — Screen
- `reports/templates/reports/lab_experiment_print.html` — Print/PDF
- `reports/templates/reports/lab_experiments.html` — Screen
- `reports/templates/reports/meeting_create.html` — Screen
- `reports/templates/reports/meeting_detail.html` — Screen
- `reports/templates/reports/meeting_print.html` — Print/PDF
- `reports/templates/reports/my_assignments.html` — Screen
- `reports/templates/reports/my_subscription.html` — Screen
- `reports/templates/reports/partials/_meeting_copy_link.html` — Partial
- `reports/templates/reports/partials/business_disclosure.html` — Partial
- `reports/templates/reports/partials/consumption_panel.html` — Partial
- `reports/templates/reports/partials/consumption_rows.html` — Partial
- `reports/templates/reports/partials/global_search.html` — Partial
- `reports/templates/reports/partials/kpi_trend.html` — Partial
- `reports/templates/reports/partials/lab_print_official_styles.html` — Partial
- `reports/templates/reports/partials/mansour_assistant.html` — Partial
- `reports/templates/reports/partials/storage_alert.html` — Partial
- `reports/templates/reports/partials/theme_bootstrap.html` — Partial
- `reports/templates/reports/partials/ui_state.html` — Partial
- `reports/templates/reports/plan_create.html` — Screen
- `reports/templates/reports/plan_detail.html` — Screen
- `reports/templates/reports/plan_edit.html` — Screen
- `reports/templates/reports/plan_print.html` — Print/PDF
- `reports/templates/reports/platform_academic_years.html` — Screen
- `reports/templates/reports/platform_discount_code_detail.html` — Screen
- `reports/templates/reports/platform_discount_codes.html` — Screen
- `reports/templates/reports/platform_executive_directors.html` — Screen
- `reports/templates/reports/platform_mansour_content.html` — Screen
- `reports/templates/reports/platform_payment_detail.html` — Screen
- `reports/templates/reports/platform_plans.html` — Screen
- `reports/templates/reports/platform_pricing_matrix.html` — Screen
- `reports/templates/reports/platform_school_notify.html` — Screen
- `reports/templates/reports/platform_subscription_add.html` — Screen
- `reports/templates/reports/platform_subscription_detail.html` — Screen
- `reports/templates/reports/request_create.html` — Screen
- `reports/templates/reports/role_guidance.html` — Screen
- `reports/templates/reports/school_health.html` — Screen
- `reports/templates/reports/school_manager_create.html` — Screen
- `reports/templates/reports/school_managers_manage.html` — Screen
- `reports/templates/reports/staff_role_scope.html` — Screen
- `reports/templates/reports/staff_roles.html` — Screen
- `reports/templates/reports/subscription_expired.html` — Screen
- `reports/templates/reports/subscription_history.html` — Screen

### 4. Forms (12)

- `reports/templates/reports/_lab_asset_form.html` — Partial
- `reports/templates/reports/_lab_experiment_form.html` — Partial
- `reports/templates/reports/add_teacher.html` — Screen
- `reports/templates/reports/bulk_import_teachers.html` — Screen
- `reports/templates/reports/department_form.html` — Screen
- `reports/templates/reports/edit_teacher.html` — Screen
- `reports/templates/reports/platform_archive_addon_form.html` — Screen
- `reports/templates/reports/platform_discount_code_form.html` — Screen
- `reports/templates/reports/platform_executive_director_form.html` — Screen
- `reports/templates/reports/platform_plan_form.html` — Screen
- `reports/templates/reports/school_delete_confirm.html` — Screen
- `reports/templates/reports/school_form.html` — Screen

### 5. Tables (25)

- `reports/templates/reports/approval_inbox.html` — Screen
- `reports/templates/reports/archive_record_pdf.html` — Print/PDF
- `reports/templates/reports/audit_logs.html` — Screen
- `reports/templates/reports/council_list.html` — Screen
- `reports/templates/reports/departments_list.html` — Screen
- `reports/templates/reports/document_archive.html` — Screen
- `reports/templates/reports/group_approval_inbox.html` — Screen
- `reports/templates/reports/group_archive.html` — Screen
- `reports/templates/reports/group_audit_log.html` — Screen
- `reports/templates/reports/group_subscriptions.html` — Screen
- `reports/templates/reports/initiative_list.html` — Screen
- `reports/templates/reports/manage_teachers.html` — Screen
- `reports/templates/reports/meeting_list.html` — Screen
- `reports/templates/reports/my_activity_log.html` — Screen
- `reports/templates/reports/my_requests.html` — Screen
- `reports/templates/reports/plan_list.html` — Screen
- `reports/templates/reports/platform_archive_addons.html` — Screen
- `reports/templates/reports/platform_payments.html` — Screen
- `reports/templates/reports/platform_school_addition_requests.html` — Screen
- `reports/templates/reports/platform_schools_directory.html` — Screen
- `reports/templates/reports/platform_subscriptions.html` — Screen
- `reports/templates/reports/school_addition_requests.html` — Screen
- `reports/templates/reports/school_archive.html` — Screen
- `reports/templates/reports/school_managers_list.html` — Screen
- `reports/templates/reports/schools_admin_list.html` — Screen

### 6. Reports (33)

- `reports/templates/reports/achievement_file.html` — Screen
- `reports/templates/reports/achievement_my_files.html` — Screen
- `reports/templates/reports/achievement_school_files.html` — Screen
- `reports/templates/reports/add_report.html` — Screen
- `reports/templates/reports/admin_reports.html` — Screen
- `reports/templates/reports/edit_report.html` — Screen
- `reports/templates/reports/generated_export_status.html` — Screen
- `reports/templates/reports/group_report.html` — Screen
- `reports/templates/reports/leadership_portfolio_detail.html` — Screen
- `reports/templates/reports/leadership_portfolio_list.html` — Screen
- `reports/templates/reports/my_reports.html` — Screen
- `reports/templates/reports/officer_reports.html` — Screen
- `reports/templates/reports/partials/achievement_report_picker_list.html` — Partial
- `reports/templates/reports/partials/report_actions_menu.html` — Partial
- `reports/templates/reports/partials/report_ai_improver.html` — Partial
- `reports/templates/reports/partials/report_details_limit.html` — Partial
- `reports/templates/reports/partials/report_evidence_card.html` — Partial
- `reports/templates/reports/partials/report_evidence_formset.html` — Partial
- `reports/templates/reports/partials/report_evidence_print.html` — Partial
- `reports/templates/reports/partials/report_print_official_styles.html` — Partial
- `reports/templates/reports/partials/report_review_panel.html` — Partial
- `reports/templates/reports/partials/report_state_chip.html` — Partial
- `reports/templates/reports/partials/report_submit_btn.html` — Partial
- `reports/templates/reports/partials/report_summary_rail.html` — Partial
- `reports/templates/reports/partials/report_voice_input.html` — Partial
- `reports/templates/reports/pdf/achievement_file.html` — Print/PDF
- `reports/templates/reports/pdf/group_report_pdf.html` — Print/PDF
- `reports/templates/reports/pdf/leadership_portfolio.html` — Print/PDF
- `reports/templates/reports/report_print.html` — Print/PDF
- `reports/templates/reports/report_trash.html` — Screen
- `reports/templates/reports/reporttype_form.html` — Screen
- `reports/templates/reports/reporttypes_list.html` — Screen
- `reports/templates/reports/school_data_export.html` — Screen

### 7. Profiles (5)

- `reports/templates/reports/my_data.html` — Screen
- `reports/templates/reports/my_data_readable.html` — Screen
- `reports/templates/reports/my_profile.html` — Screen
- `reports/templates/reports/my_work_archive.html` — Screen
- `reports/templates/reports/school_profile.html` — Screen

### 8. Settings (5)

- `reports/templates/reports/api_keys.html` — Screen
- `reports/templates/reports/platform_email_settings.html` — Email
- `reports/templates/reports/platform_operations.html` — Screen
- `reports/templates/reports/platform_settings.html` — Screen
- `reports/templates/reports/school_settings.html` — Screen

### 9. Notifications (34)

- `reports/templates/reports/circular_detail.html` — Screen
- `reports/templates/reports/circular_draft_detail.html` — Screen
- `reports/templates/reports/circular_draft_list.html` — Screen
- `reports/templates/reports/circulars_create.html` — Screen
- `reports/templates/reports/circulars_sent.html` — Screen
- `reports/templates/reports/emails/branded_base.html` — Email
- `reports/templates/reports/emails/message.html` — Email
- `reports/templates/reports/emails/subscription_activated.html` — Email
- `reports/templates/reports/emails/subscription_expiry.html` — Email
- `reports/templates/reports/group_notification_create.html` — Screen
- `reports/templates/reports/group_notification_report.html` — Screen
- `reports/templates/reports/group_notifications_sent.html` — Screen
- `reports/templates/reports/my_circular_detail.html` — Screen
- `reports/templates/reports/my_circulars.html` — Screen
- `reports/templates/reports/my_notification_detail.html` — Screen
- `reports/templates/reports/my_notifications.html` — Screen
- `reports/templates/reports/my_support_tickets.html` — Screen
- `reports/templates/reports/notification_detail.html` — Screen
- `reports/templates/reports/notification_signatures_print.html` — Print/PDF
- `reports/templates/reports/notifications_create.html` — Screen
- `reports/templates/reports/notifications_sent.html` — Screen
- `reports/templates/reports/platform_complaint_detail.html` — Screen
- `reports/templates/reports/platform_complaints.html` — Screen
- `reports/templates/reports/platform_email_compose.html` — Email
- `reports/templates/reports/platform_email_detail.html` — Email
- `reports/templates/reports/platform_email_inbox.html` — Email
- `reports/templates/reports/platform_tickets.html` — Screen
- `reports/templates/reports/send_circular.html` — Screen
- `reports/templates/reports/send_notification.html` — Screen
- `reports/templates/reports/support_ticket_create.html` — Screen
- `reports/templates/reports/ticket_detail.html` — Screen
- `reports/templates/reports/ticket_note_edit.html` — Screen
- `reports/templates/reports/ticket_print.html` — Print/PDF
- `reports/templates/reports/tickets_inbox.html` — Screen

### 10. Public pages (15)

- `reports/templates/reports/achievement_share_manage.html` — Screen
- `reports/templates/reports/complaints_policy.html` — Screen
- `reports/templates/reports/faq.html` — Screen
- `reports/templates/reports/landing.html` — Screen
- `reports/templates/reports/legal_base.html` — Base/Layout
- `reports/templates/reports/partials/public_seo.html` — Partial
- `reports/templates/reports/partials/user_guide_content.html` — Partial
- `reports/templates/reports/privacy_policy.html` — Screen
- `reports/templates/reports/refund_policy.html` — Screen
- `reports/templates/reports/report_share_manage.html` — Screen
- `reports/templates/reports/service_delivery_policy.html` — Screen
- `reports/templates/reports/share_links_dashboard.html` — Screen
- `reports/templates/reports/terms_conditions.html` — Screen
- `reports/templates/reports/user_guide.html` — Screen
- `reports/templates/reports/user_guide_pdf.html` — Print/PDF

### 11. Error pages (5)

- `reports/templates/403.html` — Screen
- `reports/templates/404.html` — Screen
- `reports/templates/500.html` — Screen
- `reports/templates/reports/maintenance_mode.html` — Screen
- `reports/templates/reports/share_invalid.html` — Screen

### 12. Mobile-specific interfaces (4)

- `reports/templates/reports/partials/pwa_install.html` — Partial
- `reports/templates/reports/partials/pwa_install_head.html` — Partial
- `reports/templates/reports/partials/report_mobile_actions.html` — Partial
- `reports/templates/reports/partials/web_push_prompt.html` — Partial
