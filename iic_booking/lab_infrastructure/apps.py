"""Laboratory Infrastructure — enterprise fleet aggregation (Phase 2)."""

from django.apps import AppConfig


class LabInfrastructureConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "iic_booking.lab_infrastructure"
    verbose_name = "Laboratory Infrastructure"

    def ready(self) -> None:
        from iic_booking.lab_infrastructure import signals  # noqa: F401
