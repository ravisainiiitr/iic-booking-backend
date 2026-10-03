"""Admin for Deployment Center models."""

from django.contrib import admin

from iic_booking.deployment.models import EquipmentPcWizardRelease, MobileAppRelease, MobileAppSettings


@admin.register(MobileAppRelease)
class MobileAppReleaseAdmin(admin.ModelAdmin):
    list_display = ("platform", "version_name", "version_code", "release_date", "is_latest", "is_active", "download_count")
    list_filter = ("platform", "is_latest", "is_active")
    search_fields = ("version_name", "release_notes", "sha256")
    readonly_fields = ("sha256", "download_size_bytes", "download_count", "created_at", "updated_at")


@admin.register(MobileAppSettings)
class MobileAppSettingsAdmin(admin.ModelAdmin):
    list_display = ("__str__", "updated_at", "updated_by")
    readonly_fields = ("updated_at", "updated_by")

    def has_add_permission(self, request):
        return not MobileAppSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        from iic_booking.deployment.mobile_app import invalidate_audience_cache

        obj.updated_by = request.user
        super().save_model(request, obj, form, change)
        invalidate_audience_cache()


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
