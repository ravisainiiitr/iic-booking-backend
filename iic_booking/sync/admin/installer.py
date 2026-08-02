"""Django admin for DSA installer releases."""

from django.contrib import admin

from iic_booking.sync.installer.models import DsaInstallerRelease


@admin.register(DsaInstallerRelease)
class DsaInstallerReleaseAdmin(admin.ModelAdmin):
    list_display = (
        "version",
        "channel",
        "release_date",
        "signature_status",
        "is_latest",
        "is_active",
        "download_size_bytes",
    )
    list_filter = ("channel", "signature_status", "is_latest", "is_active")
    search_fields = ("version", "sha256", "original_name")
    readonly_fields = ("sha256", "created_at", "updated_at")
