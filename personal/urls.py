from django.urls import path

from . import views

app_name = "personal"

urlpatterns = [
    path("register/", views.register, name="register"),
    path("setup/", views.setup, name="setup"),
    path("checkout/<int:plan_id>/", views.checkout_start, name="checkout_start"),
    path("payments/", views.billing, name="billing"),
    path("payments/<uuid:payment_id>/invoice.pdf", views.payment_invoice, name="payment_invoice"),
    path("payments/moyasar/callback/<uuid:payment_id>/", views.moyasar_callback, name="moyasar_callback"),
    path("payments/moyasar/return/<uuid:payment_id>/", views.moyasar_return, name="moyasar_return"),
    path("", views.dashboard, name="dashboard"),
    path("reports/", views.report_list, name="reports"),
    path("reports/new/", views.report_create, name="report_create"),
    path("reports/<int:pk>/", views.report_detail, name="report_detail"),
    path("reports/<int:pk>/edit/", views.report_edit, name="report_edit"),
    path("reports/<int:pk>/delete/", views.report_delete, name="report_delete"),
    path("reports/<int:pk>/print/", views.report_print, name="report_print"),
    path("evidence/", views.evidence_list, name="evidence"),
    path("evidence/new/", views.evidence_create, name="evidence_create"),
    path("evidence/<int:pk>/download/", views.evidence_download, name="evidence_download"),
    path("evidence/<int:pk>/delete/", views.evidence_delete, name="evidence_delete"),
    path("portfolio/", views.portfolio, name="portfolio"),
    path("portfolio/print/", views.portfolio_print, name="portfolio_print"),
]
