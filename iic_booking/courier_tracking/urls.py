from django.urls import path

from . import views

app_name = "courier_tracking"

# Mounted at /api/v1/courier-tracking/. Courier details are entered by hand; there is no provider lookup or webhook.
urlpatterns = [
    path("couriers/", views.couriers, name="couriers"),
    path("bookings/<int:booking_id>/arrival/", views.booking_arrival, name="booking-arrival"),
    path("shipments/", views.shipments, name="shipments"),
    path("shipments/<int:pk>/", views.shipment_detail, name="shipment-detail"),
]
