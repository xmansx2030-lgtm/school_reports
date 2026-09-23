from django.urls import path

from . import views

app_name = "operations"

urlpatterns = [
    path("auth/login/", views.login, name="login"),
    path("auth/logout/", views.logout, name="logout"),
    path("auth/password/", views.change_password, name="change-password"),
    path("accounts/", views.accounts, name="accounts"),
    path("accounts/<int:user_id>/", views.account_detail, name="account-detail"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("payment-links/", views.payment_links, name="payment-links"),
    path(
        "payment-links/<uuid:public_id>/sync/",
        views.sync_payment_link_status,
        name="payment-link-sync",
    ),
    path(
        "payment-links/<uuid:public_id>/cancel/",
        views.cancel_payment_link,
        name="payment-link-cancel",
    ),
    path(
        "payment-links/callback/<uuid:public_id>/<str:token>/",
        views.payment_link_callback,
        name="payment-link-callback",
    ),
    path("deployment/status/", views.deployment_status, name="deployment-status"),
    path("deployment/deploy/", views.trigger_deployment, name="trigger-deployment"),
    path("projects/<int:project_id>/", views.project_detail, name="project-detail"),
    path("projects/<int:project_id>/actions/", views.create_action, name="create-action"),
    path("devices/", views.device_registration, name="device-registration"),
    path("incidents/<int:incident_id>/acknowledge/", views.acknowledge_incident, name="acknowledge-incident"),
]
