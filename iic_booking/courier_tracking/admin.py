from django.contrib import admin

from .models import (
    Courier,
    CourierTrackingAlert,
    SampleArrival,
    ShipmentNotification,
    ShipmentReminder,
    TrackingLookup,
    TrackingQuotaMonth,
    TrackingWebhookEvent,
)


class ReadOnlyAdmin(admin.ModelAdmin):
    """Ledgers are written by the application only."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SampleArrival)
class SampleArrivalAdmin(ReadOnlyAdmin):
    list_display = ("id", "booking", "arrival_mode", "courier_slug", "status", "is_active", "last_lookup_success_at", "created_at")
    list_filter = ("arrival_mode", "status", "is_active", "courier_slug")
    search_fields = ("booking__booking_id", "booking__virtual_booking_id", "tracking_number")
    raw_id_fields = ("booking", "user", "equipment", "department")


@admin.register(TrackingLookup)
class TrackingLookupAdmin(ReadOnlyAdmin):
    list_display = ("reserved_at", "period", "trigger", "courier_slug", "outcome", "provider_error_code", "consumed", "provider_used", "provider_quota")
    list_filter = ("period", "trigger", "outcome")


@admin.register(TrackingQuotaMonth)
class TrackingQuotaMonthAdmin(ReadOnlyAdmin):
    list_display = ("period", "local_consumed", "provider_used", "provider_quota", "provider_plan", "in_flight", "blocked_outcome")


@admin.register(TrackingWebhookEvent)
class TrackingWebhookEventAdmin(ReadOnlyAdmin):
    list_display = ("received_at", "event", "mode", "courier_slug", "status", "matched", "deliveries")
    list_filter = ("status", "event", "mode")
    exclude = ("payload",)


@admin.register(ShipmentNotification)
class ShipmentNotificationAdmin(ReadOnlyAdmin):
    list_display = ("created_at", "arrival", "kind", "state_key", "recipient", "channel", "status", "attempts")
    list_filter = ("kind", "channel", "status")


@admin.register(ShipmentReminder)
class ShipmentReminderAdmin(ReadOnlyAdmin):
    list_display = ("created_at", "booking", "kind", "sequence", "recipient", "status")
    list_filter = ("kind", "status")


@admin.register(CourierTrackingAlert)
class CourierTrackingAlertAdmin(ReadOnlyAdmin):
    list_display = ("created_at", "kind", "message", "resolved_at")
    list_filter = ("kind",)


@admin.register(Courier)
class CourierAdmin(ReadOnlyAdmin):
    list_display = ("name", "slug", "is_active", "last_seen_at")
    search_fields = ("name", "slug")
