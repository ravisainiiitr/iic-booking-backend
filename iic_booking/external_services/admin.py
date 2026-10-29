from django.contrib import admin

from . import models as m


class _ReadOnly(admin.ModelAdmin):
    """Changed only through the audited APIs; Django admin shows these records read-only."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(m.ExternalBookingWindowConfig)
class ExternalBookingWindowConfigAdmin(_ReadOnly):
    list_display = ("enabled", "initial_open_at", "first_week_start", "initial_weeks", "auto_expand", "updated_at")


@admin.register(m.ExternalBookingWindowChange)
class ExternalBookingWindowChangeAdmin(_ReadOnly):
    list_display = ("created_at", "action", "actor", "reason")
    list_filter = ("action",)


@admin.register(m.ExternalBookingWindowOpening)
class ExternalBookingWindowOpeningAdmin(_ReadOnly):
    list_display = ("week_start", "recorded_at", "slots_prepared_at", "quota_prepared_at")


@admin.register(m.ExternalServicesSettings)
class ExternalServicesSettingsAdmin(_ReadOnly):
    list_display = ("special_requests_enabled", "invoice_requests_enabled", "sric_dispatch_mode", "updated_at")


@admin.register(m.ExternalServicesSettingsChange)
class ExternalServicesSettingsChangeAdmin(_ReadOnly):
    list_display = ("created_at", "actor", "reason")


@admin.register(m.PolicyText)
class PolicyTextAdmin(_ReadOnly):
    list_display = ("key", "version", "approved", "created_at")
    list_filter = ("key", "approved")


@admin.register(m.SpecialRequestDetail)
class SpecialRequestDetailAdmin(_ReadOnly):
    list_display = ("request", "preferred_start", "short_notice", "created_at")


@admin.register(m.SpecialRequestEvent)
class SpecialRequestEventAdmin(_ReadOnly):
    list_display = ("created_at", "request", "action", "actor")
    list_filter = ("action",)


@admin.register(m.SpecialAllocation)
class SpecialAllocationAdmin(_ReadOnly):
    list_display = ("request", "booking", "state", "payment_deadline_at", "paid_at", "expired_at", "late_payment_at")
    list_filter = ("state",)


@admin.register(m.ExternalNotification)
class ExternalNotificationAdmin(_ReadOnly):
    list_display = ("created_at", "key", "recipient", "channel", "status", "attempts")
    list_filter = ("status", "channel")


@admin.register(m.SricInvoiceRequest)
class SricInvoiceRequestAdmin(_ReadOnly):
    list_display = ("booking", "trigger", "status", "attempts", "sent_at", "created_at")
    list_filter = ("status", "trigger")
    exclude = ("snapshot",)  # billing details: shown in the Accounts In Charge screen, not here


@admin.register(m.SricInvoiceBatch)
class SricInvoiceBatchAdmin(_ReadOnly):
    list_display = ("batch_date", "mode", "requests_count", "opened_at")


@admin.register(m.SricDispatchAttempt)
class SricDispatchAttemptAdmin(_ReadOnly):
    list_display = ("created_at", "request", "outcome", "detail")


@admin.register(m.OfficialInvoiceDocument)
class OfficialInvoiceDocumentAdmin(_ReadOnly):
    list_display = ("request", "invoice_number", "is_current", "uploaded_by", "uploaded_at")
    exclude = ("file",)  # downloaded only through the authenticated endpoint


@admin.register(m.PhysicalCopyRequest)
class PhysicalCopyRequestAdmin(_ReadOnly):
    list_display = ("invoice_request", "status", "dispatch_date", "speed_post_reference", "created_at")
    list_filter = ("status",)
    exclude = ("address_snapshot",)


@admin.register(m.SampleShipmentLabel)
class SampleShipmentLabelAdmin(_ReadOnly):
    list_display = ("reference", "booking", "sample_count", "created_at", "revoked_at")
    exclude = ("token_sha256", "sender_snapshot")


@admin.register(m.SampleReceiptScan)
class SampleReceiptScanAdmin(_ReadOnly):
    list_display = ("created_at", "label", "staff", "action", "trace_status")


@admin.register(m.ExternalBookingCheckout)
class ExternalBookingCheckoutAdmin(_ReadOnly):
    """``paid_not_confirmed`` rows are captured payments whose booking could not be made: refund under existing rules."""

    list_display = ("created_at", "user", "equipment", "state", "choice", "total_amount", "wallet_amount",
                    "gateway_amount", "booking", "payment_order")
    list_filter = ("state", "choice")
    search_fields = ("public_id", "user__email", "payment_order__razorpay_order_id")
