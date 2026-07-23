from django.contrib import admin

from .models import Payment, PaymentOrder, PaymentRefund, PaymentSettlement


@admin.register(PaymentOrder)
class PaymentOrderAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "razorpay_order_id",
        "purpose",
        "status",
        "total_amount",
        "user",
        "booking",
        "created_at",
    )
    list_filter = ("purpose", "status")
    search_fields = ("razorpay_order_id", "receipt", "idempotency_key")
    raw_id_fields = ("user", "booking", "wallet", "department")


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "razorpay_payment_id",
        "payment_order",
        "amount",
        "status",
        "method",
        "verified_via",
        "created_at",
    )
    list_filter = ("status", "verified_via", "method")
    search_fields = ("razorpay_payment_id", "gateway_reference", "customer_txn_ref")
    raw_id_fields = ("payment_order",)


@admin.register(PaymentRefund)
class PaymentRefundAdmin(admin.ModelAdmin):
    list_display = ("id", "razorpay_refund_id", "payment", "amount", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("razorpay_refund_id",)
    raw_id_fields = ("payment", "initiated_by")


@admin.register(PaymentSettlement)
class PaymentSettlementAdmin(admin.ModelAdmin):
    list_display = ("id", "settlement_id", "bank_utr", "amount", "settled_on", "created_at")
    search_fields = ("settlement_id", "bank_utr")
    filter_horizontal = ("payments",)
