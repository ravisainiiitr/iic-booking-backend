from django.urls import path

from . import views

app_name = "courier_tracking"

# Mounted at /api/v1/courier-tracking/.
urlpatterns = [
    path("couriers/", views.couriers, name="couriers"),
    path("bookings/<int:booking_id>/arrival/", views.booking_arrival, name="booking-arrival"),
    path("shipments/", views.shipments, name="shipments"),
    path("shipments/<int:pk>/", views.shipment_detail, name="shipment-detail"),
    path("shipments/<int:pk>/refresh/", views.shipment_refresh, name="shipment-refresh"),
    path("admin/overview/", views.admin_overview, name="admin-overview"),
    path("admin/settings/", views.admin_settings, name="admin-settings"),
    path("admin/alerts/<int:pk>/resolve/", views.admin_resolve_alert, name="admin-resolve-alert"),
    path("admin/clear-block/", views.admin_clear_block, name="admin-clear-block"),
    path("admin/refresh-catalog/", views.admin_refresh_catalog, name="admin-refresh-catalog"),
    path("webhooks/trackcourier/", views.trackcourier_webhook, name="webhook"),
]
