from django.urls import path

from iic_booking.users.api import wallet_payment_modes_views as views

urlpatterns = [
    path("admin/wallet-payment-modes/", views.admin_wallet_payment_modes_overview, name="admin-wallet-payment-modes"),
    path(
        "admin/wallet-payment-modes/departments/",
        views.admin_wallet_payment_modes_departments,
        name="admin-wallet-payment-modes-departments",
    ),
    path(
        "admin/wallet-payment-modes/recipients/",
        views.admin_wallet_payment_modes_recipients,
        name="admin-wallet-payment-modes-recipients",
    ),
    path(
        "admin/wallet-payment-modes/recipients/preview/",
        views.admin_wallet_payment_modes_recipients_preview,
        name="admin-wallet-payment-modes-recipients-preview",
    ),
    path("admin/wallet-payment-modes/audit/", views.admin_wallet_payment_modes_audit, name="admin-wallet-payment-modes-audit"),
    path(
        "admin/wallet-payment-modes/user-search/",
        views.admin_wallet_payment_modes_user_search,
        name="admin-wallet-payment-modes-user-search",
    ),
    path("admin/wallet-direct-recharge/grants/", views.admin_direct_recharge_grants, name="admin-wallet-direct-recharge-grants"),
    path(
        "admin/wallet-direct-recharge/grants/<int:grant_id>/revoke/",
        views.admin_direct_recharge_grant_revoke,
        name="admin-wallet-direct-recharge-grant-revoke",
    ),
    path("wallet/direct-recharge/access/", views.direct_recharge_access_view, name="wallet-direct-recharge-access"),
    path("wallet/direct-recharge/wallets/", views.direct_recharge_wallet_search, name="wallet-direct-recharge-wallets"),
    path("wallet/direct-recharge/preview/", views.direct_recharge_preview, name="wallet-direct-recharge-preview"),
    path("wallet/direct-recharge/history/", views.direct_recharge_history, name="wallet-direct-recharge-history"),
    path("wallet/direct-recharge/", views.direct_recharge_create, name="wallet-direct-recharge"),
]
