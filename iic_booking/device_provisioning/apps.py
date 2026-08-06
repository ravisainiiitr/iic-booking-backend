"""Unified Device Provisioning — portal-authoritative device lifecycle (Phase R.2.1+)."""

from django.apps import AppConfig


class DeviceProvisioningConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "iic_booking.device_provisioning"
    verbose_name = "Device Provisioning"

    def ready(self):
        # Register Department post_save → Trusted Auto-Approve default policy.
        from iic_booking.device_provisioning import policy  # noqa: F401
