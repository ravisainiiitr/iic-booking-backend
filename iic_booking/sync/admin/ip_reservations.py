"""Admin for soft IP reservation mirror."""

from django.contrib import admin

from iic_booking.sync.models import EquipmentPcIpReservation


@admin.register(EquipmentPcIpReservation)
class EquipmentPcIpReservationAdmin(admin.ModelAdmin):
    list_display = (
        "mac_address",
        "computer_name",
        "preferred_ip",
        "observed_ip",
        "network_mode",
        "status",
        "equipment",
        "last_seen",
    )
    list_filter = ("status", "network_mode")
    search_fields = ("mac_address", "computer_name", "preferred_ip", "observed_ip")
