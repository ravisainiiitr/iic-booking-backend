from django.contrib import admin
from django.core.exceptions import ValidationError
from django import forms

from .models import UpsDevice, UpsReading
from .register_maps import validate_points


class UpsDeviceForm(forms.ModelForm):
    class Meta:
        model = UpsDevice
        fields = "__all__"

    def clean(self):
        data = super().clean()
        if data.get("register_map") == "custom":
            problems = validate_points(data.get("custom_map"))
            if problems:
                raise ValidationError({"custom_map": problems})
        return data


@admin.register(UpsDevice)
class UpsDeviceAdmin(admin.ModelAdmin):
    form = UpsDeviceForm
    list_display = ("name", "manufacturer", "model", "host", "port", "unit_id", "sync_agent", "map_verified", "enabled")
    list_filter = ("enabled", "map_verified", "manufacturer")
    search_fields = ("name", "host", "model")
    filter_horizontal = ("equipment",)


@admin.register(UpsReading)
class UpsReadingAdmin(admin.ModelAdmin):
    list_display = ("device", "read_at", "reachable", "load_percent", "battery_percent", "on_battery", "fault")
    list_filter = ("reachable", "on_battery", "device")
    date_hierarchy = "read_at"
