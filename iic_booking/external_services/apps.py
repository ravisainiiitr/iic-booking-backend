from django.apps import AppConfig


class ExternalServicesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "iic_booking.external_services"
    label = "external_services"
    verbose_name = "External user services"
