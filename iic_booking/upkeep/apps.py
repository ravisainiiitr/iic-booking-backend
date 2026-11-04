from django.apps import AppConfig


class UpkeepConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "iic_booking.upkeep"
    verbose_name = "Equipment upkeep (maintenance schedule, uptime, early warnings)"
