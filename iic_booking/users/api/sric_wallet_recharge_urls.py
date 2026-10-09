from django.urls import path

from iic_booking.users.api import sric_wallet_recharge_views as views

urlpatterns = [
    path("wallet/sric-recharges/", views.my_sric_recharges, name="wallet-sric-recharges"),
    path("wallet/sric-recharges/refresh/", views.refresh_my_sric_recharges, name="wallet-sric-recharges-refresh"),
    path("admin/sric-wallet-recharges/", views.admin_list, name="admin-sric-wallet-recharges"),
    path("admin/sric-wallet-recharges/refresh/", views.admin_refresh, name="admin-sric-wallet-recharges-refresh"),
    path("admin/sric-wallet-recharges/credit-ready/", views.admin_credit_ready, name="admin-sric-wallet-recharges-credit-ready"),
    path("admin/sric-wallet-recharges/user-lookup/", views.admin_user_lookup, name="admin-sric-wallet-recharges-user-lookup"),
    path("admin/sric-wallet-recharges/settings/", views.admin_settings, name="admin-sric-wallet-recharges-settings"),
    path("admin/sric-wallet-recharges/mappings/", views.admin_mappings, name="admin-sric-wallet-recharges-mappings"),
    path("admin/sric-wallet-recharges/<int:row_id>/credit/", views.admin_credit, name="admin-sric-wallet-recharges-credit"),
    path("admin/sric-wallet-recharges/<int:row_id>/reject/", views.admin_reject, name="admin-sric-wallet-recharges-reject"),
    path("admin/sric-wallet-recharges/<int:row_id>/verify/", views.admin_verify, name="admin-sric-wallet-recharges-verify"),
]
