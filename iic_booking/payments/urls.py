from django.urls import path

from iic_booking.payments import views

urlpatterns = [
    path("razorpay/create-order/", views.razorpay_create_order, name="payments-razorpay-create-order"),
    path("razorpay/verify/", views.razorpay_verify, name="payments-razorpay-verify"),
    path("razorpay/webhook/", views.razorpay_webhook, name="payments-razorpay-webhook"),
    path("razorpay/orders/<int:order_id>/", views.razorpay_order_detail, name="payments-razorpay-order-detail"),
    path("razorpay/refund/", views.razorpay_refund, name="payments-razorpay-refund"),
    path("fee-settings/", views.fee_settings, name="payments-fee-settings"),
]
