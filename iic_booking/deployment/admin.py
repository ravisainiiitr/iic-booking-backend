"""Admin for Deployment Center models."""

from django.contrib import admin

from iic_booking.deployment.models import EquipmentPcWizardRelease


@admin.register(EquipmentPcWizardRelease)
class EquipmentPcWizardReleaseAdmin(admin.ModelAdmin):
    list_display = (
        "version",
        "build_number",
        "channel",
        "release_date",
        "signature_status",
        "is_latest",
        "is_active",
        "download_count",
    )
    list_filter = ("channel", "signature_status", "is_latest", "is_active")
    search_fields = ("version", "build_number", "release_notes", "sha256")
    readonly_fields = ("sha256", "download_count", "created_at", "updated_at")
