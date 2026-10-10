from django.apps import AppConfig


class FacilityGroupsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "iic_booking.facility_groups"
    label = "facility_groups"
    verbose_name = "Facility user groups"

    def ready(self):
        from . import signals  # noqa: F401
