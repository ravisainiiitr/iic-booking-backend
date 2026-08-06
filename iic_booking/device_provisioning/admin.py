"""Django admin for Device Provisioning."""

from django.contrib import admin

from iic_booking.device_provisioning.models import (
    DepartmentProvisioningPolicy,
    DeviceAssignment,
    DeviceAuditLog,
    DeviceBootstrapToken,
    DeviceCertificate,
    DeviceHeartbeat,
    DeviceInventory,
    DevicePolicy,
    ProvisionedDevice,
    ProvisioningSession,
)


@admin.register(ProvisioningSession)
class ProvisioningSessionAdmin(admin.ModelAdmin):
    list_display = ("id", "device_type", "status", "hostname", "display_name", "created_at", "expires_at")
    list_filter = ("device_type", "status")
    search_fields = ("hostname", "machine_guid", "fingerprint", "display_name")
    readonly_fields = ("session_proof_hash", "session_proof_prefix", "created_at", "updated_at")


@admin.register(ProvisionedDevice)
class ProvisionedDeviceAdmin(admin.ModelAdmin):
    list_display = ("id", "device_type", "lifecycle", "display_name", "hostname", "updated_at")
    list_filter = ("device_type", "lifecycle")
    search_fields = ("hostname", "machine_guid", "fingerprint", "display_name")
    readonly_fields = ("access_token_hash", "access_token_prefix", "created_at", "updated_at")


@admin.register(DeviceBootstrapToken)
class DeviceBootstrapTokenAdmin(admin.ModelAdmin):
    list_display = ("id", "session", "device", "token_prefix", "expires_at", "used_at")
    readonly_fields = ("token_hash", "token_prefix", "created_at")


@admin.register(DeviceAssignment)
class DeviceAssignmentAdmin(admin.ModelAdmin):
    list_display = ("device", "department", "equipment", "workstation_role")


@admin.register(DevicePolicy)
class DevicePolicyAdmin(admin.ModelAdmin):
    list_display = ("name", "device_type", "version", "is_active", "updated_at")


@admin.register(DeviceAuditLog)
class DeviceAuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "action", "device", "session", "actor", "message")
    list_filter = ("action",)
    readonly_fields = ("created_at",)


@admin.register(DepartmentProvisioningPolicy)
class DepartmentProvisioningPolicyAdmin(admin.ModelAdmin):
    list_display = (
        "department",
        "provisioning_mode",
        "require_mfa",
        "require_device_fingerprint",
        "audit_enabled",
        "updated_at",
    )
    list_filter = ("provisioning_mode", "require_mfa", "audit_enabled")


admin.site.register(DeviceCertificate)
admin.site.register(DeviceInventory)
admin.site.register(DeviceHeartbeat)
