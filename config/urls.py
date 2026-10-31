from django.contrib.auth import views as auth
from django.urls import path
from core import views
from core import security_views as security
from core import bulk_views as bulk

urlpatterns = [
    path("health/", views.health, name="health"),
    path("login/", views.login_view, name="login"),
    path("mfa/verify/", security.challenge, name="mfa_challenge"),
    path("mfa/setup/", security.setup, name="mfa_setup"),
    path("account/security/", security.account_security, name="account_security"),
    path("settings/access/", security.access_index, name="access_index"),
    path("settings/access/roles/new/", security.role_form, name="role_create"),
    path("settings/access/roles/<int:pk>/", security.role_form, name="role_edit"),
    path("settings/access/members/new/", security.member_form, name="member_create"),
    path("settings/access/members/<int:pk>/", security.member_form, name="member_edit"),
    path("logout/", auth.LogoutView.as_view(), name="logout"),
    path("reset/", views.ThrottledPasswordResetView.as_view(), name="password_reset"),
    path("reset/sent/", auth.PasswordResetDoneView.as_view(template_name="registration/reset_done.html"), name="password_reset_done"),
    path("reset/<uidb64>/<token>/", auth.PasswordResetConfirmView.as_view(template_name="registration/reset_confirm.html"), name="password_reset_confirm"),
    path("reset/complete/", auth.PasswordResetCompleteView.as_view(template_name="registration/reset_complete.html"), name="password_reset_complete"),
    path("", views.dashboard, name="dashboard"),
    path("customers/", views.customers, name="customers"),
    path("customers/new/", views.customer_form, name="customer_create"),
    path("customers/export/", views.customer_export, name="customer_export"),
    path("customers/<int:pk>/", views.customer_detail, name="customer_detail"),
    path("customers/<int:pk>/edit/", views.customer_form, name="customer_edit"),
    path("invoices/", views.invoices, name="invoices"),
    path("invoices/new/", views.invoice_create, name="invoice_create"),
    path("invoices/batches/", bulk.batch_list, name="billing_batches"),
    path("invoices/batches/new/", bulk.batch_create, name="billing_batch_create"),
    path("invoices/batches/<uuid:pk>/", bulk.batch_detail, name="billing_batch_detail"),
    path("invoices/batches/<uuid:pk>/confirm/", bulk.batch_confirm, name="billing_batch_confirm"),
    path("invoices/batches/<uuid:pk>/cancel/", bulk.batch_cancel, name="billing_batch_cancel"),
    path("invoices/<int:pk>/", views.invoice_detail, name="invoice_detail"),
    path("invoices/<int:pk>/pdf/", views.invoice_pdf, name="invoice_pdf"),
    path("invoices/<int:pk>/pay/", views.payment_create, name="payment_create"),
    path("payments/<int:pk>/reverse/", views.payment_reverse, name="payment_reverse"),
    path("network/", views.network, name="network"),
    path("settings/billing/", views.billing_settings, name="billing_settings"),
    path("audit/", views.audit_log, name="audit"),
    path("portal/", views.portal, name="portal"),
]
