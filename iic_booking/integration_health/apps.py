from django.apps import AppConfig


class IntegrationHealthConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "iic_booking.integration_health"
    label = "integration_health"
    verbose_name = "Integration health"
