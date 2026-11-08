from django.urls import path

from iic_booking.payments import views

urlpatterns = [
    path("razorpay/create-order/", views.razorpay_create_order, name="payments-razorpay-create-order"),
    path("razorpay/verify/", views.razorpay_verify, name="payments-razorpay-verify"),
    path("razorpay/webhook/", views.razorpay_webhook, name="payments-razorpay-webhook"),
    path("razorpay/orders/<int:order_id>/", views.razorpay_order_detail, name="payments-razorpay-order-detail"),
    path("razorpay/refund/", views.razorpay_refund, name="payments-razorpay-refund"),
    path("fee-settings/", views.fee_settings, name="payments-fee-settings"),
    path("receipts/<str:razorpay_payment_id>/", views.payment_receipt, name="payments-receipt"),
    path("booking-preview/<int:booking_id>/", views.booking_payment_preview, name="payments-booking-preview"),
    path("issues/", views.payment_issues, name="payments-issues"),
    path("reconciliation/", views.reconciliation_summary, name="payments-reconciliation"),
    path("issues/<int:issue_id>/resolve/", views.payment_issue_resolve, name="payments-issue-resolve"),
]
