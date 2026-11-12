from django.contrib import admin

from .models import Payment, PaymentIssue, PaymentOrder, PaymentRefund, PaymentSettlement, RazorpayWebhookEvent
from .models import ReceiptTemplate, ReceiptTemplateChange


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


@admin.register(RazorpayWebhookEvent)
class RazorpayWebhookEventAdmin(admin.ModelAdmin):
    list_display = ("received_at", "event_type", "event_id", "razorpay_order_id", "razorpay_payment_id", "status", "attempts")
    list_filter = ("event_type", "status")
    search_fields = ("event_id", "razorpay_order_id", "razorpay_payment_id")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PaymentIssue)
class PaymentIssueAdmin(admin.ModelAdmin):
    list_display = ("raised_at", "kind", "razorpay_payment_id", "amount", "mode", "status")
    list_filter = ("kind", "status", "mode")
    search_fields = ("razorpay_order_id", "razorpay_payment_id", "key")
    readonly_fields = [f.name for f in PaymentIssue._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False




@admin.register(ReceiptTemplate)
class ReceiptTemplateAdmin(admin.ModelAdmin):
    list_display = ("id", "updated_at", "updated_by")
    readonly_fields = ("updated_at", "updated_by")


@admin.register(ReceiptTemplateChange)
class ReceiptTemplateChangeAdmin(admin.ModelAdmin):
    list_display = ("changed_at", "changed_by")
    readonly_fields = ("changed_at", "changed_by", "changes")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
