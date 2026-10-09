from django.urls import path

from iic_booking.users.api import admin_wallet_ledger_views as views

urlpatterns = [
    path("admin/wallet-ledger/owners/", views.wallet_ledger_owners, name="admin-wallet-ledger-owners"),
    path("admin/wallet-ledger/options/", views.wallet_ledger_options, name="admin-wallet-ledger-options"),
    path(
        "admin/wallet-ledger/owners/<int:owner_id>/",
        views.wallet_ledger_owner_detail,
        name="admin-wallet-ledger-owner-detail",
    ),
    path("admin/wallet-ledger/transactions/", views.wallet_ledger_transactions, name="admin-wallet-ledger-transactions"),
    path(
        "admin/wallet-ledger/linked-students/",
        views.wallet_ledger_linked_students,
        name="admin-wallet-ledger-linked-students",
    ),
    path(
        "admin/wallet-ledger/adjustments/preview/",
        views.wallet_ledger_adjustment_preview,
        name="admin-wallet-ledger-adjustment-preview",
    ),
    path("admin/wallet-ledger/adjustments/", views.wallet_ledger_adjustment_create, name="admin-wallet-ledger-adjustments"),
]
