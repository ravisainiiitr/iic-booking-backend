from django.apps import AppConfig


class PowerConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "iic_booking.power"
    verbose_name = "UPS health monitoring"
